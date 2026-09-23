"""协议断连回归：临时日志、临时数据库和本机 WebSocket，不访问 QQ 或模型。"""
import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from msgx.config import DEFAULTS
from msgx.napcat import login_failure
from msgx.onebot import build_app
from msgx.pipeline import Pipeline


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["napcat"]["dir"] = str(self.root)
        self.cfg["app"].update(db_path=str(self.root / "test.db"),
                               report_dir=str(self.root / "reports"))
        (self.root / "logs").mkdir()
        self.log = self.root / "logs" / "2026-09-22.log"
        self.pipe = Pipeline(self.cfg)
        self.pipe._auto_refresh_names = AsyncMock()

    async def asyncTearDown(self):
        await self.pipe.stop()
        self.tmp.cleanup()

    def kick(self):
        self.log.write_text(time.strftime("%m-%d %H:%M:%S") +
                            " [error] bot | [KickedOffLine] [下线通知] 你的账号当前登录已失效，请重新登录。\n",
                            encoding="utf-8")

    async def test_kick_is_login_failure_even_after_service_restart(self):
        self.kick()
        status = self.pipe.protocol_status()
        self.assertEqual(status["state"], "login_required")
        self.assertIn("重新扫码", status["message"])
        self.assertEqual(status["log_path"], str(self.log))
        self.assertEqual(login_failure(self.cfg, time.time() + 1), {})

    async def test_group_text_cannot_forge_login_failure(self):
        self.log.write_text(time.strftime("%m-%d %H:%M:%S") +
                            " [info] bot | 接收 <- 群聊 [测试] [KickedOffLine] [下线通知] 失效\n",
                            encoding="utf-8")
        self.assertEqual(login_failure(self.cfg), {})
        self.assertEqual(self.pipe.protocol_status()["state"], "disconnected")

    async def test_new_messages_and_new_log_clear_old_failure(self):
        self.kick()
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(time.strftime("%m-%d %H:%M:%S") + " [info] bot | 接收 <- 群聊 [测试] 恢复\n")
        self.assertEqual(login_failure(self.cfg), {})
        self.kick()
        newer = self.log.with_name("new.log")
        newer.write_text("[AdapterManager] 协议适配器初始化完成\n", encoding="utf-8")
        os.utime(newer, (time.time() + 1, time.time() + 1))
        self.assertEqual(login_failure(self.cfg), {})

    async def test_missing_logs_do_not_invent_cause(self):
        self.assertEqual(login_failure(self.cfg), {})
        self.assertEqual(self.pipe.protocol_status()["state"], "disconnected")

    async def test_stalled_account_recovers_once_and_requires_fresh_message(self):
        pipe = self.pipe
        now = time.time()
        pipe.bot_uin = "123"
        pipe.started_at = pipe.ws_connected_at = now - 4000
        pipe.ws_conns.add(Mock(closed=False))
        pipe.last_protocol_message = {"self_id": "123", "received_at": now - 2000}
        with patch("msgx.pipeline.napcat.qq_msg_db_age", return_value=2000) as age, \
                patch.object(pipe.store, "hour_baseline", return_value=20), \
                patch("msgx.pipeline.napcat.recover_worker", return_value={"ok": True, "note": "等待新消息"}) as recover:
            await pipe._check_receive_health()
            age.assert_called_once_with("123")
            recover.assert_called_once_with(pipe.cfg, "123")
            self.assertEqual(pipe.protocol_status()["state"], "recovering")
            pipe._receive_warning["since"] -= 121
            await pipe._check_receive_health()
            self.assertEqual(pipe.protocol_status()["state"], "stalled")
            event = {"post_type": "message", "message_type": "group", "self_id": 456,
                     "group_id": 789, "user_id": 111, "message_id": 1, "time": int(now),
                     "message": [{"type": "text", "data": {"text": "测试"}}]}
            pipe.note_protocol_message(event)
            self.assertTrue(pipe._receive_warning)  # 另一账号不能确认恢复
            pipe.note_protocol_message(dict(event, self_id=123, time=int(now - 4000)))
            self.assertTrue(pipe._receive_warning)  # 旧历史消息不能确认恢复
            pipe.note_protocol_message(dict(event, self_id=123))
            self.assertFalse(pipe._receive_warning)
            self.assertEqual(pipe.protocol_status()["state"], "connected")
            # 恢复后再次停收仍遵守一小时冷却，不反复重启。
            pipe.last_protocol_message["received_at"] = now - 2000
            await pipe._check_receive_health()
            self.assertEqual(recover.call_count, 1)

    async def test_no_recovery_without_account_evidence_stale_db_and_busy_baseline(self):
        pipe = self.pipe
        now = time.time()
        pipe.bot_uin = "123"
        pipe.started_at = pipe.ws_connected_at = now - 4000
        pipe.ws_conns.add(Mock(closed=False))
        with patch("msgx.pipeline.napcat.qq_msg_db_age", return_value=2000) as age, \
                patch.object(pipe.store, "hour_baseline", return_value=20) as baseline, \
                patch("msgx.pipeline.napcat.recover_worker") as recover:
            await pipe._check_receive_health()  # 尚未验证过消息
            pipe.last_protocol_message = {"self_id": "123", "received_at": now}
            await pipe._check_receive_health()  # 活跃消息
            pipe.last_protocol_message["received_at"] = now - 2000
            for db_age in (None, 10):
                age.return_value = db_age
                await pipe._check_receive_health()
            age.return_value = 2000
            baseline.return_value = 0
            await pipe._check_receive_health()  # 安静时段
            recover.assert_not_called()
            self.assertFalse(pipe._receive_warning)

    async def test_year_boundary(self):
        self.log.write_text("12-31 23:59:59 [error] bot | [KickedOffLine] [下线通知] 失效\n", encoding="utf-8")
        stamp = time.mktime(time.strptime("2027-01-01 00:00:02", "%Y-%m-%d %H:%M:%S"))
        os.utime(self.log, (stamp, stamp))
        self.assertEqual(login_failure(self.cfg)["since"], stamp - 3)

    async def test_reconnections_duplicate_peer_and_offline_heartbeat(self):
        runner = web.AppRunner(build_app(self.cfg, self.pipe))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        async with aiohttp.ClientSession() as session:
            async def health():
                async with session.get(url + "/health") as response:
                    return await response.json()

            async def wait_status(state, conns):
                for _ in range(100):
                    data = await health()
                    if data["protocol"]["state"] == state and data["ws_conns"] == conns:
                        return data
                    await asyncio.sleep(.01)
                self.fail(f"未达到状态 {state}/{conns}: {data}")

            try:
                for _ in range(8):
                    first = await session.ws_connect(url + "/onebot/ws")
                    second = await session.ws_connect(url + "/onebot/ws")
                    await wait_status("connected", 2)
                    await first.close()
                    data = await wait_status("connected", 1)
                    self.assertEqual(data["ws_peers"], ["127.0.0.1"])
                    self.assertTrue(self.pipe._peer_watch is None or self.pipe._peer_watch.done())
                    await second.send_json({"post_type": "meta_event", "meta_event_type": "heartbeat",
                                            "status": {"online": False}})
                    await wait_status("offline", 1)
                    await second.send_json({"post_type": "message", "message_type": "group"})
                    await wait_status("connected", 1)
                    event = await asyncio.wait_for(self.pipe.ingest_q.get(), 1)
                    self.assertEqual(event["post_type"], "message")
                    await second.close()
                    data = await wait_status("disconnected", 0)
                    self.assertEqual(data["ws_peers"], [])
                    self.assertIn("关闭码", data["protocol"]["last_close"])
                self.assertFalse(self.pipe.ws_online)
                self.assertFalse(self.pipe.ws_addresses)
            finally:
                await self.pipe.close_ws_conns()
                await runner.cleanup()

    async def test_login_evidence_requires_new_protocol_group_message(self):
        runner = web.AppRunner(build_app(self.cfg, self.pipe))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        event = {"post_type": "message", "message_type": "group", "self_id": 123,
                 "group_id": 456, "user_id": 789, "message_id": 1, "time": int(time.time()),
                 "message": [{"type": "text", "data": {"text": "注入消息不能证明QQ在线"}}]}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url + "/inject", json=event) as response:
                    self.assertEqual(response.status, 200)
                self.assertFalse(self.pipe.last_protocol_message)
                ws = await session.ws_connect(url + "/onebot/ws")
                for ignored in ({"post_type": "meta_event", "meta_event_type": "heartbeat", "status": {"online": True}},
                                {"status": "ok", "echo": "old-query"}, event):
                    await ws.send_json(ignored)
                await asyncio.wait_for(self.pipe.ingest_q.get(), 1)
                self.assertFalse(self.pipe.last_protocol_message)  # 注入后已入库的旧消息不能再次验证
                self.pipe.cfg["filters"] = {"skip_image_only": True}
                image = dict(event, message_id=2, message=[{"type": "image", "data": {"url": "https://example.invalid/p.png"}}])
                await ws.send_json(image)
                queued = await asyncio.wait_for(self.pipe.ingest_q.get(), 1)
                self.assertTrue((await self.pipe.handle_event(queued))["filtered"])
                evidence = self.pipe.health()["last_protocol_message"]
                self.assertEqual(evidence["self_id"], "123")
                self.assertEqual(evidence["message_time"], event["time"])
                self.assertGreater(evidence["received_at"], self.pipe.ws_connected_at)
                async with session.post(url + "/onebot", json=dict(event, message_id=3)) as response:
                    self.assertEqual(response.status, 200)
                self.assertGreater(self.pipe.last_protocol_message["received_at"], evidence["received_at"])
                self.pipe.last_protocol_message = {}
                for malformed in (dict(event, time="bad"), dict(event, sender="bad", message_id=4)):
                    self.pipe.note_protocol_message(malformed)
                self.assertFalse(self.pipe.last_protocol_message)
                self.pipe.submit = AsyncMock(return_value=False)
                async with session.post(url + "/onebot", json=dict(event, message_id=5)) as response:
                    self.assertEqual(response.status, 200)
                self.assertFalse(self.pipe.last_protocol_message)  # 队列拒收不能冒充恢复
                await ws.close()
            finally:
                await self.pipe.close_ws_conns()
                await runner.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
