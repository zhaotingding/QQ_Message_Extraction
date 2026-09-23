"""换号数据与登录边界：虚构消息、离线管理接口，不操作真实 QQ。"""
import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx import napcat
from msgx.config import DEFAULTS
from msgx.models import Msg
from msgx.pipeline import Pipeline
from msgx.store import SCHEMA, Store
from msgx.prefs import apply_to_config


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "messages.db"

    def store(self):
        st = Store(self.db)
        self.addCleanup(st.close)
        return st

    def test_legacy_migration_preserves_ids_and_results(self):
        with sqlite3.connect(self.db) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO groups VALUES('g','旧群',1,'999')")
            for row_id, raw in ((12, json.dumps({"self_id": 111})), (15, ""), (16, '{"self_id":')):
                db.execute("INSERT INTO messages(id,group_id,msg_id,ts,raw,ocr_text,vision_text)"
                           " VALUES(?,'g',?,1,?,'原OCR','原图片解析')", (row_id, str(row_id), raw))
            db.execute("INSERT INTO summaries(group_id,window_start,window_end,content) VALUES('g',0,2,'旧摘要')")
            db.execute("INSERT INTO api_calls(ts,purpose,source_ids,source_type,model,status)"
                       " VALUES(1,'vision','[12]','image','model','ok')")
        db.close()
        st = self.store()
        rows = st.conn.execute("SELECT id,received_by,ocr_text,vision_text FROM messages ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(12, '111', '原OCR', '原图片解析'),
                                                  (15, '', '原OCR', '原图片解析'), (16, '', '原OCR', '原图片解析')])
        self.assertEqual(st.conn.execute("SELECT source_ids FROM api_calls").fetchone()[0], '[12]')
        self.assertEqual(st.conn.execute("SELECT content FROM summaries").fetchone()[0], '旧摘要')
        self.assertTrue(list(Path(self.tmp.name).glob('*.bak')))
        self.assertEqual(st.conn.execute("PRAGMA integrity_check").fetchone()[0], 'ok')
        again = self.store()
        self.assertEqual(again.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 3)
        self.assertEqual(again.conn.execute("SELECT COUNT(*) FROM message_receipts").fetchone()[0], 3)

    def test_colliding_ids_do_not_lose_other_accounts_message(self):
        st = self.store()
        first = st.insert_message(Msg(msg_id='42', group_id='g', sender_id='10', ts=1, text='小号内容'), bot_uin='111')
        second = st.insert_message(Msg(msg_id='42', group_id='g', sender_id='20', ts=2, text='大号内容'), bot_uin='222')
        self.assertNotEqual(first, second)
        self.assertEqual(len(st.messages_between('g', 0, 3)), 2)
        self.assertEqual(st.get_message(first)['text'], '小号内容')

    def test_failed_table_migration_rolls_back_original_messages(self):
        db = sqlite3.connect(self.db)
        db.executescript(SCHEMA)
        db.execute("INSERT INTO messages(group_id,msg_id,ts,text) VALUES('g','42',1,'保留原文')")
        db.commit()
        db.close()
        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                result = super().execute(sql, parameters)
                if sql == 'DROP TABLE messages':
                    raise RuntimeError('模拟迁移中断')
                return result
        original_connect = sqlite3.connect
        opened = []
        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs, factory=FailingConnection)
            opened.append(connection)
            return connection
        try:
            with patch('msgx.store.sqlite3.connect', side_effect=connect):
                with self.assertRaisesRegex(RuntimeError, '模拟迁移中断'):
                    Store(self.db)
        finally:
            for connection in opened:
                connection.close()
        db = original_connect(self.db)
        try:
            self.assertEqual(db.execute('SELECT text FROM messages').fetchone()[0], '保留原文')
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='messages_accounts'").fetchone())
            self.assertIn('UNIQUE(group_id, msg_id)', db.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0])
        finally:
            db.close()
        self.assertEqual(self.store().recent_messages()[0]['text'], '保留原文')

    def test_shared_message_is_counted_once_and_keeps_image_result(self):
        st = self.store()
        msg = Msg(msg_id='42', group_id='g', sender_id='10', ts=1, text='正文')
        first = st.insert_message(msg, bot_uin='111')
        st.set_media_result(first, ocr_text='OCR', vision_text='图像解读', vision_state='ok')
        self.assertIsNone(st.insert_message(msg, bot_uin='222'))
        self.assertIsNone(st.insert_message(msg, bot_uin='222'))
        self.assertTrue(st.has_receipt('111', 'g', '42'))
        self.assertTrue(st.has_receipt('222', 'g', '42'))
        self.assertEqual(len(st.messages_between('g', 0, 2)), 1)
        self.assertEqual(st.get_message(first)['vision_text'], '图像解读')
        self.assertEqual(set(st.recent_messages()[0]['receiver_accounts'].split(',')), {'111', '222'})
        self.assertEqual({r[0] for r in st.conn.execute("SELECT account FROM group_accounts")}, {'111','222'})
        self.assertEqual(st.hour_baseline(0, account='333'), 0)

    def test_different_images_are_not_collapsed(self):
        st = self.store()
        a = st.insert_message(Msg(msg_id='42', group_id='g', ts=1, text='图片', image_urls=['a']), bot_uin='111')
        b = st.insert_message(Msg(msg_id='42', group_id='g', ts=1, text='图片', image_urls=['b']), bot_uin='222')
        self.assertNotEqual(a, b)

    def test_target_account_rejects_other_account_without_overwriting_state(self):
        cfg = apply_to_config(DEFAULTS, {'qq_account': '222'})
        cfg['app']['db_path'] = str(self.db)
        cfg['llm']['enabled'] = False
        with patch('msgx.pipeline.api_key', return_value=''):
            pipe = Pipeline(cfg)
        self.addCleanup(pipe.store.close)
        event = {'post_type':'message', 'message_type':'group', 'self_id':111,
                 'group_id':'g', 'message_id':42, 'time':1, 'user_id':10,
                 'message':[{'type':'text','data':{'text':'消息正文'}}]}
        pipe.note_protocol_message(event)
        self.assertFalse(asyncio.run(pipe.handle_event(event))['ok'])
        self.assertEqual(pipe.bot_uin, '')
        self.assertEqual(pipe.last_protocol_message, {})
        event['self_id'] = 222
        pipe.note_protocol_message(event)
        self.assertTrue(asyncio.run(pipe.handle_event(event))['ok'])
        self.assertEqual(pipe.last_protocol_message['self_id'], '222')
        self.assertEqual(pipe.store.recent_messages()[0]['received_by'], '222')

    def test_online_other_account_cannot_enable_quick_login_or_restart(self):
        client = Mock()
        client.call.side_effect = lambda path, data=None: {
            '/QQLogin/CheckLoginStatus': {'isLogin':True,'coreReady':True},
            '/QQLogin/GetQQLoginInfo': {'uin':'111'},
            '/QQLogin/GetQuickLoginQQ': '111',
        }[path]
        cfg = {'napcat':{'dir':'mock','account':'222'}}
        with patch.object(napcat, '_WebUI', return_value=client):
            self.assertFalse(napcat.quick_login(cfg, wait_seconds=0)['ok'])
            self.assertFalse(napcat.refresh_login_qrcode(cfg)['ok'])
            self.assertFalse(napcat.restart_target(cfg)['ok'])
            self.assertFalse(napcat.recover_worker(cfg, '111')['ok'])
        self.assertFalse(any('Set' in c.args[0] or 'Restart' in c.args[0] for c in client.call.call_args_list))

    def test_offline_quick_login_selects_target_not_previous_account(self):
        client = Mock()
        def call(path, data=None):
            return {'/QQLogin/CheckLoginStatus': {'loginPhase':'waiting_qrcode'},
                    '/QQLogin/GetQuickLoginList': ['111','222'],
                    '/QQLogin/GetQuickLoginQQ': '111'}.get(path)
        client.call.side_effect = call
        with patch.object(napcat, '_WebUI', return_value=client):
            napcat.quick_login({'napcat':{'dir':'mock','account':'222'}}, wait_seconds=0)
        client.call.assert_any_call('/QQLogin/SetQuickLogin', {'uin':'222'})
        self.assertFalse(any(c.args[-1] == {'uin':'111'} for c in client.call.call_args_list))

    def test_manual_restart_only_uses_target_worker(self):
        client = Mock()
        client.call.side_effect = lambda path: {
            '/QQLogin/GetQQLoginInfo': {'uin':'222'},
            '/QQLogin/GetQuickLoginQQ': '222',
            '/QQLogin/RestartNapCat': None,
        }[path]
        with patch.object(napcat, '_WebUI', return_value=client), patch.object(napcat, 'restart') as force:
            result = napcat.restart_target({'napcat':{'dir':'mock','account':'222'}})
        self.assertTrue(result['ok'])
        client.call.assert_any_call('/QQLogin/RestartNapCat')
        force.assert_not_called()


if __name__ == '__main__':
    unittest.main()
