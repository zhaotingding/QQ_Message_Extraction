"""纪要长度硬约束的单元测试。

背景：用户要的是「会议纪要式」摘要 —— 一段连贯的话，有明确字数上限。
但**模型不遵守字数**：提示词里写明「不超过 N 字」，实测仍会写到 350~700 字。
因此长度不能靠提示词保证，必须由 _fit_length 做确定性收敛。

v0.5.0 变更：上限从固定 200 字改为**按窗口长度取 600~1500 字**
（`DIGEST_RANGES` / `chars_for_hours`），_merge_all / _fit_length 都接受 `limit`。
这条契约必须锁死：无论模型怎么答（写超长 / 答成扩写 / 直接抛错），
最终产物都不得超过传入的 limit。不联网，用假 LLM。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / "src"))

from msgx.store import Store  # noqa: E402
from msgx import summarize as S  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        fails.append(name)


class FakeLLM:
    """按脚本依次回话；脚本项为 Exception 时抛错。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.available = True
        self.calls: list[tuple[str, str, dict]] = []

    async def chat(self, system, user, **kw):
        self.calls.append((system, user, kw))
        if not self.replies:
            return ""
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple] = []

    async def notify(self, *a, **k):
        self.sent.append((a, k))


def make_summarizer(llm) -> S.Summarizer:
    tmp = tempfile.mkdtemp()
    cfg = {
        "app": {"report_dir": tmp},
        "summary": {"min_messages": 8, "chunk_char_budget": 6000},
        "llm": {},
    }
    return S.Summarizer(cfg, Store(os.path.join(tmp, "d.db")), llm, FakeNotifier())


def run(coro):
    return asyncio.run(coro)


print("=== 字数区间常量 ===")
check("默认字数是 800", S.DIGEST_DEFAULT_CHARS == 800, str(S.DIGEST_DEFAULT_CHARS))
check("下限 600", S.DIGEST_MIN_CHARS == 600, str(S.DIGEST_MIN_CHARS))
check("上限 1500", S.DIGEST_MAX_CHARS == 1500, str(S.DIGEST_MAX_CHARS))
check(
    "区间内所有目标字数都在 600~1500 之内",
    all(S.DIGEST_MIN_CHARS <= c <= S.DIGEST_MAX_CHARS for _l, _h, c in S.DIGEST_RANGES),
    str([c for _l, _h, c in S.DIGEST_RANGES]),
)

print("\n=== 窗口越长允许写越长（单调不减）===")
hours_seq = [24, 72, 168, 24 * 30]
chars_seq = [S.chars_for_hours(h) for h in hours_seq]
check("单调不减", all(b >= a for a, b in zip(chars_seq, chars_seq[1:])), str(chars_seq))
check("1 天 -> 800", S.chars_for_hours(24) == 800, str(S.chars_for_hours(24)))
check("1 个月 -> 1500", S.chars_for_hours(24 * 30) == 1500, str(S.chars_for_hours(24 * 30)))
check("超出最大范围也不越界", S.chars_for_hours(24 * 365) <= S.DIGEST_MAX_CHARS)

LIMIT = S.DIGEST_DEFAULT_CHARS

print("\n=== 未超限时原样返回（不多调一次模型）===")
llm = FakeLLM([])
sm = make_summarizer(llm)
short = "短" * 150
out = run(sm._fit_length(short, LIMIT))
check("原样返回", out == short)
check("没有多余调用", len(llm.calls) == 0, f"{len(llm.calls)} 次")

print("\n=== 超限时压缩一轮即可达标 ===")
llm = FakeLLM(["压" * 700])
sm = make_summarizer(llm)
out = run(sm._fit_length("长" * 3000, LIMIT))
check("采纳压缩结果", out == "压" * 700)
check("长度不超限", len(out) <= LIMIT, f"{len(out)} 字")
check("只调一次", len(llm.calls) == 1)

print("\n=== 一轮不够时再压一轮 ===")
llm = FakeLLM(["中" * 1200, "短" * 500])
sm = make_summarizer(llm)
out = run(sm._fit_length("长" * 4000, LIMIT))
check("第二轮被调用", len(llm.calls) == 2, f"{len(llm.calls)} 次")
check("最终不超限", len(out) <= LIMIT, f"{len(out)} 字")

print("\n=== 压缩失败 → 仍必须收敛（硬截断兜底）===")
llm = FakeLLM([RuntimeError("429 rate limited")])
sm = make_summarizer(llm)
long_text = "甲" * 600 + "。" + "乙" * 1500
out = run(sm._fit_length(long_text, LIMIT))
check("调用抛错不冒泡", isinstance(out, str))
check("长度被强压到上限内", len(out) <= LIMIT, f"{len(out)} 字")
check("截断在句末标点处（不把句子切两半）", out.endswith("。"), repr(out[-6:]))

print("\n=== 模型把「压缩」写成扩写 → 不采纳，走硬截断 ===")
llm = FakeLLM(["更" * 5000, "还" * 6000])
sm = make_summarizer(llm)
out = run(sm._fit_length("长" * 1600, LIMIT))
check("未采纳更长的结果", "更" not in out and "还" not in out)
check("长度不超限", len(out) <= LIMIT, f"{len(out)} 字")

print("\n=== 无句末标点的长文本 → 截断后带省略号 ===")
llm = FakeLLM([RuntimeError("down")])
sm = make_summarizer(llm)
out = run(sm._fit_length("字" * 2000, LIMIT))
check("长度不超限", len(out) <= LIMIT, f"{len(out)} 字")
check("以省略号收尾（明示被截断）", out.endswith("…"), repr(out[-4:]))

print("\n=== 大窗口：1500 字口径同样被硬约束 ===")
llm = FakeLLM([RuntimeError("down")])
sm = make_summarizer(llm)
big = S.chars_for_hours(24 * 30)
out = run(sm._fit_length("甲" * (big * 3), big))
check("长度不超过 1500", len(out) <= big, f"{len(out)} 字")

print("\n=== 分层合并：多段 → 单段，收口层才写字数约束 ===")
llm = FakeLLM(["一" * 300, "二" * 300, "终" * 700])
sm = make_summarizer(llm)
parts = [f"第{i}段内容" * 20 for i in range(1, 6)]      # 5 段 → 4+1 → 收口
out = run(sm._merge_all(parts, LIMIT))
check("返回单段", isinstance(out, str) and out.strip() != "")
check("产生了合并调用", len(llm.calls) >= 1, f"{len(llm.calls)} 次")
first_sys = llm.calls[0][0]
mid_used = S.MERGE_MID_RULE.strip()[:20] in first_sys
check("第一层用「先别丢信息」规则（避免过早塌缩）", mid_used)
last_sys = llm.calls[-1][0]
check("收口层才写字数约束", S.merge_final_rule(LIMIT).strip()[:12] in last_sys)
check("收口层约束用的是传入的 limit", str(LIMIT) in last_sys, last_sys[-40:])

print("\n=== 单段输入直接返回，不浪费调用 ===")
llm = FakeLLM([])
sm = make_summarizer(llm)
out = run(sm._merge_all(["唯一一段"], LIMIT))
check("原样返回", out == "唯一一段")
check("零调用", len(llm.calls) == 0)

print()
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
