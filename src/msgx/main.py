from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from contextlib import suppress
from pathlib import Path

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


async def cmd_run(cfg: dict, stop_file: str | None = None) -> None:
    """启动常驻服务。

    stop_file 用于「协作退出」：界面点停止时只创建一个文件，由本进程在两次
    检查之间自行收尾退出。比直接 kill 安全 —— 不会打断正在写库或下载图片的动作。
    """
    log = logging.getLogger("msgx")
    pipe = Pipeline(cfg)
    await pipe.start()
    app = build_app(cfg, pipe)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    host = cfg["onebot"]["host"]
    ws_port = int(cfg["onebot"]["ws_port"])
    http_port = int(cfg["onebot"]["http_port"])
    site_ws = web.TCPSite(runner, host, ws_port)
    site_http = web.TCPSite(runner, host, http_port)

    # 端口被占用是「已经有一个服务在跑」最常见的表现，而原始 OSError 栈对用户毫无意义
    # （实测用户看到「winerror 10048 通常每个套接字地址只允许使用一次」就卡住了）。
    for site, port, what in ((site_ws, ws_port, "反向 WS"), (site_http, http_port, "HTTP 上报")):
        try:
            await site.start()
        except OSError as exc:
            log.error("启动失败：%s 端口 %s 无法绑定 —— %s", what, port, exc.strerror or exc)
            log.error("  最常见的原因：**已经有一个服务实例在运行**。")
            log.error("  确认方法：浏览器打开 http://127.0.0.1:%s/health", http_port)
            log.error("           能返回 JSON 就说明服务已在运行，无需重复启动。")
            log.error("  若确实没有实例，用下面命令看谁占了端口：")
            log.error("           netstat -ano | findstr :%s", port)
            await runner.cleanup()
            await pipe.stop()
            raise SystemExit(2) from exc

    log.info("=" * 62)
    log.info("消息萃取服务已启动")
    log.info("  NapCat 反向 WS  : ws://%s:%s/onebot/ws", host, ws_port)
    log.info("  OneBot HTTP 上报: http://%s:%s/onebot", host, http_port)
    log.info("  本地注入测试     : POST http://%s:%s/inject", host, http_port)
    log.info("  健康检查         : http://%s:%s/health", host, http_port)
    if not pipe.llm.available:
        log.warning("  未设置 DEEPSEEK_API_KEY —— 当前为「仅规则」模式")
    log.info("=" * 62)

    stop_evt = asyncio.Event()
    watcher: asyncio.Task | None = None
    if stop_file:
        marker = Path(stop_file)

        async def _watch_stop() -> None:
            while not stop_evt.is_set():
                if marker.exists():
                    log.info("检测到停止信号，正在安全退出…")
                    stop_evt.set()
                    return
                await asyncio.sleep(1)

        watcher = asyncio.create_task(_watch_stop(), name="stop-watch")
        log.info("停止信号文件: %s", marker)

    try:
        await stop_evt.wait()
    finally:
        if watcher is not None:
            watcher.cancel()
        # 顺序不能反：必须先主动关掉反向 WS，再 runner.cleanup()。
        # cleanup() 会等待所有活动连接处理完毕，而反向 WS 的处理器是
        # `async for raw in ws` —— NapCat 只要不断线，它就永远不返回。
        # 实测：NapCat 未连入时退出 0.6s；NapCat 连着时要卡 39s，
        # 直到 aiohttp 心跳报 "No PONG received after 15.0 seconds" 才解开。
        closed = await pipe.close_ws_conns()
        if closed:
            log.info("已关闭 %s 个协议端连接，开始收尾", closed)
        try:
            await asyncio.wait_for(runner.cleanup(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("服务端清理超时（10s），继续退出")
        try:
            await asyncio.wait_for(pipe.stop(), timeout=15)
        except asyncio.TimeoutError:
            log.warning("管道收尾超时（15s），继续退出")


async def cmd_summary(cfg: dict, kind: str, hours: int = 24) -> None:
    pipe = Pipeline(cfg)
    try:
        if kind == "daily":
            path = await pipe.summarizer.run_daily()
            print(f"日报已生成: {path}" if path else "没有可汇总的内容")
        elif kind == "full":
            # full = 按时间范围重做。目标字数由窗口长度决定（见 summarize.DIGEST_RANGES）
            item = await pipe.summarizer.run_digest(full=True, hours=hours)
            label = f"近 {hours} 小时" if hours < 24 else f"近 {hours // 24} 天"
            if item:
                print(
                    f"{label}纪要已生成: {item['path']}"
                    f"（{item['count']} 条消息 / {item['chars']} 字 / 目标 {item['target_chars']} 字）"
                )
            else:
                print(f"{label}内没有可总结的消息")
        elif kind == "digest":
            item = await pipe.summarizer.run_digest()
            print(f"群消息纪要已生成: {item['path']}" if item else "自上次纪要以来没有新消息")
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
        # 先主动抓一次账单：命令行走不到后台刷新循环，不抓的话 health 里的
        # billing 永远是空的，用户会以为这功能没生效。
        snap = await pipe.billing_snapshot(refresh=True)
        print(json.dumps(pipe.health(), ensure_ascii=False, indent=2))

        src = "平台真实值" if snap.get("source") == "platform" else "本地估算"
        print(
            f"\n花费（{src}）：今日 ¥{float(snap.get('today_cny') or 0):.4f} · "
            f"累计 ¥{float(snap.get('total_cny') or 0):.4f}"
            + (f"（自 {snap['from_day']}）" if snap.get("from_day") else "")
        )
        if snap.get("platform_error"):
            print(f"  平台数据不可用的原因：{snap['platform_error']}")
        bal = snap.get("balance") or {}
        if bal.get("ok"):
            print(f"  账户余额：¥{float(bal.get('total') or 0):.4f} {bal.get('currency') or ''}")
        else:
            print(f"  账户余额：读取失败（{bal.get('error') or '不可用'}）")

        print("\n用量明细：")
        for r in pipe.store.usage_all()[:14]:
            print(
                f"  {r['day']}  调用 {r['calls']:>4}  输入 {r['in_tokens']:>8}"
                f"（命中 {r['cache_hit_tokens']:>7}）  输出 {r['out_tokens']:>7}  ¥{r['cost_cny']:.4f}"
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

    # --stop-file 必须挂在 run 子命令上。挂在顶层的话，写成
    # `msgx run --stop-file x` 时该参数位于子命令之后，会由子命令的解析器处理
    # 而报 "unrecognized arguments"。
    p_run = sub.add_parser("run", help="启动常驻服务")
    p_run.add_argument(
        "--stop-file",
        default=None,
        help="该文件一旦出现，服务就安全退出。供图形界面点「停止服务」时使用",
    )
    sub.add_parser("gui", help="打开图形界面（配置 + 启停 + 实时日志）")

    p_sum = sub.add_parser("summary", help="立即生成摘要")
    p_sum.add_argument(
        "--kind", choices=["digest", "full", "periodic", "daily"], default="digest",
        help="digest=增量纪要（默认）；full=按 --hours 范围重做；periodic/daily=按群摘要/日报",
    )
    p_sum.add_argument(
        "--hours", type=int, default=24,
        help="仅 kind=full 时生效：回看多少小时（24/72/168/720 分别对应界面上的 1 天~1 个月）",
    )

    p_inj = sub.add_parser("inject", help="直接向管道注入 OneBot 事件（调试用）")
    p_inj.add_argument("json", help="事件 JSON 字符串")

    p_vis = sub.add_parser("vision", help="探测模型是否支持图片输入")
    p_vis.add_argument("--force", action="store_true")

    sub.add_parser("stats", help="查看运行状态与用量")
    sub.add_parser("prune", help="按保留策略清理旧数据")

    args = ap.parse_args(argv)

    # 图形界面有自己的事件循环，不能塞进 asyncio.run
    if args.cmd == "gui":
        from .gui import main as gui_main

        return gui_main(args.config)

    cfg = load(args.config)
    setup_logging(cfg["app"]["log_level"])

    coro = {
        "run": lambda: cmd_run(cfg, args.stop_file),
        "summary": lambda: cmd_summary(cfg, args.kind, args.hours),
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
