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


def _hit_spans(pattern, text):
    # 捕获组也能给出零宽前瞻内部的命中。常用的「.*(?:甲|乙)」只突出末端词。
    tail = re.search(r"\(\?:([^()]+)\)\$?$", pattern.pattern)
    suffixes = []
    if tail and re.fullmatch(r"[\w\s|.\\\-]+", tail[1]):
        suffixes = [word.replace(r"\.", ".") for word in tail[1].split("|") if word]
    for match in pattern.finditer(text):
        captures = [span for span in match.regs[1:] if span[1] > span[0]]
        if captures:
            yield from captures
        elif match.end() > match.start():
            suffix = next((word for word in suffixes if match[0].lower().endswith(word.lower())), "")
            yield (match.end() - len(suffix), match.end()) if suffix else match.span()
        elif not pattern.groups:
            break  # 只断言位置的规则没有可标绿的正文。


def keyword_preview(rules, row):
    fields = [("", row.get("plain_text") or row.get("text") or "")]
    for item in [row, *(row.get("images") or [])]:
        fields.extend((label, item.get(key) or "") for label, key in (("OCR · ", "ocr_text"), ("图片识别 · ", "vision_text")))
    fields = list(dict.fromkeys((label, value) for label, value in fields if value))
    hits = [[] for _ in fields]
    labels = []
    for rule, pattern in rules:
        if rule not in row["keyword_matches"]:
            continue
        examples = []
        for index, (_, text) in enumerate(fields):
            for start, end in _hit_spans(pattern, text):
                example = text[start:end]
                if example not in examples and len(examples) < 4:
                    examples.append(example[:32])
                hits[index].append((start, end))
                if len(hits[index]) >= 100:
                    break
        labels.append({"rule": rule, "examples": examples})

    # 取实际命中所在字段，留出前后文，逐图 OCR 也能直接看到。
    index = next((i for i, spans in enumerate(hits) if spans), 0)
    prefix, text = fields[index] if fields else ("", "（无文本）")
    spans = sorted(hits[index]) if hits else []
    start = max(0, spans[0][0] - 70) if spans else 0
    end = min(len(text), start + 280)
    parts = [(prefix + ("…" if start else ""), False)]
    pos = start
    for a, b in spans:
        if a >= end:
            break
        if b <= pos:
            continue
        if pos < a:
            parts.append((text[pos:a], False))
        parts.append((text[max(a, pos):min(b, end)], True))
        pos = min(b, end)
    parts.append((text[pos:end] + ("…" if end < len(text) else ""), False))
    return {"keyword_excerpt": parts, "keyword_labels": labels}
