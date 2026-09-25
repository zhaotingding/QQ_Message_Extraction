from __future__ import annotations

import json
import logging
from datetime import date

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
    if not isinstance(event, dict):
        return web.json_response({"error": "事件必须是对象"}, status=400)
    target = str((request.app["cfg"].get("napcat") or {}).get("account") or "")
    if target and str(event.get("self_id") or "") != target:
        return web.json_response({"error": "非目标 QQ 账号"}, status=403)
    if await request.app["pipe"].submit(event):
        request.app["pipe"].note_protocol_message(event)
    return web.json_response({"status": "ok", "retcode": 0})


async def _ws(request: web.Request) -> web.WebSocketResponse:
    """反向 WebSocket：NapCat 主动连入。"""
    if not _auth_ok(request):
        log.warning("WS 鉴权失败，来自 %s", request.remote)
        return web.Response(status=401, text="unauthorized")

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_WS_MSG)
    target = str((request.app["cfg"].get("napcat") or {}).get("account") or "")
    if target and request.headers.get("X-Self-ID") != target:
        log.warning("拒绝非目标 QQ 的协议连接，请核对设置中的目标 QQ 号")
        return web.Response(status=403, text="account mismatch")
    await ws.prepare(request)
    pipe = request.app["pipe"]
    peer = str(request.remote)
    pipe.ws_peers.add(peer)
    # 同时登记连接对象：退出时要靠它主动关闭，否则 runner.cleanup() 会
    # 一直等 WS 处理器返回（NapCat 不断线就永远不返回）。
    pipe.ws_conns.add(ws)
    pipe.ws_online[ws] = None
    pipe.ws_addresses[ws] = peer
    log.info("协议端已连接: %s", request.remote)
    pipe.on_peer_connected()
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
                if target and "post_type" in event and str(event.get("self_id") or "") != target:
                    await ws.close(code=1008, message=b"account mismatch")
                    break
                # OneBot API 响应（带 echo、无 post_type）不是事件：
                # 它是对我们 call_action 的应答，必须先于事件处理拦截，否则
                # 会被当成消息提交、然后被「非群消息」静默丢弃。
                if "echo" in event and "post_type" not in event:
                    pipe.resolve_action(event)
                    continue
                if event.get("post_type") == "meta_event":
                    status = event.get("status")
                    if event.get("meta_event_type") == "heartbeat" and isinstance(status, dict):
                        online = status.get("online")
                        if isinstance(online, bool):
                            pipe.ws_online[ws] = online
                            pipe._protocol_checked = 0.0
                    continue
                if event.get("post_type") in ("notice", "request"):
                    continue
                if event.get("post_type") == "message":
                    pipe.ws_online[ws] = True
                if await pipe.submit(event):
                    pipe.note_protocol_message(event)
                accepted += 1
            elif raw.type == WSMsgType.ERROR:
                log.warning("WS 错误: %s", ws.exception())
                break
    finally:
        # 必须在 finally 里移除，否则断线后 /health 会一直显示「已连接」
        pipe.ws_conns.discard(ws)
        pipe.ws_online.pop(ws, None)
        pipe.ws_addresses.pop(ws, None)
        # 多条连接可能来自同一个 IP；其中一条断开不能抹掉仍在线的连接。
        pipe.ws_peers.clear()
        pipe.ws_peers.update(addr for conn, addr in pipe.ws_addresses.items() if not conn.closed)
        reason = "服务主动停止" if pipe._closing_ws or pipe._stop.is_set() else "连接关闭"
        pipe.ws_last_close = f"{reason}，关闭码 {ws.close_code}，错误 {ws.exception() or '无'}"
        log.info("协议端断开（本次接收 %s 条事件；%s）", accepted, pipe.ws_last_close)
        pipe.on_peer_disconnected()
    return ws


async def _alerts(request: web.Request) -> web.Response:
    level = request.query.get("level")
    limit = int(request.query.get("limit", 50))
    rows = request.app["pipe"].store.recent_alerts(limit, level)
    return web.json_response({"ok": True, "items": [dict(r) for r in rows]})


async def _summary_now(request: web.Request) -> web.Response:
    try:
        return await _summary_request(request)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:
        log.warning("摘要请求失败: %s", exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=502)


def _summary_dates(start: str, end: str) -> tuple[str, str]:
    try:
        start_day, end_day = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise ValueError("日期必须采用 YYYY-MM-DD 格式") from exc
    if start_day.isoformat() != start or end_day.isoformat() != end:
        raise ValueError("日期必须采用 YYYY-MM-DD 格式")
    if start_day > end_day:
        raise ValueError("开始日期不能晚于结束日期")
    return start_day.isoformat(), end_day.isoformat()


async def _summary_request(request: web.Request) -> web.Response:
    pipe = request.app["pipe"]
    kind = request.query.get("kind", "digest")
    if kind not in ("digest", "full", "range", "by_date"):
        return web.json_response({"ok": False, "error": "不支持此摘要类型"}, status=400)
    options = {}
    data = {}
    if request.can_read_body:
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "摘要设置必须是 JSON 对象"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"ok": False, "error": "摘要设置必须是 JSON 对象"}, status=400)
        if "prompt" in data:
            if not isinstance(data["prompt"], str):
                return web.json_response({"ok": False, "error": "prompt 必须是文本"}, status=400)
            options["prompt"] = data["prompt"]
        if "target_chars" in data:
            value = data["target_chars"]
            if type(value) is not int or not 1 <= value <= 100000:
                return web.json_response({"ok": False, "error": "目标字数必须是 1 到 100000 的整数"}, status=400)
            options["target_chars"] = value
    if kind == "by_date":
        start, end = _summary_dates(str(data.get("start_date") or ""),
                                    str(data.get("end_date") or ""))
        force = data.get("force", False)
        if not isinstance(force, bool):
            return web.json_response({"ok": False, "error": "force 必须是布尔值"}, status=400)
        item = await pipe.summarizer.run_dates(start, end, force=force, **options)
        return web.json_response({"ok": True, "created": [item] if item else []})
    if kind == "range":
        # 用户指定的统计窗口，不会创建定时摘要任务。
        try:
            hours = int(request.query.get("hours", 24))
        except ValueError:
            return web.json_response({"ok": False, "error": "hours 必须是整数"}, status=400)
        if hours <= 0:
            return web.json_response({"ok": False, "error": "hours 必须为正"}, status=400)
        item = await pipe.summarizer.run_digest(full=True, hours=hours, **options)
        return web.json_response({"ok": True, "created": [item] if item else []})
    # digest（默认）= 接着上次已总结过的消息做增量纪要；full = 近 24h 全量重做
    item = await pipe.summarizer.run_digest(full=(kind == "full"), **options)
    return web.json_response({"ok": True, "created": [item] if item else []})


async def _summary_days(request: web.Request) -> web.Response:
    try:
        start, end = _summary_dates(request.query.get("start_date", ""),
                                    request.query.get("end_date", ""))
        status = request.app["pipe"].summarizer.daily_status(start, end)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return web.json_response({"ok": True, **status})


async def _messages_recent(request: web.Request) -> web.Response:
    """界面「实时消息」用：最近入库的消息（新的在前）。"""
    try:
        limit = int(request.query.get("limit", 60))
    except ValueError:
        limit = 60
    limit = max(1, min(limit, 300))
    images_only = request.query.get("images", "") == "1"
    rows = request.app["pipe"].store.recent_messages(limit, images_only=images_only)
    return web.json_response({"ok": True, "items": [dict(r) for r in rows]})


def _local_edit_allowed(request: web.Request) -> bool:
    return request.content_type == "application/json" and not request.headers.get("Origin")


async def _message_vision(request: web.Request) -> web.Response:
    if not _local_edit_allowed(request):
        return web.json_response({"ok": False, "error": "仅接受本地程序的 JSON 请求"}, status=403)
    try:
        result = await request.app["pipe"].media.manual_vision(int(request.match_info["image_id"]))
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except RuntimeError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=502)
    return web.json_response({"ok": True, **result})


async def _message_summary_include(request: web.Request) -> web.Response:
    if not _local_edit_allowed(request):
        return web.json_response({"ok": False, "error": "仅接受本地程序的 JSON 请求"}, status=403)
    try:
        include = (await request.json())["include"]
    except (ValueError, KeyError, TypeError):
        include = None
    if not isinstance(include, bool):
        return web.json_response({"ok": False, "error": "include 必须是布尔值"}, status=400)
    changed = request.app["pipe"].store.set_image_include(int(request.match_info["image_id"]), include)
    if not changed:
        return web.json_response({"ok": False, "error": "找不到可设置的图片消息"}, status=404)
    return web.json_response({"ok": True, "include": include})


async def _billing(request: web.Request) -> web.Response:
    """真实账单：官方余额 + 平台逐日消费（有 token 时）。"""
    pipe = request.app["pipe"]
    try:
        data = await pipe.billing_snapshot(refresh=True)
    except Exception as e:  # noqa: BLE001
        return web.json_response({"ok": False, "error": str(e)}, status=502)
    return web.json_response({"ok": True, **data})


async def _usage_details(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, **request.app["pipe"].store.api_usage_details(active_only=True)})


async def _usage_sources(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, **request.app["pipe"].store.api_call_sources(int(request.match_info["call_id"]))})


async def _groups_refresh(request: web.Request) -> web.Response:
    """通过协议端拉取真实群名并写库。协议端不在线时返回 502，由界面回退本地库。

    带 ``?prune=1`` 时顺手删掉「不属于当前账号且没有消息」的群 ——
    这就是「只关注目前已登录账号的群」。**默认不删**：自动路径不能有副作用，
    删数据必须由用户显式点一下。
    """
    pipe = request.app["pipe"]
    prune = str(request.query.get("prune", "")).lower() in ("1", "true", "yes")
    try:
        r = await pipe.refresh_group_names(prune=prune)
    except Exception as e:  # noqa: BLE001
        return web.json_response({"ok": False, "error": str(e)}, status=502)
    return web.json_response({"ok": True, "groups": r["count"], **r})


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
    app.router.add_get("/messages/recent", _messages_recent)
    app.router.add_post(r"/images/{image_id:\d+}/vision", _message_vision)
    app.router.add_post(r"/images/{image_id:\d+}/summary-include", _message_summary_include)
    app.router.add_get("/billing", _billing)
    app.router.add_get("/usage/details", _usage_details)
    app.router.add_get(r"/usage/calls/{call_id:\d+}", _usage_sources)
    app.router.add_post("/summary/now", _summary_now)
    app.router.add_get("/summary/days", _summary_days)
    app.router.add_post("/groups/refresh", _groups_refresh)
    return app
