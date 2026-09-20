"""量一下「不复用连接」的代价，为修复方案提供依据。

候选修复：让连接池不复用连接（TCPConnector(force_close=True)），
这样就不存在「陈旧连接」可踩，也就**根本不需要 reset 会话**（级联 bug 随之消失）。

代价是每次请求多一次 TCP+TLS 握手。这里实测这个代价有多大。

跑法：.venv\\Scripts\\python.exe tools\\probe_conn_reuse_cost.py
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time

import aiohttp

URL = "https://api.deepseek.com/chat/completions"
KEY = os.environ.get("DEEPSEEK_API_KEY", "")
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
MODEL = os.environ.get("MSGX_PROBE_MODEL", "deepseek-flash")

PAYLOAD = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "只回复两个字：收到"}],
    "max_tokens": 16,
    "temperature": 0,
    "stream": False,
    "reasoning_effort": "none",
}


async def sample(label: str, force_close: bool, n: int = 12) -> float:
    connector = aiohttp.TCPConnector(force_close=force_close)
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60), connector=connector
    )
    dts, fails = [], 0
    try:
        for _ in range(n):
            t0 = time.time()
            try:
                async with session.post(URL, headers=HDR, json=PAYLOAD) as r:
                    await r.text()
                    if r.status != 200:
                        fails += 1
                        continue
                dts.append(time.time() - t0)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"   失败: {type(e).__name__}: {e}")
            await asyncio.sleep(0.2)
    finally:
        await session.close()

    med = statistics.median(dts) if dts else 0.0
    print(f"\n=== {label} ===")
    print(f"  样本 {n} | 失败 {fails} | 中位耗时 {med*1000:.0f}ms | 最小 {min(dts or [0])*1000:.0f}ms")
    return med


async def main() -> int:
    if not KEY:
        print("缺少 DEEPSEEK_API_KEY")
        return 2
    a = await sample("复用连接（默认）", force_close=False)
    b = await sample("不复用连接（force_close=True）", force_close=True)
    if a:
        print(f"\n握手带来的额外开销：约 {(b-a)*1000:+.0f}ms（{(b/a-1)*100:+.0f}%）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
