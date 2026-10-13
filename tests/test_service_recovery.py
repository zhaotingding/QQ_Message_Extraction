"""离线验证异常退出恢复及原生输出留存，不启动消息服务或操作 QQ。"""
from __future__ import annotations

import copy
import io
import queue
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / "src"))

from msgx import gui
from msgx.config import DEFAULTS


class Value:
    def __init__(self, value=""):
        self.value = value

    def set(self, value):
        self.value = value

    def get(self):
        return self.value


class Root:
    def __init__(self):
        self.callbacks = {}
        self.delays = []
        self.serial = 0
        self.destroyed = False

    def after(self, delay, callback):
        self.serial += 1
        identity = f"after-{self.serial}"
        self.callbacks[identity] = callback
        self.delays.append(delay)
        return identity

    def after_cancel(self, identity):
        del self.callbacks[identity]

    def fire(self, identity):
        self.callbacks.pop(identity)()

    def destroy(self):
        self.destroyed = True
        self.callbacks.clear()


def process(pid=123, code=None):
    proc = SimpleNamespace(pid=pid, stdout=io.StringIO(), returncode=code)
    proc.poll = lambda: proc.returncode
    proc.wait = Mock(side_effect=lambda **_: proc.returncode)
    return proc


class ServiceRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = PROJ / "data/test-temp"
        folder.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="service-recovery-", dir=folder)
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        app = gui.App.__new__(gui.App)
        self.app = app
        app.root = Root()
        app.cfg = copy.deepcopy(DEFAULTS)
        app.cfg["app"]["db_path"] = str(self.folder / "messages.db")
        app.config_path = None
        app.prefs_file = self.folder / "prefs.yaml"
        app.stop_file = self.folder / "stop.signal"
        app.prefs = {"qq_account": "100"}
        app.proc = process()
        app.events = queue.Queue()
        app.closing = False
        app._service_wanted = True
        app._service_recoveries = 0
        app._service_restart_id = None
        app._expected_service_exit = None
        app._service_recovering = False
        app._receiver_rebuilding = False
        app._restart_cancelled = False
        app._login_busy = False
        app._login_pending = False
        app._login_verify_since = 0
        app._login_account = ""
        app._login_wait_noted = False
        app.qr_watching = False
        for name in ("status", "v_service", "v_peer", "v_protocol_detail", "v_stored",
                     "v_cost", "v_total", "v_balance", "v_llm", "v_rt_auto"):
            setattr(app, name, Value())
        for name in ("btn_start", "btn_stop", "btn_relogin", "btn_restart"):
            setattr(app, name, Mock())
        for name in ("_trace_gui", "_log_ui", "_verify_login_message", "_apply_summary_schedule"):
            setattr(app, name, Mock())
        app._save_report_notes = Mock(return_value=True)
        app.save_prefs = Mock(return_value=True)
        app._login_config = Mock(return_value={"napcat": {"account": "100"}})

    def exit(self, code=1):
        proc = self.app.proc
        proc.returncode = code
        self.app._dispatch_event("exit", (proc, code))
        return proc

    def test_abnormal_exit_recovers_after_delay_without_operating_qq(self):
        app = self.app
        with patch.object(gui.napcat_mod, "restart_target") as restart, \
                patch.object(gui.napcat_mod, "refresh_login_qrcode") as qr, \
                patch.object(app, "start_service", return_value=True) as start:
            self.exit()
            start.assert_not_called()
            self.assertEqual(app.root.delays, [5000])
            self.assertIn("5 秒后自动恢复", app.status.get())
            app.root.fire(app._service_restart_id)
            start.assert_called_once_with(automatic=True)
            restart.assert_not_called()
            qr.assert_not_called()

    def test_clean_config_expected_and_closing_exits_do_not_restart(self):
        for code, expected, closing in ((0, False, False), (2, False, False),
                                         (1, True, False), (1, False, True)):
            with self.subTest(code=code, expected=expected, closing=closing):
                app = self.app
                app.proc = process(code=code)
                app._service_wanted = True
                app._expected_service_exit = app.proc if expected else None
                app.closing = closing
                self.exit(code)
                self.assertIsNone(app._service_restart_id)
                self.assertEqual(app.root.callbacks, {})
                if code in (0, 2):
                    self.assertFalse(app._service_wanted)

    def test_stale_exit_does_not_clear_new_process_or_schedule_recovery(self):
        app = self.app
        old, new = process(1, 1), process(2)
        app.proc = new
        app._expected_service_exit = old
        app.status.set("新服务正常")
        app._dispatch_event("exit", (old, 1))
        self.assertIs(app.proc, new)
        self.assertFalse(new.stdout.closed)
        self.assertEqual(app.status.get(), "新服务正常")
        self.assertEqual(app.root.callbacks, {})

    def test_stop_during_delay_cancels_recovery_and_stale_callback_is_harmless(self):
        app = self.app
        self.exit()
        callback = app.root.callbacks[app._service_restart_id]
        app.stop_service()
        self.assertFalse(app._service_wanted)
        self.assertIsNone(app._service_restart_id)
        self.assertEqual(app.root.callbacks, {})
        self.assertIn("已取消自动恢复", app.status.get())
        with patch.object(app, "start_service") as start:
            callback()
            start.assert_not_called()

    def test_close_during_delay_cancels_recovery(self):
        app = self.app
        self.exit()
        callback = app.root.callbacks[app._service_restart_id]
        app.on_close()
        self.assertTrue(app.closing)
        self.assertTrue(app.root.destroyed)
        self.assertFalse(app._service_wanted)
        self.assertIsNone(app._service_restart_id)
        with patch.object(app, "start_service") as start:
            callback()
            start.assert_not_called()

    def test_three_rapid_recoveries_then_circuit_breaker(self):
        app = self.app

        def start(**kwargs):
            self.assertEqual(kwargs, {"automatic": True})
            app.proc = process(123 + app._service_recoveries)
            return True

        with patch.object(app, "start_service", side_effect=start) as started:
            for attempt in range(1, 4):
                self.exit()
                self.assertEqual(app._service_recoveries, attempt)
                app.root.fire(app._service_restart_id)
            self.exit()
            self.assertEqual(started.call_count, 3)
        self.assertEqual(app.root.delays, [5000, 15000, 30000])
        self.assertFalse(app._service_wanted)
        self.assertEqual(app.root.callbacks, {})
        self.assertIn("已停止自动恢复", app.status.get())

    def test_stable_uptime_resets_budget_but_first_health_does_not(self):
        app = self.app
        app._service_recoveries = 3
        app._service_recovering = True
        data = {"uptime_seconds": 299, "protocol": {"state": "disconnected"}}
        app._dispatch_event("health", (app.proc, data))
        self.assertEqual(app._service_recoveries, 3)
        self.assertIn("接收服务已重新启动", app.status.get())
        data["uptime_seconds"] = 300
        app._dispatch_event("health", (app.proc, data))
        self.assertEqual(app._service_recoveries, 0)
        self.exit()
        self.assertEqual(app.root.delays, [5000])

    def test_automatic_start_does_not_save_uncommitted_edits(self):
        app = self.app
        app.proc = None
        app.v_unsaved_key = Value("sk-SYNTHETIC-UNSAVED-KEY")
        app.save_prefs.side_effect = AssertionError("不能保存编辑中的字段")
        app._login_config.side_effect = AssertionError("不能验证未保存字段")
        spawned = process()
        with patch.object(gui.subprocess, "Popen", return_value=spawned) as popen, \
                patch.object(gui.threading, "Thread") as thread:
            self.assertTrue(app.start_service(automatic=True))
        app.save_prefs.assert_not_called()
        app._login_config.assert_not_called()
        self.assertEqual(app.v_unsaved_key.get(), "sk-SYNTHETIC-UNSAVED-KEY")
        self.assertFalse(app.prefs_file.exists())
        self.assertIs(app.proc, spawned)
        self.assertTrue(app._service_recovering)
        self.assertTrue(app._service_wanted)
        self.assertNotIn("sk-SYNTHETIC-UNSAVED-KEY", str(popen.call_args))
        thread.return_value.start.assert_called_once()

    def test_custom_config_precedes_subcommand_for_manual_and_automatic_start(self):
        app = self.app
        app.config_path = self.folder / "config.yaml"
        for automatic in (False, True):
            with self.subTest(automatic=automatic):
                app.proc = None
                with patch.object(gui.subprocess, "Popen", return_value=process()) as popen, \
                        patch.object(gui.threading, "Thread"):
                    self.assertTrue(app.start_service(automatic=automatic))
                args = popen.call_args.args[0]
                self.assertEqual(args[4:], ["--config", str(app.config_path), "run",
                                            "--stop-file", str(app.stop_file)])

    def test_receiver_cleanup_is_expected_and_does_not_double_start(self):
        app = self.app
        old, new = app.proc, process(124)
        app._receiver_rebuilding = True
        old.wait.side_effect = lambda **_: setattr(old, "returncode", 0) or 0
        app._stop_unresponsive_receiver(old)
        self.assertIs(app._expected_service_exit, old)

        def start():
            app.proc = new
            app._service_wanted = True
            return True

        with patch.object(app, "start_service", side_effect=start) as started, \
                patch.object(app, "_restart_with_receiver") as receiver:
            app._dispatch_event("receiver_recovered", (old, app.cfg))
            started.assert_called_once_with()
            receiver.assert_called_once_with(app.cfg, recover=False)
            app._dispatch_event("exit", (old, 1))
        self.assertIs(app.proc, new)
        self.assertEqual(app.root.callbacks, {})

    def test_rebuilding_suppresses_auto_but_plain_login_busy_does_not(self):
        app = self.app
        app._receiver_rebuilding = True
        self.exit()
        self.assertEqual(app.root.callbacks, {})
        app.proc = process()
        app._receiver_rebuilding = False
        app._login_busy = True
        self.exit()
        self.assertEqual(app.root.delays, [5000])

    def test_stop_signal_failure_preserves_running_intent(self):
        app = self.app
        with patch.object(Path, "touch", side_effect=PermissionError("fixture write failure")):
            app.stop_service()
        self.assertTrue(app._service_wanted)
        self.assertIsNone(app._expected_service_exit)
        self.assertIn("服务停止失败", app.status.get())
        self.exit()
        self.assertEqual(app.root.delays, [5000])

    def test_native_stdout_stderr_and_exit_are_saved_and_redacted(self):
        app = self.app
        key = "sk-SYNTHETIC-SERVICE-OUTPUT-123456789"
        script = f"import sys; print('stdout fixture'); print('stderr {key}', file=sys.stderr); sys.exit(1)"
        proc = subprocess.Popen([sys.executable, "-u", "-c", script],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        try:
            app._read_pipe(proc)
        finally:
            proc.stdout.close()
        saved = (self.folder / "logs/service-output.log").read_text(encoding="utf-8")
        self.assertIn(f"launcher_pid={proc.pid}", saved)
        self.assertIn("stdout fixture", saved)
        self.assertIn("stderr", saved)
        self.assertIn("退出码 1", saved)
        self.assertNotIn(key, saved)
        self.assertIn("[已隐藏密钥]", saved)
        events = list(app.events.queue)
        self.assertEqual(events[-1], ("exit", (proc, 1)))
        self.assertNotIn(key, str(events))
        self.assertEqual(sum(kind == "line" for kind, _ in events), 2)

    def test_unwritable_output_log_still_delivers_lines_and_exit(self):
        app = self.app
        proc = process(code=1)
        proc.stdout = io.StringIO("继续接收消息\n")
        with patch.object(gui, "RotatingFileHandler", side_effect=PermissionError):
            app._read_pipe(proc)
        events = list(app.events.queue)
        self.assertEqual(events[0][0], "note")
        self.assertIn("继续接收消息", events[0][1])
        self.assertIn(("line", "继续接收消息\n"), events)
        self.assertEqual(events[-1], ("exit", (proc, 1)))


if __name__ == "__main__":
    unittest.main()
