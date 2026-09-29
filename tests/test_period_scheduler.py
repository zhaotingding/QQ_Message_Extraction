"""固定北京时间、假摘要器和隔离 SQLite；不请求 QQ 或模型。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from msgx.llm import AmbiguousResponseError, BudgetExceeded, IncompleteResponse, RetryableRequestError
from msgx.pipeline import Pipeline, SHANGHAI
from msgx.store import Store
from msgx.summarize import MissingSummary


class PeriodSchedulerTests(IsolatedAsyncioTestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parent.parent / "data" / "test-temp"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="period-schedule-", dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "schedule.db"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.calls = []
        self.failure = {}
        self.pipe = self.make_pipe()

    def make_pipe(self):
        pipe = Pipeline.__new__(Pipeline)
        pipe.store = self.store
        pipe.llm = SimpleNamespace(available=True, budget_ok=lambda: True)
        pipe.llm.snapshot = lambda: pipe.llm
        pipe._stop = asyncio.Event()
        pipe._summary_recovered = False
        pipe.summary_schedule = {"state": "idle", "message": "", "kind": "", "start_date": "", "end_date": ""}
        pipe.daily_schedule = {}
        async def generate(kind, start, end):
            self.calls.append((kind, start, end))
            failure = self.failure.get((kind, start))
            if failure:
                raise failure
            start_ts, end_ts = pipe._period_bounds(start, end)
            self.store.add_summary("digest", start_ts, end_ts, 1, "# 已完成摘要", kind)
            row = self.store.summary_for_window(kind, start_ts, end_ts)
            return {"id": row["id"], "chars": 6}
        pipe.summarizer = SimpleNamespace(run_period=generate)
        return pipe

    @staticmethod
    def clock(day: str, minutes: int = 0):
        return datetime.combine(date.fromisoformat(day), datetime.min.time(), SHANGHAI) + timedelta(minutes=minutes, seconds=30)

    def add(self, day: str):
        ts = self.clock(day, 1).timestamp()
        self.store.conn.execute("INSERT INTO messages(group_id,msg_id,ts,text) VALUES('g',?,?,?)", (day, ts, "有效正文"))
        self.store.conn.commit()

    def job(self, kind: str, start: str, end: str):
        return self.store.summary_job(kind, start, end)

    async def test_first_start_persists_all_baselines_without_key(self):
        self.pipe.llm.available = False
        self.add("2026-09-28")
        await self.pipe._catch_up_summaries(self.clock("2026-09-29"))
        self.assertEqual(self.store.auto_daily_start(), "2026-09-28")
        self.assertEqual(self.store.auto_period_start("weekly"), "2026-09-28")
        self.assertEqual(self.store.auto_period_start("monthly"), "2026-09-01")
        self.assertEqual(self.job("daily", "2026-09-28", "2026-09-29")["attempts"], 0)
        self.assertEqual(self.calls, [])
        self.store.close()
        self.store = Store(self.path)
        pipe = self.make_pipe()
        await pipe._catch_up_summaries(self.clock("2026-09-30"))
        self.assertEqual(self.store.auto_period_start("weekly"), "2026-09-28")
        self.assertEqual(self.calls, [("daily", date(2026, 9, 28), date(2026, 9, 29))])

    async def test_monday_runs_daily_before_previous_complete_week_once(self):
        self.pipe._ensure_summary_baselines(date(2026, 9, 29))
        self.add("2026-10-04")
        await self.pipe._catch_up_summaries(self.clock("2026-10-05"))
        kinds = [kind for kind, _, _ in self.calls]
        self.assertEqual(kinds, ["daily", "weekly"])
        self.assertEqual(self.calls[-1], ("weekly", date(2026, 9, 28), date(2026, 10, 5)))
        pipe = self.make_pipe()
        await pipe._catch_up_summaries(self.clock("2026-10-05", 30))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(pipe.latest_reports()["weekly"]["status"], "done")

    async def test_month_uses_in_month_weeks_and_edge_days_without_older_month(self):
        self.pipe._ensure_summary_baselines(date(2026, 9, 29))
        for day in ("2026-09-02", "2026-09-08", "2026-09-30"):
            self.add(day)
        await self.pipe._catch_up_summaries(self.clock("2026-10-01"))
        self.assertEqual([kind for kind, _, _ in self.calls], ["daily", "daily", "daily", "weekly", "monthly"])
        self.assertIn(("weekly", date(2026, 9, 7), date(2026, 9, 14)), self.calls)
        self.assertEqual(self.calls[-1], ("monthly", date(2026, 9, 1), date(2026, 10, 1)))
        self.assertNotIn(("weekly", date(2026, 8, 31), date(2026, 9, 7)), self.calls)
        self.assertIsNone(self.job("monthly", "2026-08-01", "2026-09-01"))

    async def test_monday_first_of_month_orders_daily_weekly_monthly(self):
        self.pipe._ensure_summary_baselines(date(2026, 5, 31))
        self.add("2026-05-31")
        await self.pipe._catch_up_summaries(self.clock("2026-06-01"))
        self.assertEqual([kind for kind, _, _ in self.calls], ["daily", "weekly", "monthly"])
        self.assertEqual(self.calls[1], ("weekly", date(2026, 5, 25), date(2026, 6, 1)))
        self.assertEqual(self.calls[2], ("monthly", date(2026, 5, 1), date(2026, 6, 1)))

    async def test_long_outage_keeps_baselines_and_checks_each_finished_cycle(self):
        self.pipe._ensure_summary_baselines(date(2026, 8, 3))
        await self.make_pipe()._catch_up_summaries(self.clock("2026-10-05"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.auto_daily_start(), "2026-08-02")
        self.assertEqual(self.job("daily", "2026-10-04", "2026-10-05")["state"], "empty")
        self.assertEqual(self.job("monthly", "2026-08-01", "2026-09-01")["state"], "empty")
        self.assertEqual(self.job("monthly", "2026-09-01", "2026-10-01")["state"], "empty")
        self.assertIsNone(self.job("monthly", "2026-07-01", "2026-08-01"))

    async def test_empty_periods_persist_and_never_call_model(self):
        self.pipe._ensure_summary_baselines(date(2026, 9, 29))
        await self.pipe._catch_up_summaries(self.clock("2026-10-05"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.job("weekly", "2026-09-28", "2026-10-05")["state"], "empty")
        self.assertEqual(self.job("monthly", "2026-09-01", "2026-10-01")["state"], "empty")

    async def test_transient_retry_is_thirty_minutes_and_max_three_across_restart(self):
        self.add("2026-09-28")
        self.failure[("daily", date(2026, 9, 28))] = RetryableRequestError("HTTP 429")
        await self.pipe._catch_up_summaries(self.clock("2026-09-29"))
        self.assertEqual(self.job("daily", "2026-09-28", "2026-09-29")["state"], "retry")
        for minutes in (1, 29):
            await self.make_pipe()._catch_up_summaries(self.clock("2026-09-29", minutes))
        self.assertEqual(len(self.calls), 1)
        for minutes in (31, 62, 93):
            await self.make_pipe()._catch_up_summaries(self.clock("2026-09-29", minutes))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.job("daily", "2026-09-28", "2026-09-29")["state"], "blocked")

    async def test_permanent_or_ambiguous_errors_are_not_automatically_retried(self):
        self.pipe._ensure_summary_baselines(date(2026, 9, 25))
        failures = [ValueError("配置不正确"), AmbiguousResponseError("响应中断"),
                    IncompleteResponse("残稿", "length", 100, 100)]
        for index, error in enumerate(failures):
            day = date(2026, 9, 25) + timedelta(days=index)
            self.add(day.isoformat())
            self.failure[("daily", day)] = error
        await self.pipe._catch_up_summaries(self.clock("2026-09-28"))
        await self.make_pipe()._catch_up_summaries(self.clock("2026-09-28", 60))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.store.summary_jobs_in_state("blocked")), 3)

    async def test_failed_dependency_blocks_parent_but_later_days_continue(self):
        self.pipe._ensure_summary_baselines(date(2026, 9, 29))
        self.add("2026-09-29")
        self.add("2026-10-04")
        self.failure[("daily", date(2026, 9, 29))] = RuntimeError("已计费输出未完成")
        await self.pipe._catch_up_summaries(self.clock("2026-10-05"))
        self.assertIn(("daily", date(2026, 10, 4), date(2026, 10, 5)), self.calls)
        self.assertFalse(any(kind in ("weekly", "monthly") for kind, _, _ in self.calls))
        self.assertEqual(self.job("weekly", "2026-09-28", "2026-10-05")["state"], "waiting")
        self.assertEqual(self.job("monthly", "2026-09-01", "2026-10-01")["state"], "waiting")
        self.assertEqual(self.pipe.summary_schedule["state"], "blocked")
        self.assertIn("已计费输出未完成", self.pipe.summary_schedule["message"])

    async def test_recover_running_reuses_saved_result_or_blocks_unknown_outcome(self):
        for start in ("2026-09-27", "2026-09-28"):
            day = date.fromisoformat(start)
            self.add(start)
            self.store.save_summary_job("daily", start, (day + timedelta(days=1)).isoformat(), "running", now=1, attempts=1)
        start, end = self.pipe._period_bounds(date(2026, 9, 27), date(2026, 9, 28))
        self.store.add_summary("digest", start, end, 1, "# 已保存正文", "daily")
        await self.pipe._catch_up_summaries(self.clock("2026-09-29"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.job("daily", "2026-09-27", "2026-09-28")["state"], "done")
        self.assertEqual(self.job("daily", "2026-09-28", "2026-09-29")["state"], "blocked")

    async def test_previously_empty_period_rechecks_late_arriving_material(self):
        self.pipe._ensure_summary_baselines(date(2026, 9, 29))
        await self.pipe._catch_up_summaries(self.clock("2026-10-05"))
        self.assertEqual(self.calls, [])
        self.add("2026-09-29")
        await self.pipe._catch_up_summaries(self.clock("2026-10-05", 31))
        self.assertEqual([kind for kind, _, _ in self.calls], ["daily", "weekly", "monthly"])

    async def test_missing_dependency_waits_without_spending_attempt(self):
        self.add("2026-09-28")
        self.failure[("daily", date(2026, 9, 28))] = MissingSummary("daily", date(2026, 9, 28), date(2026, 9, 29))
        await self.pipe._catch_up_summaries(self.clock("2026-09-29"))
        row = self.job("daily", "2026-09-28", "2026-09-29")
        self.assertEqual((row["state"], row["attempts"]), ("waiting", 0))

    async def test_budget_wait_does_not_spend_attempts_and_recovers(self):
        self.add("2026-09-28")
        self.pipe.llm.budget_ok = lambda: False
        await self.pipe._catch_up_summaries(self.clock("2026-09-29"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.job("daily", "2026-09-28", "2026-09-29")["attempts"], 0)
        self.pipe.llm.budget_ok = lambda: True
        self.failure[("daily", date(2026, 9, 28))] = BudgetExceeded("并发请求先耗尽预算")
        await self.pipe._catch_up_summaries(self.clock("2026-09-29", 30))
        self.assertEqual(self.job("daily", "2026-09-28", "2026-09-29")["attempts"], 0)
        self.failure.clear()
        await self.pipe._catch_up_summaries(self.clock("2026-09-29", 60))
        self.assertEqual(self.job("daily", "2026-09-28", "2026-09-29")["state"], "done")

    async def test_current_profile_key_and_budget_are_reloaded_without_restart(self):
        self.add("2026-09-28")
        current = SimpleNamespace(available=False, budget_ok=lambda: True)
        self.pipe.llm.snapshot = lambda: current
        # 启动时的旧密钥存在，但当前 profile 无密钥，不能误发旧 profile。
        await self.pipe._catch_up_summaries(self.clock("2026-09-29"))
        self.assertEqual(self.calls, [])
        current.available = True
        current.budget_ok = lambda: False
        await self.pipe._catch_up_summaries(self.clock("2026-09-29", 31))
        self.assertEqual(self.calls, [])
        current.budget_ok = lambda: True
        self.pipe.llm.available = False  # 旧启动快照仍没密钥不应拦截新 profile。
        await self.pipe._catch_up_summaries(self.clock("2026-09-29", 62))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.job("daily", "2026-09-28", "2026-09-29")["attempts"], 1)

    async def test_invalid_profile_is_blocked_before_request(self):
        self.add("2026-09-28")
        def invalid_profile():
            raise ValueError("预算配置不是有效数字")
        self.pipe.llm.snapshot = invalid_profile
        await self.pipe._catch_up_summaries(self.clock("2026-09-29"))
        row = self.job("daily", "2026-09-28", "2026-09-29")
        self.assertEqual((row["state"], row["attempts"]), ("blocked", 0))
        self.assertEqual(self.calls, [])

    def test_health_calendar_and_status_only_read_local_rows(self):
        self.pipe._ensure_summary_baselines(date(2026, 9, 29))
        now = self.clock("2026-09-29")
        changes = self.store.conn.total_changes
        info = self.pipe.summary_schedule_status(now)
        self.assertEqual(info["next_runs"], {"daily": "2026-09-30T00:00:00+08:00",
                         "weekly": "2026-10-05T00:00:00+08:00", "monthly": "2026-10-01T00:00:00+08:00"})
        self.assertEqual(self.pipe.latest_reports(), {"daily": None, "weekly": None, "monthly": None})
        self.assertEqual(self.store.conn.total_changes, changes)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    main()
