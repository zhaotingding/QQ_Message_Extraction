"""界面冒烟测试。

`python -m compileall` 只能查语法，查不出「控件绑错变量」「布局参数冲突」
「属性名写错」这类只在运行时才暴露的问题 —— 而那恰恰是界面最常见的故障。
本测试会真正构建全部控件、走一遍取值逻辑、再用假数据刷新显示。

不会写盘，也不会真的启动服务。
需要 tkinter。若当前解释器没有 tkinter，会明确提示而不是直接报错。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / "src"))

try:
    import tkinter as tk
except ImportError:
    print("当前 Python 解释器不带 tkinter，无法运行界面测试。")
    print(f"  解释器：{sys.executable}")
    print("  说明：python.org 的官方安装版自带 tkinter；某些精简发行版不带。")
    raise SystemExit(1)

from msgx.gui import App  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        fails.append(name)


print("=== 构建界面 ===")
root = tk.Tk()
app = App(root)
root.update()  # 强制完成一次几何计算，布局错误会在这时抛出
print("  界面构建完成，无异常")

print("\n=== 控件取值 ===")
interval = app.v_interval.get()
check("摘要间隔可读", interval.isdigit(), f"值={interval!r}")
kw = app.txt_keywords.get("1.0", "end").strip()
check("关键词框已填充", len(kw) > 0, f"{len(kw.splitlines())} 行")
check("群列表已加载", isinstance(app._group_ids, list), f"{len(app._group_ids)} 个群")

data = app._collect()
check("_collect 可执行", isinstance(data, dict))
check(
    "filters 键完整",
    set(data.get("filters", {})) == {
        "skip_face_only", "skip_image_only", "skip_forward", "skip_record", "min_text_length"
    },
    str(sorted(data.get("filters", {}))),
)
check("保存内容不含辅助键", "skip_min" not in data.get("filters", {}))

print("\n=== 显示刷新（用假数据）===")
app._apply_health(None)
check("health=None 时不崩", "超时" in app.v_peer.get(), app.v_peer.get())

app._apply_health({
    "ok": True,
    "ws_peers": ["127.0.0.1"],
    "counters": {"received": 5, "filtered": 2},
    "stats": {"messages": 33},
    "spent_today_cny": 0.0286,
    "budget_cny": 10.0,
    "llm_available": True,
    "vision_supported": True,
})
check("协议端显示已连接", "已连接" in app.v_peer.get(), app.v_peer.get())
check("入库数显示", "33" in app.v_stored.get(), app.v_stored.get())
check("过滤数显示", "2" in app.v_stored.get(), app.v_stored.get())
check("花费显示", "0.0286" in app.v_cost.get(), app.v_cost.get())
check("模型状态显示", "支持图片" in app.v_llm.get(), app.v_llm.get())

# 无 API Key 时不能崩，且要给出可理解的说明
app._apply_health({
    "ok": True, "ws_peers": [], "counters": {}, "stats": {},
    "spent_today_cny": 0, "budget_cny": 10,
    "llm_available": False, "vision_supported": False,
})
# 文案要既点明「未连接」，又说清缺的是协议端 NapCat —— 只说「未连接」用户不知道下一步做什么
peer_txt = app.v_peer.get()
check(
    "未连协议端时提示准确",
    "未连接" in peer_txt and ("NapCat" in peer_txt or "协议端" in peer_txt),
    peer_txt,
)
check("无 API Key 时提示准确", "仅规则模式" in app.v_llm.get(), app.v_llm.get())

print("\n=== 协议端启动脚本解析 ===")
app.cfg["napcat"] = {"start_script": ""}
check("未配置 start_script 时返回 None", app.napcat_script() is None, str(app.napcat_script()))
app.cfg["napcat"] = {"start_script": "tools/whatever.bat"}
check(
    "相对路径拼到项目根",
    app.napcat_script() == PROJ / "tools" / "whatever.bat",
    str(app.napcat_script()),
)
app.cfg["napcat"] = {"start_script": r"D:\App\NapCat\start-napcat.bat"}
check(
    "绝对路径原样返回",
    app.napcat_script() == Path(r"D:\App\NapCat\start-napcat.bat"),
    str(app.napcat_script()),
)

print("\n=== 日志区 ===")
app._log_ui("测试日志行\n")
check("日志可写入", "测试日志行" in app.log.get("1.0", "end"))

root.update()
root.destroy()

print()
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
