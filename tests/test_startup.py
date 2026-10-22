"""轻量 EXE、同环境后台解释器和离线诊断回归；不启动真实界面或网络请求。"""
import ctypes
import importlib.util
import io
import os
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # 测试只导入本仓库源码。
from msgx import conda_runtime


def load_tool(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # 只加载定义，不执行工具的 main 入口。
    return module


class IsolatedToolTest(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / "data/test-temp"  # 不接触真实项目环境、偏好或日志。
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="msgx startup ", dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)


class LauncherTests(IsolatedToolTest):
    def setUp(self):
        super().setUp()
        self.launcher = load_tool("startup_exe", "exe_main.py")
        self.launcher.PROJECT_DIR = self.folder
        self.launcher.GUI_ENTRY = self.folder / "tools/gui_entry.py"
        self.launcher.LOG_PATH = self.folder / ".ui/exe_error.log"

    def conda_binary(self):
        path = self.folder / "Conda/Scripts/conda.exe"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()  # 仅用于路径选择，真正 Conda 执行另做隔离验收。
        return path

    def gui_entry(self):
        self.launcher.GUI_ENTRY.parent.mkdir(parents=True, exist_ok=True)
        self.launcher.GUI_ENTRY.touch()
        source = self.folder / "src/msgx/gui.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.touch()  # 环境失败测试须先满足源码完整性门槛。

    def test_missing_conda_reports_installation_without_running_global_python(self):
        self.gui_entry()  # 先满足源码条件，确实走到环境缺失分支。
        with patch.dict(os.environ, {"CONDA_EXE": "", "MSGX_CONDA_ENV": "msgx"}), \
                patch.object(conda_runtime.shutil, "which", return_value=None), \
                patch.object(self.launcher, "_report") as report, \
                patch.object(self.launcher.subprocess, "run") as run:
            self.assertEqual(self.launcher.main(), 1)
        run.assert_not_called()
        report.assert_called_once()
        detail = "\n".join(report.call_args.args)
        self.assertIn("conda.exe", detail)
        self.assertIn("README.md", detail)
        self.assertNotIn("start.cmd", detail)  # 安装引导必须指向当前 PowerShell 流程。

    def test_main_launches_selected_conda_environment_entry_and_directory(self):
        conda = self.conda_binary()
        self.gui_entry()
        with patch.dict(os.environ, {"CONDA_EXE": str(conda), "MSGX_CONDA_ENV": "msgx-test"}), \
                patch.object(self.launcher, "_report") as report, \
                patch.object(self.launcher.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(self.launcher.main(), 0)
        report.assert_not_called()
        self.assertEqual(run.call_args.args[0], [str(conda), "run", "-n", "msgx-test",
                                               "--no-capture-output", "python", "-s", str(self.launcher.GUI_ENTRY)])
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.folder))
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONPATH"], str(self.folder / "src"))
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONNOUSERSITE"], "1")

    def test_conda_failure_returns_error_and_preserves_diagnostic_output(self):
        conda = self.conda_binary()
        self.gui_entry()
        def fail(*args, **kwargs):
            kwargs["stdout"].write("EnvironmentNameNotFound: msgx-missing\n")
            from subprocess import CompletedProcess
            return CompletedProcess(args[0], 1)
        with patch.dict(os.environ, {"CONDA_EXE": str(conda), "MSGX_CONDA_ENV": "msgx-missing"}), \
                patch.object(self.launcher.subprocess, "run", side_effect=fail), \
                patch.object(self.launcher, "_report") as report:
            self.assertEqual(self.launcher.main(), 1)
        self.assertIn("EnvironmentNameNotFound", report.call_args.args[1])
        self.assertIn("conda create -n msgx-missing", report.call_args.args[1])

    def test_build_env_replaces_foreign_pythonpath_and_preserves_data_root(self):
        foreign = str(self.folder / "other-project/src")
        data_root = str(self.folder / "separate-data")
        with patch.dict(os.environ, {"PYTHONPATH": foreign, "MSGX_ROOT": data_root}):
            env = self.launcher.build_env()
        self.assertEqual(env["PYTHONPATH"], str(self.folder / "src"))
        self.assertNotIn(foreign, env["PYTHONPATH"])
        self.assertEqual(env["MSGX_ROOT"], data_root)  # 显式数据目录不是源码目录。

    @unittest.skipUnless(sys.platform == "win32", "Windows 可见错误提示")
    def test_report_writes_log_and_displays_failure(self):
        with patch.object(ctypes.windll.user32, "MessageBoxW", return_value=1) as show:
            self.launcher._report("启动失败", "请按 README.md 准备 Conda 环境")
        self.assertIn("准备 Conda", self.launcher.LOG_PATH.read_text(encoding="utf-8"))
        show.assert_called_once()  # 替代真实提示框，防止测试阻塞或弹窗。
        self.assertIn("准备 Conda", show.call_args.args[1])

    @unittest.skipUnless(sys.platform == "win32", "Windows 可见错误提示")
    def test_gui_report_writes_log_and_displays_failure(self):
        entry = load_tool("startup_gui", "gui_entry.py")
        entry.ROOT = self.folder
        with patch.dict(os.environ, {"MSGX_EXE_LAUNCH": ""}), redirect_stderr(io.StringIO()), \
                patch.object(ctypes.windll.user32, "MessageBoxW", return_value=1) as show:
            entry._report("测试解释器缺少 Tk")
        self.assertEqual((self.folder / ".ui/gui_error.log").read_text(encoding="utf-8"), "测试解释器缺少 Tk")
        show.assert_called_once()
        self.assertIn("缺少 Tk", show.call_args.args[1])

    @unittest.skipUnless(sys.platform == "win32", "Windows 可见错误提示")
    def test_reports_remain_visible_when_log_directory_is_unwritable(self):
        entry = load_tool("startup_gui_unwritable", "gui_entry.py")
        entry.ROOT = self.folder
        reporters = ((self.launcher._report, ("启动失败", "测试无法保存日志")),
                     (entry._report, ("测试无法保存日志",)))
        for report, arguments in reporters:
            with self.subTest(entry=report.__module__), redirect_stderr(io.StringIO()), \
                    patch.dict(os.environ, {"MSGX_EXE_LAUNCH": ""}), \
                    patch.object(Path, "mkdir", side_effect=PermissionError), \
                    patch.object(ctypes.windll.user32, "MessageBoxW", return_value=1) as show:
                report(*arguments)
                show.assert_called_once()  # 保存失败仍必须向用户反馈。
                self.assertIn("无法保存日志", show.call_args.args[1])

    def test_gui_report_defers_to_exe_without_duplicate_dialog(self):
        entry = load_tool("startup_gui_exe", "gui_entry.py")
        entry.ROOT = self.folder
        error = io.StringIO()
        with patch.dict(os.environ, {"MSGX_EXE_LAUNCH": "1"}), redirect_stderr(error), \
                patch.object(ctypes.windll.user32, "MessageBoxW") as show:
            entry._report("环境错误")
        show.assert_not_called()
        self.assertIn("环境错误", error.getvalue())


class CondaEnvironmentTests(IsolatedToolTest):
    def test_environment_name_rejects_public_environment_and_paths(self):
        for name in ("base", "ROOT", "", "../other", "D:/Conda/envs/msgx"):
            with self.subTest(name=name), patch.dict(os.environ, {"MSGX_CONDA_ENV": name}):
                with self.assertRaises(RuntimeError):
                    conda_runtime.environment_name()
        with patch.dict(os.environ, {"MSGX_CONDA_ENV": "msgx-dev"}):
            self.assertEqual(conda_runtime.environment_name(), "msgx-dev")

    def test_explicit_invalid_conda_does_not_select_another_installation(self):
        with patch.dict(os.environ, {"CONDA_EXE": str(self.folder / "missing/conda.exe")}), \
                patch.object(conda_runtime.shutil, "which") as find:
            with self.assertRaises(RuntimeError):
                conda_runtime.conda_executable()
        find.assert_not_called()

    def test_condabin_path_resolves_executable_without_running_batch(self):
        executable = self.folder / "Conda/Scripts/conda.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
        wrapper = self.folder / "Conda/condabin/conda.bat"
        with patch.dict(os.environ, {"CONDA_EXE": ""}), \
                patch.object(conda_runtime.shutil, "which", side_effect=[None, str(wrapper)]):
            self.assertEqual(conda_runtime.conda_executable(), str(executable))

    def test_actual_prefix_must_match_selected_conda_environment(self):
        prefix = self.folder / "envs/msgx"
        (prefix / "conda-meta").mkdir(parents=True)
        env = {"CONDA_PREFIX": str(prefix), "CONDA_DEFAULT_ENV": "msgx", "MSGX_CONDA_ENV": "msgx"}
        with patch.dict(os.environ, env), patch.object(sys, "prefix", str(prefix)), \
                patch.object(conda_runtime, "conda_executable", return_value=str(self.folder / "Conda/Scripts/conda.exe")):
            self.assertEqual(conda_runtime.require_environment(), prefix)
            with patch.dict(os.environ, {"CONDA_DEFAULT_ENV": str(prefix)}):
                self.assertEqual(conda_runtime.require_environment(), prefix)  # Conda 自定义环境目录的正常输出。
            with patch.dict(os.environ, {"CONDA_DEFAULT_ENV": str(self.folder / "other/msgx")}):
                with self.assertRaises(RuntimeError):
                    conda_runtime.require_environment()
            with patch.object(sys, "prefix", str(self.folder / "global")):
                with self.assertRaisesRegex(RuntimeError, "全局 Python"):
                    conda_runtime.require_environment()
            with patch.dict(os.environ, {"CONDA_DEFAULT_ENV": "base"}):
                with self.assertRaises(RuntimeError):
                    conda_runtime.require_environment()
            with patch.object(conda_runtime.site, "ENABLE_USER_SITE", True):
                with self.assertRaisesRegex(RuntimeError, "site-packages"):
                    conda_runtime.require_environment()

    def test_gui_entry_rejects_wrong_environment_before_business_import(self):
        entry = load_tool("startup_gui_guard", "gui_entry.py")
        with patch.object(conda_runtime, "require_environment", side_effect=RuntimeError("wrong environment")), \
                patch.object(entry, "_report") as report:
            self.assertEqual(entry.main(), 1)
        report.assert_called_once_with("wrong environment")

    def test_cli_checks_environment_before_importing_business_dependencies(self):
        script = ("import os, sys, runpy; os.environ['CONDA_DEFAULT_ENV'] = 'base'; "
                  "sys.modules['aiohttp'] = None; runpy.run_module('msgx.main', run_name='__main__')")
        result = subprocess.run(conda_runtime.command("-c", script), cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("不是项目专用环境", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)


class DiagnosticTests(IsolatedToolTest):
    def test_diagnostic_uses_configured_paths_and_port_without_probe(self):
        module = load_tool("startup_diagnose", "diagnose_freeze.py")
        cfg = {**module.config_mod.DEFAULTS,
               "app": {**module.config_mod.DEFAULTS["app"], "db_path": str(self.folder / "isolated.db")},
               "onebot": {**module.config_mod.DEFAULTS["onebot"], "http_port": 18765}}
        with patch.object(module.config_mod, "load", return_value=cfg), \
                patch.object(module.napcat_mod, "resolve_dir", return_value=self.folder / "NapCat"), \
                patch.object(module, "snapshot", return_value={}), \
                patch.object(module, "health", return_value=None), \
                patch.object(module, "qq_db_report", return_value="未接入 QQ"), \
                patch.object(module, "probe") as probe, \
                patch.object(sys, "argv", ["diagnose_freeze.py", "--no-probe"]), \
                redirect_stdout(io.StringIO()):
            module.main()
        probe.assert_not_called()
        self.assertEqual(module.BASE, "http://127.0.0.1:18765")
        self.assertEqual(module.DB_FILE, self.folder / "isolated.db")
        self.assertEqual(module.NAPCAT_DIR, self.folder / "NapCat")


if __name__ == "__main__":
    unittest.main()
