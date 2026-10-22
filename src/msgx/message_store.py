"""消息、群、图片及告警的 SQL 操作；连接和事务生命周期由 Store 持有。"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from .models import Msg, message_links
from .message_keywords import matching_fields
from .prefs import QGROUP_MANAGER_ID, QGROUP_MANAGER_NAME, blocked_senders
from .time_rules import BEIJING, beijing_day

VISION_MAX_TRIES = 3  # 沿用已有的图片语义失败次数上限。


class MessageStore:
    def _sender_condition(self, prefix: str = "") -> tuple[str, tuple]:
        """接收与历史查询共用名单；名称只通过 SQL 参数传入。"""
        names = blocked_senders(self.filters)
        if not names:
            return "1=1", ()
        sql = f"TRIM(COALESCE({prefix}sender_name,'')) NOT IN ({','.join('?' for _ in names)})"
        params = tuple(names)
        if QGROUP_MANAGER_NAME in names:
            sql += f" AND COALESCE({prefix}sender_id,'')<>?"
            params += (QGROUP_MANAGER_ID,)
        return sql, params

    def has_receipt(self, account: str, group_id: str, msg_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM message_receipts WHERE account=? AND group_id=? AND msg_id=?",
                                 (account, group_id, msg_id)).fetchone() is not None

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

    def group_rows(self) -> list[tuple]:
        """设置页的群、已有属主及消息数；只读旧库时保留原单属主回退。"""
        owners = "g.bot_uin"
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='group_accounts'").fetchone():
            owners = "(SELECT GROUP_CONCAT(account) FROM group_accounts WHERE group_id=g.id)"
        return [tuple(row) for row in self.conn.execute(
            f"SELECT g.id,g.name,{owners},"
            " (SELECT COUNT(*) FROM messages m WHERE m.group_id=g.id)"
            " FROM groups g ORDER BY g.id")]

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
            "SELECT date(ts,'unixepoch','+8 hours') AS d, COUNT(*) AS n"
            " FROM messages"
            " WHERE ts >= ? AND CAST(strftime('%H', ts,'unixepoch','+8 hours') AS INTEGER) = ?"
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
            d = beijing_day(t)
            span[d] = counts.get(d, 0)
            t += 3600
        vals = sorted(span.values())
        if not vals:
            return 0
        mid = len(vals) // 2
        return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) // 2

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
            " has_media, image_urls, media_state, at_me, at_all, reply_to, raw, received_by, include_vision,mentioned_ids)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                json.dumps(sorted(set(message_links(m.raw)[0]) | ({bot_uin} if m.at_me and bot_uin else set()))),
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
        sender_sql, sender_params = self._sender_condition("m.")
        return list(self.conn.execute(
            "SELECT i.* FROM message_images i JOIN messages m ON m.id=i.message_row"
            " WHERE i.media_state='pending' AND i.image_url<>''"
            f" AND {sender_sql} ORDER BY i.id LIMIT ?", (*sender_params, limit),
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

    def recent_messages(self, limit: int = 60, images_only: bool = False, group_id: str = "",
                        account: str = "", related_only: bool = False,
                        keyword_rules: tuple | None = None, offset: int = 0) -> list:
        """最近入库的消息（倒序）。带群名与图片处理状态，供界面直接展示。"""
        keyword_sql = ""
        if keyword_rules is not None:
            if not keyword_rules:
                return []
            self.conn.create_function("keyword_match", 3,
                                      lambda text, ocr, vision: bool(matching_fields(keyword_rules, text, ocr, vision)))
            keyword_sql = (" AND (keyword_match(m.text,m.ocr_text,m.vision_text) OR EXISTS"
                           " (SELECT 1 FROM message_images i WHERE i.message_row=m.id"
                           " AND keyword_match('',i.ocr_text,i.vision_text)))")
        sender_sql, sender_params = self._sender_condition("m.")
        rows = list(
            self.conn.execute(
                "SELECT m.id, m.group_id, m.sender_name, m.sender_id, m.ts, m.text, m.msg_type,"
                " CASE WHEN ?<>'' AND m.sender_id=? THEN 'self'"
                " WHEN ?<>'' AND m.at_all=0 AND (EXISTS(SELECT 1 FROM json_each(m.mentioned_ids) WHERE value=?)"
                " OR (m.reply_to<>'' AND EXISTS(SELECT 1 FROM messages parent INDEXED BY idx_msg_reply WHERE parent.group_id=m.group_id"
                " AND parent.msg_id=m.reply_to AND parent.sender_id=? AND parent.ts<=m.ts))) THEN 'reply' ELSE '' END AS relation,"
                " m.media_state, m.vision_state, m.ocr_text, m.vision_text, m.media_path,"
                " m.has_media, m.include_vision, m.at_me,"
                " m.received_by, (SELECT GROUP_CONCAT(account) FROM message_receipts"
                " WHERE message_row=m.id AND account<>'') AS receiver_accounts,"
                " COALESCE(g.name, '') AS group_name"
                " FROM messages m LEFT JOIN groups g ON g.id = m.group_id"
                " WHERE (?=0 OR EXISTS (SELECT 1 FROM message_images i WHERE i.message_row=m.id))"
                " AND (?='' OR m.group_id=?)"
                " AND (?=0 OR (relation<>'' AND m.at_all=0))"
                f" AND {sender_sql}"
                + keyword_sql + " ORDER BY m.ts DESC, m.id DESC LIMIT ? OFFSET ?",
                (account, account, account, account, account, int(images_only), group_id, group_id,
                 int(related_only), *sender_params, limit, offset),
            )
        )
        if not rows:
            return []
        result = [{**dict(row), "images": []} for row in rows]
        by_id = {item["id"]: item for item in result}
        for image in self.conn.execute(
                f"SELECT * FROM message_images WHERE message_row IN ({','.join('?' for _ in rows)})"
                " ORDER BY message_row,image_index", tuple(by_id)):
            by_id[image["message_row"]]["images"].append(dict(image))
        return result

    def group_activity(self, now: datetime | None = None) -> dict:
        """北京时间今日未过滤消息榜；按消息正文计数，不展开图片或接收账号。"""
        beijing = BEIJING
        current = now or datetime.now(beijing)
        if current.tzinfo is None:
            current = current.replace(tzinfo=beijing)
        start = current.astimezone(beijing).replace(hour=0, minute=0, second=0, microsecond=0)
        window_start = int(start.timestamp())
        window_end = int((start + timedelta(days=1)).timestamp())
        sender_sql, sender_params = self._sender_condition()
        rows = self.conn.execute(
            "WITH known_groups AS (SELECT id AS group_id FROM groups UNION SELECT group_id FROM messages),"
            " today AS (SELECT group_id, COUNT(*) AS count, MAX(ts) AS last_ts FROM messages"
            " WHERE ts>=? AND ts<? AND media_state<>'filtered'"
            f" AND {sender_sql} GROUP BY group_id)"
            " SELECT k.group_id, COALESCE(g.name, '') AS group_name,"
            " COALESCE(t.count, 0) AS count, COALESCE(t.last_ts, 0) AS last_ts"
            " FROM known_groups k LEFT JOIN groups g ON g.id=k.group_id"
            " LEFT JOIN today t ON t.group_id=k.group_id"
            " ORDER BY count DESC, last_ts DESC, k.group_id",
            (window_start, window_end, *sender_params),
        )
        return {"date": start.date().isoformat(), "window_start": window_start,
                "window_end": window_end, "groups": [dict(row) for row in rows]}

    def messages_between_all(self, start: int, end: int, limit: int = -1) -> list:
        """窗口内消息按时间升序；逐张图片选择已勾选的语义或 OCR。"""
        sender_sql, sender_params = self._sender_condition()
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE ts>? AND ts<=?"
            f" AND {sender_sql}"
            " AND (media_state<>'filtered' OR (include_vision=1 AND (vision_text<>'' OR ocr_text<>'')))"
            " ORDER BY ts ASC,id ASC LIMIT ?", (start, end, *sender_params, limit),
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

    def count_messages_window(self, start: int, end: int) -> int:
        sender_sql, sender_params = self._sender_condition()
        body = "COALESCE(text,'')"
        for marker in ("[图片]", "[表情]", "[表情包]", "[语音]", "[视频]", "[合并转发]"):
            body = f"REPLACE({body},'{marker}','')"
        row = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE ts>=? AND ts<?"
            f" AND {sender_sql}"
            " AND (media_state<>'filtered' OR (include_vision=1 AND (vision_text<>'' OR ocr_text<>'')))"
            f" AND (TRIM({body}, char(9)||char(10)||char(13)||' ')<>''"
            " OR (include_vision=1 AND (vision_text<>'' OR ocr_text<>'')))",
            (start, end, *sender_params),
        ).fetchone()
        return int(row[0])

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
