"""跨群热点聚合。

核心设计判断：**「重复度」必须用确定性统计算出来，不能交给模型去「发现」。**
一个词是不是热点，看的是它同时出现在几个群、被几个不同的人提到、总共提了几次 ——
这三个量都能免费、确定地统计出来；模型只负责最后的归纳，不做发现。

热度 = 群数 × 人数 × log(1 + 次数)。群数与人数是整数乘法，权重远高于次数的对数：
「跨群 + 多人」才是真信号，单人在单群刷屏不算热点。

零新增依赖：只用标准库 re / math / collections / dataclasses。
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field

# 连续汉字片段 / 英文数字记号（GPT、CUDA、vLLM、Qwen3、PYTORCH_CUDA_ALLOC_CONF…）
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,}")

# 中文口语噪声：即使跨群出现也几乎不构成信息，直接排除
_STOP_ZH = frozenset(
    """
    这个 那个 这些 那些 就是 不是 没有 什么 怎么 为什么 还是 所以 但是 因为 因此 可以
    已经 如果 我们 你们 他们 大家 老师 谢谢 辛苦 现在 时候 时间 一下 一个 两个 这样 那样
    知道 觉得 感觉 应该 可能 真的 直接 然后 而且 其实 这种 那种 今天 昨天 明天 上午 下午
    晚上 刚刚 开始 最后 问题 东西 事情 地方 方法 情况 一些 关于 对于 通过 使用 出来 进去
    起来 过去 上面 下面 有个 好像 看看 需要 不能 不会 是不是 有些 一样 不同 自己 别人
    这里 那里 哪个 哪些 一定 非常 特别 尤其 而已 罢了 总之 有没有 的话 不了 得着 的时候
    哈哈 嘿嘿 草 牛逼 卧槽
    """.split()
)
# 英文停用词：技术群里英文 token 极多，公共词是主要噪声源
_STOP_EN = frozenset(
    """
    the and for you are was were has have had this that with from will would could should
    not but all can one two how what when who our your their they them then than into over
    under out get got make made just like also more most some such only same very much many
    its don didn isn about after before other new old may might must shall there here where
    which while why yes ok okay lol www com net org http https html index main
    """.split()
)
STOPWORDS = _STOP_ZH | _STOP_EN

# 中文停用词按「最长优先」拼成分割正则：先把 run 里的停用词子串整体剥掉，
# 再对残余片段做 n-gram。不做这一步的话，滑动 n-gram 会把「这个就是那个」
# 切出「个就」「是那」这类碎片 —— 它们能过群数/人数门槛，是最主要的噪声源。
_STOP_SPLIT = re.compile("|".join(sorted(_STOP_ZH, key=len, reverse=True)))

MIN_GRAM = 2     # 中文 n-gram 最短长度
MAX_GRAM = 4     # 中文 n-gram 最长长度
MAX_SAMPLES = 4  # 每个热点保留的原文片段数


@dataclass
class TermStat:
    term: str
    groups: set = field(default_factory=set)
    senders: set = field(default_factory=set)
    count: int = 0
    first_ts: int = 0
    last_ts: int = 0
    samples: list = field(default_factory=list)  # (ts, group_id, sender_name, excerpt)
    support: set = field(default_factory=set)    # 支持消息的序号集合，仅用于碎片归并

    @property
    def heat(self) -> float:
        return len(self.groups) * len(self.senders) * math.log1p(self.count)


def grams(text: str) -> set[str]:
    """抽候选词：中文（剥停用词后）2~4 字 n-gram + 英文/数字 token。"""
    out: set[str] = set()
    for run in _CJK_RUN.findall(text or ""):
        for piece in _STOP_SPLIT.split(run):
            if not piece:
                continue
            n_max = min(MAX_GRAM, len(piece))
            for n in range(MIN_GRAM, n_max + 1):
                for i in range(len(piece) - n + 1):
                    g = piece[i : i + n]
                    if g not in STOPWORDS:
                        out.add(g)
    for tok in _TOKEN.findall(text or ""):
        if len(tok) >= 2 and tok.lower() not in STOPWORDS:
            out.add(tok)
    return out


def _excerpt(text: str, term: str, width: int = 90) -> str:
    i = text.find(term)
    if i < 0:
        return text[:width]
    lo = max(0, i - 36)
    return text[lo : lo + width].replace("\n", " ").strip()


def collect(
    rows,
    keywords=(),
    *,
    min_groups: int = 2,
    min_senders: int = 2,
    top_n: int = 10,
) -> list[TermStat]:
    """统计窗口内全部消息，返回按热度排序的热点列表。

    rows 需支持 r["text"] / r["ocr_text"] / r["group_id"] / r["sender_id"] /
    r["sender_name"] / r["ts"]（sqlite3.Row 或 dict 均可）。

    过滤门槛是硬性的：至少跨 min_groups 个群 **且** min_senders 个不同的人，
    否则不进榜单 —— 这也是口语噪声的天然滤网（废话可以跨群，但很难跨群又多人还有实义）。
    """
    kw = {str(k).strip() for k in (keywords or ()) if str(k).strip()}
    stats: dict[str, TermStat] = {}
    for idx, r in enumerate(rows):
        text = " ".join(x for x in (r["text"] or "", r["ocr_text"] or "") if x)
        if not text:
            continue
        terms = grams(text)
        for k in kw:  # 用户配置的关键词是强信号，命中即计入候选
            if k in text:
                terms.add(k)
        gid = str(r["group_id"])
        sid = str(r["sender_id"] or r["sender_name"])
        ts = int(r["ts"])
        for t in terms:
            st = stats.get(t)
            if st is None:
                st = stats[t] = TermStat(term=t)
            st.groups.add(gid)
            st.senders.add(sid)
            st.count += 1
            st.first_ts = ts if st.first_ts == 0 else min(st.first_ts, ts)
            st.last_ts = max(st.last_ts, ts)
            st.support.add(idx)
            if len(st.samples) < MAX_SAMPLES:
                st.samples.append((ts, gid, str(r["sender_name"] or sid), _excerpt(text, t)))

    hot = [
        st
        for st in stats.values()
        if len(st.groups) >= min_groups and len(st.senders) >= min_senders
    ]
    # 热度降序；同热度时短词优先（「降智」比「降智了」做标题更干净）
    hot.sort(key=lambda s: (-s.heat, len(s.term)))
    return _dedup_nested(hot, top_n)


def _dedup_nested(hot: list[TermStat], top_n: int) -> list[TermStat]:
    """归并两类冗余，只留代表词：

    1. 子串关系：「降智」与「降智了」同时上榜 → 留排在前面的。
    2. 同句碎片：「智了」「模型」「质量」和「降智」出现在同一批句子里，
       支持集几乎被前者覆盖（≥80%）→ 它们不是独立话题，只是那句话的碎渣，
       并入最热词。覆盖率门槛防止误伤真正的独立话题（低覆盖率的词保留）。
    """
    out: list[TermStat] = []
    for st in hot:  # 已按（热度, 长度）排好，先入者胜
        dup = False
        for kept in out:
            if st.term in kept.term or kept.term in st.term:
                dup = True
                break
            if st.support <= kept.support and len(st.support) >= 0.8 * len(kept.support):
                dup = True
                break
        if dup:
            continue
        out.append(st)
        if len(out) >= top_n:
            break
    return out


def format_for_llm(hotspots: list[TermStat], group_names: dict) -> str:
    """把热点 + 原文片段排成给模型的输入块（附原文而非摘要，避免二次失真）。"""

    def gname(g: str) -> str:
        return group_names.get(g) or g

    blocks = []
    for i, st in enumerate(hotspots, 1):
        lines = [
            f"### 热点{i}：{st.term}",
            f"出现在 {len(st.groups)} 个群（{'、'.join(gname(g) for g in sorted(st.groups))}），"
            f"{len(st.senders)} 个不同的人，共提到 {st.count} 次。",
        ]
        for ts, gid, name, exc in st.samples:
            hhmm = time.strftime("%m-%d %H:%M", time.localtime(ts))
            lines.append(f"- [{hhmm}][{gname(gid)}] {name}: {exc}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
