from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone

from aiohttp import WSCloseCode

from . import billing, napcat
from .config import api_key, load
from .llm import LLM
from .media import MediaWorker
from .models import Msg, parse_onebot_event
from .prefs import skip_reason
from .store import Store
from .summarize import Summarizer

log = logging.getLogger("msgx.pipeline")
SHANGHAI = timezone(timedelta(hours=8))


class Pipeline:
    """消息总线：接入 → 入库 → 本地 OCR；图片语义和摘要由用户手动发起。"""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load()
        self.store = Store(self._db_path())
        self.llm = LLM(self.cfg, self.store)
        self.llm.configure(api_key(self.cfg))
        self.media = MediaWorker(self.cfg, self.store, self.llm)
        self.summarizer = Summarizer(self.cfg, self.store, self.llm)

        # 老库的历史金额回填（幂等、一次性）。放在这里而不是 start()：`msgx stats`
        # 这类只读命令不会调 start()，但同样要显示「累计花费」，所以两条路都得走到。
        self.llm.backfill_legacy_cost()

        self.watch = {str(g) for g in self.cfg["watch"].get("groups", [])}
        self.names = {str(k): str(v) for k, v in (self.cfg["watch"].get("names") or {}).items()}

        # 接入队列：WebSocket 回调只做入队（不能阻塞协议端），由 _ingest_loop 串行消费。
        self.ingest_q: asyncio.Queue[dict] = asyncio.Queue(maxsize=5000)
        # 当前连入的协议端（NapCat）地址集合。由接入层维护，供 /health 展示，
        # 让界面能明确区分「服务在跑」和「NapCat 已连上」——这两件事经常被混淆。
        self.ws_peers: set[str] = set()
        self.ws_connected_at = 0.0  # v0.5.7：GUI 用新连接时间区分扫码成功与重置前旧连接
        self.ws_disconnected_at = 0.0
        self.ws_last_close = ""
        self.last_protocol_message: dict = {}
        self._receive_warning: dict = {}
        self._recovery_attempt_at = 0.0
        self.ws_online: dict = {}
        self.ws_addresses: dict = {}
        self._closing_ws = False
        self._protocol_checked = 0.0
        self._protocol_failure: dict = {}
        # 活着的 WebSocket 连接对象。**退出时必须主动关掉它们**：
        # aiohttp 的 AppRunner.cleanup() 会等待所有活动连接处理完毕，而反向 WS 的
        # 处理器是 `async for raw in ws`，只要 NapCat 不断线就永远不返回。
        # 症状：NapCat 没连入时 0.6s 退干净；NapCat 连着时要卡到心跳超时（实测 39s，
        # 日志里表现为等 15s 没收到 PONG 才报 WS 错误）。界面「停止服务」会一直
        # 停在「正在退出中…」。
        self.ws_conns: set = set()
        # 反向 WS 上的 OneBot API 调用：请求沿连入的那条连接下行，响应带 echo 上行。
        # 这是未配置 NapCat HTTP API 时调 get_group_list（拿真实群名）的唯一通道。
        self._action_futs: dict[str, asyncio.Future] = {}
        self._echo_seq = 0
        # 当前协议端登录的 QQ 号（群属主）。连上后查一次就缓存，见 login_uin()。
        self.bot_uin = ""
        self.target_account = str((self.cfg.get("napcat") or {}).get("account") or "").strip()
        self._peer_watch: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        # 账单快照缓存。界面每 2 秒轮询 /health，绝不能每次都打网络（1 次余额 + N 次
        # 逐月消费）。所以：/health 只读缓存，真正的抓取由 _billing_loop 与 /billing 触发。
        self._billing_ts = 0.0
        self._billing_data: dict = {}
        self._billing_lock: asyncio.Lock | None = None
        self.daily_schedule = {"state": "idle", "message": "", "day": ""}
        self.started_at = time.time()
        self.counters = {
            "received": 0,
            "stored": 0,
            "duplicate": 0,
            "skipped": 0,
            "filtered": 0,
        }

    def _db_path(self) -> str:
        from .config import abs_path

        return str(abs_path(self.cfg, "db_path"))

    def group_name(self, group_id: str) -> str:
        return self.names.get(str(group_id), "")

    # ---------- lifecycle ----------

    async def start(self) -> None:
        log.info("LLM 可用: %s | 预算上限 %.2f 元/日", self.llm.available, self.cfg["llm"]["max_daily_cost_cny"])
        if not self.llm.available:
            log.warning("未配置当前 API 的密钥，仅接收和存储消息，不调用模型")
        self._tasks = [
            # ingest 必须在这里启动：_ws 回调只往 ingest_q 里塞事件，
            # 少了这个消费者，消息会全部堆在队列里（上限 5000）然后在进程退出时静默丢失，
            # 表现为「服务日志显示协议端已连接，但数据库里一条消息都没有」。
            asyncio.create_task(self._ingest_loop(), name="ingest"),
            asyncio.create_task(self._media_loop(), name="media"),
            asyncio.create_task(self._prune_loop(), name="prune"),
            asyncio.create_task(self._billing_loop(), name="billing"),
            asyncio.create_task(self._watchdog_loop(), name="watchdog"),
            asyncio.create_task(self._daily_summary_loop(), name="daily-summary"),
        ]
        # v0.4.0 的下载缺陷误伤了一批图片（标记 failed）。启动时重置一次：
        # URL 仍有效即被挽回，已过期则再次 failed，不会死循环。
        n = self.store.requeue_failed_media()
        if n:
            log.info("已把 %d 张上次下载失败的图片重新排入处理队列", n)
    # ---------- OneBot API（反向 WS 通道） ----------

    def resolve_action(self, resp: dict) -> None:
        """WS 接入层收到 API 响应（带 echo、无 post_type）时转交到这里。"""
        echo = str(resp.get("echo") or "")
        fut = self._action_futs.pop(echo, None)
        if fut is not None and not fut.done():
            fut.set_result(resp)

    async def call_action(self, action: str, params: dict | None = None, timeout: float = 8.0):
        """通过反向 WS 向协议端（NapCat）发起 OneBot API 调用，返回 data 字段。"""
        live = [ws for ws in self.ws_conns if not ws.closed]
        if not live:
            raise RuntimeError("协议端未连接")
        self._echo_seq += 1
        echo = f"msgx-{self._echo_seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._action_futs[echo] = fut
        try:
            await live[0].send_str(json.dumps({"action": action, "params": params or {}, "echo": echo}))
            resp = await asyncio.wait_for(fut, timeout)
        finally:
            self._action_futs.pop(echo, None)
        if not isinstance(resp, dict) or resp.get("retcode") != 0:
            raise RuntimeError(f"{action} 调用失败: {resp!r}")
        return resp.get("data")

    async def refresh_group_names(self, prune: bool = False) -> dict:
        """从协议端拉群列表，把真实群名 + **属主账号**写进 groups 表。

        groups 表的 name 此前只能靠 [watch] names 手填，没人填就永远是
        「(未命名) 754842541」—— NapCat 就在线上，没理由不去问它。

        ``prune=True`` 时顺手删掉「不属于当前账号且没有消息」的群 —— 那就是
        「只关注目前已登录账号的群」。默认 False：自动同步路径不能有副作用，
        删数据必须由用户显式点一下（界面的「只保留当前账号的群」）。
        """
        uin = await self.login_uin()
        if self.target_account and uin != self.target_account:
            raise RuntimeError("协议端登录账号与设置的目标 QQ 不一致，请核对账号")
        data = await self.call_action("get_group_list", {}, 8.0)
        keep: set[str] = set()
        for g in data or []:
            gid = str((g or {}).get("group_id") or "")
            if gid:
                keep.add(gid)
                self.store.upsert_group(gid, str((g or {}).get("group_name") or ""), uin)
        log.info("已从协议端拉取群列表：%d 个群（账号 %s）", len(keep), uin or "未知")
        out = {"count": len(keep), "uin": uin, "deleted": [], "protected": []}
        if prune and keep:
            deleted, protected = self.store.prune_groups(keep)
            out["deleted"], out["protected"] = deleted, protected
            if deleted or protected:
                log.info("按当前账号清理群列表：删除 %d 个，保留(有消息) %d 个",
                         len(deleted), len(protected))
        return out

    async def login_uin(self) -> str:
        """当前协议端登录的 QQ 号（拿不到返回空串，绝不猜）。"""
        if self.bot_uin:
            return self.bot_uin
        try:
            info = await self.call_action("get_login_info", {}, 5.0)
            self.bot_uin = str((info or {}).get("user_id") or "")
        except Exception as e:  # noqa: BLE001
            log.debug("取 get_login_info 失败（群属主将留空）: %s", e)
            self.bot_uin = ""
        return self.bot_uin

    # ---------- 协议端连上 / 断开 ----------

    def on_peer_connected(self) -> None:
        self.ws_connected_at = time.time()
        self._protocol_checked = 0.0
        self._protocol_failure = {}
        t = self._peer_watch
        if t is not None and not t.done():
            t.cancel()
        # 换了一次登录 = 可能换了一个账号，缓存的属主必须作废，否则新账号的群
        # 会被记到旧账号名下（而「属主」正是「只保留当前账号的群」的依据）。
        self.bot_uin = ""
        asyncio.create_task(self._auto_refresh_names())

    async def _auto_refresh_names(self) -> None:
        try:
            await asyncio.sleep(2)  # 等 NapCat 完成初始化
            await self.refresh_group_names()
        except Exception as e:  # noqa: BLE001
            log.debug("自动拉取群列表失败（不影响消息接收）: %s", e)

    def on_peer_disconnected(self) -> None:
        """最后一条连接断开后检查上游原因；不盲目重启 QQ 或创建重复实例。"""
        if self._stop.is_set() or self._closing_ws or self.ws_conns:
            return
        self.ws_disconnected_at = time.time()
        self._protocol_checked = 0.0
        if self._peer_watch is not None and not self._peer_watch.done():
            self._peer_watch.cancel()

        async def _watch() -> None:
            await asyncio.sleep(1)
            if self._stop.is_set() or self.ws_conns:
                return
            status = self.protocol_status()
            if status["state"] == "login_required":
                log.warning("%s；协议日志：%s", status["message"], status["log_path"])
                return
            await asyncio.sleep(59)
            if self._stop.is_set() or self.ws_conns:
                return
            log.warning("协议端已断开超过 60s 且未重连 —— 消息不再入库；%s", self.protocol_status()["message"])

        self._peer_watch = asyncio.create_task(_watch())

    def protocol_status(self) -> dict:
        live = [ws for ws in self.ws_conns if not ws.closed]
        if live and any(self.ws_online.get(ws) is not False for ws in live):
            if self._receive_warning:
                return dict(self._receive_warning)
            return {"state": "connected", "message": "协议端已连接", "since": self.ws_connected_at}
        now = time.monotonic()
        if not self._protocol_checked or now - self._protocol_checked >= 5:
            self._protocol_failure = napcat.login_failure(self.cfg, self.ws_connected_at)
            self._protocol_checked = now
        if self._protocol_failure:
            return dict(self._protocol_failure)
        if live:
            return {"state": "offline", "message": "QQ 心跳报告离线，等待协议端恢复登录", "since": self.ws_connected_at}
        return {"state": "disconnected", "since": self.ws_disconnected_at,
                "message": "等待 NapCat 自动重连；若持续未恢复，请查看协议端日志",
                "last_close": self.ws_last_close}

    async def close_ws_conns(self) -> int:
        """主动关闭所有反向 WS 连接，返回关闭条数。

        必须在 runner.cleanup() 之前调用，否则 cleanup 会一直等 WS 处理器返回。
        用 GOING_AWAY 告知对端是「服务要关了」，NapCat 收到后会自行重连。
        """
        self._closing_ws = True
        conns = list(self.ws_conns)
        for ws in conns:
            with contextlib.suppress(Exception):
                await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutdown")
        # 给 aiohttp 一点时间把关闭帧发出去并回收连接
        for _ in range(20):
            if not self.ws_conns:
                break
            await asyncio.sleep(0.05)
        return len(conns)

    async def stop(self) -> None:
        self._stop.set()
        if self._peer_watch is not None and not self._peer_watch.done():
            self._peer_watch.cancel()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.llm.close()
        await self.media.close()
        self.store.close()

    # ---------- ingest ----------

    def note_protocol_message(self, event: dict) -> None:
        """仅由真实接入路径记录新群消息，过滤消息也可验证接收链路。"""
        if event.get("post_type") != "message" or event.get("message_type") != "group":
            return
        self_id = str(event.get("self_id") or "")
        if self.target_account and self_id != self.target_account:
            return
        if not self_id.isdigit() or self_id == "0" or not event.get("message_id") or not event.get("time"):
            return
        try:
            msg = parse_onebot_event(event, self_id, {self_id})
        except (TypeError, ValueError, AttributeError):
            return
        if msg is None or self.store.has_receipt(self_id, msg.group_id, msg.msg_id):
            return
        self.last_protocol_message = {"received_at": time.time(), "message_time": msg.ts, "self_id": self_id}
        if (self._receive_warning and self_id == self._receive_warning.get("account")
                and msg.ts >= int(max(self.ws_connected_at, self._receive_warning["since"]))):
            self._receive_warning = {}
            log.info("消息接收已恢复：已收到当前账号的新 QQ 群消息")

    async def submit(self, event: dict) -> bool:
        """非阻塞投递事件，由接入层调用。队列满时丢弃并计数，避免阻塞 WebSocket。"""
        try:
            self.ingest_q.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self.counters["skipped"] += 1
            log.warning("接入队列已满，丢弃 1 条事件")
            return False

    async def _ingest_loop(self) -> None:
        while not self._stop.is_set():
            try:
                event = await asyncio.wait_for(self.ingest_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            try:
                await self.handle_event(event)
            except Exception as e:  # noqa: BLE001
                log.warning("事件处理失败: %s", e)

    async def handle_event(self, event: dict) -> dict:
        self.counters["received"] += 1
        self_id = str(event.get("self_id", ""))
        if self.target_account and self_id != self.target_account:
            self.counters["skipped"] += 1
            return {"ok": False, "reason": "非目标 QQ 账号，未入库"}
        # self_id 就是「收到这条消息的账号」，是群属主最直接的证据。
        # 但**不能无脑采信**：本项目的诊断探针（diagnose_freeze）走的就是这条路，
        # 它把 self_id 填成 0 —— 若不设防，探针会把群属主写成 "0"。
        if self_id.isdigit() and self_id != "0" and self.bot_uin != self_id:
            self.bot_uin = self_id
        msg: Msg | None = parse_onebot_event(event, self_id, {self_id})
        if msg is None:
            self.counters["skipped"] += 1
            return {"ok": False, "reason": "非群消息或空消息"}

        if self.watch and msg.group_id not in self.watch:
            self.counters["skipped"] += 1
            return {"ok": False, "reason": f"群 {msg.group_id} 不在监听列表"}

        # 过滤的图片仍下载并本地 OCR，供用户在实时页查看和手动选择语义解析。
        # 默认全部关闭，因此默认行为与不启用过滤时完全一致。
        reason = skip_reason(msg, self.cfg.get("filters") or {})
        if reason:
            self.counters["filtered"] += 1
            # 过滤记录默认不进入摘要；保留可下载图片，OCR 不调用模型。
            if reason in ("纯表情", "纯图片"):
                row_id = self.store.insert_message(msg, self.group_name(msg.group_id), self_id if self_id.isdigit() and self_id != "0" else "",
                                                   filtered=True)
                if row_id is None:
                    self.counters["duplicate"] += 1
                    return {"ok": True, "duplicate": True, "filtered": True}
                return {"ok": True, "row_id": row_id, "filtered": True, "reason": reason}
            return {"ok": False, "reason": f"已按过滤规则丢弃: {reason}"}

        name = self.group_name(msg.group_id)
        row_id = self.store.insert_message(msg, name, self_id if self_id.isdigit() and self_id != "0" else "")
        if row_id is None:
            self.counters["duplicate"] += 1
            return {"ok": True, "duplicate": True}

        self.counters["stored"] += 1

        if msg.image_urls:
            # 媒体循环只下载并执行本地 OCR。
            return {"ok": True, "row_id": row_id, "queued_media": True}

        return {"ok": True, "row_id": row_id}

    # ---------- background loops ----------

    async def _media_loop(self) -> None:
        """轮询图片下载与本地 OCR 队列。"""
        while not self._stop.is_set():
            try:
                done = await self.media.process_pending(20)
                if done:
                    continue
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("媒体循环异常: %s", e)
                await asyncio.sleep(5)

    async def _prune_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(6 * 3600)
                r = self.store.prune(
                    int(self.cfg["retention"]["keep_days_raw"]),
                    int(self.cfg["retention"]["keep_days_media"]),
                )
                log.info("清理完成: %s", r)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("清理异常: %s", e)

    # ---------- 看门狗 ----------

    async def _check_receive_health(self) -> None:
        """按当前账号检查停收；一次故障至多尝试恢复一次，真实新消息才解除。"""
        now = time.time()
        if self._receive_warning:
            if (self._receive_warning["state"] == "recovering"
                    and now - self._receive_warning["since"] >= 120):
                self._receive_warning.update(state="stalled", message="自动恢复尚未收到新消息；已停止重复重启，请查看协议端登录状态")
                log.warning(self._receive_warning["message"])
            return
        if not self.bot_uin or not any(not ws.closed for ws in self.ws_conns):
            return
        wg = self.cfg.get("watchdog") or {}
        stale_s = max(300, int(wg.get("stale_minutes", 30)) * 60)
        evidence = self.last_protocol_message
        if (evidence.get("self_id") != self.bot_uin
                or now - max(self.started_at, self.ws_connected_at, evidence.get("received_at", 0)) < stale_s):
            return
        age = napcat.qq_msg_db_age(self.bot_uin)
        if age is None or age < stale_s:
            return
        base = self.store.hour_baseline(datetime.now().hour, int(wg.get("baseline_days", 7)), self.bot_uin)
        if base < int(wg.get("busy_min_messages", 5)):
            return
        self._receive_warning = {"state": "stalled", "since": now, "account": self.bot_uin,
                                 "message": "疑似停收：当前 QQ 账号和服务均长时间未收到消息"}
        log.warning("疑似停收：QQ %s 消息库已 %.0f 分钟未写入，服务同期无新群消息（小时基线 %d 条）",
                    self.bot_uin, age / 60, base)
        if now - self._recovery_attempt_at < 3600:
            self._receive_warning["message"] += "；一小时内已尝试恢复，不再自动重启"
            return
        self._recovery_attempt_at = now
        self._receive_warning.update(state="recovering", message="正在尝试恢复消息接收；等待新消息验证")
        log.warning("正在尝试恢复当前账号的 NapCat 登录进程；仅本次一次，不关闭普通 QQ")
        result = await asyncio.to_thread(napcat.recover_worker, self.cfg, self.bot_uin)
        # 请求期间若已有真实消息恢复，不能把状态覆盖回等待。
        if self._receive_warning:
            self._receive_warning.update(state="recovering" if result["ok"] else "stalled", message=result["note"])
            log.warning(result["note"])

    async def _watchdog_loop(self) -> None:
        """低频检查静默停收；不调用模型，不弹窗或发送通知。"""
        wg = self.cfg.get("watchdog") or {}
        if not wg.get("enabled", True):
            return
        every = max(30, int(wg.get("check_seconds", 300)))
        while not self._stop.is_set():
            try:
                await asyncio.sleep(every)
                await self._check_receive_health()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("看门狗异常: %s", e)

    # ---------- 账单 ----------

    def _billing_start(self, bc: dict) -> tuple[int, int]:
        """平台逐日消费的抓取起点（年, 月）。

        平台接口只支持**按月**查，累计值只能靠逐月累加，所以起点越早请求越多。
        默认取「库里最早有记录的那一天」—— 我们没开始用之前不可能有消费，
        这样既不会漏也不会白问一堆空月份。
        """
        try:
            y, m = int(bc.get("start_year") or 0), int(bc.get("start_month") or 0)
            if 2000 < y < 2100 and 1 <= m <= 12:
                return y, m
        except (TypeError, ValueError):
            pass
        rows = self.store.usage_all()  # ORDER BY day DESC
        if rows:
            try:
                d = date.fromisoformat(str(rows[-1]["day"]))
                return d.year, d.month
            except (ValueError, KeyError, IndexError):
                pass
        t = date.today()
        return t.year, t.month

    async def billing_snapshot(self, refresh: bool = False, min_interval: float = 60.0) -> dict:
        """账单快照：官方余额 + 余额差值推算的真实消费 + 平台逐日消费 + 本地估算。

        **口径优先级**（``source`` 字段）：
          ``platform``      平台明细，带「哪个模型花了多少」，但要有浏览器登录态 token；
          ``balance_diff``  余额差值法 —— 官方余额的下降量累加。**不需要额外凭据**，
                            余额接口本来每次刷新都要查；代价是分辨率只到 ¥0.01、
                            且只能给总额（v0.5.1 新增）；
          ``estimate``      本地按 token 用量估算，是**兜底**，通常偏低。

        为什么必须区分口径：本地只按 token 用量估算，和平台账单对不上是常态
        （重试后服务端已处理、但我们没拿到响应体的那些请求无法计数）。
        用户看到的数字必须是「有出处的」：抓到了就标来源，没抓到就标 estimate
        并把失败原因带上 —— 不许把估算值伪装成真实值。

        三张凭据互不通用（见 billing.py）：余额用 API Key，消费明细用平台 token，
        差值法只要读数历史。任何一路失败都不影响另外两路。
        """
        bc = self.cfg.get("billing") or {}
        cache_s = float(bc.get("cache_seconds", 600) or 600)
        now = time.time()
        age = now - self._billing_ts
        if self._billing_data and (
            (refresh and age < min_interval) or (not refresh and age < cache_s)
        ):
            return self._billing_data

        if self._billing_lock is None:
            self._billing_lock = asyncio.Lock()
        async with self._billing_lock:
            # 等锁期间可能已被别的协程刷过
            age = time.time() - self._billing_ts
            if self._billing_data and age < min_interval:
                return self._billing_data

            est_total_row = self.store.cost_totals()
            est_today = float(self.store.daily_cost_cny(date.today().isoformat()))
            est_total = float(est_total_row.get("cost_cny", 0.0))

            if "api" in self.cfg:
                # 新接入只统计本机请求用量，不把其他平台密钥发送到 DeepSeek 账单接口。
                data = {"ts": int(now), "source": "estimate", "today_source": "estimate",
                        "total_source": "estimate", "today_cny": est_today, "total_cny": est_total,
                        "currency": "CNY", "days": 0,
                        "platform_error": "按请求返回 Token 与配置费率估算；账户余额请查看官方平台",
                        "estimate": {"today_cny": est_today, "total_cny": est_total,
                                     "calls": int(est_total_row.get("calls", 0))},
                        "balance": {"ok": False, "error": "未查询官方余额"}}
                self._billing_ts, self._billing_data = now, data
                return data

            # --- 真实余额（官方接口，用 API Key）---
            token_key = api_key(self.cfg)
            timeout = float(bc.get("timeout_seconds", 20) or 20)
            if token_key:
                try:
                    bal = await billing.fetch_balance(token_key, self.cfg["llm"]["base_url"], timeout)
                except Exception as e:  # noqa: BLE001
                    bal = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            else:
                bal = {"ok": False, "error": "未配置 API Key"}

            # 抓到余额就**落一条读数** —— 这是差值法唯一的原料。
            # 只存不花：不额外发请求（余额本来每次刷新都要查）。
            if bal.get("ok"):
                try:
                    self.store.record_balance(
                        int(time.time()),
                        float(bal.get("total") or 0),
                        float(bal.get("topped_up") or 0),
                        float(bal.get("granted") or 0),
                    )
                except Exception as e:  # noqa: BLE001
                    log.debug("余额读数落库失败（不影响账单显示）: %s", e)

            # --- 真实消费（余额差值法：不需要任何额外凭据）---
            today_str = date.today().isoformat()
            diff_today = diff_total = None
            diff_first_day = ""
            prior_est = 0.0
            try:
                rows = self.store.balance_readings()
                diff_today = billing.daily_from_readings(rows, today_str)
                diff_total = billing.consumption_from_readings(rows)
                if diff_total.get("points"):
                    diff_first_day = time.strftime(
                        "%Y-%m-%d", time.localtime(int(diff_total["from_ts"]))
                    )
                    # 差值看不到起始日之前的历史，用那部分估算把区间补齐。
                    # 否则刚装上的那一天「累计」会显示 ¥0.00 —— 比估算还离谱。
                    if diff_first_day < today_str:
                        prior_est = float(self.store.cost_before(diff_first_day))
            except Exception as e:  # noqa: BLE001
                log.debug("余额差值推算失败: %s", e)

            # --- 真实消费（平台前端接口，用登录态 token）---
            plat_days: dict[str, float] = {}
            plat_currency = "CNY"
            plat_err = ""
            plat_token = str(bc.get("platform_token") or "").strip()
            if not bc.get("platform_enabled", True):
                plat_err = "配置里已关闭平台数据（[billing] platform_enabled = false）"
            elif not plat_token:
                plat_err = "未配置 platform_token（消费明细拿不到；今日/累计已改用余额差值推算）"
            else:
                y, m = self._billing_start(bc)
                try:
                    r = await billing.fetch_platform_cost(plat_token, y, m, timeout)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                if r.get("ok"):
                    plat_days = r.get("days") or {}
                    plat_currency = r.get("currency") or "CNY"
                else:
                    plat_err = str(r.get("error") or "抓取失败")

            today = today_str
            # 口径优先级：平台明细 > 余额差值 > 本地估算。
            # 差值法是「拿官方数字做减法」，比按 token 估算硬得多，所以排在估算前面。
            # 今日与累计**分别**判定：差值法可能已经攒够累计的读数、但今天刚启动
            # 所以今日还不够 —— 两个数字必须各自标自己的口径，不能共用。
            diff_ok_today = bool(
                diff_today and diff_today.get("enough") and diff_today.get("full_day")
            )
            # 「累计」还要求读数跨过了一个午夜（起点日 < 今天）。读数起点就是今天时，
            # 差值只覆盖了今天的一小段，报出来会明显偏小 —— 那种情况下今天先沿用估算，
            # 明天 00:00 之后自然切换到差值口径。
            diff_ok_total = bool(
                diff_total and diff_total.get("enough") and diff_first_day
                and diff_first_day < today_str
            )
            if plat_days:
                today_cny, today_src = float(plat_days.get(today, 0.0)), "platform"
                total_cny, total_src = float(sum(plat_days.values())), "platform"
            else:
                if diff_ok_today:
                    today_cny, today_src = float(diff_today["spent"]), "balance_diff"
                else:
                    today_cny, today_src = est_today, "estimate"
                if diff_ok_total:
                    # 累计 = 起点日之前的历史估算 + 起点之后的差值。
                    # prior_est > 0 说明这个数字是「真实 + 估算」拼的，必须标出来。
                    total_cny = prior_est + float(diff_total["spent"])
                    total_src = "balance_diff"
                else:
                    total_cny, total_src = est_total, "estimate"
            # source = 取数最好的那一路（保留旧字段，界面/测试仍在用）
            for cand in ("platform", "balance_diff", "estimate"):
                if cand in (today_src, total_src):
                    source = cand
                    break

            data = {
                "ts": int(time.time()),
                "source": source,
                "today_source": today_src,
                "total_source": total_src,
                "today_cny": round(today_cny, 4),
                "total_cny": round(total_cny, 4),
                "currency": plat_currency,
                "days": len(plat_days),
                "platform_error": plat_err,
                "estimate": {
                    "today_cny": round(est_today, 4),
                    "total_cny": round(est_total, 4),
                    "calls": int(est_total_row.get("calls", 0)),
                },
                # 余额差值法：真实消费的**下限**（分辨率 ¥0.01）+ 覆盖起点。
                # enough=False 表示读数还不够（程序刚开始跑），界面要回落到估算。
                # today_full_day=False 表示今天的起点离 00:00 太远（首次安装那天
                # 就是这样），今日也会回落估算 —— 否则会报出一个「只算了几小时」
                # 的小数字，用户只会以为程序坏了。
                "balance_diff": {
                    "today_cny": round(float(diff_today["spent"]), 4) if diff_today else 0.0,
                    "total_cny": round(float(diff_total["spent"]), 4) if diff_total else 0.0,
                    "enough": diff_ok_total,
                    "today_enough": bool(diff_today and diff_today.get("enough")),
                    "today_full_day": bool(diff_today and diff_today.get("full_day")),
                    "points": int(diff_total["points"]) if diff_total else 0,
                    "resolution": 0.01,
                    "today_from": int(diff_today["from_ts"]) if diff_today else 0,
                    "total_from": int(diff_total["from_ts"]) if diff_total else 0,
                    # 起点之前那段是估算的：>0 说明累计值是「差值 + 估算」拼出来的，
                    # 界面必须照实标，不能让用户以为整个数字都来自官方余额。
                    "prior_est_cny": round(prior_est, 4),
                    "mixed": prior_est > 0,
                    "from_day": diff_first_day,
                },
                "balance": {
                    "ok": bool(bal.get("ok")),
                    "total": round(float(bal.get("total") or 0), 4),
                    "currency": bal.get("currency") or "CNY",
                    "error": str(bal.get("error") or ""),
                },
            }
            if plat_days:
                data["from_day"] = min(plat_days)
            self._billing_ts = time.time()
            self._billing_data = data
            log.info(
                "账单快照（%s）：今日 %.4f / 累计 %.4f %s%s",
                source, data["today_cny"], data["total_cny"], data["currency"],
                f"（{plat_err}）" if plat_err else "",
            )
            return data

    async def _billing_loop(self) -> None:
        """定期刷新账单快照。界面每 2 秒轮询 /health，抓取动作绝不能挂在它上面。"""
        try:
            await asyncio.sleep(20)  # 让启动期的多模态探测先跑完
        except asyncio.CancelledError:
            raise
        while not self._stop.is_set():
            try:
                await self.billing_snapshot(refresh=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("账单刷新失败（不影响消息接收）: %s", e)
            wait = max(60, int(self.cfg["billing"].get("cache_seconds", 600)))
            # 额外盯一次「刚过午夜」：差值法的「今日消费」以当天第一条读数为起点，
            # 若午夜过后要等满一个周期才读，那段时间的消费就被算进昨天了。
            # 取「下个周期」与「午夜 +5 秒」中较早的那个。
            lt = time.localtime()
            wait = min(wait, 86400 - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec) + 5)
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                raise

    async def _catch_up_daily_summaries(self) -> bool:
        """补齐持久基线之后缺失的完整自然日；返回是否需要稍后重试。"""
        yesterday = datetime.now(SHANGHAI).date() - timedelta(days=1)
        start = self.store.auto_daily_start()
        if start is None:
            start = yesterday.isoformat()
            self.store.set_auto_daily_start(start)
        if not self.llm.available:
            self.daily_schedule = {"state": "pending", "message": "尚未配置模型密钥，日摘要待补做",
                                   "day": yesterday.isoformat()}
            return False
        cursor = date.fromisoformat(start)
        if cursor > yesterday:
            self.daily_schedule = {"state": "idle", "message": "", "day": ""}
            return False
        while cursor <= yesterday:
            last = min(cursor + timedelta(days=30), yesterday)
            days = self.summarizer.daily_status(cursor.isoformat(), last.isoformat())["days"]
            for item in days:
                if self._stop.is_set():
                    return False
                if item["status"] in ("done", "empty"):
                    continue
                day = item["date"]
                try:
                    self.daily_schedule = {"state": "running", "message": f"正在生成 {day} 的日摘要", "day": day}
                    log.info("正在补做 %s 的日摘要", day)
                    result = await self.summarizer.run_dates(day, day)
                    if result:
                        log.info("%s 日摘要已完成：%d 字", day, result.get("chars", 0))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s 日摘要生成失败，稍后重试（%s）", day, type(exc).__name__)
                    self.daily_schedule = {"state": "failed", "message": f"{day} 日摘要失败，稍后重试", "day": day}
                    return True
            cursor = last + timedelta(days=1)
        self.daily_schedule = {"state": "idle", "message": "", "day": ""}
        return False

    async def _daily_summary_loop(self) -> None:
        """北京时间午夜后生成前一天摘要，重启时补做既有自动计划。"""
        await asyncio.sleep(5)
        now = datetime.now(SHANGHAI)
        after_midnight = now.hour == 0 and now.minute == 0 and now.second < 30
        while not self._stop.is_set():
            if after_midnight:
                day = (datetime.now(SHANGHAI).date() - timedelta(days=1)).isoformat()
                log.info("北京时间 00:00 已到，准备生成 %s 日摘要；等待 30 秒让已接收消息入库", day)
                self.daily_schedule = {"state": "settling", "message": "正在等待昨日消息入库完成", "day": day}
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=30)
                    return
                except asyncio.TimeoutError:
                    pass
            try:
                retry = await self._catch_up_daily_summaries()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("自动日摘要检查失败，稍后重试（%s）", type(exc).__name__)
                self.daily_schedule = {"state": "failed", "message": "自动日摘要检查失败，稍后重试", "day": ""}
                retry = True
            now = datetime.now(SHANGHAI)
            next_run = datetime.combine(now.date() + timedelta(days=1), datetime_time.min,
                                        SHANGHAI)
            wait = max(0.001, (next_run - now).total_seconds())
            if retry:
                wait = min(wait, 30 * 60)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                after_midnight = datetime.now(SHANGHAI) >= next_run

    # ---------- introspection ----------

    def health(self) -> dict:
        llm = self.llm.snapshot()
        return {
            "ok": True,
            "uptime_seconds": int(time.time() - self.started_at),
            "llm_available": llm.available,
            "vision_enabled": llm.vision_available,
            "api_provider": llm.cfg.get("api", {}).get("provider", "旧版 DeepSeek"),
            "api_model": llm.llm["model"],
            "spent_today_cny": round(self.llm.spent_today_cny(), 4),
            "budget_cny": self.cfg["llm"]["max_daily_cost_cny"],
            # 真实账单快照（可能为空：启动后 20 秒内还没抓第一次）。只读缓存，不打网络。
            "billing": self._billing_data,
            "watch_groups": sorted(self.watch) or "全部",
            "ws_peers": sorted(self.ws_peers),
            "ws_connected_at": self.ws_connected_at,
            "last_protocol_message": dict(self.last_protocol_message),
            "protocol": self.protocol_status(),
            "summary_progress": self.summarizer.progress,
            "latest_daily": self.summarizer.latest_daily_status(),
            "daily_schedule": dict(self.daily_schedule),
            # ⚠️ ws_peers 是「对端 IP 的**集合**」：多份 NapCat 全从 127.0.0.1 连进来时
            # 会被去重成 1 个 —— 7 份实例看起来和 1 份完全一样，这正是 2026-09-21
            # 「消息冻结」排查中最费时间的一处盲点。真正该看的是**连接条数**：
            # ws_conns 与 len(ws_peers) 不等，就说明有重复实例在连。
            "ws_conns": len(self.ws_conns),
            # 队列积压同理：消费者死了的话事件会堆在这里（上限 5000）而 received 不动。
            "queue": self.ingest_q.qsize(),
            "counters": self.counters,
            "stats": self.store.stats(),
        }
