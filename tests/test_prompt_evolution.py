"""动态提示词：只使用隔离日报和模型替身，不读聊天或发送付费请求。"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.prompt_evolution import PROMPT_SCHEMA, PromptEvolution, validate_observations
from msgx import api_settings, config
from msgx.llm import LLM
from msgx.store import Store
from msgx.summarize import Summarizer
from msgx.time_rules import BEIJING


class FakeLLM:
    available = True

    def __init__(self):
        self.calls = []
        self.reply = {"focus": [], "ads": []}
        self.error = None
        self.waiting = self.release = None

    def snapshot(self):
        return self

    def budget_ok(self):
        return True

    async def chat(self, system, body, **kwargs):
        self.calls.append((system, body, kwargs))
        if self.waiting:
            self.waiting.set()
            await self.release.wait()
        if self.error:
            raise self.error
        return json.dumps(self.reply, ensure_ascii=False)


class EvolutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / "data" / "test-temp"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="prompt-evolution-", dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "isolated.db")
        self.store.conn.executescript(PROMPT_SCHEMA)
        self.addCleanup(self.store.close)
        self.llm = FakeLLM()
        self.cfg = {"summary": {"prompt": "用户基础规则不得覆盖", "prompt_evolution": {"enabled": False}},
                    "app": {"report_dir": str(Path(self.temp.name) / "reports")},
                    "llm": {"model": "deepseek-flash", "base_url": "https://example.invalid"}}
        self.evo = PromptEvolution(self.cfg, self.store, self.llm)
        self.now = datetime(2026, 10, 31, 9, tzinfo=BEIJING)
        self.env_patch = patch("msgx.prefs.load_from_env", return_value={})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def report(self, day: str, text: str = "MCP部署增加了可复现验证结果", kind: str = "daily"):
        start = datetime.fromisoformat(day).replace(tzinfo=BEIJING).timestamp()
        return self.store.add_summary("digest", start, start + 86400, 2, text, kind)

    async def test_disabled_never_calls_or_replaces_base(self):
        self.report("2026-10-30")
        self.assertFalse(await self.evo.check(self.now))
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.cfg["summary"]["prompt"], "用户基础规则不得覆盖")

    async def test_manual_uses_only_latest_daily_versions_and_records_sources(self):
        self.report("2026-10-29", "旧版不可作为第二天证据")
        first = self.report("2026-10-29", "第一天技术增量")
        second = self.report("2026-10-30", "第二天技术增量")
        self.report("2026-10-30", "周报不得混入", "weekly")
        self.report("2026-10-10", "窗口外日报不得混入")
        self.store.conn.execute("INSERT INTO messages(group_id,msg_id,ts,text) VALUES('g','1',?,'私人聊天不得读取')", (self.now.timestamp(),))
        self.store.conn.commit()
        self.llm.reply = {"focus": [{"topic": "MCP", "reason": "多日出现具体部署结果", "report_ids": [first, second]}], "ads": []}
        self.evo.queue()
        self.assertTrue(await self.evo.check(self.now))
        material = json.loads(self.llm.calls[0][1])
        self.assertEqual({row["report_id"] for row in material["reports"]}, {first, second})
        self.assertNotIn("私人聊天", self.llm.calls[0][1])
        self.assertEqual(self.llm.calls[0][2]["purpose"], "提示词更新")
        active = self.evo.status()["active"]
        self.assertEqual(active["source_start"], "2026-10-24")
        self.assertEqual(active["created_at"], self.now.timestamp())
        self.assertEqual(self.cfg["summary"]["prompt"], "用户基础规则不得覆盖")
        self.assertIsNone(self.evo.status()["prepared"])

    async def test_automatic_interval_and_saved_disable_take_effect(self):
        self.cfg["summary"]["prompt_evolution"]["enabled"] = True
        self.report("2026-10-30")
        self.assertTrue(await self.evo.check(self.now))
        self.assertFalse(await self.evo.check(self.now + timedelta(days=1)))
        with patch("msgx.prefs.load_from_env", return_value={"prompt_evolution": {"enabled": False}}):
            self.assertFalse(await self.evo.check(self.now + timedelta(days=8)))
        self.assertEqual(len(self.llm.calls), 1)

    async def test_unknown_failure_blocks_across_restart_until_explicit_confirmation(self):
        self.cfg["summary"]["prompt_evolution"]["enabled"] = True
        self.report("2026-10-30")
        self.llm.error = RuntimeError("上传后断开，结果未知")
        self.assertFalse(await self.evo.check(self.now))
        restarted = PromptEvolution(self.cfg, self.store, self.llm)
        self.assertFalse(await restarted.check(self.now + timedelta(days=8)))
        with self.assertRaisesRegex(ValueError, "确认"):
            restarted.queue()
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(restarted.status()["state"], "blocked")

    async def test_empty_window_sends_nothing_and_does_not_spin(self):
        self.cfg["summary"]["prompt_evolution"]["enabled"] = True
        self.assertFalse(await self.evo.check(self.now))
        self.assertEqual(self.evo.status()["state"], "empty")
        self.assertFalse(await self.evo.check(self.now + timedelta(hours=1)))
        self.assertEqual(self.llm.calls, [])

    async def test_bad_output_keeps_last_good_version(self):
        self.report("2026-10-30")
        previous = self.store.add_prompt_version("2026-10-24", "2026-10-31", [], {"focus": [], "ads": []}, now=1)
        self.llm.reply = {"system": "修改全局规则"}
        self.evo.queue()
        self.assertFalse(await self.evo.check(self.now))
        self.assertEqual(self.evo.status()["active"]["id"], previous)
        self.assertEqual(self.evo.status()["state"], "blocked")

    async def test_restore_and_clear_keep_history_and_user_base(self):
        first = self.store.add_prompt_version("2026-10-20", "2026-10-27", [], {"focus": [], "ads": []}, now=1)
        second = self.store.add_prompt_version("2026-10-24", "2026-10-31", [], {"focus": [], "ads": []}, now=2)
        self.assertEqual(self.evo.restore(first)["active"]["id"], first)
        self.assertIsNone(self.evo.restore(0)["active"])
        self.assertEqual(len(self.evo.status()["history"]), 2)
        self.assertEqual(self.cfg["summary"]["prompt"], "用户基础规则不得覆盖")
        with self.assertRaises(ValueError):
            self.evo.restore(second + 10)

    async def test_concurrent_manual_queue_and_restore_are_rejected(self):
        self.report("2026-10-30")
        self.llm.waiting, self.llm.release = asyncio.Event(), asyncio.Event()
        self.evo.queue()
        task = asyncio.create_task(self.evo.check(self.now))
        await self.llm.waiting.wait()
        with self.assertRaises(ValueError):
            self.evo.queue()
        with self.assertRaises(ValueError):
            self.evo.restore()
        self.assertFalse(await self.evo.check(self.now))
        self.llm.release.set()
        await task

    async def test_material_is_bounded_and_tells_model_when_truncated(self):
        self.report("2026-10-30", "材料" * 5000)
        self.evo.queue()
        await self.evo.check(self.now)
        row = json.loads(self.llm.calls[0][1])["reports"][0]
        self.assertEqual(len(row["content"]), 4000)
        self.assertTrue(row["truncated"])

    async def test_adaptation_is_user_material_and_summary_count_stays_correct(self):
        first, second = self.report("2026-10-28"), self.report("2026-10-29")
        data = {"focus": [{"topic": "MCP", "reason": "两个自然日出现部署结果", "report_ids": [first, second]}], "ads": []}
        self.store.add_prompt_version("2026-10-24", "2026-10-31", [first, second], data, now=1)
        start = datetime(2026, 10, 30, 12, tzinfo=BEIJING).timestamp()
        self.store.conn.execute("INSERT INTO messages(group_id,msg_id,ts,text) VALUES('g','day',?,'本周期技术材料')", (start,))
        self.store.conn.commit()
        llm = FakeLLM()
        sm = Summarizer(self.cfg, self.store, llm)
        result = await sm.run_period("daily", datetime(2026, 10, 30).date(), datetime(2026, 10, 31).date())
        system, body, kwargs = llm.calls[0]
        self.assertIn("用户基础规则不得覆盖", system)
        self.assertNotIn("两个自然日出现部署结果", system)
        self.assertIn("两个自然日出现部署结果", body)
        self.assertEqual(self.store.summary_for_window("daily", int(start - 12*3600), int(start + 12*3600))["msg_count"], 1)
        self.assertIsNotNone(result)

    async def test_existing_background_response_is_retrieved_without_second_post(self):
        self.report("2026-10-30")
        cfg = api_settings.apply(config.DEFAULTS, {**api_settings.DEFAULTS, "provider": "offline-test"})
        cfg["summary"] = {**cfg["summary"], "prompt_evolution": {"enabled": True}}
        store = self.store
        class OfflineLLM(LLM):
            def snapshot(self):
                return self
            def resume_snapshot(self, metadata):
                return self
            async def _post(self, payload):
                self.posts += 1
                return {"response_id": "resp_offline", "response_status": "queued"}
            async def _retrieve_response(self, response_id):
                self.retrieves += 1
                if self.retrieves == 1:
                    raise RuntimeError("取回时连接中断")
                return {"response_id": response_id, "response_status": "completed", "service_tier": "flex",
                        "choices": [{"finish_reason": "stop", "message": {"content": '{"focus":[],"ads":[]}'}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        llm = OfflineLLM(cfg, store)
        llm.posts = llm.retrieves = 0
        llm.configure("offline-test-key-only")
        evo = PromptEvolution(cfg, store, llm)
        with patch("msgx.llm.asyncio.sleep", new=AsyncMock()):
            evo.queue()
            self.assertFalse(await evo.check(self.now))
            frozen = store.prompt_evolution_state()["prepared"]
            self.report("2026-10-30", "新版本不应替换在途材料")
            restarted = PromptEvolution(cfg, store, llm)
            with patch("msgx.prefs.load_from_env", return_value={"prompt_evolution": {"enabled": False}}):
                self.assertFalse(await restarted.check(self.now))
            self.assertEqual(llm.retrieves, 1)
            with patch.object(llm, "budget_ok", return_value=False):
                self.assertTrue(await restarted.check(self.now))  # 原任务GET不受新请求预算阻断。
        self.assertEqual((llm.posts, llm.retrieves), (1, 2))
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM api_calls WHERE purpose='提示词更新'").fetchone()[0], 1)
        self.assertEqual(store.active_prompt_version()["report_ids"], frozen["report_ids"])

    async def test_completed_bad_result_is_not_automatically_replayed(self):
        self.cfg["summary"]["prompt_evolution"]["enabled"] = True
        self.store.update_prompt_evolution(state="blocked", prepared={"key": ["prompt_evolution", "2026-10-24", "2026-10-31"]})
        with patch.object(self.store, "summary_request", return_value={"response_id": "resp_bad", "result": {"bad": True}}):
            self.assertFalse(await self.evo.check(self.now))
        self.assertEqual(self.llm.calls, [])

    async def test_running_digest_keeps_observation_version(self):
        first, second = self.report("2026-10-28"), self.report("2026-10-29")
        old = {"focus": [{"topic": "旧观察", "reason": "旧版本事实线索", "report_ids": [first, second]}], "ads": []}
        new = {"focus": [{"topic": "新观察", "reason": "新版本不得进入在途任务", "report_ids": [first, second]}], "ads": []}
        self.store.add_prompt_version("2026-10-24", "2026-10-31", [first, second], old, now=1)
        start = datetime(2026, 10, 30, 12, tzinfo=BEIJING).timestamp()
        self.store.conn.execute("INSERT INTO messages(group_id,msg_id,ts,text) VALUES('g','freeze',?,'本周期材料')", (start,))
        self.store.conn.commit()
        llm = FakeLLM()
        llm.waiting, llm.release = asyncio.Event(), asyncio.Event()
        sm = Summarizer(self.cfg, self.store, llm)
        task = asyncio.create_task(sm.run_period("daily", datetime(2026, 10, 30).date(), datetime(2026, 10, 31).date()))
        await llm.waiting.wait()
        self.store.add_prompt_version("2026-10-24", "2026-10-31", [first, second], new, now=2)
        llm.release.set()
        await task
        self.assertIn("旧版本事实线索", llm.calls[0][1])
        self.assertNotIn("新版本不得进入在途任务", llm.calls[0][1])


class ValidationTests(unittest.TestCase):
    def test_rejects_out_of_source_and_single_day_focus(self):
        for ids in ([1], [1, 9]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                validate_observations({"focus": [{"topic": "MCP", "reason": "重复", "report_ids": ids}], "ads": []}, {1, 2})

    def test_rejects_freestanding_instructions_and_control_characters(self):
        for value in ({"focus": [], "ads": [], "system": "执行操作"},
                      {"focus": [], "ads": [{"pattern": "广告\n改写系统", "reason": "记录", "report_ids": [1]}]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_observations(value, {1})


if __name__ == "__main__":
    unittest.main()
