"""图片主题评分规则编辑与本地试匹配；保存由调用方局部更新个人YAML。"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from ..image_policy import default_keyword_rules, screen_image, validate_keyword_rules


class ImageRulesEditor(ttk.Frame):
    def __init__(self, parent, rules, on_save):
        super().__init__(parent, padding=12)
        self.on_save = on_save
        self.rules = validate_keyword_rules(rules)
        self.editing = None
        self.results = queue.Queue()
        self.testing = False
        ttk.Label(self, text="图片主题与评分规则（保存在本机 YAML）", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        ttk.Label(self, text="初始22项来自近期日报／周报；个人列表保存后优先。普通词按字面匹配，正则可用 | 表示多个候选。\n"
                  "忽略英文大小写；首个命中按分数计，后续命中乘重复倍率，并受次数上限约束；总分最高100。",
                  wraplength=880).pack(anchor="w", pady=(3, 6))
        rows = ttk.Frame(self)
        rows.pack(fill="both", expand=True)
        self.table = ttk.Treeview(rows, columns=("enabled", "name", "mode", "pattern", "score", "hits", "repeat"),
                                  show="headings", selectmode="browse", height=8)
        for key, title, width in (("enabled", "启用", 45), ("name", "主题名称", 125), ("mode", "匹配方式", 70),
                                  ("pattern", "关键词／正则", 280), ("score", "首命中分", 70),
                                  ("hits", "最多次数", 70), ("repeat", "重复倍率", 70)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=40, stretch=key == "pattern")
        scroll = ttk.Scrollbar(rows, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", self._selected)
        form = ttk.Frame(self)
        form.pack(fill="x", pady=7)
        self.enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="启用此规则", variable=self.enabled).grid(row=0, column=0, sticky="w")
        self.fields = {key: tk.StringVar() for key in ("name", "mode", "pattern", "score", "max_hits", "repeat_factor")}
        entries = (("name", "主题名称", 0, 1), ("mode", "匹配方式", 0, 3), ("score", "首命中分", 0, 5),
                   ("pattern", "关键词／正则", 1, 1), ("max_hits", "最多次数", 2, 1), ("repeat_factor", "重复倍率", 2, 3))
        for key, title, row, column in entries:
            ttk.Label(form, text=title).grid(row=row, column=column, sticky="w", padx=(8, 3), pady=2)
            widget = ttk.Combobox(form, textvariable=self.fields[key], values=("普通词", "正则"), state="readonly", width=9) if key == "mode" else ttk.Entry(form, textvariable=self.fields[key], width=12 if key != "name" else 18)
            widget.grid(row=row, column=column+1, sticky="ew", columnspan=5 if key == "pattern" else 1)
        form.columnconfigure(2, weight=1)
        actions = ttk.Frame(self)
        actions.pack(fill="x")
        for title, command in (("新增规则", self.new), ("应用到列表", self.apply_current), ("删除选中", self.remove),
                               ("载入初始22项（待保存）", self.restore_defaults), ("保存规则到本机", self.save)):
            ttk.Button(actions, text=title, command=command).pack(side="left", padx=(0, 5))
        ttk.Label(self, text="本地试匹配：粘贴 OCR；测试不调用 API，也不改已保存规则。只有 OCR 命中才获得主题分数。",
                  wraplength=880).pack(anchor="w", pady=(9, 3))
        self.sample = ScrolledText(self, height=3, wrap="word")
        self.sample.pack(fill="x")
        self.test_button = ttk.Button(self, text="试匹配当前规则", command=self.test)
        self.test_button.pack(anchor="w", pady=4)
        self.feedback = tk.StringVar(value="修改后点击保存；删除全部规则并保存表示暂停主题评分，不会恢复默认。")
        ttk.Label(self, textvariable=self.feedback, wraplength=880).pack(anchor="w", fill="x")
        self.new()
        self._render()
        self.bind("<Destroy>", self._destroyed, add=True)
        self._poll_id = self.after(80, self._poll)

    def _destroyed(self, event):
        if event.widget is self:
            self.after_cancel(self._poll_id)  # 关闭窗口后不再读取Tk控件。

    def _render(self, selected=None):
        self.table.delete(*self.table.get_children())
        for index, rule in enumerate(self.rules):
            self.table.insert("", "end", iid=str(index), values=("✓" if rule["enabled"] else "", rule["name"],
                "普通词" if rule["mode"] == "literal" else "正则", rule["pattern"], rule["score"], rule["max_hits"], rule["repeat_factor"]))
        if selected is not None and self.table.exists(str(selected)):
            self.table.selection_set(str(selected))

    def _selected(self, _event=None):
        selection = self.table.selection()
        if not selection:
            return
        self.editing = int(selection[0])
        rule = self.rules[self.editing]
        self.enabled.set(rule["enabled"])
        for key, variable in self.fields.items():
            variable.set(("普通词" if rule[key] == "literal" else "正则") if key == "mode" else str(rule[key]))

    def new(self):
        self.table.selection_remove(*self.table.selection())
        self.editing = None
        self.enabled.set(True)
        for key, value in (("name", ""), ("mode", "普通词"), ("pattern", ""), ("score", "20"), ("max_hits", "2"), ("repeat_factor", "0.5")):
            self.fields[key].set(value)

    def _current(self):
        values = {key: var.get() for key, var in self.fields.items()}
        values.update(enabled=self.enabled.get(), mode="literal" if values["mode"] == "普通词" else "regex")
        return validate_keyword_rules([values])[0]

    def collect(self):
        rules = [dict(rule) for rule in self.rules]
        if self.fields["name"].get().strip() or self.fields["pattern"].get().strip():
            current = self._current()  # 保存／试匹配包含正在编辑的条目，不要求额外再点一次应用。
            if self.editing is None:
                rules.append(current)
            else:
                rules[self.editing] = current
        return validate_keyword_rules(rules)

    def apply_current(self):
        try:
            rule = self._current()
            position = len(self.rules) if self.editing is None else self.editing
            candidate = [dict(item) for item in self.rules]
            if self.editing is None:
                candidate.append(rule)
            else:
                candidate[position] = rule
            self.rules = validate_keyword_rules(candidate)
        except ValueError as exc:
            self.feedback.set(f"规则有误：{exc}；未应用")
            return False
        self.editing = position
        self._render(position)
        self.feedback.set("规则已应用到当前列表，尚未保存到本机。")
        return True

    def remove(self):
        if self.editing is None:
            self.feedback.set("请先选择要删除的规则。")
            return
        del self.rules[self.editing]
        self.new()
        self._render()
        self.feedback.set("已从当前列表删除；保存后生效。空列表可保存，表示不做主题加分。")

    def restore_defaults(self):
        self.rules = default_keyword_rules()
        self.new()
        self._render()
        self.feedback.set("已载入初始22项到当前列表，尚未覆盖本机设置；点击保存才生效。")

    def save(self):
        try:
            rules = self.collect()
            if self.on_save(rules) is False:
                self.feedback.set("规则未保存；请查看设置页的具体错误，当前输入保留。")
                return False
        except (ValueError, OSError) as exc:
            self.feedback.set(f"规则未保存：{exc}；当前输入保留。")
            return False
        self.rules = rules
        self.new()
        self._render()
        self.feedback.set(f"已保存 {len(rules)} 条图片主题规则到本机 YAML，后续初筛使用新规则。")
        return True

    def test(self):
        if self.testing:
            return
        try:
            rules = self.collect()
        except ValueError as exc:
            self.feedback.set(f"无法试匹配：{exc}")
            return
        sample = self.sample.get("1.0", "end-1c")[:12000]
        self.testing = True
        self.test_button.configure(state="disabled")
        self.feedback.set("正在本地匹配…")

        def work():
            try:
                decision = screen_image(sample, "", keyword_rules=rules)
                self.results.put(f"本地得分：{decision.score}/100；{decision.reason}")
            except Exception as exc:
                self.results.put(f"本地匹配失败：{exc}")
        threading.Thread(target=work, daemon=True).start()  # 工作线程只接收普通数据，不碰Tk变量。

    def _poll(self):
        try:
            message = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.feedback.set(message)
            self.testing = False
            self.test_button.configure(state="normal")
        self._poll_id = self.after(80, self._poll)
