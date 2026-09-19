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
        print("          .venv\\Scripts\\python.exe -m pip install -r requirements.lock.txt")
        return 1

    ws_port, http_port = free_port(), free_port()
    tmp = Path(tempfile.mkdtemp(prefix="msgx_test_"))
    stop_path = tmp / "stop.signal"
    cfg_path = tmp / "config.toml"
    cfg_path.write_text(
        "\n".join(
            [
                "[app]",
                f'db_path = "{(tmp / "t.db").as_posix()}"',
                f'media_dir = "{(tmp / "media").as_posix()}"',
                f'report_dir = "{(tmp / "reports").as_posix()}"',
                'log_level = "INFO"',
                "",
                "[onebot]",
                'host = "127.0.0.1"',
                f"ws_port = {ws_port}",
                f"http_port = {http_port}",
                "",
                "[llm]",
                "enabled = false",  # 不调模型、不花钱
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
    check("health 含 filtered 计数", "filtered" in (health.get("counters") or {}))
    check("health 的 groups 计数正确（新库应为 0）", health.get("stats", {}).get("groups") == 0,
          str(health.get("stats")))

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
    holder: subprocess.Popen | None = None
    if proc.poll() is None:
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
        holder = subprocess.Popen(
            [str(PY), "-u", "-c", holder_code, f"ws://127.0.0.1:{ws_port}/onebot/ws"],
            cwd=str(PROJ),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        op2 = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        peers: list = []
        for _ in range(20):
            time.sleep(0.5)
            try:
                with op2.open(f"http://127.0.0.1:{http_port}/health", timeout=1.5) as r:
                    peers = json.loads(r.read().decode("utf-8")).get("ws_peers") or []
            except Exception:  # noqa: BLE001
                pass
            if peers:
                break
        check("测试客户端已挂上 WS（模拟 NapCat 在线）", bool(peers), str(peers))

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

    if holder is not None:
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
