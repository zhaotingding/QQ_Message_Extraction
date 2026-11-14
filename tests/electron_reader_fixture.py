"""为 Electron 摘要和用量验收准备明确隔离的新数据库。"""
import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from msgx.models import Msg
from msgx.store import Store
from msgx.summary_review import content_hash, save_review
from msgx.time_rules import BEIJING


def seed(root: Path) -> dict:
    db = root / "data" / "msgx.db"
    now = int(time.time())
    text = "# 桌面验收摘要\n## 主题 🧪\n保留 **原有批注** 和正文标点。\n**后续计划**：\n下一段需要能定位。"
    weekly_text = "# 周报验收\n## 概览\n本周内容。"
    store = Store(db)
    try:
        today = datetime.now(BEIJING).date()
        store.save_summary_job("daily", today.isoformat(), (today + timedelta(days=1)).isoformat(),
                               "blocked", now=now, attempts=4, error="隔离请求失败示例")
        report_id = store.add_summary("", now - 86400, now, 1, text, "daily")
        weekly_id = store.add_summary("", now - 86400 * 7, now - 86400, 1, weekly_text, "weekly")
        store.add_summary("", now - 86400 * 35, now - 86400 * 7, 1, "# 月报验收\n本月内容。", "monthly")
        msg_id = store.insert_message(Msg(msg_id="reader-test-no-qq", group_id="90001",
                                          sender_name="验收作者", text="关联消息验收文本"),
                                      bot_uin="100001")
        store.record_api_call(ts=now, purpose="摘要总结", source_ids=[msg_id], model="验收模型",
                              usage={"prompt_tokens": 10, "completion_tokens": 2},
                              cost_cny=0.0012, cost_usd=0.0001, status="ok", provider="OpenAI 官方",
                              service_tier="flex", pricing={"base_url": "https://api.openai.com/v1"})
        store.record_api_call(ts=now, purpose="自动图片理解", source_ids=[], model="验收模型",
                              usage=None, cost_cny=None, status="unknown")
    finally:
        store.close()
    # 旧 Tk 界面去掉标题号和粗体标记，两个可导航标题之间多一个换行。
    offset = len("桌面验收摘要\n主题 🧪\n保留 ")
    save_review(db, report_id, content_hash(text), read_state="skimmed",
                annotations=[{"start": offset, "end": offset + 4, "text": "原有批注", "level": "blue"}])
    logs = db.parent / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "service.log").write_text(
        "2026-11-14 05:00:00 INFO    msgx.llm     API 开始上传：测试请求\n"
        "2026-11-14 05:00:01 INFO    msgx.llm     API 响应头已收到：HTTP 200；正在接收结果\n"
        "2026-11-14 05:00:02 INFO    msgx.llm     API 请求成功 · 摘要总结 · 验收记录\n"
        "2026-11-14 05:00:03 INFO    msgx.llm     API 内容摘要 · 正文中含有“请求失败”的字样\n"
        "2026-11-14 05:00:04 WARNING msgx.llm     LLM 请求最终失败（模拟）\n",
        encoding="utf-8")
    save_review(db, weekly_id, content_hash(weekly_text), read_state="deep")
    return {"id": report_id, "db": str(db)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path, help="新建或空置的临时目录")
    args = ap.parse_args()
    root = args.root.resolve()
    if root.exists() and any(root.iterdir()):
        ap.error("只可写入新建或空置的隔离目录")
    print(json.dumps(seed(root), ensure_ascii=False))
