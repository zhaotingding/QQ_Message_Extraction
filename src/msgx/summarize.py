from __future__ import annotations

import html
import logging
import re
import time
from pathlib import Path

from .config import abs_path

log = logging.getLogger("msgx.summarize")

DIGEST_SYSTEM = """你在为一位科研 + 工程双线作战的用户做群聊信息萃取。
把下面这段群聊记录压缩成结构化摘要。

要求：
1. 按「话题」分组，把同一话题下的多条消息合并。寒暄、表情包、签到、接龙、纯情绪输出一律丢弃。
2. 每个话题下写 1-4 条要点，只保留有信息量的内容：技术结论、方法、数据、版本号、报错原因、资源名。
3. 凡是出现链接、命令、参数、仓库名、论文名、数据集名、联系方式，必须原样保留，不要改写或缩写 URL。
4. 如果整段记录都没有值得保留的信息，只输出一行：本段无有效信息
5. 直接输出 Markdown，不要任何开场白、客套话或总结性评价。

输出格式（严格遵守）：
### 话题：<不超过 15 字的标题>
- <要点>
- <要点>
> 可复用：<链接 / 命令 / 参数；没有则整行省略>
"""

MERGE_SYSTEM = """你在合并同一时间段内、来自同一群的多份摘要片段。
把重复的话题合并成一个，按信息价值从高到低排序，保留所有链接、命令、参数、版本号原文。
直接输出合并后的 Markdown，格式与输入一致，不要开场白。
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
        lines = [f"{names.get(c['group_id']) or c['group_id']}：{c['count']} 条" for c in created[:6]]
        newest = max(created, key=lambda c: c["id"])
        await self.notifier.notify(
            f"群摘要已生成（{len(created)} 个群 / {total} 条）",
            "\n".join(lines),
            level="P1",
            action=Path(newest["path"]).as_uri(),
            action_label="查看摘要",
        )

    # ---------- daily ----------

    async def run_daily(self) -> str | None:
        since = int(time.time()) - 86400
        rows = [r for r in self.store.recent_summaries(300) if r["window_end"] >= since]
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
