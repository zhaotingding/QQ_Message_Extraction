from __future__ import annotations

import html
import asyncio
import logging
import re
import time
from pathlib import Path

from .config import abs_path
from . import hotspot

log = logging.getLogger("msgx.summarize")

# v0.6.0：这是模型总结的目标篇幅，不是客户端字符串裁剪上限。
DIGEST_DEFAULT_CHARS = 3000
DIGEST_MIN_CHARS = 3000
DIGEST_MAX_CHARS = 3000

# (界面标签, 小时数, 目标字数) —— 界面按这个列表渲染「按范围生成」的选项
DIGEST_RANGES = (
    ("最近 1 天", 24, 3000),
    ("最近 3 天", 72, 3000),
    ("最近 7 天", 168, 3000),
    ("最近 1 个月", 24 * 30, 3000),
)


def chars_for_hours(hours: int) -> int:
    """所有时间范围统一使用约 3000 字的总结目标。"""
    for _label, h, chars in DIGEST_RANGES:
        if hours <= h:
            return chars
    return DIGEST_MAX_CHARS


def digest_system(limit: int) -> str:
    return f"""你在为一位科研 + 工程双线作战的用户写一份「群消息纪要」。
他的唯一诉求：不遗漏业界动向。下面是一段时间的群聊记录（带群名、时间、发言人）。

写成按主题分段的纪要，像申论正文：每段都是相对独立的内容块。
第一行用 # 写一个概括全篇内容的总标题，然后按主题组织正文。

铁律：
1. 总结后的正文目标约 {limit} 字，充分概括重要信息；原文信息不足时不凑字、不编造。
   必须完整写完各段，不靠省略号或截取原文来达到篇幅要求。
2. 写「发生了什么、结论是什么」，不写「谁说了什么」。
3. 按内容归并为若干主题段，通常 3~6 段；只有一个有效主题时允许一段，不凑段数。
   每段只讲一个主题，先写 **加粗的小标题**，再交代事实、进展与结论；段间用空行分隔。
   不把无关主题塞进同一段，不写碎片化清单。小标题必须具体，不能只有「其他」「总结」。
4. 只留真正重要的：重要发布/更新、模型或工具行为变化、可复用的资源与结论。
   闲聊、寒暄、表情包、接龙、纯情绪、广告、账号交易一律不写。
   拿不准的内容标注（未证实）。
5. 不写任何前缀（如「本期纪要」「以下为」）与结尾评价。
6. 若本段确实没有值得知道的信息，只输出四个字：本段无有效信息
"""


MERGE_SYSTEM = """把下面几段同一时间段的群聊纪要合并成一份。

要求：
1. 去掉重复，只保留最重要的信息。
2. 同一主题的信息合并，不同主题分段，每段是可独立阅读的内容块，以 **加粗的小标题** 开头。
   通常保留 3~6 个内容段，段间用空行分隔；不按来源群机械分段，也不写碎片化清单。
3. 第一行用 # 写唯一的全篇总标题；删去输入各分块的重复总标题，不写前缀与结尾评价。
"""


# 中间层：此时还没到最后一步，先别丢信息
MERGE_MID_RULE = """
4. 这一轮先不要丢信息：保留全部重要要点及未证实标注，可以写到 3500 字左右。
"""


def merge_final_rule(limit: int) -> str:
    return f"""
4. 由你总结为约 {limit} 字的完整成稿；覆盖主要主题，不能通过截断结尾缩短篇幅。
"""


HOTSPOT_SYSTEM = """你在为一位科研 + 工程双线作战的用户做「跨群热点」归纳。
下面每个热点都已统计好：出现在几个群、几个不同的人、提到几次，并附有原文片段。
你的任务不是发现热点（统计已经做了），而是把每个热点压缩成高密度结论。

要求：
1. 每个热点输出：一句话结论（这件事到底是什么、进展到哪一步）+ 值得保留的细节
   （命令 / 链接 / 版本号 / 结论性判断）。
2. 标题必须保留统计数字，格式如「### GPT 降智（3 群 · 5 人 · 9 次）」——
   密度是读者判断重要性的依据，不许省略。
3. 保留原文里的链接、命令、参数，原样不改写。
4. 信息密度优先：能一句话说清的绝不用两句；没有实质内容的热点直接跳过。
5. 直接输出 Markdown，无开场白、无总结性评价。

输出格式（严格遵守）：
### <词>（<N> 群 · <M> 人 · <K> 次）
- <一句话结论>
- <细节 / 可复用内容>
"""


def _chunk(rows: list, budget: int) -> list[list]:
    out: list[list] = []
    cur: list = []
    size = 0
    for r in rows:
        line_len = len(r["text"] or "") + len(r["ocr_text"] or "") + 40
        if cur and size + line_len > budget:
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += line_len
    if cur:
        out.append(cur)
    return out


def _rows_to_text(rows: list) -> str:
    lines = []
    for r in rows:
        hhmm = time.strftime("%H:%M", time.localtime(r["ts"]))
        body = (r["text"] or "").replace("\n", " ").strip()
        if r["ocr_text"]:
            body += "  〔图中文字: " + r["ocr_text"].replace("\n", " / ")[:600] + "〕"
        elif r["vision_text"]:
            body += "  〔图片内容: " + r["vision_text"].replace("\n", " ")[:200] + "〕"
        lines.append(f"[{hhmm}] {r['sender_name']}: {body}")
    return "\n".join(lines)


def _rows_to_text_multi(rows: list, names: dict) -> str:
    """全量总结用：每行带群名，让模型能做跨群归纳。"""
    lines = []
    for r in rows:
        hhmm = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
        body = (r["text"] or "").replace("\n", " ").strip()
        if r["ocr_text"]:
            body += "  〔图中文字: " + r["ocr_text"].replace("\n", " / ")[:600] + "〕"
        elif r["vision_text"]:
            body += "  〔图片内容: " + r["vision_text"].replace("\n", " ")[:200] + "〕"
        gname = names.get(r["group_id"]) or r["group_id"]
        lines.append(f"[{gname} · {hhmm}] {r['sender_name']}: {body}")
    return "\n".join(lines)


def md_to_html(md: str) -> str:
    out: list[str] = []
    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        esc = html.escape(line)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
        esc = re.sub(r"(https?://[^\s<]+)", r'<a href="\1" target="_blank">\1</a>', esc)
        if line.startswith("### "):
            out.append(f"<h3>{esc[4:]}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{esc[3:]}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{esc[2:]}</h1>")
        elif line.startswith("> "):
            # 必须先切片再转义：html.escape 会把 ">" 变成 4 字符的 "&gt;"，
            # 若用未转义串的下标去切转义串，会切出 "t; ..." 这样的残渣。
            out.append(f'<p class="ref">{html.escape(line[2:])}</p>')
        elif line.lstrip().startswith(("- ", "* ")):
            out.append(f"<li>{esc.lstrip()[2:]}</li>")
        else:
            out.append(f"<p>{esc}</p>")
    # 把连续的 <li> 包进 <ul>
    merged: list[str] = []
    for item in out:
        if item.startswith("<li>") and merged and merged[-1] == "</ul>":
            merged[-1] = item
            merged.append("</ul>")
        elif item.startswith("<li>"):
            merged.append("<ul>")
            merged.append(item)
            merged.append("</ul>")
        else:
            merged.append(item)
    return "\n".join(merged)


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<style>
 body{{font-family:"Segoe UI","Microsoft YaHei",sans-serif;max-width:860px;margin:40px auto;
      padding:0 24px;background:#fafaf9;color:#1a1a19;line-height:1.75}}
 h1{{font-size:22px;font-weight:600;border-bottom:2px solid #e5e5e3;padding-bottom:10px}}
 h2{{font-size:17px;font-weight:600;margin-top:30px;color:#2c2c2a}}
 h3{{font-size:15px;font-weight:600;margin-top:22px;color:#534ab7}}
 .meta{{color:#888780;font-size:13px;margin-bottom:26px}}
 p{{margin:0 0 1.2em}}
 .ref{{color:#5f5e5a;font-size:13px;border-left:3px solid #b4b2a9;padding-left:10px;margin:6px 0}}
 a{{color:#185fa5}}
 ul{{margin:6px 0 6px 0;padding-left:22px}} li{{margin:3px 0}}
 .p0{{background:#fcebeb;border-left:3px solid #a32d2d;padding:10px 14px;margin:10px 0;border-radius:6px}}
 .p1{{background:#faeeda;border-left:3px solid #ba7517;padding:10px 14px;margin:10px 0;border-radius:6px}}
 hr{{border:none;border-top:1px solid #e5e5e3;margin:30px 0}}
</style></head><body>
{body}
</body></html>
"""


class Summarizer:
    def __init__(self, cfg: dict, store, llm, notifier):
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.notifier = notifier
        self.scfg = cfg["summary"]
        self.report_dir: Path = abs_path(cfg, "report_dir")
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._digest_lock = asyncio.Lock()
        self.progress = {"state": "idle", "message": "", "started_at": 0.0}

    def _progress(self, message: str) -> None:
        self.progress["message"] = message
        log.info("摘要生成进度：%s", message)

    # ---------- periodic ----------

    async def run_periodic(self, force: bool = False) -> list[dict]:
        if not self.llm.available:
            log.warning("LLM 未配置，跳过摘要生成")
            return []
        now = int(time.time())
        names = self.store.group_names()
        created: list[dict] = []
        # 跨群热点先行：同一件事散在多个群里时，逐群摘要永远体现不出「多群共鸣」，
        # 这是用户最容易漏掉的信号 —— 必须单独统计、放在最前面。
        hot = await self._summarize_hotspots(now)
        if hot:
            created.append(hot)
        for group_id in names:
            try:
                item = await self._summarize_group(group_id, names.get(group_id, ""), now)
                if item:
                    created.append(item)
            except Exception as e:  # noqa: BLE001
                log.warning("群 %s 摘要失败: %s", group_id, e)
        if created:
            await self._notify_periodic(created, names)
        return created

    async def _summarize_hotspots(self, now: int) -> dict | None:
        """跨群热点：先统计后归纳。

        统计（群数/人数/次数）免费且确定，不依赖模型；模型只负责把
        附带原文片段的热点压缩成高密度结论。窗口独立于各群摘要 ——
        各群的窗口推进节奏不同，热点必须用自己的（kind='hotspot'）节奏。
        """
        first = self.store.first_ts_all()
        if not first:
            return None
        # 与各群摘要同理：messages_between_all 是开区间（ts > start），
        # 首窗口须以 first - 1 为下界，否则全局第一条消息被永久排除。
        start = self.store.window_end_all() or (first - 1)
        rows = self.store.messages_between_all(start, now)
        if len(rows) < int(self.scfg.get("min_messages", 8)):
            return None

        hotspots = hotspot.collect(rows, keywords=self.cfg["score"].get("keywords") or ())
        if not hotspots:
            return None

        body = hotspot.format_for_llm(hotspots, self.store.group_names())
        try:
            content = await self.llm.chat(
                HOTSPOT_SYSTEM, body, max_tokens=2000, temperature=0.2, think=False,
                purpose="跨群热点", source_ids=[int(r["id"]) for r in rows], require_complete=True,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("热点归纳调用失败: %s", e)
            return None
        content = (content or "").strip()
        if not content:
            return None

        title = (
            f"跨群热点 {time.strftime('%m-%d %H:%M', time.localtime(start))}"
            f"~{time.strftime('%H:%M', time.localtime(now))}"
        )
        path = self._write_report(title, content, "热点", start, now, len(rows))
        sid = self.store.add_summary("hotspot", start, now, len(rows), content, "hotspot", str(path))
        log.info("跨群热点 %d 个（%d 条消息）已生成", len(hotspots), len(rows))
        return {
            "id": sid,
            "group_id": "hotspot",
            "title": title,
            "path": str(path),
            "count": len(rows),
            "n_hotspots": len(hotspots),
        }

    async def _summarize_group(self, group_id: str, group_name: str, now: int) -> dict | None:
        first = self.store.first_ts(group_id)
        if not first:
            return None
        # 窗口下界是开区间（ts > start）。从未摘要过时取首条消息之前一秒，
        # 否则 messages_between 会把该群的第一条消息永久排除在摘要之外。
        start = self.store.window_end(group_id) or (first - 1)
        rows = self.store.messages_between(group_id, start, now)
        if len(rows) < int(self.scfg.get("min_messages", 8)):
            return None

        budget = int(self.scfg.get("chunk_char_budget", 6000))
        chunks = _chunk(rows, budget)
        limit = DIGEST_DEFAULT_CHARS
        parts: list[str] = []
        part_sources: list[list[int]] = []
        for ch in chunks:
            body = f"（下面是「{group_name or group_id}」这个群的记录）\n\n" + _rows_to_text(ch)
            try:
                part = await self.llm.chat(
                    digest_system(limit), body, max_tokens=9000, temperature=0.2, think=False,
                    purpose="群摘要分块", source_ids=[int(r["id"]) for r in ch], require_complete=True,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("分块摘要调用失败: %s", e)
                raise
            if not (part or "").strip():
                raise RuntimeError("模型分块摘要为空，请重新生成；本次不保存残缺摘要")
            if "无有效信息" not in part[:40]:
                parts.append(part.strip())
                part_sources.append([int(r["id"]) for r in ch])

        if not parts:
            content = "本时段无有效信息。"
        else:
            content = await self._merge_all(parts, limit, part_sources)

        content = await self._finalize_digest(content, limit, [int(r["id"]) for r in rows])

        title = f"{group_name or group_id} 摘要 {time.strftime('%m-%d %H:%M', time.localtime(start))}~{time.strftime('%H:%M', time.localtime(now))}"
        path = self._write_report(title, content, group_name, start, now, len(rows))
        sid = self.store.add_summary(group_id, start, now, len(rows), content, "periodic", str(path))
        return {"id": sid, "group_id": group_id, "title": title, "path": str(path), "count": len(rows)}

    async def run_digest(
        self,
        full: bool = False,
        hours: int = 24,
        target_chars: int | None = None,
    ) -> dict | None:
        if self._digest_lock.locked():
            raise RuntimeError("已有摘要正在生成，请等待完成，避免重复请求")
        async with self._digest_lock:
            self.progress = {"state": "running", "message": "正在读取消息", "started_at": time.time()}
            log.info("开始生成摘要，目标约 %d 字", DIGEST_DEFAULT_CHARS)
            try:
                result = await self._run_digest(full, hours, target_chars)
                self.progress.update(state="done", message="摘要已生成" if result else "没有可总结的新消息")
                return result
            except Exception as exc:
                self.progress.update(state="failed", message=f"摘要生成失败：{exc}")
                raise

    async def _run_digest(self, full: bool, hours: int, target_chars: int | None) -> dict | None:
        """跨群合并成约 3000 字的完整总结。

        - 增量（默认）：接着**上次已经被总结过的消息**继续，游标是 kind='digest'，
          与热点、各群周期摘要的游标互不干扰，因此不会重复总结同一批消息。
        - 按范围（full=True + hours）：把最近 hours 小时的全部消息总结一遍。
          支持「最近 1 天 / 3 天 / 7 天 / 1 个月」四种口径，统一约 3000 字。

        两种模式都**不受 min_messages 门槛限制**：用户主动点击就意味着
        「把手上已有的东西总结给我」，否则会出现「明明收了几十条、点按钮却回 0 份」。
        """
        if not self.llm.available:
            log.warning("LLM 未配置，跳过纪要生成")
            return None
        now = int(time.time())
        first = self.store.first_ts_all()
        if not first:
            return None
        if full:
            start = max(first - 1, now - hours * 3600)
            limit = target_chars or chars_for_hours(hours)
        else:
            start = self.store.window_end_by_kind("digest") or (first - 1)  # 开区间下界
            limit = target_chars or DIGEST_DEFAULT_CHARS
        rows = self.store.messages_between_all(start, now)
        if not rows:
            log.info("纪要：窗口内没有新消息（起点 %s）", time.strftime("%m-%d %H:%M", time.localtime(start)))
            return None

        names = self.store.group_names()
        chunks = _chunk(rows, int(self.scfg.get("chunk_char_budget", 6000)))
        source_ids = [int(r["id"]) for r in rows]
        parts: list[str] = []
        part_sources: list[list[int]] = []
        for i, ch in enumerate(chunks, 1):
            self._progress(f"正在总结第 {i}/{len(chunks)} 组消息，共 {len(rows)} 条")
            head = f"（第 {i}/{len(chunks)} 段；先各自成稿，最后会合并成一份纪要。）\n\n"
            try:
                part = await self.llm.chat(
                    digest_system(limit), head + _rows_to_text_multi(ch, names),
                    max_tokens=9000, temperature=0.2, think=False,
                    purpose="纪要分块", source_ids=[int(r["id"]) for r in ch], require_complete=True,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("纪要分块调用失败: %s", e)
                raise  # 不能把漏掉后半批消息的摘要作为全量成稿交付
            part = (part or "").strip()
            if not part:
                raise RuntimeError("模型分块摘要为空，请重新生成；本次不保存残缺摘要")
            if "无有效信息" not in part[:20]:
                parts.append(part)
                part_sources.append([int(r["id"]) for r in ch])
        if not parts:
            return None

        self._progress("正在合并主题并组织完整纪要")
        content = await self._merge_all(parts, limit, part_sources)
        self._progress("正在检查总标题、小标题与篇幅")
        content = await self._finalize_digest(content, limit, source_ids)

        kind_label = "全量纪要" if full else "群消息纪要"
        span = f"近 {hours // 24} 天" if full and hours >= 24 else (
            f"近 {hours} 小时" if full else "增量"
        )
        title = (
            f"{kind_label}（{span}） {time.strftime('%m-%d %H:%M', time.localtime(start))}"
            f"~{time.strftime('%H:%M', time.localtime(now))}"
        )
        path = self._write_report(title, content, "全部", start, now, len(rows))
        sid = self.store.add_summary("digest", start, now, len(rows), content, "digest", str(path))
        log.info(
            "%s 已生成（%d 条消息 / %d 块 / %d 字 / 目标 %d 字）",
            kind_label, len(rows), len(chunks), len(content), limit,
        )
        await self.notifier.notify(
            title,
            f"{len(rows)} 条消息 · {len(content)} 字",
            level="P1",
            action=path.as_uri(),
            action_label="打开纪要",
        )
        return {
            "id": sid,
            "group_id": "digest",
            "title": title,
            "path": str(path),
            "content": content,
            "count": len(rows),
            "chars": len(content),
            "target_chars": limit,
            "hours": hours if full else None,
        }

    async def _merge_all(self, parts: list[str], limit: int = DIGEST_DEFAULT_CHARS,
                         source_groups: list[list[int]] | None = None, purpose: str = "摘要合并") -> str:
        """分层合并多段纪要，最后一层请求模型写成约 limit 字。

        为什么不分一次合并：一次把 20 段塞给模型、要求压到目标字数，压缩比接近 20:1，
        模型必然做不到（实测只从 440 压到 415）。分层后每步只需压 4:1 左右，
        成稿质量与长度都明显更可控。层数 = ceil(log_4(段数))，代价可忽略。
        """
        level = [p for p in parts if p and p.strip()]
        sources = source_groups or [[] for _ in level]
        if not level:
            return ""
        if len(level) == 1:
            return level[0]

        GROUP = 4
        while True:
            last_round = len(level) <= GROUP
            nxt: list[str] = []
            next_sources: list[list[int]] = []
            for i in range(0, len(level), GROUP):
                batch = level[i : i + GROUP]
                batch_ids = list(dict.fromkeys(rid for group in sources[i:i + GROUP] for rid in group))
                next_sources.append(batch_ids)
                if len(batch) == 1:
                    nxt.append(batch[0])
                    continue
                sys_prompt = MERGE_SYSTEM + (merge_final_rule(limit) if last_round else MERGE_MID_RULE)
                try:
                    out = await self.llm.chat(
                        sys_prompt, "\n\n---\n\n".join(batch),
                        max_tokens=9000, temperature=0.2, think=False,
                        purpose=purpose, source_ids=batch_ids, require_complete=True,
                    )
                    out = (out or "").strip()
                except Exception as e:  # noqa: BLE001
                    log.warning("纪要分层合并失败: %s", e)
                    raise
                if not out:
                    raise RuntimeError("模型合并结果为空，请重新生成")
                nxt.append(out)
            level = nxt
            sources = next_sources
            if last_round:
                return level[0]

    async def _finalize_digest(self, content: str, limit: int = DIGEST_DEFAULT_CHARS,
                               source_ids: list[int] | None = None) -> str:
        """v0.6.0：篇幅与结构交给模型重写，绝不裁剪正文或拼接替代总结。"""
        if "无有效信息" in content[:30]:
            return content
        structured = content.startswith("# ") and re.search(r"(?m)^\*\*[^*\n]+\*\*", content)
        if not structured or len(content) > limit * 1.2:
            content = await self.llm.chat(
                digest_system(limit),
                "请基于以下材料重新整理为完整总结，保留事实、各主题与未证实标注：\n\n" + content,
                max_tokens=9000, temperature=0.2, think=False,
                purpose="摘要成稿", source_ids=source_ids, require_complete=True,
            )
            content = (content or "").strip()
        if not content:
            raise RuntimeError("模型返回了空摘要，请重新生成")
        if not content.startswith("# ") or not re.search(r"(?m)^\*\*[^*\n]+\*\*", content):
            raise RuntimeError("模型未按总标题与加粗小标题生成，请重新生成")
        if len(content) > limit * 1.2:
            log.warning("摘要实际 %d 字，超出约 %d 字的目标；保留完整模型结果，不截断", len(content), limit)
        return content

    def _write_report(self, title, content, group_name, start, end, count) -> Path:
        sub = self.report_dir / time.strftime("%Y%m%d")
        sub.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w\u4e00-\u9fff.-]", "_", title)[:60]
        path = sub / f"{safe}_{time.time_ns()}.html"  # 同一分钟重做也保留各次完整结果
        if content.startswith("# "):
            heading, _, content = content.partition("\n")
            title = heading[2:].strip()
        meta = (
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(start))} — "
            f"{time.strftime('%H:%M', time.localtime(end))} · {count} 条消息"
        )
        path.write_text(
            PAGE.format(title=html.escape(title), body=f"<h1>{html.escape(title)}</h1>"
                        f'<div class="meta">{html.escape(meta)}</div>'
                        f"{md_to_html(content)}"),
            encoding="utf-8",
        )
        return path

    async def _notify_periodic(self, created: list[dict], names: dict) -> None:
        total = sum(c["count"] for c in created)
        hot = next((c for c in created if c.get("group_id") == "hotspot"), None)
        if hot:
            # 有热点时以热点为标题 —— 这是读者最该先看的东西
            title = f"跨群热点 {hot['n_hotspots']} 个（覆盖 {total} 条消息）"
        else:
            title = f"群摘要已生成（{len(created)} 个群 / {total} 条）"
        lines = [
            f"{names.get(c['group_id']) or c['title']}: {c['count']} 条"
            for c in created[:6]
        ]
        newest = max(created, key=lambda c: c["id"])
        await self.notifier.notify(
            title,
            "\n".join(lines),
            level="P1",
            action=Path(newest["path"]).as_uri(),
            action_label="查看热点" if hot else "查看摘要",
        )

    # ---------- daily ----------

    async def run_daily(self) -> str | None:
        since = int(time.time()) - 86400
        # kind='digest'/'full' 的报告本身就是跨群总结，再并进来只会整段重复
        rows = [
            r
            for r in self.store.recent_summaries(300)
            if r["window_end"] >= since and r["kind"] not in ("full", "digest")
        ]
        if not rows:
            return None
        names = self.store.group_names()
        blocks = []
        for r in sorted(rows, key=lambda x: x["window_end"]):
            gname = names.get(r["group_id"]) or r["group_id"]
            blocks.append(f"## {gname} · {time.strftime('%m-%d %H:%M', time.localtime(r['window_end']))}\n{r['content']}")

        if not self.llm.available or not self.llm.budget_ok():
            log.warning("日报暂不能调用模型，本次不生成")
            return None
        merged = await self._merge_all(blocks, purpose="日报合并")
        merged = await self._finalize_digest(merged)

        title = f"群消息日报 {time.strftime('%Y-%m-%d')}"
        path = self._write_report(title, merged, "全部", since, int(time.time()), len(rows))
        await self.notifier.notify(
            title,
            f"已汇总 {len(rows)} 份摘要，点击查看",
            level="P1",
            action=path.as_uri(),
            action_label="打开日报",
        )
        return str(path)
