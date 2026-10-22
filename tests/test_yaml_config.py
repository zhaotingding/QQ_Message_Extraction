"""YAML 迁移回归：隔离配置与偏好，验证真实字段类型、错误反馈和图标读取。"""
import copy
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from msgx import config, prefs, yaml_config
from msgx.main import main


class YamlTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "data/test-temp"
        folder.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="yaml-", dir=folder)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml_config.dumps(data), encoding="utf-8")
        return path

    def test_scalar_types_regex_paths_and_multiline_roundtrip(self):
        data = {"account": "01371109209", "groups": ["867701092"],
                "names": {"00123": "技术群"}, "enabled": False, "price": .1,
                "keywords": ["on", "false", "2026-10-13", r"re:\bGPT\d+\b"],
                "path": r"D:\App\QQ\QQ.exe", "prompt": "第一行\n第二行\n"}
        path = self.write("config.yaml", data)
        self.assertEqual(yaml_config.read(path), data)
        self.assertIsInstance(yaml_config.read(path)["account"], str)
        self.assertIs(yaml_config.read(path)["enabled"], False)
        self.assertIn("prompt: |", path.read_text(encoding="utf-8"))

    def test_main_default_and_explicit_path_use_yaml(self):
        self.write("config.yaml", {"summary": {"target_chars": 3456}})
        explicit = self.write("custom.yaml", {"summary": {"target_chars": 4567}})
        with patch.object(config, "ROOT", self.root), patch.dict(os.environ, {prefs.ENV_PREFS: ""}):
            self.assertEqual(config.load()["summary"]["target_chars"], 3456)
            self.assertEqual(config.load(explicit)["summary"]["target_chars"], 4567)

    def test_local_main_config_replaces_shared_without_writing_it(self):
        shared = self.write("config.yaml", {"summary": {"target_chars": 3456},
                                          "onebot": {"http_port": 7654}})  # 共享端口不能泄漏到本机配置。
        original = shared.read_bytes()  # 读取配置不得改写共享文件。
        local = self.write(".ui/config.local.yaml", {"summary": {"target_chars": 4567}})
        local_original = local.read_bytes()  # 本机文件也只能读取，不能自动补写默认值。
        with patch.object(config, "ROOT", self.root), patch.dict(os.environ, {prefs.ENV_PREFS: ""}):
            loaded = config.load()  # 未传路径时选用完整本机主配置。
        self.assertEqual(loaded["summary"]["target_chars"], 4567)
        self.assertEqual(loaded["onebot"]["http_port"], config.DEFAULTS["onebot"]["http_port"])
        self.assertEqual(shared.read_bytes(), original)
        self.assertEqual(local.read_bytes(), local_original)

    def test_explicit_main_config_takes_priority_and_missing_path_keeps_defaults(self):
        self.write("config.yaml", {"summary": {"target_chars": 3456}})
        self.write(".ui/config.local.yaml", {"summary": {"target_chars": 4567}})
        explicit = self.write("custom.yaml", {"summary": {"target_chars": 5678}})
        with patch.object(config, "ROOT", self.root), patch.dict(os.environ, {prefs.ENV_PREFS: ""}):
            self.assertEqual(config.load(explicit)["summary"]["target_chars"], 5678)
            missing = config.load(self.root / "missing.yaml")  # 显式缺文件不能转而采用本机或共享配置。
        self.assertEqual(missing["summary"]["target_chars"], config.DEFAULTS["summary"]["target_chars"])

    def test_corrupt_local_main_config_reports_error_without_shared_fallback(self):
        self.write("config.yaml", {"summary": {"target_chars": 3456}})
        local = self.write(".ui/config.local.yaml", {})
        explicit = self.write("custom.yaml", {"summary": {"target_chars": 5678}})
        for text in ("summary: [", "onebot: 6701"):  # 同时覆盖语法损坏和配置段类型错误。
            with self.subTest(text=text):
                local.write_text(text, encoding="utf-8")
                with patch.object(config, "ROOT", self.root), patch.dict(os.environ, {prefs.ENV_PREFS: ""}):
                    with self.assertRaises(ValueError):
                        config.load()  # 不得把损坏本机配置悄悄替换成共享配置。
                    self.assertEqual(config.load(explicit)["summary"]["target_chars"], 5678)
                self.assertEqual(local.read_text(encoding="utf-8"), text)

    def test_preferences_overlay_selected_local_main_config(self):
        self.write("config.yaml", {"summary": {"target_chars": 3456}})
        self.write(".ui/config.local.yaml", {"summary": {"target_chars": 4567},
                                           "onebot": {"http_port": 7654}})
        preference_path = self.write(".ui/preferences.yaml", {"summary_target_chars": 5678})
        with patch.object(config, "ROOT", self.root), \
                patch.dict(os.environ, {prefs.ENV_PREFS: str(preference_path)}):
            loaded = config.load()  # 选定主配置之后，仍由已保存偏好覆盖 GUI 管理字段。
        self.assertEqual(loaded["summary"]["target_chars"], 5678)
        self.assertEqual(loaded["onebot"]["http_port"], 7654)

    def test_empty_document_and_invalid_mapping(self):
        self.assertEqual(yaml_config.loads("# 空配置\n"), {})
        for text in ("- item", "plain string", "summary: [", "value: !!python/object/apply:os.system []"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                yaml_config.loads(text)
        malformed = self.write("invalid.yaml", {"onebot": 6701})
        with self.assertRaisesRegex(ValueError, "onebot.*缩进"):
            config.load(malformed)

    def test_invalid_yaml_reports_location_without_source_values(self):
        secret = "SYNTHETIC-PRIVATE-VALUE"
        with self.assertRaises(ValueError) as result:
            yaml_config.loads("value: [" + secret)
        self.assertIn("第", str(result.exception))
        self.assertNotIn(secret, str(result.exception))

    def test_preferences_preserve_explicit_empty_and_overlay(self):
        data = {"qq_account": "01371109209", "message_keywords": [r"re:\bGPT\d+"],
                "filters": {"blocked_senders": []}, "napcat_hidden": False}
        path = prefs.prefs_path(self.root)
        self.assertEqual(path.name, "preferences.yaml")
        prefs.save(data, path)
        self.assertEqual(prefs.load_raw(path), data)
        self.assertFalse(path.with_suffix(".yaml.tmp").exists())
        with patch.dict(os.environ, {prefs.ENV_PREFS: str(path)}):
            loaded = prefs.load_from_env()
            self.assertNotIn("summary_target_chars", loaded)
            cfg = prefs.apply_to_config(copy.deepcopy(config.DEFAULTS), loaded)
            self.assertEqual(cfg["napcat"]["account"], data["qq_account"])
            self.assertIs(cfg["napcat"]["hidden"], False)
            self.assertEqual(cfg["filters"]["blocked_senders"], [])
        prefs.save({**data, "summary_target_chars": 3000}, path)
        with patch.dict(os.environ, {prefs.ENV_PREFS: str(path)}):
            self.assertEqual(prefs.load_from_env()["summary_target_chars"], 3000)

    def test_corrupt_preferences_keep_original_file_and_failed_write_keeps_saved(self):
        path = prefs.prefs_path(self.root)
        prefs.save({"qq_account": "123"}, path)
        original = path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("模拟写入失败")), self.assertRaises(OSError):
            prefs.save({"qq_account": "456"}, path)
        self.assertEqual(path.read_bytes(), original)
        path.write_text("filters: [", encoding="utf-8")
        self.assertEqual(prefs.load(path), prefs.DEFAULTS)
        self.assertEqual(path.read_text(encoding="utf-8"), "filters: [")

    def test_launcher_icon_reads_yaml_preferences(self):
        spec = importlib.util.spec_from_file_location("yaml_build_launcher", ROOT / "tools/build_launcher.py")
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        icon = self.root / "chosen.ico"
        icon.touch()
        self.write(".ui/preferences.yaml", {"ui": {"icon_path": str(icon)}})
        self.assertEqual(build.selected_icon(self.root), icon)

    def test_cli_and_gui_report_invalid_config_without_traceback(self):
        path = self.root / "invalid.yaml"
        path.write_text("app: [", encoding="utf-8")
        with patch("msgx.main.sys.stderr") as stderr:
            self.assertEqual(main(["--config", str(path), "run"]), 2)
            self.assertIn("配置读取失败", str(stderr.write.call_args_list))
        from msgx import gui
        root = Mock()
        with patch.object(gui.tk, "Tk", return_value=root), \
                patch.object(gui.messagebox, "showerror") as error:
            self.assertEqual(gui.main(str(path)), 2)
        error.assert_called_once()
        root.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
