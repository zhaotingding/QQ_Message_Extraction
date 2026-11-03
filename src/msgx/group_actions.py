"""群发与退群：预览固定范围、一次确认、逐群留痕；不自动重发外部操作。"""
from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from pathlib import Path

from . import security, yaml_config
from .protocol_session import ActionRejected


class GroupActions:
    def __init__(self, pipe, *, prefs_path=None, delay=1.0):
        self.pipe = pipe
        self.prefs_path = Path(prefs_path) if prefs_path else None
        self.delay = delay
        self.previews = {}
        self.task = None
        self.busy = False
        self.cancelled = False
        self.active_id = ""
        self.pipe.store.recover_group_jobs()

    def _scope(self):
        account = str(self.pipe.target_account or "")
        watch = tuple(sorted(self.pipe.watch))
        if not account.isascii() or not account.isdigit() or account == "0":
            raise ValueError("请先保存明确的目标 QQ 账号并重启服务")
        if self.prefs_path:
            data = yaml_config.read(self.prefs_path)  # 损坏/丢失配置直接失败，不能退回默认全部群。
            if not isinstance(data, dict):
                raise ValueError("本地配置不可读取，操作已阻止")
            if "qq_account" not in data or not isinstance(data.get("groups"), list):
                raise ValueError("本地配置缺少账号或统计群字段，请在设置中保存后重新预览")
            saved_watch = tuple(sorted(str(gid) for gid in data["groups"]))
            if str(data["qq_account"]) != account or saved_watch != watch:
                raise ValueError("保存的账号或统计群已变化，请重启服务后重新预览")
        return account, watch

    async def _members(self, connection, account):
        info = await self.pipe.call_action("get_login_info", {}, connection=connection)
        if str((info or {}).get("user_id", "")) != account:
            raise ValueError("实际登录账号与确认账号不一致，已阻止操作")
        rows = await self.pipe.call_action("get_group_list", {"no_cache": True}, connection=connection)
        if not isinstance(rows, list):
            raise ValueError("未取得有效的当前群列表")
        groups = {}
        for row in rows:
            gid = str(row.get("group_id", "")) if isinstance(row, dict) else ""
            if not gid.isascii() or not gid.isdigit() or int(gid) <= 0:
                raise ValueError("协议返回了无效群号，操作已阻止")
            groups[gid] = str(row.get("group_name") or "未命名群")
        return groups

    async def prepare(self, body: dict) -> dict:
        if self.busy:
            raise ValueError("已有群操作正在执行，请等待完成")
        kind = body.get("kind")
        if kind not in ("broadcast", "leave"):
            raise ValueError("未知群操作")
        account, watch = self._scope()
        if body.get("account") != account or tuple(sorted(body.get("watch_groups", []))) != watch:
            raise ValueError("界面和后台的账号/统计群不一致，请保存并重启服务后再预览")
        live = [ws for ws in self.pipe.ws_conns if not ws.closed]
        if len(live) != 1:
            raise ValueError("需要唯一且已连接的目标 QQ 协议连接，当前不能安全操作")
        connection = live[0]
        members = await self._members(connection, account)
        text = body.get("text", "")
        if kind == "broadcast":
            if not watch:
                raise ValueError("未明确选择统计群，禁止群发。请在设置中选择并保存统计群，再重启服务")
            if not isinstance(text, str) or not text.strip() or len(text) > 10000:
                raise ValueError("请输入 1～10000 字的群发正文")
            ids = sorted(set(watch) & members.keys())
        else:
            gid = str(body.get("group_id", ""))
            if gid not in members or (watch and gid not in watch):
                raise ValueError("该群不属于当前账号的统计范围")
            member = await self.pipe.call_action("get_group_member_info", {"group_id": int(gid), "user_id": int(account), "no_cache": True}, connection=connection)
            if not isinstance(member, dict) or member.get("role") not in ("member", "admin"):
                raise ValueError("无法确认退群权限；群主请先在 QQ 中转让群主，程序不会解散群")
            ids, text = [gid], ""
        if not ids:
            raise ValueError("当前没有可发送的已统计群，未创建发送任务")
        token = secrets.token_urlsafe(24)
        preview = {"token": token, "kind": kind, "account": account, "watch": watch, "text": text,
                   "groups": [{"group_id": gid, "group_name": members[gid]} for gid in ids],
                   "excluded": sorted(set(watch)-members.keys()), "expires_at": time.time()+300,
                   "connection": connection}
        if kind == "leave":
            preview["message_count"] = self.pipe.store.group_message_count(ids[0])
        self.previews = {token: preview}  # 新预览使旧确认失效，不累计可重放的发送草稿。
        return {key: value for key, value in preview.items() if key not in ("connection", "watch")}

    async def _validate(self, preview):
        account, watch = self._scope()
        if (account, watch) != (preview["account"], preview["watch"]):
            raise ValueError("账号或统计群已变化，请重新预览")
        members = await self._members(preview["connection"], account)
        if self._scope() != (account, watch):
            raise ValueError("读取群列表期间设置发生变化，请重新预览")
        ids = (sorted(set(watch) & members.keys()) if preview["kind"] == "broadcast"
               else [group["group_id"] for group in preview["groups"] if group["group_id"] in members])
        if [{"group_id": gid, "group_name": members[gid]} for gid in ids] != preview["groups"]:
            raise ValueError("接收群名单或群名已变化，已阻止操作，请重新预览")

    async def confirm(self, token: str, confirmed: bool) -> dict:
        if confirmed is not True:
            raise ValueError("必须完成二次确认")
        preview = self.previews.pop(token, None)
        if not preview or preview["expires_at"] < time.time():
            raise ValueError("确认已过期或已使用，请重新预览；不会自动重发")
        if self.busy:
            raise ValueError("已有群操作正在执行")
        self.busy = True
        try:
            await self._validate(preview)
            job = {"id": token, "kind": preview["kind"], "account": preview["account"], "text": preview["text"],
                   "created_at": time.time(), "state": "running", "note": "开始执行，逐群返回结果",
                   "groups": [{**group, "state": "pending"} for group in preview["groups"]]}
            self.pipe.store.save_group_job(job)
            self.active_id, self.cancelled = token, False
            self.task = asyncio.create_task(self._run(preview, job))
            return job
        except BaseException:
            self.busy = False
            raise

    def cancel(self, job_id):
        if job_id != self.active_id or not self.busy:
            raise ValueError("该操作已结束或不属于当前任务")
        self.cancelled = True  # 只停止未发群，已提交或已成功的消息不能视为撤回。

    async def _run(self, preview, job):
        blocked = self.pipe.media.blocked_groups
        try:
            for index, group in enumerate(job["groups"]):
                if self.cancelled:
                    job["note"] = "已停止后续群；已提交的消息不会撤回"
                    break
                await self._validate(preview)  # 每群发送前重新验证配置、身份和冻结名单。
                if self.cancelled:
                    job["note"] = "已停止后续群；本次核对结束后未提交新消息"
                    break  # 用户可能在等待群列表响应期间取消，发送前必须再次检查。
                gid = group["group_id"]
                if preview["kind"] == "leave":
                    blocked.add(gid)
                    image_ids = {row[0] for row in self.pipe.store.conn.execute(
                        "SELECT i.id FROM message_images i JOIN messages m ON m.id=i.message_row WHERE m.group_id=?", (gid,))}
                    if image_ids & (self.pipe.media._processing_images | self.pipe.media._vision_running):
                        raise ValueError("该群图片仍在处理，请完成后重新确认退群；本次未退群")
                group["state"] = "sending"
                self.pipe.store.save_group_job(job)  # 明确记录提交边界，未知结果不能当作失败重试。
                if preview["kind"] == "broadcast":
                    data = await self.pipe.call_action("send_group_msg", {"group_id": int(gid),
                        "message": [{"type": "text", "data": {"text": preview["text"]}}]}, timeout=20, connection=preview["connection"])
                    if not isinstance(data, dict) or not data.get("message_id"):
                        raise RuntimeError("未取得消息 ID，发送结果未知，请在 QQ 中核对")
                    group.update(state="sent", message_id=str(data["message_id"]))
                else:
                    await self.pipe.call_action("set_group_leave", {"group_id": int(gid), "is_dismiss": False}, timeout=20, connection=preview["connection"])
                    group.update(state="left", note="协议已确认退群，本地清理待完成")
                    self.pipe.store.save_group_job(job)  # 本地清理失败也不能让用户再次调用退群。
                    result = self.pipe.store.clear_departed_group(preview["account"], gid)
                    self.pipe.departed_groups[gid] = result["left_at"]
                    self.pipe.activity_revision = getattr(self.pipe, "activity_revision", 0)+1
                    group.update(state="cleaned", **result, note="已退群并清理消息，摘要保留")
                self.pipe.store.save_group_job(job)
                if index+1 < len(job["groups"]):
                    await asyncio.sleep(self.delay)
            job["state"] = "completed" if all(g["state"] in ("sent", "cleaned") for g in job["groups"]) else "stopped"
            if job["state"] == "completed":
                job["note"] = "群发完成，全部目标群已返回消息 ID" if preview["kind"] == "broadcast" else "退群及消息清理完成；已生成摘要保留"
        except asyncio.CancelledError:
            job.update(state="stopped", note="服务关闭，未恢复或重发操作")
        except Exception as exc:
            job.update(state="stopped", note=security.redact(str(exc)))
            if isinstance(exc, ActionRejected):
                for group in job["groups"]:
                    if group["state"] == "sending":
                        group.update(state="failed", error=job["note"])
        finally:
            for group in job["groups"]:
                if group["state"] == "sending":
                    group.update(state="unknown", error=job.get("note", "结果未知"))
                elif group["state"] == "pending":
                    group.update(state="not_sent", error=job.get("note", "操作停止"))
                blocked.discard(group["group_id"])
            try:
                self.pipe.store.save_group_job(job)
            finally:
                self.busy = False

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.pipe.store.recover_group_jobs()  # 任务尚未首次执行就被取消时也记录为中断。
