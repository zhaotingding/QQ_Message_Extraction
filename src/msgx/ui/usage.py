"""消费明细与图表；由 App 组合，控件仅在主线程操作。"""
from __future__ import annotations

import json
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from .. import security
from ..summarize import BEIJING
from ..usage_chart import daily_series, day_description, draw_daily_costs
from ..local_client import request_json
from .common import FONT


class UsagePage:
    def _build_usage_tab(self, nb: ttk.Notebook) -> None:
        self.usage_tab = ttk.Frame(nb, padding=12)
        nb.add(self.usage_tab, text="消费明细")
        self.usage_viewport = tk.Canvas(self.usage_tab, highlightthickness=0, yscrollincrement=24)
        page_scroll = ttk.Scrollbar(self.usage_tab, command=self.usage_viewport.yview)
        self.usage_viewport.configure(yscrollcommand=page_scroll.set)
        page_scroll.pack(side="right", fill="y")
        self.usage_viewport.pack(side="left", fill="both", expand=True)
        tab = self.usage_content = ttk.Frame(self.usage_viewport)
        self._usage_window = self.usage_viewport.create_window(0, 0, window=tab, anchor="nw")
        self.btn_usage_refresh = ttk.Button(tab, text="刷新消费明细", command=self.refresh_usage)
        self.btn_usage_refresh.pack(anchor="w")
        self.v_usage = tk.StringVar(value="Token 来自 API；金额为本地估算。今日按北京时间，OpenAI 平台按 UTC 日期统计。")
        self.usage_info_label = ttk.Label(tab, textvariable=self.v_usage, wraplength=880)
        self.usage_info_label.pack(fill="x", pady=6)
        self.v_openai_utc = tk.StringVar(value="OpenAI UTC 日用量：启动服务后查看")
        self.usage_utc_label = ttk.Label(tab, textvariable=self.v_openai_utc, wraplength=1040)
        self.usage_utc_label.pack(fill="x", pady=(0, 6))
        self.usage_groups = ttk.Treeview(tab, columns=("purpose", "type", "calls", "input", "output", "cost", "today", "api", "usd"),
                                        show="headings", height=4)
        for key, label, width in (("purpose", "用途", 150), ("type", "消息类型", 120), ("calls", "请求数", 70),
                                  ("input", "输入 Token", 90), ("output", "输出 Token", 90), ("cost", "总金额（元·估算）", 130),
                                  ("today", "今日消费（北京·元）", 145), ("api", "平台 / 模型", 170), ("usd", "美元（估算）", 110)):
            self.usage_groups.heading(key, text=label)
            if key in ("calls", "input", "output", "cost", "today", "usd"):
                self.usage_groups.heading(key, command=lambda k=key: self._sort_usage(self.usage_groups, k))
            self.usage_groups.column(key, width=width, stretch=True)
        self.usage_groups.pack(fill="x", pady=(0, 10))
        group_xscroll = ttk.Scrollbar(tab, orient="horizontal", command=self.usage_groups.xview)
        self.usage_groups.configure(xscrollcommand=group_xscroll.set)
        group_xscroll.pack(fill="x", pady=(0, 6))
        self.usage_hint_label = ttk.Label(tab, text="点击请求数、Token 或金额表头排序；最近 500 次请求，选中一行查看关联消息（上方汇总覆盖全部明细）")
        self.usage_hint_label.pack(fill="x")
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        columns = ("time", "purpose", "source", "input", "output", "cache", "cost", "state", "today", "api", "usd", "tier", "cache_write")
        self.usage_table = ttk.Treeview(frame, columns=columns, show="headings", height=7)
        for key, label, width in zip(columns, ("时间（北京）", "用途", "消息/任务", "输入", "输出", "缓存读命中", "总金额（元·估算）", "结果", "今日消费（北京·元）", "平台 / 模型", "美元（估算）", "服务层级", "缓存写入"),
                                   (125, 90, 100, 65, 65, 90, 125, 115, 145, 160, 105, 160, 80)):
            self.usage_table.heading(key, text=label)
            if key in ("input", "output", "cache", "cost", "today", "usd", "cache_write"):
                self.usage_table.heading(key, command=lambda k=key: self._sort_usage(self.usage_table, k))
            self.usage_table.column(key, width=width, stretch=True)
        scroll = ttk.Scrollbar(frame, command=self.usage_table.yview)
        self.usage_table.configure(yscrollcommand=scroll.set)
        self.usage_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        xscroll = ttk.Scrollbar(tab, orient="horizontal", command=self.usage_table.xview)
        self.usage_table.configure(xscrollcommand=xscroll.set)
        xscroll.pack(fill="x")
        self.usage_table.bind("<<TreeviewSelect>>", self._usage_selected)
        self.usage_bottom = ttk.Notebook(tab)
        self.usage_bottom.pack(fill="both", pady=(8, 0))
        chart_tab = ttk.Frame(self.usage_bottom, padding=4)
        self.usage_detail_tab = ttk.Frame(self.usage_bottom)
        self.usage_bottom.add(chart_tab, text="消费趋势")
        self.usage_bottom.add(self.usage_detail_tab, text="关联消息")
        self.usage_chart = tk.Canvas(chart_tab, height=180, background="white", highlightthickness=0)
        self.usage_chart.pack(fill="both", expand=True)
        self.v_usage_chart = tk.StringVar(value="最近 30 天 · 北京时间 · 鼠标移至曲线查看当日金额")
        self.usage_chart_label = ttk.Label(chart_tab, textvariable=self.v_usage_chart, wraplength=1000)
        self.usage_chart_label.pack(fill="x")
        self._usage_daily_series = daily_series([], datetime.now(BEIJING).date())
        self.usage_chart.bind("<Configure>", self._draw_usage_chart)
        self.usage_chart.bind("<Motion>", self._usage_chart_hover)
        self.usage_chart.bind("<Leave>", lambda _e: self.v_usage_chart.set(self._usage_chart_note()))
        self.usage_detail = ScrolledText(self.usage_detail_tab, height=10, wrap="word", font=(FONT, 10), state="disabled")
        self.usage_detail.pack(fill="both", expand=True)
        self.usage_viewport.bind("<Configure>", self._layout_usage_content)
        tab.bind("<Configure>", self._layout_usage_content)
        for variable in (self.v_usage, self.v_openai_utc, self.v_usage_chart):
            variable.trace_add("write", lambda *_args: self.usage_viewport.after_idle(self._layout_usage_content))
        for widget in (self.usage_viewport, tab, chart_tab, self.usage_chart,
                       self.usage_info_label, self.usage_utc_label, self.usage_hint_label, self.usage_chart_label):
            widget.bind("<MouseWheel>", lambda event: self.usage_viewport.yview_scroll(int(-event.delta / 120), "units"))
        nb.bind("<<NotebookTabChanged>>", lambda _e: self.refresh_usage() if nb.select() == str(self.usage_tab) else None, add="+")


    def _layout_usage_content(self, _event=None) -> None:
        width = max(1, self.usage_viewport.winfo_width())
        for label in (self.usage_info_label, self.usage_utc_label, self.usage_hint_label, self.usage_chart_label):
            label.configure(wraplength=max(240, width - 16))
        self.usage_viewport.itemconfigure(self._usage_window, width=width)
        # 换行后的自然高度需等 Tk 完成布局后读取，避免保留窄宽度时的虚高。
        self.usage_viewport.after_idle(self._fit_usage_content)


    def _fit_usage_content(self) -> None:
        width = max(1, self.usage_viewport.winfo_width())
        height = max(self.usage_viewport.winfo_height(), self.usage_content.winfo_reqheight())
        self.usage_viewport.itemconfigure(self._usage_window, width=width, height=height)
        self.usage_viewport.configure(scrollregion=(0, 0, width, height))


    def refresh_usage(self) -> None:
        if self._usage_busy:
            self.v_usage.set("正在读取消费明细，请稍候…")
            return
        if self.proc is None:
            self.v_usage.set("请启动服务后刷新；旧版每日合计无法还原到具体消息。")
            return
        self._usage_busy = True
        self.btn_usage_refresh.configure(state="disabled")
        self.v_usage.set("正在读取消费明细…")
        port = int(self.cfg["onebot"]["http_port"])
        def work() -> None:
            try:
                self.events.put(("usage", request_json(port, "/usage/details", timeout=10)))
            except Exception as exc:
                self.events.put(("usage", {"error": str(exc)}))
        threading.Thread(target=work, daemon=True).start()


    def _render_usage(self, data: dict) -> None:
        self._usage_busy = False
        self.btn_usage_refresh.configure(state="normal")
        if data.get("error"):
            self.v_usage.set(f"消费明细读取失败：{security.redact(str(data['error']))}")
            return
        self.v_usage.set(f"已刷新 {time.strftime('%H:%M:%S')} · 当前用途总金额 ¥{data.get('visible_cost_cny', 0):.6f}；今日消费 ¥{data.get('today_cost_cny', 0):.6f}。"
                         "仅显示摘要总结、图片理解；金额为本地估算，今日按北京时间。"
                         "旧记录缺缓存写入、服务层级或美元信息，无法精确对账；未知费用另行标注。")
        utc_days = data.get("openai_utc_days", [])
        utc_labels = []
        for day in utc_days[:3]:
            amount = f"${day['cost_usd']:.6f}" if day.get("cost_usd") is not None else "美元未知"
            unknown = max(int(day.get("unknown_calls") or 0), int(day.get("unknown_usd_calls") or 0))
            utc_labels.append(f"{day['day']}：{amount}" + (f" + {unknown} 次未知" if unknown else ""))
        self.v_openai_utc.set("OpenAI UTC 日用量（本地估算）：" + ("；".join(utc_labels) if utc_labels else "暂无记录")
                             + "。UTC 00:00 = 北京 08:00；平台账单为最终依据，可能包含其他程序调用。")
        self.usage_groups.delete(*self.usage_groups.get_children())
        self._usage_numbers = {self.usage_groups: {}, self.usage_table: {}}
        for row in data.get("groups", []):
            if row["purpose"] not in ("摘要总结", "图片理解"):
                continue
            iid = self.usage_groups.insert("", "end", values=(row["purpose"], row["source_type"], row["calls"],
                row["in_tokens"] or 0, row["out_tokens"] or 0,
                f"{row['cost_cny']:.6f}" + (f" + {row['unknown_calls']} 次未知" if row["unknown_calls"] else ""),
                f"{row.get('today_cost_cny', 0):.6f}" + (f" + {row['today_unknown_calls']} 次未知" if row.get("today_unknown_calls") else ""),
                f"{row.get('provider') or '历史未记录'} / {row.get('model') or '未知'}",
                (f"{row['cost_usd']:.6f}" if row.get("cost_usd") is not None else "未知")
                + (f" + {row['unknown_usd_calls']} 次未知" if row.get("unknown_usd_calls") else "")))
            self._usage_numbers[self.usage_groups][iid] = dict(calls=row["calls"], input=row["in_tokens"],
                                                              output=row["out_tokens"], cost=row["cost_cny"], today=row.get("today_cost_cny", 0), usd=row.get("cost_usd"))
        self.usage_table.delete(*self.usage_table.get_children())
        self._usage_items = {str(r["id"]): r for r in data.get("items", []) if r["purpose"] in ("摘要总结", "图片理解")}
        for iid, row in self._usage_items.items():
            self.usage_table.insert("", "end", iid=iid, values=(datetime.fromtimestamp(row["ts"], BEIJING).strftime("%m-%d %H:%M:%S"),
                row["purpose"], row["source_label"], *[row[k] if row[k] is not None else "未知" for k in ("in_tokens", "out_tokens", "cache_hit_tokens")],
                f"{row['cost_cny']:.6f}" if row["cost_cny"] is not None else "未知", row["status"],
                f"{row.get('today_cost_cny', 0):.6f}" if row.get("today_cost_cny", 0) is not None else "未知",
                f"{row.get('provider') or '历史未记录'} / {row.get('model') or '未知'}",
                f"{row['cost_usd']:.6f}" if row.get("cost_usd") is not None else "未知", self._usage_tier_label(row),
                row["cache_write_tokens"] if row.get("cache_write_tokens") is not None else "未知"))
            self._usage_numbers[self.usage_table][iid] = dict(input=row["in_tokens"], output=row["out_tokens"],
                                                             cache=row["cache_hit_tokens"], cost=row["cost_cny"], today=row.get("today_cost_cny", 0), usd=row.get("cost_usd"),
                                                             cache_write=row.get("cache_write_tokens"))
        for tree, (key, descending) in self._usage_sort.items():
            self._sort_usage(tree, key, descending)
        self._usage_daily_series = daily_series(data.get("daily_costs", []), datetime.now(BEIJING).date())
        self.v_usage_chart.set(self._usage_chart_note())
        self._draw_usage_chart()


    def _usage_chart_note(self) -> str:
        unknown = sum(row["unknown_calls"] for values in self._usage_daily_series.values() for row in values)
        return ("最近 30 天 · 北京时间 · 鼠标移至曲线查看当日金额"
                + (f"；{unknown} 次费用未知：未计入金额，全部未知的日期断线（橙色叉号）" if unknown else ""))


    def _draw_usage_chart(self, _event=None) -> None:
        self._usage_chart_range = draw_daily_costs(self.usage_chart, self._usage_daily_series)


    def _usage_chart_hover(self, event) -> None:
        left, right = self._usage_chart_range
        count = len(next(iter(self._usage_daily_series.values())))
        index = round(max(0, min(1, (event.x - left) / max(right - left, 1))) * (count - 1))
        self.v_usage_chart.set(day_description(self._usage_daily_series, index))


    def _sort_usage(self, tree, key: str, descending: bool | None = None) -> None:
        if descending is None:
            previous = self._usage_sort.get(tree)
            descending = not previous[1] if previous and previous[0] == key else True
        self._usage_sort[tree] = (key, descending)
        values = self._usage_numbers.get(tree, {})
        known = [iid for iid in tree.get_children() if values[iid][key] is not None]
        unknown = [iid for iid in tree.get_children() if values[iid][key] is None]
        for index, iid in enumerate(sorted(known, key=lambda item: values[item][key], reverse=descending) + unknown):
            tree.move(iid, "", index)
        for column in tree["columns"]:
            label = tree.heading(column, "text").removesuffix(" ▼").removesuffix(" ▲")
            tree.heading(column, text=label + ((" ▼" if descending else " ▲") if column == key else ""))


    @staticmethod
    def _usage_tier_label(row: dict) -> str:
        pricing = json.loads(row.get("pricing") or "{}")
        tier = row.get("service_tier") or pricing.get("requested_service_tier") or ""
        multiplier = {"default": 1, "flex": .5, "fast": 2, "priority": 2}.get(tier)
        if pricing.get("base_url", "").rstrip("/") == "https://api.openai.com/v1" and multiplier is not None:
            return f"{tier} ×{multiplier:g}" + ("（按请求估算）" if not row.get("service_tier") else "")
        return tier or "未记录"


    def _usage_selected(self, _event=None) -> None:
        selection = self.usage_table.selection()
        if not selection:
            return
        iid = selection[0]
        row = self._usage_items[iid]
        self.usage_bottom.select(self.usage_detail_tab)
        self.usage_detail.configure(state="normal")
        self.usage_detail.delete("1.0", "end")
        self.usage_detail.insert("end", f"{row['purpose']} · {row['source_type']} · {row.get('provider') or '历史平台未记录'} · 模型 {row['model']}\n正在读取关联消息…")
        pricing = json.loads(row.get("pricing") or "{}")
        if pricing:
            self.usage_detail.insert("1.end", f" · {self._usage_tier_label(row)} · 基准单价/百万Token："
                                     f"输入 {pricing.get('input_price', '未知')} / 缓存 {pricing.get('cached_price', '未知')} / 输出 {pricing.get('output_price', '未知')} {pricing.get('currency', '')}；金额已按档位折算")
        self.usage_detail.configure(state="disabled")
        port = int(self.cfg["onebot"]["http_port"])
        def work() -> None:
            try:
                data = request_json(port, f"/usage/calls/{iid}", timeout=10)
                text = f"共关联 {data['total']} 条消息，最多显示前 100 条；已清理的原消息无法展示。\n"
                text += "\n\n".join(f"群 {r['group_id']} · {r['sender_name']} · 消息 {r['msg_id']}（#{r['id']}）\n{r['text']}" for r in data["items"])
            except Exception as exc:
                text = f"关联消息读取失败：{exc}"
            self.events.put(("usage_sources", (iid, text)))
        threading.Thread(target=work, daemon=True).start()
