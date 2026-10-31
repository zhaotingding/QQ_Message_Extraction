"""统一 ttk 复选框为勾号，保留当前主题的标签、焦点与操作行为。"""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk


def _indicator(root: tk.Misc, size: int, *, selected=False, disabled=False,
               active=False, pressed=False, alternate=False) -> tk.PhotoImage:
    image = tk.PhotoImage(master=root, width=size, height=size)  # 每个解释器持有自己的图像。
    edge = "#a8adb3" if disabled else "#235aa3" if active or pressed else "#65717d"
    fill = ("#d5d9df" if disabled else "#174d94" if pressed else "#2468b8") if selected else (
        "#eeeeee" if disabled else "#eef5fe" if active else "#ffffff")
    mark = "#8a929c" if disabled else "#ffffff" if selected else "#526475"
    border = max(1, round(size / 14))  # 依据真实像素尺寸缩放，不固定为一像素细边。
    image.put(edge, to=(0, 0, size, size))
    image.put(fill, to=(border, border, size - border, size - border))
    scale = size / 14
    points = [(3.0 * scale, 7.0 * scale), (5.6 * scale, 9.5 * scale), (11.0 * scale, 3.8 * scale)]
    if selected:
        for y in range(border, size - border):
            for x in range(border, size - border):
                for (ax, ay), (bx, by) in zip(points, points[1:]):
                    fraction = max(0, min(1, ((x + .5 - ax) * (bx - ax) + (y + .5 - ay) * (by - ay)) /
                                              ((bx - ax) ** 2 + (by - ay) ** 2)))
                    if math.hypot(x + .5 - ax - fraction * (bx - ax), y + .5 - ay - fraction * (by - ay)) <= .9 * scale:
                        image.put(mark, (x, y))  # 两段折线形成勾，不使用字体里的叉形字形。
                        break
    elif alternate:
        image.put(mark, to=(round(3 * scale), round(6 * scale), round(11 * scale), round(8 * scale)))
    return image


def install_check_style(root: tk.Tk, style: ttk.Style) -> None:
    if getattr(root, "_msgx_check_images", None):
        return  # 同一窗口重复应用设置时不丢失正在使用的图片引用。
    size = max(14, round(14 * root.winfo_fpixels("1i") / 96))
    variants = {
        "normal": {}, "selected": {"selected": True},
        "active": {"active": True}, "active_selected": {"active": True, "selected": True},
        "pressed": {"pressed": True}, "pressed_selected": {"pressed": True, "selected": True},
        "disabled": {"disabled": True}, "disabled_selected": {"disabled": True, "selected": True},
        "alternate": {"alternate": True}, "disabled_alternate": {"disabled": True, "alternate": True},
    }
    images = {name: _indicator(root, size, **options) for name, options in variants.items()}
    element = "Msgx.Checkbutton.indicator"
    style.element_create(
        element, "image", images["normal"],
        ("disabled", "selected", images["disabled_selected"]),
        ("disabled", "alternate", images["disabled_alternate"]), ("disabled", images["disabled"]),
        ("pressed", "selected", images["pressed_selected"]), ("active", "selected", images["active_selected"]),
        ("selected", images["selected"]), ("alternate", images["alternate"]),
        ("pressed", images["pressed"]), ("active", images["active"]), sticky="", border=0,
    )

    def replace(layout):
        updated = []
        for name, settings in layout:
            settings = dict(settings)
            if "children" in settings:
                settings["children"] = replace(settings["children"])
            updated.append((element if name.endswith("Checkbutton.indicator") else name, settings))
        return updated

    style.layout("TCheckbutton", replace(style.layout("TCheckbutton")))  # 标签和焦点布局完整沿用。
    root._msgx_check_images = images  # 防止 Python 垃圾回收使控件变成空白框。
