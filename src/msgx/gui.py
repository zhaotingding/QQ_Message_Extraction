"""消息萃取 图形界面。

职责：
  * 本模块组合页面、分发后台事件并协调关闭。页面组织在 ui/；进程归 service_process 管理。
  * 服务跑在独立子进程里，日志经 stdout 管道 → queue → after() 回主线程。
    Tkinter 不是线程安全的，**所有控件更新必须发生在主线程**。
  * 界面设置写入 .ui/preferences.yaml，**永不改写**受 Git 管理的 config.yaml，
    这样多人协作时不会因为各人的本地偏好互相覆盖。

只用标准库 tkinter —— 不引入任何新依赖。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from . import __version__, DISPLAY_VERSION
from . import prefs as prefs_mod
from . import security
from .config import ROOT, abs_path, load
from .service_process import ServiceProcess
from .ui.status import StatusPage
from .ui.settings import SettingsPage
from .ui.usage import UsagePage
from .ui.reader import ReaderPage
from .ui.realtime import RealtimePage
from .ui.login import LoginActions
from .ui.service import ServiceActions
from .ui.common import APP_TITLE, FONT, MUTED, open_path, open_url




class App(StatusPage, SettingsPage, UsagePage, ReaderPage, RealtimePage, LoginActions, ServiceActions):
    def __init__(self, root: tk.Tk, config_path: str | None = None) -> None:
        self.root = root
        self.config_path = config_path
        self.cfg = load(config_path)
        self.ui_dir = ROOT / ".ui"
        self.ui_dir.mkdir(parents=True, exist_ok=True)
        self.prefs_file = prefs_mod.prefs_path(ROOT)
        self.stop_file = self.ui_dir / "stop.signal"

        self.prefs = prefs_mod.load(self.prefs_file)
        self.service = ServiceProcess()
        self._service_restart_id = None
        self.events: queue.Queue = queue.Queue()
        self.closing = False
        self.health_busy = False
        # 实时消息页：拉取是否在进行中（避免慢响应时叠请求）、上次用的条数
        self._rt_busy = False
        self._rt_limit = 200
        self._rt_rows: dict[str, dict] = {}
        self._rt_actions: set[str] = set()
        self._summary_path = ""
        self._latest_report_ids = {}
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
        self._apply_app_icon()
        self.refresh_groups()
        self.root.after(0, self.refresh_report_history)
        self.root.after(0, self._refresh_keyword_messages)
        self.root.after(100, self._drain)
        self.root.after(800, self._tick_health)
        self.root.after(1200, self._tick_realtime)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    @property
    def proc(self):
        """页面只读当前进程身份，进程和恢复状态由 service 独占。"""
        return self.service.proc

    def _build(self) -> None:
        self.root.title(f"{APP_TITLE}  v{DISPLAY_VERSION}")
        # 默认尺寸按屏幕算：写死 800 高在 768p 屏上会顶出屏幕，
        # 设置页底部的「保存设置」直接被切掉（用户得手动拉窗口才能看到）。
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = min(1280, max(820, sw - 80))
        h = min(940, max(680, sh - 90))
        self.root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        self.root.minsize(820, 560)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.option_add("*Font", (FONT, 10))
        style.configure(".", font=(FONT, 10))
        style.configure("TButton", padding=(8, 4), font=(FONT, 10))
        style.configure("TLabel", font=(FONT, 10))
        style.configure("TCheckbutton", font=(FONT, 10))
        style.configure("TLabelframe.Label", font=(FONT, 10, "bold"))
        style.configure("Hint.TLabel", font=(FONT, 10), foreground=MUTED)
        style.configure("Card.TLabel", font=(FONT, 10))
        style.configure("CardKey.TLabel", font=(FONT, 10), foreground=MUTED)
        style.configure("Treeview", font=(FONT, 10), rowheight=24)
        style.configure("Treeview.Heading", font=(FONT, 10))
        style.configure("TNotebook.Tab", font=(FONT, 10), padding=(14, 5))

        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        self.status = tk.StringVar(value="就绪")
        self.status_label = ttk.Label(outer, textvariable=self.status, foreground="#245cc4")
        self.status_label.pack(fill="x", pady=(0, 4))
        self.status_label.bind("<Configure>", lambda e: self.status_label.configure(wraplength=max(200, e.width)))

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


    # 用户的原话：「正常状态时白色，一旦进入连接状态收听消息就变成绿色，
    # 一旦出现问题就是红色」。规则必须**可解释**，所以是关键词而不是猜：
    _RED_KEYS = ("WARNING", "ERROR", "CRITICAL", "失败", "异常", "错误", "不再入库", "无法", "未成功", "未能成功")
    _GREEN_KEYS = ("成功", "已连入", "已连接", "已恢复", "已启动", "已生成", "已完成",
                   "已刷新", "已保存", "已入库", "已接收", "已弹出", "清理完成", "引擎就绪",
                   "已从协议端拉取群列表", "账单快照（", "多模态支持: True", "LLM 可用: True")


    def _apply_app_icon(self) -> None:
        path = (self.prefs.get("ui") or {}).get("icon_path")
        local_icon = self.prefs_file.parent / "app-icon.ico" if hasattr(self, "prefs_file") else ROOT / ".ui" / "app-icon.ico"
        if (not path or not Path(path).is_file()) and local_icon.is_file():
            path = local_icon
        if not path:
            return
        try:
            from PIL import Image, ImageTk
            # 显式设置当前窗口的各尺寸像素，避免文件图标加载路径重新缩放小帧。
            with Image.open(path) as icon:
                frames = [icon.ico.getimage(size).convert("RGBA") for size in sorted(icon.ico.sizes())]
            self._icon_photos = [ImageTk.PhotoImage(frame, master=self.root) for frame in frames]
            self.root.iconphoto(False, *self._icon_photos)
            self.v_icon_status.set("已应用自定义图标；以后启动继续使用")
        except (OSError, tk.TclError):
            self.v_icon_status.set("图标加载失败，请重新选择 PNG 或 ICO")


    def open_data_dir(self) -> None:
        self._open_external(abs_path(self.cfg, "report_dir").parent)

    def _open_external(self, target: Path | str) -> None:
        try:
            if isinstance(target, Path):
                open_path(target)
            else:
                open_url(target)
            self.status.set("已交给系统默认程序打开：" + str(target))
        except OSError as exc:
            self.status.set("打开失败：" + security.redact(str(exc)))


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
        elif kind == "summary_retry_done":
            self._summary_retry_busy = False
            self.btn_summary_retry.configure(state="normal")
            proc, result = value
            if proc is not self.proc:
                return
            note = ("已找到成稿，直接复用" if result.get("state") == "done" else "已加入自动摘要队列") if result.get("ok") else result.get("error", "补生成提交失败")
            self.status.set(note)
            self._log_ui(f"[界面] {note}\n")
        elif kind == "messages":
            self._accept_messages(*value)
        elif kind == "keyword_messages":
            self._accept_keyword_messages(*value)
        elif kind == "message_action":
            key, action, result = value
            self._rt_actions.discard(key)
            if result.get("ok"):
                self.status.set("图片识别完成" if action == "vision" and result.get("state") == "ok"
                                else "图片识别未完成" if action == "vision" else "摘要选项已更新")
                self.refresh_messages()
            else:
                self.status.set(result.get("error") or "图片操作失败")
        elif kind == "groups_done":
            self._groups_busy = False
            self.btn_groups_refresh.configure(state="normal")
            self.btn_groups_prune.configure(state="normal")
            fetched, prune, rows, error = value
            rendered = self._render_groups(fetched, rows, error)
            if prune and not fetched:
                self.status.set("清理结果未确认：服务不可达或协议端未连接，请刷新检查；当前显示本地群列表")
            elif prune and rendered:
                self.status.set(f"清理完成：删除 {len(fetched.get('deleted') or [])} 个空群，保留 {len(fetched.get('protected') or [])} 个有消息的历史群")
        elif kind == "report_review_loaded":
            self._apply_report_review(*value)
        elif kind == "report_review_saved":
            self._report_review_saved(*value)
        elif kind == "report_history":
            self._render_report_history(*value)
        elif kind == "login_done":
            self._login_finished(value)
        elif kind == "receiver_recovered":
            proc, cfg = value
            if self.closing or self.service.restart_cancelled or self.proc not in (None, proc):
                self._login_finished({"ok": False, "note": "已取消强制重启，接收服务状态已变化"})
                return
            if self.proc is proc and proc is not None:
                self._dispatch_event("exit", (proc, proc.returncode))
            if not self.start_service():
                self._login_finished({"ok": False, "note": "接收服务重建失败，未重启协议端；请查看服务日志"})
                return
            self._restart_with_receiver(cfg, recover=False)
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
            exited_proc = self.proc
            if isinstance(value, tuple):
                proc, code = value
                if proc is not self.proc:
                    return  # 旧进程的排队退出事件不能清掉新服务
                value = code
            expected, recover = self.service.exited(exited_proc, value, closing=self.closing)
            self._trace_gui(f"消息服务已退出 code={value} launcher_pid={getattr(exited_proc, 'pid', None)} expected={expected}")
            self.v_service.set("未运行")
            self.v_peer.set("消息服务未运行 · 协议端无法接入")
            self.v_protocol_detail.set("接收服务已停止，NapCat 的本机连接会被拒绝；不表示 QQ 掉线。")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self._log_ui(f"\n[界面] 服务进程已退出（退出码 {value}）\n")
            self.status.set("服务已停止" if value == 0 else f"服务异常退出（{value}），请看日志")
            if recover:
                self._schedule_service_recovery()


    def on_close(self) -> None:
        if self._report_save_busy:
            self._report_save_action = self.on_close
            self.v_report_review.set("正在保存本地编辑；保存成功后关闭，失败则保留当前窗口")
            return
        if not self._save_report_notes(on_success=self.on_close):
            return
        if self.proc is None:
            self.closing = True
            self.service.wanted = False
            self._cancel_service_recovery()
            self.qr_watching = False
            self.v_rt_auto.set(False)
            self.root.destroy()
            return
        if not messagebox.askyesno(
            "服务仍在运行",
            "服务进程还在运行。\n\n是否停止服务并关闭界面？",
            parent=self.root,
        ):
            self.status.set("已取消关闭，服务继续运行")
            return
        self.qr_watching = False  # 确认关闭后再停止二维码监视和实时刷新。
        self.v_rt_auto.set(False)
        self.closing = True
        self.stop_service()
        self.status.set("正在等待服务安全退出后关闭…")
        # 若 10 秒内没退干净，给出明确提示，而不是让窗口卡住
        self.root.after(10000, self._force_close_hint)

    def _force_close_hint(self) -> None:
        if self.proc is not None and self.closing:
            self.status.set("服务仍在退出中…若长时间无响应，可在任务管理器结束 python.exe")


def main(config_path: str | None = None) -> int:
    if os.name == "nt":
        import ctypes
        # 在创建窗口之前设置固定 ID，版本号不参与任务栏分组。
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("msgx.desktop")
    root = tk.Tk()
    try:
        App(root, config_path)
    except (OSError, ValueError) as exc:
        messagebox.showerror("启动失败", f"无法载入设置，请检查 YAML 配置：\n{exc}", parent=root)
        root.destroy()
        return 2
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
