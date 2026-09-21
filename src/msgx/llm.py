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


class BudgetExceeded(Exception):
    pass


class IncompleteResponse(RuntimeError):
    """接口已返回并计费，但正文没有完整结束；保留原文供摘要保存残稿。"""

    def __init__(self, content: str, finish_reason: str, max_tokens: int, output_tokens):
        self.content = content
        self.finish_reason = finish_reason
        self.max_tokens = max_tokens
        self.output_tokens = output_tokens
        super().__init__(
            f"模型输出未完成（finish_reason={finish_reason}，输出 {output_tokens} Token，"
            f"max_tokens={max_tokens}）；请检查输出额度或模型返回原因，本次未自动重试"
        )


class _Transient(Exception):
    """可重试的失败：连接被掐、超时、5xx、429。

    单独分一类是为了让重试逻辑能明确区分「重试有意义」与「重试白费」：
    HTTP 400/401/403 属于后者 —— 重试只是浪费时间和钱。
    """


# 会被算作「可重试」的 HTTP 状态码
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# 思考开启时的**最小** max_tokens。
#
# 思考 token 与正文共用这个额度，给少了就会出现「思考吃光预算 → 正文为空」的
# 静默空结果（线上日志里的「输出被截断且正文为空」）。实测思考量在 0~290 之间
# 剧烈波动（tools/probe_think_budget.py），而调用方原来只给 400，余量仅 110。
# 1200 给的是安全余量：**max_tokens 是上限不是预扣**，模型提前收尾不花钱，
# 所以这个下限几乎零成本；而截断的代价是同一份输入再买一次。
THINK_MIN_TOKENS = 1200


class LLM:
    def __init__(self, cfg: dict, store):
        self.cfg = cfg
        self.store = store
        self.llm = cfg["llm"]
        self.key = ""
        self._session: aiohttp.ClientSession | None = None

    # ---------- lifecycle ----------

    def configure(self, api_key: str) -> None:
        self.key = api_key or ""

    @property
    def available(self) -> bool:
        return bool(self.llm.get("enabled") and self.key)

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # force_close=True：**每次都新建连接、绝不复用**。
            #
            # 实测代价只有 +23ms（中位 500ms → 523ms，约 5%）—— 见
            # tools/probe_conn_reuse_cost.py。换来两件事：
            #   ① 不存在「连接池里的陈旧连接」这一整类失败（对端早已关闭、
            #      我们却把它捡起来用 → ServerDisconnectedError）；
            #   ② 因而**失效时根本不需要 reset 会话** —— 而那个 reset 正是
            #      v0.5.0 把「一次抖动放大成一片失败」的原因：
            #      tools/probe_session_cascade.py 实测，共享 session 上 4 个并发
            #      请求，中途 close() 会让**全部 4 个**一起挂到超时（60s 才报错）。
            # 5% 的握手开销换掉一个会自我放大的故障模式，很划算。
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=float(self.llm.get("timeout_seconds", 90))),
                connector=aiohttp.TCPConnector(force_close=True, limit=8),
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

    async def _accounted_post(self, payload: dict, purpose: str, source_ids: list[int] | None) -> dict:
        """v0.6.0：每个有返回的请求立即落账，后续重试失败也不会丢掉前次费用。"""
        started = time.time()
        try:
            data = await self._post(payload)
        except Exception:
            self.store.record_api_call(ts=started, purpose=purpose, source_ids=source_ids or [],
                                       model=payload["model"], usage=None, cost_cny=None, status="请求失败，用量未知")
            raise
        usage = data.get("usage")
        if not usage or "prompt_tokens" not in usage or "completion_tokens" not in usage:
            usage = None
        cost = None
        if usage is not None:
            cost = self.cost_cny(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)),
                                 int(usage.get("prompt_cache_hit_tokens", 0)), ts=started)
        choice = (data.get("choices") or [{}])[0]
        finish_reason = choice.get("finish_reason") or "missing"
        content = (choice.get("message") or {}).get("content") or ""
        complete = finish_reason == "stop" and bool(content.strip())
        reason = "stop/empty_content" if finish_reason == "stop" and not content.strip() else finish_reason
        status = "完成" if complete else f"输出未完成（{reason}）"
        if usage is None:
            status += "，用量未知"
        self.store.record_api_call(ts=started, purpose=purpose, source_ids=source_ids or [],
                                   model=payload["model"], usage=usage, cost_cny=cost, status=status)
        if complete:
            log.info("API 请求成功 · %s · 输入 %s / 输出 %s Token", purpose,
                     (usage or {}).get("prompt_tokens", "未知"), (usage or {}).get("completion_tokens", "未知"))
        else:
            log.warning("API 输出未完成 · %s · finish_reason=%s · 输入 %s / 输出 %s Token · max_tokens=%s",
                        purpose, reason, (usage or {}).get("prompt_tokens", "未知"),
                        (usage or {}).get("completion_tokens", "未知"), payload.get("max_tokens", "默认"))
        return data

    async def chat(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        max_tokens: int = 1500,
        temperature: float = 0.2,
        think: bool = True,
        purpose: str = "其他文本请求",
        source_ids: list[int] | None = None,
        require_complete: bool = False,
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

        # ⚠️ 思考开启时必须给足预算，否则会出「正文为空」的静默空结果。
        #
        # 机制：思考 token 与正文**共用** max_tokens。预算给小了，思考把额度吃光 →
        # finish_reason=length 且 content 为空，调用方拿到空串还以为是模型没内容。
        # 实测（tools/probe_think_budget.py）：同一打分提示词下思考 token 在 0~290
        # 之间剧烈波动，而 score.py 原来给的预算是 400 —— 余量只剩 110，
        # 稍难一点的输入就越线（线上日志里那条「输出被截断且正文为空」即此）。
        #
        # 为什么敢直接抬下限：**max_tokens 是上限、不是预扣**。模型提前收尾就不会
        # 产生费用，所以抬高它几乎零成本；而截断的代价是「同一份输入再买一次」。
        budget = int(max_tokens)
        if think:
            budget = max(budget, int(self.llm.get("think_min_tokens", THINK_MIN_TOKENS)))
        payload["max_tokens"] = budget

        data = await self._accounted_post(payload, purpose, source_ids)

        choice = (data.get("choices") or [{}])[0]
        if require_complete:
            content = (choice.get("message") or {}).get("content") or ""
            finish_reason = choice.get("finish_reason") or "missing"
            if finish_reason != "stop" or not content.strip():
                reason = "stop/empty_content" if finish_reason == "stop" else finish_reason
                raise IncompleteResponse(content, reason, budget,
                                         (data.get("usage") or {}).get("completion_tokens", "未知"))
            return content.strip()

        # 非摘要调用保留旧的空正文兜底；摘要额度由调用方预估，未完成时不追加收费请求。
        if not (choice.get("message") or {}).get("content", "").strip() and choice.get(
            "finish_reason"
        ) == "length":
            bigger = min(budget * 4, 12000)
            log.warning(
                "输出被截断且正文为空（max_tokens=%d，思考占满），放大到 %d 重试一次",
                budget, bigger,
            )
            budget = bigger
            payload["max_tokens"] = budget
            data = await self._accounted_post(payload, purpose, source_ids)
            choice = (data.get("choices") or [{}])[0]
            if not (choice.get("message") or {}).get("content", "").strip() and choice.get(
                "finish_reason"
            ) == "length":
                # 放大到 12000 还是空的，说明不是预算问题（如被内容审核拦下）。
                # 必须吱声 —— 静默返回空串会让上层把它当成「模型认为没内容」。
                log.error(
                    "放大预算后仍被截断（max_tokens=%d）—— 请检查 finish_reason 与模型侧限制",
                    budget,
                )

        try:
            # **返回前 strip()**。理由：上面判定「空正文」时用的就是 strip()，
            # 如果这里把 "   " 原样返回，就等于「判定它为空、却交给调用方一个非空串」，
            # 自相矛盾；而本项目所有调用方（打分取 JSON、摘要取正文）都只要剥净的文本。
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError):
            log.warning("LLM 返回结构异常: %s", json.dumps(data)[:400])
            return ""

    async def _post(self, payload: dict, retries: int | None = None) -> dict:
        """POST 一次；**连接层失败自动重试**。

        瞬时链路故障仍允许有限重试。大请求突发上传导致的断连由 _post_once
        的上传节奏处理，不能靠反复重发大包解决。失败未返回 usage 时费用未知。

        ⚠️ **重试时绝不要 reset 会话。** v0.5.0 在这里调 `_reset_session()`
        （close 掉共享 session），实测会把**其他并发请求一起掐死**
        （tools/probe_session_cascade.py：4 并发请求全部失败，而且不是快速报错，
        是**挂到 60 秒超时**）。于是 1 次真实抖动被放大成 N 次失败、N 次 reset。
        现在改为连接不复用（见 `session()`），池里没有陈旧连接可踩，
        自然也就不需要 reset。**这个「不 reset」是修复而不是省事，别加回来。**
        """
        n = int(self.llm.get("post_retries", 4)) if retries is None else int(retries)
        last: Exception | str = ""
        for attempt in range(1, max(1, n) + 1):
            t0 = time.time()
            try:
                return await self._post_once(payload)
            except _Transient as e:
                last = e
                dt = time.time() - t0
                if attempt >= n:
                    log.warning(
                        "LLM 请求最终失败（已试 %d 次，最后一次耗时 %.1fs）：%s", n, dt, e
                    )
                    break
                # 打出**该次耗时**：这是区分「握手就被拒（<1s）」与
                # 「传输中被掐（10~16s，像中间设备按空闲超时断开）」的唯一线索。
                # v0.5.0 的日志没有这个数字，只能靠猜。
                log.warning(
                    "LLM 请求失败（第 %d/%d 次，该次耗时 %.1fs）：%s —— 重试",
                    attempt, n, dt, e,
                )
                await asyncio.sleep(min(0.6 * attempt, 3.0))
        raise RuntimeError(f"请求失败（已重试 {n} 次）: {last}")

    async def _post_once(self, payload: dict) -> dict:
        s = await self.session()
        base = self.llm["base_url"].rstrip("/")
        encoded = json.dumps(payload).encode("utf-8")
        paced = len(encoded) > 64 * 1024
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Content-Length": str(len(encoded)),
        }
        last = ""
        stage = "连接或发送请求"
        queued = 0
        started = time.monotonic()

        async def upload():
            nonlocal stage, queued
            stage = "上传请求体"
            # 同一个 HTTP 请求分段写入，不拆消息、不增加模型调用。
            # 本机约 452 KiB 大包直接写出会在 15～20 秒断连；相同内容按此节奏
            # 上传已实测成功。保留 Content-Length，避免改成 chunked 传输编码。
            for offset in range(0, len(encoded), 8192):
                await asyncio.sleep(0.01)
                chunk = encoded[offset:offset + 8192]
                yield chunk
                queued += len(chunk)
            stage = "等待响应头"
            log.info("API 请求体已提交：%d 字节，耗时 %.2fs；等待响应", queued, time.monotonic() - started)

        try:
            for url in (f"{base}/chat/completions", f"{base}/v1/chat/completions"):
                stage, queued, started = "连接或发送请求", 0, time.monotonic()
                if paced:
                    log.info("API 开始上传：%.1f KiB，单次请求，每段 8 KiB", len(encoded) / 1024)
                # 每次尝试都创建新的迭代器，404 路径回退和重试不能复用耗尽的正文。
                async with s.post(url, headers=headers, data=upload() if paced else encoded) as r:
                    stage = "读取响应正文"
                    if paced:
                        log.info("API 响应头已收到：HTTP %d，耗时 %.2fs；正在接收结果", r.status, time.monotonic() - started)
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
        except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError) as e:
            # 含 ServerDisconnectedError / ClientOSError(10053) / ClientConnectorError
            raise _Transient(f"{type(e).__name__}: {e}（阶段：{stage}）") from e
        except asyncio.TimeoutError as e:
            raise _Transient(f"请求超时（阶段：{stage}）") from e
        raise RuntimeError(last or "请求失败")

    # 这里原本有个 `_reset_session()`（close 掉共享 session 以「换掉半死的连接池」）。
    # v0.5.1 **已把它删掉**，不是重构、是修 bug：它会把并发的在途请求一起掐死
    # （实测 4 并发全灭并挂到 60s 超时，见 tools/probe_session_cascade.py），
    # 于是 1 次抖动被放大成一片失败。现在连接不复用（见 session()），
    # 没有「陈旧连接」这个对象需要 reset。**不要把这个方法加回来。**
    # 关闭会话用 close()。

    # ---------- vision ----------

    @property
    def vision_available(self) -> bool:
        """配置允许解析真实图片；不发送测试图片，也不读取旧探测缓存。"""
        return self.available and bool(self.cfg.get("media", {}).get("vision_enabled", True))

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

    async def describe_image(self, path: str | Path, prompt: str, *, source_ids: list[int] | None = None,
                             purpose: str = "图片理解") -> tuple[str, bool]:
        """让模型读图，返回 ``(文本, 是否成功)``。

        失败**不抛异常**（图片语义是增强项，不该拖垮媒体流水线），但必须把失败
        如实报给调用方 —— v0.4.x 的写法是失败也返回空串、调用方照样把这条记成
        `done`，于是这张图永远不会再被处理。现在由调用方落 `vision_state`，
        之后可以重跑。
        """
        if not self.vision_available:
            return "", False
        if not self.budget_ok():
            log.warning("今日预算已达上限，跳过图片解析")
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
        try:
            resp = await self._accounted_post(payload, purpose, source_ids)
            text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            text = text.strip()
            return text, bool(text)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "图片语义提取失败（上传 %.0fKB）：%s", len(data) / 1024, e
            )
            return "", False
