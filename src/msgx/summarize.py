from __future__ import annotations

import html
import logging
import re
import time
from pathlib import Path

from .config import abs_path
from . import hotspot

log = logging.getLogger("msgx.summarize")

DIGEST_SYSTEM = """你在为一位科研 + 工程双线作战的用户做群聊情报总结。他的核心诉求：不错过业界最新动态。
下面是某个群一段时间内的完整聊天记录。你的任务不是「把消息压短」，而是**总结关键信息**。

要求：
1. 通读全部记录，识别真正有信息量的内容：技术结论、业界动态、版本发布、方法经验、
   踩坑原因、资源（仓库 / 论文 / 工具 / 数据集）。
2. 按「话题」组织。同一话题下多个人的发言必须**归纳成一条结论**，不要罗列「某用户说某句」。
   每个话题给出：发生了什么 / 结论是什么；必要时补一句为什么值得注意（影响、适用场景）。
3. 寒暄、表情包、签到、接龙、纯情绪输出一律丢弃。
4. 凡是出现链接、命令、参数、仓库名、论文名、数据集名，必须原样保留，不要改写或缩写 URL。
5. 如果整段记录都没有值得保留的信息，只输出一行：本段无有效信息
6. 直接输出 Markdown，不要任何开场白、客套话或总结性评价。

输出格式（严格遵守）：
### 话题：<不超过 15 字的标题>
- <结论 / 要点>
- <要点>
> 可复用：<链接 / 命令 / 参数；没有则整行省略>
"""

FULL_SYSTEM = """你在为一位科研 + 工程双线作战的用户编写「群聊情报总结」。他的唯一目标：不错过业界最新动态和值得复用的技术信息。
下面是多技术群一段时间内的全部聊天记录（带群名、时间与发言人）。

你的任务：像写情报简报一样总结，而不是压缩转述。

要求：
1. 通读全部记录后，先输出「## 今日重点」：最多 5 条、每条一句话，选最有行业价值 /
   时效性 / 影响面的信息（新品或版本发布、模型行为变化、大规模故障、平台规则变化等）。
2. 之后按话题组织正文。同一话题分散在多个群、多个人的发言必须**归纳合并成一条结论**，
   不要罗列「某某说某某」。
3. 区分「事实」与「个人观点」：个人经验标注（个人经验），未经证实的消息标注（未经证实）。
4. 链接、命令、参数、版本号、仓库名、论文名原样保留，URL 不许改写。
5. 纯闲聊、寒暄、表情包、接龙一律丢弃；内容少就短，不要注水。
6. 直接输出 Markdown，无开场白、无总结性评价。

输出格式：
## 今日重点
- <要点>

### 话题：<不超过 15 字的标题>
- <结论 / 要点>
> 可复用：<链接 / 命令 / 参数；没有则整行省略>
"""

MERGE_SYSTEM = """你在合并同一时间段内、来自同一群的多份摘要片段。
把重复的话题合并成一个，按信息价值从高到低排序，保留所有链接、命令、参数、版本号原文。
直接输出合并后的 Markdown，格式与输入一致，不要开场白。
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
                HOTSPOT_SYSTEM, body, max_tokens=2000, temperature=0.2, think=False
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
        parts: list[str] = []
        for ch in chunks:
            body = f"群名：{group_name or group_id}\n\n" + _rows_to_text(ch)
            try:
                part = await self.llm.chat(DIGEST_SYSTEM, body, max_tokens=1500, temperature=0.2, think=False)
            except Exception as e:  # noqa: BLE001
                log.warning("分块摘要调用失败: %s", e)
                break
            if part.strip() and "无有效信息" not in part[:40]:
                parts.append(part.strip())

        if not parts:
            content = "本时段无有效信息。"
        elif len(parts) == 1:
            content = parts[0]
        else:
            try:
                content = await self.llm.chat(
                    MERGE_SYSTEM, "\n\n---\n\n".join(parts), max_tokens=2000, temperature=0.2, think=False
                )
                content = content.strip() or parts[0]
            except Exception:  # noqa: BLE001
                content = "\n\n".join(parts)

        title = f"{group_name or group_id} 摘要 {time.strftime('%m-%d %H:%M', time.localtime(start))}~{time.strftime('%H:%M', time.localtime(now))}"
        path = self._write_report(title, content, group_name, start, now, len(rows))
        sid = self.store.add_summary(group_id, start, now, len(rows), content, "periodic", str(path))
        return {"id": sid, "group_id": group_id, "title": title, "path": str(path), "count": len(rows)}

    async def run_full(self, hours: int = 24) -> dict | None:
        """手动「立即生成摘要」入口：把最近一段时间**全部消息**交给模型做情报总结。

        与周期摘要（增量窗口、每组不足 min_messages 条就跳过）刻意不同：
        用户主动点击意味着「把手上已有的东西总结给我」，不受窗口推进与条数门槛
        限制 —— 否则会出现「明明收了几十条，点按钮却回 0 份」的死状态。
        """
        if not self.llm.available:
            log.warning("LLM 未配置，跳过全量总结")
            return None
        now = int(time.time())
        start = now - hours * 3600
        rows = self.store.messages_between_all(start, now)
        if not rows:
            first = self.store.first_ts_all()
            if not first:
                return None
            start = first - 1  # 开区间下界，同第 10 条坑
            rows = self.store.messages_between_all(start, now)
        if not rows:
            return None

        names = self.store.group_names()
        budget = int(self.scfg.get("chunk_char_budget", 6000))
        chunks = _chunk(rows, budget)
        parts: list[str] = []
        for i, ch in enumerate(chunks, 1):
            body = _rows_to_text_multi(ch, names)
            head = f"（这是第 {i}/{len(chunks)} 段，先提炼关键信息点，之后会统一汇总。）\n\n"
            try:
                part = await self.llm.chat(
                    FULL_SYSTEM, head + body, max_tokens=2000, temperature=0.2, think=False
                )
            except Exception as e:  # noqa: BLE001
                log.warning("全量总结分块调用失败: %s", e)
                break
            if part.strip():
                parts.append(part.strip())

        if not parts:
            return None
        if len(parts) == 1:
            content = parts[0]
        else:
            try:
                content = await self.llm.chat(
                    "下面是对同一段时间群聊记录分段提炼出的要点。请合并成一份最终情报总结："
                    "去掉重复，先输出「## 今日重点」（最多 5 条），再按话题组织；"
                    "保留所有链接与命令原文。直接输出 Markdown。",
                    "\n\n---\n\n".join(parts),
                    max_tokens=3000,
                    temperature=0.2,
                    think=False,
                )
                content = (content or "").strip() or "\n\n".join(parts)
            except Exception as e:  # noqa: BLE001
                log.warning("全量总结合并失败，使用原始拼接: %s", e)
                content = "\n\n".join(parts)

        title = (
            f"全量情报总结 {time.strftime('%m-%d %H:%M', time.localtime(start))}"
            f"~{time.strftime('%H:%M', time.localtime(now))}"
        )
        path = self._write_report(title, content, "全部", start, now, len(rows))
        sid = self.store.add_summary("full", start, now, len(rows), content, "full", str(path))
        log.info("全量情报总结已生成（%d 条消息 / %d 块）", len(rows), len(chunks))
        await self.notifier.notify(
            title,
            f"覆盖 {len(rows)} 条消息，点击查看",
            level="P1",
            action=path.as_uri(),
            action_label="打开总结",
        )
        return {"id": sid, "group_id": "full", "title": title, "path": str(path), "count": len(rows)}

    def _write_report(self, title, content, group_name, start, end, count) -> Path:
        sub = self.report_dir / time.strftime("%Y%m%d")
        sub.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w\u4e00-\u9fff.-]", "_", title)[:60]
        path = sub / f"{safe}.html"
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
        # kind='full' 的报告本身就是全天总结，再并进来只会整段重复
        rows = [
            r
            for r in self.store.recent_summaries(300)
            if r["window_end"] >= since and r["kind"] != "full"
        ]
        if not rows:
            return None
        names = self.store.group_names()
        blocks = []
        for r in sorted(rows, key=lambda x: x["window_end"]):
            gname = names.get(r["group_id"]) or r["group_id"]
            blocks.append(f"## {gname} · {time.strftime('%m-%d %H:%M', time.localtime(r['window_end']))}\n{r['content']}")

        merged = "\n\n".join(blocks)
        if self.llm.available and self.llm.budget_ok():
            try:
                merged = await self.llm.chat(
                    "把下列多个群的摘要合并成一份当日总览：先按群分组，再把跨群重复的话题合并，"
                    "把最值得当天阅读的 3-5 条信息提到最前面作为「今日重点」。保留所有链接与命令原文。"
                    "直接输出 Markdown。",
                    merged[:24000],
                    max_tokens=3000,
                    temperature=0.2,
                    think=False,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("日报合并失败，使用原始拼接: %s", e)

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
