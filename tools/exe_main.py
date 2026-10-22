"""轻量主 EXE：通过指定 Conda 环境运行仓库源码，记录并显示启动失败。

仅打包本入口、标准库及共享环境选择代码；业务依赖留在独立 Conda 环境。
普通业务源码修改无需重建 EXE，主程序放在仓库根目录使用。
"""
from __future__ import annotations

import ctypes  # 无需加载 Tk 或业务依赖即可显示启动错误。
import os
import subprocess
import sys
import tempfile  # Conda 错误输出写入临时文件，避免长时间运行时累积在内存。
from pathlib import Path

IS_FROZEN = bool(getattr(sys, "frozen", False))

if IS_FROZEN:
    PROJECT_DIR = Path(sys.executable).resolve().parent
else:
    PROJECT_DIR = Path(__file__).resolve().parent.parent

if not IS_FROZEN:
    sys.path.insert(0, str(PROJECT_DIR / "src"))  # 冻结入口用内置选择器，源码缺失时仍能显示错误。
from msgx import conda_runtime

GUI_ENTRY = PROJECT_DIR / "tools" / "gui_entry.py"
LOG_PATH = PROJECT_DIR / ".ui" / "exe_error.log"


def _report(title: str, detail: str) -> None:
    """保存启动错误并向双击 EXE 的用户显示处理办法。"""
    full = f"{title}\n\n{detail}"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text(full, encoding="utf-8")
    except OSError:
        pass
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, full, "消息萃取启动失败", 0x10)  # 日志不可写时仍能看到错误。


def build_env() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_DIR / "src")
    env["PYTHONUTF8"] = "1"
    env["PYTHONNOUSERSITE"] = "1"  # 不从用户 site-packages 借用依赖。
    env["MSGX_CONDA_ENV"] = conda_runtime.environment_name()  # GUI 与后台继承同一环境名。
    env["MSGX_EXE_LAUNCH"] = "1"  # 子入口把失败交回本进程，避免重复提示框。
    # 本服务要与 127.0.0.1 上的 NapCat 通信。若这些变量指向系统代理，
    # aiohttp 会把本地请求也发过去，症状是「连不上自己的服务」，很难查。
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(var, None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


def main() -> int:
    if not GUI_ENTRY.is_file() or not (PROJECT_DIR / "src/msgx/gui.py").is_file():
        _report(
            "找不到界面程序。",
            f"期望位置：{GUI_ENTRY}\n\n"
            "请确认 QQ_Message_Extraction.exe 位于完整仓库根目录，保留 tools\\gui_entry.py 与 src\\msgx 源码。",
        )
        return 1

    try:
        command = conda_runtime.command(str(GUI_ENTRY))  # 不使用当前全局 Python 或旧虚拟环境。
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as output:
            result = subprocess.run(command, cwd=str(PROJECT_DIR), env=build_env(),
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                    creationflags=flags, close_fds=True)  # 等待 Conda 返回真实退出状态。
            if result.returncode:
                end = output.seek(0, os.SEEK_END)
                output.seek(max(0, end - 24000))  # 只读取日志尾部，避免长期运行失败时一次读入大文件。
                detail = output.read()[-6000:]  # 提示中只保留最后一段错误，完整 GUI 错误另有日志。
                name = conda_runtime.environment_name()
                _report("项目 Conda 环境或界面启动失败。",
                        f"环境：{name}\n{detail}\n\n在项目目录的 PowerShell 中执行：\n"
                        f"conda create -n {name} python=3.13 pip tk\n"
                        f"conda run -n {name} --no-capture-output python -s -m pip --isolated install --no-user -e .\n"
                        "已有环境时先排查错误，不要覆盖；详见 README.md「快速开始」。")
                return 1
    except (OSError, RuntimeError) as exc:
        _report("无法启动图形界面。", str(exc))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
