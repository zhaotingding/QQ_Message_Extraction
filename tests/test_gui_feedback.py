"""v0.8.8 操作反馈回归；隔离配置、合成凭据、本机无服务。"""
import copy
import json
import queue
import sys
import tempfile
import tkinter as tk
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx import gui, prefs, api_settings
from msgx.config import DEFAULTS
from msgx.store import Store


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = copy.deepcopy(DEFAULTS)
        cfg["app"]["db_path"] = str(Path(self.tmp.name) / "test.db")
        Store(cfg["app"]["db_path"]).close()
        self.root = tk.Tk()
        self.addCleanup(self.close_root)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for method in ("read", "save", "delete"):
            self.patches.enter_context(patch.object(gui.credentials, method, return_value=""))
        with ExitStack() as setup:
            setup.enter_context(patch.object(gui, "load", return_value=cfg))
            setup.enter_context(patch.object(prefs, "load", return_value=copy.deepcopy(prefs.DEFAULTS)))
            setup.enter_context(patch.object(prefs, "load_raw", return_value={}))
            for method in ("refresh_groups", "refresh_report_history", "_tick_health", "_tick_realtime", "_trace_gui"):
                setup.enter_context(patch.object(gui.App, method))
            self.app = gui.App(self.root)
        self.app.prefs_file = Path(self.tmp.name) / "prefs.json"
        self.key = "sk-feedback-SYNTHETIC-NOT-REAL-123456789"

    def close_root(self):
        for callback in self.root.tk.call("after", "info"):
            self.root.tk.call("after", "cancel", callback)
        self.root.destroy()

    def test_custom_icon_persists_as_local_copy(self):
        from PIL import Image
        source = Path(self.tmp.name) / "source.png"
        Image.new("RGBA", (64, 48), "red").save(source)
        with patch.object(gui.filedialog, "askopenfilename", return_value=str(source)):
            self.app.choose_app_icon()
        saved = prefs.load(self.app.prefs_file)
        icon = Path(saved["ui"]["icon_path"])
        self.assertTrue(icon.is_file())
        source.unlink()
        self.app.prefs = saved
        self.app._apply_app_icon()
        self.assertIn("已应用", self.app.v_icon_status.get())
        with patch.object(gui.filedialog, "askopenfilename", return_value=""):
            self.app.choose_app_icon()
        self.assertIn("已取消", self.app.status.get())
        self.assertTrue(icon.is_file())

    def test_related_tab_red_rows_detail_and_flex_label(self):
        app = self.app
        self.assertEqual([app.rt_bottom.tab(i, "text") for i in range(3)], ["群活跃度", "消息详情", "与我相关"])
        rows = [{"id": 1, "ts": 1, "group_id": "1", "text": "我的正文", "relation": "self"},
                {"id": 2, "ts": 2, "group_id": "1", "text": "回复正文", "relation": "reply"},
                {"id": 3, "ts": 3, "group_id": "1", "text": "普通正文"}]
        app._render_messages(rows)
        self.assertIn("related", app.rt.item("1", "tags"))
        self.assertIn("related", app.rt.item("2", "tags"))
        self.assertNotIn("related", app.rt.item("3", "tags"))
        app._render_related(rows[:2], "100")
        app.rt_related.selection_set("2")
        app._related_selected()
        self.assertIn("回复正文", app.rt_detail.get("1.0", "end"))
        app._render_messages(rows)
        self.assertIn("回复正文", app.rt_detail.get("1.0", "end"))
        self.assertEqual(app._usage_tier_label({"service_tier": "flex", "pricing": json.dumps({"base_url":"https://api.openai.com/v1"})}), "flex ×0.5")

    def test_key_save_pending_success_and_no_replacement(self):
        app = self.app
        app.v_api_key.set(self.key)
        self.assertIn("尚未保存", app.v_api_key_status.get())
        with patch.object(prefs, "save") as save:
            self.assertTrue(app.save_prefs())
            self.assertNotIn(self.key, json.dumps(save.call_args.args[0]))
        self.assertEqual(app.v_api_key.get(), "")
        self.assertIn("新密钥已配置", app.v_api_feedback.get())
        self.assertIn("未验证 API", app.v_api_feedback.get())
        self.assertNotIn(self.key, app.v_api_feedback.get())
        app.status.set("后台状态更新")
        self.assertIn("保存成功", app.v_api_feedback.get())
        with patch.object(prefs, "save"), patch.object(gui.credentials, "save") as save_key:
            self.assertTrue(app.save_prefs())
            save_key.assert_not_called()
        self.assertIn("密钥未更改", app.v_api_feedback.get())

    def test_failures_and_partial_success_are_not_reported_as_saved(self):
        app = self.app
        app.v_api_key.set(self.key)
        app.v_summary_chars.set("invalid")
        with patch.object(gui.credentials, "save") as save:
            self.assertFalse(app.save_prefs())
            save.assert_not_called()
        self.assertIn("未保存", app.v_api_feedback.get())
        self.assertEqual(app.v_api_key.get(), self.key)
        app.v_summary_chars.set("2000")
        with patch.object(gui.credentials, "save", side_effect=OSError(self.key)):
            self.assertFalse(app.save_prefs())
        self.assertIn("保存失败", app.v_api_feedback.get())
        self.assertNotIn(self.key, app.v_api_feedback.get())
        with patch.object(prefs, "save", side_effect=OSError("fixture write failure")):
            self.assertFalse(app.save_prefs())
        self.assertEqual(app.v_api_key.get(), "")
        self.assertIn("新密钥已保存，但配置保存失败", app.v_api_feedback.get())

    def test_cancel_import_export_duplicate_sender_and_log_refresh(self):
        app = self.app
        with patch.object(gui.filedialog, "askopenfilename", return_value=""):
            app.import_api_config()
        self.assertIn("已取消导入", app.v_api_feedback.get())
        with patch.object(gui.filedialog, "asksaveasfilename", return_value=""):
            app.export_api_config()
        self.assertIn("已取消导出", app.v_api_feedback.get())
        app.v_blocked_sender.set("")
        app._add_blocked_sender()
        self.assertIn("请先填写", app.status.get())
        for _ in range(2):
            app.v_blocked_sender.set("fixture sender")
            app._add_blocked_sender()
        self.assertIn("已在列表", app.status.get())
        app.lst_blocked_senders.selection_clear(0, "end")
        app._remove_blocked_senders()
        self.assertIn("请先选中", app.status.get())
        app.v_log_src.set("服务日志")
        app._refresh_log()
        self.assertIn("最新记录", app.status.get())

    def test_open_paths_reports_failure_and_only_os_handoff_success(self):
        app = self.app
        with patch.object(gui, "open_path", side_effect=OSError("fixture missing")):
            app.open_data_dir()
        self.assertIn("打开失败", app.status.get())
        with patch.object(gui, "open_url"):
            app._open_external("https://example.invalid")
        self.assertIn("已交给系统", app.status.get())
        with patch.object(gui, "open_path") as opened, patch.object(prefs, "save"):
            app.open_prefs_file()
        self.assertEqual(opened.call_args.args, (app.prefs_file,))

    def test_reload_invalid_file_preserves_edits_and_close_cancel_preserves_refresh(self):
        app = self.app
        app.prefs_file.write_text("invalid json", encoding="utf-8")
        app.v_summary_chars.set("3456")
        app.reload_prefs()
        self.assertIn("重新加载失败", app.v_api_feedback.get())
        self.assertEqual(app.v_summary_chars.get(), "3456")
        app.proc = object()
        app.qr_watching = True
        app.v_rt_auto.set(True)
        with patch.object(gui.messagebox, "askyesno", return_value=False):
            app.on_close()
        self.assertIn("已取消关闭", app.status.get())
        self.assertTrue(app.v_rt_auto.get())
        self.assertTrue(app.qr_watching)
        with patch.object(app, "_login_config", return_value={"napcat": {"account": "123"}}), \
                patch.object(gui.messagebox, "askyesno", return_value=False):
            app.restart_napcat()
        self.assertIn("已取消强制重启", app.status.get())

    def test_group_refresh_does_not_block_tk_and_has_failure_completion(self):
        app = self.app
        app.proc = object()
        with patch.object(gui.threading, "Thread") as thread:
            app.refresh_groups()
            self.assertTrue(app.btn_groups_refresh.instate(["disabled"]))
            self.assertIn("正在刷新", app.status.get())
            app.refresh_groups()
            self.assertEqual(thread.call_count, 1)
            work = thread.call_args.kwargs["target"]
        with patch.object(gui.urllib.request, "build_opener", side_effect=OSError("offline")):
            work()
        kind, value = app.events.get_nowait()
        app._dispatch_event(kind, value)
        self.assertFalse(app._groups_busy)
        self.assertTrue(app.btn_groups_refresh.instate(["!disabled"]))
        self.assertIn("本地库", app.status.get())
        app._dispatch_event("groups_done", ({}, True))
        self.assertIn("未确认", app.status.get())
        app._dispatch_event("groups_done", ({"uin": "123", "deleted": [], "protected": []}, True))
        self.assertIn("清理完成", app.status.get())

    def test_usage_refresh_and_rt_busy_have_visible_feedback(self):
        app = self.app
        app.proc = object()
        with patch.object(gui.threading, "Thread") as thread:
            app.refresh_usage()
            app.refresh_usage()
            self.assertEqual(thread.call_count, 1)
            self.assertIn("正在读取", app.v_usage.get())
            self.assertTrue(app.btn_usage_refresh.instate(["disabled"]))
        app._render_usage({"error": "fixture error"})
        self.assertIn("读取失败", app.v_usage.get())
        self.assertTrue(app.btn_usage_refresh.instate(["!disabled"]))
        app._rt_busy = True
        app.refresh_messages()
        self.assertIn("请稍候", app.v_rt_status.get())

    def test_vision_wait_matches_flex_and_completion_is_not_assumed(self):
        app = self.app
        with patch.object(gui.threading, "Thread") as thread:
            app._start_rt_action("1", "vision", {})
            app._start_rt_action("1", "vision", {})
            self.assertEqual(thread.call_count, 1)
            work = thread.call_args.kwargs["target"]
        opener = Mock()
        opener.open.side_effect = TimeoutError("fixture timeout")
        with patch.object(gui.urllib.request, "build_opener", return_value=opener):
            work()
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 960)
        kind, value = app.events.get_nowait()
        app._dispatch_event(kind, value)
        self.assertIn("结果未确认", app.status.get())

    def test_status_remains_visible_at_minimum_size_and_api_feedback_near_button(self):
        app = self.app
        self.root.geometry("820x560")
        app.status.set("保存失败：" + "说明内容" * 40)
        self.root.update()
        self.assertTrue(app.status_label.winfo_ismapped())
        self.assertLess(app.status_label.winfo_rooty() + app.status_label.winfo_height(), app.nb.winfo_rooty() + 1)
        app._settings_feedback("保存成功 · 新密钥已配置")
        app.nb.select(2)
        app._scroll_canvas.yview_moveto(0.35)
        self.root.update()
        self.assertTrue(app.api_feedback_label.winfo_ismapped())
        self.assertIn("保存成功", app.v_api_feedback.get())


if __name__ == "__main__":
    unittest.main()
