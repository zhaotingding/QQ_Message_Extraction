"""NapCat 登录与二维码交互；由 App 组合，控件仅在主线程操作。"""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from .. import napcat as napcat_mod
from .. import prefs as prefs_mod
from ..config import ROOT
from ..local_client import request_json
from .common import FONT


class LoginActions:
    def napcat_script(self) -> Path | None:
        """协议端启动脚本的绝对路径；config.yaml 没配置时返回 None。"""
        raw = str((self.cfg.get("napcat") or {}).get("start_script") or "").strip()
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_absolute() else ROOT / p


    def _saved_qq_account(self) -> str:
        account = str(self.prefs.get("qq_account") or "").strip()
        return account if account.isascii() and account.isdigit() and account != "0" else ""  # 提示与操作沿用相同账号校验。


    def _protocol_setup_hint(self) -> str:
        missing = []  # 只说明已保存设置，不把输入框中的草稿当作可用配置。
        if not self._saved_qq_account():
            missing.append("目标 QQ 号")
        if self.napcat_dir() is None:  # 复用配置、NAPCAT_HOME 与已有自动发现规则。
            missing.append("NapCat 目录（先完成首次安装）")
        return "待配置：请在设置中填写并保存" + "、".join(missing) if missing else ""


    def _login_config(self) -> dict | None:
        account = self._saved_qq_account()
        if not account:
            self.status.set("请先在设置中填写并保存目标 QQ 号，再操作协议端")
            return None
        cfg = prefs_mod.apply_to_config(self.cfg, self.prefs)
        return {**cfg, "napcat": {**cfg.get("napcat", {}), "account": account}}


    def _login_finished(self, result: dict) -> None:
        self._login_busy = False
        rebuilding = self.service.rebuilding
        self.service.rebuilding = False
        self.btn_relogin.configure(state="normal")
        if self.btn_restart is not None:
            self.btn_restart.configure(state="normal")
        self.status.set(result["note"])
        self._log_ui(f"[界面] {result['note']}\n")
        self._trace_gui(f"协议操作结果：{result['note']}")
        if rebuilding and self.proc is None and self.service.wanted and not self.closing:
            self._schedule_service_recovery()
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
                                  "先恢复接收服务；无响应时会结束本程序的服务进程并重建。\n"
                                  "不结束普通 QQ，不删除历史数据。\n"
                                  "可能需要一次 UAC 授权。继续吗？", parent=self.root):
            self.status.set("已取消强制重启，进程保持原状")
            return
        self._login_busy = True
        self.service.rebuilding = True
        self._cancel_service_recovery()
        self.service.restart_cancelled = False
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
        self._restart_with_receiver(cfg)


    def _restart_with_receiver(self, cfg: dict, recover: bool = True) -> None:
        proc = self.proc
        def work() -> None:
            if not self._wait_receiver_ready(proc, cfg):
                if recover and not self.closing and not self.service.restart_cancelled:
                    self.events.put(("note", "接收服务未响应，正在停止旧服务并重建…"))
                    try:
                        self._stop_unresponsive_receiver(proc)
                    except (OSError, subprocess.SubprocessError) as exc:
                        self.events.put(("login_done", {"ok": False, "note": "旧接收服务未能退出，未重启协议端：" + type(exc).__name__}))
                        return
                    self.events.put(("receiver_recovered", (proc, cfg)))
                    return
                self.events.put(("login_done", {"ok": False, "note": "消息接收服务未就绪或已停止，未重启协议端；请查看服务日志"}))
                return
            if self.closing or self.service.restart_cancelled:
                self.events.put(("login_done", {"ok": False, "note": "已取消强制重启"}))
                return
            self.events.put(("note", "消息接收服务已就绪，正在退出目标 NapCat 并请求新二维码；如有 UAC 请允许…"))
            result = napcat_mod.restart_target(cfg)
            self.events.put(("login_done", result))

        threading.Thread(target=work, daemon=True).start()


    def _stop_unresponsive_receiver(self, proc):
        self.service.stop_owned(proc, self.stop_file,
                                cancelled=lambda: self.closing or self.service.restart_cancelled)


    def _wait_receiver_ready(self, proc, cfg: dict, timeout: float = 30) -> bool:
        """工作线程就绪门禁，只访问本机；失败时不动协议端。"""
        port = int(cfg["onebot"]["http_port"])
        ws_port = int(cfg["onebot"]["ws_port"])
        account = str(cfg["napcat"]["account"])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc is None or proc.poll() is not None or self.closing or self.stop_file.exists():
                return False
            try:
                data = request_json(port, "/health", timeout=2)
                if data.get("ok") and str(data.get("target_account") or "") == account:
                    with socket.create_connection(("127.0.0.1", ws_port), timeout=2):
                        return proc.poll() is None and not self.closing and not self.stop_file.exists()
            except (OSError, ValueError, AttributeError):
                pass
            time.sleep(0.5)
        return False


    def napcat_dir(self) -> Path | None:
        """v0.5.7：与启动器共用目录解析，否则自动发现能启动却不会监听二维码。"""
        return napcat_mod.resolve_dir(prefs_mod.apply_to_config(self.cfg, self.prefs))


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
