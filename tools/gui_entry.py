"""图形界面的启动入口。

由轻量主程序 EXE 通过 conda run 调用，也可在 PowerShell 中显式运行。
启动失败时显示处理办法，并将错误保存到 .ui/gui_error.log。
"""
from __future__ import annotations

import ctypes  # Tk 缺失时仍可使用 Windows 原生错误对话框。
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from msgx import conda_runtime  # 先校验环境，再加载 Tk 和业务依赖。


def _report(detail: str) -> None:
    """保存启动错误，让无控制台的入口也能反馈失败。"""
    log_dir = ROOT / ".ui"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "gui_error.log").write_text(detail, encoding="utf-8")
    except OSError:
        pass
    if sys.stderr is not None:
        print(detail, file=sys.stderr)  # Conda 及 EXE 能取得子入口的实际失败原因。
    if sys.platform == "win32" and os.environ.get("MSGX_EXE_LAUNCH") != "1":
        ctypes.windll.user32.MessageBoxW(None, detail, "消息萃取界面启动失败", 0x10)  # 不依赖 Tk 初始化。


def main() -> int:
    try:
        conda_runtime.require_environment()
    except RuntimeError as exc:
        _report(str(exc))
        return 1
    # tkinter 是标准库，但**部分精简发行版不带**（例如某些便携版、
    # 由工具链自带的精简 Python）。这里显式检查，避免只报一句 ImportError。
    try:
        import tkinter  # noqa: F401
    except ImportError:
        _report(
            "当前 Python 解释器不带 tkinter，无法打开图形界面。\n\n"
            f"解释器：{sys.executable}\n\n"
            "tkinter 属于标准库，但精简发行版常常删掉了它。\n"
            f"请检查项目环境：conda install -n {conda_runtime.environment_name()} tk\n"
            "不要覆盖已有环境或向公共 base 安装项目依赖。\n\n"
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
