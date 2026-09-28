"""阅读元数据可脱离服务保存，并保留正文、并发更新与版本隔离。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from msgx.summary_review import content_hash, get_review, save_review


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "reviews.db"
        self.content = "# 标题\n\n**主题**\n完整正文🙂。"
        self.digest = content_hash(self.content)
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE summaries(id INTEGER PRIMARY KEY,content TEXT)")
        self.conn.execute("INSERT INTO summaries VALUES(1,?)", (self.content,))
        self.conn.commit()
        self.mark = {"start": 20, "end": 24, "text": "正文🙂", "level": "red"}

    def test_read_only_defaults_do_not_migrate(self):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as ro:
            self.assertEqual(get_review(ro, 1, self.content), {
                "content_hash": self.digest, "read_state": "unread", "annotations": [], "notes": ""})
        self.assertEqual(self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                         [("summaries",)])

    def test_partial_saves_survive_reopen_and_preserve_original(self):
        saved = save_review(self.path, 1, self.digest, annotations=[self.mark], notes="个人心得")
        self.assertEqual(saved["annotations"], [self.mark])
        saved = save_review(self.path, 1, self.digest, read_state="deep")
        self.assertEqual(saved["annotations"], [self.mark])
        self.assertEqual(saved["notes"], "个人心得")
        with closing(sqlite3.connect(self.path)) as reopened:
            self.assertEqual(get_review(reopened, 1, self.content), saved)
            self.assertEqual(reopened.execute("SELECT content FROM summaries WHERE id=1").fetchone()[0], self.content)
            self.assertEqual({r[0] for r in reopened.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                             {"summaries", "summary_reviews"})
        cleared = save_review(self.path, 1, self.digest, annotations=[], notes="")
        self.assertEqual((cleared["annotations"], cleared["notes"], cleared["read_state"]), ([], "", "deep"))

    def test_concurrent_field_updates_are_not_lost(self):
        barrier = Barrier(2)
        def update(**kwargs):
            barrier.wait()
            return save_review(self.path, 1, self.digest, **kwargs)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(update, notes="并发心得"), pool.submit(update, read_state="skimmed")]
            for future in futures:
                future.result()
        review = get_review(self.conn, 1, self.content)
        self.assertEqual((review["notes"], review["read_state"]), ("并发心得", "skimmed"))

    def test_wrong_version_rejected_and_new_id_has_no_marks(self):
        save_review(self.path, 1, self.digest, read_state="deep", annotations=[self.mark])
        changed = self.content + "更正"
        self.conn.execute("UPDATE summaries SET content=? WHERE id=1", (changed,))
        self.conn.execute("INSERT INTO summaries VALUES(2,?)", (self.content,))
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "正文已变化"):
            save_review(self.path, 1, self.digest, notes="不应写入")
        self.assertEqual(get_review(self.conn, 1, changed)["annotations"], [])
        self.assertEqual(get_review(self.conn, 2, self.content)["read_state"], "unread")

    def test_invalid_input_and_missing_database_do_not_create_metadata(self):
        for kwargs in ({"read_state": "invalid"}, {"annotations": [{**self.mark, "start": -1}]},
                       {"annotations": [{**self.mark, "level": "green"}]}, {"notes": 1}):
            with self.assertRaises(ValueError):
                save_review(self.path, 1, self.digest, **kwargs)
        with self.assertRaisesRegex(ValueError, "不存在"):
            save_review(self.path, 999, self.digest, notes="不应写入")
        missing = self.path.with_name("missing.db")
        with self.assertRaises(sqlite3.OperationalError):
            save_review(missing, 1, self.digest, notes="不应创建库")
        self.assertFalse(missing.exists())
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='summary_reviews'").fetchone())


if __name__ == "__main__":
    unittest.main()
