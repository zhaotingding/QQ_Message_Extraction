"""单群日活跃度折线；仅绘制已入库统计，不发起网络或模型请求。"""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk
from .theme import palette


class ActivityChart(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.series = {}  # 保存当前数据，缩放时仅重画，不重新查库。
        self.canvas = tk.Canvas(self, height=150, background="white", highlightthickness=0)
        self.note = ttk.Label(self, text="", wraplength=360, foreground="#777777")
        self.note.pack(side="bottom", fill="x")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._draw)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Leave>", lambda _event: self.canvas.delete("hover"))
        self.canvas._msgx_redraw = self._draw  # 切换配色时重绘一次，不新增定时器或查询。

    def set_series(self, series: dict) -> None:
        if series == self.series:
            return  # 三秒刷新数据未变时保留画布，避免无效重绘。
        self.series = dict(series)
        self.note.configure(text=self.series.get("note") or "")
        self._draw()

    def _draw(self, _event=None) -> None:
        canvas = self.canvas
        colors = palette(canvas)
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 180), max(canvas.winfo_height(), 130)
        dates, counts = self.series.get("dates", []), self.series.get("counts", [])
        if not self.series.get("group_id") or not dates or len(counts) != len(dates):
            canvas.create_text(width / 2, height / 2, text="选择统计群，查看每日有效消息趋势", fill=colors["muted"])
            self._points = []
            return
        name = self.series.get("group_name") or self.series["group_id"]
        canvas.create_text(46, 12, anchor="w", text=f"{name} · 最近 {len(dates)} 天", fill=colors["fg"])
        left, top, right, bottom = 46, 31, width - 14, height - 27
        peak = max([int(value) for value in counts] + [1])
        ceiling = max(1, math.ceil(peak / 4) * 4)
        for step in range(5):
            y = bottom - step * (bottom - top) / 4
            canvas.create_line(left, y, right, y, fill=colors["grid"])
            canvas.create_text(left - 5, y, anchor="e", text=str(round(ceiling * step / 4)), fill=colors["muted"])
        self._points = [(left + index * (right - left) / max(1, len(counts) - 1),
                         bottom - int(value) * (bottom - top) / ceiling)
                        for index, value in enumerate(counts)]
        if len(self._points) > 1:
            canvas.create_line(*[coordinate for point in self._points for coordinate in point], fill=colors["accent"], width=2)
        for index, (x, y) in enumerate(self._points):
            canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=colors["accent"], outline="")
            if index in (0, len(dates) - 1) or index % max(1, math.ceil(len(dates) / 5)) == 0:
                canvas.create_text(x, bottom + 13, text=dates[index][5:], fill=colors["muted"])

    def _hover(self, event) -> None:
        self.canvas.delete("hover")
        points = getattr(self, "_points", [])
        if not points:
            return
        index = min(range(len(points)), key=lambda i: abs(points[i][0] - event.x))
        label = f"{self.series['dates'][index]}：{self.series['counts'][index]} 条"
        self.canvas.create_text(48, 23, text=label, anchor="w", fill=palette(self.canvas)["accent"], tags="hover")
