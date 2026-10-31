"""有界本地初筛和单张自动语义任务；下载、接收、摘要不等待它。"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from . import security
from .image_policy import ANALYSIS_DELAY_SECONDS, is_sticker, screen_image, validate_analysis
from .llm import LLM


def local_decision(path: Path, row: dict, context: str):
    from PIL import Image
    with Image.open(path) as image:
        animated = bool(getattr(image, "is_animated", False))
    return screen_image(row.get("ocr_text", ""), context,
                        sticker=is_sticker(row.get("raw", ""), row.get("image_url", "")), animated=animated)


class ImageAnalysis:
    def __init__(self, media):
        self.media = media
        self.store = media.store
        self.store.recover_auto_images()
        self.status = {"state": "idle", "message": "本地初筛待处理；自动上传默认关闭"}

    def options(self) -> tuple[dict, dict]:
        from .prefs import load_from_env
        saved = load_from_env() or {}
        media = self.media.cfg.get("media", {})
        return validate_analysis(saved.get("image_analysis", media.get("analysis", {}))), saved

    async def process(self) -> int:
        options, _ = self.options()
        count = 0
        for source in self.store.screening_images(5, options["history_days"]):
            row = dict(source)
            context = self.store.image_context(row["message_row"], options["context_messages"])
            try:
                decision = await asyncio.to_thread(local_decision, Path(row["media_path"]), row, context)
            except (OSError, ValueError):
                from .image_policy import ImageDecision
                decision = ImageDecision(0, "本地文件不可读取，未上传", True)
            self.store.set_screen_result(row["id"], decision, options["threshold"])
            count += 1
        if not options["enabled"]:
            self.status = {"state": "disabled", "message": "本地初筛运行，自动图片 API 已关闭"}
            return count
        usage = self.store.automatic_image_usage()
        if usage["attempts"] >= options["daily_limit"] or usage["cost_cny"] >= options["daily_budget_cny"] or usage["unknown"]:
            self.status = {"state": "limited", "message": "今日次数/预算已达上限，或存在用量未知请求；已暂停自动上传", **usage}
            return count
        account = str(self.media.cfg.get("napcat", {}).get("account", ""))
        watch = {str(value) for value in self.media.cfg.get("watch", {}).get("groups", [])}
        rows = self.store.auto_image_candidates(options, account, watch)
        if not rows:
            self.status = {"state": "idle", "message": "暂无达到阈值的待分析图片", **usage}
            return count
        row = dict(rows[0])
        if row["auto_ready_at"] > time.time():
            remaining = max(1, int(row["auto_ready_at"]-time.time()))
            self.status = {"state": "waiting", "message": f"图片 #{row['id']} 已标记（{row['screen_score']}分），等待后续群聊约{remaining//60}分{remaining%60}秒",
                           "image_id": row["id"], "ready_at": row["auto_ready_at"], **usage}
            return count
        if row["id"] in self.media._vision_running:
            return count
        llm = self.media.llm.snapshot(role="image") if isinstance(self.media.llm, LLM) else self.media.llm
        if not llm.vision_available:
            self.status = {"state": "unconfigured", "message": "图片 API 尚未配置密钥或图片能力"}
            return count
        if isinstance(llm, LLM) and not llm.budget_ok():
            self.status = {"state": "limited", "message": "全局每日模型预算已达上限，未提交图片"}
            return count
        # 以有界上下文和本次输出上限预留估算额度；实际供应商账单仍可能有延迟或汇率差异。
        reserve = llm.cost_cny(8000, llm.image_output_limit(), service_tier="default") if isinstance(llm, LLM) else 0  # 预留可回退 Standard 的费用。
        if usage["cost_cny"] + reserve > options["daily_budget_cny"]:
            self.status = {"state": "limited", "message": "剩余预算不足以预留下一张图片的估算费用", **usage}
            return count
        cached = self.store.reusable_image_vision(row["media_hash"], row["message_row"])
        if cached:
            self.store.set_image_result(row["id"], vision_text=cached["vision_text"], vision_state="ok")
            self.store.set_image_include(row["id"], True)
            self.store.finish_auto_image(row["id"], "cached")
            return count + 1
        if not self.store.claim_auto_image(row["id"]):
            return count
        self.media._vision_running.add(row["id"])
        self.status = {"state": "running", "message": f"正在分析图片 #{row['id']}（本地重要度 {row['screen_score']}）"}
        try:
            context = self.store.image_context(row["message_row"], options["context_messages"], account=account,
                                               after_seconds=ANALYSIS_DELAY_SECONDS)
            text, state = await self.media._run_vision(Path(row["media_path"]), row, purpose="自动图片理解", llm=llm, context=context)
            self.store.set_image_result(row["id"], vision_text=text if state == "ok" else "", vision_state=state)
            self.store.finish_auto_image(row["id"], state if state == "ok" else "unknown")
            if state == "ok":
                self.store.set_image_include(row["id"], True)  # 摘要只读取已成功保存的语义。
            self.status = {"state": state, "message": "分析已入库并纳入摘要" if state == "ok" else "分析未完成，不自动重发；可手动检查"}
        except asyncio.CancelledError:
            raise  # running 已落盘，关闭重开也不会盲目重发。
        except Exception as exc:
            self.store.finish_auto_image(row["id"], "unknown")
            self.status = {"state": "unknown", "message": "自动分析失败，已停止重发：" + security.redact(str(exc))[:200]}
        finally:
            self.media._vision_running.discard(row["id"])
        return count + 1
