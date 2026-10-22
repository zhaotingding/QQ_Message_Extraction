from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from . import security, pricing

log = logging.getLogger("msgx.llm")


class BudgetExceeded(Exception):
    pass


class APIRequestRejected(RuntimeError):
    """服务端拒绝了请求；媒体后台也不能按临时网络故障自动重发。"""


class RetryableRequestError(RuntimeError):
    """连接未建立或服务端临时拒绝；单次请求重试耗尽，可由调度有限重试。"""


class AmbiguousResponseError(RuntimeError):
    """请求可能已被处理但未拿到完整结果；不自动整单重发。"""


class BackgroundResponsePending(RuntimeError):
    """已有后台响应 ID；下次只取回同一响应，不重新生成。"""


def _safe_request_id(value) -> str:
    value = security.redact(value) if isinstance(value, str) else ""
    return value if re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", value) else ""


def _http_error(status: int, body: str, headers) -> str:
    """只保留短诊断标识，不记录可能回显密钥、聊天或图片的 message/原始正文。"""
    try:
        data = json.loads(body)
        error = data.get("error", {}) if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        error = {}
    if not isinstance(error, dict):
        error = {}
    fields = {name: error.get(name) for name in ("type", "code", "param")}
    fields["request_id"] = headers.get("x-request-id")
    safe = []
    for name, value in fields.items():
        if not isinstance(value, str):
            continue
        value = security.redact(value)
        if re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,96}", value):
            safe.append(f"{name}={value}")
    detail = "；".join(safe) or "服务端未提供可安全显示的错误标识"
    return f"HTTP {status}：API 请求未成功；{detail}（原始响应已隐藏）"


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
    """可重试的失败：连接未建立、服务端明确临时拒绝。

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
        self._session_owner: LLM | None = None

    # ---------- lifecycle ----------

    def configure(self, api_key: str) -> None:
        self.key = api_key or ""
        security.register(self.key)

    @property
    def available(self) -> bool:
        return bool(self.llm.get("enabled") and self.key)

    async def session(self) -> aiohttp.ClientSession:
        if self._session_owner is not None:
            return await self._session_owner.session()
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
                read_bufsize=1024 * 1024,
            )
        return self._session

    async def close(self) -> None:
        if self._session_owner is not None:
            return
        if self._session and not self._session.closed:
            await self._session.close()

    def snapshot(self) -> LLM:
        """新任务读取已保存选择，在途任务保留自己的配置和费率。"""
        from . import api_settings, prefs
        from .config import api_key
        saved = prefs.load_from_env() or {}
        if "api" not in self.cfg and "api_profiles" not in saved and not any(
                key in saved for key in ("summary_prompt", "summary_target_chars")):
            return self
        cfg = {**self.cfg, "summary": dict(self.cfg.get("summary", {}))}
        prompt = str(saved.get("summary_prompt") or "").strip()
        if prompt:
            cfg["summary"]["prompt"] = prompt
        try:
            target = int(saved.get("summary_target_chars") or 0)
            if target > 0:
                cfg["summary"]["target_chars"] = target
        except (TypeError, ValueError):
            pass
        if "api_profiles" in saved or "api" in saved:
            profiles, active = api_settings.catalog(cfg, saved)
            cfg = api_settings.apply(cfg, profiles[active])
            cfg.update(api_profiles=profiles, active_api_profile=active)
        result = LLM(cfg, self.store)
        result.configure(api_key(cfg))
        result._session_owner = self._session_owner or self
        return result

    def resume_snapshot(self, metadata: dict) -> LLM:
        from .api_settings import apply
        from .config import api_key
        cfg = apply(self.cfg, metadata["api"])
        cfg.update(active_api_profile=metadata["active_api_profile"], summary=metadata["summary"])
        result = LLM(cfg, self.store)
        result.configure(api_key(cfg))
        result._session_owner = self._session_owner or self
        return result

    # ---------- budget ----------

    def cache_write_multiplier(self, model: str | None = None) -> float:
        return pricing.cache_write_multiplier(self.cfg, model)

    def is_peak(self, ts: float | None = None) -> bool:
        """调用时刻是否处于 DeepSeek 高峰时段。

        官方定义：高峰 = UTC 01:00-04:00 与 06:00-10:00 的**周一至周五**，
        其余时段（含整个周末）为空闲。折北京时间即工作日 09:00-12:00、14:00-18:00。
        """
        return pricing.is_peak(self.cfg, ts)

    def cost_cny(
        self,
        in_tokens: int,
        out_tokens: int,
        cache_hit_tokens: int = 0,
        ts: float | None = None,
        peak: bool | None = None,
        service_tier: str = "",
        cache_write_tokens: int = 0,
        model: str | None = None,
    ) -> float:
        """按调用**发生的那一刻**的峰谷价计算人民币金额。

        输入分「缓存命中 / 写入 / 普通输入」，输出单列；
        高峰时段各项都是空闲时段的 peak_multiplier 倍。
        缓存 Token 由 API 返回；缺写入字段时暂按零估算并在账本标注。

        ``peak`` 显式传值可覆盖时间判定 —— 给「只有日期、没有小时」的历史数据
        补算金额时用得到（那就只能按空闲价估，是个下限）。
        """
        return pricing.cost_cny(self.cfg, in_tokens, out_tokens, cache_hit_tokens, ts, peak,
                                service_tier, cache_write_tokens, model)

    def backfill_legacy_cost(self) -> int:
        """给「有 token 但金额为 0」的历史日期补一个估算金额（一次性，幂等）。

        背景：v0.4.2 把计费口径从美元换成人民币时，老库只加了列、**没有换算历史金额**
        （旧单价已作废，硬换算只会失真）。但「累计花费」现在是要显示给用户看的数字 ——
        让它少一天、显示 ¥0，比显示一个粗略值更容易误导。

        只能按**空闲时段**估价：库里只存了日期、没有小时，无从判断峰谷。
        所以补出来的值是**下限**，真实只会更高；日志里会写明这一点。
        """
        if "api" in self.cfg:
            return 0  # 不能用新服务的费率回填旧服务历史账目。
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
        return self.store.daily_cost_cny(time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600)))

    def budget_ok(self) -> bool:
        return self.spent_today_cny() < float(self.llm.get("max_daily_cost_cny", 10.0))

    # ---------- core call ----------

    async def _accounted_post(self, payload: dict, purpose: str, source_ids: list[int] | None,
                              request_key: tuple | None = None, metadata: dict | None = None) -> dict:
        """v0.6.0：每个有返回的请求立即落账，后续重试失败也不会丢掉前次费用。"""
        started = time.time()
        provider = self.cfg.get("api", {}).get("provider", "旧版 DeepSeek")
        saved = self.store.summary_request(request_key) if request_key else None
        if saved and saved["metadata"]["signature"] != metadata["signature"]:
            # 分块摘要的上一段已经完成、落账并写入本地缓存，才允许开始下一段。
            if not saved["result"] or saved["result"]["choices"][0]["finish_reason"] != "stop":
                raise RuntimeError("后台摘要材料不一致，请检查任务记录")
            self.store.clear_summary_request(request_key)
            saved = None
        if request_key and saved is None:
            self.store.start_summary_request(request_key, metadata, started)
            saved = self.store.summary_request(request_key)
        elif saved and not saved["response_id"]:
            raise AmbiguousResponseError("上次后台提交未收到任务 ID，结果及费用不明；请确认后补生成")
        if saved:
            started = saved["started_at"]
        try:
            if request_key:
                data = await self._background_response(payload, request_key, saved)
            else:
                data = await self._post(payload)
        except BackgroundResponsePending:
            raise  # 查询中断没有产生新模型请求，不记一笔未知费用。
        except Exception as exc:
            if saved and (saved["response_id"] or self.store.summary_request(request_key)["response_id"]):
                raise  # 已接受的后台任务保留 ID，不能因查询失败重新提交。
            if request_key and isinstance(exc, (RetryableRequestError, APIRequestRejected)):
                self.store.clear_summary_request(request_key)
            status = f"请求被拒绝：{exc}；用量未知" if isinstance(exc, APIRequestRejected) else "请求失败，用量未知"
            self.store.record_api_call(ts=started, purpose=purpose, source_ids=source_ids or [],
                                       model=payload["model"], usage=None, cost_cny=None, status=status, provider=provider,
                                       request_id=_safe_request_id(getattr(exc, "request_id", "")))
            raise
        usage = data.get("usage")
        if usage and "prompt_cache_hit_tokens" not in usage:
            usage["prompt_cache_hit_tokens"] = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        if usage and "prompt_cache_write_tokens" not in usage:
            usage["prompt_cache_write_tokens"] = (usage.get("prompt_tokens_details") or {}).get("cache_write_tokens")
        if not usage or "prompt_tokens" not in usage or "completion_tokens" not in usage:
            usage = None
        official = urlsplit(self.llm["base_url"]).hostname == "api.openai.com"
        tier_returned = bool(data.get("service_tier"))
        tier = str(data.get("service_tier") or "")
        if tier not in ("default", "priority", "fast", "flex", "scale", "auto", ""):
            tier = "unknown"
        cost = None
        requested_tier = self.cfg.get("api", {}).get("service_tier", "default") if official else ""
        estimated_tier = tier or requested_tier
        if usage is not None and (not official or estimated_tier in ("default", "priority", "fast", "flex")):
            cost = self.cost_cny(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)),
                                 int(usage.get("prompt_cache_hit_tokens", 0)), ts=started, service_tier=estimated_tier,
                                 cache_write_tokens=int(usage.get("prompt_cache_write_tokens") or 0), model=payload["model"])
        choice = (data.get("choices") or [{}])[0]
        finish_reason = choice.get("finish_reason") or "missing"
        content = (choice.get("message") or {}).get("content") or ""
        complete = finish_reason == "stop" and bool(content.strip())
        reason = "stop/empty_content" if finish_reason == "stop" and not content.strip() else finish_reason
        status = "完成" if complete else f"输出未完成（{reason}）"
        if usage is None:
            status += "，用量未知"
        elif self.cache_write_multiplier(payload["model"]) != 1 and usage.get("prompt_cache_write_tokens") is None:
            status += "，缓存写入用量未返回（按0估算）"
        if official and not tier_returned:
            status += f"，服务等级未返回（按请求{'Flex' if requested_tier == 'flex' else 'Standard'}估算）"
        self.store.record_api_call(ts=started, purpose=purpose, source_ids=source_ids or [],
                                   model=payload["model"], usage=usage, cost_cny=cost, status=status, provider=provider,
                                   cost_usd=cost / self.cfg["api"]["usd_cny"] if cost is not None and self.cfg.get("api", {}).get("currency") == "USD" else None,
                                   service_tier=tier, request_id=_safe_request_id(data.get("request_id")),
                                   pricing={**{key: self.cfg["api"][key] for key in ("currency", "usd_cny", "input_price", "cached_price", "output_price",
                                            "long_context_threshold", "long_input_multiplier", "long_output_multiplier", "peak_multiplier", "peak_windows_utc", "base_url")},
                                            "cache_write_multiplier": self.cache_write_multiplier(payload["model"]),
                                            "requested_service_tier": requested_tier,
                                            "service_tier_source": "response" if tier_returned else f"request_{requested_tier}" if official else "unknown"} if "api" in self.cfg else None,
                                   summary_key=request_key)
        if complete:
            log.info("API 请求成功 · %s · 输入 %s / 输出 %s Token", purpose,
                     (usage or {}).get("prompt_tokens", "未知"), (usage or {}).get("completion_tokens", "未知"))
            excerpt = " ".join(content.split())[:160]
            log.info("API 内容摘要 · %s · %s", purpose, security.redact(excerpt))
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
        request_key: tuple | None = None,
        request_signature: str = "",
        report_info: dict | None = None,
    ) -> str:
        saved = self.store.summary_request(request_key) if request_key else None
        resuming = bool(saved and saved["metadata"]["signature"] == request_signature and saved["response_id"])
        if not self.available and not (resuming and saved["result"]):
            raise RuntimeError("LLM 未配置 api_key")
        if not resuming and not self.budget_ok():
            raise BudgetExceeded(f"今日预算已达上限 {self.llm['max_daily_cost_cny']} 元")

        payload = {
            "model": model or self.llm["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": require_complete and urlsplit(self.llm["base_url"]).hostname == "api.openai.com",
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

        metadata = None
        if request_key:
            payload["background"] = True
            metadata = {"signature": request_signature, "api": self.cfg["api"],
                        "active_api_profile": self.cfg.get("active_api_profile", ""),
                        "summary": self.cfg.get("summary", {}), "report_info": report_info}
        data = await self._accounted_post(payload, purpose, source_ids, request_key, metadata)

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
            log.warning("LLM 返回结构异常，未保存原始响应")
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
                flex = (urlsplit(self.llm["base_url"]).hostname == "api.openai.com"
                        and self.cfg.get("api", {}).get("service_tier") == "flex")
                await asyncio.sleep(min(2 ** attempt, 30) if flex else min(0.6 * attempt, 3.0))
        raise RetryableRequestError(f"请求失败（已重试 {n} 次）: {last}")

    async def _post_once(self, payload: dict) -> dict:
        if self.llm.get("credential_store"):
            from .config import api_key
            self.configure(api_key(self.cfg))
            if not self.key:
                raise RuntimeError("该 API 地址未配置安全密钥，或密钥已删除")
        s = await self.session()
        base = self.llm["base_url"].rstrip("/")
        responses = self.llm.get("protocol") == "responses" or bool(payload.get("background"))
        summary_stream = bool(payload.get("stream"))
        outgoing = self._responses_payload(payload) if responses else dict(payload)
        if summary_stream and not responses:
            outgoing["stream_options"] = {"include_usage": True}
        if not responses and "api" in self.cfg:
            effort = self.cfg["api"]["reasoning"]
            outgoing["reasoning_effort"] = effort
            if effort != "none":
                outgoing.pop("temperature", None)
        # 内部仍统一用 max_tokens；官方 Chat Completions 在线路层转换，
        # 不把 OpenAI 参数强加给 DeepSeek 或其他兼容服务。
        if not responses and urlsplit(base).hostname == "api.openai.com" and "max_tokens" in outgoing:
            outgoing["max_completion_tokens"] = outgoing.pop("max_tokens")
        if urlsplit(base).hostname == "api.openai.com":
            outgoing["service_tier"] = self.cfg.get("api", {}).get("service_tier", "default")
        encoded = json.dumps(outgoing).encode("utf-8")
        paced = len(encoded) > 64 * 1024
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Content-Length": str(len(encoded)),
        }
        last = ""
        queued = 0
        started = time.monotonic()
        phase, request_id = "连接或发送请求", ""
        options = {}
        if outgoing.get("service_tier") == "flex":
            options["timeout"] = aiohttp.ClientTimeout(
                total=max(900, float(self.llm.get("timeout_seconds", 90))), connect=30, sock_read=900)
        elif summary_stream:
            options["timeout"] = aiohttp.ClientTimeout(
                total=max(600, float(self.llm.get("timeout_seconds", 90))), connect=30, sock_read=180)

        async def upload():
            nonlocal queued, phase
            phase = "上传请求体"
            # 同一个 HTTP 请求分段写入，不拆消息、不增加模型调用。
            # 本机约 452 KiB 大包直接写出会在 15～20 秒断连；相同内容按此节奏
            # 上传已实测成功。保留 Content-Length，避免改成 chunked 传输编码。
            for offset in range(0, len(encoded), 8192):
                await asyncio.sleep(0.01)
                chunk = encoded[offset:offset + 8192]
                yield chunk
                queued += len(chunk)
            phase = "等待响应头"
            log.info("API 请求体已提交：%d 字节，耗时 %.2fs；等待响应", queued, time.monotonic() - started)

        try:
            urls = [f"{base}/responses"] if responses else [f"{base}/chat/completions"]
            if not responses and not base.endswith("/v1"):
                urls.append(f"{base}/v1/chat/completions")
            for url in urls:
                queued, started = 0, time.monotonic()
                phase, request_id = "连接或发送请求", ""
                if paced:
                    log.info("API 开始上传：%.1f KiB，单次请求，每段 8 KiB", len(encoded) / 1024)
                # 每次尝试都创建新的迭代器，404 路径回退和重试不能复用耗尽的正文。
                async with s.post(url, headers=headers, data=upload() if paced else encoded, allow_redirects=False, **options) as r:
                    phase = "接收响应正文"
                    request_id = _safe_request_id(getattr(r, "headers", {}).get("x-request-id"))
                    if paced:
                        log.info("API 响应头已收到：HTTP %d，耗时 %.2fs；正在接收结果", r.status, time.monotonic() - started)
                    if r.status == 200:
                        if responses:
                            return await self._read_response(r)
                        if outgoing.get("stream") and "text/event-stream" in getattr(r, "headers", {}).get("Content-Type", ""):
                            return await self._read_chat_stream(r)
                        body = security.redact(await r.text())
                        try:
                            result = json.loads(body)
                            request_id = _safe_request_id(getattr(r, "headers", {}).get("x-request-id")) or _safe_request_id(result.get("request_id"))
                            if request_id:
                                result["request_id"] = request_id
                            else:
                                result.pop("request_id", None)
                            return result
                        except json.JSONDecodeError:
                            raise AmbiguousResponseError("API 响应不是完整 JSON，用量未知；未自动重发") from None
                    last = _http_error(r.status, await r.text(), r.headers)
                    if r.status in RETRY_STATUS:
                        raise _Transient(last)
                    if r.status != 404:
                        break
        except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, asyncio.TimeoutError,
                AmbiguousResponseError, json.JSONDecodeError, UnicodeDecodeError) as e:
            if isinstance(e, aiohttp.ClientConnectorError):
                raise _Transient(f"{type(e).__name__}（阶段：连接未建立）") from None
            detail = (f"API 请求中断：{type(e).__name__}；阶段={phase}；"
                      f"耗时={time.monotonic() - started:.1f}s；request_id={request_id or '未收到'}；"
                      "处理结果及用量未知；未自动重发")
            log.warning("%s", detail)
            error = AmbiguousResponseError(detail)
            error.request_id = request_id
            raise error from None
        raise APIRequestRejected(last or "请求失败")

    async def _read_chat_stream(self, response) -> dict:
        """只接受有完成标志和 DONE 的流；用量块可缺失，此时账本保留未知。"""
        parts, lines = [], []
        finish, usage, tier = None, None, ""
        try:
            async for raw in response.content:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("data:"):
                    lines.append(line[5:].lstrip())
                elif not line and lines:
                    value, lines = "\n".join(lines), []
                    if value == "[DONE]":
                        if finish is None:
                            break
                        return {"choices": [{"message": {"content": "".join(parts)}, "finish_reason": finish}],
                                "usage": usage, "service_tier": tier,
                                "request_id": _safe_request_id(response.headers.get("x-request-id"))}
                    event = json.loads(security.redact(value))
                    if event.get("error"):
                        break
                    if event.get("usage") is not None:
                        usage = event["usage"]
                    tier = event.get("service_tier") or tier
                    for choice in event.get("choices") or []:
                        if choice.get("index", 0) == 0:
                            parts.append((choice.get("delta") or {}).get("content") or "")
                            finish = choice.get("finish_reason") or finish
        except (ValueError, UnicodeError):
            pass  # 原始响应不得写入错误日志。
        raise AmbiguousResponseError("流式响应不完整；未自动重发")

    async def _background_response(self, payload: dict, key: tuple, saved: dict) -> dict:
        if saved["result"]:
            return saved["result"]
        response_id = saved["response_id"]
        data = None
        if not response_id:
            data = await self._post(payload)
            response_id = data.get("response_id", "")
            if not response_id:
                raise AmbiguousResponseError("后台响应未返回任务 ID，未自动重发")
            self.store.update_summary_request(key, response_id=response_id, request_id=data.get("request_id", ""))
            log.info("自动摘要后台任务已接受：%s；后续仅查询同一响应", response_id)
        started = time.monotonic()
        while True:
            if data and data.get("response_status") not in ("queued", "in_progress"):
                original_id = self.store.summary_request(key)["request_id"]
                data["request_id"] = original_id or data.get("request_id", "")
                self.store.update_summary_request(key, result=data)
                return data
            if time.monotonic() - started >= 900:
                raise BackgroundResponsePending("后台摘要仍在生成，稍后继续取回同一响应；不会重新提交消息")
            if data:
                await asyncio.sleep(5)
            data = await self._retrieve_response(response_id)

    async def _retrieve_response(self, response_id: str) -> dict:
        session = await self.session()
        try:
            async with session.get(f"{self.llm['base_url'].rstrip('/')}/responses/{response_id}",
                                   headers={"Authorization": f"Bearer {self.key}"}, allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=60, connect=30)) as response:
                if response.status == 200:
                    return await self._read_response(response)
                if response.status in RETRY_STATUS:
                    raise BackgroundResponsePending(f"后台结果查询暂不可用（HTTP {response.status}），稍后继续取回同一响应")
                raise APIRequestRejected(_http_error(response.status, await response.text(), response.headers))
        except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, asyncio.TimeoutError,
                ValueError, UnicodeError):
            raise BackgroundResponsePending("后台结果查询连接中断，稍后继续取回同一响应；不会重新提交消息") from None

    def _responses_payload(self, payload: dict) -> dict:
        inputs = []
        for message in payload["messages"]:
            content = message["content"]
            if isinstance(content, list):
                content = [({"type": "input_text", "text": item["text"]} if item["type"] == "text" else
                            {"type": "input_image", "image_url": item["image_url"]["url"],
                             "detail": item["image_url"].get("detail", "low")}) for item in content]
            inputs.append({"role": message["role"], "content": content})
        effort = self.cfg["api"]["reasoning"]
        background = bool(payload.get("background"))
        result = {"model": payload["model"], "input": inputs, "max_output_tokens": payload["max_tokens"],
                  "reasoning": {"effort": effort}, "store": background, "stream": not background}
        if background:
            result["background"] = True
        return result

    async def _read_response(self, response) -> dict:
        headers = getattr(response, "headers", {})
        if "text/event-stream" not in headers.get("Content-Type", ""):
            data = json.loads(security.redact(await response.text()))
        else:
            data = None
            lines = []
            # SSE 消息按空行分隔，支持多行 data；只在终态事件读取最终正文和用量。
            async for raw in response.content:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("data:"):
                    lines.append(line[5:].lstrip())
                elif not line and lines:
                    value = "\n".join(lines)
                    lines = []
                    if value == "[DONE]":
                        continue
                    event = json.loads(security.redact(value))
                    if event.get("type") in ("response.completed", "response.incomplete", "response.failed"):
                        data = event.get("response") or {}
                        break
                    if event.get("type") == "error":
                        raise AmbiguousResponseError("API 流式响应报告失败，原始内容已隐藏；未自动重发")
            if data is None:
                # 服务端可能已经计费，不能把缺终态的流自动整单重发。
                raise AmbiguousResponseError("API 响应中断，未收到完成事件；用量未知，未自动重发")
        status = data.get("status")
        text = "\n".join(part.get("text", "") for item in data.get("output", [])
                         if item.get("type") == "message" for part in item.get("content", [])
                         if part.get("type") == "output_text")
        usage = data.get("usage")
        normalized = None
        if usage and "input_tokens" in usage and "output_tokens" in usage:
            normalized = {"prompt_tokens": usage["input_tokens"], "completion_tokens": usage["output_tokens"],
                          "prompt_cache_hit_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
                          "prompt_cache_write_tokens": (usage.get("input_tokens_details") or {}).get("cache_write_tokens")}
        result = {"response_id": _safe_request_id(data.get("id")), "response_status": status,
                  "choices": [{"message": {"content": text},
                             "finish_reason": "stop" if status == "completed" else "incomplete"}], "usage": normalized}
        if data.get("service_tier"):
            result["service_tier"] = data["service_tier"]
        request_id = _safe_request_id(headers.get("x-request-id")) or _safe_request_id(data.get("request_id"))
        if request_id:
            result["request_id"] = request_id
        return result

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

        临时失败返回 False；不可重试的 API 拒绝交给媒体层记录为 rejected，
        防止后台重复提交。两种情况均保留本地 OCR，不影响后续消息接收。
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
            "max_tokens": 2000,
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
            if isinstance(e, APIRequestRejected):
                raise
            return "", False
