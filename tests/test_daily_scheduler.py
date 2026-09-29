"""日摘要补做只使用本地假对象，不连接 QQ 或模型。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from msgx.onebot import _summary_dates, _summary_days, _summary_request


class DailySchedulerTests(IsolatedAsyncioTestCase):
    def test_date_validation(self):
        self.assertEqual(_summary_dates("2026-09-24", "2026-09-24"),
                         ("2026-09-24", "2026-09-24"))
        with self.assertRaises(ValueError):
            _summary_dates("2026-09-25", "2026-09-24")
        with self.assertRaises(ValueError):
            _summary_dates("2026-09-24", "x")
        with self.assertRaises(ValueError):
            _summary_dates("20260924", "2026-09-24")

    async def test_manual_route_retired_and_dates_remain_read_only(self):
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
        self.assertEqual(response.status, 410)
        summarizer.run_dates.assert_not_awaited()
        request.json.assert_not_awaited()
        request.query = {"start_date": "2026-09-23", "end_date": "2026-09-24"}
        response = await _summary_days(request)
        self.assertEqual(response.status, 200)
        summarizer.daily_status.assert_called_once_with("2026-09-23", "2026-09-24")
        summarizer.daily_status.side_effect = ValueError("最多 31 天")
        response = await _summary_days(request)
        self.assertEqual(response.status, 400)


if __name__ == "__main__":
    main()
