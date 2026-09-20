"""协议端（NapCat）启动器 —— 隐藏控制台窗口版。

## 为什么要有这个模块

用户的原话：**「我不想再看见这个独立的 CMD 黑框了」「我不喜欢一大堆 CMD 黑框在我的
任务栏呆着」**。原来的做法是 ``os.startfile("tools/start_napcat.bat")``，链路上会出现：

1. 提权后新开的 cmd 窗口（跑包装脚本）；
2. 包装脚本末尾的 ``pause`` —— 让这个窗口**永远留在任务栏**，直到手动关掉。

而真正的启动动作（``NapCatWinBootMain.exe`` 注入 QQ）其实很快就会返回，窗口却一直挂着。
所以「黑框」对一个正常启动的协议端来说，完全是副作用。

## 现在的链路

::

    python ──ShellExecuteW(runas)──▶ wscript.exe      （GUI 子系统，本身没有控制台）
                                      └─ WshShell.Run(launcher.bat, 0, True)
                                                       └─ 窗口样式 0 = 隐藏

- UAC 授权弹窗**绕不过去**：NapCat 要往 QQ 进程注入 DLL，这是管理员操作。
  但 UAC 是个对话框，不占任务栏 —— 这是设计使然，不是缺陷。
- 成功后**全程无窗口、无任务栏图标**；失败（脚本返回非 0）会弹一个消息框告诉你，
  不会静默失败。
- 输出没有被捕获 —— 所以排障信息靠两处：本模块写自己的 ``napcat.log``（含预检结果），
  以及 ``<NapCat 目录>\\logs\\`` 里 NapCat 自己的日志。界面「协议端日志」按钮把两者都给你。
- 要看完整原始输出时，仍可手动双击 ``tools/start_napcat.bat``（带自检 + pause，
  它是「看得见」的那条路）。**刻意保留**，排障时比隐藏模式有用。

## 为什么预检放在 Python 而不是靠批处理

``launcher.bat`` 在「QQ 路径无效」时会 ``pause``。隐藏模式下没人能按键 → 隐藏的
命令行会一直挂着，用户什么都看不到。所以启动前先在 Python 里把这些坑查掉
（关键文件是否齐全、QQ 路径能否从注册表推导出来），不合格就直接拒绝启动并说清原因。
读注册表必须用 ``winreg``——本机 ``reg.exe`` 被安全策略拉黑。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .config import ROOT

# 起子进程查进程表时不要闪出控制台窗口（本模块整个存在的意义就是「没有黑框」）
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 关键文件清单（缺任何一个都说明 NapCat 解压不完整）
REQUIRED_FILES = (
    "launcher.bat",
    "NapCatWinBootMain.exe",
    "NapCatWinBootHook.dll",
    "napcat.mjs",
    "qqnt.json",
)

# 隐藏启动器。**纯 ASCII 内容** —— .vbs 由 wscript.exe 按 ANSI 读取，
# 写中文注释会引入编码风险，而这里没有任何必要冒这个险。
#
# 关键点：
#   * WshShell.Run(cmd, 0, True) —— 0 = 窗口隐藏；True = 等它结束，
#     这样才能拿到退出码、在失败时弹框（成功则悄无声息地退出）。
#   * 直接 Run 一个 .bat，不经 cmd /c 拼重定向 —— 省掉 cmd 那套
#     「首字符是引号且引号多于两个就掐头去尾」的解析陷阱。
VBS_SOURCE = r'''Option Explicit
' msgx: launch NapCat (launcher.bat) with NO visible console window.
' argv(0) = NapCat install dir (contains launcher.bat)
' NOTE: this file must stay PURE ASCII -- wscript.exe reads .vbs as ANSI,
' so any non-ASCII byte here is a latent encoding bug.
Dim sh, fso, dir, rc
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
If WScript.Arguments.Count < 1 Then
    WScript.Quit 1
End If
dir = WScript.Arguments(0)
If Not fso.FileExists(dir & "\launcher.bat") Then
    MsgBox "launcher.bat not found in:" & vbCrLf & dir, 16, "msgx - NapCat"
    WScript.Quit 2
End If
sh.CurrentDirectory = dir
' 0 = hidden window, True = wait for it to finish
rc = sh.Run("""" & dir & "\launcher.bat""", 0, True)
If rc <> 0 Then
    MsgBox "NapCat launcher.bat exited with code " & rc & vbCrLf & vbCrLf & _
           "Run tools\start_napcat.bat manually to see the full output.", 16, "msgx - NapCat"
End If
WScript.Quit rc
'''

VBS_NAME = "napcat_hidden.vbs"


# ---------------------------------------------------------------- 路径解析

def _resolve(raw: str) -> Path | None:
    raw = str(raw or "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def resolve_dir(cfg: dict) -> Path | None:
    """NapCat 安装目录。

    配置里**明确填了** ``[napcat] dir`` 就尊重它、不做二次猜测 —— 否则「填错路径」
    会静默地启动到别处的另一个 NapCat 上，这比报错难查得多。
    只有完全没配时才依次尝试 ``NAPCAT_HOME`` 与几个常见位置。
    """
    nc = cfg.get("napcat") or {}
    raw = str(nc.get("dir") or "").strip()
    if raw:
        return _resolve(raw)
    env = _resolve(os.environ.get("NAPCAT_HOME", ""))
    if env and (env / "launcher.bat").exists():
        return env
    for guess in ("D:/App/NapCat", "C:/NapCat", "D:/NapCat"):
        g = Path(guess)
        if (g / "launcher.bat").exists():
            return g
    return None


def start_script(cfg: dict) -> Path | None:
    """``[napcat] start_script`` 的绝对路径（可见排障模式用）。"""
    return _resolve((cfg.get("napcat") or {}).get("start_script"))


def log_path(cfg: dict) -> Path:
    nc = cfg.get("napcat") or {}
    p = _resolve(nc.get("log_file") or "data/logs/napcat.log")
    return p or (ROOT / "data" / "logs" / "napcat.log")


def vbs_path() -> Path | None:
    """确保隐藏启动器存在且内容与源码一致，返回它的路径。

    优先写到 ``tools/``（进版本库、可审查）；不可写就退到 ``.ui/``（本地目录）。
    内容不一致时直接覆盖 —— 这个文件是本模块的派生物，不是用户资产。
    """
    payload = VBS_SOURCE.encode("ascii")
    for p in (ROOT / "tools" / VBS_NAME, ROOT / ".ui" / VBS_NAME):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists() or p.read_bytes() != payload:
                p.write_bytes(payload)
            return p
        except OSError:
            continue
    return None


# ---------------------------------------------------------------- 预检

def qq_exe() -> str:
    """从注册表推导 QQ.exe 路径。

    ``launcher.bat`` 用 ``reg query ...\\Uninstall\\QQ /v UninstallString`` 再取目录，
    这里做同一件事 —— 但走 ``winreg``（Win32 API），因为本机 ``reg.exe`` 被安全策略拉黑。
    """
    try:
        import winreg
    except ImportError:
        return ""
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
    )
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as k:
                raw, _ = winreg.QueryValueEx(k, "UninstallString")
        except OSError:
            continue
        cand = Path(str(raw).strip().strip('"')).parent / "QQ.exe"
        if cand.exists():
            return str(cand)
    for guess in (r"D:\App\QQ\QQ.exe", r"C:\Program Files\Tencent\QQNT\QQ.exe"):
        if Path(guess).exists():
            return guess
    return ""


def preflight(dir_: Path | None, cfg: dict) -> dict:
    """启动前检查。返回 ``{"dir", "missing", "qq", "ok"}``。"""
    out: dict = {"dir": str(dir_) if dir_ else "", "missing": [], "qq": "", "ok": False}
    if dir_ is None:
        return out
    for name in REQUIRED_FILES:
        if not (dir_ / name).exists():
            out["missing"].append(name)
    out["qq"] = str((cfg.get("napcat") or {}).get("qq_path") or "").strip() or qq_exe()
    out["ok"] = not out["missing"] and bool(out["qq"])
    return out


# ---------------------------------------------------------------- 已在运行？

def _tasklist_count(image: str) -> int:
    """``tasklist`` 数一下某个进程名有多少个实例。查不到就返回 0（不抛异常）。

    用 ``tasklist`` 而不是 ``wmic``：本机 ``wmic.exe`` 已被安全策略拉黑。
    ``/FI`` 过滤 + ``/FO CSV /NH`` 便于解析；输出为空时 tasklist 会打印一行
    ``INFO: No tasks are running…``（中文系统是「信息: 没有运行的任务…」），
    这类行里不含进程名，过滤掉即可。
    """
    try:
        p = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    n = 0
    for line in (p.stdout or "").splitlines():
        if image.lower() in line.lower():
            n += 1
    return n


def running_instances() -> dict:
    """检测**此刻**是否已有协议端实例在跑。返回 ``{"injectors", "qq", "any"}``。

    为什么需要它：点一次「启动协议端」= 注入一份 ``NapCatWinBootMain.exe`` **并且再拉起
    一份 QQ**。用户看到「没反应」时很自然的反应是反复点 —— 于是变成多份实例并存。
    多份实例各自往 ``<dir>/cache/qrcode.png`` 写二维码、互相覆盖，
    结果是**扫哪一次都可能立刻被下一份覆盖掉，谁也登不上去**，
    表现恰好就是「点了很多次，消息一直不来」。

    所以界面在启动前先问一句，而不是默默再注入一份。

    ``injectors`` = ``NapCatWinBootMain.exe`` 的实例数：
      0 = 还没启动过；1 = 正常；**≥2 = 重复启动过，建议先清理**。
    """
    injectors = _tasklist_count("NapCatWinBootMain.exe")
    qq = _tasklist_count("QQ.exe")
    return {"injectors": injectors, "qq": qq, "any": injectors > 0}


# ---------------------------------------------------------------- 日志

def _append_log(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 超过 1MB 就轮转一次，避免无限增长
        if path.exists() and path.stat().st_size > 1024 * 1024:
            path.replace(path.with_suffix(".log.1"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def napcat_own_log(dir_: Path | None) -> Path | None:
    """NapCat 自己写的日志（``<dir>/logs/`` 里最新的那个）。

    NapCat 4.x 把运行日志写在这里；没有就返回 None，界面据此提示。
    """
    if dir_ is None:
        return None
    logs = dir_ / "logs"
    if not logs.is_dir():
        return None
    try:
        files = [p for p in logs.iterdir() if p.is_file()]
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def log_tail(cfg: dict, lines: int = 400) -> str:
    """把「我们的启动记录 + NapCat 自己的日志尾部」拼成一段可读文本。"""
    out: list[str] = []
    lp = log_path(cfg)
    if lp.exists():
        try:
            body = lp.read_text(encoding="utf-8", errors="replace").splitlines()
            out.append(f"===== 启动记录（{lp}）=====")
            out.extend(body[-lines:])
        except OSError as e:
            out.append(f"（读取 {lp} 失败：{e}）")
    else:
        out.append(f"（还没有启动记录：{lp} 不存在 —— 说明还没从界面启动过协议端）")

    d = resolve_dir(cfg)
    out.append("")
    # 先给出**当前进程**实况。它比日志有用得多，也是判断「是否重复启动过」的唯一依据。
    try:
        ri = running_instances()
        note = ""
        if ri["injectors"] >= 2:
            note = "  ← 重复启动过多份！建议全部关掉后只启动一次（多份会互相覆盖二维码）"
        elif ri["injectors"] == 1:
            note = "  ← 正常"
        out.append(
            "===== 当前进程 =====\n"
            f"  NapCat 注入器（NapCatWinBootMain.exe）: {ri['injectors']} 个{note}\n"
            f"  QQ.exe                              : {ri['qq']} 个"
        )
    except Exception as e:  # noqa: BLE001
        out.append(f"===== 当前进程 =====\n（查询失败：{type(e).__name__}: {e}）")

    out.append("")
    own = napcat_own_log(d)
    if own is None:
        # ⚠️ 这里**绝不能**下「协议端从未启动过」的结论。
        # NapCat 4.x 默认不把运行日志落盘，所以 logs\ 空着是常态。之前写成
        # 「未找到 logs\ —— 协议端可能从未成功启动过」，把一个「没有证据」当成了
        # 「否定的证据」，直接误导用户（消息明明一直在进，却让他以为协议端没起来）。
        # 判断「是否在跑」只该看进程与实际入库的消息，不该看日志文件在不在。
        if d is None:
            why = "（未配置 [napcat] dir，无法定位 NapCat 目录）"
        elif not (d / "logs").is_dir():
            why = f"（{d}\\logs\\ 目录不存在）"
        else:
            why = f"（{d}\\logs\\ 是空目录）"
        out.append(
            "===== NapCat 自身日志 =====\n"
            + why
            + "\nNapCat 4.x 默认不往这里写日志 —— 看不到日志 **不等于** 协议端没启动。\n"
            "判断协议端是否正常，请看上面的「当前进程」「启动记录」，"
            "以及运行状态页的「协议端(NapCat)」是否已连接。"
        )
    else:
        out.append(f"===== NapCat 自身日志（{own}）=====")
        try:
            data = own.read_text(encoding="utf-8", errors="replace").splitlines()
            out.extend(data[-lines:])
        except OSError as e:
            out.append(f"（读取失败：{e}）")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 启动

def _shell_execute_runas(exe: str, params: str) -> int:
    """以管理员身份启动 exe，返回 ShellExecuteW 的返回值（>32 即成功）。"""
    import ctypes

    return int(
        ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
            None, "runas", exe, params, None, 1  # 1 = SW_SHOWNORMAL（wscript 无窗口，无影响）
        )
    )


def launch(cfg: dict, visible: bool = False) -> dict:
    """启动协议端。成功返回 ``{"ok": True, "mode": ..., "dir": ..., "log": ...}``。

    ``visible`` 由调用方（界面勾选框）决定，这里是唯一真值来源 ——
    不要再去读 config 的 ``hidden``：那个键只是勾选框的**初始值**，
    用户取消勾选后 config 不会变，两个来源并存就会打架。
    """
    nc = cfg.get("napcat") or {}
    d = resolve_dir(cfg)
    lp = log_path(cfg)
    header = f"\n[{_stamp()}] ---- 启动协议端 ----\n"

    # --- 显式要求可见模式：沿用旧行为 ---
    if visible:
        script = start_script(cfg)
        if script is None and d is not None:
            cand = d / "launcher.bat"
            script = cand if cand.exists() else None
        if script is None:
            return {"ok": False, "error": "未配置 [napcat] start_script，也找不到 launcher.bat"}
        if not script.exists():
            return {"ok": False, "error": f"启动脚本不存在：{script}"}
        _append_log(lp, header + f"  模式   : 可见（os.startfile）\n  脚本   : {script}\n")
        try:
            os.startfile(str(script))  # type: ignore[attr-defined]
        except OSError as e:
            return {"ok": False, "error": f"启动失败：{e}"}
        return {
            "ok": True,
            "mode": "visible",
            "dir": str(d or ""),
            "log": str(lp),
            "note": "已按可见模式启动 —— 会弹出一个控制台窗口，里面是完整输出（含自检）。",
        }

    # --- 隐藏模式（默认）---
    pf = preflight(d, cfg)
    # 把「启动前已经有多少个实例」记进日志。事后回看时，这一行能直接解释
    # 「为什么消息一直不来」——重复启动的每一次都留了痕。
    try:
        ri = running_instances()
        prior = (
            f"  启动前 : 已有 {ri['injectors']} 个 NapCat 注入器 / {ri['qq']} 个 QQ.exe"
            + ("  ⚠️ 这是重复启动\n" if ri["injectors"] >= 1 else "\n")
        )
    except Exception:  # noqa: BLE001
        prior = ""
    detail = (
        header
        + "  模式   : 隐藏（无控制台窗口）\n"
        + f"  目录   : {pf['dir'] or '(未配置)'}\n"
        + f"  QQ     : {pf['qq'] or '(未能从注册表推导出 QQ.exe)'}\n"
        + f"  缺失   : {', '.join(pf['missing']) if pf['missing'] else '无'}\n"
        + prior
    )
    _append_log(lp, detail)

    if d is None:
        # 隐藏模式必须能定位 launcher.bat，也就是必须知道目录。没配目录但配了
        # 脚本时退回可见模式（脚本自己会提权）—— 不要只丢一句报错给用户。
        if start_script(cfg) is not None:
            res = launch(cfg, visible=True)
            if res.get("ok"):
                res["mode"] = "visible-fallback"
                res["note"] = (
                    "没配置 [napcat] dir，无法定位 launcher.bat，因此改用可见模式启动。"
                    "填上 dir 后即可隐藏启动。"
                )
            return res
        return {
            "ok": False,
            "error": "没有配置 NapCat 目录：请在 config.toml 的 [napcat] dir 里填上安装目录"
                     "（例如 D:\\App\\NapCat），或在界面设置里改用可见模式。",
        }
    if pf["missing"]:
        return {
            "ok": False,
            "error": f"NapCat 目录不完整，缺少：{', '.join(pf['missing'])}\n\n{d}\n\n"
            "请重新完整解压 NapCat.Shell.zip 到该目录。",
        }
    if not pf["qq"]:
        return {
            "ok": False,
            "error": "没能定位到 QQ.exe（注册表里没有 QQ 的安装记录）。\n\n"
            "NapCat 必须注入 QQ 才能工作。请确认 QQ 已安装；"
            "或在 config.toml 的 [napcat] 段直接填 qq_path = \"D:\\\\App\\\\QQ\\\\QQ.exe\"。",
        }

    vbs = vbs_path()
    if vbs is None:
        return {"ok": False, "error": f"无法生成隐藏启动器 {VBS_NAME}（目录不可写）"}

    _append_log(lp, f"  启动器 : {vbs}\n  结果   : 已请求提权并启动\n")
    rc = _shell_execute_runas("wscript.exe", f'"{vbs}" "{d}"')
    if rc <= 32:
        # 5 = ERROR_ACCESS_DENIED（用户在 UAC 点了「否」）
        hint = "（很可能是 UAC 授权窗口被取消了）" if rc == 5 else ""
        return {"ok": False, "error": f"提权启动失败，ShellExecuteW 返回 {rc}{hint}"}

    _append_log(lp, "")
    return {
        "ok": True,
        "mode": "hidden",
        "dir": str(d),
        "log": str(lp),
        "note": "已以隐藏方式拉起（无控制台窗口）。先点 UAC 的「是」，随后完成扫码登录。",
    }
