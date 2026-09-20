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
        # 上传前把图片缩到长边不超过这个像素。请求里带的是 detail="low"，
        # 服务端本来就会大幅降采样，客户端先缩可省 30 倍带宽且看不出差别。
        "vision_max_side": 1280,
        "vision_jpeg_quality": 85,
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
    #   start_script —— 填上后界面出现「启动协议端(NapCat)」按钮（可见排障模式用）
    #   dir          —— NapCat 安装目录。隐藏启动靠它定位；登录二维码也从这里找
    #                   （<dir>\cache\qrcode.png）；NapCat 自身日志在 <dir>\logs\
    #   hidden       —— true = 隐藏控制台窗口启动（默认）。false / 界面勾「可见模式」时
    #                   回到 os.startfile(批处理) 的老行为，能看到完整输出
    #   log_file     —— 界面「协议端日志」展示的启动记录（相对项目根）
    #   qq_path      —— QQ.exe 路径。留空 = 从注册表 UninstallString 自动推导
    "napcat": {
        "start_script": "",
        "dir": "",
        "hidden": True,
        "log_file": "data/logs/napcat.log",
        "qq_path": "",
    },
    # 真实账单数据源。余额和消费用的是**两套凭证**（见 billing.py）：
    #   [llm] api_key  → 官方 GET /user/balance，查真实余额（始终可用）
    #   platform_token → 平台前端接口 /api/v0/usage/cost，查逐日真实消费
    #                    取自浏览器登录 platform.deepseek.com 后的 localStorage.userToken
    # 拿不到 platform_token 时只显示本地估算，界面会明确标注数据来源。
    "billing": {
        "platform_token": "",
        "platform_enabled": True,
        "cache_seconds": 600,   # 快照缓存时长，避免界面每次轮询都打网络
        "start_year": 0,        # 0 = 用库里最早有记录的那一天作为起点
        "start_month": 0,
        "timeout_seconds": 20,
    },
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
