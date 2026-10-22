"""接收子进程的唯一持有者；负责启动、输出、目标树回收和有限恢复规则。

本模块不导入 Tk。UI 负责定时器与反馈，所有退出事件携带原 Popen 身份。
"""
from __future__ import annotations

import logging
import os
import subprocess
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import conda_runtime, security

CREATE_NO_WINDOW = 0x08000000
SOURCE_DIR = Path(__file__).resolve().parents[1]  # 源码目录不随用户指定的数据根改变。


class ServiceProcess:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.wanted = False
        self.recoveries = 0
        self.expected_exit = None
        self.recovering = False
        self.rebuilding = False
        self.restart_cancelled = False

    def spawn(self, *, root: Path, prefs_file: Path, stop_file: Path,
              config_path=None, automatic: bool = False):
        stop_file.unlink(missing_ok=True)
        env = os.environ.copy()
        env.update(MSGX_PREFS=str(prefs_file), PYTHONUTF8="1", PYTHONNOUSERSITE="1",
                   MSGX_CONDA_ENV=conda_runtime.environment_name(),  # 后台与界面使用同名项目环境。
                   PYTHONPATH=str(SOURCE_DIR),  # 后台只导入本项目源码，不借用外层项目路径。
                   NO_PROXY="127.0.0.1,localhost")
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env.pop(name, None)
        args = ["-u", "-m", "msgx.main"]
        if config_path:
            args += ["--config", str(config_path)]
        args += ["run", "--stop-file", str(stop_file)]
        cmd = conda_runtime.command(*args)  # Conda 选择环境；源码入口再校验真实解释器归属。
        self.proc = subprocess.Popen(cmd, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace",
                                     creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.wanted = True
        self.recovering = automatic
        return self.proc

    def request_stop(self, stop_file: Path):
        # 写入失败时仍保留运行意图，随后异常退出仍应按原规则恢复。
        stop_file.touch()
        self.wanted = False
        self.expected_exit = self.proc

    def recovery_delay(self) -> int | None:
        if self.recoveries >= 3:
            self.wanted = False
            return None
        delay = (5, 15, 30)[self.recoveries]
        self.recoveries += 1
        return delay

    def healthy(self, uptime: float) -> bool:
        if uptime >= 300:
            self.recoveries = 0
        recovered, self.recovering = self.recovering, False
        return recovered

    def exited(self, proc, code, *, closing: bool) -> tuple[bool, bool]:
        expected = proc is not None and proc is self.expected_exit
        self.expected_exit = None
        self.recovering = False
        if proc is not None and proc.stdout:
            try:
                proc.stdout.close()
            except OSError:
                pass
        self.proc = None
        if code in (0, 2):
            self.wanted = False
        recover = code not in (0, 2) and self.wanted and not expected and not closing and not self.rebuilding
        return expected, recover

    def stop_owned(self, proc, stop_file: Path, *, cancelled):
        """只处理本控制器当前持有的进程树；取消或换代后不再强杀。"""
        if proc is None or proc.poll() is not None or proc is not self.proc:
            return
        stop_file.touch()
        self.expected_exit = proc
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        if cancelled() or proc is not self.proc or proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           timeout=10, creationflags=CREATE_NO_WINDOW)
        else:
            proc.kill()
        proc.wait(timeout=5)

    def read_output(self, proc, path: Path, events):
        """管道线程保留原生输出并脱敏；输出日志失败不影响退出事件。"""
        handler = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024,
                                          backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter(f"%(asctime)s launcher_pid={proc.pid} %(message)s",
                                                  datefmt="%Y-%m-%d %H:%M:%S"))
        except OSError as exc:
            events.put(("note", f"服务输出日志无法保存（{type(exc).__name__}），继续接收消息"))
        try:
            for line in proc.stdout:
                line = security.redact(line)
                if handler is not None:
                    handler.emit(logging.LogRecord("msgx.output", logging.INFO, "", 0,
                                                   line.rstrip("\r\n"), (), None))
                events.put(("line", line))
        finally:
            code = proc.wait()
            if handler is not None:
                handler.emit(logging.LogRecord("msgx.output", logging.INFO, "", 0,
                                               f"服务输出结束，退出码 {code}", (), None))
                handler.close()
            events.put(("exit", (proc, code)))
