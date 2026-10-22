from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import date

from aiohttp import WSMsgType, web

from .store import Store

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
    pipe.register_ws(ws, peer)
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
                            pipe.note_ws_online(ws, online)
                    continue
                if event.get("post_type") in ("notice", "request"):
                    continue
                if event.get("post_type") == "message":
                    pipe.note_ws_online(ws, True)
                if await pipe.submit(event):
                    pipe.note_protocol_message(event)
                accepted += 1
            elif raw.type == WSMsgType.ERROR:
                log.warning("WS 错误: %s", ws.exception())
                break
    finally:
        # 必须在 finally 里移除，否则断线后 /health 会一直显示「已连接」
        reason = pipe.unregister_ws(ws)
        log.info("协议端断开（本次接收 %s 条事件；%s）", accepted, reason)
    return ws


async def _alerts(request: web.Request) -> web.Response:
    level = request.query.get("level")
    limit = int(request.query.get("limit", 50))
    rows = request.app["pipe"].store.recent_alerts(limit, level)
    return web.json_response({"ok": True, "items": [dict(r) for r in rows]})


async def _summary_now(request: web.Request) -> web.Response:
    return await _summary_request(request)


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
    return web.json_response({
        "ok": False, "error": "手动摘要入口已移除，日报、周报、月报由自动计划生成",
    }, status=410)


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
    group_id = request.query.get("group_id", "")
    pipe = request.app["pipe"]
    account = str(getattr(pipe, "target_account", "") or getattr(pipe, "bot_uin", ""))

    def query(store):
        # 一个只读事务让消息、本人关联和群统计使用相同快照；不借用接收线程的连接。
        return {"ok": True,
                "items": [dict(row) for row in store.recent_messages(limit, images_only=images_only,
                           group_id=group_id, account=account)],
                "related": store.recent_messages(limit, group_id=group_id, account=account,
                            related_only=True) if account else [],
                "related_account": account, "activity": store.group_activity()}

    return web.json_response(await _read_store(pipe.store, query))


async def _read_store(source: Store, query):
    """耗时只读查询在线程内独占连接，避免历史筛选阻塞 WS 接收与健康响应。"""
    path, filters = source.db_path, copy.deepcopy(source.filters)

    def read():
        store = Store(path, filters=filters, readonly=True)
        try:
            store.conn.execute("BEGIN")
            return query(store)
        finally:
            store.close()

    return await asyncio.to_thread(read)


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
    data = await _read_store(request.app["pipe"].store, lambda store: store.api_usage_details(active_only=True))
    return web.json_response({"ok": True, **data})


async def _usage_sources(request: web.Request) -> web.Response:
    call_id = int(request.match_info["call_id"])
    data = await _read_store(request.app["pipe"].store, lambda store: store.api_call_sources(call_id))
    return web.json_response({"ok": True, **data})


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


async def _summary_retry(request: web.Request) -> web.Response:
    # GUI 本机 JSON 请求；拒绝浏览器跨站请求，避免意外触发收费补生成。
    if request.remote not in ("127.0.0.1", "::1") or request.headers.get("Origin") or not _auth_ok(request):
        return web.json_response({"ok": False, "error": "仅允许本机授权操作"}, status=403)
    if request.content_type != "application/json":
        return web.json_response({"ok": False, "error": "需要 JSON 请求"}, status=400)
    try:
        body = await request.json()
        result = request.app["pipe"].retry_summary_job(
            str(body["kind"]), str(body["start_date"]), str(body["end_date"]),
            confirmed=body.get("confirmed") is True, revision=float(body["updated_at"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        return web.json_response({"ok": False, "error": "未确认或任务状态已变化，请刷新后重新确认"}, status=409)
    return web.json_response(result)


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
    app.router.add_post("/summary/retry", _summary_retry)
    app.router.add_post("/groups/refresh", _groups_refresh)
    return app
