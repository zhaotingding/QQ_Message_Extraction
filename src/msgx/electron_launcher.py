"""从项目 Conda 环境打开 Electron，同时把同一解释器交给桌面桥接进程。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .conda_runtime import require_environment

DESKTOP = Path(__file__).resolve().parents[2] / "desktop"


def launch(config_path: str | None = None) -> int:
    require_environment()
    runtime = DESKTOP / "node_modules" / "electron" / "dist" / "electron.exe"
    if not runtime.is_file():
        raise RuntimeError("缺少项目 Electron 运行时。请在仓库根目录执行 npm.cmd ci --prefix desktop，"
                           "再执行 npm.cmd exec --prefix desktop -- install-electron。")
    env = {**os.environ, "MSGX_DESKTOP_PYTHON": sys.executable, "PYTHONUTF8": "1",
           "PYTHONIOENCODING": "utf-8", "PYTHONNOUSERSITE": "1"}
    env.pop("ELECTRON_RUN_AS_NODE", None)
    args = [str(runtime), str(DESKTOP)]
    if config_path:
        args += ["--msgx-config", str(Path(config_path).resolve())]
    return subprocess.run(args, cwd=DESKTOP.parent, env=env, check=False).returncode
