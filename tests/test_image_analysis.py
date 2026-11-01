"""图片初筛、独立API和收费边界；全程临时库、合成图片和本地替身。"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from PIL import Image
from msgx import api_settings, prefs
from msgx.config import DEFAULTS
from msgx.image_policy import ANALYSIS_DELAY_SECONDS, KEYWORD_RULES, SCREEN_VERSION, is_sticker, screen_image, validate_analysis
from msgx.llm import LLM
from msgx.media import MediaWorker
from msgx.models import Msg
from msgx.store import Store


class FakeLLM:
    vision_available = True

    def __init__(self):
        self.calls = []
        self.ok = True

    async def describe_image(self, path, prompt, **kwargs):
        self.calls.append((str(path), prompt, kwargs))
        return ("可复现的异常与修复方法" if self.ok else "", self.ok)


class PolicyTests(unittest.TestCase):
    def test_technical_evidence_and_ads(self):
        self.assertGreaterEqual(screen_image("Traceback RuntimeError: CUDA error\n" * 8, "复现原因").score, 70)
        decision = screen_image("货源现货、低价报价，联系我", "代充合租")
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.score, 0)
        self.assertLess(screen_image("哈哈", "这张表情不错").score, 70)

    def test_sticker_and_animation_are_never_candidates(self):
        raw = json.dumps({"message": [{"type": "mface", "data": {"url": "u"}}]})
        self.assertTrue(is_sticker(raw, "u"))
        self.assertFalse(is_sticker(raw, "another"))
        self.assertTrue(screen_image("RuntimeError", "复现", animated=True).blocked)

    def test_invalid_limits(self):
        for value in ({"threshold": 0}, {"daily_limit": -1}, {"daily_budget_cny": float('nan')}, {"history_days": 999}):
            with self.assertRaises(ValueError):
                validate_analysis(value)

    def test_report_keywords_have_different_weights_and_repetition_cap(self):
        weights = {name: weight for name, _, weight in KEYWORD_RULES}
        self.assertEqual(len(weights), 22)
        self.assertGreater(weights["Codex"], weights["训练与梯度"])
        once = screen_image("Codex", "").score
        twice = screen_image("Codex Codex", "").score
        self.assertGreater(twice, once)
        self.assertEqual(screen_image("Codex " * 500, "").score, twice)
        self.assertLess(twice, 70)

    def test_plain_text_and_context_alone_do_not_score(self):
        self.assertEqual(screen_image("明天一起去吃饭。" * 100, "").score, 0)
        self.assertEqual(screen_image("", "CUDA RuntimeError Codex API").score, 0)
        decision = screen_image("CUDA RuntimeError: failed; API 429", "")
        self.assertGreaterEqual(decision.score, 70)
        self.assertIn("GPU与显存", decision.reason)


class AnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="msgx-image-analysis-")
        self.root = Path(self.temp.name)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["app"].update(db_path=str(self.root / "test.db"), media_dir=str(self.root / "media"))
        self.cfg["napcat"]["account"] = "111"
        self.store = Store(self.cfg["app"]["db_path"])
        self.fake = FakeLLM()
        self.worker = MediaWorker(self.cfg, self.store, self.fake)
        self.env = patch.dict(os.environ, {"MSGX_PREFS": str(self.root / "absent.yaml")})
        self.env.start()
        self.path = self.root / "image.png"
        Image.new("RGB", (200, 180), "white").save(self.path)

    async def asyncTearDown(self):
        self.env.stop()
        await self.worker.close()
        self.store.close()
        self.temp.cleanup()

    def image(self, key, *, text="复现原因[图片]", group="g", account="111", digest="same"):
        row_id = self.store.insert_message(Msg(msg_id=key, group_id=group, sender_id="222", ts=int(time.time()),
            text=text, msg_type="mixed", image_urls=["local:" + key]), bot_uin=account)
        image = self.store.conn.execute("SELECT * FROM message_images WHERE message_row=?", (row_id,)).fetchone()
        self.store.set_image_result(image["id"], ocr_text="Traceback RuntimeError: CUDA error\n" * 8,
            media_path=str(self.path), media_hash=digest, media_state="done")
        return self.store.get_image(image["id"])

    def enable(self, **values):
        self.cfg["media"]["analysis"].update(enabled=True, **values)

    async def process_ready(self):
        await self.worker.analysis.process()  # 首次只标记，测试显式推进持久化就绪时间。
        self.store.conn.execute("UPDATE message_images SET auto_ready_at=? WHERE auto_state='waiting'", (time.time()-1,))
        self.store.conn.commit()
        await self.worker.analysis.process()

    async def test_disabled_only_screens_local_files(self):
        row = self.image("off")
        await self.worker.analysis.process()
        self.assertEqual(self.fake.calls, [])
        self.assertGreaterEqual(self.store.get_image(row["id"])["screen_score"], 70)
        self.assertEqual(self.store.get_image(row["id"])["include_vision"], 0)

    async def test_success_context_and_summary_reuse(self):
        self.enable()
        row = self.image("one")
        await self.process_ready()
        saved = self.store.get_image(row["id"])
        self.assertEqual(saved["auto_state"], "ok")
        self.assertEqual(saved["include_vision"], 1)
        self.assertIn("复现原因", self.fake.calls[0][1])
        self.assertEqual(self.fake.calls[0][2]["purpose"], "自动图片理解")
        parent = self.store.get_message(saved["message_row"])
        self.assertIn("可复现", parent["vision_text"])
        self.assertTrue(self.store.messages_between_all(int(time.time())-100, int(time.time())+100)[0]["summary_picture"])
        await self.worker.analysis.process()
        self.assertEqual(len(self.fake.calls), 1)

    async def test_failed_unknown_not_retried_after_restart(self):
        self.enable()
        self.fake.ok = False
        row = self.image("failure")
        await self.process_ready()
        other = MediaWorker(self.cfg, self.store, self.fake)
        await other.analysis.process()
        await other.close()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.store.get_image(row["id"])["auto_state"], "unknown")
        self.assertGreater(self.store.automatic_image_usage()["unknown"], 0)

    async def test_budget_and_count_stop_additional_calls(self):
        self.enable(daily_limit=1)
        self.image("first")
        await self.process_ready()
        self.image("second", digest="different")
        await self.worker.analysis.process()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.worker.analysis.status["state"], "limited")

    async def test_duplicate_same_context_reuses_saved_vision(self):
        self.enable()
        self.image("first")
        await self.process_ready()
        second = self.image("second")
        await self.process_ready()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.store.get_image(second["id"])["auto_state"], "cached")

    async def test_other_account_and_unwatched_groups_not_uploaded(self):
        self.enable()
        self.cfg["watch"]["groups"] = ["wanted"]
        self.image("wrong_group")
        self.image("wrong_account", group="wanted", account="999")
        await self.worker.analysis.process()
        self.assertEqual(self.fake.calls, [])

    async def test_left_group_not_uploaded(self):
        self.enable()
        self.image("old_group")
        self.store.sync_account_groups("111", {"current"})
        await self.worker.analysis.process()
        self.assertEqual(self.fake.calls, [])

    async def test_context_excludes_other_accounts_and_senders(self):
        row = self.image("context")
        self.store.insert_message(Msg(msg_id="hidden", group_id="g", ts=int(time.time()), text="other-account-secret"), bot_uin="999")
        self.assertNotIn("other-account-secret", self.store.image_context(row["message_row"]))

    async def test_waits_fifteen_minutes_and_keeps_wait_across_restart(self):
        self.enable()
        row = self.image("delayed")
        started = time.time()
        await self.worker.analysis.process()
        saved = self.store.get_image(row["id"])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(saved["auto_state"], "waiting")
        self.assertGreaterEqual(saved["auto_ready_at"], started+ANALYSIS_DELAY_SECONDS)
        self.assertFalse(self.store.claim_auto_image(row["id"]))
        self.assertEqual(self.worker.analysis.status["state"], "waiting")
        other = MediaWorker(self.cfg, self.store, self.fake)
        await other.analysis.process()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.store.get_image(row["id"])["auto_ready_at"], saved["auto_ready_at"])
        await other.close()
        await self.process_ready()
        self.assertEqual(len(self.fake.calls), 1)

    async def test_delayed_context_contains_followup_and_preserves_ocr(self):
        self.enable(context_messages=8)
        row = self.image("followup")
        now = int(time.time())
        self.store.insert_message(Msg(msg_id="resolution", group_id="g", sender_id="222", ts=now+12*60,
            text="后续结论：CUDA驱动版本不兼容，回滚解决"), bot_uin="111")
        self.store.insert_message(Msg(msg_id="outsider", group_id="other", ts=now+12*60, text="其他群不应出现"), bot_uin="111")
        await self.process_ready()
        self.assertIn("后续结论", self.fake.calls[0][1])
        self.assertNotIn("其他群不应出现", self.fake.calls[0][1])
        saved = self.store.get_image(row["id"])
        self.assertIn("Traceback", saved["ocr_text"])
        self.assertIn("修复方法", saved["vision_text"])

    async def test_disabling_after_mark_keeps_waiting_without_call(self):
        self.enable()
        row = self.image("disable-marked")
        await self.worker.analysis.process()
        self.cfg["media"]["analysis"]["enabled"] = False
        self.store.conn.execute("UPDATE message_images SET auto_ready_at=?", (time.time()-1,))
        self.store.conn.commit()
        await self.worker.analysis.process()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.store.get_image(row["id"])["auto_state"], "waiting")

    async def test_higher_score_waiting_does_not_block_ready_candidate(self):
        self.enable()
        ready = self.image("ready-first", digest="ready")
        waiting = self.image("waiting-high", digest="waiting")
        await self.worker.analysis.process()
        self.store.conn.execute("UPDATE message_images SET screen_score=80,auto_ready_at=? WHERE id=?", (time.time()-1, ready["id"]))
        self.store.conn.execute("UPDATE message_images SET screen_score=100 WHERE id=?", (waiting["id"],))
        self.store.conn.commit()
        await self.worker.analysis.process()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.store.get_image(ready["id"])["auto_state"], "ok")
        self.assertEqual(self.store.get_image(waiting["id"])["auto_state"], "waiting")

    async def test_rescreening_uses_two_bounded_index_queries(self):
        images = [self.image(f"batch-{index}") for index in range(8)]
        for row in images[::2]:
            self.store.conn.execute("UPDATE message_images SET screen_state='done',screen_version=? WHERE id=?", (SCREEN_VERSION-1, row["id"]))
        self.store.conn.commit()
        queries = []
        self.store.conn.set_trace_callback(queries.append)
        try:
            rows = self.store.screening_images(5)
        finally:
            self.store.conn.set_trace_callback(None)
        self.assertEqual([row["id"] for row in rows], [row["id"] for row in reversed(images[-5:])])
        selects = [query for query in queries if query.startswith("SELECT i.*")]
        self.assertEqual(len(selects), 2)
        self.assertTrue(all(query.endswith("LIMIT 5") for query in selects))
        plans = [[str(row[3]) for row in self.store.conn.execute("EXPLAIN QUERY PLAN " + query)] for query in selects]
        for plan in plans:
            self.assertTrue(any("SEARCH i" in part and "idx_image_screen" in part for part in plan), plans)
            self.assertFalse(any("SCAN i" in part for part in plan), plans)  # 两分支走图片状态索引，不按特定索引名绑定优化器。
        self.assertIn("i.screen_version<>", selects[1])  # 规则指纹无大小顺序，所有不同版本均须回筛。

    async def test_empty_rescreening_is_not_repeated_until_scope_expands(self):
        old = self.image("expired-legacy")
        self.store.conn.execute("UPDATE message_images SET screen_state='done',screen_version=? WHERE id=?", (SCREEN_VERSION-1, old["id"]))
        self.store.conn.execute("UPDATE messages SET ts=? WHERE id=?", (time.time()-14*86400, old["message_row"]))
        self.store.conn.commit()
        queries = []
        self.store.conn.set_trace_callback(queries.append)
        try:
            self.assertEqual(self.store.screening_images(history_days=7), [])
            first = [query for query in queries if query.startswith("SELECT i.*")]
            self.assertEqual(len(first), 2)
            queries.clear()
            self.assertEqual(self.store.screening_images(history_days=7), [])
            second = [query for query in queries if query.startswith("SELECT i.*")]
            self.assertEqual(len(second), 1)
            self.assertNotIn("screen_version<", second[0])
            queries.clear()
            expanded = self.store.screening_images(history_days=30)
            self.assertEqual([row["id"] for row in expanded], [old["id"]])
            self.assertEqual(len([query for query in queries if query.startswith("SELECT i.*")]), 2)
        finally:
            self.store.conn.set_trace_callback(None)

    async def test_shared_message_context_uses_current_receiving_account(self):
        row = self.image("shared", account="999")
        self.store.conn.execute("INSERT INTO message_receipts VALUES(?,?,?,?)", ("111", "g", "shared", row["message_row"]))
        self.store.insert_message(Msg(msg_id="private", group_id="g", ts=int(time.time()), text="account999-only"), bot_uin="999")
        self.store.insert_message(Msg(msg_id="mine", group_id="g", ts=int(time.time()), text="account111-context"), bot_uin="111")
        context = self.store.image_context(row["message_row"], account="111")
        self.assertIn("account111-context", context)
        self.assertNotIn("account999-only", context)

    async def test_image_output_limit_respects_selected_profile(self):
        cfg = api_settings.apply(self.cfg, {**api_settings.DEFAULTS, "max_output_tokens": 500})
        client = LLM(cfg, self.store)
        client.configure("test-only-key")
        result = {"choices": [{"message": {"content": "图片说明"}, "finish_reason": "stop"}]}
        with patch.object(client, "_accounted_post", AsyncMock(return_value=result)) as request:
            text, ok = await client.describe_image(self.path, "test")
        self.assertTrue(ok)
        self.assertEqual(text, "图片说明")
        self.assertEqual(request.call_args.args[0]["max_tokens"], 500)
        self.assertEqual(client.image_output_limit(), 500)

    async def test_sticker_not_downloaded_or_ocr(self):
        msg = self.store.insert_message(Msg(msg_id="face", group_id="g", ts=int(time.time()), text="[表情包]", image_urls=["mface-url"],
            raw={"message": [{"type": "mface", "data": {"url": "mface-url"}}]}), bot_uin="111")
        row = self.store.conn.execute("SELECT * FROM message_images WHERE message_row=?", (msg,)).fetchone()
        with patch.object(self.worker, "_download", side_effect=AssertionError("sticker download")):
            await self.worker._process_image(row)
        self.assertEqual(self.store.get_image(row["id"])["screen_state"], "blocked")
        self.assertEqual(self.store.get_message(msg)["media_state"], "filtered")
        self.assertEqual(self.store.group_activity()["groups"][0]["count"], 0)

    async def test_saved_image_profile_does_not_change_summary_profile(self):
        summary = {**api_settings.DEFAULTS, "model": "summary-model"}
        vision = {**api_settings.DEFAULTS, "model": "cheap-vision", "input_price": .01}
        file = self.root / "preferences.yaml"
        prefs.save({"api_profiles": {"openai": summary, "cheap": vision}, "active_api_profile": "openai", "image_api_profile": "cheap"}, file)
        with patch.dict(os.environ, {"MSGX_PREFS": str(file)}), patch("msgx.config.api_key", return_value="test-key"):
            client = LLM(self.cfg, self.store)
            image = client.snapshot(role="image")
            normal = client.snapshot()
        self.assertEqual(image.llm["model"], "cheap-vision")
        self.assertEqual(image.cfg["active_api_profile"], "cheap")
        self.assertEqual(normal.llm["model"], "summary-model")
        self.assertEqual(normal.cfg["active_api_profile"], "openai")

    async def test_changed_personal_rules_rescore_and_empty_list_does_not_upload(self):
        row = self.image("changed-rules")
        await self.worker.analysis.process()
        before = self.store.get_image(row["id"])
        self.assertGreaterEqual(before["screen_score"], 70)
        self.cfg["media"]["analysis"]["keyword_rules"] = []
        self.enable()
        self.store.conn.execute("UPDATE message_images SET auto_ready_at=? WHERE id=?", (time.time()-1, row["id"]))
        self.store.conn.commit()
        await self.worker.analysis.process()
        after = self.store.get_image(row["id"])
        self.assertEqual(after["screen_score"], 0)
        self.assertNotEqual(after["screen_version"], before["screen_version"])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(after["ocr_text"], before["ocr_text"])

    async def test_partial_saved_limits_keep_configured_rules_and_hidden_values(self):
        self.cfg["media"]["analysis"].update(keyword_rules=[], context_messages=16)
        saved = {"image_analysis": {"threshold": 50, "daily_limit": 200, "daily_budget_cny": 10}}
        with patch("msgx.prefs.load_from_env", return_value=saved):
            options, _ = self.worker.analysis.options()
        self.assertEqual(options["keyword_rules"], [])
        self.assertEqual(options["context_messages"], 16)
        self.assertEqual(options["daily_limit"], 200)

    async def test_old_high_score_is_not_uploaded_before_rule_rescore_finishes(self):
        rows = [self.image(f"revision-{index}", digest=str(index)) for index in range(6)]
        for row in rows:
            self.store.set_screen_result(row["id"], screen_image("CUDA API RuntimeError:", ""))
        self.store.conn.execute("UPDATE message_images SET auto_ready_at=?", (time.time()-1,))
        self.store.conn.commit()
        self.cfg["media"]["analysis"]["keyword_rules"] = []
        self.enable()
        await self.worker.analysis.process()  # 每批只重筛5张；余下旧高分不能作为新规则候选。
        self.assertEqual(self.fake.calls, [])
        self.assertGreaterEqual(self.store.get_image(rows[0]["id"])["screen_score"], 70)


if __name__ == "__main__":
    unittest.main()
