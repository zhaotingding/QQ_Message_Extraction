"""低成本图片初筛：仅处理本地 OCR 和有限上下文，不加载额外模型。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageDecision:
    score: int
    reason: str
    blocked: bool = False


def is_sticker(raw: str, url: str = "") -> bool:
    try:
        segments = json.loads(raw or "{}").get("message", [])  # 使用协议类型，不凭图片尺寸猜测表情。
    except (ValueError, AttributeError):
        return False
    if isinstance(segments, str):
        return "[CQ:mface," in segments and "[CQ:image," not in segments
    for segment in segments if isinstance(segments, list) else []:
        data = segment.get("data") or {}
        if segment.get("type") == "mface" and (not url or data.get("url") == url):
            return True
        if segment.get("type") == "image" and str(data.get("sub_type", "")) == "1" and data.get("url") == url:
            return True
    return False


def screen_image(ocr: str, context: str, *, sticker: bool = False, animated: bool = False) -> ImageDecision:
    if sticker or animated:
        return ImageDecision(0, "协议表情包或动图，不自动上传", True)
    text = (ocr or "")[:12000]  # 输入有界，避免一张超长截图拖慢规则判断。
    combined = text + "\n" + (context or "")[:4000]
    ad_hits = re.findall(r"代充|合租|返佣|发卡|代理价|现货|库存|货源|开通Plus|充值|购买链接|联系[我客]|出货|收货|报价|低价|包月|售卖", combined, re.I)
    technical = re.search(r"traceback|exception|error[:：]|stack trace|segmentation fault|CUDA.*(?:error|OOM)|RuntimeError|故障复现|报错堆栈", text, re.I)
    if len(ad_hits) >= 2 and not technical:
        return ImageDecision(0, "广告/交易特征：" + "、".join(dict.fromkeys(ad_hits))[:100], True)
    score, reasons = 0, []
    if technical:
        score += 65
        reasons.append("明确报错或可复现诊断")
    if re.search(r"(?:def |class |import |pip install|git clone|docker run|SELECT .+ FROM |[{};].*[{};])", text, re.I):
        score += 45
        reasons.append("代码/命令/配置")
    if re.search(r"benchmark|吞吐|延迟|消融|对照实验|准确率|性能测试|tokens/s|推理速度|架构图|流程图", combined, re.I):
        score += 35
        reasons.append("测量/结构证据")
    if len(text.strip()) >= 100:
        score += 15
        reasons.append("含可读细节")
    if re.search(r"复现|解决|原因|怎么修|测试结果|修复|实现|对比数据", context or ""):
        score += 20
        reasons.append("上下文讨论具体问题")
    if ad_hits:
        score -= 35
        reasons.append("含交易噪声降权")
    score = max(0, min(score, 100))
    return ImageDecision(score, "；".join(reasons) or "缺乏明确技术证据，保留本地查看")


DEFAULT_IMAGE_ANALYSIS = {
    "enabled": False, "threshold": 70, "daily_limit": 20, "daily_budget_cny": 1.0,
    "context_messages": 8, "history_days": 7,
}


def validate_analysis(values: dict) -> dict:
    if not isinstance(values, dict) or set(values) - set(DEFAULT_IMAGE_ANALYSIS):
        raise ValueError("图片初筛设置包含未知字段")
    out = {**DEFAULT_IMAGE_ANALYSIS, **values}
    if not isinstance(out["enabled"], bool):
        raise ValueError("自动图片分析开关必须为布尔值")
    for key, low, high in (("threshold", 1, 100), ("daily_limit", 1, 500), ("context_messages", 1, 20), ("history_days", 1, 30)):
        try:
            out[key] = int(out[key])
            if not low <= out[key] <= high:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"图片参数 {key} 必须为 {low}～{high} 的整数") from None
    import math
    try:
        out["daily_budget_cny"] = float(out["daily_budget_cny"])
        if not math.isfinite(out["daily_budget_cny"]) or out["daily_budget_cny"] <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("自动图片每日预算必须为正数") from None
    return out
