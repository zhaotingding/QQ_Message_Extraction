"""一条命令回答：**消息为什么不来了？**

## 为什么需要它

2026-09-21 排查「消息冻结」时，现场是「服务在跑 + 协议端已连接 + 一条消息都不来」，
而这个状态**从界面上完全看不出来**：所有指示灯都是绿的。当时是分了好几轮、手写了
好几个一次性脚本才把范围缩到「协议端连着但不转发事件」。这套判断步骤值得固化 ——
否则下一次还是从零开始猜。

## 它按什么顺序排除

1. **进程**：注入器几份？`QQ.exe` 是不是全由注入器拉起来的？
   → 点一次「启动协议端」= 注入一份 NapCat **并且再拉起一份 QQ**；
   ≥2 份就会互相覆盖二维码 / 抢同一个账号的登录态，谁也收不到消息。
2. **服务**：`/health` 可达吗？`ws_conns` 几条？
   → 注意 `ws_peers` 是**去重过的 IP 集合**，7 份实例连进来也只显示 1 个；
     要看连接**条数**。
3. **队列探针**（决定性）：往 `/onebot` 发一条固定 id 的测试事件。
   → `received` 涨了 ⇒ 消费者活着 ⇒ 事件确实没从协议端出来；
     不涨 ⇒ 事件堵在 `ingest_q` 里没人取（消费者死了）。
   ⚠️ 必须用 `/onebot`，**不能用 `/inject`** —— 后者直接调 handle_event、绕过队列，
   测了等于没测。固定 `msg_id` 让探针可反复运行而只入库一次。
4. **二维码**：隔几秒采两次 mtime，还在涨 ⇒ 协议端正停在扫码界面等登录。
5. **数据库**：最后一条消息是什么时候，落在哪些群。

## 用法

    python tools\\diagnose_freeze.py              # 全量诊断（含一次队列探针）
    python tools\\diagnose_freeze.py --no-probe   # 不动服务，只读现场

结论会以「结论 + 处置」两行列在最后，直接照做即可。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:6700"
INJECTOR = "NapCatWinBootMain.exe"
# 固定 id / 固定文本：探针可以反复跑，第二次起会被判为 duplicate 而不再入库。
PROBE_MSG_ID = 990001
PROBE_TEXT = "【链路自检】这条消息由 diagnose_freeze 经 /onebot 队列入口注入，用于确认 ingest 消费者是否存活。"

op = build_opener(ProxyHandler({}))  # 本机服务：必须绕开系统代理

TH32CS_SNAPPROCESS = 0x00000002
_INVALID = ctypes.c_void_p(-1).value


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),  # ULONG_PTR，64 位下不能是 DWORD
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


def snapshot() -> dict[int, tuple[str, int]]:
    """{pid: (exe 名, 父 pid)}。用 Win32 取，不碰 wmic（已被安全策略拉黑）。"""
    k32 = ctypes.windll.kernel32
    h = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if h == _INVALID:
        return {}
    try:
        e = _PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        out: dict[int, tuple[str, int]] = {}
        ok = k32.Process32FirstW(h, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = (str(e.szExeFile), int(e.th32ParentProcessID))
            ok = k32.Process32NextW(h, ctypes.byref(e))
        return out
    finally:
        k32.CloseHandle(h)


def ancestry(pid: int, table: dict[int, tuple[str, int]], limit: int = 12) -> list[str]:
    chain, seen, cur = [], set(), pid
    for _ in range(limit):
        if cur in seen or cur not in table:
            break
        seen.add(cur)
        name, parent = table[cur]
        chain.append(name)
        if parent in (0, cur):
            break
        cur = parent
    return chain


def section(title: str) -> None:
    print(f"\n{'─' * 62}\n{title}\n{'─' * 62}")


def health() -> dict | None:
    try:
        with op.open(f"{BASE}/health", timeout=4) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def probe() -> tuple[str, str]:
    """往 /onebot 发一条测试事件。返回 (判定, 说明)。"""
    before = health()
    if before is None:
        return ("skip", "服务不可达，跳过探针")
    c0 = dict(before["counters"])
    ev = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": PROBE_MSG_ID,
        "group_id": 0,  # 占位；下面按 watch 列表挑一个真实群
        "user_id": 10001,
        "self_id": 0,
        "time": int(time.time()),
        "raw_message": PROBE_TEXT,
        "message": [{"type": "text", "data": {"text": PROBE_TEXT}}],
        "sender": {"user_id": 10001, "nickname": "链路自检", "card": ""},
    }
    # 群必须**固定**（取排序后的第一个），否则探针会在不同群里各留一条记录，
    # 「固定 msg_id ⇒ 重复判定 ⇒ 只入库一次」这个幂等前提就废了。
    wg = before.get("watch_groups")
    if isinstance(wg, list) and wg:
        ev["group_id"] = int(sorted(wg)[0])
    try:
        req = Request(
            f"{BASE}/onebot",
            data=json.dumps(ev).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with op.open(req, timeout=8) as r:
            r.read()
    except Exception as e:  # noqa: BLE001
        return ("bad", f"POST /onebot 失败：{type(e).__name__}: {e}")

    for _ in range(12):
        time.sleep(0.5)
        c1 = (health() or {}).get("counters") or {}
        if c1.get("received", 0) > c0["received"]:
            extra = "（判定为重复消息，未新增记录）" if c1.get("duplicate", 0) > c0["duplicate"] \
                else "（新增 1 条记录）"
            return ("alive", f"received {c0['received']} → {c1['received']}{extra} ⇒ ingest 消费者【活着】")
    return ("dead", "6 秒内 received / stored / filtered 全都没动 ⇒ 事件进了队列但【没有消费者】")


def main() -> int:
    ap = argparse.ArgumentParser(description="诊断「消息不来了」")
    ap.add_argument("--no-probe", action="store_true", help="不向服务注入探针事件（纯只读）")
    args = ap.parse_args()

    print("=" * 62)
    print("消息冻结诊断  ", time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        from msgx import __version__

        print("代码版本      ", __version__)
    except Exception:  # noqa: BLE001
        print("代码版本       (无法导入 msgx —— 不在项目 venv 里跑？)")
    print("=" * 62)

    verdicts: list[tuple[str, str]] = []

    # ---------- 1. 进程 ----------
    section("① 进程：是不是重复启动过？")
    table = snapshot()
    inj_pids = [p for p, v in table.items() if v[0].lower() == INJECTOR.lower()]
    qq_pids = [p for p, v in table.items() if v[0].lower() == "qq.exe"]
    mine, other = [], []
    for pid in qq_pids:
        (mine if INJECTOR.lower() in [n.lower() for n in ancestry(pid, table)]
         else other).append(pid)
    print(f"进程总数                        : {len(table)}")
    print(f"{INJECTOR:30s}: {len(inj_pids)} 个   (0=没启动过 / 1=正常 / ≥2=重复启动过)")
    print(f"{'QQ.exe':30s}: {len(qq_pids)} 个   （1 主进程 + 3~4 辅助进程属正常）")
    print(f"  其中属于 NapCat 注入器的      : {len(mine)} 个")
    print(f"  父链里没有注入器的（你自己的）: {len(other)} 个")
    if inj_pids:
        for pid in inj_pids:
            kids = sum(1 for q in qq_pids if table[q][1] == pid)
            print(f"    注入器 pid={pid:<7} 直接子 QQ.exe × {kids}")
    if len(inj_pids) >= 2:
        verdicts.append((
            f"协议端重复启动了 {len(inj_pids)} 份",
            "多份实例会互相覆盖二维码、抢同一个账号的登录态，谁都收不到消息。"
            "处置：python tools\\kill_dup_napcat.py --elevate（点一次 UAC），"
            "然后点**一次**「启动协议端」、扫**一次**码。",
        ))
    elif len(inj_pids) == 1 and len(qq_pids) > 8:
        verdicts.append((
            f"只有 1 份注入器但 QQ.exe 有 {len(qq_pids)} 个（常见是 4 个）",
            "可能有上次残留的 QQ 没退干净。处置：kill_dup_napcat.py --elevate 清一次再启动。",
        ))
    elif not inj_pids:
        verdicts.append(("协议端没在运行", "点「启动协议端(NapCat)」，然后扫码登录。"))

    # ---------- 2. 服务 ----------
    section("② 服务：连接与计数")
    h = health()
    if h is None:
        verdicts.append(("服务不可达", "确认服务已启动（界面里点「启动服务」）。"))
        return _finish(verdicts)

    print(f"uptime                : {h['uptime_seconds']} 秒")
    print(f"ws_peers（去重后的 IP）: {h['ws_peers']}")
    print(f"ws_conns（连接条数）   : {h.get('ws_conns', '(旧代码没有这个字段)')}")
    print(f"queue（队列积压）      : {h.get('queue', '(旧代码没有这个字段)')}")
    print(f"counters              : {json.dumps(h['counters'], ensure_ascii=False)}")
    print(f"stats                 : {json.dumps(h['stats'], ensure_ascii=False)}")

    n_conn = h.get("ws_conns")
    if n_conn == 0:
        verdicts.append(("服务侧没有任何协议端连接",
                         "协议端没连上来。看它是停在扫码界面，还是 6700 端口/地址填错了。"))
    elif isinstance(n_conn, int) and n_conn > len(h.get("ws_peers") or []):
        verdicts.append((f"有 {n_conn} 条协议端连接（去重后只剩 {len(h['ws_peers'])} 个 IP）",
                         "连接条数 > IP 数 ⇒ 多份实例在同时连。处置：kill_dup_napcat.py --elevate。"))

    # ---------- 3. 队列探针 ----------
    section("③ 队列探针：事件到底有没有进来（决定性）")
    if args.no_probe:
        print("（--no-probe：跳过）")
    else:
        state, msg = probe()
        print(msg)
        if state == "dead":
            verdicts.append(("事件进了队列但没人消费",
                             "ingest 循环死了 —— 这是**代码问题**，不是协议端问题。"
                             "重启服务；若复现请保留 data/service.log。"))
        elif state == "alive":
            print("    ⇒ 排除服务侧：处理链路完好，问题在协议端一侧。")
            if n_conn:
                verdicts.append((
                    "协议端连着、能应答 API，但就是不推消息事件",
                    "本次冻结就是这个形态。契约上「已连接」只证明 socket 通了，"
                    "不证明 QQ 登录态正常。处置：清掉重复实例（kill_dup_napcat.py --elevate）"
                    "→ 只启动一份 → 只扫一次码；再看 D:\\App\\NapCat\\logs\\（已开 fileLog）。",
                ))

    # ---------- 4. 二维码 ----------
    section("④ 二维码：是不是卡在扫码界面？")
    qr = Path(r"D:\App\NapCat\cache\qrcode.png")
    if qr.exists():
        a = qr.stat().st_mtime
        time.sleep(6)
        b = qr.stat().st_mtime
        refreshing = b > a
        print(f"{qr}")
        print(f"  mtime {time.time() - b:.0f} 秒前；6 秒内{'仍在刷新 ⇒ 协议端正等你扫码' if refreshing else '没有变化'}")
        if refreshing:
            verdicts.append(("协议端正停在扫码界面",
                             "登录态已经丢了（QQ 未登录时 NapCat 不会转发任何消息）。"
                             "扫一次码即可；若二维码刚扫完就被换掉，说明有多份实例在互相覆盖。"))
    else:
        print(f"{qr} 不存在（没有实例在等扫码）")

    # ---------- 5. 数据库 ----------
    section("⑤ 数据库：最后一条消息是什么时候")
    db = ROOT / "data" / "msgx.db"
    if db.exists():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*), MAX(ts) FROM messages")
            total, mx = cur.fetchone()
            print(f"messages 总数 = {total}")
            if mx:
                gap = time.time() - mx
                print(f"最新一条      = {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mx))}"
                      f"  （{gap / 60:.1f} 分钟前）")
                if gap > 600:
                    verdicts.append((f"已经 {gap / 3600:.1f} 小时没有新消息入库",
                                     "结合上面几节判断：若是「连着但不推事件」，清场重启即可。"))
            cur.execute("SELECT group_id, COUNT(*) FROM messages GROUP BY group_id ORDER BY 2 DESC LIMIT 8")
            for gid, c in cur.fetchall():
                print(f"  群 {gid}: {c} 条")
            conn.close()
        except Exception as e:  # noqa: BLE001
            print("DB 读取失败:", e)
    else:
        print(f"{db} 不存在")

    return _finish(verdicts)


def _finish(verdicts: list[tuple[str, str]]) -> int:
    section("结论")
    if not verdicts:
        print("没有发现异常 —— 各环节都正常。若仍收不到消息，把本输出发出来一起看。")
        return 0
    for i, (what, todo) in enumerate(verdicts, 1):
        print(f"{i}. {what}")
        print(f"   → {todo}")
    return 1 if verdicts else 0


if __name__ == "__main__":
    sys.exit(main())
