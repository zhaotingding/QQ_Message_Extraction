"""Windows 安装版 Python 后台入口，供 Electron 与接收子进程调用。"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from msgx.runtime_paths import FROZEN, PROGRAM_ROOT


def main() -> int | None:
    parser = argparse.ArgumentParser(prog="msgx-backend")
    parser.add_argument("mode", choices=("bridge", "run"))
    parser.add_argument("--config")
    parser.add_argument("--stop-file")
    args = parser.parse_args()

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    data_root = Path(os.environ.get("MSGX_ROOT", ""))
    resources = Path(os.environ.get("MSGX_PROGRAM_RESOURCES", ""))
    if not FROZEN or resources.resolve() != PROGRAM_ROOT:
        parser.error("后台必须由安装版消息萃取从程序资源目录启动。")
    if not data_root.is_absolute() or data_root.resolve().is_relative_to(PROGRAM_ROOT):
        parser.error("安装版需要与程序资源分开的个人资料目录。")

    config = ["--config", args.config] if args.config else []
    if args.mode == "bridge":
        import bridge

        return bridge.main(config)

    from msgx.main import main as run

    stop = ["--stop-file", args.stop_file] if args.stop_file else []
    return run([*config, "run", *stop])


if __name__ == "__main__":
    raise SystemExit(main())
