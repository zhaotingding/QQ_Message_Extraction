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
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__
from . import prefs as prefs_mod
from .config import ROOT, abs_path, load

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

        self._group_ids: list[str] = []
        self._build()
        self._load_into_widgets()
        self.refresh_groups()
        self.root.after(100, self._drain)
        self.root.after(800, self._tick_health)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- 界面构建 ----------------

    def _build(self) -> None:
        self.root.title(f"{APP_TITLE}  v{__version__}")
        self.root.geometry("1000x800")
        self.root.minsize(860, 620)

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
        self._build_status_tab(nb)
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
        self.v_cost = tk.StringVar(value="—")
        self.v_llm = tk.StringVar(value="—")

        rows = [
            ("服务进程", self.v_service),
            ("协议端(NapCat)", self.v_peer),
            ("已入库消息", self.v_stored),
            ("今日花费", self.v_cost),
            ("模型状态", self.v_llm),
        ]
        for i, (key, var) in enumerate(rows):
            ttk.Label(card, text=key, style="CardKey.TLabel").grid(
                row=i, column=0, sticky="w", padx=(0, 14), pady=3
            )
            ttk.Label(card, textvariable=var, style="Card.TLabel").grid(
                row=i, column=1, sticky="w", pady=3
            )
        card.columnconfigure(1, weight=1)

        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(12, 8))
        self.btn_start = ttk.Button(buttons, text="启动服务", command=self.start_service)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(buttons, text="停止服务", command=self.stop_service, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.btn_summary = ttk.Button(buttons, text="立即生成摘要", command=self.summary_now)
        self.btn_summary.pack(side="left")
        ttk.Button(buttons, text="打开数据目录", command=self.open_data_dir).pack(side="right")

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

    # ---- 标签页 2：设置 ----

    def _build_settings_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=14)
        nb.add(tab, text="设置")

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
        self.status.set(f"群列表已刷新，共 {len(self._group_ids)} 个群")

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

    def summary_now(self) -> None:
        if self.proc is None:
            messagebox.showinfo("服务未运行", "请先启动服务，再生成摘要。", parent=self.root)
            return
        port = int(self.cfg["onebot"]["http_port"])
        self.status.set("正在请求生成摘要…")

        def work() -> None:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/summary/now?kind=periodic", data=b"", method="POST"
                )
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with op.open(req, timeout=120) as r:
                    body = json.loads(r.read().decode("utf-8"))
                n = len(body.get("created") or [])
                self.events.put(("note", f"摘要请求完成，生成 {n} 份。详见 data/reports/"))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("note", f"摘要请求失败：{exc}"))

        threading.Thread(target=work, daemon=True).start()

    def open_data_dir(self) -> None:
        open_path(abs_path(self.cfg, "report_dir").parent)

    # ---------------- 健康轮询 ----------------

    def _tick_health(self) -> None:
        if not self.health_busy and self.proc is not None:
            self.health_busy = True
            threading.Thread(target=self._fetch_health, daemon=True).start()
        elif self.proc is None:
            self.v_peer.set("—")
            self.v_stored.set("—")
            self.v_cost.set("—")
            self.v_llm.set("—")
        self.root.after(2000, self._tick_health)

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
        else:
            self.v_peer.set("未连接（NapCat 未连入）")

        c = data.get("counters") or {}
        s = data.get("stats") or {}
        self.v_stored.set(
            f"{s.get('messages', 0)} 条"
            f"（本次运行新收 {c.get('received', 0)}，"
            f"其中已过滤 {c.get('filtered', 0)}）"
        )
        cost = data.get("spent_today_cny", 0)
        budget = data.get("budget_cny", 0)
        self.v_cost.set(f"¥{cost:.4f} / ¥{budget:.2f}")

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
