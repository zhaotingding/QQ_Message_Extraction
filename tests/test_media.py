"""图片下载校验的单元测试。

不联网：只测「这段字节到底是不是一张能被解码的完整图片」这个判断本身。
半张图是 OCR 报 `image file is truncated`、多模态接口报 `unsupported image`
的共同根因，必须在落盘前拦掉。
"""
from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / "src"))

from msgx.media import _decodable, _sniff_ext  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def png(color: str = "red") -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, "PNG")
    return buf.getvalue()


print("=== 完整性判断 _decodable ===")
good = png()
check("完整 PNG 可解码", _decodable(good), f"{len(good)} 字节")
check("截断的 PNG 不可解码", not _decodable(good[: len(good) // 2]))
check("只留文件头不可解码", not _decodable(good[:32]))
check("随机字节不可解码", not _decodable(b"\x01\x02\x03\x04" * 16))
check("空数据不可解码", not _decodable(b""))

print("\n=== 格式嗅探 _sniff_ext（不信任扩展名）===")
check("PNG 头 → .png", _sniff_ext(good) == ".png")
check("GIF 头 → .gif", _sniff_ext(b"GIF89a" + b"\0" * 20) == ".gif")
check("JPEG 头 → .jpg", _sniff_ext(b"\xff\xd8\xff\xe0" + b"\0" * 20) == ".jpg")
check("WEBP 头 → .webp", _sniff_ext(b"RIFF\x00\x00\x00\x00WEBP" + b"\0" * 20) == ".webp")
check("未知内容回落到 .jpg", _sniff_ext(b"\x01\x02\x03\x04") == ".jpg")

print()
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
