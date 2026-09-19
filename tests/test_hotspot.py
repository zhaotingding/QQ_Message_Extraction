"""跨群热点统计的单元测试。

核心场景：某话题（「降智」）在 3 个群被 5 个人零散提到 —— 这正是用户抱怨的
「信息散在各群、看不出是同一件事」的典型形态。统计必须把它顶到最前；
而单群噪声词、单人刷屏词、停用词碎片必须被过滤掉。零依赖，直接跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / "src"))

from msgx import hotspot  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def msg(gid: str, sid: str, name: str, text: str, ts: int) -> dict:
    return {
        "group_id": gid, "sender_id": sid, "sender_name": name,
        "text": text, "ocr_text": "", "ts": ts,
    }


print("=== 分词 ===")
g = hotspot.grams("GPT 降智了，试试 PYTORCH_CUDA_ALLOC_CONF=expandable_segments")
check("中文 n-gram 抽出「降智」", "降智" in g, str(sorted(x for x in g if "降" in x)))
check(
    "英文 token 抽出 GPT / PYTORCH_CUDA_ALLOC_CONF",
    "GPT" in g and "PYTORCH_CUDA_ALLOC_CONF" in g,
)
check("纯停用词文本切不出任何词", hotspot.grams("这个 那个 就是 不是 没有") == set())
check("停用词碎片不会漏出（「这个就是那个」）", "个就" not in hotspot.grams("这个就是那个"))

print("\n=== 跨群统计（核心场景：「降智」3 群 5 人 9 次）===")
T0 = 1800000000
rows: list[dict] = []
i = 0
for gid in ("g1", "g2", "g3"):
    for sid in ("u1", "u2", "u3"):
        rows.append(msg(gid, sid, f"用户{sid}", "今天感觉模型降智了，回答质量差很多", T0 + i))
        i += 1
rows.append(msg("g1", "u4", "用户4", "GPT 降智实锤，官方承认了", T0 + 90))
rows.append(msg("g3", "u5", "用户5", "降智 + 上下文一长就崩", T0 + 91))
# 噪声 1：单群多人（火锅）→ 群数门槛应拦下
for sid in ("u1", "u2", "u3"):
    rows.append(msg("g1", sid, f"用户{sid}", "中午吃火锅不", T0 + 200))
# 噪声 2：跨群但只有一个人（打卡）→ 人数门槛应拦下
for gid in ("g1", "g2"):
    rows.append(msg(gid, "u9", "打卡机", "打卡第 100 天", T0 + 300))
# 噪声 3：跨群多人的纯停用词 → 剥离后无候选词
for gid in ("g1", "g2"):
    for sid in ("u1", "u2"):
        rows.append(msg(gid, sid, f"用户{sid}", "这个就是那个", T0 + 400))

hot = hotspot.collect(rows, keywords=["降智"], top_n=10)
terms = [st.term for st in hot]
top = hot[0] if hot else None
check("结果非空", bool(hot))
if top:
    check("Top1 含「降智」", "降智" in top.term, top.term)
    check("「降智」统计：3 个群", len(top.groups) == 3, str(sorted(top.groups)))
    check("「降智」统计：5 个不同的人", len(top.senders) == 5, str(len(top.senders)))
    check("「降智」统计：共 11 次", top.count == 11, str(top.count))
    check("榜单按热度降序", all(hot[k].heat >= hot[k + 1].heat for k in range(len(hot) - 1)))
check("单群词（火锅）被拦下", not any("火锅" in t for t in terms), str(terms))
check("单人跨群词（打卡）被拦下", not any("打卡" in t for t in terms))
check("停用词文本未混入榜单", not any(t in hotspot.STOPWORDS for t in terms), str(terms))
check("子串去重（「降智」「降智了」只留一个）", sum(1 for t in terms if "降智" in t) == 1, str(terms))
check(
    "同句碎片（智了/模型/质量）被并入最热词",
    not any(t in ("智了", "型降", "模型", "质量", "回答") for t in terms),
    str(terms),
)

print("\n=== 给模型的输入格式 ===")
body = hotspot.format_for_llm(hot, {"g1": "AI 原理群", "g2": "炼丹群", "g3": "部署群"})
check("含群数统计", "3 个群" in body)
check("含人数统计", "5 个不同的人" in body)
check("含次数统计", "11 次" in body)
check("群名已映射", "AI 原理群" in body)
check("原文片段已附上", "降智" in body)

print()
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
