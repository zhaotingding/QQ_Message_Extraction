"""消费图的日期、未知费用和界面集成；只用合成数据，不访问账本或网络。"""
from __future__ import annotations

import sys
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from msgx.service_process import ServiceProcess
from msgx.usage_chart import daily_series, day_description, draw_daily_costs


class CanvasRecorder:
    def __init__(self, width=800, height=180):
        self.width, self.height = width, height
        self.items = []

    def winfo_width(self):
        return self.width

    def winfo_height(self):
        return self.height

    def delete(self, _tag):
        self.items.clear()

    def create_text(self, *coords, **options):
        self.items.append(("text", coords, options))

    def create_line(self, *coords, **options):
        self.items.append(("line", coords, options))

    def create_oval(self, *coords, **options):
        self.items.append(("oval", coords, options))


class UsageChartTests(unittest.TestCase):
    def setUp(self):
        self.end = date(2026, 9, 28)

    def test_empty_window_has_zero_days_but_no_fictitious_line(self):
        series = daily_series([], self.end)
        self.assertEqual(len(series["摘要总结"]), 30)
        self.assertEqual(series["摘要总结"][0]["day"], "2026-08-30")
        self.assertTrue(all(row["cost_cny"] == 0 and row["calls"] == 0 for row in series["摘要总结"]))
        canvas = CanvasRecorder()
        draw_daily_costs(canvas, series)
        self.assertTrue(any("暂无" in options.get("text", "") for _, _, options in canvas.items))
        self.assertFalse(any(options.get("tags") == "series" for _, _, options in canvas.items))

    def test_single_known_day_and_purpose_filter(self):
        rows = [dict(day="2026-09-28", purpose="摘要总结", cost_cny=0.02, calls=1),
                dict(day="2026-08-29", purpose="图片理解", cost_cny=8, calls=1),
                dict(day="2026-09-29", purpose="图片理解", cost_cny=9, calls=1),
                dict(day="2026-09-28", purpose="消息评分", cost_cny=7, calls=1)]
        series = daily_series(rows, self.end)
        canvas = CanvasRecorder()
        draw_daily_costs(canvas, series)
        self.assertEqual(sum(row["cost_cny"] for row in series["摘要总结"]), 0.02)
        self.assertEqual(sum(row["cost_cny"] for row in series["图片理解"]), 0)
        self.assertEqual(sum(options.get("tags") == "point" for _, _, options in canvas.items), 1)
        self.assertIn("¥0.020000", day_description(series, 29))

    def test_all_unknown_day_breaks_line_instead_of_zero(self):
        rows = [dict(day="2026-09-27", purpose="摘要总结", cost_cny=None, calls=2, unknown_calls=2)]
        series = daily_series(rows, self.end, days=3)
        self.assertIsNone(series["摘要总结"][1]["cost_cny"])
        self.assertEqual(series["摘要总结"][0]["cost_cny"], 0)
        canvas = CanvasRecorder()
        draw_daily_costs(canvas, series)
        summary_lines = [options for _, _, options in canvas.items
                         if options.get("tags") == "series" and options.get("fill") == "#2563eb"]
        self.assertEqual(summary_lines, [])
        self.assertIn("费用未知（2 次）", day_description(series, 1))

    def test_partial_unknown_keeps_known_amount_and_explicit_notice(self):
        rows = [dict(day="2026-09-28", purpose="图片理解", cost_cny=0.008, calls=3, unknown_calls=1),
                dict(day="2026-09-28", purpose="图片理解", cost_cny=None, calls=1, unknown_calls=1)]
        series = daily_series(rows, self.end)
        point = series["图片理解"][-1]
        self.assertEqual(point["cost_cny"], 0.008)
        self.assertEqual(point["unknown_calls"], 2)
        self.assertEqual(point["calls"], 4)
        self.assertIn("¥0.008000（另有 2 次费用未知）", day_description(series, 29))

    def test_resize_redraws_positions_without_accumulating_items(self):
        series = daily_series([dict(day="2026-09-28", purpose="摘要总结", cost_cny=1, calls=1)], self.end)
        canvas = CanvasRecorder(width=800)
        first_range = draw_daily_costs(canvas, series)
        count = len(canvas.items)
        canvas.width, canvas.height = 300, 150
        second_range = draw_daily_costs(canvas, series)
        self.assertLess(second_range[1], first_range[1])
        self.assertEqual(len(canvas.items), count)
        for kind, coords, _ in canvas.items:
            if kind != "text":
                self.assertTrue(all(0 <= x <= 300 for x in coords[::2]))
                self.assertTrue(all(0 <= y <= 150 for y in coords[1::2]))

    def test_notebook_keeps_details_and_defaults_to_chart(self):
        import tkinter as tk
        from tkinter import ttk
        from msgx.gui import App
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = App.__new__(App)
        app.service = ServiceProcess()
        app.refresh_usage = lambda: None
        app._usage_sort = {}
        app.cfg = {"onebot": {"http_port": 6701}}
        book = ttk.Notebook(root)
        app._build_usage_tab(book)
        self.assertEqual(app.usage_bottom.tab(app.usage_bottom.select(), "text"), "消费趋势")
        self.assertEqual(app.usage_bottom.tab(1, "text"), "关联消息")
        app._render_usage({"daily_costs": [dict(day="2026-09-28", purpose="摘要总结", cost_cny=1, calls=1)]})
        self.assertGreater(len(app.usage_chart.find_all()), 0)
        app._usage_items = {"1": dict(purpose="摘要总结", source_type="多条消息汇总", model="测试模型")}
        app.usage_table.insert("", "end", iid="1")
        app.usage_table.selection_set("1")
        # 只验证选中行为，禁止后台线程执行关联消息请求。
        with patch("threading.Thread") as thread:
            app._usage_selected()
        thread.return_value.start.assert_called_once()
        self.assertEqual(app.usage_bottom.select(), str(app.usage_detail_tab))
        self.assertIn("正在读取关联消息", app.usage_detail.get("1.0", "end"))

    def test_full_window_resize_can_reach_complete_chart_and_details(self):
        import tkinter as tk
        from tkinter import ttk
        from msgx.gui import App, FONT
        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.title("消费布局离线验证")
        root.geometry("1040x940")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TButton", padding=(12, 7), font=(FONT, 10))
        style.configure("TLabel", font=(FONT, 10))
        style.configure("TNotebook.Tab", font=(FONT, 10), padding=(18, 8))
        outer = ttk.Frame(root, padding=18)
        outer.pack(fill="both", expand=True)
        # 保留实际主窗口标题和底部状态栏所占空间。
        ttk.Frame(outer, height=76).pack(fill="x")
        ttk.Frame(outer, height=20).pack(side="bottom", fill="x")
        book = ttk.Notebook(outer)
        book.pack(fill="both", expand=True)
        app = App.__new__(App)
        app.service = ServiceProcess()
        app.refresh_usage = lambda: None
        app._usage_sort = {}
        app._build_usage_tab(book)
        app._render_usage({"openai_utc_days": [
            dict(day="2026-09-28", cost_usd=0.02, unknown_calls=2),
            dict(day="2026-09-27", cost_usd=None, unknown_calls=3),
            dict(day="2026-09-26", cost_usd=0.01)],
            "daily_costs": [dict(day="2026-09-28", purpose="摘要总结", cost_cny=1, calls=2, unknown_calls=1)]})
        root.update()
        self.assertGreaterEqual(app.usage_chart.winfo_height(), 180)
        viewport_bottom = app.usage_viewport.winfo_rooty() + app.usage_viewport.winfo_height()
        label_bottom = app.usage_chart_label.winfo_rooty() + app.usage_chart_label.winfo_height()
        self.assertLessEqual(label_bottom, viewport_bottom)
        root.geometry("820x560")
        root.update()
        self.assertLess(app.usage_viewport.yview()[1], 1)
        app.usage_viewport.yview_moveto(1)
        root.update()
        viewport_bottom = app.usage_viewport.winfo_rooty() + app.usage_viewport.winfo_height()
        self.assertGreaterEqual(app.usage_chart.winfo_rooty(), app.usage_viewport.winfo_rooty())
        self.assertLessEqual(app.usage_chart_label.winfo_rooty() + app.usage_chart_label.winfo_height(), viewport_bottom)
        self.assertLess(app.usage_info_label.cget("wraplength"), app.usage_viewport.winfo_width())
        for item in app.usage_chart.find_all():
            if app.usage_chart.type(item) == "text" and app.usage_chart.itemcget(item, "text") in ("08-30", "09-13", "09-28"):
                self.assertLessEqual(app.usage_chart.bbox(item)[3], app.usage_chart.winfo_height())
        app.usage_bottom.select(app.usage_detail_tab)
        root.update()
        app.usage_viewport.yview_moveto(1)
        root.update()
        self.assertGreaterEqual(app.usage_detail.winfo_rooty(), app.usage_viewport.winfo_rooty())
        self.assertLessEqual(app.usage_detail.winfo_rooty() + app.usage_detail.winfo_height(), viewport_bottom)


if __name__ == "__main__":
    unittest.main()
