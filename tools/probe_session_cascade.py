"""验证一个怀疑：`_reset_session()` 会不会把「别人的」在途请求一起掐死。

背景：LLM 只维护**一个**共享 ClientSession，而调用方是并发的
（media 的 Semaphore(2) + scorer + summarizer 同时都在用同一个 session）。
`_post` 在遇到一次瞬时失败时会 `await self._reset_session()` → `session.close()`。
aiohttp 的 `ClientSession.close()` 会关掉连接器，**在途请求全部被中止**。

如果成立，则「1 次真实抖动」会连累 N 个无辜请求各报一次
ServerDisconnectedError，而那些失败又各自触发一次 reset → 失败自我放大。

跑法：.venv\\Scripts\\python.exe tools\\probe_session_cascade.py
"""
from __future__ import annotations

import asyncio
import os
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


async def one(session, idx: int, results: list) -> None:
    t0 = time.time()
    try:
        async with session.post(URL, headers=HDR, json=PAYLOAD) as r:
            await r.text()
            results.append((idx, "ok", round(time.time() - t0, 2), ""))
    except Exception as e:  # noqa: BLE001
        results.append((idx, "FAIL", round(time.time() - t0, 2), f"{type(e).__name__}: {e}"))


async def scenario(label: str, n: int, kill_at: float | None) -> list:
    results: list = []
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
    tasks = [asyncio.create_task(one(session, i, results)) for i in range(n)]

    async def killer():
        await asyncio.sleep(kill_at)
        await session.close()  # 这正是 _reset_session() 干的事

    kill_task = asyncio.create_task(killer()) if kill_at is not None else None
    await asyncio.gather(*tasks, return_exceptions=True)
    if kill_task is not None:
        kill_task.cancel()
    if not session.closed:
        await session.close()

    bad = [r for r in results if r[1] == "FAIL"]
    print(f"\n=== {label} ===")
    print(f"  {n} 个并发请求 | 失败 {len(bad)} 个")
    for idx, st, dt, err in sorted(results):
        print(f"    #{idx} {st} {dt}s {err[:70]}")
    return results


async def main() -> int:
    if not KEY:
        print("缺少 DEEPSEEK_API_KEY")
        return 2

    await scenario("对照组：4 并发，没人 reset 会话", 4, None)
    await scenario("实验组：4 并发，中途有人在途 reset 会话", 4, 0.25)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
