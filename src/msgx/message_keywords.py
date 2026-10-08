"""本地关键词规则；只匹配已有文字，不触发 OCR 或模型调用。"""
from __future__ import annotations

import re


def compile_rules(lines: list[str]) -> tuple:
    rules = []
    seen = set()
    for number, line in enumerate(lines, 1):
        label = line.strip()
        if not label or label in seen:
            continue
        expression = label[3:] if label.startswith("re:") else re.escape(label)
        if not expression:
            raise ValueError(f"第 {number} 行正则表达式不能为空")
        try:
            pattern = re.compile(expression, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"第 {number} 行正则表达式无效：{exc}") from exc
        rules.append((label, pattern))
        seen.add(label)
    return tuple(rules)


def matching_fields(rules: tuple, *fields: str) -> list[str]:
    return [label for label, pattern in rules
            if any(field and pattern.search(field) for field in fields)]


def matching_message(rules: tuple, row: dict) -> list[str]:
    fields = [row.get(key) or "" for key in ("text", "ocr_text", "vision_text")]
    for image in row.get("images") or []:
        fields.extend((image.get("ocr_text") or "", image.get("vision_text") or ""))
    return matching_fields(rules, *fields)
