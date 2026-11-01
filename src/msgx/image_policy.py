"""低成本图片初筛：仅处理本地 OCR 和有限上下文，不加载额外模型。"""
from __future__ import annotations

import json
import re
import hashlib
from dataclasses import dataclass
from functools import lru_cache


ANALYSIS_DELAY_SECONDS = 15 * 60  # 从首次达到阈值的标记时间起等待后续群聊。

# 2026-10-31：最近14个去重日报的主题命中天数；周报只复核主题，不重复计频。
# 权重 = 16 + min(16, 2 * 命中天数)，同主题重复最多计两次，第二次只加半权。
_REPORT_THEMES = (
    ("Agent与多代理", r"\bagent(?:s|ic)?\b|多\s*agent|多代理|子代理|智能体", 13),
    ("Codex", r"\bcodex\b", 12),
    ("Claude Code", r"\bclaude\s*code\b", 3),
    ("Skill与工作流", r"\bskills?\b|技能|工作流|harness", 12),
    ("MCP与工具调用", r"\bmcp\b|工具调用|function\s*call", 8),
    ("上下文与记忆", r"上下文|context|记忆|压缩", 13),
    ("RAG与检索", r"\brag\b|检索增强|知识库|向量检索", 6),
    ("推理与思考", r"推理|reasoning|thinking|思考预算", 7),
    ("量化与精度", r"量化|\bint[48]\b|\bbf16\b|\bfp[148]+\b", 4),
    ("LoRA与微调", r"\blora\b|微调|蒸馏", 2),
    ("训练与梯度", r"训练|梯度|\bloss\b|学习率|优化器", 1),
    ("性能与延迟", r"吞吐|延迟|性能|tokens?/s|\bfps\b", 11),
    ("本地部署与推理服务", r"本地部署|本地模型|\bvllm\b|\bollama\b|推理服务", 7),
    ("GPU与显存", r"\bcuda\b|\bgpu\b|显存|\boom\b|内存占用", 9),
    ("API与限流", r"\bapi\b|\bresponses?\b|限流|\b429\b|速率限制", 13),
    ("报错与故障", r"traceback|runtimeerror|exception|error[:：]|故障|报错|崩溃", 9),
    ("安全漏洞", r"\bcve[- ]?\d*|漏洞|入侵|勒索|加密勒索", 5),
    ("权限与沙箱", r"权限|沙箱|sandbox|隔离", 12),
    ("版本与恢复", r"\bgit\b|回滚|备份|版本控制|恢复", 11),
    ("缓存与成本", r"缓存|cache|token消耗|成本", 14),
    ("评测与对照", r"评测|benchmark|消融|对照实验|准确率", 5),
    ("数据库与存储", r"数据库|\bredis\b|\bsql\b|持久化|存储", 3),
)


def default_keyword_rules() -> list[dict]:
    return [{"name": name, "mode": "regex", "pattern": pattern, "score": 16 + min(16, 2 * days),
             "enabled": True, "max_hits": 2, "repeat_factor": .5} for name, pattern, days in _REPORT_THEMES]


@lru_cache(maxsize=256)
def _compile_pattern(mode: str, pattern: str):
    return re.compile(re.escape(pattern) if mode == "literal" else pattern, re.I | re.ASCII)  # 普通关键词按字面匹配。


def validate_keyword_rules(values: list | None) -> list[dict]:
    if values is None:
        values = default_keyword_rules()  # 缺省才用初始主题；显式空列表保持停用。
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError("图片主题规则必须是列表，最多100条")
    result = []
    for index, item in enumerate(values, 1):
        if not isinstance(item, dict) or set(item) - {"name", "mode", "pattern", "score", "enabled", "max_hits", "repeat_factor"}:
            raise ValueError(f"第{index}条图片规则格式无效")
        rule = {"enabled": True, "mode": "literal", "max_hits": 2, "repeat_factor": .5, **item}
        for key, maximum in (("name", 80), ("pattern", 500)):
            if not isinstance(rule.get(key), str) or not rule[key].strip() or len(rule[key]) > maximum:
                raise ValueError(f"第{index}条规则的{key}不能为空且最多{maximum}字")
            if key == "name":
                rule[key] = rule[key].strip()  # 关键词／正则中的首尾空白可有匹配意义，原样保留。
        if rule["mode"] not in ("literal", "regex") or not isinstance(rule["enabled"], bool):
            raise ValueError(f"第{index}条规则的匹配模式或启用状态无效")
        try:
            if isinstance(rule.get("score"), bool) or isinstance(rule["max_hits"], bool):
                raise ValueError
            score, hits = float(rule.get("score", -1)), float(rule["max_hits"])
            repeat = float(rule["repeat_factor"])
            if not score.is_integer() or not 0 <= score <= 100 or not hits.is_integer() or not 1 <= hits <= 10 or not 0 <= repeat <= 1:
                raise ValueError
            rule.update(score=int(score), max_hits=int(hits), repeat_factor=repeat)
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f"第{index}条规则分数须为0～100整数，次数1～10，重复倍率0～1") from None
        try:
            pattern = _compile_pattern(rule["mode"], rule["pattern"])
            if pattern.search("") is not None:
                raise ValueError(f"第{index}条规则不能匹配空文字")
        except re.error as exc:
            raise ValueError(f"第{index}条正则无效：{exc}") from None
        result.append(rule)
    return result


def _rule_payload(rules: list | None) -> str:
    return json.dumps(validate_keyword_rules(rules), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@lru_cache(maxsize=16)
def _compiled_rules(payload: str) -> tuple:
    return tuple((item["name"], _compile_pattern(item["mode"], item["pattern"]), item["score"],
                  item["max_hits"], item["repeat_factor"]) for item in json.loads(payload) if item["enabled"])


def compile_keyword_rules(rules: list | None = None) -> tuple:
    return _compiled_rules(_rule_payload(rules))


def rules_version(rules: list | None = None) -> int:
    payload = "image-policy-3:" + _rule_payload(rules)
    return max(1, int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big") & ((1 << 63)-1))


SCREEN_VERSION = rules_version()  # 只有规则实际变化才重筛；指纹在SQLite有符号整数范围内。
KEYWORD_RULES = tuple((name, pattern, score) for name, pattern, score, _, _ in compile_keyword_rules())


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


def screen_image(ocr: str, context: str, *, sticker: bool = False, animated: bool = False,
                 keyword_rules: list | None = None) -> ImageDecision:
    if sticker or animated:
        return ImageDecision(0, "协议表情包或动图，不自动上传", True)
    text = (ocr or "")[:12000]  # 输入有界，避免一张超长截图拖慢规则判断。
    combined = text + "\n" + (context or "")[:4000]
    ad_hits = re.findall(r"代充|合租|返佣|发卡|代理价|现货|库存|货源|开通Plus|充值|购买链接|联系[我客]|出货|收货|报价|低价|包月|售卖", combined, re.I)
    technical = re.search(r"traceback|exception|error[:：]|stack trace|segmentation fault|CUDA.*(?:error|OOM)|RuntimeError|故障复现|报错堆栈", text, re.I)
    if len(ad_hits) >= 2 and not technical:
        return ImageDecision(0, "广告/交易特征：" + "、".join(dict.fromkeys(ad_hits))[:100], True)
    score, reasons = 0, []
    rules = compile_keyword_rules(keyword_rules)
    for name, pattern, weight, max_hits, repeat_factor in rules:
        hits = sum(1 for _ in zip(range(max_hits), pattern.finditer(text)))  # 重复次数按用户上限封顶。
        if hits:
            points = weight + int(weight * repeat_factor) * (hits - 1)
            score += points
            reasons.append(f"{name}×{hits}:{points}")
    if not score:
        return ImageDecision(0, "OCR未命中近期报告主题，保留本地查看")  # 只有字数、只有上下文均不能触发上传。
    if any(pattern.search((context or "")[:4000]) for _, pattern, _, _, _ in rules):
        score += 10
        reasons.append("相关上下文:+10")
    if ad_hits:
        score -= 35
        reasons.append("含交易噪声降权")
    score = max(0, min(score, 100))
    return ImageDecision(score, "；".join(reasons)[:500])


DEFAULT_IMAGE_ANALYSIS = {
    "enabled": False, "threshold": 70, "daily_limit": 20, "daily_budget_cny": 1.0,
    "context_messages": 8, "history_days": 7, "keyword_rules": default_keyword_rules(),
}


def validate_analysis(values: dict) -> dict:
    if not isinstance(values, dict) or set(values) - set(DEFAULT_IMAGE_ANALYSIS):
        raise ValueError("图片初筛设置包含未知字段")
    out = {**DEFAULT_IMAGE_ANALYSIS, **values}
    out["keyword_rules"] = validate_keyword_rules(out["keyword_rules"])
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
