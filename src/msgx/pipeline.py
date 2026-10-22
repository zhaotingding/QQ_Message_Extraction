from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime


from .config import api_key, load
from .llm import LLM
from .media import MediaWorker
from .models import Msg, parse_onebot_event
from .prefs import skip_reason
from .store import Store
from .summarize import Summarizer

from .protocol_session import ProtocolSession
from .billing_coordinator import BillingCoordinator
from .summary_scheduler import SummaryScheduler
from .time_rules import BEIJING as SHANGHAI

log = logging.getLogger("msgx.pipeline")





class Pipeline(ProtocolSession, BillingCoordinator, SummaryScheduler):
    """消息总线：接入 → 入库 → 本地 OCR；图片语义手动识别，日周月摘要自动生成。"""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load()
        self.store = Store(self._db_path(), filters=self.cfg.get("filters"))
        self.llm = LLM(self.cfg, self.store)
        self.llm.configure(api_key(self.cfg))
        self.media = MediaWorker(self.cfg, self.store, self.llm)
        self.summarizer = Summarizer(self.cfg, self.store, self.llm)

        # 老库的历史金额回填（幂等、一次性）。放在这里而不是 start()：`msgx stats`
        # 这类只读命令不会调 start()，但同样要显示「累计花费」，所以两条路都得走到。
        self.llm.backfill_legacy_cost()

        self.watch = {str(g) for g in self.cfg["watch"].get("groups", [])}
        self.names = {str(k): str(v) for k, v in (self.cfg["watch"].get("names") or {}).items()}

        # 组合入口统一拥有连接/任务的关闭责任；各领域只定义一份实现与状态。
        self.ingest_q: asyncio.Queue[dict] = asyncio.Queue(maxsize=5000)
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._init_protocol_session()
        self._init_billing_coordinator()
        self._init_summary_scheduler()
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
        self._ensure_summary_baselines(datetime.now(SHANGHAI).date())
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
            asyncio.create_task(self._summary_loop(), name="summary-schedule"),
        ]
        # v0.4.0 的下载缺陷误伤了一批图片（标记 failed）。启动时重置一次：
        # URL 仍有效即被挽回，已过期则再次 failed，不会死循环。
        n = self.store.requeue_failed_media()
        if n:
            log.info("已把 %d 张上次下载失败的图片重新排入处理队列", n)
    async def stop(self) -> None:
        self._stop.set()
        # 连接后群同步和延迟断线检查也归本进程，停止时取消并等待后再关闭 Store。
        tasks = [*self._tasks, *self._group_refresh_tasks]
        if self._peer_watch is not None:
            tasks.append(self._peer_watch)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.llm.close()
        await self.media.close()
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

        # 名单中的发言人直接丢弃；普通纯图片过滤仍保留图片供本地 OCR 和手动选择。
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

    # ---------- introspection ----------

    def health(self) -> dict:
        llm = self.llm.snapshot()
        return {
            "ok": True,
            "uptime_seconds": int(time.time() - self.started_at),
            "target_account": self.target_account,
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
            "summary_schedule": self.summary_schedule_status(),
            "latest_reports": self.latest_reports(),
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
