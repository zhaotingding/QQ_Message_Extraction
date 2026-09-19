from __future__ import annotations

import json
import logging

from aiohttp import WSMsgType, web

log = logging.getLogger("msgx.onebot")

MAX_WS_MSG = 16 * 1024 * 1024


def _auth_ok(request: web.Request) -> bool:
    token = str(request.app["cfg"]["onebot"].get("access_token") or "")
    if not token:
        return True
    got = request.query.get("access_token", "")
    if not got:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            got = auth[7:]
    return got == token


async def _health(request: web.Request) -> web.Response:
    return web.json_response(request.app["pipe"].health())


async def _inject(request: web.Request) -> web.Response:
    """本地注入接口：在 NapCat 就绪前用于验证全链路。

    body 可以是单个 OneBot 事件对象，也可以是事件数组。
    """
    pipe = request.app["pipe"]
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "JSON 解析失败"}, status=400)

    events = payload if isinstance(payload, list) else [payload]
    results = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        results.append(await pipe.handle_event(ev))
    return web.json_response({"ok": True, "count": len(results), "results": results})


async def _http_report(request: web.Request) -> web.Response:
    """OneBot HTTP 上报（备用接入方式）。"""
    if not _auth_ok(request):
        return web.Response(status=401, text="unauthorized")
    try:
        event = await request.json()
    except json.JSONDecodeError:
        return web.Response(status=400, text="bad json")
    await request.app["pipe"].submit(event)
    return web.json_response({"status": "ok", "retcode": 0})


async def _ws(request: web.Request) -> web.WebSocketResponse:
    """反向 WebSocket：NapCat 主动连入。"""
    if not _auth_ok(request):
        log.warning("WS 鉴权失败，来自 %s", request.remote)
        return web.Response(status=401, text="unauthorized")

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_WS_MSG)
    await ws.prepare(request)
    pipe = request.app["pipe"]
    peer = str(request.remote)
    pipe.ws_peers.add(peer)
    # 同时登记连接对象：退出时要靠它主动关闭，否则 runner.cleanup() 会
    # 一直等 WS 处理器返回（NapCat 不断线就永远不返回）。
    pipe.ws_conns.add(ws)
    log.info("协议端已连接: %s", request.remote)
    accepted = 0
    try:
        async for raw in ws:
            if raw.type == WSMsgType.TEXT:
                try:
                    event = json.loads(raw.data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("post_type") in ("meta_event", "notice", "request"):
                    continue
                await pipe.submit(event)
                accepted += 1
            elif raw.type == WSMsgType.ERROR:
                log.warning("WS 错误: %s", ws.exception())
                break
    finally:
        # 必须在 finally 里移除，否则断线后 /health 会一直显示「已连接」
        pipe.ws_peers.discard(peer)
        pipe.ws_conns.discard(ws)
        log.info("协议端断开（本次接收 %s 条事件）", accepted)
    return ws


async def _alerts(request: web.Request) -> web.Response:
    level = request.query.get("level")
    limit = int(request.query.get("limit", 50))
    rows = request.app["pipe"].store.recent_alerts(limit, level)
    return web.json_response({"ok": True, "items": [dict(r) for r in rows]})


async def _summary_now(request: web.Request) -> web.Response:
    pipe = request.app["pipe"]
    kind = request.query.get("kind", "periodic")
    if kind == "daily":
        path = await pipe.summarizer.run_daily()
        return web.json_response({"ok": True, "path": path})
    created = await pipe.summarizer.run_periodic(force=True)
    return web.json_response({"ok": True, "created": created})


def build_app(cfg: dict, pipeline) -> web.Application:
    app = web.Application(client_max_size=MAX_WS_MSG)
    app["cfg"] = cfg
    app["pipe"] = pipeline
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_get("/onebot/ws", _ws)
    app.router.add_post("/onebot", _http_report)
    app.router.add_post("/inject", _inject)
    app.router.add_get("/alerts", _alerts)
    app.router.add_post("/summary/now", _summary_now)
    return app
