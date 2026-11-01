"""可编辑图片评分规则：纯规则、隔离YAML与实际Tk编辑器，不调用服务商。"""
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.image_policy import (compile_keyword_rules, default_keyword_rules, rules_version,
                               screen_image, validate_analysis, validate_keyword_rules)
from msgx import prefs
from msgx.ui.image_rules import ImageRulesEditor


def rule(**values):
    return {"name": "自定义主题", "mode": "literal", "pattern": "独立环境", "score": 40,
            "enabled": True, "max_hits": 2, "repeat_factor": .5, **values}


class ImageRuleTests(unittest.TestCase):
    def test_defaults_are_editable_copies_with_original_report_weights(self):
        first = default_keyword_rules()
        self.assertEqual(len(first), 22)
        self.assertGreater(next(item["score"] for item in first if item["name"] == "Codex"),
                           next(item["score"] for item in first if item["name"] == "训练与梯度"))
        first[0]["score"] = 0
        self.assertNotEqual(first, default_keyword_rules())

    def test_literal_and_regex_modes_and_repeated_points(self):
        self.assertEqual(screen_image("APIv2", "", keyword_rules=[rule(pattern=r"API.v2")]).score, 0)
        self.assertEqual(screen_image("API.v2", "", keyword_rules=[rule(pattern=r"API.v2")]).score, 40)
        rules = [rule(mode="regex", pattern=r"\bAPI\s*\d+\b", score=30, max_hits=3, repeat_factor=.5)]
        self.assertEqual(screen_image("使用API2和api 3 API4 API5", "", keyword_rules=rules).score, 60)
        spaced = [rule(pattern="API ")]
        self.assertEqual(validate_keyword_rules(spaced)[0]["pattern"], "API ")
        self.assertEqual(screen_image("API", "", keyword_rules=spaced).score, 0)
        self.assertEqual(screen_image("API ", "", keyword_rules=spaced).score, 40)

    def test_empty_disabled_zero_and_no_ocr_never_score(self):
        for rules in ([], [rule(enabled=False)], [rule(score=0)]):
            self.assertEqual(screen_image("独立环境", "", keyword_rules=rules).score, 0)
        self.assertEqual(screen_image("", "独立环境", keyword_rules=[rule()]).score, 0)
        self.assertEqual(validate_analysis({"threshold": 50, "daily_limit": 200, "daily_budget_cny": 10,
                                           "keyword_rules": []})["keyword_rules"], [])

    def test_invalid_rules_are_rejected_without_mutation(self):
        for values in ([rule(mode="regex", pattern="[")], [rule(score=101)], [rule(score="1.5")],
                       [rule(max_hits=0)], [rule(repeat_factor=2)], [rule(mode="regex", pattern=".*")],
                       [rule(pattern="")], [rule(enabled="yes")], [rule(extra="unknown")]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_keyword_rules(values)
        original = rule(score="23")
        self.assertEqual(validate_keyword_rules([original])[0]["score"], 23)
        self.assertEqual(original["score"], "23")

    def test_compilation_cache_and_stable_signed_sqlite_revision(self):
        rules = [rule()]
        self.assertIs(compile_keyword_rules(rules), compile_keyword_rules([dict(rules[0])]))
        version = rules_version(rules)
        self.assertEqual(version, rules_version([dict(reversed(list(rules[0].items())))]))
        self.assertTrue(0 < version < 2**63)
        self.assertNotEqual(version, rules_version([rule(score=41)]))

    def test_yaml_roundtrip_preserves_personal_limits_and_explicit_empty(self):
        with tempfile.TemporaryDirectory(prefix="msgx-rule-prefs-") as folder:
            path = Path(folder) / "preferences.yaml"
            values = {"image_analysis": {"enabled": True, "threshold": 50, "daily_limit": 200,
                "daily_budget_cny": 10, "history_days": 7, "keyword_rules": [rule()]}, "image_api_profile": "cheap"}
            prefs.save(values, path)
            restored = prefs.load(path)
            self.assertEqual(validate_analysis(restored["image_analysis"])["daily_limit"], 200)
            self.assertEqual(restored["image_analysis"]["keyword_rules"], [rule()])
            values["image_analysis"]["keyword_rules"] = []
            prefs.save(values, path)
            self.assertEqual(prefs.load(path)["image_analysis"]["keyword_rules"], [])


class ImageRuleEditorTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        ttk.Style(self.root).theme_use("clam")
        self.saved = Mock(return_value=True)
        self.editor = ImageRulesEditor(self.root, [rule()], self.saved)
        self.editor.pack(fill="both", expand=True)

    def tearDown(self):
        self.root.destroy()

    def test_edit_score_and_pattern_save_current_form_without_extra_apply(self):
        self.editor.table.selection_set("0")
        self.editor._selected()
        self.editor.fields["score"].set("55")
        self.editor.fields["pattern"].set("可编辑")
        self.assertTrue(self.editor.save())
        self.assertEqual(self.saved.call_args.args[0][0]["score"], 55)
        self.assertEqual(self.saved.call_args.args[0][0]["pattern"], "可编辑")

    def test_invalid_regex_does_not_call_save_and_keeps_draft(self):
        self.editor.fields["name"].set("新规则")
        self.editor.fields["mode"].set("正则")
        self.editor.fields["pattern"].set("[")
        self.assertFalse(self.editor.save())
        self.saved.assert_not_called()
        self.assertEqual(self.editor.fields["pattern"].get(), "[")

    def test_delete_all_rules_then_save_keeps_explicit_empty(self):
        self.editor.table.selection_set("0")
        self.editor._selected()
        self.editor.remove()
        self.assertTrue(self.editor.save())
        self.saved.assert_called_once_with([])

    def test_local_preview_does_not_save_or_require_api(self):
        self.editor.sample.insert("1.0", "独立环境 独立环境")
        self.editor.test()
        deadline = time.monotonic() + 3
        while self.editor.testing and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.editor.testing)
        self.assertIn("60/100", self.editor.feedback.get())
        self.saved.assert_not_called()


if __name__ == "__main__":
    unittest.main()
