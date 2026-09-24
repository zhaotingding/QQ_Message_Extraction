from __future__ import annotations

from .llm import APIRequestRejected, LLM

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

import aiohttp

from .config import abs_path
from .models import CQ_RE
from .prefs import plain_text

log = logging.getLogger("msgx.media")

VISION_PROMPT = (
    "这是一张从技术群聊里抓到的图片。请：\n"
    "1) 完整提取图中所有可读文字（代码、报错、命令、终端输出、图表标签都要，按原样保留）；\n"
    "2) 用一句话说明这是什么内容。\n"
    "不要输出任何额外解释。"
)

ERROR_HINTS = ("error", "exception", "traceback", "failed", "报错", "错误", "失败", "异常")


def _has_caption(row) -> bool:
    """只认可原消息的正文；协议占位符和 OCR 结果不算配文。"""
    try:
        event = json.loads(row["raw"] or "{}")
    except (TypeError, json.JSONDecodeError):
        event = {}
    segments = event.get("message") if isinstance(event, dict) else None
    if isinstance(segments, list):
        return any(str((seg.get("data") or {}).get("text", "")).strip()
                   for seg in segments if isinstance(seg, dict) and seg.get("type") == "text")
    if isinstance(segments, str):
        return bool(CQ_RE.sub("", segments).strip())
    # 早期导入数据可能没有原事件，超长原事件也可能被截断。
    return bool(plain_text(row["text"]))


def _sniff_ext(data: bytes) -> str:
    """按文件内容判断图片格式，不信任扩展名。"""
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:3] == b"GIF":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    return ".jpg"


async def fetch_full(session: aiohttp.ClientSession, url: str, cap: int) -> bytes:
    """GET 一张图并**读到 EOF**。HTTP 非 200 / 超过 cap 时抛 ValueError，网络异常原样抛出。

    ⚠️ v0.4.0 的教训：当时写的是 `r.content.read(cap + 1)` —— aiohttp 的
    `read(n)`（n>0）语义是「至多 n 字节，**缓冲里有数据就立刻返回**」，不是
    「读满 n 字节或到 EOF」。实际效果是每张图只拿到第一个 TCP chunk（实测恒为
    16384 字节）就返回，长度校验于是恒报「收到 16384 / 应为 N」且重试同路径
    永远失败 —— 校验生效了，下载没修。必须用 `r.read()` 读到流结束。
    """
    async with session.get(url) as r:
        if r.status != 200:
            raise ValueError(f"HTTP {r.status}")
        expected = r.content_length
        if expected is not None and expected > cap:
            raise ValueError(f"over cap ({expected} bytes)")
        return await r.read()


def _decodable(data: bytes) -> bool:
    """用 Pillow 试着解码，确认内容完整。

    Pillow 是 rapidocr 的既有依赖，不是新增。fetch 时的长度校验只能挡住
    「字节数不足」，挡不住「字节数刚好但结构损坏」的情况 —— 后者正是
    OCR 报 `image file is truncated`、多模态接口报 `unsupported image` 的来源。
    """
    try:
        from io import BytesIO

        from PIL import Image  # 惰性导入：装不上 OCR 时本模块仍可用

        with Image.open(BytesIO(data)) as im:
            im.verify()  # 只查结构完整性，不解码像素
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_ocr():
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        return RapidOCR, "rapidocr_onnxruntime"
    except ImportError:
        pass
    try:
        from rapidocr import RapidOCR  # type: ignore
        return RapidOCR, "rapidocr"
    except ImportError:
        return None, ""


class MediaWorker:
    """下载图片 → 去重 → OCR → （可选）多模态语义。"""

    def __init__(self, cfg: dict, store, llm):
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.media_dir: Path = abs_path(cfg, "media_dir")
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.mcfg = cfg["media"]
        self._ocr = None
        self._ocr_mod = ""
        self._ocr_tried = False
        self._session: aiohttp.ClientSession | None = None
        self._sem = asyncio.Semaphore(2)
        self._min_interval = 60.0 / max(1, int(self.mcfg.get("images_per_minute", 60)))
        self._last_start = 0.0

    # ---------- infra ----------

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_ocr(self):
        if not self._ocr_tried:
            self._ocr_tried = True
            cls, mod = _load_ocr()
            if cls is None:
                log.warning("未安装 OCR 引擎，图片将只做下载与去重（pip install rapidocr-onnxruntime）")
            else:
                try:
                    self._ocr = cls()
                    self._ocr_mod = mod
                    log.info("OCR 引擎就绪: %s", mod)
                except Exception as e:  # noqa: BLE001
                    log.warning("OCR 初始化失败: %s", e)
        return self._ocr

    def _ocr_sync(self, path: Path) -> str:
        engine = self._get_ocr()
        if engine is None:
            return ""
        try:
            out = engine(str(path))
        except Exception as e:  # noqa: BLE001
            log.warning("OCR 执行失败 %s: %s", path.name, e)
            return ""
        texts: list[str] = []
        if isinstance(out, tuple):
            result = out[0]
            for item in result or []:
                try:
                    texts.append(str(item[1]))
                except (IndexError, TypeError):
                    continue
        else:
            txts = getattr(out, "txts", None)
            if txts:
                texts.extend(str(t) for t in txts)
        return "\n".join(t for t in texts if t.strip()).strip()

    async def _fetch_complete(self, url: str, cap: int) -> bytes | None:
        """下载并校验完整性：整包读回 + Pillow 可解码，坏数据返回 None、绝不落盘。

        重试策略：网络异常、解不开 → 重试一次（瞬时故障）；HTTP 错误、超过
        上限 → 直接放弃（结果确定，重试无意义）。
        """
        s = await self.session()
        for attempt in (1, 2):
            again = "重试一次" if attempt == 1 else "放弃"
            try:
                data = await fetch_full(s, url, cap)
            except ValueError as e:
                log.debug("图片跳过: %s (%s)", e, url[:80])
                return None
            except Exception as e:  # noqa: BLE001
                log.warning("图片下载异常（%s）: %s", again, e)
                continue
            if not _decodable(data):
                log.warning("图片无法解码（%s）", again)
                continue
            return data
        return None

    async def _download(self, url: str) -> Path | None:
        cap = int(float(self.mcfg.get("max_download_mb", 20)) * 1024 * 1024)
        if url.startswith("file:"):
            from urllib.parse import unquote, urlparse
            from urllib.request import url2pathname

            p = Path(url2pathname(unquote(urlparse(url).path)))
            if not p.exists():
                log.debug("本地文件不存在: %s", p)
                return None
            return p
        data = await self._fetch_complete(url, cap)
        if data is None:
            return None

        sha = hashlib.sha256(data).hexdigest()[:32]
        day = time.strftime("%Y%m%d")
        d = self.media_dir / day
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{sha}{_sniff_ext(data)}"
        if not path.exists():
            path.write_bytes(data)
        return path

    # ---------- main ----------

    async def process_pending(self, limit: int = 15) -> list[int]:
        """处理待办图片，返回已完成处理的 messages.id 列表。"""
        rows = self.store.pending_media(limit)
        if not rows:
            return []
        done: list[int] = []
        for row in rows:
            async with self._sem:
                gap = self._min_interval - (time.monotonic() - self._last_start)
                if gap > 0:
                    await asyncio.sleep(gap)
                self._last_start = time.monotonic()
                try:
                    await self._process_row(row)
                    done.append(int(row["id"]))
                except Exception as e:  # noqa: BLE001
                    log.warning("媒体处理失败 msg_id=%s: %s", row["msg_id"], e)
                    self.store.set_media_result(row["id"], state="failed")
        return done

    async def _process_row(self, row) -> None:
        if not _has_caption(row):
            self._skip_uncaptioned(row)
            return

        urls = json.loads(row["image_urls"] or "[]")
        if not urls:
            self.store.set_media_result(row["id"], state="none")
            return

        path = await self._download(str(urls[0]))
        if path is None:
            self.store.set_media_result(row["id"], state="failed")
            return

        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:32]
        prev = self.store.find_by_hash(digest)
        if prev is not None:
            self.store.set_media_result(
                row["id"],
                ocr_text=prev["ocr_text"],
                vision_text=prev["vision_text"],
                media_path=str(path),
                media_hash=digest,
                state="done",
                # 复用同图的历史结果时，语义状态也要跟着复用，否则会被当成
                # 「还没试过」而重复调用模型。
                vision_state=("ok" if (prev["vision_text"] or "").strip() else
                              "rejected" if prev["vision_state"] == "rejected" else "failed"),
            )
            return

        ocr_text = ""
        if self.mcfg.get("ocr_enabled", True):
            ocr_text = await asyncio.to_thread(self._ocr_sync, path)

        vision_text, vision_state = await self._run_vision(path, row)
        if vision_state != "skipped":
            # 第一次尝试也计数：否则「首跑失败」的行会白得一次额外重试机会，
            # 而补跑计数只覆盖后续轮次，上限就不是真正的上限了。
            self.store.bump_vision_tries(row["id"])

        self.store.set_media_result(
            row["id"],
            ocr_text=ocr_text,
            vision_text=vision_text,
            media_path=str(path),
            media_hash=digest,
            state="done",
            vision_state=vision_state,
        )

    def _skip_uncaptioned(self, row) -> None:
        """旧待办和失败记录也只按消息配文放行，保留已有识别结果。"""
        self.store.set_media_result(
            row["id"], ocr_text=row["ocr_text"] or "", vision_text=row["vision_text"] or "",
            media_path=row["media_path"] or "", media_hash=row["media_hash"] or "",
            state="done", vision_state="skipped",
        )

    async def _run_vision(self, path: Path, row, retry: bool = False) -> tuple[str, str]:
        """跑图片语义，返回 ``(文本, 状态)``。状态见 store.MESSAGE_MIGRATIONS。

        为什么要把「失败」单独记下来：失败不落状态的话，这张图以后永远不会
        再被尝试（实测因此丢掉 62 张）。记成 'failed' 后由 `retry_vision()`
        在后续轮次里补跑 —— 图片已在本地，补跑只是重发一次推理请求。
        """
        if not _has_caption(row):
            return "", "skipped"
        llm = self.llm.snapshot() if isinstance(self.llm, LLM) else self.llm
        if not llm.vision_available:
            return "", "skipped"
        try:
            text, ok = await llm.describe_image(path, VISION_PROMPT, source_ids=[int(row["id"])],
                                             purpose="图片理解重试" if retry else "图片理解")
        except APIRequestRejected:
            return "", "rejected"
        return text, ("ok" if ok else "failed")

    async def retry_vision(self, limit: int = 8) -> list[int]:
        """补跑之前语义失败的那些图（只重跑推理，不重新下载、不重跑 OCR）。

        每轮只取少量，避免一次网络恢复后突然对模型发起几十个并发请求。
        **每次尝试都计数**（store.bump_vision_tries）—— 没有这个上限，一张
        永远读不出来的图会被每一轮补跑反复送去推理，安静地烧钱。次数用满后
        它就停在 'failed'，不再进入 `failed_vision` 的结果集。

        返回**这一次补出内容**的行 id，供后续摘要使用。
        """
        llm = self.llm.snapshot() if isinstance(self.llm, LLM) else self.llm
        if not llm.vision_available:
            return []
        rows = self.store.failed_vision(limit)
        done: list[int] = []
        for row in rows:
            if not _has_caption(row):
                self._skip_uncaptioned(row)
                continue
            self.store.bump_vision_tries(row["id"])
            path = Path(row["media_path"])
            if not path.exists():
                continue  # 文件被清理了，保持 failed 即可
            async with self._sem:
                text, state = await self._run_vision(path, row, retry=True)
            self.store.set_media_result(
                row["id"],
                ocr_text=row["ocr_text"] or "",
                vision_text=text if state == "ok" else "",
                media_path=row["media_path"],
                media_hash=row["media_hash"] or "",
                state="done",
                vision_state=state,
            )
            if state == "ok":
                done.append(int(row["id"]))
        if done:
            log.info("补跑图片语义成功 %d 张（本轮取出 %d 张）", len(done), len(rows))
        return done

    @staticmethod
    def looks_like_error(text: str) -> bool:
        low = (text or "").lower()
        return any(h in low for h in ERROR_HINTS)
