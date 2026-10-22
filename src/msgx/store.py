"""存储组合入口：唯一连接、结构迁移及跨领域统计；领域 SQL 由明确方法组提供。"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .models import message_links
from .message_store import MessageStore, VISION_MAX_TRIES
from .summary_store import SummaryStore
from .usage_store import UsageStore

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

CREATE TABLE IF NOT EXISTS summary_requests (
  kind TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  response_id TEXT NOT NULL DEFAULT '',
  request_id TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL,
  metadata TEXT NOT NULL,
  result TEXT NOT NULL DEFAULT '',
  accounted INTEGER NOT NULL DEFAULT 0,
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

class Store(MessageStore, SummaryStore, UsageStore):
    def __init__(self, db_path: str | Path, *, filters: dict | None = None, readonly: bool = False):
        self.filters = filters if filters is not None else {}
        p = Path(db_path)
        self.db_path = p  # 后台读者使用此路径在线程内创建自己的只读连接。
        if readonly:
            self.conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
            self.conn.row_factory = sqlite3.Row
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(p), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        old = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()
        if old and "UNIQUE(group_id, msg_id)" in old[0]:
            backup = p.with_name(p.name + f".pre-account-{time.time_ns()}.bak")
            with closing(sqlite3.connect(backup)) as dest:
                self.conn.backup(dest)
        elif old and self.conn.execute("PRAGMA user_version").fetchone()[0] < 705:
            # 705 将历史回填改为一次执行；首次升级前保留包含 WAL 的可恢复快照。
            # 不覆盖已有备份，正常再次启动也不会重做备份或全表回填。
            backup = p.with_name(p.name + ".pre-705.bak")
            if not backup.exists():
                with closing(sqlite3.connect(backup)) as dest:
                    self.conn.backup(dest)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """幂等补齐老库缺失的列。"""
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        links_missing = "mentioned_ids" not in {c[1] for c in self.conn.execute("PRAGMA table_info(messages)")}
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

        self._migrate_accounts(backfill=version < 705)
        if links_missing:
            self.conn.execute("ALTER TABLE messages ADD COLUMN mentioned_ids TEXT NOT NULL DEFAULT '[]'")
            for row in self.conn.execute("SELECT id,raw,at_me,received_by,reply_to FROM messages").fetchall():
                try:
                    event = json.loads(row["raw"] or "{}")
                    targets, reply = message_links(event if isinstance(event, dict) else {})
                except ValueError:
                    targets, reply = [], ""
                if row["at_me"] and row["received_by"] and row["received_by"] not in targets:
                    targets.append(row["received_by"])
                self.conn.execute("UPDATE messages SET mentioned_ids=?,reply_to=? WHERE id=?",
                                  (json.dumps(targets), row["reply_to"] or reply, row["id"]))
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_reply ON messages(group_id,msg_id,sender_id)")
        # 高频健康查询只扫描候选项，不随聊天库增长反复扫描全部正文。
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_pending_media ON messages(id) WHERE media_state='pending'")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_vision_failed ON messages(vision_tries)"
                          " WHERE vision_state='failed' AND media_path<>''")

        if version >= 705:
            return  # 旧图片及接收凭据回填已完成；普通启动不重复扫描和写入。

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
        self.conn.execute("PRAGMA user_version=705")

    def close(self) -> None:
        self.conn.close()

    def _migrate_accounts(self, *, backfill: bool) -> None:
        """保留行 ID、解析结果和账本关联；未知历史账号不猜测。"""
        sql = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
        if "UNIQUE(group_id, msg_id)" in sql:
            backfill = True  # 表结构仍旧时，账号迁移完成后必须补齐关联。
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
        if backfill:
            self.conn.execute("INSERT OR IGNORE INTO message_receipts SELECT received_by,group_id,msg_id,id FROM messages")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_receipt_message ON message_receipts(message_row,account)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS group_accounts ("
                          "group_id TEXT NOT NULL, account TEXT NOT NULL, PRIMARY KEY(group_id,account))")
        if backfill:
            self.conn.execute("INSERT OR IGNORE INTO group_accounts SELECT id,bot_uin FROM groups WHERE bot_uin<>''")
            self.conn.execute("INSERT OR IGNORE INTO group_accounts SELECT group_id,received_by FROM messages WHERE received_by<>''")

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
