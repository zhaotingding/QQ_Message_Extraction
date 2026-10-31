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
from msgx import api_settings, prefs
from msgx.config import DEFAULTS
from msgx.onebot import build_app
from msgx.pipeline import Pipeline
from msgx.ui.automation import AutomationControls


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="msgx-image-controls-")
        self.addCleanup(self.temp.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        cfg = copy.deepcopy(DEFAULTS)
        self.page = SimpleNamespace(root=self.root, cfg=cfg, prefs={}, proc=object(), events=queue.Queue(),
            status=tk.StringVar(), _api_profiles={"openai": dict(api_settings.DEFAULTS),
            "cheap": {**api_settings.DEFAULTS, "model": "cheap-model"}}, save_prefs=Mock(return_value=True),
            prefs_file=Path(self.temp.name) / "preferences.yaml")
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

    def test_image_module_saves_own_settings_and_reopens_with_personal_values(self):
        controls = self.controls
        controls.prompt_days.set("无效的其他模块草稿")
        controls.profile.current(controls.profile_ids.index("cheap"))
        controls.image_enabled.set(True)
        for name, value in {"threshold": "50", "daily_limit": "200", "daily_budget_cny": "10", "history_days": "7"}.items():
            controls.image_values[name].set(value)
        self.assertTrue(controls.image_dirty)
        self.assertIn("未保存", controls.image_save_status.get())
        self.assertTrue(controls.save_image_settings())
        self.page.save_prefs.assert_not_called()
        saved = prefs.load_raw(self.page.prefs_file)
        self.assertEqual(saved["image_api_profile"], "cheap")
        self.assertEqual(saved["image_analysis"]["threshold"], 50)
        self.assertEqual(saved["image_analysis"]["daily_limit"], 200)
        self.assertEqual(saved["image_analysis"]["daily_budget_cny"], 10)
        self.assertIs(saved["image_analysis"]["enabled"], True)
        self.assertFalse(controls.image_dirty)
        self.assertIn(str(self.page.prefs_file.resolve()), controls.image_save_status.get())
        self.page.cfg["media"]["analysis"].update(threshold=90, daily_limit=1, daily_budget_cny=1)
        self.page.prefs = prefs.load(self.page.prefs_file)
        reopened = AutomationControls(self.page, self.root, tk.Frame(self.root))
        reopened.load()
        self.assertEqual(reopened.collect_image()["image_analysis"], saved["image_analysis"])

    def test_image_save_preserves_latest_other_module_and_hidden_values(self):
        prefs.save({"ui": {"theme": "dark", "last_tab": 2}, "personal_rule": "保留", "image_analysis": {
            "enabled": False, "threshold": 50, "daily_limit": 200, "daily_budget_cny": 10,
            "history_days": 7, "context_messages": 2, "keyword_rules": []}}, self.page.prefs_file)
        self.page.prefs = prefs.load(self.page.prefs_file)
        self.controls.load()
        self.controls.image_enabled.set(False)
        self.assertTrue(self.controls.save_image_settings())
        saved = prefs.load_raw(self.page.prefs_file)
        self.assertEqual(saved["ui"], {"theme": "dark", "last_tab": 2})
        self.assertEqual(saved["personal_rule"], "保留")
        self.assertEqual(saved["image_analysis"]["context_messages"], 2)
        self.assertEqual(saved["image_analysis"]["keyword_rules"], [])
        self.assertIs(saved["image_analysis"]["enabled"], False)

    def test_saving_rules_does_not_submit_invalid_numeric_draft(self):
        self.assertTrue(self.controls.save_image_settings())
        original = prefs.load_raw(self.page.prefs_file)["image_analysis"]["daily_limit"]
        self.controls.image_values["daily_limit"].set("无效草稿")
        self.assertTrue(self.controls.save_image_rules([]))
        saved = prefs.load_raw(self.page.prefs_file)
        self.assertEqual(saved["image_analysis"]["keyword_rules"], [])
        self.assertEqual(saved["image_analysis"]["daily_limit"], original)
        self.assertTrue(self.controls.image_dirty)
        self.assertIn("仍未保存", self.controls.image_save_status.get())

    def test_failed_image_save_keeps_values_and_reports_failure(self):
        self.controls.image_values["threshold"].set("50")
        with patch("msgx.ui.automation.prefs_mod.update", side_effect=OSError("模拟无写权限")):
            self.assertFalse(self.controls.save_image_settings())
        self.assertEqual(self.controls.image_values["threshold"].get(), "50")
        self.assertTrue(self.controls.image_dirty)
        self.assertIn("保存失败", self.controls.image_save_status.get())
        self.assertFalse(self.page.prefs_file.exists())

    def test_partial_image_preferences_keep_main_hidden_fields_and_latest_yaml(self):
        self.page.cfg["media"]["analysis"].update(context_messages=16, threshold=61, keyword_rules=[])
        self.page.prefs = {"image_analysis": {"daily_budget_cny": 10}}
        self.controls.load()
        options = self.controls.collect_image()["image_analysis"]
        self.assertEqual(options["context_messages"], 16)
        self.assertEqual(options["threshold"], 61)
        self.assertEqual(options["daily_budget_cny"], 10)
        self.assertEqual(options["keyword_rules"], [])
        prefs.save({"image_analysis": {"context_messages": 19, "keyword_rules": []}}, self.page.prefs_file)
        self.assertEqual(self.controls.collect_image()["image_analysis"]["context_messages"], 19)
        self.assertTrue(self.controls.save_image_settings())
        self.assertEqual(prefs.load_raw(self.page.prefs_file)["image_analysis"]["context_messages"], 19)


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
