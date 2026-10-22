"""实时群筛选与北京时间入库榜：隔离数据库及离线接口，不调用 QQ 或模型。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.models import Msg
from msgx.onebot import _messages_recent
from msgx.store import Store


BEIJING = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 30, 12, tzinfo=BEIJING)
START = int(NOW.replace(hour=0).timestamp())
END = START + 86400


class ActivityFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "activity.db")
        self.addCleanup(self.store.close)
        self.sequence = 0

    def add_message(self, group="1", ts=START + 1, *, name="", images=(), filtered=False):
        self.sequence += 1
        msg = Msg(msg_id=str(self.sequence), group_id=group, sender_id="sender", ts=ts,
                  text="图片消息" if images else "文字消息", image_urls=list(images))
        return self.store.insert_message(msg, group_name=name, bot_uin="account-a", filtered=filtered)

    def activity(self, now=NOW):
        return self.store.group_activity(now)


class GroupActivityTests(ActivityFixture, unittest.TestCase):
    def test_legacy_manager_hidden_before_limits_without_deleting_records(self):
        normal = self.add_message("1", START + 10, images=("normal-image",))
        for mid, sender_id, name, ts in (("bot1", "2854196310", "改名管家", START + 1),
                                        ("bot2", "other", " Q群管家 ", START + 20)):
            self.store.insert_message(Msg(msg_id=mid, group_id="1", sender_id=sender_id,
                sender_name=name, ts=ts, text="广告[图片]", image_urls=["bot-image"]))
        before = list(self.store.conn.execute("SELECT * FROM messages ORDER BY id"))
        self.assertEqual([r["id"] for r in self.store.recent_messages(1)], [normal])
        self.assertEqual([r["id"] for r in self.store.recent_messages(1, True, "1")], [normal])
        self.assertEqual([r["id"] for r in self.store.messages_between_all(START - 1, END, 1)], [normal])
        self.assertEqual([r["id"] for r in self.store.recent_messages(group_id="1")], [normal])  # 群筛选仍隐藏管家。
        self.assertEqual(self.activity()["groups"][0]["count"], 1)
        self.assertEqual(self.activity()["groups"][0]["last_ts"], START + 10)
        self.assertEqual(list(self.store.conn.execute("SELECT * FROM messages ORDER BY id")), before)

    def test_beijing_midnight_is_left_closed_right_open_and_rolls_over(self):
        for ts in (START - 1, START, END - 1, END):
            self.add_message(ts=ts)
        current = self.activity()
        self.assertEqual((current["date"], current["window_start"], current["window_end"]),
                         ("2026-09-30", START, END))
        self.assertEqual(current["groups"][0]["count"], 2)
        self.assertEqual(current["groups"][0]["last_ts"], END - 1)
        following = self.activity(datetime(2026, 10, 1, tzinfo=BEIJING))
        self.assertEqual(following["date"], "2026-10-01")
        self.assertEqual(following["groups"][0]["count"], 1)
        self.assertEqual(following["groups"][0]["last_ts"], END)

    def test_utc_and_naive_clock_do_not_use_host_timezone(self):
        utc = datetime(2026, 9, 29, 16, tzinfo=timezone.utc)
        self.assertEqual(self.activity(utc)["window_start"], START)
        self.assertEqual(self.activity(datetime(2026, 9, 30, 0))["window_start"], START)
        self.assertEqual(self.activity(utc - timedelta(seconds=1))["date"], "2026-09-29")

    def test_unfiltered_multi_image_and_receipts_count_as_one_message(self):
        msg = Msg(msg_id="shared", group_id="1", sender_id="sender", ts=START,
                  text="[图片][图片]", image_urls=["image-a", "image-b"],
                  raw={"message": [{"type": "image", "data": {"file": "image-a"}},
                                   {"type": "image", "data": {"file": "image-b"}}]})
        row_id = self.store.insert_message(msg, bot_uin="account-a")
        self.assertIsNotNone(row_id)
        self.assertIsNone(self.store.insert_message(msg, bot_uin="account-b"))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM message_images").fetchone()[0], 2)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM message_receipts").fetchone()[0], 2)
        self.assertEqual(self.activity()["groups"][0]["count"], 1)
        self.assertEqual(self.store.recent_messages()[0]["media_state"], "pending")

    def test_filtered_image_stays_visible_but_never_counts_even_when_included_in_summary(self):
        row_id = self.add_message(images=("filtered-image",), filtered=True)
        image_id = self.store.recent_messages()[0]["images"][0]["id"]
        self.store.set_image_result(image_id, ocr_text="需要保留的 OCR", media_state="done")
        self.store.set_image_include(image_id, True)
        row = self.store.recent_messages()[0]
        self.assertEqual((row["id"], row["media_state"], row["ocr_text"], row["include_vision"]),
                         (row_id, "filtered", "需要保留的 OCR", 1))
        self.assertEqual(self.store.messages_between_all(START - 1, END)[0]["summary_picture"], "需要保留的 OCR")
        group = self.activity()["groups"][0]
        self.assertEqual((group["count"], group["last_ts"]), (0, 0))

    def test_filtered_messages_do_not_raise_rank_or_latest_timestamp(self):
        self.add_message("a", START + 1)
        self.add_message("a", START + 2)
        self.add_message("b", START + 3)
        for offset in range(3):
            self.add_message("b", START + 10 + offset, filtered=True)
            self.add_message("only-filtered", START + 20 + offset, filtered=True)
        groups = self.activity()["groups"]
        self.assertEqual([(g["group_id"], g["count"], g["last_ts"]) for g in groups],
                         [("a", 2, START + 2), ("b", 1, START + 3), ("only-filtered", 0, 0)])

    def test_zero_message_historical_and_missing_group_metadata_remain_available(self):
        self.store.upsert_group("empty", "已知空群")
        self.add_message("old", START - 1, name="历史群")
        self.add_message("orphan", START - 2)
        self.store.conn.execute("DELETE FROM groups WHERE id='orphan'")
        self.store.conn.commit()
        groups = {row["group_id"]: row for row in self.activity()["groups"]}
        self.assertEqual(set(groups), {"empty", "old", "orphan"})
        self.assertEqual(groups["old"]["group_name"], "历史群")
        self.assertEqual(groups["orphan"]["group_name"], "")
        self.assertTrue(all(row["count"] == 0 and row["last_ts"] == 0 for row in groups.values()))
        self.assertEqual(len(self.store.recent_messages(group_id="old")), 1)

    def test_sort_by_count_then_latest_then_stable_group_id(self):
        self.add_message("z", START + 1)
        self.add_message("z", START + 2)
        self.add_message("a", START + 5)
        self.add_message("c", START + 6)
        self.add_message("b", START + 6)
        self.assertEqual([row["group_id"] for row in self.activity()["groups"]], ["z", "b", "c", "a"])

    def test_group_filter_is_before_limit_and_same_name_groups_are_distinct(self):
        wanted = self.add_message("1", START + 1, name="同名群")
        for offset in range(5):
            self.add_message("2", START + 2 + offset, name="同名群")
        rows = self.store.recent_messages(limit=1, group_id="1")
        self.assertEqual([row["id"] for row in rows], [wanted])
        self.assertEqual(self.store.recent_messages(limit=1, group_id="2")[0]["group_id"], "2")
        self.assertEqual(len(self.activity()["groups"]), 2)

    def test_group_filter_and_images_filter_combine_before_limit(self):
        wanted = self.add_message("1", START + 1, images=["image-a", "image-b"])
        self.add_message("1", START + 2)
        self.add_message("2", START + 3, images=["image-c"])
        rows = self.store.recent_messages(limit=1, images_only=True, group_id="1")
        self.assertEqual([row["id"] for row in rows], [wanted])
        self.assertEqual(len(rows[0]["images"]), 2)
        self.assertEqual(sum(group["count"] for group in self.activity()["groups"]), 3)

    def test_filter_uses_bound_group_id_and_unknown_group_is_empty(self):
        self.add_message("one's group")
        self.assertEqual(len(self.store.recent_messages(group_id="one's group")), 1)
        self.assertEqual(self.store.recent_messages(group_id="' OR 1=1 --"), [])
        self.assertEqual(self.store.recent_messages(group_id="unknown"), [])

    def test_activity_is_one_read_only_query(self):
        self.add_message()
        statements = []
        before = self.store.conn.total_changes
        self.store.conn.set_trace_callback(statements.append)
        self.activity()
        self.store.conn.set_trace_callback(None)
        self.assertEqual(len(statements), 1)
        self.assertTrue(statements[0].startswith("WITH known_groups"))
        self.assertEqual(self.store.conn.total_changes, before)


class GroupActivityRouteTests(ActivityFixture, unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_preserves_items_and_activity_ignores_message_filters(self):
        wanted = self.add_message("1", images=["image-a"], filtered=True)
        self.add_message("1", ts=START + 2)
        self.add_message("2", ts=START + 3)
        self.store.upsert_group("empty", "零消息群")
        pipe = SimpleNamespace(store=self.store, llm=Mock(), media=Mock(), bot=Mock())
        with patch("msgx.message_store.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            response = await _messages_recent(SimpleNamespace(
                query={"limit": "1", "images": "1", "group_id": "1"}, app={"pipe": pipe}))
        data = json.loads(response.text)
        self.assertEqual(response.status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual([row["id"] for row in data["items"]], [wanted])
        self.assertEqual(data["activity"]["date"], "2026-09-30")
        self.assertEqual({row["group_id"]: row["count"] for row in data["activity"]["groups"]},
                         {"1": 1, "2": 1, "empty": 0})
        for service in (pipe.llm, pipe.media, pipe.bot):
            self.assertEqual(service.mock_calls, [])

    async def test_endpoint_keeps_default_and_clamped_limit_contract(self):
        fake_store = SimpleNamespace(recent_messages=Mock(return_value=[]), group_activity=Mock(return_value={}))
        pipe = SimpleNamespace(store=fake_store)
        for value, expected in (("bad", 60), ("0", 1), ("999", 300)):
            with self.subTest(limit=value):
                with patch("msgx.onebot._read_store", side_effect=lambda source, query: query(source)):
                    response = await _messages_recent(SimpleNamespace(query={"limit": value}, app={"pipe": pipe}))
                self.assertEqual(response.status, 200)
                fake_store.recent_messages.assert_called_with(expected, images_only=False, group_id="", account="")


if __name__ == "__main__":
    unittest.main(verbosity=2)
