"""Electron 主进程通过专用管道使用现有 Python 服务和本地资料。"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError

SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from msgx import __version__, conda_runtime, napcat, prefs, security
from msgx.app_icon import selected_icon
from msgx.config import ROOT, abs_path, load
from msgx.local_client import request_json
from msgx.runtime_paths import FROZEN, PROGRAM_ROOT
from msgx.managed_napcat import ManagedNapCat
from msgx.service_process import ServiceProcess
from msgx.store import Store


class Desktop:
    def __init__(self, config_path=None):
        self.root = ROOT
        self.config_path = config_path
        self.prefs_file = prefs.prefs_path(ROOT)
        self.prefs_lock = threading.RLock()
        self.service_lock = threading.RLock()
        self.login_lock = threading.Lock()
        self.service = ServiceProcess()
        self.portable = ManagedNapCat(PROGRAM_ROOT)
        self.instance = uuid.uuid4().hex
        self.stop_file = self.prefs_file.parent / f"electron-{self.instance}.stop"
        self.service_cfg = None
        self.recovery = None
        self.verified_proc = None
        self.closing = False
        self.service_note = ""
        self.service_failed = False
        self.service_started_at = 0.0
        self.login_note = ""
        self.login_cfg = None
        self.login_since = 0.0
        self.reload_config()

    def reload_config(self):
        # 主配置与偏好由现有 YAML 规则合并，项目路径不随前端改变。
        with self.prefs_lock:
            self.base_cfg = load(self.config_path, include_preferences=False)
            self.prefs = prefs.load(self.prefs_file)
            os.environ[prefs.ENV_PREFS] = str(self.prefs_file)
            self.cfg = load(self.config_path)
        return self.cfg

    @property
    def service_running(self):
        proc = self.service.proc
        return proc is not None and proc.poll() is None

    @contextmanager
    def read_store(self, cfg=None):
        cfg = self.cfg if cfg is None else cfg
        path = abs_path(cfg, "db_path")
        if not path.is_file():
            raise FileNotFoundError("本地数据库尚未建立。启动服务接收消息后即可查看。")
        store = Store(path, filters=cfg.get("filters"), readonly=True)
        try:
            yield store
        finally:
            store.close()

    def request(self, path, *, method="GET", payload=None, query=None, timeout=10):
        cfg, proc = self.service_cfg, self.service.proc
        if not self.service_running or cfg is None:
            raise RuntimeError("请先启动本窗口的消息接收服务。")
        port = int(cfg["onebot"]["http_port"])
        token = cfg["onebot"].get("access_token") or ""
        try:
            if path == "/health" or method != "GET" or self.verified_proc is not proc:
                health = request_json(port, "/health", timeout=min(timeout, 3))
                if health.get("desktop_instance") != self.instance:
                    self.verified_proc = None
                    raise RuntimeError("接收端口由其他实例占用。")
                self.verified_proc = proc
                if path == "/health":
                    health.pop("desktop_instance", None)
                    return health
            return request_json(port, path, method=method, payload=payload, query=query,
                                timeout=timeout, token=token)
        except HTTPError as exc:
            try:
                detail = json.load(exc)
            except ValueError:
                detail = {}
            raise RuntimeError(detail.get("error") or f"服务返回 HTTP {exc.code}") from exc
        except OSError as exc:
            raise RuntimeError(f"接收服务暂不可用：{exc}") from exc

    def account(self, cfg):
        account = str(cfg.get("napcat", {}).get("account") or "").strip()
        if not account and cfg.get("napcat", {}).get("managed_dir"):
            return ""  # 安装版首次从已认证的本机 OneBot 握手识别扫码账号。
        if not (account.isascii() and account.isdigit() and int(account)):
            raise ValueError("请在设置中保存目标 QQ 号。")
        return account

    def start(self, automatic=False, *, scan=False):
        with self.service_lock:
            if self.closing:
                raise RuntimeError("窗口正在关闭。")
            if self.service.proc is not None:
                return {"note": "接收服务已启动，正在等待状态更新。"}
            if not automatic:
                self.cancel_recovery()
                self.service.recoveries = 0
                self.service.restart_cancelled = False
                self.reload_config()
                self.account(self.cfg)
                self.service_cfg = self.cfg
            cfg = self.service_cfg
            managed = bool(cfg["napcat"].get("managed_dir"))
            if managed and (PROGRAM_ROOT / "protocol-skipped").is_file():
                raise ValueError("当前只安装了主程序。请重新运行安装向导勾选 QQ 接收组件，或在设置中选择已有协议端。")
            if managed:
                token = cfg["onebot"].get("access_token") or secrets.token_urlsafe(32)
                cfg = self.service_cfg = {**cfg, "onebot": {**cfg["onebot"], "access_token": token}}
            for key in ("ws_port", "http_port"):
                port = int(cfg["onebot"][key])
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                        raise RuntimeError(f"端口 {port} 已被占用，请在原窗口停止接收服务。")
                except OSError:
                    pass
            self.stop_file.parent.mkdir(parents=True, exist_ok=True)
            proc = self.service.spawn(root=self.root, prefs_file=self.prefs_file,
                                      stop_file=self.stop_file, config_path=self.config_path,
                                      automatic=automatic, desktop_instance=self.instance,
                                      managed_token=cfg["onebot"]["access_token"] if managed else "")
            self.service_note = "接收服务启动中"
            self.service_failed = False
            self.service_started_at = time.monotonic()
            threading.Thread(target=self.watch_service, args=(proc,), daemon=True).start()
            if managed:
                try:
                    self.start_portable(cfg, scan=scan)
                except Exception:
                    self.service.request_stop(self.stop_file)
                    raise
            return {"note": self.service_note}

    def start_portable(self, cfg, *, scan=False):
        since = time.time()
        self.portable.start(cfg, scan=scan)
        self.login_cfg, self.login_since = cfg, since
        self.login_note = "正在启动内置协议端，首次登录请用手机 QQ 扫码"

    def watch_service(self, proc):
        path = abs_path(self.service_cfg, "db_path").parent / "logs/service-output.log"
        # 现有读取器负责脱敏及轮转。窗口按需读取日志尾部。
        events = queue.Queue(maxsize=1)
        class ExitEvent:
            def put(self, event):
                if event[0] == "exit":
                    events.put(event)
        self.service.read_output(proc, path, ExitEvent())
        _, (_, code) = events.get()
        with self.service_lock:
            if proc is not self.service.proc:
                return
            self.portable.stop()
            expected, recover = self.service.exited(proc, code, closing=self.closing)
            self.service_failed = code != 0 and not expected and not self.closing
            self.verified_proc = None
            self.service_note = f"接收服务已退出（{code}）"
            if recover:
                delay = self.service.recovery_delay()
                if delay is not None:
                    self.service_note += f"，{delay} 秒后恢复"
                    self.recovery = threading.Timer(delay, self.recover)
                    self.recovery.daemon = True
                    self.recovery.start()

    def recover(self):
        with self.service_lock:
            self.recovery = None
            if not self.service.wanted or self.closing:
                return
            try:
                self.start(automatic=True)
            except (OSError, RuntimeError) as exc:
                self.service_note = f"自动恢复失败：{exc}"
                self.service_failed = True

    def cancel_recovery(self):
        if self.recovery is not None:
            self.recovery.cancel()
            self.recovery = None

    def stop(self):
        with self.service_lock:
            self.cancel_recovery()
            self.service.restart_cancelled = True
            self.portable.stop()
            self.login_cfg, self.login_note = None, ""
            if self.service_running:
                self.service.request_stop(self.stop_file)
                self.service_note = "正在安全停止接收服务"
            else:
                self.service.wanted = False
                self.service_note = "接收服务已停止"
            self.service_failed = False
        return {"note": self.service_note}

    def wait_ready(self, account):
        until = time.monotonic() + 30
        while self.service_running and time.monotonic() < until:
            if self.closing or self.service.restart_cancelled:
                break
            try:
                health = self.request("/health", timeout=2)
                if not account or str(health.get("target_account")) == account:
                    return True
            except RuntimeError:
                pass
            time.sleep(0.4)
        return False

    def login(self, action, payload):
        if not self.login_lock.acquire(blocking=False):
            raise RuntimeError("协议端操作正在进行。")
        try:
            cfg = self.reload_config()
            account = self.account(cfg)
            if payload.get("account") != account:
                raise ValueError("目标 QQ 已发生变化，请刷新运行状态后重新操作。")
            self.login_cfg, self.login_since = None, 0
            if action == "status.relogin":
                result = napcat.refresh_login_qrcode(cfg)
            else:
                self.service.rebuilding = True
                self.service.restart_cancelled = False
                previous_portable = self.portable.proc
                if not self.service_running:
                    self.start(scan=True)
                if not self.wait_ready(account):
                    proc = self.service.proc
                    if self.closing or self.service.restart_cancelled:
                        raise RuntimeError("重启已取消。")
                    self.service.stop_owned(proc, self.stop_file, cancelled=lambda: self.closing or self.service.restart_cancelled)
                    # 管道读取线程先释放该进程的持有权，再启动替代实例。
                    until = time.monotonic() + 5
                    while self.service.proc is not None and time.monotonic() < until:
                        time.sleep(0.1)
                    self.start(scan=True)
                    if not self.wait_ready(account):
                        raise RuntimeError("接收服务未就绪，协议端重启未执行。")
                if self.closing or self.service.restart_cancelled:
                    raise RuntimeError("重启已取消。")
                if self.service_cfg["napcat"].get("managed_dir"):
                    with self.service_lock:
                        if self.closing or self.service.restart_cancelled or not self.service_running:
                            raise RuntimeError("重启已取消。")
                        if self.portable.proc is previous_portable:
                            self.portable.stop()
                            self.start_portable(self.service_cfg, scan=True)
                    return {"note": self.login_note}
                result = napcat.restart_target(cfg)
            self.login_note = str(result["note"])
            if result.get("ok") or result.get("scan"):
                self.login_cfg = cfg
                self.login_since = float(result.get("qr_since") or time.time())
            return {"note": self.login_note}
        finally:
            self.service.rebuilding = False
            self.login_lock.release()

    def login_status(self, health, known_mtime):
        info = {"note": self.login_note, "pending": bool(self.login_cfg), "qr": None}
        cfg = self.login_cfg
        # 成功收到消息后 login_cfg 已经释放，仍须显示随后发生的协议进程退出。
        proc = self.portable.proc
        if proc is not None:
            code = proc.poll()
            if code is not None:
                return {"note": f"内置协议端已退出（{code}），可点火箭重启协议端。", "pending": False, "qr": None}
        if cfg is None:
            return info
        msg = health.get("last_protocol_message") or {}
        since = max(self.login_since, float(health.get("ws_connected_at") or 0))
        if cfg["napcat"].get("managed_dir") and health.get("protocol", {}).get("state") == "connected":
            info["note"] = "QQ 协议已连接，等待首条新群消息验证接收"
        if (health.get("protocol", {}).get("state") == "connected"
                and str(msg.get("self_id")) == (self.account(cfg) or health.get("target_account"))
                and float(msg.get("received_at") or 0) > since
                and float(msg.get("message_time") or 0) >= int(since)):
            self.login_cfg = None
            self.login_note = "已收到目标 QQ 的新群消息，登录验证成功"
            return {"note": self.login_note, "pending": False, "qr": None}
        folder = napcat.resolve_dir(cfg)
        if folder is not None:
            qr = folder / "cache/qrcode.png"
            try:
                mtime = qr.stat().st_mtime
                if mtime >= self.login_since and time.time() - mtime < 600 and mtime != known_mtime:
                    from PIL import Image
                    data = qr.read_bytes()
                    with Image.open(io.BytesIO(data)) as image:
                        image.verify()
                    info["qr"] = {"mtime": mtime, "data": "data:image/png;base64," + base64.b64encode(data).decode("ascii")}
            except OSError:
                pass  # 文件缺失或仍在写入时，下一轮继续读同一张码。
        return info

    def status(self, payload):
        health, note = {}, self.service_note
        if self.service_running:
            try:
                health = self.request("/health", timeout=2.5)
                self.service.healthy(float(health.get("uptime_seconds") or 0))
                paired = health.get("target_account")
                if paired and self.service_cfg["napcat"].get("managed_dir"):
                    self.service_cfg["napcat"]["account"] = paired
                    if not self.cfg["napcat"].get("account"):
                        self.cfg["napcat"]["account"] = paired
            except RuntimeError as exc:
                note = str(exc)
        running = self.service_running
        # Conda 拉起服务需要时间，已就绪后无响应或启动超时才按故障显示。
        starting = (running and self.verified_proc is None and not health.get("ok")
                    and time.monotonic() - self.service_started_at < 15)
        return {"serviceRunning": running, "recovering": self.recovery is not None,
                "starting": starting, "failed": self.service_failed,
                "stopping": self.service_running and not self.service.wanted, "health": health, "note": note,
                "login": self.login_status(health, payload.get("qrMtime")),
                "protocolSkipped": FROZEN and bool(self.cfg["napcat"].get("managed_dir")) and (PROGRAM_ROOT / "protocol-skipped").is_file(),
                "managedProtocol": bool((self.service_cfg if running else self.cfg)["napcat"].get("managed_dir")),
                "account": str(health.get("target_account") or self.cfg.get("napcat", {}).get("account") or "")}

    def close(self, *, for_update=False):
        with self.service_lock:
            self.stop()
            self.closing = True
            proc = self.service.proc
        if proc is not None:
            # 接收服务需要等 WS 和摘要任务收尾，给原有正常关停流程留出时间。
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired as exc:
                if for_update:
                    self.closing = False
                    raise RuntimeError("接收服务正在收尾。停止完成后可重新安装更新。") from exc
                self.service.stop_owned(proc, self.stop_file, cancelled=lambda: False)
        self.stop_file.unlink(missing_ok=True)
        # 朗读有独立的 COM 线程，由阅读模块释放。
        if "reader_api" in sys.modules:
            sys.modules["reader_api"].close(self)

    def dispatch(self, action, p):
        if action == "desktop.bootstrap":
            icon = selected_icon(self.root, preferences_file=self.prefs_file)
            return {"version": __version__, "theme": self.prefs["ui"].get("theme", "dark"),
                    "iconPath": str(icon) if icon else None}
        if action == "desktop.close":
            self.close(for_update=p.get("forUpdate", False))
            return {}
        if action == "desktop.openPreferences":
            if not self.prefs_file.exists():
                raise ValueError("保存一项设置后即可打开偏好文件。")
            os.startfile(self.prefs_file)
            return {}
        if action == "status.read":
            return self.status(p)
        if action == "status.start":
            try:
                return self.start()
            except (OSError, ValueError, RuntimeError) as exc:
                self.service_failed = True
                self.service_note = security.redact(str(exc))
                raise
        if action == "status.stop":
            return self.stop()
        if action in ("status.relogin", "status.restart"):
            return self.login(action, p)
        if action == "status.retrySummary":
            body = {key: p[key] for key in ("kind", "start_date", "end_date", "updated_at")}
            return self.request("/summary/retry", method="POST", payload={**body, "confirmed": True})
        if action == "status.billing":
            return self.request("/billing", timeout=90)
        if action == "status.openBilling":
            import webbrowser
            provider = self.cfg.get("api", {}).get("provider", "").casefold()
            url = ("https://platform.openai.com/settings/organization/billing/overview" if "openai" in provider
                   else "https://platform.deepseek.com/usage")
            webbrowser.open(url)
            return {}
        data_folder = abs_path(self.cfg, "db_path").parent
        if action == "status.log":
            if p.get("source") == "napcat":
                if self.cfg["napcat"].get("managed_dir"):
                    file = napcat.napcat_own_log(napcat.resolve_dir(self.cfg))
                    if file is None:
                        return self.login_note or "随包协议端已就绪，点击启动服务以连接。"
                    with file.open("rb") as log_file:
                        log_file.seek(max(0, os.fstat(log_file.fileno()).st_size - 48000))
                        return security.redact(log_file.read().decode("utf-8", errors="replace"))
                return security.redact(napcat.log_tail(self.cfg))
            subpath = {"service": "service.log", "output": "service-output.log"}[p.get("source", "service")]
            path = data_folder / "logs" / subpath
            if not path.is_file():
                return ""
            with path.open("rb") as file:
                start = max(0, file.seek(0, os.SEEK_END) - 48000)
                file.seek(0)
                if start:
                    file.seek(start - 1)
                    if file.read(1) != b"\n":
                        file.readline()  # 跳过尾部窗口截断的半行。
                return security.redact(file.read().decode("utf-8", errors="replace"))
        if action in ("status.openData", "status.openLogs"):
            path = data_folder if action == "status.openData" else data_folder / "logs"
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)
            return {}
        if action.startswith("messages."):
            import messages_api
            return messages_api.handle(self, action, p)
        if action.startswith("settings."):
            import settings_api
            return settings_api.handle(self, action, p)
        if action.startswith(("reader.", "usage.")):
            import reader_api
            return reader_api.handle(self, action, p)
        raise ValueError(f"未知桌面操作：{action}")


def main(argv=None):
    if not FROZEN:
        conda_runtime.require_environment()
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ctx = Desktop(ap.parse_args(argv).config)
    output_lock = threading.Lock()

    def reply(request, *, busy=False):
        ident = request["id"]
        try:
            if busy:
                raise RuntimeError("桌面后台还有请求未完成，稍后可重新安装更新。")
            value = ctx.dispatch(request["action"], request["payload"])
            response = {"id": ident, "ok": True, "value": value}
            line = json.dumps(response, ensure_ascii=False)
        except Exception as exc:
            logging.getLogger("msgx.desktop").error("%s: %s", request["action"], security.redact(str(exc)))
            response = {"id": ident, "ok": False, "error": security.redact(str(exc))}
            line = json.dumps(response, ensure_ascii=False)
        with output_lock:
            print(line, flush=True)
        return response["ok"]

    # 图片识别、重启等耗时请求不阻塞读取状态和浏览本地历史。
    pool = ThreadPoolExecutor(max_workers=8)
    tasks = set()
    try:
        for line in sys.stdin:
            request = json.loads(line)
            tasks = {task for task in tasks if not task.done()}
            if request["action"] == "desktop.close":
                for_update = request["payload"].get("forUpdate", False)
                # 常规轮询通常很快结束。短暂等待，仍忙时保留后台与窗口供稍后重试。
                busy = for_update and bool(wait(tasks, timeout=2).not_done)
                closed = reply(request, busy=busy)
                if closed or not for_update:
                    break
            else:
                tasks.add(pool.submit(reply, request))
    finally:
        if not ctx.closing:
            ctx.close()
        pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
