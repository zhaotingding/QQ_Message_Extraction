"""Token 费用纯规则：只读配置和用量；不请求网络、读取密钥或写账本。"""
from __future__ import annotations

import time
from urllib.parse import urlsplit


def cache_write_multiplier(cfg, model: str | None = None) -> float:
    name = model or cfg['llm']["model"]
    official = urlsplit(cfg['llm']["base_url"]).hostname == "api.openai.com"
    return 1.25 if official and name.startswith(("gpt-6", "gpt-5.6")) else 1.0


def is_peak(cfg, ts: float | None = None) -> bool:
    """调用时刻是否处于 DeepSeek 高峰时段。

    官方定义：高峰 = UTC 01:00-04:00 与 06:00-10:00 的**周一至周五**，
    其余时段（含整个周末）为空闲。折北京时间即工作日 09:00-12:00、14:00-18:00。
    """
    t = time.gmtime(time.time() if ts is None else ts)
    if t.tm_wday >= 5:  # 5=周六 6=周日
        return False
    minutes = t.tm_hour * 60 + t.tm_min
    for win in str(cfg['llm'].get("peak_windows_utc", "")).split(","):
        win = win.strip()
        if "-" not in win:
            continue
        a, b = win.split("-", 1)
        try:
            ah, am = (int(x) for x in a.strip().split(":"))
            bh, bm = (int(x) for x in b.strip().split(":"))
        except ValueError:
            continue
        if ah * 60 + am <= minutes < bh * 60 + bm:
            return True
    return False


def cost_cny(
    cfg,
    in_tokens: int,
    out_tokens: int,
    cache_hit_tokens: int = 0,
    ts: float | None = None,
    peak: bool | None = None,
    service_tier: str = "",
    cache_write_tokens: int = 0,
    model: str | None = None,
) -> float:
    """按调用**发生的那一刻**的峰谷价计算人民币金额。

    输入分「缓存命中 / 写入 / 普通输入」，输出单列；
    高峰时段各项都是空闲时段的 peak_multiplier 倍。
    缓存 Token 由 API 返回；缺写入字段时暂按零估算并在账本标注。

    ``peak`` 显式传值可覆盖时间判定 —— 给「只有日期、没有小时」的历史数据
    补算金额时用得到（那就只能按空闲价估，是个下限）。
    """
    if "api" in cfg:
        p = cfg["api"]
        hit = max(0, min(int(cache_hit_tokens), int(in_tokens)))
        written = max(0, min(int(cache_write_tokens), int(in_tokens) - hit))
        long = p["long_context_threshold"] > 0 and in_tokens > p["long_context_threshold"]
        input_mult = p["long_input_multiplier"] if long else 1
        output_mult = p["long_output_multiplier"] if long else 1
        fx = p["usd_cny"] if p["currency"] == "USD" else 1
        peak_mult = p["peak_multiplier"] if (is_peak(cfg, ts) if peak is None else peak) else 1
        tier_mult = {"priority": 2, "fast": 2, "flex": 0.5}.get(service_tier or p.get("service_tier", "default"), 1) if urlsplit(p["base_url"]).hostname == "api.openai.com" else 1
        # 缓存写入属于输入的一部分，按写入价替换普通价，不能重复相加。
        return fx * peak_mult * tier_mult / 1_000_000 * ((hit * p["cached_price"]
                                  + (in_tokens - hit - written) * p["input_price"]
                                  + written * p["input_price"] * cache_write_multiplier(cfg, model)) * input_mult
                                  + out_tokens * p["output_price"] * output_mult)
    mult = float(cfg['llm'].get("peak_multiplier", 2.0)) if (
        is_peak(cfg, ts) if peak is None else bool(peak)
    ) else 1.0
    hit = max(0, min(int(cache_hit_tokens), int(in_tokens)))
    miss = max(0, int(in_tokens) - hit)
    return mult * (
        hit / 1_000_000 * float(cfg['llm'].get("price_cache_hit_cny_per_mtok", 0.02))
        + miss / 1_000_000 * float(cfg['llm'].get("price_cache_miss_cny_per_mtok", 1.00))
        + int(out_tokens) / 1_000_000 * float(cfg['llm'].get("price_output_cny_per_mtok", 4.00))
    )
