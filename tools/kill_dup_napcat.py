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

    python tools/kill_dup_napcat.py            # 只看不动（默认 dry run）
    python tools/kill_dup_napcat.py --yes      # 真正结束这些进程
    python tools/kill_dup_napcat.py --elevate  # 自己请求管理员权限再干（会弹一次 UAC）

清完之后：点**一次**「启动协议端」，然后扫**一次**码。

> **为什么常常需要管理员**：这些进程是 NapCat 提权注入后拉起的，
> 普通权限 `taskkill` 会报「拒绝访问」；而且注入器还活着会**把 QQ 重新拉起来**，
> 所以必须先杀注入器、再杀 QQ。`--elevate` 会自动走这一步，
> 结果同时写到 `data/logs/kill_dup_napcat.log` 供事后核对。

## ⚠️ 注意

会结束**所有** `QQ.exe`。如果你同时用这个 QQ 客户端登着别的号，它们也会一起登出 ——
本项目从不发消息，重新扫码登录只会重建接收通道，代价是一分钟。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

TARGETS = ("NapCatWinBootMain.exe", "QQ.exe")
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_LOG = Path(__file__).resolve().parents[1] / "data" / "logs" / "kill_dup_napcat.log"

_opened = {"done": False}


def say(line: str = "") -> None:
    """打印并同时落盘 —— 提权后会在**另一个控制台窗口**里跑，落盘才查得到结果。"""
    print(line)
    try:
        if not _opened["done"]:
            _LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            _opened["done"] = True
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def self_elevate() -> bool:
    """以管理员身份重启本脚本（会弹 UAC）。返回 True = 提权请求已发出。"""
    import ctypes

    script = Path(__file__).resolve()
    params = f'"{script}" --yes --elevated'
    rc = int(
        ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
            None, "runas", sys.executable, params, None, 1
        )
    )
    if rc <= 32:
        say(f"提权失败（ShellExecuteW 返回 {rc}）—— 5 表示 UAC 被取消。")
        return False
    return True


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """跑一个 Windows 命令行工具。

    ⚠️ **不要用 ``text=True``**：``taskkill`` / ``tasklist`` 的输出是**本地编码**
    （中文系统是 GBK），按 UTF-8 解码会抛 ``UnicodeDecodeError`` ——
    而且它发生在 subprocess 的读取线程里，会连带把 ``p.stdout`` / ``p.stderr`` 变成
    ``None``，于是「读错误信息」这行自己先崩了（第一版就栽在这）。
    所以拿 bytes、自己按 utf-8 → gbk → mbcs 逐个试。
    """
    return subprocess.run(args, capture_output=True, timeout=30, creationflags=_CREATE_NO_WINDOW)


def _dec(raw: bytes | None) -> str:
    if not raw:
        return ""
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def count(image: str) -> int:
    """数实例数。注意：没有匹配进程时 tasklist 会打印一行 INFO/信息，那不是进程。"""
    try:
        p = _run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"])
    except (OSError, subprocess.SubprocessError) as e:
        say(f"  查询 {image} 失败：{type(e).__name__}: {e}")
        return 0
    return sum(1 for ln in _dec(p.stdout).splitlines() if image.lower() in ln.lower())


def _kill(name: str) -> tuple[bool, str]:
    """结束某进程名的所有实例，返回 (是否成功, 去重后的首条错误信息)。"""
    p = _run(["taskkill", "/F", "/IM", name])
    msg = (_dec(p.stdout) + _dec(p.stderr)).strip()
    bad = ""
    for ln in msg.splitlines():
        ln = ln.strip()
        if ln.startswith("错误") or "denied" in ln.lower() or "not found" in ln.lower():
            bad = ln[:160]
            break
    return p.returncode == 0, bad


def main() -> int:
    ap = argparse.ArgumentParser(description="清理重复启动的协议端实例")
    ap.add_argument("--yes", action="store_true", help="确认结束这些进程（默认只看不动）")
    ap.add_argument("--elevate", action="store_true", help="自己请求管理员权限再干（弹一次 UAC）")
    ap.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.elevate and not args.elevated:
        say("正在请求管理员权限…（在弹出的 UAC 窗口点「是」；结果会写到 "
            f"{_LOG}）")
        ok = self_elevate()
        return 0 if ok else 1

    before = {name: count(name) for name in TARGETS}
    say("当前实例：")
    for name, n in before.items():
        flag = ""
        if name == "NapCatWinBootMain.exe" and n >= 2:
            flag = "  ← 正常应为 0~1；≥2 说明重复启动过"
        say(f"  {name:24s} {n:3d} 个{flag}")

    if not any(before.values()):
        say("\n没有任何协议端实例在跑 —— 无需清理。")
        say("（如果消息还是不动，问题不在重复启动：看 <NapCat 目录>/cache/qrcode.png "
            "是否正在刷新 —— 那说明协议端在等你扫码。）")
        return 0

    if not args.yes:
        say("\n【只看不动】要真正结束这些进程，请二选一：")
        say("    python tools\\kill_dup_napcat.py --yes       (已用管理员终端时)")
        say("    python tools\\kill_dup_napcat.py --elevate   (自动请求管理员)")
        say("\n⚠️ 会结束所有 QQ.exe —— 同一客户端上登着的其它号也会一起登出。")
        return 0

    # ⚠️ 顺序很关键：**先杀注入器再杀 QQ**。反过来的话注入器还活着，会把 QQ 重新拉起来
    # （实测：QQ.exe 报「成功: 已终止」4 个，回头一数还是 28 个）。
    say("\n正在结束（先杀注入器，再杀 QQ）…")
    denied: list[str] = []
    for name in TARGETS:
        if not before[name]:
            continue
        ok, bad = _kill(name)
        say(f"  taskkill {name:24s} {'成功' if ok else '失败'}")
        if bad:
            say(f"    {bad}")
            denied.append(name)

    # 补一轮：残留的注入器可能在这几秒里又把 QQ 拉了起来
    time.sleep(1.5)
    for name in TARGETS:
        if count(name):
            say(f"  复查发现 {name} 又被拉起来了，再杀一次…")
            _kill(name)

    after = {name: count(name) for name in TARGETS}
    say("\n结束之后：")
    for name in TARGETS:
        say(f"  {name:24s} {after[name]:3d} 个")
    say(f"（完整记录：{_LOG}）")

    if any(after.values()):
        say("\n⚠️ 还有残留。")
        if denied:
            say("   原因是**权限不够**：这些进程由提权后的 NapCat 拉起，普通权限杀不掉。")
            say("   处置：python tools\\kill_dup_napcat.py --elevate（或在管理员终端里跑 --yes）")
        else:
            say("   处置：稍等几秒再跑一次；仍不行就在任务管理器里结束它们。")
        return 1

    say("\n已清空。接下来：点一次「启动协议端」，扫一次码。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
