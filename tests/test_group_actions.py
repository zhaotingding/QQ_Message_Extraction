"""群发和退群安全边界：临时 SQLite、模拟 QQ、本机 HTTP；绝不真实发送。"""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.group_actions import GroupActions
from msgx.models import Msg
from msgx.onebot import _group_operation, _group_operation_status
from msgx.protocol_session import ActionRejected, ProtocolSession
from msgx.pipeline import Pipeline
from msgx.store import Store
from msgx import yaml_config


class Connection:
    closed = False


class GroupActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/"test.db")
        self.addCleanup(self.store.close)
        self.connection = Connection()
        self.pipe = SimpleNamespace(store=self.store, target_account="100", watch={"11", "22"},
            ws_conns={self.connection}, departed_groups={}, media=SimpleNamespace(
                blocked_groups=set(), _processing_images=set(), _vision_running=set()))
        self.members = {"11": "同名交流群", "22": "同名交流群", "99": "工作群禁止发送"}
        self.login, self.role, self.failure, self.after_send = "100", "member", None, None
        self.calls = []
        self.pipe.call_action = self.call
        self.actions = GroupActions(self.pipe, delay=0)
        self.pipe.group_actions = self.actions
        self.addAsyncCleanup(self.actions.close)

    async def call(self, action, params=None, timeout=8, *, connection=None):
        self.assertIs(connection, self.connection)
        if connection.closed or connection not in self.pipe.ws_conns:
            raise RuntimeError("连接已变化")
        self.calls.append((action, params))
        if action == "get_login_info":
            return {"user_id": self.login}
        if action == "get_group_list":
            return [{"group_id": gid, "group_name": name} for gid, name in self.members.items()]
        if action == "get_group_member_info":
            return {"role": self.role}
        if action in ("send_group_msg", "set_group_leave"):
            if self.failure:
                raise self.failure
            if self.after_send:
                self.after_send()
            return {"message_id": 123} if action == "send_group_msg" else None
        raise AssertionError(action)

    async def preview(self, kind="broadcast", **kwargs):
        return await self.actions.prepare({"kind": kind, "text": "新动态\n[CQ:at,qq=all]只应是文字", "group_id": "11",
            "account": "100", "watch_groups": sorted(self.pipe.watch), **kwargs})

    def external_calls(self):
        return [(name, params) for name, params in self.calls if name in ("send_group_msg", "set_group_leave")]

    async def execute(self, preview):
        job = await self.actions.confirm(preview["token"], True)
        await self.actions.task
        return self.store.group_job(job["id"])

    async def test_preview_no_send_and_confirm_only_exact_whitelist_as_plain_text(self):
        preview = await self.preview()
        self.assertEqual([row["group_id"] for row in preview["groups"]], ["11", "22"])
        self.assertEqual(self.external_calls(), [])
        job = await self.execute(preview)
        self.assertEqual(job["state"], "completed")
        self.assertEqual([params["group_id"] for _, params in self.external_calls()], [11, 22])
        for _, params in self.external_calls():
            self.assertEqual(params["message"], [{"type": "text", "data": {"text": preview["text"]}}])
        self.assertTrue(all(item["state"] == "sent" for item in job["groups"]))

    async def test_empty_watch_never_means_broadcast_all(self):
        self.pipe.watch.clear()
        with self.assertRaisesRegex(ValueError, "禁止群发"):
            await self.preview()
        self.assertEqual(self.external_calls(), [])

    async def test_wrong_account_or_multiple_connections_cannot_prepare(self):
        self.login = "200"
        with self.assertRaisesRegex(ValueError, "账号"):
            await self.preview()
        self.login = "100"
        self.pipe.ws_conns.add(Connection())
        with self.assertRaisesRegex(ValueError, "唯一"):
            await self.preview()
        self.assertEqual(self.external_calls(), [])

    async def test_require_confirmation_expiry_and_duplicate_token(self):
        preview = await self.preview()
        with self.assertRaisesRegex(ValueError, "二次确认"):
            await self.actions.confirm(preview["token"], False)
        self.actions.previews[preview["token"]]["expires_at"] = time.time()-1
        with self.assertRaisesRegex(ValueError, "过期"):
            await self.actions.confirm(preview["token"], True)
        preview = await self.preview()
        await self.actions.confirm(preview["token"], True)
        with self.assertRaisesRegex(ValueError, "已使用"):
            await self.actions.confirm(preview["token"], True)
        await self.actions.task
        self.assertEqual(len(self.external_calls()), 2)

    async def test_new_preview_invalidates_old_text(self):
        old = await self.preview(text="旧正文")
        new = await self.preview(text="新正文")
        with self.assertRaises(ValueError):
            await self.actions.confirm(old["token"], True)
        await self.execute(new)
        self.assertEqual(self.external_calls()[0][1]["message"][0]["data"]["text"], "新正文")

    async def test_saved_scope_change_bad_yaml_and_member_change_fail_closed(self):
        path = Path(self.temp.name)/"prefs.yaml"
        path.write_text(yaml_config.dumps({"qq_account": "100", "groups": ["11", "22"]}), encoding="utf-8")
        self.actions.prefs_path = path
        preview = await self.preview()
        path.write_text("groups: []\nqq_account: '100'\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "统计群"):
            await self.actions.confirm(preview["token"], True)
        path.write_text("groups: [", encoding="utf-8")
        with self.assertRaises(ValueError):
            await self.preview()
        self.actions.prefs_path = None
        preview = await self.preview()
        del self.members["22"]
        with self.assertRaisesRegex(ValueError, "名单"):
            await self.actions.confirm(preview["token"], True)
        self.assertEqual(self.external_calls(), [])

    async def test_scope_changes_during_send_stop_remaining_groups(self):
        self.after_send = lambda: self.pipe.watch.remove("22")
        job = await self.execute(await self.preview())
        self.assertEqual([item["state"] for item in job["groups"]], ["sent", "not_sent"])
        self.assertEqual(len(self.external_calls()), 1)

    async def test_missing_yaml_scope_fields_invalidate_confirmation(self):
        path = Path(self.temp.name)/"prefs.yaml"
        self.actions.prefs_path = path
        for field in ("groups", "qq_account"):
            settings = {"qq_account": "100", "groups": ["11", "22"]}
            path.write_text(yaml_config.dumps(settings), encoding="utf-8")
            preview = await self.preview()
            del settings[field]
            path.write_text(yaml_config.dumps(settings), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "缺少"):
                await self.actions.confirm(preview["token"], True)
        self.assertEqual(self.external_calls(), [])

    async def test_actual_protocol_async_ack_is_unknown_not_explicit_failure(self):
        session = ProtocolSession()
        session.cfg = {"napcat": {"account": "100"}}
        session._init_protocol_session()
        connection = Connection()
        async def send(raw):
            session.resolve_action({"echo": json.loads(raw)["echo"], "status": "async", "retcode": 1})
        connection.send_str = send
        session.ws_conns = {connection}
        with self.assertRaises(RuntimeError) as raised:
            await session.call_action("send_group_msg", {}, connection=connection)
        self.assertNotIsInstance(raised.exception, ActionRejected)
        self.assertIn("未知", str(raised.exception))
        replacement = Connection()
        with self.assertRaisesRegex(RuntimeError, "连接已断开"):
            await session.call_action("send_group_msg", {}, connection=replacement)

    async def test_timeout_is_unknown_never_retried_and_stops_later_groups(self):
        self.failure = asyncio.TimeoutError("发送超时")
        job = await self.execute(await self.preview())
        self.assertEqual([item["state"] for item in job["groups"]], ["unknown", "not_sent"])
        self.assertEqual(len(self.external_calls()), 1)
        self.store.recover_group_jobs()
        self.assertEqual(self.store.group_job(job["id"]), job)

    async def test_explicit_rejection_is_failure_and_no_retry(self):
        self.failure = ActionRejected("群禁言，协议明确拒绝")
        job = await self.execute(await self.preview())
        self.assertEqual([item["state"] for item in job["groups"]], ["failed", "not_sent"])
        self.assertEqual(len(self.external_calls()), 1)

    async def test_cancel_before_first_send_has_no_external_effect(self):
        preview = await self.preview()
        job = await self.actions.confirm(preview["token"], True)
        self.actions.cancel(job["id"])
        await self.actions.task
        self.assertEqual(self.external_calls(), [])
        self.assertTrue(all(item["state"] == "not_sent" for item in self.store.group_job(job["id"])["groups"]))

    async def test_cancel_while_verifying_members_does_not_submit_next_message(self):
        preview = await self.preview()
        job = await self.actions.confirm(preview["token"], True)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.pipe.call_action
        async def delayed(action, *args, **kwargs):
            if action == "get_group_list":
                entered.set()
                await release.wait()
            return await original(action, *args, **kwargs)
        self.pipe.call_action = delayed
        await asyncio.wait_for(entered.wait(), 1)
        self.actions.cancel(job["id"])
        release.set()
        await self.actions.task
        self.assertEqual(self.external_calls(), [])

    def seed_messages(self):
        for gid in ("11", "22"):
            self.store.insert_message(Msg(msg_id=gid, group_id=gid, sender_id="1", ts=int(time.time()), text="[图片]", image_urls=["fake-url"]), bot_uin="100")
        self.store.sync_account_groups("100", {"11", "22", "99"})
        self.store.conn.execute("INSERT INTO summaries(group_id,window_start,window_end,content,report_path) VALUES('11',1,2,'摘要保留','report.html')")
        self.store.conn.execute("INSERT INTO alerts(group_id,level) VALUES('11','notice')")
        self.store.conn.commit()

    async def test_leave_success_cleans_only_selected_group_and_keeps_reports(self):
        self.seed_messages()
        before = [tuple(row) for row in self.store.conn.execute("SELECT * FROM summaries")]
        preview = await self.preview("leave")
        self.assertEqual(preview["message_count"], 1)
        job = await self.execute(preview)
        self.assertEqual(self.external_calls(), [("set_group_leave", {"group_id": 11, "is_dismiss": False})])
        self.assertEqual(job["groups"][0]["state"], "cleaned")
        self.assertEqual(self.store.group_message_count("11"), 0)
        self.assertEqual(self.store.group_message_count("22"), 1)
        self.assertEqual([tuple(row) for row in self.store.conn.execute("SELECT * FROM summaries")], before)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM message_images").fetchone()[0], 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM message_receipts WHERE group_id='11'").fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM alerts WHERE group_id='11'").fetchone()[0], 0)
        self.assertNotIn("11", {row[0] for row in self.store.group_rows("100")})
        self.assertIn("11", self.store.departure_cutoffs("100"))

    async def test_failed_or_unknown_leave_never_deletes_messages(self):
        self.seed_messages()
        self.failure = asyncio.TimeoutError("结果未知")
        job = await self.execute(await self.preview("leave"))
        self.assertEqual(job["groups"][0]["state"], "unknown")
        self.assertEqual(self.store.group_message_count("11"), 1)
        self.assertEqual(self.store.departure_cutoffs("100"), {})

    async def test_messages_received_while_leave_waits_survive_explicit_failure(self):
        self.seed_messages()
        self.pipe.counters = dict(received=0, skipped=0, filtered=0, stored=0, duplicate=0)
        self.pipe.bot_uin = "100"
        self.pipe.cfg = {"filters": {"blocked_senders": []}}
        self.pipe.group_name = lambda gid: self.members[gid]
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.pipe.call_action
        async def delayed(action, *args, **kwargs):
            if action == "set_group_leave":
                entered.set()
                await release.wait()
                raise ActionRejected("模拟退群失败")
            return await original(action, *args, **kwargs)
        self.pipe.call_action = delayed
        preview = await self.preview("leave")
        job = await self.actions.confirm(preview["token"], True)
        await asyncio.wait_for(entered.wait(), 1)
        event = {"post_type": "message", "message_type": "group", "self_id": 100, "group_id": 11,
                 "user_id": 200, "message_id": 987, "time": int(time.time()), "message": "退群待回应期间收到的消息"}
        try:
            result = await Pipeline.handle_event(self.pipe, event)
            self.assertIsNotNone(result.get("row_id"))
        finally:
            release.set()
            await self.actions.task
        self.assertEqual(self.store.group_message_count("11"), 2)
        self.assertEqual(self.store.group_job(job["id"])["groups"][0]["state"], "failed")
        self.assertEqual(self.pipe.media.blocked_groups, set())

    async def test_owner_or_active_image_cannot_leave(self):
        self.seed_messages()
        self.role = "owner"
        with self.assertRaisesRegex(ValueError, "群主"):
            await self.preview("leave")
        self.role = "member"
        image_id = self.store.recent_messages(group_id="11")[0]["images"][0]["id"]
        self.pipe.media._processing_images.add(image_id)
        job = await self.execute(await self.preview("leave"))
        self.assertEqual(self.external_calls(), [])
        self.assertEqual(self.store.group_message_count("11"), 1)
        self.assertIn("图片", job["note"])
        self.assertEqual(self.pipe.media.blocked_groups, set())

    async def test_restart_records_unknown_without_replaying(self):
        job = {"id": "interrupted", "created_at": time.time(), "state": "running", "kind": "broadcast",
               "groups": [{"group_id": "11", "state": "sending"}, {"group_id": "22", "state": "pending"}]}
        self.store.save_group_job(job)
        self.store.recover_group_jobs()
        recovered = self.store.group_job("interrupted")
        self.assertEqual([row["state"] for row in recovered["groups"]], ["unknown", "not_sent"])
        self.assertEqual(self.external_calls(), [])

    async def test_local_http_requires_auth_and_second_confirmation(self):
        app = web.Application()
        app["cfg"], app["pipe"] = {"onebot": {"access_token": "test-token"}}, self.pipe
        app.router.add_post("/groups/{operation:preview|confirm|cancel}", _group_operation)
        app.router.add_get("/groups/status", _group_operation_status)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        body = {"kind": "broadcast", "text": "本机模拟", "account": "100", "watch_groups": ["11", "22"]}
        response = await client.post("/groups/preview", json=body)
        self.assertEqual(response.status, 403)
        headers = {"Authorization": "Bearer test-token"}
        response = await client.post("/groups/preview", json=body, headers={**headers, "Origin": "https://example.test"})
        self.assertEqual(response.status, 403)
        response = await client.post("/groups/preview", json=body, headers=headers)
        preview = (await response.json())["result"]
        self.assertEqual(self.external_calls(), [])
        response = await client.post("/groups/confirm", json={"token": preview["token"]}, headers=headers)
        self.assertEqual(response.status, 409)
        response = await client.post("/groups/confirm", json={"token": preview["token"], "confirmed": True}, headers=headers)
        self.assertEqual(response.status, 200)
        await self.actions.task
        response = await client.get("/groups/status", headers=headers)
        self.assertEqual((await response.json())["result"]["state"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
