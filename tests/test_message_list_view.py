"""首屏只读查询与群/发送者组合筛选，使用隔离历史库。"""
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "desktop")]
import messages_api
from msgx.store import Store


class MessageListView(unittest.TestCase):
    def test_first_page_and_combined_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "fixture.db")
            try:
                args = [(str(i % 2), str(i), str(i % 3), "同名", i) for i in range(360)]
                store.conn.executemany("INSERT INTO messages(group_id,msg_id,sender_id,sender_name,ts) VALUES(?,?,?,?,?)", args)
                store.conn.commit()
                cfg = {"napcat": {"account": "99"}, "watch": {"groups": []}}
                ctx = SimpleNamespace(cfg=cfg, service_cfg=cfg, service_running=True,
                                      prefs_file=Path(tmp) / "prefs.yaml", read_store=lambda cfg: contextlib.nullcontext(store))
                with patch.object(store, "group_activity", side_effect=AssertionError("首屏不能读取月榜")):
                    data = messages_api.handle(ctx, "messages.recent", {"view": "list", "limit": 5, "group_id": "1", "sender_id": "2"})
                self.assertEqual([r["ts"] for r in data["items"]], [359, 353, 347, 341, 335])
                self.assertTrue(all(r["sender_id"] == "2" and r["group_id"] == "1" for r in data["items"]))
                self.assertEqual(data["related"], [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
