"""v0.5.7：通知抑制与过滤记录的实际入库链路；临时数据库，不访问模型或 QQ。"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from msgx.config import DEFAULTS
from msgx.pipeline import Pipeline

fails: list[str] = []


def check(name: str, ok: bool) -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")
    if not ok:
        fails.append(name)


def event(mid: int, message) -> dict:
    return {"post_type": "message", "message_type": "group", "self_id": 100,
            "group_id": 200, "user_id": 300, "message_id": mid,
            "time": int(time.time()), "message": message}


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = copy.deepcopy(DEFAULTS)
        cfg["app"].update(db_path=str(Path(tmp) / "test.db"),
                          media_dir=str(Path(tmp) / "media"), report_dir=str(Path(tmp) / "reports"))
        cfg["llm"]["enabled"] = False
        pipe = Pipeline(cfg)
        pipe.scorer.score = AsyncMock(return_value={"level": "P0", "score": 10, "reason": "测试高分"})
        pipe.notifier.notify = AsyncMock()
        try:
            for mid, message in enumerate([
                [{"type": "at", "data": {"qq": "all"}}, {"type": "text", "data": {"text": "紧急通知"}}],
                "[CQ:at,qq=all]紧急通知",
            ], 1):
                await pipe.handle_event(event(mid, message))
            check("两种格式的 @全体成员 即使 P0 也不弹通知", pipe.notifier.notify.await_count == 0)
            check("全体消息正常保留且未标记已通知",
                  len(pipe.store.recent_messages()) == 2
                  and all(not r["notified"] for r in pipe.store.recent_alerts()))
            await pipe.handle_event(event(3, [
                {"type": "at", "data": {"qq": "100"}}, {"type": "text", "data": {"text": "请回答"}},
            ]))
            check("单独 @我 仍然通知", pipe.notifier.notify.await_count == 1)
            await pipe.handle_event(event(4, "文档里提到 @全体成员 这几个字"))
            check("普通文本不因出现同名文字而误抑制", pipe.notifier.notify.await_count == 2)
            await pipe.handle_event(event(5, "很长的正文" * 5000 + "[CQ:at,qq=all]"))
            check("原始载荷被截断的长消息仍能抑制全体通知", pipe.notifier.notify.await_count == 2)
            await pipe.handle_event(event(6, "[CQ:at,qq=100][CQ:at,qq=all]同时点名"))
            check("包含全体提醒和单独点名时仍不弹窗", pipe.notifier.notify.await_count == 2)

            cfg["filters"].update(skip_face_only=True, skip_image_only=True)
            pipe.scorer.score.reset_mock()
            cases = [
                {"type": "face", "data": {"id": "1"}},
                {"type": "mface", "data": {"url": "https://invalid.test/face.png"}},
                {"type": "image", "data": {"url": "https://invalid.test/image.png"}},
            ]
            for mid, segment in enumerate(cases, 10):
                result = await pipe.handle_event(event(mid, [segment]))
                check(f"过滤消息 {mid} 留有简要记录", result.get("filtered") is True and bool(result.get("row_id")))
            rows = [dict(r) for r in pipe.store.recent_messages() if r["media_state"] == "filtered"]
            check("表情、表情包和纯图片均标记已过滤", len(rows) == 3)
            check("过滤消息不进入图片队列或评分", not pipe.store.pending_media() and pipe.scorer.score.await_count == 0)
            stored = pipe.store.conn.execute("SELECT * FROM messages WHERE media_state='filtered'").fetchall()
            check("过滤记录不保留图片链接或原始载荷",
                  len(stored) == 3 and all(json.loads(r["image_urls"]) == [] and not r["raw"] for r in stored))
            end = int(time.time()) + 10
            check("按群摘要排除过滤记录", len(pipe.store.messages_between("200", 0, end)) == 6)
            check("跨群摘要排除过滤记录", len(pipe.store.messages_between_all(0, end)) == 6)
            again = await pipe.handle_event(event(12, [cases[2]]))
            check("重复的过滤消息不会新增一行", again.get("duplicate") is True and len(pipe.store.recent_messages()) == 9)
            await pipe.handle_event(event(20, [cases[2], {"type": "text", "data": {"text": "这是报错截图"}}]))
            check("带文字的图片仍进入处理队列", len(pipe.store.pending_media()) == 1)
            cfg["filters"].update(skip_face_only=False, skip_image_only=False)
            await pipe.handle_event(event(21, [cases[2]]))
            check("关闭过滤后纯图片恢复处理", len(pipe.store.pending_media()) == 2)
        finally:
            await pipe.stop()


asyncio.run(main())
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
