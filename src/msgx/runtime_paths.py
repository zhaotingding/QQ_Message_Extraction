"""程序资源与个人资料位置。安装版仅从随应用安装的位置加载程序资源。"""
from __future__ import annotations

import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
SOURCE_DIR = Path(__file__).resolve().parents[1]
PROGRAM_ROOT = Path(sys.executable).resolve().parents[1] if FROZEN else SOURCE_DIR.parent
