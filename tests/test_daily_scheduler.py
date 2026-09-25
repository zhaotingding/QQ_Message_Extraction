"""日摘要补做只使用本地假对象，不连接 QQ 或模型。"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from msgx.onebot import _summary_dates, _summary_days, _summary_request
from msgx.pipeline import Pipeline, SHANGHAI


class DailySchedulerTests(IsolatedAsyncioTestCase):
    async def test_first_start_only_schedules_yesterday(self):
        yesterday = (datetime.now(SHANGHAI).date() - timedelta(days=1)).isoformat()
        store = SimpleNamespace(auto_daily_start=Mock(return_value=None),
                                set_auto_daily_start=Mock())
        summarizer = SimpleNamespace(
            daily_status=Mock(return_value={"days": [{"date": yesterday, "status": "missing"}]}),
            run_dates=AsyncMock(return_value={"chars": 3000}),
        )
        pipe = SimpleNamespace(llm=SimpleNamespace(available=True), store=store,
                               summarizer=summarizer, _stop=asyncio.Event(), daily_schedule={})
        self.assertFalse(await Pipeline._catch_up_daily_summaries(pipe))
        store.set_auto_daily_start.assert_called_once_with(yesterday)
        summarizer.daily_status.assert_called_once_with(yesterday, yesterday)
        summarizer.run_dates.assert_awaited_once_with(yesterday, yesterday)

    async def test_first_start_without_key_still_keeps_recovery_baseline(self):
        yesterday = (datetime.now(SHANGHAI).date() - timedelta(days=1)).isoformat()
        store = SimpleNamespace(auto_daily_start=Mock(return_value=None),
                                set_auto_daily_start=Mock())
        pipe = SimpleNamespace(llm=SimpleNamespace(available=False), store=store,
                               summarizer=SimpleNamespace(daily_status=Mock()),
                               _stop=asyncio.Event(), daily_schedule={})
        self.assertFalse(await Pipeline._catch_up_daily_summaries(pipe))
        store.set_auto_daily_start.assert_called_once_with(yesterday)
        pipe.summarizer.daily_status.assert_not_called()
        self.assertEqual(pipe.daily_schedule["state"], "pending")

    async def test_recovery_skips_finished_and_empty_days_and_stops_after_failure(self):
        yesterday = datetime.now(SHANGHAI).date() - timedelta(days=1)
        dates = [(yesterday - timedelta(days=3 - i)).isoformat() for i in range(4)]
        store = SimpleNamespace(auto_daily_start=Mock(return_value=dates[0]),
                                set_auto_daily_start=Mock())
        summarizer = SimpleNamespace(
            daily_status=Mock(return_value={"days": [
                {"date": dates[0], "status": "done"},
                {"date": dates[1], "status": "empty"},
                {"date": dates[2], "status": "missing"},
                {"date": dates[3], "status": "missing"},
            ]}),
            run_dates=AsyncMock(side_effect=RuntimeError("测试限流")),
        )
        pipe = SimpleNamespace(llm=SimpleNamespace(available=True), store=store,
                               summarizer=summarizer, _stop=asyncio.Event(), daily_schedule={})
        self.assertTrue(await Pipeline._catch_up_daily_summaries(pipe))
        store.set_auto_daily_start.assert_not_called()
        summarizer.run_dates.assert_awaited_once_with(dates[2], dates[2])
        self.assertEqual(pipe.daily_schedule["state"], "failed")

    async def test_long_outage_checks_days_in_supported_month_windows(self):
        yesterday = datetime.now(SHANGHAI).date() - timedelta(days=1)
        first = yesterday - timedelta(days=40)
        summarizer = SimpleNamespace(
            daily_status=Mock(side_effect=lambda start, end: {"days": [
                {"date": (datetime.fromisoformat(start).date() + timedelta(days=i)).isoformat(),
                 "status": "empty"}
                for i in range((datetime.fromisoformat(end) - datetime.fromisoformat(start)).days + 1)
            ]}),
            run_dates=AsyncMock(),
        )
        pipe = SimpleNamespace(
            llm=SimpleNamespace(available=True),
            store=SimpleNamespace(auto_daily_start=Mock(return_value=first.isoformat())),
            summarizer=summarizer, _stop=asyncio.Event(), daily_schedule={},
        )
        self.assertFalse(await Pipeline._catch_up_daily_summaries(pipe))
        self.assertEqual(summarizer.daily_status.call_count, 2)
        for call in summarizer.daily_status.call_args_list:
            start, end = call.args
            self.assertLess((datetime.fromisoformat(end) - datetime.fromisoformat(start)).days, 31)
        summarizer.run_dates.assert_not_awaited()

    def test_date_validation(self):
        self.assertEqual(_summary_dates("2026-09-24", "2026-09-24"),
                         ("2026-09-24", "2026-09-24"))
        with self.assertRaises(ValueError):
            _summary_dates("2026-09-25", "2026-09-24")
        with self.assertRaises(ValueError):
            _summary_dates("2026-09-24", "x")
        with self.assertRaises(ValueError):
            _summary_dates("20260924", "2026-09-24")

    async def test_date_routes_pass_explicit_dates_and_force(self):
        summarizer = SimpleNamespace(
            run_dates=AsyncMock(return_value={"id": 7, "chars": 3000}),
            daily_status=Mock(return_value={"days": [], "latest_daily": None}),
        )
        pipe = SimpleNamespace(summarizer=summarizer)
        request = SimpleNamespace(
            app={"pipe": pipe}, query={"kind": "by_date"}, can_read_body=True,
            json=AsyncMock(return_value={"start_date": "2026-09-24", "end_date": "2026-09-24",
                                         "force": True}),
        )
        response = await _summary_request(request)
        self.assertEqual(json.loads(response.text)["created"][0]["id"], 7)
        summarizer.run_dates.assert_awaited_once_with("2026-09-24", "2026-09-24", force=True)
        request.query = {"start_date": "2026-09-23", "end_date": "2026-09-24"}
        response = await _summary_days(request)
        self.assertEqual(response.status, 200)
        summarizer.daily_status.assert_called_once_with("2026-09-23", "2026-09-24")
        summarizer.daily_status.side_effect = ValueError("最多 31 天")
        response = await _summary_days(request)
        self.assertEqual(response.status, 400)


if __name__ == "__main__":
    main()
