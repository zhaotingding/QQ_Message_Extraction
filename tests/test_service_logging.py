"""仅用假服务与临时日志验证生命周期落盘，不启动真实服务或调用 API。"""
from __future__ import annotations

import copy
import io
import logging
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / "src"))

from msgx import main as app
from msgx.config import DEFAULTS
from msgx.security import register


class ServiceLoggingTests(unittest.TestCase):
    def setUp(self):
        temp_root = PROJ / "data/test-temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = temp_root / ("service-log-" + uuid.uuid4().hex)
        self.tmp.mkdir()
        self.root = logging.getLogger()
        self.old_handlers, self.old_level = self.root.handlers[:], self.root.level
        self.root.handlers.clear()
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.out_patch = patch("sys.stdout", self.stdout)
        self.err_patch = patch("sys.stderr", self.stderr)
        self.out_patch.start()
        self.err_patch.start()
        self.path = self.tmp / "logs/service.log"
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["app"]["db_path"] = str(self.tmp / "test.db")

    def tearDown(self):
        for handler in self.root.handlers:
            handler.close()
        self.root.handlers[:] = self.old_handlers
        self.root.setLevel(self.old_level)
        self.out_patch.stop()
        self.err_patch.stop()
        assert self.tmp.resolve().is_relative_to((PROJ / "data/test-temp").resolve())
        shutil.rmtree(self.tmp)

    def test_dates_redaction_and_stdout_format(self):
        key = "test-service-logging-secret"
        register(key)
        app.setup_logging("INFO", self.path)
        app.setup_logging("INFO", self.path)
        try:
            raise RuntimeError(key)
        except RuntimeError:
            logging.getLogger("msgx").exception("Authorization: Bearer %s", key)
        saved = self.path.read_text(encoding="utf-8")
        self.assertRegex(saved, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ERROR")
        self.assertRegex(self.stdout.getvalue(), r"^\d{2}:\d{2}:\d{2} ERROR")
        for text in (saved, self.stdout.getvalue()):
            self.assertNotIn(key, text)
            self.assertIn("[已隐藏密钥]", text)
            self.assertEqual(text.count("Authorization:"), 1)

    def test_rotation_is_bounded(self):
        app.setup_logging("INFO", self.path)
        handler = next(h for h in self.root.handlers if isinstance(h, app._ServiceLogHandler))
        self.assertEqual(handler.maxBytes, 2 * 1024 * 1024)
        self.assertEqual(handler.backupCount, 3)
        handler.maxBytes = 200
        for n in range(20):
            logging.getLogger("msgx").info("运行状态 %s", n)
        files = list(self.path.parent.glob("service.log*"))
        self.assertEqual(len(files), 4)
        self.assertTrue(all(p.stat().st_size <= 200 for p in files))

    def test_unwritable_directory_keeps_stdout(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError):
            app.setup_logging("INFO", self.path)
        logging.getLogger("msgx").info("继续运行")
        self.assertIn("服务日志无法写入", self.stdout.getvalue())
        self.assertIn("继续运行", self.stdout.getvalue())
        self.assertFalse(self.path.exists())

    def test_later_write_failure_does_not_crash_or_dump_record(self):
        app.setup_logging("INFO", self.path)
        handler = next(h for h in self.root.handlers if isinstance(h, app._ServiceLogHandler))
        handler.maxBytes = 1
        with patch.object(handler, "doRollover", side_effect=PermissionError):
            logging.getLogger("msgx").info("继续运行")
        self.assertIn("继续运行", self.stdout.getvalue())
        self.assertEqual(self.stderr.getvalue(), "")

    def test_normal_service_lifecycle(self):
        with patch.object(app, "load", return_value=self.cfg), \
                patch.object(app, "cmd_run", new=AsyncMock()):
            self.assertEqual(app.main(["run"]), 0)
        saved = self.path.read_text(encoding="utf-8")
        self.assertIn("服务进程开始启动（PID", saved)
        self.assertIn("服务进程已正常退出（退出码 0）", saved)

    def test_exception_is_logged_and_redacted(self):
        key = "test-service-exit-secret"
        register(key)
        with patch.object(app, "load", return_value=self.cfg), \
                patch.object(app, "cmd_run", new=AsyncMock(side_effect=RuntimeError(key))):
            self.assertEqual(app.main(["run"]), 1)
        saved = self.path.read_text(encoding="utf-8")
        self.assertIn("服务进程异常退出（退出码 1）", saved)
        self.assertIn("RuntimeError", saved)
        self.assertNotIn(key, saved)

    def test_explicit_startup_failure_retains_exit_code(self):
        with patch.object(app, "load", return_value=self.cfg), \
                patch.object(app, "cmd_run", new=AsyncMock(side_effect=SystemExit(2))):
            with self.assertRaises(SystemExit) as error:
                app.main(["run"])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("退出码 2", self.path.read_text(encoding="utf-8"))

    def test_non_service_command_does_not_create_service_log(self):
        with patch.object(app, "load", return_value=self.cfg), \
                patch.object(app, "cmd_summary", new=AsyncMock()):
            self.assertEqual(app.main(["summary"]), 0)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
