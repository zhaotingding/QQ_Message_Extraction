"""逐请求费用账本：真实临时 SQLite + 假 API，验证重试、未知用量与来源归因。"""
from __future__ import annotations
import asyncio
import copy
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
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


def check_daily_costs():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "trend.db")
        beijing = timezone(timedelta(hours=8))
        today = datetime.now(beijing).date()
        midnight = datetime.combine(today, datetime.min.time(), beijing).timestamp()
        rows = [(midnight + 1, "摘要总结", .01)] * 600
        rows += [(midnight - 1, "摘要总结", 2), (midnight, "摘要分块", 1),
                 (midnight + 2, "超上下文合并", 3), (midnight + 3, "摘要总结", None),
                 (midnight + 4, "图片理解", None), (midnight + 5, "摘要合并", 99),
                 (midnight + 86400, "摘要总结", 98),
                 (midnight - 29 * 86400, "图片理解", .25),
                 (midnight - 29 * 86400 - 1, "图片理解", 97)]
        store.conn.executemany(
            "INSERT INTO api_calls(ts,purpose,cost_cny,source_type,model,status) VALUES(?,?,?,'test','test','test')", rows)
        store.conn.commit()
        trend = store.api_usage_details(1, active_only=True)["daily_costs"]
        check("消费趋势使用全账本且不受最近500条限制", trend == store.api_usage_details(500)["daily_costs"])
        by_day = {(r["day"], r["purpose"]): r for r in trend}
        summary = by_day[(today.isoformat(), "摘要总结")]
        check("消费趋势按北京时间分日并合并现用摘要步骤",
              abs(summary["cost_cny"] - 10) < 1e-8 and summary["calls"] == 603 and summary["unknown_calls"] == 1
              and by_day[((today - timedelta(days=1)).isoformat(), "摘要总结")]["cost_cny"] == 2)
        picture = by_day[(today.isoformat(), "图片理解")]
        check("全未知金额保持None，不能画成免费", picture["cost_cny"] is None and picture["unknown_calls"] == 1)
        check("消费趋势只含最近30个自然日和两种用途", len(trend) == 4
              and by_day[((today - timedelta(days=29)).isoformat(), "图片理解")]["cost_cny"] == .25)
        store.close()

async def main():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = copy.deepcopy(DEFAULTS)
        cfg["app"]["db_path"] = str(Path(tmp) / "calls.db")
        store = Store(cfg["app"]["db_path"])
        text_id = store.insert_message(Msg(msg_id="m1", group_id="g", sender_name="测试发送者", text="原始文字"))
        image_id = store.insert_message(Msg(msg_id="m2", group_id="g", text="运行日志[图片]", msg_type="mixed"))
        store.add_usage("2026-01-01", 30, 10, 0.25)  # 旧版只有合计
        llm = LLM(cfg, store)
        llm.configure("test-key-no-network")
        replies = [response(), response("", "length"), RuntimeError("retry failed"),
                   response("图片内容"), response("这是残缺文本", "length"),
                   {"choices": [{"message": {"content": "无 usage 响应"}, "finish_reason": "stop"}]}]
        async def fake_post(payload):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        llm._post = fake_post
        await llm.chat("sys", "message", purpose="群摘要", source_ids=[text_id])
        try:
            await llm.chat("sys", "summary", purpose="纪要分块", source_ids=[text_id, image_id])
        except RuntimeError:
            pass
        check("第二次请求失败也保留第一次截断响应的费用", len(store.api_usage_details()["items"]) == 3)
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
        data = store.api_usage_details()
        items = data["items"]
        check("每次 API 响应与最终失败均有独立记录", len(items) == 6)
        check("配置可用性判断不会产生图片探测费用", llm.vision_available and len(store.api_usage_details()["items"]) == 6)
        unknown = [r for r in items if r["cost_cny"] is None]
        check("失败或未返回 usage 保持未知，不记成零费用", len(unknown) == 2 and all(r["in_tokens"] is None for r in unknown))
        known = [r for r in items if r["cost_cny"] is not None]
        incomplete = [r for r in items if r["status"].startswith("输出未完成")]
        check("输出未完成仍记入已返回用量及费用，并标注结束原因",
              len(incomplete) == 2 and all("length" in r["status"]
                  and r["cost_cny"] > 0 and r["out_tokens"] == 50 for r in incomplete))
        check("输入输出和缓存 Token 与 API 返回一致", all((r["in_tokens"], r["out_tokens"], r["cache_hit_tokens"]) == (100, 50, 60) for r in known))
        check("逐次费用与日累计相等且没有重复计费", abs(store.cost_totals()["cost_cny"] - 0.25 - sum(r["cost_cny"] for r in known)) < 1e-10)
        check("旧版未归因合计单列，不伪造消息记录", abs(data["historical_unattributed_cny"] - 0.25) < 1e-10)
        check("类型统计包含配文图片和文字", {r["source_type"] for r in data["groups"]} >= {"图文/附件", "文字消息"})
        check("用途统计单独区分各类摘要和图片理解", {r["purpose"] for r in data["groups"]} >= {"群摘要", "纪要分块", "图片理解"})
        original = next(r for r in items if r["purpose"] == "群摘要")
        sources = store.api_call_sources(original["id"])
        check("可以从消费记录追溯真实消息 ID 与全文", sources["items"][0]["msg_id"] == "m1" and sources["items"][0]["text"] == "原始文字")
        check("界面明细分页不改变全量分类汇总", len(store.api_usage_details(1)["items"]) == 1 and store.api_usage_details(1)["groups"] == data["groups"])
        await llm.close()
        store.close()
        reopened = Store(cfg["app"]["db_path"])
        check("重启后逐次费用记录仍保留", len(reopened.api_usage_details()["items"]) == 6)
        from datetime import date, timedelta
        midnight = time.mktime(date.today().timetuple())
        tomorrow = time.mktime((date.today() + timedelta(days=1)).timetuple())
        for ts, purpose, cost in [(midnight-1, "摘要总结", 10.0), (midnight, "摘要总结", 2.0),
                                   (midnight+1, "图片理解", 0.5), (midnight+2, "摘要合并", 99.0),
                                   (midnight+3, "超上下文合并", 1.0), (midnight+4, "摘要分块", 3.0),
                                   (midnight+5, "图片理解", None), (tomorrow, "摘要总结", 20.0)]:
            reopened.record_api_call(ts=ts, purpose=purpose, source_ids=[], model="test",
                                      usage=response()["usage"] if cost is not None else None, cost_cny=cost, status="测试")
        active = reopened.api_usage_details(active_only=True)
        check("消费页仅显示两类现用用途", {r["purpose"] for r in active["groups"]} == {"摘要总结", "图片理解"}
              and all(r["purpose"] in ("摘要总结", "图片理解") for r in active["items"]))
        summary = next(r for r in active["groups"] if r["purpose"] == "摘要总结")
        check("跨午夜统计今日消费，旧日及次日不混入", summary["today_cost_cny"] == 6 and summary["cost_cny"] == 36)
        check("分页不改变今日及总金额", reopened.api_usage_details(1, active_only=True)["groups"] == active["groups"])
        check("今日未知费用保留未知", sum(r["today_unknown_calls"] for r in active["groups"]) == 1
              and any(r["today_cost_cny"] is None for r in active["items"]))
        check("隐藏不删历史、不污染历史未归因金额", len(reopened.api_usage_details()["items"]) == 14
              and abs(active["historical_unattributed_cny"] - .25) < 1e-8)
        reopened.close()

check_daily_costs()
asyncio.run(main())
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
