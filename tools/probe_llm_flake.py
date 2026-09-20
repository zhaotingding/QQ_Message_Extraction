"""诊断 api.deepseek.com 的连接层抖动（ServerDisconnectedError）。

要回答三个问题：
  1. 失败率和「请求体大小」有没有关系？（用户怀疑是图片太大）
  2. 失败发生在连接的哪个时刻？（<1s = 握手就被拒；10~16s = 中间设备按空闲超时掐）
  3. 复用连接池 vs 每次新建连接，哪个更容易失败？

跑法：.venv\\Scripts\\python.exe tools\\probe_llm_flake.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import statistics
import sys
import time

import aiohttp

URL = "https://api.deepseek.com/chat/completions"
KEY = os.environ.get("DEEPSEEK_API_KEY", "")
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
MODEL = os.environ.get("MSGX_PROBE_MODEL", "deepseek-flash")


def make_jpeg(target_kb: int) -> bytes:
    """造一张噪声 JPEG，尺寸尽量贴近 target_kb（噪声不可压，方便定尺）。"""
    from PIL import Image

    side = 900
    for _ in range(8):
        px = [((i * 37 + (i // side) * 91) % 256) for i in range(side * side)]
        im = Image.new("L", (side, side))
        im.putdata(px)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=85)
        got = len(buf.getvalue()) / 1024
        if abs(got - target_kb) / target_kb < 0.25 or side > 4000:
            return buf.getvalue()
        side = int(side * (target_kb / got) ** 0.5)
    return buf.getvalue()


def text_payload() -> dict:
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": "只回复两个字：收到"}],
        "max_tokens": 16,
        "temperature": 0,
        "stream": False,
        "reasoning_effort": "none",
    }


def image_payload(jpg: bytes) -> dict:
    b64 = base64.b64encode(jpg).decode()
    return {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这张图里有文字吗？只回答有或没有。"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
                    },
                ],
            }
        ],
        "max_tokens": 32,
        "temperature": 0,
        "stream": False,
        "reasoning_effort": "none",
    }


async def one(session: aiohttp.ClientSession, payload: dict) -> dict:
    t0 = time.time()
    try:
        async with session.post(URL, headers=HDR, json=payload) as r:
            body = await r.text()
            ok = r.status == 200
            return {
                "ok": ok,
                "status": r.status,
                "dt": round(time.time() - t0, 2),
                "err": "" if ok else body[:120],
            }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "status": 0,
            "dt": round(time.time() - t0, 2),
            "err": f"{type(e).__name__}: {e}",
        }


async def phase(name: str, n: int, payload: dict, fresh: bool, timeout: float = 120.0):
    results = []
    shared = None
    if not fresh:
        shared = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))
    try:
        for _ in range(n):
            if fresh:
                s = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))
            else:
                s = shared
            results.append(await one(s, payload))
            if fresh:
                await s.close()
            await asyncio.sleep(0.3)
    finally:
        if shared is not None:
            await shared.close()

    bad = [r for r in results if not r["ok"]]
    dts = [r["dt"] for r in results]
    print(f"\n=== {name} ===")
    print(f"  样本 {n} 次 | 失败 {len(bad)} 次 | 失败率 {len(bad)/n:.0%}")
    print(f"  成功耗时 中位 {statistics.median([r['dt'] for r in results if r['ok']] or [0]):.2f}s")
    if bad:
        print(f"  失败耗时: {[r['dt'] for r in bad]}")
        kinds = {}
        for r in bad:
            key = (r["err"].split(":")[0] or f"HTTP {r['status']}")[:60]
            kinds[key] = kinds.get(key, 0) + 1
        for k, v in kinds.items():
            print(f"    {v}x  {k}")
    return results


async def main() -> int:
    if not KEY:
        print("缺少 DEEPSEEK_API_KEY")
        return 2

    small = make_jpeg(120)
    tiny = make_jpeg(12)
    print(f"测试图：小图 {len(tiny)/1024:.0f}KB / 常规图 {len(small)/1024:.0f}KB"
          f"（base64 后 {len(small)*4/3/1024:.0f}KB）")

    await phase("A 纯文本 · 复用连接池", 16, text_payload(), fresh=False)
    await phase("B 纯文本 · 每次新建连接", 16, text_payload(), fresh=True)
    await phase("C 12KB 图 · 复用连接池", 6, image_payload(tiny), fresh=False)
    await phase("D 120KB 图 · 复用连接池", 6, image_payload(small), fresh=False)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
