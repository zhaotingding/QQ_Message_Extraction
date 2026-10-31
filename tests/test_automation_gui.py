"""自动化设置与本机HTTP入口的闭环；仅临时Tk、随机端口和模拟模型。"""
from __future__ import annotations

import copy
import json
import os
import queue
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aiohttp import ClientSession, web
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx import api_settings
from msgx.config import DEFAULTS
from msgx.onebot import build_app
from msgx.pipeline import Pipeline
from msgx.ui.automation import AutomationControls


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        cfg = copy.deepcopy(DEFAULTS)
        self.page = SimpleNamespace(root=self.root, cfg=cfg, prefs={}, proc=object(), events=queue.Queue(),
            status=tk.StringVar(), _api_profiles={"openai": dict(api_settings.DEFAULTS),
            "cheap": {**api_settings.DEFAULTS, "model": "cheap-model"}}, save_prefs=Mock(return_value=True))
        self.controls = AutomationControls(self.page, self.root, tk.Frame(self.root))
        self.controls.load()

    def tearDown(self):
        self.root.destroy()

    def test_defaults_and_independent_profile_collection(self):
        data = self.controls.collect()
        self.assertFalse(data["image_analysis"]["enabled"])
        self.assertFalse(data["prompt_evolution"]["enabled"])
        self.page.prefs["image_analysis"] = {"context_messages": 2}
        self.assertEqual(self.controls.collect()["image_analysis"]["context_messages"], 2)
        self.controls.profile.current(self.controls.profile_ids.index("cheap"))
        self.controls.image_enabled.set(True)
        selected = self.controls.collect()
        self.assertEqual(selected["image_api_profile"], "cheap")
        self.assertTrue(selected["image_analysis"]["enabled"])
        self.controls.image_values["daily_limit"].set("0")
        with self.assertRaises(ValueError):
            self.controls.collect()

    def test_update_confirm_and_async_local_payload(self):
        self.page.cfg["onebot"]["access_token"] = "local-test-token"
        result = {"state": "idle", "queued": True, "history": [], "active": None}
        with patch("msgx.ui.automation.messagebox.askyesno", return_value=True), \
                patch("msgx.ui.automation.request_json", return_value=result) as request:
            self.controls.update()
            kind, (proc, response, error) = self.page.events.get(timeout=3)
        self.assertEqual(kind, "automation_done")
        self.assertIs(proc, self.page.proc)
        self.assertEqual(request.call_args_list[0].kwargs["payload"], {"confirmed": True})
        self.assertEqual(request.call_args_list[0].kwargs["token"], "local-test-token")
        self.controls.receive(response, error)
        self.assertFalse(self.controls.busy)
        self.assertIn("queued", self.controls.prompt_status.get())

    def test_cancellation_and_missing_service_do_not_submit(self):
        with patch("msgx.ui.automation.messagebox.askyesno", return_value=False), \
                patch("msgx.ui.automation.request_json") as request:
            self.controls.update()
        request.assert_not_called()
        self.page.proc = None
        self.controls.refresh()
        self.assertFalse(self.controls.busy)
        self.assertIn("启动", self.page.status.get())

    def test_health_poll_does_not_clear_history_or_selection(self):
        record = dict(id=1, created_at=1793000000, source_start="2026-10-20", source_end="2026-10-27",
                      data={"focus": [], "ads": []})
        self.controls.render_prompt({"state": "done", "active": record, "history": [record]})
        self.controls.health({"prompt_evolution": {"state": "done", "active": record}})
        self.assertEqual(self.controls.version_ids, [1])
        self.assertIn('"focus"', self.controls.preview.get("1.0", "end"))


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="msgx-automation-http-")
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"MSGX_PREFS": str(self.root / "absent.yaml")})
        self.env.start()
        cfg = copy.deepcopy(DEFAULTS)
        cfg["app"].update(db_path=str(self.root / "test.db"), media_dir=str(self.root / "media"), report_dir=str(self.root / "reports"))
        cfg["llm"]["enabled"] = False
        cfg["onebot"]["access_token"] = "test-local-token"
        with patch("msgx.pipeline.api_key", return_value=""):
            self.pipe = Pipeline(cfg)
        self.runner = web.AppRunner(build_app(cfg, self.pipe))
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.session = ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()
        await self.pipe.stop()
        self.env.stop()
        self.temp.cleanup()

    async def test_status_and_authorized_queue_rejects_duplicates(self):
        async with self.session.get(self.url + "/prompt-evolution") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["history"], [])
        async with self.session.post(self.url + "/prompt-evolution/update", json={"confirmed": True}) as response:
            self.assertEqual(response.status, 403)
        headers = {"Authorization": "Bearer test-local-token"}
        async with self.session.post(self.url + "/prompt-evolution/update", json={"confirmed": True}, headers=headers) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["state"], "queued")
        async with self.session.post(self.url + "/prompt-evolution/update", json={"confirmed": True}, headers=headers) as response:
            self.assertEqual(response.status, 409)
        self.assertEqual(self.pipe.store.conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0], 0)

    async def test_health_is_bounded_and_restore_never_invokes_model(self):
        async with self.session.get(self.url + "/health") as response:
            data = await response.json()
        self.assertNotIn("history", data["prompt_evolution"])
        self.assertIn("image_analysis", data)
        headers = {"Authorization": "Bearer test-local-token"}
        async with self.session.post(self.url + "/prompt-evolution/restore", json={"version_id": 0}, headers=headers) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.pipe.store.conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
