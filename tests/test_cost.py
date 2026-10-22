"""固定测试费率、预算与余额差值回归；不联网，不将样本费率视为当前官方报价。"""
from __future__ import annotations

import atexit
import calendar
from contextlib import ExitStack, closing
from datetime import datetime
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / "src"))

from msgx.llm import LLM  # noqa: E402
from msgx.store import Store  # noqa: E402
from msgx.time_rules import BEIJING, beijing_day  # 余额日期与覆盖时长统一按北京时间计算。

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

resources = ExitStack()  # 统一回收本文件的临时库、WAL 与升级备份。
atexit.register(resources.close)  # 发生异常时仍按连接先于目录的顺序释放。
tmp = resources.enter_context(tempfile.TemporaryDirectory(prefix="msgx_cost_"))
store = resources.enter_context(closing(Store(os.path.join(tmp, "cost.db"))))
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
old = resources.enter_context(closing(Store(old_path)))  # 异常中断时也可关闭迁移测试库。
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

print("\n=== 余额差值法（v0.5.1：官方余额的下降量 = 真实消费）===")
from msgx.billing import consumption_from_readings, daily_from_readings  # noqa: E402

check("空读数 → 算不出，且明说不够", consumption_from_readings([])["enough"] is False)
one = [{"ts": 100, "total": 20.0}]
check("只有一个读数 → 够不着（一个点只能说明「当时有多少钱」）",
      consumption_from_readings(one)["enough"] is False)
check("只有一个读数 → 消费算作 0（不是负数）",
      consumption_from_readings(one)["spent"] == 0)

down = [{"ts": 1, "total": 20.0}, {"ts": 2, "total": 19.4}, {"ts": 3, "total": 19.1}]
d = consumption_from_readings(down)
check("余额下降被累加成消费", abs(d["spent"] - 0.9) < 1e-9, f"{d['spent']}")
check("两个以上读数即可算", d["enough"] is True)

up = [{"ts": 1, "total": 20.0}, {"ts": 2, "total": 30.0}]
u = consumption_from_readings(up)
check("余额上升不算消费（那是充值）", u["spent"] == 0)
check("充值被单独记成 added", abs(u["added"] - 10.0) < 1e-9, f"{u['added']}")

mixed = [
    {"ts": 1, "total": 20.0},
    {"ts": 2, "total": 19.5},   # 花 0.5
    {"ts": 3, "total": 29.5},   # 充值 10
    {"ts": 4, "total": 28.5},   # 花 1.0
    {"ts": 5, "total": 28.5},   # 持平（余额只到分，可能读不出变化）
]
m = consumption_from_readings(mixed)
check("充值夹在中间时不污染消费（只看下降量）", abs(m["spent"] - 1.5) < 1e-9, f"{m['spent']}")
check("充值总额也算得出", abs(m["added"] - 10.0) < 1e-9, f"{m['added']}")
check("读数个数被如实报告（界面据此标注）", m["points"] == 5)
check("分辨率如实暴露为 0.01", m["resolution"] == 0.01)

# 「今日」只统计当天读数：跨午夜那段含昨天的尾巴，算进来会让今天虚增
rows = [
    {"ts": 100, "total": 20.0, "day": "2026-09-19"},
    {"ts": 200, "total": 18.0, "day": "2026-09-19"},
    {"ts": 300, "total": 17.5, "day": "2026-09-20"},
    {"ts": 400, "total": 17.0, "day": "2026-09-20"},
]
t = daily_from_readings(rows, "2026-09-20")
check("今日只取当天读数", abs(t["spent"] - 0.5) < 1e-9, f"{t['spent']}")
check("昨日那一大段下降不被算进今天", t["spent"] < 1.0)
check("今日覆盖起点 = 当天第一条读数（界面据此标「自 HH:MM 起」）",
      t["from_ts"] == 300, str(t["from_ts"]))


def local_ts(y, m, d, hh, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=BEIJING).timestamp())  # 不依赖测试电脑时区。


DAY = "2026-09-20"
# 首次安装那天：程序 14:00 才起来，当天读数只覆盖半天
half = [
    {"ts": local_ts(2026, 9, 20, 14, 0), "total": 20.0, "day": DAY},
    {"ts": local_ts(2026, 9, 20, 14, 10), "total": 19.6, "day": DAY},
]
h = daily_from_readings(half, DAY)
check("中途才启动 → 算得出但不算「完整一天」", h["enough"] is True and h["full_day"] is False,
      f"delay={h['anchor_delay']}s")
check("锚点延迟被如实暴露（界面/上层据此回落估算）", h["anchor_delay"] >= 13 * 3600,
      str(h["anchor_delay"]))

full = [
    {"ts": local_ts(2026, 9, 20, 0, 5), "total": 20.0, "day": DAY},
    {"ts": local_ts(2026, 9, 20, 23, 55), "total": 19.0, "day": DAY},
]
f = daily_from_readings(full, DAY)
check("全天在跑（起点 00:05）→ 视为完整一天", f["full_day"] is True, f"delay={f['anchor_delay']}s")
check("完整一天时今日消费即真实值", abs(f["spent"] - 1.0) < 1e-9, f"{f['spent']}")
with patch("msgx.billing.time.localtime", side_effect=time.gmtime):  # 模拟系统设置为 UTC。
    utc_host = daily_from_readings(full, DAY)  # 同一组北京时间余额应有相同覆盖口径。
check("系统时区不影响北京时间零点锚点", utc_host["anchor_delay"] == 300 and utc_host["full_day"],
      f"delay={utc_host['anchor_delay']}s")
check("当天没有读数 → 不完整", daily_from_readings([], DAY)["full_day"] is False)
check("完整性的宽限可调（给 20 小时宽限时半天也算满）",
      daily_from_readings(half, DAY, anchor_slack=20 * 3600)["full_day"] is True)
check("当天只有一条读数的日期算不出", daily_from_readings(rows[:3], "2026-09-21")["enough"] is False)
check("当天只有一条读数时也不算完整一天",
      daily_from_readings([{"ts": 300, "total": 17.5, "day": DAY}], DAY)["full_day"] is False)

print("\n=== 把差值补成完整区间（否则首日「累计」会显示 ¥0.00）===")
cb = resources.enter_context(closing(Store(os.path.join(tmp, "prior.db"))))
cb.add_usage("2026-09-18", 1000, 100, 0.5, 0, 1000)
cb.add_usage("2026-09-19", 1000, 100, 0.25, 0, 1000)
cb.add_usage("2026-09-20", 1000, 100, 0.9, 0, 1000)
check("cost_before 只累加指定日期之前的估算", abs(cb.cost_before("2026-09-20") - 0.75) < 1e-9,
      f"{cb.cost_before('2026-09-20')}")
check("cost_before 不含当天（当天由差值覆盖）", abs(cb.cost_before("2026-09-20") - 0.75) < 1e-9)
check("cost_before 对最早一天返回 0", cb.cost_before("2026-09-18") == 0)
check("cost_before 对未知日期返回全部历史", abs(cb.cost_before("2030-01-01") - 1.65) < 1e-9,
      f"{cb.cost_before('2030-01-01')}")
check("累积口径：差值 + 起点前的估算 = 更完整的累计",
      abs(cb.cost_before("2026-09-19") + 0.4 - 0.9) < 1e-9,
      f"{cb.cost_before('2026-09-19')} + 0.4")
cb.close()

print("\n=== 读数落库（差值法的原料）===")
bs = resources.enter_context(closing(Store(os.path.join(tmp, "bal.db"))))
check("首次写入成功", bs.record_balance(1000, 20.0, 20.0, 0.0) is True)
check("同一秒重复写入被主键挡掉（幂等）", bs.record_balance(1000, 20.0, 20.0, 0.0) is False)
bs.record_balance(2000, 19.4, 19.4, 0.0)
bs.record_balance(3000, 19.1, 19.1, 0.0)
rl = bs.balance_readings()
check("读数按时间升序返回", [r["ts"] for r in rl] == [1000, 2000, 3000], str([r["ts"] for r in rl]))
check("落库后能直接算出消费", abs(consumption_from_readings(rl)["spent"] - 0.9) < 1e-9)
check("balance_last 取最新一条", bs.balance_last()["ts"] == 3000)
check("按北京时间日期过滤可用", len(bs.balance_readings(beijing_day(1000))) == 3)
check("剪枝有返回且不报错", isinstance(bs.prune_balance(keep_days=400), int))
bs.close()

store.close()
resources.close()  # 正常路径立即删除临时数据，atexit 仅作为异常退出的清理。

print()
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
