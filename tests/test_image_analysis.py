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
from msgx.image_policy import is_sticker, screen_image, validate_analysis
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
        self.store.set_image_result(image["id"], ocr_text="Traceback RuntimeError: failed\n" * 8,
            media_path=str(self.path), media_hash=digest, media_state="done")
        return self.store.get_image(image["id"])

    def enable(self, **values):
        self.cfg["media"]["analysis"].update(enabled=True, **values)

    async def test_disabled_only_screens_local_files(self):
        row = self.image("off")
        await self.worker.analysis.process()
        self.assertEqual(self.fake.calls, [])
        self.assertGreaterEqual(self.store.get_image(row["id"])["screen_score"], 70)
        self.assertEqual(self.store.get_image(row["id"])["include_vision"], 0)

    async def test_success_context_and_summary_reuse(self):
        self.enable()
        row = self.image("one")
        await self.worker.analysis.process()
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
        await self.worker.analysis.process()
        other = MediaWorker(self.cfg, self.store, self.fake)
        await other.analysis.process()
        await other.close()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.store.get_image(row["id"])["auto_state"], "unknown")
        self.assertGreater(self.store.automatic_image_usage()["unknown"], 0)

    async def test_budget_and_count_stop_additional_calls(self):
        self.enable(daily_limit=1)
        self.image("first")
        await self.worker.analysis.process()
        self.image("second", digest="different")
        await self.worker.analysis.process()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.worker.analysis.status["state"], "limited")

    async def test_duplicate_same_context_reuses_saved_vision(self):
        self.enable()
        self.image("first")
        await self.worker.analysis.process()
        second = self.image("second")
        await self.worker.analysis.process()
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
        result = {"choices": [{"message": {"content": "图片说明"}}]}
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


if __name__ == "__main__":
    unittest.main()
