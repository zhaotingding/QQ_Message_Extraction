"""退群清理及群操作流水；成稿、批注和费用账本不参与消息删除。"""
from __future__ import annotations

import json
import time


GROUP_ACTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_action_jobs (
  id TEXT PRIMARY KEY, created_at REAL NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_departures (
  account TEXT NOT NULL, group_id TEXT NOT NULL, left_at REAL NOT NULL,
  PRIMARY KEY(account,group_id)
);
"""


class GroupActionStore:
    def save_group_job(self, job: dict) -> None:
        self.conn.execute("INSERT INTO group_action_jobs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                          (job["id"], job["created_at"], json.dumps(job, ensure_ascii=False)))
        self.conn.commit()  # 每次外部提交之前落盘，重启后不重放未明请求。

    def group_job(self, job_id: str = "") -> dict | None:
        row = (self.conn.execute("SELECT payload FROM group_action_jobs WHERE id=?", (job_id,)).fetchone() if job_id
               else self.conn.execute("SELECT payload FROM group_action_jobs ORDER BY created_at DESC LIMIT 1").fetchone())
        return json.loads(row[0]) if row else None

    def recover_group_jobs(self) -> None:
        rows = list(self.conn.execute("SELECT payload FROM group_action_jobs"))
        for row in rows:
            job = json.loads(row[0])
            if job["state"] != "running":
                continue
            for item in job["groups"]:
                if item["state"] == "sending":
                    item.update(state="unknown", error="服务中断，结果未知；请在 QQ 中核对，未自动重发")
                elif item["state"] == "pending":
                    item.update(state="not_sent", error="服务中断，未发送")
            job.update(state="stopped", note="上次操作中断；保留逐群结果，不自动恢复发送")
            self.save_group_job(job)

    def departure_cutoffs(self, account: str) -> dict:
        return {row[0]: row[1] for row in self.conn.execute(
            "SELECT group_id,left_at FROM group_departures WHERE account=?", (account,))}

    def group_message_count(self, group_id: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM messages WHERE group_id=?", (group_id,)).fetchone()[0]

    def clear_departed_group(self, account: str, group_id: str) -> dict:
        """只在明确退群成功后调用；同群全部消息清理，不触碰已生成报告。"""
        with self.conn:
            cutoff = time.time()
            self.conn.execute("INSERT INTO group_departures VALUES(?,?,?) ON CONFLICT(account,group_id) DO UPDATE SET left_at=excluded.left_at",
                              (account, group_id, cutoff))
            images = self.conn.execute("DELETE FROM message_images WHERE message_row IN (SELECT id FROM messages WHERE group_id=?)", (group_id,)).rowcount
            self.conn.execute("DELETE FROM message_receipts WHERE group_id=?", (group_id,))
            self.conn.execute("DELETE FROM alerts WHERE group_id=?", (group_id,))
            count = self.conn.execute("DELETE FROM messages WHERE group_id=?", (group_id,)).rowcount
            self.conn.execute("DELETE FROM group_accounts WHERE account=? AND group_id=?", (account, group_id))
            snapshot = self.conn.execute("SELECT group_ids FROM account_group_snapshots WHERE account=?", (account,)).fetchone()
            if snapshot:
                ids = [gid for gid in json.loads(snapshot[0]) if gid != group_id]
                self.conn.execute("UPDATE account_group_snapshots SET group_ids=?,updated_at=? WHERE account=?", (json.dumps(ids), cutoff, account))
        return {"messages_deleted": count, "images_deleted": images, "left_at": cutoff}
