"""图片初筛与自动调用状态；复用 Store 的连接和逐图结果写入入口。"""
from __future__ import annotations

import time
from datetime import datetime

from .time_rules import BEIJING
from .image_policy import ANALYSIS_DELAY_SECONDS, SCREEN_VERSION


IMAGE_COLUMNS = (
    ("screen_score", "INTEGER NOT NULL DEFAULT 0"),
    ("screen_reason", "TEXT NOT NULL DEFAULT ''"),
    ("screen_state", "TEXT NOT NULL DEFAULT 'pending'"),
    ("screened_at", "REAL NOT NULL DEFAULT 0"),
    ("auto_state", "TEXT NOT NULL DEFAULT ''"),
    ("auto_attempted_at", "REAL NOT NULL DEFAULT 0"),
    ("auto_ready_at", "REAL NOT NULL DEFAULT 0"),
    ("screen_version", "INTEGER NOT NULL DEFAULT 0"),
)


class ImageAnalysisStore:
    def screening_images(self, limit: int = 5, history_days: int = 7) -> list:
        sender_sql, sender_params = self._sender_condition("m.")
        select = "SELECT i.*,m.text,m.raw,m.group_id,m.ts FROM message_images i JOIN messages m ON m.id=i.message_row"
        common = " AND i.media_state='done' AND i.media_path<>'' AND " + sender_sql
        pending = list(self.conn.execute(select + " WHERE i.screen_state='pending'" + common +
                                        " ORDER BY i.id DESC LIMIT ?", (*sender_params, limit)))
        filter_key = (sender_sql, sender_params)
        if getattr(self, "_screening_filter_key", None) != filter_key:
            self._screening_filter_key = filter_key
            self._screening_completed_days = 0  # 过滤规则改变后，先前隐藏的旧图需要重新检查。
        legacy = []
        if history_days > self._screening_completed_days:
            legacy = list(self.conn.execute(select + " WHERE i.screen_state='done' AND i.screen_version<? AND m.ts>=?" + common +
                                           " ORDER BY i.id DESC LIMIT ?", (SCREEN_VERSION, time.time()-history_days*86400, *sender_params, limit)))
            if not legacy:
                self._screening_completed_days = history_days  # 本进程扫空后不再轮询过期旧图；扩大范围或重开再查。
        # 两个状态分支分别有界；新图始终由pending索引接续，不依赖旧图回筛缓存。
        return sorted(pending + legacy, key=lambda row: row["id"], reverse=True)[:limit]

    def set_screen_result(self, image_id: int, decision, threshold: int = 70) -> None:
        now = time.time()
        eligible = not decision.blocked and decision.score >= threshold
        self.conn.execute("UPDATE message_images SET screen_score=?,screen_reason=?,screen_state=?,screened_at=?,screen_version=?,"
                          " auto_ready_at=CASE WHEN ? AND auto_ready_at=0 AND auto_state='' AND vision_state<>'ok' THEN ? ELSE auto_ready_at END,"
                          " auto_state=CASE WHEN ? AND auto_state='' AND vision_state<>'ok' THEN 'waiting' ELSE auto_state END WHERE id=?",
                          (decision.score, decision.reason, "blocked" if decision.blocked else "done", now, SCREEN_VERSION,
                           eligible, now+ANALYSIS_DELAY_SECONDS, eligible, image_id))
        self.conn.commit()

    def image_context(self, message_row: int, limit: int = 8, account: str = "", *, after_seconds: int = 60) -> str:
        parent = self.get_message(message_row)
        if parent is None:
            return ""
        account = account or str(parent["received_by"] or "")
        sender_sql, sender_params = self._sender_condition("m.")
        ordering = "m.ts DESC,m.id DESC" if after_seconds > 60 else "ABS(m.ts-?),m.id"
        rows = self.conn.execute(
            "SELECT m.id,m.ts,m.text FROM messages m WHERE m.group_id=? AND m.ts BETWEEN ? AND ?"
            " AND (m.id=? OR m.has_media=0) AND " + sender_sql +
            " AND (?='' OR EXISTS(SELECT 1 FROM message_receipts r WHERE r.message_row=m.id AND r.account=?))"
            " ORDER BY " + ordering + " LIMIT ?",
            (parent["group_id"], parent["ts"]-180, parent["ts"]+after_seconds, message_row, *sender_params,
             account, account, *((parent["ts"],) if after_seconds <= 60 else ()), max(limit, 200) if after_seconds > 60 else limit)).fetchall()
        if after_seconds > 60 and not any(row["id"] == message_row for row in rows):
            rows = list(rows) + [parent]  # 高流量群的后文截取也必须保留原图配文。
        if len(rows) > limit:
            # 保留配文与跨时间均匀取样的后续消息，防止只取最近几秒而漏掉15分钟内的结论。
            pool = [row for row in rows if row["id"] != message_row]
            caption = [row for row in rows if row["id"] == message_row]
            pool.sort(key=lambda row: (row["ts"], row["id"]))
            slots = max(0, limit-len(caption))
            positions = {round(index*(len(pool)-1)/max(1, slots-1)) for index in range(slots)}
            rows = caption + [pool[index] for index in sorted(positions)]
        rows = sorted(rows, key=lambda row: (row["ts"], row["id"]))
        return "\n".join(f"{datetime.fromtimestamp(row['ts'], BEIJING):%H:%M:%S} {str(row['text'] or '')[:500]}" for row in rows)[:4000]

    def auto_image_candidates(self, options: dict, account: str, watch: set[str]) -> list:
        sender_sql, sender_params = self._sender_condition("m.")
        groups = sorted(watch)
        group_sql = " AND m.group_id IN (" + ",".join("?" for _ in groups) + ")" if groups else ""
        current_scope, scope_params = self._activity_group_scope("m.group_id", account)
        eligible = (" FROM message_images i JOIN messages m ON m.id=i.message_row"
                    " WHERE i.screen_state='done' AND i.screen_score>=? AND i.vision_state<>'ok' AND m.ts>=? AND " +
                    sender_sql + group_sql + " AND " + current_scope +
                    " AND (?='' OR EXISTS(SELECT 1 FROM message_receipts r WHERE r.message_row=m.id AND r.account=?))")
        params = (options["threshold"], time.time()-options["history_days"]*86400, *sender_params, *groups, *scope_params, account, account)
        # 降低阈值后的新候选也从首次命中时计时；限定范围与每批5张，不全库反复写入。
        new = list(self.conn.execute("SELECT i.id" + eligible + " AND i.auto_state='' AND i.auto_ready_at=0 LIMIT 5", params))
        if new:
            self.conn.executemany("UPDATE message_images SET auto_state='waiting',auto_ready_at=? WHERE id=? AND auto_state=''",
                                  [(time.time()+ANALYSIS_DELAY_SECONDS, row["id"]) for row in new])
            self.conn.commit()
        return list(self.conn.execute("SELECT i.*" + eligible + " AND i.auto_state='waiting'"
                                      " ORDER BY (i.auto_ready_at>?),i.screen_score DESC,i.id DESC LIMIT 1", (*params, time.time())))

    def claim_auto_image(self, image_id: int) -> bool:
        result = self.conn.execute("UPDATE message_images SET auto_state='running',auto_attempted_at=? WHERE id=? AND auto_state='waiting' AND auto_ready_at<=?",
                                   (time.time(), image_id, time.time()))
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
