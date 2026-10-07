"""与我相关：协议、历史迁移、跨群与多账号，仅合成数据。"""
import json
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.models import parse_onebot_event, message_links
from msgx.store import Store
from msgx.onebot import _messages_recent
from msgx.config import DEFAULTS
from msgx.pipeline import Pipeline

class RelatedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:", filters={"blocked_senders": []})
        self.addCleanup(self.store.close)
        self.seq = 0

    def add(self, sender="200", group="1", segments=None, receiver="100", ident=None):
        self.seq += 1
        event = {"post_type": "message_sent" if sender == receiver else "message", "message_type": "group",
                 "message_id": ident or str(self.seq), "group_id": group, "user_id": sender,
                 "self_id": receiver, "time": 1000+self.seq,
                 "message": segments if segments is not None else [{"type": "text", "data": {"text": "正文"}}]}
        msg = parse_onebot_event(event, receiver)
        return self.store.insert_message(msg, bot_uin=receiver)

    async def test_own_mentions_and_quotes_exclude_all_and_do_not_confuse_accounts(self):
        own = self.add(sender="100", ident="original")
        at = self.add(segments="[CQ:at,qq=100]回答")
        reply = self.add(segments="[CQ:reply,id=original]引用回答")
        cross_account = self.add(receiver="999", segments="[CQ:at,qq=100]单独回复")
        self.add(segments="[CQ:at,qq=all][CQ:at,qq=100]广播")
        self.add(segments="[CQ:at,qq=999]别人的消息")
        self.add(group="2", segments="[CQ:reply,id=original]不同群同ID不能关联")
        self.add(segments="[CQ:reply,id=missing]原文缺失")
        self.add(segments="提到@全体成员这个词不是真正的群提醒")
        rows = self.store.recent_messages(200, account="100", related_only=True)
        self.assertEqual({r["id"] for r in rows}, {own, at, reply, cross_account})
        self.assertEqual({r["id"]: r["relation"] for r in rows}[own], "self")
        self.assertEqual(self.store.recent_messages(account="", related_only=True), [])
        self.assertNotIn(own, {r["id"] for r in self.store.recent_messages(account="999", related_only=True)})
        for _ in range(20):
            self.add()
        self.assertEqual(len(self.store.recent_messages(2, account="100", related_only=True)), 2)
        self.assertTrue(all(not r["relation"] for r in self.store.recent_messages(2, account="100")))
        pipe = SimpleNamespace(store=self.store, target_account="100", bot_uin="999")
        result = json.loads((await _messages_recent(SimpleNamespace(query={"limit": "2", "images": "1", "group_id": "1"}, app={"pipe": pipe}))).text)
        self.assertEqual(result["items"], [])
        self.assertEqual(len(result["related"]), 2)
        self.assertEqual(result["related_account"], "100")

    def test_list_protocol_and_message_ids(self):
        targets, reply = message_links({"message": [{"type":"at","data":{"qq":100}},
            {"type":"at","data":{"qq":"all"}}, {"type":"reply","data":{"id":12}}]})
        self.assertEqual((targets, reply), (["100"], "12"))

    async def test_own_sent_event_and_quote_enter_pipeline_without_llm(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = copy.deepcopy(DEFAULTS)
            cfg["app"].update(db_path=str(Path(directory)/"test.db"),
                              media_dir=str(Path(directory)/"media"), report_dir=str(Path(directory)/"reports"))
            pipe = Pipeline(cfg)
            pipe.llm._post = AsyncMock(side_effect=AssertionError("不得调用模型"))
            try:
                own = {"post_type":"message_sent", "message_type":"group", "self_id":100,
                       "user_id":100, "group_id":1, "message_id":1, "time":1000, "message":"我的消息"}
                await pipe.submit(own)
                result = await pipe.handle_event(pipe.ingest_q.get_nowait())
                self.assertTrue(result.get("ok"))
                await pipe.handle_event({**own, "post_type":"message", "user_id":200,
                                         "message_id":2, "time":1001, "message":"[CQ:reply,id=1]回复"})
                self.assertEqual([r["relation"] for r in pipe.store.recent_messages(account="100", related_only=True)], ["reply", "self"])
                pipe.llm._post.assert_not_awaited()
            finally:
                await pipe.stop()

    def test_legacy_backfill_preserves_rows_and_only_known_relations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"old.db"
            old = Store(path)
            event = {"post_type":"message", "message_type":"group", "group_id":"1", "message_id":"old",
                     "user_id":"200", "time":1, "message":"[CQ:at,qq=100]历史"}
            old.insert_message(parse_onebot_event(event, "999"), bot_uin="999")
            before = old.conn.execute("SELECT id,text FROM messages").fetchall()
            old.conn.execute("ALTER TABLE messages DROP COLUMN mentioned_ids")
            old.conn.commit()
            old.close()
            migrated = Store(path)
            self.assertEqual([tuple(r) for r in before], [tuple(r) for r in migrated.conn.execute("SELECT id,text FROM messages")])
            self.assertEqual(len(migrated.recent_messages(account="100", related_only=True)), 1)
            migrated.close()
            again = Store(path)
            self.assertEqual(len(again.recent_messages(account="100", related_only=True)), 1)
            again.close()

if __name__ == "__main__":
    unittest.main()
