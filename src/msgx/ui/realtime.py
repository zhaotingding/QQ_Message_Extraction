"""实时消息、关键词与相关消息；由 App 组合，控件仅在主线程操作。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import tkinter as tk
import urllib.error
from datetime import datetime
from pathlib import Path
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from .. import prefs as prefs_mod
from .. import security
from ..config import abs_path
from ..summarize import BEIJING
from ..message_keywords import compile_rules, matching_message
from ..store import Store
from ..local_client import request_json
from .common import FONT


class RealtimePage:
    def _build_realtime_tab(self, nb: ttk.Notebook) -> None:
        """把「现在收到了什么」搬进界面。

        起因是 NapCat 那个 CMD 黑框：以前有人习惯盯着协议端窗口看消息到底进没进来。
        既然要把黑框干掉，就得在界面里给一个**更好的**替代品 —— 这里能看到
        入库后的真实内容（含文本和图片处理状态），比协议端滚屏有用得多。
        """
        tab = ttk.Frame(nb, padding=14)
        nb.add(tab, text="实时消息")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=3)
        tab.rowconfigure(4, weight=1)
        self._rt_group_id = ""
        self._rt_group_labels = {"全部群": ""}
        self._rt_rendered = {}  # 已绘制值与标签；未变行不向 Tk 重复提交。

        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        bar.columnconfigure(7, weight=1)
        self.v_rt_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            bar, text="自动刷新（每 3 秒）", variable=self.v_rt_auto,
            command=lambda: self.status.set("已开启自动刷新" if self.v_rt_auto.get() else "已暂停自动刷新，可点击立即刷新")
        ).grid(row=0, column=0)
        ttk.Button(bar, text="立即刷新", width=9, command=self.refresh_messages).grid(row=0, column=1, padx=6)
        self.v_rt_limit = tk.StringVar(value="200")
        ttk.Label(bar, text="显示条数").grid(row=0, column=2, padx=(6, 4))
        ttk.Entry(bar, textvariable=self.v_rt_limit, width=4).grid(row=0, column=3)
        self.v_rt_filter = tk.StringVar(value="全部消息")
        image_filter = ttk.Combobox(bar, textvariable=self.v_rt_filter, state="readonly", width=9,
                                    values=("全部消息", "仅看图片"))
        image_filter.grid(row=0, column=4, padx=(12, 6))
        image_filter.bind("<<ComboboxSelected>>", self._on_rt_filter_changed)
        ttk.Label(bar, text="群筛选").grid(row=0, column=6, padx=(6, 4))
        self.v_rt_group = tk.StringVar(value="全部群")
        self.cb_rt_group = ttk.Combobox(bar, textvariable=self.v_rt_group, state="readonly",
                                        width=22, values=("全部群",))
        self.cb_rt_group.grid(row=0, column=7, sticky="ew")
        self.cb_rt_group.bind("<<ComboboxSelected>>", self._on_rt_group_changed)
        self.v_rt_status = tk.StringVar(value="尚未加载")

        wrap = ttk.Frame(tab)
        wrap.grid(row=1, column=0, sticky="nsew")
        cols = ("time", "group", "who", "text", "state", "image", "recognize", "include", "account")
        self.rt = ttk.Treeview(wrap, columns=cols, show="headings", height=8)
        headings = {
            "time": ("时间", 130, "center"),
            "group": ("群", 130, "w"),
            "who": ("发言人", 110, "w"),
            "text": ("内容", 300, "w"),
            "state": ("图片", 70, "center"),
            "image": ("原图链接", 80, "center"),
            "recognize": ("手动识别", 95, "center"),
            "include": ("纳入摘要", 82, "center"),
            "account": ("接收账号", 125, "w"),
        }
        for key, (title, width, anchor) in headings.items():
            self.rt.heading(key, text=title)
            self.rt.column(key, width=width, minwidth=180 if key == "text" else width,
                           anchor=anchor, stretch=(key == "text"))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.rt.yview)
        xsb = ttk.Scrollbar(tab, orient="horizontal", command=self.rt.xview)
        self.rt.configure(yscrollcommand=sb.set, xscrollcommand=xsb.set)
        self.rt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        xsb.grid(row=2, column=0, sticky="ew")
        self.rt.bind("<ButtonRelease-1>", self._on_rt_click)
        self.rt.bind("<<TreeviewSelect>>", self._on_rt_select)
        # 交替行色便于阅读；与我相关的消息使用红字。
        self.rt.tag_configure("keyword", background="#fff2a8")
        self.rt.tag_configure("odd", background="#f5f7fa")
        self.rt.tag_configure("failed", foreground="#a15c00")
        self.rt.tag_configure("related", foreground="#c62828")

        hint = ttk.Frame(tab)
        hint.grid(row=3, column=0, sticky="ew", pady=(5, 4))
        ttk.Label(hint, text="点击消息查看详情；图片默认仅 OCR，手动识别才调用 API。",
                  style="Hint.TLabel").pack(side="left")
        ttk.Label(hint, textvariable=self.v_rt_status, style="Hint.TLabel").pack(side="right")
        self.rt_bottom = ttk.Notebook(tab)
        self.rt_bottom.grid(row=4, column=0, sticky="nsew")
        self.rt_activity_tab = ttk.Frame(self.rt_bottom)
        self.rt_details_tab = ttk.Frame(self.rt_bottom)
        self.rt_bottom.add(self.rt_activity_tab, text="群活跃度")
        self.rt_bottom.add(self.rt_details_tab, text="消息详情")
        self.rt_related_tab = ttk.Frame(self.rt_bottom)
        self.rt_bottom.add(self.rt_related_tab, text="与我相关")
        self._rt_related_rows = {}
        self.v_rt_related = tk.StringVar(value="我发出的 / 单独 @ 我或引用回复我的；排除 @全体成员，双击查看详情")
        ttk.Label(self.rt_related_tab, textvariable=self.v_rt_related, style="Hint.TLabel").pack(anchor="w")
        related_wrap = ttk.Frame(self.rt_related_tab)
        related_wrap.pack(fill="both", expand=True)
        self.rt_related = ttk.Treeview(related_wrap, columns=("time", "group", "kind", "who", "text"), show="headings", height=5)
        for key, title, width in (("time", "时间", 130), ("group", "群", 160), ("kind", "关系", 85), ("who", "发言人", 110), ("text", "内容", 400)):
            self.rt_related.heading(key, text=title)
            self.rt_related.column(key, width=width, stretch=key == "text")
        related_scroll = ttk.Scrollbar(related_wrap, command=self.rt_related.yview)
        related_xscroll = ttk.Scrollbar(related_wrap, orient="horizontal", command=self.rt_related.xview)
        self.rt_related.configure(yscrollcommand=related_scroll.set, xscrollcommand=related_xscroll.set)
        related_xscroll.pack(side="bottom", fill="x")
        related_scroll.pack(side="right", fill="y")
        self.rt_related.pack(fill="both", expand=True)
        self.rt_related.bind("<<TreeviewSelect>>", self._related_selected)
        self.rt_related.bind("<Double-1>", lambda _e: self.rt_bottom.select(self.rt_details_tab))
        self._rt_keyword_rules = ()
        self._rt_keyword_rows = {}
        self._rt_source_rows = []
        self._rt_keyword_busy = False
        self._rt_keyword_offset = 0
        self.rt_keyword_tab = ttk.Frame(self.rt_bottom)
        self.rt_bottom.add(self.rt_keyword_tab, text="关键词消息")
        keyword_bar = ttk.Frame(self.rt_keyword_tab)
        keyword_bar.pack(fill="x")
        ttk.Label(keyword_bar, text="关键信息").pack(side="left")
        ttk.Button(keyword_bar, text="配置关键词", command=self._configure_message_keywords).pack(side="left", padx=6)
        self.v_rt_keywords = tk.StringVar(value="尚未配置关键词；点击配置关键词添加，支持正则表达式")
        ttk.Label(keyword_bar, textvariable=self.v_rt_keywords, style="Hint.TLabel").pack(side="left", padx=6)
        self.btn_keyword_next = ttk.Button(keyword_bar, text="下一页", command=lambda: self._keyword_page(1), state="disabled")
        self.btn_keyword_next.pack(side="right")
        self.btn_keyword_prev = ttk.Button(keyword_bar, text="上一页", command=lambda: self._keyword_page(-1), state="disabled")
        self.btn_keyword_prev.pack(side="right", padx=6)
        keyword_wrap = ttk.Frame(self.rt_keyword_tab)
        keyword_wrap.pack(fill="both", expand=True)
        self.rt_keywords = ttk.Treeview(keyword_wrap, columns=("time", "group", "who", "rules", "text"), show="headings", height=5)
        for key, title, width in (("time", "时间", 130), ("group", "群", 160), ("who", "发言人", 110),
                                  ("rules", "命中规则", 180), ("text", "内容", 400)):
            self.rt_keywords.heading(key, text=title)
            self.rt_keywords.column(key, width=width, stretch=key == "text")
        keyword_scroll = ttk.Scrollbar(keyword_wrap, command=self.rt_keywords.yview)
        keyword_xscroll = ttk.Scrollbar(keyword_wrap, orient="horizontal", command=self.rt_keywords.xview)
        self.rt_keywords.configure(yscrollcommand=keyword_scroll.set, xscrollcommand=keyword_xscroll.set)
        keyword_xscroll.pack(side="bottom", fill="x")
        keyword_scroll.pack(side="right", fill="y")
        self.rt_keywords.pack(fill="both", expand=True)
        self.rt_keywords.tag_configure("keyword", background="#fff2a8")
        self.rt_keywords.bind("<<TreeviewSelect>>", self._keyword_selected)
        self.rt_keywords.bind("<Double-1>", lambda _e: self.rt_bottom.select(self.rt_details_tab))
        self.v_rt_activity_status = tk.StringVar(value="今日未过滤消息累计 · 点击群可筛选消息")
        ttk.Label(self.rt_activity_tab, textvariable=self.v_rt_activity_status,
                  style="Hint.TLabel").pack(anchor="w", pady=3)
        activity_wrap = ttk.Frame(self.rt_activity_tab)
        activity_wrap.pack(fill="both", expand=True)
        self.rt_activity = ttk.Treeview(activity_wrap, columns=("group", "count", "last"),
                                        show="headings", height=5)
        for key, title, width in (("group", "群 / 群号", 380), ("count", "今日消息数", 110),
                                  ("last", "最近未过滤消息", 180)):
            self.rt_activity.heading(key, text=title)
            self.rt_activity.column(key, width=width, stretch=key == "group",
                                    anchor="w" if key == "group" else "center")
        activity_sb = ttk.Scrollbar(activity_wrap, orient="vertical", command=self.rt_activity.yview)
        self.rt_activity.configure(yscrollcommand=activity_sb.set)
        activity_sb.pack(side="right", fill="y")
        self.rt_activity.pack(side="left", fill="both", expand=True)
        self.rt_activity.bind("<ButtonRelease-1>", self._on_rt_activity_click)
        self.rt_detail = ScrolledText(self.rt_details_tab, height=5, wrap="word", font=(FONT, 10))
        self.rt_detail.pack(fill="both", expand=True)
        self.rt_detail.configure(state="disabled")


    @staticmethod
    def _media_label(row) -> str:
        m = str(row.get("media_state") or "")
        v = str(row.get("vision_state") or "")
        if v == "download_failed":
            return "下载失败"
        if m == "filtered":
            return "已过滤" if row.get("media_path") else "OCR处理中" if row.get("has_media") else "已过滤"
        if m in ("", "none"):
            return "—"
        if m == "pending":
            return "待处理"
        if m == "failed":
            return "下载失败"
        if v == "failed":
            return "图片未识别"
        if v == "rejected":
            return "请求被拒绝"
        if v == "skipped":
            return "仅 OCR" if row.get("ocr_text") else "已跳过"
        return "完成"


    def refresh_messages(self) -> None:
        self._refresh_keyword_messages()
        if self.proc is None:
            self.status.set("服务未运行 —— 启动服务后才能看到实时消息")
            self.v_rt_status.set("服务未运行，请先启动服务")
            return
        if self._rt_busy:
            self.v_rt_status.set("正在刷新消息，请稍候…")
            return
        self._rt_busy = True
        self.v_rt_status.set("正在刷新消息…")
        self._rt_limit = self._read_rt_limit()
        threading.Thread(target=self._fetch_messages,
                         args=(*self._rt_request_key(), self.proc), daemon=True).start()


    def _rt_request_key(self) -> tuple:
        return self._read_rt_limit(), self.v_rt_filter.get() == "仅看图片", self._rt_group_id


    def _on_rt_filter_changed(self, _event=None) -> None:
        self._rt_keyword_offset = 0
        self._render_messages([])
        self.v_rt_status.set("正在读取筛选结果…")
        self._refresh_keyword_messages()
        self.refresh_messages()


    def _on_rt_group_changed(self, _event=None) -> None:
        self._rt_group_id = self._rt_group_labels.get(self.v_rt_group.get(), "")
        self._on_rt_filter_changed()


    def _on_rt_activity_click(self, event=None) -> None:
        selected = self.rt_activity.selection()
        group_id = self.rt_activity.identify_row(event.y) if event else (selected[0] if selected else "")
        if not group_id:
            return
        self._rt_group_id = group_id
        for label, value in self._rt_group_labels.items():
            if value == group_id:
                self.v_rt_group.set(label)
                break
        self._on_rt_filter_changed()


    def _read_rt_limit(self) -> int:
        """在主线程里读控件（Tkinter 变量不能在子线程碰）。"""
        try:
            return max(1, min(int(self.v_rt_limit.get() or 200), 300))
        except ValueError:
            return 200


    def _fetch_messages(self, limit: int, images_only: bool = False, group_id: str = "", proc=None) -> None:
        port = int(self.cfg["onebot"]["http_port"])
        payload = None
        try:
            payload = request_json(port, "/messages/recent", timeout=5,
                                   query={"limit": limit, "images": int(images_only), "group_id": group_id})
        except Exception:  # noqa: BLE001
            payload = None
        self.events.put(("messages", ((limit, images_only, group_id), proc, payload)))


    def _accept_messages(self, key: tuple, proc, payload) -> None:
        self._rt_busy = False
        if proc is not self.proc or key != self._rt_request_key():
            if self.proc is not None:
                self.refresh_messages()
            return
        if payload is not None:
            self._render_activity(payload.get("activity") or {})
            self._render_related(payload.get("related") or [], payload.get("related_account") or "")
        else:
            self.v_rt_related.set("与我相关的消息刷新失败；保留上次结果")
        self._render_messages(payload.get("items") if payload else None)


    def _configure_message_keywords(self) -> None:
        existing = getattr(self, "keyword_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        window = self.keyword_window = tk.Toplevel(self.root)
        window.title("配置关键词消息")
        window.transient(self.root)
        window.geometry("660x430")
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="每行一条：普通文字按关键词匹配；以 re: 开头表示正则表达式。\n"
                  "例如：Codex    或    re:额度|限流    或    re:价格\\s*[:：]\\s*\\d+\n"
                  "匹配正文、已有 OCR 和图片语义，忽略英文大小写；不消耗 Token。",
                  font=(FONT, 10)).pack(anchor="w", pady=(0, 8))
        editor = ScrolledText(frame, wrap="none", font=(FONT, 10), height=9)
        editor.pack(fill="both", expand=True)
        editor.insert("1.0", "\n".join(self.prefs.get("message_keywords") or []))
        feedback = tk.StringVar(value="保存后立即生效；清空列表并保存可关闭匹配。")
        ttk.Label(frame, textvariable=feedback, wraplength=615).pack(anchor="w", pady=8)
        actions = ttk.Frame(frame)
        actions.pack(fill="x")

        def save() -> None:
            try:
                rules = compile_rules(editor.get("1.0", "end-1c").splitlines())
                data = {**self.prefs, "message_keywords": [label for label, _pattern in rules]}
                prefs_mod.save(data, self.prefs_file)
            except (ValueError, OSError) as exc:
                feedback.set(f"保存失败：{exc}；原规则保持不变")
                return
            self.prefs = data
            self._rt_keyword_rules = rules
            self._rt_keyword_offset = 0
            feedback.set(f"保存成功 · {len(rules)} 条规则已生效 · 无需重启服务")
            self.status.set(feedback.get())
            self._render_messages(self._rt_source_rows)
            self._refresh_keyword_messages()

        ttk.Button(actions, text="保存并生效", command=save).pack(side="left")
        ttk.Button(actions, text="关闭", command=window.destroy).pack(side="right")
        window.grab_set()
        editor.focus_set()


    def _keyword_request_key(self) -> tuple:
        return (tuple(label for label, _pattern in self._rt_keyword_rules), self._rt_group_id,
                tuple(prefs_mod.blocked_senders(self.prefs.get("filters") or {})), self._rt_keyword_offset)


    def _keyword_page(self, direction: int) -> None:
        self._rt_keyword_offset = max(0, self._rt_keyword_offset + direction * 200)
        self._refresh_keyword_messages()


    def _refresh_keyword_messages(self) -> None:
        if self._rt_keyword_busy:
            return
        key = self._keyword_request_key()
        if not self._rt_keyword_rules:
            self._accept_keyword_messages(key, [], "")
            return
        self._rt_keyword_busy = True
        self.v_rt_keywords.set("正在检索已保存的关键词消息…")
        self.btn_keyword_prev.configure(state="disabled")
        self.btn_keyword_next.configure(state="disabled")
        db = abs_path(self.cfg, "db_path")
        rules = self._rt_keyword_rules
        filters = {"blocked_senders": list(key[2])}
        events = self.events

        def work() -> None:
            rows, error = [], ""
            try:
                if db.exists():
                    store = Store(db, filters=filters, readonly=True)
                    try:
                        rows = store.recent_messages(201, group_id=key[1], keyword_rules=rules, offset=key[3])
                    finally:
                        store.close()
            except (OSError, sqlite3.Error) as exc:
                error = security.redact(str(exc))
            events.put(("keyword_messages", (key, rows, error)))
        threading.Thread(target=work, daemon=True).start()


    def _accept_keyword_messages(self, key: tuple, rows: list, error: str) -> None:
        self._rt_keyword_busy = False
        if key != self._keyword_request_key():
            self._refresh_keyword_messages()
            return
        if error:
            self.v_rt_keywords.set(f"关键词读取失败：{error}；保留上次结果")
            self.btn_keyword_prev.configure(state="normal" if self._rt_keyword_offset else "disabled")
            return
        selected = self.rt_keywords.selection()
        scroll = self.rt_keywords.yview()[0]
        self._rt_keyword_rows = {str(row["id"]): row for row in rows[:200]}
        self.rt_keywords.delete(*self.rt_keywords.get_children())
        for iid, row in self._rt_keyword_rows.items():
            ocr = " / ".join(image.get("ocr_text") or "" for image in row.get("images") or []).strip(" / ")
            body = " ".join((row.get("text", "") + (" OCR:" + ocr if ocr else "")).split())
            self.rt_keywords.insert("", "end", iid=iid, tags=("keyword",), values=(
                datetime.fromtimestamp(row["ts"], BEIJING).strftime("%m-%d %H:%M:%S"),
                row.get("group_name") or row.get("group_id"), row.get("sender_name", ""),
                " / ".join(matching_message(self._rt_keyword_rules, row)), body[:400]))
        if selected and self.rt_keywords.exists(selected[0]):
            self.rt_keywords.selection_set(selected[0])
            self._keyword_selected()
        elif selected:
            self._show_message_detail(None)
        self.rt_keywords.yview_moveto(scroll)
        self.btn_keyword_prev.configure(state="normal" if self._rt_keyword_offset else "disabled")
        self.btn_keyword_next.configure(state="normal" if len(rows) > 200 else "disabled")
        self.v_rt_keywords.set((f"第 {self._rt_keyword_offset // 200 + 1} 页 · {len(self._rt_keyword_rows)} 条 · 随群筛选，双击查看详情"
                                if self._rt_keyword_rules else "尚未配置关键词；点击配置关键词添加，支持正则表达式"))


    def _keyword_selected(self, _event=None) -> None:
        selected = self.rt_keywords.selection()
        row = self._rt_keyword_rows.get(selected[0]) if selected else None
        if row:
            self.rt.selection_remove(*self.rt.selection())
            self.rt_related.selection_remove(*self.rt_related.selection())
            self._show_collected_message(row)


    def _render_related(self, rows: list, account: str) -> None:
        selected = self.rt_related.selection()
        self._rt_related_rows = {str(row["id"]): row for row in rows}
        self.rt_related.delete(*self.rt_related.get_children())
        for iid, row in self._rt_related_rows.items():
            ocr = " / ".join(image.get("ocr_text") or "" for image in row.get("images", [])).strip(" / ")
            body = " ".join((row.get("text", "") + (" OCR:" + ocr if ocr else "")).split())
            self.rt_related.insert("", "end", iid=iid, values=(
                datetime.fromtimestamp(row["ts"], BEIJING).strftime("%m-%d %H:%M:%S"),
                row.get("group_name") or row.get("group_id"), "我发出的" if row.get("relation") == "self" else "回复我的",
                row.get("sender_name", ""), body[:300]))
        if selected and self.rt_related.exists(selected[0]):
            self.rt_related.selection_set(selected[0])
        self.v_rt_related.set((f"QQ {account} · 最近 {len(rows)} 条 · 随群筛选；排除 @全体成员，双击查看详情"
                               if account else "尚未确定当前 QQ 账号，请先启动并连接服务"))


    def _related_selected(self, _event=None) -> None:
        selected = self.rt_related.selection()
        row = self._rt_related_rows.get(selected[0]) if selected else None
        if row:
            self.rt.selection_remove(*self.rt.selection())
            self.rt_keywords.selection_remove(*self.rt_keywords.selection())
            self._show_collected_message(row)


    def _show_collected_message(self, row: dict) -> None:
        detail = dict(row)
        if row.get("images"):
            detail["ocr_text"] = "\n".join(image.get("ocr_text") or "" for image in row["images"])
            detail["vision_text"] = "\n".join(image.get("vision_text") or "" for image in row["images"])
        self._show_message_detail(detail)


    def _render_activity(self, activity: dict) -> None:
        groups = activity.get("groups") or []
        labels = {"全部群": ""}
        for group in groups:
            group_id = str(group["group_id"])
            labels[f"{group.get('group_name') or group_id} ({group_id})"] = group_id
        if self._rt_group_id and self._rt_group_id not in labels.values():
            labels[self.v_rt_group.get()] = self._rt_group_id
        self._rt_group_labels = labels
        self.cb_rt_group.configure(values=tuple(labels))
        self.v_rt_group.set(next(label for label, value in labels.items() if value == self._rt_group_id))
        selection = self.rt_activity.selection()
        scroll = self.rt_activity.yview()[0]
        self.rt_activity.delete(*self.rt_activity.get_children())
        total = 0
        for group in groups:
            group_id = str(group["group_id"])
            count = int(group["count"])
            total += count
            ts = group.get("last_ts") or 0
            self.rt_activity.insert("", "end", iid=group_id, values=(
                f"{group.get('group_name') or group_id} ({group_id})", count,
                datetime.fromtimestamp(ts, BEIJING).strftime("%H:%M:%S") if ts else "—"))
        if selection and self.rt_activity.exists(selection[0]):
            self.rt_activity.selection_set(selection[0])
        self.rt_activity.yview_moveto(scroll)
        self.v_rt_activity_status.set(
            f"{activity.get('date') or '今日'} · 北京时间当天未过滤消息 {total} 条 · 点击群筛选（不受上方筛选及显示条数限制）")


    def _render_messages(self, rows) -> None:
        if rows is None:
            self.v_rt_status.set("读取失败（服务没跑，或接口不可达）")
            return
        self._rt_source_rows = rows
        # 仅更新变化行；保留选择及滚动锚点，刷新不能打断正在阅读的内容。
        sel = self.rt.selection()
        keep = str(sel[0]) if sel else ""
        previous = list(self.rt.get_children())
        yview, xview = self.rt.yview(), self.rt.xview()
        anchor = previous[min(round(yview[0] * len(previous)), len(previous) - 1)] if previous and yview[0] else ""
        display_rows = []
        for parent in rows:
            parent = {**parent, "keyword_matches": matching_message(self._rt_keyword_rules, parent)}
            images = parent.get("images") or []
            if images:
                for index, image in enumerate(images, 1):
                    item = {**parent, **image, "id": f"{parent['id']}:{image['id']}",
                            "image_id": image["id"], "has_media": 1,
                            "image_index": index, "image_count": len(images),
                            "media_state": "filtered" if parent.get("media_state") == "filtered"
                            else image["media_state"]}
                    display_rows.append(item)
            else:
                display_rows.append(parent)
        self._rt_rows = {str(r.get("id")): r for r in display_rows}
        deleted = [iid for iid in previous if iid not in self._rt_rows]
        if deleted:
            self.rt.delete(*deleted)
        rendered = {}
        for i, r in enumerate(display_rows):
            ts = int(r.get("ts") or 0)
            raw = r.get("text") or ""
            body = prefs_mod.plain_text(raw) or raw.strip() or ("[图片]" if r.get("has_media") else "（无文本）")
            vision = (r.get("vision_text") or "").strip()
            body = " ".join(body.split())[:300]
            ocr = " ".join((r.get("ocr_text") or "").split())
            if r.get("has_media") and ocr:
                # 给 OCR 留出预览空间；完整正文与逐图识别结果仍保留在消息详情。
                caption = " ".join(prefs_mod.plain_text(raw).split())
                if len(caption) > 100:
                    caption = caption[:100] + "…"
                preview = ocr[:180] + ("…" if len(ocr) > 180 else "")
                body = f"{caption} [图片] OCR:{preview}".strip()
            state = self._media_label(r)
            tags: list[str] = []
            if r.get("keyword_matches"):
                tags.append("keyword")
            if i % 2:
                tags.append("odd")
            if state == "图片未识别":
                tags.append("failed")
            if r.get("relation") in ("self", "reply"):
                tags.append("related")
            values = (
                    time.strftime("%m-%d %H:%M:%S", time.localtime(ts)),
                    r.get("group_name") or r.get("group_id") or "",
                    r.get("sender_name") or "",
                    body,
                    state,
                    "打开" if r.get("media_path") else "—",
                    ("☑ 已识别" if vision else "□ 识别") if r.get("media_path") else "—",
                    ("☑" if r.get("include_vision") else "□") if r.get("has_media") else "—",
                    r.get("receiver_accounts") or r.get("received_by") or "历史未知",
                )
            iid = str(r.get("id"))
            state = values, tuple(tags)
            if not self.rt.exists(iid):
                self.rt.insert("", i, iid=iid, values=values, tags=tuple(tags))
            elif self._rt_rendered.get(iid) != state:
                self.rt.item(iid, values=values, tags=tuple(tags))
            rendered[iid] = state
        order = list(self._rt_rows)
        if list(self.rt.get_children()) != order:
            for index, iid in enumerate(order):
                self.rt.move(iid, "", index)
        self._rt_rendered = rendered
        if keep and self.rt.exists(keep):
            self.rt.selection_set(keep)
        self.rt.xview_moveto(xview[0])
        self.rt.yview_moveto(order.index(anchor) / len(order) if order != previous and anchor in self._rt_rows else yview[0])
        self._on_rt_select()
        self.v_rt_status.set(f"共 {len(display_rows)} 行 · 更新于 {time.strftime('%H:%M:%S')}")


    def _on_rt_select(self, _event=None) -> None:
        selection = self.rt.selection()
        row = self._rt_rows.get(str(selection[0])) if selection else None
        if row:
            self.rt_related.selection_remove(*self.rt_related.selection())
            self.rt_keywords.selection_remove(*self.rt_keywords.selection())
        elif self.rt_related.selection() or self.rt_keywords.selection():
            return
        self._show_message_detail(row)


    def _show_message_detail(self, row) -> None:
        detail = ""
        if row:
            detail = f"消息正文：\n{row.get('text') or '（无文本）'}"
            if row.get("has_media"):
                detail += (f"\n\n图片 {row.get('image_index', 1)} / {row.get('image_count', 1)}"
                           f"\nOCR:\n{row.get('ocr_text') or '（无文字）'}\n\n"
                           f"图片语义：\n{row.get('vision_text') or '（尚未手动识别）'}")
        if self.rt_detail.get("1.0", "end-1c") == detail:
            return
        self.rt_detail.configure(state="normal")
        self.rt_detail.delete("1.0", "end")
        self.rt_detail.insert("end", detail)
        self.rt_detail.configure(state="disabled")


    def _on_rt_click(self, event) -> None:
        row_id = self.rt.identify_row(event.y)
        column = self.rt.identify_column(event.x)
        row = self._rt_rows.get(row_id)
        if not row or not column or column == "#0":
            return
        column = self.rt.column(column, "id")
        self.rt_bottom.select(self.rt_details_tab)
        if column == "image" and row.get("media_path"):
            path = Path(row["media_path"])
            if path.is_file():
                self._open_external(path)
            else:
                self.status.set("本地图片已不存在")
        elif column == "recognize" and row.get("image_id") and row.get("media_path") and not row.get("vision_text"):
            self._start_rt_action(str(row["image_id"]), "vision", {})
        elif column == "include" and row.get("image_id"):
            self._start_rt_action(str(row["image_id"]), "summary-include", {"include": not bool(row.get("include_vision"))})


    def _start_rt_action(self, row_id: str, action: str, payload: dict) -> None:
        key = f"{row_id}:{action}"
        if key in self._rt_actions:
            self.status.set("该图片操作正在进行，请稍候…")
            return
        self._rt_actions.add(key)
        self.status.set("正在提交图片识别…" if action == "vision" else "正在更新摘要选项…")

        def work() -> None:
            port = int(self.cfg["onebot"]["http_port"])
            try:
                result = request_json(port, f"/images/{row_id}/{action}", method="POST",
                                      payload=payload, timeout=960 if action == "vision" else 120)
            except urllib.error.HTTPError as exc:
                try:
                    result = json.loads(exc.read().decode("utf-8"))
                except (ValueError, UnicodeError):
                    result = {"ok": False, "error": f"HTTP {exc.code}"}
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": "操作结果未确认，请刷新查看后再决定是否重试：" + security.redact(str(exc))}
            self.events.put(("message_action", (key, action, result)))

        threading.Thread(target=work, daemon=True).start()


    def _tick_realtime(self) -> None:
        """实时消息页的节拍器。

        只在「实时消息」页处于前台且勾了自动刷新时才拉数据 —— 后台页没必要
        每 3 秒打一次接口（用户看不见，纯浪费）。
        """
        try:
            visible = self.nb.index(self.nb.select()) == 1
        except Exception:  # noqa: BLE001  窗口销毁过程中可能取不到
            visible = False
        if visible and self.v_rt_auto.get() and self.proc is not None and not self._rt_busy:
            self.refresh_messages()
        self.root.after(3000, self._tick_realtime)
