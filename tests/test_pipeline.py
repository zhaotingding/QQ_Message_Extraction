"""收消息与启动不推理、不评分、不通知；临时数据库，不访问模型或 QQ。"""
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
        pipe = Pipeline(cfg)
        pipe.llm.configure("test-key-no-network")
        pipe.llm._post = AsyncMock(side_effect=AssertionError("消息接收不得调用模型"))
        pipe.call_action = AsyncMock(side_effect=AssertionError("消息接收不得向 QQ 发送动作"))  # 用行为阻止意外通知。
        pipe._billing_loop = AsyncMock()  # 账单网络独立，测试不访问真实服务。
        try:
            await pipe.start()
            await asyncio.sleep(0)
            check("启动不发起模型探测", pipe.llm._post.await_count == 0)
            check("启动只创建统一自动周期摘要任务",
                  {task.get_name() for task in pipe._tasks} ==
                  {"ingest", "media", "prune", "billing", "watchdog", "summary-schedule"})
            for mid, message in enumerate([
                [{"type": "at", "data": {"qq": "all"}}, {"type": "text", "data": {"text": "紧急通知"}}],
                "[CQ:at,qq=all]紧急通知",
            ], 1):
                await pipe.handle_event(event(mid, message))
            check("两种格式的 @全体成员 均不触发推理", pipe.llm._post.await_count == 0)
            check("全体消息正常保留且不生成告警",
                  len(pipe.store.recent_messages()) == 2
                  and not pipe.store.recent_alerts())
            await pipe.handle_event(event(3, [
                {"type": "at", "data": {"qq": "100"}}, {"type": "text", "data": {"text": "请回答"}},
            ]))
            check("单独 @我 不推理或生成告警", pipe.llm._post.await_count == 0 and not pipe.store.recent_alerts())
            await pipe.handle_event(event(4, "文档里提到 @全体成员 这几个字"))
            check("普通文本直接入库不推理", pipe.llm._post.await_count == 0)
            await pipe.handle_event(event(5, "很长的正文" * 5000 + "[CQ:at,qq=all]"))
            check("长消息直接入库不推理", pipe.llm._post.await_count == 0)
            await pipe.handle_event(event(6, "[CQ:at,qq=100][CQ:at,qq=all]同时点名"))
            check("同时点名和全体提醒仍不生成告警", not pipe.store.recent_alerts())
            check("收消息没有产生任何 API 消费记录", not pipe.store.api_usage_details()["items"])

            for mid, name, user_id in ((100, "改名的群管家", 2854196310), (101, "Q群管家", 999)):
                bot_event = event(mid, [{"type": "image", "data": {"url": "https://invalid.test/bot.png"}},
                                        {"type": "text", "data": {"text": "签到广告"}}])
                bot_event.update(user_id=user_id, sender={"nickname": name})
                result = await pipe.handle_event(bot_event)
                check("管家消息接收后直接过滤", "Q群管家" in result.get("reason", ""))
            check("管家不入库、不进入 OCR 队列、不调用模型", pipe.store.conn.execute(
                "SELECT COUNT(*) FROM messages").fetchone()[0] == 6
                and not pipe.store.pending_images() and pipe.llm._post.await_count == 0)

            cfg["filters"].update(skip_face_only=True, skip_image_only=True)
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
            check("过滤图片进入本地 OCR 队列但不调用模型",
                  len(pipe.store.pending_images()) == 2 and pipe.llm._post.await_count == 0)
            stored = pipe.store.conn.execute("SELECT * FROM messages WHERE media_state='filtered'").fetchall()
            check("过滤图片保留下载链接但不保存完整原始载荷",
                  len(stored) == 3 and sum(bool(json.loads(r["image_urls"])) for r in stored) == 2
                  and all(not r["raw"] for r in stored))
            end = int(time.time()) + 10
            check("按群实时列表保留过滤标记", sum(row["media_state"] == "filtered"
                  for row in pipe.store.recent_messages(group_id="200")) == 3)  # UI 仍可查看被类型过滤的记录。
            check("跨群摘要排除过滤记录", len(pipe.store.messages_between_all(0, end)) == 6)
            again = await pipe.handle_event(event(12, [cases[2]]))
            check("重复的过滤消息不会新增一行", again.get("duplicate") is True and len(pipe.store.recent_messages()) == 9)
            await pipe.handle_event(event(20, [cases[2], {"type": "text", "data": {"text": "这是报错截图"}}]))
            check("带文字的图片仍进入处理队列", len(pipe.store.pending_images()) == 3)
            cfg["filters"].update(skip_face_only=False, skip_image_only=False)
            await pipe.handle_event(event(21, [cases[2]]))
            check("关闭入库过滤后纯图也执行本地 OCR", len(pipe.store.pending_images()) == 4)
            pipe.call_action.assert_not_awaited()  # 普通接收与过滤都不能向群发送消息。
        finally:
            await pipe.stop()

asyncio.run(main())
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
