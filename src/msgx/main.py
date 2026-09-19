from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from contextlib import suppress

from aiohttp import web

from .config import load
from .onebot import build_app
from .pipeline import Pipeline


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def cmd_run(cfg: dict) -> None:
    pipe = Pipeline(cfg)
    await pipe.start()
    app = build_app(cfg, pipe)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    host = cfg["onebot"]["host"]
    site_ws = web.TCPSite(runner, host, int(cfg["onebot"]["ws_port"]))
    site_http = web.TCPSite(runner, host, int(cfg["onebot"]["http_port"]))
    await site_ws.start()
    await site_http.start()

    log = logging.getLogger("msgx")
    log.info("=" * 62)
    log.info("消息萃取服务已启动")
    log.info("  NapCat 反向 WS  : ws://%s:%s/onebot/ws", host, cfg["onebot"]["ws_port"])
    log.info("  OneBot HTTP 上报: http://%s:%s/onebot", host, cfg["onebot"]["http_port"])
    log.info("  本地注入测试     : POST http://%s:%s/inject", host, cfg["onebot"]["http_port"])
    log.info("  健康检查         : http://%s:%s/health", host, cfg["onebot"]["http_port"])
    if not pipe.llm.available:
        log.warning("  未设置 DEEPSEEK_API_KEY —— 当前为「仅规则」模式")
    log.info("=" * 62)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await pipe.stop()


async def cmd_summary(cfg: dict, kind: str) -> None:
    pipe = Pipeline(cfg)
    try:
        if kind == "daily":
            path = await pipe.summarizer.run_daily()
            print(f"日报已生成: {path}" if path else "没有可汇总的内容")
        else:
            created = await pipe.summarizer.run_periodic(force=True)
            if not created:
                print("没有需要生成摘要的窗口（消息不足或 LLM 不可用）")
            for c in created:
                print(f"[{c['group_id']}] {c['count']} 条 -> {c['path']}")
    finally:
        await pipe.stop()


async def cmd_inject(cfg: dict, payload: str) -> None:
    """把 OneBot 事件直接喂给管道（不经过 HTTP，便于离线调试）。"""
    pipe = Pipeline(cfg)
    try:
        data = json.loads(payload)
        events = data if isinstance(data, list) else [data]
        for ev in events:
            r = await pipe.handle_event(ev)
            print(json.dumps(r, ensure_ascii=False))
    finally:
        await pipe.stop()


async def cmd_vision(cfg: dict, force: bool) -> None:
    pipe = Pipeline(cfg)
    try:
        if not pipe.llm.available:
            print("未配置 api_key，无法探测")
            return
        ok = await pipe.llm.probe_vision(force=force)
        print(f"图片输入支持：{'是' if ok else '否'}")
        if not ok:
            print("→ 已自动降级为「本地 OCR only」模式，功能不受影响。")
    finally:
        await pipe.stop()


async def cmd_stats(cfg: dict) -> None:
    pipe = Pipeline(cfg)
    try:
        print(json.dumps(pipe.health(), ensure_ascii=False, indent=2))
        print("\n用量明细：")
        for r in pipe.store.usage_all()[:14]:
            print(
                f"  {r['day']}  调用 {r['calls']:>4}  输入 {r['in_tokens']:>8}  "
                f"输出 {r['out_tokens']:>7}  ${r['cost_usd']:.4f}"
            )
    finally:
        await pipe.stop()


async def cmd_prune(cfg: dict) -> None:
    pipe = Pipeline(cfg)
    try:
        r = pipe.store.prune(
            int(cfg["retention"]["keep_days_raw"]), int(cfg["retention"]["keep_days_media"])
        )
        print(f"清理完成: {r}")
    finally:
        await pipe.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="msgx", description="QQ 群消息萃取服务")
    ap.add_argument("-c", "--config", default=None, help="配置文件路径（默认 config.toml）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="启动常驻服务")

    p_sum = sub.add_parser("summary", help="立即生成摘要")
    p_sum.add_argument("--kind", choices=["periodic", "daily"], default="periodic")

    p_inj = sub.add_parser("inject", help="直接向管道注入 OneBot 事件（调试用）")
    p_inj.add_argument("json", help="事件 JSON 字符串")

    p_vis = sub.add_parser("vision", help="探测模型是否支持图片输入")
    p_vis.add_argument("--force", action="store_true")

    sub.add_parser("stats", help="查看运行状态与用量")
    sub.add_parser("prune", help="按保留策略清理旧数据")

    args = ap.parse_args(argv)
    cfg = load(args.config)
    setup_logging(cfg["app"]["log_level"])

    coro = {
        "run": lambda: cmd_run(cfg),
        "summary": lambda: cmd_summary(cfg, args.kind),
        "inject": lambda: cmd_inject(cfg, args.json),
        "vision": lambda: cmd_vision(cfg, args.force),
        "stats": lambda: cmd_stats(cfg),
        "prune": lambda: cmd_prune(cfg),
    }[args.cmd]

    with suppress(KeyboardInterrupt):
        asyncio.run(coro())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
