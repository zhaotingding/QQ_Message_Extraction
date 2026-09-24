"""图片下载校验的单元测试。

不联网：只测「这段字节到底是不是一张能被解码的完整图片」这个判断本身。
半张图是 OCR 报 `image file is truncated`、多模态接口报 `unsupported image`
的共同根因，必须在落盘前拦掉。
"""
from __future__ import annotations

import sys
import json
import time
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
from msgx.models import Msg  # noqa: E402

tmp = tempfile.mkdtemp()
st = Store(os.path.join(tmp, "t.db"))
parent = st.insert_message(Msg(msg_id="m1", group_id="g1", ts=1,
                               image_urls=["https://example.invalid/image"]))
child_id = st.conn.execute("SELECT id FROM message_images WHERE message_row=?", (parent,)).fetchone()[0]
st.set_image_result(child_id, media_state="failed")
n = st.requeue_failed_media()
row = st.conn.execute("SELECT media_state FROM messages WHERE msg_id='m1'").fetchone()
check("failed 图片被重新入队", n == 1)
check("父子状态恢复为 pending", row["media_state"] == "pending"
      and st.get_image(child_id)["media_state"] == "pending")
check("重复调用不产生副作用", st.requeue_failed_media() == 0)
st.close()

print("\n=== 旧消息图片迁移 ===")
with tempfile.TemporaryDirectory() as directory:
    db = Path(directory) / "legacy.db"
    store = Store(db)
    parent = store.insert_message(Msg(msg_id="legacy", group_id="g", ts=100,
                                      image_urls=["https://example.invalid/1", "https://example.invalid/2"]))
    store.set_media_result(parent, ocr_text="旧 OCR", vision_text="旧语义", media_path="saved.png",
                           state="done", vision_state="ok")
    store.conn.execute("DELETE FROM message_images")
    store.conn.commit()
    store.close()
    store = Store(db)
    migrated = store.conn.execute("SELECT * FROM message_images WHERE message_row=? ORDER BY image_index",
                                  (parent,)).fetchall()
    check("旧版第一张结果保留、其余图片独立待 OCR", len(migrated) == 2
          and migrated[0]["ocr_text"] == "旧 OCR" and migrated[0]["vision_text"] == "旧语义"
          and migrated[0]["media_state"] == "done" and migrated[1]["media_state"] == "pending")
    store.close()

print("\n=== 图片先 OCR、手动模型识别 ===")

from msgx.config import DEFAULTS  # noqa: E402
from msgx.media import MediaWorker  # noqa: E402
from msgx.models import parse_onebot_event  # noqa: E402
import copy  # noqa: E402

async def manual_image_tests():
    with tempfile.TemporaryDirectory() as directory:
        cfg = copy.deepcopy(DEFAULTS)
        cfg["app"]["media_dir"] = str(Path(directory) / "media")
        store = Store(str(Path(directory) / "media.db"))
        image = Path(directory) / "source.png"
        image.write_bytes(png())

        class FakeLLM:
            vision_available = True
            def __init__(self):
                self.calls = []
                self.ready = asyncio.Event()
                self.release = asyncio.Event()
            async def describe_image(self, path, prompt, **kwargs):
                self.calls.append((path, prompt, kwargs))
                self.ready.set()
                await self.release.wait()
                return "图片中展示了错误日志", True

        llm = FakeLLM()
        worker = MediaWorker(cfg, store, llm)
        downloaded = []
        async def download(url):
            downloaded.append(url)
            return image
        worker._download = download
        worker._ocr_sync = lambda path: "本地 OCR 文字"
        def insert(mid, text, filtered=False):
            return store.insert_message(Msg(msg_id=mid, group_id="g", text=text,
                                            image_urls=[image.as_uri()], msg_type="mixed"), filtered=filtered)
        def child(message_row):
            return store.conn.execute("SELECT * FROM message_images WHERE message_row=?", (message_row,)).fetchone()

        row_id = insert("filtered", "[图片]", filtered=True)
        check("过滤图片也进入 OCR 队列", [r["message_row"] for r in store.pending_images()] == [row_id])
        await worker.process_pending()
        row = store.get_message(row_id)
        check("过滤图片保留文件和 OCR、默认无模型调用",
              row["media_state"] == "filtered" and row["media_path"] == str(image)
              and row["ocr_text"] == "本地 OCR 文字" and row["vision_state"] == "skipped"
              and not llm.calls and not store.pending_images())

        task = asyncio.create_task(worker.manual_vision(child(row_id)["id"]))
        await llm.ready.wait()
        concurrent = await worker.manual_vision(child(row_id)["id"])
        llm.release.set()
        result = await task
        check("同图并发手动操作只提交一次模型请求", concurrent["state"] == "running"
              and result["state"] == "ok" and len(llm.calls) == 1)
        check("图片理解关联原消息", llm.calls[0][2] == {"source_ids": [row_id], "purpose": "图片理解"})
        check("成功结果保留过滤状态与 OCR", store.get_message(row_id)["media_state"] == "filtered"
              and store.get_message(row_id)["ocr_text"] == "本地 OCR 文字")
        cached = await worker.manual_vision(child(row_id)["id"])
        check("已识别图片再次点击不重复收费", cached.get("cached") and len(llm.calls) == 1)

        duplicate = insert("duplicate", "另一条[图片]")
        await worker.process_pending()
        check("相同图片复用 OCR 但不自动复用语义", store.get_message(duplicate)["ocr_text"] == "本地 OCR 文字"
              and not store.get_message(duplicate)["vision_text"] and len(llm.calls) == 1)
        old = store.insert_message(Msg(msg_id="old", group_id="g", text="[图片]",
                                       image_urls=[image.as_uri()]), filtered=True)
        store.conn.execute("DELETE FROM message_images WHERE message_row=?", (old,))
        store.conn.execute("UPDATE messages SET image_urls='[]',has_media=0 WHERE id=?", (old,))
        store.conn.commit()
        check("旧版未保存链接的过滤图片不冒充可恢复", old not in [r["message_row"] for r in store.pending_images()])

        multi = store.insert_message(Msg(msg_id="multi", group_id="g", text="两张图[图片][图片]",
                                         image_urls=[image.as_uri(), image.as_uri()], msg_type="mixed"))
        check("一条消息两张图分别建立可操作记录",
              len(store.conn.execute("SELECT id FROM message_images WHERE message_row=?", (multi,)).fetchall()) == 2)
        await worker.process_pending()
        check("两张图分别保存 OCR 与路径且没有自动模型请求",
              all(r["ocr_text"] == "本地 OCR 文字" and r["media_path"] == str(image)
                  for r in store.conn.execute("SELECT * FROM message_images WHERE message_row=?", (multi,)))
              and len(llm.calls) == 1)

        from types import SimpleNamespace
        from msgx.onebot import build_app
        runner = web.AppRunner(build_app(cfg, SimpleNamespace(store=store, media=worker)))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        children = store.conn.execute("SELECT id FROM message_images WHERE message_row=? ORDER BY image_index",
                                      (multi,)).fetchall()
        second = children[1]["id"]
        try:
            async with ClientSession() as session:
                root = f"http://127.0.0.1:{port}"
                async with session.post(f"{root}/images/{second}/vision", json={}) as response:
                    result = await response.json()
                check("本地接口只识别指定的第二张图", response.status == 200 and result["state"] == "ok"
                      and len(llm.calls) == 2 and store.get_image(children[0]["id"])["vision_text"] == "")
                async with session.post(f"{root}/images/{second}/summary-include", json={"include": False}) as response:
                    result = await response.json()
                check("接口可独立排除已识别语义", response.status == 200 and not result["include"]
                      and not store.get_message(multi)["vision_text"] and store.get_image(second)["vision_text"])
                async with session.post(f"{root}/images/{second}/summary-include", json={"include": True},
                                        headers={"Origin": "https://example.invalid"}) as response:
                    check("网页跨域请求不能修改摘要开关", response.status == 403
                          and not store.get_image(second)["include_vision"])
        finally:
            await runner.cleanup()

        event = {"post_type": "message", "message_type": "group", "message_id": "mface",
                 "group_id": "g", "message": [{"type": "mface", "data": {"url": image.as_uri()}}]}
        check("有链接的表情包也能本地 OCR", parse_onebot_event(event, "123").image_urls == [image.as_uri()])
        await worker.close()
        store.close()

asyncio.run(manual_image_tests())

print("\n=== 多图清理保留共享文件 ===")
with tempfile.TemporaryDirectory() as directory:
    store = Store(Path(directory) / "prune.db")
    shared = Path(directory) / "shared.png"
    shared.write_bytes(png())
    now = int(time.time())
    old_id = store.insert_message(Msg(msg_id="old", group_id="g", ts=now - 10 * 86400,
                                      image_urls=["https://example.invalid/old"]))
    new_id = store.insert_message(Msg(msg_id="new", group_id="g", ts=now - 86400,
                                      image_urls=["https://example.invalid/new"]))
    for parent in (old_id, new_id):
        image_id = store.conn.execute("SELECT id FROM message_images WHERE message_row=?", (parent,)).fetchone()[0]
        store.set_image_result(image_id, media_path=str(shared), media_state="done")
    store.prune(7, 7)
    check("删除旧消息时清理子图片但保留仍引用的文件", store.get_message(old_id) is None
          and store.get_message(new_id) is not None and shared.exists()
          and not store.conn.execute("SELECT 1 FROM message_images WHERE message_row=?", (old_id,)).fetchone())
    store.prune(7, 0)
    check("媒体保留期结束时清理子图片路径与文件", not shared.exists()
          and not store.conn.execute("SELECT media_path FROM message_images WHERE message_row=?", (new_id,)).fetchone()[0])
    store.close()

print()
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
