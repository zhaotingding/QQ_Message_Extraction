"""设置页的图片初筛与提示词更新控件；HTTP在线程运行，Tk只在主线程渲染。"""
from __future__ import annotations

import json
import threading
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.error import HTTPError

from .. import security
from ..image_policy import validate_analysis
from ..local_client import request_json
from ..time_rules import BEIJING


class AutomationControls:
    def __init__(self, page, tab, summary):
        self.page = page
        self.busy = False
        self.profile_ids = []
        self.version_ids = []
        self.image_enabled = tk.BooleanVar()
        self.image_values = {key: tk.StringVar() for key in ("threshold", "daily_limit", "daily_budget_cny", "history_days")}
        self.prompt_enabled = tk.BooleanVar()
        self.prompt_days = tk.StringVar()
        self.prompt_interval = tk.StringVar()
        self.prompt_status = tk.StringVar(value="尚未读取动态提示词记录")
        self.image_status = tk.StringVar(value="本地初筛无需额外模型；自动 API 默认关闭")
        line = ttk.Frame(summary)
        line.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(line, text="启用提示词自更新", variable=self.prompt_enabled).pack(side="left")
        for label, variable in (("取最近日报天数", self.prompt_days), ("更新间隔（天）", self.prompt_interval)):
            ttk.Label(line, text=label).pack(side="left", padx=(10, 3))
            ttk.Entry(line, textvariable=variable, width=4).pack(side="left")
        ttk.Label(summary, text="用摘要 API 提炼近期重点和广告特征，作为动态补充；基础提示词保留，调用计入消费。",
                  wraplength=850).pack(anchor="w", pady=4)
        ttk.Label(summary, textvariable=self.prompt_status, wraplength=850).pack(anchor="w")
        actions = ttk.Frame(summary)
        actions.pack(fill="x", pady=4)
        self.buttons = []
        for title, command in (("立即更新（调用 API）", self.update), ("刷新记录", self.refresh),
                               ("应用选中版本", self.restore), ("清空动态补充", self.clear)):
            button = ttk.Button(actions, text=title, command=command)
            button.pack(side="left", padx=(0, 5))
            self.buttons.append(button)
        self.versions = ttk.Combobox(summary, state="readonly")
        self.versions.pack(fill="x")
        self.versions.bind("<<ComboboxSelected>>", lambda _event: self.show_selected())
        self.preview = ScrolledText(summary, height=4, wrap="word", state="disabled")
        self.preview.pack(fill="x", pady=4)
        self.records = []

        frame = ttk.LabelFrame(tab, text="图片本地初筛与独立 API", padding=12)
        frame.pack(fill="x", pady=(12, 0))
        ttk.Label(frame, text="图片分析使用配置：").grid(row=0, column=0, sticky="w")
        self.profile = ttk.Combobox(frame, state="readonly")
        self.profile.grid(row=0, column=1, sticky="ew")
        ttk.Checkbutton(frame, text="达到重要度阈值后自动调用图片 API", variable=self.image_enabled).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=5)
        for row, (key, title) in enumerate((("threshold", "重要度阈值（1～100）"),
                     ("daily_limit", "每日最多自动分析张数"), ("daily_budget_cny", "每日自动分析预算（元，估算）"),
                     ("history_days", "自动处理最近几天图片（1～30）")), 2):
            ttk.Label(frame, text=title).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=self.image_values[key], width=12).grid(row=row, column=1, sticky="w")
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="已下载图片逐批本地初筛，表情包直接过滤；仅高分候选上传，结合附近群聊，成功入库后纳入摘要。\n"
                  "筛选是保守规则，不能保证语义准确；广告降权、同图去重、次数/预算限制和未知结果不自动重发共同控制调用。\n"
                  "先在上方创建并保存低价图片 API 配置，再在此选择；摘要继续使用上方选中的摘要配置。",
                  wraplength=850).grid(row=6, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Label(frame, textvariable=self.image_status, wraplength=850).grid(row=7, column=0, columnspan=2, sticky="w")

    def refresh_profiles(self):
        old = self.profile_ids[self.profile.current()] if self.profile.current() >= 0 else self.page.prefs.get("image_api_profile", "")
        self.profile_ids = [""] + list(self.page._api_profiles)
        self.profile.configure(values=["跟随摘要 API"] + [
            f"{profile['provider']} / {profile['model']} [{name}]" for name, profile in self.page._api_profiles.items()])
        self.profile.current(self.profile_ids.index(old) if old in self.profile_ids else 0)

    def load(self):
        self.refresh_profiles()
        saved = self.page.prefs
        selected = saved.get("image_api_profile", self.page.cfg.get("media", {}).get("image_api_profile", ""))
        self.profile.current(self.profile_ids.index(selected) if selected in self.profile_ids else 0)
        options = validate_analysis(saved.get("image_analysis", self.page.cfg.get("media", {}).get("analysis", {})))
        self.image_enabled.set(options["enabled"])
        for key, var in self.image_values.items():
            var.set(str(options[key]))
        prompt = {"enabled": False, "days": 7, "interval_days": 7,
                  **self.page.cfg.get("summary", {}).get("prompt_evolution", {}), **saved.get("prompt_evolution", {})}
        self.prompt_enabled.set(prompt["enabled"])
        self.prompt_days.set(str(prompt["days"]))
        self.prompt_interval.set(str(prompt["interval_days"]))

    def collect(self) -> dict:
        base = {**self.page.cfg.get("media", {}).get("analysis", {}), **self.page.prefs.get("image_analysis", {})}
        options = validate_analysis({**base, "enabled": self.image_enabled.get(), **{key: value.get() for key, value in self.image_values.items()}})
        try:
            days, interval = int(self.prompt_days.get()), int(self.prompt_interval.get())
            if not 2 <= days <= 30 or not 1 <= interval <= 30:
                raise ValueError
        except ValueError:
            raise ValueError("提示词材料天数为2～30，更新间隔为1～30天") from None
        selected = self.profile.current()
        if selected < 0:
            raise ValueError("请选择已保存的图片 API 配置")
        return {"image_api_profile": self.profile_ids[selected], "image_analysis": options,
                "prompt_evolution": {"enabled": self.prompt_enabled.get(), "days": days, "interval_days": interval}}

    def send(self, action: str = "", body: dict | None = None):
        if self.busy:
            self.page.status.set("动态提示词操作进行中，请稍候")
            return
        if self.page.proc is None:
            self.page.status.set("请先启动接收服务，再读取或更新动态提示词")
            return
        self.busy = True
        for button in self.buttons:
            button.configure(state="disabled")
        self.prompt_status.set("正在读取记录…" if not action else "正在提交操作…")
        port = int(self.page.cfg["onebot"]["http_port"])
        proc = self.page.proc
        token = str(self.page.cfg["onebot"].get("access_token") or "")
        def work():
            try:
                if action:
                    request_json(port, "/prompt-evolution/" + action, method="POST", payload=body or {}, timeout=10, token=token)
                result = request_json(port, "/prompt-evolution", timeout=10, token=token)
                error = ""
            except HTTPError as exc:
                try:
                    detail = json.loads(exc.read()).get("error") or str(exc)
                except (ValueError, AttributeError):
                    detail = str(exc)
                result, error = {}, security.redact(str(detail))
            except Exception as exc:
                result, error = {}, security.redact(str(exc))
            self.page.events.put(("automation_done", (proc, result, error)))
        threading.Thread(target=work, daemon=True).start()

    def update(self):
        if self.page.proc is None:
            self.page.status.set("请先启动服务；本操作会调用当前摘要 API")
            return
        if not messagebox.askyesno("更新动态提示词", "使用已保存的摘要 API 总结最近日报，会产生一次模型费用。\n若上次没有响应ID且结果未知，再次确认可能重复计费；已有ID只取回原任务。继续吗？", parent=self.page.root):
            self.page.status.set("已取消动态提示词更新")
            return
        if self.page.save_prefs():
            self.send("update", {"confirmed": True})

    def refresh(self):
        self.send()

    def restore(self):
        selected = self.versions.current()
        if selected < 0:
            self.page.status.set("请先选择一条动态提示词历史记录")
            return
        self.send("restore", {"version_id": self.version_ids[selected]})

    def clear(self):
        self.send("restore", {"version_id": 0})

    def receive(self, result, error):
        self.busy = False
        for button in self.buttons:
            button.configure(state="normal")
        if error:
            self.prompt_status.set("操作失败：" + error)
            self.page.status.set("动态提示词操作失败：" + error)
        else:
            self.render_prompt(result)
            self.page.status.set("提示词更新已排队，等待摘要调度执行" if result.get("queued") else
                                 "动态提示词状态已更新：" + str(result.get("state", "")))

    def render_prompt(self, result):
        active = result.get("active") or {}
        stamp = active.get("created_at") or 0
        when = datetime.fromtimestamp(stamp, BEIJING).strftime("%m-%d %H:%M") if stamp else "暂无"
        state = "queued（等待当前摘要结束）" if result.get("queued") else result.get("state", "idle")
        self.prompt_status.set(f"状态：{state} · 当前版本生成：{when} · {result.get('error') or ''}")
        if "history" not in result:
            return  # 健康轮询仅传状态，历史按用户刷新获取，避免每三秒重复传输版本全文。
        records = result.get("history") or []
        if records != self.records:
            current = self.version_ids[self.versions.current()] if self.versions.current() >= 0 else active.get("id")
            self.records = records
            self.version_ids = [record["id"] for record in records]
            self.versions.configure(values=[f"#{record['id']} · {record['source_start']} 至 {record['source_end']} · " +
                datetime.fromtimestamp(record['created_at'], BEIJING).strftime("%m-%d %H:%M") for record in records])
            if self.version_ids:
                self.versions.current(self.version_ids.index(current) if current in self.version_ids else 0)
                self.show_selected()

    def show_selected(self):
        index = self.versions.current()
        if 0 <= index < len(self.records):
            self.preview.configure(state="normal")
            self.preview.delete("1.0", "end")
            self.preview.insert("1.0", json.dumps(self.records[index]["data"], ensure_ascii=False, indent=2))
            self.preview.configure(state="disabled")

    def health(self, data):
        if data.get("prompt_evolution") and not self.busy:
            self.render_prompt(data["prompt_evolution"])
        image = data.get("image_analysis") or {}
        if image:
            self.image_status.set("图片队列：" + str(image.get("message", "")))
