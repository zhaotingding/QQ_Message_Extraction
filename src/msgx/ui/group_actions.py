"""群操作界面：先预览，再由用户核对完整范围并确认；工作线程只传数据。"""
from __future__ import annotations

import json
import threading
import tkinter as tk
import urllib.error
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from .. import prefs as prefs_mod
from .. import security
from ..local_client import request_json


STATE_NAMES = {"pending": "待发送", "sending": "已提交，等待结果", "sent": "发送成功",
               "unknown": "结果未知，勿重复发送", "failed": "明确失败", "not_sent": "未发送",
               "left": "已退群，清理未完成", "cleaned": "已退群并清理"}


class GroupActionsPanel:
    def __init__(self, page, notebook, activity_bar):
        self.page, self.busy, self.window = page, False, None
        self.job_id, self.job_proc = "", None
        self._poll_timer = None
        self.tab = ttk.Frame(notebook, padding=5)
        notebook.add(self.tab, text="群发喊话")
        ttk.Label(self.tab, text="仅发送到已保存的统计群；先预览全部群名/群号和正文，再确认发送。纯文字，不自动 @ 全体成员。",
                  style="Hint.TLabel", wraplength=850).pack(anchor="w")
        self.editor = ScrolledText(self.tab, height=4, wrap="word")
        self.editor.pack(fill="x", pady=4)
        bar = ttk.Frame(self.tab)
        bar.pack(fill="x")
        self.send_button = ttk.Button(bar, text="预览群发范围…", command=self.prepare_broadcast)
        self.send_button.pack(side="left")
        self.cancel_button = ttk.Button(bar, text="停止后续发送", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=6)
        self.history_button = ttk.Button(bar, text="读取最近操作记录", command=lambda: self.request("status", {}))
        self.history_button.pack(side="left")
        self.status = tk.StringVar(value="未发送；预览不发送任何消息")
        ttk.Label(self.tab, textvariable=self.status, wraplength=850).pack(anchor="w", pady=3)
        frame = ttk.Frame(self.tab)
        frame.pack(fill="both", expand=True)
        self.results = ttk.Treeview(frame, columns=("group", "id", "state", "detail"), show="headings", height=3)
        for key, title, width in (("group", "群名", 190), ("id", "群号", 110), ("state", "结果", 160), ("detail", "说明 / 消息 ID", 360)):
            self.results.heading(key, text=title)
            self.results.column(key, width=width, stretch=key in ("group", "detail"))
        scroll = ttk.Scrollbar(frame, command=self.results.yview)
        self.results.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.results.pack(fill="both", expand=True)
        self.leave_button = ttk.Button(activity_bar, text="退出所选群…", command=self.prepare_leave)
        self.leave_button.pack(side="right", padx=8)

    def _scope(self):
        page = self.page
        return {"account": str(page.prefs.get("qq_account", page.cfg.get("napcat", {}).get("account", ""))),
                "watch_groups": sorted(str(gid) for gid in page.prefs.get("groups", page.cfg.get("watch", {}).get("groups", [])))}

    def _enabled(self, enabled):
        self.busy = not enabled
        for button in (self.send_button, self.leave_button, self.history_button):
            button.configure(state="normal" if enabled else "disabled")
        self.editor.configure(state="normal" if enabled else "disabled")

    def prepare_broadcast(self):
        if self.busy:
            return
        self.request("preview", {"kind": "broadcast", "text": self.editor.get("1.0", "end-1c"), **self._scope()})

    def prepare_leave(self):
        selected = self.page.rt_activity.selection()
        if not selected:
            self.page.status.set("请先在群活跃度表中选择要退出的群")
            return
        if not self.busy:
            self.request("preview", {"kind": "leave", "group_id": selected[0], **self._scope()})

    def request(self, action, payload):
        if self._poll_timer is not None:
            self.page.root.after_cancel(self._poll_timer)
            self._poll_timer = None  # 手动读取或取消接管同一个轮询链，不叠加计时器。
        page, proc = self.page, self.page.proc
        if proc is None:
            self.status.set("服务未运行，请启动并连接目标 QQ 后再操作")
            page.status.set(self.status.get())
            return
        self._enabled(False)
        self.status.set("正在读取操作结果…" if action == "status" else "正在核对账号与群范围…" if action == "preview" else "正在提交操作，请勿重复点击…")
        if action != "status":
            page.status.set(self.status.get())
        port, token = int(page.cfg["onebot"]["http_port"]), str(page.cfg["onebot"].get("access_token") or "")
        def work():
            try:
                result = request_json(port, "/groups/operations/"+action, method="GET" if action == "status" else "POST",
                    payload=None if action == "status" else payload, query=payload if action == "status" else None, timeout=35, token=token)
            except urllib.error.HTTPError as exc:
                try:
                    result = json.loads(exc.read().decode("utf-8"))
                except (ValueError, OSError):
                    result = {"ok": False, "error": f"HTTP {exc.code}；未确认操作结果，请读取记录核对"}
            except Exception as exc:
                result = {"ok": False, "error": security.redact(str(exc))+"；操作结果未确认，请读取记录，勿重复发送"}
            page.events.put(("group_operation", (proc, action, result)))
        threading.Thread(target=work, daemon=True).start()

    def receive(self, proc, action, response):
        if proc is not self.page.proc:
            self._enabled(True)
            self.status.set("服务已变化，请读取最近记录核对；未自动重发")
            return
        if not response.get("ok"):
            self._enabled(True)
            self.status.set(response.get("error") or "操作失败，请核对记录")
            self.page.status.set(self.status.get())
            return
        data = response.get("result")
        if action == "preview":
            self.show_confirmation(data)
        elif action == "cancel":
            self.status.set(data["note"])
            self._poll()
        elif data:
            self.job_id, self.job_proc = data["id"], proc
            self.results.delete(*self.results.get_children())
            for group in data["groups"]:
                self.results.insert("", "end", values=(group["group_name"], group["group_id"], STATE_NAMES.get(group["state"], group["state"]),
                    group.get("error") or group.get("note") or group.get("message_id", "")))
            running = data["state"] == "running"
            if running:
                sent = sum(g["state"] in ("sent", "cleaned") for g in data["groups"])
                failed = sum(g["state"] == "failed" for g in data["groups"])
                self.status.set(f"QQ {data['account']} · 已成功 {sent}/{len(data['groups'])} 群"
                                + (f" · 失败 {failed} 个" if failed else "") + " · 执行中，可停止后续发送")
            else:  # 结束时 note 已含「共 N 个群：成功／失败／未发送」计数，不再重复写一次成功数。
                self.status.set(f"QQ {data['account']} · " + (data.get("note") or "操作结束，请核对逐群结果"))
            self._enabled(not running)
            self.page.status.set(self.status.get())
            self.cancel_button.configure(state="normal" if running else "disabled")
            if running:
                if self._poll_timer is not None:
                    self.page.root.after_cancel(self._poll_timer)
                self._poll_timer = self.page.root.after(700, self._poll)
            elif data["kind"] == "leave":
                if any(group["state"] == "cleaned" for group in data["groups"]):
                    self.page._exit_context()
                    self.page._render_messages([])
                self.page.refresh_messages()
                self.page.refresh_groups()
        else:
            self._enabled(True)
            self.status.set("没有已确认的操作记录")

    def _poll(self):
        if self._poll_timer is not None:
            self.page.root.after_cancel(self._poll_timer)
            self._poll_timer = None
        if not self.job_id or not self.page.root.winfo_exists():
            return
        if self.page.proc is not self.job_proc:
            self.cancel_button.configure(state="disabled")
            self._enabled(True)
            self.status.set("服务已停止或切换，请读取最近记录核对；未自动重发")
            return
        self.request("status", {"job_id": self.job_id})

    def cancel(self):
        self.cancel_button.configure(state="disabled")
        self.request("cancel", {"job_id": self.job_id})

    # ---------- 群发勾选 ----------

    def _saved_broadcast_groups(self) -> set[str]:
        """上次勾选过的群号；没记录过 = 一个都不勾（默认不预选任何群）。"""
        saved = (getattr(self.page, "prefs", {}) or {}).get("broadcast_groups") or []
        return {str(gid) for gid in saved} if isinstance(saved, (list, tuple, set)) else set()

    def _remember_broadcast_groups(self, group_ids: list[str]) -> None:
        """记住这次的勾选结果，供下次预览复用；写盘失败不影响本次发送。"""
        page = self.page
        page.prefs = {**(getattr(page, "prefs", {}) or {}), "broadcast_groups": list(group_ids)}
        path = getattr(page, "prefs_file", None)
        if not path:
            return
        try:
            prefs_mod.update({"broadcast_groups": list(group_ids)}, path)
        except (OSError, ValueError) as exc:
            self.status.set(f"本次按勾选发送；勾选结果保存失败：{exc}")

    def _selected_groups(self) -> list[str]:
        return [gid for gid in self._group_order if self._group_vars[gid].get()]

    def _check_all(self, value: bool) -> None:
        for var in self._group_vars.values():
            var.set(value)
        self._refresh_selection()

    def _refresh_selection(self) -> None:
        button = getattr(self, "_confirm_button", None)
        if button is None:
            return
        if not self._group_order:  # 退群没有勾选列表：核对勾选即可提交
            button.configure(state="normal" if self._ack.get() else "disabled")
            return
        chosen = self._selected_groups()
        button.configure(text=f"确认发送至 {len(chosen)} 个群",
                         state="normal" if chosen and self._ack.get() else "disabled")
        self.v_selection.set(f"已勾选 {len(chosen)} / {len(self._group_order)} 个群"
                             + ("" if chosen else "；一个都没勾选时不会发送任何消息"))

    def _build_group_picker(self, window, preview) -> None:
        """可勾选的接收群列表：默认全不勾，上次勾过的自动恢复。"""
        saved = self._saved_broadcast_groups()
        box = ttk.Frame(window, padding=(12, 0, 12, 6))
        box.pack(fill="both", expand=True)
        bar = ttk.Frame(box)
        bar.pack(fill="x")
        ttk.Label(bar, text=f"勾选接收群（候选 {len(preview['groups'])} 个；默认一个都不选）").pack(side="left")
        ttk.Button(bar, text="全不选", command=lambda: self._check_all(False)).pack(side="right")
        ttk.Button(bar, text="全选", command=lambda: self._check_all(True)).pack(side="right", padx=4)

        holder = ttk.Frame(box)
        holder.pack(fill="both", expand=True, pady=(4, 2))
        canvas = tk.Canvas(holder, highlightthickness=0, height=230)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))

        def wheel(event) -> None:
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        canvas.bind("<MouseWheel>", wheel)
        inner.bind("<MouseWheel>", wheel)
        for group in preview["groups"]:
            gid, name = str(group["group_id"]), str(group["group_name"])
            var = tk.BooleanVar(value=gid in saved)
            check = ttk.Checkbutton(inner, text=f"{name}（{gid}）", variable=var,
                                    command=self._refresh_selection)
            check.pack(anchor="w", padx=4)
            check.bind("<MouseWheel>", wheel)
            self._group_vars[gid] = var
            self._group_order.append(gid)
        ttk.Label(box, textvariable=self.v_selection, style="Hint.TLabel").pack(anchor="w")

    def show_confirmation(self, preview):
        window = self.window = tk.Toplevel(self.page.root)
        broadcast = preview["kind"] == "broadcast"
        window.title("二次确认：群发消息" if broadcast else "二次确认：退出群并清理消息")
        window.geometry("780x700" if broadcast else "720x560")
        window.minsize(580, 470)
        window.transient(self.page.root)
        window.grab_set()
        self._group_vars: dict[str, tk.BooleanVar] = {}
        self._group_order: list[str] = []
        self.v_selection = tk.StringVar(value="")
        self._ack = tk.BooleanVar(value=False)
        self._confirm_button = None

        head = ttk.Frame(window, padding=(12, 12, 12, 6))
        head.pack(fill="x")
        ttk.Label(head, text=f"发送/操作账号：QQ {preview['account']}").pack(anchor="w")
        if preview["excluded"]:
            ttk.Label(head, text="已退出或不属于当前账号、不会发送的统计群：" + "、".join(preview["excluded"]),
                      style="Hint.TLabel", wraplength=720).pack(anchor="w", pady=(2, 0))

        if broadcast:
            self._build_group_picker(window, preview)
            body_frame = ttk.Frame(window, padding=(12, 0, 12, 0))
            body_frame.pack(fill="x")
            ttk.Label(body_frame, text="将发送的完整正文（纯文字）：").pack(anchor="w")
            body = ScrolledText(body_frame, wrap="word", height=7)
            body.pack(fill="x", pady=(2, 0))
            body.insert("1.0", preview["text"])
        else:
            body = ScrolledText(window, wrap="word", height=16)
            body.pack(fill="both", expand=True, padx=12, pady=(0, 6))
            lines = [f"涉及 {len(preview['groups'])} 个群：",
                     *[f"{index}. {group['group_name']}（{group['group_id']}）"
                       for index, group in enumerate(preview["groups"], 1)],
                     "",
                     f"退出后清理本地该群全部账号的消息（当前 {preview['message_count']} 条）及 OCR/图片语义记录。",
                     "",
                     "已生成摘要、批注和费用记录保留；原图缓存文件保留，避免误删跨群复用或报告引用。",
                     "不会解散群。退出和消息清理不可在程序中撤销。"]
            body.insert("1.0", "\n".join(lines))
        body.configure(state="disabled")
        ttk.Label(window, text="预览 5 分钟内有效；账号、名单或设置变化后必须重新预览。",
                  style="Hint.TLabel").pack(fill="x", padx=12)
        bar = ttk.Frame(window, padding=12)
        bar.pack(fill="x")
        def close():
            window.destroy()
            self.window = None
            self._enabled(True)
            self.status.set("已取消，未执行发送或退群")
        def confirm():
            if not self._ack.get():
                return
            chosen = self._selected_groups()
            if broadcast and not chosen:
                return
            submit.configure(state="disabled")
            window.destroy()
            self.window = None
            self.page.rt_bottom.select(self.tab)
            if broadcast:
                self._remember_broadcast_groups(chosen)
                self.request("confirm", {"token": preview["token"], "confirmed": True, "groups": chosen})
            else:
                self.request("confirm", {"token": preview["token"], "confirmed": True})
        submit = self._confirm_button = ttk.Button(
            bar, text=f"确认发送至 {len(preview['groups'])} 个群" if broadcast else "确认退出并清理消息",
            command=confirm, state="disabled")
        submit.pack(side="right")
        ttk.Button(bar, text="取消", command=close).pack(side="right", padx=8)
        self._ack_check = ttk.Checkbutton(bar, text="我已核对账号、全部群号和内容", variable=self._ack,
                                          command=self._refresh_selection)
        self._ack_check.pack(side="left")
        window.protocol("WM_DELETE_WINDOW", close)
        self._refresh_selection()
