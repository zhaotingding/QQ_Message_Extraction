"""后台读取不阻塞接收、连接归属和停止任务；隔离数据库，不连接 QQ/模型。"""
import asyncio
import copy
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.config import DEFAULTS
from msgx.onebot import _read_store
from msgx.pipeline import Pipeline
from msgx.store import Store
from msgx import yaml_config


class ReadOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.db"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    async def test_slow_read_leaves_event_loop_free_and_owns_its_connection(self):
        started, release = threading.Event(), threading.Event()
        main_thread = threading.get_ident()

        def query(reader):
            self.assertNotEqual(threading.get_ident(), main_thread)
            self.assertIsNot(reader.conn, self.store.conn)
            started.set()
            release.wait(2)
            return reader.stats()["messages"]

        task = asyncio.create_task(_read_store(self.store, query))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            self.assertEqual(await asyncio.wait_for(asyncio.sleep(0, result="响应"), .1), "响应")
            self.assertFalse(task.done())
        finally:
            release.set()
        self.assertEqual(await task, 0)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    async def test_reader_cannot_write_or_run_migrations(self):
        before = self.store.conn.total_changes
        def query(reader):
            with self.assertRaises(sqlite3.OperationalError):
                reader.conn.execute("INSERT INTO groups(id,name) VALUES('x','禁止写入')")
            return reader.conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(await _read_store(self.store, query), self.store.conn.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(self.store.conn.total_changes, before)

    async def test_protocol_group_refresh_is_cancelled_and_awaited_before_stop(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["app"].update(db_path=str(Path(self.temp.name) / "pipe.db"),
                          media_dir=str(Path(self.temp.name) / "media"),
                          report_dir=str(Path(self.temp.name) / "reports"))
        cfg["llm"]["enabled"] = False
        pipe = Pipeline(cfg)
        cancelled = asyncio.Event()
        async def refresh():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        pipe._auto_refresh_names = refresh
        pipe.on_peer_connected()
        tasks = list(pipe._group_refresh_tasks)
        await asyncio.sleep(0)
        await pipe.stop()
        self.assertTrue(cancelled.is_set())
        self.assertTrue(all(task.done() for task in tasks))
        self.assertFalse(pipe._group_refresh_tasks)

    def test_shared_config_has_no_machine_napcat_path_or_misplaced_keys(self):
        cfg = yaml_config.read(Path(__file__).resolve().parents[1] / "config.yaml")
        self.assertEqual(cfg["napcat"]["dir"], "")
        self.assertTrue({"dir", "hidden", "log_file", "qq_path"} <= cfg["napcat"].keys())
        self.assertFalse({"dir", "hidden", "log_file", "qq_path"} & cfg["watchdog"].keys())

    def test_balance_day_and_hour_baseline_do_not_use_host_timezone(self):
        ts = int(datetime(2026, 10, 13, 16, 5, tzinfo=timezone.utc).timestamp())
        self.store.conn.execute("INSERT INTO messages(group_id,msg_id,ts,text) VALUES('g','m',?,'普通消息')", (ts,))
        self.store.conn.commit()
        with patch("msgx.usage_store.time.localtime", side_effect=AssertionError("不依赖系统时区")), \
                patch("msgx.message_store.time.time", return_value=ts):
            self.store.record_balance(ts, 10, 0, 0)
            self.assertEqual(self.store.balance_readings()[0]["day"], "2026-10-14")
            self.assertEqual(self.store.hour_baseline(0, days=1), 1)


if __name__ == "__main__":
    unittest.main()
