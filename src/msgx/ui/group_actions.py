"""群操作界面：先预览，再由用户核对完整范围并确认；工作线程只传数据。"""
from __future__ import annotations

import json
import threading
import tkinter as tk
import urllib.error
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

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
            sent = sum(g["state"] in ("sent", "cleaned") for g in data["groups"])
            self.status.set(f"QQ {data['account']} · 成功 {sent}/{len(data['groups'])} 群 · "+
                            ("执行中，可停止后续发送" if running else data.get("note") or "操作结束，请核对逐群结果"))
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

    def show_confirmation(self, preview):
        window = self.window = tk.Toplevel(self.page.root)
        window.title("二次确认：群发消息" if preview["kind"] == "broadcast" else "二次确认：退出群并清理消息")
        window.geometry("720x580")
        window.minsize(520, 420)
        window.transient(self.page.root)
        window.grab_set()
        body = ScrolledText(window, wrap="word", height=20)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        lines = [f"发送/操作账号：QQ {preview['account']}", f"涉及 {len(preview['groups'])} 个群："]
        lines.extend(f"{index}. {group['group_name']}（{group['group_id']}）" for index, group in enumerate(preview["groups"], 1))
        if preview["excluded"]:
            lines.append("已退出或不属于当前账号、不会发送的统计群："+"、".join(preview["excluded"]))
        if preview["kind"] == "broadcast":
            lines.extend(["", "将发送的完整正文（纯文字）：", preview["text"], "", "每群仅提交一次。异常时停止后续发送；结果未知不会自动重发。"])
        else:
            lines.extend(["", f"退出后清理本地该群全部账号的消息（当前 {preview['message_count']} 条）及 OCR/图片语义记录。",
                          "已生成摘要、批注和费用记录保留；原图缓存文件保留，避免误删跨群复用或报告引用。", "不会解散群。退出和消息清理不可在程序中撤销。"])
        body.insert("1.0", "\n".join(lines))
        body.configure(state="disabled")
        checked = tk.BooleanVar(value=False)
        ttk.Label(window, text="预览 5 分钟内有效；账号、名单或设置变化后必须重新预览。", wraplength=680).pack(anchor="w", padx=12)
        bar = ttk.Frame(window, padding=12)
        bar.pack(fill="x")
        def close():
            window.destroy()
            self.window = None
            self._enabled(True)
            self.status.set("已取消，未执行发送或退群")
        def confirm():
            if not checked.get():
                return
            submit.configure(state="disabled")
            window.destroy()
            self.window = None
            self.page.rt_bottom.select(self.tab)
            self.request("confirm", {"token": preview["token"], "confirmed": True})
        submit = ttk.Button(bar, text=f"确认发送至 {len(preview['groups'])} 个群" if preview["kind"] == "broadcast" else "确认退出并清理消息",
                            command=confirm, state="disabled")
        submit.pack(side="right")
        ttk.Button(bar, text="取消", command=close).pack(side="right", padx=8)
        ttk.Checkbutton(bar, text="我已核对账号、全部群号和内容", variable=checked,
                        command=lambda: submit.configure(state="normal" if checked.get() else "disabled")).pack(side="left")
        window.protocol("WM_DELETE_WINDOW", close)
