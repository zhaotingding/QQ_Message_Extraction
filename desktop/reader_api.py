"""摘要阅读、朗读和账本的桌面接口，复用现有 SQLite 与 SAPI 实现。"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from msgx import prefs, summary_review, tts
from msgx.config import abs_path
from msgx.summarize import BEIJING, summary_chars
from msgx.ui.common import open_path


KINDS = {"daily": "日报", "weekly": "周报", "monthly": "月报"}
RATES = (-3, 0, 3, 6)
_VOICE_LOCK = threading.Lock()
_REVIEW_LOCK = threading.Lock()


def _blocks(content: str) -> list[dict]:
    """沿用 Tk 阅读页的文本、换行、去星号规则，保存同一份批注坐标。"""
    blocks = []
    has_heading = False
    for line in content.splitlines():
        title = re.match(r"^#\s+(.+)$", line)
        heading = re.match(r"^#{2,6}\s+(.+)$", line)
        if heading is None:
            heading = re.match(r"^(?:(?:[一二三四五六七八九十百]+、|\d+[.、)]\s*)\s*)?\*\*(.+?)\*\*[:：]?$", line.strip())
        if title:
            blocks.append({"kind": "title", "parts": [[title.group(1), False]]})
        elif heading:
            if has_heading:
                blocks.append({"kind": "gap", "parts": []})
            has_heading = True
            text = heading.group(1) if line.startswith("#") else line.strip()
            blocks.append({"kind": "heading", "parts": [[text.replace("**", ""), False]]})
        else:
            blocks.append({"kind": "body", "parts": [
                [part[2:-2] if part.startswith("**") else part, part.startswith("**")]
                for part in re.split(r"(\*\*.+?\*\*)", line) if part
            ]})
    return blocks


def _display_text(blocks: list[dict]) -> str:
    return "".join("".join(text for text, _ in block["parts"]) + "\n" for block in blocks)


def _metadata(item: dict) -> dict:
    content = item["content"] or ""
    start = datetime.fromtimestamp(item["window_start"], BEIJING)
    end = datetime.fromtimestamp(item["window_end"], BEIJING)
    last = end - timedelta(seconds=1)
    fmt = "%m-%d" if start.year == last.year else "%Y-%m-%d"
    label = f"{start:%m-%d}" if item["kind"] == "daily" else f"{start:{fmt}} – {last:{fmt}}"
    title = next((re.sub(r"^#{1,6}\s*|\*\*", "", s).strip()
                  for s in content.splitlines() if s.strip()), "无标题摘要")
    return dict(
        id=item["id"], kind=item["kind"], kind_label=KINDS[item["kind"]],
        title=title, date_label=label, chars=summary_chars(content),
        window=f"{start:%Y-%m-%d %H:%M} 至 {end:%Y-%m-%d %H:%M}",
        read_state=item["review"]["read_state"], content_hash=item["review"]["content_hash"],
    )


def _report(ctx, report_id: int) -> dict:
    with ctx.read_store() as store:
        row = store.conn.execute(
            "SELECT id,window_start,window_end,kind,content,report_path FROM summaries WHERE id=?",
            (report_id,),
        ).fetchone()
        if row is None or row["kind"] not in KINDS:
            raise ValueError("摘要不存在，请刷新列表")
        item = dict(row)
        item["review"] = summary_review.get_review(store.conn, report_id, item["content"] or "")
    return item


def _report_file(ctx, item: dict) -> Path | None:
    if not item["report_path"]:
        return None
    root = abs_path(ctx.cfg, "report_dir").resolve()
    path = Path(item["report_path"]).resolve()
    if path.suffix.lower() == ".html" and path.is_relative_to(root) and path.is_file():
        return path
    return None


def _verify_content(item: dict, payload: dict) -> None:
    if item["review"]["content_hash"] != payload.get("hash"):
        raise ValueError("摘要正文已变化，请重新打开")


def _mark(ctx, item: dict, payload: dict) -> dict:
    """保留选区外的旧颜色，同一选区只应用一种标记。"""
    text = _display_text(_blocks(item["content"] or ""))
    start, end, level = payload["start"], payload["end"], payload["level"]
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
            or not text[start:end].strip() or level not in ("blue", "red", "underline", "clear")):
        raise ValueError("请先选中正文文字")
    result = []
    for mark in item["review"]["annotations"]:
        left, right = mark["start"], mark["end"]
        if right <= start or left >= end or text[left:right] != mark["text"]:
            result.append(mark)
            continue
        for a, b in ((left, min(right, start)), (max(left, end), right)):
            if a < b and text[a:b].strip():
                result.append({"start": a, "end": b, "text": text[a:b], "level": mark["level"]})
    if level != "clear":
        result.append({"start": start, "end": end, "text": text[start:end], "level": level})
    return summary_review.save_review(
        abs_path(ctx.cfg, "db_path"), item["id"], payload["hash"], annotations=result,
    )


class _Voice:
    """由现有 Narrator 的 STA 线程提供状态，页面只读普通字典。"""

    def __init__(self):
        self.status = {"state": "idle", "note": "", "index": 0, "total": 0}
        self.narrator = tts.Narrator(self._event)

    def _event(self, kind, value):
        if kind == "tts_state":
            state, note = value
            self.status = {**self.status, "state": state, "note": note}
        elif kind == "tts_seg":
            index, total = value
            self.status = {**self.status, "index": index, "total": total}


def _voice(ctx) -> _Voice:
    if not hasattr(ctx, "_reader_voice"):
        ctx._reader_voice = _Voice()
    return ctx._reader_voice


def close(ctx) -> None:
    with _VOICE_LOCK:
        voice = getattr(ctx, "_reader_voice", None)
        if voice:
            voice.narrator.close()
            del ctx._reader_voice


def _rate(ctx) -> int:
    value = (prefs.load_raw(ctx.prefs_file).get("tts") or {}).get("rate", 0)
    return value if value in RATES else 0


def _narration(ctx, payload: dict) -> dict:
    voice = _voice(ctx)
    op = payload["op"]
    if op == "rate":
        rate = payload["rate"]
        if rate not in RATES:
            raise ValueError("朗读语速无效")
        with ctx.prefs_lock:
            saved = (prefs.load_raw(ctx.prefs_file).get("tts") or {})
            prefs.update({"tts": {**saved, "rate": rate}}, ctx.prefs_file)
        voice.narrator.set_rate(rate)
    elif op == "play":
        item = _report(ctx, int(payload["id"]))
        _verify_content(item, payload)
        segments = tts.segments_from_text(_display_text(_blocks(item["content"] or "")))
        index = payload["index"]
        if type(index) is not int or not 0 <= index < len(segments):
            raise ValueError("当前没有可朗读的正文段落")
        # 旧播报的结束事件只属于旧对象，不能覆盖换段请求的新进度。
        voice.narrator.close()
        voice = ctx._reader_voice = _Voice()
        voice.status = {"state": "starting", "index": index, "total": len(segments), "note": ""}
        voice.narrator.play([segment.text for segment in segments], start=index, rate=_rate(ctx))
    elif op == "stop":
        voice.narrator.close()
        voice.status = {**voice.status, "state": "idle", "note": ""}
    elif op == "pause":
        voice.narrator.pause()
    elif op == "resume":
        voice.narrator.resume()
    elif op != "status":
        raise ValueError("未知朗读操作")
    return voice.status


def _usage_details(ctx, totals_only=False) -> dict:
    day = datetime.now(BEIJING).date()
    try:
        with ctx.read_store() as store:
            store.conn.execute("BEGIN")
            if totals_only:
                return store.native_totals(day.isoformat())
            result = store.spending_dashboard(day.isoformat())
    except FileNotFoundError:
        totals = dict.fromkeys(("usd", "cny", "today_usd", "today_cny"), 0.0)
        if totals_only:
            return totals
        result = {"totals": totals, "daily": [], "groups": [], "items": []}
    result["days"] = [(day - timedelta(days=i)).isoformat() for i in reversed(range(30))]
    for row in result["items"]:
        pricing = json.loads(row.get("pricing") or "{}")
        tier = row.get("service_tier") or pricing.get("requested_service_tier")
        multiplier = {"default": 1, "flex": 0.5, "fast": 2, "priority": 2}.get(tier)
        row["tier_label"] = tier or "未记录"
        if pricing.get("base_url", "").rstrip("/") == "https://api.openai.com/v1" and multiplier is not None:
            row["tier_label"] += f" ×{multiplier:g}" + ("（按请求估算）" if not row.get("service_tier") else "")
        # 只交给渲染进程展示用得上的价格信息，不透传快照中的其它配置。
        row["pricing"] = {key: pricing[key] for key in ("input_price", "cached_price", "output_price", "currency")
                          if key in pricing}
    return result


def handle(ctx, action: str, payload: dict):
    if action == "reader.topicUpdate":
        if not ctx.service_running:
            raise ValueError("请在主页启动接收服务，再生成主题报")
        return ctx.request("/topics/update", method="POST", payload=payload, timeout=30)
    if action == "reader.topics":
        from msgx.topic_reports import candidates
        try:
            with ctx.read_store() as store:
                store.conn.execute("BEGIN")
                return {"reports": store.topic_reports(),
                        "candidates": candidates(store) if payload.get("candidates") else []}
        except FileNotFoundError:
            return {"reports": [], "candidates": []}
    if action == "reader.history":
        if "kinds" in payload:
            kinds = _reader_kinds(payload["kinds"])
        else:
            kind = payload.get("kind", "")
            kinds = (kind,) if kind in KINDS else tuple(KINDS)
        if not kinds or not abs_path(ctx.cfg, "db_path").is_file():
            return []
        with ctx.read_store() as store:
            return [_metadata(item) for item in store.report_history(kinds)]
    if action == "reader.filters":
        # 只更新摘要筛选；其它窗口偏好和摘要内容沿用原文件。
        with ctx.prefs_lock:
            if "kinds" in payload:
                kinds = _reader_kinds(payload["kinds"])
                prefs.update({"reader_kinds": kinds}, ctx.prefs_file)
                return kinds
            saved = prefs.load_raw(ctx.prefs_file)
            return _reader_kinds(saved.get("reader_kinds", list(KINDS)))
    if action == "reader.narration":
        with _VOICE_LOCK:
            return _narration(ctx, payload)
    if action in ("usage.details", "usage.totals"):
        return _usage_details(ctx, totals_only=action == "usage.totals")
    if action == "usage.sources":
        with ctx.read_store() as store:
            return store.api_call_sources(int(payload["id"]))
    if action not in ("reader.report", "reader.review", "reader.mark", "reader.openHtml"):
        raise ValueError("未知摘要或用量操作")
    report_id = int(payload["id"])
    if action in ("reader.mark", "reader.review"):
        # 标注依赖上一份标注列表，桥接线程必须串行完成读改写。
        with _REVIEW_LOCK:
            item = _report(ctx, report_id)
            _verify_content(item, payload)
            if action == "reader.mark":
                return _mark(ctx, item, payload)
            changes = {key: payload[key] for key in ("read_state", "notes") if key in payload}
            return summary_review.save_review(abs_path(ctx.cfg, "db_path"), report_id, payload["hash"], **changes)
    item = _report(ctx, report_id)
    if action == "reader.report":
        content = item["content"] or ""
        blocks = _blocks(content)
        path = _report_file(ctx, item)
        return {**_metadata(item), "review": item["review"], "content": content, "blocks": blocks,
                "segments": [asdict(s) for s in tts.segments_from_text(_display_text(blocks))],
                "report_path": str(path) if path else "", "rate": _rate(ctx)}
    path = _report_file(ctx, item)
    if path is None:
        raise ValueError("摘要 HTML 文件不在配置的报告目录或已丢失")
    open_path(path)
    return True


def _reader_kinds(values) -> list[str]:
    if not isinstance(values, list) or any(not isinstance(value, str) or value not in KINDS for value in values):
        raise ValueError("摘要筛选必须是日报、周报和月报中的类型列表")
    return [kind for kind in KINDS if kind in values]
