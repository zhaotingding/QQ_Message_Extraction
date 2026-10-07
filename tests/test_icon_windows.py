"""直接核验 Windows 窗口图标像素，避免仅凭 Tk 未报错就宣称加载成功。"""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import struct
import sys
import tempfile
import tkinter as tk
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.gui import App


def window_icon(root, kind):
    user = C.WinDLL("user32")
    gdi = C.WinDLL("gdi32")
    user.GetParent.argtypes = [W.HWND]
    user.GetParent.restype = W.HWND
    user.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    user.SendMessageW.restype = W.LPARAM
    user.GetClassLongPtrW.argtypes = [W.HWND, C.c_int]
    user.GetClassLongPtrW.restype = C.c_size_t

    class IconInfo(C.Structure):
        _fields_ = [("icon", W.BOOL), ("x", W.DWORD), ("y", W.DWORD),
                    ("mask", W.HBITMAP), ("color", W.HBITMAP)]

    class Bitmap(C.Structure):
        _fields_ = [("type", W.LONG), ("width", W.LONG), ("height", W.LONG),
                    ("stride", W.LONG), ("planes", W.WORD), ("bits", W.WORD), ("data", C.c_void_p)]

    user.GetIconInfo.argtypes = [W.HICON, C.POINTER(IconInfo)]
    gdi.GetObjectW.argtypes = [W.HGDIOBJ, C.c_int, C.c_void_p]
    gdi.CreateCompatibleDC.argtypes = [W.HDC]
    gdi.CreateCompatibleDC.restype = W.HDC
    gdi.GetDIBits.argtypes = [W.HDC, W.HBITMAP, W.UINT, W.UINT, C.c_void_p, C.c_void_p, W.UINT]
    gdi.DeleteDC.argtypes = [W.HDC]
    gdi.DeleteObject.argtypes = [W.HGDIOBJ]
    hwnd = user.GetParent(root.winfo_id())
    handle = user.SendMessageW(hwnd, 0x7F, kind, 0) or user.GetClassLongPtrW(hwnd, -14 if kind == 1 else -34)
    assert handle, "窗口没有可用图标"
    info = IconInfo()
    assert user.GetIconInfo(handle, C.byref(info))
    dc = gdi.CreateCompatibleDC(None)
    try:
        bm = Bitmap()
        assert gdi.GetObjectW(info.color, C.sizeof(bm), C.byref(bm))
        header = C.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, bm.width, -bm.height, 1, 32, 0, 0, 0, 0, 0, 0))
        bits = C.create_string_buffer(bm.width * bm.height * 4)
        assert gdi.GetDIBits(dc, info.color, 0, bm.height, bits, header, 0)
        return Image.frombytes("RGBA", (bm.width, bm.height), bits.raw, "raw", "BGRA")
    finally:
        gdi.DeleteDC(dc)
        gdi.DeleteObject(info.color)
        gdi.DeleteObject(info.mask)


@unittest.skipUnless(os.name == "nt", "Windows 图标句柄测试")
class WindowIconTests(unittest.TestCase):
    def test_png_compressed_ico_sets_actual_large_and_small_pixels(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "compressed.ico"
            sizes = [(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
            frames = [Image.new("RGBA", size, (20*i, 60, 190, 255)) for i, size in enumerate(sizes)]
            frames[-1].save(path, sizes=sizes, append_images=frames[:-1])
            for already_mapped in (False, True):
                with self.subTest(already_mapped=already_mapped):
                    root = tk.Tk()
                    try:
                        root.attributes("-alpha", 0)
                        root.geometry("160x80")
                        if already_mapped:
                            root.update()
                        app = App.__new__(App)
                        app.root = root
                        app.prefs = {"ui": {"icon_path": str(path)}}
                        app.v_icon_status = tk.StringVar(root)
                        app._apply_app_icon()
                        root.update()
                        for kind in (0, 1):
                            actual = window_icon(root, kind)
                            expected = frames[sizes.index(actual.size)]
                            self.assertEqual(actual.tobytes(), expected.tobytes())
                    finally:
                        root.destroy()


if __name__ == "__main__":
    unittest.main()
