"""摘要只读接口、停用入口与确认恢复：只用内存库、隔离 HTTP 和模拟响应。"""
import asyncio
import json
import sys
import unittest
import copy
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from aiohttp import web, ClientPayloadError
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.llm import LLM, AmbiguousResponseError
from msgx.pipeline import Pipeline
from msgx.store import Store
from msgx.config import DEFAULTS
from msgx import api_settings
from msgx.onebot import _summary_dates, _summary_days, _summary_now, _summary_retry


class SummaryReadEndpointTests(unittest.IsolatedAsyncioTestCase):
    def test_date_validation(self):
        self.assertEqual(_summary_dates("2026-09-24", "2026-09-24"),
                         ("2026-09-24", "2026-09-24"))  # 同一天是有效查询范围。
        for start, end in (("2026-09-25", "2026-09-24"), ("2026-09-24", "x"),
                           ("20260924", "2026-09-24")):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                _summary_dates(start, end)  # 拒绝倒置范围与非标准日期。

    async def test_manual_summary_is_gone_without_reading_body_or_accessing_pipeline(self):
        for kind in ("digest", "full", "range", "by_date", "periodic", "daily", "typo"):
            request = SimpleNamespace(
                app={}, query={"kind": kind, "hours": "72"}, can_read_body=True,
                json=AsyncMock(side_effect=AssertionError("已停用入口不能读取生成参数")),
            )  # 不提供管道：任何尝试调度或生成都会立即失败。
            response = await _summary_now(request)  # 验证实际注册的旧 HTTP 入口。
            self.assertEqual(response.status, 410)
            self.assertIn("自动计划", json.loads(response.text)["error"])
            request.json.assert_not_awaited()  # force 参数不能恢复已移除的生成入口。

    async def test_days_status_is_read_only(self):
        summarizer = Mock(spec_set=["daily_status"])  # 唯一可用能力是查询，不允许调用生成方法。
        summarizer.daily_status.return_value = {"days": [], "latest_daily": None}
        request = SimpleNamespace(
            app={"pipe": SimpleNamespace(summarizer=summarizer)},
            query={"start_date": "2026-09-23", "end_date": "2026-09-24"},
        )
        self.assertEqual((await _summary_days(request)).status, 200)
        summarizer.daily_status.assert_called_once_with("2026-09-23", "2026-09-24")
        summarizer.daily_status.side_effect = ValueError("最多 31 天")  # 业务范围错误需反馈为请求错误。
        self.assertEqual((await _summary_days(request)).status, 400)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.pipe = Pipeline.__new__(Pipeline)
        self.pipe.store = self.store
        self.pipe._summary_wake = asyncio.Event()
        self.key = ("daily", "2026-10-03", "2026-10-04")
        self.store.save_summary_job(*self.key, "blocked", now=100, attempts=3, error="连接中断，用量未知")

    def test_confirmation_and_latest_failure_required(self):
        for confirmed, revision in ((False, 100), (True, 99)):
            with self.assertRaises(ValueError):
                self.pipe.retry_summary_job(*self.key, confirmed=confirmed, revision=revision)
        self.assertEqual(self.store.summary_job(*self.key)["state"], "blocked")
        self.assertFalse(self.pipe._summary_wake.is_set())

    def test_confirmed_retry_wakes_scheduler_and_duplicate_is_rejected(self):
        result = self.pipe.retry_summary_job(*self.key, confirmed=True, revision=100)
        self.assertEqual(result["state"], "pending")
        self.assertTrue(self.pipe._summary_wake.is_set())
        self.assertEqual(self.store.summary_job(*self.key)["attempts"], 0)
        with self.assertRaises(ValueError):
            self.pipe.retry_summary_job(*self.key, confirmed=True, revision=100)
        self.store.save_summary_job(*self.key, "blocked", now=200, attempts=1)
        with self.assertRaises(ValueError):
            self.pipe.retry_summary_job(*self.key, confirmed=True, revision=100)

    def test_existing_report_is_reused_without_new_request(self):
        start, end = self.pipe._period_bounds(date(2026, 10, 3), date(2026, 10, 4))
        self.store.add_summary("digest", start, end, 1, "已经完成", "daily")
        self.assertEqual(self.pipe.retry_summary_job(*self.key, confirmed=True, revision=100)["state"], "done")
        self.assertEqual(self.store.summary_job(*self.key)["attempts"], 3)


def response(events):
    async def lines():
        for event in events:
            if isinstance(event, Exception):
                raise event
            value = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
            yield ("data: " + value + "\n").encode()
            yield b"\n"
    return SimpleNamespace(status=200, headers={"Content-Type": "text/event-stream", "x-request-id": "req_test"}, content=lines())


class ChatStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_summary_uses_stream_long_timeout_and_records_usage(self):
        cfg = api_settings.apply(copy.deepcopy(DEFAULTS), {**api_settings.DEFAULTS, "service_tier": "default"})
        cfg["llm"].update(protocol="chat_completions", credential_store=False)
        store = Store(":memory:")
        self.addCleanup(store.close)
        llm = LLM(cfg, store)
        llm.configure("synthetic-key")
        sent = []
        events = [{"choices": [{"index": 0, "delta": {"content": "完整摘要"}, "finish_reason": "stop"}]},
                  {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 5}}, "[DONE]"]
        @asynccontextmanager
        async def post(url, **kwargs):
            sent.append(kwargs)
            yield response(events)
        llm.session = AsyncMock(return_value=SimpleNamespace(post=post))
        self.assertEqual(await llm.chat("x", "y", require_complete=True, purpose="摘要总结"), "完整摘要")
        self.assertTrue(json.loads(sent[0]["data"])["stream"])
        self.assertEqual(json.loads(sent[0]["data"])["stream_options"], {"include_usage": True})
        self.assertEqual(sent[0]["timeout"].total, 600)
        self.assertEqual(sent[0]["timeout"].sock_read, 180)
        self.assertEqual(store.api_usage_details()["items"][0]["in_tokens"], 100)
        events[:] = [ClientPayloadError("private-body-secret")]
        with self.assertRaises(AmbiguousResponseError) as caught:
            await llm.chat("x", "y", require_complete=True)
        self.assertIn("ClientPayloadError", str(caught.exception))
        self.assertIn("接收响应正文", str(caught.exception))
        self.assertNotIn("private-body-secret", str(caught.exception))
        self.assertEqual(len(sent), 2, "中断后不得自动再发")
        self.assertEqual(store.api_usage_details()["items"][0]["request_id"], "req_test")

    async def test_text_finish_and_usage_are_combined(self):
        llm = LLM({"llm": {}}, None)
        data = await llm._read_chat_stream(response([
            {"choices": [{"index": 0, "delta": {"content": "完整"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "摘要"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 5}, "service_tier": "default"},
            "[DONE]"]))
        self.assertEqual(data["choices"][0]["message"]["content"], "完整摘要")
        self.assertEqual(data["usage"]["prompt_tokens"], 100)
        self.assertEqual(data["request_id"], "req_test")

    async def test_truncated_stream_is_not_a_complete_report(self):
        llm = LLM({"llm": {}}, None)
        for events in ([{"choices": [{"index": 0, "delta": {"content": "残缺"}}]}],
                       [{"choices": [{"index": 0, "delta": {"content": "残缺"}}]}, "[DONE]"],
                       ["invalid JSON"]):
            with self.assertRaises(AmbiguousResponseError):
                await llm._read_chat_stream(response(events))


class RetryEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_confirmation_origin_and_duplicate_guards(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        pipe = Pipeline.__new__(Pipeline)
        pipe.store, pipe._summary_wake = store, asyncio.Event()
        key = ("daily", "2026-10-03", "2026-10-04")
        store.save_summary_job(*key, "blocked", now=100, attempts=1)
        app = web.Application()
        app["cfg"], app["pipe"] = {"onebot": {"access_token": "local-token"}}, pipe
        app.router.add_post("/summary/retry", _summary_retry)
        async with TestClient(TestServer(app)) as client:
            body = dict(kind=key[0], start_date=key[1], end_date=key[2], updated_at=100, confirmed=True)
            self.assertEqual((await client.post("/summary/retry", json=body)).status, 403)
            headers = {"Authorization": "Bearer local-token"}
            self.assertEqual((await client.post("/summary/retry", json=body, headers={**headers, "Origin": "https://invalid.test"})).status, 403)
            self.assertEqual((await client.post("/summary/retry", json={**body, "confirmed": False}, headers=headers)).status, 409)
            self.assertEqual((await client.post("/summary/retry", json=body, headers=headers)).status, 200)
            self.assertEqual((await client.post("/summary/retry", json=body, headers=headers)).status, 409)
            self.assertEqual(store.summary_job(*key)["state"], "pending")


if __name__ == "__main__":
    unittest.main()
