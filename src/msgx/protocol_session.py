"""协议连接、账号、群同步和接收检查。需要宿主的 cfg/store/停止事件；不管理 UI。"""
from __future__ import annotations
import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime
from aiohttp import WSCloseCode
from . import napcat
from .models import parse_onebot_event

from .time_rules import BEIJING as SHANGHAI

log = logging.getLogger("msgx.protocol_session")

class ProtocolSession:
    def register_ws(self, ws, peer: str) -> None:
        """接入层只提交连接；本职责统一登记状态并安排群同步。"""
        self.ws_peers.add(peer)
        self.ws_conns.add(ws)
        self.ws_online[ws] = None
        self.ws_addresses[ws] = peer
        self.on_peer_connected()

    def note_ws_online(self, ws, online: bool) -> None:
        self.ws_online[ws] = online
        self._protocol_checked = 0.0

    def unregister_ws(self, ws) -> str:
        """移除一条连接不会抹掉同 IP 的其它连接，返回真实关闭原因。"""
        self.ws_conns.discard(ws)
        self.ws_online.pop(ws, None)
        self.ws_addresses.pop(ws, None)
        self.ws_peers.clear()
        self.ws_peers.update(addr for conn, addr in self.ws_addresses.items() if not conn.closed)
        reason = "服务主动停止" if self._closing_ws or self._stop.is_set() else "连接关闭"
        self.ws_last_close = f"{reason}，关闭码 {ws.close_code}，错误 {ws.exception() or '无'}"
        self.on_peer_disconnected()
        return self.ws_last_close

    def _init_protocol_session(self) -> None:
        """连接与待应答任务归本职责；共享的停止事件由 Pipeline 持有。"""
        self.ws_peers = set()
        self.ws_connected_at = self.ws_disconnected_at = 0.0
        self.ws_last_close = ""
        self.last_protocol_message = {}
        self._receive_warning = {}
        self._recovery_attempt_at = 0.0
        self.ws_online = {}
        self.ws_addresses = {}
        self._closing_ws = False
        self._protocol_checked = 0.0
        self._protocol_failure = {}
        self.ws_conns = set()
        self._action_futs = {}
        self._echo_seq = 0
        self.bot_uin = ""
        self.target_account = str((self.cfg.get("napcat") or {}).get("account") or "").strip()
        self._peer_watch = None
        self._group_refresh_tasks = set()

    def resolve_action(self, resp: dict) -> None:
        """WS 接入层收到 API 响应（带 echo、无 post_type）时转交到这里。"""
        echo = str(resp.get("echo") or "")
        fut = self._action_futs.pop(echo, None)
        if fut is not None and not fut.done():
            fut.set_result(resp)


    async def call_action(self, action: str, params: dict | None = None, timeout: float = 8.0):
        """通过反向 WS 向协议端（NapCat）发起 OneBot API 调用，返回 data 字段。"""
        live = [ws for ws in self.ws_conns if not ws.closed]
        if not live:
            raise RuntimeError("协议端未连接")
        self._echo_seq += 1
        echo = f"msgx-{self._echo_seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._action_futs[echo] = fut
        try:
            await live[0].send_str(json.dumps({"action": action, "params": params or {}, "echo": echo}))
            resp = await asyncio.wait_for(fut, timeout)
        finally:
            self._action_futs.pop(echo, None)
        if not isinstance(resp, dict) or resp.get("retcode") != 0:
            raise RuntimeError(f"{action} 调用失败: {resp!r}")
        return resp.get("data")


    async def refresh_group_names(self, prune: bool = False) -> dict:
        """从协议端拉群列表，把真实群名 + **属主账号**写进 groups 表。

        groups 表的 name 此前只能靠 watch.names 手填，没人填就永远是
        「(未命名) 754842541」—— NapCat 就在线上，没理由不去问它。

        ``prune=True`` 时顺手删掉「不属于当前账号且没有消息」的群 —— 那就是
        「只关注目前已登录账号的群」。默认 False：自动同步路径不能有副作用，
        删数据必须由用户显式点一下（界面的「只保留当前账号的群」）。
        """
        uin = await self.login_uin()
        if self.target_account and uin != self.target_account:
            raise RuntimeError("协议端登录账号与设置的目标 QQ 不一致，请核对账号")
        data = await self.call_action("get_group_list", {}, 8.0)
        keep: set[str] = set()
        for g in data or []:
            gid = str((g or {}).get("group_id") or "")
            if gid:
                keep.add(gid)
                self.store.upsert_group(gid, str((g or {}).get("group_name") or ""), uin)
        log.info("已从协议端拉取群列表：%d 个群（账号 %s）", len(keep), uin or "未知")
        out = {"count": len(keep), "uin": uin, "deleted": [], "protected": []}
        if prune and keep:
            deleted, protected = self.store.prune_groups(keep)
            out["deleted"], out["protected"] = deleted, protected
            if deleted or protected:
                log.info("按当前账号清理群列表：删除 %d 个，保留(有消息) %d 个",
                         len(deleted), len(protected))
        return out


    async def login_uin(self) -> str:
        """当前协议端登录的 QQ 号（拿不到返回空串，绝不猜）。"""
        if self.bot_uin:
            return self.bot_uin
        try:
            info = await self.call_action("get_login_info", {}, 5.0)
            self.bot_uin = str((info or {}).get("user_id") or "")
        except Exception as e:  # noqa: BLE001
            log.debug("取 get_login_info 失败（群属主将留空）: %s", e)
            self.bot_uin = ""
        return self.bot_uin


    def on_peer_connected(self) -> None:
        self.ws_connected_at = time.time()
        self._protocol_checked = 0.0
        self._protocol_failure = {}
        t = self._peer_watch
        if t is not None and not t.done():
            t.cancel()
        # 换了一次登录 = 可能换了一个账号，缓存的属主必须作废，否则新账号的群
        # 会被记到旧账号名下（而「属主」正是「只保留当前账号的群」的依据）。
        self.bot_uin = ""
        task = asyncio.create_task(self._auto_refresh_names())
        self._group_refresh_tasks.add(task)
        task.add_done_callback(self._group_refresh_tasks.discard)


    async def _auto_refresh_names(self) -> None:
        try:
            await asyncio.sleep(2)  # 等 NapCat 完成初始化
            await self.refresh_group_names()
        except Exception as e:  # noqa: BLE001
            log.debug("自动拉取群列表失败（不影响消息接收）: %s", e)


    def on_peer_disconnected(self) -> None:
        """最后一条连接断开后检查上游原因；不盲目重启 QQ 或创建重复实例。"""
        if self._stop.is_set() or self._closing_ws or self.ws_conns:
            return
        self.ws_disconnected_at = time.time()
        self._protocol_checked = 0.0
        if self._peer_watch is not None and not self._peer_watch.done():
            self._peer_watch.cancel()

        async def _watch() -> None:
            await asyncio.sleep(1)
            if self._stop.is_set() or self.ws_conns:
                return
            status = self.protocol_status()
            if status["state"] == "login_required":
                log.warning("%s；协议日志：%s", status["message"], status["log_path"])
                return
            await asyncio.sleep(59)
            if self._stop.is_set() or self.ws_conns:
                return
            log.warning("协议端已断开超过 60s 且未重连 —— 消息不再入库；%s", self.protocol_status()["message"])

        self._peer_watch = asyncio.create_task(_watch())


    def protocol_status(self) -> dict:
        live = [ws for ws in self.ws_conns if not ws.closed]
        if live and any(self.ws_online.get(ws) is not False for ws in live):
            if self._receive_warning:
                return dict(self._receive_warning)
            return {"state": "connected", "message": "协议端已连接", "since": self.ws_connected_at}
        now = time.monotonic()
        if not self._protocol_checked or now - self._protocol_checked >= 5:
            self._protocol_failure = napcat.login_failure(self.cfg, self.ws_connected_at)
            self._protocol_checked = now
        if self._protocol_failure:
            return dict(self._protocol_failure)
        if live:
            return {"state": "offline", "message": "QQ 心跳报告离线，等待协议端恢复登录", "since": self.ws_connected_at}
        return {"state": "disconnected", "since": self.ws_disconnected_at,
                "message": "等待 NapCat 自动重连；若持续未恢复，请查看协议端日志",
                "last_close": self.ws_last_close}


    async def close_ws_conns(self) -> int:
        """主动关闭所有反向 WS 连接，返回关闭条数。

        必须在 runner.cleanup() 之前调用，否则 cleanup 会一直等 WS 处理器返回。
        用 GOING_AWAY 告知对端是「服务要关了」，NapCat 收到后会自行重连。
        """
        self._closing_ws = True
        conns = list(self.ws_conns)
        for ws in conns:
            with contextlib.suppress(Exception):
                await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutdown")
        # 给 aiohttp 一点时间把关闭帧发出去并回收连接
        for _ in range(20):
            if not self.ws_conns:
                break
            await asyncio.sleep(0.05)
        return len(conns)


    def note_protocol_message(self, event: dict) -> None:
        """仅由真实接入路径记录新群消息，过滤消息也可验证接收链路。"""
        if event.get("post_type") != "message" or event.get("message_type") != "group":
            return
        self_id = str(event.get("self_id") or "")
        if self.target_account and self_id != self.target_account:
            return
        if not self_id.isdigit() or self_id == "0" or not event.get("message_id") or not event.get("time"):
            return
        try:
            msg = parse_onebot_event(event, self_id, {self_id})
        except (TypeError, ValueError, AttributeError):
            return
        if msg is None or self.store.has_receipt(self_id, msg.group_id, msg.msg_id):
            return
        self.last_protocol_message = {"received_at": time.time(), "message_time": msg.ts, "self_id": self_id}
        if (self._receive_warning and self_id == self._receive_warning.get("account")
                and msg.ts >= int(max(self.ws_connected_at, self._receive_warning["since"]))):
            self._receive_warning = {}
            log.info("消息接收已恢复：已收到当前账号的新 QQ 群消息")


    async def _check_receive_health(self) -> None:
        """按当前账号检查停收；一次故障至多尝试恢复一次，真实新消息才解除。"""
        now = time.time()
        if self._receive_warning:
            if (self._receive_warning["state"] == "recovering"
                    and now - self._receive_warning["since"] >= 120):
                self._receive_warning.update(state="stalled", message="自动恢复尚未收到新消息；已停止重复重启，请查看协议端登录状态")
                log.warning(self._receive_warning["message"])
            return
        if not self.bot_uin or not any(not ws.closed for ws in self.ws_conns):
            return
        wg = self.cfg.get("watchdog") or {}
        stale_s = max(300, int(wg.get("stale_minutes", 30)) * 60)
        evidence = self.last_protocol_message
        if (evidence.get("self_id") != self.bot_uin
                or now - max(self.started_at, self.ws_connected_at, evidence.get("received_at", 0)) < stale_s):
            return
        age = napcat.qq_msg_db_age(self.bot_uin)
        if age is None or age < stale_s:
            return
        base = self.store.hour_baseline(datetime.now(SHANGHAI).hour, int(wg.get("baseline_days", 7)), self.bot_uin)
        if base < int(wg.get("busy_min_messages", 5)):
            return
        self._receive_warning = {"state": "stalled", "since": now, "account": self.bot_uin,
                                 "message": "疑似停收：当前 QQ 账号和服务均长时间未收到消息"}
        log.warning("疑似停收：QQ %s 消息库已 %.0f 分钟未写入，服务同期无新群消息（小时基线 %d 条）",
                    self.bot_uin, age / 60, base)
        if now - self._recovery_attempt_at < 3600:
            self._receive_warning["message"] += "；一小时内已尝试恢复，不再自动重启"
            return
        self._recovery_attempt_at = now
        self._receive_warning.update(state="recovering", message="正在尝试恢复消息接收；等待新消息验证")
        log.warning("正在尝试恢复当前账号的 NapCat 登录进程；仅本次一次，不关闭普通 QQ")
        result = await asyncio.to_thread(napcat.recover_worker, self.cfg, self.bot_uin)
        # 请求期间若已有真实消息恢复，不能把状态覆盖回等待。
        if self._receive_warning:
            self._receive_warning.update(state="recovering" if result["ok"] else "stalled", message=result["note"])
            log.warning(result["note"])


    async def _watchdog_loop(self) -> None:
        """低频检查静默停收；不调用模型，不弹窗或发送通知。"""
        wg = self.cfg.get("watchdog") or {}
        if not wg.get("enabled", True):
            return
        every = max(30, int(wg.get("check_seconds", 300)))
        while not self._stop.is_set():
            try:
                await asyncio.sleep(every)
                await self._check_receive_health()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("看门狗异常: %s", e)
