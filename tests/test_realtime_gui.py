"""实时消息界面行为回归：仅合成消息和真实 Tk 控件，无配置/凭据/网络读取。"""
from __future__ import annotations

import json
import queue
import sys
import tkinter as tk
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from tkinter import ttk
from tkinter import font as tkfont
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.gui import App


BEIJING = timezone(timedelta(hours=8))
DAY = datetime(2026, 9, 30, tzinfo=BEIJING)


def sample_payload():
    """包含同名不同群、多图各自 OCR 和长正文的独立输入样本。"""
    start = int(DAY.timestamp())
    image = {
        "id": 201, "media_path": "synthetic-image-a.png", "media_state": "ready",
        "ocr_text": "第一张图片：控制实验条件后再比较结果。", "vision_text": "",
        "vision_state": "skipped", "include_vision": 0,
    }
    return {
        "items": [
            {"id": 3, "ts": start + 38000, "group_id": "1001", "group_name": "模型讨论群",
             "sender_name": "甲", "text": "今天比较两组实验结果。\n" + "先核对输入条件，再核对结果。\n" * 50,
             "media_state": "none", "received_by": "示例账号", "has_media": 0},
            {"id": 2, "ts": start + 37900, "group_id": "1002", "group_name": "同名交流群",
             "sender_name": "乙", "text": "[图片][图片]", "media_state": "filtered",
             "received_by": "示例账号", "has_media": 1,
             "images": [image, {**image, "id": 202, "media_path": "synthetic-image-b.png",
                                "ocr_text": "第二张图片：学习计划与记录。",
                                "vision_text": "一张学习进度表，各列对应日期和任务。",
                                "vision_state": "done", "include_vision": 1}]},
            {"id": 1, "ts": start + 37800, "group_id": "1003", "group_name": "同名交流群",
             "sender_name": "丙", "text": "这是第三个群的消息。", "has_media": 0,
             "media_state": "none", "received_by": "示例账号"},
        ],
        "activity": {
            "date": "2026-09-30", "window_start": start, "window_end": start + 86400,
            "groups": [
                {"group_id": "1002", "group_name": "同名交流群", "count": 358, "last_ts": start + 37900},
                {"group_id": "1001", "group_name": "模型讨论群", "count": 206, "last_ts": start + 38000},
                {"group_id": "1003", "group_name": "同名交流群", "count": 41, "last_ts": start + 37800},
            ],
        },
    }


class RealtimeGuiTests(unittest.TestCase):
    def setUp(self):
        guard = patch("msgx.gui.urllib.request.build_opener", side_effect=AssertionError("test must not access network"))
        guard.start()
        self.addCleanup(guard.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        ttk.Style(self.root).theme_use("clam")
        self.app = App.__new__(App)
        app = self.app
        app.root = self.root
        app.cfg = {"onebot": {"http_port": 1}}
        app.events = queue.Queue()
        app.proc = object()
        app.status = tk.StringVar(master=self.root)
        app.nb = ttk.Notebook(self.root)
        app.nb.pack(fill="both", expand=True)
        app._rt_busy = False
        app._rt_rows = {}
        app._rt_actions = set()
        app._build_realtime_tab(app.nb)
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def render(self, payload=None):
        self.app._accept_messages(self.app._rt_request_key(), self.app.proc,
                                  sample_payload() if payload is None else payload)
        self.root.update()

    def select(self, ident):
        self.app.rt.selection_set(ident)
        self.app._on_rt_select()
        with patch.object(self.app.rt, "identify_row", return_value=ident), \
                patch.object(self.app.rt, "identify_column", return_value="#4"):
            self.app._on_rt_click(SimpleNamespace(x=1, y=1))
        self.root.update()

    def detail(self):
        return self.app.rt_detail.get("1.0", "end-1c")

    def test_table_previews_each_images_ocr_without_semantics_or_extra_columns(self):
        self.render()
        app = self.app
        self.assertNotIn("ocr", app.rt.cget("columns"))
        self.assertNotIn("vision", app.rt.cget("columns"))
        self.assertEqual(app.rt.get_children(), ("3", "2:201", "2:202", "1"))
        self.assertIn("今天比较两组实验结果", app.rt.set("3", "text"))
        self.assertEqual(app.rt.set("2:201", "text"), "[图片] OCR:第一张图片：控制实验条件后再比较结果。")
        self.assertEqual(app.rt.set("2:202", "text"), "[图片] OCR:第二张图片：学习计划与记录。")
        self.assertNotIn("学习进度表", app.rt.set("2:202", "text"))
        self.assertEqual(app.rt.set("2:201", "image"), "打开")
        self.assertIn("识别", app.rt.set("2:201", "recognize"))

    def test_caption_and_saved_ocr_preview_are_single_line_and_do_not_change_source(self):
        payload = sample_payload()
        message = payload["items"][1]
        message["text"] = "看看这个\n实验结果[图片][图片]"
        message["images"][0]["ocr_text"] = "甲行\n乙行\t结论"
        before = json.dumps(payload, ensure_ascii=False)
        with patch.object(self.app, "_start_rt_action") as action:
            self.render(payload)
            self.assertEqual(self.app.rt.set("2:201", "text"), "看看这个 实验结果 [图片] OCR:甲行 乙行 结论")
            action.assert_not_called()
        self.assertEqual(json.dumps(payload, ensure_ascii=False), before)
        self.select("2:201")
        self.assertIn(message["text"], self.detail())
        self.assertIn("甲行\n乙行\t结论", self.detail())

    def test_long_image_preview_retains_ocr_and_full_detail(self):
        payload = sample_payload()
        message = payload["items"][1]
        message["text"] = "长正文" * 150 + "[图片]"
        message["images"][0]["ocr_text"] = "识别文字" * 150 + "末尾信息"
        self.render(payload)
        preview = self.app.rt.set("2:201", "text")
        self.assertIn("[图片] OCR:识别文字", preview)
        self.assertTrue(preview.endswith("…"))
        self.assertLessEqual(len(preview), 300)
        self.select("2:201")
        self.assertIn(message["text"], self.detail())
        self.assertIn(message["images"][0]["ocr_text"], self.detail())

    def test_image_without_ocr_keeps_placeholder_until_result_arrives(self):
        payload = sample_payload()
        message = payload["items"][1]
        message["text"] = "[图片]"
        message["images"] = message["images"][:1]
        message["images"][0]["ocr_text"] = " \n\t"
        self.render(payload)
        self.assertEqual(self.app.rt.set("2:201", "text"), "[图片]")
        self.select("2:201")
        message["images"][0]["ocr_text"] = "稍后完成的 OCR"
        self.render(payload)
        self.assertEqual(self.app.rt.selection(), ("2:201",))
        self.assertEqual(self.app.rt.set("2:201", "text"), "[图片] OCR:稍后完成的 OCR")
        self.assertIn("稍后完成的 OCR", self.detail())

    def test_shared_details_show_complete_text_and_each_image_recognition(self):
        self.render()
        self.select("3")
        self.assertIn(sample_payload()["items"][0]["text"].strip(), self.detail())
        self.assertEqual(self.app.rt_bottom.select(), str(self.app.rt_details_tab))
        self.select("2:201")
        self.assertIn("[图片][图片]", self.detail())
        self.assertIn("第一张图片：控制实验条件", self.detail())
        self.assertNotIn("第二张图片：学习计划", self.detail())
        self.select("2:202")
        self.assertIn("第二张图片：学习计划", self.detail())
        self.assertIn("一张学习进度表", self.detail())
        self.assertNotIn("第一张图片：控制实验条件", self.detail())

    def test_default_activity_counts_are_independent_of_visible_message_limit(self):
        self.app.v_rt_limit.set("1")
        payload = sample_payload()
        payload["items"] = payload["items"][:1]
        self.render(payload)
        self.assertEqual(self.app.rt_bottom.select(), str(self.app.rt_activity_tab))
        values = [self.app.rt_activity.item(row, "values") for row in self.app.rt_activity.get_children()]
        self.assertEqual(len(values), 3)
        self.assertIn("358", values[0])
        self.assertIn("206", values[1])
        self.assertIn("41", values[2])
        self.assertIn("未过滤", self.app.v_rt_activity_status.get())

    def test_refresh_preserves_selected_message_details_scroll_and_activity_tab(self):
        self.root.deiconify()
        self.root.geometry("1040x650")
        self.render()
        self.select("3")
        self.app.rt_detail.yview_moveto(.65)
        self.root.update()
        before = self.app.rt_detail.yview()
        self.assertGreater(before[0], 0)
        self.render()
        self.assertEqual(self.app.rt.selection(), ("3",))
        self.assertEqual(self.app.rt_bottom.select(), str(self.app.rt_details_tab))
        self.assertAlmostEqual(self.app.rt_detail.yview()[0], before[0], places=5)
        self.app.rt_bottom.select(self.app.rt_activity_tab)
        self.render()
        self.assertEqual(self.app.rt_bottom.select(), str(self.app.rt_activity_tab))

    def test_empty_day_and_zero_messages_do_not_leave_old_details(self):
        self.render()
        self.select("3")
        payload = sample_payload()
        payload["items"] = []
        payload["activity"]["groups"] = []
        self.render(payload)
        self.assertEqual(self.app.rt.get_children(), ())
        self.assertEqual(self.app.rt_activity.get_children(), ())
        self.assertNotIn("今天比较两组实验结果", self.detail())

    def test_read_failure_does_not_falsely_replace_activity_with_zero(self):
        self.render()
        before = [self.app.rt_activity.item(row, "values") for row in self.app.rt_activity.get_children()]
        self.app._render_messages(None)
        self.root.update()
        after = [self.app.rt_activity.item(row, "values") for row in self.app.rt_activity.get_children()]
        self.assertEqual(after, before)
        self.assertIn("失败", self.app.v_rt_status.get())

    def test_picture_actions_keep_image_identity_after_column_removal(self):
        self.render()
        app = self.app
        columns = list(app.rt.cget("columns"))
        event = SimpleNamespace(x=1, y=1)
        with patch.object(app.rt, "identify_row", return_value="2:201"), \
                patch.object(app.rt, "identify_column", return_value=f"#{columns.index('recognize') + 1}"), \
                patch.object(app, "_start_rt_action") as action:
            app._on_rt_click(event)
            action.assert_called_once_with("201", "vision", {})
        with patch.object(app.rt, "identify_row", return_value="2:202"), \
                patch.object(app.rt, "identify_column", return_value=f"#{columns.index('include') + 1}"), \
                patch.object(app, "_start_rt_action") as action:
            app._on_rt_click(event)
            action.assert_called_once_with("202", "summary-include", {"include": False})

    def test_same_name_groups_and_picture_filter_are_combined_by_group_id(self):
        self.render()
        app = self.app
        labels = list(app.cb_rt_group.cget("values"))
        group_a = next(label for label in labels if "1002" in label)
        group_b = next(label for label in labels if "1003" in label)
        self.assertNotEqual(group_a, group_b)
        with patch.object(app, "refresh_messages") as refresh:
            app.v_rt_group.set(group_a)
            app._on_rt_group_changed()
            refresh.assert_called_once()
        self.assertEqual(app._rt_request_key(), (200, False, "1002"))
        self.assertEqual(app.rt.get_children(), ())
        payload = sample_payload()
        payload["items"] = [row for row in payload["items"] if row["group_id"] == "1002"]
        self.render(payload)
        self.assertEqual(app.v_rt_group.get(), group_a)
        self.assertEqual(app.rt.get_children(), ("2:201", "2:202"))
        with patch.object(app, "refresh_messages"):
            app.v_rt_filter.set("仅看图片")
            app._on_rt_filter_changed()
        self.assertEqual(app._rt_request_key(), (200, True, "1002"))
        with patch.object(app, "refresh_messages"):
            app.rt_activity.selection_set("1003")
            app._on_rt_activity_click()
        self.assertEqual(app._rt_request_key(), (200, True, "1003"))
        self.assertEqual(app.v_rt_group.get(), group_b)
        with patch.object(app, "refresh_messages"):
            app.v_rt_group.set("全部群")
            app._on_rt_group_changed()
        self.assertEqual(app._rt_request_key(), (200, True, ""))

    def test_stale_filter_or_process_result_never_replaces_current_rows(self):
        self.render()
        app = self.app
        key = app._rt_request_key()
        old_proc = app.proc
        payload = sample_payload()
        payload["items"] = [payload["items"][1]]
        with patch.object(app, "refresh_messages") as refresh:
            app.v_rt_group.set(next(label for label in app._rt_group_labels if "1002" in label))
            app._on_rt_group_changed()
            refresh.reset_mock()
            app._accept_messages(key, old_proc, sample_payload())
            self.assertEqual(app.rt.get_children(), ())
            refresh.assert_called_once()
        self.render(payload)
        current_rows = app.rt.get_children()
        app.proc = object()
        with patch.object(app, "refresh_messages") as refresh:
            app._accept_messages(app._rt_request_key(), old_proc, sample_payload())
            refresh.assert_called_once()
        self.assertEqual(app.rt.get_children(), current_rows)

    def test_fetch_sends_group_and_image_filters_in_one_request(self):
        app = self.app
        response = Mock()
        response.read.return_value = json.dumps(sample_payload()).encode("utf-8")
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = context
        with patch("msgx.gui.urllib.request.build_opener", return_value=opener):
            app._fetch_messages(13, True, "1002", app.proc)
        url = opener.open.call_args.args[0]
        self.assertEqual(parse_qs(urlsplit(url).query), {"limit": ["13"], "images": ["1"], "group_id": ["1002"]})
        kind, (key, proc, payload) = app.events.get_nowait()
        self.assertEqual(kind, "messages")
        self.assertEqual(key, (13, True, "1002"))
        self.assertIs(proc, app.proc)
        self.assertEqual(payload, sample_payload())

    def test_small_window_keeps_readable_body_width_and_complete_time_column(self):
        self.root.deiconify()
        self.root.geometry("820x560")
        self.render()
        app = self.app
        self.assertGreaterEqual(app.rt.column("text", "width"), 180)
        font = tkfont.Font(root=self.root, font=ttk.Style(self.root).lookup("Treeview", "font"))
        self.assertGreaterEqual(app.rt.column("time", "width"), font.measure("09-30 10:33:20") + 8)
        self.assertGreaterEqual(app.rt_bottom.winfo_height(), 140)
        self.assertGreaterEqual(app.cb_rt_group.winfo_width(), 100)
        self.assertLess(app.rt.xview()[1], 1)
        app.rt.xview_moveto(1)
        self.root.update()
        for column in ("image", "recognize", "include", "account"):
            x, _, width, _ = app.rt.bbox("3", column)
            self.assertGreaterEqual(x, 0)
            self.assertLessEqual(x + width, app.rt.winfo_width())


if __name__ == "__main__":
    unittest.main(verbosity=2)
