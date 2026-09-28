"""自然日日报与多日复用：只使用假模型，不产生 API 费用。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from msgx.summarize import BEIJING, Summarizer, output_token_budget, summary_chars
from msgx.store import Store
from msgx.summary_review import content_hash, save_review


class FakeLLM:
    available = True

    def __init__(self):
        self.calls = []
        self.content = None

    async def chat(self, system, body, **kwargs):
        self.calls.append((system, body, kwargs))
        return self.content or f"# 第 {len(self.calls)} 份摘要\n\n**主题**\n内容已归纳。"


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
        assert (result["kind"], result["window_start"], result["window_end"]) == ("daily", start, end)
        assert "昨天零点的消息" in llm.calls[0][1]
        assert "今天零点的消息" not in llm.calls[0][1]
        saved = sm.store.summary_for_window("daily", start, end)
        assert saved and saved["msg_count"] == 1
        page = Path(result["path"]).read_text(encoding="utf-8")
        assert f"{yesterday} 00:00 — {yesterday + timedelta(days=1)} 00:00" in page
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


async def check_review_material_and_cache():
    with tempfile.TemporaryDirectory() as tmp:
        sm, llm = make(tmp)
        yesterday = datetime.now(BEIJING).date() - timedelta(days=1)
        first = yesterday - timedelta(days=1)
        for index, day in enumerate((first, yesterday)):
            start, end = sm._day_bounds(day)
            sm.store.add_summary("digest", start, end, 1, f"# 日报{index}\n蓝色普通内容。红色重要内容。", "daily")
        row = sm.store.summary_for_window("daily", *sm._day_bounds(first))
        digest = content_hash(row["content"])
        path = Path(tmp) / "msgx.db"
        blue = {"start": 5, "end": 11, "text": "蓝色普通内容", "level": "blue"}
        red = {"start": 12, "end": 18, "text": "红色重要内容", "level": "red"}
        save_review(path, row["id"], digest, annotations=[blue, red], notes="我认为需要继续核实。")
        assert not llm.calls  # 保存是本地操作。
        report = await sm.run_dates(first.isoformat(), yesterday.isoformat())
        assert len(llm.calls) == 1
        system, body, _ = llm.calls[0]
        assert "优先关注红色，再关注蓝色" in system
        assert "不能改写成群聊共识或客观事实" in system
        assert body.index("【用户特别重点（红色）】") < body.index("【用户一般重点（蓝色）】")
        assert "【用户心得：个人观点，不是群聊事实】\n我认为需要继续核实。" in body
        save_review(path, row["id"], digest, read_state="deep", annotations=[
            {**red, "start": 20, "end": 26}, blue, blue,
            {"start": 0, "end": 2, "text": "日报", "level": "underline"}])
        cached = await sm.run_dates(first.isoformat(), yesterday.isoformat())
        assert cached["id"] == report["id"] and len(llm.calls) == 1
        save_review(path, row["id"], digest, notes="更正：这是我的个人判断。")
        updated = await sm.run_dates(first.isoformat(), yesterday.isoformat())
        assert updated["id"] != report["id"] and len(llm.calls) == 2
        assert "更正：这是我的个人判断。" in llm.calls[-1][1]
        save_review(path, row["id"], digest, annotations=[blue])
        await sm.run_dates(first.isoformat(), yesterday.isoformat())
        assert len(llm.calls) == 3 and "【用户特别重点（红色）】" not in llm.calls[-1][1]
        sm.store.close()


async def check_multi_reuses_reviewed_today_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        sm, llm = make(tmp)
        today = datetime.now(BEIJING).date()
        yesterday = today - timedelta(days=1)
        before, midnight = sm._day_bounds(yesterday)
        sm.store.add_summary("digest", before, midnight, 1, "# 昨日日报\n昨天内容。", "daily")
        add(sm.store, "morning", midnight + 60, "今日上午原始消息")
        cutoff = midnight + 9 * 3600
        with patch("msgx.summarize.time.time", return_value=cutoff):
            daily = await sm.run_dates(today.isoformat(), today.isoformat())
        saved_id = daily["id"]
        save_review(Path(tmp) / "msgx.db", saved_id, content_hash(daily["content"]),
                    annotations=[{"start": 0, "end": 5, "text": "内容已归纳", "level": "red"}],
                    notes="这是我对上午日报的心得。")
        add(sm.store, "later", cutoff + 60, "快照之后的新消息不应自动重新总结")
        progress = []
        sm._progress = progress.append
        with patch("msgx.summarize.time.time", return_value=midnight + 11 * 3600):
            report = await sm.run_dates(yesterday.isoformat(), today.isoformat())
        assert len(llm.calls) == 2  # 单日报一次，多日汇总一次，不重做今日日报。
        assert sm.store.latest_summary_kind("daily_partial", midnight)["id"] == saved_id
        body = llm.calls[-1][1]
        assert "【用户特别重点（红色）】\n内容已归纳" in body
        assert "这是我对上午日报的心得。" in body
        assert "快照之后的新消息不应自动重新总结" not in body
        assert f"{today} 00:00:00 — {today} 09:00:00" in body
        saved = sm.store.summary_for_window("multi_partial", before, cutoff)
        assert saved and saved["id"] == report["id"]
        assert any("请先更新今日日报" in item for item in progress)
        page = Path(report["path"]).read_text(encoding="utf-8")
        assert f"{yesterday} 00:00 — {today} 09:00" in page and "截至当前" not in page
        with patch("msgx.summarize.time.time", return_value=midnight + 12 * 3600):
            cached = await sm.run_dates(yesterday.isoformat(), today.isoformat())
        assert cached["id"] == report["id"] and len(llm.calls) == 2
        with patch("msgx.summarize.time.time", return_value=midnight + 13 * 3600):
            updated = await sm.run_dates(today.isoformat(), today.isoformat())
        assert updated["id"] != saved_id and len(llm.calls) == 3
        assert "快照之后的新消息不应自动重新总结" in llm.calls[-1][1]
        sm.store.close()


async def check_multi_today_missing_snapshot():
    for has_messages in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            sm, llm = make(tmp)
            today = datetime.now(BEIJING).date()
            yesterday = today - timedelta(days=1)
            before, midnight = sm._day_bounds(yesterday)
            sm.store.add_summary("digest", before, midnight, 1, "# 昨日日报\n昨天内容。", "daily")
            if has_messages:
                add(sm.store, "today", midnight + 1, "此前尚未总结的今日消息")
            now = midnight + 12 * 3600
            progress = []
            sm._progress = progress.append
            with patch("msgx.summarize.time.time", return_value=now):
                report = await sm.run_dates(yesterday.isoformat(), today.isoformat())
            cutoff = now if has_messages else midnight
            saved = sm.store.summary_for_window("multi_partial", before, cutoff)
            assert saved and saved["id"] == report["id"]
            assert len(llm.calls) == (2 if has_messages else 1)
            page = Path(report["path"]).read_text(encoding="utf-8")
            if has_messages:
                assert sm.store.latest_summary_kind("daily_partial", midnight) is not None
                assert "此前尚未总结的今日消息" in llm.calls[0][1]
            else:
                assert sm.store.latest_summary_kind("daily_partial", midnight) is None
                assert f"{yesterday} 00:00 — {today} 00:00" in page
                assert any(f"日报材料实际截至 {today} 00:00:00，今日暂无可总结材料" in item for item in progress)
                assert report["end_date"] == yesterday.isoformat()
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


async def check_single_day_respects_2000_setting():
    with tempfile.TemporaryDirectory() as tmp:
        sm, llm = make(tmp)
        sm.scfg["target_chars"] = 2000
        day = datetime.now(BEIJING).date() - timedelta(days=1)
        start, _ = sm._day_bounds(day)
        add(sm.store, "text", start + 1, "需要总结的消息")
        result = await sm.run_dates(day.isoformat(), day.isoformat())
        system, _, options = llm.calls[0]
        assert "全文篇幅上限为 2000 字" in system
        assert options["max_tokens"] == output_token_budget(2000)
        assert result["chars"] == summary_chars(result["content"])
        assert "上限 2000 字" in Path(result["path"]).read_text(encoding="utf-8")
        assert summary_chars("# 标题\n\n**主题**\n内容。") == len("标题主题内容。")
        sm.store.close()


async def check_overlength_keeps_complete_output():
    with tempfile.TemporaryDirectory() as tmp:
        sm, llm = make(tmp)
        sm.scfg["target_chars"] = 2000
        llm.content = "# 总标题\n\n**主题**\n" + "完整内容。" * 500
        day = datetime.now(BEIJING).date() - timedelta(days=1)
        start, _ = sm._day_bounds(day)
        add(sm.store, "text", start + 1, "需要总结的消息")
        result = await sm.run_dates(day.isoformat(), day.isoformat())
        assert result["chars"] > 2000
        assert result["content"] == llm.content and len(llm.calls) == 1
        assert f"全文 {result['chars']} 字 / 上限 2000 字" in Path(result["path"]).read_text(encoding="utf-8")
        sm.store.close()


async def main():
    await check_days()
    await check_multi_reuses_daily()
    await check_review_material_and_cache()
    await check_today_partial()
    await check_multi_reuses_reviewed_today_snapshot()
    await check_multi_today_missing_snapshot()
    await check_empty_picture_is_not_sent()
    await check_single_day_respects_2000_setting()
    await check_overlength_keeps_complete_output()
    print("自然日边界、日报复用、重点心得与缓存、今日快照复用及实际截止、缺日补做、显式重做、今日未完成、单日字数设置、超长全文保留、纯图片不请求模型：全部通过")


if __name__ == "__main__":
    asyncio.run(main())
