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


class _Transient(Exception):
    """可重试的失败：连接被掐、超时、5xx、429。

    单独分一类是为了让重试逻辑能明确区分「重试有意义」与「重试白费」：
    HTTP 400/401/403 属于后者 —— 重试只是浪费时间和钱。
    """


# 会被算作「可重试」的 HTTP 状态码
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


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

    def is_peak(self, ts: float | None = None) -> bool:
        """调用时刻是否处于 DeepSeek 高峰时段。

        官方定义：高峰 = UTC 01:00-04:00 与 06:00-10:00 的**周一至周五**，
        其余时段（含整个周末）为空闲。折北京时间即工作日 09:00-12:00、14:00-18:00。
        """
        t = time.gmtime(time.time() if ts is None else ts)
        if t.tm_wday >= 5:  # 5=周六 6=周日
            return False
        minutes = t.tm_hour * 60 + t.tm_min
        for win in str(self.llm.get("peak_windows_utc", "")).split(","):
            win = win.strip()
            if "-" not in win:
                continue
            a, b = win.split("-", 1)
            try:
                ah, am = (int(x) for x in a.strip().split(":"))
                bh, bm = (int(x) for x in b.strip().split(":"))
            except ValueError:
                continue
            if ah * 60 + am <= minutes < bh * 60 + bm:
                return True
        return False

    def cost_cny(
        self,
        in_tokens: int,
        out_tokens: int,
        cache_hit_tokens: int = 0,
        ts: float | None = None,
        peak: bool | None = None,
    ) -> float:
        """按调用**发生的那一刻**的峰谷价计算人民币金额。

        账单的三档计价：输入分「缓存命中 / 未命中」、输出单列；
        高峰时段各项都是空闲时段的 peak_multiplier 倍。
        缓存命中 token 由 API 返回；缺失时视为全部未命中（保守，宁高不低）。

        ``peak`` 显式传值可覆盖时间判定 —— 给「只有日期、没有小时」的历史数据
        补算金额时用得到（那就只能按空闲价估，是个下限）。
        """
        mult = float(self.llm.get("peak_multiplier", 2.0)) if (
            self.is_peak(ts) if peak is None else bool(peak)
        ) else 1.0
        hit = max(0, min(int(cache_hit_tokens), int(in_tokens)))
        miss = max(0, int(in_tokens) - hit)
        return mult * (
            hit / 1_000_000 * float(self.llm.get("price_cache_hit_cny_per_mtok", 0.02))
            + miss / 1_000_000 * float(self.llm.get("price_cache_miss_cny_per_mtok", 1.00))
            + int(out_tokens) / 1_000_000 * float(self.llm.get("price_output_cny_per_mtok", 4.00))
        )

    def backfill_legacy_cost(self) -> int:
        """给「有 token 但金额为 0」的历史日期补一个估算金额（一次性，幂等）。

        背景：v0.4.2 把计费口径从美元换成人民币时，老库只加了列、**没有换算历史金额**
        （旧单价已作废，硬换算只会失真）。但「累计花费」现在是要显示给用户看的数字 ——
        让它少一天、显示 ¥0，比显示一个粗略值更容易误导。

        只能按**空闲时段**估价：库里只存了日期、没有小时，无从判断峰谷。
        所以补出来的值是**下限**，真实只会更高；日志里会写明这一点。
        """
        try:
            rows = [
                r for r in self.store.usage_all()
                if float(r["cost_cny"] or 0) == 0 and int(r["in_tokens"] or 0) > 0
            ]
        except Exception as e:  # noqa: BLE001
            log.debug("历史金额回填跳过: %s", e)
            return 0
        for r in rows:
            cost = self.cost_cny(
                int(r["in_tokens"]), int(r["out_tokens"]), int(r["cache_hit_tokens"]), peak=False
            )
            self.store.set_usage_cost(str(r["day"]), cost)
        if rows:
            log.info(
                "已为 %d 个历史日期补算金额（按空闲价，属**下限**，真实值只会更高）：%s",
                len(rows), ", ".join(str(r["day"]) for r in rows[:6]),
            )
        return len(rows)


    def spent_today_cny(self) -> float:
        return self.store.daily_cost_cny(date.today().isoformat())

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

        started = time.time()  # 峰谷按调用发生的时刻判定
        data = await self._post(payload)
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens", 0))
        out_tok = int(usage.get("completion_tokens", 0))
        hit_tok = int(usage.get("prompt_cache_hit_tokens", 0))

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
            hit_tok += int(usage.get("prompt_cache_hit_tokens", 0))

        cost = self.cost_cny(in_tok, out_tok, hit_tok, ts=started)
        self.store.add_usage(
            date.today().isoformat(), in_tok, out_tok, cost, hit_tok, max(0, in_tok - hit_tok)
        )
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError):
            log.warning("LLM 返回结构异常: %s", json.dumps(data)[:400])
            return ""

    async def _post(self, payload: dict, retries: int = 3) -> dict:
        """POST 一次；**连接层失败自动重试**。

        为什么必须有重试：实测本机到 api.deepseek.com 会偶发
        `ServerDisconnectedError` / `WinError 10053`（本地代理撤连接），
        约 1/8 的概率、与图片大小无关。v0.4.x 的 `describe_image` 没有任何重试，
        一次抖动就把这张图的语义永久丢掉 —— 62 张图就是这么没的。
        重试前先重建会话，避免复用一个已经半死的连接池。
        """
        last: Exception | str = ""
        for attempt in range(1, max(1, retries) + 1):
            try:
                return await self._post_once(payload)
            except _Transient as e:
                last = e
                if attempt >= retries:
                    break
                log.warning("LLM 请求失败（第 %d/%d 次）：%s —— 重试", attempt, retries, e)
                await self._reset_session()
                await asyncio.sleep(0.6 * attempt)
        raise RuntimeError(f"请求失败（已重试 {retries} 次）: {last}")

    async def _post_once(self, payload: dict) -> dict:
        s = await self.session()
        base = self.llm["base_url"].rstrip("/")
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        last = ""
        try:
            for url in (f"{base}/chat/completions", f"{base}/v1/chat/completions"):
                async with s.post(url, headers=headers, json=payload) as r:
                    body = await r.text()
                    if r.status == 200:
                        try:
                            return json.loads(body)
                        except json.JSONDecodeError as e:
                            # 半截响应体：连接被掐断的另一种表现，同样值得重试
                            raise _Transient(f"响应不是完整 JSON: {e}") from e
                    last = f"HTTP {r.status} @ {url}: {body[:300]}"
                    if r.status in RETRY_STATUS:
                        raise _Transient(last)
                    if r.status != 404:
                        break
        except aiohttp.ClientConnectionError as e:
            # 含 ServerDisconnectedError / ClientOSError(10053) / ClientConnectorError
            raise _Transient(f"{type(e).__name__}: {e}") from e
        except asyncio.TimeoutError as e:
            raise _Transient("请求超时") from e
        raise RuntimeError(last or "请求失败")

    async def _reset_session(self) -> None:
        """丢掉当前会话，下次调用重新建连接池。

        连接被对端掐断后，复用原 ClientSession 容易继续踩到坏连接。
        """
        old, self._session = self._session, None
        if old is not None and not old.closed:
            try:
                await old.close()
            except Exception:  # noqa: BLE001
                pass

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

    def _prepare_image(self, path: Path) -> tuple[bytes, str]:
        """把待上传的图片压到合适尺寸，返回 (字节, mime)。

        为什么要压：请求里带的是 `detail: "low"`，**服务端本来就会把图缩到很低分辨率**
        （实测一张 2.8MB 的图进来只计 198 个 prompt token）。也就是说我们花带宽和
        时间上传的绝大多数像素都会被丢掉。客户端先缩到长边 1280、JPEG q85，实测
        2.8MB → 约 90KB（缩到 1/30），图像内容与提取质量没有可感知差异。

        压不动（Pillow 缺失 / 格式古怪）就原样返回，绝不因此让整条链路失败。
        """
        try:
            from io import BytesIO

            from PIL import Image

            max_side = int(self.llm.get("vision_max_side", 1280))
            quality = int(self.llm.get("vision_jpeg_quality", 85))
            with Image.open(path) as im:
                if getattr(im, "is_animated", False):
                    im.seek(0)  # GIF 取第一帧
                rgb = im.convert("RGB")
                if max(rgb.size) > max_side:
                    rgb.thumbnail((max_side, max_side), Image.LANCZOS)
                buf = BytesIO()
                rgb.save(buf, "JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if data:
                return data, "image/jpeg"
        except Exception as e:  # noqa: BLE001
            log.debug("图片预处理失败，按原图上传: %s", e)

        suffix = path.suffix.lower().lstrip(".") or "jpeg"
        mime = {"jpg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}.get(suffix, "jpeg")
        return path.read_bytes(), f"image/{mime}"

    async def describe_image(self, path: str | Path, prompt: str) -> tuple[str, bool]:
        """让模型读图，返回 ``(文本, 是否成功)``。

        失败**不抛异常**（图片语义是增强项，不该拖垮媒体流水线），但必须把失败
        如实报给调用方 —— v0.4.x 的写法是失败也返回空串、调用方照样把这条记成
        `done`，于是这张图永远不会再被处理。现在由调用方落 `vision_state`，
        之后可以重跑。
        """
        if not (self.available and self.vision_supported()):
            return "", False
        p = Path(path)
        if not p.exists():
            return "", False

        data, mime = await asyncio.to_thread(self._prepare_image, p)
        b64 = base64.b64encode(data).decode()
        payload = {
            "model": self.llm.get("vision_model", self.llm["model"]),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "low"},
                        },
                    ],
                }
            ],
            "max_tokens": 400,
            "temperature": 0.2,
            "stream": False,
            "reasoning_effort": "none",
        }
        started = time.time()
        try:
            resp = await self._post(payload)
            usage = resp.get("usage") or {}
            in_tok = int(usage.get("prompt_tokens", 0))
            out_tok = int(usage.get("completion_tokens", 0))
            hit_tok = int(usage.get("prompt_cache_hit_tokens", 0))
            self.store.add_usage(
                date.today().isoformat(),
                in_tok,
                out_tok,
                self.cost_cny(in_tok, out_tok, hit_tok, ts=started),
                hit_tok,
                max(0, in_tok - hit_tok),
            )
            text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            text = text.strip()
            return text, bool(text)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "图片语义提取失败（上传 %.0fKB）：%s", len(data) / 1024, e
            )
            return "", False
