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
