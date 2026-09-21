"""图片下载校验的单元测试。

不联网：只测「这段字节到底是不是一张能被解码的完整图片」这个判断本身。
半张图是 OCR 报 `image file is truncated`、多模态接口报 `unsupported image`
的共同根因，必须在落盘前拦掉。
"""
from __future__ import annotations

import sys
import json
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

print("\n=== 仅消息带配文的图片允许解析 ===")

from msgx.config import DEFAULTS  # noqa: E402
from msgx.media import MediaWorker, _has_caption  # noqa: E402
from msgx.models import Msg, parse_onebot_event  # noqa: E402
import copy  # noqa: E402

async def captioned_image_tests():
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
            async def describe_image(self, path, prompt, **kwargs):
                self.calls.append((path, prompt, kwargs))
                return "图片中展示了错误日志", True

        llm = FakeLLM()
        worker = MediaWorker(cfg, store, llm)
        downloaded = []
        async def download(url):
            downloaded.append(url)
            return image
        worker._download = download
        worker._ocr_sync = lambda path: ""

        def insert(mid, text):
            return store.insert_message(Msg(msg_id=mid, group_id="g", text=text,
                                            image_urls=[image.as_uri()], msg_type="mixed"))

        for index, text in enumerate(("[图片]", "[图片][表情][表情包]", "@全体成员[图片]",
                                      "[图片][文件:资料.pdf][语音][视频][合并转发]", "  ",
                                      "[图片][record][video][forward]")):
            row_id = insert(f"bare-{index}", text)
            await worker._process_row(store.get_message(row_id))
            row = store.get_message(row_id)
            check(f"无配文或仅占位符 {index + 1} 跳过且不耗重试次数",
                  row["vision_state"] == "skipped" and row["vision_tries"] == 0)
        check("无配文图片完全不下载、不调用 API", not downloaded and not llm.calls)

        event = {"post_type": "message", "message_type": "group", "message_id": "at-only",
                 "group_id": "g", "message": [{"type": "at", "data": {"qq": "123"}},
                                              {"type": "image", "data": {"url": image.as_uri()}}]}
        at_only = store.insert_message(parse_onebot_event(event, "123"))
        await worker._process_row(store.get_message(at_only))
        check("只有点名和图片也不算配文", not downloaded and not llm.calls)

        for segment_type, marker in (("record", "[语音]"), ("video", "[视频]"),
                                     ("forward", "[合并转发]"), ("json", "")):
            cq_event = {"post_type": "message", "message_type": "group", "group_id": "g",
                        "message_id": f"cq-{segment_type}",
                        "message": f"[CQ:{segment_type},file=attachment][CQ:image,url={image.as_uri()}]"}
            cq_msg = parse_onebot_event(cq_event, "123")
            array_event = {**cq_event, "message": [{"type": segment_type, "data": {"file": "attachment"}},
                                                    {"type": "image", "data": {"url": image.as_uri()}}]}
            array_msg = parse_onebot_event(array_event, "123")
            check(f"CQ {segment_type} 与数组格式占位符一致", cq_msg.text == array_msg.text == marker + "[图片]")
            cq_id = store.insert_message(cq_msg)
            await worker._process_row(store.get_message(cq_id))
            check(f"图片与 {segment_type} 混合但无配文不会解析", not downloaded and not llm.calls)
            # 复现旧版入库格式，包括未知 CQ 类型产生的英文占位符。
            store.conn.execute("UPDATE messages SET text=?, media_path=?, vision_state='failed' WHERE id=?",
                               (f"[{segment_type}][图片]", str(image), cq_id))
            store.conn.commit()
            recovered = await worker.retry_vision()
            check(f"旧 {segment_type} 占位符不能通过重试绕过配文门槛",
                  not recovered and not llm.calls and store.get_message(cq_id)["vision_state"] == "skipped")

        for message in (f"[ERROR][CQ:image,url={image.as_uri()}]",
                        [{"type": "text", "data": {"text": "[ERROR]"}},
                         {"type": "image", "data": {"url": image.as_uri()}}]):
            error_event = {**event, "message_id": "error-caption", "message": message}
            parsed = parse_onebot_event(error_event, "123")
            check("正文里的 [ERROR] 是真实配文，不会被当协议占位符删除",
                  _has_caption({"raw": json.dumps(parsed.raw), "text": parsed.text}))
        check("旧 raw 截断时仍识别已知英文占位符",
              not _has_caption({"raw": '{"message":', "text": "[record][video][forward][图片]"}))

        captioned = insert("captioned", "部署时出现这个错误[图片]")
        await worker._process_row(store.get_message(captioned))
        check("真实配文图片无需能力探测且不依赖 OCR 检出文字", len(llm.calls) == 1
              and store.get_message(captioned)["ocr_text"] == "")
        check("图片请求关联原消息且不再解释表情包",
              llm.calls[0][2] == {"source_ids": [captioned], "purpose": "图片理解"}
              and "梗" not in llm.calls[0][1] and "情绪" not in llm.calls[0][1])
        duplicate = insert("duplicate", "另一个群也遇到了[图片]")
        await worker._process_row(store.get_message(duplicate))
        check("配文同图复用已成功结果，不重复花费", len(llm.calls) == 1
              and store.get_message(duplicate)["vision_state"] == "ok")
        bare_duplicate = insert("bare-duplicate", "[图片]")
        count = len(downloaded)
        await worker._process_row(store.get_message(bare_duplicate))
        check("无配文同图不能通过去重复用绕过门槛", len(downloaded) == count
              and store.get_message(bare_duplicate)["vision_text"] == "")

        old_bare = insert("old-bare", "[图片]")
        store.set_media_result(old_bare, ocr_text="旧 OCR 内容", vision_text="旧结果", media_path=str(image),
                               state="done", vision_state="failed")
        recovered = await worker.retry_vision()
        old_row = store.get_message(old_bare)
        check("旧无配文失败队列不再烧 Token", not recovered and len(llm.calls) == 1
              and old_row["vision_state"] == "skipped" and old_row["vision_tries"] == 0)
        check("跳过旧记录时保留已有内容和路径", old_row["ocr_text"] == "旧 OCR 内容"
              and old_row["vision_text"] == "旧结果" and old_row["media_path"] == str(image))
        old_caption = insert("old-caption", "这些报错是什么[图片]")
        store.set_media_result(old_caption, media_path=str(image), state="done", vision_state="failed")
        recovered = await worker.retry_vision()
        check("带配文的失败图片仍可在次数限制内补跑", recovered == [old_caption]
              and store.get_message(old_caption)["vision_tries"] == 1 and len(llm.calls) == 2)
        check("补跑费用仍关联原消息", llm.calls[-1][2] == {"source_ids": [old_caption], "purpose": "图片理解重试"})
        await worker.close()
        store.close()

asyncio.run(captioned_image_tests())

print()
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
