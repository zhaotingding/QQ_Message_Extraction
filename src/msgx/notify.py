from __future__ import annotations

import asyncio
import base64
import logging
import subprocess
import sys

import aiohttp

log = logging.getLogger("msgx.notify")

PS_BALLOON = """Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.ShowBalloonTip({hold}, "{title}", "{msg}", [System.Windows.Forms.ToolTipIcon]::Info)
Start-Sleep -Seconds {sleep}
$n.Dispose()
"""


def _esc(s: str) -> str:
    return (s or "").replace('"', "'").replace("\r", " ").replace("\n", " ")[:400]


class Notifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ncfg = cfg["notify"]
        self._winotify = None
        self._tried = False
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _backend(self):
        if not self._tried:
            self._tried = True
            try:
                from winotify import Notification  # type: ignore

                self._winotify = Notification
            except Exception:  # noqa: BLE001
                self._winotify = None
        return self._winotify

    # ---------- public ----------

    async def notify(
        self,
        title: str,
        message: str,
        *,
        level: str = "P2",
        action: str = "",
        action_label: str = "打开",
        play_sound: bool = False,
    ) -> bool:
        ok = await asyncio.to_thread(self._toast, title, message, level, action, action_label)
        if play_sound or (level == "P0" and self.ncfg.get("sound", True)):
            self._sound()
        url = self.ncfg.get("webhook_url", "")
        if url:
            await self._webhook(url, title, message, level)
        return ok

    # ---------- local toast ----------

    def _toast(self, title: str, message: str, level: str, action: str, action_label: str) -> bool:
        if sys.platform != "win32":
            log.info("[通知] %s | %s", title, message)
            return False

        backend = self.ncfg.get("backend", "auto")
        notification_cls = self._backend() if backend in ("auto", "winotify") else None

        if notification_cls is not None:
            try:
                from winotify import audio  # type: ignore

                duration = "long" if level in ("P0", "P1") else "short"
                toast = notification_cls(
                    app_id="消息萃取",
                    title=title,
                    msg=message,
                    duration=duration,
                )
                if action:
                    toast.add_actions(label=action_label, launch=action)
                if level == "P0":
                    try:
                        toast.set_audio(audio.LoopingAlarm, loop=False)
                    except Exception:  # noqa: BLE001
                        pass
                toast.show()
                return True
            except Exception as e:  # noqa: BLE001
                log.debug("winotify 失败，回退 PowerShell: %s", e)

        return self._toast_powershell(title, message)

    def _toast_powershell(self, title: str, message: str) -> bool:
        script = PS_BALLOON.format(hold=10000, title=_esc(title), msg=_esc(message), sleep=8)
        try:
            encoded = base64.b64encode(script.encode("utf-16-le")).decode()
            flags = 0x08000000 if sys.platform == "win32" else 0
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                creationflags=flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("PowerShell 通知失败: %s", e)
            return False

    def _sound(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import winsound

            winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC)
        except Exception:  # noqa: BLE001
            pass

    # ---------- optional webhook ----------

    async def _webhook(self, url: str, title: str, message: str, level: str) -> None:
        kind = self.ncfg.get("webhook_type", "wecom")
        text = f"【{level}】{title}\n{message}"
        if kind == "wecom":
            payload = {"msgtype": "text", "text": {"content": text}}
        elif kind == "serverchan":
            payload = {"title": title, "desp": message}
        else:
            payload = {"title": title, "message": message, "level": level}
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        try:
            async with self._session.post(url, json=payload) as r:
                if r.status >= 300:
                    log.warning("Webhook 返回 %s", r.status)
        except Exception as e:  # noqa: BLE001
            log.warning("Webhook 推送失败: %s", e)
