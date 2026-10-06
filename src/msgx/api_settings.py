"""可导出的 API 设置。这里永远不包含密钥。"""
from __future__ import annotations

import math
import re
from urllib.parse import urlsplit, urlunsplit

DEFAULTS = {
    "provider": "OpenAI 官方", "base_url": "https://api.openai.com/v1",
    "protocol": "responses", "model": "gpt-6-luna", "reasoning": "none",
    "service_tier": "flex",
    "context_tokens": 1050000, "max_output_tokens": 128000,
    "input_price": 0.10, "cached_price": 0.01, "output_price": 0.50,
    "currency": "USD", "usd_cny": 7.0,
    "long_context_threshold": 272000, "long_input_multiplier": 2.0,
    "long_output_multiplier": 1.5,
    "peak_multiplier": 1.0, "peak_windows_utc": "",
}


def endpoint(value: str) -> str:
    try:
        u = urlsplit(value.strip())
        if ("sk-" in value or any(c.isspace() for c in value.strip())
                or u.scheme != "https" or not u.hostname or u.username is not None
                or u.password is not None or u.query or u.fragment):
            raise ValueError
        port = u.port
        host = u.hostname.encode("idna").decode().lower()
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{port}" if port and port != 443 else "")
        return urlunsplit(("https", netloc, u.path.rstrip("/"), "", ""))
    except (ValueError, UnicodeError):
        raise ValueError("API 地址必须是 HTTPS 基础地址，不能包含账号、密钥、查询参数或片段") from None


def validate(values: dict) -> dict:
    if not isinstance(values, dict):
        raise ValueError("API 配置必须是对象")
    if set(values) - set(DEFAULTS):
        raise ValueError("API 配置包含未知字段；密钥必须在程序中单独保存")
    out = {**DEFAULTS, **values}
    out["base_url"] = endpoint(str(out["base_url"]))
    if "service_tier" not in values and urlsplit(out["base_url"]).hostname != "api.openai.com":
        out["service_tier"] = "default"
    if out["service_tier"] not in ("default", "flex"):
        raise ValueError("服务档位必须是 default（Standard）或 flex")
    for key in ("provider", "model"):
        text = str(out[key]).strip()
        if not text or len(text) > 100 or any(c in text for c in "\r\n") or "sk-" in text:
            raise ValueError("服务名称或模型名称无效，请勿填写密钥")
        out[key] = text
    if out["protocol"] not in ("responses", "chat_completions"):
        raise ValueError("接口格式必须是 responses 或 chat_completions")
    if out["reasoning"] not in ("none", "low", "medium", "high", "xhigh", "max"):
        raise ValueError("思考强度无效")
    if out["currency"] not in ("USD", "CNY"):
        raise ValueError("计价币种必须是 USD 或 CNY")
    windows = str(out["peak_windows_utc"]).strip()
    if windows and not re.fullmatch(r"(?:[0-1]\d|2[0-3]):[0-5]\d-(?:[0-1]\d|2[0-3]):[0-5]\d(?:,(?:[0-1]\d|2[0-3]):[0-5]\d-(?:[0-1]\d|2[0-3]):[0-5]\d)*", windows):
        raise ValueError("高峰时段格式应为 HH:MM-HH:MM，多个时段用英文逗号分隔")
    out["peak_windows_utc"] = windows
    try:
        for key in ("context_tokens", "max_output_tokens", "long_context_threshold"):
            out[key] = int(out[key])
            if out[key] < (0 if key == "long_context_threshold" else 1):
                raise ValueError
        for key in ("input_price", "cached_price", "output_price", "usd_cny", "long_input_multiplier", "long_output_multiplier", "peak_multiplier"):
            out[key] = float(out[key])
            if not math.isfinite(out[key]) or out[key] < 0 or (key not in ("input_price", "cached_price", "output_price") and out[key] == 0):
                raise ValueError
        if out["max_output_tokens"] >= out["context_tokens"]:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Token 上限、费率或汇率无效；输出上限必须小于上下文") from None
    return out


def apply(cfg: dict, values: dict) -> dict:
    profile = validate(values)
    out = dict(cfg)
    out["api"] = profile
    out["llm"] = {**cfg["llm"], "base_url": profile["base_url"], "model": profile["model"],
                  "vision_model": profile["model"], "protocol": profile["protocol"],
                  "peak_multiplier": profile["peak_multiplier"], "peak_windows_utc": profile["peak_windows_utc"],
                  "api_key": "", "api_key_env": "", "credential_store": True}
    return out


def deepseek_profile(llm: dict) -> dict:
    """只迁移旧配置的公开参数，不读取或复制旧密钥。"""
    return validate({**DEFAULTS, "provider": "DeepSeek", "base_url": llm["base_url"],
                     "model": llm["model"], "protocol": "chat_completions", "service_tier": "default",
                     "context_tokens": 1000000, "max_output_tokens": 393216, "currency": "CNY",
                     "input_price": llm["price_cache_miss_cny_per_mtok"],
                     "cached_price": llm["price_cache_hit_cny_per_mtok"],
                     "output_price": llm["price_output_cny_per_mtok"], "long_context_threshold": 0,
                     "peak_multiplier": llm.get("peak_multiplier", 2), "peak_windows_utc": llm.get("peak_windows_utc", ""),
                     "long_input_multiplier": 1, "long_output_multiplier": 1})


def catalog(cfg: dict, prefs: dict) -> tuple[dict, str]:
    from .config import DEFAULTS as CONFIG_DEFAULTS
    profiles = {"openai": dict(DEFAULTS), "deepseek": deepseek_profile(CONFIG_DEFAULTS["llm"])}
    profiles.update(cfg.get("api_profiles", {}))
    saved = prefs.get("api_profiles")
    if saved is not None:
        if not isinstance(saved, dict) or not saved:
            raise ValueError("API 配置列表无效")
        profiles.update(saved)
    elif "api" in prefs:
        # 兼容初版 0.7.0 的单配置；自定义地址也原样保留。
        profiles["openai"] = prefs["api"]
    profiles = {name: validate(value) for name, value in profiles.items()}
    if any(not isinstance(name, str) or not name.isidentifier() or len(name) > 64 for name in profiles):
        raise ValueError("API 配置标识无效")
    active = prefs.get("active_api_profile", cfg.get("active_api_profile", "openai"))
    if active not in profiles:
        raise ValueError("所选 API 配置不存在")
    return profiles, active
