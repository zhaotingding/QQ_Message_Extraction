"""关键词命中、逐图文字、独立历史分页和只读查询回归；不调用 API。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.message_keywords import compile_rules, matching_message
from msgx.models import parse_onebot_event
from msgx.store import Store
from msgx import prefs


class MessageKeywordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.db"
        self.store = Store(self.path, filters={"blocked_senders": ["智能客服"]})
        self.addCleanup(self.store.close)
        self.seq = 0

    def add(self, text, group="1", sender="群友", images=False):
        self.seq += 1
        segments = [{"type": "text", "data": {"text": text}}]
        if images:
            segments += [{"type": "image", "data": {"url": f"https://example.invalid/{n}.png"}} for n in (1, 2)]
        event = {"post_type": "message", "message_type": "group", "group_id": group,
                 "user_id": "200", "self_id": "100", "message_id": str(self.seq),
                 "time": 1000 + self.seq, "sender": {"nickname": sender}, "message": segments}
        return self.store.insert_message(parse_onebot_event(event, "100"))

    def test_literal_regex_case_and_validation(self):
        rules = compile_rules(["C++", "re:价格\\s*[:：]\\s*\\d+", "Codex", "C++", " "])
        self.assertEqual(matching_message(rules, {"text": "CODEX 价格：30 C++"}),
                         ["C++", "re:价格\\s*[:：]\\s*\\d+", "Codex"])
        self.assertEqual(matching_message(rules, {"text": "CCCC", "sender_name": "Codex"}), [])
        self.assertEqual(matching_message(compile_rules(["re:^$"]), {"text": "正常消息"}), [])
        for lines, expected in ((["普通", "re:["], "第 2 行"), (["re:"], "不能为空")):
            with self.assertRaisesRegex(ValueError, expected):
                compile_rules(lines)

    def test_text_ocr_semantics_any_rule_and_sender_group_filters(self):
        text = self.add("Codex 的价格：30")
        image = self.add("图片说明", images=True)
        children = self.store.conn.execute("SELECT id FROM message_images WHERE message_row=? ORDER BY image_index", (image,)).fetchall()
        self.store.set_image_result(children[0][0], ocr_text="用量限制", media_state="done")
        self.store.set_image_result(children[1][0], vision_text="额度恢复时间", media_state="done")
        self.assertEqual(self.store.conn.execute("SELECT include_vision,ocr_text,vision_text FROM messages WHERE id=?", (image,)).fetchone()[0], 0)
        self.add("Codex", sender="智能客服")
        other = self.add("Codex", group="2")
        rules = compile_rules(["codex", "re:限制|恢复"])
        rows = self.store.recent_messages(keyword_rules=rules)
        self.assertEqual([row["id"] for row in rows], [other, image, text])
        self.assertEqual(matching_message(rules, rows[1]), ["re:限制|恢复"])
        self.assertEqual([row["id"] for row in self.store.recent_messages(group_id="1", keyword_rules=rules)], [image, text])
        # OCR 后写入后再次查询即可出现，不依赖图片纳入摘要开关。
        self.assertEqual([row["id"] for row in self.store.recent_messages(keyword_rules=compile_rules(["用量限制"]))], [image])
        self.assertEqual(self.store.recent_messages(keyword_rules=()), [])

    def test_history_is_matched_before_limit_and_pages_do_not_duplicate(self):
        old = self.add("关键消息")
        for _ in range(230):
            self.add("普通消息")
        self.assertNotIn(old, [row["id"] for row in self.store.recent_messages(200)])
        self.assertEqual([row["id"] for row in self.store.recent_messages(200, keyword_rules=compile_rules(["关键"]))], [old])
        for _ in range(205):
            self.add("关键消息")
        rules = compile_rules(["关键"])
        first = self.store.recent_messages(200, keyword_rules=rules)
        rest = self.store.recent_messages(200, keyword_rules=rules, offset=200)
        self.assertEqual((len(first), len(rest)), (200, 6))
        self.assertFalse({row["id"] for row in first} & {row["id"] for row in rest})

    def test_rules_and_messages_survive_reopening_without_writes(self):
        ident = self.add("持久关键词")
        path = Path(self.temp.name) / "prefs.json"
        prefs.save({"message_keywords": ["持久"], "qq_account": "100"}, path)
        settings = prefs.load(path)
        self.assertEqual(settings["qq_account"], "100")
        reader = Store(self.path, readonly=True)
        try:
            self.assertEqual([row["id"] for row in reader.recent_messages(keyword_rules=compile_rules(settings["message_keywords"]))], [ident])
            with self.assertRaises(sqlite3.OperationalError):
                reader.conn.execute("DELETE FROM messages")
        finally:
            reader.close()
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        missing = Path(self.temp.name) / "missing" / "test.db"
        with self.assertRaises(sqlite3.OperationalError):
            Store(missing, readonly=True)
        self.assertFalse(missing.parent.exists())

    def test_sparse_keyword_query_in_large_history_uses_bounded_sql_work(self):
        ident = self.add("很早的关键消息")
        self.store.conn.executemany(
            "INSERT INTO messages(group_id,msg_id,sender_id,ts,text) VALUES('1',?,'200',?,'普通消息')",
            ((f"bulk-{i}", 2000+i) for i in range(135000)))
        self.store.conn.commit()
        steps = 0
        def budget():
            nonlocal steps
            steps += 10000
            return steps > 20000000
        self.store.conn.set_progress_handler(budget, 10000)
        try:
            self.assertEqual([row["id"] for row in self.store.recent_messages(keyword_rules=compile_rules(["关键"]))], [ident])
        finally:
            self.store.conn.set_progress_handler(None, 0)


if __name__ == "__main__":
    unittest.main()
