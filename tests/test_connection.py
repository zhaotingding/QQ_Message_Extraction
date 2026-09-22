"""协议断连回归：临时日志、临时数据库和本机 WebSocket，不访问 QQ 或模型。"""
import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
