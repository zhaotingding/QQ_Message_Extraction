"""手动主题报的正文、已使用材料和进行中的修订，独立于周期调度。"""
import json
import time

TOPIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS topic_reports (
    topic_key TEXT PRIMARY KEY, name TEXT NOT NULL, sections TEXT NOT NULL DEFAULT '[]',
    sources TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'ready',
    note TEXT NOT NULL DEFAULT '', pending TEXT NOT NULL DEFAULT ''
);
"""


class TopicStore:
    def topic_reports(self):
        # 旧库在首次启动新版接收服务前仍可只读打开。
        if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='topic_reports'").fetchone():
            return []
        rows = self.conn.execute("SELECT topic_key,name,revision,updated_at,state,note,sections FROM topic_reports"
                                 " ORDER BY updated_at DESC,name")
        return [{**dict(row), "sections": json.loads(row["sections"])} for row in rows]

    def topic_work(self, key):
        row = self.conn.execute("SELECT * FROM topic_reports WHERE topic_key=?", (key,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "sections": json.loads(row["sections"]), "sources": json.loads(row["sources"]),
                "pending": json.loads(row["pending"]) if row["pending"] else None}

    def start_topic(self, key, name, work):
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO topic_reports(topic_key,name) VALUES(?,?)", (key, name))
            cur = self.conn.execute("UPDATE topic_reports SET pending=?,state='running',note='' WHERE topic_key=? AND pending=''",
                                    (json.dumps(work, ensure_ascii=False), key))
            if not cur.rowcount:
                raise ValueError("此主题已有待完成的更新")

    def topic_progress(self, key, work, state="running", note=""):
        with self.conn:
            self.conn.execute("UPDATE topic_reports SET pending=?,state=?,note=? WHERE topic_key=?",
                              (json.dumps(work, ensure_ascii=False), state, note, key))

    def finish_topic(self, key, sections, sources, changed):
        # 多批全部成功后才替换可读成稿；中途失败保留上次的完整正文。
        with self.conn:
            self.conn.execute("""UPDATE topic_reports SET sections=?,sources=?,pending='',state='ready',
                revision=revision+?,updated_at=CASE WHEN ? THEN ? ELSE updated_at END,note=? WHERE topic_key=?""",
                (json.dumps(sections, ensure_ascii=False), json.dumps(sources), int(changed), changed,
                 time.time(), "已更新主题报" if changed else "已读完新材料，没有新进展", key))
