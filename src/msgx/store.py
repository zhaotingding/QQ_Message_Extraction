from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .models import Msg

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
  id         TEXT PRIMARY KEY,
  name       TEXT DEFAULT '',
  added_at   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id    TEXT NOT NULL,
  msg_id      TEXT NOT NULL,
  sender_id   TEXT DEFAULT '',
  sender_name TEXT DEFAULT '',
  ts          INTEGER NOT NULL,
  msg_type    TEXT DEFAULT 'text',
  text        TEXT DEFAULT '',
  has_media   INTEGER DEFAULT 0,
  image_urls  TEXT DEFAULT '[]',
  media_path  TEXT DEFAULT '',
  media_hash  TEXT DEFAULT '',
  ocr_text    TEXT DEFAULT '',
  vision_text TEXT DEFAULT '',
  media_state TEXT DEFAULT 'none',
  at_me       INTEGER DEFAULT 0,
  reply_to    TEXT DEFAULT '',
  raw         TEXT DEFAULT '',
  UNIQUE(group_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_group_ts ON messages(group_id, ts);

CREATE TABLE IF NOT EXISTS summaries (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id      TEXT NOT NULL,
  window_start  INTEGER NOT NULL,
  window_end    INTEGER NOT NULL,
  msg_count     INTEGER DEFAULT 0,
  kind          TEXT DEFAULT 'periodic',
  content       TEXT DEFAULT '',
  report_path   TEXT DEFAULT '',
  created_at    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sum_group ON summaries(group_id, window_end);

CREATE TABLE IF NOT EXISTS alerts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id   TEXT NOT NULL,
  msg_id     TEXT DEFAULT '',
  level      TEXT NOT NULL,
  score      REAL DEFAULT 0,
  reason     TEXT DEFAULT '',
  excerpt    TEXT DEFAULT '',
  created_at INTEGER DEFAULT 0,
  notified   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alert_level ON alerts(level, created_at);

CREATE TABLE IF NOT EXISTS usage (
  day        TEXT PRIMARY KEY,
  calls      INTEGER DEFAULT 0,
  in_tokens  INTEGER DEFAULT 0,
  out_tokens INTEGER DEFAULT 0,
  cost_usd   REAL DEFAULT 0
);
"""


class Store:
    def __init__(self, db_path: str | Path):
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(p), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------- groups ----------

    def upsert_group(self, group_id: str, name: str = "") -> None:
        self.conn.execute(
            "INSERT INTO groups(id, name, added_at) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE groups.name END",
            (group_id, name, int(time.time())),
        )
        self.conn.commit()

    def group_names(self) -> dict:
        return {r["id"]: r["name"] for r in self.conn.execute("SELECT id, name FROM groups")}

    # ---------- messages ----------

    def insert_message(self, m: Msg, group_name: str = "") -> int | None:
        """返回自增 id；重复消息返回 None。"""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO messages"
            "(group_id, msg_id, sender_id, sender_name, ts, msg_type, text,"
            " has_media, image_urls, media_state, at_me, reply_to, raw)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                m.group_id,
                m.msg_id,
                m.sender_id,
                m.sender_name,
                m.ts,
                m.msg_type,
                m.text,
                1 if (m.image_urls or m.file_names) else 0,
                json.dumps(m.image_urls, ensure_ascii=False),
                "pending" if m.image_urls else "none",
                1 if m.at_me else 0,
                m.reply_to,
                json.dumps(m.raw, ensure_ascii=False)[:20000],
            ),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        # 无条件注册群：群名可能暂时为空（待配置补全），但群必须出现在 groups 表里，
        # 否则摘要调度遍历 group_names() 时看不到这个群。upsert_group 内部已保护空名不覆盖已有名字。
        self.upsert_group(m.group_id, group_name)
        return int(cur.lastrowid)

    def set_media_result(
        self,
        row_id: int,
        *,
        ocr_text: str = "",
        vision_text: str = "",
        media_path: str = "",
        media_hash: str = "",
        state: str = "done",
    ) -> None:
        self.conn.execute(
            "UPDATE messages SET ocr_text=?, vision_text=?, media_path=?, media_hash=?, media_state=?"
            " WHERE id=?",
            (ocr_text, vision_text, media_path, media_hash, state, row_id),
        )
        self.conn.commit()

    def get_message(self, row_id: int):
        return self.conn.execute("SELECT * FROM messages WHERE id=?", (row_id,)).fetchone()

    def find_by_hash(self, media_hash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT ocr_text, vision_text, media_path FROM messages"
            " WHERE media_hash=? AND media_state='done' LIMIT 1",
            (media_hash,),
        ).fetchone()

    def pending_media(self, limit: int = 200) -> list:
        return list(
            self.conn.execute(
                "SELECT * FROM messages WHERE media_state='pending' ORDER BY ts ASC LIMIT ?",
                (limit,),
            )
        )

    def messages_between(self, group_id: str, start: int, end: int) -> list:
        return list(
            self.conn.execute(
                "SELECT * FROM messages WHERE group_id=? AND ts>? AND ts<=? ORDER BY ts ASC",
                (group_id, start, end),
            )
        )

    def window_end(self, group_id: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(window_end) AS e FROM summaries WHERE group_id=?", (group_id,)
        ).fetchone()
        return int(row["e"]) if row and row["e"] else 0

    def first_ts(self, group_id: str) -> int:
        row = self.conn.execute(
            "SELECT MIN(ts) AS t FROM messages WHERE group_id=?", (group_id,)
        ).fetchone()
        return int(row["t"]) if row and row["t"] else 0

    # ---------- 跨群（热点统计用） ----------

    def first_ts_all(self) -> int:
        row = self.conn.execute("SELECT MIN(ts) AS t FROM messages").fetchone()
        return int(row["t"]) if row and row["t"] else 0

    def window_end_all(self) -> int:
        """跨群热点视角的窗口上界：最近一份热点报告覆盖到的时间点。"""
        row = self.conn.execute(
            "SELECT MAX(window_end) AS e FROM summaries WHERE kind='hotspot'"
        ).fetchone()
        return int(row["e"]) if row and row["e"] else 0

    def messages_between_all(self, start: int, end: int, limit: int = 8000) -> list:
        """窗口内全部群的消息，按时间升序。供跨群热点统计使用。"""
        return list(
            self.conn.execute(
                "SELECT * FROM messages WHERE ts>? AND ts<=? ORDER BY ts ASC LIMIT ?",
                (start, end, limit),
            )
        )

    # ---------- summaries ----------

    def add_summary(
        self,
        group_id: str,
        window_start: int,
        window_end: int,
        msg_count: int,
        content: str,
        kind: str = "periodic",
        report_path: str = "",
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO summaries(group_id, window_start, window_end, msg_count, kind,"
            " content, report_path, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (group_id, window_start, window_end, msg_count, kind, content, report_path, int(time.time())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_summaries(self, limit: int = 200) -> list:
        return list(
            self.conn.execute("SELECT * FROM summaries ORDER BY window_end DESC LIMIT ?", (limit,))
        )

    # ---------- alerts ----------

    def add_alert(self, group_id: str, msg_id: str, level: str, score: float, reason: str, excerpt: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO alerts(group_id, msg_id, level, score, reason, excerpt, created_at, notified)"
            " VALUES(?,?,?,?,?,?,?,0)",
            (group_id, msg_id, level, score, reason, excerpt, int(time.time())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_notified(self, alert_id: int) -> None:
        self.conn.execute("UPDATE alerts SET notified=1 WHERE id=?", (alert_id,))
        self.conn.commit()

    def recent_alerts(self, limit: int = 100, level: str | None = None) -> list:
        if level:
            return list(
                self.conn.execute(
                    "SELECT * FROM alerts WHERE level=? ORDER BY created_at DESC LIMIT ?",
                    (level, limit),
                )
            )
        return list(self.conn.execute("SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)))

    # ---------- usage ----------

    def add_usage(self, day: str, in_tokens: int, out_tokens: int, cost_usd: float) -> None:
        self.conn.execute(
            "INSERT INTO usage(day, calls, in_tokens, out_tokens, cost_usd) VALUES(?,1,?,?,?)"
            " ON CONFLICT(day) DO UPDATE SET calls=calls+1,"
            " in_tokens=in_tokens+excluded.in_tokens,"
            " out_tokens=out_tokens+excluded.out_tokens,"
            " cost_usd=cost_usd+excluded.cost_usd",
            (day, in_tokens, out_tokens, cost_usd),
        )
        self.conn.commit()

    def daily_cost_usd(self, day: str) -> float:
        row = self.conn.execute("SELECT cost_usd FROM usage WHERE day=?", (day,)).fetchone()
        return float(row["cost_usd"]) if row else 0.0

    def usage_all(self) -> list:
        return list(self.conn.execute("SELECT * FROM usage ORDER BY day DESC"))

    # ---------- maintenance ----------

    def prune(self, keep_days_raw: int, keep_days_media: int) -> dict:
        now = int(time.time())
        raw_cut = now - keep_days_raw * 86400
        c1 = self.conn.execute("DELETE FROM messages WHERE ts < ? AND media_state<>'pending'", (raw_cut,))
        media_cut = now - keep_days_media * 86400
        rows = list(
            self.conn.execute(
                "SELECT id, media_path FROM messages WHERE ts < ? AND media_path<>''", (media_cut,)
            )
        )
        for r in rows:
            Path(r["media_path"]).unlink(missing_ok=True)
            self.conn.execute("UPDATE messages SET media_path='' WHERE id=?", (r["id"],))
        self.conn.commit()
        return {"messages_deleted": c1.rowcount, "media_files_deleted": len(rows)}

    def stats(self) -> dict:
        q = lambda sql, *a: self.conn.execute(sql, a).fetchone()[0]  # noqa: E731
        return {
            "groups": q("SELECT COUNT(*) FROM groups"),
            "messages": q("SELECT COUNT(*) FROM messages"),
            "messages_pending_media": q("SELECT COUNT(*) FROM messages WHERE media_state='pending'"),
            "summaries": q("SELECT COUNT(*) FROM summaries"),
            "alerts": q("SELECT COUNT(*) FROM alerts"),
            "alerts_p0": q("SELECT COUNT(*) FROM alerts WHERE level='P0'"),
        }
