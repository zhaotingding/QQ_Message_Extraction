"""在独立 Tk 解释器验证勾选图形、状态操作和 DPI 尺寸。"""
from __future__ import annotations

import gc
import sys
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.ui.check_style import _indicator, install_check_style


class CheckStyleTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.style = ttk.Style(self.root)
        self.style.theme_use("clam")

    def tearDown(self):
        self.root.destroy()

    def test_selected_picture_is_a_tick_not_an_x(self):
        image = _indicator(self.root, 14, selected=True)
        self.assertEqual(image.get(5, 9), (255, 255, 255))
        self.assertEqual(image.get(9, 5), (255, 255, 255))
        self.assertEqual(image.get(3, 3), (36, 104, 184))  # 叉左上斜线的位置必须没有白笔画。
        self.assertEqual(image.get(10, 10), (36, 104, 184))

    def test_keeps_clam_layout_and_all_registered_states(self):
        original = self.style.layout("TCheckbutton")
        install_check_style(self.root, self.style)
        rendered = repr(self.style.layout("TCheckbutton"))
        self.assertIn("Msgx.Checkbutton.indicator", rendered)
        self.assertIn("Checkbutton.focus", rendered)
        self.assertIn("Checkbutton.label", rendered)
        self.assertEqual(self.style.theme_use(), "clam")
        self.assertEqual(len(original), len(self.style.layout("TCheckbutton")))
        self.assertEqual(len(self.root._msgx_check_images), 10)
        self.assertNotEqual(self.root._msgx_check_images["selected"].get(5, 9),
                            self.root._msgx_check_images["disabled_selected"].get(5, 9))

    def test_controls_toggle_and_disabled_does_not_toggle(self):
        install_check_style(self.root, self.style)
        value = tk.BooleanVar(self.root, False)
        calls = []
        button = ttk.Checkbutton(self.root, text="自动刷新", variable=value, command=lambda: calls.append(value.get()))
        button.invoke()
        self.assertTrue(value.get())
        self.assertTrue(button.instate(["selected"]))
        button.state(["disabled", "active"])
        button.invoke()
        self.assertEqual(calls, [True])
        self.assertTrue(value.get())
        button.state(["!disabled", "!active"])
        button.invoke()
        self.assertEqual(calls, [True, False])
        gc.collect()
        self.root.update_idletasks()  # 留存引用后，刷新控件不应出现不存在的图像。
        self.assertTrue(all(self.root.tk.call("image", "type", str(image)) == "photo"
                            for image in self.root._msgx_check_images.values()))

    def test_scaled_indicators_and_repeated_install(self):
        image = _indicator(self.root, 28, selected=True)
        self.assertEqual((image.width(), image.height()), (28, 28))
        self.assertEqual(image.get(11, 18), (255, 255, 255))
        install_check_style(self.root, self.style)
        first = self.root._msgx_check_images
        install_check_style(self.root, self.style)
        self.assertIs(first, self.root._msgx_check_images)


if __name__ == "__main__":
    unittest.main()
