"""摘要阅读批注的真实 Tk 控件回归；仅使用临时数据库，无网络或模型调用。"""
from __future__ import annotations

import queue
import sqlite3
import sys
import tempfile
import time
import tkinter as tk
import unittest
from datetime import datetime
from contextlib import closing
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.gui import App, FONT
from msgx import summary_review
from msgx.summarize import BEIJING


class ReaderReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="reader-review-")
        self.db = Path(self.tmp.name) / "reader.db"
        self.root = tk.Tk()
        self.root.withdraw()
        ttk.Style(self.root).theme_use("clam")
        self.app = App.__new__(App)
        app = self.app
        app.root = self.root
        app.cfg = {"app": {"db_path": str(self.db)}}
        app.events = queue.Queue()
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
        app._build_report_tab(app.nb)
        self.root.update()
        self.content = "# 测试日报\n## 第一主题\n甲段🙂相同词，乙段相同词。\n\n## 第二主题\n" + "正文内容。\n" * 60
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE summaries (id INTEGER PRIMARY KEY,window_start INTEGER,window_end INTEGER,"
                         "kind TEXT,content TEXT,report_path TEXT,msg_count INTEGER,created_at INTEGER)")
            self.add_summary(conn, 1, "daily", self.content, 100)
            self.add_summary(conn, 2, "multi", "# 多日结果\n## 主题\n汇总正文。", 101)

    def tearDown(self):
        self.root.destroy()
        self.tmp.cleanup()

    def add_summary(self, conn, ident, kind, content, created):
        start = int(datetime(2026, 9, 25, tzinfo=BEIJING).timestamp())
        end = int(datetime(2026, 9, 26 if kind.startswith("daily") else 28, tzinfo=BEIJING).timestamp())
        conn.execute("INSERT INTO summaries VALUES (?,?,?,?,?,?,?,?)",
                     (ident, start, end, kind, content, "", 3, created))

    def refresh(self):
        self.app.refresh_report_history()
        deadline = time.time() + 5
        while self.app._report_history_busy and time.time() < deadline:
            kind, value = self.app.events.get(timeout=5)
            self.assertEqual(kind, "report_history")
            self.app._render_report_history(*value)
            self.root.update()
        self.assertFalse(self.app._report_history_busy)

    def show_daily(self):
        self.refresh()
        self.app.report_history.selection_set("1")
        self.app._report_selected()
        self.root.update()

    def review(self, ident=1, content=None):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            return summary_review.get_review(conn, ident, content if content is not None else self.content)

    def select_text(self, text, start="1.0"):
        widget = self.app.report_text
        index = widget.search(text, start, stopindex="end")
        self.assertTrue(index)
        length = len(text)
        end = widget.index(f"{index} + {length} chars")
        widget.tag_remove("sel", "1.0", "end")
        widget.tag_add("sel", index, end)
        self.assertEqual(widget.get(index, end), text)
        return index, end

    def test_filter_before_limit_and_no_read_side_effects(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for ident in range(10, 75):
                self.add_summary(conn, ident, "periodic", "旧版内容", ident + 200)
        before = self.db.read_bytes()
        self.refresh()
        self.assertEqual(self.app.report_history.get_children(), ("2", "1"))
        self.assertIn("【日报】09-25", self.app.report_history.item("1", "text"))
        self.assertIn("【汇总】09-25—09-27", self.app.report_history.item("2", "text"))
        self.assertEqual(self.app.report_history.item("1", "tags"), ("unread",))
        self.assertEqual(self.db.read_bytes(), before)
        self.app.v_report_filter.set("仅日报")
        self.app._report_filter_changed()
        self.refresh()
        self.assertEqual(self.app.report_history.get_children(), ("1",))
        self.assertEqual(self.app._report_current["id"], 1)
        self.app.v_report_filter.set("仅汇总")
        self.app._report_filter_changed()
        self.refresh()
        self.assertEqual(self.app.report_history.get_children(), ("2",))

    def test_read_state_persists_and_opening_does_not_mark_read(self):
        self.show_daily()
        self.app.v_report_read_state.set("skimmed")
        self.app._set_report_read_state()
        self.assertEqual(self.review()["read_state"], "skimmed")
        self.assertEqual(self.app.report_history.item("1", "tags"), ("skimmed",))
        self.app.v_report_read_state.set("deep")
        self.app._set_report_read_state()
        self.assertEqual(self.app.report_history.item("1", "values"), ("深读",))
        self.app._show_report(self.app._report_items["2"])
        self.app._show_report(self.app._report_items["1"])
        self.assertEqual(self.app.v_report_read_state.get(), "deep")
        self.assertEqual(self.review(2, "# 多日结果\n## 主题\n汇总正文。")["read_state"], "unread")
        self.app.v_report_read_state.set("unread")
        self.app._set_report_read_state()
        self.assertEqual(self.review()["read_state"], "unread")

    def test_annotations_keep_exact_display_text_and_split_overlap(self):
        self.show_daily()
        self.select_text("甲段🙂相同词，乙段相同词。")
        self.app._mark_report("blue")
        self.assertEqual(self.review()["annotations"][0]["text"], "甲段🙂相同词，乙段相同词。")
        self.select_text("乙段相同词")
        self.app._mark_report("red")
        self.app._mark_report("underline")
        marks = self.review()["annotations"]
        self.assertEqual([m["text"] for m in marks if m["level"] == "red"], ["乙段相同词"])
        self.assertTrue(any(m["level"] == "blue" and "甲段🙂" in m["text"] for m in marks))
        self.assertTrue(any(m["level"] == "underline" for m in marks))
        self.app._show_report(self.app._report_items["2"])
        self.app._show_report(self.app._report_items["1"])
        self.assertTrue(self.app.report_text.tag_ranges("review_red"))
        self.assertTrue(self.app.report_text.tag_ranges("review_underline"))
        self.select_text("乙段相同词")
        self.app._mark_report("")
        self.assertTrue(all(m["level"] == "blue" for m in self.review()["annotations"]))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT content FROM summaries WHERE id=1").fetchone()[0], self.content)

    def test_notes_auto_save_on_switch_and_refresh_preserves_scroll(self):
        self.show_daily()
        self.app.report_notes.insert("1.0", "这是我的观点，不是聊天事实。")
        self.app.report_outline.selection_set(1)
        self.app._report_jump()
        scroll = self.app.report_text.yview()
        self.refresh()
        self.assertEqual(self.app.report_text.yview(), scroll)
        self.assertEqual(self.review()["notes"], "这是我的观点，不是聊天事实。")
        self.assertEqual(self.app.report_outline.curselection(), (1,))
        self.app.report_notes.insert("end", "继续思考。")
        self.app._show_report(self.app._report_items["2"])
        self.assertTrue(self.review()["notes"].endswith("继续思考。"))
        self.assertFalse(self.app.report_notes_frame.winfo_manager())
        self.app._show_report(self.app._report_items["1"])
        self.assertEqual(self.app.report_notes.get("1.0", "end-1c"), self.review()["notes"])

    def test_save_failure_keeps_current_draft_and_rolls_back_state(self):
        self.show_daily()
        self.app.report_notes.insert("1.0", "未保存心得")
        with patch("msgx.gui.summary_review.save_review", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertFalse(self.app._show_report(self.app._report_items["2"]))
            self.assertEqual(self.app._report_current["id"], 1)
            self.assertEqual(self.app.report_notes.get("1.0", "end-1c"), "未保存心得")
            self.app.v_report_read_state.set("deep")
            self.app._set_report_read_state()
            self.assertEqual(self.app.v_report_read_state.get(), "unread")
            self.select_text("甲段")
            self.app._mark_report("red")
            self.assertFalse(self.app.report_text.tag_ranges("review_red"))
        self.assertTrue(self.app._save_report_notes())

    def test_new_content_does_not_inherit_old_annotations(self):
        self.show_daily()
        self.select_text("甲段")
        self.app._mark_report("red")
        self.app.v_report_read_state.set("deep")
        self.app._set_report_read_state()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE summaries SET content=? WHERE id=1", ("# 新的日报\n完全不同。",))
        self.app.v_report_read_state.set("skimmed")
        self.app._set_report_read_state()
        self.assertIn("正文已变化", self.app.v_report_review.get())
        self.refresh()
        self.assertEqual(self.app.v_report_read_state.get(), "unread")
        self.assertFalse(self.app.report_text.tag_ranges("review_red"))
        self.assertEqual(self.review()["read_state"], "deep")  # 旧版本元数据仍在。

    def test_partial_date_labels_and_empty_filter(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.add_summary(conn, 3, "daily_partial", "# 今天", 103)
        self.refresh()
        self.assertIn("截至 09-26 00:00，未完成", self.app.report_history.item("3", "text"))
        self.assertIn("【汇总】09-25—09-27", self.app.report_history.item("2", "text"))
        self.assertIn("2026-09-25 00:00 — 2026-09-28 00:00", self.app.report_history.item("detail-2", "text"))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM summaries WHERE kind='multi'")
        self.app.v_report_filter.set("仅汇总")
        self.app._report_filter_changed()
        self.refresh()
        self.assertFalse(self.app.report_history.get_children())
        self.assertEqual(self.app.report_text.get("1.0", "end-1c"), "")

    def test_stale_history_cannot_replace_newly_generated_report(self):
        self.show_daily()
        old_items = list(self.app._report_items.values())
        self.app._report_history_busy = True
        self.app._show_report({"path": str(Path(self.tmp.name) / "new.html"), "content": "# 刚生成"})
        self.app.refresh_report_history(preferred_path=str(Path(self.tmp.name) / "new.html"))
        self.app._render_report_history(self.app._report_history_seq, old_items, "")
        self.assertIn("刚生成", self.app.report_text.get("1.0", "end"))
        self.refresh()

    def test_history_snapshot_cannot_undo_new_read_state_or_notes(self):
        self.show_daily()
        stale_items = [dict(item) for item in self.app._report_items.values()]
        self.app.v_report_read_state.set("deep")
        self.app._set_report_read_state()
        self.app.report_notes.insert("1.0", "后台读取期间的新心得")
        self.app._render_report_history(self.app._report_history_seq, stale_items, "")
        self.assertEqual(self.app.report_history.item("1", "tags"), ("deep",))
        self.assertEqual(self.app.v_report_read_state.get(), "deep")
        self.assertEqual(self.app.report_notes.get("1.0", "end-1c"), "后台读取期间的新心得")
        self.assertEqual(self.review()["notes"], "后台读取期间的新心得")

    def test_minimum_layout_keeps_notes_outline_and_reading_column_visible(self):
        self.show_daily()
        self.app.v_report_path.set("D:\\Dev\\MCP\\消息萃取-MCP\\data\\reports\\20260928\\日报摘要_2026-09-27_1790352065298671900.html")
        style = ttk.Style(self.root)
        style.configure("TButton", padding=(12, 7), font=(FONT, 10))
        style.configure("TLabel", font=(FONT, 10))
        style.configure("Hint.TLabel", font=(FONT, 9))
        # 预留主界面的标题和底部状态区域，与完整界面最小窗口相同。
        ttk.Frame(self.root, height=108).pack(side="top", fill="x", before=self.app.nb)
        ttk.Frame(self.root, height=20).pack(side="bottom", fill="x", before=self.app.nb)
        self.root.geometry("820x560")
        self.root.deiconify()
        self.root.update()
        self.assertTrue(self.app.report_notes.winfo_ismapped())
        self.assertGreaterEqual(self.app.report_text.winfo_height(), 80)
        self.assertGreaterEqual(self.app.report_notes.winfo_height(), 35)
        self.assertTrue(self.app.report_outline.winfo_ismapped())
        tree = self.app.report_history
        self.assertLessEqual(tree.column("#0", "width") + tree.column("reading", "width"), tree.winfo_width())
        self.root.withdraw()


if __name__ == "__main__":
    unittest.main()
