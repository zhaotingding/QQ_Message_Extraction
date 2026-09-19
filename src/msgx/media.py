from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path

import aiohttp

from .config import abs_path

log = logging.getLogger("msgx.media")

VISION_PROMPT = (
    "这是一张从技术群聊里抓到的图片。请：\n"
    "1) 完整提取图中所有可读文字（代码、报错、命令、终端输出、图表标签都要，按原样保留）；\n"
    "2) 用一句话说明这是什么内容。\n"
    "若图片是表情包或梗图，没有实质文字，则只输出一句话说明它的情绪/梗义（20 字内）。\n"
    "不要输出任何额外解释。"
)

ERROR_HINTS = ("error", "exception", "traceback", "failed", "报错", "错误", "失败", "异常")


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
        """下载并校验完整性：字节数必须与 Content-Length 一致，且能被 Pillow 解码。

        原来的写法只做 `read(cap + 1)` 就写盘，从不校验长度 —— NapCat 的图床
        偶发半途断流时，半张图会被存下来，随后 OCR 报 `image file is truncated`、
        多模态接口报 `unsupported image`，两处都只是结果，根因在下载。
        坏数据必须在源头拦下，**不落盘**，否则会污染 media 目录并反复报错。
        """
        s = await self.session()
        for attempt in (1, 2):
            try:
                async with s.get(url) as r:
                    if r.status != 200:
                        log.debug("图片下载失败 HTTP %s: %s", r.status, url[:80])
                        return None
                    expected = r.content_length  # chunked 传输时为 None
                    data = await r.content.read(cap + 1)
            except Exception as e:  # noqa: BLE001
                log.debug("图片下载异常: %s", e)
                return None
            if len(data) > cap:
                log.warning("图片超过 %sMB，跳过", self.mcfg.get("max_download_mb"))
                return None
            if expected is not None and len(data) != expected:
                log.warning(
                    "图片不完整（收到 %d / 应为 %d 字节），%s",
                    len(data), expected, "重试一次" if attempt == 1 else "放弃",
                )
                continue
            if not _decodable(data):
                log.warning("图片无法解码，%s", "重试一次" if attempt == 1 else "放弃")
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
        import json

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
            )
            return

        ocr_text = ""
        if self.mcfg.get("ocr_enabled", True):
            ocr_text = await asyncio.to_thread(self._ocr_sync, path)

        vision_text = ""
        if self.mcfg.get("vision_enabled", True) and self.llm.vision_supported():
            vision_text = await self.llm.describe_image(path, VISION_PROMPT)

        self.store.set_media_result(
            row["id"],
            ocr_text=ocr_text,
            vision_text=vision_text,
            media_path=str(path),
            media_hash=digest,
            state="done",
        )

    @staticmethod
    def looks_like_error(text: str) -> bool:
        low = (text or "").lower()
        return any(h in low for h in ERROR_HINTS)
