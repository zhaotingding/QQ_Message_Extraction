"""一键运行全部测试。

用法（在项目根目录）：
    .venv\\Scripts\\python.exe tests\\run_all.py

刻意不依赖 pytest —— 本项目的设计原则是依赖越少越好，
而这三个测试都是自包含的脚本，用子进程逐个跑就能得到清晰的隔离。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
PY = PROJ / ".venv" / "Scripts" / "python.exe"

SUITES = [
    ("过滤规则", "test_filters.py"),
    ("跨群热点", "test_hotspot.py"),
    ("计费口径", "test_cost.py"),
    ("纪要长度", "test_digest.py"),
    ("媒体校验", "test_media.py"),
    ("服务生命周期", "test_service.py"),
    ("界面构建", "test_gui.py"),
]


def main() -> int:
    if not PY.exists():
        print(f"找不到项目虚拟环境：{PY}")
        print("请先执行：")
        print("    py -3.13 -m venv .venv")
        print("    .venv\\Scripts\\python.exe -m pip install -r requirements.lock.txt")
        return 1

    results: list[tuple[str, int]] = []
    for title, script in SUITES:
        print("=" * 62)
        print(f"  {title}  ({script})")
        print("=" * 62)
        r = subprocess.run([str(PY), str(HERE / script)], cwd=str(PROJ))
        results.append((title, r.returncode))
        print()

    print("=" * 62)
    print("  汇总")
    print("=" * 62)
    for title, rc in results:
        print(f"  [{'通过' if rc == 0 else '失败'}] {title}")

    failed = [t for t, rc in results if rc != 0]
    if failed:
        print(f"\n有 {len(failed)} 组测试失败：{', '.join(failed)}")
        return 1
    print("\n全部测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
