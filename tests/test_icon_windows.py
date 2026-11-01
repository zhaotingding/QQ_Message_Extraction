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
from msgx.app_icon import DEFAULT_ICON, selected_icon


class IconSelectionTests(unittest.TestCase):
    def test_clean_personal_root_uses_source_asset_without_creating_local_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(selected_icon(root), DEFAULT_ICON)
            self.assertEqual(list(root.iterdir()), [])
            with Image.open(DEFAULT_ICON) as icon:
                self.assertTrue({(16, 16), (32, 32), (256, 256)} <= icon.ico.sizes())
                self.assertEqual(icon.ico.getimage((256, 256)).convert("RGBA").getextrema()[3][0], 0)

    def test_personal_override_then_local_copy_then_default(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            local = root / ".ui" / "app-icon.ico"
            local.parent.mkdir()
            local.write_bytes(b"local")
            custom = root / "custom.ico"
            custom.write_bytes(b"custom")
            saved = {"ui": {"icon_path": str(custom)}}
            self.assertEqual(selected_icon(root, saved), custom)
            custom.unlink()
            self.assertEqual(selected_icon(root, saved), local)
            local.unlink()
            self.assertEqual(selected_icon(root, saved), DEFAULT_ICON)

    def test_custom_preferences_location_and_build_use_same_rule(self):
        from importlib.util import module_from_spec, spec_from_file_location
        from msgx import yaml_config
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            personal = root / "personal"
            personal.mkdir()
            local = personal / "app-icon.ico"
            local.write_bytes(b"local")
            self.assertEqual(selected_icon(root, {}, personal / "preferences.yaml"), local)
            custom = root / "custom.ico"
            custom.write_bytes(b"custom")
            preferences = root / ".ui" / "preferences.yaml"
            preferences.parent.mkdir()
            preferences.write_text(yaml_config.dumps({"ui": {"icon_path": "custom.ico"}}), encoding="utf-8")
            spec = spec_from_file_location("icon_build_test", Path(__file__).resolve().parents[1] / "tools/build_launcher.py")
            module = module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.selected_icon(root), custom)


def window_icon(root, kind):
    user = C.WinDLL("user32")
    user.GetParent.argtypes = [W.HWND]
    user.GetParent.restype = W.HWND
    user.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    user.SendMessageW.restype = W.LPARAM
    user.GetClassLongPtrW.argtypes = [W.HWND, C.c_int]
    user.GetClassLongPtrW.restype = C.c_size_t
    hwnd = user.GetParent(root.winfo_id())
    handle = user.SendMessageW(hwnd, 0x7F, kind, 0) or user.GetClassLongPtrW(hwnd, -14 if kind == 1 else -34)
    assert handle, "窗口没有可用图标"
    return icon_pixels(handle)


def icon_pixels(handle):
    user = C.WinDLL("user32")
    gdi = C.WinDLL("gdi32")

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
    def test_clean_clone_sets_default_small_and_large_window_icons(self):
        with tempfile.TemporaryDirectory() as folder:
            root = tk.Tk()
            try:
                root.attributes("-alpha", 0)
                app = App.__new__(App)
                app.root, app.prefs = root, {}
                app.prefs_file = Path(folder) / "preferences.yaml"
                app.v_icon_status = tk.StringVar(root)
                app._apply_app_icon()
                root.update()
                with Image.open(DEFAULT_ICON) as icon:
                    for kind in (0, 1):
                        actual = window_icon(root, kind)
                        expected = icon.ico.getimage(actual.size).convert("RGBA")
                        self.assertEqual(actual.tobytes(), expected.tobytes())
                self.assertIn("项目默认图标", app.v_icon_status.get())
            finally:
                root.destroy()

    def test_missing_preference_reuses_saved_local_icon(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "app-icon.ico"
            Image.new("RGBA", (256, 256), (25, 90, 200, 255)).save(path)
            root = tk.Tk()
            try:
                root.attributes("-alpha", 0)
                app = App.__new__(App)
                app.root, app.prefs = root, {"ui": {"last_tab": 0}}
                app.prefs_file = Path(folder) / "preferences.yaml"
                app.v_icon_status = tk.StringVar(root)
                app._apply_app_icon()
                root.update()
                for kind in (0, 1):
                    actual = window_icon(root, kind)
                    self.assertEqual(actual.getpixel((0, 0)), (25, 90, 200, 255))
                self.assertIn("已应用自定义图标", app.v_icon_status.get())
            finally:
                root.destroy()

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
