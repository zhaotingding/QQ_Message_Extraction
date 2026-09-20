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

# ---------------------------------------------------------------------------
# v0.5.2：点一次「启动协议端」= 注入一份 NapCat **并且再拉起一份 QQ**。
# 用户看到「没反应」很容易反复点 → 多份实例并存、各自写 cache/qrcode.png 互相覆盖
# → 扫哪一次都被覆盖，谁也登不上，表现恰好是「点了很多次、消息一直不来」。
# 所以：① 启动前要能查「是不是已经在跑了」；② 界面要拦一下；③ 日志页不能再拿
# 「logs 目录是空的」当成「协议端从未启动过」的证据（这个错误结论真的误导过用户）。
# ---------------------------------------------------------------------------
print("\n=== 检测「协议端是否已在运行」（v0.5.2）===")
import subprocess as _sp  # noqa: E402

_orig_sp_run = _sp.run
_orig_running = N.running_instances
_orig_launch = N.launch


class _FakeProc:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.returncode = 0


def _patch_tasklist(stdout: str) -> None:
    _sp.run = lambda *a, **k: _FakeProc(stdout)  # type: ignore[assignment]


# 没有匹配进程时 tasklist 会打印一行「INFO: ...」/「信息: ...」——那不是进程
_patch_tasklist("INFO: No tasks are running which match the specified criteria.\n")
check("没有匹配进程时计数为 0（不把 INFO 行当进程）", N._tasklist_count("QQ.exe") == 0, "")
_patch_tasklist("信息: 没有运行的任务匹配指定标准。\n")
check("中文系统的「信息:」行同样不计数", N._tasklist_count("QQ.exe") == 0, "")
_patch_tasklist('"QQ.exe","11","Console","1","1 K"\n"QQ.exe","22","Console","1","1 K"\n')
check("两行进程就数出 2 个", N._tasklist_count("QQ.exe") == 2, str(N._tasklist_count("QQ.exe")))
_patch_tasklist('"NapCatWinBootMain.exe","9","Console","1","1 K"\n')
_ri = N.running_instances()
check("running_instances 报告注入器数量", _ri["injectors"] == 1, str(_ri))
check("running_instances 的 any 由注入器决定", _ri["any"] is True, str(_ri))
_patch_tasklist("")
_ri0 = N.running_instances()
check("没有实例时 injectors=0 且 any=False", _ri0["injectors"] == 0 and _ri0["any"] is False, str(_ri0))
_sp.run = _orig_sp_run  # type: ignore[assignment]

print("\n=== 日志页：不得把「logs 空着」当成「协议端没启动」的证据 ===")
N.running_instances = lambda: {"injectors": 0, "qq": 0, "any": False}  # type: ignore[assignment]
_empty = tempfile.mkdtemp()
os.makedirs(os.path.join(_empty, "logs"), exist_ok=True)
_txt = N.log_tail({"napcat": {"dir": _empty, "log_file": os.path.join(tmpd, "n2.log")}})
check("不再断言「协议端可能从未成功启动过」", "从未成功启动" not in _txt, _txt[-160:])
check("解释「看不到日志 ≠ 没启动」", "不等于" in _txt, _txt[-200:])
check("给出当前进程实况（重复启动一眼可见）", "当前进程" in _txt, _txt[-260:])
check("区分「空目录」这一情形", "空目录" in _txt, _txt[-200:])
_gone = os.path.join(tempfile.mkdtemp(), "__nope__")
_txt2 = N.log_tail({"napcat": {"dir": _gone, "log_file": os.path.join(tmpd, "n3.log")}})
check("目录不存在时说的是「目录不存在」而不是「空目录」",
      "目录不存在" in _txt2 and "空目录" not in _txt2, _txt2[-200:])
# 注意：dir 留空时 resolve_dir 会去探测常见安装位置，所以这里不一定是「未配置」分支。
# 要保证的只是「给出可读说明」且「不把日志缺失当成没启动的证据」。
_txt3 = N.log_tail({"napcat": {"dir": "", "log_file": os.path.join(tmpd, "n4.log")}})
check("dir 留空时仍给出可读说明且不下否定结论",
      ("未配置" in _txt3 or "logs" in _txt3) and "从未成功启动" not in _txt3, _txt3[-160:])
N.running_instances = _orig_running  # type: ignore[assignment]

print("\n=== 启动前拦截重复注入（v0.5.2）===")
app.cfg["napcat"] = {"dir": ""}  # 不监视二维码，避免测试里弹窗
_calls = {"n": 0}


def _fake_launch(*a, **k):
    _calls["n"] += 1
    return {"ok": True, "note": "stub"}


N.launch = _fake_launch  # type: ignore[assignment]
import tkinter.messagebox as _mb  # noqa: E402

_orig_ask = _mb.askyesno
_asked = {"n": 0, "msg": ""}


def _ask_no(*a, **k):
    _asked["n"] += 1
    # 记下弹窗正文：文案本身也是契约的一部分（见下面的检查）
    parts = [str(x) for x in a] + [str(v) for v in k.values()]
    _asked["msg"] = "\n".join(parts)
    return False


N.running_instances = lambda: {"injectors": 3, "qq": 12, "any": True}  # type: ignore[assignment]
_mb.askyesno = _ask_no  # type: ignore[assignment]
app.start_napcat()
check("检测到已有实例时先问一句", _asked["n"] == 1, str(_asked["n"]))
# 正常状态下 QQ.exe 就有 3~4 个（1 主进程 + 辅助进程），单看它会让人以为「怎么这么多」。
# 判定只看注入器数，QQ.exe 的数量必须在文案里说明白，否则弹窗本身就成了新的误导源。
check("弹窗说明了 QQ.exe 有辅助进程、数量多属正常",
      "辅助进程" in _asked["msg"], _asked["msg"][:160])
check("用户选「否」时不再注入一份", _calls["n"] == 0, str(_calls))
check("取消时状态栏说清原因", "已在运行" in app.status.get() or "取消" in app.status.get(),
      app.status.get())

_mb.askyesno = lambda *a, **k: True  # type: ignore[assignment]
app.start_napcat()
check("用户确认「是」时照旧启动（不挡用户）", _calls["n"] == 1, str(_calls))

N.running_instances = lambda: {"injectors": 0, "qq": 0, "any": False}  # type: ignore[assignment]
app.start_napcat()
check("没有实例时不打扰用户、直接启动", _calls["n"] == 2, str(_calls))

_mb.askyesno = _orig_ask  # type: ignore[assignment]
N.running_instances = _orig_running  # type: ignore[assignment]
N.launch = _orig_launch  # type: ignore[assignment]

root.update()
root.destroy()

print()
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
