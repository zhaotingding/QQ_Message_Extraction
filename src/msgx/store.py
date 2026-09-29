from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Msg

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
  id         TEXT PRIMARY KEY,
  name       TEXT DEFAULT '',
  added_at   INTEGER DEFAULT 0,
  bot_uin    TEXT DEFAULT ''
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
CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS message_images (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_row INTEGER NOT NULL,
  image_index INTEGER NOT NULL,
  image_url TEXT NOT NULL DEFAULT '',
  media_path TEXT NOT NULL DEFAULT '',
  media_hash TEXT NOT NULL DEFAULT '',
  ocr_text TEXT NOT NULL DEFAULT '',
  vision_text TEXT NOT NULL DEFAULT '',
  media_state TEXT NOT NULL DEFAULT 'pending',
  vision_state TEXT NOT NULL DEFAULT 'skipped',
  include_vision INTEGER NOT NULL DEFAULT 0,
  UNIQUE(message_row, image_index)
);
CREATE INDEX IF NOT EXISTS idx_image_pending ON message_images(media_state, id);

CREATE TABLE IF NOT EXISTS summaries (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id      TEXT NOT NULL,
  window_start  INTEGER NOT NULL,
  window_end    INTEGER NOT NULL,
  msg_count     INTEGER DEFAULT 0,
  kind          TEXT DEFAULT 'periodic',
  content       TEXT DEFAULT '',
  report_path   TEXT DEFAULT '',
  source_sig    TEXT NOT NULL DEFAULT '',
  created_at    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sum_group ON summaries(group_id, window_end);

CREATE TABLE IF NOT EXISTS daily_schedule (
  id         INTEGER PRIMARY KEY CHECK(id=1),
  start_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summary_schedule_baselines (
  kind TEXT PRIMARY KEY,
  start_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS summary_jobs (
  kind TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  summary_id INTEGER,
  updated_at REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(kind,start_date,end_date)
);

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

-- v0.6.0：逐次请求账本；旧版只有每日合计，不能伪造历史消息归因。
CREATE TABLE IF NOT EXISTS api_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  purpose TEXT NOT NULL,
  source_ids TEXT NOT NULL DEFAULT '[]',
  source_type TEXT NOT NULL,
  model TEXT NOT NULL,
  in_tokens INTEGER,
  out_tokens INTEGER,
  cache_hit_tokens INTEGER,
  cost_cny REAL,
  status TEXT NOT NULL
);
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
#   'rejected' = API 拒绝请求，不自动重试；保留图片和 OCR。
#
# vision_tries 是**补跑次数**：没有它，一张永远读不出来的图会被每一轮补跑
# 反复送去推理，安静地烧钱。到上限就停在 'failed'，不再重试。
MESSAGE_MIGRATIONS = (
    ("received_by", "TEXT NOT NULL DEFAULT ''"),
    ("vision_state", "TEXT DEFAULT ''"),
    ("vision_tries", "INTEGER DEFAULT 0"),
    ("include_vision", "INTEGER NOT NULL DEFAULT 0"),
    ("at_all", "INTEGER DEFAULT 0"),  # v0.5.7：独立记录，不能依赖可能截断的 raw JSON
)

# groups 表 v0.5.4 加 bot_uin：**这个群属于哪个协议端账号**。
#
# 为什么非要有它：`groups` 原先只记 (id, name, added_at)，于是「群列表」是
# **历次登录的并集** —— 谁登进来一次，它的全部群就永久留在库里。
# 实测：误登一次主号，33 个群（30 个一条消息都没有）就灌进来了，用户在下拉框里
# 再也找不到自己关心的那 3 个群。**没有「属主」这一维，就永远分不清谁是谁。**
# 它同时也是「只保留当前账号的群」这个动作的依据。
GROUP_MIGRATIONS = (
    ("bot_uin", "TEXT DEFAULT ''"),
)

# 语义补跑的最大次数（attempts）。三次都失败基本可判定是图本身的问题。
VISION_MAX_TRIES = 3


class Store:
    def __init__(self, db_path: str | Path):
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(p), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        old = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()
        if old and "UNIQUE(group_id, msg_id)" in old[0]:
            backup = p.with_name(p.name + f".pre-account-{time.time_ns()}.bak")
            with closing(sqlite3.connect(backup)) as dest:
                self.conn.backup(dest)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """幂等补齐老库缺失的列。"""
        for table, migs in (
            ("usage", USAGE_MIGRATIONS),
            ("messages", MESSAGE_MIGRATIONS),
            ("groups", GROUP_MIGRATIONS),
            ("api_calls", (("provider", "TEXT NOT NULL DEFAULT ''"),
                           ("cost_usd", "REAL"), ("service_tier", "TEXT NOT NULL DEFAULT ''"),
                           ("request_id", "TEXT NOT NULL DEFAULT ''"), ("pricing", "TEXT NOT NULL DEFAULT ''"),
                           ("cache_write_tokens", "INTEGER"))),
            ("summaries", (("source_sig", "TEXT NOT NULL DEFAULT ''"),)),
        ):
            cols = {c[1] for c in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, spec in migs:
                if name not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")

        self._migrate_accounts()

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
        for row in self.conn.execute("SELECT id,image_urls,media_path,media_hash,ocr_text,vision_text,"
                                     "media_state,vision_state,include_vision FROM messages WHERE has_media=1"):
            try:
                urls = json.loads(row["image_urls"] or "[]")
            except ValueError:
                urls = []
            if not urls and row["media_path"]:
                urls = [""]
            for index, url in enumerate(urls):
                first = index == 0
                self.conn.execute(
                    "INSERT OR IGNORE INTO message_images"
                    "(message_row,image_index,image_url,media_path,media_hash,ocr_text,vision_text,"
                    "media_state,vision_state,include_vision) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (row["id"], index, str(url), row["media_path"] if first else "",
                     row["media_hash"] if first else "", row["ocr_text"] if first else "",
                     row["vision_text"] if first else "",
                     ("done" if row["media_path"] else "pending") if first else "pending",
                     (row["vision_state"] or "skipped") if first else "skipped",
                     row["include_vision"]),
                )
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 704:
            self.conn.execute("UPDATE message_images SET include_vision=0")
            self.conn.execute("UPDATE messages SET include_vision=0 WHERE has_media=1")
            self.conn.execute("PRAGMA user_version=704")

    def close(self) -> None:
        self.conn.close()

    def _migrate_accounts(self) -> None:
        """保留行 ID、解析结果和账本关联；未知历史账号不猜测。"""
        sql = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
        if "UNIQUE(group_id, msg_id)" in sql:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            with self.conn:
                indexes = [r[0] for r in self.conn.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name='messages' AND type='index' AND sql IS NOT NULL")]
                seq = self.conn.execute("SELECT seq FROM sqlite_sequence WHERE name='messages'").fetchone()
                self.conn.execute(sql.replace("messages", "messages_accounts", 1).replace(
                    "UNIQUE(group_id, msg_id)", "UNIQUE(received_by, group_id, msg_id)"))
                self.conn.execute("INSERT INTO messages_accounts SELECT * FROM messages")
                self.conn.execute("DROP TABLE messages")
                self.conn.execute("ALTER TABLE messages_accounts RENAME TO messages")
                for statement in indexes:
                    self.conn.execute(statement)
                if seq:
                    self.conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='messages'", (seq[0],))
                for row in self.conn.execute("SELECT id, raw FROM messages WHERE raw<>''").fetchall():
                    try:
                        account = str(json.loads(row["raw"]).get("self_id") or "")
                    except (ValueError, AttributeError):
                        continue
                    if account.isascii() and account.isdigit() and account != "0":
                        self.conn.execute("UPDATE messages SET received_by=? WHERE id=?", (account, row["id"]))
        self.conn.execute("CREATE TABLE IF NOT EXISTS message_receipts ("
                          "account TEXT NOT NULL, group_id TEXT NOT NULL, msg_id TEXT NOT NULL,"
                          "message_row INTEGER NOT NULL, PRIMARY KEY(account, group_id, msg_id))")
        self.conn.execute("INSERT OR IGNORE INTO message_receipts SELECT received_by,group_id,msg_id,id FROM messages")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_receipt_message ON message_receipts(message_row,account)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS group_accounts ("
                          "group_id TEXT NOT NULL, account TEXT NOT NULL, PRIMARY KEY(group_id,account))")
        self.conn.execute("INSERT OR IGNORE INTO group_accounts SELECT id,bot_uin FROM groups WHERE bot_uin<>''")
        self.conn.execute("INSERT OR IGNORE INTO group_accounts SELECT group_id,received_by FROM messages WHERE received_by<>''")

    def has_receipt(self, account: str, group_id: str, msg_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM message_receipts WHERE account=? AND group_id=? AND msg_id=?",
                                 (account, group_id, msg_id)).fetchone() is not None

    # ---------- groups ----------

    def upsert_group(self, group_id: str, name: str = "", bot_uin: str = "") -> None:
        """登记/更新一个群。``bot_uin`` 是它的属主账号，留空 = 不动已有属主。

        空名与空属主都**不覆盖**已有值：群名可能一时拿不到（待配置补全），
        属主可能在拿不到 get_login_info 时未知 —— 都不能因此把已知信息抹掉。
        """
        self.conn.execute(
            "INSERT INTO groups(id, name, added_at, bot_uin) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            " name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE groups.name END,"
            " bot_uin=CASE WHEN excluded.bot_uin<>'' THEN excluded.bot_uin ELSE groups.bot_uin END",
            (group_id, name, int(time.time()), bot_uin),
        )
        if bot_uin:
            self.conn.execute("INSERT OR IGNORE INTO group_accounts VALUES(?,?)", (group_id, bot_uin))
        self.conn.commit()

    def group_names(self) -> dict:
        return {r["id"]: r["name"] for r in self.conn.execute("SELECT id, name FROM groups")}

    def groups_with_owner(self) -> list[dict]:
        """群列表 + 属主 + 消息条数（界面「统计哪些群」用）。"""
        rows = self.conn.execute(
            "SELECT g.id, g.name, g.bot_uin,"
            " (SELECT COUNT(*) FROM messages m WHERE m.group_id = g.id) AS n"
            " FROM groups g ORDER BY g.id"
        )
        return [dict(r) for r in rows]

    def prune_groups(self, keep_ids: set[str]) -> tuple[list[str], list[str]]:
        """删掉「不在 keep_ids 里 **且** 一条消息都没有」的群。

        返回 ``(已删, 因有消息而保留)``。

        为什么必须带「没有消息」这个条件：`groups` 表是**摘要调度的遍历源**
        （`group_names()`），删掉一个有消息的群会让它的摘要静默失效。
        而协议端返回的列表可能因为一次 API 抖动就是空的 —— 那时若照着删，
        用户的群会被自己的一次抖动清光。**有消息 = 有既成事实，一律保留。**
        """
        ids = [str(r["id"]) for r in self.conn.execute("SELECT id FROM groups")]
        stale = [i for i in ids if i not in keep_ids]
        if not stale:
            return [], []
        with_msg = {
            str(r["group_id"]) for r in self.conn.execute("SELECT DISTINCT group_id FROM messages")
        }
        deleted = [i for i in stale if i not in with_msg]
        protected = [i for i in stale if i in with_msg]
        if deleted:
            self.conn.executemany("DELETE FROM groups WHERE id=?", [(i,) for i in deleted])
            self.conn.commit()
        return deleted, protected

    def messages_since(self, since_ts: int) -> int:
        """某个时刻之后的**真实**消息条数（排除本工具的【链路自检】探针）。"""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM messages"
            " WHERE ts >= ? AND COALESCE(text,'') NOT LIKE '【链路自检】%'",
            (since_ts,),
        ).fetchone()
        return int(row[0] or 0)

    def hour_baseline(self, hour: int, days: int = 7, account: str = "") -> int:
        """最近若干天里，``hour`` 点这一小时每天消息条数的**中位数**。

        干什么用：区分「现在这个点本来就没人说话」与「消息冻结了」。
        没有它，看门狗会在凌晨一点准点误报 —— 深夜没人发言是常态，不是故障。

        两个刻意的选择：
        * **用中位数而不是均值** —— 某天一次刷屏会把均值抬到不可能达到的高度，
          于是看门狗变成哑巴。
        * **缺数据的那天按 0 计入**（只统计「库里本来就有数据的那些天」）——
          一条都没说的日子恰恰是「这个点常常很安静」的证据。若把它整个跳过，
          基线会被系统性抬高，同样导致误报。更早的日子（程序还没跑）不算，
          那不是「没人说话」。
        """
        now = int(time.time())
        since = now - days * 86400
        rows = self.conn.execute(
            "SELECT date(ts,'unixepoch','localtime') AS d, COUNT(*) AS n"
            " FROM messages"
            " WHERE ts >= ? AND CAST(strftime('%H', ts,'unixepoch','localtime') AS INTEGER) = ?"
            "   AND COALESCE(text,'') NOT LIKE '【链路自检】%'"
            " AND (?='' OR EXISTS (SELECT 1 FROM message_receipts r WHERE r.message_row=messages.id AND r.account=?))"
            " GROUP BY d",
            (since, hour, account, account),
        ).fetchall()
        first = self.conn.execute(
            "SELECT MIN(ts) FROM messages WHERE COALESCE(text,'') NOT LIKE '【链路自检】%'"
            " AND (?='' OR EXISTS (SELECT 1 FROM message_receipts r WHERE r.message_row=messages.id AND r.account=?))",
            (account, account),
        ).fetchone()[0]
        if first is None:
            return 0  # 一条真实消息都没有：没有基线可言
        counts = {r["d"]: int(r["n"]) for r in rows}
        # 按小时步进铺满，绝不漏掉任何一天（比按 86400 步进更稳，能容忍时区跳变）
        span: dict[str, int] = {}
        t = max(since, int(first))
        while t <= now:
            d = time.strftime("%Y-%m-%d", time.localtime(t))
            span[d] = counts.get(d, 0)
            t += 3600
        vals = sorted(span.values())
        if not vals:
            return 0
        mid = len(vals) // 2
        return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) // 2

    # ---------- messages ----------

    def insert_message(self, m: Msg, group_name: str = "", bot_uin: str = "", *, filtered: bool = False) -> int | None:
        """返回自增 id；重复消息返回 None。"""
        if self.has_receipt(bot_uin, m.group_id, m.msg_id):
            return None
        # 同一群消息被不同账号接收时只保留一份正文，分别记录接收凭据。
        # 不仅凭文本去重：必须消息 ID、时间、发送人、类型、内容均相同。
        duplicate = self.conn.execute(
            "SELECT id,raw,image_urls FROM messages WHERE group_id=? AND msg_id=? AND ts=? AND sender_id=?"
            " AND msg_type=? AND text=? AND received_by<>?",
            (m.group_id, m.msg_id, m.ts, m.sender_id, m.msg_type, m.text, bot_uin)).fetchall()
        for row in duplicate:
            try:
                same_media = bool(m.raw.get("message")) and json.loads(row["raw"]).get("message") == m.raw.get("message")
            except (ValueError, AttributeError):
                same_media = False
            same_filtered_image = filtered and bool(m.image_urls) and json.loads(row["image_urls"] or "[]") == m.image_urls
            if (not m.image_urls and not m.file_names) or same_media or same_filtered_image:
                self.conn.execute("INSERT INTO message_receipts VALUES(?,?,?,?)",
                                  (bot_uin, m.group_id, m.msg_id, row["id"]))
                self.upsert_group(m.group_id, group_name, bot_uin)
                return None
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO messages"
            "(group_id, msg_id, sender_id, sender_name, ts, msg_type, text,"
            " has_media, image_urls, media_state, at_me, at_all, reply_to, raw, received_by, include_vision)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                m.group_id,
                m.msg_id,
                m.sender_id,
                m.sender_name,
                m.ts,
                m.msg_type,
                m.text,
                1 if m.image_urls or (not filtered and m.file_names) else 0,
                json.dumps(m.image_urls, ensure_ascii=False),
                "filtered" if filtered else "pending" if m.image_urls else "none",
                1 if m.at_me else 0,
                1 if m.at_all else 0,
                m.reply_to,
                "" if filtered else json.dumps(m.raw, ensure_ascii=False)[:20000],
                bot_uin,
                0,
            ),
        )
        if cur.rowcount == 0:
            return None
        self.conn.execute("INSERT INTO message_receipts VALUES(?,?,?,?)",
                          (bot_uin, m.group_id, m.msg_id, cur.lastrowid))
        for index, url in enumerate(m.image_urls):
            self.conn.execute("INSERT INTO message_images(message_row,image_index,image_url,include_vision) VALUES(?,?,?,0)",
                              (cur.lastrowid, index, str(url)))
        # 无条件注册群：群名可能暂时为空（待配置补全），但群必须出现在 groups 表里，
        # 否则摘要调度遍历 group_names() 时看不到这个群。upsert_group 内部已保护空名不覆盖已有名字。
        #
        # bot_uin 是**最可靠的属主来源**：消息只可能来自当前登录的那个账号。
        # 靠 get_group_list 判属主会受 API 抖动影响，而「收到了消息」是既成事实。
        self.upsert_group(m.group_id, group_name, bot_uin)
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

    def set_vision_include(self, row_id: int, include: bool) -> bool:
        cur = self.conn.execute(
            "UPDATE messages SET include_vision=? WHERE id=? AND has_media=1",
            (int(include), row_id),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def get_image(self, image_id: int):
        return self.conn.execute("SELECT * FROM message_images WHERE id=?", (image_id,)).fetchone()

    def pending_images(self, limit: int = 200) -> list:
        return list(self.conn.execute(
            "SELECT * FROM message_images WHERE media_state='pending' AND image_url<>''"
            " ORDER BY id LIMIT ?", (limit,),
        ))

    def find_image_by_hash(self, media_hash: str):
        return self.conn.execute(
            "SELECT ocr_text FROM message_images WHERE media_hash=? AND media_state='done'"
            " AND ocr_text<>'' LIMIT 1", (media_hash,),
        ).fetchone()

    def set_image_result(self, image_id: int, *, ocr_text: str | None = None,
                         vision_text: str | None = None, media_path: str | None = None,
                         media_hash: str | None = None, media_state: str | None = None,
                         vision_state: str | None = None) -> None:
        fields = {"ocr_text": ocr_text, "vision_text": vision_text, "media_path": media_path,
                  "media_hash": media_hash, "media_state": media_state, "vision_state": vision_state}
        updates = {key: value for key, value in fields.items() if value is not None}
        if not updates:
            return
        self.conn.execute("UPDATE message_images SET " + ",".join(f"{key}=?" for key in updates)
                          + " WHERE id=?", (*updates.values(), image_id))
        self._sync_image_message(self.get_image(image_id)["message_row"])

    def set_image_include(self, image_id: int, include: bool) -> bool:
        cur = self.conn.execute("UPDATE message_images SET include_vision=? WHERE id=?",
                                (int(include), image_id))
        if cur.rowcount:
            self._sync_image_message(self.get_image(image_id)["message_row"])
        return bool(cur.rowcount)

    def _sync_image_message(self, message_row: int) -> None:
        """父消息保留聚合字段，供现有摘要读取；逐张结果始终保存在子表。"""
        images = list(self.conn.execute("SELECT * FROM message_images WHERE message_row=? ORDER BY image_index",
                                        (message_row,)))
        if not images:
            return
        ocr = "\n".join(r["ocr_text"] for r in images if r["include_vision"] and r["ocr_text"])
        vision = "\n".join(r["vision_text"] for r in images if r["include_vision"] and r["vision_text"])
        first = images[0]
        current = self.conn.execute("SELECT media_state FROM messages WHERE id=?", (message_row,)).fetchone()
        state = current["media_state"] if current and current["media_state"] == "filtered" else (
            "pending" if any(r["media_state"] == "pending" for r in images) else
            "failed" if any(r["media_state"] == "failed" for r in images) else "done"
        )
        self.conn.execute("UPDATE messages SET ocr_text=?,vision_text=?,media_path=?,media_hash=?,"
                          "vision_state=?,media_state=?,include_vision=? WHERE id=?",
                          (ocr, vision, first["media_path"], first["media_hash"], first["vision_state"],
                           state, int(any(r["include_vision"] for r in images)), message_row))
        self.conn.commit()

    def find_by_hash(self, media_hash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT ocr_text, vision_text, media_path, vision_state FROM messages"
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
        children = self.conn.execute(
            "UPDATE message_images SET media_state='pending' WHERE media_state='failed' AND image_url<>''"
        )
        self.conn.execute(
            "UPDATE messages SET media_state='pending' WHERE media_state='failed'"
            " AND EXISTS (SELECT 1 FROM message_images i WHERE i.message_row=messages.id"
            " AND i.media_state='pending')"
        )
        self.conn.commit()
        return children.rowcount

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

    def recent_messages(self, limit: int = 60, images_only: bool = False) -> list:
        """最近入库的消息（倒序）。带群名与图片处理状态，供界面直接展示。"""
        rows = list(
            self.conn.execute(
                "SELECT m.id, m.group_id, m.sender_name, m.ts, m.text, m.msg_type,"
                " m.media_state, m.vision_state, m.ocr_text, m.vision_text, m.media_path,"
                " m.has_media, m.include_vision, m.at_me,"
                " m.received_by, (SELECT GROUP_CONCAT(account) FROM message_receipts"
                " WHERE message_row=m.id AND account<>'') AS receiver_accounts,"
                " COALESCE(g.name, '') AS group_name"
                " FROM messages m LEFT JOIN groups g ON g.id = m.group_id"
                " WHERE (?=0 OR EXISTS (SELECT 1 FROM message_images i WHERE i.message_row=m.id))"
                " ORDER BY m.ts DESC, m.id DESC LIMIT ?",
                (int(images_only), limit),
            )
        )
        result = []
        for row in rows:
            item = dict(row)
            item["images"] = [dict(image) for image in self.conn.execute(
                "SELECT * FROM message_images WHERE message_row=? ORDER BY image_index", (row["id"],))]
            result.append(item)
        return result

    def messages_between(self, group_id: str, start: int, end: int) -> list:
        return list(
            self.conn.execute(
                "SELECT * FROM messages WHERE group_id=? AND ts>? AND ts<=?"
                " AND (media_state<>'filtered' OR (include_vision=1 AND (vision_text<>'' OR ocr_text<>''))) ORDER BY ts ASC",
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
            "SELECT MIN(ts) AS t FROM messages WHERE group_id=?"
            " AND (media_state<>'filtered' OR (include_vision=1 AND (vision_text<>'' OR ocr_text<>'')))", (group_id,)
        ).fetchone()
        return int(row["t"]) if row and row["t"] else 0

    # ---------- 跨群（热点统计用） ----------

    def first_ts_all(self) -> int:
        row = self.conn.execute("SELECT MIN(ts) AS t FROM messages WHERE"
                                " media_state<>'filtered' OR (include_vision=1 AND (vision_text<>'' OR ocr_text<>''))").fetchone()
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

    def messages_between_all(self, start: int, end: int, limit: int = -1) -> list:
        """窗口内消息按时间升序；逐张图片选择已勾选的语义或 OCR。"""
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE ts>? AND ts<=?"
            " AND (media_state<>'filtered' OR (include_vision=1 AND (vision_text<>'' OR ocr_text<>'')))"
            " ORDER BY ts ASC,id ASC LIMIT ?", (start, end, limit),
        )
        result = []
        for row in rows:
            item = dict(row)
            if item["has_media"]:
                images = list(self.conn.execute(
                    "SELECT ocr_text,vision_text,include_vision FROM message_images"
                    " WHERE message_row=? ORDER BY image_index", (item["id"],)))
            else:
                images = []
            if images:
                parts = []
                for image in images:
                    if item["include_vision"] and image["include_vision"]:
                        part = image["vision_text"] or image["ocr_text"]
                        if part:
                            parts.append(part)
                item["summary_picture"] = "\n".join(parts)
            else:
                item["summary_picture"] = ((item["vision_text"] or item["ocr_text"])
                                           if item["include_vision"] else "")
            result.append(item)
        return result

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

    def count_messages_window(self, start: int, end: int) -> int:
        body = "COALESCE(text,'')"
        for marker in ("[图片]", "[表情]", "[表情包]", "[语音]", "[视频]", "[合并转发]"):
            body = f"REPLACE({body},'{marker}','')"
        row = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE ts>=? AND ts<?"
            " AND (media_state<>'filtered' OR (include_vision=1 AND (vision_text<>'' OR ocr_text<>'')))"
            f" AND (TRIM({body}, char(9)||char(10)||char(13)||' ')<>''"
            " OR (include_vision=1 AND (vision_text<>'' OR ocr_text<>'')))",
            (start, end),
        ).fetchone()
        return int(row[0])

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

    def claim_summary_job(self, kind: str, start: str, end: str, now: float) -> bool:
        cur = self.conn.execute(
            "UPDATE summary_jobs SET state='running',attempts=attempts+1,updated_at=?"
            " WHERE kind=? AND start_date=? AND end_date=?"
            " AND state IN ('pending','retry','waiting') AND attempts<3 AND next_attempt_at<=?",
            (now, kind, start, end, now))
        self.conn.commit()
        return cur.rowcount == 1

    def summary_jobs_in_state(self, *states: str) -> list[dict]:
        marks = ",".join("?" for _ in states)
        return [dict(row) for row in self.conn.execute(
            f"SELECT * FROM summary_jobs WHERE state IN ({marks}) ORDER BY updated_at DESC", states)]

    def latest_period_summary(self, kind: str):
        return self.conn.execute("SELECT * FROM summaries WHERE kind=? ORDER BY window_end DESC,id DESC LIMIT 1", (kind,)).fetchone()

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

    def record_api_call(self, *, ts: float, purpose: str, source_ids: list[int], model: str,
                        usage: dict | None, cost_cny: float | None, status: str, provider: str = "",
                        cost_usd: float | None = None, service_tier: str = "",
                        request_id: str = "", pricing: dict | None = None) -> None:
        ids = list(dict.fromkeys(source_ids))
        source_type = ("汇总报告" if purpose in ("日报合并", "摘要合并", "摘要成稿") else "系统检查") if not ids else "多条消息汇总"
        if len(ids) == 1:
            row = self.get_message(ids[0])
            source_type = {"text": "文字消息", "image": "纯图片", "mixed": "图文/附件"}.get(
                row["msg_type"] if row else "", "原消息已清理")
        incoming = int(usage.get("prompt_tokens", 0)) if usage is not None else None
        outgoing = int(usage.get("completion_tokens", 0)) if usage is not None else None
        hit = int(usage.get("prompt_cache_hit_tokens", 0)) if usage is not None else None
        written = int(usage["prompt_cache_write_tokens"]) if usage is not None and usage.get("prompt_cache_write_tokens") is not None else None
        self.conn.execute(
            "INSERT INTO api_calls(ts,purpose,source_ids,source_type,model,in_tokens,out_tokens,cache_hit_tokens,cost_cny,status,provider,cost_usd,service_tier,request_id,pricing,cache_write_tokens)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, purpose, json.dumps(ids), source_type, model, incoming, outgoing, hit, cost_cny, status, provider,
             cost_usd, service_tier, request_id, json.dumps(pricing, ensure_ascii=False) if pricing else "", written),
        )
        if usage is not None:
            self.add_usage(datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).date().isoformat(), incoming, outgoing,
                           cost_cny or 0.0, hit, max(0, incoming - hit))
        else:
            self.conn.commit()  # 无返回用量时保留未知值，不把失败伪装成免费

    def api_usage_details(self, limit: int = 500, *, active_only: bool = False) -> dict:
        beijing = timezone(timedelta(hours=8))
        today = datetime.now(beijing).date()
        start = datetime.combine(today, datetime.min.time(), beijing).timestamp()
        end = start + 86400
        # 仅显示现用流程；旧账不删除、不改写。超上下文步骤仍归入摘要总结。
        purpose = "CASE WHEN purpose IN ('摘要分块','超上下文合并') THEN '摘要总结' ELSE purpose END"
        where = "WHERE purpose IN ('摘要总结','图片理解','摘要分块','超上下文合并')" if active_only else ""
        items = [dict(r) for r in self.conn.execute(f"SELECT * FROM api_calls {where} ORDER BY id DESC LIMIT ?", (limit,))]
        for item in items:
            if active_only and item["purpose"] in ("摘要分块", "超上下文合并"):
                item["purpose"] = "摘要总结"
            item["today_cost_cny"] = item["cost_cny"] if start <= item["ts"] < end else 0.0
            ids = json.loads(item["source_ids"])
            item["source_count"] = len(ids)
            item["source_label"] = f"消息 #{ids[0]}" if len(ids) == 1 else f"{len(ids)} 条消息" if ids else item["source_type"]
        grouped = [dict(r) for r in self.conn.execute(
            f"SELECT {purpose if active_only else 'purpose'} AS purpose,source_type,provider,model,COUNT(*) AS calls,SUM(in_tokens) AS in_tokens,"
            " SUM(out_tokens) AS out_tokens,COALESCE(SUM(cost_cny),0) AS cost_cny,"
            " SUM(cost_usd) AS cost_usd,SUM(cost_usd IS NULL) AS unknown_usd_calls,"
            " SUM(cache_write_tokens) AS cache_write_tokens,SUM(cache_write_tokens IS NULL) AS unknown_cache_write_calls,"
            " COALESCE(SUM(CASE WHEN ts>=? AND ts<? THEN cost_cny END),0) AS today_cost_cny,"
            " SUM(CASE WHEN ts>=? AND ts<? AND cost_cny IS NULL THEN 1 ELSE 0 END) AS today_unknown_calls,"
            f" SUM(cost_cny IS NULL) AS unknown_calls FROM api_calls {where} GROUP BY 1,2,3,4 ORDER BY cost_cny DESC",
            (start, end, start, end))]
        known = float(self.conn.execute("SELECT COALESCE(SUM(cost_cny),0) FROM api_calls").fetchone()[0])
        utc_days = [dict(r) for r in self.conn.execute(
            "SELECT strftime('%Y-%m-%d',ts,'unixepoch') AS day,COUNT(*) AS calls,"
            " SUM(cost_cny) AS cost_cny,SUM(cost_usd) AS cost_usd,"
            " SUM(cost_cny IS NULL) AS unknown_calls,SUM(cost_usd IS NULL) AS unknown_usd_calls"
            " FROM api_calls WHERE provider='OpenAI 官方' OR"
            " (json_valid(pricing) AND json_extract(pricing,'$.base_url')='https://api.openai.com/v1')"
            " GROUP BY 1 ORDER BY day DESC LIMIT 31")]
        daily_costs = [dict(r) for r in self.conn.execute(
            f"SELECT strftime('%Y-%m-%d',ts,'unixepoch','+8 hours') AS day,{purpose} AS purpose,"
            " SUM(cost_cny) AS cost_cny,SUM(cost_cny IS NULL) AS unknown_calls,COUNT(*) AS calls"
            " FROM api_calls WHERE ts>=? AND ts<?"
            " AND purpose IN ('摘要总结','图片理解','摘要分块','超上下文合并')"
            " GROUP BY 1,2 ORDER BY 1,2", (start - 29 * 86400, end))]
        return {"items": items, "groups": grouped, "recorded_cost_cny": known,
                "openai_utc_days": utc_days, "today_timezone": "Asia/Shanghai",
                "daily_costs": daily_costs,
                "visible_cost_cny": sum(r["cost_cny"] for r in grouped),
                "today_cost_cny": sum(r["today_cost_cny"] for r in grouped),
                "historical_unattributed_cny": max(0.0, self.cost_totals()["cost_cny"] - known)}

    def api_call_sources(self, call_id: int) -> dict:
        row = self.conn.execute("SELECT source_ids FROM api_calls WHERE id=?", (call_id,)).fetchone()
        ids = json.loads(row[0]) if row else []
        items = []
        for rid in ids[:100]:
            msg = self.get_message(rid)
            if msg:
                items.append({key: msg[key] for key in ("id", "msg_id", "group_id", "sender_name", "text")})
        return {"items": items, "total": len(ids), "shown_limit": 100}

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
        media_cut = now - keep_days_media * 86400
        paths = {r[0] for r in self.conn.execute(
            "SELECT media_path FROM messages WHERE media_path<>'' AND ts<?", (max(raw_cut, media_cut),))}
        paths.update(r[0] for r in self.conn.execute(
            "SELECT i.media_path FROM message_images i JOIN messages m ON m.id=i.message_row"
            " WHERE i.media_path<>'' AND m.ts<?", (max(raw_cut, media_cut),)))
        c1 = self.conn.execute("DELETE FROM messages WHERE ts < ? AND media_state<>'pending'", (raw_cut,))
        self.conn.execute("DELETE FROM message_images WHERE NOT EXISTS"
                          " (SELECT 1 FROM messages WHERE messages.id=message_images.message_row)")
        self.conn.execute("UPDATE messages SET media_path='' WHERE ts<?", (media_cut,))
        self.conn.execute("UPDATE message_images SET media_path='' WHERE message_row IN"
                          " (SELECT id FROM messages WHERE ts<?)", (media_cut,))
        retained = {r[0] for r in self.conn.execute("SELECT media_path FROM messages WHERE media_path<>''")}
        retained.update(r[0] for r in self.conn.execute(
            "SELECT media_path FROM message_images WHERE media_path<>''"))
        for path in paths - retained:
            Path(path).unlink(missing_ok=True)
        self.conn.commit()
        return {"messages_deleted": c1.rowcount, "media_files_deleted": len(paths - retained)}

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
