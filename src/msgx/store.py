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
  cost_cny   REAL DEFAULT 0,
  cache_hit_tokens  INTEGER DEFAULT 0,
  cache_miss_tokens INTEGER DEFAULT 0
);

-- v0.5.1：账户余额读数流水，用来「用官方数字做减法」推算**真实**消费。
-- 官方只提供 GET /user/balance（只给余额、不给消费明细），所以消费只能靠
-- 相邻两次读数的差值累加得到。见 billing.consumption_from_readings。
-- ts 用 INTEGER 主键（秒）并加唯一约束：同一秒重复读数只留一条，天然幂等。
CREATE TABLE IF NOT EXISTS balance_readings (
  ts       INTEGER PRIMARY KEY,
  total    REAL DEFAULT 0,
  topped_up REAL DEFAULT 0,
  granted  REAL DEFAULT 0,
  day      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_balance_day ON balance_readings(day, ts);
"""

# v0.4.2：计费口径改为 DeepSeek 官方的「峰谷 + 缓存命中」三档计价，金额单位由
# 美元改为人民币。老库的 usage 表没有这三列，这里幂等补齐（历史行的 cost_cny
# 为 0，不追溯换算 —— 旧金额是按已作废的价目表算的，换算反而会失真）。
USAGE_MIGRATIONS = (
    ("cost_cny", "REAL DEFAULT 0"),
    ("cache_hit_tokens", "INTEGER DEFAULT 0"),
    ("cache_miss_tokens", "INTEGER DEFAULT 0"),
)

# v0.5.0：图片语义单独记状态。
# 旧实现用「vision_text 是否为空」兼任「成功 / 失败」两种含义，失败后这条又被
# 标成 media_state='done' → 这张图永远不会再被处理（实测丢掉 62 张）。
# 有了独立状态才能「只重跑失败的语义提取」，不必重新下载和 OCR。
#   '' = 未尝试或需重试 / 'ok' / 'failed' / 'skipped'（未启用或模型不支持图片）
#
# vision_tries 是**补跑次数**：没有它，一张永远读不出来的图会被每一轮补跑
# 反复送去推理，安静地烧钱。到上限就停在 'failed'，不再重试。
MESSAGE_MIGRATIONS = (
    ("vision_state", "TEXT DEFAULT ''"),
    ("vision_tries", "INTEGER DEFAULT 0"),
)

# 语义补跑的最大次数（attempts）。三次都失败基本可判定是图本身的问题。
VISION_MAX_TRIES = 3


class Store:
    def __init__(self, db_path: str | Path):
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(p), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """幂等补齐老库缺失的列。"""
        for table, migs in (("usage", USAGE_MIGRATIONS), ("messages", MESSAGE_MIGRATIONS)):
            cols = {c[1] for c in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, spec in migs:
                if name not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")

        # 老库里已经处理完的图没有 vision_state。按「有没有语义文本」回填一次，
        # 否则它们会被当成「未尝试」而被无意义地全量重跑。
        self.conn.execute(
            "UPDATE messages SET vision_state='ok'"
            " WHERE vision_state='' AND media_state='done' AND vision_text<>''"
        )
        self.conn.execute(
            "UPDATE messages SET vision_state='failed'"
            " WHERE vision_state='' AND media_state='done' AND vision_text='' AND media_path<>''"
        )

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
        vision_state: str | None = None,
    ) -> None:
        """写入媒体处理结果。

        ``vision_state=None``（默认）表示**不动**该列 —— 只更新 OCR 的场景不该
        顺手把语义状态抹掉；要改就显式传 'ok' / 'failed' / 'skipped'。
        """
        if vision_state is None:
            self.conn.execute(
                "UPDATE messages SET ocr_text=?, vision_text=?, media_path=?, media_hash=?,"
                " media_state=? WHERE id=?",
                (ocr_text, vision_text, media_path, media_hash, state, row_id),
            )
        else:
            self.conn.execute(
                "UPDATE messages SET ocr_text=?, vision_text=?, media_path=?, media_hash=?,"
                " media_state=?, vision_state=? WHERE id=?",
                (ocr_text, vision_text, media_path, media_hash, state, vision_state, row_id),
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

    def requeue_failed_media(self) -> int:
        """把上次运行中失败的图片重新置回待处理队列，返回重置条数。

        v0.4.0 的下载缺陷（只读第一个 chunk 就判「不完整」）留下一批被误判
        failed 的图片。服务启动时重置一次：URL 仍有效即被挽回；已过期会再次
        failed，但只在本策略下每次启动重试一次，不会死循环刷 URL。
        """
        cur = self.conn.execute(
            "UPDATE messages SET media_state='pending' WHERE media_state='failed' AND has_media=1"
        )
        self.conn.commit()
        return cur.rowcount

    def failed_vision(self, limit: int = 200, max_tries: int = VISION_MAX_TRIES) -> list:
        """语义提取失败的图片（图片本身已落盘，只需重跑推理，不必重新下载）。

        ``max_tries`` 之外的会被排除：那些已经试满次数，再跑只是白花钱。
        """
        return list(
            self.conn.execute(
                "SELECT * FROM messages WHERE vision_state='failed' AND media_path<>''"
                " AND COALESCE(vision_tries,0) < ?"
                " ORDER BY ts DESC LIMIT ?",
                (int(max_tries), limit),
            )
        )

    def bump_vision_tries(self, row_id: int) -> int:
        """记一次语义尝试，返回累计次数。"""
        self.conn.execute(
            "UPDATE messages SET vision_tries=COALESCE(vision_tries,0)+1 WHERE id=?", (row_id,)
        )
        self.conn.commit()
        row = self.conn.execute("SELECT vision_tries FROM messages WHERE id=?", (row_id,)).fetchone()
        return int(row[0]) if row else 0

    # ---------- 给界面「实时消息」页 ----------

    def recent_messages(self, limit: int = 60) -> list:
        """最近入库的消息（倒序）。带群名与图片处理状态，供界面直接展示。"""
        return list(
            self.conn.execute(
                "SELECT m.id, m.group_id, m.sender_name, m.ts, m.text, m.msg_type,"
                " m.media_state, m.vision_state, m.ocr_text, m.vision_text, m.at_me,"
                " COALESCE(g.name, '') AS group_name"
                " FROM messages m LEFT JOIN groups g ON g.id = m.group_id"
                " ORDER BY m.ts DESC, m.id DESC LIMIT ?",
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

    def window_end_by_kind(self, kind: str) -> int:
        """某条摘要轨道（periodic 的按群游标之外）最近覆盖到的时间点。

        热点（kind='hotspot'）与纪要（kind='digest'）各有独立游标：
        两者的生成节奏不同，共用一个游标会互相顶掉对方的窗口起点。
        """
        row = self.conn.execute(
            "SELECT MAX(window_end) AS e FROM summaries WHERE kind=?", (kind,)
        ).fetchone()
        return int(row["e"]) if row and row["e"] else 0

    def window_end_all(self) -> int:
        """跨群热点视角的窗口上界：最近一份热点报告覆盖到的时间点。"""
        return self.window_end_by_kind("hotspot")

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

    def add_usage(
        self,
        day: str,
        in_tokens: int,
        out_tokens: int,
        cost_cny: float,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
    ) -> None:
        self.conn.execute(
            "INSERT INTO usage(day, calls, in_tokens, out_tokens, cost_cny,"
            " cache_hit_tokens, cache_miss_tokens) VALUES(?,1,?,?,?,?,?)"
            " ON CONFLICT(day) DO UPDATE SET calls=calls+1,"
            " in_tokens=in_tokens+excluded.in_tokens,"
            " out_tokens=out_tokens+excluded.out_tokens,"
            " cost_cny=cost_cny+excluded.cost_cny,"
            " cache_hit_tokens=cache_hit_tokens+excluded.cache_hit_tokens,"
            " cache_miss_tokens=cache_miss_tokens+excluded.cache_miss_tokens",
            (day, in_tokens, out_tokens, cost_cny, cache_hit_tokens, cache_miss_tokens),
        )
        self.conn.commit()

    def daily_cost_cny(self, day: str) -> float:
        row = self.conn.execute("SELECT cost_cny FROM usage WHERE day=?", (day,)).fetchone()
        return float(row["cost_cny"]) if row else 0.0

    def set_usage_cost(self, day: str, cost_cny: float) -> None:
        """改写某一天的金额。

        只有「老库历史金额回填」会用（见 ``LLM.backfill_legacy_cost``）——
        线上计费靠 ``add_usage`` 累加，不要用这个方法改当天的数。
        """
        self.conn.execute("UPDATE usage SET cost_cny=? WHERE day=?", (float(cost_cny), day))
        self.conn.commit()

    def cost_totals(self) -> dict:
        """历史累计（本地估算口径）。"""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_cny),0) AS c, COALESCE(SUM(calls),0) AS n,"
            " COALESCE(SUM(in_tokens),0) AS i, COALESCE(SUM(out_tokens),0) AS o FROM usage"
        ).fetchone()
        return {
            "cost_cny": float(row["c"]),
            "calls": int(row["n"]),
            "in_tokens": int(row["i"]),
            "out_tokens": int(row["o"]),
        }

    def cost_before(self, day: str) -> float:
        """``day`` **之前**那些天的本地估算金额合计。

        用途：余额差值只能看到「开始记读数之后」的消费，看不到历史。直接把差值
        当作「累计花费」会在刚装上的那天显示 ¥0.00 —— 比原来的估算还离谱。
        用它把区间补齐：``累计 ≈ 读数起点之前的历史估算 + 读数起点之后的差值``。
        只有起点那天 00:00 到第一条读数之间那一小段是估的，误差可以忽略。
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_cny),0) AS c FROM usage WHERE day < ?", (day,)
        ).fetchone()
        return float(row["c"] if row else 0.0)

    def usage_all(self) -> list:
        return list(self.conn.execute("SELECT * FROM usage ORDER BY day DESC"))

    # ---------- 余额读数（v0.5.1 差值法）----------

    def record_balance(
        self, ts: int, total: float, topped_up: float = 0.0, granted: float = 0.0
    ) -> bool:
        """落一条余额读数。同一秒重复写会被主键挡掉（返回 False），天然幂等。

        为什么要把读数**存下来**而不是只显示当下值：官方只给「还剩多少」，
        不给「花了多少」。想拿到真实消费，只能靠相邻两次读数的**下降量**累加，
        所以必须留下历史。（见 ``billing.consumption_from_readings``）
        """
        day = time.strftime("%Y-%m-%d", time.localtime(int(ts)))
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO balance_readings(ts,total,topped_up,granted,day)"
            " VALUES(?,?,?,?,?)",
            (int(ts), float(total), float(topped_up), float(granted), day),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def balance_readings(self, day: str | None = None) -> list[dict]:
        """按时间升序返回读数；给 day 则只返回那一天的。"""
        if day:
            rows = self.conn.execute(
                "SELECT * FROM balance_readings WHERE day=? ORDER BY ts ASC", (day,)
            )
        else:
            rows = self.conn.execute("SELECT * FROM balance_readings ORDER BY ts ASC")
        return [dict(r) for r in rows]

    def balance_last(self) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM balance_readings ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return dict(r) if r else None

    def prune_balance(self, keep_days: int = 400) -> int:
        """读数只是小行（几十字节），但别无限涨。保留足够长的历史给「累计」用。"""
        cut = int(time.time()) - max(1, int(keep_days)) * 86400
        cur = self.conn.execute("DELETE FROM balance_readings WHERE ts < ?", (cut,))
        self.conn.commit()
        return cur.rowcount

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
            "vision_failed": q(
                "SELECT COUNT(*) FROM messages WHERE vision_state='failed' AND media_path<>''"
            ),
            # 还会被补跑的（次数没用满）—— 界面显示「还能自愈多少张」
            "vision_retry": q(
                f"SELECT COUNT(*) FROM messages WHERE vision_state='failed'"
                f" AND media_path<>'' AND COALESCE(vision_tries,0) < {int(VISION_MAX_TRIES)}"
            ),
        }
