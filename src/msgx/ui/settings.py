"""本机设置、凭据选择与群范围；由 App 组合，控件仅在主线程操作。"""
from __future__ import annotations

import sqlite3
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from .. import prefs as prefs_mod
from .. import api_settings, credentials, security, yaml_config
from ..config import abs_path
from ..store import Store
from ..summarize import DEFAULT_DIGEST_PROMPT, DIGEST_DEFAULT_CHARS
from ..message_keywords import compile_rules
from ..local_client import request_json
from .common import FONT


class SettingsPage:
    def _build_settings_tab(self, nb: ttk.Notebook) -> None:
        # 设置项比一屏还高，窗口一矮底部（保存设置）就掉出可见区。套一层 Canvas 做滚动：
        # 任何窗口高度都能滚到底，不必再手动拉窗口。
        holder = ttk.Frame(nb)
        nb.add(holder, text="设置")
        canvas = tk.Canvas(holder, highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        tab = ttk.Frame(canvas, padding=14)
        inner = canvas.create_window((0, 0), window=tab, anchor="nw")

        def _fit(_evt=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(inner, width=canvas.winfo_width())

        tab.bind("<Configure>", _fit)
        canvas.bind("<Configure>", _fit)
        self._scroll_canvas = canvas

        api = ttk.LabelFrame(tab, text="API 配置（密钥独立加密保存）", padding=12)
        api.pack(fill="x", pady=(0, 12))
        selector = ttk.Frame(api)
        selector.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(selector, text="使用配置：").pack(side="left")
        self.api_selector = ttk.Combobox(selector, state="readonly", width=30)
        self.api_selector.pack(side="left", padx=8)
        self.api_selector.bind("<<ComboboxSelected>>", self._select_api_profile)
        ttk.Button(selector, text="新增自定义配置", command=self.add_api_profile).pack(side="left")
        self.v_api = {}
        fields = [("provider", "服务平台"), ("base_url", "API 基础地址"), ("model", "模型（摘要及图片）"),
                  ("protocol", "接口格式"), ("reasoning", "思考强度"),
                  ("service_tier", "OpenAI 服务档位"),
                  ("context_tokens", "上下文 Token 上限"), ("max_output_tokens", "输出 Token 上限"),
                  ("currency", "费率币种"), ("input_price", "输入单价 / 百万 Token"),
                  ("cached_price", "缓存输入单价 / 百万 Token"), ("output_price", "输出单价 / 百万 Token"),
                  ("usd_cny", "美元折人民币（估算）"), ("long_context_threshold", "长输入加价阈值（0 关闭）"),
                  ("long_input_multiplier", "长输入价格倍数"), ("long_output_multiplier", "长输出价格倍数"),
                  ("peak_multiplier", "高峰价格倍数"), ("peak_windows_utc", "工作日高峰时段（UTC）")]
        choices = {"protocol": ("responses", "chat_completions"), "currency": ("USD", "CNY"),
                   "reasoning": ("none", "low", "medium", "high", "xhigh", "max"),
                   "service_tier": ("flex", "default")}
        for row, (key, title) in enumerate(fields, 1):
            ttk.Label(api, text=title).grid(row=row, column=0, sticky="w", pady=2)
            var = self.v_api[key] = tk.StringVar()
            widget = (ttk.Combobox(api, textvariable=var, values=choices[key], state="readonly")
                      if key in choices else ttk.Entry(api, textvariable=var))
            widget.grid(row=row, column=1, sticky="ew", padx=8, pady=2)
            if key == "base_url":
                widget.bind("<FocusOut>", lambda _e: self._api_key_status())
        api.columnconfigure(1, weight=1)
        row = len(fields) + 1
        ttk.Label(api, text="新密钥（留空保留已有）").grid(row=row, column=0, sticky="w")
        self.v_api_key = tk.StringVar()
        ttk.Entry(api, textvariable=self.v_api_key, show="•").grid(row=row, column=1, sticky="ew", padx=8)
        self.v_api_key_status = tk.StringVar(value="未配置")
        ttk.Label(api, textvariable=self.v_api_key_status).grid(row=row+1, column=1, sticky="w", padx=8)
        controls = ttk.Frame(api)
        controls.grid(row=row+2, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Button(controls, text="保存配置 / 替换密钥", command=self.save_prefs).pack(side="left")
        ttk.Button(controls, text="删除此地址的密钥", command=self.delete_api_key).pack(side="left", padx=4)
        ttk.Button(controls, text="导入配置", command=self.import_api_config).pack(side="left")
        ttk.Button(controls, text="导出配置", command=self.export_api_config).pack(side="left", padx=4)
        self.v_api_feedback = tk.StringVar(value="")
        self.api_feedback_label = ttk.Label(api, textvariable=self.v_api_feedback, foreground="#245cc4", wraplength=730)
        self.api_feedback_label.grid(row=row+3, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        self.v_api_key.trace_add("write", lambda *_: self._api_key_status())
        ttk.Label(api, text="选择配置即保存切换；编辑参数后点击保存。新任务使用所选 API，在途摘要仍用原配置。\n"
                  "配置导出不含密钥；不同配置或地址不复用密钥。更换模型请核对容量和费率，汇率为手动估值。\n"
                  "官方 OpenAI：flex 为半价慢速，default 为 Standard；单价填写 Standard 基准价，自动按档位折算。\n"
                  "Flex 最长等待 15 分钟，资源不足会有限重试，不自动切换 Standard；其他平台不发送此参数。",
                  style="Hint.TLabel", wraplength=730).grid(row=row+4, column=0, columnspan=2, sticky="w")

        # --- 摘要 ---
        sm = ttk.LabelFrame(tab, text="自动摘要", padding=12)
        sm.pack(fill="x")
        line = ttk.Frame(sm)
        line.pack(fill="x")
        ttk.Label(line, text="日报目标字数 D").pack(side="left")
        self.v_summary_chars = tk.StringVar()
        ttk.Entry(line, textvariable=self.v_summary_chars, width=8).pack(side="left", padx=6)
        ttk.Button(line, text="恢复默认提示词", command=self.reset_summary_prompt).pack(side="right")
        ttk.Label(
            sm,
            text="只配置日报字数 D；周报自动使用 2D，月报自动使用 3D。\n"
                 "提示词中的 {{目标字数}} 自动替换；保存后下一次自动任务生效，在途任务保持原设置。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(6, 0))
        self.txt_summary_prompt = ScrolledText(
            sm, height=12, wrap="word", font=(FONT, 10), relief="solid", borderwidth=1,
        )
        self.txt_summary_prompt.pack(fill="both", expand=True, pady=(6, 0))

        # --- 群范围 ---
        gp = ttk.LabelFrame(tab, text="统计哪些群", padding=12)
        gp.pack(fill="both", expand=True, pady=(12, 0))
        ttk.Label(
            gp,
            text="不选任何一项 = 关注全部群。按住 Ctrl 可多选；右键或 Ctrl+点击可取消选择。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(0, 6))
        wrap = ttk.Frame(gp)
        wrap.pack(fill="both", expand=True)
        self.lst_groups = tk.Listbox(
            wrap,
            selectmode="multiple",
            height=6,
            font=(FONT, 10),
            activestyle="none",
            relief="solid",
            borderwidth=1,
            exportselection=False,
        )
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.lst_groups.yview)
        self.lst_groups.configure(yscrollcommand=sb.set)
        self.lst_groups.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        row = ttk.Frame(gp)
        row.pack(anchor="w", pady=(8, 0))
        self.btn_groups_refresh = ttk.Button(row, text="刷新群列表", command=self.refresh_groups)
        self.btn_groups_refresh.pack(side="left")
        # 「只保留当前账号的群」（v0.5.4）。群表是**历次登录的并集**：换个账号登进来
        # 一次，它的全部群就永久留在库里（实测误登一次主号灌进来 30 个空群），
        # 下拉框里再也找不到自己关心的那几个。这个按钮把不属于当前账号、又一条消息
        # 都没有的群清掉 —— 有消息的一律保留（删了会让那个群的摘要静默失效）。
        self.btn_groups_prune = ttk.Button(row, text="只保留当前账号的群", command=self.prune_groups)
        self.btn_groups_prune.pack(side="left", padx=8)
        ttk.Label(
            gp,
            text="群列表保留各账号的历史数据；共同群记录多个接收账号。"
                 "「只保留当前账号的群」仅清理空群，有消息的群一律保留。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        # --- 过滤 ---
        ft = ttk.LabelFrame(tab, text="不接收的消息类型", padding=12)
        ft.pack(fill="x", pady=(12, 0))
        ttk.Label(
            ft,
            text="过滤的图片/表情保留简要记录并做本地 OCR，默认不进摘要；其他命中过滤的消息直接丢弃。\n"
                 "所有带链接的图片先做本地 OCR；只有在实时消息页手动勾选才调用图片 API。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(0, 6))
        self.v_filters = {
            "skip_face_only": tk.BooleanVar(),
            "skip_image_only": tk.BooleanVar(),
            "skip_forward": tk.BooleanVar(),
            "skip_record": tk.BooleanVar(),
        }
        grid = ttk.Frame(ft)
        grid.pack(fill="x")
        labels = [
            ("skip_face_only", "纯表情 / 表情包"),
            ("skip_image_only", "纯图片（消息未附带文字）"),
            ("skip_forward", "合并转发"),
            ("skip_record", "语音消息"),
        ]
        for i, (key, text) in enumerate(labels):
            ttk.Checkbutton(grid, text=text, variable=self.v_filters[key]).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, 26), pady=3
            )
        minline = ttk.Frame(ft)
        minline.pack(fill="x", pady=(8, 0))
        ttk.Label(minline, text="丢弃短于").pack(side="left")
        self.v_minlen = tk.StringVar()
        ttk.Entry(minline, textvariable=self.v_minlen, width=6).pack(side="left", padx=6)
        ttk.Label(minline, text="字的消息（0 = 不过滤，不计占位符）").pack(side="left")

        senders = ttk.LabelFrame(tab, text="过滤发言人（所有群）", padding=12)
        senders.pack(fill="x", pady=(12, 0))
        entry_row = ttk.Frame(senders)
        entry_row.pack(fill="x")
        self.v_blocked_sender = tk.StringVar()
        sender_entry = ttk.Entry(entry_row, textvariable=self.v_blocked_sender, width=24)
        sender_entry.pack(side="left", fill="x", expand=True)
        sender_entry.bind("<Return>", self._add_blocked_sender)
        ttk.Button(entry_row, text="添加", command=self._add_blocked_sender).pack(side="left", padx=6)
        ttk.Button(entry_row, text="删除选中", command=self._remove_blocked_senders).pack(side="left")
        ttk.Button(entry_row, text="保存设置", command=self.save_prefs).pack(side="left", padx=(6, 0))
        sender_list = ttk.Frame(senders)
        sender_list.pack(fill="x", pady=6)
        self.lst_blocked_senders = tk.Listbox(sender_list, height=4, selectmode="extended",
                                             exportselection=False, font=(FONT, 10))
        sender_scroll = ttk.Scrollbar(sender_list, orient="vertical", command=self.lst_blocked_senders.yview)
        self.lst_blocked_senders.configure(yscrollcommand=sender_scroll.set)
        self.lst_blocked_senders.pack(side="left", fill="x", expand=True)
        sender_scroll.pack(side="right", fill="y")
        ttk.Label(senders, text="按发言人完整名称匹配；Q群管家同时匹配已知账号，删除该项即取消。保存后重启消息服务生效。\n"
                  "命中后不入库、不做 OCR / LLM；历史消息隐藏且不计数、不进新摘要，移出名单可恢复已有记录。",
                  style="Hint.TLabel").pack(anchor="w")

        # --- 协议端 ---
        nc = ttk.LabelFrame(tab, text="协议端（NapCat）", padding=12)
        nc.pack(fill="x", pady=(12, 0))
        account_row = ttk.Frame(nc)
        account_row.pack(fill="x", pady=(0, 8))
        ttk.Label(account_row, text="目标 QQ 号").pack(side="left")
        self.v_qq_account = tk.StringVar()
        ttk.Entry(account_row, textvariable=self.v_qq_account, width=20).pack(side="left", padx=8)
        self.v_napcat_dir = tk.StringVar()
        self.v_qq_path = tk.StringVar()
        for title, variable, action in (("本机 NapCat 目录", self.v_napcat_dir, self.choose_napcat_dir),
                                        ("QQ.exe 路径（可选）", self.v_qq_path, self.choose_qq_path)):
            path_row = ttk.Frame(nc)
            path_row.pack(fill="x", pady=(0, 6))
            ttk.Label(path_row, text=title, width=20).pack(side="left")
            ttk.Entry(path_row, textvariable=variable).pack(side="left", fill="x", expand=True, padx=8)
            ttk.Button(path_row, text="选择", command=action).pack(side="right")
        ttk.Label(nc, text="保存后重启消息服务生效；历史数据保留。首次换号请在 NapCat 管理页切换并扫码。",
                  style="Hint.TLabel").pack(anchor="w", pady=(0, 6))
        self.v_napcat_hidden = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            nc,
            text="隐藏控制台窗口启动（推荐）",
            variable=self.v_napcat_hidden,
        ).pack(anchor="w")
        ttk.Label(
            nc,
            text="勾上 = 启动协议端时不出现 CMD 窗口；启动失败会显示在状态栏和日志中。\n"
                 "取消勾选 = 回到「看得见」的模式，会弹控制台并显示完整自检输出（排障时用）。\n"
                 "两种方式都绕不过 UAC 授权弹窗 —— NapCat 要往 QQ 进程注入 DLL，这是管理员操作。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(6, 0))

        appearance = ttk.LabelFrame(tab, text="程序图标", padding=12)
        appearance.pack(fill="x", pady=(12, 0))
        ttk.Button(appearance, text="选择图标（PNG / ICO）", command=self.choose_app_icon).pack(side="left")
        self.v_icon_status = tk.StringVar(value="未指定图标")
        ttk.Label(appearance, textvariable=self.v_icon_status, style="Hint.TLabel", wraplength=600).pack(side="left", padx=8)

        # --- 操作 ---
        ops = ttk.Frame(tab)
        ops.pack(fill="x", pady=(14, 0))
        ttk.Button(ops, text="保存设置", command=self.save_prefs).pack(side="left")
        ttk.Button(ops, text="重新加载", command=self.reload_prefs).pack(side="left", padx=8)
        ttk.Button(ops, text="打开偏好文件", command=self.open_prefs_file).pack(side="right")
        ttk.Label(
            tab,
            text="设置保存在 .ui/preferences.yaml（本人本地，不入版本库）。"
                 "配置文件 config.yaml 受版本管理，界面不会改写它。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(8, 0))
        self._bind_wheel(tab, self._scroll_canvas)


    @staticmethod
    def _bind_wheel(widget, canvas: tk.Canvas) -> None:
        """把滚轮事件绑到设置页的全部子控件上。

        Tk 的滚轮事件由指针下的控件消费、不冒泡到 Canvas，必须逐个绑定。
        必须在本页控件都建完之后调用，否则绑不到任何东西。
        Text / Listbox 自带滚动语义，跳过，免得「滚内容」和「滚整页」互相打架。
        """

        def on_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        def walk(w) -> None:
            if not isinstance(w, (tk.Text, tk.Listbox)):
                w.bind("<MouseWheel>", on_wheel)
            for child in w.winfo_children():
                walk(child)

        walk(widget)


    def _add_blocked_sender(self, _event=None) -> str:
        name = self.v_blocked_sender.get().strip()
        names = self.lst_blocked_senders.get(0, "end")
        if name and name not in names:
            self.lst_blocked_senders.insert("end", name)
            self.status.set("已添加过滤发言人，点击保存设置后生效")
        else:
            self.status.set("该发言人已在列表中，无需重复添加" if name else "请先填写要过滤的发言人")
        self.v_blocked_sender.set("")
        return "break"


    def _remove_blocked_senders(self) -> None:
        selected = self.lst_blocked_senders.curselection()
        for index in reversed(selected):
            self.lst_blocked_senders.delete(index)
        self.status.set(f"已移除 {len(selected)} 项，点击保存设置后生效" if selected else "请先选中要删除的发言人")


    def _load_into_widgets(self) -> None:
        p = self.prefs
        self._api_profiles, active = api_settings.catalog(self.cfg, p)
        self._show_api_profile(active)
        if "summary_target_chars" not in prefs_mod.load_raw(self.prefs_file):
            p["summary_target_chars"] = self.cfg.get("summary", {}).get("target_chars", DIGEST_DEFAULT_CHARS)
        options = self._summary_options()
        self.txt_summary_prompt.delete("1.0", "end")
        self.txt_summary_prompt.insert("1.0", options["prompt"])
        self.v_summary_chars.set(str(options["target_chars"]))
        self.v_summary_schedule_desc.set(self._summary_schedule_desc())

        f = p.get("filters") or {}
        self.lst_blocked_senders.delete(0, "end")
        for name in prefs_mod.blocked_senders(f):
            self.lst_blocked_senders.insert("end", name)
        self.v_blocked_sender.set("")
        for key, var in self.v_filters.items():
            var.set(bool(f.get(key, False)))
        self.v_minlen.set(str(int(f.get("min_text_length") or 0)))
        self.v_qq_account.set(str(p.get("qq_account") or (self.cfg.get("napcat") or {}).get("account") or ""))
        self.v_napcat_dir.set(str(p.get("napcat_dir") or self.cfg.get("napcat", {}).get("dir") or ""))
        self.v_qq_path.set(str(p.get("qq_path") or self.cfg.get("napcat", {}).get("qq_path") or ""))

        self.v_napcat_hidden.set(
            bool(
                prefs_mod.load_raw(self.prefs_file).get(
                    "napcat_hidden", (self.cfg.get("napcat") or {}).get("hidden", True)
                )
            )
        )
        self._rt_keyword_rules = ()
        try:
            self._rt_keyword_rules = compile_rules(p.get("message_keywords") or [])
        except ValueError as exc:
            self.v_rt_keywords.set(f"关键词配置有误：{exc}；请重新配置")
        saved = {str(g) for g in (p.get("groups") or [])}
        for i, gid in enumerate(self._group_ids):
            if gid in saved:
                self.lst_groups.selection_set(i)


    def _summary_options(self) -> dict:
        """仅使用已保存的设置，生成过程中编辑提示词不会改变当前请求。"""
        config = self.cfg.get("summary", {})
        prompt = str(self.prefs.get("summary_prompt") or config.get("prompt") or DEFAULT_DIGEST_PROMPT)
        try:
            chars = int(self.prefs.get("summary_target_chars") or config.get("target_chars") or DIGEST_DEFAULT_CHARS)
        except (TypeError, ValueError):
            chars = DIGEST_DEFAULT_CHARS
        return {"prompt": prompt, "target_chars": chars if 1 <= chars <= 100000 else DIGEST_DEFAULT_CHARS}


    def reset_summary_prompt(self) -> None:
        self.txt_summary_prompt.delete("1.0", "end")
        self.txt_summary_prompt.insert("1.0", DEFAULT_DIGEST_PROMPT)
        self.status.set("已恢复默认提示词；点击保存设置后生效")


    def _collect(self) -> dict:
        try:
            target_chars = int(self.v_summary_chars.get())
            if not 1 <= target_chars <= 100000:
                raise ValueError
        except ValueError:
            raise ValueError("摘要目标字数必须是 1～100000 的整数")
        try:
            minlen = max(0, int(self.v_minlen.get() or 0))
        except ValueError:
            raise ValueError("最小长度必须是整数")

        groups = [self._group_ids[i] for i in self.lst_groups.curselection()]
        account = self.v_qq_account.get().strip()
        if account and (not account.isascii() or not account.isdigit() or account == "0"):
            raise ValueError("目标 QQ 号必须为非零数字账号")
        profile = api_settings.validate({key: var.get() for key, var in self.v_api.items()})
        return {
            **self.prefs,
            "api": profile,
            "api_profiles": {**self._api_profiles, self._api_selected: profile},
            "active_api_profile": self._api_selected,
            "summary_prompt": self.txt_summary_prompt.get("1.0", "end").strip(),
            "summary_target_chars": target_chars,
            "groups": groups,
            "filters": {
                "blocked_senders": list(self.lst_blocked_senders.get(0, "end")),
                "skip_face_only": bool(self.v_filters["skip_face_only"].get()),
                "skip_image_only": bool(self.v_filters["skip_image_only"].get()),
                "skip_forward": bool(self.v_filters["skip_forward"].get()),
                "skip_record": bool(self.v_filters["skip_record"].get()),
                "min_text_length": minlen,
            },
            "napcat_hidden": bool(self.v_napcat_hidden.get()),
            "qq_account": account,
            "napcat_dir": self.v_napcat_dir.get().strip(),
            "qq_path": self.v_qq_path.get().strip(),
            "ui": self.prefs.get("ui", {}),
        }


    def _settings_feedback(self, message: str) -> None:
        message = security.redact(message)
        self.status.set(message)
        self.v_api_feedback.set(time.strftime("%H:%M:%S") + " · " + message)

    def choose_napcat_dir(self):
        path = filedialog.askdirectory(title="选择本机 NapCat 安装目录", parent=self.root)
        if path:
            self.v_napcat_dir.set(path)
            self._settings_feedback("已选择 NapCat 目录；保存设置后生效")
        else:
            self._settings_feedback("已取消选择 NapCat 目录")

    def choose_qq_path(self):
        path = filedialog.askopenfilename(title="选择 QQ.exe（留空时由 NapCat 自动查找）",
                                          filetypes=[("QQ 程序", "*.exe")], parent=self.root)
        if path:
            self.v_qq_path.set(path)
            self._settings_feedback("已选择 QQ 路径；保存设置后生效")
        else:
            self._settings_feedback("已取消选择 QQ 路径")


    def save_prefs(self, silent: bool = False) -> bool:
        new_key = self.v_api_key.get().strip()
        if new_key:
            security.register(new_key)
        try:
            data = self._collect()
        except ValueError as exc:
            if not silent:
                self._settings_feedback(f"设置有误：{exc}；未保存")
                self._log_ui(f"[界面] 设置有误：{exc}\n")
            return False
        key_saved = False
        try:
            if new_key:
                credentials.save(data["api"]["base_url"], self.v_api_key.get(), profile_id=self._api_selected)
                key_saved = True
                self.v_api_key.set("")
            prefs_mod.save(data, self.prefs_file)
        except (OSError, credentials.CredentialError) as exc:
            if not silent:
                self._settings_feedback(("新密钥已保存，但配置保存失败：" if key_saved else "保存失败：") + str(exc))
                self._log_ui(f"[界面] 保存失败：{exc}\n")
            return False
        self.prefs = data
        self._api_profiles = data["api_profiles"]
        self.api_selector.configure(values=[p["provider"] for p in self._api_profiles.values()])
        self.api_selector.current(list(self._api_profiles).index(self._api_selected))
        self._api_key_status()
        self.v_summary_schedule_desc.set(self._summary_schedule_desc())
        n_groups = len(data["groups"]) or "全部"
        self._settings_feedback(
            ("保存成功 · 新密钥已配置（仅本地保存，未验证 API 可用性）" if key_saved else "保存成功 · 密钥未更改")
            + f" · 新任务使用 {data['api']['provider']} / {data['api']['model']} · 摘要约 {data['summary_target_chars']} 字 · 群 {n_groups}"
            + ("（其他非 API 服务设置重启后生效）" if self.proc else "")
        )
        return True


    def _show_api_profile(self, active: str) -> None:
        self._api_selected = active
        self.api_selector.configure(values=[p["provider"] for p in self._api_profiles.values()])
        self.api_selector.current(list(self._api_profiles).index(active))
        for key, var in self.v_api.items():
            var.set(str(self._api_profiles[active][key]))
        self.v_api_key.set("")
        self._api_key_status()


    def _select_api_profile(self, _event=None) -> None:
        selected = list(self._api_profiles)[self.api_selector.current()]
        previous = self._api_selected
        if selected == previous:
            return
        if not self.save_prefs():
            self.api_selector.current(list(self._api_profiles).index(previous))
            return
        self._show_api_profile(selected)
        if not self.save_prefs():
            self._show_api_profile(previous)


    def add_api_profile(self) -> None:
        if not self.save_prefs():
            return
        i = 1
        while f"custom_{i}" in self._api_profiles:
            i += 1
        name = f"custom_{i}"
        self._api_profiles[name] = {**api_settings.DEFAULTS, "provider": f"自定义 {i}"}
        self._show_api_profile(name)
        self._settings_feedback("新配置待填写并保存；未保存前继续使用原配置。密钥需单独设置。")


    def _api_key_status(self) -> None:
        if self.v_api_key.get().strip():
            self.v_api_key_status.set("新密钥尚未保存；点击下方「保存配置 / 替换密钥」后生效")
            self.v_api_feedback.set("新密钥待保存；此前的保存结果不代表本次输入已保存")
            return
        try:
            present = bool(credentials.read(api_settings.endpoint(self.v_api["base_url"].get()), profile_id=self._api_selected))
            self.v_api_key_status.set("已配置（Windows 凭据管理器）" if present else "此地址未配置密钥")
        except (ValueError, credentials.CredentialError):
            self.v_api_key_status.set("无法读取密钥或地址无效")


    def delete_api_key(self) -> None:
        try:
            credentials.delete(api_settings.endpoint(self.v_api["base_url"].get()), profile_id=self._api_selected)
            self.v_api_key.set("")
            self._api_key_status()
            self._settings_feedback("此地址的密钥已删除；在途请求可能继续完成，后续请求不再使用该密钥")
        except (ValueError, credentials.CredentialError) as exc:
            self._settings_feedback(str(exc))


    def import_api_config(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("YAML API 配置", "*.yaml")])
        if not path:
            self._settings_feedback("已取消导入，配置未更改")
            return
        try:
            profile = api_settings.validate(yaml_config.read(path))
            for key, var in self.v_api.items():
                var.set(str(profile[key]))
            self.v_api_key.set("")
            self._api_key_status()
            self._settings_feedback("API 配置已载入，点击保存后生效；密钥需单独配置")
        except (OSError, ValueError, TypeError):
            self._settings_feedback("导入失败：配置格式无效或含不允许的字段；请勿在配置文件中填写密钥")


    def export_api_config(self) -> None:
        try:
            profile = api_settings.validate({key: var.get() for key, var in self.v_api.items()})
            path = filedialog.asksaveasfilename(defaultextension=".yaml", initialfile="api-config.yaml",
                                              filetypes=[("YAML API 配置", "*.yaml")])
            if path:
                Path(path).write_text(yaml_config.dumps(profile), encoding="utf-8", newline="\n")
                self._settings_feedback("API 配置已导出，不含密钥")
            else:
                self._settings_feedback("已取消导出，未写入文件")
        except (OSError, ValueError):
            self._settings_feedback("导出失败：请检查配置或文件写入权限")


    def reload_prefs(self) -> None:
        try:
            if self.prefs_file.exists():
                yaml_config.read(self.prefs_file)
            loaded = prefs_mod.load(self.prefs_file)
            api_settings.catalog(self.cfg, loaded)
        except (OSError, ValueError, TypeError):
            self._settings_feedback("重新加载失败：偏好文件无法读取或格式无效，当前编辑已保留")
            return
        self.prefs = loaded
        self._load_into_widgets()
        self._settings_feedback("已重新加载设置" if self.prefs_file.exists() else "偏好文件尚不存在，已加载默认设置")


    def open_prefs_file(self) -> None:
        if not self.prefs_file.exists():
            if not self.save_prefs():
                return
        self._open_external(self.prefs_file)


    def choose_app_icon(self) -> None:
        source = filedialog.askopenfilename(filetypes=[("程序图标", "*.png *.ico")])
        if not source:
            self.status.set("已取消选择图标")
            return
        try:
            from io import BytesIO
            from PIL import Image
            path = self.prefs_file.parent / "app-icon.ico"
            path.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(source) as image:
                if image.format == "ICO":
                    image.load()
                    payload = Path(source).read_bytes()  # 保留作者提供的全部尺寸，不重新编码。
                elif image.format == "PNG":
                    icon = image.convert("RGBA")
                    side = max(icon.size)
                    canvas = Image.new("RGBA", (side, side))
                    canvas.paste(icon, ((side-icon.width)//2, (side-icon.height)//2))
                    sizes = [(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
                    frames = [canvas.resize(size, Image.Resampling.LANCZOS) for size in sizes]
                    output = BytesIO()
                    frames[-1].save(output, format="ICO", sizes=sizes, append_images=frames[:-1])
                    payload = output.getvalue()
                else:
                    raise ValueError("请选择 PNG 或 ICO")
            path.write_bytes(payload)
            data = {**self.prefs, "ui": {**self.prefs.get("ui", {}), "icon_path": str(path)}}
            prefs_mod.save(data, self.prefs_file)
            self.prefs = data
            self._apply_app_icon()
            self.status.set(self.v_icon_status.get() + "；已保存本地副本，不依赖原文件")
        except (OSError, ValueError):
            self.status.set("图标保存失败，请检查图片格式和文件权限")


    def refresh_groups(self, prune: bool = False) -> None:
        if getattr(self, "_groups_busy", False):
            self.status.set("正在刷新群列表，请稍候…")
            return
        self.status.set("正在清理空群并刷新…" if prune else "正在刷新群列表…")
        self._groups_busy = True
        self.btn_groups_refresh.configure(state="disabled")
        self.btn_groups_prune.configure(state="disabled")
        port = int(self.cfg["onebot"]["http_port"])
        db = abs_path(self.cfg, "db_path")
        running = self.proc is not None
        events = self.events
        def work():
            fetched, rows, error = {}, [], ""
            if running:
                try:
                    fetched = request_json(port, "/groups/refresh", method="POST",
                                           query={"prune": 1} if prune else None, timeout=20)
                except Exception:
                    pass  # 完成时明确标注回退本地库，不能把清理未确认显示为成功。
            try:
                if db.exists():
                    store = Store(db, readonly=True)
                    try:
                        rows = store.group_rows()
                    finally:
                        store.close()
            except (OSError, sqlite3.Error) as exc:
                error = security.redact(str(exc))
            events.put(("groups_done", (fetched, prune, rows, error)))
        threading.Thread(target=work, daemon=True).start()


    def _render_groups(self, fetched: dict, rows: list, error: str = "") -> bool:
        saved = set(self.prefs.get("groups") or [])
        uin = str(fetched.get("uin") or "")
        if error:
            self.status.set(f"读取群列表失败：{error}；保留原列表")
            return False
        self._group_ids = []
        self.lst_groups.delete(0, "end")
        for gid, name, owner, n in rows:
            gid, name, owner = str(gid), str(name or ""), str(owner or "")
            self._group_ids.append(gid)
            label = f"{name}  ({gid})" if name else f"(未命名)  {gid}"
            if owner:
                label += f"   接收账号：{owner}"
            if uin and owner and uin not in owner.split(","):
                label += "   · 历史群"
            elif n == 0:
                label += "   · 0 条"
            self.lst_groups.insert("end", label)
        for i, gid in enumerate(self._group_ids):
            if gid in saved:
                self.lst_groups.selection_set(i)
        parts = [f"群列表已刷新，共 {len(self._group_ids)} 个群"]
        if uin:
            mine = sum(1 for _g, _n, o, _c in rows if uin in str(o or "").split(",")) if rows else 0
            parts.append(f"当前协议端账号 {uin}（其中 {mine} 个已标注属主）")
        else:
            parts.append("未能连到协议端，群名来自本地库；启动服务与协议端后可拉到真实群名")
        deleted = fetched.get("deleted") or []
        protected = fetched.get("protected") or []
        if deleted:
            parts.append(f"已删除 {len(deleted)} 个不属于当前账号的空群")
        if protected:
            parts.append(f"保留 {len(protected)} 个有消息但不属于当前账号的群（删了会让摘要失效）")
        self.status.set("；".join(parts))
        return True


    def prune_groups(self) -> None:
        """「只保留当前账号的群」：让服务按当前登录账号删掉别的账号留下的空群。"""
        if self.proc is None:
            self.status.set("请先启动服务，再向协议端查询当前账号的群。")
            return
        if not messagebox.askyesno(
            "只保留当前账号的群",
            "将删掉「不属于当前登录账号、且一条消息都没有」的群。\n\n"
            "· 有消息的群**一律保留**（删了会让那个群的摘要静默失效）\n"
            "· 删的只是群列表里的条目，不涉及任何消息数据\n\n"
            "继续吗？",
            parent=self.root,
        ):
            self.status.set("已取消「只保留当前账号的群」")
            return
        self.refresh_groups(prune=True)
