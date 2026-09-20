from __future__ import annotations

import os
import tomllib
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent      # <项目根>/src/msgx
SRC_DIR = PACKAGE_DIR.parent                       # <项目根>/src
# 项目根 = src 的上一层。用 src 布局后这里是三级上溯，不是两级 —— 少写一层
# 会让 config.toml、data/ 全部解析到 src/ 下面，表现为「配置读不到、数据库凭空重建」。
# 允许用环境变量覆盖，便于把包安装到别处时仍能定位数据目录。
ROOT = Path(os.environ["MSGX_ROOT"]) if os.environ.get("MSGX_ROOT") else SRC_DIR.parent

DEFAULTS: dict = {
    "app": {
        "db_path": "data/msgx.db",
        "media_dir": "data/media",
        "report_dir": "data/reports",
        "log_level": "INFO",
    },
    "onebot": {
        "host": "127.0.0.1",
        "ws_port": 6700,
        "http_port": 6701,
        "access_token": "",
    },
    "watch": {"groups": [], "names": {}, "at_qq": []},
    "llm": {
        "enabled": True,
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "vision_model": "deepseek-flash",
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_key": "",
        "max_daily_cost_cny": 10.0,
        # DeepSeek 官方峰谷 + 缓存命中的三档计价（元/百万 token，空闲时段价）
        "price_cache_hit_cny_per_mtok": 0.02,
        "price_cache_miss_cny_per_mtok": 1.00,
        "price_output_cny_per_mtok": 4.00,
        "peak_multiplier": 2.0,
        "peak_windows_utc": "01:00-04:00,06:00-10:00",
        "timeout_seconds": 90,
    },
    "media": {
        "ocr_enabled": True,
        "vision_enabled": True,
        "max_download_mb": 20,
        "images_per_minute": 60,
    },
    "summary": {
        "interval_minutes": 30,
        "daily_report_at": "22:00",
        "chunk_char_budget": 6000,
        "min_messages": 8,
    },
    "score": {
        "keywords": [],
        "link_domains": [],
        "p0_min_score": 8,
        "p1_min_score": 5,
        "llm_review_min_score": 2,
    },
    "notify": {
        "backend": "auto",
        "sound": True,
        "webhook_url": "",
        "webhook_type": "wecom",
    },
    # 协议端（NapCat）。本服务只监听协议端连入，**不负责启动它**，所以默认都是空的。
    #   start_script —— 填上后界面出现「启动协议端(NapCat)」按钮
    #   dir          —— NapCat 安装目录，用于自动弹出登录二维码（<dir>\cache\qrcode.png）
    "napcat": {"start_script": "", "dir": ""},
    # 消息类型过滤。默认全部为 false —— 过滤是界面（0.2.0）引入的能力，
    # 默认值必须保持「与 0.1.0 行为完全一致」：什么都不丢。
    "filters": {
        "skip_face_only": False,
        "skip_image_only": False,
        "skip_forward": False,
        "skip_record": False,
        "min_text_length": 0,
    },
    "retention": {"keep_days_raw": 90, "keep_days_media": 30},
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | Path | None = None) -> dict:
    cfg_path = Path(path) if path else ROOT / "config.toml"
    user: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "rb") as f:
            user = tomllib.load(f)
    cfg = _merge(DEFAULTS, user)

    # 叠加界面偏好。只有 GUI 启动的子进程会设置 MSGX_PREFS 环境变量，
    # 直接跑命令行时不设，因此 CLI 行为完全不受影响。
    # 这样界面改设置无需改写受 Git 管理的 config.toml，多人协作不会冲突。
    from .prefs import apply_to_config, load_from_env

    prefs = load_from_env()
    if prefs:
        cfg = apply_to_config(cfg, prefs)
    return cfg


def api_key(cfg: dict) -> str:
    llm = cfg["llm"]
    return os.environ.get(llm.get("api_key_env", "DEEPSEEK_API_KEY"), "") or llm.get("api_key", "")


def abs_path(cfg: dict, key: str) -> Path:
    p = Path(cfg["app"][key])
    if not p.is_absolute():
        p = ROOT / p
    return p
