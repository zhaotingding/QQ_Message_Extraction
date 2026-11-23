"""真实临时 SQLite、隔离协议替身，覆盖主题取材、手动确认、修订和请求恢复。"""
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from msgx import api_settings, config, onebot
from msgx.llm import AmbiguousResponseError, LLM
from msgx.store import Store
from msgx.time_rules import BEIJING
from msgx.topic_reports import TopicReports, apply_changes, candidates


def response(ident, text):
    return json.dumps({"changes":[{"id":ident, "title":"检索评估", "body":text}]}, ensure_ascii=False)


class Offline:
    available = True

    def __init__(self):
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.calls = []
        self.reply = response(None, "召回率提高。")

    def snapshot(self):
        return self

    def budget_ok(self):
        return True

    async def chat(self, system, body, **options):
        self.calls.append((system, json.loads(body), options))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class TopicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "topics.db")
        self.addCleanup(self.store.close)
        self.llm = Offline()
        self.topics = TopicReports(self.store, self.llm)
        self.addAsyncCleanup(self.topics.close)
        self.today = datetime.now(BEIJING).date()

    def report(self, ago, text, kind="daily"):
        start = datetime.combine(self.today - timedelta(days=ago), time.min, BEIJING).timestamp()
        return self.store.add_summary("", int(start), int(start)+86400, 1, text, kind)

    async def update(self, name="RAG", **options):
        value = self.topics.request(name, confirmed=True, **options)
        if value["state"] == "running":
            await self.topics.tasks["rag"]
        return value

    async def test_incremental_sources_highlights_and_late_revisions(self):
        self.report(30, "**RAG**\n老方法用于对照。")
        self.report(3, "**检索增强生成**\n甲群实测。\n\n**绘图**\n不相关。")
        self.report(1, "**RAG**\n乙群复验。")
        self.report(2, "扫码进群 HotAd HotAd HotAd\nhello friend welcome back hello")
        self.report(4, "RAG 周报不作来源", "weekly")
        topics = candidates(self.store)
        self.assertIn({"name":"RAG","days":2,"mentions":2}, topics)
        self.assertLessEqual(len(topics), 4)
        self.assertFalse({"hello", "friend", "HotAd"} & {r["name"] for r in topics})
        with self.assertRaises(ValueError):
            self.topics.request("RAG", confirmed=False)
        await self.update("检索增强生成")
        initial = self.store.topic_work("rag")
        self.assertEqual(initial["revision"], 1)
        self.assertEqual(initial["sections"][0]["parts"], [["召回率提高。", True]])
        material = str(self.llm.calls[0][1]["daily_material"])
        for yes in ("老方法","甲群","乙群"):
            self.assertIn(yes, material)
        for no in ("周报", "绘图", "HotAd"):
            self.assertNotIn(no, material)
        self.assertEqual(self.llm.calls[0][2]["purpose"], "主题报")
        self.assertEqual((await self.update())["state"], "ready")
        self.assertEqual(len(self.llm.calls), 1)
        self.report(8, "RAG 延迟补生成。")
        self.report(3, "检索增强生成 修订后方法。")
        self.llm.reply = response(1, "召回率提高。推理延迟下降。")
        self.topics.request("RAG", confirmed=True)
        await self.update("rag")  # 重复点击时取回同一个进行中的任务。
        edited = self.store.topic_work("rag")
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(self.llm.calls[1][1]["previous_sections"][0]["body"], "召回率提高。")
        self.assertEqual(len(self.llm.calls[1][1]["daily_material"]), 2)
        self.assertEqual(edited["revision"], 2)
        self.assertEqual(edited["sections"][0]["parts"], [["召回率提高。", False], ["推理延迟下降。", True]])

    async def test_manual_retry_preserves_previous_and_handles_empty_delta(self):
        self.report(3, "RAG 原稿")
        await self.update()
        previous = self.store.topic_reports()[0]["sections"]
        self.report(1, "RAG 追加材料。")
        self.llm.reply = AmbiguousResponseError("提交结果未知")
        await self.update()
        self.assertEqual(self.store.topic_reports()[0]["sections"], previous)
        self.assertEqual((await self.update())["state"], "confirm_retry")
        self.assertEqual(len(self.llm.calls), 2)
        self.llm.reply = '{"changes":[{"id":88,"title":"坏编号","body":"错误"}]}'
        await self.update(repeat=True)
        self.assertEqual(self.store.topic_work("rag")["state"], "invalid")
        self.llm.reply = '{"changes":[]}'
        await self.update(repeat=True)
        self.assertEqual(self.store.topic_work("rag")["revision"], 1)
        self.assertEqual((await self.update())["state"], "ready")
        self.assertEqual(len(self.llm.calls), 4)

    def test_new_section_with_same_heading_keeps_earlier_findings(self):
        previous = [{"id":1,"title":"检索评估","body":"第一组测试。第二组测试。"}]
        result = apply_changes(previous, response(None, "第二组测试。第三组测试。"))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["body"], "第一组测试。第二组测试。\n第三组测试。")

    async def test_authenticated_route_and_multiple_batches(self):
        for i in range(1, 5):
            self.report(i, "RAG 资料 " + "测"*450)
        app = web.Application()
        app["cfg"] = {"onebot": {"access_token": "isolated"}}
        app["pipe"] = SimpleNamespace(topics=self.topics)
        app.router.add_post("/topics/update", onebot._topic_update)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        payload = {"name": "RAG", "confirmed": True}
        self.assertEqual((await client.post("/topics/update",json=payload)).status, 403)
        headers = {"Authorization":"Bearer isolated"}
        with patch("msgx.topic_reports.period_token_limits", return_value=(2000, 1370)):
            result = await client.post("/topics/update",json=payload,headers=headers)
            self.assertEqual(result.status, 200)
            task = self.topics.tasks.get("rag")
            if task:
                await task
        self.assertGreater(len(self.llm.calls), 1, self.store.topic_work("rag")["note"])
        self.assertEqual(sum(len(body["daily_material"]) for _,body,_ in self.llm.calls), 4)
        self.assertEqual(len(self.store.topic_work("rag")["sources"]), 4)

    async def test_official_retrieval_reuses_request_and_accounts_once(self):
        class Stub(LLM):
            posts = reads = 0
            def snapshot(self):
                return self
            def resume_snapshot(self, metadata):
                return self
            async def _post(self, payload):
                self.posts += 1
                return {"response_id":"resp_local","response_status":"queued"}
            async def _retrieve_response(self, _):
                self.reads += 1
                if self.reads == 1:
                    raise RuntimeError("隔离断线")
                return {"response_id":"resp_local","response_status":"completed","service_tier":"default",
                        "choices":[{"finish_reason":"stop","message":{"content":response(None, "隔离结果。")}}],
                        "usage":{"prompt_tokens":100,"completion_tokens":30}}
        self.report(1, "RAG 一条新进展")
        cfg = api_settings.apply(config.DEFAULTS, {**api_settings.DEFAULTS, "provider":"测试"})
        llm = Stub(cfg, self.store)
        llm.configure("offline-not-a-key")
        self.topics.llm = llm
        await self.update()
        self.assertEqual(self.store.topic_work("rag")["state"], "interrupted")
        await self.update()
        self.assertEqual((llm.posts,llm.reads), (1,2))
        self.assertEqual(self.store.topic_reports()[0]["revision"], 1)
        charges = self.store.conn.execute("SELECT source_type FROM api_calls WHERE purpose='主题报'").fetchall()
        self.assertEqual([r["source_type"] for r in charges], ["汇总报告"])


if __name__ == "__main__":
    unittest.main()
