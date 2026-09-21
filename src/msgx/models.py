from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")


@dataclass
class Msg:
    msg_id: str
    group_id: str
    group_name: str = ""
    sender_id: str = ""
    sender_name: str = ""
    ts: int = 0
    msg_type: str = "text"
    text: str = ""
    image_urls: list = field(default_factory=list)
    file_names: list = field(default_factory=list)
    at_me: bool = False
    at_all: bool = False
    reply_to: str = ""
    ocr_text: str = ""
    vision_text: str = ""
    media_hash: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return f"{self.group_id}:{self.msg_id}"


def _cq_params(raw: str) -> dict:
    out = {}
    for part in raw.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _from_cq_string(s: str, at_ids: set) -> tuple[str, list, bool]:
    """解析 CQ 码字符串，返回 (可读文本, 图片URL列表, 是否被点名)。"""
    images: list = []
    at_me = False
    out: list[str] = []
    pos = 0
    for m in CQ_RE.finditer(s):
        out.append(s[pos:m.start()])
        pos = m.end()
        ctype, cparams = m.group(1), _cq_params(m.group(2))
        if ctype == "image":
            url = cparams.get("url", "") or cparams.get("file", "")
            if url:
                images.append(url)
            out.append("[图片]")
        elif ctype == "at":
            qq = cparams.get("qq", "")
            if qq == "all":
                out.append("@全体成员")
            elif qq in at_ids:
                at_me = True
        elif ctype == "face":
            out.append("[表情]")
        elif ctype == "mface":
            # 表情包不采 URL：它几乎没有可 OCR 的文字，却和图片一样走
            # 下载 + OCR + 多模态的完整流程 —— 量大、耗算力、还占额度。
            # 处理方式与 video / record 一致，只留占位符。
            out.append("[表情包]")
        elif ctype == "reply":
            out.append("")
        elif ctype == "file":
            out.append(f"[文件:{cparams.get('name', '')}]")
        else:
            out.append(f"[{ctype}]")
    out.append(s[pos:])
    return "".join(out), images, at_me


def mentions_all(event: dict) -> bool:
    """按协议标记识别 @全体成员，避免把正文中引用的同名文字误判为群提醒。"""
    segments = event.get("message")
    if isinstance(segments, list):
        return any(isinstance(seg, dict) and seg.get("type") == "at"
                   and str((seg.get("data") or {}).get("qq")) == "all" for seg in segments)
    if isinstance(segments, str):
        return any(m.group(1) == "at" and _cq_params(m.group(2)).get("qq") == "all"
                   for m in CQ_RE.finditer(segments))
    return False


def parse_onebot_event(event: dict, self_id: str, at_ids: set | None = None) -> Msg | None:
    """把 OneBot 11 群消息事件归一化为 Msg；非群消息返回 None。

    at_ids 是需要被点名关注的 QQ 集合（机器人自身 + 用户自己的主号）。
    """
    if event.get("post_type") not in ("message", "message_sent"):
        return None
    if event.get("message_type") != "group":
        return None

    watched = {str(self_id)} if at_ids is None else {str(x) for x in at_ids}
    watched.discard("")

    segments = event.get("message")
    text_parts: list[str] = []
    images: list[str] = []
    files: list[str] = []
    at_me = False

    if isinstance(segments, list):
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type", "")
            data = seg.get("data") or {}
            if stype == "text":
                text_parts.append(str(data.get("text", "")))
            elif stype == "image":
                url = data.get("url") or data.get("file") or ""
                if url:
                    images.append(str(url))
                text_parts.append("[图片]")
            elif stype == "at":
                qq = str(data.get("qq", ""))
                if qq == "all":
                    text_parts.append("@全体成员")
                elif qq in watched:
                    at_me = True
            elif stype == "face":
                text_parts.append("[表情]")
            elif stype == "mface":
                # 同上：表情包不采 URL，不下载、不 OCR、不送多模态。
                text_parts.append("[表情包]")
            elif stype == "reply":
                text_parts.append("")
            elif stype == "file":
                name = str(data.get("name") or data.get("file") or "")
                files.append(name)
                text_parts.append(f"[文件:{name}]")
            elif stype == "forward":
                text_parts.append("[合并转发]")
            elif stype == "record":
                text_parts.append("[语音]")
            elif stype == "video":
                text_parts.append("[视频]")
    elif isinstance(segments, str):
        joined, images, at_me = _from_cq_string(segments, watched)
        text_parts.append(joined)

    text = "".join(text_parts).strip()
    raw_msg = event.get("raw_message") or ""

    reply_to = ""
    if isinstance(segments, list):
        for seg in segments:
            if isinstance(seg, dict) and seg.get("type") == "reply":
                reply_to = str((seg.get("data") or {}).get("id", ""))
                break

    sender = event.get("sender") or {}
    sender_name = (
        sender.get("card")
        or sender.get("nickname")
        or str(event.get("user_id", ""))
    )

    if not text and not images and not files:
        return None

    if images and not text.replace("[图片]", "").strip():
        msg_type = "image"
    elif len(images) + len(files) > 0 and text.replace("[图片]", "").replace("[表情包]", "").strip():
        msg_type = "mixed"
    else:
        msg_type = "text"

    return Msg(
        msg_id=str(event.get("message_id", "")),
        group_id=str(event.get("group_id", "")),
        group_name="",
        sender_id=str(event.get("user_id", "")),
        sender_name=sender_name,
        ts=int(event.get("time") or time.time()),
        msg_type=msg_type,
        text=text or raw_msg,
        image_urls=images,
        file_names=files,
        at_me=at_me,
        at_all=mentions_all(event),
        reply_to=reply_to,
        raw=event,
    )


def to_line(m: Msg) -> str:
    """把消息压成一行，供喂给模型使用。"""
    hhmm = time.strftime("%H:%M", time.localtime(m.ts))
    body = m.text
    if m.ocr_text:
        body += f"  〔图片文字: {m.ocr_text}〕"
    body = body.replace("\n", " ").strip()
    return f"[{hhmm}] {m.sender_name}: {body}"
