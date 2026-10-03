"""消息过滤判定测试。

覆盖 prefs.skip_reason 与 prefs.plain_text。
这些规则直接决定「什么消息会被丢弃」，误判会造成静默丢数据，因此每条都要有断言。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from msgx.models import Msg  # noqa: E402
from msgx.prefs import apply_to_config, load, load_from_env, plain_text, save, skip_reason  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        fails.append(name)


face = Msg(msg_id="1", group_id="g", text="[表情]")
mface = Msg(msg_id="2", group_id="g", text="[表情包]", image_urls=["http://x/a.png"])
img = Msg(msg_id="3", group_id="g", text="[图片]", image_urls=["http://x/b.png"])
mixed = Msg(msg_id="4", group_id="g", text="看这个报错 [图片]", image_urls=["http://x/c.png"])
fwd = Msg(msg_id="5", group_id="g", text="[合并转发]")
rec = Msg(msg_id="6", group_id="g", text="[语音]")
short = Msg(msg_id="7", group_id="g", text="好的")
normal = Msg(msg_id="8", group_id="g", text="正常的技术讨论内容")

print("=== 过滤规则 ===")
for sender_id, sender_name in (("2854196310", "改过群名片"), ("other", "Q群管家"), ("", " Q群管家 ")):
    bot = Msg(msg_id="bot", group_id="g", sender_id=sender_id, sender_name=sender_name,
              text="签到广告[图片]", image_urls=["https://invalid.test/bot.png"])
    check("管家按账号或精确名称过滤，无需开启其它过滤", skip_reason(bot, {}) == "过滤发言人: Q群管家")
    check("管家图片不走普通纯图保留规则", skip_reason(bot, {"skip_image_only": True}) == "过滤发言人: Q群管家")
check("正文提及管家或相似昵称不误过滤", skip_reason(
    Msg(msg_id="person", group_id="g", sender_name="我不是Q群管家", text="Q群管家怎么关闭"), {}) == "")
check("纯表情 命中", skip_reason(face, {"skip_face_only": True}) == "纯表情")
check("表情包 命中（它也带图片 URL）", skip_reason(mface, {"skip_face_only": True}) == "纯表情")
check("纯图片 命中", skip_reason(img, {"skip_image_only": True}) == "纯图片")
check("纯图片 不被纯表情规则误伤", skip_reason(img, {"skip_face_only": True}) == "")
check("带文字的图片 不命中", skip_reason(mixed, {"skip_image_only": True}) == "")
check("合并转发 命中", skip_reason(fwd, {"skip_forward": True}) == "合并转发")
check("语音 命中", skip_reason(rec, {"skip_record": True}) == "语音")
check("短消息 命中", skip_reason(short, {"min_text_length": 5}) == "短于 5 字")

print("\n=== 占位符处理（关键回归）===")
# "[图片]" 是 4 个字符。若直接按原文长度判断，min_text_length=5 时它会被
# 当成「有 4 个字的短消息」而放过/拦下，与用户预期不符。
check("占位符不计入长度", plain_text("[图片][表情]") == "", repr(plain_text("[图片][表情]")))
check("占位符不影响真实文本", plain_text("[图片] 显存怎么调") == "显存怎么调")
check("文件占位符可剥离", plain_text("[文件:report.pdf] 见附件") == "见附件")
check("纯图片在 min_length 下不被误判为短消息", skip_reason(img, {"min_text_length": 5}) == "")

print("\n=== 默认值必须放行一切 ===")
check("默认配置下放行", skip_reason(normal, {}) == "")
check("开关关闭时不生效", skip_reason(face, {"skip_face_only": False}) == "")
check("min_length=0 不过滤", skip_reason(short, {"min_text_length": 0}) == "")

print("\n=== 偏好叠加到配置 ===")
cfg = {"summary": {"prompt": "原有提示词", "target_chars": 3000},
       "watch": {"groups": []}, "filters": {}}
merged = apply_to_config(cfg, {"summary_prompt": "自定义提示词 {{目标字数}}", "summary_target_chars": 4500,
                              "keywords": ["旧热点词"], "interval_minutes": 15, "groups": ["123"],
                              "filters": {"skip_face_only": True, "min_text_length": 3}})
check("提示词被覆盖", merged["summary"]["prompt"] == "自定义提示词 {{目标字数}}")
check("目标字数被覆盖", merged["summary"]["target_chars"] == 4500)
check("旧热点词和自动间隔不再生效", "score" not in merged and "interval_minutes" not in merged["summary"])
check("群列表被覆盖", merged["watch"]["groups"] == ["123"])
check("过滤开关生效", merged["filters"]["skip_face_only"] is True)
check("最小长度生效", merged["filters"]["min_text_length"] == 3)
check("入参 cfg 未被修改", cfg["summary"] == {"prompt": "原有提示词", "target_chars": 3000})

empty = apply_to_config(cfg, {"summary_prompt": "", "summary_target_chars": 0, "groups": []})
check("空提示词 = 沿用配置文件", empty["summary"]["prompt"] == "原有提示词")
check("无目标覆盖时沿用配置文件", empty["summary"]["target_chars"] == 3000)
check("空群列表 = 关注全部", empty["watch"]["groups"] == [])
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "preferences.json"
    saved = {"summary_prompt": "只归纳技术主题 {{目标字数}}", "summary_target_chars": 4200,
             "interval_minutes": 720, "unrelated_preference": "保留"}
    save(saved, path)
    loaded = load(path)
    check("提示词与目标字数持久保存", loaded["summary_prompt"] == saved["summary_prompt"]
          and loaded["summary_target_chars"] == 4200)
    check("旧偏好和无关偏好仍保留", loaded["interval_minutes"] == 720
          and loaded["unrelated_preference"] == "保留")
    save({"interval_minutes": 720}, path)
    with patch.dict("os.environ", {"MSGX_PREFS": str(path)}):
        config_custom = {**cfg, "summary": {"target_chars": 5000}}
        check("旧偏好文件不覆盖配置中的目标字数", apply_to_config(config_custom, load_from_env())["summary"]["target_chars"] == 5000)

print()
if fails:
    print(f"存在 {len(fails)} 项失败: {fails}")
    raise SystemExit(1)
print("全部通过")
