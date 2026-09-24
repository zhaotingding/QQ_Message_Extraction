"""收消息与启动不推理、不评分、不通知；临时数据库，不访问模型或 QQ。"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from msgx.config import DEFAULTS
from msgx.onebot import _summary_now
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
        pipe._billing_loop = AsyncMock()  # 账单网络独立，测试不访问真实服务。
        try:
            await pipe.start()
            await asyncio.sleep(0)
            check("启动不发起模型探测", pipe.llm._post.await_count == 0)
            check("启动不创建任何自动摘要任务",
                  {task.get_name() for task in pipe._tasks} == {"ingest", "media", "prune", "billing", "watchdog"})
            check("旧摘要定时循环已删除",
                  not hasattr(pipe, "_summary_loop") and not hasattr(pipe, "_daily_loop"))
            check("评分器和通知器已从管道移除", not hasattr(pipe, "scorer") and not hasattr(pipe, "notifier"))
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
            check("按群摘要排除过滤记录", len(pipe.store.messages_between("200", 0, end)) == 6)
            check("跨群摘要排除过滤记录", len(pipe.store.messages_between_all(0, end)) == 6)
            again = await pipe.handle_event(event(12, [cases[2]]))
            check("重复的过滤消息不会新增一行", again.get("duplicate") is True and len(pipe.store.recent_messages()) == 9)
            await pipe.handle_event(event(20, [cases[2], {"type": "text", "data": {"text": "这是报错截图"}}]))
            check("带文字的图片仍进入处理队列", len(pipe.store.pending_images()) == 3)
            cfg["filters"].update(skip_face_only=False, skip_image_only=False)
            await pipe.handle_event(event(21, [cases[2]]))
            check("关闭入库过滤后纯图也执行本地 OCR", len(pipe.store.pending_images()) == 4)
        finally:
            await pipe.stop()

    print("\n=== 手动摘要接口 ===")
    summarizer = SimpleNamespace(run_digest=AsyncMock(return_value={"content": "完整摘要"}))

    class Request:
        app = {"pipe": SimpleNamespace(summarizer=summarizer)}

        def __init__(self, query, body=None):
            self.query = query
            self.body = body
            self.can_read_body = body is not None

        async def json(self):
            return self.body

    for kind in ("periodic", "daily", "typo"):
        response = await _summary_now(Request({"kind": kind}))
        check(f"拒绝旧入口或未知类型 {kind}，不调用模型",
              response.status == 400 and summarizer.run_digest.await_count == 0)
    for body in (["错误对象"], {"prompt": 1}, {"target_chars": True},
                 {"target_chars": 0}, {"target_chars": 100001}, {"target_chars": "3000"}):
        response = await _summary_now(Request({"kind": "range", "hours": "24"}, body))
        check(f"非法摘要设置不发起请求 {body}",
              response.status == 400 and summarizer.run_digest.await_count == 0)
    response = await _summary_now(Request({"kind": "range", "hours": "72"},
                                          {"prompt": "用户设定提示词", "target_chars": 4500}))
    check("按范围摘要完整转交已保存提示词与目标字数",
          response.status == 200 and summarizer.run_digest.call_args.kwargs == {
              "full": True, "hours": 72, "prompt": "用户设定提示词", "target_chars": 4500})
    response = await _summary_now(Request({"kind": "digest"}))
    check("空请求使用服务配置，不以 3000 覆盖用户设置",
          response.status == 200 and summarizer.run_digest.call_args.kwargs == {"full": False})
    response = await _summary_now(Request({"kind": "full"}))
    check("保留手动全量入口", response.status == 200
          and summarizer.run_digest.call_args.kwargs == {"full": True})


asyncio.run(main())
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
