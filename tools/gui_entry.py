"""图形界面的启动入口。

由 tools/start_gui.cmd 用 pythonw.exe 调用，所以**没有控制台窗口**。
这意味着任何未捕获的异常都不会有输出 —— 因此这里必须自己兜住异常并把
错误信息弹给用户 + 落盘到 .ui/gui_error.log，否则「双击没反应」将无从排查。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _report(detail: str) -> None:
    """把启动失败的原因同时呈现给用户和日志文件。"""
    log_dir = ROOT / ".ui"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "gui_error.log").write_text(detail, encoding="utf-8")
    except OSError:
        pass
    try:
        import tkinter.messagebox as mb

        mb.showerror(
            "消息萃取 启动失败",
            f"{detail[:1500]}\n\n完整信息已写入：\n{log_dir / 'gui_error.log'}",
        )
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    # tkinter 是标准库，但**部分精简发行版不带**（例如某些便携版、
    # 由工具链自带的精简 Python）。这里显式检查，避免只报一句 ImportError。
    try:
        import tkinter  # noqa: F401
    except ImportError:
        _report(
            "当前 Python 解释器不带 tkinter，无法打开图形界面。\n\n"
            f"解释器：{sys.executable}\n\n"
            "tkinter 属于标准库，但精简发行版常常删掉了它。\n"
            "解决办法（任选其一）：\n"
            "  1. 安装 python.org 的官方 Python（自带 tkinter），用它重建 .venv\n"
            "  2. 换用带 tkinter 的发行版（如 Anaconda）重建 .venv\n"
            "  3. 暂时改用命令行菜单：START_HERE.cmd --cli\n\n"
            "详见 README 的「环境要求」一节。"
        )
        return 1

    try:
        from msgx.gui import main as gui_main

        return gui_main()
    except Exception:  # noqa: BLE001
        _report(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
