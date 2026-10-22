"""窗口样式常量和系统打开操作；不包含业务或持久化状态。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

APP_TITLE = "消息萃取 msgx"

FONT = "Microsoft YaHei UI"

LOG_BG = "#1b1f27"

LOG_FG = "#d6dbe5"

LOG_RED = "#ff7b72"

LOG_GREEN = "#7ee787"

OK_COLOR = "#1a7f37"

MUTED = "#6b6b68"

def open_url(url: str) -> None:
    """提交给系统默认浏览器；调用方负责显示成功或失败。"""
    if os.name == "nt":
        os.startfile(url)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", url])

def open_path(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])
