"""用「更难的输入」复现「输出被截断且正文为空」。

上一版用一条清晰的技术消息测不出来（思考只花 26~42 token）。
真实打分请求还有两种更难的形态：
  A. 附带最多 1500 字的 **OCR 文本**（报错截图）→ 模型要在噪声里判断价值
  B. 极其模糊的短消息（一个裸链接 / 一句寒暄）→ 要在「留存 or 丢弃」上反复权衡

跑法：.venv\\Scripts\\python.exe tools\\probe_truncation2.py
"""
from __future__ import annotations

import asyncio
import os
import sys

import aiohttp

URL = "https://api.deepseek.com/chat/completions"
KEY = os.environ.get("DEEPSEEK_API_KEY", "")
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
MODEL = os.environ.get("MSGX_PROBE_MODEL", "deepseek-flash")

SYS = (
    "你是技术社群的信息过滤器，为一位科研+工程双线作战的用户服务。\n"
    "用户不希望被闲聊打扰，也不想错过有价值的沉淀；但他的注意力极其有限，\n"
    "只有真正紧急的事才值得打断他手头的工作。\n\n"
    "请判断两个相互独立的问题，不要互相影响：\n\n"
    "一、信息价值 score（0-10）：这条消息值不值得留存、进摘要、以后检索？\n"
    "  8-10：可直接复用的资源（仓库 / 论文 / 数据集 / 模型权重 / 工具），\n"
    "        或可复现的经验（配置参数 / 踩坑记录 / 性能数据 / 版本兼容结论）\n"
    "  4-7 ：有信息量的技术讨论、具体提问、有链接但未知内容\n"
    "  0-3 ：寒暄、表情包、情绪输出、闲聊、接龙、签到、无上下文附和\n\n"
    "二、紧迫性 urgent（true / false）：这条消息是否必须立刻打断用户？\n"
    "  默认 false。只有「几十分钟内不知道就会造成损失」才算 true，仅包括：\n"
    "  - 用户在群中被点名提问，正等着他回答\n"
    "  - 有人直接回答了用户先前提出的问题\n"
    "  - 限时机会：内推 / 报名截止、线上活动即将开始\n"
    "  - 用户正在处理的问题出现了可用解法\n"
    "  以下虽然高价值，但一律 urgent=false：\n"
    "  分享开源仓库、转发论文、常规招聘信息、经验总结、技术讨论、资料合集。\n\n"
    '只输出 JSON，不要任何其它内容：\n'
    '{"score": <0-10 的数字>, "urgent": <true 或 false>, "reason": "<不超过 30 字的中文理由>"}'
)

OCR_NOISE = (
    "Traceback (most recent call last):\n"
    "  File \"train.py\", line 412, in <module>\n"
    "    main()\n"
    "  File \"train.py\", line 388, in main\n"
    "    model = build_model(cfg)\n"
    "  File \"/opt/conda/lib/python3.11/site-packages/torch/nn/modules/module.py\", line 1736\n"
    "RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB "
    "(GPU 0; 23.65 GiB total capacity; 21.10 GiB already allocated; 1.02 GiB free; "
    "21.44 GiB reserved in total by PyTorch)\n"
    "If reserved but unallocated memory is large try setting "
    "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True\n"
    "torch.cuda.OutOfMemoryError: CUDA out of memory\n"
    "报错 失败 异常 OOM nvidia-smi 显存占用 98% 进程 12345 python train.py "
    "batch_size=8 grad_accum=4 lr=2e-5 warmup=500 epochs=3 "
    "Error: NCCL timeout / CommunicationError rank 0 out of 8 "
    "wandb: ERROR Failed to upload, retrying 1 2 3 "
    "Traceback ... 2000 行省略 ..."
)

CASES = [
    ("清晰技术消息（基线）", "群：大模型交流\n发送者：甲\n内容：PyTorch 2.8 显存涨 1.8G，加 expandable_segments:True 解决。\n"),
    ("附 1500 字报错 OCR", f"群：大模型交流\n发送者：乙\n内容：[图片]\n该消息附带图片，图片中识别出的文字：\n{OCR_NOISE}\n"),
    ("裸链接", "群：大模型交流\n发送者：丙\n内容：https://github.com/karpathy/nanoGPT\n"),
    ("无上下文附和", "群：大模型交流\n发送者：丁\n内容：对\n"),
    ("长混合讨论", "群：大模型交流\n发送者：戊\n内容：" + "这个和那个的区别我之前也想过，" * 40 + "\n"),
]


async def call(session, label: str, body: str, max_tokens: int, think: bool) -> bool:
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": body}],
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "stream": False,
    }
    if not think:
        payload["reasoning_effort"] = "none"
    try:
        async with session.post(URL, headers=HDR, json=payload) as r:
            data = await r.json()
    except Exception as e:  # noqa: BLE001
        print(f"  {label:20s} tok={max_tokens:<5d} 异常 {type(e).__name__}: {e}")
        return False
    ch = (data.get("choices") or [{}])[0]
    content = (ch.get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    det = usage.get("completion_tokens_details") or {}
    empty = not content.strip()
    flag = "  <<< 空正文 + 截断" if (empty and ch.get("finish_reason") == "length") else ""
    print(
        f"  {label:20s} tok={max_tokens:<5d} finish={str(ch.get('finish_reason')):7s} "
        f"正文{len(content):4d}字 completion={usage.get('completion_tokens')} "
        f"思考={det.get('reasoning_tokens')}{flag}"
    )
    return empty and ch.get("finish_reason") == "length"


async def main() -> int:
    if not KEY:
        print("缺少 DEEPSEEK_API_KEY")
        return 2
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240))
    hit = 0
    try:
        for label, body in CASES:
            print(f"\n【{label}】")
            for tok in (400, 1200):
                if await call(session, "思考开", body, tok, True):
                    hit += 1
            await call(session, "思考关", body, 400, False)
    finally:
        await session.close()
    print(f"\n复现到「空正文+截断」共 {hit} 次")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
