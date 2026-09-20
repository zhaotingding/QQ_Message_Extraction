"""界面冒烟测试。

`python -m compileall` 只能查语法，查不出「控件绑错变量」「布局参数冲突」
「属性名写错」这类只在运行时才暴露的问题 —— 而那恰恰是界面最常见的故障。
本测试会真正构建全部控件、走一遍取值逻辑、再用假数据刷新显示。

不会写盘，也不会真的启动服务。
需要 tkinter。若当前解释器没有 tkinter，会明确提示而不是直接报错。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / "src"))

try:
    import tkinter as tk
    from tkinter import ttk
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

print("\n=== 设置页可滚动（窗口矮时「保存设置」不再被切掉）===")
allw: list = []


def walk_all(w) -> None:
    for c in w.winfo_children():
        allw.append(c)
        walk_all(c)


walk_all(root)
canvases = [w for w in allw if isinstance(w, tk.Canvas)]
check("设置页有滚动容器", len(canvases) >= 1, f"{len(canvases)} 个 Canvas")
check("滚动容器已设滚动区域", bool(canvases and canvases[0].cget("scrollregion")),
      str(canvases[0].cget("scrollregion")) if canvases else "")
wheel_bound = [w for w in allw if w.bind("<MouseWheel>") and not isinstance(w, (tk.Text, tk.Listbox))]
check("滚轮已绑到设置页控件上", len(wheel_bound) >= 5, f"{len(wheel_bound)} 个控件")
buttons = [str(w.cget("text")) for w in allw if isinstance(w, ttk.Button)]
check("有增量摘要按钮", any("最新摘要" in b for b in buttons), str(buttons))
# v0.5.0：原来的「全量重做」被换成「范围下拉 + 生成」——因为它没说清范围与字数，
# 用户原话是「意义不明」。这里锁死「不再有那个按钮」+「必须有范围选择器」。
check("不再有说不清范围的『全量重做』", not any("全量" in b for b in buttons), str(buttons))
combos = [w for w in allw if isinstance(w, ttk.Combobox)]
check("有摘要范围下拉", len(combos) >= 1, f"{len(combos)} 个 Combobox")
if combos:
    from msgx.summarize import DIGEST_RANGES  # noqa: E402

    values = list(combos[0].cget("values"))
    check(
        "范围选项 = 最近 1/3/7 天 + 1 个月",
        values == [label for label, _h, _c in DIGEST_RANGES],
        str(values),
    )

print("\n=== 实时消息页（v0.5.0：替代 NapCat 的 CMD 黑框）===")
nb = app.nb
check("有实时消息标签页", any(str(nb.tab(i, "text")) == "实时消息" for i in range(nb.index("end"))),
      str([str(nb.tab(i, "text")) for i in range(nb.index("end"))]))
check("实时消息表格列齐全",
      list(app.rt["columns"]) == ["time", "group", "who", "text", "state"],
      str(list(app.rt["columns"])))
app._render_messages(None)
check("拉取失败时给出说明", "失败" in app.v_rt_status.get(), app.v_rt_status.get())
app._render_messages([
    {"id": 2, "ts": 1700000000, "group_id": "g1", "group_name": "AI 群", "sender_name": "小明",
     "text": "第二条", "media_state": "none", "vision_state": "", "at_me": 1},
    {"id": 1, "ts": 1699999900, "group_id": "g2", "group_name": "", "sender_name": "小红",
     "text": "[图片]", "media_state": "done", "vision_state": "failed", "at_me": 0,
     "ocr_text": "报错截图", "vision_text": ""},
])
rows = app.rt.get_children()
check("消息渲染成行", len(rows) == 2, str(len(rows)))
vals = [app.rt.item(r, "values") for r in rows]
check("按新的在前排序（列表顺序即服务返回顺序）", vals[0][3] == "第二条", str(vals[0]))
check("空文本回落到 OCR 内容", vals[1][3] == "报错截图", str(vals[1]))
check("被 @ 的行有专属标记", "atme" in app.rt.item(rows[0], "tags"), str(app.rt.item(rows[0], "tags")))
check("语义失败的行有专属标记", "failed" in app.rt.item(rows[1], "tags"), str(app.rt.item(rows[1], "tags")))

print("\n=== 图片状态标注 ===")
labels = {
    app._media_label({"media_state": "none"}): "—",
    app._media_label({"media_state": "pending"}): "待处理",
    app._media_label({"media_state": "failed"}): "下载失败",
    app._media_label({"media_state": "done", "vision_state": "failed"}): "语义失败",
    app._media_label({"media_state": "done", "vision_state": "skipped"}): "仅 OCR",
    app._media_label({"media_state": "done", "vision_state": "ok"}): "完成",
}
for got, want in labels.items():
    check(f"状态 {want}", got == want, got)

print("\n=== 显示刷新（用假数据）===")
app._apply_health(None)
check("health=None 时不崩", "超时" in app.v_peer.get(), app.v_peer.get())

app._apply_health({
    "ok": True,
    "ws_peers": ["127.0.0.1"],
    "counters": {"received": 5, "filtered": 2},
    "stats": {"messages": 33, "vision_failed": 4, "vision_retry": 3},
    "spent_today_cny": 0.0286,
    "budget_cny": 10.0,
    "llm_available": True,
    "vision_supported": True,
})
check("协议端显示已连接", "已连接" in app.v_peer.get(), app.v_peer.get())
check("入库数显示", "33" in app.v_stored.get(), app.v_stored.get())
check("过滤数显示", "2" in app.v_stored.get(), app.v_stored.get())
check("花费显示（无账单快照时标明是估算）", "估算" in app.v_cost.get(), app.v_cost.get())
check("模型状态显示", "支持图片" in app.v_llm.get(), app.v_llm.get())
check("语义失败数显示", "4" in app.v_vision.get(), app.v_vision.get())

print("\n=== 账单显示（平台真实数据 vs 本地估算）===")
app._apply_health({
    "ok": True, "ws_peers": [], "counters": {}, "stats": {},
    "spent_today_cny": 0.47, "budget_cny": 10,
    "llm_available": True, "vision_supported": True,
    "billing": {
        "source": "platform", "today_cny": 1.107, "total_cny": 3.5, "currency": "CNY",
        "from_day": "2026-09-18", "platform_error": "",
        "balance": {"ok": True, "total": 16.02, "error": ""},
    },
})
check("今日花费用平台值", "1.11" in app.v_cost.get() and "平台" in app.v_cost.get(), app.v_cost.get())
check("累计花费在今日花费相邻位并标出起点", "3.50" in app.v_total.get() and "2026-09-18" in app.v_total.get(),
      app.v_total.get())
check("余额显示", "16.02" in app.v_balance.get(), app.v_balance.get())

app._apply_billing({
    "source": "estimate", "today_cny": 0.47, "total_cny": 0.47,
    "platform_error": "未配置 platform_token，只能显示本地估算",
    "balance": {"ok": False, "total": 0, "error": "未配置 API Key"},
})
check("拿不到平台数据时标注估算", "估算" in app.v_cost.get(), app.v_cost.get())
check("拿不到余额时说明原因而不是留空", "API Key" in app.v_balance.get(), app.v_balance.get())
check("抓取失败原因呈现在状态栏", "platform_token" in app.status.get(), app.status.get())

# v0.5.1 差值法：没有 platform_token 时，日/累计应是**真实值**而不是估算。
# 这是用户直接质疑过的那句「只能显示本地估算」——它当时说得太满。
print("\n=== 账单显示：余额差值法（v0.5.1）===")
_TODAY_FROM = int(time.time()) - 6 * 3600     # 今天 6 小时前 = 覆盖起点
_TOTAL_FROM = int(time.time()) - 3 * 86400    # 累计从 3 天前开始
app._apply_billing({
    "source": "balance_diff", "today_source": "balance_diff", "total_source": "balance_diff",
    "today_cny": 0.64, "total_cny": 0.66,
    "platform_error": "未配置 platform_token（消费明细拿不到；今日/累计已改用余额差值推算）",
    "balance_diff": {
        "today_cny": 0.64, "total_cny": 0.66, "enough": True, "today_enough": True,
        "points": 37, "resolution": 0.01,
        "today_from": _TODAY_FROM, "total_from": _TOTAL_FROM,
    },
    "balance": {"ok": True, "total": 15.83, "error": ""},
})
check("今日花费用余额差值口径", "0.64" in app.v_cost.get() and "余额差值" in app.v_cost.get(),
      app.v_cost.get())
check("不再标成「估算」", "估算" not in app.v_cost.get(), app.v_cost.get())
check("起点不是 00:00 时标出「自 HH:MM 起」（否则半天会被当成一整天）",
      "自" in app.v_cost.get(), app.v_cost.get())
check("累计花费同样标余额差值并给出起点日期",
      "0.66" in app.v_total.get() and "自" in app.v_total.get(), app.v_total.get())
check("状态栏说清「明细没取到但日/累计是真实的」（不再一杆子打成都估算）",
      "余额差值" in app.status.get() and "明细" in app.status.get(), app.status.get())
check("状态栏给出读数个数（便于判断可信度）", "37" in app.status.get(), app.status.get())

# 刚装上的时候读数不够两个：今日必须老实回落到估算，累计若够则仍可显示真实。
app._apply_billing({
    "source": "balance_diff", "today_source": "estimate", "total_source": "balance_diff",
    "today_cny": 0.12, "total_cny": 0.66,
    "platform_error": "未配置 platform_token",
    "balance_diff": {
        "today_cny": 0.0, "total_cny": 0.66, "enough": True, "today_enough": False,
        "points": 9, "resolution": 0.01, "today_from": 0, "total_from": _TOTAL_FROM,
    },
    "balance": {"ok": True, "total": 15.83, "error": ""},
})
check("今日读数不够时今日单独回落估算（两个数字各标各的口径）",
      "估算" in app.v_cost.get() and "余额差值" in app.v_total.get(),
      f"{app.v_cost.get()} | {app.v_total.get()}")

# 累计的起点之前没有读数，那段只能用估算补齐 —— 此时整个数字是「真实 + 估算」，
# 不能再标成纯「余额差值」（这是同一类"说得太满"的错误）。
app._apply_billing({
    "source": "balance_diff", "today_source": "balance_diff", "total_source": "balance_diff",
    "today_cny": 0.64, "total_cny": 0.99,
    "platform_error": "未配置 platform_token",
    "balance_diff": {
        "today_cny": 0.64, "total_cny": 0.24, "enough": True, "today_enough": True,
        "today_full_day": True, "points": 37, "resolution": 0.01,
        "today_from": _TODAY_FROM, "total_from": _TOTAL_FROM,
        "prior_est_cny": 0.75, "mixed": True, "from_day": "2026-09-17",
    },
    "balance": {"ok": True, "total": 15.83, "error": ""},
})
check("累计由真实+估算拼成时标明「差值+估算」",
      "差值+估算" in app.v_total.get(), app.v_total.get())
check("今日是完整的差值口径时不带 mixed 标",
      "余额差值" in app.v_cost.get(), app.v_cost.get())
check("状态栏交代「起点之前用估算补齐」并给出金额",
      "补齐" in app.status.get() and "0.75" in app.status.get(), app.status.get())

app._apply_billing({
    "source": "estimate", "today_source": "estimate", "total_source": "estimate",
    "today_cny": 0.47, "total_cny": 0.47,
    "platform_error": "未配置 platform_token，只能显示本地估算",
    "balance_diff": {"enough": False, "points": 1, "resolution": 0.01},
    "balance": {"ok": False, "total": 0, "error": "未配置 API Key"},
})

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
# NapCat 安装目录：留空 → 不自动弹二维码；填了 → 供监视 <dir>\cache\qrcode.png
app.cfg["napcat"] = {"dir": ""}
check("未配置 NapCat 目录时返回 None", app.napcat_dir() is None, str(app.napcat_dir()))
app.cfg["napcat"] = {"dir": "NapCat"}
check("相对目录拼到项目根", app.napcat_dir() == PROJ / "NapCat", str(app.napcat_dir()))
app.cfg["napcat"] = {"dir": r"D:\App\NapCat"}
check(
    "绝对目录原样返回",
    app.napcat_dir() == Path(r"D:\App\NapCat"),
    str(app.napcat_dir()),
)

print("\n=== 日志区 ===")
app._log_ui("测试日志行\n")
check("日志可写入", "测试日志行" in app.log.get("1.0", "end"))

print("\n=== v0.5.0 新增开关的读写往返 ===")
data2 = app._collect()
check("保存内容含协议端隐藏开关", "napcat_hidden" in data2, str(sorted(data2)))
check("保存内容含平台 token", "platform_token" in data2, str(sorted(data2)))
app.v_napcat_hidden.set(False)
app.txt_platform_token.delete("1.0", "end")
app.txt_platform_token.insert("1.0", "  tok-abc  ")
d3 = app._collect()
check("取消勾选后读到 False", d3["napcat_hidden"] is False, str(d3["napcat_hidden"]))
check("token 两端空白被去掉", d3["platform_token"] == "tok-abc", repr(d3["platform_token"]))

# 偏好 → 配置的叠加（只覆盖界面明确管理的字段）
from msgx import prefs as P  # noqa: E402

base = {"napcat": {"hidden": True}, "billing": {"platform_token": "cfg-token"},
        "score": {"keywords": []}, "summary": {}, "watch": {}, "filters": {}}
merged = P.apply_to_config(base, {"napcat_hidden": False, "platform_token": "ui-token"})
check("界面勾选覆盖 config", merged["napcat"]["hidden"] is False, str(merged["napcat"]))
check("界面 token 覆盖 config", merged["billing"]["platform_token"] == "ui-token",
      merged["billing"]["platform_token"])
merged2 = P.apply_to_config(base, {"platform_token": ""})
check("token 留空 = 沿用 config（不是清空）",
      merged2["billing"]["platform_token"] == "cfg-token", merged2["billing"]["platform_token"])
check("load_raw 不填默认值", P.load_raw(PROJ / ".ui" / "__not_exist__.json") == {},
      str(P.load_raw(PROJ / ".ui" / "__not_exist__.json")))

print("\n=== 协议端隐藏启动器 ===")
import os  # noqa: E402
import tempfile  # noqa: E402

from msgx import napcat as N  # noqa: E402

vbs = N.vbs_path()
check("VBS 已生成", vbs is not None and vbs.exists(), str(vbs))
if vbs is not None:
    src = vbs.read_text("ascii")  # 必须是纯 ASCII，否则 wscript 会读出错码
    check("VBS 为纯 ASCII", "Run" in src)
    check("VBS 用隐藏窗口样式 0", ", 0, True)" in src, src[-140:].replace("\n", " "))

# 关键安全性质：目录不完整 / 路径写错时必须**拒绝启动**，而不是静默换个目录启动。
tmpd = tempfile.mkdtemp()
bad = {"napcat": {"dir": r"Z:\__msgx_nope__", "log_file": os.path.join(tmpd, "n.log")}}
res = N.launch(bad, visible=False)
check("目录不存在时拒绝启动", res.get("ok") is False, str(res))
check("拒绝原因指向配置路径", "__msgx_nope__" in str(res.get("error")), str(res.get("error"))[:120])
check("写下了启动记录（便于排障）", os.path.exists(os.path.join(tmpd, "n.log")))

r = N.preflight(PROJ, {"napcat": {}})
check("预检能列出缺失文件", isinstance(r["missing"], list) and bool(r["missing"]), str(r["missing"]))
check("文件不齐时 ok = False", r["ok"] is False, str(r))
check("没配目录时 resolve_dir 不瞎猜", N.resolve_dir({"napcat": {"dir": ""}}) is None
      or (N.resolve_dir({"napcat": {"dir": ""}}) or Path()).name == "NapCat",
      str(N.resolve_dir({"napcat": {"dir": ""}})))
check("明确配了目录就原样返回（不猜测）",
      N.resolve_dir({"napcat": {"dir": r"Z:\__msgx_nope__"}}) == Path(r"Z:\__msgx_nope__"),
      str(N.resolve_dir({"napcat": {"dir": r"Z:\__msgx_nope__"}})))

root.update()
root.destroy()

print()
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
