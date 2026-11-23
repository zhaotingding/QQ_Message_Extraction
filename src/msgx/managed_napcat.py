"""安装版便携协议端。程序目录只读，当前资料根保存连接设置、二维码和 QQ 会话。"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
from pathlib import Path
from uuid import uuid4


def valid_account(value: str) -> bool:
    return 0 < len(value) <= 20 and value.isascii() and value.isdigit() and not value.startswith("0")


def pair_account(folder: Path, account: str) -> None:
    if not valid_account(account):
        raise ValueError("协议端未提供有效的 QQ 号。")
    _save(folder / "account.json", account)


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def configure(cfg: dict, root: Path, installed: bool) -> None:
    from . import napcat

    nc = cfg["napcat"] = dict(cfg["napcat"])
    nc.pop("managed_dir", None)
    if not installed or nc.get("dir"):
        return
    # 兼容此前自动找到、已为这个账号初始化的外部协议端。新资料默认使用随包组件。
    account = str(nc.get("account") or "")
    folder = root / ".ui" / "napcat"
    if valid_account(account) and not (folder / "config/onebot11.json").is_file():
        external = napcat.resolve_dir(cfg)
        if (external and (external / "config" / f"onebot11_{account}.json").is_file()
                and (external / "config/webui.json").is_file() and napcat.preflight(external, cfg)["ok"]):
            return
    nc["managed_dir"] = str(folder)
    if not account:
        paired = _read(folder / "account.json", "")
        if isinstance(paired, str) and valid_account(paired):
            nc["account"] = paired
    if token := os.environ.get("MSGX_MANAGED_WS_TOKEN"):
        cfg["onebot"] = {**cfg["onebot"], "access_token": token}


class ManagedNapCat:
    def __init__(self, resources: Path):
        self.runtime = resources.parent / "NapCat"
        self.guard = resources / "tools" / "napcat-lifetime.cjs"
        self.proc: subprocess.Popen | None = None

    def start(self, cfg: dict, *, scan=False) -> None:
        folder = Path(cfg["napcat"]["managed_dir"])
        account = str(cfg["napcat"].get("account") or "")
        if account and not valid_account(account):
            raise ValueError("保存的目标 QQ 号无效。")
        files = ("node.exe", "index.js", "wrapper.node", "napcat/napcat.mjs")
        if not all((self.runtime / name).is_file() for name in files) or not self.guard.is_file():
            raise RuntimeError("内置 QQ 组件不完整，请用消息萃取安装包覆盖安装。")
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError("当前窗口的协议端正在运行。")

        config = folder / "config"
        control = _read(config / "webui.json", {})
        with socket.socket() as port:
            port.bind(("127.0.0.1", 0))
            control.update(host="127.0.0.1", port=port.getsockname()[1])
        if not control.get("token") or control["token"] == "random":
            control["token"] = secrets.token_urlsafe(32)
        _save(config / "webui.json", control)
        connection = {"name": "msgx", "enable": True, "messagePostFormat": "array", "reportSelfMessage": True,
                      "url": f"ws://127.0.0.1:{int(cfg['onebot']['ws_port'])}/onebot/ws",
                      "token": cfg["onebot"]["access_token"], "reconnectInterval": 3000}
        # 首次扫码前账号未知，上游会把通用配置复制到该账号。重启时刷新自管的连接。
        paths = {config / "onebot11.json", *config.glob("onebot11_*.json")}
        if account:
            paths.add(config / f"onebot11_{account}.json")
        for path in paths:
            data = _read(path, {})
            network = data.setdefault("network", {})
            others = [item for item in network.get("websocketClients", []) if item.get("name") != "msgx"]
            network["websocketClients"] = [*others, connection]
            _save(path, data)
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("NAPCAT_", "MSGX_", "OPENAI_", "DEEPSEEK_"))
               and k.upper() not in ("NODE_OPTIONS", "NODE_PATH", "ELECTRON_RUN_AS_NODE")}
        env.update(NAPCAT_WORKDIR=str(folder), NAPCAT_QQ_DATA_DIR=str(folder / "qq-data"),
                   NAPCAT_DISABLE_MULTI_PROCESS="1")
        command = [str(self.runtime / "node.exe"), "--require", str(self.guard), str(self.runtime / "index.js")]
        if account and not scan:
            command.extend(["-q", account])
        self.proc = subprocess.Popen(command, cwd=self.runtime, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=subprocess.CREATE_NO_WINDOW)

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        proc.stdin.close()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        self.proc = None
