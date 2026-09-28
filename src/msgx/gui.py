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
import re
import sqlite3
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.parse import urlencode, urlsplit

from . import __version__, DISPLAY_VERSION
from . import napcat as napcat_mod
from . import prefs as prefs_mod
from . import api_settings, credentials, security, summary_review
from .config import ROOT, abs_path, load
from .summarize import BEIJING, DEFAULT_DIGEST_PROMPT, DIGEST_DEFAULT_CHARS, summary_chars
from .usage_chart import daily_series, day_description, draw_daily_costs

APP_TITLE = "消息萃取 msgx"
FONT = "Microsoft YaHei UI"
CREATE_NO_WINDOW = 0x08000000  # Windows: 子进程不弹控制台窗口

# 日志框配色：沿用终端的深底浅字，长时间盯着不刺眼
LOG_BG = "#1b1f27"
LOG_FG = "#d6dbe5"
# 行级颜色：暗底上要够亮才看得清。红 = 出问题，绿 = 操作成功，白 = 普通运行。
LOG_RED = "#ff7b72"
LOG_GREEN = "#7ee787"
OK_COLOR = "#1a7f37"
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
        self._rt_rows: dict[str, dict] = {}
        self._rt_actions: set[str] = set()
        self._summary_busy = False
        self._summary_started = 0.0
        self._summary_path = ""
        self._summary_days_busy = False
        self._summary_days_checked = 0.0
        self._report_history_busy = False
        self._report_history_checked = 0.0
        self._report_history_seq = 0
        self._report_pending_path = ""
        self._report_items: dict[str, dict] = {}
        self._report_content = ""
        self._report_identity = None
        self._report_outline: list[str] = []
        self._usage_busy = False
        self._billing_busy = False
        self._usage_items = {}
        self._usage_sort = {}
        self._usage_numbers = {}
        self._login_busy = False
        self._login_verify_since = 0.0
        self._login_pending = False
        self._login_account = ""
        self._login_wait_noted = False
        self._protocol_state = {}
        # 登录二维码弹窗。Tkinter 只能在主线程碰控件，所以监视线程只把路径
        # 丢进 events 队列，由 _drain 在主线程里弹窗。
        self.qr_win: tk.Toplevel | None = None
        # 二维码监视的三个配套状态（v0.5.6）：
        #   qr_gen          —— 监视线程的「换代号」。每次 _start_qrcode_watch 都 +1，
        #                      旧线程发现代数对不上就自行退出。此前用「在跑就不重开」的
        #                      单例守卫，重置时的新基线请求会被旧线程吞掉。
        #   仅目标账号在本次扫码后收到的新消息可以关闭二维码；连接和心跳不能代替它。
        #   qr_shown_mtime  —— 已成功展示的二维码 mtime。展示失败（图片正被
        #                      NapCat 重写、读到半张 PNG）就不推进监视基线，
        #                      下一秒同一张码再试，直到弹出来为止。
        self.qr_gen = 0
        self.qr_started_at = 0.0
        self.qr_shown_mtime = 0.0
        self._qr_fail_logged = 0.0
        self.qr_watching = False
        self._trace_gui(f"启动 version={__version__} python={sys.executable} napcat={self.napcat_dir()}")

        self._group_ids: list[str] = []
        self._build()
        self._load_into_widgets()
        self.refresh_groups()
        self.root.after(0, self.refresh_report_history)
        self.root.after(100, self._drain)
        self.root.after(800, self._tick_health)
        self.root.after(1200, self._tick_realtime)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- 界面构建 ----------------

    def _build(self) -> None:
        self.root.title(f"{APP_TITLE}  v{DISPLAY_VERSION}")
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
        self._build_usage_tab(nb)
        self._build_report_tab(nb)

        self.status = tk.StringVar(value="就绪")
        ttk.Label(outer, textvariable=self.status, foreground="#245cc4").pack(
            anchor="w", pady=(10, 0)
        )

    # ---- 标签页 1：运行状态 ----

    def _build_status_tab(self, nb: ttk.Notebook) -> None:
        page = ttk.Frame(nb)
        nb.add(page, text="运行状态")
        canvas = tk.Canvas(page, highlightthickness=0)
        scrollbar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        tab = ttk.Frame(canvas, padding=14)
        window = canvas.create_window((0, 0), window=tab, anchor="nw")
        def resize_status(event) -> None:
            canvas.itemconfigure(window, width=event.width, height=max(780, event.height))
            canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.bind("<Configure>", resize_status)
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))

        card = ttk.LabelFrame(tab, text="服务状态", padding=14)
        card.pack(fill="x")

        self.v_service = tk.StringVar(value="未运行")
        self.v_peer = tk.StringVar(value="—")
        self.v_stored = tk.StringVar(value="—")
        self.v_cost = tk.StringVar(value="—")
        self.v_total = tk.StringVar(value="—")
        self.v_balance = tk.StringVar(value="—")
        self.v_llm = tk.StringVar(value="—")

        # 两列并排。用户的原话：「出了今日花费，总花费呢，不应该写在进入花费的右边吗」——
        # 所以这两个必须是同一行的左右邻居，而不是上下两行。
        rows = [
            [("服务进程", self.v_service), ("协议端(NapCat)", self.v_peer)],
            [("已入库消息", self.v_stored)],
            [("今日花费", self.v_cost), ("累计花费", self.v_total)],
            [("账户余额", self.v_balance)],
            [("模型状态", self.v_llm)],
        ]
        for r, pair in enumerate(rows):
            for c, (key, var) in enumerate(pair):
                col = c * 2
                ttk.Label(card, text=key, style="CardKey.TLabel").grid(
                    row=r, column=col, sticky="w", padx=(0 if col == 0 else 28, 10), pady=3
                )
                if key == "账户余额":
                    balance_actions = ttk.Frame(card)
                    balance_actions.grid(row=r, column=1, columnspan=3, sticky="w", pady=3)
                    ttk.Label(balance_actions, textvariable=var, style="Card.TLabel").pack(side="left")
                    self.btn_billing = ttk.Button(balance_actions, text="刷新账单", command=self.refresh_billing)
                    self.btn_billing.pack(side="left", padx=8)
                    ttk.Button(balance_actions, text="官方平台", command=self._open_billing_platform).pack(side="left")
                else:
                    ttk.Label(card, textvariable=var, style="Card.TLabel").grid(
                        row=r, column=col + 1, columnspan=3 if key == "模型状态" else 1,
                        sticky="w", pady=3
                    )
        card.columnconfigure(1, weight=1)
        card.columnconfigure(3, weight=1)

        self.v_protocol_detail = tk.StringVar(value="")
        ttk.Label(card, textvariable=self.v_protocol_detail, wraplength=900).grid(
            row=len(rows), column=0, columnspan=4, sticky="w", pady=(6, 0)
        )
        self.btn_relogin = ttk.Button(card, text="重新扫码", command=self.relogin_napcat)
        self.btn_relogin.grid(row=len(rows) + 2, column=0, sticky="w")
        self.btn_relogin.grid_remove()

        service_actions = ttk.Frame(card)
        service_actions.grid(row=len(rows) + 1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.btn_start = ttk.Button(service_actions, text="启动服务", command=self.start_service)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(service_actions, text="停止服务", command=self.stop_service, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.btn_restart = None
        if self.napcat_script() or self.napcat_dir():
            self.btn_restart = ttk.Button(service_actions, text="强制重启", command=self.restart_napcat)
            self.btn_restart.pack(side="left")

        # ---- 摘要操作 ----
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(12, 4))

        yesterday = (datetime.now(BEIJING).date() - timedelta(days=1)).isoformat()
        ttk.Label(buttons, text="摘要日期：").pack(side="left", padx=(0, 4))
        self.v_summary_start = tk.StringVar(value=yesterday)
        self.v_summary_end = tk.StringVar(value=yesterday)
        start_entry = ttk.Entry(buttons, textvariable=self.v_summary_start, width=12)
        start_entry.pack(side="left")
        ttk.Label(buttons, text="至").pack(side="left", padx=5)
        end_entry = ttk.Entry(buttons, textvariable=self.v_summary_end, width=12)
        end_entry.pack(side="left")
        for entry in (start_entry, end_entry):
            entry.bind("<FocusOut>", self._on_summary_dates_change)
            entry.bind("<Return>", self._on_summary_dates_change)
        self.btn_summary = ttk.Button(buttons, text="生成摘要", command=self.summary_by_date)
        self.btn_summary.pack(side="left", padx=6)
        self.btn_regenerate = ttk.Button(buttons, text="重新生成", command=lambda: self.summary_by_date(force=True))
        ttk.Button(buttons, text="打开数据目录", command=self.open_data_dir).pack(side="right")

        buttons2 = ttk.Frame(tab)
        buttons2.pack(fill="x")
        self.v_billing_feedback = tk.StringVar(value="账单金额为本地估算")
        ttk.Label(buttons2, textvariable=self.v_billing_feedback, wraplength=640).pack(side="left")

        self.v_range_desc = tk.StringVar(value=self._summary_date_desc())
        ttk.Label(tab, textvariable=self.v_range_desc, style="Hint.TLabel").pack(anchor="w", pady=(3, 0))
        self.v_daily_latest = tk.StringVar(value="最近日报：启动服务后查看")
        ttk.Label(tab, textvariable=self.v_daily_latest, wraplength=940).pack(anchor="w", pady=(3, 0))
        self.v_summary_days = tk.StringVar(value="所选日期：启动服务后查看")
        ttk.Label(tab, textvariable=self.v_summary_days, wraplength=940).pack(anchor="w", pady=(3, 0))

        progress = ttk.Frame(tab)
        progress.pack(fill="x", pady=(6, 0))
        self.summary_bar = ttk.Progressbar(progress, mode="indeterminate", length=110)
        self.summary_bar.pack(side="left")
        self.v_summary = tk.StringVar(value=f"摘要待生成，目标约 {self._summary_options()['target_chars']} 字")
        ttk.Label(progress, textvariable=self.v_summary, wraplength=740).pack(side="left", padx=8)

        # ---- 日志区（v0.5.5：服务日志 / 协议端日志在这里切换，不再弹独立窗口）----
        # 用户的原话：「我希望这个 UI 界面就是所有的程序载体，什么功能都能在上面，
        # 而不需要我去别的地方再找寻浪费时间。」弹窗式日志正好是反面教材 ——
        # 它把信息放到了一个用户不知道何时会出现、还得专门去点的另一个窗口里。
        log_frame = ttk.LabelFrame(tab, text="日志", padding=8)
        log_frame.pack(fill="both", expand=True, pady=(6, 0))
        bar = ttk.Frame(log_frame)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="看哪个").pack(side="left")
        self.v_log_src = tk.StringVar(value="服务日志")
        self.cb_log_src = ttk.Combobox(
            bar, textvariable=self.v_log_src, state="readonly",
            values=["服务日志", "协议端日志"], width=11, font=(FONT, 10),
        )
        self.cb_log_src.pack(side="left", padx=(6, 0))
        self.cb_log_src.bind("<<ComboboxSelected>>", lambda _e: self._switch_log_source())
        ttk.Button(bar, text="刷新", width=5, command=self._refresh_alt_log).pack(
            side="left", padx=6
        )
        ttk.Button(
            bar, text="打开记录目录",
            command=lambda: open_path(napcat_mod.log_path(self.cfg).parent),
        ).pack(side="right")
        self.v_log_hint = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.v_log_hint, style="Hint.TLabel").pack(
            side="right", padx=8
        )
        # 两块**独立**的文本区，切换用 pack / pack_forget —— 这样两边的滚动位置
        # 与历史内容都保得住。做成一个控件 + 清屏重灌的话，切回来服务日志就空了。
        self.log = self._make_log_text(log_frame)
        self.log.pack(fill="both", expand=True)
        self.alt_log = self._make_log_text(log_frame)  # 协议端日志（默认不显示）

    def _summary_date_desc(self) -> str:
        chars = self._summary_options()["target_chars"]
        return f"北京时间自然日（YYYY-MM-DD）；单日目标约 {chars} 字，多日二次归纳会适当加长"

    @staticmethod
    def _make_log_text(parent) -> ScrolledText:
        w = ScrolledText(
            parent,
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
        # 行级颜色：白 = 普通运行，绿 = 操作成功，红 = 出问题了。
        w.tag_configure("warn", foreground=LOG_RED)
        w.tag_configure("good", foreground=LOG_GREEN)
        return w

    # ---- 日志行的颜色判定 ----
    # 用户的原话：「正常状态时白色，一旦进入连接状态收听消息就变成绿色，
    # 一旦出现问题就是红色」。规则必须**可解释**，所以是关键词而不是猜：
    _RED_KEYS = ("WARNING", "ERROR", "CRITICAL", "失败", "异常", "错误", "不再入库", "无法", "未成功", "未能成功")
    _GREEN_KEYS = ("成功", "已连入", "已连接", "已恢复", "已启动", "已生成", "已完成",
                   "已刷新", "已保存", "已入库", "已接收", "已弹出", "清理完成", "引擎就绪",
                   "已从协议端拉取群列表", "账单快照（", "多模态支持: True", "LLM 可用: True")

    @classmethod
    def _log_tag(cls, line: str) -> str:
        """返回 tag 名（'' = 默认白）。**先判红再判绿**：同一行可能两个词都出现
        （如「……消息不再入库」里也有「入库」），出了问题必须压过一切。"""
        # v0.5.8：成功事件也来自界面回调，不能再要求时间戳或只认「连接/入库」。
        match = re.match(r"^\d{2}:\d{2}:\d{2}\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\S+\s+(.*)", line)
        if match and match[1] in ("WARNING", "ERROR", "CRITICAL"):
            return "warn"
        body = match[2] if match else line.removeprefix("[界面]").strip()
        if body.startswith("API 内容摘要 ·"):
            return "good"
        # 这是历史失败任务重新排队的进度，既不是本次失败，也不等于处理成功。
        if body.startswith("已把 ") and "重新排入处理队列" in body:
            return ""
        if any(k in body for k in cls._RED_KEYS):
            return "warn"
        if body.startswith(("正在", "等待", "请在", "请先", "请用", "请点", "请等待", "若", "如果",
                            "连上后", "登录成功后", "已请求")):
            return ""  # 操作说明、等待和已提交请求，不冒充执行成功
        if any(k in body for k in cls._GREEN_KEYS):
            return "good"
        if re.search(r"\bHTTP(?:/\d(?:\.\d)?)?\s+2\d\d\b", body):
            return "good"
        return ""

    def _switch_log_source(self) -> None:
        show_alt = self.v_log_src.get() == "协议端日志"
        if show_alt:
            self.log.pack_forget()
            self.alt_log.pack(fill="both", expand=True)
            self.v_log_hint.set("启动/重置记录 + NapCat 自身日志（每 2 秒自动刷新）")
            self._refresh_alt_log()
        else:
            self.alt_log.pack_forget()
            self.log.pack(fill="both", expand=True)
            self.v_log_hint.set("")

    def _refresh_alt_log(self) -> None:
        """把协议端日志尾部灌进第二块文本区（含预检结果 + NapCat 自身日志）。"""
        if self.v_log_src.get() != "协议端日志":
            return
        text = napcat_mod.log_tail(self.cfg)
        self.alt_log.configure(state="normal")
        self.alt_log.delete("1.0", "end")
        for line in text.splitlines():
            self._append_log_line(self.alt_log, line)
        self.alt_log.see("end")
        self.alt_log.configure(state="disabled")

    @classmethod
    def _append_log_line(cls, widget, line: str) -> None:
        tag = cls._log_tag(line)
        if tag:
            widget.insert("end", line + "\n", tag)
        else:
            widget.insert("end", line + "\n")

    def _on_summary_dates_change(self, _event=None) -> None:
        self._summary_days_checked = 0.0
        self.v_summary_days.set("所选日期：正在查询…")
        self.btn_summary.configure(text="生成摘要")
        self.btn_regenerate.pack_forget()
        self.refresh_summary_days()

    # ---- 标签页 2：实时消息 ----

    def _build_realtime_tab(self, nb: ttk.Notebook) -> None:
        """把「现在收到了什么」搬进界面。

        起因是 NapCat 那个 CMD 黑框：以前有人习惯盯着协议端窗口看消息到底进没进来。
        既然要把黑框干掉，就得在界面里给一个**更好的**替代品 —— 这里能看到
        入库后的真实内容（含文本和图片处理状态），比协议端滚屏有用得多。
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
        self.v_rt_filter = tk.StringVar(value="全部消息")
        ttk.Label(bar, text="内容筛选").pack(side="left", padx=(16, 4))
        image_filter = ttk.Combobox(bar, textvariable=self.v_rt_filter, state="readonly", width=10,
                                    values=("全部消息", "仅看图片"))
        image_filter.pack(side="left")
        image_filter.bind("<<ComboboxSelected>>", lambda _event: self.refresh_messages())
        self.v_rt_status = tk.StringVar(value="尚未加载")
        ttk.Label(bar, textvariable=self.v_rt_status, style="Hint.TLabel").pack(side="right")

        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True)
        cols = ("time", "group", "who", "text", "state", "image", "ocr", "vision", "recognize", "include", "account")
        self.rt = ttk.Treeview(wrap, columns=cols, show="headings", height=18)
        headings = {
            "time": ("时间", 78, "center"),
            "group": ("群", 130, "w"),
            "who": ("发言人", 110, "w"),
            "text": ("内容", 300, "w"),
            "state": ("图片", 70, "center"),
            "image": ("原图链接", 80, "center"),
            "ocr": ("OCR 文字", 180, "w"),
            "vision": ("图片语义", 200, "w"),
            "recognize": ("手动识别", 95, "center"),
            "include": ("纳入摘要", 82, "center"),
            "account": ("接收账号", 125, "w"),
        }
        for key, (title, width, anchor) in headings.items():
            self.rt.heading(key, text=title)
            self.rt.column(key, width=width, anchor=anchor, stretch=(key == "text"))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.rt.yview)
        xsb = ttk.Scrollbar(tab, orient="horizontal", command=self.rt.xview)
        self.rt.configure(yscrollcommand=sb.set, xscrollcommand=xsb.set)
        self.rt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        xsb.pack(fill="x")
        self.rt.bind("<ButtonRelease-1>", self._on_rt_click)
        self.rt.bind("<<TreeviewSelect>>", self._on_rt_select)
        # 交替行色便于阅读；被 @ 的消息按普通消息呈现。
        self.rt.tag_configure("odd", background="#f5f7fa")
        self.rt.tag_configure("failed", foreground="#a15c00")

        ttk.Label(tab, text="点击「打开」查看原图；点「□ 识别」才调用图片 API。"
                         "默认只做 OCR；勾选「纳入摘要」即可使用 OCR，无需先做图片识别。",
                  style="Hint.TLabel").pack(anchor="w", pady=(8, 0))
        self.rt_detail = ScrolledText(tab, height=6, wrap="word", font=(FONT, 9))
        self.rt_detail.pack(fill="x", pady=(5, 0))
        self.rt_detail.configure(state="disabled")

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
                  ("context_tokens", "上下文 Token 上限"), ("max_output_tokens", "输出 Token 上限"),
                  ("currency", "费率币种"), ("input_price", "输入单价 / 百万 Token"),
                  ("cached_price", "缓存输入单价 / 百万 Token"), ("output_price", "输出单价 / 百万 Token"),
                  ("usd_cny", "美元折人民币（估算）"), ("long_context_threshold", "长输入加价阈值（0 关闭）"),
                  ("long_input_multiplier", "长输入价格倍数"), ("long_output_multiplier", "长输出价格倍数"),
                  ("peak_multiplier", "高峰价格倍数"), ("peak_windows_utc", "工作日高峰时段（UTC）")]
        choices = {"protocol": ("responses", "chat_completions"), "currency": ("USD", "CNY"),
                   "reasoning": ("none", "low", "medium", "high", "xhigh", "max")}
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
        ttk.Label(api, text="选择配置即保存切换；编辑参数后点击保存。新任务使用所选 API，在途摘要仍用原配置。\n"
                  "配置导出不含密钥；不同配置或地址不复用密钥。更换模型请核对容量和费率，汇率为手动估值。",
                  style="Hint.TLabel", wraplength=730).grid(row=row+3, column=0, columnspan=2, sticky="w")

        # --- 摘要 ---
        sm = ttk.LabelFrame(tab, text="手动摘要", padding=12)
        sm.pack(fill="x")
        line = ttk.Frame(sm)
        line.pack(fill="x")
        ttk.Label(line, text="目标字数").pack(side="left")
        self.v_summary_chars = tk.StringVar()
        ttk.Entry(line, textvariable=self.v_summary_chars, width=8).pack(side="left", padx=6)
        ttk.Button(line, text="恢复默认提示词", command=self.reset_summary_prompt).pack(side="right")
        ttk.Label(
            sm,
            text="提示词中的 {{目标字数}} 会自动替换。保存后下一次生成即生效；"
                 "在运行状态页选择统计时间范围后手动生成。",
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
        ttk.Button(row, text="刷新群列表", command=self.refresh_groups).pack(side="left")
        # 「只保留当前账号的群」（v0.5.4）。群表是**历次登录的并集**：换个账号登进来
        # 一次，它的全部群就永久留在库里（实测误登一次主号灌进来 30 个空群），
        # 下拉框里再也找不到自己关心的那几个。这个按钮把不属于当前账号、又一条消息
        # 都没有的群清掉 —— 有消息的一律保留（删了会让那个群的摘要静默失效）。
        ttk.Button(
            row, text="只保留当前账号的群", command=self.prune_groups
        ).pack(side="left", padx=8)
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

        # --- 协议端 ---
        nc = ttk.LabelFrame(tab, text="协议端（NapCat）", padding=12)
        nc.pack(fill="x", pady=(12, 0))
        account_row = ttk.Frame(nc)
        account_row.pack(fill="x", pady=(0, 8))
        ttk.Label(account_row, text="目标 QQ 号（本分支填写大号）").pack(side="left")
        self.v_qq_account = tk.StringVar()
        ttk.Entry(account_row, textvariable=self.v_qq_account, width=20).pack(side="left", padx=8)
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
        self._api_profiles, active = api_settings.catalog(self.cfg, p)
        self._show_api_profile(active)
        if "summary_target_chars" not in prefs_mod.load_raw(self.prefs_file):
            p["summary_target_chars"] = self.cfg.get("summary", {}).get("target_chars", DIGEST_DEFAULT_CHARS)
        options = self._summary_options()
        self.txt_summary_prompt.delete("1.0", "end")
        self.txt_summary_prompt.insert("1.0", options["prompt"])
        self.v_summary_chars.set(str(options["target_chars"]))
        self.v_range_desc.set(self._summary_date_desc())
        if not self._summary_busy:
            self.v_summary.set(f"摘要待生成，目标约 {options['target_chars']} 字")

        f = p.get("filters") or {}
        for key, var in self.v_filters.items():
            var.set(bool(f.get(key, False)))
        self.v_minlen.set(str(int(f.get("min_text_length") or 0)))
        self.v_qq_account.set(str(p.get("qq_account") or (self.cfg.get("napcat") or {}).get("account") or ""))

        self.v_napcat_hidden.set(
            bool(
                prefs_mod.load_raw(self.prefs_file).get(
                    "napcat_hidden", (self.cfg.get("napcat") or {}).get("hidden", True)
                )
            )
        )
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
                "skip_face_only": bool(self.v_filters["skip_face_only"].get()),
                "skip_image_only": bool(self.v_filters["skip_image_only"].get()),
                "skip_forward": bool(self.v_filters["skip_forward"].get()),
                "skip_record": bool(self.v_filters["skip_record"].get()),
                "min_text_length": minlen,
            },
            "napcat_hidden": bool(self.v_napcat_hidden.get()),
            "qq_account": account,
            "ui": self.prefs.get("ui", {}),
        }

    def save_prefs(self, silent: bool = False) -> bool:
        try:
            data = self._collect()
        except ValueError as exc:
            if not silent:
                self.status.set(f"设置有误：{exc}")
                self._log_ui(f"[界面] 设置有误：{exc}\n")
            return False
        try:
            if self.v_api_key.get().strip():
                credentials.save(data["api"]["base_url"], self.v_api_key.get(), profile_id=self._api_selected)
                security.register(self.v_api_key.get().strip())
                self.v_api_key.set("")
            prefs_mod.save(data, self.prefs_file)
        except (OSError, credentials.CredentialError) as exc:
            if not silent:
                self.status.set(f"保存失败：{exc}")
                self._log_ui(f"[界面] 保存失败：{exc}\n")
            return False
        self.prefs = data
        self._api_profiles = data["api_profiles"]
        self.api_selector.configure(values=[p["provider"] for p in self._api_profiles.values()])
        self.api_selector.current(list(self._api_profiles).index(self._api_selected))
        self._api_key_status()
        self.v_range_desc.set(self._summary_date_desc())
        if not self._summary_busy:
            self.v_summary.set(f"摘要待生成，目标约 {data['summary_target_chars']} 字")
        n_groups = len(data["groups"]) or "全部"
        self.status.set(
            f"已保存 · 新任务使用 {data['api']['provider']} / {data['api']['model']} · 摘要约 {data['summary_target_chars']} 字 · 群 {n_groups}"
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
        self.status.set("新配置待填写并保存；未保存前继续使用原配置。密钥需单独设置。")

    def _api_key_status(self) -> None:
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
            self.status.set("此地址的密钥已删除；在途请求可能继续完成，后续请求不再使用该密钥")
        except (ValueError, credentials.CredentialError) as exc:
            self.status.set(str(exc))

    def import_api_config(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("API 配置", "*.json")])
        if not path:
            return
        try:
            profile = api_settings.validate(json.loads(Path(path).read_text(encoding="utf-8")))
            for key, var in self.v_api.items():
                var.set(str(profile[key]))
            self.v_api_key.set("")
            self._api_key_status()
            self.status.set("API 配置已载入，点击保存后生效；密钥需单独配置")
        except (OSError, ValueError, TypeError):
            self.status.set("导入失败：配置格式无效或含不允许的字段；请勿在配置文件中填写密钥")

    def export_api_config(self) -> None:
        try:
            profile = api_settings.validate({key: var.get() for key, var in self.v_api.items()})
            path = filedialog.asksaveasfilename(defaultextension=".json", initialfile="api-config.json")
            if path:
                Path(path).write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
                self.status.set("API 配置已导出，不含密钥")
        except (OSError, ValueError):
            self.status.set("导出失败：请检查配置或文件写入权限")

    def reload_prefs(self) -> None:
        self.prefs = prefs_mod.load(self.prefs_file)
        self._load_into_widgets()
        self.status.set("已重新加载设置")

    def open_prefs_file(self) -> None:
        if not self.prefs_file.exists():
            self.save_prefs(silent=True)
        open_path(self.prefs_file.parent)

    # ---------------- 群列表 ----------------

    def refresh_groups(self, prune: bool = False) -> dict:
        """刷新群列表。``prune=True`` 时同时请服务删掉别的账号留下的空群。

        先让服务经协议端调 get_group_list 拉真实群名（NapCat 就在线上，
        没理由显示「(未命名)」）；服务没跑或协议端不在线时退回本地库。
        返回服务端给的元信息（``uin`` / ``deleted`` / ``protected``），失败返回 ``{}``。
        """
        fetched: dict = {}
        if self.proc is not None:
            try:
                port = int(self.cfg["onebot"]["http_port"])
                url = f"http://127.0.0.1:{port}/groups/refresh"
                if prune:
                    url += "?prune=1"
                req = urllib.request.Request(url, data=b"", method="POST")
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with op.open(req, timeout=20) as r:
                    fetched = json.loads(r.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                fetched = {}
        saved = set(self.prefs.get("groups") or [])
        uin = str(fetched.get("uin") or "")
        self._group_ids = []
        rows: list = []
        self.lst_groups.delete(0, "end")
        try:
            db = abs_path(self.cfg, "db_path")
            if db.exists():
                conn = sqlite3.connect(str(db), timeout=3)
                try:
                    owners = "g.bot_uin"
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='group_accounts'").fetchone():
                        owners = "(SELECT GROUP_CONCAT(account) FROM group_accounts WHERE group_id=g.id)"
                    rows = list(
                        conn.execute(
                            f"SELECT g.id, g.name, {owners},"
                            " (SELECT COUNT(*) FROM messages m WHERE m.group_id = g.id)"
                            " FROM groups g ORDER BY g.id"
                        )
                    )
                finally:
                    conn.close()
                for gid, name, owner, n in rows:
                    gid, name, owner = str(gid), str(name or ""), str(owner or "")
                    self._group_ids.append(gid)
                    label = f"{name}  ({gid})" if name else f"(未命名)  {gid}"
                    # 属主看板：只有在「知道当前账号」且「这个群记的是别的账号」时才标。
                    # 属主为空 = 未知（老数据），不当成「别人的群」——那会误标一大片。
                    if owner:
                        label += f"   接收账号：{owner}"
                    if uin and owner and uin not in owner.split(","):
                        label += "   · 历史群"
                    elif n == 0:
                        label += "   · 0 条"
                    self.lst_groups.insert("end", label)
        except Exception as exc:  # noqa: BLE001
            self.status.set(f"读取群列表失败：{exc}")
            return fetched
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
        return fetched

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
        r = self.refresh_groups(prune=True)
        if not r:
            self.status.set("清理失败：服务不可达或协议端未连接（群列表未改动）")
            return
        deleted = r.get("deleted") or []
        protected = r.get("protected") or []
        if deleted:
            self._log_ui(f"[界面] 已清理 {len(deleted)} 个不属于当前账号的空群：{', '.join(deleted)}\n")
        if protected:
            self._log_ui(f"[界面] 保留（有消息，未删）：{', '.join(protected)}\n")

    # ---------------- 消费明细与摘要全文（v0.6.0） ----------------

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
        ttk.Button(tab, text="刷新消费明细", command=self.refresh_usage).pack(anchor="w")
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
                                   (125, 90, 100, 65, 65, 90, 125, 115, 145, 160, 105, 80, 80)):
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
            return
        if self.proc is None:
            self.v_usage.set("请启动服务后刷新；旧版每日合计无法还原到具体消息。")
            return
        self._usage_busy = True
        port = int(self.cfg["onebot"]["http_port"])
        def work() -> None:
            try:
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with op.open(f"http://127.0.0.1:{port}/usage/details", timeout=10) as response:
                    self.events.put(("usage", json.loads(response.read().decode("utf-8"))))
            except Exception as exc:
                self.events.put(("usage", {"error": str(exc)}))
        threading.Thread(target=work, daemon=True).start()

    def _render_usage(self, data: dict) -> None:
        self._usage_busy = False
        if data.get("error"):
            self.v_usage.set(f"消费明细读取失败：{security.redact(str(data['error']))}")
            return
        self.v_usage.set(f"当前用途总金额 ¥{data.get('visible_cost_cny', 0):.6f}；今日消费 ¥{data.get('today_cost_cny', 0):.6f}。"
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
                f"{row['cost_usd']:.6f}" if row.get("cost_usd") is not None else "未知", row.get("service_tier") or "未记录",
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
        self.usage_detail.configure(state="disabled")
        port = int(self.cfg["onebot"]["http_port"])
        def work() -> None:
            try:
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with op.open(f"http://127.0.0.1:{port}/usage/calls/{iid}", timeout=10) as response:
                    data = json.loads(response.read().decode("utf-8"))
                text = f"共关联 {data['total']} 条消息，最多显示前 100 条；已清理的原消息无法展示。\n"
                text += "\n\n".join(f"群 {r['group_id']} · {r['sender_name']} · 消息 {r['msg_id']}（#{r['id']}）\n{r['text']}" for r in data["items"])
            except Exception as exc:
                text = f"关联消息读取失败：{exc}"
            self.events.put(("usage_sources", (iid, text)))
        threading.Thread(target=work, daemon=True).start()

    def _build_report_tab(self, nb: ttk.Notebook) -> None:
        self._report_current = None
        self._report_review = None
        self._report_review_updates = {}
        self._report_notes_saved = ""
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
        ttk.Label(bar, text="最近摘要", font=(FONT, 11, "bold")).pack(side="left")
        self.btn_report_refresh = ttk.Button(bar, text="刷新", command=self.refresh_report_history)
        self.btn_report_refresh.pack(side="right")
        self.v_report_filter = tk.StringVar(value="全部")
        report_filter = ttk.Combobox(history, textvariable=self.v_report_filter, state="readonly",
                                    values=("全部", "仅日报", "仅汇总"), width=12)
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
                                         command=lambda: open_path(Path(self._summary_path)))
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
                   command=self._save_report_notes).pack(side="right", anchor="n", padx=(6, 0))
        self.report_notes = ScrolledText(self.report_notes_frame, wrap="word", height=2, font=(FONT, 10))
        self.report_notes.pack(side="left", fill="x", expand=True)
        self.v_report_review = tk.StringVar(value="标注与阅读状态保存在本地，不调用 API。")
        self.report_review_label = ttk.Label(reader, textvariable=self.v_report_review, style="Hint.TLabel", wraplength=620)
        self.report_review_label.pack(side="bottom", fill="x", pady=(4, 0), before=self.report_text)
        self.nb.bind("<<NotebookTabChanged>>", self._report_tab_changed, add="+")

    def _report_tab_changed(self, _event=None) -> None:
        if self.nb.select() != str(self.report_tab):
            self._save_report_notes()
        if self.nb.select() == str(self.report_tab) and time.time() - self._report_history_checked >= 30:
            self.refresh_report_history()

    def _report_filter_changed(self, _event=None) -> None:
        if not self._save_report_notes():
            self.v_report_filter.set(self._report_query_filter)
            return
        self._report_choose_first = True
        self.refresh_report_history()

    def refresh_report_history(self, preferred_path: str = "") -> None:
        if not self._save_report_notes():
            return
        if self._report_history_busy:
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
        kinds = {"仅日报": ("daily", "daily_partial"), "仅汇总": ("multi", "multi_partial")}.get(
            self._report_query_filter, ("daily", "daily_partial", "multi", "multi_partial"))

        def work() -> None:
            items, error = [], ""
            try:
                if db.exists():
                    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
                    try:
                        conn.row_factory = sqlite3.Row
                        items = [dict(row) for row in conn.execute(
                            "SELECT id,window_start,window_end,kind,content,report_path,msg_count,created_at"
                            " FROM summaries WHERE kind IN (" + ",".join("?" for _ in kinds) + ")"
                            " ORDER BY created_at DESC,id DESC LIMIT 50", kinds)]
                        for item in items:
                            item["review"] = summary_review.get_review(conn, item["id"], item["content"] or "")
                    finally:
                        conn.close()
            except (OSError, sqlite3.Error, ValueError) as exc:
                error = security.redact(str(exc))
            self.events.put(("report_history", (seq, items, error, preferred_path)))
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
        if not self._save_report_notes():
            return
        selected = self.report_history.selection()
        current = selected[0].removeprefix("detail-") if selected else ""
        shown_path = self._summary_path
        kinds = {"仅日报": ("daily", "daily_partial"), "仅汇总": ("multi", "multi_partial")}.get(
            self.v_report_filter.get(), ("daily", "daily_partial", "multi", "multi_partial"))
        self._report_items = {str(item["id"]): item for item in items if item.get("kind") in kinds}
        self.report_history.delete(*self.report_history.get_children())
        chosen = ""
        for iid, item in self._report_items.items():
            content = item.get("content") or ""
            title = next((re.sub(r"^#{1,6}\s*|\*\*", "", line).strip()
                          for line in content.splitlines() if line.strip()), "无标题摘要")
            start = datetime.fromtimestamp(item["window_start"], BEIJING)
            end = datetime.fromtimestamp(item["window_end"], BEIJING)
            daily = item["kind"].startswith("daily")
            partial = item["kind"].endswith("_partial")
            kind = "日报" if daily else "汇总"
            label_end = end if partial else end - timedelta(seconds=1)
            dates = (f"{start:%m-%d}" if daily else f"{start:%m-%d}—{label_end:%m-%d}"
                     if start.year == label_end.year else f"{start:%Y-%m-%d}—{label_end:%Y-%m-%d}")
            suffix = f" · 截至 {end:%m-%d %H:%M}，未完成" if partial else ""
            review = self._report_review_updates.get((item["id"], summary_review.content_hash(content)), item.get("review", {}))
            item["review"] = review
            state = review.get("read_state", "unread")
            label = {"unread": "未读", "skimmed": "粗读", "deep": "深读"}.get(state, "未读")
            self.report_history.insert("", "end", iid=iid, text=f"【{kind}】{dates}{suffix} · {title}",
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
        self.v_report_history.set(f"最近 {count} 份 · 本地读取" if count else "暂无此类摘要；旧版摘要已隐藏")
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
        if not unchanged and not self._save_report_notes():
            old_id = str((self._report_current or {}).get("id", ""))
            if self.report_history.exists(old_id):
                self.report_history.selection_set(old_id)
            return False
        selected = self.report_history.selection()
        selected_id = selected[0].removeprefix("detail-") if selected else ""
        selected_item = self._report_items.get(selected_id)
        if selected_item and selected_item.get("id") != item.get("id") and selected_item.get("report_path") != path:
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
        try:
            db = abs_path(self.cfg, "db_path")
            conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
            try:
                review = summary_review.get_review(conn, item["id"], self._report_content)
            finally:
                conn.close()
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.v_report_review.set("批注读取失败：" + security.redact(str(exc)))
            return
        self._report_review = review
        self.v_report_read_state.set(review["read_state"])
        self._report_notes_saved = review["notes"]
        self.report_notes.insert("1.0", review["notes"])
        if item.get("kind", "").startswith("daily"):
            self.report_notes_frame.pack(side="bottom", fill="x", pady=(5, 0), before=self.report_text)
        for button in self._report_review_buttons:
            button.configure(state="normal")
        self._draw_report_annotations(review["annotations"])
        self.v_report_review.set("标注与阅读状态立即保存；打开摘要不会改变阅读状态。")

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

    def _save_report_review(self, **changes) -> bool:
        if not self._report_current or self._report_review is None:
            return False
        try:
            review = summary_review.save_review(
                abs_path(self.cfg, "db_path"), self._report_current["id"],
                self._report_review["content_hash"], **changes)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.v_report_review.set("保存失败，当前编辑已保留：" + security.redact(str(exc)))
            return False
        self._report_review = review
        self._report_review_updates[(self._report_current["id"], review["content_hash"])] = review
        self._report_current["review"] = review
        iid = str(self._report_current["id"])
        state = review["read_state"]
        self.v_report_read_state.set(state)
        if self.report_history.exists(iid):
            self.report_history.item(iid, tags=(state,), values=({"unread": "未读", "skimmed": "粗读", "deep": "深读"}[state],))
        self.v_report_review.set("已保存到本地；下次生成多日汇总将使用当前重点与心得。")
        return True

    def _save_report_notes(self) -> bool:
        if self._report_review is None or not (self._report_current or {}).get("kind", "").startswith("daily"):
            return True
        notes = self.report_notes.get("1.0", "end-1c")
        if notes == self._report_notes_saved:
            return True
        if not self._save_report_review(notes=notes):
            return False
        self._report_notes_saved = notes
        return True

    def _set_report_read_state(self) -> None:
        if self._report_review is None:
            return
        old_state = self._report_review["read_state"]
        if not self._save_report_review(read_state=self.v_report_read_state.get()):
            self.v_report_read_state.set(old_state)

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
        if self._save_report_review(annotations=annotations):
            self._draw_report_annotations(annotations)
        else:
            self._draw_report_annotations(self._report_review["annotations"])

    def _report_jump(self, _event=None) -> None:
        selected = self.report_outline.curselection()
        if selected and selected[0] < len(self._report_outline):
            self.report_text.yview(self._report_outline[selected[0]])

    def _copy_report(self) -> None:
        if self._report_content:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._report_content)
            self.status.set("已复制完整摘要")

    def _summary_finished(self, data: dict) -> None:
        self._summary_busy = False
        self.summary_bar.stop()
        self.btn_summary.configure(state="normal")
        self.btn_regenerate.configure(state="normal")
        if data.get("error"):
            note = f"摘要生成失败：{data['error']}"
        elif not data.get("created"):
            note = "所选日期暂无可生成的摘要"
        else:
            item = data["created"][0]
            if not self._show_report(item):
                self.v_summary.set("摘要已生成；当前心得保存失败，请保存后刷新摘要列表查看。")
                self._summary_days_checked = 0.0
                return
            self.refresh_report_history(preferred_path=item.get("path", ""))
            self.nb.select(self.report_tab)
            note = f"摘要已生成（{item.get('chars', 0)} 字），完整文件：{self._summary_path}"
        self.v_summary.set(note)
        self._log_ui(f"[界面] {note}\n")
        self.status.set(note)
        self._summary_days_checked = 0.0
        self.refresh_summary_days()

    # ---------------- 服务进程 ----------------

    def start_service(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        if not self.save_prefs(silent=False):
            return False
        if self._login_config() is None:
            return False

        try:
            self.stop_file.unlink(missing_ok=True)  # 清掉旧停止信号，否则新进程会立刻退出
        except OSError as exc:
            self.status.set("无法清理旧停止信号，服务未启动；请查看界面日志")
            self._trace_gui(f"清理停止信号失败：{exc}")
            return False
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
        self._trace_gui("请求启动消息服务")
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
            self.status.set(f"服务启动失败：{exc}")
            self._log_ui(f"[界面] 服务启动失败：{exc}\n")
            self._trace_gui(f"服务启动失败：{exc}")
            return False

        self.v_service.set("启动中")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status.set("服务进程已创建，正在等待接收端口就绪…")
        self._trace_gui(f"服务进程已创建 pid={self.proc.pid}")
        threading.Thread(target=self._read_pipe, args=(self.proc,), daemon=True).start()
        return True

    def stop_service(self) -> None:
        if self.proc is None:
            return
        try:
            self.stop_file.touch()
            self._trace_gui("用户请求停止消息服务，已写入停止信号")
        except OSError as exc:
            self.status.set(f"服务停止失败：{exc}")
            self._log_ui(f"[界面] 服务停止失败：{exc}\n")
            return
        self.btn_stop.configure(state="disabled")
        self.status.set("已发出停止信号，正在等待服务安全退出…")

    def _read_pipe(self, proc: subprocess.Popen) -> None:
        """子线程：只把日志丢进队列，绝不碰控件。"""
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                self.events.put(("line", line))
        finally:
            self.events.put(("exit", (proc, proc.wait())))

    def _chosen_summary_dates(self) -> tuple[date, date]:
        raw_start = self.v_summary_start.get().strip()
        raw_end = self.v_summary_end.get().strip()
        if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in (raw_start, raw_end)):
            raise ValueError("摘要日期请按 YYYY-MM-DD 填写")
        try:
            start, end = date.fromisoformat(raw_start), date.fromisoformat(raw_end)
        except ValueError as exc:
            raise ValueError("摘要日期不是有效的日历日期") from exc
        if end < start:
            raise ValueError("结束日期不能早于起始日期")
        if end > datetime.now(BEIJING).date():
            raise ValueError("摘要日期不能晚于今天")
        if (end - start).days >= 31:
            raise ValueError("一次最多选择 31 天")
        return start, end

    def summary_now(self, kind: str = "by_date", hours: int | None = None,
                    start_date: str | None = None, end_date: str | None = None,
                    force: bool = False) -> None:
        if self._summary_busy:
            return
        if not self._save_report_notes():
            self.v_summary.set("当前心得尚未保存，暂未生成摘要；请先保存成功后重试。")
            self.status.set(self.v_summary.get())
            return
        if self.proc is None:
            self.v_summary.set("请先启动服务，再生成摘要。")
            self.status.set(self.v_summary.get())
            return
        if kind == "by_date":
            try:
                start, end = self._chosen_summary_dates() if start_date is None or end_date is None else (
                    date.fromisoformat(start_date), date.fromisoformat(end_date))
            except ValueError as exc:
                self.v_summary.set(str(exc))
                self.status.set(str(exc))
                return
            start_date, end_date = start.isoformat(), end.isoformat()
        self._summary_busy = True
        self._summary_started = time.time()
        self.btn_summary.configure(state="disabled")
        self.btn_regenerate.configure(state="disabled")
        self.summary_bar.start(12)
        options = self._summary_options()
        if kind == "by_date":
            days = (end - start).days + 1
            if days > 1:
                extra, minimum = (1500, 4500) if days <= 3 else (3000, 6000) if days <= 7 else (7000, 10000)
                options["target_chars"] = min(100000, max(options["target_chars"] + extra, minimum))
            target = start_date if days == 1 else f"{start_date} 至 {end_date}"
        else:
            target = f"最近 {hours // 24} 天" if kind == "range" and hours else "增量"
        note = f"开始生成{target}摘要，目标约 {options['target_chars']} 字；正在读取消息并调用已配置的模型…"
        self.v_summary.set(note)
        self.status.set(note)
        self._log_ui(f"[界面] {note}\n")
        port = int(self.cfg["onebot"]["http_port"])

        def work() -> None:
            try:
                url = f"http://127.0.0.1:{port}/summary/now?kind={kind}"
                if kind == "range" and hours:
                    url += f"&hours={int(hours)}"
                if kind == "by_date":
                    options.update({"start_date": start_date, "end_date": end_date, "force": force})
                req = urllib.request.Request(
                    url, data=json.dumps(options, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with op.open(req, timeout=1800) as response:
                    body = json.loads(response.read().decode("utf-8"))
                self.events.put(("summary_done", body))
            except urllib.error.HTTPError as exc:
                try:
                    detail = json.loads(exc.read().decode("utf-8")).get("error") or str(exc)
                except (ValueError, UnicodeError):
                    detail = str(exc)
                self.events.put(("summary_done", {"error": detail}))
            except Exception as exc:
                self.events.put(("summary_done", {"error": str(exc)}))

        threading.Thread(target=work, daemon=True).start()

    def summary_by_date(self, force: bool = False) -> None:
        """按北京时间的自然日生成日报，或复用日报生成多日摘要。"""
        try:
            start, end = self._chosen_summary_dates()
        except ValueError as exc:
            self.v_summary.set(str(exc))
            self.status.set(str(exc))
            return
        self.summary_now("by_date", start_date=start.isoformat(), end_date=end.isoformat(), force=force)

    def refresh_summary_days(self) -> None:
        if self.proc is None or self._summary_days_busy:
            return
        try:
            start, end = self._chosen_summary_dates()
        except ValueError as exc:
            self.v_summary_days.set(str(exc))
            self.btn_regenerate.pack_forget()
            return
        self._summary_days_busy = True
        self._summary_days_checked = time.time()
        threading.Thread(target=self._fetch_summary_days,
                         args=(start.isoformat(), end.isoformat()), daemon=True).start()

    def _fetch_summary_days(self, start: str, end: str) -> None:
        port = int(self.cfg["onebot"]["http_port"])
        url = f"http://127.0.0.1:{port}/summary/days?{urlencode({'start_date': start, 'end_date': end})}"
        try:
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(url, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            data = {"error": str(exc)}
        finally:
            self._summary_days_busy = False
        self.events.put(("summary_days", (start, end, data)))

    def _apply_summary_days(self, start: str, end: str, data: dict) -> None:
        latest = data.get("latest_daily")
        if latest is not None:
            self._show_latest_daily(latest)
        if (self.v_summary_start.get().strip(), self.v_summary_end.get().strip()) != (start, end):
            return
        if data.get("error"):
            self.v_summary_days.set(f"所选日期状态读取失败：{data['error']}")
            return
        rows = {str(row.get("date")): row for row in data.get("days", [])}
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        dates = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
        done = [day for day in dates if rows.get(day, {}).get("status") == "done"]
        empty = [day for day in dates if rows.get(day, {}).get("status") == "empty"]
        partial = [day for day in dates if rows.get(day, {}).get("status") == "partial"]
        missing = [day for day in dates if day not in done and day not in empty and day not in partial]
        if len(dates) == 1:
            day = dates[0]
            state = rows.get(day, {}).get("status")
            labels = {"done": "已完成", "empty": "无消息", "partial": "截至当前的临时摘要"}
            self.v_summary_days.set(f"所选日期 {day}：{labels.get(state, '待生成')}")
            self.btn_summary.configure(text="查看摘要" if state == "done" else "生成摘要")
            if state == "done":
                self.btn_regenerate.pack(side="left", padx=(0, 6))
            else:
                self.btn_regenerate.pack_forget()
        else:
            detail = f"所选 {len(dates)} 天：已完成 {len(done)} 天，无消息 {len(empty)} 天"
            if partial:
                detail += f"，临时摘要 {len(partial)} 天"
            if missing:
                detail += f"；缺少 {len(missing)} 天（{', '.join(missing[:3])}{' 等' if len(missing) > 3 else ''}），点击生成会先补齐"
            self.v_summary_days.set(detail)
            self.btn_summary.configure(text="生成摘要")
            self.btn_regenerate.pack_forget()

    def _show_latest_daily(self, latest: dict | None) -> None:
        yesterday = (datetime.now(BEIJING).date() - timedelta(days=1)).isoformat()
        if latest and str(latest.get("date")) == yesterday and latest.get("status") == "done":
            self.v_daily_latest.set(f"昨日日报 {yesterday}：已完成（{latest.get('chars', 0)} 字）")
        elif latest:
            status = "无消息" if latest.get("status") == "empty" else "待生成"
            self.v_daily_latest.set(f"昨日日报 {yesterday}：{status}")
        else:
            self.v_daily_latest.set(f"昨日日报 {yesterday}：尚未完成")

    def open_data_dir(self) -> None:
        open_path(abs_path(self.cfg, "report_dir").parent)

    # ---------------- 实时消息 ----------------

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
        if self.proc is None:
            self.status.set("服务未运行 —— 启动服务后才能看到实时消息")
            return
        if self._rt_busy:
            return
        self._rt_busy = True
        self._rt_limit = self._read_rt_limit()
        threading.Thread(target=self._fetch_messages,
                         args=(self._rt_limit, self.v_rt_filter.get() == "仅看图片"), daemon=True).start()

    def _read_rt_limit(self) -> int:
        """在主线程里读控件（Tkinter 变量不能在子线程碰）。"""
        try:
            return max(1, min(int(self.v_rt_limit.get() or 80), 300))
        except ValueError:
            return 80

    def _fetch_messages(self, limit: int, images_only: bool = False) -> None:
        port = int(self.cfg["onebot"]["http_port"])
        rows = None
        try:
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(f"http://127.0.0.1:{port}/messages/recent?limit={limit}&images={int(images_only)}", timeout=5) as r:
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
        display_rows = []
        for parent in rows:
            images = parent.get("images") or []
            if images:
                for image in images:
                    item = {**parent, **image, "id": f"{parent['id']}:{image['id']}",
                            "image_id": image["id"], "has_media": 1,
                            "media_state": "filtered" if parent.get("media_state") == "filtered"
                            else image["media_state"]}
                    display_rows.append(item)
            else:
                display_rows.append(parent)
        self._rt_rows = {str(r.get("id")): r for r in display_rows}
        for i, r in enumerate(display_rows):
            ts = int(r.get("ts") or 0)
            raw = r.get("text") or ""
            # 剥掉 [图片]/[表情] 这类占位符后如果没剩下东西，就把图片的语义/OCR
            # 内容顶上来 —— 纯图片消息的正文是占位符，真正有信息的是识别结果。
            body = prefs_mod.plain_text(raw)
            ocr = (r.get("ocr_text") or "").strip()
            vision = (r.get("vision_text") or "").strip()
            if r.get("media_state") == "filtered" and ocr:
                body = "OCR:" + ocr
            elif not body:
                body = (vision or ocr).strip()
            if not body:
                body = raw.strip() or "（无文本）"
            body = " ".join(body.split())[:300]
            state = self._media_label(r)
            tags: list[str] = []
            if i % 2:
                tags.append("odd")
            if state == "图片未识别":
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
                    "打开" if r.get("media_path") else "—",
                    " ".join(ocr.split())[:100] or "—",
                    " ".join(vision.split())[:100] or "—",
                    ("☑ 已识别" if vision else "□ 识别") if r.get("media_path") else "—",
                    ("☑" if r.get("include_vision") else "□") if r.get("has_media") else "—",
                    r.get("receiver_accounts") or r.get("received_by") or "历史未知",
                ),
                tags=tuple(tags),
            )
        if keep and self.rt.exists(keep):
            self.rt.selection_set(keep)
        self._on_rt_select()
        self.v_rt_status.set(f"共 {len(display_rows)} 行 · 更新于 {time.strftime('%H:%M:%S')}")

    def _on_rt_select(self, _event=None) -> None:
        selection = self.rt.selection()
        row = self._rt_rows.get(str(selection[0])) if selection else None
        detail = (f"OCR:\n{row.get('ocr_text') or '（无文字）'}\n\n"
                  f"图片语义：\n{row.get('vision_text') or '（尚未手动识别）'}") if row else ""
        self.rt_detail.configure(state="normal")
        self.rt_detail.delete("1.0", "end")
        self.rt_detail.insert("end", detail)
        self.rt_detail.configure(state="disabled")

    def _on_rt_click(self, event) -> None:
        row_id = self.rt.identify_row(event.y)
        column = self.rt.identify_column(event.x)
        row = self._rt_rows.get(row_id)
        if not row:
            return
        if column == "#6" and row.get("media_path"):
            path = Path(row["media_path"])
            if path.is_file():
                open_path(path)
            else:
                self.status.set("本地图片已不存在")
        elif column == "#9" and row.get("image_id") and row.get("media_path") and not row.get("vision_text"):
            self._start_rt_action(str(row["image_id"]), "vision", {})
        elif column == "#10" and row.get("image_id"):
            self._start_rt_action(str(row["image_id"]), "summary-include", {"include": not bool(row.get("include_vision"))})

    def _start_rt_action(self, row_id: str, action: str, payload: dict) -> None:
        key = f"{row_id}:{action}"
        if key in self._rt_actions:
            return
        self._rt_actions.add(key)
        self.status.set("正在提交图片识别…" if action == "vision" else "正在更新摘要选项…")

        def work() -> None:
            port = int(self.cfg["onebot"]["http_port"])
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/images/{row_id}/{action}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            try:
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with op.open(req, timeout=120) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                try:
                    result = json.loads(exc.read().decode("utf-8"))
                except (ValueError, UnicodeError):
                    result = {"ok": False, "error": f"HTTP {exc.code}"}
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            self.events.put(("message_action", (key, action, result)))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 账单 ----------------

    def _open_billing_platform(self) -> None:
        profile = self.prefs.get("api") or self.cfg.get("api") or self.cfg.get("llm", {})
        host = urlsplit(str(profile.get("base_url") or "")).hostname
        if host == "api.openai.com":
            open_url("https://platform.openai.com/settings/organization/billing/overview")
        elif host == "api.deepseek.com":
            open_url("https://platform.deepseek.com/usage")
        else:
            self.status.set("当前为自定义接口，请到该服务商的官方平台查看账单。")

    def refresh_billing(self) -> None:
        """刷新当前服务的费用统计；新 API 只使用本机请求估算。"""
        if self._billing_busy:
            return
        if self.proc is None:
            self.status.set("请先启动服务，再刷新账单。")
            self.v_billing_feedback.set("请先启动服务，再刷新账单。")
            return
        self._billing_busy = True
        self.btn_billing.configure(state="disabled", text="正在刷新…")
        port = int(self.cfg["onebot"]["http_port"])
        self.v_billing_feedback.set("正在刷新账单与消费明细…")
        self.status.set(self.v_billing_feedback.get())
        self._log_ui("[界面] 开始刷新账单与消费明细…\n")

        def work() -> None:
            try:
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with op.open(f"http://127.0.0.1:{port}/billing", timeout=90) as r:
                    body = json.loads(r.read().decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError("invalid billing response")
                self.events.put(("billing_done", body))
            except urllib.error.HTTPError as exc:
                self.events.put(("billing_done", {"ok": False, "error": f"HTTP {exc.code}"}))
            except TimeoutError:
                self.events.put(("billing_done", {"ok": False, "error": "请求超时（等待上限 90 秒）"}))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("billing_done", {"ok": False, "error": type(exc).__name__}))

        threading.Thread(target=work, daemon=True).start()

    def _billing_finished(self, body: dict) -> None:
        self._billing_busy = False
        self.btn_billing.configure(state="normal", text="刷新账单")
        if body.get("ok"):
            self._apply_billing(body)
            if body.get("source") == "estimate":
                note = "本地估算已刷新；未读取平台账单，实际扣费请查看平台"
            else:
                note = f"账单已刷新：{self.status.get()}"
            self.refresh_usage()
        else:
            detail = security.redact(str(body.get("error") or "服务未返回有效结果"))[:180]
            note = f"账单刷新失败：{detail}；可再次点击重试"
        self.v_billing_feedback.set(note)
        self.status.set(note)
        self._log_ui(f"[界面] {note}\n")

    # 账单口径的显示名。**每个数字都必须带口径**：用户没法从数值本身分辨
    # 它是官方数字还是本地估算，含糊显示等于骗人。
    BILL_SOURCES = {
        "platform": "平台",
        "balance_diff": "余额差值",
        "estimate": "估算",
    }
    # 累计由差值算时，起点**之前**那段没有读数、只能用估算补齐。这时整个数字
    # 「真实 + 估算」拼成，不能标成纯「余额差值」。
    BILL_MIXED = "差值+估算"

    def _bill_origin(self, src: str, mixed: bool = False) -> str:
        if src == "balance_diff" and mixed:
            return self.BILL_MIXED
        return self.BILL_SOURCES.get(str(src or ""), "未知")

    def _apply_billing(self, b: dict) -> None:
        if not b:
            # 服务刚起来还没抓过：只显示本地估算，并说清这是估算
            self.v_total.set("—")
            self.v_balance.set("—")
            return
        bd = b.get("balance_diff") or {}
        # 兼容：老快照只有 source，没有 today_source / total_source
        t_src = str(b.get("today_source") or b.get("source") or "")
        s_src = str(b.get("total_source") or b.get("source") or "")

        today = f"¥{float(b.get('today_cny') or 0):.2f}（{self._bill_origin(t_src)}）"
        # 差值法的「今日」= 当天第一条读数之后的下降量之和。程序当天中途才启动时
        # 起点会明显晚于 00:00，这时必须写出来 —— 否则用户会把半天当成一整天。
        if t_src == "balance_diff" and bd.get("today_from"):
            hhmm = time.strftime("%H:%M", time.localtime(int(bd["today_from"])))
            if hhmm > "00:10":
                today += f" 自 {hhmm} 起"
        self.v_cost.set(today)

        total = (
            f"¥{float(b.get('total_cny') or 0):.2f}"
            f"（{self._bill_origin(s_src, bool(bd.get('mixed')))}）"
        )
        if s_src == "platform" and b.get("from_day"):
            total += f" 自 {b['from_day']}"
        elif s_src == "balance_diff" and bd.get("total_from"):
            total += f" 自 {time.strftime('%m-%d', time.localtime(int(bd['total_from'])))}"
        self.v_total.set(total)

        bal = b.get("balance") or {}
        if bal.get("ok"):
            self.v_balance.set(f"¥{float(bal.get('total') or 0):.2f}")
        else:
            err = str(bal.get("error") or "不可用")
            if err == "未查询官方余额":
                self.v_balance.set("请到官方平台查看")
            else:
                self.v_balance.set(f"—（{err[:26]}{'…' if len(err) > 26 else ''}）")

        err = str(b.get("platform_error") or "")
        if s_src == "estimate":
            # 一路真实数据都拿不到：数字确实是纯估算，说清原因即可
            self.status.set(f"账单：{err}" if err else "账单：只能显示本地估算（读数还不够）")
            return
        if s_src == "platform":
            self.status.set(f"账单：{err}" if err else "账单：平台明细已更新")
            return
        # 累计已经是余额差值算出来的真实值。今日可能还不行（读数不够两个，
        # 或起点离 00:00 太远）—— 必须解释，否则用户会以为功能没生效。
        parts: list[str] = []
        if err:
            parts.append(f"消费明细未取到（{err}）")
        if bd.get("points"):
            parts.append(
                f"累计由余额差值推算（{int(bd['points'])} 个读数，分辨率 ¥0.01）"
            )
        if bd.get("mixed"):
            # 起点之前差值看不到，那一段是估算补的 —— 必须讲，否则用户以为整个数
            # 都来自官方余额。
            parts.append(
                f"{bd.get('from_day') or '起点'} 之前的历史用估算补齐"
                f"（¥{float(bd.get('prior_est_cny') or 0):.2f}）"
            )
        if t_src == "estimate":
            if bd.get("today_from"):
                hhmm = time.strftime("%H:%M", time.localtime(int(bd["today_from"])))
                parts.append(
                    f"今日的读数起点是 {hhmm}、还不满一天，先给估算值；"
                    f"明日 00:00 起今日花费也会变成真实值"
                )
            else:
                parts.append("今日读数还不够两个，先给估算值")
        self.status.set("账单：" + ("；".join(parts) if parts else "已用余额差值推算"))

    # ---------------- 协议端 ----------------

    def napcat_script(self) -> Path | None:
        """协议端启动脚本的绝对路径；config.toml 没配置时返回 None。"""
        raw = str((self.cfg.get("napcat") or {}).get("start_script") or "").strip()
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_absolute() else ROOT / p

    def _login_config(self) -> dict | None:
        account = str(self.prefs.get("qq_account") or "").strip()
        if not account.isascii() or not account.isdigit() or account == "0":
            self.status.set("请先在设置中填写并保存目标 QQ 号，再操作协议端")
            return None
        return {**self.cfg, "napcat": {**self.cfg.get("napcat", {}), "account": account}}

    def _login_finished(self, result: dict) -> None:
        self._login_busy = False
        self.btn_relogin.configure(state="normal")
        if self.btn_restart is not None:
            self.btn_restart.configure(state="normal")
        self.status.set(result["note"])
        self._log_ui(f"[界面] {result['note']}\n")
        self._trace_gui(f"协议操作结果：{result['note']}")
        if result.get("ok") or result.get("scan"):
            self._login_pending = True
            self._login_account = str(result.get("account") or self.prefs.get("qq_account") or "")
            self._login_verify_since = float(result.get("qr_since") or self._login_verify_since or time.time())
        if result.get("scan"):
            self._close_qrcode()
            self._start_qrcode_watch(fresh_only=True, since=self._login_verify_since)
        elif result.get("ok"):
            self.status.set("已连接，等待新消息验证；收到新 QQ 群消息后才确认登录成功")
            if result.get("restart_since"):
                self._start_qrcode_watch(fresh_only=True, since=result["restart_since"])

    def _verify_login_message(self, data: dict) -> None:
        if not self._login_pending or self._login_busy:
            return
        message = data.get("last_protocol_message") or {}
        if ((data.get("protocol") or {}).get("state") == "connected"
                and float(message.get("received_at") or 0) > max(self._login_verify_since, float(data.get("ws_connected_at") or 0))
                and float(message.get("message_time") or 0) >= int(max(self._login_verify_since, float(data.get("ws_connected_at") or 0)))
                and (not self._login_account or str(message.get("self_id")) == self._login_account)):
            self._login_pending = False
            note = "登录成功，已收到新 QQ 群消息，消息接收已恢复"
            self.status.set(note)
            self._log_ui(f"[界面] {note}\n")
            self._trace_gui(note)
        elif not self._login_wait_noted and time.time() - self._login_verify_since >= 60:
            self._login_wait_noted = True
            note = "尚未验证消息接收：暂未收到新 QQ 群消息；继续等待，不自动重启"
            self.status.set(note)
            self._log_ui(f"[界面] {note}\n")

    def relogin_napcat(self) -> None:
        """二维码过期只刷新登录码，不结束普通 QQ。"""
        if self._login_busy:
            return
        cfg = self._login_config()
        if cfg is None:
            return
        self._login_busy = True
        self._login_verify_since = time.time()
        self._login_pending = False
        self._login_account = cfg["napcat"]["account"]
        self._login_wait_noted = False
        self.btn_relogin.configure(state="disabled")
        if self.btn_restart is not None:
            self.btn_restart.configure(state="disabled")
        self.status.set("正在刷新 NapCat 登录二维码…")

        def work() -> None:
            self.events.put(("login_done", napcat_mod.refresh_login_qrcode(cfg)))

        threading.Thread(target=work, daemon=True).start()

    def restart_napcat(self) -> None:
        """先确保接收服务就绪，再重启目标 NapCat 并请求新二维码。"""
        cfg = self._login_config()
        if cfg is None or self._login_busy:
            return
        account = cfg["napcat"]["account"]
        if not messagebox.askyesno("强制重启协议端", f"退出目标 QQ {account} 的 NapCat 进程并重新扫码。\n"
                                  "消息接收服务未运行时会先启动；不结束普通 QQ，不删除历史数据。\n"
                                  "可能需要一次 UAC 授权。继续吗？", parent=self.root):
            return
        self._login_busy = True
        self._login_verify_since = time.time()
        self._login_account = account
        self._login_pending = False
        self._login_wait_noted = False
        self.btn_relogin.configure(state="disabled")
        if self.btn_restart is not None:
            self.btn_restart.configure(state="disabled")
        self.qr_watching = False
        self.qr_gen += 1
        self._close_qrcode()
        self.status.set("正在检查消息接收服务；就绪后重启协议端并生成新二维码…")
        self._log_ui(f"[界面] 开始强制重启目标 QQ {account}：先检查消息接收服务…\n")
        self._trace_gui(f"用户请求强制重启目标 QQ {account}")
        if self.proc is not None and self.proc.poll() is not None:
            self._dispatch_event("exit", (self.proc, self.proc.returncode))
        if self.proc is None and not self.start_service():
            self._login_finished({"ok": False, "note": "消息服务未能启动，未重启协议端；请检查设置和服务日志"})
            return
        proc = self.proc

        def work() -> None:
            if not self._wait_receiver_ready(proc, cfg):
                self.events.put(("login_done", {"ok": False, "note": "消息接收服务未就绪或已停止，未重启协议端；请查看服务日志"}))
                return
            self.events.put(("note", "消息接收服务已就绪，正在退出目标 NapCat 并请求新二维码；如有 UAC 请允许…"))
            result = napcat_mod.restart_target(cfg)
            self.events.put(("login_done", result))

        threading.Thread(target=work, daemon=True).start()

    def _wait_receiver_ready(self, proc, cfg: dict, timeout: float = 30) -> bool:
        """工作线程就绪门禁，只访问本机；失败时不动协议端。"""
        port = int(cfg["onebot"]["http_port"])
        ws_port = int(cfg["onebot"]["ws_port"])
        account = str(cfg["napcat"]["account"])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc is None or proc.poll() is not None or self.closing or self.stop_file.exists():
                return False
            try:
                with opener.open(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                    data = json.loads(response.read().decode("utf-8"))
                if data.get("ok") and str(data.get("target_account") or "") == account:
                    with socket.create_connection(("127.0.0.1", ws_port), timeout=2):
                        return proc.poll() is None and not self.closing and not self.stop_file.exists()
            except (OSError, ValueError, AttributeError):
                pass
            time.sleep(0.5)
        return False

    # ---------------- 登录二维码 ----------------

    def napcat_dir(self) -> Path | None:
        """v0.5.7：与启动器共用目录解析，否则自动发现能启动却不会监听二维码。"""
        return napcat_mod.resolve_dir(self.cfg)

    def _trace_gui(self, message: str) -> None:
        """pythonw 没有控制台，关键链路单独落盘供现场核对。"""
        try:
            path = ROOT / "data" / "logs" / "gui.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} "
                        f"thread={threading.current_thread().name} {security.redact(message)}\n")
        except OSError:
            pass  # 日志不可写不能阻止扫码

    def _start_qrcode_watch(self, fresh_only: bool = False, since: float | None = None) -> None:
        """（重新）武装二维码监视线程。**每次调用都换代**，不是「在跑就不重开」。

        为什么不能用单例守卫：守卫会把「重置时要求换新基线」的请求整个吞掉 ——
        旧线程带着旧基线继续跑，看起来还在监视，实际语义已经错了。
        现在每次调用都换代 + 起新线程，旧线程下一秒发现代数对不上自行退出；
        排队中的旧代事件也会被丢弃，避免旧码盖住新码。

        只在目标账号的新消息验证通过后停止监视，不能由旧连接或心跳提前关闭。
        """
        self.qr_gen += 1
        self.qr_shown_mtime = 0.0
        since = time.time() if since is None else since
        self.qr_started_at = since
        if not self.qr_watching:
            self.qr_watching = True
        self._trace_gui(f"监视启动 gen={self.qr_gen} fresh_only={fresh_only} since={since} dir={self.napcat_dir()}")
        threading.Thread(
            target=self._watch_qrcode, args=(fresh_only, self.qr_gen, since), daemon=True
        ).start()

    def _watch_qrcode(self, fresh_only: bool = False, gen: int = 0, since: float = 0.0) -> None:
        """子线程：盯住 NapCat 的登录二维码文件。

        NapCat 需要登录时会把二维码写到「安装目录/cache/qrcode.png」就完事了，
        从不主动展示 —— 用户原本得自己去文件管理器里翻。这里检测到新二维码
        就把路径丢进事件队列，交给主线程弹窗。

        ``fresh_only``：只认本次启动/重置请求之后新写的码（起点由主线程传入）。
        「重启协议端」必须用它 —— 重置那一刻 cache 里很可能还躺着**上一轮的旧码**，
        它早失效了；若照旧弹出，用户扫一张死码，下一张新码又把它顶掉，
        表现恰好是「怎么扫都登不上」。正常「启动」不用它：界面刚开时弹一个
        已经在等的旧码反而是用户想要的。

        ``gen``：换代号。被更新的调用换代后，旧线程在下一轮循环退出。

        **基线只在展示成功后推进**（``qr_shown_mtime`` 由 ``_show_qrcode`` 回填）：
        弹窗那一刻图片可能正被 NapCat 重写（读到半张 PNG），若照旧立刻推进基线，
        这张码就永远不会再弹 —— 单实例下 NapCat 只写一次码，一次碰撞等于永久失败。
        """
        d = self.napcat_dir()
        if d is None:
            self._trace_gui(f"监视退出 gen={gen} 原因=未找到安装目录")
            return
        qr = d / "cache" / "qrcode.png"
        detected = 0.0
        while self.qr_watching and not self.closing and gen == self.qr_gen:
            try:
                mtime = qr.stat().st_mtime
            except OSError:
                mtime = 0.0
            # 只认 10 分钟内更新过的码：过期文件弹出来是误导
            if (mtime and mtime != self.qr_shown_mtime and (time.time() - mtime) < 600
                    and (not fresh_only or mtime >= since)):
                if detected != mtime:
                    self._trace_gui(f"检测到二维码 gen={gen} mtime={mtime} path={qr}")
                    detected = mtime
                self.events.put(("qrcode", (gen, str(qr))))
            time.sleep(1.0)
        self._trace_gui(f"监视退出 gen={gen} current={self.qr_gen} watching={self.qr_watching}")

    def _show_qrcode(self, path: str) -> None:
        """主线程：弹出扫码窗口。同一时刻只保留一个。

        成功展示后回填 ``qr_shown_mtime``，监视线程据此推进基线（见其注释）。
        读取失败**不**回填 —— 监视线程下一轮会拿同一个 mtime 再试，
        直到图片完整读出为止；只把首日志打出来，避免每秒刷屏。
        """
        shown_at = 0.0
        try:
            shown_at = os.stat(path).st_mtime
        except OSError:
            pass
        if shown_at and shown_at == self.qr_shown_mtime:
            return  # v0.5.7：主线程忙时可能积压同一码，不能反复关窗重建
        win = None
        try:
            from PIL import Image, ImageTk  # Pillow 是 rapidocr 的既有依赖

            with Image.open(path) as img:
                img.load()
                img.thumbnail((340, 340))
                photo = ImageTk.PhotoImage(img, master=self.root)
            win = tk.Toplevel(self.root)
            win.title("扫码登录 QQ")
            win.attributes("-topmost", True)
            ttk.Label(win, text="请用手机 QQ 扫描下方二维码完成登录", font=(FONT, 11)).pack(
                padx=20, pady=(16, 8)
            )
            ttk.Label(win, image=photo).pack(padx=20, pady=4)
            ttk.Button(win, text="二维码过期？刷新二维码", command=self.relogin_napcat).pack(pady=6)
            ttk.Label(
                win, text="登录成功后本窗口会自动关闭；也可以直接关掉它", style="Hint.TLabel"
            ).pack(pady=(6, 14))
            win.photo = photo  # 必须挂在控件上，否则被 GC 后图像变空白
            win.update_idletasks()
            win.deiconify()
            win.lift()
        except Exception as exc:  # noqa: BLE001
            if win is not None:
                win.destroy()
            if self._qr_fail_logged != shown_at:
                # 只在「这张码第一次失败」时打日志；重试期每秒失败一次不刷屏
                self._qr_fail_logged = shown_at
                self._trace_gui(f"二维码展示失败 mtime={shown_at} error={exc!r}")
                self._log_ui(f"[界面] 二维码读取失败（会自动重试）：{exc}\n")
            return
        self._close_qrcode()
        self.qr_win = win
        self._qr_fail_logged = 0.0
        # v0.5.7：窗口完整建成后才确认成功，不能在 Toplevel 创建前就停止重试。
        self.qr_shown_mtime = shown_at
        self._trace_gui(f"已弹出扫码窗口 mtime={shown_at} path={path}")
        self._log_ui("[界面] 已弹出扫码窗口 —— 请用手机 QQ 扫描\n")

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
            threading.Thread(target=self._fetch_health, args=(self.proc,), daemon=True).start()
            if not self._summary_days_busy and time.time() - self._summary_days_checked >= 30:
                self.refresh_summary_days()
        elif self.proc is None:
            self.v_service.set("未运行")
            self.v_peer.set("消息服务未运行 · 协议端无法接入")
            self.v_protocol_detail.set("请启动服务；强制重启也会先启动接收服务。此状态不表示 QQ 登录失效。")
            self.v_stored.set("—")
            self.v_cost.set("—")
            self.v_total.set("—")
            self.v_balance.set("—")
            self.v_llm.set("—")
            self.v_daily_latest.set("最近日报：启动服务后查看")
            self.v_summary_days.set("所选日期：启动服务后查看")
        self.root.after(2000, self._tick_health)
        if self.nb.select() == str(self.report_tab) and time.time() - self._report_history_checked >= 30:
            self.refresh_report_history()
        # 协议端日志是**快照**，不像服务日志那样有流 —— 显示着它就顺手刷新一下。
        # 读的是本地文件尾部，2 秒一次的开销可以忽略；不显示时一个字节都不读。
        if self.v_log_src.get() == "协议端日志":
            self._refresh_alt_log()

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
            threading.Thread(target=self._fetch_messages,
                             args=(self._rt_limit, self.v_rt_filter.get() == "仅看图片"), daemon=True).start()
        self.root.after(3000, self._tick_realtime)

    def _fetch_health(self, proc) -> None:
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
        self.events.put(("health", (proc, data)))

    # ---------------- 事件循环 ----------------

    def _drain(self) -> None:
        for _ in range(300):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break

            # ⚠️ 每个事件单独兜底，绝不让一个坏事件把整个事件泵带死：
            # _drain 是靠结尾的 after() 自续的，任何一个 handler 抛异常都会
            # 跳过自续 —— 界面看着还活着，但健康刷新、日志、二维码弹窗**全部**
            # 从此静默失效。这类「半死」状态比崩溃难查得多（v0.5.6 教训）。
            try:
                self._dispatch_event(kind, value)
            except Exception:  # noqa: BLE001
                import traceback

                self._log_ui(
                    "[界面] 事件处理异常（kind=%s）：%s\n"
                    % (kind, traceback.format_exc(limit=3).replace("\n", " | "))
                )

            if kind == "exit" and self.closing and self.proc is None:
                self.root.destroy()
                return

        self.root.after(100, self._drain)

    def _dispatch_event(self, kind: str, value) -> None:
        """单个事件的分发（供 _drain 调用，独立成方法便于单测兜底逻辑）。"""
        if kind == "line":
            self._log_ui(value)
        elif kind == "note":
            self._log_ui(f"[界面] {value}\n")
            self.status.set(value)
        elif kind == "health":
            if isinstance(value, tuple):
                proc, value = value
                if proc is not self.proc or proc is None:
                    return
            self._apply_health(value)
        elif kind == "billing":
            self._apply_billing(value)
        elif kind == "billing_done":
            self._billing_finished(value)
        elif kind == "messages":
            self._render_messages(value)
        elif kind == "message_action":
            key, action, result = value
            self._rt_actions.discard(key)
            if result.get("ok"):
                self.status.set("图片识别完成" if action == "vision" and result.get("state") == "ok"
                                else "图片识别未完成" if action == "vision" else "摘要选项已更新")
                self.refresh_messages()
            else:
                self.status.set(result.get("error") or "图片操作失败")
        elif kind == "summary_done":
            self._summary_finished(value)
        elif kind == "report_history":
            self._render_report_history(*value)
        elif kind == "summary_days":
            start, end, data = value
            self._apply_summary_days(start, end, data)
        elif kind == "login_done":
            self._login_finished(value)
        elif kind == "usage":
            self._render_usage(value)
        elif kind == "usage_sources":
            iid, text = value
            if self.usage_table.selection() == (iid,):
                self.usage_detail.configure(state="normal")
                self.usage_detail.delete("2.0", "end")
                self.usage_detail.insert("end", "\n" + text)
                self.usage_detail.configure(state="disabled")
        elif kind == "qrcode":
            gen, path = value
            if gen == self.qr_gen and self.qr_watching and not self.closing:
                self._show_qrcode(path)
        elif kind == "exit":
            if isinstance(value, tuple):
                proc, code = value
                if proc is not self.proc:
                    return  # 旧进程的排队退出事件不能清掉新服务
                value = code
            self._trace_gui(f"消息服务已退出 code={value}")
            if self.proc is not None and self.proc.stdout:
                try:
                    self.proc.stdout.close()
                except OSError:
                    pass
            self.proc = None
            self.v_service.set("未运行")
            self.v_peer.set("消息服务未运行 · 协议端无法接入")
            self.v_protocol_detail.set("接收服务已停止，NapCat 的本机连接会被拒绝；不表示 QQ 掉线。")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self._log_ui(f"\n[界面] 服务进程已退出（退出码 {value}）\n")
            self.status.set("服务已停止" if value == 0 else f"服务异常退出（{value}），请看日志")

    def _apply_health(self, data: dict | None) -> None:
        self._verify_login_message(data or {})
        if data is None:
            self.v_service.set("启动中或未响应")
            self.v_peer.set("消息服务未响应 · QQ 状态未知")
            self.v_protocol_detail.set("服务未响应，暂时无法确认 QQ 登录状态")
            self.btn_relogin.grid_remove()
            return
        self.v_service.set("运行中")
        peers = data.get("ws_peers") or []
        protocol = data.get("protocol") or {}
        self._protocol_state = protocol
        state = protocol.get("state")
        self.v_protocol_detail.set(protocol.get("message", "") if state != "connected" else "")
        self.btn_relogin.grid_remove()
        if state == "login_required":
            self.v_peer.set("QQ 登录失效 · 请重新扫码")
            self.btn_relogin.grid()
        elif state == "offline":
            self.v_peer.set("连接存在 · QQ 已离线")
        elif state in ("stalled", "recovering"):
            self.v_peer.set("疑似停止接收消息" if state == "stalled" else "正在恢复 · 等待新消息验证")
        elif peers:
            received = data.get("last_protocol_message") or {}
            verified = (float(received.get("received_at") or 0) > max(float(data.get("ws_connected_at") or 0), self._login_verify_since)
                        and float(received.get("message_time") or 0) >= int(max(float(data.get("ws_connected_at") or 0), self._login_verify_since))
                        and (not self.prefs.get("qq_account") or str(received.get("self_id")) == self.prefs["qq_account"])
                        and (not self._login_account or str(received.get("self_id")) == self._login_account)
                        and not self._login_busy and state == "connected")
            self.v_peer.set(f"已连接 · {'已验证消息接收' if verified else '等待新消息验证'} · {', '.join(peers)}")
            if verified:
                if self.qr_watching:
                    self._trace_gui("收到目标账号的新消息，结束扫码监视")
                self.qr_watching = False
                self._close_qrcode()
        elif self._login_pending and self.qr_watching:
            self.v_peer.set("等待扫码 · 消息接收未验证")
            self.btn_relogin.grid()
        elif state == "disconnected":
            self.v_peer.set("连接中断 · 等待自动重连")
        elif self.napcat_script() or self.napcat_dir():
            self.v_peer.set("协议端未连接 · 可强制重启或重新扫码")
        else:
            self.v_peer.set("未连接（NapCat 未启动）")

        c = data.get("counters") or {}
        s = data.get("stats") or {}
        self.v_stored.set(
            f"{s.get('messages', 0)} 条"
            f"（本次运行新收 {c.get('received', 0)}，"
            f"其中已过滤 {c.get('filtered', 0)}）"
        )
        progress = data.get("summary_progress") or {}
        if progress.get("state") == "running":
            elapsed = int(time.time() - self._summary_started) if self._summary_busy else 0
            self.v_summary.set(f"{progress.get('message', '正在生成')} · 已用 {elapsed} 秒" if self._summary_busy
                               else f"每日摘要：{progress.get('message', '正在生成')}")
        if "latest_daily" in data:
            self._show_latest_daily(data.get("latest_daily"))
            daily = data.get("daily_schedule") or {}
            state = daily.get("state")
            if state in ("settling", "running"):
                self.v_daily_latest.set(f"{self.v_daily_latest.get()}；自动任务：{daily.get('message') or '正在生成'}")
            elif state == "failed":
                self.v_daily_latest.set(f"{self.v_daily_latest.get()}；自动任务失败：{daily.get('message') or '请查看日志'}")

        # 账单：优先用服务抓到的真实数据；没抓到就退回本地估算，并标明是估算。
        budget = float(data.get("budget_cny") or 0)
        b = data.get("billing") or {}
        est_today = float(data.get("spent_today_cny") or 0)
        if not b:
            self.v_cost.set(f"¥{est_today:.4f}（估算）/ 预算 ¥{budget:.2f}")
            self.v_total.set(f"¥{est_today:.4f}（估算）")
            self.v_balance.set("请到官方平台查看" if self.cfg.get("api") else "（服务启动后自动抓取）")
        else:
            self._apply_billing(b)

        if not data.get("llm_available"):
            self.v_llm.set("未配置 API Key（不生成摘要或解析图片）")
        elif data.get("vision_enabled"):
            self.v_llm.set("已配置 · 手动图片解析已开启")
        else:
            self.v_llm.set("已配置 · 图片解析 API 已关闭")
        if data.get("api_provider"):
            self.v_llm.set(f"{data['api_provider']} / {data.get('api_model', '')} · {self.v_llm.get()}")

    def _log_ui(self, text: str) -> None:
        text = security.redact(text)
        # 按**行**插入才能按行上色（v0.5.5）。所有调用方传进来的都是完整行
        # （服务 stdout 按 readline、界面侧都带换行），所以这里不做跨块缓冲。
        self.log.configure(state="normal")
        for line in text.split("\n"):
            if line == "" and text.endswith("\n"):
                continue  # 只是结尾那个换行，不值得为它插入一个空行
            self._append_log_line(self.log, line)
        # 只裁剪控件里的早期文本，服务侧的日志与数据库不受影响
        if int(self.log.index("end-1c").split(".")[0]) > 3000:
            self.log.delete("1.0", "501.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------- 关闭 ----------------

    def on_close(self) -> None:
        if not self._save_report_notes():
            return
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
