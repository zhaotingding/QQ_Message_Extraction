from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from .config import api_key, load
from .llm import BudgetExceeded, LLM
from .media import MediaWorker
from .models import Msg, parse_onebot_event
from .notify import Notifier
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

        self.watch = {str(g) for g in self.cfg["watch"].get("groups", [])}
        self.names = {str(k): str(v) for k, v in (self.cfg["watch"].get("names") or {}).items()}
        self.at_ids = {str(x) for x in (self.cfg["watch"].get("at_qq") or [])}

        # 接入队列：WebSocket 回调只做入队（不能阻塞协议端），由 _ingest_loop 串行消费。
        self.ingest_q: asyncio.Queue[dict] = asyncio.Queue(maxsize=5000)
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.started_at = time.time()
        self.counters = {"received": 0, "stored": 0, "duplicate": 0, "skipped": 0, "alerts": 0}

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
        ]

    async def stop(self) -> None:
        self._stop.set()
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
        """轮询待处理图片；处理完立刻打分（此时 OCR 结果已就绪）。"""
        while not self._stop.is_set():
            try:
                done = await self.media.process_pending(20)
                if not done:
                    await asyncio.sleep(3)
                    continue
                for row_id in done:
                    await self._score(row_id)
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

    # ---------- introspection ----------

    def health(self) -> dict:
        return {
            "ok": True,
            "uptime_seconds": int(time.time() - self.started_at),
            "llm_available": self.llm.available,
            "vision_supported": self.llm.vision_supported() if self.llm.available else False,
            "spent_today_cny": round(self.llm.spent_today_cny(), 4),
            "budget_cny": self.cfg["llm"]["max_daily_cost_cny"],
            "watch_groups": sorted(self.watch) or "全部",
            "counters": self.counters,
            "stats": self.store.stats(),
        }
