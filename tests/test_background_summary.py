"""模拟 OpenAI 后台任务及断线，验证恢复、成稿和只记账一次；不调用付费 API。"""
import asyncio
import copy
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import ClientPayloadError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx import api_settings
from msgx.config import DEFAULTS
from msgx.llm import LLM, BackgroundResponsePending, AmbiguousResponseError, IncompleteResponse, APIRequestRejected
from msgx.pipeline import Pipeline
from msgx.store import Store
from msgx.summarize import Summarizer


def response(status, text="完整摘要", identity="resp_test"):
    data = dict(id=identity, status=status, service_tier="flex", output=[], usage=None)
    if status == "completed":
        data.update(output=[dict(type="message", content=[dict(type="output_text", text=text)])],
                    usage=dict(input_tokens=100, output_tokens=10, input_tokens_details=dict(cached_tokens=20)))
    return SimpleNamespace(status=200, headers={"Content-Type": "application/json", "x-request-id": "req_original"},
                           text=AsyncMock(return_value=json.dumps(data)))


class BackgroundSummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / "messages.db")
        self.addCleanup(lambda: self.store.close())
        self.cfg = api_settings.apply(copy.deepcopy(DEFAULTS), api_settings.DEFAULTS)
        self.cfg["llm"]["credential_store"] = False
        self.cfg["app"]["report_dir"] = str(self.folder / "reports")
        self.cfg["summary"]["target_chars"] = 3000
        self.llm = LLM(self.cfg, self.store)
        self.llm.configure("synthetic-key")
        self.llm.snapshot = lambda: self.llm
        self.posts, self.gets = [], []
        self.fail_get = True

        @asynccontextmanager
        async def post(url, **options):
            self.posts.append((url, json.loads(options["data"])))
            yield response("queued")

        @asynccontextmanager
        async def get(url, **options):
            self.gets.append(url)
            if self.fail_get:
                raise ClientPayloadError("模拟正文断线")
            yield response("completed")

        self.llm.session = AsyncMock(return_value=SimpleNamespace(post=post, get=get))
        self.key = ("daily", "2026-10-08", "2026-10-09")
        start, end = Pipeline._period_bounds(date(2026, 10, 8), date(2026, 10, 9))
        self.store.conn.execute("INSERT INTO messages(group_id,msg_id,ts,text) VALUES('g','1',?,'有效消息')", (start + 100,))
        self.store.conn.commit()

    async def run_daily(self):
        return await Summarizer(self.llm.cfg, self.store, self.llm).run_period(
            "daily", date(2026, 10, 8), date(2026, 10, 9))

    async def test_disconnect_restart_preserves_material_profile_and_single_bill(self):
        with patch("msgx.llm.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(BackgroundResponsePending):
                await self.run_daily()
        payload = self.posts[0][1]
        self.assertTrue(self.posts[0][0].endswith("/responses"))
        self.assertTrue(payload["background"])
        self.assertTrue(payload["store"])
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["service_tier"], "flex")
        self.assertEqual(self.store.summary_request(self.key)["response_id"], "resp_test")
        self.assertNotIn("synthetic-key", json.dumps(self.store.summary_request(self.key)))
        self.assertEqual(self.store.api_usage_details()["items"], [])
        self.store.close()
        self.store = Store(self.folder / "messages.db")
        self.llm.store = self.store
        self.store.conn.execute("DELETE FROM messages")
        self.store.conn.commit()
        self.llm.cfg["api"]["input_price"] = 999
        self.llm.cfg["summary"]["target_chars"] = 9000
        self.fail_get = False
        # 模拟成稿保存失败：API 结果及账单已经持久保存，下一次只完成本地写入。
        with patch("msgx.config.api_key", return_value="synthetic-key"), patch.object(LLM, "budget_ok", return_value=False):
            with patch.object(Summarizer, "_write_report", side_effect=OSError("模拟文件写入失败")):
                with self.assertRaises(OSError):
                    await self.run_daily()
            result = await self.run_daily()
        self.assertTrue(Path(result["path"]).is_file())
        self.assertEqual(result["content"], "完整摘要")
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.gets), 2)
        ledger = self.store.api_usage_details()["items"]
        self.assertEqual(len(ledger), 1)
        self.assertAlmostEqual(ledger[0]["cost_cny"], (80*.1+20*.01+10*.5)/1_000_000*7*.5)
        self.assertEqual(ledger[0]["request_id"], "req_original")

    async def test_scheduler_retrieval_does_not_consume_generation_attempt(self):
        with patch("msgx.llm.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(BackgroundResponsePending):
                await self.run_daily()
        self.store.save_summary_job(*self.key, "running", now=100, attempts=3)
        pipe = Pipeline.__new__(Pipeline)
        pipe.store, pipe.llm = self.store, self.llm
        pipe._summary_recovered = False
        pipe._stop = asyncio.Event()
        pipe._summary_wake = asyncio.Event()
        pipe._recover_summary_jobs(101)
        self.assertEqual(self.store.summary_job(*self.key)["state"], "pending")
        pipe.summarizer = SimpleNamespace(run_period=AsyncMock(side_effect=BackgroundResponsePending("继续取回")))
        pipe.llm.snapshot = lambda: (_ for _ in ()).throw(AssertionError("取回结果不检查新请求预算"))
        self.assertFalse(await pipe._ensure_summary_period("daily", date(2026, 10, 8), date(2026, 10, 9), 102, {}))
        job = self.store.summary_job(*self.key)
        self.assertEqual(job["attempts"], 3)
        self.assertEqual(job["state"], "retry")
        self.assertAlmostEqual(job["next_attempt_at"], 162, delta=1)

    async def test_creation_without_id_stays_blocked_until_confirmed(self):
        self.llm._post = AsyncMock(side_effect=AmbiguousResponseError("提交中断，未收到 ID"))
        with self.assertRaises(AmbiguousResponseError):
            await self.run_daily()
        with self.assertRaises(AmbiguousResponseError):
            await self.run_daily()
        self.store.save_summary_job(*self.key, "running", now=100, attempts=1)
        pipe = Pipeline.__new__(Pipeline)
        pipe.store = self.store
        pipe._summary_recovered = False
        pipe._summary_wake = asyncio.Event()
        pipe._recover_summary_jobs(101)
        self.assertEqual(self.store.summary_job(*self.key)["state"], "blocked")
        pipe.retry_summary_job(*self.key, confirmed=True, revision=101)
        self.assertIsNone(self.store.summary_request(self.key))
        self.assertEqual(self.llm._post.await_count, 1)

    async def test_chunk_checkpoint_resume_does_not_repeat_completed_parts(self):
        summarizer = Summarizer(self.cfg, self.store, self.llm)
        summarizer._request_key = self.key
        blocks = [(str(i) + "资料" * 35, [i]) for i in range(5)]
        summarizer._report_info = {"prepared": dict(system="归纳", blocks=blocks, header="", output_tokens=20, input_limit=250)}
        sent = []
        async def post(payload):
            sent.append(payload["messages"][1]["content"])
            return await self.llm._read_response(response("queued", identity=f"resp_{len(sent)}"))
        self.llm._post = post
        async def get(identity):
            if identity == "resp_2" and self.fail_get:
                raise BackgroundResponsePending("模拟第二块断线")
            return await self.llm._read_response(response("completed", text="归纳结果", identity=identity))
        self.llm._retrieve_response = get
        with patch("msgx.llm.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(BackgroundResponsePending):
                await summarizer._summarize_blocks(**summarizer._report_info["prepared"])
            self.fail_get = False
            result = await summarizer._summarize_blocks(**summarizer._report_info["prepared"])
        self.assertEqual(result, "归纳结果")
        self.assertEqual(len(sent), len(set(sent)), "已完成分块和已接受的分块都不得重新提交")
        self.assertGreater(len(sent), 2)

    async def test_terminal_failure_is_not_saved_as_report_or_reposted(self):
        self.llm._retrieve_response = AsyncMock(return_value=await self.llm._read_response(response("failed")))
        with patch("msgx.llm.asyncio.sleep", new=AsyncMock()):
            for _ in range(2):
                with self.assertRaises(IncompleteResponse):
                    await self.run_daily()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.store.api_usage_details()["items"]), 1)
        self.assertIsNone(self.store.summary_for_window("daily", *Pipeline._period_bounds(date(2026, 10, 8), date(2026, 10, 9))))

    async def test_cancellation_after_ack_preserves_retrievable_task(self):
        self.llm._retrieve_response = AsyncMock(side_effect=asyncio.CancelledError)
        with patch("msgx.llm.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                await self.run_daily()
        self.assertEqual(self.store.summary_request(self.key)["response_id"], "resp_test")
        self.assertEqual(self.store.api_usage_details()["items"], [])

    async def test_retrieval_rejection_keeps_id_without_false_unknown_bill(self):
        @asynccontextmanager
        async def get(url, **options):
            yield SimpleNamespace(status=401, headers={}, text=AsyncMock(return_value='{"error":{"code":"invalid_api_key"}}'))
        self.llm.session.return_value.get = get
        with patch("msgx.llm.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(APIRequestRejected):
                await self.run_daily()
        self.assertEqual(self.store.summary_request(self.key)["response_id"], "resp_test")
        self.assertEqual(self.store.api_usage_details()["items"], [])


if __name__ == "__main__":
    unittest.main()
