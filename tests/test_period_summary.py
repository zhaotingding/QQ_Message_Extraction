"""日、周、月分层摘要：真实临时数据库与假模型，不产生 API 费用。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from msgx.store import Store
from msgx import summarize as S
from msgx.llm import IncompleteResponse
from msgx.models import Msg
from msgx.summarize import BEIJING, MissingSummary, Summarizer, output_token_budget, period_parts, summary_chars
from msgx.summary_review import content_hash, get_review, save_review


class FakeLLM:
    available = True

    def __init__(self):
        self.calls = []
        self.error = None
        self.waiting = None
        self.release = None
        self.replies = []  # 顺序模拟完整、空白、失败和分块响应。

    async def chat(self, system, body, **kwargs):
        self.calls.append((system, body, kwargs))
        if self.waiting:
            self.waiting.set()
            await self.release.wait()
        if self.error:
            raise self.error
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return "# 周期报告\n\n**主题**\n完整归纳结果。"


class PeriodPartsTests(unittest.TestCase):
    def test_prompt_template_token_estimate_and_output_budget(self):
        self.assertEqual(S.digest_system(4500, "约{{目标字数}}字 {custom}"), "约4500字 {custom}")
        self.assertLess(output_token_budget(1000), output_token_budget(3000))
        self.assertLess(output_token_budget(3000), output_token_budget(6000))
        self.assertEqual(output_token_budget(3000), 10240)
        self.assertGreater(S.estimate_tokens("1234中文😀"), S.estimate_tokens("abcd"))
        self.assertEqual(summary_chars("# 标题\n\n**主题**\n内容。"), len("标题主题内容。"))

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

    def add_messages(self, day, count, text="消息"):
        stamp = self.sm._day_bounds(day)[0]  # 所有合成消息都在同一完整自然日内。
        self.store.conn.executemany(
            "INSERT INTO messages(group_id,msg_id,ts,text) VALUES('g',?,?,?)",
            [(str(i), stamp, f"{text}【消息{i}】") for i in range(count)],
        )
        self.store.conn.commit()
        return [int(row["id"]) for row in self.store.messages_between_all(stamp - 1, stamp)]

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
        changes = self.store.conn.total_changes  # 状态接口只读取已有报告与消息，不能触发生成或写库。
        status = self.sm.daily_status(start.isoformat(), end.isoformat())
        self.assertEqual([day["date"] for day in status["days"]], [start.isoformat(), end.isoformat()])
        self.assertEqual([day["status"] for day in status["days"]], ["done", "missing"])
        self.assertEqual(status["days"][0]["count"], 1)
        self.assertEqual(self.store.conn.total_changes, changes)
        self.assertEqual(len(self.llm.calls), 1)
        page = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn(f"{start} 00:00 — {end} 00:00", page)
        self.assertIn("上限 2000 字", page)
        self.assertEqual(result["chars"], summary_chars(result["content"]))

    async def test_all_messages_over_8000_and_complete_report_are_preserved(self):
        start, end = date(2026, 8, 3), date(2026, 8, 4)
        ids = self.add_messages(start, 8005)
        self.store.conn.execute("UPDATE messages SET media_state='filtered' WHERE id=?", (ids[4],))
        self.store.conn.commit()
        self.sm.scfg.update(target_chars=4500, prompt="请总结成{{目标字数}}字 {literal}")
        complete = "# 总标题\n\n**主题**\n" + "完整内容。" * 1000 + "完整结尾标记。"
        self.llm.replies = [complete]  # 超出目标字数也必须一次完整落库，不追加改写。
        result = await self.sm.run_period("daily", start, end)
        self.assertEqual(len(self.llm.calls), 1)
        system, body, options = self.llm.calls[0]
        self.assertEqual(result["count"], 8004)
        self.assertIn("【消息0】", body)
        self.assertIn("【消息8004】", body)
        self.assertNotIn("【消息4】", body)
        self.assertEqual(options["source_ids"], [rid for rid in ids if rid != ids[4]])
        self.assertTrue(system.startswith("请总结成4500字 {literal}"))
        self.assertEqual(options["max_tokens"], output_token_budget(4500))
        self.assertTrue(options["require_complete"])
        self.assertFalse(options["think"])
        self.assertEqual(result["content"], complete)
        self.assertEqual(self.store.summary_for_window("daily", *self.sm._day_bounds(start))["content"], complete)
        page = Path(result["path"]).read_text(encoding="utf-8")
        self.assertEqual(page.count("<h1>"), 1)
        self.assertIn("<strong>主题</strong>", page)
        self.assertIn("完整结尾标记。", page)
        self.assertIn(f"全文 {result['chars']} 字 / 上限 4500 字", page)
        self.assertTrue(Path(result["path"]).is_absolute())
        self.assertFalse(self.sm._digest_lock.locked())

    async def test_picture_material_requires_selection_and_prefers_saved_semantics(self):
        cases = [(False, False, "手动语义", None), (True, False, "手动语义", "手动语义"),
                 (False, True, "手动语义", None), (True, True, "手动语义", "手动语义"),
                 (True, True, "", "本地OCR")]
        for index, (include, filtered, vision, expected) in enumerate(cases):
            with self.subTest(include=include, filtered=filtered, vision=vision):
                day = date(2026, 8, 3) + timedelta(days=index)  # 每种选择使用独立日期，避免命中已完成摘要。
                self.add_message(day, "图片讨论[图片]")
                row = self.store.conn.execute("SELECT id FROM messages WHERE msg_id=?", (day.isoformat(),)).fetchone()
                self.store.conn.execute("UPDATE messages SET has_media=1,ocr_text='本地OCR',vision_text=?,media_state=? WHERE id=?",
                                        (vision, "filtered" if filtered else "done", row["id"]))
                self.store.conn.commit()
                self.store.set_vision_include(row["id"], include)
                before = len(self.llm.calls)
                result = await self.sm.run_period("daily", day, day + timedelta(days=1))
                if filtered and not include:
                    self.assertIsNone(result)
                    self.assertEqual(len(self.llm.calls), before)
                    continue
                self.assertEqual(len(self.llm.calls), before + 1)
                body = self.llm.calls[-1][1]
                for material in ("本地OCR", "手动语义"):
                    self.assertEqual(material in body, material == expected)

    async def test_each_image_is_selected_independently_with_complete_ocr_fallback(self):
        semantics = "第二张语义" * 200 + "语义尾部"  # 已存语义与 OCR 都应完整复用。
        for index, (include, vision) in enumerate([(False, semantics), (True, semantics), (True, "")]):
            with self.subTest(include=include, has_semantics=bool(vision)):
                day = date(2026, 8, 3) + timedelta(days=index)
                row_id = self.store.insert_message(Msg(
                    msg_id=str(index), group_id="g", ts=self.sm._day_bounds(day)[0], text="两张截图[图片][图片]",
                    image_urls=["https://a.invalid/1", "https://a.invalid/2"],
                ))
                images = self.store.conn.execute("SELECT id FROM message_images WHERE message_row=? ORDER BY image_index", (row_id,)).fetchall()
                ocr = "第二张OCR" * 200 + "OCR尾部"  # 长结果不能在整理材料时被截断。
                self.store.set_image_result(images[0]["id"], media_state="done", ocr_text="第一张OCR", vision_text="第一张语义")
                self.store.set_image_result(images[1]["id"], media_state="done", ocr_text=ocr, vision_text=vision)
                self.store.set_image_include(images[1]["id"], include)
                await self.sm.run_period("daily", day, day + timedelta(days=1))
                body = self.llm.calls[-1][1]
                self.assertNotIn("第一张OCR", body)
                self.assertNotIn("第一张语义", body)
                self.assertEqual(semantics in body, include and bool(vision))
                self.assertEqual(ocr in body, include and not vision)

    async def test_daily_sends_body_in_time_order_without_sender_or_group(self):
        day = date(2026, 8, 3)
        stamp = self.sm._day_bounds(day)[0]
        for index, (offset, body) in enumerate([(2, "第二时刻甲[图片]"), (1, "第一时刻"), (2, "第二时刻乙")]):
            self.store.insert_message(Msg(msg_id=str(index), group_id="secret-group", sender_name="secret-sender",
                                          ts=stamp + offset, text=body), "secret-name")
        await self.sm.run_period("daily", day, day + timedelta(days=1))
        self.assertEqual(self.llm.calls[0][1], "第一时刻\n\n第二时刻甲\n\n第二时刻乙")

    async def test_picture_without_text_does_not_request_model(self):
        day = date(2026, 8, 3)
        self.add_message(day, "[图片]")
        self.assertIsNone(await self.sm.run_period("daily", day, day + timedelta(days=1)))
        self.assertEqual(self.sm.daily_status(day.isoformat(), day.isoformat())["days"][0]["status"], "empty")
        self.assertFalse(self.llm.calls)

    def test_legacy_partial_status_is_read_without_new_generation(self):
        today = datetime.now(BEIJING).date()  # 旧版本已保存的当日部分报告仍可查询。
        start = self.sm._day_bounds(today)[0]
        self.store.add_summary("digest", start, int(datetime.now(BEIJING).timestamp()),
                               1, "旧版部分报告", "daily_partial")
        changes = self.store.conn.total_changes
        status = self.sm.daily_status(today.isoformat(), today.isoformat())["days"][0]
        self.assertEqual((status["status"], status["count"]), ("partial", 1))
        self.assertEqual(self.store.conn.total_changes, changes)
        self.assertFalse(self.llm.calls)  # 保留读取兼容，不恢复旧的部分报告生成入口。

    async def test_daily_failure_never_saves_a_report_and_keeps_incomplete_draft(self):
        day = date(2026, 8, 3)
        self.add_message(day, "需要归纳的消息")
        for failure in (RuntimeError("API unavailable"), "", IncompleteResponse("尚未结束的正文", "length", 10240, 10240)):
            with self.subTest(failure=type(failure).__name__):
                self.llm.replies = [failure]
                before = len(self.llm.calls)
                with self.assertRaises(RuntimeError) as raised:
                    await self.sm.run_period("daily", day, day + timedelta(days=1))
                self.assertEqual(len(self.llm.calls), before + 1)
                self.assertIsNone(self.store.summary_for_window("daily", *self.sm._day_bounds(day)))
                self.assertEqual(self.sm.progress["state"], "failed")
                self.assertFalse(self.sm._digest_lock.locked())
                if isinstance(failure, IncompleteResponse):
                    drafts = list((self.sm.report_dir / "unfinished").glob("*.txt"))
                    self.assertEqual(len(drafts), 1)
                    self.assertEqual(drafts[0].read_text(encoding="utf-8"), failure.content)
                    self.assertIn(str(drafts[0]), str(raised.exception))

    async def test_chunk_retry_reuses_completed_parts_and_preserves_source_ids(self):
        day = date(2026, 8, 3)
        ids = self.add_messages(day, 12, "正文" * 500)
        self.sm.scfg["target_chars"] = 500
        self.llm.replies = ["# 第一批\n完整归纳。", RuntimeError("第二批失败")]
        with patch.object(S, "model_limits", return_value=(12000, 10000)):
            with self.assertRaisesRegex(RuntimeError, "第二批失败"):
                await self.sm.run_period("daily", day, day + timedelta(days=1))
            self.assertEqual(len(self.llm.calls), 2)
            self.assertIsNone(self.store.summary_for_window("daily", *self.sm._day_bounds(day)))
            first_body = self.llm.calls[0][1]
            result = await self.sm.run_period("daily", day, day + timedelta(days=1))
        self.assertEqual(sum(call[1] == first_body for call in self.llm.calls), 1)
        self.assertEqual(self.llm.calls[-1][2]["source_ids"], ids)
        self.assertEqual(result["count"], 12)
        self.assertTrue(list((self.sm.report_dir / ".summary-cache").glob("*.json")))
        for system, body, options in self.llm.calls:
            self.assertEqual(options["purpose"], "摘要总结")
            self.assertLessEqual(S.estimate_tokens(system) + S.estimate_tokens(body) + 128 + options["max_tokens"] + 2048, 12000)

    async def test_single_long_message_keeps_tail_and_source_id_for_every_part(self):
        day = date(2026, 8, 3)
        ids = self.add_messages(day, 1, "长消息内容" * 3000 + "完整长消息尾部")
        self.sm.scfg["target_chars"] = 500
        with patch.object(S, "model_limits", return_value=(12000, 10000)):
            await self.sm.run_period("daily", day, day + timedelta(days=1))
        self.assertGreater(len(self.llm.calls), 1)
        self.assertTrue(any("完整长消息尾部" in call[1] for call in self.llm.calls))
        self.assertTrue(all(call[2]["source_ids"] == ids for call in self.llm.calls))

    async def test_invalid_budget_and_excessive_prompt_fail_before_api(self):
        day = date(2026, 8, 3)
        self.add_message(day)
        for target, prompt in [(-1, ""), (200000, ""), (5000, ""), (500, "巨大提示词" * 5000)]:
            with self.subTest(target=target), patch.object(S, "model_limits", return_value=(12000, 10000)):
                self.sm.scfg.update(target_chars=target, prompt=prompt)
                with self.assertRaises(ValueError):
                    await self.sm.run_period("daily", day, day + timedelta(days=1))
                self.assertFalse(self.llm.calls)

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
