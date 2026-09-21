"""v0.6.0：完整模型总结、约 3000 字目标、标题渲染、生成进度与请求归因。"""
from __future__ import annotations
import asyncio
import tempfile
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from msgx import summarize as S
from msgx.models import Msg
from msgx.store import Store

fails = []
def check(name, ok):
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")
    if not ok:
        fails.append(name)

class FakeLLM:
    available = True
    def budget_ok(self):
        return True
    def __init__(self, replies):
        self.replies, self.calls, self.progress = list(replies), [], []
        self.sm = None
    async def chat(self, system, user, **kw):
        self.calls.append((system, user, kw))
        if self.sm:
            self.progress.append(dict(self.sm.progress))
        reply = self.replies.pop(0) if self.replies else ""
        if isinstance(reply, Exception):
            raise reply
        return reply

def make(llm, tmp):
    sm = S.Summarizer({"app": {"report_dir": tmp}, "summary": {"chunk_char_budget": 1}, "llm": {}},
                      Store(Path(tmp) / "test.db"), llm)
    llm.sm = sm
    return sm

def report(repeats=10):
    return "# 本期技术讨论纪要\n\n**模型研究**\n" + "需要核对版本和对照条件。" * repeats + "\n\n**工具进展**\n" + "部署前应核对依赖和使用方法。" * repeats + "完整结尾标记。"

async def main():
    check("全部时间范围和增量目标统一为 3000 字",
          S.DIGEST_DEFAULT_CHARS == 3000 and all(S.chars_for_hours(h) == 3000 for h in (24, 72, 168, 720)))
    for prompt in (S.digest_system(3000), S.MERGE_SYSTEM, S.merge_final_rule(3000)):
        check("提示词要求完整总结，未要求客户端截取", "完整" in prompt or "独立" in prompt)
    check("明确要求总标题和加粗小标题", "# " in S.digest_system(3000) and "**加粗的小标题**" in S.digest_system(3000))

    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeLLM([])
        sm = make(llm, tmp)
        content = report(130)
        out = await sm._finalize_digest(content)
        check("超过原 800/1500 字的全文仍完整保留", len(out) > 1500 and out == content)
        check("格式合格时没有多余模型调用", not llm.calls)
        sm.store.close()

    with tempfile.TemporaryDirectory() as tmp:
        complete = report(190)
        llm = FakeLLM([complete])
        sm = make(llm, tmp)
        out = await sm._finalize_digest(complete)
        check("超过 3000 字通过模型重新总结而不是硬截断", len(llm.calls) == 1 and out == complete and out.endswith("完整结尾标记。"))
        path = sm._write_report("范围与时间", out, "", 10, 20, 6)
        page = path.read_text(encoding="utf-8")
        check("HTML 只有一个模型总标题", page.count("<h1>") == 1 and "<h1>本期技术讨论纪要</h1>" in page)
        check("小标题渲染为加粗", "<strong>模型研究</strong>" in page and "**模型研究**" not in page)
        check("HTML 完整保留最后一段结尾", "完整结尾标记。" in page)
        again = sm._write_report("范围与时间", out, "", 10, 20, 6)
        check("同一分钟重做不会覆盖上一份文件", path != again and path.exists())
        check("加粗内容仍做 HTML 转义", "<script>" not in S.md_to_html("**<script>**"))
        sm.store.close()

    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeLLM([report()] * 9)
        sm = make(llm, tmp)
        ids = []
        for i in range(6):
            ids.append(sm.store.insert_message(Msg(msg_id=str(i), group_id="g", text=f"第{i}条消息", ts=int(time.time())-10+i)))
        result = await sm.run_digest(full=True)
        check("完成接口返回完整正文和绝对文件路径", result["content"] == report() and Path(result["path"]).exists())
        check("全部 API 调用要求完整输出且有充足输出预算", all(c[2]["require_complete"] and c[2]["max_tokens"] >= 9000 for c in llm.calls))
        check("分块请求精确关联对应消息", [c[2]["source_ids"] for c in llm.calls[:6]] == [[i] for i in ids])
        check("中间层只关联本批次消息", llm.calls[6][2]["source_ids"] == ids[:4] and llm.calls[7][2]["source_ids"] == ids[4:])
        check("最后合并关联全部来源", llm.calls[8][2]["source_ids"] == ids)
        check("处理中可观测分块进度", any("1/6" in p["message"] for p in llm.progress))
        check("完成后进度状态恢复", sm.progress["state"] == "done")
        check("摘要库保存完整正文", sm.store.recent_summaries(1)[0]["content"] == report())
        sm.store.close()

    for failure in (RuntimeError("API unavailable"), ""):
        with tempfile.TemporaryDirectory() as tmp:
            llm = FakeLLM([report(), failure])
            sm = make(llm, tmp)
            for i in range(2):
                sm.store.insert_message(Msg(msg_id=str(i), group_id="g", text="消息", ts=int(time.time())-10+i))
            try:
                await sm.run_digest(full=True)
                check("分块失败不交付残缺总结", False)
            except RuntimeError:
                check("分块失败不交付残缺总结", not sm.store.recent_summaries(1) and sm.progress["state"] == "failed")
            check("失败后释放生成锁", not sm._digest_lock.locked())
            await sm._digest_lock.acquire()
            try:
                try:
                    await sm.run_digest()
                    check("防止并发重复生成", False)
                except RuntimeError as exc:
                    check("防止并发重复生成", "已有摘要" in str(exc))
            finally:
                sm._digest_lock.release()
            sm.store.close()

    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeLLM([report()] * 3)
        sm = make(llm, tmp)
        now = int(time.time())
        for i in range(6):
            sm.store.add_summary("g", now-60, now-i, 2, report() + f"最后来源{i}", "periodic", "")
        path = await sm.run_daily()
        check("日报也分层调用模型并保留最后来源", len(llm.calls) == 3
              and any("最后来源5" in c[1] for c in llm.calls) and all(c[2]["purpose"] == "日报合并" for c in llm.calls))
        check("日报同样使用 3000 字与完整标题结构", "3000" in llm.calls[-1][0]
              and "<strong>工具进展</strong>" in Path(path).read_text(encoding="utf-8"))
        llm.replies = [RuntimeError("API unavailable")]
        try:
            await sm.run_daily()
            check("日报失败不以拼接原文伪装模型总结", False)
        except RuntimeError:
            check("日报失败不以拼接原文伪装模型总结", len(list(Path(tmp).rglob("*.html"))) == 1)
        sm.store.close()

asyncio.run(main())
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
