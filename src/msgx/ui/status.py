"""运行状态与操作反馈；由 App 组合，控件仅在主线程操作。"""
from __future__ import annotations

import re
import threading
import time
import tkinter as tk
import urllib.error
from datetime import datetime
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.parse import urlsplit
from .. import napcat as napcat_mod
from .. import security
from ..summarize import BEIJING
from ..local_client import request_json
from .common import FONT, LOG_BG, LOG_FG, LOG_GREEN, LOG_RED


class StatusPage:
    def _build_status_tab(self, nb: ttk.Notebook) -> None:
        page = ttk.Frame(nb)
        nb.add(page, text="运行状态")
        canvas = tk.Canvas(page, highlightthickness=0)
        scrollbar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        tab = ttk.Frame(canvas, padding=8)
        window = canvas.create_window((0, 0), window=tab, anchor="nw")
        def resize_status(_event=None) -> None:
            canvas.itemconfigure(window, width=canvas.winfo_width(),
                                 height=max(tab.winfo_reqheight(), canvas.winfo_height()))
            canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.bind("<Configure>", resize_status)
        tab.bind("<Configure>", resize_status)
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))

        card = ttk.LabelFrame(tab, text="服务状态", padding=8)
        card.pack(fill="x")
        card.bind("<Configure>", resize_status)

        setup_hint = self._protocol_setup_hint()  # 首屏即可说明缺失设置，无需先触发失败操作。
        self.v_service = tk.StringVar(value="待配置" if setup_hint else "未运行")
        self.v_peer = tk.StringVar(value="消息服务未运行 · 请完成设置" if setup_hint else "消息服务未运行")
        self.v_stored = tk.StringVar(value="—")
        self.v_cost = tk.StringVar(value="—")
        self.v_total = tk.StringVar(value="—")
        self.v_balance = tk.StringVar(value="—")
        self.v_llm = tk.StringVar(value="—")
        self.v_billing_feedback = tk.StringVar(value="")

        ttk.Label(card, text="服务进程", style="CardKey.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        service_actions = ttk.Frame(card)
        service_actions.grid(row=0, column=1, columnspan=3, sticky="ew", pady=(0, 4))
        ttk.Label(service_actions, textvariable=self.v_service, style="Card.TLabel").pack(side="left", padx=(0, 12))
        self.btn_start = ttk.Button(service_actions, text="启动服务", command=self.start_service)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(service_actions, text="停止服务", command=self.stop_service, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_restart = None
        if self.napcat_script() or self.napcat_dir():
            self.btn_restart = ttk.Button(service_actions, text="强制重启", command=self.restart_napcat)
            self.btn_restart.pack(side="left")
        ttk.Button(service_actions, text="打开数据目录", command=self.open_data_dir).pack(side="right")

        # 两列并排。用户的原话：「出了今日花费，总花费呢，不应该写在进入花费的右边吗」——
        # 所以这两个必须是同一行的左右邻居，而不是上下两行。
        rows = [
            [("协议端", self.v_peer), ("已入库消息", self.v_stored)],
            [("今日花费", self.v_cost), ("累计花费", self.v_total)],
            [("账户余额", self.v_balance)],
            [("模型状态", self.v_llm)],
        ]
        for r, pair in enumerate(rows, 1):
            for c, (key, var) in enumerate(pair):
                col = c * 2
                ttk.Label(card, text=key, style="CardKey.TLabel").grid(
                    row=r, column=col, sticky="w", padx=(0 if col == 0 else 20, 8), pady=2
                )
                if key == "账户余额":
                    balance_actions = ttk.Frame(card)
                    balance_actions.grid(row=r, column=1, columnspan=3, sticky="ew", pady=2)
                    ttk.Label(balance_actions, textvariable=var, style="Card.TLabel").pack(side="left")
                    self.btn_billing = ttk.Button(balance_actions, text="刷新账单", command=self.refresh_billing)
                    self.btn_billing.pack(side="left", padx=6)
                    ttk.Button(balance_actions, text="官方平台", command=self._open_billing_platform).pack(side="left")
                    feedback = ttk.Label(balance_actions, textvariable=self.v_billing_feedback, style="Hint.TLabel", width=1)
                    feedback.pack(side="left", fill="x", expand=True, padx=(8, 0))
                    feedback.bind("<Configure>", lambda event: event.widget.configure(wraplength=max(80, event.width)))
                else:
                    value_label = ttk.Label(card, textvariable=var, style="Card.TLabel", width=1)
                    value_label.grid(
                        row=r, column=col + 1, columnspan=3 if key == "模型状态" else 1,
                        sticky="ew", pady=2
                    )
                    value_label.bind("<Configure>", lambda event: event.widget.configure(wraplength=max(80, event.width)))
        card.columnconfigure(1, weight=1)
        card.columnconfigure(3, weight=1)

        self.v_protocol_detail = tk.StringVar(value=setup_hint)
        protocol_detail = ttk.Label(card, textvariable=self.v_protocol_detail, width=1)
        protocol_detail.grid(
            row=len(rows) + 1, column=0, columnspan=4, sticky="ew", pady=(2, 0)
        )
        protocol_detail.bind("<Configure>", lambda event: event.widget.configure(wraplength=max(80, event.width)))
        self.v_protocol_detail.trace_add("write", lambda *_: protocol_detail.grid() if self.v_protocol_detail.get()
                                        else protocol_detail.grid_remove())
        protocol_detail.grid_remove()
        self.btn_relogin = ttk.Button(card, text="重新扫码", command=self.relogin_napcat)
        self.btn_relogin.grid(row=len(rows) + 2, column=0, sticky="w")
        self.btn_relogin.grid_remove()

        # ---- 自动摘要状态 ----
        automatic = ttk.LabelFrame(tab, text="自动日报 / 周报 / 月报", padding=6)
        automatic.pack(fill="x", pady=(4, 0))
        self.v_summary_schedule_desc = tk.StringVar(value=self._summary_schedule_desc())
        ttk.Label(automatic, textvariable=self.v_summary_schedule_desc, style="Hint.TLabel", wraplength=900).pack(anchor="w")
        self.v_latest_reports = {}
        for kind, title in (("daily", "日报"), ("weekly", "周报"), ("monthly", "月报")):
            self.v_latest_reports[kind] = tk.StringVar(value=f"最近{title}：启动服务后查看")
            ttk.Label(automatic, textvariable=self.v_latest_reports[kind], wraplength=900).pack(anchor="w")
        self.v_summary_schedule = tk.StringVar(value="自动摘要：服务未运行")
        ttk.Label(automatic, textvariable=self.v_summary_schedule, wraplength=900).pack(anchor="w")
        self.v_summary_next = tk.StringVar(value="")
        ttk.Label(automatic, textvariable=self.v_summary_next, style="Hint.TLabel", wraplength=900).pack(anchor="w")
        self._summary_retry_jobs = []
        self._summary_retry_busy = False
        self.summary_retry_row = ttk.Frame(automatic)
        ttk.Label(self.summary_retry_row, text="待确认补生成").pack(side="left")
        self.cb_summary_retry = ttk.Combobox(self.summary_retry_row, state="readonly", width=43)
        self.cb_summary_retry.pack(side="left", fill="x", expand=True, padx=6)
        self.btn_summary_retry = ttk.Button(self.summary_retry_row, text="确认补生成", command=self.retry_summary)
        self.btn_summary_retry.pack(side="right")
        def resize_summary(event) -> None:
            for label in automatic.winfo_children():
                if isinstance(label, ttk.Label):
                    label.configure(wraplength=max(200, event.width - 20))
            resize_status()
        automatic.bind("<Configure>", resize_summary)

        # ---- 日志区（v0.5.5：服务日志 / 协议端日志在这里切换，不再弹独立窗口）----
        # 用户的原话：「我希望这个 UI 界面就是所有的程序载体，什么功能都能在上面，
        # 而不需要我去别的地方再找寻浪费时间。」弹窗式日志正好是反面教材 ——
        # 它把信息放到了一个用户不知道何时会出现、还得专门去点的另一个窗口里。
        log_frame = ttk.LabelFrame(tab, text="日志", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(4, 0))
        bar = ttk.Frame(log_frame)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="看哪个").pack(side="left")
        self.v_log_src = tk.StringVar(value="服务日志")
        self.cb_log_src = ttk.Combobox(
            bar, textvariable=self.v_log_src, state="readonly",
            values=["服务日志", "协议端日志"], width=11, font=(FONT, 10),
        )
        self.cb_log_src.pack(side="left", padx=(6, 0))
        self.cb_log_src.bind("<<ComboboxSelected>>", lambda _e: self._switch_log_source())
        ttk.Button(bar, text="刷新", width=5, command=self._refresh_log).pack(
            side="left", padx=6
        )
        ttk.Button(
            bar, text="打开记录目录",
            command=lambda: self._open_external(napcat_mod.log_path(self.cfg).parent),
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


    def _summary_schedule_desc(self) -> str:
        chars = self._summary_options()["target_chars"]
        return (f"北京时间 00:00：日报每天 · 周报周一 · 月报每月1日；依次生成，"
                 f"字数：日报 {chars} / 周报 {2 * chars}（2D）/ 月报 {3 * chars}（3D）。")


    def _apply_summary_retry_jobs(self, jobs: list) -> None:
        index = self.cb_summary_retry.current()
        selected = self._summary_retry_jobs[index] if 0 <= index < len(self._summary_retry_jobs) else None
        self._summary_retry_jobs = [dict(job) for job in jobs if job.get("state") == "blocked"]
        titles = {"daily": "日报", "weekly": "周报", "monthly": "月报"}
        self.cb_summary_retry.configure(values=[f"{titles.get(j['kind'], j['kind'])} {j['start_date']} — {j['end_date']}"
                                                for j in self._summary_retry_jobs])
        if self._summary_retry_jobs:
            self.cb_summary_retry.current(self._summary_retry_jobs.index(selected) if selected in self._summary_retry_jobs else 0)
            self.summary_retry_row.pack(fill="x", pady=(4, 0))
        else:
            self.cb_summary_retry.set("")
            self.summary_retry_row.pack_forget()
        self.btn_summary_retry.configure(state="disabled" if self._summary_retry_busy else "normal")


    def retry_summary(self) -> None:
        index = self.cb_summary_retry.current()
        if self._summary_retry_busy or self.proc is None or not 0 <= index < len(self._summary_retry_jobs):
            self.status.set("正在提交，请稍候…" if self._summary_retry_busy else "请先启动服务并选择需要补生成的摘要")
            return
        job = dict(self._summary_retry_jobs[index])
        if not messagebox.askyesno("确认补生成", f"补生成 {job['start_date']} — {job['end_date']} 的摘要？\n\n"
                "此前请求可能已经计费，重新生成可能重复收费。程序会先检查已有成稿；缺失时才重新请求模型。\n"
                "完成后将自动继续依赖它的周报、月报。", parent=self.root):
            self.status.set("已取消补生成，未提交请求")
            return
        self._summary_retry_busy = True
        self.btn_summary_retry.configure(state="disabled")
        self.status.set("正在提交补生成确认…")
        proc = self.proc
        def work() -> None:
            port = int(self.cfg["onebot"]["http_port"])
            token = self.cfg["onebot"].get("access_token")
            payload = {key: job[key] for key in ("kind", "start_date", "end_date", "updated_at")}
            payload["confirmed"] = True
            try:
                result = request_json(port, "/summary/retry", method="POST", payload=payload,
                                      timeout=10, token=token or "")
            except urllib.error.HTTPError as exc:
                result = {"ok": False, "error": f"HTTP {exc.code}，请等待状态刷新后重新确认"}
            except (OSError, ValueError):
                result = {"ok": False, "error": "未能确认提交结果，请等待状态刷新，勿连续重发"}
            self.events.put(("summary_retry_done", (proc, result)))
        threading.Thread(target=work, daemon=True).start()


    def _apply_summary_schedule(self, data: dict) -> None:
        reports = data.get("latest_reports")
        if isinstance(reports, dict):
            identities = {}
            for kind, title in (("daily", "日报"), ("weekly", "周报"), ("monthly", "月报")):
                item = reports.get(kind)
                if not item:
                    self.v_latest_reports[kind].set(f"最近{title}：暂无已完成记录")
                    continue
                start = datetime.fromtimestamp(item["window_start"], BEIJING)
                end = datetime.fromtimestamp(item["window_end"], BEIJING)
                created = datetime.fromtimestamp(item["created_at"], BEIJING)
                self.v_latest_reports[kind].set(
                    f"最近{title}：{start:%Y-%m-%d %H:%M} — {end:%Y-%m-%d %H:%M} · "
                    f"已完成 {item.get('chars', 0)} 字 · 生成于 {created:%m-%d %H:%M}")
                identities[kind] = (item.get("id"), item["created_at"])
            if identities != self._latest_report_ids:
                self._latest_report_ids = identities
                self.refresh_report_history()
        schedule = data.get("summary_schedule") or {}
        self._apply_summary_retry_jobs(schedule.get("failures") or [])
        labels = {"idle": "等待执行", "waiting": "等待前置摘要", "pending": "等待恢复", "settling": "等待消息入库",
                  "running": "正在生成", "failed": "任务失败", "done": "已完成", "empty": "无内容",
                  "blocked": "已停止重试，请检查", "retry": "等待重试", "disabled": "未启用", "unknown": "状态未知"}
        state = schedule.get("state", "unknown")
        title = {"daily": "日报", "weekly": "周报", "monthly": "月报"}.get(schedule.get("kind"), "自动摘要")
        message = security.redact(str(schedule.get("message") or "服务尚未提供调度状态"))
        progress = data.get("summary_progress") or {}
        if state == "running" and progress.get("state") == "running" and progress.get("message"):
            message += "；" + security.redact(str(progress["message"]))
        period = f" {schedule['start_date']} — {schedule['end_date']}" if schedule.get("start_date") and schedule.get("end_date") else ""
        self.v_summary_schedule.set(f"{title}{period}：{labels.get(state, state)} · {message}")
        next_runs = schedule.get("next_runs") or {}
        times = []
        for kind, title in (("daily", "日报"), ("weekly", "周报"), ("monthly", "月报")):
            value = next_runs.get(kind)
            if value:
                try:
                    when = datetime.fromisoformat(value).astimezone(BEIJING)
                    times.append(f"{title} {when:%m-%d %H:%M}")
                except (TypeError, ValueError):
                    pass
        self.v_summary_next.set("下次计划（北京时间）：" + "；".join(times) if times else "")


    @staticmethod
    def _make_log_text(parent) -> ScrolledText:
        w = ScrolledText(
            parent,
            height=8,
            wrap="word",
            state="disabled",
            font=("Consolas", 10),
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


    def _refresh_log(self) -> None:
        if self.v_log_src.get() == "协议端日志":
            self._refresh_alt_log()
            self.status.set("协议端日志已刷新 · " + time.strftime("%H:%M:%S"))
        else:
            self.log.see("end")
            self.status.set("服务日志为实时接收，已定位到最新记录 · " + time.strftime("%H:%M:%S"))


    @classmethod
    def _append_log_line(cls, widget, line: str) -> None:
        tag = cls._log_tag(line)
        if tag:
            widget.insert("end", line + "\n", tag)
        else:
            widget.insert("end", line + "\n")


    def _open_billing_platform(self) -> None:
        profile = self.prefs.get("api") or self.cfg.get("api") or self.cfg.get("llm", {})
        host = urlsplit(str(profile.get("base_url") or "")).hostname
        if host == "api.openai.com":
            self._open_external("https://platform.openai.com/settings/organization/billing/overview")
        elif host == "api.deepseek.com":
            self._open_external("https://platform.deepseek.com/usage")
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
                body = request_json(port, "/billing", timeout=90)
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


    def _apply_health(self, data: dict | None) -> None:
        self._verify_login_message(data or {})
        if data is None:
            self.v_service.set("启动中或未响应")
            self.v_summary_schedule.set("自动摘要：服务未响应，调度状态未知")
            self.v_peer.set("消息服务未响应 · QQ 状态未知")
            self.v_protocol_detail.set("服务未响应，暂时无法确认 QQ 登录状态")
            self.btn_relogin.grid_remove()
            return
        self.v_service.set("运行中")
        if self.service.healthy(data.get("uptime_seconds", 0)):
            note = "接收服务已重新启动，健康检查通过"
            self.status.set(note)
            self._log_ui(f"[界面] {note}\n")
            self._trace_gui(note)
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
        else:
            setup_hint = self._protocol_setup_hint()  # 脚本存在不能证明本机协议端已经配置。
            self.v_peer.set("协议端未连接 · 待配置" if setup_hint else "协议端未连接 · 可强制重启或重新扫码")
            if setup_hint:
                self.v_protocol_detail.set(setup_hint)

        c = data.get("counters") or {}
        s = data.get("stats") or {}
        self.v_stored.set(
            f"{s.get('messages', 0)} 条"
            f"（本次运行新收 {c.get('received', 0)}，"
            f"其中已过滤 {c.get('filtered', 0)}）"
        )
        self._apply_summary_schedule(data)

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
