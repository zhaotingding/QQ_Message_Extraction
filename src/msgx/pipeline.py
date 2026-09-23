from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import date, datetime

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


class Pipeline:
    """消息总线：接入 → 入库 → 带配文图片解析，摘要仅由用户手动生成。"""

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
        self._peer_watch: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        # 账单快照缓存。界面每 2 秒轮询 /health，绝不能每次都打网络（1 次余额 + N 次
        # 逐月消费）。所以：/health 只读缓存，真正的抓取由 _billing_loop 与 /billing 触发。
        self._billing_ts = 0.0
        self._billing_data: dict = {}
        self._billing_lock: asyncio.Lock | None = None
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
            log.warning("未检测到 DEEPSEEK_API_KEY，仅接收和存储消息，不调用模型")
        self._tasks = [
            # ingest 必须在这里启动：_ws 回调只往 ingest_q 里塞事件，
            # 少了这个消费者，消息会全部堆在队列里（上限 5000）然后在进程退出时静默丢失，
            # 表现为「服务日志显示协议端已连接，但数据库里一条消息都没有」。
            asyncio.create_task(self._ingest_loop(), name="ingest"),
            asyncio.create_task(self._media_loop(), name="media"),
            asyncio.create_task(self._prune_loop(), name="prune"),
            asyncio.create_task(self._billing_loop(), name="billing"),
            asyncio.create_task(self._watchdog_loop(), name="watchdog"),
        ]
        # v0.4.0 的下载缺陷误伤了一批图片（标记 failed）。启动时重置一次：
        # URL 仍有效即被挽回，已过期则再次 failed，不会死循环。
        n = self.store.requeue_failed_media()
        if n:
            log.info("已把 %d 张上次下载失败的图片重新排入处理队列", n)
        # v0.5.0 同类问题：图片语义提取曾把「失败」写成「完成」，导致 62 张图
        # 永久不再重试。现在有独立的 vision_state 能区分了，那些老行会被
        # `media.retry_vision()` 在空闲轮里自动捡回来（图片还在本地，只是重发一次推理）。
        # 注意：不要在这里把 vision_state 清空 —— 清空后既不在 pending_media 里、
        # 也不在 failed_vision 里，会变成谁也看不见的孤儿行。
        n2 = self.store.stats().get("vision_retry", 0)
        if n2:
            log.info("有 %d 张图片的语义提取待补跑（失败过，但次数没用满）", n2)

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
        if not self_id.isdigit() or self_id == "0" or not event.get("message_id") or not event.get("time"):
            return
        try:
            msg = parse_onebot_event(event, self_id, {self_id})
        except (TypeError, ValueError, AttributeError):
            return
        if msg is None or self.store.conn.execute(
            "SELECT 1 FROM messages WHERE group_id=? AND msg_id=?", (msg.group_id, msg.msg_id)
        ).fetchone():
            return
        self.last_protocol_message = {"received_at": time.time(), "message_time": msg.ts, "self_id": self_id}

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

        # 界面配置的消息类型过滤。在处理前拦截，省下图片存储与模型成本。
        # 默认全部关闭，因此默认行为与不启用过滤时完全一致。
        reason = skip_reason(msg, self.cfg.get("filters") or {})
        if reason:
            self.counters["filtered"] += 1
            # v0.5.7：图片/表情过滤后仍留一行状态，让实时页能说明为何没有识别结果。
            # 只存简要记录，既不进媒体队列，也不参与打分或摘要。
            if reason in ("纯表情", "纯图片"):
                row_id = self.store.insert_message(msg, self.group_name(msg.group_id), self.bot_uin,
                                                   filtered=True)
                if row_id is None:
                    self.counters["duplicate"] += 1
                    return {"ok": True, "duplicate": True, "filtered": True}
                return {"ok": True, "row_id": row_id, "filtered": True, "reason": reason}
            return {"ok": False, "reason": f"已按过滤规则丢弃: {reason}"}

        name = self.group_name(msg.group_id)
        row_id = self.store.insert_message(msg, name, self.bot_uin)
        if row_id is None:
            self.counters["duplicate"] += 1
            return {"ok": True, "duplicate": True}

        self.counters["stored"] += 1

        if msg.image_urls:
            # 交给媒体循环处理；只有消息带配文时才允许解析图片。
            return {"ok": True, "row_id": row_id, "queued_media": True}

        return {"ok": True, "row_id": row_id}

    # ---------- background loops ----------

    async def _media_loop(self) -> None:
        """轮询图片队列；允许解析的失败图片按既有次数上限补跑。"""
        idle_rounds = 0
        while not self._stop.is_set():
            try:
                done = await self.media.process_pending(20)
                if done:
                    idle_rounds = 0
                    continue
                idle_rounds += 1
                if idle_rounds % 20 == 0:
                    await self.media.retry_vision(4)
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

    async def _watchdog_loop(self) -> None:
        """协议端「连着却不推消息」时记录警告（v0.5.4）。

        要解决的问题：协议端可以**连着、能应答 API，却一条消息都不推**。
        这种状态下 `/health` 一切正常，界面上「协议端已连接」是绿的，
        唯一征兆是消息不再进来 —— 实测最长挂了 9 小时，全靠用户自己发现。

        **判据必须同时满足四条，少一条都会误报**（这是本项目的血泪）：
          ① 协议端连着 —— 断开有另一条专门的告警，这里不重复报；
          ② 桌面 QQ 的消息库 `nt_msg.db-wal` 长时间没写入 —— 上游的直接证据；
          ③ 本库同期也没有新消息 —— 排除「QQ 库在写、只是没写我们的群」；
          ④ 这个小时按历史基线**本该是活跃的** —— 否则凌晨一点必然误报，
             而深夜没人讲话是常态，不是故障。

        只记录一次：警告后解除武装，等 ② 恢复（QQ 库重新开始写）才重新武装。
        仅记录日志，不弹窗、不发声、不推送，也不自动重启。
        """
        wg = self.cfg.get("watchdog") or {}
        if not wg.get("enabled", True):
            return
        every = max(30, int(wg.get("check_seconds", 300)))
        stale_s = max(300, int(wg.get("stale_minutes", 30)) * 60)
        busy_min = int(wg.get("busy_min_messages", 5))
        days = int(wg.get("baseline_days", 7))
        armed = True
        while not self._stop.is_set():
            try:
                await asyncio.sleep(every)
                age = napcat.qq_msg_db_age()
                if age is None:
                    continue  # 查不到 QQ 数据目录 —— 不据此下任何结论
                if age < stale_s:
                    armed = True  # 上游活着，重新武装
                    continue
                if not self.ws_peers:
                    continue  # 协议端没连着：那是另一种告警，这里不抢
                if self.store.messages_since(int(time.time()) - stale_s):
                    continue  # 还在收消息，说明只是 QQ 库的 mtime 没动（别的号在写）
                hour = datetime.now().hour
                base = self.store.hour_baseline(hour, days)
                if base < busy_min:
                    log.debug("看门狗：%d 点历史基线仅 %d 条，判定『本来就没人说话』，不告警", hour, base)
                    armed = True
                    continue
                if not armed:
                    continue
                armed = False
                log.warning("看门狗：QQ 消息库已 %.0f 分钟无写入，本库同期也无新消息"
                            "（%d 点历史基线 %d 条）—— 判定消息冻结", age / 60, hour, base)
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

    # ---------- introspection ----------

    def health(self) -> dict:
        return {
            "ok": True,
            "uptime_seconds": int(time.time() - self.started_at),
            "llm_available": self.llm.available,
            "vision_enabled": self.llm.vision_available,
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
