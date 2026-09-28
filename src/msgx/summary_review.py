"""摘要阅读标记：独立写入元数据，不启动服务或执行整库迁移。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS summary_reviews (
  summary_id   INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  read_state   TEXT NOT NULL DEFAULT 'unread'
               CHECK(read_state IN ('unread','skimmed','deep')),
  annotations  TEXT NOT NULL DEFAULT '[]',
  notes        TEXT NOT NULL DEFAULT '',
  updated_at   INTEGER NOT NULL,
  PRIMARY KEY(summary_id, content_hash)
)
"""


def content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _annotations(value: list) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("批注必须是列表")
    result = []
    for item in value:
        if (not isinstance(item, dict) or type(item.get("start")) is not int
                or type(item.get("end")) is not int or item["start"] < 0
                or item["end"] <= item["start"] or not isinstance(item.get("text"), str)
                or not item["text"].strip() or item.get("level") not in ("blue", "red", "underline")):
            raise ValueError("批注区间、文字或类型无效")
        # 坐标来自 Tk 显示正文，不能按 Markdown 原文或 Python 字符切片验证。
        result.append({key: item[key] for key in ("start", "end", "text", "level")})
    return result


def get_review(conn: sqlite3.Connection, summary_id: int, content: str) -> dict:
    digest = content_hash(content)
    result = {"read_state": "unread", "annotations": [], "notes": "", "content_hash": digest}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='summary_reviews'").fetchone():
        return result
    row = conn.execute(
        "SELECT read_state,annotations,notes FROM summary_reviews WHERE summary_id=? AND content_hash=?",
        (summary_id, digest),
    ).fetchone()
    if row:
        result.update(read_state=row[0], annotations=_annotations(json.loads(row[1])), notes=row[2])
    return result


def save_review(db_path: str | Path, summary_id: int, expected_hash: str, *,
                read_state: str | None = None, annotations: list | None = None,
                notes: str | None = None) -> dict:
    if read_state is not None and read_state not in ("unread", "skimmed", "deep"):
        raise ValueError("阅读状态无效")
    if annotations is not None:
        annotations = _annotations(annotations)
    if notes is not None and not isinstance(notes, str):
        raise ValueError("心得必须是文字")
    # mode=rw 防止路径错误时创建空库；事务锁避免不同窗口互相覆盖未修改的字段。
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT content FROM summaries WHERE id=?", (summary_id,)).fetchone()
            if row is None:
                raise ValueError("摘要不存在，请刷新摘要列表")
            if content_hash(row[0]) != expected_hash:
                raise ValueError("摘要正文已变化，请重新打开后标注")
            conn.execute(SCHEMA)
            review = get_review(conn, summary_id, row[0])
            for key, value in (("read_state", read_state), ("annotations", annotations), ("notes", notes)):
                if value is not None:
                    review[key] = value
            conn.execute(
                "INSERT INTO summary_reviews(summary_id,content_hash,read_state,annotations,notes,updated_at)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(summary_id,content_hash) DO UPDATE SET"
                " read_state=excluded.read_state,annotations=excluded.annotations,"
                " notes=excluded.notes,updated_at=excluded.updated_at",
                (summary_id, expected_hash, review["read_state"],
                 json.dumps(review["annotations"], ensure_ascii=False), review["notes"], int(time.time())),
            )
        return review
    finally:
        conn.close()
