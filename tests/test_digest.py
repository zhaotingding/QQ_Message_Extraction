"""手动摘要：一次提交、动态 Token 冗余、完整保存和超上下文断点复用。"""
from __future__ import annotations
import asyncio
import tempfile
import time
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from msgx import summarize as S
from msgx.llm import IncompleteResponse
from msgx.models import Msg
from msgx.store import Store

fails = []
def check(name, ok):
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")
    if not ok:
        fails.append(name)

class FakeLLM:
    available = True
    def __init__(self, replies):
        self.replies, self.calls, self.progress = list(replies), [], []
        self.sm = None
    async def chat(self, system, user, **kw):
        self.calls.append((system, user, kw))
        if self.sm:
            self.progress.append(dict(self.sm.progress))
        reply = self.replies.pop(0) if self.replies else report()
        if isinstance(reply, Exception):
            raise reply
        return reply

def make(llm, tmp, **summary):
    sm = S.Summarizer({"app": {"report_dir": tmp}, "summary": summary,
                       "llm": {"model": "deepseek-flash", "base_url": "https://example.invalid"}},
                      Store(Path(tmp) / "test.db"), llm)
    llm.sm = sm
    return sm

def report(repeats=10):
    return "# 本期技术讨论纪要\n\n**模型研究**\n" + "需要核对版本和对照条件。" * repeats + "\n\n**工具进展**\n" + "部署前应核对依赖和使用方法。" * repeats + "完整结尾标记。"

def insert(sm, count, body="消息"):
    now = int(time.time())
    sm.store.conn.executemany(
        "INSERT INTO messages(group_id,msg_id,ts,text) VALUES(?,?,?,?)",
        [("g", str(i), now - 10, f"{body}【消息{i}】") for i in range(count)],
    )
    sm.store.conn.commit()
    return [int(row["id"]) for row in sm.store.messages_between_all(0, now)]

async def main():
    check("默认提示词保留总标题、加粗小标题和归纳要求",
          "# 标题" in S.DEFAULT_DIGEST_PROMPT and "**小标题**" in S.DEFAULT_DIGEST_PROMPT
          and "关键词" in S.DEFAULT_DIGEST_PROMPT and "分歧" in S.DEFAULT_DIGEST_PROMPT)
    check("只替换目标字数变量，保留用户模板其他花括号",
          S.digest_system(4500, "约{{目标字数}}字 {custom}") == "约4500字 {custom}")
    check("输出预算随字数增长且带冗余，并非固定9000",
          S.output_token_budget(1000) < S.output_token_budget(3000) < S.output_token_budget(6000)
          and S.output_token_budget(3000) == 10240)
    check("数字、中文、Unicode混排都纳入本地估算",
          S.estimate_tokens("1234中文😀") > S.estimate_tokens("abcd"))
    check("自动摘要方法已删除",
          not hasattr(S.Summarizer, "run_periodic") and not hasattr(S.Summarizer, "run_daily"))

    with tempfile.TemporaryDirectory() as tmp:
        complete = report(190)
        llm = FakeLLM([complete])
        sm = make(llm, tmp, chunk_char_budget=1)  # 旧设置不能继续触发分块
        ids = insert(sm, 1922, "讨论模型变化、工具配置和排查方法。" * 3)
        with patch.object(S.log, "info") as log_info:
            result = await sm.run_digest(full=True)
        messages = [call.args[0] % call.args[1:] for call in log_info.call_args_list]
        check("1922条消息只调用一次API", len(llm.calls) == 1)
        body, kw = llm.calls[0][1:]
        check("一次请求包含首尾及全部来源", "【消息0】" in body and "【消息1921】" in body and kw["source_ids"] == ids)
        check("显式传动态max_tokens并使用非思考模式",
              kw["max_tokens"] == S.output_token_budget(3000) and not kw["think"] and kw["require_complete"])
        check("超过目标字数也完整保存，不追加改写请求",
              result["content"] == complete and len(complete) > 3000
              and sm.store.recent_summaries(1)[0]["content"] == complete)
        check("进度日志包含预估输入和实际发送的输出额度",
              any("预估输入" in message and "max_tokens=10240" in message for message in messages))
        page = Path(result["path"]).read_text(encoding="utf-8")
        check("HTML只有一个总标题并渲染加粗", page.count("<h1>") == 1 and "<strong>模型研究</strong>" in page)
        check("HTML保留完整结尾", "完整结尾标记。" in page)
        check("返回路径为绝对路径", Path(result["path"]).is_absolute())
        check("完成进度状态正确且释放锁", sm.progress["state"] == "done" and not sm._digest_lock.locked())
        sm.store.close()

    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeLLM([])
        sm = make(llm, tmp, target_chars=4500, prompt="请总结成{{目标字数}}字")
        ids = insert(sm, 8005)
        sm.store.conn.execute("UPDATE messages SET media_state='filtered' WHERE id=?", (ids[4],))
        sm.store.conn.commit()
        result = await sm.run_digest(full=True)
        check("超过8000条仍完整读取，过滤行不参与", result["count"] == 8004 and len(llm.calls) == 1
              and "【消息8004】" in llm.calls[0][1] and ids[4] not in llm.calls[0][2]["source_ids"])
        check("配置提示词和字数用于请求", llm.calls[0][0] == "请总结成4500字"
              and llm.calls[0][2]["max_tokens"] == S.output_token_budget(4500))
        await sm.run_digest(full=True, target_chars=1200, prompt="自定义{{目标字数}}字 {literal}")
        check("单次已保存的GUI设置覆盖服务启动配置", llm.calls[-1][0] == "自定义1200字 {literal}"
              and llm.calls[-1][2]["max_tokens"] == S.output_token_budget(1200))
        sm.store.close()

    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeLLM(["用户自定义格式\n完整返回内容"])
        sm = make(llm, tmp)
        ids = insert(sm, 1)
        ocr, vision = "图中文字" * 200 + "OCR尾部", "图片解释" * 200 + "图片解析尾部"
        sm.store.conn.execute("UPDATE messages SET ocr_text=?,vision_text=? WHERE id=?", (ocr, vision, ids[0]))
        sm.store.conn.commit()
        result = await sm.run_digest(full=True, prompt="使用自定义格式")
        check("同时发送全部已存OCR与图片解析，不截600/200字符",
              ocr in llm.calls[0][1] and vision in llm.calls[0][1])
        check("图片复用不增加API调用，自定义格式也直接保存",
              len(llm.calls) == 1 and result["content"] == "用户自定义格式\n完整返回内容")
        sm.store.close()

    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeLLM([])
        sm = make(llm, tmp)
        now = int(time.time())
        for name, ts in (("范围之前", now-86401), ("范围内", now-10), ("未来消息", now+60)):
            sm.store.insert_message(Msg(msg_id=name, group_id="g", text=name, ts=ts))
        result = await sm.run_digest(full=True, hours=24)
        check("统计范围排除过去窗口外及未来消息", result["count"] == 1
              and "范围内" in llm.calls[0][1] and "范围之前" not in llm.calls[0][1] and "未来消息" not in llm.calls[0][1])
        result = await sm.run_digest()
        check("已总结窗口之后无新消息时不调用API", result is None and len(llm.calls) == 1)
        sm.store.close()

    for failure in (RuntimeError("API unavailable"), "", IncompleteResponse("尚未结束的正文", "length", 10240, 10240)):
        with tempfile.TemporaryDirectory() as tmp:
            llm = FakeLLM([failure])
            sm = make(llm, tmp)
            insert(sm, 2)
            try:
                await sm.run_digest(full=True)
                check("失败不冒充成功成稿", False)
            except RuntimeError as exc:
                check("失败不冒充成功成稿", not sm.store.recent_summaries(1) and sm.progress["state"] == "failed")
                if isinstance(failure, IncompleteResponse):
                    drafts = list((Path(tmp) / "unfinished").glob("*.txt"))
                    check("未完成正文单独保存且错误说明位置", len(drafts) == 1
                          and drafts[0].read_text(encoding="utf-8") == failure.content and str(drafts[0]) in str(exc))
            check("失败无额外自动收费请求且释放锁", len(llm.calls) == 1 and not sm._digest_lock.locked())
            await sm._digest_lock.acquire()
            try:
                try:
                    await sm.run_digest()
                    check("阻止并发重复付费", False)
                except RuntimeError:
                    check("阻止并发重复付费", len(llm.calls) == 1)
            finally:
                sm._digest_lock.release()
            sm.store.close()

    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeLLM([report(), RuntimeError("第二批失败")])
        sm = make(llm, tmp)
        ids = insert(sm, 12, "正文" * 500)
        with patch.object(S, "model_limits", return_value=(12000, 10000)):
            try:
                await sm.run_digest(full=True, target_chars=500)
                check("超出上下文按容量分批且失败暂停", False)
            except RuntimeError:
                check("超出上下文按容量分批且失败暂停", len(llm.calls) == 2 and not sm.store.recent_summaries(1))
            first_body = llm.calls[0][1]
            result = await sm.run_digest(full=True, target_chars=500)
        check("手动重试复用已完成分块", sum(c[1] == first_body for c in llm.calls) == 1)
        check("分批后最终合并关联全部消息", llm.calls[-1][2]["source_ids"] == ids and result["count"] == 12)
        check("超上下文合并不复用已弃用用途名", llm.calls[-1][2]["purpose"] == "超上下文合并")
        check("分块和合并请求都留有上下文输出空间", all(
              S.estimate_tokens(c[0]) + S.estimate_tokens(c[1]) + 128 + c[2]["max_tokens"] + 2048 <= 12000
              for c in llm.calls))
        check("缓存保存成功中间结果", bool(list((Path(tmp) / ".summary-cache").glob("*.json"))))
        sm.store.close()

    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeLLM([])
        sm = make(llm, tmp)
        insert(sm, 1, "长消息内容" * 3000 + "完整长消息尾部")
        with patch.object(S, "model_limits", return_value=(12000, 10000)):
            result = await sm.run_digest(full=True, target_chars=500)
        check("单条超长消息不丢尾部", any("完整长消息尾部" in c[1] for c in llm.calls))
        check("单条超长消息分段都关联原始消息", all(c[2]["source_ids"] == [1] for c in llm.calls))
        sm.store.close()

    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeLLM([])
        sm = make(llm, tmp)
        insert(sm, 1)
        for kwargs in ({"target_chars": -1}, {"target_chars": 200000}, {"hours": 0},
                       {"prompt": "巨大提示词" * 300000}):
            try:
                await sm.run_digest(full=True, **kwargs)
                check("无效额度与过大提示词在API前拒绝", False)
            except ValueError:
                check("无效额度与过大提示词在API前拒绝", not llm.calls)
        sm.store.close()

asyncio.run(main())
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
