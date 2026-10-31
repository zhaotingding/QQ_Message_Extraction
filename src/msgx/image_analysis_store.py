"""图片初筛与自动调用状态；复用 Store 的连接和逐图结果写入入口。"""
from __future__ import annotations

import time
from datetime import datetime

from .time_rules import BEIJING


IMAGE_COLUMNS = (
    ("screen_score", "INTEGER NOT NULL DEFAULT 0"),
    ("screen_reason", "TEXT NOT NULL DEFAULT ''"),
    ("screen_state", "TEXT NOT NULL DEFAULT 'pending'"),
    ("screened_at", "REAL NOT NULL DEFAULT 0"),
    ("auto_state", "TEXT NOT NULL DEFAULT ''"),
    ("auto_attempted_at", "REAL NOT NULL DEFAULT 0"),
)


class ImageAnalysisStore:
    def screening_images(self, limit: int = 5) -> list:
        sender_sql, sender_params = self._sender_condition("m.")
        return list(self.conn.execute(
            "SELECT i.*,m.text,m.raw,m.group_id,m.ts FROM message_images i JOIN messages m ON m.id=i.message_row"
            " WHERE i.screen_state='pending' AND i.media_state='done' AND i.media_path<>'' AND " + sender_sql +
            " ORDER BY i.id DESC LIMIT ?", (*sender_params, limit,)))  # 每批有界，不在界面刷新时扫描全库。

    def set_screen_result(self, image_id: int, decision) -> None:
        self.conn.execute("UPDATE message_images SET screen_score=?,screen_reason=?,screen_state=?,screened_at=? WHERE id=?",
                          (decision.score, decision.reason, "blocked" if decision.blocked else "done", time.time(), image_id))
        self.conn.commit()

    def image_context(self, message_row: int, limit: int = 8, account: str = "") -> str:
        parent = self.get_message(message_row)
        if parent is None:
            return ""
        account = account or str(parent["received_by"] or "")
        sender_sql, sender_params = self._sender_condition("m.")
        rows = self.conn.execute(
            "SELECT m.text FROM messages m WHERE m.group_id=? AND m.ts BETWEEN ? AND ?"
            " AND (m.id=? OR m.has_media=0) AND " + sender_sql +
            " AND (?='' OR EXISTS(SELECT 1 FROM message_receipts r WHERE r.message_row=m.id AND r.account=?))"
            " ORDER BY ABS(m.ts-?),m.id LIMIT ?",
            (parent["group_id"], parent["ts"]-180, parent["ts"]+60, message_row, *sender_params,
             account, account, parent["ts"], limit)).fetchall()
        return "\n".join(str(row["text"] or "")[:500] for row in rows)[:4000]

    def auto_image_candidates(self, options: dict, account: str, watch: set[str]) -> list:
        sender_sql, sender_params = self._sender_condition("m.")
        groups = sorted(watch)
        group_sql = " AND m.group_id IN (" + ",".join("?" for _ in groups) + ")" if groups else ""
        current_scope, scope_params = self._activity_group_scope("m.group_id", account)
        return list(self.conn.execute(
            "SELECT i.* FROM message_images i JOIN messages m ON m.id=i.message_row"
            " WHERE i.screen_state='done' AND i.screen_score>=? AND i.auto_state=''"
            " AND i.vision_state<>'ok' AND m.ts>=? AND " + sender_sql + group_sql + " AND " + current_scope +
            " AND (?='' OR EXISTS(SELECT 1 FROM message_receipts r WHERE r.message_row=m.id AND r.account=?))"
            " ORDER BY i.screen_score DESC,i.id DESC LIMIT 1",
            (options["threshold"], time.time()-options["history_days"]*86400, *sender_params, *groups, *scope_params, account, account)))

    def claim_auto_image(self, image_id: int) -> bool:
        result = self.conn.execute("UPDATE message_images SET auto_state='running',auto_attempted_at=? WHERE id=? AND auto_state=''",
                                   (time.time(), image_id))
        self.conn.commit()  # 网络请求前持久化；崩溃后 running 不会自动重发。
        return result.rowcount == 1

    def finish_auto_image(self, image_id: int, state: str) -> None:
        self.conn.execute("UPDATE message_images SET auto_state=? WHERE id=?", (state, image_id))
        self.conn.commit()

    def automatic_image_usage(self) -> dict:
        start = datetime.now(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        attempts = self.conn.execute("SELECT COUNT(*) FROM message_images WHERE auto_attempted_at>=?", (start,)).fetchone()[0]
        row = self.conn.execute("SELECT COALESCE(SUM(cost_cny),0),SUM(cost_cny IS NULL),COUNT(*) FROM api_calls"
                                " WHERE purpose='自动图片理解' AND ts>=?", (start,)).fetchone()
        unresolved = self.conn.execute("SELECT COUNT(*) FROM message_images WHERE auto_state='unknown' AND auto_attempted_at>=?", (start,)).fetchone()[0]
        return {"attempts": max(attempts, int(row[2])), "cost_cny": float(row[0]), "unknown": max(unresolved, int(row[1] or 0))}

    def recover_auto_images(self) -> None:
        self.conn.execute("UPDATE message_images SET auto_state='unknown' WHERE auto_state='running'")
        self.conn.commit()  # 请求可能已处理；仅标未知，不重新排队。

    def reusable_image_vision(self, digest: str, message_row: int):
        return self.conn.execute(
            "SELECT i.vision_text FROM message_images i JOIN messages m ON m.id=i.message_row"
            " JOIN messages current ON current.id=? WHERE i.media_hash=? AND i.vision_state='ok'"
            " AND i.vision_text<>'' AND m.group_id=current.group_id AND m.text=current.text"
            " AND ABS(m.ts-current.ts)<=180 LIMIT 1", (message_row, digest)).fetchone() if digest else None
