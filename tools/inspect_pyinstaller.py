# -*- coding: utf-8 -*-
"""解析 PyInstaller onefile 的 CArchive 目录，确认打包了哪些模块。

背景：用户点「重启协议端」后二维码图片窗口从不弹出，而二维码文件确实在被刷新。
怀疑 _show_qrcode 里 `from PIL import Image, ImageTk` 在打包环境里失败
（ImageTk 依赖 tkinter；若包里没有 tcl/tk，这一行必然 ImportError → 永不弹窗）。
"""
import struct
import collections
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

EXE = r"D:\Dev\MCP\消息萃取-MCP\消息萃取.exe"
MAGIC = b"MEI\014\013\012\013\016"

data = open(EXE, "rb").read()
pos = data.rfind(MAGIC)
print("cookie at", pos, "file size", len(data))
if pos < 0:
    print("not a PyInstaller onefile archive")
    raise SystemExit

# PyInstaller >= 5 cookie: magic(8) pkgLen(4) toc(4) tocLen(4) pyver(4) pylibname(64)
magic, pkg_len, toc, toc_len, pyver = struct.unpack("!8sIIII", data[pos : pos + 24])
pylib = data[pos + 24 : pos + 88].rstrip(b"\x00")
print("pkg_len", pkg_len, "toc_off", toc, "toc_len", toc_len, "pyver", pyver, "pylib", pylib)

pkg_start = pos + 88 - pkg_len
tocbeg = pkg_start + toc
names = []
p = tocbeg
end = tocbeg + toc_len
while p < end:
    (entry_len,) = struct.unpack("!i", data[p : p + 4])
    entry_pos, csize, usize, flag, type_c = struct.unpack("!IIIBc", data[p + 4 : p + 18])
    name = data[p + 18 : p + entry_len].rstrip(b"\x00").decode("utf-8", "replace")
    names.append((name, type_c.decode(), csize, usize))
    p += entry_len

print("total entries", len(names))
tops = collections.Counter(
    n.replace("\\", "/").split("/")[0].split(".")[0] for n, t, s, u in names
)
for k, v in sorted(tops.items()):
    print(f"{v:5d}  {k}")

print("\n--- 感兴趣的条目 ---")
for n, t, s, u in names:
    ln = n.replace("\\", "/").lower()
    if any(k in ln for k in ("tkinter", "pil", "msgx", "tcl", "_tk", "gui")):
        print(f"[{t}] {n}  ({u} bytes)")
