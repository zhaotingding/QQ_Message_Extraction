"""深浅主题与本地保存回归；真实Tk，临时配置，不连接服务或读取密钥。"""
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx import prefs
from msgx.ui.activity_chart import ActivityChart
from msgx.ui.check_style import install_check_style
from msgx.ui.theme import PALETTES, ThemeManager, build_theme_controls, palette, theme_name
from msgx.usage_chart import daily_series, draw_daily_costs
from datetime import date


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        ttk.Style(self.root).theme_use("clam")
        self.manager = ThemeManager(self.root)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.close)

    def close(self):
        for token in self.root.tk.call("after", "info"):
            self.root.after_cancel(token)
        self.root.destroy()

    def test_default_and_saved_theme_do_not_depend_on_source_default(self):
        self.assertEqual(theme_name({}), "dark")
        self.assertEqual(theme_name({"ui": {"theme": "light"}}), "light")
        self.assertEqual(theme_name({"ui": {"theme": "dark", "last_tab": 2}}), "dark")
        self.assertEqual(theme_name({"ui": {"theme": "obsolete"}}), "dark")

    def test_widget_and_annotation_colors_follow_theme_without_losing_emphasis(self):
        text = tk.Text(self.root)
        text.tag_configure("heading", foreground="#164b78")
        text.tag_configure("review_blue", background="#cfe8ff")
        text.tag_configure("review_red", background="#ffd5d5")
        text.tag_configure("card0", background="#f2f6fa")
        box = tk.Listbox(self.root)
        tree = ttk.Treeview(self.root)
        tree.tag_configure("related", foreground="#c62828")
        tree.tag_configure("keyword", background="#fff2a8")
        tree.tag_configure("image_api", background="#dff3e5")
        for name in ("dark", "light"):
            self.manager.apply(name)
            p = PALETTES[name]
            self.assertEqual(text.cget("background"), p["field"])
            self.assertEqual(text.cget("foreground"), p["fg"])
            self.assertEqual(text.tag_cget("heading", "foreground"), p["accent"])
            self.assertEqual(text.tag_cget("review_blue", "background"), p["review_blue"])
            self.assertEqual(text.tag_cget("review_red", "background"), p["review_red"])
            self.assertEqual(box.cget("selectbackground"), p["selected"])
            self.assertEqual(str(tree.tag_configure("image_api", "background")), p["image_api"])
            self.assertEqual(str(tree.tag_configure("related", "foreground")), p["related"])
            self.assertEqual(str(tree.tag_configure("keyword", "background")), p["keyword"])

    def test_check_indicator_layout_and_keyboard_operation_survive_switch(self):
        style = ttk.Style(self.root)
        install_check_style(self.root, style)
        before = style.layout("TCheckbutton")
        value = tk.BooleanVar(self.root, False)
        check = ttk.Checkbutton(self.root, variable=value)
        for name in ("dark", "light", "dark"):
            self.manager.apply(name)
            self.assertEqual(style.layout("TCheckbutton"), before)
            check.invoke()
        self.assertTrue(value.get())

    def test_charts_redraw_from_cached_data_with_selected_palette(self):
        chart = ActivityChart(self.root)
        chart.set_series(dict(group_id="g", group_name="合成群", dates=["2026-10-30", "2026-10-31"], counts=[1, 3]))
        cost = tk.Canvas(self.root)
        draw_daily_costs(cost, daily_series([dict(day="2026-10-31", purpose="摘要总结", cost_cny=.1, calls=1)], date(2026, 10, 31)))
        for name in ("dark", "light"):
            self.manager.apply(name)
            p = PALETTES[name]
            self.assertEqual(chart.canvas.cget("background"), p["field"])
            self.assertEqual(cost.cget("background"), p["field"])
            self.assertIn(p["accent"], [chart.canvas.itemcget(item, "fill") for item in chart.canvas.find_all()])
            self.assertIn(p["muted"], [cost.itemcget(item, "fill") for item in cost.find_all()])

    def test_new_toplevel_inherits_palette_without_polling(self):
        self.manager.apply("dark")
        popup = tk.Toplevel(self.root)
        text = tk.Text(popup, background="white")
        text.pack()
        popup.update()
        self.assertEqual(text.cget("background"), PALETTES["dark"]["field"])
        popup.destroy()

    def test_theme_save_preserves_latest_other_settings_and_reopens_selection(self):
        path = Path(self.temp.name) / "preferences.yaml"
        prefs.save({"ui": {"last_tab": 3}, "image_analysis": {"threshold": 50, "daily_limit": 200},
                    "image_api_profile": "cheap", "summary_prompt": "个人提示词"}, path)
        page = SimpleNamespace(root=self.root, prefs_file=path, prefs={"ui": {}}, theme=self.manager,
                               _settings_feedback=Mock())
        controls = build_theme_controls(page, self.root)
        controls.choice.current(1)
        controls.changed()
        saved = prefs.load_raw(path)
        self.assertEqual(saved["ui"], {"last_tab": 3, "theme": "light"})
        self.assertEqual(saved["image_analysis"]["threshold"], 50)
        self.assertEqual(saved["image_analysis"]["daily_limit"], 200)
        self.assertEqual(saved["image_api_profile"], "cheap")
        self.assertEqual(saved["summary_prompt"], "个人提示词")
        self.assertEqual(self.manager.name, "light")
        self.assertEqual(theme_name(prefs.load(path)), "light")

    def test_broken_yaml_does_not_get_replaced_by_theme_save(self):
        path = Path(self.temp.name) / "preferences.yaml"
        path.write_text("ui: [broken", encoding="utf-8")
        page = SimpleNamespace(root=self.root, prefs_file=path, prefs={"ui": {"theme": "dark"}},
                               theme=self.manager, _settings_feedback=Mock())
        self.manager.apply("dark")
        controls = build_theme_controls(page, self.root)
        controls.choice.current(1)
        controls.changed()
        self.assertEqual(path.read_text(encoding="utf-8"), "ui: [broken")
        self.assertEqual(self.manager.name, "dark")
        self.assertEqual(controls.choice.current(), 0)
        self.assertIn("失败", page._settings_feedback.call_args.args[0])

    def test_scrolled_text_uses_one_themed_scrollbar_and_keeps_bidirectional_scroll(self):
        text = ScrolledText(self.root, height=4, width=30)
        text.pack(fill="both", expand=True)
        text.insert("end", "".join(f"合成内容第{index}行\n" for index in range(100)))
        original = text.vbar
        self.root.geometry("360x180")
        self.root.deiconify()
        self.root.update()
        self.manager.apply("dark")
        bar = text.vbar
        self.assertIsInstance(bar, ttk.Scrollbar)
        self.assertIsNot(bar, original)
        self.assertFalse(original.winfo_exists())
        self.root.tk.call(bar.cget("command"), "moveto", 1.0)
        self.root.update()
        self.assertGreater(text.yview()[0], .8)
        text.yview_moveto(0)
        self.root.update()
        self.assertEqual(bar.get()[0], 0)
        for name in ("light", "dark"):
            self.manager.apply(name)
            self.assertIs(text.vbar, bar)
            self.assertEqual(str(ttk.Style(self.root).lookup("TCombobox", "arrowcolor", ("readonly",))), PALETTES[name]["fg"])
            self.assertEqual(str(ttk.Style(self.root).lookup("TSpinbox", "arrowcolor")), PALETTES[name]["fg"])


if __name__ == "__main__":
    unittest.main()
