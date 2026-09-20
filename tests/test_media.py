"""图片下载校验的单元测试。

不联网：只测「这段字节到底是不是一张能被解码的完整图片」这个判断本身。
半张图是 OCR 报 `image file is truncated`、多模态接口报 `unsupported image`
的共同根因，必须在落盘前拦掉。
"""
from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / "src"))

from msgx.media import _decodable, _sniff_ext  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def png(color: str = "red") -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, "PNG")
    return buf.getvalue()


print("=== 完整性判断 _decodable ===")
good = png()
check("完整 PNG 可解码", _decodable(good), f"{len(good)} 字节")
check("截断的 PNG 不可解码", not _decodable(good[: len(good) // 2]))
check("只留文件头不可解码", not _decodable(good[:32]))
check("随机字节不可解码", not _decodable(b"\x01\x02\x03\x04" * 16))
check("空数据不可解码", not _decodable(b""))

print("\n=== 格式嗅探 _sniff_ext（不信任扩展名）===")
check("PNG 头 → .png", _sniff_ext(good) == ".png")
check("GIF 头 → .gif", _sniff_ext(b"GIF89a" + b"\0" * 20) == ".gif")
check("JPEG 头 → .jpg", _sniff_ext(b"\xff\xd8\xff\xe0" + b"\0" * 20) == ".jpg")
check("WEBP 头 → .webp", _sniff_ext(b"RIFF\x00\x00\x00\x00WEBP" + b"\0" * 20) == ".webp")
check("未知内容回落到 .jpg", _sniff_ext(b"\x01\x02\x03\x04") == ".jpg")

# ---------- fetch_full：真实 HTTP 下载（本地 aiohttp 服务，不外联） ----------
print("\n=== fetch_full（真实 HTTP 下载）===")

import asyncio  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402

from aiohttp import ClientSession, web  # noqa: E402

import aiohttp  # noqa: E402

from msgx.media import fetch_full  # noqa: E402


def make_app(data: bytes, chunk: int, drop: bool):
    async def handler(request):
        resp = web.StreamResponse()
        resp.content_length = len(data)  # 声明全长，模拟真实图床
        await resp.prepare(request)
        end = len(data) if not drop else len(data) // 2
        for i in range(0, end, chunk):
            await resp.write(data[i : i + chunk])
            await asyncio.sleep(0)  # 让出事件循环，强制分多次 TCP 发送
        if drop:
            # 模拟图床半途断流：必须硬切 socket。仅 return 的话 aiohttp 会
            # 让连接保持打开，客户端（无超时时）会永远等剩余字节。
            request.transport.abort()
            return resp
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/img", handler)
    return app


async def run_fetch_tests() -> None:
    big = png("blue") * 200  # ~18KB：必须分多个 chunk，且 > 16384（旧 bug 的截断点）
    timeout = aiohttp.ClientTimeout(total=10)
    for label, drop in (("完整流", False), ("半途断流", True)):
        runner = web.AppRunner(make_app(big, 1000, drop))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with ClientSession(timeout=timeout) as s:
                try:
                    got = await fetch_full(s, f"http://127.0.0.1:{port}/img", 10 * 1024 * 1024)
                    err = None
                except Exception as e:  # noqa: BLE001
                    got, err = None, e
            if drop:
                check("半途断流报错（半张图不会当完整图返回）", got is None and err is not None)
            else:
                check("完整流整包读回", got == big, f"{len(got)} 字节")
        finally:
            await runner.cleanup()


asyncio.run(run_fetch_tests())

# ---------- requeue_failed_media：启动时挽回误判 failed 的图片 ----------
print("\n=== requeue_failed_media ===")

from msgx.store import Store  # noqa: E402

tmp = tempfile.mkdtemp()
st = Store(os.path.join(tmp, "t.db"))
st.conn.execute(
    "INSERT INTO messages(group_id, msg_id, ts, has_media, media_state)"
    " VALUES('g1','m1',1,1,'failed')"
)
st.conn.commit()
n = st.requeue_failed_media()
row = st.conn.execute("SELECT media_state FROM messages WHERE msg_id='m1'").fetchone()
check("failed 图片被重新入队", n == 1)
check("状态恢复为 pending", row["media_state"] == "pending")
check("重复调用不产生副作用", st.requeue_failed_media() == 0)
st.close()

print()
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
