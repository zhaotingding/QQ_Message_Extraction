"""深浅配色与本地外观选择；只改变界面，不读取业务数据或启动服务。"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from .. import prefs as prefs_mod


PALETTES = {
    "light": dict(name="light", bg="#eceef1", surface="#e2e6ec", field="#ffffff", fg="#243247", muted="#627084",
                  border="#b8c2cf", selected="#cce2ff", selected_fg="#142b48", accent="#2468b8",
                  success="#197647", error="#b42332", warning="#9b5d00", keyword="#fff2a8",
                  odd="#f4f6f9", related="#c62828", image_api="#dff3e5", grid="#dfe5ed",
                  card0="#f0f5fb", card1="#f7f3e9", review_blue="#cfe8ff", review_red="#ffd5d5"),
    "dark": dict(name="dark", bg="#181c23", surface="#242a34", field="#202630", fg="#e4e9f2", muted="#a6b2c4",
                 border="#465163", selected="#355373", selected_fg="#ffffff", accent="#82b6ff",
                 success="#7ee787", error="#ff7b72", warning="#f2c16d", keyword="#554718",
                 odd="#252d39", related="#ff919b", image_api="#183d2b", grid="#36404e",
                 card0="#293645", card1="#3a352a", review_blue="#224d72", review_red="#66323b"),
}


def theme_name(prefs: dict) -> str:
    choice = (prefs.get("ui") or {}).get("theme", "dark")
    return choice if choice in PALETTES else "dark"  # 已保存选择优先，未配置时使用深色。


def palette(widget=None) -> dict:
    try:
        return getattr(widget.winfo_toplevel()._root(), "_msgx_palette", PALETTES["light"])
    except (AttributeError, tk.TclError):
        return PALETTES["light"]  # 独立图表及非Tk记录器继续使用已有浅色默认。


class ThemeManager:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.name = "light"
        root.bind_all("<Map>", self._mapped, add="+")  # 仅新顶层/菜单做一次处理，不定时扫描控件。

    def apply(self, name: str) -> None:
        if name not in PALETTES:
            raise ValueError("主题必须为 dark 或 light")
        self.name = name
        p = PALETTES[name]
        self.root._msgx_theme, self.root._msgx_palette = name, p
        style = ttk.Style(self.root)
        style.configure(".", background=p["bg"], foreground=p["fg"], bordercolor=p["border"],
                        lightcolor=p["surface"], darkcolor=p["border"], troughcolor=p["bg"], focuscolor=p["accent"])
        for kind in ("TFrame", "TLabelframe", "TCheckbutton", "TRadiobutton", "TPanedwindow"):
            style.configure(kind, background=p["bg"], foreground=p["fg"])
        for kind in ("TLabel", "TLabelframe.Label", "Card.TLabel"):
            style.configure(kind, background=p["bg"], foreground=p["fg"])
        for kind in ("Hint.TLabel", "CardKey.TLabel"):
            style.configure(kind, background=p["bg"], foreground=p["muted"])
        for kind in ("TButton", "TMenubutton"):
            style.configure(kind, background=p["surface"], foreground=p["fg"], bordercolor=p["border"])
            style.map(kind, background=[("pressed", p["selected"]), ("active", p["selected"])],
                      foreground=[("disabled", p["muted"])])
        for kind in ("TCheckbutton", "TRadiobutton"):
            style.map(kind, background=[("active", p["surface"])], foreground=[("disabled", p["muted"])])
        for kind in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(kind, fieldbackground=p["field"], foreground=p["fg"], insertcolor=p["fg"],
                            selectbackground=p["selected"], selectforeground=p["selected_fg"], background=p["surface"],
                            arrowcolor=p["fg"])
            style.map(kind, fieldbackground=[("readonly", p["field"]), ("disabled", p["surface"])],
                      foreground=[("disabled", p["muted"]), ("readonly", p["fg"])],
                      background=[("active", p["selected"])],
                      arrowcolor=[("disabled", p["muted"]), ("active", p["fg"]), ("readonly", p["fg"])])
        style.configure("TNotebook", background=p["bg"], bordercolor=p["border"])
        style.configure("TNotebook.Tab", background=p["surface"], foreground=p["muted"])
        style.map("TNotebook.Tab", background=[("selected", p["field"]), ("active", p["selected"])],
                  foreground=[("selected", p["fg"]), ("active", p["fg"])])
        for kind in ("Treeview", "Report.Treeview"):
            style.configure(kind, background=p["field"], fieldbackground=p["field"], foreground=p["fg"], bordercolor=p["border"])
            style.map(kind, background=[("selected", p["selected"])], foreground=[("selected", p["selected_fg"])])
        style.configure("Treeview.Heading", background=p["surface"], foreground=p["fg"])
        style.map("Treeview.Heading", background=[("active", p["selected"])])
        for kind in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(kind, background=p["surface"], arrowcolor=p["fg"], troughcolor=p["bg"])
            style.map(kind, background=[("active", p["selected"]), ("pressed", p["selected"])])
        for option, color in (("background", p["field"]), ("foreground", p["fg"]),
                              ("selectBackground", p["selected"]), ("selectForeground", p["selected_fg"])):
            self.root.option_add("*TCombobox*Listbox." + option, color)  # Tcl原生下拉列表不在Python子树里。
            self.root.option_add("*Menu." + option, color)
        self.apply_tree(self.root)

    def _mapped(self, event) -> None:
        if isinstance(event.widget, (tk.Toplevel, tk.Menu)):
            event.widget.after_idle(lambda widget=event.widget: self.apply_tree(widget))

    def apply_tree(self, widget) -> None:
        try:
            if not widget.winfo_exists():
                return
            p = PALETTES[self.name]
            if isinstance(widget, (tk.Tk, tk.Toplevel)):
                widget.configure(background=p["bg"])
                self._titlebar(widget)
            elif isinstance(widget, tk.Text):
                if isinstance(widget, ScrolledText) and not isinstance(widget.vbar, ttk.Scrollbar):
                    original = widget.vbar
                    packing = {key: value for key, value in original.pack_info().items() if key != "in"}
                    widget.vbar = ttk.Scrollbar(widget.frame, orient="vertical", command=widget.yview)
                    widget.vbar.pack(before=widget._w, **packing)  # ScrolledText.__str__指向frame，排序须用真实Text路径。
                    widget.configure(yscrollcommand=widget.vbar.set)
                    original.destroy()  # Windows原生Scrollbar不响应深色，用ttk替换一次后持续复用。
                widget.configure(background=p["field"], foreground=p["fg"], insertbackground=p["fg"],
                                 selectbackground=p["selected"], selectforeground=p["selected_fg"], highlightbackground=p["border"])
                for tag, color in (("heading", p["accent"]), ("warn", p["error"]), ("good", p["success"])):
                    if tag in widget.tag_names():
                        widget.tag_configure(tag, foreground=color)
                for tag, key in (("card0", "card0"), ("card1", "card1"), ("review_blue", "review_blue"), ("review_red", "review_red")):
                    if tag in widget.tag_names():
                        widget.tag_configure(tag, background=p[key])
            elif isinstance(widget, tk.Listbox):
                widget.configure(background=p["field"], foreground=p["fg"], selectbackground=p["selected"],
                                 selectforeground=p["selected_fg"], highlightbackground=p["border"], disabledforeground=p["muted"])
            elif isinstance(widget, tk.Canvas):
                widget.configure(background=p["field"], highlightbackground=p["border"])
                redraw = getattr(widget, "_msgx_redraw", None)
                if redraw:
                    redraw()
            elif isinstance(widget, tk.Menu):
                widget.configure(background=p["surface"], foreground=p["fg"], activebackground=p["selected"],
                                 activeforeground=p["selected_fg"], disabledforeground=p["muted"])
            elif isinstance(widget, (tk.Frame, tk.Label, tk.Button, tk.Entry)):
                widget.configure(background=p["bg"])
                if "foreground" in widget.keys():
                    widget.configure(foreground=p["fg"])
            elif isinstance(widget, ttk.Treeview):
                for tag, key in (("odd", "odd"), ("keyword", "keyword"), ("image_api", "image_api"),
                                  ("unread", "field"), ("skimmed", "keyword"), ("deep", "image_api")):
                    widget.tag_configure(tag, background=p[key])
                for tag, key in (("related", "related"), ("failed", "warning"), ("metadata", "muted")):
                    widget.tag_configure(tag, foreground=p[key])
            elif isinstance(widget, ttk.Label) and widget.cget("foreground"):
                value = str(widget.cget("foreground"))
                known = {"#245cc4", "#2468b8", "#82b6ff", "#164b78", "#1d5f9d"}
                if value in known:
                    widget.configure(foreground=p["accent"])
                elif value in {"#777777", "#666666", "#6b6b68", "#627084", "#a6b2c4"}:
                    widget.configure(foreground=p["muted"])
            for child in widget.winfo_children():
                self.apply_tree(child)
        except tk.TclError:
            return  # 新窗口在idle回调前关闭时不再访问其控件。

    def _titlebar(self, window) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
            enabled = ctypes.c_int(self.name == "dark")
            setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
            setter.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            setter(hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
        except (AttributeError, OSError):
            pass  # 系统不支持深色标题栏时，应用内容仍按选定主题显示。


class ThemeControls:
    def __init__(self, page, tab):
        self.page = page
        frame = ttk.LabelFrame(tab, text="外观", padding=12)
        frame.pack(fill="x", pady=(0, 12))
        ttk.Label(frame, text="界面主题：").pack(side="left")
        self.choice = ttk.Combobox(frame, state="readonly", values=("深色", "浅色"), width=12)
        self.choice.pack(side="left", padx=8)
        self.choice.current(0 if theme_name(page.prefs) == "dark" else 1)
        self.choice.bind("<<ComboboxSelected>>", self.changed)
        ttk.Label(frame, text="选择后立即保存到本地 YAML，并应用到当前窗口。", style="Hint.TLabel").pack(side="left", padx=8)

    def changed(self, _event=None) -> None:
        page = self.page
        name = "dark" if self.choice.current() == 0 else "light"
        old = theme_name(page.prefs)
        try:
            saved = prefs_mod.load_raw(page.prefs_file) if page.prefs_file.exists() else page.prefs
            data = prefs_mod.update({"ui": {**saved.get("ui", {}), "theme": name}}, page.prefs_file)
        except (OSError, ValueError) as exc:
            self.choice.current(0 if old == "dark" else 1)
            page._settings_feedback("主题保存失败：" + str(exc))
            return
        page.prefs = {**page.prefs, **data}
        page.theme.apply(name)
        page._settings_feedback(f"已切换{'深色' if name == 'dark' else '浅色'}主题并保存到 {page.prefs_file}")


def build_theme_controls(page, tab):
    page.theme_controls = ThemeControls(page, tab)
    return page.theme_controls
