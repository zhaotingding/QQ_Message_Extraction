"""账单刷新与缓存协调。需要 cfg/store/停止事件；不持有 UI 或周期摘要状态。"""
from __future__ import annotations
import asyncio
import logging
import time
from datetime import date, datetime
from . import billing
from .config import api_key

from .time_rules import BEIJING as SHANGHAI

log = logging.getLogger("msgx.billing_coordinator")

class BillingCoordinator:
    def _init_billing_coordinator(self) -> None:
        """账单缓存只在本职责更新，健康接口仅读快照。"""
        self._billing_ts = 0.0
        self._billing_data = {}
        self._billing_lock = None

    def _billing_start(self, bc: dict) -> tuple[int, int]:
        """平台逐日消费的抓取起点（年, 月）。

        平台接口只支持**按月**查，累计值只能靠逐月累加，所以起点越早请求越多。
        默认取「库里最早有记录的那一天」—— 我们没开始用之前不可能有消费，
        这样既不会漏也不会白问一堆空月份。
        """
        try:
            y, m = int(bc.get("start_year") or 0), int(bc.get("start_month") or 0)
            if 2000 < y < 2100 and 1 <= m <= 12:
                return y, m
        except (TypeError, ValueError):
            pass
        rows = self.store.usage_all()  # ORDER BY day DESC
        if rows:
            try:
                d = date.fromisoformat(str(rows[-1]["day"]))
                return d.year, d.month
            except (ValueError, KeyError, IndexError):
                pass
        t = datetime.now(SHANGHAI).date()
        return t.year, t.month


    async def billing_snapshot(self, refresh: bool = False, min_interval: float = 60.0) -> dict:
        """账单快照：官方余额 + 余额差值推算的真实消费 + 平台逐日消费 + 本地估算。

        **口径优先级**（``source`` 字段）：
          ``platform``      平台明细，带「哪个模型花了多少」，但要有浏览器登录态 token；
          ``balance_diff``  余额差值法 —— 官方余额的下降量累加。**不需要额外凭据**，
                            余额接口本来每次刷新都要查；代价是分辨率只到 ¥0.01、
                            且只能给总额（v0.5.1 新增）；
          ``estimate``      本地按 token 用量估算，是**兜底**，通常偏低。

        为什么必须区分口径：本地只按 token 用量估算，和平台账单对不上是常态
        （重试后服务端已处理、但我们没拿到响应体的那些请求无法计数）。
        用户看到的数字必须是「有出处的」：抓到了就标来源，没抓到就标 estimate
        并把失败原因带上 —— 不许把估算值伪装成真实值。

        三张凭据互不通用（见 billing.py）：余额用 API Key，消费明细用平台 token，
        差值法只要读数历史。任何一路失败都不影响另外两路。
        """
        bc = self.cfg.get("billing") or {}
        cache_s = float(bc.get("cache_seconds", 600) or 600)
        now = time.time()
        age = now - self._billing_ts
        if self._billing_data and (
            (refresh and age < min_interval) or (not refresh and age < cache_s)
        ):
            return self._billing_data

        if self._billing_lock is None:
            self._billing_lock = asyncio.Lock()
        async with self._billing_lock:
            # 等锁期间可能已被别的协程刷过
            age = time.time() - self._billing_ts
            if self._billing_data and age < min_interval:
                return self._billing_data

            est_total_row = self.store.cost_totals()
            est_today = float(self.store.daily_cost_cny(datetime.now(SHANGHAI).date().isoformat()))
            est_total = float(est_total_row.get("cost_cny", 0.0))

            if "api" in self.cfg:
                # 新接入只统计本机请求用量，不把其他平台密钥发送到 DeepSeek 账单接口。
                data = {"ts": int(now), "source": "estimate", "today_source": "estimate",
                        "total_source": "estimate", "today_cny": est_today, "total_cny": est_total,
                        "currency": "CNY", "days": 0,
                        "platform_error": "按请求返回 Token 与配置费率估算；账户余额请查看官方平台",
                        "estimate": {"today_cny": est_today, "total_cny": est_total,
                                     "calls": int(est_total_row.get("calls", 0))},
                        "balance": {"ok": False, "error": "未查询官方余额"}}
                self._billing_ts, self._billing_data = now, data
                return data

            # --- 真实余额（官方接口，用 API Key）---
            token_key = api_key(self.cfg)
            timeout = float(bc.get("timeout_seconds", 20) or 20)
            if token_key:
                try:
                    bal = await billing.fetch_balance(token_key, self.cfg["llm"]["base_url"], timeout)
                except Exception as e:  # noqa: BLE001
                    bal = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            else:
                bal = {"ok": False, "error": "未配置 API Key"}

            # 抓到余额就**落一条读数** —— 这是差值法唯一的原料。
            # 只存不花：不额外发请求（余额本来每次刷新都要查）。
            if bal.get("ok"):
                try:
                    self.store.record_balance(
                        int(time.time()),
                        float(bal.get("total") or 0),
                        float(bal.get("topped_up") or 0),
                        float(bal.get("granted") or 0),
                    )
                except Exception as e:  # noqa: BLE001
                    log.debug("余额读数落库失败（不影响账单显示）: %s", e)

            # --- 真实消费（余额差值法：不需要任何额外凭据）---
            today_str = datetime.now(SHANGHAI).date().isoformat()
            diff_today = diff_total = None
            diff_first_day = ""
            prior_est = 0.0
            try:
                rows = self.store.balance_readings()
                diff_today = billing.daily_from_readings(rows, today_str)
                diff_total = billing.consumption_from_readings(rows)
                if diff_total.get("points"):
                    diff_first_day = time.strftime(
                        "%Y-%m-%d", time.localtime(int(diff_total["from_ts"]))
                    )
                    # 差值看不到起始日之前的历史，用那部分估算把区间补齐。
                    # 否则刚装上的那一天「累计」会显示 ¥0.00 —— 比估算还离谱。
                    if diff_first_day < today_str:
                        prior_est = float(self.store.cost_before(diff_first_day))
            except Exception as e:  # noqa: BLE001
                log.debug("余额差值推算失败: %s", e)

            # --- 真实消费（平台前端接口，用登录态 token）---
            plat_days: dict[str, float] = {}
            plat_currency = "CNY"
            plat_err = ""
            plat_token = str(bc.get("platform_token") or "").strip()
            if not bc.get("platform_enabled", True):
                plat_err = "配置里已关闭平台数据（[billing] platform_enabled = false）"
            elif not plat_token:
                plat_err = "未配置 platform_token（消费明细拿不到；今日/累计已改用余额差值推算）"
            else:
                y, m = self._billing_start(bc)
                try:
                    r = await billing.fetch_platform_cost(plat_token, y, m, timeout)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                if r.get("ok"):
                    plat_days = r.get("days") or {}
                    plat_currency = r.get("currency") or "CNY"
                else:
                    plat_err = str(r.get("error") or "抓取失败")

            today = today_str
            # 口径优先级：平台明细 > 余额差值 > 本地估算。
            # 差值法是「拿官方数字做减法」，比按 token 估算硬得多，所以排在估算前面。
            # 今日与累计**分别**判定：差值法可能已经攒够累计的读数、但今天刚启动
            # 所以今日还不够 —— 两个数字必须各自标自己的口径，不能共用。
            diff_ok_today = bool(
                diff_today and diff_today.get("enough") and diff_today.get("full_day")
            )
            # 「累计」还要求读数跨过了一个午夜（起点日 < 今天）。读数起点就是今天时，
            # 差值只覆盖了今天的一小段，报出来会明显偏小 —— 那种情况下今天先沿用估算，
            # 明天 00:00 之后自然切换到差值口径。
            diff_ok_total = bool(
                diff_total and diff_total.get("enough") and diff_first_day
                and diff_first_day < today_str
            )
            if plat_days:
                today_cny, today_src = float(plat_days.get(today, 0.0)), "platform"
                total_cny, total_src = float(sum(plat_days.values())), "platform"
            else:
                if diff_ok_today:
                    today_cny, today_src = float(diff_today["spent"]), "balance_diff"
                else:
                    today_cny, today_src = est_today, "estimate"
                if diff_ok_total:
                    # 累计 = 起点日之前的历史估算 + 起点之后的差值。
                    # prior_est > 0 说明这个数字是「真实 + 估算」拼的，必须标出来。
                    total_cny = prior_est + float(diff_total["spent"])
                    total_src = "balance_diff"
                else:
                    total_cny, total_src = est_total, "estimate"
            # source = 取数最好的那一路（保留旧字段，界面/测试仍在用）
            for cand in ("platform", "balance_diff", "estimate"):
                if cand in (today_src, total_src):
                    source = cand
                    break

            data = {
                "ts": int(time.time()),
                "source": source,
                "today_source": today_src,
                "total_source": total_src,
                "today_cny": round(today_cny, 4),
                "total_cny": round(total_cny, 4),
                "currency": plat_currency,
                "days": len(plat_days),
                "platform_error": plat_err,
                "estimate": {
                    "today_cny": round(est_today, 4),
                    "total_cny": round(est_total, 4),
                    "calls": int(est_total_row.get("calls", 0)),
                },
                # 余额差值法：真实消费的**下限**（分辨率 ¥0.01）+ 覆盖起点。
                # enough=False 表示读数还不够（程序刚开始跑），界面要回落到估算。
                # today_full_day=False 表示今天的起点离 00:00 太远（首次安装那天
                # 就是这样），今日也会回落估算 —— 否则会报出一个「只算了几小时」
                # 的小数字，用户只会以为程序坏了。
                "balance_diff": {
                    "today_cny": round(float(diff_today["spent"]), 4) if diff_today else 0.0,
                    "total_cny": round(float(diff_total["spent"]), 4) if diff_total else 0.0,
                    "enough": diff_ok_total,
                    "today_enough": bool(diff_today and diff_today.get("enough")),
                    "today_full_day": bool(diff_today and diff_today.get("full_day")),
                    "points": int(diff_total["points"]) if diff_total else 0,
                    "resolution": 0.01,
                    "today_from": int(diff_today["from_ts"]) if diff_today else 0,
                    "total_from": int(diff_total["from_ts"]) if diff_total else 0,
                    # 起点之前那段是估算的：>0 说明累计值是「差值 + 估算」拼出来的，
                    # 界面必须照实标，不能让用户以为整个数字都来自官方余额。
                    "prior_est_cny": round(prior_est, 4),
                    "mixed": prior_est > 0,
                    "from_day": diff_first_day,
                },
                "balance": {
                    "ok": bool(bal.get("ok")),
                    "total": round(float(bal.get("total") or 0), 4),
                    "currency": bal.get("currency") or "CNY",
                    "error": str(bal.get("error") or ""),
                },
            }
            if plat_days:
                data["from_day"] = min(plat_days)
            self._billing_ts = time.time()
            self._billing_data = data
            log.info(
                "账单快照（%s）：今日 %.4f / 累计 %.4f %s%s",
                source, data["today_cny"], data["total_cny"], data["currency"],
                f"（{plat_err}）" if plat_err else "",
            )
            return data


    async def _billing_loop(self) -> None:
        """定期刷新账单快照。界面每 2 秒轮询 /health，抓取动作绝不能挂在它上面。"""
        try:
            await asyncio.sleep(20)  # 让启动期的多模态探测先跑完
        except asyncio.CancelledError:
            raise
        while not self._stop.is_set():
            try:
                await self.billing_snapshot(refresh=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("账单刷新失败（不影响消息接收）: %s", e)
            wait = max(60, int(self.cfg["billing"].get("cache_seconds", 600)))
            # 额外盯一次「刚过午夜」：差值法的「今日消费」以当天第一条读数为起点，
            # 若午夜过后要等满一个周期才读，那段时间的消费就被算进昨天了。
            # 取「下个周期」与「午夜 +5 秒」中较早的那个。
            lt = time.localtime()
            wait = min(wait, 86400 - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec) + 5)
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                raise
