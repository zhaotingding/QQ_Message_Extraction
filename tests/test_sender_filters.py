"""发言人名单：合成消息/内存库验证，不访问 QQ、模型或用户配置。"""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx import prefs
from msgx.config import DEFAULTS
from msgx.models import Msg
from msgx.pipeline import Pipeline
from msgx.store import Store

DAY = datetime(2026, 10, 3, tzinfo=timezone(timedelta(hours=8)))
START = int(DAY.timestamp())


class SenderFilterTests(unittest.TestCase):
    def test_defaults_and_explicit_empty_survive_save_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.json"
            prefs.save({"filters": {"skip_image_only": True}}, path)
            self.assertEqual(prefs.load(path)["filters"]["blocked_senders"], ["Q群管家", "智能客服"])
            for names in (["自定义客服", "O'Brien"], []):
                prefs.save({"filters": {"blocked_senders": names}, "unrelated": 123}, path)
                loaded = prefs.load(path)
                self.assertEqual(loaded["unrelated"], 123)
                with patch.dict("os.environ", {"MSGX_PREFS": str(path)}):
                    cfg = prefs.apply_to_config(DEFAULTS, prefs.load_from_env())
                self.assertEqual(cfg["filters"]["blocked_senders"], names)

    def test_names_are_exact_and_removal_disables_manager_account_rule(self):
        for name in ("Q群管家", " 智能客服 "):
            self.assertTrue(prefs.skip_reason(Msg(msg_id="test", group_id="g", sender_name=name), {}))
        for name in ("我是智能客服", "智能客服助手", "普通人"):
            self.assertFalse(prefs.skip_reason(Msg(msg_id="test", group_id="g", sender_name=name, text="智能客服"), {}))
        self.assertTrue(prefs.skip_reason(Msg(msg_id="test", group_id="g", sender_id="2854196310", sender_name="新群名片"), {}))
        self.assertFalse(prefs.skip_reason(Msg(msg_id="test", group_id="g", sender_id="2854196310", sender_name="Q群管家"),
                                           {"blocked_senders": []}))
        self.assertEqual(prefs.blocked_senders({"blocked_senders": [" 自定义 ", "自定义", ""]}), ["自定义"])

    def test_history_queries_share_filter_and_restore_without_deleting(self):
        filters = {"blocked_senders": ["智能客服", "O'Brien", "x') OR 1=1 --"]}
        store = Store(":memory:", filters=filters)
        self.addCleanup(store.close)
        regular = store.insert_message(Msg(msg_id="normal", group_id="g", ts=START + 5,
                                            sender_name="普通人", text="保留正文"))
        for i, name in enumerate(filters["blocked_senders"]):
            row_id = store.insert_message(Msg(msg_id=str(i), group_id="g", ts=START + i + 10,
                                              sender_name=name, text="[图片]广告", image_urls=["https://invalid.test/a"]))
            image_id = store.conn.execute("SELECT id FROM message_images WHERE message_row=?", (row_id,)).fetchone()[0]
            store.set_image_result(image_id, ocr_text="广告 OCR", media_state="pending")
            store.set_image_include(image_id, True)
        count = store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual([r["id"] for r in store.recent_messages(limit=1)], [regular])
        self.assertEqual(store.recent_messages(images_only=True), [])
        activity = store.group_activity(DAY)["groups"][0]
        self.assertEqual((activity["count"], activity["last_ts"]), (1, START + 5))
        self.assertEqual([r["id"] for r in store.messages_between("g", START, START + 86400)], [regular])
        self.assertEqual([r["id"] for r in store.messages_between_all(START, START + 86400, 1)], [regular])
        self.assertEqual(store.count_messages_window(START, START + 86400), 1)
        self.assertEqual(store.first_ts("g"), START + 5)
        self.assertEqual(store.first_ts_all(), START + 5)
        self.assertEqual(store.pending_images(), [])
        filters["blocked_senders"] = []
        self.assertEqual(len(store.recent_messages()), count)
        self.assertEqual(store.group_activity(DAY)["groups"][0]["count"], count)
        self.assertEqual(len(store.messages_between_all(START, START + 86400)), count)
        self.assertEqual(len(store.pending_images()), 3)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], count)

    def test_filtered_only_day_has_no_summary_input(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.insert_message(Msg(msg_id="1", group_id="g", ts=START + 1, sender_name="智能客服", text="签到"))
        self.assertEqual(store.first_ts_all(), 0)
        self.assertEqual(store.first_ts("g"), 0)
        self.assertEqual(store.count_messages_window(START, START + 86400), 0)
        self.assertEqual(store.messages_between_all(START, START + 86400), [])


class SenderIngestTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_sender_images_dropped_before_ocr_and_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = copy.deepcopy(DEFAULTS)
            cfg["app"].update(db_path=str(Path(tmp) / "test.db"), media_dir=str(Path(tmp) / "media"))
            # 与类型过滤原因同名也必须直接丢弃，不能误走保留图片分支。
            cfg["filters"]["blocked_senders"] = ["自定义客服", "纯图片"]
            with patch("msgx.pipeline.api_key", return_value=""):
                pipe = Pipeline(cfg)
            pipe.media.process_pending = AsyncMock(side_effect=AssertionError("不能做 OCR"))
            pipe.llm._post = AsyncMock(side_effect=AssertionError("不能调用 API"))
            try:
                for mid, name in enumerate(cfg["filters"]["blocked_senders"]):
                    result = await pipe.handle_event({"post_type": "message", "message_type": "group",
                        "self_id": 100, "group_id": 200, "user_id": 300, "message_id": mid,
                        "time": START + 1, "sender": {"nickname": name},
                        "message": [{"type": "image", "data": {"url": "https://invalid.test/a"}}]})
                    self.assertIn("过滤", result["reason"])
                self.assertEqual(pipe.store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
                self.assertEqual(pipe.store.pending_images(), [])
                pipe.media.process_pending.assert_not_awaited()
                pipe.llm._post.assert_not_awaited()
            finally:
                pipe.store.close()


if __name__ == "__main__":
    unittest.main()
