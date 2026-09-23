"""界面偏好的读写与叠加。

设计原则（与 config.toml 的分工）：
  * `config.toml` 受 Git 管理，是**团队共享**的默认配置，界面永不改写它。
  * `.ui/preferences.json` 是**每个人本地**的偏好，不入库，由界面自由读写。

服务进程通过环境变量 `MSGX_PREFS` 获得偏好文件路径，在 `config.load()` 里叠加。
摘要提示词和目标字数随每次手动请求传入；其他服务设置在重启后生效。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# 偏好文件的默认位置（相对项目根）
PREFS_REL = Path(".ui") / "preferences.json"
ENV_PREFS = "MSGX_PREFS"

# 界面可调项的默认值。注意「默认不过滤任何东西」——
# 过滤是 0.2.0 才引入的能力，默认值必须保持与 0.1.0 完全一致的行为。
DEFAULTS: dict = {
    "summary_prompt": "",    # 空 = 沿用 config.toml 或内置提示词
    "summary_target_chars": 3000,
    "groups": [],             # 空 = 关注全部群（群列表是「全选/多选」语义，空即全部）
    "filters": {
        "skip_face_only": False,    # 纯表情 / 表情包
        "skip_image_only": False,   # 纯图片（无任何文字）
        "skip_forward": False,      # 合并转发
        "skip_record": False,       # 语音
        "min_text_length": 0,       # 短于此长度的消息不接收（0 = 不过滤）
    },
    # 仅供参考展示，不入库
    "ui": {"last_tab": 0},
    # 协议端启动方式：true = 隐藏控制台窗口（默认，用户明确不要任务栏里堆黑框）。
    # 它是界面上的勾选框，所以必须走偏好；界面不改写 config.toml。
    "napcat_hidden": True,
    "qq_account": "",
    # DeepSeek 平台登录态 token（查真实消费用）。**留空 = 沿用 config.toml**。
    # 说明：它是短期凭证，存在本地偏好文件里（.ui/ 不入版本库），不会进 Git。
    "platform_token": "",
}

# 已知的消息占位符（models.py 生成的）。过滤判定要基于「去掉占位符后的实际文本」，
# 否则 "[图片]" 这 4 个字符会被误判成一条有内容的短消息。
_PLACEHOLDER_RE = re.compile(
    r"\[(?:图片|表情|表情包|合并转发|语音|视频|record|video|forward|文件:[^\]]*)\]|@全体成员"
)


def prefs_path(root: Path | str) -> Path:
    return Path(root) / PREFS_REL


def load(path: Path | str | None = None) -> dict:
    """读取偏好。文件不存在或损坏时返回默认值（并保留原始文件不动）。"""
    p = Path(path) if path else None
    if p is None or not p.exists():
        return json.loads(json.dumps(DEFAULTS))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULTS))
    return _merge(json.loads(json.dumps(DEFAULTS)), data if isinstance(data, dict) else {})


def save(prefs: dict, path: Path | str) -> None:
    """原子写入：先写临时文件再替换，避免中途中断留下半份 JSON。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    if "api" in prefs:
        from .api_settings import validate
        prefs = {**prefs, "api": validate(prefs["api"])}
    if "api_profiles" in prefs:
        from .api_settings import catalog
        profiles, active = catalog({}, prefs)
        prefs = {**prefs, "api_profiles": profiles, "active_api_profile": active, "api": profiles[active]}
    payload = json.dumps(prefs, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(payload, encoding="utf-8", newline="\n")
    tmp.replace(p)


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_raw(path: Path | str | None = None) -> dict:
    """只读文件里**真实写了**的内容，不做默认值填充。

    用途：区分「用户从没设置过」和「用户就是选了默认值」。界面靠它决定某个
    开关要不要回退到 config.toml 里的团队默认值 —— 填了默认值的 `load()`
    会把两种情形抹平成一样的，那样 config.toml 里的默认值就永远不生效。
    """
    p = Path(path) if path else None
    if p is None or not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_from_env() -> dict | None:
    """服务进程侧：若设置了 MSGX_PREFS 且文件存在，返回偏好；否则 None。"""
    raw = os.environ.get(ENV_PREFS)
    if not raw:
        return None
    p = Path(raw)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = data if isinstance(data, dict) else {}
    prefs = _merge(json.loads(json.dumps(DEFAULTS)), raw)
    if "summary_target_chars" not in raw:
        prefs.pop("summary_target_chars", None)
    return prefs


def apply_to_config(cfg: dict, prefs: dict) -> dict:
    """把偏好叠加到配置字典上（返回新字典，不修改入参）。

    只覆盖界面明确管理的字段。未设置或为空的项保持 config.toml 的值不变 ——
    这样「界面留空」等于「沿用配置文件」，而不是「清空」。
    """
    out = json.loads(json.dumps(cfg, ensure_ascii=False, default=str))

    prompt = str(prefs.get("summary_prompt") or "").strip()
    if prompt:
        out.setdefault("summary", {})["prompt"] = prompt
    try:
        target_chars = int(prefs.get("summary_target_chars") or 0)
        if target_chars > 0:
            out.setdefault("summary", {})["target_chars"] = target_chars
    except (TypeError, ValueError):
        pass

    groups = [str(g).strip() for g in (prefs.get("groups") or []) if str(g).strip()]
    # 群列表是「多选」语义：界面永远写出完整列表。全部取消勾选 = 关注全部群，
    # 所以这里不区分「未设置」和「空」—— 空就是全部。
    out["watch"]["groups"] = groups

    filters = prefs.get("filters") or {}
    out.setdefault("filters", {})
    for key in ("skip_face_only", "skip_image_only", "skip_forward", "skip_record"):
        out["filters"][key] = bool(filters.get(key, False))
    try:
        out["filters"]["min_text_length"] = max(0, int(filters.get("min_text_length") or 0))
    except (TypeError, ValueError):
        out["filters"]["min_text_length"] = 0

    # 协议端隐藏启动：勾选框是「明确表达」，所以 true/false 都写下去。
    if "napcat_hidden" in prefs:
        out.setdefault("napcat", {})["hidden"] = bool(prefs.get("napcat_hidden"))
    if "qq_account" in prefs:
        out.setdefault("napcat", {})["account"] = str(prefs.get("qq_account") or "").strip()

    # 平台 token：留空 = 沿用 config.toml。
    token = str(prefs.get("platform_token") or "").strip()
    if token:
        out.setdefault("billing", {})["platform_token"] = token

    if "api_profiles" in prefs:
        from .api_settings import apply, catalog
        profiles, active = catalog(cfg, prefs)
        out = apply(out, profiles[active])
        out.update(api_profiles=profiles, active_api_profile=active)
    elif "api" in prefs:
        from .api_settings import apply
        out = apply(out, prefs["api"])
    return out


# ---------- 过滤判定 ----------

def plain_text(text: str) -> str:
    """去掉所有占位符与 @全体成员后的实际文本。"""
    return _PLACEHOLDER_RE.sub("", text or "").strip()


def skip_reason(msg, filters: dict) -> str:
    """判断消息是否应被丢弃。返回原因字符串；空字符串表示应当接收。

    msg 是 models.Msg 实例。判定全部基于已归一化的字段，不依赖原始事件。
    """
    if not filters:
        return ""

    text = msg.text or ""
    body = plain_text(text)

    if filters.get("skip_face_only") and not body:
        # 文本里只剩 [表情] / [表情包] → 纯表情消息
        stripped_all = text
        for marker in ("[表情]", "[表情包]"):
            stripped_all = stripped_all.replace(marker, "")
        if not stripped_all.strip():
            return "纯表情"

    if filters.get("skip_image_only") and msg.image_urls and not body:
        # 有图但去掉占位符后没有任何文字。注意纯表情包也会带图片 URL，
        # 但它在上一条已被处理；这里再排除一次，避免两个开关互相干扰。
        stripped_all = text
        for marker in ("[表情]", "[表情包]"):
            stripped_all = stripped_all.replace(marker, "")
        if not stripped_all.strip():
            return "纯表情"
        return "纯图片"

    if filters.get("skip_forward") and "[合并转发]" in text:
        return "合并转发"

    if filters.get("skip_record") and "[语音]" in text:
        return "语音"

    try:
        minimum = int(filters.get("min_text_length") or 0)
    except (TypeError, ValueError):
        minimum = 0
    # 只对「本来就有文字」的消息做长度判定。
    # 否则纯图片 / 纯表情（去掉占位符后 body 为空）会被顺带干掉 ——
    # 用户只是想过滤「嗯」「好的」这类短回复，却发现所有图片都没了，非常反直觉。
    # 图片和表情各自有专门的开关。
    if minimum > 0 and body and len(body) < minimum:
        return f"短于 {minimum} 字"

    return ""
