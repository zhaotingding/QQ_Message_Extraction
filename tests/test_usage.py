"""逐请求费用账本：真实临时 SQLite + 假 API，验证重试、未知用量与来源归因。"""
from __future__ import annotations
import asyncio
import copy
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from msgx.config import DEFAULTS
from msgx.llm import LLM
from msgx.models import Msg
from msgx.store import Store

fails = []
def check(name, ok):
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")
    if not ok:
        fails.append(name)

def response(text="完整正文", finish="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "prompt_cache_hit_tokens": 60}}

async def main():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = copy.deepcopy(DEFAULTS)
        cfg["app"]["db_path"] = str(Path(tmp) / "calls.db")
        store = Store(cfg["app"]["db_path"])
        text_id = store.insert_message(Msg(msg_id="m1", group_id="g", sender_name="测试发送者", text="原始文字"))
        image_id = store.insert_message(Msg(msg_id="m2", group_id="g", text="[图片]", msg_type="image"))
        store.add_usage("2026-01-01", 30, 10, 0.25)  # 旧版只有合计
        llm = LLM(cfg, store)
        llm.configure("test-key-no-network")
        replies = [response(), response("", "length"), RuntimeError("retry failed"),
                   response("图片内容"), response("这是残缺文本", "length"),
                   {"choices": [{"message": {"content": "无 usage 响应"}, "finish_reason": "stop"}]},
                   response("MSGX-7301")]
        async def fake_post(payload):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        llm._post = fake_post
        await llm.chat("sys", "message", purpose="消息评分", source_ids=[text_id])
        try:
            await llm.chat("sys", "summary", purpose="纪要分块", source_ids=[text_id, image_id])
        except RuntimeError:
            pass
        check("第二次请求失败也保留第一次截断响应的费用", len(store.api_usage_details()["items"]) == 3)
        llm._vision = True
        image = Path(tmp) / "image.png"
        from PIL import Image
        Image.new("RGB", (12, 12), "white").save(image)
        await llm.describe_image(image, "识别图片", source_ids=[image_id])
        try:
            await llm.chat("sys", "summary", purpose="摘要成稿", source_ids=[text_id], require_complete=True)
            check("API 返回 length 时拒绝把非空残稿当完成稿", False)
        except RuntimeError as exc:
            check("API 返回 length 时拒绝把非空残稿当完成稿", "输出未完成" in str(exc))
        await llm.chat("sys", "unknown", purpose="未知用量测试")
        await llm.probe_vision(force=True)
        data = store.api_usage_details()
        items = data["items"]
        check("每次 API 响应与最终失败均有独立记录", len(items) == 7)
        check("图片能力检测也纳入账单", items[0]["purpose"] == "图片能力检测" and items[0]["cost_cny"] is not None)
        unknown = [r for r in items if r["cost_cny"] is None]
        check("失败或未返回 usage 保持未知，不记成零费用", len(unknown) == 2 and all(r["in_tokens"] is None for r in unknown))
        known = [r for r in items if r["cost_cny"] is not None]
        check("输入输出和缓存 Token 与 API 返回一致", all((r["in_tokens"], r["out_tokens"], r["cache_hit_tokens"]) == (100, 50, 60) for r in known))
        check("逐次费用与日累计相等且没有重复计费", abs(store.cost_totals()["cost_cny"] - 0.25 - sum(r["cost_cny"] for r in known)) < 1e-10)
        check("旧版未归因合计单列，不伪造消息记录", abs(data["historical_unattributed_cny"] - 0.25) < 1e-10)
        check("类型统计包含图片和文字", {r["source_type"] for r in data["groups"]} >= {"纯图片", "文字消息"})
        check("用途统计单独区分评分、摘要和图片理解", {r["purpose"] for r in data["groups"]} >= {"消息评分", "纪要分块", "图片理解"})
        original = next(r for r in items if r["purpose"] == "消息评分")
        sources = store.api_call_sources(original["id"])
        check("可以从消费记录追溯真实消息 ID 与全文", sources["items"][0]["msg_id"] == "m1" and sources["items"][0]["text"] == "原始文字")
        check("界面明细分页不改变全量分类汇总", len(store.api_usage_details(1)["items"]) == 1 and store.api_usage_details(1)["groups"] == data["groups"])
        await llm.close()
        store.close()
        reopened = Store(cfg["app"]["db_path"])
        check("重启后逐次费用记录仍保留", len(reopened.api_usage_details()["items"]) == 7)
        reopened.close()

asyncio.run(main())
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
