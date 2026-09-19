from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import date
from pathlib import Path

import aiohttp

log = logging.getLogger("msgx.llm")

PROBE_FILE = "vision_probe.json"


class BudgetExceeded(Exception):
    pass


class LLM:
    def __init__(self, cfg: dict, store):
        self.cfg = cfg
        self.store = store
        self.llm = cfg["llm"]
        self.key = ""
        self._session: aiohttp.ClientSession | None = None
        self._vision: bool | None = None
        self._lock = asyncio.Lock()

    # ---------- lifecycle ----------

    def configure(self, api_key: str) -> None:
        self.key = api_key or ""

    @property
    def available(self) -> bool:
        return bool(self.llm.get("enabled") and self.key)

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=float(self.llm.get("timeout_seconds", 90)))
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---------- budget ----------

    def cost_usd(self, in_tokens: int, out_tokens: int) -> float:
        return (
            in_tokens / 1_000_000 * float(self.llm.get("price_in_usd_per_mtok", 0.22))
            + out_tokens / 1_000_000 * float(self.llm.get("price_out_usd_per_mtok", 0.66))
        )

    def spent_today_cny(self) -> float:
        usd = self.store.daily_cost_usd(date.today().isoformat())
        return usd * float(self.llm.get("usd_to_cny", 7.2))

    def budget_ok(self) -> bool:
        return self.spent_today_cny() < float(self.llm.get("max_daily_cost_cny", 10.0))

    # ---------- core call ----------

    async def chat(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        max_tokens: int = 1500,
        temperature: float = 0.2,
        think: bool = True,
    ) -> str:
        if not self.available:
            raise RuntimeError("LLM 未配置 api_key")
        if not self.budget_ok():
            raise BudgetExceeded(f"今日预算已达上限 {self.llm['max_daily_cost_cny']} 元")

        payload = {
            "model": model or self.llm["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if not think:
            # 该模型默认开启思考，思考 token 与正文共用 max_tokens 且按输出价计费。
            # 摘要、图片描述这类「忠实提取」任务不需要推理，关掉可省去数倍输出成本。
            payload["reasoning_effort"] = "none"

        data = await self._post(payload)
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens", 0))
        out_tok = int(usage.get("completion_tokens", 0))

        # 兜底：若思考占满预算被截断、正文为空，放大预算重试一次。
        # 没有这层保护会静默产出空摘要，用户完全察觉不到。
        choice = (data.get("choices") or [{}])[0]
        if not (choice.get("message") or {}).get("content", "").strip() and choice.get(
            "finish_reason"
        ) == "length":
            log.warning("输出被截断且正文为空，放大 max_tokens 重试一次")
            payload["max_tokens"] = min(int(max_tokens) * 4, 12000)
            data = await self._post(payload)
            usage = data.get("usage") or {}
            in_tok += int(usage.get("prompt_tokens", 0))
            out_tok += int(usage.get("completion_tokens", 0))

        cost = self.cost_usd(in_tok, out_tok)
        self.store.add_usage(date.today().isoformat(), in_tok, out_tok, cost)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError):
            log.warning("LLM 返回结构异常: %s", json.dumps(data)[:400])
            return ""

    async def _post(self, payload: dict) -> dict:
        s = await self.session()
        base = self.llm["base_url"].rstrip("/")
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        last = ""
        for url in (f"{base}/chat/completions", f"{base}/v1/chat/completions"):
            async with s.post(url, headers=headers, json=payload) as r:
                body = await r.text()
                if r.status == 200:
                    return json.loads(body)
                last = f"HTTP {r.status} @ {url}: {body[:300]}"
                if r.status != 404:
                    break
        raise RuntimeError(last or "请求失败")

    # ---------- vision ----------

    def _probe_path(self) -> Path:
        from .config import ROOT

        p = Path(self.cfg["app"]["db_path"])
        if not p.is_absolute():
            p = ROOT / p
        return p.parent / PROBE_FILE

    def vision_supported(self) -> bool:
        """是否支持图片输入。未探测过时返回 False（保守）。"""
        if self._vision is not None:
            return self._vision
        p = self._probe_path()
        if p.exists():
            try:
                self._vision = bool(json.loads(p.read_text("utf-8")).get("vision"))
            except Exception:
                self._vision = False
        else:
            self._vision = False
        return self._vision

    async def probe_vision(self, force: bool = False) -> bool:
        """发一张自制的小图给模型，判断是否支持图片输入。结果落盘缓存。"""
        async with self._lock:
            if self.vision_supported() and not force:
                return True
            if not self.available:
                return False
            try:
                from PIL import Image, ImageDraw
            except ImportError:
                log.warning("缺少 Pillow，跳过多模态探测")
                return False

            tmp = self._probe_path().parent / "_probe.png"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            img = Image.new("RGB", (320, 100), "white")
            ImageDraw.Draw(img).text((16, 40), "MSGX-7301", fill="black")
            img.save(tmp)

            b64 = base64.b64encode(tmp.read_bytes()).decode()
            payload = {
                "model": self.llm.get("vision_model", self.llm["model"]),
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "图中的编号是什么？只回答编号本身。"},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64}"},
                            },
                        ],
                    }
                ],
                "max_tokens": 512,
                "stream": False,
                "reasoning_effort": "none",
            }
            ok = False
            detail = ""
            try:
                data = await self._post(payload)
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
                ok = "7301" in content
                detail = content.strip()[:120]
            except Exception as e:  # noqa: BLE001
                detail = str(e)[:200]
            self._vision = ok
            self._probe_path().write_text(
                json.dumps({"vision": ok, "detail": detail, "ts": int(time.time())}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.unlink(missing_ok=True)
            log.info("多模态探测结果: %s (%s)", ok, detail)
            return ok

    async def describe_image(self, path: str | Path, prompt: str) -> str:
        if not (self.available and self.vision_supported()):
            return ""
        p = Path(path)
        if not p.exists():
            return ""
        b64 = base64.b64encode(p.read_bytes()).decode()
        suffix = p.suffix.lower().lstrip(".") or "jpeg"
        mime = {"jpg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}.get(suffix, "jpeg")
        payload = {
            "model": self.llm.get("vision_model", self.llm["model"]),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/{mime};base64,{b64}", "detail": "low"},
                        },
                    ],
                }
            ],
            "max_tokens": 400,
            "temperature": 0.2,
            "stream": False,
            "reasoning_effort": "none",
        }
        try:
            data = await self._post(payload)
            usage = data.get("usage") or {}
            self.store.add_usage(
                date.today().isoformat(),
                int(usage.get("prompt_tokens", 0)),
                int(usage.get("completion_tokens", 0)),
                self.cost_usd(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))),
            )
            return ((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        except Exception as e:  # noqa: BLE001
            log.warning("图片语义提取失败: %s", e)
            return ""
