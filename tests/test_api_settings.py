"""API 安全与 Responses 离线回归：全部使用虚构密钥和模拟响应。"""
import asyncio
import copy
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
import aiohttp
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx import api_settings, credentials, prefs, security
from msgx.config import DEFAULTS, api_key
from msgx.llm import LLM, IncompleteResponse
from msgx.pipeline import Pipeline
from msgx.store import Store
from msgx.summarize import model_limits, Summarizer

KEY = "sk-msgx-test-NOT-A-REAL-KEY-123456789"


class Response:
    def __init__(self, data, status=200, stream=False):
        self.data, self.status = data, status
        self.headers = {"Content-Type": "text/event-stream" if stream else "application/json"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def text(self):
        return json.dumps(self.data)

    @property
    def content(self):
        async def events():
            for event in self.data:
                # 模拟一个 SSE 事件由多个 data 行组成，支持 UTF-8 中文。
                value = json.dumps(event, ensure_ascii=False, indent=2)
                for line in value.splitlines():
                    yield ("data: " + line + "\n").encode()
                yield b"\n"
        return events()


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = api_settings.apply(copy.deepcopy(DEFAULTS), api_settings.DEFAULTS)
        self.cfg["app"]["db_path"] = str(Path(self.tmp.name) / "test.db")
        self.store = Store(self.cfg["app"]["db_path"])
        self.llm = LLM(self.cfg, self.store)
        self.llm.configure(KEY)
        self.credential_patch = patch.object(credentials, "read", return_value=KEY)
        self.credential_patch.start()

    async def asyncTearDown(self):
        self.credential_patch.stop()
        await self.llm.close()
        self.store.close()
        self.tmp.cleanup()

    def response(self, status="completed"):
        return {"status": status, "output": [{"type": "reasoning", "summary": []},
                {"type": "message", "content": [{"type": "output_text", "text": "完整摘要"}]}],
                "usage": {"input_tokens": 1000, "output_tokens": 100,
                          "input_tokens_details": {"cached_tokens": 400}}}

    async def test_responses_stream_maps_body_usage_and_price(self):
        session = Mock()
        session.post.return_value = Response([
            {"type": "response.created", "response": {"status": "in_progress"}},
            {"type": "response.completed", "response": self.response()}], stream=True)
        with patch.object(self.llm, "session", AsyncMock(return_value=session)):
            text = await self.llm.chat("只总结", "正文", require_complete=True, purpose="摘要总结")
        self.assertEqual(text, "完整摘要")
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "https://api.openai.com/v1/responses")
        self.assertFalse(kwargs["allow_redirects"])
        body = json.loads(kwargs["data"])
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])
        self.assertEqual(body["reasoning"], {"effort": "none"})
        self.assertNotIn("temperature", body)
        self.assertNotIn(KEY, json.dumps(body))
        cost = (600 * .1 + 400 * .01 + 100 * .5) / 1000000 * 7
        self.assertAlmostEqual(self.store.cost_totals()["cost_cny"], cost)

    async def test_tier_currency_and_utc_accounting_preserve_history(self):
        from datetime import datetime, timezone
        from contextlib import closing
        import sqlite3
        for tier, multiplier in (("default", 1), ("priority", 2), ("fast", 2), ("flex", .5), ("scale", None)):
            value = self.response()
            value["service_tier"] = tier
            response = Response(value)
            response.headers["x-request-id"] = "req_accounting_test"
            session = Mock()
            session.post.return_value = response
            with patch.object(self.llm, "session", AsyncMock(return_value=session)), \
                    patch("msgx.llm.time.time", return_value=datetime(2026, 9, 25, 16, tzinfo=timezone.utc).timestamp()):
                await self.llm.chat("x", "y", purpose="摘要总结")
            body = json.loads(session.post.call_args.kwargs["data"])
            self.assertEqual(body["service_tier"], "default")
            row = self.store.api_usage_details()["items"][0]
            self.assertEqual(row["service_tier"], tier)
            self.assertEqual(row["request_id"], "req_accounting_test")
            self.assertEqual(json.loads(row["pricing"])["currency"], "USD")
            if multiplier is None:
                self.assertIsNone(row["cost_usd"])
                self.assertIsNone(row["cost_cny"])
            else:
                expected = (600 * .1 + 400 * .01 + 100 * .5) / 1000000 * multiplier
                self.assertAlmostEqual(row["cost_usd"], expected)
                self.assertAlmostEqual(row["cost_cny"], expected * 7)
        utc = self.store.api_usage_details(limit=1)["openai_utc_days"][0]
        self.assertEqual(utc["day"], "2026-09-25")
        self.assertEqual(utc["calls"], 5)
        self.assertEqual(utc["unknown_calls"], 1)
        self.assertAlmostEqual(utc["cost_usd"], (600 * .1 + 400 * .01 + 100 * .5) / 1000000 * 5.5)
        self.assertAlmostEqual(self.store.daily_cost_cny("2026-09-26"), utc["cost_cny"])
        # 旧账的原币和服务等级没有证据，迁移不按新费率回算。
        old_path = Path(self.tmp.name) / "old.db"
        with closing(sqlite3.connect(old_path)) as old:
            old.execute("CREATE TABLE api_calls(id INTEGER PRIMARY KEY,ts REAL NOT NULL,purpose TEXT NOT NULL,source_ids TEXT NOT NULL,source_type TEXT NOT NULL,model TEXT NOT NULL,in_tokens INTEGER,out_tokens INTEGER,cache_hit_tokens INTEGER,cost_cny REAL,status TEXT NOT NULL,provider TEXT NOT NULL)")
            old.execute("INSERT INTO api_calls VALUES(1,0,'摘要总结','[]','汇总报告','gpt-6-luna',1000,100,0,1.23,'完成','OpenAI 官方')")
            old.commit()
        migrated = Store(old_path)
        try:
            row = migrated.api_usage_details()["items"][0]
            self.assertEqual(row["cost_cny"], 1.23)
            self.assertIsNone(row["cost_usd"])
            self.assertIsNone(row["cache_write_tokens"])
            self.assertEqual(row["service_tier"], "")
        finally:
            migrated.close()

    async def test_cache_write_replaces_ordinary_input_and_preserves_tokens(self):
        value = self.response()
        value["usage"]["input_tokens_details"]["cache_write_tokens"] = 300
        session = Mock()
        session.post.return_value = Response(value)
        with patch.object(self.llm, "session", AsyncMock(return_value=session)):
            await self.llm.chat("x", "y", purpose="摘要总结")
        row = self.store.api_usage_details()["items"][0]
        self.assertEqual(row["cache_write_tokens"], 300)
        self.assertEqual(row["cache_hit_tokens"], 400)
        expected = (300 * .1 + 300 * .125 + 400 * .01 + 100 * .5) / 1000000
        self.assertAlmostEqual(row["cost_usd"], expected)
        self.assertEqual(json.loads(row["pricing"])["cache_write_multiplier"], 1.25)
        self.assertNotIn("缓存写入用量未返回", row["status"])
        self.assertEqual(self.store.api_usage_details()["groups"][0]["cache_write_tokens"], 300)
        # 长输入与服务等级乘数在缓存写入价上统一计算。
        long_expected = (100000 * .1 * 2 + 100000 * .125 * 2 + 100000 * .01 * 2 + 1000 * .5 * 1.5) / 1000000 * 7
        for tier, mult in (("default", 1), ("fast", 2), ("priority", 2), ("flex", .5)):
            self.assertAlmostEqual(self.llm.cost_cny(300000, 1000, 100000,
                                   cache_write_tokens=100000, service_tier=tier), long_expected * mult)
        # 缺字段保留未知并说明估算基础，不倒推历史写入量。
        session.post.return_value = Response(self.response())
        with patch.object(self.llm, "session", AsyncMock(return_value=session)):
            await self.llm.chat("x", "y", purpose="摘要总结")
        unknown = self.store.api_usage_details()["items"][0]
        self.assertIsNone(unknown["cache_write_tokens"])
        self.assertIn("缓存写入用量未返回", unknown["status"])
        self.assertGreater(unknown["cost_cny"], 0)
        # Chat Completions 的详情字段也能进入同一账本。
        self.llm.llm["protocol"] = "chat_completions"
        session.post.return_value = Response({"choices": [{"message": {"content": "完整"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100,
                      "prompt_tokens_details": {"cached_tokens": 400, "cache_write_tokens": 300}}})
        with patch.object(self.llm, "session", AsyncMock(return_value=session)):
            await self.llm.chat("x", "y", purpose="摘要总结")
        chat = self.store.api_usage_details()["items"][0]
        self.assertEqual(chat["cache_write_tokens"], 300)
        self.assertAlmostEqual(chat["cost_usd"], expected)
        # 兼容服务不套用 OpenAI 缓存写入溢价。
        ds = LLM(api_settings.apply(self.cfg, api_settings.catalog({}, {})[0]["deepseek"]), self.store)
        self.assertEqual(ds.cost_cny(1000, 100, cache_write_tokens=600), ds.cost_cny(1000, 100))

    async def test_snapshot_reads_saved_summary_only_without_changing_running_config(self):
        path = Path(self.tmp.name) / "summary-prefs.json"
        prefs.save({"summary_target_chars": 2000, "summary_prompt": "新提示词",
                    "filters": {"skip_image_only": True}}, path)
        original = copy.deepcopy(self.cfg)
        with patch.dict(os.environ, {prefs.ENV_PREFS: str(path)}):
            snapshot = self.llm.snapshot()
        self.assertEqual(snapshot.cfg["summary"]["target_chars"], 2000)
        self.assertEqual(snapshot.cfg["summary"]["prompt"], "新提示词")
        self.assertEqual(snapshot.cfg["filters"], original["filters"])
        self.assertEqual(self.cfg, original)
        self.assertIs(snapshot._session_owner, self.llm)

    async def test_accounting_distinguishes_requested_tier_and_sanitizes_request_ids(self):
        value = self.response()
        value["usage"]["input_tokens_details"]["cache_write_tokens"] = 0
        value["request_id"] = KEY
        response = Response(value)
        response.headers["x-request-id"] = KEY
        session = Mock()
        session.post.return_value = response
        with patch.object(self.llm, "session", AsyncMock(return_value=session)):
            await self.llm.chat("x", "y", purpose="摘要总结")
        row = self.store.api_usage_details()["items"][0]
        self.assertEqual(row["request_id"], "")
        self.assertEqual(row["service_tier"], "")
        self.assertIn("服务等级未返回", row["status"])
        self.assertEqual(json.loads(row["pricing"])["service_tier_source"], "request_default")
        self.assertGreater(row["cost_cny"], 0)
        self.assertNotIn(KEY, json.dumps(row))
        # 兼容接口的响应正文也须经过同一诊断标识检查。
        self.llm.llm["protocol"] = "chat_completions"
        response.data = {"choices": [{"message": {"content": "完整"}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
                         "request_id": "private chat " + KEY}
        with patch.object(self.llm, "session", AsyncMock(return_value=session)):
            await self.llm.chat("x", "y", purpose="摘要总结")
        row = self.store.api_usage_details()["items"][0]
        self.assertEqual(row["request_id"], "")
        self.assertNotIn(KEY, json.dumps(row))

    async def test_images_and_reasoning_use_responses_format(self):
        self.cfg["api"]["reasoning"] = "high"
        wire = self.llm._responses_payload({"model": "gpt-6-luna", "max_tokens": 2000,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": "配文"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "low"}}]}]})
        self.assertEqual(wire["input"][0]["content"][1]["type"], "input_image")
        self.assertEqual(wire["max_output_tokens"], 2000)
        self.assertEqual(wire["reasoning"]["effort"], "high")
        self.assertNotIn("temperature", wire)

    async def test_redirect_and_error_echo_do_not_leak_or_follow(self):
        for code in (302, 401, 400):
            session = Mock()
            session.post.return_value = Response({"error": {"message": KEY}}, code)
            with patch.object(self.llm, "session", AsyncMock(return_value=session)):
                with self.assertRaises(RuntimeError) as raised:
                    await self.llm.chat("x", "y")
            self.assertNotIn(KEY, str(raised.exception))
            self.assertEqual(session.post.call_count, 1)
            self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

    async def test_chat_output_limit_matches_provider(self):
        for profile in (api_settings.DEFAULTS, api_settings.catalog({}, {})[0]["deepseek"]):
            cfg = api_settings.apply(self.cfg, {**profile, "protocol": "chat_completions"})
            llm = LLM(cfg, self.store)
            llm.configure(KEY)
            session = Mock()
            session.post.return_value = Response({"choices": [{"message": {"content": "完整"},
                                                    "finish_reason": "stop"}]})
            with patch.object(llm, "session", AsyncMock(return_value=session)):
                await llm.chat("x", "y", max_tokens=2000, think=False)
            body = json.loads(session.post.call_args.kwargs["data"])
            expected = "max_completion_tokens" if profile is api_settings.DEFAULTS else "max_tokens"
            self.assertEqual(body[expected], 2000)
            self.assertNotIn("max_tokens" if expected == "max_completion_tokens" else "max_completion_tokens", body)

    async def test_http_error_retains_only_safe_diagnostics(self):
        response = Response({"error": {"type": "invalid_request_error", "code": "unsupported_parameter",
                             "param": "max_tokens", "message": KEY + " private chat data:image/png;base64,AAAA"}}, 400)
        response.headers["x-request-id"] = "req_test_123"
        session = Mock()
        session.post.return_value = response
        with patch.object(self.llm, "session", AsyncMock(return_value=session)):
            with self.assertRaises(RuntimeError) as raised:
                await self.llm.chat("x", "y")
        error = str(raised.exception)
        for value in ("HTTP 400", "unsupported_parameter", "max_tokens", "req_test_123"):
            self.assertIn(value, error)
        for value in (KEY, "private chat", "base64"):
            self.assertNotIn(value, error)
        self.assertEqual(session.post.call_count, 1)
        self.assertIn("unsupported_parameter", self.store.api_usage_details()["items"][0]["status"])
        # 即使服务端把密钥放进结构化字段，也不能进入日志或账本。
        response.data["error"].update(type=KEY, code="Bearer " + KEY, param="data:image/png;base64,AAAA")
        response.headers["x-request-id"] = KEY
        with patch.object(self.llm, "session", AsyncMock(return_value=session)):
            with self.assertRaises(RuntimeError) as raised:
                await self.llm.chat("x", "y")
        self.assertNotIn(KEY, str(raised.exception))
        self.assertNotIn("base64", str(raised.exception))

    async def test_rejected_manual_image_preserves_ocr_without_auto_retry(self):
        from PIL import Image
        from msgx.media import MediaWorker
        from msgx.models import Msg
        image = Path(self.tmp.name) / "image.png"
        Image.new("RGB", (16, 16), "white").save(image)
        self.cfg["app"]["media_dir"] = str(Path(self.tmp.name) / "media")
        worker = MediaWorker(self.cfg, self.store, self.llm)
        session = Mock()
        session.post.return_value = Response({"error": {"code": "unsupported_parameter", "param": "max_tokens"}}, 400)
        try:
            row_id = self.store.insert_message(Msg(msg_id="first", group_id="g", text="请看报错[图片]",
                                                   image_urls=[image.as_uri()], msg_type="mixed"))
            image_id = self.store.conn.execute("SELECT id FROM message_images WHERE message_row=?", (row_id,)).fetchone()[0]
            with patch.object(self.llm, "snapshot", return_value=self.llm), \
                    patch.object(self.llm, "session", AsyncMock(return_value=session)), \
                    patch.object(worker, "_download", AsyncMock(return_value=image)), \
                    patch.object(worker, "_ocr_sync", return_value="本地文字"):
                await worker.process_pending()
                self.assertEqual(session.post.call_count, 0)
                result = await worker.manual_vision(image_id)
                self.assertEqual(result["state"], "rejected")
                await worker.process_pending()
            row = self.store.get_message(row_id)
            self.assertEqual(row["vision_state"], "rejected")
            self.assertEqual(row["media_state"], "done")
            self.assertEqual(self.store.get_image(image_id)["ocr_text"], "本地文字")
            self.assertEqual(self.store.get_image(image_id)["vision_state"], "rejected")
            self.assertEqual(session.post.call_count, 1)
            self.assertEqual(self.store.failed_vision(), [])
            duplicate = self.store.insert_message(Msg(msg_id="duplicate", group_id="g", text="相同报错[图片]",
                                                      image_urls=[image.as_uri()], msg_type="mixed"))
            with patch.object(worker, "_download", AsyncMock(return_value=image)), \
                    patch.object(worker, "_ocr_sync", return_value="本地文字"):
                await worker.process_pending()
            self.assertEqual(self.store.get_message(duplicate)["vision_state"], "skipped")
            duplicate_image = self.store.conn.execute(
                "SELECT id FROM message_images WHERE message_row=?", (duplicate,)).fetchone()[0]
            self.assertEqual(self.store.get_image(duplicate_image)["ocr_text"], "本地文字")
            self.assertEqual(session.post.call_count, 1)
        finally:
            await worker.close()

    async def test_openai_image_succeeds_with_modern_chat_limit(self):
        from PIL import Image
        self.llm.llm["protocol"] = "chat_completions"
        image = Path(self.tmp.name) / "image.png"
        Image.new("RGB", (16, 16), "white").save(image)
        def accept_request(url, **kwargs):
            body = json.loads(kwargs["data"])
            self.assertEqual(body["max_completion_tokens"], 2000)
            self.assertNotIn("max_tokens", body)
            self.assertTrue(body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
            return Response({"choices": [{"message": {"content": "图片文字"}, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 100, "completion_tokens": 10}})
        session = Mock()
        session.post.side_effect = accept_request
        with patch.object(self.llm, "session", AsyncMock(return_value=session)):
            self.assertEqual(await self.llm.describe_image(image, "提取文字"), ("图片文字", True))
        self.assertEqual(session.post.call_count, 1)

    async def test_non_json_error_is_rejected_without_echo_or_retry(self):
        response = Response({}, 400)
        response.text = AsyncMock(return_value="<html>" + KEY + "private chat</html>")
        session = Mock()
        session.post.return_value = response
        with patch.object(self.llm, "session", AsyncMock(return_value=session)):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400") as raised:
                await self.llm.chat("x", "y")
        self.assertNotIn("private chat", str(raised.exception))
        self.assertNotIn(KEY, str(raised.exception))
        self.assertEqual(session.post.call_count, 1)

    async def test_incomplete_or_missing_terminal_is_not_saved_as_complete_or_retried(self):
        for events in ([{"type": "response.incomplete", "response": self.response("incomplete")}],
                       [{"type": "response.output_text.delta", "delta": "半篇"}]):
            session = Mock()
            session.post.return_value = Response(events, stream=True)
            with patch.object(self.llm, "session", AsyncMock(return_value=session)):
                with self.assertRaises(RuntimeError):
                    await self.llm.chat("x", "y", require_complete=True)
            self.assertEqual(session.post.call_count, 1)

    async def test_deleted_key_prevents_next_http_request(self):
        session = Mock()
        with patch.object(credentials, "read", return_value=""), \
                patch.object(self.llm, "session", AsyncMock(return_value=session)):
            with self.assertRaisesRegex(RuntimeError, "未配置安全密钥"):
                await self.llm.chat("x", "y")
        session.post.assert_not_called()

    async def test_stream_disconnect_does_not_repeat_billable_request(self):
        class Broken(Response):
            @property
            def content(self):
                async def chunks():
                    yield b'data: {"type":"response.created"}\n'
                    yield b'\n'
                    raise aiohttp.ClientPayloadError(KEY)
                return chunks()
        session = Mock()
        session.post.return_value = Broken([], stream=True)
        with patch.object(self.llm, "session", AsyncMock(return_value=session)):
            with self.assertRaisesRegex(RuntimeError, "未自动重发") as error:
                await self.llm.chat("x", "y")
        self.assertEqual(session.post.call_count, 1)
        self.assertNotIn(KEY, str(error.exception))

    async def test_no_cross_provider_billing_or_old_price_backfill(self):
        pipe = Pipeline(self.cfg)
        try:
            with patch("msgx.pipeline.billing.fetch_balance", AsyncMock()) as balance, \
                    patch("msgx.pipeline.billing.fetch_platform_cost", AsyncMock()) as platform:
                result = await pipe.billing_snapshot()
            balance.assert_not_called()
            platform.assert_not_called()
            self.assertEqual(result["source"], "estimate")
            self.assertFalse(result["balance"]["ok"])
            self.assertEqual(self.llm.backfill_legacy_cost(), 0)
        finally:
            await pipe.stop()

    async def test_settings_persist_without_key_and_bind_full_endpoint(self):
        path = Path(self.tmp.name) / "prefs.json"
        prefs.save({"api": api_settings.DEFAULTS}, path)
        loaded = prefs.apply_to_config(copy.deepcopy(DEFAULTS), prefs.load(path))
        self.assertEqual(loaded["llm"]["model"], "gpt-6-luna")
        self.assertEqual(api_key(loaded), KEY)
        self.assertNotIn(KEY, path.read_text())
        with self.assertRaises(ValueError):
            prefs.save({"api": {"api_key": KEY}}, path)
        for url in ("http://api.openai.com/v1", "https://user:pass@api.openai.com/v1",
                    "https://api.openai.com/v1?key=secret", "https://api.openai.com/sk-secret"):
            with self.assertRaises(ValueError):
                api_settings.endpoint(url)
        self.assertEqual(credentials._target("https://API.OPENAI.COM:443/v1/"), credentials._target("https://api.openai.com/v1"))
        self.assertNotEqual(credentials._target("https://api.openai.com/v1"), credentials._target("https://example.invalid/v1"))
        self.assertNotEqual(credentials._target("https://api.openai.com/v1"), credentials._target("https://api.openai.com/other"))

    async def test_long_context_pricing_and_editable_limits(self):
        self.assertEqual(model_limits(self.cfg), (1050000, 128000))
        self.assertAlmostEqual(self.llm.cost_cny(300000, 1000, 100000),
                               (200000*.1*2 + 100000*.01*2 + 1000*.5*1.5)/1000000*7)
        custom = api_settings.validate({"model": "future-model", "context_tokens": 50000,
                                       "max_output_tokens": 5000, "long_context_threshold": 0})
        self.assertEqual(model_limits(api_settings.apply(self.cfg, custom)), (50000, 5000))

    async def test_log_filter_redacts_key_header_and_exception(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(security.SecretFilter())
        logger = logging.getLogger("msgx.test.security")
        logger.addHandler(handler)
        try:
            try:
                raise RuntimeError(KEY)
            except RuntimeError:
                logger.exception("Authorization: Bearer %s", KEY)
            self.assertNotIn(KEY, stream.getvalue())
        finally:
            logger.removeHandler(handler)

    async def test_profiles_switch_requests_without_changing_inflight_usage(self):
        path = Path(self.tmp.name) / "profiles.json"
        profiles, _ = api_settings.catalog({}, {})
        def select(active):
            prefs.save({"api_profiles": profiles, "active_api_profile": active}, path)
        select("openai")
        ready, release = asyncio.Event(), asyncio.Event()
        class Delayed(Response):
            async def __aenter__(self):
                ready.set()
                await release.wait()
                return self
        session = Mock()
        session.post.side_effect = [Delayed(self.response()), Response({
            "choices": [{"message": {"content": "DeepSeek 摘要"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})]
        def key_for(url, *, profile_id=""):
            return KEY + "-" + profile_id
        with patch.dict(os.environ, {prefs.ENV_PREFS: str(path)}), \
                patch.object(credentials, "read", side_effect=key_for), \
                patch.object(self.llm, "session", AsyncMock(return_value=session)):
            old = self.llm.snapshot()
            task = asyncio.create_task(old.chat("x", "y", purpose="摘要总结", require_complete=True))
            await asyncio.wait_for(ready.wait(), 2)
            select("deepseek")
            new = self.llm.snapshot()
            self.assertEqual(await new.chat("x", "y", purpose="摘要总结", require_complete=True), "DeepSeek 摘要")
            release.set()
            self.assertEqual(await task, "完整摘要")
            select("openai")
            self.assertEqual(self.llm.snapshot().cfg["api"], profiles["openai"])
        first, second = session.post.call_args_list
        self.assertEqual(first.args[0], "https://api.openai.com/v1/responses")
        self.assertEqual(second.args[0], "https://api.deepseek.com/chat/completions")
        self.assertNotEqual(first.kwargs["headers"]["Authorization"], second.kwargs["headers"]["Authorization"])
        self.assertEqual(json.loads(second.kwargs["data"])["model"], "deepseek-flash")
        rows = self.store.api_usage_details(active_only=True)
        self.assertEqual({r["provider"] for r in rows["groups"]}, {"OpenAI 官方", "DeepSeek"})
        for row in rows["items"]:
            client = old if row["provider"] == "OpenAI 官方" else new
            self.assertAlmostEqual(row["cost_cny"], client.cost_cny(row["in_tokens"], row["out_tokens"],
                                   row["cache_hit_tokens"], ts=row["ts"]))
        self.assertNotIn(KEY, path.read_text())

    async def test_summary_pins_profile_and_keeps_single_job_lock(self):
        path = Path(self.tmp.name) / "profiles.json"
        profiles, _ = api_settings.catalog({}, {})
        prefs.save({"api_profiles": profiles, "active_api_profile": "openai"}, path)
        summary = Summarizer(self.cfg, self.store, self.llm)
        ready, release = asyncio.Event(), asyncio.Event()
        used = []
        async def fake_run(worker, *args):
            used.append(worker.cfg["api"]["model"])
            ready.set()
            await release.wait()
            used.append(worker.cfg["api"]["model"])
            return {"content": "完成"}
        with patch.dict(os.environ, {prefs.ENV_PREFS: str(path)}), patch.object(Summarizer, "_run_digest", fake_run):
            task = asyncio.create_task(summary.run_digest())
            await asyncio.wait_for(ready.wait(), 2)
            prefs.save({"api_profiles": profiles, "active_api_profile": "deepseek"}, path)
            with self.assertRaisesRegex(RuntimeError, "已有摘要"):
                await summary.run_digest()
            release.set()
            await task
            await summary.run_digest()
        self.assertEqual(used, ["gpt-6-luna", "gpt-6-luna", "deepseek-flash", "deepseek-flash"])
        self.assertEqual(summary.progress["state"], "done")

    async def test_catalog_migrates_single_settings_and_isolates_same_address_keys(self):
        profiles, active = api_settings.catalog({}, {"api": {**api_settings.DEFAULTS, "model": "existing-model"}})
        self.assertEqual(active, "openai")
        self.assertEqual(profiles["openai"]["model"], "existing-model")
        self.assertEqual(profiles["deepseek"]["currency"], "CNY")
        self.assertEqual(profiles["deepseek"]["peak_multiplier"], 2)
        url = profiles["openai"]["base_url"]
        self.assertEqual(credentials._target(url), credentials._target(url, "openai"))
        self.assertNotEqual(credentials._target(url, "openai"), credentials._target(url, "custom_1"))
        with self.assertRaises(ValueError):
            api_settings.catalog({}, {"api_profiles": {"deepseek": {"api_key": KEY}}})
        with self.assertRaises(ValueError):
            api_settings.catalog({}, {"active_api_profile": "missing"})
        ds = LLM(api_settings.apply(self.cfg, profiles["deepseek"]), self.store)
        self.assertAlmostEqual(ds.cost_cny(1000, 100, peak=True), 2 * ds.cost_cny(1000, 100, peak=False))


@unittest.skipUnless(os.environ.get("MSGX_TEST_CREDENTIALS") == "1", "仅显式启用时测试 Windows 虚构凭据")
class NativeCredentialTests(unittest.TestCase):
    def test_native_roundtrip_delete_and_endpoint_isolation(self):
        url = "https://msgx-credential-test.invalid/" + uuid.uuid4().hex
        try:
            self.assertEqual(credentials.read(url), "")
            credentials.save(url, KEY)
            self.assertEqual(credentials.read(url), KEY)
            self.assertEqual(credentials.read(url + "/different"), "")
            credentials.save(url, KEY + "-second", profile_id="custom_1")
            self.assertEqual(credentials.read(url, profile_id="custom_1"), KEY + "-second")
            credentials.delete(url)
            self.assertEqual(credentials.read(url), "")
            self.assertEqual(credentials.read(url, profile_id="custom_1"), KEY + "-second")
        finally:
            credentials.delete(url)
            credentials.delete(url, profile_id="custom_1")


if __name__ == "__main__":
    unittest.main()
