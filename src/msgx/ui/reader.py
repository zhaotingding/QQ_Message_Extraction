"""摘要阅读、标记和心得；由 App 组合，控件仅在主线程操作。"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from .. import security, summary_review
from ..config import abs_path
from ..store import Store
from ..summarize import BEIJING, summary_chars
from .common import FONT, MUTED


class ReaderPage:
    def _build_report_tab(self, nb: ttk.Notebook) -> None:
        self._report_current = None
        self._report_review = None
        self._report_review_updates = {}
        self._report_notes_saved = ""
        self._report_save_busy = False
        self._report_save_action = None
        self._report_review_load_seq = 0
        self._report_query_filter = "全部"
        self._report_choose_first = False
        self.report_tab = ttk.Frame(nb, padding=12)
        nb.add(self.report_tab, text="摘要全文")
        panes = ttk.Panedwindow(self.report_tab, orient="horizontal")
        panes.pack(fill="both", expand=True)
        history = ttk.Frame(panes, width=300)
        history.pack_propagate(False)
        reader = ttk.Frame(panes, padding=(14, 0, 0, 0))
        panes.add(history, weight=1)
        panes.add(reader, weight=3)
        bar = ttk.Frame(history)
        bar.pack(fill="x")
        ttk.Label(bar, text="最近摘要", font=(FONT, 10, "bold")).pack(side="left")
        self.btn_report_refresh = ttk.Button(bar, text="刷新", command=self.refresh_report_history)
        self.btn_report_refresh.pack(side="right")
        self.v_report_filter = tk.StringVar(value="全部")
        report_filter = ttk.Combobox(history, textvariable=self.v_report_filter, state="readonly",
                                    values=("全部", "仅日报", "仅周报", "仅月报"), width=12)
        report_filter.pack(fill="x", pady=(6, 0))
        report_filter.bind("<<ComboboxSelected>>", self._report_filter_changed)
        self.v_report_history = tk.StringVar(value="正在读取本地摘要…")
        ttk.Label(history, textvariable=self.v_report_history, style="Hint.TLabel").pack(anchor="w", pady=(3, 6))
        rows = ttk.Frame(history)
        rows.pack(fill="both", expand=True)
        ttk.Style(history).map("Report.Treeview", background=[], foreground=[("selected", "#253342")])
        self.report_history = ttk.Treeview(rows, columns=("reading",), show="tree headings", selectmode="browse",
                                          style="Report.Treeview")
        self.report_history.heading("#0", text="类型 / 日期 / 标题")
        self.report_history.heading("reading", text="阅读状态")
        self.report_history.column("#0", width=180, minwidth=100, stretch=False)
        self.report_history.column("reading", width=65, minwidth=65, stretch=False, anchor="center")
        self.report_history.bind("<Configure>", lambda event: self.report_history.column(
            "#0", width=max(100, event.width - 69)))
        self.report_history.tag_configure("metadata", foreground=MUTED)
        for state, color in (("unread", "#ffffff"), ("skimmed", "#fff2b2"), ("deep", "#d7f0d4")):
            self.report_history.tag_configure(state, background=color)
        scroll = ttk.Scrollbar(rows, orient="vertical", command=self.report_history.yview)
        horizontal = ttk.Scrollbar(rows, orient="horizontal", command=self.report_history.xview)
        self.report_history.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.report_history.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        rows.rowconfigure(0, weight=1)
        rows.columnconfigure(0, weight=1)
        self.report_history.bind("<<TreeviewSelect>>", self._report_selected)
        self.v_report_meta = tk.StringVar(value="选择左侧摘要即可阅读；查看不调用 API。")
        self.report_meta_label = ttk.Label(reader, textvariable=self.v_report_meta, wraplength=620)
        self.report_meta_label.pack(fill="x")
        self.v_report_path = tk.StringVar(value="生成完成后在这里展示全文及完整文件路径。")
        self.report_path_label = ttk.Label(reader, textvariable=self.v_report_path, style="Hint.TLabel", wraplength=620)
        self.report_path_label.pack(fill="x", pady=(3, 3))
        def wrap_labels(event) -> None:
            for label in (self.report_meta_label, self.report_path_label, self.report_review_label):
                label.configure(wraplength=max(180, event.width - 18))
        reader.bind("<Configure>", wrap_labels)
        actions = ttk.Frame(reader)
        actions.pack(fill="x")
        self.btn_open_report = ttk.Button(actions, text="打开完整 HTML 文件", state="disabled", style="Reader.TButton",
                                         command=lambda: self._open_external(Path(self._summary_path)))
        self.btn_open_report.pack(side="left")
        self.btn_copy_report = ttk.Button(actions, text="复制全文", command=self._copy_report, state="disabled", style="Reader.TButton")
        self.btn_copy_report.pack(side="left", padx=6)
        read_bar = ttk.Frame(reader)
        read_bar.pack(fill="x", pady=(3, 0))
        ttk.Label(read_bar, text="阅读状态：").pack(side="left")
        self.v_report_read_state = tk.StringVar(value="unread")
        self._report_review_buttons = []
        for label, state in (("未读", "unread"), ("粗浅阅读", "skimmed"), ("深入阅读", "deep")):
            button = ttk.Radiobutton(read_bar, text=label, value=state, variable=self.v_report_read_state,
                                     command=self._set_report_read_state, state="disabled")
            button.pack(side="left", padx=(0, 8))
            self._report_review_buttons.append(button)
        marks = ttk.Frame(reader)
        marks.pack(fill="x", pady=(3, 4))
        ttk.Label(marks, text="选中文字：").pack(side="left")
        ttk.Style(reader).configure("Reader.TButton", padding=(5, 3))
        for label, level in (("下划线", "underline"), ("蓝色重点", "blue"), ("红色重点", "red"), ("清除标注", "")):
            button = ttk.Button(marks, text=label, style="Reader.TButton", state="disabled",
                                command=lambda value=level: self._mark_report(value))
            button.pack(side="left", padx=(0, 3))
            self._report_review_buttons.append(button)
        outline = ttk.Frame(history)
        outline.pack(side="bottom", fill="x", pady=(8, 0), before=rows)
        ttk.Label(outline, text="主题目录 · 点击跳转", style="Hint.TLabel").pack(anchor="w")
        outline_rows = ttk.Frame(outline)
        outline_rows.pack(fill="x", pady=(5, 0))
        self.report_outline = tk.Listbox(outline_rows, height=3, font=(FONT, 10), exportselection=False,
                                         relief="flat", background="#f0f5fa", selectbackground="#d6e8fa")
        self.report_outline.grid(row=0, column=0, sticky="ew")
        outline_vertical = ttk.Scrollbar(outline_rows, orient="vertical", command=self.report_outline.yview)
        self.report_outline.configure(yscrollcommand=outline_vertical.set)
        outline_vertical.grid(row=0, column=1, sticky="ns")
        outline_rows.columnconfigure(0, weight=1)
        outline_scroll = ttk.Scrollbar(outline, orient="horizontal", command=self.report_outline.xview)
        self.report_outline.configure(xscrollcommand=outline_scroll.set)
        outline_scroll.pack(fill="x")
        self.report_outline.bind("<<ListboxSelect>>", self._report_jump)
        self.report_text = ScrolledText(reader, wrap="word", font=(FONT, 12), state="disabled", exportselection=False,
                                       background="#ffffff", foreground="#253342", relief="flat",
                                       padx=18, pady=16, spacing1=4, spacing3=8, spacing2=5)
        self.report_text.tag_configure("title", font=(FONT, 18, "bold"), spacing3=20)
        self.report_text.tag_configure("heading", font=(FONT, 13, "bold"), foreground="#164b78",
                                       spacing1=16, spacing3=10)
        self.report_text.tag_configure("bold", font=(FONT, 12, "bold"))
        for i, color in enumerate(("#f2f6fa", "#f7f5ef")):
            self.report_text.tag_configure(f"card{i}", background=color, lmargin1=12, lmargin2=12, rmargin=12)
        self.report_text.tag_configure("review_blue", background="#cfe8ff")
        self.report_text.tag_configure("review_red", background="#ffd5d5")
        self.report_text.tag_configure("review_underline", underline=True)
        self.report_text.pack(fill="both", expand=True)
        self.report_notes_frame = ttk.LabelFrame(reader, text="我的心得（个人观点；切换、刷新及关闭时自动保存）", padding=4)
        ttk.Button(self.report_notes_frame, text="保存心得", style="Reader.TButton",
                   command=self._save_report_notes_clicked).pack(side="right", anchor="n", padx=(6, 0))
        self.report_notes = ScrolledText(self.report_notes_frame, wrap="word", height=2, font=(FONT, 10))
        self.report_notes.pack(side="left", fill="x", expand=True)
        self.v_report_review = tk.StringVar(value="标注与阅读状态保存在本地，不调用 API。")
        self.report_review_label = ttk.Label(reader, textvariable=self.v_report_review, style="Hint.TLabel", wraplength=620)
        self.report_review_label.pack(side="bottom", fill="x", pady=(4, 0), before=self.report_text)
        self.nb.bind("<<NotebookTabChanged>>", self._report_tab_changed, add="+")


    def _report_tab_changed(self, _event=None) -> None:
        target = self.nb.select()
        if target != str(self.report_tab):
            if not self._save_report_notes(on_success=lambda: self.nb.select(target)):
                self.nb.select(self.report_tab)
                return
        if target == str(self.report_tab) and time.time() - self._report_history_checked >= 30:
            self.refresh_report_history()


    def _report_filter_changed(self, _event=None) -> None:
        selected = self.v_report_filter.get()
        def proceed():
            self.v_report_filter.set(selected)
            self._report_filter_changed()
        if not self._save_report_notes(on_success=proceed):
            self.v_report_filter.set(self._report_query_filter)
            return
        self._report_choose_first = True
        self.refresh_report_history()


    def refresh_report_history(self, preferred_path: str = "") -> None:
        if not self._save_report_notes(on_success=lambda: self.refresh_report_history(preferred_path)):
            return
        if self._report_history_busy:
            self.v_report_history.set("正在读取本地摘要，请稍候…")
            if preferred_path:
                self._report_pending_path = preferred_path
            return
        self._report_history_busy = True
        self._report_history_seq += 1
        seq = self._report_history_seq
        self.btn_report_refresh.configure(state="disabled")
        self.v_report_history.set("正在读取本地摘要…")
        db = abs_path(self.cfg, "db_path")
        self._report_query_filter = self.v_report_filter.get()
        kinds = {"仅日报": ("daily",), "仅周报": ("weekly",), "仅月报": ("monthly",)}.get(
            self._report_query_filter, ("daily", "weekly", "monthly"))
        events = self.events

        def work() -> None:
            items, error = [], ""
            try:
                if db.exists():
                    store = Store(db, readonly=True)
                    try:
                        items = store.report_history(kinds)
                    finally:
                        store.close()
            except (OSError, sqlite3.Error, ValueError) as exc:
                error = security.redact(str(exc))
            events.put(("report_history", (seq, items, error, preferred_path)))
        threading.Thread(target=work, daemon=True).start()


    def _render_report_history(self, seq: int, items: list[dict], error: str, preferred_path: str = "") -> None:
        if seq != self._report_history_seq:
            return
        self._report_history_busy = False
        self._report_history_checked = time.time()
        self.btn_report_refresh.configure(state="normal")
        if self._report_query_filter != self.v_report_filter.get():
            self.refresh_report_history(preferred_path or self._report_pending_path)
            return
        if error:
            self.v_report_history.set(f"读取失败：{error}")
            if self._report_pending_path:
                pending, self._report_pending_path = self._report_pending_path, ""
                self.refresh_report_history(pending)
            return
        if not self._save_report_notes(on_success=lambda: self._render_report_history(seq, items, error, preferred_path)):
            return
        selected = self.report_history.selection()
        current = selected[0].removeprefix("detail-") if selected else ""
        shown_path = self._summary_path
        kinds = {"仅日报": ("daily",), "仅周报": ("weekly",), "仅月报": ("monthly",)}.get(
            self.v_report_filter.get(), ("daily", "weekly", "monthly"))
        self._report_items = {str(item["id"]): item for item in items if item.get("kind") in kinds}
        self.report_history.delete(*self.report_history.get_children())
        chosen = ""
        for iid, item in self._report_items.items():
            content = item.get("content") or ""
            title = next((re.sub(r"^#{1,6}\s*|\*\*", "", line).strip()
                          for line in content.splitlines() if line.strip()), "无标题摘要")
            start = datetime.fromtimestamp(item["window_start"], BEIJING)
            end = datetime.fromtimestamp(item["window_end"], BEIJING)
            daily = item["kind"] == "daily"
            kind = {"daily": "日报", "weekly": "周报", "monthly": "月报"}[item["kind"]]
            label_end = end - timedelta(seconds=1)
            dates = (f"{start:%m-%d}" if daily else f"{start:%m-%d}—{label_end:%m-%d}"
                     if start.year == label_end.year else f"{start:%Y-%m-%d}—{label_end:%Y-%m-%d}")
            review = self._report_review_updates.get((item["id"], summary_review.content_hash(content)), item.get("review", {}))
            item["review"] = review
            state = review.get("read_state", "unread")
            label = {"unread": "未读", "skimmed": "粗读", "deep": "深读"}.get(state, "未读")
            self.report_history.insert("", "end", iid=iid, text=f"【{kind}】{dates} · {title}",
                                       values=(label,), tags=(state,), open=True)
            self.report_history.insert(iid, "end", iid=f"detail-{iid}", tags=("metadata",),
                                       text=f"{kind} · {summary_chars(content)} 字 · {start:%Y-%m-%d %H:%M} — {end:%Y-%m-%d %H:%M}")
            if preferred_path and shown_path == str(Path(preferred_path).resolve()) and item.get("report_path") == preferred_path:
                chosen = iid
        chosen = chosen or (current if current in self._report_items else "")
        if not chosen and shown_path:
            chosen = next((iid for iid, item in self._report_items.items()
                           if item.get("report_path") == shown_path), "")
        if not chosen and not self._report_content and self._report_items:
            chosen = next(iter(self._report_items))
        if self._report_choose_first:
            chosen = chosen or next(iter(self._report_items), "")
            self._report_choose_first = False
            if not chosen:
                self._show_report({"content": ""})
        if chosen:
            self.report_history.selection_set(chosen)
            self.report_history.see(chosen)
            self._show_report(self._report_items[chosen])
        count = len(self._report_items)
        self.v_report_history.set((f"最近 {count} 份 · 按内容日期倒序" if count else "暂无此类摘要；旧版摘要已隐藏")
                                  + " · 更新 " + time.strftime("%H:%M:%S"))
        if self._report_pending_path:
            pending, self._report_pending_path = self._report_pending_path, ""
            self.refresh_report_history(pending)


    def _report_selected(self, _event=None) -> None:
        selected = self.report_history.selection()
        iid = selected[0].removeprefix("detail-") if selected else ""
        if iid in self._report_items:
            self._show_report(self._report_items[iid])


    def _show_report(self, item: dict) -> bool:
        path = item.get("report_path") or item.get("path") or ""
        identity = (item.get("id"), path)
        content = item.get("content") or ""
        unchanged = identity == self._report_identity and content == self._report_content
        if not unchanged and not self._save_report_notes(on_success=lambda: self._show_report(item)):
            old_id = str((self._report_current or {}).get("id", ""))
            if self.report_history.exists(old_id) and self.report_history.selection() != (old_id,):
                self.report_history.selection_set(old_id)
            return False
        selected = self.report_history.selection()
        selected_id = selected[0].removeprefix("detail-") if selected else ""
        selected_item = self._report_items.get(selected_id)
        target = str(item.get("id", ""))
        if self.report_history.exists(target) and selected_id != target:
            # 保存后继续切换时同步选择，排队中的 TreeviewSelect 不得跳回旧文章。
            self.report_history.selection_set(target)
        elif selected_item and selected_item.get("id") != item.get("id") and selected_item.get("report_path") != path:
            self.report_history.selection_remove(*selected)
        self._report_identity = identity
        self._report_content = content
        self._summary_path = str(Path(path).resolve()) if path else ""
        self.v_report_path.set(self._summary_path or "未保存 HTML 文件；数据库全文仍可阅读。")
        self.btn_open_report.configure(state="normal" if self._summary_path and Path(self._summary_path).is_file() else "disabled")
        self.btn_copy_report.configure(state="normal" if self._report_content else "disabled")
        dates = ""
        if item.get("window_start") is not None and item.get("window_end") is not None:
            start = datetime.fromtimestamp(item["window_start"], BEIJING)
            end = datetime.fromtimestamp(item["window_end"], BEIJING)
            dates = f"{start:%Y-%m-%d %H:%M} — {end:%Y-%m-%d %H:%M} · "
        self.v_report_meta.set(f"{dates}{summary_chars(self._report_content)} 字 · 完整正文")
        if unchanged:
            if item.get("kind") and self._report_current and not self._report_current.get("kind"):
                self._report_current = item
                self._load_report_review(item)
            return True
        self._report_current = item
        self.report_outline.delete(0, "end")
        self._report_outline = []
        self.report_text.configure(state="normal")
        self.report_text.delete("1.0", "end")
        card_start, card = "", 0
        for line in self._report_content.splitlines():
            main = re.match(r"^#\s+(.+)$", line)
            heading = re.match(r"^#{2,6}\s+(.+)$", line)
            if heading is None:
                heading = re.match(r"^(?:(?:[一二三四五六七八九十百]+、|\d+[.、)]\s*)\s*)?\*\*(.+?)\*\*[:：]?$", line.strip())
            if main:
                self.report_text.insert("end", main.group(1) + "\n", "title")
            elif heading:
                if card_start:
                    self.report_text.tag_add(f"card{card % 2}", card_start, "end-1c")
                    card += 1
                    self.report_text.insert("end", "\n")
                card_start = self.report_text.index("end-1c")
                title = (heading.group(1) if line.startswith("#") else line.strip()).replace("**", "")
                self._report_outline.append(card_start)
                self.report_outline.insert("end", title)
                self.report_text.insert("end", title + "\n", "heading")
            else:
                for part in re.split(r"(\*\*.+?\*\*)", line):
                    self.report_text.insert("end", part[2:-2] if part.startswith("**") else part,
                                            "bold" if part.startswith("**") else "")
                self.report_text.insert("end", "\n")
        if card_start:
            self.report_text.tag_add(f"card{card % 2}", card_start, "end-1c")
        if not self._report_outline:
            self.report_outline.insert("end", "正文未设置主题小标题")
        self.report_text.configure(state="disabled")
        self.report_text.yview_moveto(0)
        self._load_report_review(item)
        return True


    def _load_report_review(self, item: dict) -> None:
        self._report_review_load_seq += 1
        seq = self._report_review_load_seq
        self._report_review = None
        self._report_notes_saved = ""
        self.report_notes.delete("1.0", "end")
        self.report_notes_frame.pack_forget()
        self.v_report_read_state.set("unread")
        for button in self._report_review_buttons:
            button.configure(state="disabled")
        self._draw_report_annotations([])
        if item.get("id") is None:
            self.v_report_review.set("正在等待本地摘要记录；保存记录后可标注。")
            return
        key = item["id"], summary_review.content_hash(self._report_content)
        review = self._report_review_updates.get(key, item.get("review"))
        if review and review.get("content_hash") == key[1]:
            self._apply_report_review(key, review, "", seq)
            return
        self.v_report_review.set("正在读取本地批注…")
        db, content = abs_path(self.cfg, "db_path"), self._report_content
        events = self.events
        def work():
            review, error = None, ""
            try:
                review = summary_review.load_review(db, key[0], content)
            except (OSError, sqlite3.Error, ValueError) as exc:
                error = security.redact(str(exc))
            events.put(("report_review_loaded", (key, review, error, seq)))
        threading.Thread(target=work, daemon=True).start()


    def _draw_report_annotations(self, annotations: list[dict]) -> None:
        for level in ("blue", "red", "underline"):
            self.report_text.tag_remove("review_" + level, "1.0", "end")
        for annotation in annotations:
            start, end = f"1.0 + {annotation['start']} chars", f"1.0 + {annotation['end']} chars"
            # 保存的是渲染后的文字位置；显示规则改变时不把标注贴到其它文字上。
            if self.report_text.get(start, end) == annotation["text"]:
                self.report_text.tag_add("review_" + annotation["level"], start, end)
        for level in ("blue", "red", "underline"):
            self.report_text.tag_raise("review_" + level)
        self.report_text.tag_raise("sel")


    def _save_report_review(self, *, on_success=None, **changes) -> bool:
        """受理后台保存；False 表示尚未完成，绝不冒充已经落库。"""
        if not self._report_current or self._report_review is None:
            return False
        if self._report_save_busy:
            if on_success and self._report_save_action is None:
                self._report_save_action = on_success
            self.v_report_review.set("正在保存本地批注，请稍候；当前编辑仍保留")
            return False
        self._report_save_busy = True
        self._report_save_action = on_success
        key = self._report_current["id"], self._report_review["content_hash"]
        db = abs_path(self.cfg, "db_path")
        events = self.events
        for button in self._report_review_buttons:
            button.configure(state="disabled")
        self.v_report_review.set("正在保存到本地…")
        def work():
            review, error = None, ""
            try:
                review = summary_review.save_review(db, key[0], key[1], **changes)
            except (OSError, sqlite3.Error, ValueError) as exc:
                error = security.redact(str(exc))
            events.put(("report_review_saved", (key, changes, review, error)))
        threading.Thread(target=work, daemon=True).start()
        return False


    def _save_report_notes(self, *, on_success=None) -> bool:
        if self._report_save_busy:
            if on_success and self._report_save_action is None:
                self._report_save_action = on_success
            self.v_report_review.set("正在保存本地编辑，请稍候；保存成功后继续操作")
            return False
        if self._report_review is None or (self._report_current or {}).get("kind") not in ("daily", "weekly", "monthly"):
            return True
        notes = self.report_notes.get("1.0", "end-1c")
        if notes == self._report_notes_saved:
            return True
        return self._save_report_review(notes=notes, on_success=on_success)


    def _save_report_notes_clicked(self) -> None:
        if self._report_review is None:
            self.v_report_review.set("请先选择可保存心得的摘要")
        elif self._save_report_notes():
            self.v_report_review.set("心得已保存 · " + time.strftime("%H:%M:%S"))


    def _set_report_read_state(self) -> None:
        if self._report_review is None:
            return
        self._save_report_review(read_state=self.v_report_read_state.get())


    def _mark_report(self, level: str) -> None:
        if self._report_review is None:
            return
        selection = self.report_text.tag_ranges("sel")
        if not selection:
            self.v_report_review.set("请先在正文中选中需要标注的文字。")
            return
        start, end = selection[0], selection[1]
        remove = ("blue", "red", "underline") if not level else (("blue", "red") if level != "underline" else ("underline",))
        for old_level in remove:
            self.report_text.tag_remove("review_" + old_level, start, end)
        if level:
            self.report_text.tag_add("review_" + level, start, end)
        annotations = []
        for mark_level in ("blue", "red", "underline"):
            ranges = self.report_text.tag_ranges("review_" + mark_level)
            for mark_start, mark_end in zip(ranges[::2], ranges[1::2]):
                annotations.append({
                    # Tk count 在 Windows 将 emoji 计作两个单元，+N chars 却按字符前进。
                    "start": len(self.report_text.get("1.0", mark_start)),
                    "end": len(self.report_text.get("1.0", mark_end)),
                    "text": self.report_text.get(mark_start, mark_end), "level": mark_level,
                })
        self._save_report_review(annotations=annotations)


    def _report_jump(self, _event=None) -> None:
        selected = self.report_outline.curselection()
        if selected and selected[0] < len(self._report_outline):
            self.report_text.yview(self._report_outline[selected[0]])


    def _copy_report(self) -> None:
        if self._report_content:
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(self._report_content)
                self.status.set("已复制完整摘要")
            except tk.TclError:
                self.status.set("复制失败：剪贴板暂时不可用，请重试")


    def _apply_report_review(self, key, review, error, seq):
        if (seq != self._report_review_load_seq or not self._report_current
                or key != (self._report_current["id"], summary_review.content_hash(self._report_content))):
            return
        if error:
            self.v_report_review.set("批注读取失败：" + error)
            return
        self._report_review = review
        self._report_notes_saved = review["notes"]
        self.v_report_read_state.set(review["read_state"])
        self.report_notes.delete("1.0", "end")
        self.report_notes.insert("1.0", review["notes"])
        if self._report_current.get("kind") in ("daily", "weekly", "monthly"):
            self.report_notes_frame.pack(side="bottom", fill="x", pady=(5, 0), before=self.report_text)
        for button in self._report_review_buttons:
            button.configure(state="normal")
        self._draw_report_annotations(review["annotations"])
        self.v_report_review.set("标注与阅读状态保存到本地；打开摘要不会改变阅读状态。")

    def _report_review_saved(self, key, changes, review, error):
        self._report_save_busy = False
        action, self._report_save_action = self._report_save_action, None
        for button in self._report_review_buttons:
            button.configure(state="normal")
        if error:
            # 失败不切文章、不关闭窗口，也不改动心得控件中的草稿。
            self.v_report_read_state.set(self._report_review["read_state"])
            self._draw_report_annotations(self._report_review["annotations"])
            self.v_report_review.set("保存失败，当前编辑已保留：" + error)
            return
        self._report_review = review
        self._report_review_updates[key] = review
        self._report_current["review"] = review
        if "notes" in changes:
            self._report_notes_saved = changes["notes"]
        state = review["read_state"]
        self.v_report_read_state.set(state)
        iid = str(key[0])
        if self.report_history.exists(iid):
            self.report_history.item(iid, tags=(state,), values=({"unread": "未读", "skimmed": "粗读", "deep": "深读"}[state],))
        self._draw_report_annotations(review["annotations"])
        self.v_report_review.set("已保存到本地。" if self._report_current.get("kind") == "monthly" else
                                 "已保存到本地；后续自动汇总将使用来源报告中已保存的重点与心得。")
        if action:
            # 等锁时继续编辑的心得需再保存，之后才执行切换/关闭动作。
            if self._save_report_notes(on_success=action):
                action()
