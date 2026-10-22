"""摘要、周期任务及后台响应记录；沿用 Store 的唯一连接，不另存任务材料。"""
from __future__ import annotations

import json
import sqlite3
import time


class SummaryStore:
    def add_summary(
        self,
        group_id: str,
        window_start: int,
        window_end: int,
        msg_count: int,
        content: str,
        kind: str = "periodic",
        report_path: str = "",
        source_sig: str = "",
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO summaries(group_id, window_start, window_end, msg_count, kind,"
            " content, report_path, source_sig, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (group_id, window_start, window_end, msg_count, kind, content, report_path,
             source_sig, int(time.time())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_summaries(self, limit: int = 200) -> list:
        return list(
            self.conn.execute("SELECT * FROM summaries ORDER BY window_end DESC LIMIT ?", (limit,))
        )

    def report_history(self, kinds: tuple | list, limit: int = 50) -> list[dict]:
        """阅读页按覆盖窗口读取成稿及批注；不迁移数据库、不重新生成摘要。"""
        from .summary_review import get_review

        if not kinds:
            return []
        items = [dict(row) for row in self.conn.execute(
            "SELECT id,window_start,window_end,kind,content,report_path,msg_count,created_at"
            f" FROM summaries WHERE kind IN ({','.join('?' for _ in kinds)})"
            " ORDER BY window_end DESC,window_start DESC,created_at DESC,id DESC LIMIT ?",
            (*kinds, limit))]
        for item in items:
            item["review"] = get_review(self.conn, item["id"], item["content"] or "")
        return items

    def summary_for_window(self, kind: str, start: int, end: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM summaries WHERE kind=? AND window_start=? AND window_end=?"
            " ORDER BY id DESC LIMIT 1", (kind, start, end),
        ).fetchone()

    def latest_summary_kind(self, kind: str, start: int | None = None) -> sqlite3.Row | None:
        if start is None:
            return self.conn.execute(
                "SELECT * FROM summaries WHERE kind=? ORDER BY window_start DESC,id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM summaries WHERE kind=? AND window_start=? ORDER BY id DESC LIMIT 1",
            (kind, start),
        ).fetchone()

    def auto_daily_start(self) -> str | None:
        row = self.conn.execute("SELECT start_date FROM daily_schedule WHERE id=1").fetchone()
        return str(row[0]) if row else None

    def set_auto_daily_start(self, day: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO daily_schedule(id,start_date) VALUES(1,?)", (day,))
        self.conn.commit()

    def auto_period_start(self, kind: str) -> str | None:
        row = self.conn.execute("SELECT start_date FROM summary_schedule_baselines WHERE kind=?", (kind,)).fetchone()
        return str(row[0]) if row else None

    def set_auto_period_start(self, kind: str, day: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO summary_schedule_baselines(kind,start_date) VALUES(?,?)", (kind, day))
        self.conn.commit()

    def summary_job(self, kind: str, start: str, end: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM summary_jobs WHERE kind=? AND start_date=? AND end_date=?",
                                (kind, start, end)).fetchone()
        return dict(row) if row else None

    def save_summary_job(self, kind: str, start: str, end: str, state: str, *,
                         now: float, attempts: int | None = None, next_attempt_at: float = 0,
                         error: str = "", summary_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO summary_jobs(kind,start_date,end_date,state,attempts,next_attempt_at,error,summary_id,updated_at)"
            " VALUES(?,?,?,?,COALESCE(?,0),?,?,?,?) ON CONFLICT(kind,start_date,end_date) DO UPDATE SET"
            " state=excluded.state,attempts=COALESCE(?,summary_jobs.attempts),next_attempt_at=excluded.next_attempt_at,"
            " error=excluded.error,summary_id=excluded.summary_id,updated_at=excluded.updated_at",
            (kind, start, end, state, attempts, next_attempt_at, error, summary_id, now, attempts))
        self.conn.commit()

    def claim_summary_job(self, kind: str, start: str, end: str, now: float, *, resume: bool = False) -> bool:
        cur = self.conn.execute(
            "UPDATE summary_jobs SET state='running',attempts=attempts+?,updated_at=?"
            " WHERE kind=? AND start_date=? AND end_date=?"
            " AND state IN ('pending','retry','waiting') AND (attempts<3 OR ?=1) AND next_attempt_at<=?",
            (int(not resume), now, kind, start, end, int(resume), now))
        self.conn.commit()
        return cur.rowcount == 1

    def retry_summary_job(self, kind: str, start: str, end: str, revision: float, now: float) -> bool:
        cur = self.conn.execute(
            "UPDATE summary_jobs SET state='pending',attempts=0,next_attempt_at=0,error='',updated_at=?"
            " WHERE kind=? AND start_date=? AND end_date=? AND state='blocked' AND updated_at=?",
            (now, kind, start, end, revision))
        self.conn.commit()
        return cur.rowcount == 1

    def summary_jobs_in_state(self, *states: str) -> list[dict]:
        marks = ",".join("?" for _ in states)
        return [dict(row) for row in self.conn.execute(
            f"SELECT * FROM summary_jobs WHERE state IN ({marks}) ORDER BY updated_at DESC", states)]

    def latest_period_summary(self, kind: str):
        return self.conn.execute("SELECT * FROM summaries WHERE kind=? ORDER BY window_end DESC,id DESC LIMIT 1", (kind,)).fetchone()

    def summary_request(self, key: tuple) -> dict | None:
        row = self.conn.execute("SELECT * FROM summary_requests WHERE kind=? AND start_date=? AND end_date=?", key).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["metadata"] = json.loads(item["metadata"])
        item["result"] = json.loads(item["result"]) if item["result"] else None
        return item

    def start_summary_request(self, key: tuple, metadata: dict, started: float) -> None:
        self.conn.execute("INSERT INTO summary_requests(kind,start_date,end_date,started_at,metadata) VALUES(?,?,?,?,?)",
                          (*key, started, json.dumps(metadata, ensure_ascii=False)))
        self.conn.commit()

    def update_summary_request(self, key: tuple, *, response_id: str = "", request_id: str = "", result: dict | None = None) -> None:
        self.conn.execute("UPDATE summary_requests SET response_id=CASE WHEN ?<>'' THEN ? ELSE response_id END,"
                          "request_id=CASE WHEN ?<>'' THEN ? ELSE request_id END,result=COALESCE(?,result)"
                          " WHERE kind=? AND start_date=? AND end_date=?",
                          (response_id, response_id, request_id, request_id,
                           json.dumps(result, ensure_ascii=False) if result is not None else None, *key))
        self.conn.commit()

    def clear_summary_request(self, key: tuple) -> None:
        self.conn.execute("DELETE FROM summary_requests WHERE kind=? AND start_date=? AND end_date=?", key)
        self.conn.commit()
