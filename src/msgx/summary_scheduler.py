"""北京时间自然周期调度。需要 store/llm/summarizer/停止事件；不管理协议连接。"""
from __future__ import annotations
import asyncio
import logging
import time
from datetime import date, datetime, time as datetime_time, timedelta
from . import security
from .llm import BudgetExceeded, IncompleteResponse, RetryableRequestError, BackgroundResponsePending
from .summarize import MissingSummary, period_parts, summary_chars

from .time_rules import BEIJING as SHANGHAI

log = logging.getLogger("msgx.summary_scheduler")

PERIOD_NAMES = {"daily": "日报", "weekly": "周报", "monthly": "月报"}

def _next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


class SummaryScheduler:
    def _init_summary_scheduler(self) -> None:
        """一个调度循环和唤醒事件；任务结果与重试状态仍由 Store 持久化。"""
        self._summary_wake = asyncio.Event()
        self.daily_schedule = {"state": "idle", "message": "", "day": ""}
        self.summary_schedule = {"state": "idle", "message": "", "kind": "", "start_date": "", "end_date": ""}
        self._summary_recovered = False

    def _ensure_summary_baselines(self, today: date) -> None:
        if self.store.auto_daily_start() is None:
            self.store.set_auto_daily_start((today - timedelta(days=1)).isoformat())
        self.store.set_auto_period_start("weekly", (today - timedelta(days=today.weekday())).isoformat())
        self.store.set_auto_period_start("monthly", today.replace(day=1).isoformat())


    @staticmethod
    def _period_bounds(start: date, end: date) -> tuple[int, int]:
        return tuple(int(datetime.combine(day, datetime_time.min, SHANGHAI).timestamp()) for day in (start, end))


    def _set_summary_schedule(self, state: str, message: str, kind: str = "",
                              start: date | None = None, end: date | None = None) -> None:
        self.summary_schedule = {"state": state, "message": message, "kind": kind,
                                 "start_date": start.isoformat() if start else "",
                                 "end_date": end.isoformat() if end else ""}
        self.daily_schedule = {"state": state, "message": message,
                               "day": start.isoformat() if start and kind == "daily" else ""}


    def _recover_summary_jobs(self, now: float) -> None:
        if self._summary_recovered:
            return
        for job in self.store.summary_jobs_in_state("running"):
            start, end = date.fromisoformat(job["start_date"]), date.fromisoformat(job["end_date"])
            saved = self.store.summary_for_window(job["kind"], *self._period_bounds(start, end))
            request = self.store.summary_request((job["kind"], start.isoformat(), end.isoformat()))
            resuming = bool(request and request["response_id"])
            self.store.save_summary_job(job["kind"], start.isoformat(), end.isoformat(),
                "done" if saved else "pending" if resuming else "blocked", now=now, summary_id=saved["id"] if saved else None,
                error="" if saved else "继续取回已接受的后台摘要，未重新提交消息" if resuming else
                "上次请求中断，处理结果及费用不明；已停止自动重试，请检查记录")
        self._summary_recovered = True


    def _due_summary_periods(self, today: date) -> list[tuple[str, date, date]]:
        periods = []
        cursor = date.fromisoformat(self.store.auto_daily_start())
        while cursor < today:
            periods.append(("daily", cursor, cursor + timedelta(days=1)))
            cursor += timedelta(days=1)
        cursor = date.fromisoformat(self.store.auto_period_start("weekly"))
        while cursor + timedelta(days=7) <= today:
            periods.append(("weekly", cursor, cursor + timedelta(days=7)))
            cursor += timedelta(days=7)
        cursor = date.fromisoformat(self.store.auto_period_start("monthly"))
        while _next_month(cursor) <= today:
            periods.append(("monthly", cursor, _next_month(cursor)))
            cursor = _next_month(cursor)
        return sorted(periods, key=lambda part: (part[2], ("daily", "weekly", "monthly").index(part[0])))


    async def _ensure_summary_period(self, kind: str, start: date, end: date,
                                     now: float, checked: dict) -> bool:
        key = (kind, start, end)
        if key in checked:
            return checked[key]
        checked[key] = False
        if self._stop.is_set():
            return False
        saved = self.store.summary_for_window(kind, *self._period_bounds(start, end))
        args = (kind, start.isoformat(), end.isoformat())
        if saved:
            self.store.clear_summary_request(args)
            self.store.save_summary_job(*args, "done", now=now, summary_id=saved["id"])
            checked[key] = True
            return True
        job = self.store.summary_job(*args)
        request = self.store.summary_request(args)
        resuming = bool(request and request["response_id"])
        if not resuming and job and job["state"] == "empty" and kind == "daily" and not self.store.count_messages_window(*self._period_bounds(start, end)):
            checked[key] = True
            return True
        if job and (job["state"] in ("blocked", "running") or job["next_attempt_at"] > now):
            return False
        if job is None:
            self.store.save_summary_job(*args, "pending", now=now)
        # 依赖失败不能让父报告把残缺材料冒充完整报告；其余周期仍继续。
        ready = True
        for child_kind, child_start, child_end in ([] if resuming else period_parts(kind, start, end)):
            if not await self._ensure_summary_period(child_kind, child_start, child_end, now, checked):
                ready = False
        if not ready:
            self.store.save_summary_job(*args, "waiting", now=now,
                                        error="依赖摘要尚未完成，本周期暂不生成")
            return False
        if not resuming and kind == "daily" and not self.store.count_messages_window(*self._period_bounds(start, end)):
            self.store.save_summary_job(*args, "empty", now=now)
            checked[key] = True
            return True
        # 全空周/月不调用模型，各叶子任务的 empty 状态持久保存。
        if not resuming and kind != "daily" and all(self.store.summary_job(child_kind, child_start.isoformat(), child_end.isoformat())["state"] == "empty"
                                    for child_kind, child_start, child_end in period_parts(kind, start, end)):
            self.store.save_summary_job(*args, "empty", now=now)
            checked[key] = True
            return True
        if job and job["state"] == "empty":
            # 空周期尚未付费生成；晚入库或新选入图片可在后续检查中恢复。
            self.store.save_summary_job(*args, "pending", now=now)
        try:
            current_llm = self.llm.snapshot() if not resuming else None
            available = resuming or current_llm.available
            budget_ok = resuming or (available and current_llm.budget_ok())
        except Exception as exc:  # noqa: BLE001
            detail = "模型配置读取失败：" + security.redact(str(exc))[:200] + "；已停止自动重试，请检查设置"
            self.store.save_summary_job(*args, "blocked", now=now, error=detail)
            self._set_summary_schedule("blocked", detail, kind, start, end)
            return False
        if not available or not budget_ok:
            reason = "尚未配置模型密钥" if not available else "今日模型预算已用尽"
            self.store.save_summary_job(*args, "pending", now=now, error=reason + "，等待恢复")
            self._set_summary_schedule("pending", reason + "，自动摘要等待恢复", kind, start, end)
            return False
        if not self.store.claim_summary_job(*args, now, resume=resuming):
            return False
        self._set_summary_schedule("running", f"正在生成 {start} 至 {end} 的{PERIOD_NAMES[kind]}", kind, start, end)
        request_started = time.monotonic()
        try:
            result = await self.summarizer.run_period(kind, start, end)
            self.store.save_summary_job(*args, "done" if result else "empty", now=now,
                                        summary_id=result["id"] if result else None)
            checked[key] = True
            log.info("自动%s已完成：%s 至 %s%s", PERIOD_NAMES[kind], start, end, "（空周期）" if not result else "")
            return True
        except asyncio.CancelledError:
            # 保留 running；下次启动先查成稿，结果不明则停止自动重发。
            raise
        except Exception as exc:  # noqa: BLE001
            attempts = self.store.summary_job(*args)["attempts"]
            cause = exc
            while cause.__cause__ is not None:
                cause = cause.__cause__
            retry_at = 0
            failed_at = now + (time.monotonic() - request_started)
            if isinstance(cause, BackgroundResponsePending):
                state, retry_at = "retry", failed_at + 60
            elif isinstance(cause, BudgetExceeded):
                state = "pending"
                attempts = max(0, attempts - int(not resuming))
            elif isinstance(cause, MissingSummary):
                state = "waiting"
                attempts = max(0, attempts - 1)
            elif isinstance(cause, RetryableRequestError) and not isinstance(exc, IncompleteResponse) and attempts < 3:
                state, retry_at = "retry", failed_at + 30 * 60
            else:
                state = "blocked"
            detail = security.redact(str(exc))[:240]
            if state == "blocked":
                detail += "；已停止自动重试，请检查记录"
            self.store.save_summary_job(*args, state, now=failed_at, attempts=attempts,
                                        next_attempt_at=retry_at, error=detail)
            self._set_summary_schedule(state, detail, kind, start, end)
            log.warning("自动%s失败：%s 至 %s，第 %d 次，状态 %s（%s）",
                        PERIOD_NAMES[kind], start, end, attempts, state, type(exc).__name__)
            return False


    async def _catch_up_summaries(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(SHANGHAI)
        self._ensure_summary_baselines(now.date())
        self._recover_summary_jobs(now.timestamp())
        batch_started = time.monotonic()
        checked = {}
        tasks = set()
        def add_dependencies(kind: str, start: date, end: date) -> None:
            if (kind, start, end) in tasks:
                return
            tasks.add((kind, start, end))
            for child in period_parts(kind, start, end):
                add_dependencies(*child)
        for task in self._due_summary_periods(now.date()):
            add_dependencies(*task)
        # 本周期月报允许补齐其月内整周，但不会独立回补基线前的其它周/月。
        for kind, start, end in sorted(tasks, key=lambda part: (("daily", "weekly", "monthly").index(part[0]), part[2])):
            if self._stop.is_set():
                return False
            current_time = now.timestamp() + (time.monotonic() - batch_started)
            await self._ensure_summary_period(kind, start, end, current_time, checked)
        pending = self.store.summary_jobs_in_state("pending", "retry", "waiting", "blocked")
        if pending:
            # 先显示实际失败原因，避免上层“等待依赖”盖住下层已停止重试的错误。
            first = next((job for job in pending if job["state"] == "blocked"), pending[0])
            self._set_summary_schedule(first["state"], first["error"], first["kind"],
                                       date.fromisoformat(first["start_date"]), date.fromisoformat(first["end_date"]))
        else:
            self._set_summary_schedule("idle", "自动摘要已检查，等待下个完整周期")
        return any(job["state"] != "blocked" for job in pending)


    def retry_summary_job(self, kind: str, start: str, end: str, *, confirmed: bool, revision: float) -> dict:
        """只重置用户确认的最新失败；由单一调度循环接管，不另起生成任务。"""
        if confirmed is not True:
            raise ValueError("补生成可能重复计费，需要明确确认")
        job = self.store.summary_job(kind, start, end)
        if not job or job["state"] != "blocked" or job["updated_at"] != revision:
            raise ValueError("任务状态已变化，请刷新后重新确认")
        saved = self.store.summary_for_window(kind, *self._period_bounds(date.fromisoformat(start), date.fromisoformat(end)))
        if saved:
            self.store.save_summary_job(kind, start, end, "done", now=time.time(), summary_id=saved["id"])
            state = "done"
        elif self.store.retry_summary_job(kind, start, end, revision, time.time()):
            request = self.store.summary_request((kind, start, end))
            if request and (not request["response_id"] or request["result"] and
                            request["result"]["choices"][0]["finish_reason"] != "stop"):
                self.store.clear_summary_request((kind, start, end))
            state = "pending"
        else:
            raise ValueError("任务状态已变化，请刷新后重新确认")
        log.info("用户确认恢复%s：%s 至 %s；原尝试 %s 次；状态 %s",
                 PERIOD_NAMES[kind], start, end, job["attempts"], state)
        self._summary_wake.set()
        return {"ok": True, "state": state}


    async def _summary_loop(self) -> None:
        """按北京时间检查完整日、周、月；同一到期点先日再周再月。"""
        await asyncio.sleep(5)
        while not self._stop.is_set():
            now = datetime.now(SHANGHAI)
            if now.hour == 0 and now.minute == 0 and now.second < 30:
                self._set_summary_schedule("settling", "正在等待午夜前已接收消息入库完成")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=30 - now.second)
                    return
                except asyncio.TimeoutError:
                    pass
            checked_day = datetime.now(SHANGHAI).date()
            self._summary_wake.clear()
            try:
                await self._catch_up_summaries()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("自动摘要计划检查失败（%s）", type(exc).__name__)
                self._set_summary_schedule("failed", "自动摘要计划检查失败，请查看日志")
            now = datetime.now(SHANGHAI)
            if now.date() != checked_day:
                continue
            midnight = datetime.combine(now.date() + timedelta(days=1), datetime_time.min, SHANGHAI)
            wait = min(30 * 60, max(0.01, (midnight - now).total_seconds()))
            for job in self.store.summary_jobs_in_state("retry"):
                request = self.store.summary_request((job["kind"], job["start_date"], job["end_date"]))
                if request and request["response_id"]:
                    wait = min(wait, max(1, job["next_attempt_at"] - now.timestamp()))
            try:
                await asyncio.wait_for(self._summary_wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass


    def summary_schedule_status(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(SHANGHAI)
        tomorrow = now.date() + timedelta(days=1)
        monday = now.date() + timedelta(days=7 - now.weekday())
        next_dates = {"daily": tomorrow, "weekly": monday, "monthly": _next_month(now.date())}
        next_runs = {kind: datetime.combine(day, datetime_time.min, SHANGHAI).isoformat()
                     for kind, day in next_dates.items()}
        failures = self.store.summary_jobs_in_state("pending", "retry", "waiting", "blocked")
        failures = sorted(failures, key=lambda job: job["state"] != "blocked")[:10]
        return {**self.summary_schedule, "next_runs": next_runs, "failures": failures}


    def latest_reports(self) -> dict:
        result = {}
        for kind in PERIOD_NAMES:
            row = self.store.latest_period_summary(kind)
            result[kind] = ({"id": row["id"], "kind": kind, "window_start": row["window_start"],
                            "window_end": row["window_end"], "chars": summary_chars(row["content"]),
                            "created_at": row["created_at"], "status": "done", "path": row["report_path"],
                            "start_date": datetime.fromtimestamp(row["window_start"], SHANGHAI).date().isoformat(),
                            "end_date": datetime.fromtimestamp(row["window_end"], SHANGHAI).date().isoformat()}
                           if row else None)
        return result
