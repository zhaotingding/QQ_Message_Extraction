"""v0.8.8 操作反馈回归；隔离配置、合成凭据、本机无服务。"""
import copy
import os
import json
import sys
import threading
import tempfile
import tkinter as tk
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx import local_client, service_process
from msgx.ui import settings, login
from msgx import gui, prefs, api_settings
from msgx.config import DEFAULTS
from msgx.store import Store


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = copy.deepcopy(DEFAULTS)
        cfg["app"]["db_path"] = str(Path(self.tmp.name) / "test.db")
        cfg["app"]["report_dir"] = str(Path(self.tmp.name) / "reports")
        cfg["napcat"]["dir"] = str(Path(self.tmp.name) / "nonexistent-napcat")
        cfg["onebot"].update(http_port=1, ws_port=1)  # 偶发刷新也不会访问已运行的接收服务。
        Store(cfg["app"]["db_path"]).close()
        self.root = tk.Tk()
        self.addCleanup(self.close_root)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(gui, "ROOT", Path(self.tmp.name)))  # 初始化也不能读取真实图标。
        self.patches.enter_context(patch.dict(os.environ, {"MSGX_PREFS": str(Path(self.tmp.name) / "prefs.yaml")}))
        self.patches.enter_context(patch.object(local_client.urllib.request, "build_opener",
                                               side_effect=AssertionError("测试禁止真实网络请求")))
        for method in ("read", "save", "delete"):
            self.patches.enter_context(patch.object(settings.credentials, method, return_value=""))
        with ExitStack() as setup:
            setup.enter_context(patch.object(gui, "load", return_value=cfg))
            setup.enter_context(patch.object(prefs, "load", return_value=copy.deepcopy(prefs.DEFAULTS)))
            setup.enter_context(patch.object(prefs, "load_raw", return_value={}))
            for method in ("refresh_groups", "refresh_report_history", "_refresh_keyword_messages",
                           "_tick_health", "_tick_realtime", "_trace_gui"):
                setup.enter_context(patch.object(gui.App, method))
            self.app = gui.App(self.root)
        self.app.prefs_file = Path(self.tmp.name) / "prefs.yaml"
        self.app.stop_file = Path(self.tmp.name) / "stop.signal"
        self.patches.enter_context(patch.object(self.app, "_trace_gui"))
        self.key = "sk-feedback-SYNTHETIC-NOT-REAL-123456789"

    def close_root(self):
        for callback in self.root.tk.call("after", "info"):
            self.root.tk.call("after", "cancel", callback)
        self.root.destroy()

    def test_unconfigured_status_does_not_start_or_save_anything(self):
        app = self.app
        app.prefs["qq_account"] = ""
        app.v_qq_account.set("123")  # 尚未保存的输入不能让状态提前变成已配置。
        with patch.object(app, "napcat_dir", return_value=None), patch.object(app.root, "after"), \
                patch.object(app.service, "spawn") as spawn, patch.object(prefs, "save") as save:
            app._tick_health()
            self.assertEqual(app.v_service.get(), "待配置")
            self.assertIn("目标 QQ 号", app.v_protocol_detail.get())
            self.assertIn("NapCat 目录", app.v_protocol_detail.get())
            self.assertIsNone(app._login_config())
            spawn.assert_not_called()
            save.assert_not_called()
        self.assertIsNone(app.proc)

    def test_saved_settings_and_discovered_directory_clear_setup_hint(self):
        app = self.app
        app.prefs["qq_account"] = "123"
        with patch.object(app, "napcat_dir", return_value=Path(self.tmp.name)), \
                patch.object(app.root, "after"):
            app._tick_health()
        self.assertEqual(app.v_service.get(), "未运行")
        self.assertNotIn("待配置", app.v_protocol_detail.get())
        self.assertIsNone(app.proc)  # 设置齐全也不会自动启动后台。
        with patch.object(app, "napcat_dir", return_value=None), patch.object(app.root, "after"):
            app._tick_health()
        self.assertEqual(app.v_service.get(), "待配置")
        self.assertNotIn("目标 QQ 号", app.v_protocol_detail.get())
        self.assertIn("NapCat 目录", app.v_protocol_detail.get())

    def test_missing_protocol_directory_does_not_mask_live_connection(self):
        app = self.app
        app.prefs["qq_account"] = "123"
        app.cfg["napcat"]["start_script"] = "tools/start_napcat.bat"
        with patch.object(app, "napcat_dir", return_value=None), \
                patch.object(app, "_apply_summary_schedule"):
            app._apply_health({"protocol": {"state": "waiting"}})
            self.assertEqual(app.v_service.get(), "运行中")
            self.assertIn("待配置", app.v_peer.get())
            app._apply_health({"protocol": {"state": "connected"}, "ws_peers": ["127.0.0.1"]})
        self.assertEqual(app.v_service.get(), "运行中")
        self.assertIn("已连接", app.v_peer.get())
        self.assertIn("等待新消息验证", app.v_peer.get())
        self.assertNotIn("待配置", app.v_protocol_detail.get())

    def test_forced_restart_recovers_receiver_before_restarting_qq(self):
        app = self.app
        old, new = Mock(stdout=None), Mock(stdout=None)
        old.poll.return_value = None
        new.poll.return_value = None
        app.service.proc = old
        cfg = {**app.cfg, "napcat": {"account": "100"}}
        order = []
        def stop(proc):
            self.assertIs(proc, old)
            old.poll.return_value = 0
            old.returncode = 0
            order.append("stop")
        def start():
            app.service.proc = new
            order.append("start")
            return True
        with patch.object(gui.messagebox, "askyesno", return_value=True), \
                patch.object(app, "_login_config", return_value=cfg), \
                patch.object(app, "_wait_receiver_ready", side_effect=[False, True]), \
                patch.object(app, "_stop_unresponsive_receiver", side_effect=stop), \
                patch.object(app, "start_service", side_effect=start), \
                patch.object(login.napcat_mod, "restart_target", side_effect=lambda c: order.append("qq") or {"ok":False,"note":"扫码待验证"}), \
                patch.object(threading, "Thread") as thread:
            app.restart_napcat()
            thread.call_args.kwargs["target"]()
            while not app.events.empty():
                app._dispatch_event(*app.events.get_nowait())
            self.assertEqual(order, ["stop", "start"])
            thread.call_args.kwargs["target"]()
            while not app.events.empty():
                app._dispatch_event(*app.events.get_nowait())
            self.assertEqual(order, ["stop", "start", "qq"])
            self.assertFalse(app._login_busy)

    def test_receiver_force_stop_only_targets_owned_tree_and_cancel_prevents_restart(self):
        app = self.app
        proc = Mock(pid=12345, stdout=None)
        proc.poll.return_value = None
        proc.wait.side_effect = [service_process.subprocess.TimeoutExpired("test", 5), 0]
        app.service.proc = proc
        app.service.restart_cancelled = False
        with patch.object(service_process.subprocess, "run") as run, patch.object(gui.os, "name", "nt"):
            app._stop_unresponsive_receiver(proc)
            self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "12345", "/T", "/F"])
            run.reset_mock()
            app.service.proc = Mock()
            app._stop_unresponsive_receiver(proc)
            run.assert_not_called()
        app.service.proc = proc
        app.service.restart_cancelled = True
        with patch.object(app, "start_service") as start:
            app._dispatch_event("receiver_recovered", (proc, app.cfg))
            start.assert_not_called()

    def test_rebuilt_receiver_failure_does_not_loop_or_restart_qq(self):
        app = self.app
        app.service.proc = Mock()
        app.service.restart_cancelled = False
        with patch.object(app, "_wait_receiver_ready", return_value=False), \
                patch.object(app, "_stop_unresponsive_receiver") as stop, \
                patch.object(login.napcat_mod, "restart_target") as restart, \
                patch.object(threading, "Thread") as thread:
            app._restart_with_receiver(app.cfg, recover=False)
            thread.call_args.kwargs["target"]()
            kind, result = app.events.get_nowait()
            self.assertEqual(kind, "login_done")
            self.assertFalse(result["ok"])
            stop.assert_not_called()
            restart.assert_not_called()

    def test_custom_icon_persists_as_local_copy(self):
        from PIL import Image
        source = Path(self.tmp.name) / "source.png"
        Image.new("RGBA", (64, 48), "red").save(source)
        with patch.object(settings.filedialog, "askopenfilename", return_value=str(source)):
            self.app.choose_app_icon()
        saved = prefs.load(self.app.prefs_file)
        icon = Path(saved["ui"]["icon_path"])
        self.assertTrue(icon.is_file())
        source.unlink()
        self.app.prefs = saved
        self.app._apply_app_icon()
        self.assertIn("已应用", self.app.v_icon_status.get())
        with patch.object(settings.filedialog, "askopenfilename", return_value=""):
            self.app.choose_app_icon()
        self.assertIn("已取消", self.app.status.get())
        self.assertTrue(icon.is_file())

    def test_ico_import_keeps_original_bytes_and_authored_small_frames(self):
        from PIL import Image
        source = Path(self.tmp.name) / "authored.ico"
        sizes = [(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
        frames = [Image.new("RGBA", size, (index*20, 50, 200, 255)) for index, size in enumerate(sizes)]
        frames[-1].save(source, sizes=sizes, append_images=frames[:-1])
        original = source.read_bytes()
        with patch.object(settings.filedialog, "askopenfilename", return_value=str(source)):
            self.app.choose_app_icon()
        target = Path(self.app.prefs["ui"]["icon_path"])
        self.assertEqual(target.read_bytes(), original)
        with Image.open(target) as icon:
            self.assertEqual(icon.ico.sizes(), set(sizes))
            self.assertEqual(icon.ico.getimage((24, 24)).getpixel((0, 0)), (40, 50, 200, 255))
        # 再选已保存的图标也不能破坏它。
        with patch.object(settings.filedialog, "askopenfilename", return_value=str(target)):
            self.app.choose_app_icon()
        self.assertEqual(target.read_bytes(), original)

    def test_png_frames_are_resampled_from_original(self):
        from PIL import Image, ImageDraw
        source = Path(self.tmp.name) / "sharp.png"
        original = Image.new("RGBA", (1024, 1024))
        draw = ImageDraw.Draw(original)
        for offset in range(0, 1024, 7):
            draw.line((offset, 0, 0, offset), fill=(0, 120, 255, 255), width=3)
        original.save(source)
        with patch.object(settings.filedialog, "askopenfilename", return_value=str(source)):
            self.app.choose_app_icon()
        with Image.open(self.app.prefs["ui"]["icon_path"]) as icon:
            for side in (16, 20, 24, 32, 40, 48, 64, 128, 256):
                expected = original.resize((side, side), Image.Resampling.LANCZOS)
                self.assertEqual(icon.ico.getimage((side, side)).tobytes(), expected.tobytes())

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
        with patch.object(prefs, "save"), patch.object(settings.credentials, "save") as save_key:
            self.assertTrue(app.save_prefs())
            save_key.assert_not_called()
        self.assertIn("密钥未更改", app.v_api_feedback.get())

    def test_failures_and_partial_success_are_not_reported_as_saved(self):
        app = self.app
        app.v_api_key.set(self.key)
        app.v_summary_chars.set("invalid")
        with patch.object(settings.credentials, "save") as save:
            self.assertFalse(app.save_prefs())
            save.assert_not_called()
        self.assertIn("未保存", app.v_api_feedback.get())
        self.assertEqual(app.v_api_key.get(), self.key)
        app.v_summary_chars.set("2000")
        with patch.object(settings.credentials, "save", side_effect=OSError(self.key)):
            self.assertFalse(app.save_prefs())
        self.assertIn("保存失败", app.v_api_feedback.get())
        self.assertNotIn(self.key, app.v_api_feedback.get())
        with patch.object(prefs, "save", side_effect=OSError("fixture write failure")):
            self.assertFalse(app.save_prefs())
        self.assertEqual(app.v_api_key.get(), "")
        self.assertIn("新密钥已保存，但配置保存失败", app.v_api_feedback.get())

    def test_cancel_import_export_duplicate_sender_and_log_refresh(self):
        app = self.app
        with patch.object(settings.filedialog, "askopenfilename", return_value=""):
            app.import_api_config()
        self.assertIn("已取消导入", app.v_api_feedback.get())
        with patch.object(settings.filedialog, "asksaveasfilename", return_value=""):
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
        app.prefs_file.write_text("filters: [", encoding="utf-8")
        app.v_summary_chars.set("3456")
        app.reload_prefs()
        self.assertIn("重新加载失败", app.v_api_feedback.get())
        self.assertEqual(app.v_summary_chars.get(), "3456")
        app.service.proc = object()
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
        app.service.proc = object()
        with patch.object(threading, "Thread") as thread:
            app.refresh_groups()
            self.assertTrue(app.btn_groups_refresh.instate(["disabled"]))
            self.assertIn("正在刷新", app.status.get())
            app.refresh_groups()
            self.assertEqual(thread.call_count, 1)
            work = thread.call_args.kwargs["target"]
        with patch.object(local_client.urllib.request, "build_opener", side_effect=OSError("offline")):
            work()
        kind, value = app.events.get_nowait()
        app._dispatch_event(kind, value)
        self.assertFalse(app._groups_busy)
        self.assertTrue(app.btn_groups_refresh.instate(["!disabled"]))
        self.assertIn("本地库", app.status.get())
        app._dispatch_event("groups_done", ({}, True, [], ""))
        self.assertIn("未确认", app.status.get())
        app._dispatch_event("groups_done", ({"uin": "123", "deleted": [], "protected": []}, True, [], ""))
        self.assertIn("清理完成", app.status.get())

    def test_local_napcat_paths_save_only_in_preferences_and_cancel_has_feedback(self):
        app = self.app
        folder = Path(self.tmp.name) / "NapCat"
        folder.mkdir()
        qq = Path(self.tmp.name) / "QQ.exe"
        qq.touch()
        original = copy.deepcopy(app.cfg)
        with patch.object(settings.filedialog, "askdirectory", return_value=str(folder)):
            app.choose_napcat_dir()
        with patch.object(settings.filedialog, "askopenfilename", return_value=str(qq)):
            app.choose_qq_path()
        self.assertIn("保存设置后生效", app.v_api_feedback.get())
        self.assertTrue(app.save_prefs())
        saved = prefs.load_raw(app.prefs_file)
        self.assertEqual(saved["napcat_dir"], str(folder))
        self.assertEqual(saved["qq_path"], str(qq))
        effective = prefs.apply_to_config(app.cfg, saved)
        self.assertEqual(effective["napcat"]["dir"], str(folder))
        self.assertEqual(effective["napcat"]["qq_path"], str(qq))
        self.assertEqual(app.cfg, original)
        with patch.object(settings.filedialog, "askdirectory", return_value=""):
            app.choose_napcat_dir()
        self.assertIn("已取消", app.v_api_feedback.get())

    def test_usage_refresh_and_rt_busy_have_visible_feedback(self):
        app = self.app
        app.service.proc = object()
        with patch.object(threading, "Thread") as thread:
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
        with patch.object(threading, "Thread") as thread:
            app._start_rt_action("1", "vision", {})
            app._start_rt_action("1", "vision", {})
            self.assertEqual(thread.call_count, 1)
            work = thread.call_args.kwargs["target"]
        opener = Mock()
        opener.open.side_effect = TimeoutError("fixture timeout")
        with patch.object(local_client.urllib.request, "build_opener", return_value=opener):
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
