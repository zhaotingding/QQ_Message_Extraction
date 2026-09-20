from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import date, datetime

from aiohttp import WSCloseCode

from . import billing
from .config import api_key, load
from .llm import BudgetExceeded, LLM
from .media import MediaWorker
from .models import Msg, parse_onebot_event
from .notify import Notifier
from .prefs import skip_reason
from .score import Scorer
from .store import Store
from .summarize import Summarizer

log = logging.getLogger("msgx.pipeline")


class Pipeline:
    """消息总线：接入 → 入库 → 媒体理解 → 打分 → 通知 → 周期摘要。"""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load()
        self.store = Store(self._db_path())
        self.llm = LLM(self.cfg, self.store)
        self.llm.configure(api_key(self.cfg))
        self.notifier = Notifier(self.cfg)
        self.media = MediaWorker(self.cfg, self.store, self.llm)
        self.scorer = Scorer(self.cfg, self.store, self.llm)
        self.summarizer = Summarizer(self.cfg, self.store, self.llm, self.notifier)

        # 老库的历史金额回填（幂等、一次性）。放在这里而不是 start()：`msgx stats`
        # 这类只读命令不会调 start()，但同样要显示「累计花费」，所以两条路都得走到。
        self.llm.backfill_legacy_cost()

        self.watch = {str(g) for g in self.cfg["watch"].get("groups", [])}
        self.names = {str(k): str(v) for k, v in (self.cfg["watch"].get("names") or {}).items()}
        self.at_ids = {str(x) for x in (self.cfg["watch"].get("at_qq") or [])}

        # 接入队列：WebSocket 回调只做入队（不能阻塞协议端），由 _ingest_loop 串行消费。
        self.ingest_q: asyncio.Queue[dict] = asyncio.Queue(maxsize=5000)
        # 当前连入的协议端（NapCat）地址集合。由接入层维护，供 /health 展示，
        # 让界面能明确区分「服务在跑」和「NapCat 已连上」——这两件事经常被混淆。
        self.ws_peers: set[str] = set()
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
            "alerts": 0,
        }

    def _db_path(self) -> str:
        from .config import abs_path

        return str(abs_path(self.cfg, "db_path"))

    def group_name(self, group_id: str) -> str:
        return self.names.get(str(group_id), "")

    # ---------- lifecycle ----------

    async def start(self) -> None:
        log.info("LLM 可用: %s | 预算上限 %.2f 元/日", self.llm.available, self.cfg["llm"]["max_daily_cost_cny"])
        if self.llm.available:
            await self.llm.probe_vision()
            log.info("多模态支持: %s", self.llm.vision_supported())
        else:
            log.warning("未检测到 DEEPSEEK_API_KEY，将运行在「仅规则」模式：不生成摘要、不做模型复核")
        self._tasks = [
            # ingest 必须在这里启动：_ws 回调只往 ingest_q 里塞事件，
            # 少了这个消费者，消息会全部堆在队列里（上限 5000）然后在进程退出时静默丢失，
            # 表现为「服务日志显示协议端已连接，但数据库里一条消息都没有」。
            asyncio.create_task(self._ingest_loop(), name="ingest"),
            asyncio.create_task(self._media_loop(), name="media"),
            asyncio.create_task(self._summary_loop(), name="summary"),
            asyncio.create_task(self._daily_loop(), name="daily"),
            asyncio.create_task(self._prune_loop(), name="prune"),
            asyncio.create_task(self._billing_loop(), name="billing"),
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

    async def refresh_group_names(self, timeout: float = 8.0) -> int:
        """从协议端拉群列表，把真实群名写进 groups 表，返回群数。

        groups 表的 name 此前只能靠 [watch] names 手填，没人填就永远是
        「(未命名) 754842541」—— NapCat 就在线上，没理由不去问它。
        """
        data = await self.call_action("get_group_list", {}, timeout)
        n = 0
        for g in data or []:
            gid = str((g or {}).get("group_id") or "")
            if gid:
                self.store.upsert_group(gid, str((g or {}).get("group_name") or ""))
                n += 1
        log.info("已从协议端拉取群列表：%d 个群", n)
        return n

    # ---------- 协议端连上 / 断开 ----------

    def on_peer_connected(self) -> None:
        t = self._peer_watch
        if t is not None and not t.done():
            t.cancel()
        asyncio.create_task(self._auto_refresh_names())

    async def _auto_refresh_names(self) -> None:
        try:
            await asyncio.sleep(2)  # 等 NapCat 完成初始化
            await self.refresh_group_names()
        except Exception as e:  # noqa: BLE001
            log.debug("自动拉取群列表失败（不影响消息接收）: %s", e)

    def on_peer_disconnected(self) -> None:
        """最后一条协议端连接断开：60s 内没重连就 P0 告警。

        实测案例：07:28 协议端断开后 6.6 小时无人察觉 —— NapCat 的 5s 自动重连
        只在它自己活着时有效，QQ 会话掉线 / 进程退出后就永远不会再连，而服务侧
        毫无异常，唯一征兆就是 ws_peers 变空。等 60s 是为了过滤「5 秒重连」的
        正常抖动，不制造噪音。
        """
        if self._stop.is_set() or self.ws_peers:
            return

        async def _watch() -> None:
            await asyncio.sleep(60)
            if self._stop.is_set() or self.ws_peers:
                return
            log.warning("协议端已断开超过 60s 且未重连 —— 消息不再入库")
            with contextlib.suppress(Exception):
                await self.notifier.notify(
                    "协议端已断开",
                    "NapCat 超过 60 秒未重连，消息已停止入库。\n"
                    "请在界面点「启动协议端(NapCat)」重新拉起。",
                    level="P0",
                    play_sound=True,
                )

        self._peer_watch = asyncio.create_task(_watch())

    async def close_ws_conns(self) -> int:
        """主动关闭所有反向 WS 连接，返回关闭条数。

        必须在 runner.cleanup() 之前调用，否则 cleanup 会一直等 WS 处理器返回。
        用 GOING_AWAY 告知对端是「服务要关了」，NapCat 收到后会自行重连。
        """
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
        await self.notifier.close()
        self.store.close()

    # ---------- ingest ----------

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
        msg: Msg | None = parse_onebot_event(event, self_id, {self_id} | self.at_ids)
        if msg is None:
            self.counters["skipped"] += 1
            return {"ok": False, "reason": "非群消息或空消息"}

        if self.watch and msg.group_id not in self.watch:
            self.counters["skipped"] += 1
            return {"ok": False, "reason": f"群 {msg.group_id} 不在监听列表"}

        # 界面配置的消息类型过滤。在入库前拦截，省下存储与模型成本。
        # 默认全部关闭，因此默认行为与不启用过滤时完全一致。
        reason = skip_reason(msg, self.cfg.get("filters") or {})
        if reason:
            self.counters["filtered"] += 1
            return {"ok": False, "reason": f"已按过滤规则丢弃: {reason}"}

        name = self.group_name(msg.group_id)
        row_id = self.store.insert_message(msg, name)
        if row_id is None:
            self.counters["duplicate"] += 1
            return {"ok": True, "duplicate": True}

        self.counters["stored"] += 1
        self.scorer.record_links(msg.group_id, msg.sender_id, msg.text)

        if msg.image_urls:
            # 图片消息不能在这里打分：OCR / 多模态结果还没出来，而报错截图的
            # 识别文本正是打分依据。交给媒体循环按 media_state='pending' 轮询处理，
            # 处理完它会立刻回头调用 _score。
            return {"ok": True, "row_id": row_id, "queued_media": True}

        await self._score(row_id)
        return {"ok": True, "row_id": row_id}

    async def _score(self, row_id: int) -> None:
        row = self.store.get_message(row_id)
        if row is None:
            return
        gname = self.group_name(row["group_id"])
        try:
            result = await self.scorer.score(row, gname)
        except BudgetExceeded:
            log.warning("今日预算已用尽，后续消息仅走规则判定")
            result = {"score": 0.0, "level": "P2", "reason": "预算已用尽"}
        except Exception as e:  # noqa: BLE001
            log.warning("打分失败: %s", e)
            return

        if result["level"] not in ("P0", "P1"):
            return

        excerpt = (row["text"] or row["ocr_text"] or "")[:300]
        alert_id = self.store.add_alert(
            row["group_id"], row["msg_id"], result["level"], result["score"], result["reason"], excerpt
        )
        self.counters["alerts"] += 1

        if result["level"] == "P0":
            await self.notifier.notify(
                f"P0 · {gname or row['group_id']} · {row['sender_name']}",
                f"{excerpt}\n\n理由：{result['reason']}",
                level="P0",
                play_sound=True,
            )
            self.store.mark_notified(alert_id)
        log.info("[%s] %s | %s | %s", result["level"], gname or row["group_id"], result["reason"], excerpt[:60])

    # ---------- background loops ----------

    async def _media_loop(self) -> None:
        """轮询待处理图片；处理完立刻打分（此时 OCR 结果已就绪）。

        空闲轮里顺带补跑历史失败的图片语义。为什么要放在这里：语义失败是
        **瞬时连接抖动**造成的（本机到 api.deepseek.com 偶发被撤连接，约 1/8），
        隔一会儿重发基本就能成 —— 但没人补跑的话，那张图的信息就永久丢了。
        每 20 个空闲轮（≈60 秒）补一小批，避免一次网络恢复后对模型发起一堆并发。
        """
        idle_rounds = 0
        while not self._stop.is_set():
            try:
                done = await self.media.process_pending(20)
                if done:
                    idle_rounds = 0
                    for row_id in done:
                        await self._score(row_id)
                    continue
                idle_rounds += 1
                if idle_rounds % 20 == 0:
                    # 补跑成功的那些必须重新打分：报错截图的语义文本正是打分依据，
                    # 第一遍提取失败 = 这一条从来没被正确评过。
                    for row_id in await self.media.retry_vision(4):
                        await self._score(row_id)
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("媒体循环异常: %s", e)
                await asyncio.sleep(5)

    async def _summary_loop(self) -> None:
        interval = max(1, int(self.cfg["summary"].get("interval_minutes", 30))) * 60
        while not self._stop.is_set():
            try:
                await asyncio.sleep(interval)
                await self.summarizer.run_periodic()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("摘要循环异常: %s", e)

    async def _daily_loop(self) -> None:
        target = str(self.cfg["summary"].get("daily_report_at", "22:00"))
        last_day = ""
        while not self._stop.is_set():
            try:
                await asyncio.sleep(60)
                now = datetime.now()
                if now.strftime("%H:%M") == target and now.strftime("%Y-%m-%d") != last_day:
                    last_day = now.strftime("%Y-%m-%d")
                    await self.summarizer.run_daily()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("日报循环异常: %s", e)

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
        """真实账单快照：官方余额 + 平台逐日消费；两者都拿不到才用本地估算。

        为什么必须区分口径：本地只按 token 用量**估算**，和平台账单对不上是常态
        （老库还有相当一段时间的行没有金额，见 store.USAGE_MIGRATIONS 的注释）。
        用户看到的数字必须是「有出处的」：抓到了就标 platform，没抓到就标 estimate
        并把失败原因带上 —— 不许把估算值伪装成真实值。

        两张凭证互不通用（见 billing.py）：余额用 API Key，消费用平台 token。
        任何一路失败都不影响另一路。
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

            # --- 真实消费（平台前端接口，用登录态 token）---
            plat_days: dict[str, float] = {}
            plat_currency = "CNY"
            plat_err = ""
            plat_token = str(bc.get("platform_token") or "").strip()
            if not bc.get("platform_enabled", True):
                plat_err = "配置里已关闭平台数据（[billing] platform_enabled = false）"
            elif not plat_token:
                plat_err = "未配置 platform_token，只能显示本地估算（取法见 config.toml [billing]）"
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

            today = date.today().isoformat()
            if plat_days:
                source = "platform"
                today_cny = float(plat_days.get(today, 0.0))
                total_cny = float(sum(plat_days.values()))
            else:
                source = "estimate"
                today_cny = est_today
                total_cny = est_total

            data = {
                "ts": int(time.time()),
                "source": source,
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
            try:
                await asyncio.sleep(max(60, int(self.cfg["billing"].get("cache_seconds", 600))))
            except asyncio.CancelledError:
                raise

    # ---------- introspection ----------

    def health(self) -> dict:
        return {
            "ok": True,
            "uptime_seconds": int(time.time() - self.started_at),
            "llm_available": self.llm.available,
            "vision_supported": self.llm.vision_supported() if self.llm.available else False,
            "spent_today_cny": round(self.llm.spent_today_cny(), 4),
            "budget_cny": self.cfg["llm"]["max_daily_cost_cny"],
            # 真实账单快照（可能为空：启动后 20 秒内还没抓第一次）。只读缓存，不打网络。
            "billing": self._billing_data,
            "watch_groups": sorted(self.watch) or "全部",
            "ws_peers": sorted(self.ws_peers),
            "counters": self.counters,
            "stats": self.store.stats(),
        }
