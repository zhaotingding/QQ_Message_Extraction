"""自然日日报与多日复用：只使用假模型，不产生 API 费用。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from msgx.summarize import BEIJING, Summarizer
from msgx.store import Store


class FakeLLM:
    available = True

    def __init__(self):
        self.calls = []

    async def chat(self, system, body, **kwargs):
        self.calls.append((system, body, kwargs))
        return f"# 第 {len(self.calls)} 份摘要\n\n**主题**\n内容已归纳。"


def make(tmp):
    llm = FakeLLM()
    store = Store(Path(tmp) / "msgx.db")
    cfg = {"app": {"report_dir": str(Path(tmp) / "reports")},
           "summary": {"target_chars": 3000, "prompt": ""},
           "llm": {"model": "deepseek-flash", "base_url": "https://example.invalid"}}
    return Summarizer(cfg, store, llm), llm


def add(store, msg_id, ts, text):
    store.conn.execute("INSERT INTO messages(group_id,msg_id,ts,text) VALUES('g',?,?,?)",
                       (msg_id, ts, text))
    store.conn.commit()


async def check_days():
    with tempfile.TemporaryDirectory() as tmp:
        sm, llm = make(tmp)
        yesterday = datetime.now(BEIJING).date() - timedelta(days=1)
        start, end = sm._day_bounds(yesterday)
        add(sm.store, "start", start, "昨天零点的消息")
        add(sm.store, "end", end, "今天零点的消息")
        result = await sm.run_dates(yesterday.isoformat(), yesterday.isoformat())
        assert result and len(llm.calls) == 1
        assert "昨天零点的消息" in llm.calls[0][1]
        assert "今天零点的消息" not in llm.calls[0][1]
        saved = sm.store.summary_for_window("daily", start, end)
        assert saved and saved["msg_count"] == 1
        assert sm.daily_status(yesterday.isoformat(), yesterday.isoformat())["days"][0]["status"] == "done"
        again = await sm.run_dates(yesterday.isoformat(), yesterday.isoformat())
        assert again["id"] == result["id"] and len(llm.calls) == 1
        replaced = await sm.run_dates(yesterday.isoformat(), yesterday.isoformat(), force=True)
        assert replaced["id"] != result["id"] and len(llm.calls) == 2
        sm.store.close()


async def check_multi_reuses_daily():
    with tempfile.TemporaryDirectory() as tmp:
        sm, llm = make(tmp)
        yesterday = datetime.now(BEIJING).date() - timedelta(days=1)
        first = yesterday - timedelta(days=1)
        a, _ = sm._day_bounds(first)
        b, _ = sm._day_bounds(yesterday)
        add(sm.store, "a", a + 1, "第一天的原文")
        add(sm.store, "b", b + 1, "第二天的原文")
        daily = await sm.run_dates(first.isoformat(), first.isoformat())
        assert daily and len(llm.calls) == 1
        report = await sm.run_dates(first.isoformat(), yesterday.isoformat())
        assert report and len(llm.calls) == 3
        assert "第二天的原文" in llm.calls[1][1]
        assert daily["content"] in llm.calls[2][1]
        assert "第一天的原文" not in llm.calls[2][1]
        assert "第二天的原文" not in llm.calls[2][1]
        assert llm.calls[2][2]["max_tokens"] > llm.calls[1][2]["max_tokens"]
        cached = await sm.run_dates(first.isoformat(), yesterday.isoformat())
        assert cached["id"] == report["id"] and len(llm.calls) == 3
        await sm.run_dates(first.isoformat(), first.isoformat(), force=True)
        updated = await sm.run_dates(first.isoformat(), yesterday.isoformat())
        assert updated["id"] != report["id"] and len(llm.calls) == 5
        sm.store.close()


async def check_today_partial():
    with tempfile.TemporaryDirectory() as tmp:
        sm, llm = make(tmp)
        today = datetime.now(BEIJING).date()
        start, end = sm._day_bounds(today)
        add(sm.store, "today", start + 1, "今天的新消息")
        result = await sm.run_dates(today.isoformat(), today.isoformat())
        assert result and result["id"] > 0 and len(llm.calls) == 1
        assert sm.store.summary_for_window("daily", start, end) is None
        assert sm.daily_status(today.isoformat(), today.isoformat())["days"][0]["status"] == "partial"
        sm.store.close()


async def check_empty_picture_is_not_sent():
    with tempfile.TemporaryDirectory() as tmp:
        sm, llm = make(tmp)
        day = datetime.now(BEIJING).date() - timedelta(days=1)
        start, end = sm._day_bounds(day)
        add(sm.store, "picture", start + 1, "[图片]")
        assert sm.daily_status(day.isoformat(), day.isoformat())["days"][0]["status"] == "empty"
        result = await sm.run_dates(day.isoformat(), day.isoformat())
        assert result is None and not llm.calls
        sm.store.close()


async def main():
    await check_days()
    await check_multi_reuses_daily()
    await check_today_partial()
    await check_empty_picture_is_not_sent()
    print("自然日边界、日报复用、多日仅消费日报、缺日补做、显式重做、今日未完成、纯图片不请求模型：全部通过")


if __name__ == "__main__":
    asyncio.run(main())
