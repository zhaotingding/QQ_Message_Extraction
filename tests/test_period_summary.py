"""日、周、月分层摘要：真实临时数据库与假模型，不产生 API 费用。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from msgx.store import Store
from msgx.summarize import BEIJING, MissingSummary, Summarizer, output_token_budget, period_parts
from msgx.summary_review import content_hash, get_review, save_review


class FakeLLM:
    available = True

    def __init__(self):
        self.calls = []
        self.error = None
        self.waiting = None
        self.release = None

    async def chat(self, system, body, **kwargs):
        self.calls.append((system, body, kwargs))
        if self.waiting:
            self.waiting.set()
            await self.release.wait()
        if self.error:
            raise self.error
        return "# 周期报告\n\n**主题**\n完整归纳结果。"


class PeriodPartsTests(unittest.TestCase):
    def test_month_coverage_including_cross_year_and_leap_year(self):
        for start, end in [(date(2026, 8, 1), date(2026, 9, 1)),
                           (date(2026, 9, 1), date(2026, 10, 1)),
                           (date(2025, 12, 1), date(2026, 1, 1)),
                           (date(2024, 2, 1), date(2024, 3, 1))]:
            with self.subTest(start=start):
                covered = []
                for kind, begin, stop in period_parts("monthly", start, end):
                    self.assertLessEqual(start, begin)
                    self.assertLessEqual(stop, end)
                    if kind == "weekly":
                        self.assertEqual(begin.weekday(), 0)
                        self.assertEqual((stop - begin).days, 7)
                    else:
                        self.assertEqual((stop - begin).days, 1)
                    covered.extend(begin + timedelta(days=i) for i in range((stop - begin).days))
                self.assertEqual(covered, [start + timedelta(days=i) for i in range((end - start).days)])
        parts = period_parts("monthly", date(2026, 9, 1), date(2026, 10, 1))
        self.assertEqual(sum(kind == "weekly" for kind, _, _ in parts), 3)
        self.assertEqual(sum(kind == "daily" for kind, _, _ in parts), 9)

    def test_daily_weekly_shapes_and_invalid_windows(self):
        start = date(2026, 8, 3)
        self.assertEqual(period_parts("daily", start, start + timedelta(days=1)), [])
        self.assertEqual(len(period_parts("weekly", start, start + timedelta(days=7))), 7)
        for args in [("other", start, start + timedelta(days=1)),
                     ("daily", start, start + timedelta(days=2)),
                     ("weekly", start + timedelta(days=1), start + timedelta(days=8)),
                     ("monthly", start, date(2026, 9, 1)),
                     ("monthly", date(2026, 8, 1), date(2026, 9, 2))]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                period_parts(*args)


class PeriodSummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "msgx.db"
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.llm = FakeLLM()
        cfg = {"app": {"report_dir": str(Path(self.tmp.name) / "reports")},
               "summary": {"target_chars": 2000, "prompt": ""},
               "llm": {"model": "deepseek-flash", "base_url": "https://example.invalid"}}
        self.sm = Summarizer(cfg, self.store, self.llm)

    def saved(self, kind, start, end, text, count=1):
        begin, stop = self.sm._day_bounds(start)[0], self.sm._day_bounds(end)[0]
        self.store.add_summary("digest", begin, stop, count, text, kind)
        return self.store.summary_for_window(kind, begin, stop)

    def add_message(self, day, text="原始聊天正文不应传给周月报"):
        ts = self.sm._day_bounds(day)[0]
        self.store.conn.execute("INSERT INTO messages(group_id,msg_id,ts,text) VALUES('g',?,?,?)",
                                (day.isoformat(), ts, text))
        self.store.conn.commit()

    def week(self, start):
        return [self.saved("daily", day, stop, f"# 日报 {day}\n当天已保存的归纳。")
                for _, day, stop in period_parts("weekly", start, start + timedelta(days=7))]

    async def test_daily_uses_d_and_half_open_window(self):
        start, end = date(2026, 8, 3), date(2026, 8, 4)
        self.add_message(start, "需要归纳的这一天")
        self.add_message(end, "下一天不得混入")
        result = await self.sm.run_period("daily", start, end)
        system, body, options = self.llm.calls[0]
        self.assertEqual(result["kind"], "daily")
        self.assertIn("全文篇幅上限为 2000 字", system)
        self.assertEqual(options["max_tokens"], output_token_budget(2000))
        self.assertIn("需要归纳的这一天", body)
        self.assertNotIn("下一天不得混入", body)
        self.assertEqual((await self.sm.run_period("daily", start, end))["id"], result["id"])
        self.assertEqual(len(self.llm.calls), 1)

    async def test_week_only_uses_saved_daily_and_preserves_finished_result(self):
        start, end = date(2026, 8, 3), date(2026, 8, 10)
        rows = self.week(start)
        self.add_message(start)
        first = rows[0]
        marks = [{"start": 0, "end": 4, "text": "一般关注", "level": "blue"},
                 {"start": 5, "end": 9, "text": "特别关注", "level": "red"}]
        save_review(self.db, first["id"], content_hash(first["content"]), annotations=marks, notes="生成前的个人心得")
        result = await self.sm.run_period("weekly", start, end)
        self.assertEqual(len(self.llm.calls), 1)
        system, body, options = self.llm.calls[0]
        self.assertIn("全文篇幅上限为 4000 字", system)
        self.assertEqual(options["max_tokens"], output_token_budget(4000))
        self.assertNotIn("原始聊天正文", body)
        for row in rows:
            self.assertIn(row["content"], body)
        self.assertLess(body.index("【用户特别重点（红色）】"), body.index("【用户一般重点（蓝色）】"))
        self.assertIn("个人观点，不是群聊事实", body)
        self.assertIn("生成前的个人心得", body)
        save_review(self.db, first["id"], content_hash(first["content"]), notes="生成后修改的心得", read_state="deep")
        self.sm.scfg["target_chars"] = 1000
        cached = await self.sm.run_period("weekly", start, end)
        self.assertEqual(cached["id"], result["id"])
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(get_review(self.store.conn, first["id"], first["content"])["notes"], "生成后修改的心得")

    async def test_month_uses_complete_inner_weeks_and_edge_days_with_late_notes(self):
        start, end = date(2026, 8, 1), date(2026, 9, 1)
        daily = {}
        for i in range(31):
            day = start + timedelta(days=i)
            daily[day] = self.saved("daily", day, day + timedelta(days=1), f"# 日报\n日报正文唯一标记 {day}")
        weekly = []
        for kind, begin, stop in period_parts("monthly", start, end):
            if kind == "weekly":
                weekly.append(self.saved("weekly", begin, stop, f"# 周报\n完整月内周报 {begin}", 7))
        self.saved("weekly", date(2026, 7, 27), date(2026, 8, 3), "不得混入前月的跨月周报", 7)
        self.saved("weekly", date(2026, 8, 31), date(2026, 9, 7), "不得混入次月的跨月周报", 7)
        row = daily[date(2026, 8, 5)]
        save_review(self.db, row["id"], content_hash(row["content"]),
                    notes="周报生成之后补写的日报心得", annotations=[
                        {"start": 0, "end": 4, "text": "事后重点", "level": "red"}])
        row = weekly[0]
        save_review(self.db, row["id"], content_hash(row["content"]), notes="周报自己的个人心得")
        result = await self.sm.run_period("monthly", start, end)
        self.assertEqual((result["kind"], result["count"]), ("monthly", 31))
        self.assertEqual(len(self.llm.calls), 1)
        system, body, options = self.llm.calls[0]
        self.assertIn("全文篇幅上限为 6000 字", system)
        self.assertEqual(options["max_tokens"], output_token_budget(6000))
        self.assertNotIn("跨月周报", body)
        for row in weekly:
            self.assertIn(row["content"], body)
        for day in (date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 31)):
            self.assertIn(daily[day]["content"], body)
        self.assertNotIn("日报正文唯一标记 2026-08-05", body)
        self.assertIn("2026-08-05 日报最新重点与心得补充", body)
        self.assertIn("周报生成之后补写的日报心得", body)
        self.assertIn("事后重点", body)
        self.assertIn("周报自己的个人心得", body)
        self.assertEqual((await self.sm.run_period("monthly", start, end))["id"], result["id"])
        self.assertEqual(len(self.llm.calls), 1)

    async def test_missing_daily_blocks_week_without_implicit_generation(self):
        start, end = date(2026, 8, 3), date(2026, 8, 10)
        self.add_message(start)
        with self.assertRaises(MissingSummary) as raised:
            await self.sm.run_period("weekly", start, end)
        self.assertEqual((raised.exception.kind, raised.exception.start, raised.exception.end),
                         ("daily", start, start + timedelta(days=1)))
        self.assertFalse(self.llm.calls)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0], 0)

    async def test_missing_week_is_not_empty_after_raw_messages_were_removed(self):
        start = date(2026, 8, 3)
        self.saved("daily", start, start + timedelta(days=1), "已保留日报，原始消息已清理")
        with self.assertRaises(MissingSummary) as raised:
            await self.sm.run_period("monthly", date(2026, 8, 1), date(2026, 9, 1))
        self.assertEqual(raised.exception.kind, "weekly")
        self.assertFalse(self.llm.calls)

    async def test_empty_periods_do_not_call_model_even_without_api(self):
        self.llm.available = False
        for kind, start, end in [("daily", date(2026, 8, 3), date(2026, 8, 4)),
                                 ("weekly", date(2026, 8, 3), date(2026, 8, 10)),
                                 ("monthly", date(2026, 8, 1), date(2026, 9, 1))]:
            self.assertIsNone(await self.sm.run_period(kind, start, end))
        self.assertFalse(self.llm.calls)

    async def test_unfinished_period_rejected_and_generation_lock_preserved(self):
        today = datetime.now(BEIJING).date()
        with self.assertRaisesRegex(ValueError, "已经结束"):
            await self.sm.run_period("daily", today, today + timedelta(days=1))
        start, end = date(2026, 8, 3), date(2026, 8, 10)
        self.week(start)
        self.llm.waiting, self.llm.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.sm.run_period("weekly", start, end))
        await self.llm.waiting.wait()
        try:
            with self.assertRaisesRegex(RuntimeError, "正在生成"):
                await self.sm.run_period("weekly", start, end)
        finally:
            self.llm.release.set()
            await task
        self.assertEqual(len(self.llm.calls), 1)

    async def test_failed_model_does_not_save_complete_period(self):
        start, end = date(2026, 8, 3), date(2026, 8, 10)
        self.week(start)
        self.llm.error = RuntimeError("离线模拟失败")
        with self.assertRaisesRegex(RuntimeError, "离线模拟失败"):
            await self.sm.run_period("weekly", start, end)
        self.assertIsNone(self.store.summary_for_window("weekly", self.sm._day_bounds(start)[0], self.sm._day_bounds(end)[0]))
        self.assertEqual(self.sm.progress["state"], "failed")


if __name__ == "__main__":
    unittest.main()
