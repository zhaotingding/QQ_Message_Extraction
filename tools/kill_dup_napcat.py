"""清理重复启动的协议端实例（NapCat 注入器 + QQ）。

## 为什么会有这个脚本

点一次「启动协议端」= 注入一份 `NapCatWinBootMain.exe` **并且再拉起一份 QQ**。
用户看到「没反应」（最常见的原因是 QQ 登录态丢了、协议端正停在扫码界面），
很自然会**反复点** —— 于是多份实例并存。它们各自往 `<NapCat 目录>/cache/qrcode.png`
写二维码、互相覆盖，**扫哪一次都可能立刻被下一份覆盖掉，谁也登不上去**，
表现恰好就是「点了很多次，消息一直不来」。

v0.5.2 起，界面在启动前会检测实例数并弹确认框，GUI 里的连点已经拦住了。
但这个脚本仍然有用：**手动双击 `tools/start_napcat.bat` 或绕过界面直接启动，
都不受那道拦截保护** —— 出了这种状态就需要有人把场子清干净。

## 用法

    python tools/kill_dup_napcat.py          # 只看不动（默认 dry run）
    python tools/kill_dup_napcat.py --yes    # 真正结束这些进程

清完之后：点**一次**「启动协议端」，然后扫**一次**码。

## ⚠️ 注意

会结束**所有** `QQ.exe`。如果你同时用这个 QQ 客户端登着别的号，它们也会一起登出 ——
本项目从不发消息，重新扫码登录只会重建接收通道，代价是一分钟。
"""
from __future__ import annotations

import argparse
import subprocess
import sys

TARGETS = ("NapCatWinBootMain.exe", "QQ.exe")
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=30, creationflags=_CREATE_NO_WINDOW
    )


def count(image: str) -> int:
    """数实例数。注意：没有匹配进程时 tasklist 会打印一行 INFO/信息，那不是进程。"""
    try:
        p = _run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"])
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  查询 {image} 失败：{type(e).__name__}: {e}")
        return 0
    return sum(1 for ln in (p.stdout or "").splitlines() if image.lower() in ln.lower())


def main() -> int:
    ap = argparse.ArgumentParser(description="清理重复启动的协议端实例")
    ap.add_argument("--yes", action="store_true", help="确认结束这些进程（默认只看不动）")
    args = ap.parse_args()

    before = {name: count(name) for name in TARGETS}
    print("当前实例：")
    for name, n in before.items():
        flag = ""
        if name == "NapCatWinBootMain.exe":
            flag = "  ← 正常应为 0~1；≥2 说明重复启动过" if n >= 2 else ""
        print(f"  {name:24s} {n:3d} 个{flag}")

    if not any(before.values()):
        print("\n没有任何协议端实例在跑 —— 无需清理。")
        print("（如果消息还是不动，问题不在重复启动：看 <NapCat 目录>/cache/qrcode.png "
              "是否正在刷新 —— 那说明协议端在等你扫码。）")
        return 0

    if not args.yes:
        print("\n【只看不动】要真正结束这些进程，请加 --yes：")
        print("    python tools\\kill_dup_napcat.py --yes")
        print("\n⚠️ 会结束所有 QQ.exe —— 同一客户端上登着的其它号也会一起登出。")
        return 0

    print("\n正在结束…")
    for name in TARGETS:
        if not before[name]:
            continue
        p = _run(["taskkill", "/F", "/IM", name])
        ok = p.returncode == 0
        print(f"  taskkill {name:24s} {'成功' if ok else '返回码 %d' % p.returncode}")
        if not ok and p.stderr.strip():
            print(f"    {p.stderr.strip()[:200]}")

    after = {name: count(name) for name in TARGETS}
    print("\n结束之后：")
    for name in TARGETS:
        print(f"  {name:24s} {after[name]:3d} 个")

    if any(after.values()):
        print("\n⚠️ 还有残留 —— 多半是权限不够（这些进程由提权后的 NapCat 拉起）。")
        print("   请用管理员身份重跑本脚本，或在任务管理器里结束它们。")
        return 1

    print("\n已清空。接下来：点一次「启动协议端」，扫一次码。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
