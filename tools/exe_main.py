"""主程序 QQ_Message_Extraction.exe 的入口（PyInstaller 冻结后运行的就是本文件）。

职责只有一个：**打开图形界面**。服务不由它启动，用户自己在界面里点「启动服务」。

## 为什么做成「启动器」而不是把依赖全打包进去

把 onnxruntime / rapidocr / numpy 一起塞进 exe 会有两个明显代价：
产物体积达数百 MB，且 onefile 每次启动都要把这几百 MB 解压到临时目录（启动慢好几秒）。
而本项目本来就必须有自己的 `.venv`（依赖装在项目里，见 pyproject.toml），
所以 exe 只做「找到 .venv 并拉起界面」这一件事：

- 体积约 10MB，秒开
- **改 src/ 下的代码后不需要重新打包** —— exe 只是把解释器指向项目里的源码
- 代价：不能脱离这个项目目录单独拷走运行。本项目是自用工具，这个代价可以接受。

## 冻结与未冻结两种运行方式

- 冻结后（exe）：`sys.executable` 就是 exe 本身，它在项目根目录，故项目根 = exe 所在目录。
- 未冻结（用 python 直接跑本文件调试）：项目根 = 本文件的上上级目录。

因为 exe 用 `--noconsole` 构建，**没有任何控制台输出**，
所以这里所有失败路径都落盘到 .ui/exe_error.log，供排查启动故障。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_FROZEN = bool(getattr(sys, "frozen", False))

if IS_FROZEN:
    PROJECT_DIR = Path(sys.executable).resolve().parent
else:
    PROJECT_DIR = Path(__file__).resolve().parent.parent

GUI_ENTRY = PROJECT_DIR / "tools" / "gui_entry.py"
LOG_PATH = PROJECT_DIR / ".ui" / "exe_error.log"


def _report(title: str, detail: str) -> None:
    """把启动失败原因写入日志，不弹出通知。"""
    full = f"{title}\n\n{detail}"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text(full, encoding="utf-8")
    except OSError:
        pass


def worker_python() -> Path | None:
    """优先 pythonw.exe（无控制台窗口），退而求其次用 python.exe。"""
    scripts = PROJECT_DIR / ".venv" / "Scripts"
    for exe in ("pythonw.exe", "python.exe"):
        cand = scripts / exe
        if cand.exists():
            return cand
    return None


def build_env() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_DIR / "src")
    env["PYTHONUTF8"] = "1"
    # 本服务要与 127.0.0.1 上的 NapCat 通信。若这些变量指向系统代理，
    # aiohttp 会把本地请求也发过去，症状是「连不上自己的服务」，很难查。
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(var, None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


def main() -> int:
    if not GUI_ENTRY.exists():
        _report(
            "找不到界面程序。",
            f"期望位置：{GUI_ENTRY}\n\n"
            "请确认 QQ_Message_Extraction.exe 位于项目根目录，且 tools\\gui_entry.py 存在。",
        )
        return 1

    python = worker_python()
    if python is None:
        _report(
            "找不到 Python 运行环境。",
            f"期望位置：{PROJECT_DIR}\\.venv\\Scripts\\pythonw.exe\n\n"
            "本项目把依赖装在项目内的 .venv 里。请在项目根目录打开终端执行：\n"
            "    py -3.13 -m venv .venv\n"
            "    .venv\\Scripts\\python.exe -m pip install -e .\n\n"
            "详见 README.md 的「安装」一节。",
        )
        return 1

    # CREATE_NO_WINDOW：不要多弹一个黑框。
    # 本进程随即退出，子进程独立存活（Windows 默认不组成 job 对象，不会被连带杀掉）。
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        subprocess.Popen(
            [str(python), str(GUI_ENTRY)],
            cwd=str(PROJECT_DIR),
            env=build_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except OSError as exc:
        _report("无法启动图形界面。", f"命令：{python} {GUI_ENTRY}\n\n{exc!r}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
