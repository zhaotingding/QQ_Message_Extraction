from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict, deque

log = logging.getLogger("msgx.score")

URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff]+")
# 可复用的技术特征：环境变量赋值、命令行开关、安装/克隆命令
TECH_RE = re.compile(
    r"(?:[A-Z][A-Z0-9_]{4,}=|--[a-z][\w-]{2,}|pip install|git clone|docker run|conda install|export \w+=)"
)

SCORE_SYSTEM = """你是技术社群的信息过滤器，为一位科研+工程双线作战的用户服务。
用户不希望被闲聊打扰，也不想错过有价值的沉淀；但他的注意力极其有限，
只有真正紧急的事才值得打断他手头的工作。

请判断两个相互独立的问题，不要互相影响：

一、信息价值 score（0-10）：这条消息值不值得留存、进摘要、以后检索？
  8-10：可直接复用的资源（仓库 / 论文 / 数据集 / 模型权重 / 工具），
        或可复现的经验（配置参数 / 踩坑记录 / 性能数据 / 版本兼容结论）
  4-7 ：有信息量的技术讨论、具体提问、有链接但未知内容
  0-3 ：寒暄、表情包、情绪输出、闲聊、接龙、签到、无上下文附和

二、紧迫性 urgent（true / false）：这条消息是否必须立刻打断用户？
  默认 false。只有「几十分钟内不知道就会造成损失」才算 true，仅包括：
  - 用户在群中被点名提问，正等着他回答
  - 有人直接回答了用户先前提出的问题
  - 限时机会：内推 / 报名截止、线上活动即将开始
  - 用户正在处理的问题出现了可用解法
  以下虽然高价值，但一律 urgent=false：
  分享开源仓库、转发论文、常规招聘信息、经验总结、技术讨论、资料合集。

只输出 JSON，不要任何其它内容：
{"score": <0-10 的数字>, "urgent": <true 或 false>, "reason": "<不超过 30 字的中文理由>"}"""


def extract_json(text: str) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


class Scorer:
    """规则预筛 + 模型复核。

    规则负责「召回候选」，模型负责「判定是否真的值得提醒」，
    这样模型调用量被压到全部消息的一小部分。
    规则漏掉的内容由周期摘要在全量消息上兜底。
    """

    def __init__(self, cfg: dict, store, llm):
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.scfg = cfg["score"]
        self.keywords = [k.lower() for k in self.scfg.get("keywords", [])]
        self.domains = [d.lower() for d in self.scfg.get("link_domains", [])]
        self._links: dict[str, deque] = defaultdict(lambda: deque(maxlen=400))

    # ---------- rules ----------

    def record_links(self, group_id: str, sender_id: str, text: str) -> None:
        now = time.time()
        for url in URL_RE.findall(text or ""):
            self._links[group_id].append((now, sender_id, url.rstrip(".,);）】")))

    def _repeated_link(self, group_id: str, sender_id: str, text: str) -> str:
        """同一链接在 60 分钟内被 >=2 个不同的人发过 —— 强信号。"""
        now = time.time()
        for url in URL_RE.findall(text or ""):
            u = url.rstrip(".,);）】")
            senders = {
                s for (t, s, x) in self._links[group_id] if x == u and now - t <= 3600
            }
            if len(senders | {sender_id}) >= 3:
                return u
        return ""

    def rule_score(self, row, ocr_text: str) -> tuple[float, list[str]]:
        score = 0.0
        reasons: list[str] = []
        text = row["text"] or ""
        low = text.lower()

        if row["at_me"]:
            score += 8
            reasons.append("被点名")

        hits = [k for k in self.keywords if k in low]
        if hits:
            add = min(4, 2 * len(hits))
            score += add
            reasons.append("命中关键词:" + "/".join(hits[:3]))

        urls = URL_RE.findall(text)
        if urls:
            score += 1
            if any(d in u.lower() for u in urls for d in self.domains):
                score += 3
                reasons.append("高价值链接")

        n = len(text)
        if n > 600:
            score += 3
            reasons.append("长文")
        elif n > 200:
            score += 2
            reasons.append("较长")

        tech = TECH_RE.findall(text)
        if tech:
            score += 2
            reasons.append("含可复用配置/命令")

        if ocr_text:
            if len(ocr_text) > 120:
                score += 2
                reasons.append("图片信息量大")
            if any(h in ocr_text.lower() for h in ("error", "exception", "traceback", "报错", "错误", "失败")):
                score += 1
                reasons.append("疑似报错截图")

        rep = self._repeated_link(row["group_id"], row["sender_id"], text)
        if rep:
            score += 4
            reasons.append("多人转发同一链接")

        return min(score, 10.0), reasons

    # ---------- model review ----------

    async def review(self, row, group_name: str, ocr_text: str) -> dict | None:
        if not (self.llm.available and self.llm.budget_ok()):
            return None
        body = (
            f"群名：{group_name or row['group_id']}\n"
            f"发送者：{row['sender_name']}\n"
            f"内容：{row['text']}\n"
        )
        if ocr_text:
            body += f"该消息附带图片，图片中识别出的文字：\n{ocr_text[:1500]}\n"
        try:
            # 刻意**保留思考**（不传 think=False）：打分是判定类任务，需要权衡。
            # 但因此必须注意：思考 token 与正文共用 max_tokens，400 这种小预算
            # 会被思考吃光 → finish_reason=length 且正文为空（=静默丢掉一次打分的
            # 机会）。所以 chat() 里对 think=True 的调用有 THINK_MIN_TOKENS 下限兜底，
            # 实际发出的预算不会小于它。这里的 400 只是「我期望的正文长度」。
            out = await self.llm.chat(SCORE_SYSTEM, body, max_tokens=400, temperature=0.1)
        except Exception as e:  # noqa: BLE001
            log.debug("打分模型调用失败，降级为规则结果: %s", e)
            return None
        data = extract_json(out)
        if not data or "score" not in data:
            return None
        try:
            data["score"] = float(data["score"])
        except (TypeError, ValueError):
            return None
        return data

    # ---------- public ----------

    async def score(self, row, group_name: str = "") -> dict:
        ocr_text = row["ocr_text"] or ""
        rule, reasons = self.rule_score(row, ocr_text)
        p0_min = float(self.scfg.get("p0_min_score", 8))
        p1_min = float(self.scfg.get("p1_min_score", 5))

        result = {"score": rule, "level": "P2", "reason": "；".join(reasons) or "常规消息"}

        candidate = rule >= float(self.scfg.get("llm_review_min_score", 2))
        if candidate:
            reviewed = await self.review(row, group_name, ocr_text)
            if reviewed:
                ms = reviewed["score"]
                if reviewed.get("urgent"):
                    level = "P0"
                elif ms >= p1_min:
                    level = "P1"
                else:
                    level = "P2"
                result = {
                    "score": ms,
                    "level": level,
                    "reason": str(reviewed.get("reason", ""))[:60],
                }

        # 被点名是硬规则，不由模型降级：群里 @ 你必须强提醒
        if row["at_me"]:
            result["level"] = "P0"
            if "被点名" not in result["reason"]:
                result["reason"] = ("被点名；" + result["reason"])[:60]

        if result["level"] not in ("P0", "P1", "P2"):
            result["level"] = "P2"
        if result["level"] == "P2":
            s = result["score"]
            if s >= p0_min:
                result["level"] = "P0"
            elif s >= p1_min:
                result["level"] = "P1"
        return result
