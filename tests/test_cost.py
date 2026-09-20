"""计费口径的单元测试（对齐 DeepSeek 官方账单）。

官方对 deepseek-flash 实行三档计价：输入（缓存命中 / 未命中）+ 输出，
且高峰时段各项为**空闲时段的 2 倍**。高峰定义：UTC 01:00-04:00 与 06:00-10:00
的周一至周五，其余时段（含整个周末）为空闲。

不联网：只验算价目表与时段判定本身。
"""
from __future__ import annotations

import calendar
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / "src"))

from msgx.llm import LLM  # noqa: E402
from msgx.store import Store  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        fails.append(name)


CFG = {
    "llm": {
        "enabled": True,
        "max_daily_cost_cny": 10.0,
        "price_cache_hit_cny_per_mtok": 0.02,
        "price_cache_miss_cny_per_mtok": 1.00,
        "price_output_cny_per_mtok": 4.00,
        "peak_multiplier": 2.0,
        "peak_windows_utc": "01:00-04:00,06:00-10:00",
    }
}

tmp = tempfile.mkdtemp()
store = Store(os.path.join(tmp, "cost.db"))
llm = LLM(CFG, store)


def utc(y, m, d, hh, mm=0):
    return calendar.timegm((y, m, d, hh, mm, 0, 0, 0, 0))


# 2026-09-21 周一，2026-09-26 周六
MON_PEAK = utc(2026, 9, 21, 2, 0)      # 周一 02:00 UTC → 高峰
MON_OFF1 = utc(2026, 9, 21, 12, 0)     # 周一 12:00 UTC → 空闲
MON_OFF2 = utc(2026, 9, 21, 5, 0)      # 周一 05:00 UTC → 空闲（高峰窗口之间）
MON_PEAK2 = utc(2026, 9, 21, 8, 0)     # 周一 08:00 UTC → 高峰
SAT_PEAK = utc(2026, 9, 26, 2, 0)      # 周六 02:00 UTC → 空闲（周末无高峰）

print("=== 峰谷时段判定 ===")
check("周一 02:00 UTC 为高峰", llm.is_peak(MON_PEAK))
check("周一 08:00 UTC 为高峰", llm.is_peak(MON_PEAK2))
check("周一 05:00 UTC 为空闲（两个高峰窗口之间）", not llm.is_peak(MON_OFF2))
check("周一 12:00 UTC 为空闲", not llm.is_peak(MON_OFF1))
check("周六 02:00 UTC 为空闲（周末无高峰）", not llm.is_peak(SAT_PEAK))

print("\n=== 金额验算（元/百万 token）===")
MILLION = 1_000_000
check("空闲 · 100 万未命中输入 = ¥1.00", abs(llm.cost_cny(MILLION, 0, 0, ts=MON_OFF1) - 1.00) < 1e-9)
check("高峰 · 100 万未命中输入 = ¥2.00", abs(llm.cost_cny(MILLION, 0, 0, ts=MON_PEAK) - 2.00) < 1e-9)
check("空闲 · 100 万输出 = ¥4.00", abs(llm.cost_cny(0, MILLION, 0, ts=MON_OFF1) - 4.00) < 1e-9)
check("高峰 · 100 万输出 = ¥8.00", abs(llm.cost_cny(0, MILLION, 0, ts=MON_PEAK) - 8.00) < 1e-9)
check(
    "空闲 · 100 万缓存命中输入 = ¥0.02",
    abs(llm.cost_cny(MILLION, 0, MILLION, ts=MON_OFF1) - 0.02) < 1e-9,
)
check(
    "命中与未命中混合：50 万命中 + 50 万未命中 = ¥0.51",
    abs(llm.cost_cny(MILLION, 0, MILLION // 2, ts=MON_OFF1) - (0.01 + 0.50)) < 1e-9,
)
check("命中数超过输入数时按输入数封顶", abs(llm.cost_cny(MILLION, 0, MILLION * 3, ts=MON_OFF1) - 0.02) < 1e-9)

print("\n=== 用量累计与预算 ===")
store.add_usage("2026-09-21", MILLION, MILLION, 5.0, 100, MILLION - 100)
store.add_usage("2026-09-21", MILLION, 0, 1.0, 0, MILLION)
row = store.conn.execute("SELECT * FROM usage WHERE day='2026-09-21'").fetchone()
check("调用次数累加", row["calls"] == 2, str(row["calls"]))
check("金额累加（人民币）", abs(row["cost_cny"] - 6.0) < 1e-9, f"{row['cost_cny']:.4f}")
check("缓存命中 token 累加", row["cache_hit_tokens"] == 100, str(row["cache_hit_tokens"]))
check("缓存未命中 token 累加", row["cache_miss_tokens"] == 2 * MILLION - 100, str(row["cache_miss_tokens"]))
check("daily_cost_cny 可读", abs(store.daily_cost_cny("2026-09-21") - 6.0) < 1e-9)
check("无记录的日子返回 0", store.daily_cost_cny("1999-01-01") == 0.0)

print("\n=== 老库迁移（旧 usage 表没有新列）===")
old_path = os.path.join(tmp, "old.db")
conn = sqlite3.connect(old_path)
conn.execute(
    "CREATE TABLE usage (day TEXT PRIMARY KEY, calls INTEGER DEFAULT 0,"
    " in_tokens INTEGER DEFAULT 0, out_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0)"
)
conn.execute("INSERT INTO usage VALUES('2026-09-19', 19, 9093, 3769, 0.004488)")
conn.commit()
conn.close()
old = Store(old_path)
cols = {c[1] for c in old.conn.execute("PRAGMA table_info(usage)")}
check("补齐 cost_cny 列", "cost_cny" in cols)
check("补齐 cache_hit_tokens 列", "cache_hit_tokens" in cols)
check("补齐 cache_miss_tokens 列", "cache_miss_tokens" in cols)
r = old.conn.execute("SELECT * FROM usage WHERE day='2026-09-19'").fetchone()
check("历史行保留原值（不追溯换算）", r["calls"] == 19 and abs(r["cost_cny"]) < 1e-9)
old.add_usage("2026-09-20", 1000, 100, 0.001, 0, 1000)
check("迁移后可正常写入", old.conn.execute("SELECT calls FROM usage WHERE day='2026-09-20'").fetchone()[0] == 1)

# v0.5.0：历史金额回填。「累计花费」现在是显示给用户看的数字，让老库那几天
# 停在 ¥0 会明显误导（用户原话就是「界面和平台账单对不上」）。
print("\n=== 历史金额回填（老库那几天不能停在 ¥0）===")


def _cost_llm(store_obj):
    """造一个只用于算钱的 LLM（不联网）。"""
    return LLM({"llm": dict(CFG["llm"])}, store_obj)


n = _cost_llm(old).backfill_legacy_cost()
check("回填了 1 个历史日期", n == 1, str(n))
row19 = old.conn.execute("SELECT * FROM usage WHERE day='2026-09-19'").fetchone()
# 空闲价：9093 未命中输入 + 3769 输出 = 0.009093 + 0.015076 ≈ 0.024169
check("金额按空闲价算出（下限，不是 0）", abs(row19["cost_cny"] - 0.024169) < 1e-6,
      f"{row19['cost_cny']:.6f}")
check("回填是幂等的（再跑一次不动它）", _cost_llm(old).backfill_legacy_cost() == 0)
check("已补偿的日期不被重复累加",
      abs(old.conn.execute("SELECT cost_cny FROM usage WHERE day='2026-09-19'").fetchone()[0]
          - 0.024169) < 1e-6)
old.close()

print("\n=== 摘要游标分轨（增量纪要的起点）===")
s = Store(os.path.join(tmp, "cur.db"))
check("初始游标为 0", s.window_end_by_kind("digest") == 0)
s.add_summary("hotspot", 100, 200, 5, "x", "hotspot")
s.add_summary("digest", 100, 300, 5, "y", "digest")
check("热点轨道不受纪要影响", s.window_end_by_kind("hotspot") == 200)
check("纪要轨道独立推进", s.window_end_by_kind("digest") == 300)
s.add_summary("digest", 300, 400, 5, "z", "digest")
check("纪要轨道取最新值", s.window_end_by_kind("digest") == 400)
s.close()

store.close()

print()
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
