"""Electron 消息页面使用的本地操作。群操作和识别仍由接收服务执行。"""
from __future__ import annotations

from pathlib import Path

from msgx import prefs
from msgx.message_keywords import compile_rules, matching_message
from msgx.ui.common import open_path


def _saved_rules(ctx):
    return prefs.load(ctx.prefs_file).get("message_keywords") or []


def _decorate(rows, rules):
    for row in rows:
        row["keyword_matches"] = matching_message(rules, row)
        row["plain_text"] = prefs.plain_text(row.get("text") or "")
    return rows


def _account(ctx) -> str:
    return str(ctx.cfg["napcat"].get("account") or "")


def _recent(ctx, payload):
    limit = max(1, min(int(payload.get("limit", 200)), 300))
    images_only = payload.get("images") is True
    group = str(payload.get("group_id", ""))
    sender = str(payload.get("sender_id", ""))
    view = payload.get("view", "all")
    trend = str(payload.get("activity_group", ""))
    days = int(payload.get("activity_days", 14))
    days = days if days in (7, 14, 30) else 14
    live = ctx.service_running
    if view in ("list", "related"):
        # 首屏直接读取本机 WAL，无须等待协议健康、稀疏关联查询或月度统计。
        cfg = ctx.service_cfg if live and ctx.service_cfg else ctx.cfg
        account = str(cfg["napcat"].get("account") or "")
        data = {"items": [], "related": [], "groups": [], "related_account": account, "live": live}
        try:
            with ctx.read_store(cfg) as store:
                store.conn.execute("BEGIN")  # 消息、逐图信息和选群范围取自同一 WAL 快照。
                if view == "list" or account:
                    data["items" if view == "list" else "related"] = store.recent_messages(
                        limit, images_only=images_only, group_id=group, sender_id=sender,
                        account=account, related_only=view == "related")
                data["groups"] = store.message_group_choices(account, cfg["watch"].get("groups"))
        except FileNotFoundError:
            pass
    elif live:
        query = {"limit": limit, "images": int(images_only), "group_id": group,
                 "activity_group": trend, "activity_days": days,
                 "activity_refresh": int(payload.get("force_activity") is True), "view": view}
        data = ctx.request("/messages/recent", query=query, timeout=10)
    else:
        # 停止服务后仍可查看已入库消息。只读连接保持历史文件原样。
        account = _account(ctx)
        data = {"items": [], "related": [], "related_account": account, "activity": {}}
        try:
            with ctx.read_store() as store:
                if view != "activity":
                    data["items"] = store.recent_messages(limit, images_only=images_only, group_id=group, account=account)
                if account and view != "activity":
                    data["related"] = store.recent_messages(limit, group_id=group, account=account, related_only=True)
                data["activity"] = store.group_activity(account=account, watch_groups=ctx.cfg["watch"]["groups"],
                                                         trend_group=trend, days=days)
        except FileNotFoundError:
            pass
    try:
        rules = compile_rules(_saved_rules(ctx))
    except ValueError as exc:
        # 镜像旧窗口行为：坏规则需要修正，正常消息仍然可读。
        rules = ()
        data["keyword_error"] = str(exc)
    data["items"] = _decorate(data["items"], rules)
    data["related"] = _decorate(data["related"], rules)
    data["live"] = live
    return data


def _open_image(ctx, payload):
    # 特权操作只接受数据库记录 ID，渲染层无法指定任意文件路径。
    with ctx.read_store() as store:
        image = store.get_image(int(payload["image_id"]))
    if image is None or not image["media_path"]:
        raise ValueError("找不到这条图片记录的本地原图")
    path = Path(image["media_path"]).resolve()
    if path.drive.startswith("\\"):
        raise ValueError("请使用本地原图文件")
    if not path.is_file():
        raise FileNotFoundError("本地图片已不存在")
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ico"}:
        raise ValueError("该文件的扩展名不属于支持的图片格式")
    open_path(path)
    return {"ok": True}


def handle(ctx, action, payload):
    if action == "messages.recent":
        return _recent(ctx, payload)
    if action == "messages.rules-read":
        return {"rules": _saved_rules(ctx)}
    if action == "messages.rules-save":
        rules = compile_rules(payload["text"].splitlines())
        labels = [label for label, _ in rules]
        with ctx.prefs_lock:
            prefs.update({"message_keywords": labels}, ctx.prefs_file)
        return {"rules": labels}
    if action in ("messages.keywords", "messages.context"):
        rules = compile_rules(_saved_rules(ctx))
        try:
            with ctx.read_store() as store:
                if action == "messages.context":
                    cfg = ctx.service_cfg if ctx.service_running else ctx.cfg
                    account = str(cfg["napcat"].get("account") or "")
                    items = store.messages_around(int(payload["id"]), account)
                else:
                    items = store.recent_messages(201, group_id=str(payload.get("group_id", "")), sender_id=str(payload.get("sender_id", "")),
                                                  keyword_rules=rules, offset=max(0, int(payload.get("offset", 0))))
        except FileNotFoundError:
            items = []
        return {"items": _decorate(items[:200], rules), "more": len(items) > 200}
    if action == "messages.image-open":
        return _open_image(ctx, payload)
    if action in ("messages.vision", "messages.summary-include"):
        image_id = int(payload["image_id"])
        vision = action == "messages.vision"
        endpoint = "vision" if vision else "summary-include"
        body = {} if vision else {"include": payload["include"]}
        return ctx.request(f"/images/{image_id}/{endpoint}", method="POST", payload=body,
                           timeout=960 if vision else 120)
    if action == "messages.group-preview":
        kind = payload["kind"]
        # 统计范围取实际保存配置，账号或范围改变时由服务端校验拒绝。
        body = {"kind": kind, "account": _account(ctx),
                "watch_groups": sorted(str(gid) for gid in ctx.cfg["watch"]["groups"])}
        if kind == "broadcast":
            body["text"] = payload["text"]
        elif kind == "leave":
            body["group_id"] = str(payload["group_id"])
        data = ctx.request("/groups/operations/preview", method="POST", payload=body, timeout=35)
        saved = prefs.load(ctx.prefs_file).get("broadcast_groups")
        data["remembered"] = saved if isinstance(saved, list) else []
        return data
    if action == "messages.group-confirm":
        body = {"token": payload["token"], "confirmed": payload["confirmed"] is True}
        if "groups" in payload:
            body["groups"] = payload["groups"]
        data = ctx.request("/groups/operations/confirm", method="POST", payload=body, timeout=35)
        job = data.get("result")
        if job and job["kind"] == "broadcast":
            # 仅保存服务端已接受的勾选；写配置失败不改变已经创建的发送任务。
            try:
                with ctx.prefs_lock:
                    prefs.update({"broadcast_groups": [g["group_id"] for g in job["groups"]]}, ctx.prefs_file)
            except (ValueError, OSError) as exc:
                data["preference_warning"] = f"勾选历史保存失败：{exc}"
        return data
    if action == "messages.group-status":
        job_id = str(payload.get("job_id", ""))
        if not ctx.service_running:
            # 读取最近持久化结果不会启动接收服务或重新提交外部操作。
            try:
                with ctx.read_store() as store:
                    job = store.group_job(job_id)
            except FileNotFoundError:
                job = None
            return {"result": job, "offline": True}
        return ctx.request("/groups/operations/status", query={"job_id": job_id}, timeout=8)
    if action == "messages.group-cancel":
        return ctx.request("/groups/operations/cancel", method="POST", payload={"job_id": payload["job_id"]}, timeout=8)
    raise ValueError(f"未知消息页面操作：{action}")
