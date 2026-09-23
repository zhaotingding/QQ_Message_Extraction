"""LLM 调用层的回归测试（v0.5.1）。

这个文件锁的是**两个已经踩过、且不看注释就会改回去的坑**：

1. **think=True 时的预算下限。**
   思考 token 与正文**共用** max_tokens。预算给小了，思考把额度吃光 →
   `finish_reason=length` 且 `content` 为空 —— 调用方拿到空串还以为是模型没内容。
   实测思考量在 0~290 之间波动（tools/probe_think_budget.py），而打分调用原来只给
   400，余量仅 110。所以 think=True 的调用有 THINK_MIN_TOKENS 下限兜底。

2. **重试时绝不重置共享会话。**
   `LLM` 只有一个共享 session，调用方是并发的（media 的 Semaphore(2)、scorer、
   summarizer）。v0.5.0 在重试路径里调了 `session.close()`「换掉半死的连接池」，
   实测（tools/probe_session_cascade.py）会把**并发的在途请求一起掐死** ——
   4 并发全灭，而且不是快速报错，是**挂到 60 秒超时**。于是 1 次真实抖动被放大成
   一片失败。现在改成连接不复用（force_close），池里没有陈旧连接可踩。

全部离线：`_post` / `_post_once` 都被替换成假的，不发任何网络请求。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / "src"))

from msgx.llm import THINK_MIN_TOKENS, IncompleteResponse, LLM, _Transient  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        fails.append(name)


class FakeStore:
    """`chat()` 要读当日花费来做预算判断，写用量做记账。都不落库。"""

    def __init__(self) -> None:
        self.usage: list[tuple] = []
        self.details: list[dict] = []

    def daily_cost_cny(self, day: str) -> float:
        return 0.0

    def add_usage(self, *a) -> None:
        self.usage.append(a)

    def record_api_call(self, **data) -> None:
        self.details.append(data)


def make_llm(**over) -> LLM:
    conf = {
        "enabled": True,
        "model": "test-model",
        "base_url": "https://example.invalid",
        "api_key_env": "MSGX_TEST_NO_SUCH_ENV",
        "max_daily_cost_cny": 10.0,
        "think_min_tokens": THINK_MIN_TOKENS,
        "post_retries": 4,
        "timeout_seconds": 5,
    }
    conf.update(over)
    llm = LLM({"llm": conf}, FakeStore())
    llm.configure("sk-test-not-real")
    return llm


def ok_response(content: str = "好的") -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def truncated_response() -> dict:
    """思考吃光预算的典型形态：被截断 + 正文为空。"""
    return {
        "choices": [{"message": {"content": "   "}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 400},
    }


# ---------------------------------------------------------------- 预算下限

print("=== think=True 的预算下限（否则会出静默空正文）===")


async def capture(llm: LLM, **kw) -> dict:
    seen: dict = {}

    async def fake_post(payload: dict) -> dict:
        seen.update(payload)
        return ok_response()

    llm._post = fake_post  # type: ignore[method-assign]
    await llm.chat("sys", "user", **kw)
    return seen


sent = asyncio.run(capture(make_llm(), max_tokens=400, think=True))
check("打分那种小预算被抬到下限", sent["max_tokens"] >= THINK_MIN_TOKENS,
      f"发出 {sent['max_tokens']}")
check("下限确实大于原来那个会截断的 400", THINK_MIN_TOKENS > 400, str(THINK_MIN_TOKENS))
check("思考开启时**不**注入 reasoning_effort（保留思考）",
      "reasoning_effort" not in sent, str(sent.get("reasoning_effort")))

sent = asyncio.run(capture(make_llm(), max_tokens=3000, think=True))
check("预算比下限大时不被压低（max_tokens 只是上限，抬高了不花钱）",
      sent["max_tokens"] == 3000, str(sent["max_tokens"]))

sent = asyncio.run(capture(make_llm(), max_tokens=400, think=False))
check("think=False 时预算原样保留（忠实提取类不需要额外余量）",
      sent["max_tokens"] == 400, str(sent["max_tokens"]))
check("think=False 时显式关掉思考", sent.get("reasoning_effort") == "none",
      str(sent.get("reasoning_effort")))

sent = asyncio.run(capture(make_llm(think_min_tokens=2000), max_tokens=400, think=True))
check("下限可被配置覆盖", sent["max_tokens"] == 2000, str(sent["max_tokens"]))

# ---------------------------------------------------------------- 截断兜底

print("\n=== 截断兜底：放大预算再试一次（并保留确定性告警）===")


async def escalate(llm: LLM, first: dict, second: dict | None = None):
    budgets: list[int] = []

    async def fake_post(payload: dict) -> dict:
        budgets.append(int(payload["max_tokens"]))
        return first if len(budgets) == 1 else (second or ok_response())

    llm._post = fake_post  # type: ignore[method-assign]
    out = await llm.chat("sys", "user", max_tokens=400)
    return budgets, out


budgets, out = asyncio.run(escalate(make_llm(), truncated_response()))
check("第一次用抬过下限的预算", budgets[0] == THINK_MIN_TOKENS, str(budgets))
check("被截断且正文为空 → 放大到 4 倍重试", len(budgets) == 2 and budgets[1] == budgets[0] * 4,
      str(budgets))
check("重试拿到正文就正常返回", out == "好的", out)

budgets, out = asyncio.run(escalate(make_llm(), truncated_response(), truncated_response()))
check("放大后仍为空 → 不再无限重试（只发两次）", len(budgets) == 2, str(budgets))
check("放大后仍为空 → 返回空串（由调用方决定降级）", out == "", repr(out))

# 回归：正常返回时不该多打一次请求（否则每次调用都白花一倍输入钱）
budgets, out = asyncio.run(escalate(make_llm(), ok_response()))
check("正常返回只发一次请求", len(budgets) == 1, str(budgets))

# ---------------------------------------------------------------- 摘要完整性

print("\n=== 摘要完整性：保留预估额度，未完成不自动追加请求 ===")


class CaptureLogs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


async def complete_summary(reply):
    llm = make_llm()
    sent = []
    async def fake_post(payload):
        sent.append(payload.copy())
        return reply
    llm._post = fake_post
    handler = CaptureLogs()
    logger = logging.getLogger("msgx.llm")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        try:
            result = await llm.chat("sys", "完整消息", max_tokens=17342, think=False,
                                    purpose="摘要成稿", source_ids=[123], require_complete=True)
        except IncompleteResponse as exc:
            result = exc
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    return sent, result, handler.records, llm.store.details


for reason, content in [("length", "残缺正文"), ("length", ""),
                        ("content_filter", "待审核正文"), ("aborted", "中途停止"),
                        ("insufficient_system_resource", ""), (None, "缺少结束原因"),
                        ("stop", "   ")]:
    reply = ok_response(content)
    reply["choices"][0]["finish_reason"] = reason
    sent, result, records, details = asyncio.run(complete_summary(reply))
    label = f"{reason}/{bool(content.strip())}"
    check(f"{label} 未完成摘要仅请求一次", len(sent) == 1)
    check(f"{label} 保留摘要计算的 max_tokens", sent[0]["max_tokens"] == 17342)
    check(f"{label} 错误包含原因、输出量、实际额度",
          isinstance(result, IncompleteResponse) and str(reason or "missing") in str(result)
          and "输出 5 Token" in str(result) and "max_tokens=17342" in str(result))
    check(f"{label} 异常保留收到的原文供保存残稿",
          isinstance(result, IncompleteResponse) and result.content == content)
    check(f"{label} 日志明确告警且不报成功",
          len(records) == 1 and records[0].levelno == logging.WARNING
          and "API 请求成功" not in records[0].getMessage()
          and "max_tokens=17342" in records[0].getMessage())
    check(f"{label} 失败仍保留 Token、费用和来源",
          len(details) == 1 and details[0]["usage"]["completion_tokens"] == 5
          and details[0]["cost_cny"] > 0 and details[0]["source_ids"] == [123]
          and details[0]["status"].startswith("输出未完成"))

full_content = "# 总标题\n\n**独立主题**\n" + "完整内容，" * 4000
sent, result, records, details = asyncio.run(complete_summary(ok_response(full_content)))
check("完整长正文不截断、不额外改写", result == full_content and len(sent) == 1)
check("完整响应才记录 API 成功", len(records) == 1 and records[0].levelno == logging.INFO
      and "API 请求成功" in records[0].getMessage() and details[0]["status"] == "完成")

# ---------------------------------------------------------------- 连接层

print("\n=== 重试不能掐死并发请求（v0.5.0 的自我放大 bug）===")


async def retry_keeps_session(llm: LLM):
    calls: list[int] = []

    async def fake_once(payload: dict) -> dict:
        calls.append(1)
        if len(calls) == 1:
            raise _Transient("ServerDisconnectedError: boom")
        return ok_response()

    llm._post_once = fake_once  # type: ignore[method-assign]
    before = await llm.session()
    out = await llm.chat("sys", "user", max_tokens=100)
    after = llm._session
    return calls, out, before, after


calls, out, before, after = asyncio.run(retry_keeps_session(make_llm()))
check("瞬时失败会被重试一次", len(calls) == 2, str(len(calls)))
check("重试后正常返回", out == "好的", out)
check("重试**没有**重建会话（别人的在途请求不会被掐断）", before is after)
check("会话仍是打开状态", after is not None and not after.closed)
asyncio.run(after.close())

llm_probe = make_llm()
asyncio.run(llm_probe.session())
check("会话配置为「不复用连接」（force_close）",
      bool(getattr(llm_probe._session.connector, "_force_close", False)), "connector.force_close")
asyncio.run(llm_probe.close())

check("**危险方法 _reset_session 已被删除**（防止有人再加回来）",
      not hasattr(LLM, "_reset_session"))


async def exhausted(llm: LLM):
    async def always_fail(payload: dict) -> dict:
        raise _Transient("ServerDisconnectedError: boom")

    llm._post_once = always_fail  # type: ignore[method-assign]
    try:
        await llm.chat("sys", "user", max_tokens=100)
    except RuntimeError as e:
        return str(e)
    return ""


msg = asyncio.run(exhausted(make_llm(post_retries=3)))
check("重试用尽后抛出可读错误（含尝试次数）", "已重试 3 次" in msg, msg[:80])

print("\n=== 图片解析不依赖收费能力探测 ===")

async def image_without_probe():
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        llm = make_llm()
        llm.cfg["app"] = {"db_path": str(Path(tmp) / "test.db")}
        old_cache = Path(tmp) / "vision_probe.json"
        old_cache.write_text('{"vision": false}', encoding="utf-8")
        image = Path(tmp) / "image.png"
        Image.new("RGB", (12, 12), "white").save(image)
        sent = []
        async def fake_post(payload):
            sent.append(payload)
            return ok_response("图片中的运行日志")
        llm._post = fake_post
        check("收费探测入口及支持缓存接口已删除",
              not hasattr(llm, "probe_vision") and not hasattr(llm, "vision_supported"))
        check("图片可用性只读配置，不调用模型", llm.vision_available and not sent)
        text, ok = await llm.describe_image(image, "提取图片", source_ids=[12])
        check("旧失败探测缓存不会阻止真实图片解析", ok and text == "图片中的运行日志")
        check("解析图片只有一次请求，没有前置测试图片", len(sent) == 1)
        check("图片解析预留 2000 输出 Token", sent[0]["max_tokens"] == 2000)
        check("解析费用关联原消息", llm.store.details[0]["source_ids"] == [12])
        check("旧探测文件原样保留", old_cache.read_text("utf-8") == '{"vision": false}')
        llm.cfg["media"] = {"vision_enabled": False}
        result = await llm.describe_image(image, "提取图片")
        check("关闭图片解析后不会调用 API", result == ("", False) and len(sent) == 1)
        llm.cfg["media"]["vision_enabled"] = True
        llm.llm["max_daily_cost_cny"] = 0
        result = await llm.describe_image(image, "提取图片")
        check("超出每日预算不会调用图片 API", result == ("", False) and len(sent) == 1)
        llm.configure("")
        check("无 API Key 时图片解析不可用", not llm.vision_available)

asyncio.run(image_without_probe())

print()
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
