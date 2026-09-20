"""消息萃取 图形界面。

分层（照搬参考项目 autodl_gpu_watcher 的做法）：
  * 本模块只负责「交互 + 进程管理」，不掺任何业务逻辑。
  * 服务跑在独立子进程里，日志经 stdout 管道 → queue → after() 回主线程。
    Tkinter 不是线程安全的，**所有控件更新必须发生在主线程**。
  * 界面设置写入 .ui/preferences.json，**永不改写**受 Git 管理的 config.toml，
    这样多人协作时不会因为各人的本地偏好互相覆盖。

只用标准库 tkinter —— 不引入任何新依赖。
"""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__
from . import napcat as napcat_mod
from . import prefs as prefs_mod
from .config import ROOT, abs_path, load
from .summarize import DIGEST_RANGES

APP_TITLE = "消息萃取 msgx"
FONT = "Microsoft YaHei UI"
CREATE_NO_WINDOW = 0x08000000  # Windows: 子进程不弹控制台窗口

# 日志框配色：沿用终端的深底浅字，长时间盯着不刺眼
LOG_BG = "#1b1f27"
LOG_FG = "#d6dbe5"
OK_COLOR = "#1a7f37"
BAD_COLOR = "#b42318"
MUTED = "#6b6b68"


def worker_python() -> str:
    """后台任务用的解释器。

    界面本身可能由 pythonw.exe 启动（无控制台）。子进程需要能读 stdout 管道，
    所以优先换成同目录下的 python.exe；换不到就沿用当前解释器。
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return sys.executable


class App:
    def __init__(self, root: tk.Tk, config_path: str | None = None) -> None:
        self.root = root
        self.config_path = config_path
        self.cfg = load(config_path)
        self.ui_dir = ROOT / ".ui"
        self.ui_dir.mkdir(parents=True, exist_ok=True)
        self.prefs_file = prefs_mod.prefs_path(ROOT)
        self.stop_file = self.ui_dir / "stop.signal"

        self.prefs = prefs_mod.load(self.prefs_file)
        self.proc: subprocess.Popen | None = None
        self.events: queue.Queue = queue.Queue()
        self.closing = False
        self.health_busy = False
        # 实时消息页：拉取是否在进行中（避免慢响应时叠请求）、上次用的条数
        self._rt_busy = False
        self._rt_limit = 80
        self.napcat_log_win: tk.Toplevel | None = None
        # 登录二维码弹窗。Tkinter 只能在主线程碰控件，所以监视线程只把路径
        # 丢进 events 队列，由 _drain 在主线程里弹窗。
        self.qr_win: tk.Toplevel | None = None
        self.qr_watching = False

        self._group_ids: list[str] = []
        self._build()
        self._load_into_widgets()
        self.refresh_groups()
        self.root.after(100, self._drain)
        self.root.after(800, self._tick_health)
        self.root.after(1200, self._tick_realtime)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- 界面构建 ----------------

    def _build(self) -> None:
        self.root.title(f"{APP_TITLE}  v{__version__}")
        # 默认尺寸按屏幕算：写死 800 高在 768p 屏上会顶出屏幕，
        # 设置页底部的「保存设置」直接被切掉（用户得手动拉窗口才能看到）。
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = min(1040, max(900, sw - 80))
        h = min(940, max(680, sh - 90))
        self.root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        self.root.minsize(820, 560)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TButton", padding=(12, 7), font=(FONT, 10))
        style.configure("TLabel", font=(FONT, 10))
        style.configure("TCheckbutton", font=(FONT, 10))
        style.configure("TLabelframe.Label", font=(FONT, 10, "bold"))
        style.configure("Title.TLabel", font=(FONT, 20, "bold"))
        style.configure("Hint.TLabel", font=(FONT, 9), foreground=MUTED)
        style.configure("Card.TLabel", font=(FONT, 11))
        style.configure("CardKey.TLabel", font=(FONT, 10), foreground=MUTED)
        style.configure("TNotebook.Tab", font=(FONT, 10), padding=(18, 8))

        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="QQ 群消息萃取 · 只读不发言 · 配置与启停都在这里完成",
        ).pack(anchor="w", pady=(2, 12))

        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)
        self.nb = nb
        self._build_status_tab(nb)
        # 实时消息页刻意排在「运行状态」之后、「设置」之前 —— 它是日常最常看的第二屏：
        # 想确认「到底收进来了没有、内容对不对」时，不用再去翻 data/msgx.db。
        self._build_realtime_tab(nb)
        self._build_settings_tab(nb)

        self.status = tk.StringVar(value="就绪")
        ttk.Label(outer, textvariable=self.status, foreground="#245cc4").pack(
            anchor="w", pady=(10, 0)
        )

    # ---- 标签页 1：运行状态 ----

    def _build_status_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=14)
        nb.add(tab, text="运行状态")

        card = ttk.LabelFrame(tab, text="服务状态", padding=14)
        card.pack(fill="x")

        self.v_service = tk.StringVar(value="未运行")
        self.v_peer = tk.StringVar(value="—")
        self.v_stored = tk.StringVar(value="—")
        self.v_vision = tk.StringVar(value="—")
        self.v_cost = tk.StringVar(value="—")
        self.v_total = tk.StringVar(value="—")
        self.v_balance = tk.StringVar(value="—")
        self.v_llm = tk.StringVar(value="—")

        # 两列并排。用户的原话：「出了今日花费，总花费呢，不应该写在进入花费的右边吗」——
        # 所以这两个必须是同一行的左右邻居，而不是上下两行。
        rows = [
            [("服务进程", self.v_service), ("协议端(NapCat)", self.v_peer)],
            [("已入库消息", self.v_stored), ("图片语义失败", self.v_vision)],
            [("今日花费", self.v_cost), ("累计花费", self.v_total)],
            [("账户余额", self.v_balance), ("模型状态", self.v_llm)],
        ]
        for r, pair in enumerate(rows):
            for c, (key, var) in enumerate(pair):
                col = c * 2
                ttk.Label(card, text=key, style="CardKey.TLabel").grid(
                    row=r, column=col, sticky="w", padx=(0 if col == 0 else 28, 10), pady=3
                )
                ttk.Label(card, textvariable=var, style="Card.TLabel").grid(
                    row=r, column=col + 1, sticky="w", pady=3
                )
        card.columnconfigure(1, weight=1)
        card.columnconfigure(3, weight=1)

        # ---- 按钮区（两行，避免一行挤成一坨）----
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(12, 4))
        self.btn_start = ttk.Button(buttons, text="启动服务", command=self.start_service)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(buttons, text="停止服务", command=self.stop_service, state="disabled")
        self.btn_stop.pack(side="left", padx=8)

        # 摘要只有两种生成方式：
        #   ① 增量 —— 接着上次总结过的地方继续（日常就是这个）
        #   ② 按范围 —— 明确「把最近 N 天的消息重做一份」
        # 原来的「全量重做」没写清范围与字数，用户原话是「意义不明」。
        self.btn_summary = ttk.Button(
            buttons, text="生成最新摘要", command=lambda: self.summary_now("digest")
        )
        self.btn_summary.pack(side="left")
        ttk.Label(buttons, text="按范围重做：").pack(side="left", padx=(16, 4))
        self.v_range = tk.StringVar(value=DIGEST_RANGES[0][0])
        self.cb_range = ttk.Combobox(
            buttons,
            textvariable=self.v_range,
            values=[label for label, _h, _c in DIGEST_RANGES],
            state="readonly",
            width=11,
            font=(FONT, 10),
        )
        self.cb_range.pack(side="left")
        self.v_range_desc = tk.StringVar(value=self._range_desc(DIGEST_RANGES[0][0]))
        self.cb_range.bind("<<ComboboxSelected>>", lambda _e: self._on_range_change())
        ttk.Button(buttons, text="生成", command=self.summary_by_range, width=6).pack(
            side="left", padx=6
        )
        ttk.Button(buttons, text="打开数据目录", command=self.open_data_dir).pack(side="right")

        buttons2 = ttk.Frame(tab)
        buttons2.pack(fill="x")
        ttk.Label(buttons2, textvariable=self.v_range_desc, style="Hint.TLabel").pack(side="left")
        # 协议端必须单独启动 —— 本服务只监听它连入。没配置就不显示这两个按钮。
        if self.napcat_script() or self.napcat_dir():
            ttk.Button(
                buttons2, text="启动协议端(NapCat)", command=self.start_napcat
            ).pack(side="right", padx=8)
            ttk.Button(buttons2, text="协议端日志", command=self.show_napcat_log).pack(side="right")
        ttk.Button(buttons2, text="刷新账单", command=self.refresh_billing).pack(side="right", padx=8)

        log_frame = ttk.LabelFrame(tab, text="服务日志", padding=8)
        log_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.log = ScrolledText(
            log_frame,
            height=16,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            background=LOG_BG,
            foreground=LOG_FG,
            insertbackground=LOG_FG,
            padx=10,
            pady=8,
            relief="flat",
        )
        self.log.pack(fill="both", expand=True)

    @staticmethod
    def _range_desc(label: str) -> str:
        for text, hours, chars in DIGEST_RANGES:
            if text == label:
                return f"目标约 {chars} 字（窗口越长允许写越长），数据源为最近 {hours // 24} 天"
        return ""

    def _on_range_change(self) -> None:
        self.v_range_desc.set(self._range_desc(self.v_range.get()))

    # ---- 标签页 2：实时消息 ----

    def _build_realtime_tab(self, nb: ttk.Notebook) -> None:
        """把「现在收到了什么」搬进界面。

        起因是 NapCat 那个 CMD 黑框：以前有人习惯盯着协议端窗口看消息到底进没进来。
        既然要把黑框干掉，就得在界面里给一个**更好的**替代品 —— 这里能看到
        入库后的真实内容（含文本、图片处理状态、是否被 @），比协议端滚屏有用得多。
        """
        tab = ttk.Frame(nb, padding=14)
        nb.add(tab, text="实时消息")

        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 8))
        self.v_rt_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            bar, text="自动刷新（每 3 秒）", variable=self.v_rt_auto
        ).pack(side="left")
        ttk.Button(bar, text="立即刷新", command=self.refresh_messages).pack(side="left", padx=8)
        self.v_rt_limit = tk.StringVar(value="80")
        ttk.Label(bar, text="显示条数").pack(side="left", padx=(16, 4))
        ttk.Entry(bar, textvariable=self.v_rt_limit, width=6).pack(side="left")
        self.v_rt_status = tk.StringVar(value="尚未加载")
        ttk.Label(bar, textvariable=self.v_rt_status, style="Hint.TLabel").pack(side="right")

        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True)
        cols = ("time", "group", "who", "text", "state")
        self.rt = ttk.Treeview(wrap, columns=cols, show="headings", height=18)
        headings = {
            "time": ("时间", 78, "center"),
            "group": ("群", 130, "w"),
            "who": ("发言人", 110, "w"),
            "text": ("内容", 430, "w"),
            "state": ("图片", 70, "center"),
        }
        for key, (title, width, anchor) in headings.items():
            self.rt.heading(key, text=title)
            self.rt.column(key, width=width, anchor=anchor, stretch=(key == "text"))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.rt.yview)
        self.rt.configure(yscrollcommand=sb.set)
        self.rt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        # 交替行色 + 被 @ 的行标红，扫一眼就能看出有没有需要立刻处理的
        self.rt.tag_configure("odd", background="#f5f7fa")
        self.rt.tag_configure("atme", foreground=BAD_COLOR)
        self.rt.tag_configure("failed", foreground="#a15c00")

        ttk.Label(
            tab,
            text="显示最近入库的消息（新的在最上面）。「图片」列 = none 无图 / 完成 / 待处理 / "
                 "失败 / 语义失败 —— 语义失败会自动补跑，最多 3 次。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(8, 0))

    # ---- 标签页 3：设置 ----

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

        # --- 关键词 ---
        kw = ttk.LabelFrame(tab, text="关注关键词", padding=12)
        kw.pack(fill="both", expand=False)
        ttk.Label(
            kw,
            text="一行一个。命中即进入候选池，再由模型判定是否值得提醒。留空表示沿用 config.toml 里的设置。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(0, 6))
        self.txt_keywords = tk.Text(kw, height=6, wrap="word", font=(FONT, 10), relief="solid", borderwidth=1)
        self.txt_keywords.pack(fill="both", expand=True)

        # --- 摘要 ---
        sm = ttk.LabelFrame(tab, text="摘要频率", padding=12)
        sm.pack(fill="x", pady=(12, 0))
        line = ttk.Frame(sm)
        line.pack(fill="x")
        ttk.Label(line, text="每").pack(side="left")
        self.v_interval = tk.StringVar()
        ttk.Entry(line, textvariable=self.v_interval, width=6).pack(side="left", padx=6)
        ttk.Label(line, text="分钟生成一次摘要").pack(side="left")
        ttk.Label(
            sm,
            text="填 0 表示沿用 config.toml 里的值。间隔越短，模型调用越频繁，成本越高。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(6, 0))

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
        ttk.Button(gp, text="刷新群列表", command=self.refresh_groups).pack(anchor="w", pady=(8, 0))
        ttk.Label(
            gp,
            text="群列表来自已入库的数据，需要先让服务跑一会儿再刷新。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        # --- 过滤 ---
        ft = ttk.LabelFrame(tab, text="不接收的消息类型", padding=12)
        ft.pack(fill="x", pady=(12, 0))
        ttk.Label(
            ft,
            text="过滤在入库前生效，被丢弃的消息不占存储、也不产生模型费用。默认全部不过滤。",
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
            ("skip_image_only", "纯图片（不含任何文字）"),
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

        # --- 协议端 ---
        nc = ttk.LabelFrame(tab, text="协议端（NapCat）", padding=12)
        nc.pack(fill="x", pady=(12, 0))
        self.v_napcat_hidden = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            nc,
            text="隐藏控制台窗口启动（推荐）",
            variable=self.v_napcat_hidden,
        ).pack(anchor="w")
        ttk.Label(
            nc,
            text="勾上 = 启动协议端时不出现 CMD 窗口，任务栏干干净净；启动失败会弹消息框提示。\n"
                 "取消勾选 = 回到「看得见」的模式，会弹控制台并显示完整自检输出（排障时用）。\n"
                 "两种方式都绕不过 UAC 授权弹窗 —— NapCat 要往 QQ 进程注入 DLL，这是管理员操作。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(6, 0))

        # --- 账单 ---
        bl = ttk.LabelFrame(tab, text="账单（真实数据）", padding=12)
        bl.pack(fill="x", pady=(12, 0))
        ttk.Label(
            bl,
            text="余额用 API Key 查（自动）；消费明细必须用平台登录态 token，取法见下。"
                 "留空 = 只显示本地估算，界面会标注「估算」。",
            style="Hint.TLabel",
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self.txt_platform_token = tk.Text(
            bl, height=3, wrap="char", font=("Consolas", 9), relief="solid", borderwidth=1
        )
        self.txt_platform_token.pack(fill="x")
        ttk.Label(
            bl,
            text="取法：浏览器登录 platform.deepseek.com → F12 → Application → Local Storage "
                 "→ 复制 userToken 的值 → 粘到上面 → 保存设置 → 重启服务。\n"
                 "它是短期凭证，会过期；过期后界面会提示重新复制。token 只存在本机 "
                 ".ui/preferences.json 里，不会进 Git。",
            style="Hint.TLabel",
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))
        ttk.Button(
            bl,
            text="打开 DeepSeek 平台（用于取 token）",
            command=lambda: open_url("https://platform.deepseek.com/usage"),
        ).pack(anchor="w", pady=(8, 0))

        # --- 操作 ---
        ops = ttk.Frame(tab)
        ops.pack(fill="x", pady=(14, 0))
        ttk.Button(ops, text="保存设置", command=self.save_prefs).pack(side="left")
        ttk.Button(ops, text="重新加载", command=self.reload_prefs).pack(side="left", padx=8)
        ttk.Button(ops, text="打开偏好文件", command=self.open_prefs_file).pack(side="right")
        ttk.Label(
            tab,
            text="设置保存在 .ui/preferences.json（本人本地，不入版本库）。"
                 "配置文件 config.toml 受版本管理，界面不会改写它。",
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

    # ---------------- 偏好读写 ----------------

    def _load_into_widgets(self) -> None:
        p = self.prefs
        keywords = p.get("keywords") or self.cfg["score"].get("keywords") or []
        self.txt_keywords.delete("1.0", "end")
        self.txt_keywords.insert("1.0", "\n".join(str(k) for k in keywords))

        interval = int(p.get("interval_minutes") or 0)
        self.v_interval.set(str(interval))

        f = p.get("filters") or {}
        for key, var in self.v_filters.items():
            var.set(bool(f.get(key, False)))
        self.v_minlen.set(str(int(f.get("min_text_length") or 0)))

        self.v_napcat_hidden.set(
            bool(
                prefs_mod.load_raw(self.prefs_file).get(
                    "napcat_hidden", (self.cfg.get("napcat") or {}).get("hidden", True)
                )
            )
        )
        self.txt_platform_token.delete("1.0", "end")
        # 偏好文件里留空 = 沿用 config.toml；这种情况下把配置值显示出来，
        # 免得用户以为「token 没了」。
        token = str(p.get("platform_token") or "").strip() or str(
            (self.cfg.get("billing") or {}).get("platform_token") or ""
        ).strip()
        if token:
            self.txt_platform_token.insert("1.0", token)

        saved = {str(g) for g in (p.get("groups") or [])}
        for i, gid in enumerate(self._group_ids):
            if gid in saved:
                self.lst_groups.selection_set(i)

    def _collect(self) -> dict:
        keywords = [
            ln.strip()
            for ln in self.txt_keywords.get("1.0", "end").splitlines()
            if ln.strip()
        ]
        try:
            interval = max(0, int(self.v_interval.get() or 0))
        except ValueError:
            raise ValueError("摘要间隔必须是整数（分钟）")
        try:
            minlen = max(0, int(self.v_minlen.get() or 0))
        except ValueError:
            raise ValueError("最小长度必须是整数")

        groups = [self._group_ids[i] for i in self.lst_groups.curselection()]
        return {
            "keywords": keywords,
            "interval_minutes": interval,
            "groups": groups,
            "filters": {
                "skip_face_only": bool(self.v_filters["skip_face_only"].get()),
                "skip_image_only": bool(self.v_filters["skip_image_only"].get()),
                "skip_forward": bool(self.v_filters["skip_forward"].get()),
                "skip_record": bool(self.v_filters["skip_record"].get()),
                "min_text_length": minlen,
            },
            "napcat_hidden": bool(self.v_napcat_hidden.get()),
            "platform_token": self.txt_platform_token.get("1.0", "end").strip(),
            "ui": self.prefs.get("ui", {}),
        }

    def save_prefs(self, silent: bool = False) -> bool:
        try:
            data = self._collect()
        except ValueError as exc:
            if not silent:
                messagebox.showerror("设置有误", str(exc), parent=self.root)
            return False
        try:
            prefs_mod.save(data, self.prefs_file)
        except OSError as exc:
            if not silent:
                messagebox.showerror("保存失败", str(exc), parent=self.root)
            return False
        self.prefs = data
        n_groups = len(data["groups"]) or "全部"
        self.status.set(
            f"设置已保存 · 关键词 {len(data['keywords'])} 个 · "
            f"间隔 {data['interval_minutes'] or '默认'} 分钟 · 群 {n_groups}"
            + ("（服务运行中，重启后生效）" if self.proc else "")
        )
        return True

    def reload_prefs(self) -> None:
        self.prefs = prefs_mod.load(self.prefs_file)
        self._load_into_widgets()
        self.status.set("已重新加载设置")

    def open_prefs_file(self) -> None:
        if not self.prefs_file.exists():
            self.save_prefs(silent=True)
        open_path(self.prefs_file.parent)

    # ---------------- 群列表 ----------------

    def refresh_groups(self) -> None:
        # 先让服务经协议端调 get_group_list 拉真实群名（NapCat 就在线上，
        # 没理由显示「(未命名)」）；服务没跑或协议端不在线时退回本地库。
        fetched = False
        if self.proc is not None:
            try:
                port = int(self.cfg["onebot"]["http_port"])
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/groups/refresh", data=b"", method="POST"
                )
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with op.open(req, timeout=15) as r:
                    fetched = bool(json.loads(r.read().decode("utf-8")).get("ok"))
            except Exception:  # noqa: BLE001
                fetched = False
        saved = set(self.prefs.get("groups") or [])
        self._group_ids = []
        self.lst_groups.delete(0, "end")
        try:
            db = abs_path(self.cfg, "db_path")
            if db.exists():
                conn = sqlite3.connect(str(db), timeout=3)
                try:
                    rows = list(conn.execute("SELECT id, name FROM groups ORDER BY id"))
                finally:
                    conn.close()
                for gid, name in rows:
                    gid, name = str(gid), str(name or "")
                    self._group_ids.append(gid)
                    self.lst_groups.insert("end", f"{name}  ({gid})" if name else f"(未命名)  {gid}")
        except Exception as exc:  # noqa: BLE001
            self.status.set(f"读取群列表失败：{exc}")
            return
        for i, gid in enumerate(self._group_ids):
            if gid in saved:
                self.lst_groups.selection_set(i)
        msg = f"群列表已刷新，共 {len(self._group_ids)} 个群"
        if not fetched:
            msg += "（未能连到协议端，群名来自本地库；启动服务与协议端后可拉到真实群名）"
        self.status.set(msg)

    # ---------------- 服务进程 ----------------

    def start_service(self) -> None:
        if self.proc is not None:
            return
        if not self.save_prefs(silent=False):
            return

        self.stop_file.unlink(missing_ok=True)  # 清掉上次停止留下的信号，否则新进程会立刻退出
        env = os.environ.copy()
        env["MSGX_PREFS"] = str(self.prefs_file)  # 让服务读到界面偏好
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUTF8"] = "1"
        env.pop("HTTP_PROXY", None)
        env.pop("HTTPS_PROXY", None)
        env.pop("ALL_PROXY", None)
        env["NO_PROXY"] = "127.0.0.1,localhost"

        cmd = [worker_python(), "-u", "-m", "msgx.main", "run", "--stop-file", str(self.stop_file)]
        if self.config_path:
            cmd += ["--config", str(self.config_path)]

        self._log_ui("正在启动服务…\n")
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            self.proc = None
            messagebox.showerror("启动失败", str(exc), parent=self.root)
            return

        self.v_service.set("运行中")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status.set("服务已启动，等待协议端连入…")
        threading.Thread(target=self._read_pipe, args=(self.proc,), daemon=True).start()

    def stop_service(self) -> None:
        if self.proc is None:
            return
        try:
            self.stop_file.touch()
        except OSError as exc:
            messagebox.showerror("停止失败", str(exc), parent=self.root)
            return
        self.btn_stop.configure(state="disabled")
        self.status.set("已发出停止信号，正在等待服务安全退出…")

    def _read_pipe(self, proc: subprocess.Popen) -> None:
        """子线程：只把日志丢进队列，绝不碰控件。"""
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                self.events.put(("line", line))
        finally:
            self.events.put(("exit", proc.wait()))

    def summary_now(self, kind: str = "digest", hours: int | None = None) -> None:
        if self.proc is None:
            messagebox.showinfo("服务未运行", "请先启动服务，再生成摘要。", parent=self.root)
            return
        port = int(self.cfg["onebot"]["http_port"])
        if kind == "range":
            label = f"近 {hours} 小时" if hours and hours < 24 else f"近 {(hours or 24) // 24} 天"
            target = f"按范围重做（{label}）"
        else:
            label = "增量"
            target = "最新摘要"
        self.status.set(f"正在请求生成{target}…")

        def work() -> None:
            try:
                # digest：接着上次已总结过的消息继续（默认）；range：把最近 hours
                # 小时的消息重做一份，目标字数由服务侧按窗口长度取 600~1500。
                # 两条路径都不受「每组 ≥8 条」的增量门槛限制，避免点按钮却回 0 份。
                url = f"http://127.0.0.1:{port}/summary/now?kind={kind}"
                if kind == "range" and hours:
                    url += f"&hours={int(hours)}"
                req = urllib.request.Request(url, data=b"", method="POST")
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with op.open(req, timeout=600) as r:
                    body = json.loads(r.read().decode("utf-8"))
                items = body.get("created") or []
                if items:
                    it = items[0]
                    chars = it.get("chars")
                    extra = (
                        f"（{chars} 字，目标 {it.get('target_chars')} 字，{it['count']} 条消息）"
                        if chars else ""
                    )
                    self.events.put(("note", f"{target}已生成{extra}，详见 data/reports/"))
                else:
                    self.events.put(
                        ("note", f"{label}窗口内没有可总结的消息（或自上次以来没有新消息）")
                    )
            except Exception as exc:  # noqa: BLE001
                self.events.put(("note", f"摘要请求失败：{exc}"))

        threading.Thread(target=work, daemon=True).start()

    def summary_by_range(self) -> None:
        """按选定的时间范围重做摘要。窗口越长，服务侧允许的目标字数越高。"""
        label = self.v_range.get()
        hours = DIGEST_RANGES[0][1]
        for text, h, _c in DIGEST_RANGES:
            if text == label:
                hours = h
                break
        self.summary_now("range", hours=hours)

    def open_data_dir(self) -> None:
        open_path(abs_path(self.cfg, "report_dir").parent)

    # ---------------- 实时消息 ----------------

    @staticmethod
    def _media_label(row) -> str:
        m = str(row.get("media_state") or "")
        v = str(row.get("vision_state") or "")
        if m in ("", "none"):
            return "—"
        if m == "pending":
            return "待处理"
        if m == "failed":
            return "下载失败"
        if v == "failed":
            return "语义失败"
        if v == "skipped":
            return "仅 OCR"
        return "完成"

    def refresh_messages(self) -> None:
        if self.proc is None:
            self.status.set("服务未运行 —— 启动服务后才能看到实时消息")
            return
        if self._rt_busy:
            return
        self._rt_busy = True
        self._rt_limit = self._read_rt_limit()
        threading.Thread(target=self._fetch_messages, args=(self._rt_limit,), daemon=True).start()

    def _read_rt_limit(self) -> int:
        """在主线程里读控件（Tkinter 变量不能在子线程碰）。"""
        try:
            return max(1, min(int(self.v_rt_limit.get() or 80), 300))
        except ValueError:
            return 80

    def _fetch_messages(self, limit: int) -> None:
        port = int(self.cfg["onebot"]["http_port"])
        rows = None
        try:
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(f"http://127.0.0.1:{port}/messages/recent?limit={limit}", timeout=5) as r:
                rows = json.loads(r.read().decode("utf-8")).get("items")
        except Exception:  # noqa: BLE001
            rows = None
        finally:
            self._rt_busy = False
        self.events.put(("messages", rows))

    def _render_messages(self, rows) -> None:
        if rows is None:
            self.v_rt_status.set("读取失败（服务没跑，或接口不可达）")
            return
        # 每 3 秒重建整表。保留选中行，否则用户刚点中的那一行会被刷没影。
        sel = self.rt.selection()
        keep = str(sel[0]) if sel else ""
        self.rt.delete(*self.rt.get_children())
        for i, r in enumerate(rows):
            ts = int(r.get("ts") or 0)
            raw = r.get("text") or ""
            # 剥掉 [图片]/[表情] 这类占位符后如果没剩下东西，就把图片的语义/OCR
            # 内容顶上来 —— 纯图片消息的正文是占位符，真正有信息的是识别结果。
            body = prefs_mod.plain_text(raw)
            if not body:
                body = (r.get("vision_text") or r.get("ocr_text") or "").strip()
            if not body:
                body = raw.strip() or "（无文本）"
            body = " ".join(body.split())[:300]
            state = self._media_label(r)
            tags: list[str] = []
            if i % 2:
                tags.append("odd")
            if r.get("at_me"):
                tags.append("atme")
            if state == "语义失败":
                tags.append("failed")
            self.rt.insert(
                "",
                "end",
                iid=str(r.get("id")),
                values=(
                    time.strftime("%m-%d %H:%M:%S", time.localtime(ts)),
                    r.get("group_name") or r.get("group_id") or "",
                    r.get("sender_name") or "",
                    body,
                    state,
                ),
                tags=tuple(tags),
            )
        if keep and self.rt.exists(keep):
            self.rt.selection_set(keep)
        self.v_rt_status.set(f"共 {len(rows)} 条 · 更新于 {time.strftime('%H:%M:%S')}")

    # ---------------- 账单 ----------------

    def refresh_billing(self) -> None:
        """让服务去 DeepSeek 抓一次真实账单（余额 + 逐日消费）。"""
        if self.proc is None:
            messagebox.showinfo(
                "服务未运行",
                "账单是服务去抓的，请先启动服务。\n\n"
                "（余额用 API Key 查；消费明细需要平台 token，见设置页。）",
                parent=self.root,
            )
            return
        port = int(self.cfg["onebot"]["http_port"])
        self.status.set("正在向 DeepSeek 抓取真实账单…")

        def work() -> None:
            try:
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with op.open(f"http://127.0.0.1:{port}/billing", timeout=90) as r:
                    body = json.loads(r.read().decode("utf-8"))
                if body.get("ok"):
                    self.events.put(("billing", body))
                else:
                    self.events.put(("note", f"账单抓取失败：{body.get('error')}"))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("note", f"账单抓取失败：{exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _apply_billing(self, b: dict) -> None:
        if not b:
            # 服务刚起来还没抓过：只显示本地估算，并说清这是估算
            self.v_total.set("—")
            self.v_balance.set("—")
            return
        src = "平台" if b.get("source") == "platform" else "估算"
        self.v_cost.set(f"¥{float(b.get('today_cny') or 0):.2f}（{src}）")
        tot = f"¥{float(b.get('total_cny') or 0):.2f}（{src}）"
        if b.get("source") == "platform" and b.get("from_day"):
            tot += f" 自 {b['from_day']}"
        self.v_total.set(tot)

        bal = b.get("balance") or {}
        if bal.get("ok"):
            self.v_balance.set(f"¥{float(bal.get('total') or 0):.2f}")
        else:
            err = str(bal.get("error") or "不可用")
            self.v_balance.set(f"—（{err[:26]}{'…' if len(err) > 26 else ''}）")

        err = str(b.get("platform_error") or "")
        if err and b.get("source") != "platform":
            # 拿不到平台数据时必须说清原因，否则用户只会觉得「数字不对但不知道为啥」
            self.v_cost.set(f"¥{float(b.get('today_cny') or 0):.2f}（估算）")
            self.status.set(f"账单：{err}")

    # ---------------- 协议端 ----------------

    def napcat_script(self) -> Path | None:
        """协议端启动脚本的绝对路径；config.toml 没配置时返回 None。"""
        raw = str((self.cfg.get("napcat") or {}).get("start_script") or "").strip()
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_absolute() else ROOT / p

    def start_napcat(self) -> None:
        """拉起协议端（NapCat）。

        默认走**隐藏模式**：不再往任务栏里塞一个永久挂着的 CMD 黑框。
        那个黑框的成因是包装脚本末尾的 ``pause`` —— 真正的启动动作（向 QQ 注入
        DLL）几秒就结束了，窗口却会一直留着。隐藏模式用 wscript 以窗口样式 0
        拉起 launcher.bat：全程无窗口，失败会弹消息框，不会静默。

        UAC 弹窗仍然会有：注入 DLL 必须是管理员，这是设计使然，不是缺陷。
        想看完整输出就在设置页取消勾选「隐藏控制台窗口启动」。
        """
        visible = not bool(self.v_napcat_hidden.get())
        self._log_ui(f"[界面] 正在启动协议端（{'可见' if visible else '隐藏'}模式）…\n")
        self.status.set("已请求启动 NapCat —— 请在 UAC 窗口点「是」，再完成扫码登录")
        try:
            r = napcat_mod.launch(self.cfg, visible=visible)
        except Exception as exc:  # noqa: BLE001
            self._log_ui(f"[界面] 启动协议端异常：{exc}\n")
            messagebox.showerror("启动失败", str(exc), parent=self.root)
            return
        if not r.get("ok"):
            self._log_ui(f"[界面] 启动协议端失败：{r.get('error')}\n")
            messagebox.showerror("启动失败", str(r.get("error") or "未知错误"), parent=self.root)
            return
        self._log_ui(f"[界面] {r.get('note') or ''}\n")
        self._log_ui(f"[界面] 启动记录：{r.get('log')}（界面「协议端日志」按钮可查看）\n")
        self._log_ui(
            "[界面] NapCat 要先注入 QQ 再登录，通常需要 10~30 秒。\n"
            "[界面] 连上后上方「协议端(NapCat)」会自己变成「已连接」，无需重启服务。\n"
        )
        if self.napcat_dir():
            self.qr_watching = True
            threading.Thread(target=self._watch_qrcode, daemon=True).start()

    def show_napcat_log(self) -> None:
        """协议端日志窗口：我们的启动记录（含预检）+ NapCat 自己的日志尾部。"""
        text = napcat_mod.log_tail(self.cfg)
        win = self.napcat_log_win
        if win is not None:
            try:
                if win.winfo_exists():
                    body = getattr(win, "body", None)
                    if body is not None:
                        body.configure(state="normal")
                        body.delete("1.0", "end")
                        body.insert("1.0", text)
                        body.configure(state="disabled")
                    win.lift()
                    return
            except Exception:  # noqa: BLE001  窗口可能已被销毁
                pass

        win = tk.Toplevel(self.root)
        win.title("协议端日志")
        win.geometry("940x560")
        body = ScrolledText(
            win,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            background=LOG_BG,
            foreground=LOG_FG,
            insertbackground=LOG_FG,
            padx=10,
            pady=8,
            relief="flat",
        )
        body.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        body.configure(state="normal")
        body.insert("1.0", text)
        body.configure(state="disabled")
        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(
            bar, text=f"启动记录：{napcat_mod.log_path(self.cfg)}", style="Hint.TLabel"
        ).pack(side="left")
        ttk.Button(
            bar,
            text="打开记录目录",
            command=lambda: open_path(napcat_mod.log_path(self.cfg).parent),
        ).pack(side="right")
        win.body = body  # type: ignore[attr-defined]
        self.napcat_log_win = win

    # ---------------- 登录二维码 ----------------

    def napcat_dir(self) -> Path | None:
        """NapCat 安装目录（config.toml 的 [napcat] dir），未配置返回 None。"""
        raw = str((self.cfg.get("napcat") or {}).get("dir") or "").strip()
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_absolute() else ROOT / p

    def _watch_qrcode(self) -> None:
        """子线程：盯住 NapCat 的登录二维码文件。

        NapCat 需要登录时会把二维码写到「安装目录/cache/qrcode.png」就完事了，
        从不主动展示 —— 用户原本得自己去文件管理器里翻。这里检测到新二维码
        就把路径丢进事件队列，交给主线程弹窗。
        """
        d = self.napcat_dir()
        if d is None:
            return
        qr = d / "cache" / "qrcode.png"
        seen = 0.0
        while self.qr_watching and not self.closing:
            try:
                mtime = qr.stat().st_mtime
            except OSError:
                mtime = 0.0
            # 只认 10 分钟内更新过的码：过期文件弹出来是误导
            if mtime and mtime != seen and (time.time() - mtime) < 600:
                seen = mtime
                self.events.put(("qrcode", str(qr)))
            time.sleep(1.0)

    def _show_qrcode(self, path: str) -> None:
        """主线程：弹出扫码窗口。同一时刻只保留一个。"""
        self._close_qrcode()
        try:
            from PIL import Image, ImageTk  # Pillow 是 rapidocr 的既有依赖

            img = Image.open(path)
            img.thumbnail((340, 340))
            photo = ImageTk.PhotoImage(img)
        except Exception as exc:  # noqa: BLE001
            self._log_ui(f"[界面] 二维码读取失败：{exc}\n")
            return

        win = tk.Toplevel(self.root)
        win.title("扫码登录 QQ")
        win.attributes("-topmost", True)
        ttk.Label(win, text="请用手机 QQ 扫描下方二维码完成登录", font=(FONT, 11)).pack(
            padx=20, pady=(16, 8)
        )
        ttk.Label(win, image=photo).pack(padx=20, pady=4)
        ttk.Label(
            win, text="登录成功后本窗口会自动关闭；也可以直接关掉它", style="Hint.TLabel"
        ).pack(pady=(6, 14))
        win.photo = photo  # 必须挂在控件上，否则被 GC 后图像变空白
        self.qr_win = win

    def _close_qrcode(self) -> None:
        """关掉扫码窗口（协议端连上后自动调用）。"""
        win = self.qr_win
        if win is None:
            return
        try:
            win.destroy()
        except Exception:  # noqa: BLE001  窗口可能已被用户手动关闭
            pass
        self.qr_win = None

    # ---------------- 健康轮询 ----------------

    def _tick_health(self) -> None:
        if not self.health_busy and self.proc is not None:
            self.health_busy = True
            threading.Thread(target=self._fetch_health, daemon=True).start()
        elif self.proc is None:
            self.v_peer.set("—")
            self.v_stored.set("—")
            self.v_vision.set("—")
            self.v_cost.set("—")
            self.v_total.set("—")
            self.v_balance.set("—")
            self.v_llm.set("—")
        self.root.after(2000, self._tick_health)

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
            self._rt_busy = True
            self._rt_limit = self._read_rt_limit()
            threading.Thread(target=self._fetch_messages, args=(self._rt_limit,), daemon=True).start()
        self.root.after(3000, self._tick_realtime)

    def _fetch_health(self) -> None:
        port = int(self.cfg["onebot"]["http_port"])
        data = None
        try:
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(f"http://127.0.0.1:{port}/health", timeout=2.5) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            data = None
        finally:
            self.health_busy = False
        self.events.put(("health", data))

    # ---------------- 事件循环 ----------------

    def _drain(self) -> None:
        for _ in range(300):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break

            if kind == "line":
                self._log_ui(value)
            elif kind == "note":
                self._log_ui(f"[界面] {value}\n")
                self.status.set(value)
            elif kind == "health":
                self._apply_health(value)
            elif kind == "billing":
                self._apply_billing(value)
                self.status.set("账单已刷新")
            elif kind == "messages":
                self._render_messages(value)
            elif kind == "qrcode":
                self._show_qrcode(value)
            elif kind == "exit":
                if self.proc is not None and self.proc.stdout:
                    try:
                        self.proc.stdout.close()
                    except OSError:
                        pass
                self.proc = None
                self.v_service.set("未运行")
                self.btn_start.configure(state="normal")
                self.btn_stop.configure(state="disabled")
                self._log_ui(f"\n[界面] 服务进程已退出（退出码 {value}）\n")
                self.status.set("服务已停止" if value == 0 else f"服务异常退出（{value}），请看日志")
                if self.closing:
                    self.root.destroy()
                    return

        self.root.after(100, self._drain)

    def _apply_health(self, data: dict | None) -> None:
        if data is None:
            self.v_peer.set("未连接（服务响应超时）")
            return
        peers = data.get("ws_peers") or []
        if peers:
            self.v_peer.set(f"已连接 · {', '.join(peers)}")
            self.qr_watching = False  # 已连上，没必要再盯着二维码
            self._close_qrcode()
        elif self.napcat_script() or self.napcat_dir():
            self.v_peer.set("未连接 —— 请点「启动协议端(NapCat)」")
        else:
            self.v_peer.set("未连接（NapCat 未启动）")

        c = data.get("counters") or {}
        s = data.get("stats") or {}
        self.v_stored.set(
            f"{s.get('messages', 0)} 条"
            f"（本次运行新收 {c.get('received', 0)}，"
            f"其中已过滤 {c.get('filtered', 0)}）"
        )
        n_fail = int(s.get("vision_failed", 0) or 0)
        n_retry = int(s.get("vision_retry", 0) or 0)
        if not n_fail:
            self.v_vision.set("0 张")
        elif n_retry:
            self.v_vision.set(f"{n_fail} 张（{n_retry} 张会自动补跑，最多 3 次）")
        else:
            self.v_vision.set(f"{n_fail} 张（已试满 3 次，不再重试）")

        # 账单：优先用服务抓到的真实数据；没抓到就退回本地估算，并标明是估算。
        budget = float(data.get("budget_cny") or 0)
        b = data.get("billing") or {}
        est_today = float(data.get("spent_today_cny") or 0)
        if not b:
            self.v_cost.set(f"¥{est_today:.4f}（估算）/ 预算 ¥{budget:.2f}")
            self.v_total.set(f"¥{est_today:.4f}（估算）")
            self.v_balance.set("（服务启动后自动抓取）")
        else:
            self._apply_billing(b)

        if not data.get("llm_available"):
            self.v_llm.set("未配置 API Key（仅规则模式，不生成摘要）")
        elif data.get("vision_supported"):
            self.v_llm.set("正常 · 支持图片输入")
        else:
            self.v_llm.set("正常 · 图片走本地 OCR")

    def _log_ui(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        # 只裁剪控件里的早期文本，服务侧的日志与数据库不受影响
        if int(self.log.index("end-1c").split(".")[0]) > 3000:
            self.log.delete("1.0", "501.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------- 关闭 ----------------

    def on_close(self) -> None:
        self.qr_watching = False  # 先停掉二维码监视线程，再关界面
        self.v_rt_auto.set(False)  # 停掉实时消息轮询
        if self.proc is None:
            self.root.destroy()
            return
        if not messagebox.askyesno(
            "服务仍在运行",
            "服务进程还在运行。\n\n是否停止服务并关闭界面？",
            parent=self.root,
        ):
            return
        self.closing = True
        self.stop_service()
        self.status.set("正在等待服务安全退出后关闭…")
        # 若 10 秒内没退干净，给出明确提示，而不是让窗口卡住
        self.root.after(10000, self._force_close_hint)

    def _force_close_hint(self) -> None:
        if self.proc is not None and self.closing:
            self.status.set("服务仍在退出中…若长时间无响应，可在任务管理器结束 python.exe")


def open_url(url: str) -> None:
    """用系统默认浏览器打开链接。失败就静默 —— 不该因为打不开网页而打断任何操作。"""
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", url])
    except Exception:  # noqa: BLE001
        pass


def open_path(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:  # noqa: BLE001
        pass


def main(config_path: str | None = None) -> int:
    root = tk.Tk()
    App(root, config_path)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
