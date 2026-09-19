"""NapCat / OneBot11 部署体检（只读，不做任何修改）。

用法:
    python tools/check_napcat_env.py
    python tools/check_napcat_env.py --napcat "D:\\App\\NapCat"
    python tools/check_napcat_env.py --probe 6700 6701

NapCat 目录的确定顺序: --napcat 参数 > 环境变量 NAPCAT_HOME > 常见位置。

设计约束（重要）:
  * 查注册表只用 winreg，不调用 reg.exe —— reg.exe 可能被安全策略拉黑，
    在 .bat 里被 2>nul 吞掉时表现为「读空」，会被误判成「QQ 未安装」。
  * 枚举型检测**必带自检**：自检基准确认可用，才输出否定结论。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.request

REQUIRED_FILES = [
    "launcher.bat",
    "NapCatWinBootMain.exe",
    "NapCatWinBootHook.dll",
    "napcat.mjs",
    "qqnt.json",
]

QQ_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"
COMMON_NAPCAT_DIRS = [
    r"D:\App\NapCat",
    r"C:\NapCat",
    r"D:\NapCat",
    os.path.expanduser(r"~\NapCat"),
]


def resolve_napcat_dir(explicit: str | None) -> pathlib.Path | None:
    """--napcat > NAPCAT_HOME > 常见位置。都不存在则返回 None。"""
    if explicit:
        return pathlib.Path(explicit)
    env = os.environ.get("NAPCAT_HOME")
    if env:
        return pathlib.Path(env)
    for d in COMMON_NAPCAT_DIRS:
        p = pathlib.Path(d)
        if (p / "launcher.bat").exists():
            return p
    return None


def _reg_read(hive, path: str, wow_flag: int):
    """返回 {值名: 值}，键不存在返回 None。"""
    import winreg

    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | wow_flag) as k:
            out, i = {}, 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(k, i)
                    out[name] = val
                    i += 1
                except OSError:
                    break
            return out
    except FileNotFoundError:
        return None
    except OSError as e:
        return {"__error__": str(e)}


def find_qq() -> dict:
    """定位 QQ。返回 {'path','version','source','selfcheck'}"""
    import winreg

    # ---- 自检：读一个必然存在的键，确认 winreg 通路可用 ----
    base = _reg_read(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        winreg.KEY_WOW64_64KEY,
    )
    selfcheck = bool(base and "ProductName" in base)
    print(f"[自检] winreg 可读系统基准键 = {selfcheck}  (ProductName={(base or {}).get('ProductName')})")
    if not selfcheck:
        print("  !! 自检未通过 —— 禁止据此输出任何『未安装』类否定结论。")

    result = {"path": None, "version": None, "source": None, "selfcheck": selfcheck}

    for label, flag in (("32位视图", winreg.KEY_WOW64_32KEY), ("64位视图", winreg.KEY_WOW64_64KEY)):
        vals = _reg_read(winreg.HKEY_LOCAL_MACHINE, QQ_KEY, flag)
        if vals is None:
            print(f"[注册表] {label}: 键不存在")
            continue
        if "__error__" in vals:
            print(f"[注册表] {label}: 读取失败 {vals['__error__']}")
            continue
        print(f"[注册表] {label}: 键存在")
        for k in ("DisplayName", "DisplayVersion", "DisplayIcon", "UninstallString"):
            if k in vals:
                print(f"          {k:16} = {vals[k]}")
        icon = str(vals.get("DisplayIcon", ""))
        if icon and result["path"] is None:
            exe = pathlib.Path(icon.split(",")[0].strip())
            if exe.exists():
                result.update(path=exe, version=vals.get("DisplayVersion"), source=f"{label}/DisplayIcon")
        if result["path"] is None:
            unins = str(vals.get("UninstallString", "")).strip().strip('"')
            if unins:
                cand = pathlib.Path(unins).parent / "QQ.exe"
                if cand.exists():
                    result.update(path=cand, version=vals.get("DisplayVersion"), source=f"{label}/UninstallString")

    if result["path"] is None:
        for guess in (
            pathlib.Path(r"D:\App\QQ\QQ.exe"),
            pathlib.Path(r"C:\Program Files\Tencent\QQNT\QQ.exe"),
        ):
            if guess.exists():
                result.update(path=guess, version=None, source="常见路径探测")
                break

    if result["path"]:
        print(f"[QQ] 找到 -> {result['path']}  (来源: {result['source']}, 版本: {result['version']})")
    elif selfcheck:
        print("[QQ] 未找到（自检已通过，此结论可信）")
    else:
        print("[QQ] 未找到（**自检未通过，此结论不可信**）")
    return result


def check_napcat(root: pathlib.Path) -> None:
    print(f"\n[NapCat] 目录: {root}")
    if not root.is_dir():
        print("  !! 目录不存在")
        return

    missing = []
    for f in REQUIRED_FILES:
        ok = (root / f).exists()
        print(f"  [{'有' if ok else '缺'}] {f}")
        if not ok:
            missing.append(f)
    if missing:
        print(f"  !! 缺 {len(missing)} 个关键文件 -> 解压不完整，请重新解压 NapCat.Shell.zip")

    print("\n[NapCat] 启动入口自查")
    found_wrapper = False
    for name in ("start-napcat.bat", "start_napcat.bat"):
        wrapper = root / name
        if not wrapper.exists():
            continue
        found_wrapper = True
        raw = wrapper.read_bytes()
        enc = None
        for e in ("gbk", "utf-8"):
            try:
                raw.decode(e)
                enc = e
                break
            except UnicodeDecodeError:
                continue
        crlf = raw.count(b"\r\n")
        bare = raw.count(b"\n") - crlf
        has_cd = b"cd /d" in raw
        print(f"  {name}: 编码={enc}, CRLF={crlf}, 裸LF={bare}, 含 cd /d = {has_cd}")
        if enc == "utf-8":
            print("  !! 该脚本含中文却是 UTF-8 —— 控制台代码页 936 下会乱码，应转 GBK")
        if bare:
            print(f"  !! 存在 {bare} 处裸 LF —— 建议全量转 CRLF")
        if not has_cd:
            print("  !! 缺少 cd /d —— 管理员运行时 CWD 会变成 System32，导致闪退")
    if not found_wrapper:
        print("  (本目录没有启动包装器。项目 tools\\start_napcat.bat 可以替代)")
        print("  !! 不要直接运行 launcher.bat（它用 %cd% 推导路径，管理员运行时必闪退）")

    cfg = root / "config" / "onebot11.json"
    print(f"\n[NapCat] 反向 WS 配置: {cfg}")
    if not cfg.exists():
        print("  (全局模板不存在。账号首次登录后需在 WebUI http://127.0.0.1:6099/webui/ 手工添加)")
        return
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"  !! JSON 解析失败: {e}")
        return
    clients = data.get("network", {}).get("websocketClients", [])
    if not clients:
        print("  !! network.websocketClients 为空 —— 注意别误填到 websocketServers（那是正向 WS）")
    for c in clients:
        url = c.get("url", "")
        print(f"  -> name={c.get('name')!r} enable={c.get('enable')} url={url}")
        print(
            f"     messagePostFormat={c.get('messagePostFormat')!r} "
            f"reportSelfMessage={c.get('reportSelfMessage')}"
        )
        if c.get("messagePostFormat") != "array":
            print("     !! 建议 messagePostFormat 设为 'array'")
        if url.rstrip("/").endswith("/onebot"):
            print("     !! 危险: 路径以 /onebot 结尾。反向 WS 必须打到 WebSocket 路由 /onebot/ws；")
            print("        填 /onebot 会命中 HTTP POST 路由并返回 405。")


def probe(ws_port: int, http_port: int) -> None:
    print(f"\n[探测] 本地服务 http://127.0.0.1:{http_port}/health")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 必须绕过系统代理
        with opener.open(f"http://127.0.0.1:{http_port}/health", timeout=5) as r:
            body = json.loads(r.read().decode("utf-8"))
        print(
            f"  status={r.status} ok={body.get('ok')} llm={body.get('llm_available')} "
            f"vision={body.get('vision_supported')}"
        )
    except Exception as e:  # noqa: BLE001
        print(f"  !! 失败: {type(e).__name__}: {e}")
        print("     （服务未启动，或该端口被系统代理接管）")

    try:
        import aiohttp
    except ImportError:
        print("[探测] 未安装 aiohttp，跳过 WS 握手测试")
        return

    import asyncio

    async def try_ws(url: str) -> None:
        try:
            async with aiohttp.ClientSession(trust_env=False) as s:
                async with s.ws_connect(url, timeout=5) as ws:
                    await asyncio.sleep(0.3)
                    print(f"  WS {url} -> 握手成功 (closed={ws.closed})")
        except Exception as e:  # noqa: BLE001
            print(f"  WS {url} -> 握手失败: {type(e).__name__}: {e}")

    print("[探测] WebSocket 握手")
    asyncio.run(try_ws(f"ws://127.0.0.1:{ws_port}/onebot/ws"))
    print("  判据: 成功的真正标志是服务端日志出现『协议端已连接: <ip>』。")


def main() -> int:
    ap = argparse.ArgumentParser(description="NapCat / OneBot11 部署体检（只读）")
    ap.add_argument("--napcat", default=None, help="NapCat 解压目录（缺省时自动探测）")
    ap.add_argument(
        "--probe",
        nargs=2,
        type=int,
        metavar=("WS_PORT", "HTTP_PORT"),
        help="额外探测本地服务",
    )
    args = ap.parse_args()

    find_qq()

    root = resolve_napcat_dir(args.napcat)
    if root is None:
        print("\n[NapCat] 未自动找到 NapCat 目录。")
        print("  已尝试: 环境变量 NAPCAT_HOME, " + ", ".join(COMMON_NAPCAT_DIRS))
        print("  请用 --napcat \"你的NapCat目录\" 指定。")
    else:
        check_napcat(root)

    if args.probe:
        probe(args.probe[0], args.probe[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
