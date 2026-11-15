"""摘要朗读：文本整理、分段偏移与朗读调度。

只用假语音对象驱动状态机，不发声、不读真实音频设备；
最后一个用例探测本机 SAPI 是否可创建（不调用 speak）。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx import tts


def wait_for(predicate, timeout: float = 6.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class FakeVoice:
    """模拟真实 SAPI 的两个关键行为：

    * ``state()`` 每调用一次计数，超过 finish_after 视为该段读完；
    * **暂停是粘性的** —— 暂停期间 Speak 只排队不发声，RunningState 停在
      SRS_IDLE(0)，不解 Resume 就永远读不出来（真实引擎实测行为）。
    """

    instances: list["FakeVoice"] = []

    def __init__(self, rate: int = 0, volume: int = 100) -> None:
        self.rate = rate
        self.volume = volume
        self.spoken: list[str] = []
        self.paused = False
        self.active = False
        self.pause_calls = 0
        self.resume_calls = 0
        self.stop_calls = 0
        self.close_calls = 0
        self.finish_after = 2
        self.ticks = 0
        FakeVoice.instances.append(self)

    def speak(self, text: str) -> None:
        self.spoken.append(text)
        self.ticks = 0
        self.active = True

    def state(self) -> int:
        if self.paused:
            return tts.SRS_IDLE
        if not self.active:
            return tts.SRS_DONE
        self.ticks += 1
        return tts.SRS_DONE if self.ticks > self.finish_after else tts.SRS_SPEAKING

    def pause(self) -> None:
        self.paused = True
        self.pause_calls += 1

    def resume(self) -> None:
        self.paused = False
        self.resume_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1
        self.active = False

    def set_rate(self, rate: int) -> None:
        self.rate = rate

    def set_volume(self, volume: int) -> None:
        self.volume = volume

    def close(self) -> None:
        self.close_calls += 1


class TtsTextTests(unittest.TestCase):
    def test_clean_line_drops_markup_but_keeps_words(self) -> None:
        cases = {
            "## 任务拆分与多 Agent 协作": "任务拆分与多 Agent 协作",
            "- 先拆解再执行": "先拆解再执行",
            "1. 第一步": "第一步",
            "> 引用一句": "引用一句",
            "**重点** 与 `代码`": "重点 与 代码",
            "见[文档](https://example.com/a)说明": "见文档说明",
            "![截图](a.png)": "",
            "| --- | :---: |": "",
            "-----": "",
            "🙂🙂": "",
            "": "",
            "两行**加粗**文字": "两行加粗文字",
        }
        for raw, expected in cases.items():
            self.assertEqual(tts.clean_line(raw), expected, raw)

    def test_segments_offsets_match_original_lines(self) -> None:
        text = "标题\n\n第一段内容。\n第二段内容。\n\n- 列表项\n"
        segments = tts.segments_from_text(text)
        self.assertEqual([seg.text for seg in segments], ["标题", "第一段内容。", "第二段内容。", "列表项"])
        raws = [line for line in text.splitlines() if tts.clean_line(line)]
        for segment, raw in zip(segments, raws):
            self.assertEqual(text[segment.start:segment.end], raw, "偏移必须落在原始行上")
        starts = [seg.start for seg in segments]
        self.assertEqual(starts, sorted(starts))

    def test_segments_keep_offsets_after_stripping_bold(self) -> None:
        text = "**粗体** 开头的一段。\n"
        segment = tts.segments_from_text(text)[0]
        self.assertEqual(segment.text, "粗体 开头的一段。")
        self.assertEqual(text[segment.start:segment.end], "**粗体** 开头的一段。")

    def test_long_line_splits_by_sentence(self) -> None:
        line = "这是一个很长的句子。" * 60
        segments = tts.segments_from_text(line + "\n", limit=40)
        self.assertGreater(len(segments), 3)
        self.assertTrue(all(len(seg.text) <= 40 for seg in segments))
        for seg in segments:
            self.assertTrue(line[seg.start:seg.end].endswith("。"))

    def test_segments_empty_text(self) -> None:
        self.assertEqual(tts.segments_from_text(""), [])
        self.assertEqual(tts.segments_from_text("\n\n \n"), [])


class NarratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[tuple[str, object]] = []
        FakeVoice.instances.clear()
        self.narrator = tts.Narrator(lambda kind, value: self.events.append((kind, value)),
                                     voice_factory=FakeVoice)
        self.voice = None

    def tearDown(self) -> None:
        self.narrator.close()

    def states(self) -> list[str]:
        return [value[0] for kind, value in self.events if kind == "tts_state"]

    def segments(self) -> list[tuple[int, int]]:
        return [value for kind, value in self.events if kind == "tts_seg"]

    def wait_voice(self) -> FakeVoice:
        self.assertTrue(wait_for(lambda: FakeVoice.instances), "朗读线程未创建语音对象")
        return FakeVoice.instances[0]

    def test_plays_all_segments_then_idle(self) -> None:
        self.assertTrue(self.narrator.play(["一段", "二段", "三段"], rate=3))
        self.assertTrue(wait_for(lambda: "idle" in self.states()))
        voice = self.wait_voice()
        self.assertEqual(voice.spoken, ["一段", "二段", "三段"])
        self.assertEqual(voice.rate, 3)
        self.assertEqual(self.segments(), [(0, 3), (1, 3), (2, 3)])
        self.assertEqual(self.states()[0], "playing")

    def test_play_from_given_segment(self) -> None:
        self.narrator.play(["一", "二", "三", "四"], start=2)
        self.assertTrue(wait_for(lambda: "idle" in self.states()))
        self.assertEqual(self.wait_voice().spoken, ["三", "四"])
        self.assertEqual(self.segments()[0], (2, 4))

    def test_pause_and_resume(self) -> None:
        self.narrator.play(["甲", "乙"])
        self.assertTrue(wait_for(lambda: "playing" in self.states()))
        voice = self.wait_voice()
        resumes = voice.resume_calls
        self.narrator.pause()
        self.assertTrue(wait_for(lambda: "paused" in self.states()))
        self.assertEqual(voice.pause_calls, 1)
        spoken = list(voice.spoken)
        time.sleep(0.4)
        self.assertEqual(voice.spoken, spoken, "暂停后不应继续推进下一段")
        self.assertNotIn("unavailable", self.states(), "暂停超过卡死阈值也不能被判成引擎故障")
        self.narrator.resume()
        self.assertTrue(wait_for(lambda: "idle" in self.states()))
        self.assertEqual(voice.resume_calls, resumes + 1)
        self.assertEqual(voice.spoken, ["甲", "乙"])

    def test_stop_ends_immediately(self) -> None:
        self.narrator.play(["甲", "乙", "丙"])
        self.assertTrue(wait_for(lambda: "playing" in self.states()))
        voice = self.wait_voice()
        stops = voice.stop_calls
        self.narrator.stop()
        self.assertTrue(wait_for(lambda: "idle" in self.states()))
        self.assertEqual(voice.stop_calls, stops + 1)
        count = len(voice.spoken)
        time.sleep(0.4)
        self.assertEqual(len(voice.spoken), count, "停止后不应再读新段")

    def test_play_after_pause_is_not_silent(self) -> None:
        """暂停过之后必须还能出声 —— 这是 1.0.5 修的那个「有时候没声音」。

        SAPI 的 Pause 是粘性的：暂停后直接 Speak，文本只排队不发声，
        RunningState 永远停在 0，线程永久卡在该段。这里把卡死阈值调到很大，
        逼着实现只能靠「开读前 Resume」过关，不能靠超时自愈蒙混。
        """
        original = tts.STALL_SECONDS
        tts.STALL_SECONDS = 3600.0
        try:
            self.narrator.play(["一", "二"])
            self.assertTrue(wait_for(lambda: "playing" in self.states()))
            voice = self.wait_voice()
            self.narrator.pause()
            self.assertTrue(wait_for(lambda: "paused" in self.states()))
            self.narrator.stop()
            self.assertTrue(wait_for(lambda: "idle" in self.states()))
            spoken, resumes = len(voice.spoken), voice.resume_calls
            self.narrator.play(["三"])
            self.assertTrue(wait_for(lambda: len(voice.spoken) > spoken),
                            "暂停之后点朗读全文，必须真的把文本送去合成")
            self.assertTrue(wait_for(lambda: self.states()[-1] == "idle"))
            self.assertEqual(voice.spoken[-1], "三")
            self.assertGreater(voice.resume_calls, resumes, "开读前必须解除粘性暂停")
        finally:
            tts.STALL_SECONDS = original

    def test_stuck_voice_is_reopened_once(self) -> None:
        """引擎入队后迟迟不发声（设备被抢占）时，重建语音对象重试一次。"""
        class StuckVoice(FakeVoice):
            def state(self) -> int:
                return tts.SRS_IDLE  # 永远停在「已入队但没开始发声」

        self.narrator._factory = StuckVoice
        original = tts.STALL_SECONDS
        tts.STALL_SECONDS = 0.3
        try:
            self.narrator.play(["甲"])
            self.assertTrue(wait_for(lambda: len(StuckVoice.instances) >= 2, timeout=8),
                            "卡死时必须重建语音对象重试")
            self.assertTrue(wait_for(lambda: StuckVoice.instances[0].close_calls >= 1))
        finally:
            tts.STALL_SECONDS = original

    def test_rate_change_reaches_voice(self) -> None:
        self.narrator.play(["甲"])
        voice = self.wait_voice()
        self.narrator.set_rate(-5)
        self.assertTrue(wait_for(lambda: voice.rate == -5))

    def test_empty_play_is_rejected(self) -> None:
        self.assertFalse(self.narrator.play([]))
        self.assertFalse(self.narrator.play(["  ", ""]))
        self.assertEqual(self.events, [])

    def test_missing_engine_reports_unavailable(self) -> None:
        narrator = tts.Narrator(lambda kind, value: self.events.append((kind, value)),
                                voice_factory=lambda rate, volume: None)
        self.addCleanup(narrator.close)
        narrator.play(["甲"])
        self.assertTrue(wait_for(lambda: "unavailable" in [v[0] for k, v in self.events if k == "tts_state"]))
        self.assertIn("语音合成引擎", dict(self.events)["tts_state"][1])


class SapiProbeTests(unittest.TestCase):
    def test_sapi_voice_can_be_created(self) -> None:
        """只验证引擎对象能建立，不发音（不调用 speak）。"""
        voice = tts.SapiVoice.create(rate=0, volume=0)
        if voice is None:
            self.skipTest("本机没有可用的 SAPI 语音引擎")
        try:
            self.assertTrue(voice.available)
        finally:
            voice.close()

    def test_members_resolve_by_name_not_by_vtable_index(self) -> None:
        """回归守卫：成员必须靠 GetIDsOfNames 解析。

        1.0.4 首版把 Speak 手算到 20 号槽（实为 GetRate），调用返回成功、
        但一声不响 —— 表现就是「点朗读全文没声音」。这里逐一确认名字可解析，
        名字解析不出来会抛 AttributeError 而不是静默错位。
        """
        voice = tts.SapiVoice.create(rate=0, volume=0)
        if voice is None:
            self.skipTest("本机没有可用的 SAPI 语音引擎")
        try:
            for name in tts._SPEAK_MEMBERS:
                self.assertIsInstance(voice._api.dispid(name), int, name)
            # 不存在的成员必须显式失败，不能悄悄返回一个野槽位
            with self.assertRaises(AttributeError):
                voice._api.dispid("SpeakNotExistingMember")
        finally:
            voice.close()

    def test_speak_actually_starts_synthesis(self) -> None:
        """回归守卫：Speak 必须真的让 SAPI 进入「正在说」状态。

        音量设 0，所以不出声，但合成时钟照走；若 Speak 被调到了别的槽位
        （如 GetRate），状态永远停在 1，读不到 2。
        """
        voice = tts.SapiVoice.create(rate=0, volume=0)
        if voice is None:
            self.skipTest("本机没有可用的 SAPI 语音引擎")
        try:
            voice.set_volume(0)
            voice.speak("这是一段用于验证朗读引擎真的启动了的较长文本，需要几秒钟才能读完。")
            states = set()
            end = time.time() + 4
            while time.time() < end:
                states.add(voice.state())
                if tts.SRS_SPEAKING in states:
                    break
                time.sleep(0.02)
            voice.stop()
            self.assertIn(tts.SRS_SPEAKING, states,
                          f"Speak 未启动合成，观察到的状态：{sorted(states)}")
        finally:
            voice.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
