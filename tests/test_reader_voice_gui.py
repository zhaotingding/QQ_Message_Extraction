"""摘要朗读界面的真实 Tk 回归：按钮状态、段落高亮与切文停读。

朗读调度用假对象替换，不发声、不创建 COM；真实引擎只由 test_tts 的探测用例覆盖。
"""
from __future__ import annotations

import queue
import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.service_process import ServiceProcess
from msgx.gui import App


class FakeNarrator:
    def __init__(self) -> None:
        self.plays: list[tuple[list[str], int, int | None]] = []
        self.paused = 0
        self.resumed = 0
        self.stops = 0
        self.rates: list[int] = []

    def play(self, segments, start=0, rate=None):
        self.plays.append((list(segments), start, rate))
        return True

    def pause(self) -> None:
        self.paused += 1

    def resume(self) -> None:
        self.resumed += 1

    def stop(self) -> None:
        self.stops += 1

    def set_rate(self, rate) -> None:
        self.rates.append(rate)

    def close(self) -> None:
        pass


class ReaderVoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="reader-voice-")
        self.db = Path(self.tmp.name) / "voice.db"
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE summaries (id INTEGER PRIMARY KEY,window_start INTEGER,window_end INTEGER,"
                         "kind TEXT,content TEXT,report_path TEXT,msg_count INTEGER,created_at INTEGER)")
        self.root = tk.Tk()
        self.root.withdraw()
        ttk.Style(self.root).theme_use("clam")
        self.app = App.__new__(App)
        app = self.app
        app.service = ServiceProcess()
        app.root = self.root
        app.cfg = {"app": {"db_path": str(self.db)}}
        app.events = queue.Queue()
        app.prefs = {}
        app.nb = ttk.Notebook(self.root)
        app.nb.pack(fill="both", expand=True)
        app._report_history_busy = False
        app._report_history_checked = time.time()
        app._report_history_seq = 0
        app._report_pending_path = ""
        app._report_items = {}
        app._report_content = ""
        app._report_identity = None
        app._report_outline = []
        app._summary_path = ""
        app.status = tk.StringVar()
        app._apply_summary_retry_jobs = lambda _jobs: None
        app._build_report_tab(app.nb)
        app._narrator = FakeNarrator()
        self.root.update()
        self.item = {"id": 1, "kind": "daily", "content": "# 测试日报\n## 第一主题\n第一段正文内容。\n\n第二段正文内容。\n",
                     "window_start": 1_790_000_000, "window_end": 1_790_086_400}
        app._show_report(self.item)
        self.root.update()

    def tearDown(self) -> None:
        self.root.destroy()
        self.tmp.cleanup()
        self.app = None
        self.root = None

    def test_buttons_disabled_before_any_report(self) -> None:
        app = self.app
        app._voice_segments = []
        app._apply_tts_state("idle")
        self.assertEqual(str(app.btn_speak_all.cget("state")), "disabled")
        self.assertEqual(str(app.btn_speak_pause.cget("state")), "disabled")
        self.assertEqual(str(app.btn_speak_stop.cget("state")), "disabled")

    def test_report_enables_read_all(self) -> None:
        app = self.app
        self.assertTrue(app._voice_segments)
        app._apply_tts_state("idle")
        self.assertEqual(str(app.btn_speak_all.cget("state")), "normal")
        self.assertEqual(str(app.btn_speak_pause.cget("state")), "disabled")

    def test_read_all_starts_from_first_segment(self) -> None:
        app = self.app
        app._narrate(0)
        plays = app._narrator.plays
        self.assertEqual(len(plays), 1)
        segments, start, _rate = plays[0]
        self.assertEqual(start, 0)
        self.assertEqual(segments, [seg.text for seg in app._voice_segments])
        self.assertTrue(segments[0].startswith("测试日报"))

    def test_read_from_selected_segment(self) -> None:
        app = self.app
        app._narrate(2)
        segments, start, _rate = app._narrator.plays[0]
        self.assertEqual(start, 2)
        self.assertEqual(len(segments), len(app._voice_segments))

    def test_highlight_marks_exact_paragraph(self) -> None:
        app = self.app
        app._apply_tts_state("playing")
        app._highlight_speaking(1, 3)
        ranges = app.report_text.tag_ranges("speaking")
        self.assertEqual(len(ranges), 2)
        marked = app.report_text.get(ranges[0], ranges[1])
        self.assertEqual(marked, app.report_text.get("1.0", "end-1c")[
            app._voice_segments[1].start:app._voice_segments[1].end])
        self.assertIn("2/3 段", app.v_speak_hint.get())

    def test_pause_switches_button_text(self) -> None:
        app = self.app
        app._apply_tts_state("playing")
        app._apply_tts_state("paused")
        self.assertEqual(str(app.btn_speak_pause.cget("text")), "继续")
        self.assertEqual(str(app.btn_speak_stop.cget("state")), "normal")
        app._toggle_narration()
        self.assertEqual(app._narrator.resumed, 1)

    def test_idle_clears_highlight(self) -> None:
        app = self.app
        app._apply_tts_state("playing")
        app._highlight_speaking(0, 2)
        self.assertTrue(app.report_text.tag_ranges("speaking"))
        app._apply_tts_state("idle")
        self.assertFalse(app.report_text.tag_ranges("speaking"))

    def test_unavailable_keeps_read_all_retryable_with_reason(self) -> None:
        """引擎不可用时暂停/停止置灰，但「朗读全文」要留着 —— 否则用户被锁死在失败态。"""
        app = self.app
        app._apply_tts_state("unavailable", "未检测到语音合成引擎")
        self.assertEqual(str(app.btn_speak_all.cget("state")), "normal", "允许重建引擎后重试")
        for button in (app.btn_speak_pause, app.btn_speak_stop):
            self.assertEqual(str(button.cget("state")), "disabled")
        self.assertEqual(app.v_speak_hint.get(), "未检测到语音合成引擎")

    def test_switching_report_stops_narration(self) -> None:
        app = self.app
        before = app._narrator.stops
        app._narrate(0)
        app._show_report({"id": 2, "kind": "daily", "content": "# 另一篇\n另一段正文。\n",
                          "window_start": 1_790_000_000, "window_end": 1_790_086_400})
        self.assertEqual(app._narrator.stops, before + 1)

    def test_rate_choice_saved_only_when_needed(self) -> None:
        app = self.app
        app.v_speak_rate.set("较快")
        app._speak_rate_changed()
        self.assertEqual(app._narrator.rates, [3])
        self.assertIn("较快", app.status.get())

    # ---- 双击段落朗读（1.0.4 首版漏测的路径）----

    def test_double_click_binding_installed(self) -> None:
        self.assertTrue(self.app.report_text.bind("<Double-Button-1>"),
                        "正文本必须绑定双击，否则「段落朗读」无从触发")

    def test_double_click_out_of_range_falls_back_to_nearest(self) -> None:
        """点在空白处也不能没反应：回退到最近的一段。"""
        app = self.app
        app._narrator.plays.clear()
        app._narrate_from_click(SimpleNamespace(x=99999, y=99999))
        self.assertEqual(len(app._narrator.plays), 1)
        _segments, start, _rate = app._narrator.plays[0]
        self.assertGreaterEqual(start, 0)
        self.assertLess(start, len(app._voice_segments))

    def test_double_click_on_third_paragraph_starts_there(self) -> None:
        app, root = self.app, self.root
        try:
            root.geometry("1000x760+40+40")
            root.attributes("-alpha", 0.0)  # 需要真实布局才有 bbox，但别闪用户眼睛
        except tk.TclError:
            self.skipTest("当前环境不支持透明窗口，跳过坐标换算用例")
        root.deiconify()
        root.update()
        target = app._voice_segments[2]
        box = app.report_text.bbox(f"1.0 + {target.start} chars")
        if not box:
            self.skipTest("正文尚未布局出可视区域")
        x, y, _w, h = box
        app._narrator.plays.clear()
        for _ in range(2):
            app.report_text.event_generate("<ButtonPress-1>", x=x + 2, y=y + h // 2)
            app.report_text.event_generate("<ButtonRelease-1>", x=x + 2, y=y + h // 2)
        root.update()
        self.assertEqual(len(app._narrator.plays), 1, "双击必须触发一次朗读")
        self.assertEqual(app._narrator.plays[0][1], 2, "必须从被双击的那一段开始")


if __name__ == "__main__":
    unittest.main(verbosity=2)
