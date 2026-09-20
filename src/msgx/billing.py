"""DeepSeek 账单数据源。

这里要讲清一件很容易搞混的事：**查余额和查消费用的是两套完全不同的凭证**。

| | API Key（`sk-…`） | 平台登录态（`userToken`） |
|---|---|---|
| 来源 | 开放平台「API Keys」页创建 | 浏览器登录后 platform.deepseek.com 的 `localStorage.userToken` |
| 能查什么 | 只有余额（官方文档化的 `GET /user/balance`） | 消费明细（`/api/v0/usage/cost`） |
| 有效期 | 长期 | 短期，过期要重新登录取 |
| 互换 | **互不通用** —— 用 API Key 打用量接口返回 `code=40003 Authorization Failed` |

所以本模块分两条独立通路：
  * `fetch_balance()` —— 用 API Key，拿**真实余额**（始终可用）
  * `fetch_platform_cost()` —— 用平台 token，拿**真实消费**（需用户粘贴一次 token）

> 平台那两个用量接口是**前端内部接口**，未对外承诺、字段随时可能变。
> 因此这里做两件事：① 解析层尽量容错；② 拿不到就明确返回失败原因，
> 由上层回退到「本地 token 估算」，**绝不假装数据是准确的**。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

log = logging.getLogger("msgx.billing")

BALANCE_URL = "https://api.deepseek.com/user/balance"
PLATFORM_BASE = "https://platform.deepseek.com"
# 平台前端内部接口。x-app-version 是伪装成网页端请求用的，过期时改这里。
PLATFORM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PLATFORM_APP_VERSION = "20240101.1"

AUTH_ERROR_CODES = (40002, 40003)  # 平台把「会话失效」统一报成这两个


class PlatformAuthError(RuntimeError):
    """平台 token 失效（不是网络问题，重试无用）。"""


# ---------------------------------------------------------------- 余额

async def fetch_balance(
    api_key: str, base_url: str = "https://api.deepseek.com", timeout: float = 20.0
) -> dict:
    """官方余额接口。**这是唯一有公开文档的账单类接口**。

    返回 ``{"ok":bool, "currency":str, "total":float, "granted":float,
    "topped_up":float, "is_available":bool, "error":str}``
    """
    out = {
        "ok": False,
        "currency": "CNY",
        "total": 0.0,
        "granted": 0.0,
        "topped_up": 0.0,
        "is_available": False,
        "error": "",
    }
    if not api_key:
        out["error"] = "未配置 API Key"
        return out
    url = base_url.rstrip("/") + "/user/balance"
    try:
        data = await _get_json(url, {"Authorization": f"Bearer {api_key}"}, timeout)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    try:
        infos = data.get("balance_infos") or []
        if not infos:
            out["error"] = "余额接口返回空"
            return out
        # 多币种时优先取有余额的那条
        pick = next((i for i in infos if _num(i.get("total_balance")) > 0), infos[0])
        out.update(
            ok=True,
            currency=str(pick.get("currency") or "CNY"),
            total=_num(pick.get("total_balance")),
            granted=_num(pick.get("granted_balance")),
            topped_up=_num(pick.get("topped_up_balance")),
            is_available=bool(data.get("is_available")),
        )
    except Exception as e:  # noqa: BLE001
        out["error"] = f"解析失败: {e}"
    return out


# ---------------------------------------------------------------- 平台消费

async def fetch_platform_cost(
    token: str,
    start_year: int,
    start_month: int,
    timeout: float = 20.0,
) -> dict:
    """逐月抓取平台消费明细，聚合成「逐日消费表」。

    平台只提供**按月**查询，所以累计值只能靠逐月累加。这里从 ``start_year-start_month``
    一直查到当月；账户开始用之前的那几个月会返回空，代价只是几次很小的请求。

    返回 ``{"ok", "days": {"YYYY-MM-DD": 金额}, "currency", "months_ok",
    "months_failed": [...], "error"}``
    """
    out: dict[str, Any] = {
        "ok": False,
        "days": {},
        "currency": "CNY",
        "months_ok": 0,
        "months_failed": [],
        "error": "",
    }
    if not token:
        out["error"] = "未配置平台 token"
        return out

    now = time.localtime()
    pairs = _month_range(start_year, start_month, now.tm_year, now.tm_mon)
    if not pairs:
        out["error"] = "起始月份晚于当前月份"
        return out

    auth_failed = False
    for y, m in pairs:
        try:
            raw = await _get_json(
                f"{PLATFORM_BASE}/api/v0/usage/cost?month={m}&year={y}",
                {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": PLATFORM_UA,
                    "x-app-version": PLATFORM_APP_VERSION,
                },
                timeout,
            )
        except PlatformAuthError as e:
            auth_failed = True
            out["error"] = f"平台 token 已失效，请重新复制：{e}"
            break
        except Exception as e:  # noqa: BLE001
            out["months_failed"].append(f"{y}-{m:02d}")
            log.warning("平台用量 %s-%02d 抓取失败: %s", y, m, e)
            continue

        try:
            days, currency = parse_cost_response(raw)
        except PlatformAuthError as e:
            auth_failed = True
            out["error"] = f"平台 token 已失效，请重新复制：{e}"
            break
        except Exception as e:  # noqa: BLE001
            out["months_failed"].append(f"{y}-{m:02d}")
            log.warning("平台用量 %s-%02d 解析失败: %s | 原始: %s", y, m, e, json.dumps(raw)[:500])
            continue

        if currency:
            out["currency"] = currency
        for d, v in days.items():
            out["days"][d] = round(out["days"].get(d, 0.0) + v, 6)
        out["months_ok"] += 1

    # 只要有一部分月份拿到、且不是 token 失效，就算成功（缺失月份会在 months_failed 里）
    if not auth_failed:
        out["ok"] = bool(out["months_ok"]) and not out["months_failed"]
        if out["months_failed"] and not out["error"]:
            out["error"] = f"{len(out['months_failed'])} 个月份抓取失败"
    return out


def parse_cost_response(raw: dict) -> tuple[dict[str, float], str]:
    """解析 ``/api/v0/usage/cost`` 的响应。

    平台响应有两层包裹（顶层 ``code`` 与业务层 ``biz_code``），实测形态：

        {"code":0,"msg":"","data":{"biz_code":0,"biz_msg":"",
            "biz_data":[{"currency":"CNY",
                         "total":[...],
                         "days":[{"date":"2026-09-20",
                                  "data":[{"model":"deepseek-flash",
                                           "usage":[{"type":"...","amount":0.99}]}]}]}]}}

    ``biz_data`` 有时是数组、有时是对象，``amount`` 有时是数字、有时是字符串 ——
    两种都兼容。拿不到结构就抛异常，由上层记日志并回退本地估算。
    """
    _raise_if_auth_error(raw)
    data = raw.get("data") or {}
    if isinstance(data, dict):
        _raise_if_auth_error(data)
        biz = data.get("biz_data")
    else:
        biz = data
    if isinstance(biz, list):
        biz = biz[0] if biz else None
    if not isinstance(biz, dict):
        raise ValueError("biz_data 结构不认识")

    currency = str(biz.get("currency") or "")
    days: dict[str, float] = {}
    for day in biz.get("days") or []:
        if not isinstance(day, dict):
            continue
        date = str(day.get("date") or "").strip()
        if not date:
            continue
        total = 0.0
        for entry in day.get("data") or []:
            total += _sum_usage(entry)
        # 有的形态把当天金额直接挂在 day 上
        if not total:
            total = _num(day.get("cost") or day.get("amount"))
        days[date] = round(days.get(date, 0.0) + total, 6)
    return days, currency


# ---------------------------------------------------------------- 内部工具

def _sum_usage(entry: Any) -> float:
    """一个 ``{"model":..., "usage":[{"type":...,"amount":...}]}`` 的金额合计。"""
    if not isinstance(entry, dict):
        return 0.0
    total = 0.0
    usage = entry.get("usage")
    if isinstance(usage, list):
        for item in usage:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").upper() == "REQUEST":
                continue  # 请求次数不是钱
            total += _num(item.get("amount") or item.get("cost") or item.get("value"))
    if not total:
        # 兜底：直接挂在 entry 上的金额字段
        total = _num(entry.get("cost") or entry.get("amount"))
    return total


def _raise_if_auth_error(payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    for key in ("code", "biz_code"):
        try:
            code = int(payload.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if code in AUTH_ERROR_CODES:
            raise PlatformAuthError(str(payload.get("msg") or payload.get("biz_msg") or code))


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _month_range(y1: int, m1: int, y2: int, m2: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    y, m = int(y1), int(m1)
    end = int(y2) * 12 + int(m2)
    while y * 12 + m <= end:
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
        if len(out) > 240:  # 防呆：不会真跑到这里
            break
    return out


async def _get_json(url: str, headers: dict, timeout: float) -> dict:
    """GET 一个 JSON 接口。

    平台的前端接口会把「token 失效」包在 200 响应体里（``code=40003``），
    所以 HTTP 200 不代表成功，必须在解析层再看一遍业务码。
    """
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as s:
            async with s.get(url, headers=headers) as r:
                body = await r.text()
                if r.status in (401, 403):
                    raise PlatformAuthError(f"HTTP {r.status}")
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}: {body[:200]}")
                return json.loads(body)
    except asyncio.TimeoutError as e:
        raise RuntimeError("请求超时") from e
