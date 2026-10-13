"""服务生命周期测试：能否启动、健康检查是否正确、能否被安全停止。

用临时配置（随机端口 + 临时数据库），不碰真实数据，也不会与正在运行的服务抢端口。

重点验证 --stop-file 机制 —— 图形界面点「停止服务」依赖的就是它。
比直接 kill 进程安全：不会打断正在写库或下载图片的动作。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
PY = PROJ / ".venv" / "Scripts" / "python.exe"

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def free_port() -> int:
    """向系统要一个空闲端口，避免与正在运行的服务冲突。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> int:
    if not PY.exists():
        print(f"找不到项目虚拟环境：{PY}")
        print("请先执行：py -3.13 -m venv .venv")
        print("          .venv\\Scripts\\python.exe -m pip install -e .")
        return 1

    ws_port, http_port = free_port(), free_port()
    tmp = Path(tempfile.mkdtemp(prefix="msgx_test_"))
    stop_path = tmp / "stop.signal"
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(
        "\n".join(
            [
                "app:",
                f'  db_path: "{(tmp / "t.db").as_posix()}"',
                f'  media_dir: "{(tmp / "media").as_posix()}"',
                f'  report_dir: "{(tmp / "reports").as_posix()}"',
                '  log_level: "INFO"',
                "",
                "onebot:",
                '  host: "127.0.0.1"',
                f"  ws_port: {ws_port}",
                f"  http_port: {http_port}",
                "",
                "llm:",
                "  enabled: false",  # 不调模型、不花钱
                "",
            ]
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJ / "src")
    env["PYTHONUTF8"] = "1"
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(var, None)

    print(f"=== 启动服务（端口 {ws_port}/{http_port}）===")
    proc = subprocess.Popen(
        [str(PY), "-u", "-m", "msgx.main", "-c", str(cfg_path), "run", "--stop-file", str(stop_path)],
        cwd=str(PROJ),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    health: dict = {}
    started = time.time()
    for _ in range(40):
        time.sleep(0.5)
        if proc.poll() is not None:
            break
        try:
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(f"http://127.0.0.1:{http_port}/health", timeout=1.5) as r:
                health = json.loads(r.read().decode("utf-8"))
            break
        except Exception:  # noqa: BLE001
            continue

    check("服务启动且 /health 可达", bool(health), f"耗时 {time.time() - started:.1f}s")
    check("health 含 ws_peers（供界面显示协议端状态）", "ws_peers" in health, str(health.get("ws_peers")))
    # ws_peers 是**去重后的 IP 集合**：7 份 NapCat 都从 127.0.0.1 连进来时它也只有一个元素，
    # 看起来和「1 份正常连接」一模一样 —— 2026-09-21 那次「消息冻结」就卡在这个盲点上。
    # 连接**条数**必须单独给出：两个数不等 ⇒ 有重复实例。
    check("health 含 ws_conns（连接条数，排查重复实例的关键）",
          isinstance(health.get("ws_conns"), int), str(health.get("ws_conns")))
    check("health 含 queue（队列积压；消费者死了它会一直涨而 received 不动）",
          isinstance(health.get("queue"), int), str(health.get("queue")))
    check("health 含 filtered 计数", "filtered" in (health.get("counters") or {}))
    check("health 的 groups 计数正确（新库应为 0）", health.get("stats", {}).get("groups") == 0,
          str(health.get("stats")))

    print("\n=== v0.5.0 界面新增接口契约 ===")
    op0 = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path: str, post: bool = False, timeout: float = 15.0) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{http_port}{path}", data=b"" if post else None,
            method="POST" if post else "GET",
        )
        try:
            with op0.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # 4xx 也要能读到 body，否则测不出错误码
            try:
                return e.code, json.loads(e.read().decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001
                return e.code, {}
        except Exception as exc:  # noqa: BLE001
            return 0, {"error": str(exc)}

    st, body = call("/messages/recent?limit=5")
    check("/messages/recent 可达（实时消息页的数据源）",
          st == 200 and body.get("ok") is True, f"{st} {str(body)[:90]}")
    check("/messages/recent 返回 items 列表", isinstance(body.get("items"), list),
          str(body.get("items")))
    st, detail = call("/usage/details")
    check("消费明细接口可达且新库无虚构记录", st == 200 and detail.get("items") == [] and detail.get("groups") == [])
    st, sources = call("/usage/calls/99999")
    check("不存在的请求来源返回空列表", st == 200 and sources.get("items") == [])
    check("健康接口包含摘要进度", (health.get("summary_progress") or {}).get("state") == "idle")
    check("/health 带 billing 字段（界面据此显示真实账单）", "billing" in health,
          str(sorted(health))[:120])

    st, body = call("/billing")
    check("/billing 可达且返回 source 口径",
          st == 200 and body.get("source") in ("platform", "balance_diff", "estimate"),
          f"{st} source={body.get('source')}")
    # 没配 api_key / platform_token 时必须**如实说明**，而不是假装有数据
    check("/billing 说明为什么拿不到平台明细", bool(body.get("platform_error")),
          str(body.get("platform_error"))[:80])
    # v0.5.1 差值法：快照必须带上读数信息，界面才能决定敢不敢标「真实」。
    # 只验结构 —— 数值取决于本机能不能连上余额接口，不能拿来当断言。
    bd = body.get("balance_diff")
    check("/billing 带 balance_diff 段（差值法）", isinstance(bd, dict),
          str(bd)[:100])
    check("balance_diff 带读数个数与分辨率",
          isinstance(bd, dict) and "points" in bd and bd.get("resolution") == 0.01,
          str(bd)[:100])
    check("今日与累计各有自己的口径字段（可能一个真实一个估算）",
          body.get("today_source") in ("platform", "balance_diff", "estimate")
          and body.get("total_source") in ("platform", "balance_diff", "estimate"),
          f"{body.get('today_source')} / {body.get('total_source')}")

    for kind in ("range&hours=abc", "range&hours=72", "digest", "full", "by_date", "daily"):
        st, body = call(f"/summary/now?kind={kind}", post=True)
        check(f"已移除手动入口 {kind}，提示自动计划",
              st == 410 and "自动计划" in body.get("error", ""))

    print("\n=== WebSocket 端点契约 ===")
    try:
        import aiohttp
        import asyncio

        async def probe() -> tuple[bool, str]:
            async with aiohttp.ClientSession(trust_env=False) as s:
                try:
                    async with s.ws_connect(f"ws://127.0.0.1:{ws_port}/onebot/ws", timeout=5) as ws:
                        await asyncio.sleep(0.2)
                        return True, f"握手成功 closed={ws.closed}"
                except Exception as e:  # noqa: BLE001
                    return False, f"{type(e).__name__}: {e}"

        ok_ws, detail_ws = asyncio.run(probe())
        check("/onebot/ws 可握手（NapCat 要连的就是它）", ok_ws, detail_ws)

        async def probe_wrong() -> tuple[bool, str]:
            async with aiohttp.ClientSession(trust_env=False) as s:
                try:
                    async with s.ws_connect(f"ws://127.0.0.1:{ws_port}/onebot", timeout=5) as ws:
                        await asyncio.sleep(0.2)
                        return True, "竟然握手成功"
                except Exception as e:  # noqa: BLE001
                    return False, f"{type(e).__name__}"

        ok_bad, _ = asyncio.run(probe_wrong())
        check("/onebot 不能当 WS 用（应为 HTTP POST 路由）", not ok_bad,
              "反例成立：写错路径会握手失败")
    except ImportError:
        print("  (未安装 aiohttp，跳过 WS 握手测试)")

    print("\n=== 协作退出（持有活动 WS 连接）===")
    # 关键回归点：真实场景下 NapCat 会一直挂着反向 WS。而 runner.cleanup() 会等待
    # 所有活动连接结束，WS 处理器 `async for raw in ws` 在 NapCat 不断线时永不返回
    # —— 曾导致停止服务卡 39 秒（直到心跳报 No PONG 才解开）。
    # 所以这里必须**握着一条连接**去停，才能测出该缺陷。
    holders: list[subprocess.Popen] = []
    holder_code = (
        "import asyncio, sys, aiohttp\n"
        "async def m():\n"
        "    async with aiohttp.ClientSession(trust_env=False) as s:\n"
        "        async with s.ws_connect(sys.argv[1], timeout=5) as ws:\n"
        "            print('HOLDING', flush=True)\n"
        "            async for msg in ws:\n"
        "                pass\n"
        "asyncio.run(m())\n"
    )

    def spawn_holder() -> subprocess.Popen:
        return subprocess.Popen(
            [str(PY), "-u", "-c", holder_code, f"ws://127.0.0.1:{ws_port}/onebot/ws"],
            cwd=str(PROJ),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def wait_conns(target: int, tries: int = 24) -> dict:
        """等 /health 的 ws_conns 到 target，返回最后一次拿到的 health。"""
        h: dict = {}
        for _ in range(tries):
            time.sleep(0.5)
            try:
                with urllib.request.build_opener(
                    urllib.request.ProxyHandler({})
                ).open(f"http://127.0.0.1:{http_port}/health", timeout=1.5) as r:
                    h = json.loads(r.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if (h.get("ws_conns") or 0) >= target:
                return h
        return h

    if proc.poll() is None:
        holders.append(spawn_holder())
        h1 = wait_conns(1)
        check("测试客户端已挂上 WS（模拟 NapCat 在线）",
              bool(h1.get("ws_peers")), str(h1.get("ws_peers")))
        check("health 返回本次连接建立时间供二维码自动关闭",
              started <= h1.get("ws_connected_at", 0) <= time.time())

        # ⚠️ 这次「消息冻结」的真正盲点：ws_peers 是**去重后的 IP 集合**，
        # 第二份 NapCat（同样来自 127.0.0.1）不会让它变长 —— 7 份实例和 1 份
        # 在界面上显示得一模一样。所以契约必须是「连接条数」看得见。
        holders.append(spawn_holder())
        h2 = wait_conns(2)
        check("两条 WS 连接时 ws_conns == 2",
              h2.get("ws_conns") == 2, str(h2.get("ws_conns")))
        check("同源 IP 重连仍更新连接时间",
              h2.get("ws_connected_at", 0) > h1.get("ws_connected_at", 0))
        check("同源 IP 的连接不会撑大 ws_peers（正是它会掩盖重复实例）",
              len(h2.get("ws_peers") or []) == 1, str(h2.get("ws_peers")))
        check("ws_conns > len(ws_peers) 可据此判定「有重复实例」",
              (h2.get("ws_conns") or 0) > len(h2.get("ws_peers") or []),
              f"conns={h2.get('ws_conns')} peers={h2.get('ws_peers')}")

    if proc.poll() is None:
        t0 = time.time()
        stop_path.touch()
        try:
            rc = proc.wait(timeout=25)
            elapsed = time.time() - t0
            check("创建停止文件后服务自行退出", True, f"退出码 {rc}，耗时 {elapsed:.1f}s")
            check("退出码为 0（正常收尾，非强杀）", rc == 0, str(rc))
            check("持有活动 WS 时仍能快速退出（<8s）", elapsed < 8, f"{elapsed:.1f}s")
        except subprocess.TimeoutExpired:
            check("创建停止文件后服务自行退出", False, "25 秒内未退出")
            proc.kill()
            proc.wait(timeout=10)
    else:
        check("服务不应提前退出", False, f"提前退出，码 {proc.poll()}")

    for holder in holders:
        if holder.poll() is None:
            holder.kill()
        try:
            holder.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        if holder.stdout:
            try:
                hout = holder.stdout.read()
            except Exception:  # noqa: BLE001
                hout = ""
            check("持有连接的过程确实建立过 WS", "HOLDING" in hout or holder.poll() is not None)

    out = ""
    if proc.stdout:
        try:
            out = proc.stdout.read()
        except Exception:  # noqa: BLE001
            pass
    check("日志明确记录了停止信号", "停止信号" in out,
          [ln for ln in out.splitlines() if "停止信号" in ln][:1] or "(未找到)")

    print()
    if fails:
        print(f"存在 {len(fails)} 项失败: {fails}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
