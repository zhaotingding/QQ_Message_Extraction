"""用量及余额 SQL；沿用 Store 连接，使请求账本和 accounted 状态同事务提交。"""
from __future__ import annotations

import json
import time
from datetime import datetime

from .time_rules import BEIJING, beijing_day


class UsageStore:
    def record_api_call(self, *, ts: float, purpose: str, source_ids: list[int], model: str,
                        usage: dict | None, cost_cny: float | None, status: str, provider: str = "",
                        cost_usd: float | None = None, service_tier: str = "",
                        request_id: str = "", pricing: dict | None = None, summary_key: tuple | None = None) -> None:
        if summary_key and self.summary_request(summary_key)["accounted"]:
            return
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
        if summary_key:
            # 同一事务提交费用和记账标记，恢复时不会重复累计同一后台响应。
            self.conn.execute("UPDATE summary_requests SET accounted=1 WHERE kind=? AND start_date=? AND end_date=?", summary_key)
        if usage is not None:
            self.add_usage(datetime.fromtimestamp(ts, BEIJING).date().isoformat(), incoming, outgoing,
                           cost_cny or 0.0, hit, max(0, incoming - hit))
        else:
            self.conn.commit()  # 无返回用量时保留未知值，不把失败伪装成免费

    def api_usage_details(self, limit: int = 500, *, active_only: bool = False) -> dict:
        beijing = BEIJING
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

    def record_balance(
        self, ts: int, total: float, topped_up: float = 0.0, granted: float = 0.0
    ) -> bool:
        """落一条余额读数。同一秒重复写会被主键挡掉（返回 False），天然幂等。

        为什么要把读数**存下来**而不是只显示当下值：官方只给「还剩多少」，
        不给「花了多少」。想拿到真实消费，只能靠相邻两次读数的**下降量**累加，
        所以必须留下历史。（见 ``billing.consumption_from_readings``）
        """
        day = beijing_day(int(ts))
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
