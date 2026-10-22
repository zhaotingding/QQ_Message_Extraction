"""存储重构契约：隔离库中验证查询成本、页面结果及一次性旧库回填。"""
import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.models import Msg
from msgx.store import Store


class StorageQueriesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "isolated.db"

    def open_store(self, readonly=False):
        store = Store(self.path, readonly=readonly)
        self.addCleanup(store.close)
        return store

    def test_stats_counts_and_cost_do_not_scan_all_message_bodies(self):
        store = self.open_store()
        store.conn.executemany(
            "INSERT INTO messages(group_id,msg_id,ts,text) VALUES('g',?,1,?)",
            ((str(number), "普通文字" * 20) for number in range(50000)))
        store.conn.execute("UPDATE messages SET media_state='pending' WHERE id=1")
        store.conn.execute("UPDATE messages SET vision_state='failed',media_path='saved.png',vision_tries=2 WHERE id=2")
        store.conn.execute("UPDATE messages SET vision_state='failed',media_path='saved.png',vision_tries=3 WHERE id=3")
        store.conn.commit()
        # 超预算就中断：没有条件索引时全表过滤会超过该 VM 指令预算。
        steps = [0]
        def budget():
            steps[0] += 1000
            return int(steps[0] > 5000)
        store.conn.set_progress_handler(budget, 1000)
        try:
            result = store.stats()
        finally:
            store.conn.set_progress_handler(None, 0)
        self.assertEqual(result["messages"], 50000)
        self.assertEqual((result["messages_pending_media"], result["vision_failed"], result["vision_retry"]), (1, 2, 1))

    def test_recent_messages_loads_all_images_in_two_queries(self):
        store = self.open_store()
        first = store.insert_message(Msg(group_id="g", msg_id="first", ts=1, text="多图", image_urls=["one", "two"]))
        latest = store.insert_message(Msg(group_id="g", msg_id="latest", ts=2, text="文字"))
        calls = []
        store.conn.set_trace_callback(calls.append)
        try:
            rows = store.recent_messages(200)
        finally:
            store.conn.set_trace_callback(None)
        self.assertEqual(len(calls), 2)
        self.assertEqual([row["id"] for row in rows], [latest, first])
        self.assertEqual(rows[0]["images"], [])
        self.assertEqual([image["image_url"] for image in rows[1]["images"]], ["one", "two"])
        self.assertEqual([image["image_index"] for image in rows[1]["images"]], [0, 1])

    def test_704_partial_old_records_are_backfilled_once_without_losing_results(self):
        store = Store(self.path)
        parent = store.insert_message(Msg(group_id="g", msg_id="old", ts=1, image_urls=["one", "two"]), bot_uin="100")
        children = list(store.conn.execute("SELECT id FROM message_images WHERE message_row=? ORDER BY image_index", (parent,)))
        store.set_image_result(children[0][0], ocr_text="已经保存的 OCR", media_state="done")
        store.set_image_include(children[0][0], True)
        store.conn.execute("DELETE FROM message_images WHERE id=?", (children[1][0],))
        store.conn.execute("DELETE FROM message_receipts")
        store.conn.execute("DELETE FROM group_accounts")
        store.conn.execute("PRAGMA user_version=704")
        store.conn.commit()
        store.close()
        migrated = Store(self.path)
        backup = self.path.with_name(self.path.name + ".pre-705.bak")
        self.assertTrue(backup.is_file())
        with closing(sqlite3.connect(backup)) as saved:
            self.assertEqual(saved.execute("PRAGMA user_version").fetchone()[0], 704)
            self.assertEqual(saved.execute("SELECT COUNT(*) FROM message_images").fetchone()[0], 1)
        images = migrated.recent_messages()[0]["images"]
        self.assertEqual([image["image_url"] for image in images], ["one", "two"])
        self.assertEqual(images[0]["ocr_text"], "已经保存的 OCR")
        self.assertEqual(images[0]["include_vision"], 1)
        self.assertTrue(migrated.has_receipt("100", "g", "old"))
        self.assertEqual(migrated.group_rows()[0][2], "100")
        self.assertEqual(migrated.conn.execute("PRAGMA user_version").fetchone()[0], 705)
        migrated.close()
        calls = []
        connect = sqlite3.connect
        def traced_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(calls.append)
            return connection
        with patch("msgx.store.sqlite3.connect", side_effect=traced_connect):
            reopened = Store(self.path)
        self.addCleanup(reopened.close)
        self.assertFalse(any("INSERT OR IGNORE INTO message_images" in sql
                             or "INSERT OR IGNORE INTO message_receipts SELECT" in sql
                             or "UPDATE messages SET vision_state" in sql for sql in calls))
        self.assertEqual(reopened.get_image(children[0][0])["include_vision"], 1)

    def test_page_reads_keep_owner_and_window_order_without_writes(self):
        store = Store(self.path)
        store.insert_message(Msg(group_id="g", msg_id="one", ts=1, text="文字"), bot_uin="100")
        store.upsert_group("g", "已知群", "200")
        store.add_summary("digest", 10, 20, 1, "较新日报", "daily")
        store.add_summary("digest", 1, 2, 1, "后来补生成的旧日报", "daily")
        store.close()
        reader = self.open_store(readonly=True)
        before = reader.conn.total_changes
        groups = reader.group_rows()
        self.assertEqual(groups[0][:2], ("g", "已知群"))
        self.assertEqual(set(groups[0][2].split(",")), {"100", "200"})
        self.assertEqual(groups[0][3], 1)
        reports = reader.report_history(("daily",))
        self.assertEqual([row["window_end"] for row in reports], [20, 2])
        self.assertIn("review", reports[0])
        self.assertEqual(reader.conn.total_changes, before)


if __name__ == "__main__":
    unittest.main()
