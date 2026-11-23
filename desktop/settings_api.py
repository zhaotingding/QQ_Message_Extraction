"""Electron 设置页复用 msgx 配置、凭据及本地服务。"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from msgx import api_settings, credentials, prefs, security, yaml_config
from msgx.runtime_paths import FROZEN, PROGRAM_ROOT
from msgx.app_icon import selected_icon
from msgx.config import abs_path, config_path
from msgx.image_policy import default_keyword_rules, screen_image, validate_analysis
from msgx.topic_reports import DEFAULT_TOPIC_PROMPT
from msgx.summarize import DEFAULT_DIGEST_PROMPT, DIGEST_DEFAULT_CHARS


FILTER_FLAGS = ("skip_face_only", "skip_image_only", "skip_forward", "skip_record")
IMAGE_FIELDS = ("enabled", "threshold", "daily_limit", "daily_budget_cny", "history_days")


def _read(ctx):
    # 损坏的本机偏好显示为读取失败，避免保存时用内置值覆盖。
    return yaml_config.read(ctx.prefs_file) if ctx.prefs_file.exists() else {}


def _theme(saved):
    return "light" if saved.get("ui", {}).get("theme") == "light" else "dark"


def _image(ctx, saved):
    return {**ctx.base_cfg.get("media", {}).get("analysis", {}), **saved.get("image_analysis", {})}


def _image_profile(ctx, saved):
    return saved.get("image_api_profile", ctx.base_cfg.get("media", {}).get("image_api_profile", ""))


def _api_state(ctx, saved):
    profiles, active = api_settings.catalog(ctx.base_cfg, saved)
    icon = selected_icon(ctx.root, saved, ctx.prefs_file)
    return {"profiles": profiles, "active": active, "imageProfile": _image_profile(ctx, saved),
            "iconPath": str(icon) if icon else ""}


def _settings(ctx, saved):
    summary = ctx.base_cfg.get("summary", {})
    filters = {**prefs.DEFAULTS["filters"], **ctx.base_cfg.get("filters", {}), **saved.get("filters", {})}
    napcat = ctx.base_cfg.get("napcat", {})
    try:
        target_chars = int(saved.get("summary_target_chars") or summary.get("target_chars") or DIGEST_DEFAULT_CHARS)
    except (TypeError, ValueError):
        target_chars = DIGEST_DEFAULT_CHARS
    return {
        **_api_state(ctx, saved), "theme": _theme(saved), "prefsPath": str(ctx.prefs_file),
        "managedProtocol": bool(ctx.cfg.get("napcat", {}).get("managed_dir")),
        "protocolSkipped": FROZEN and bool(ctx.cfg.get("napcat", {}).get("managed_dir")) and (PROGRAM_ROOT / "protocol-skipped").is_file(),
        "configPath": str(config_path(ctx.config_path, root=ctx.root)), "defaultPrompt": DEFAULT_DIGEST_PROMPT,
        "defaultTopicPrompt": DEFAULT_TOPIC_PROMPT,
        "topicPrompt": saved.get("topic_prompt") or summary.get("topic_prompt") or DEFAULT_TOPIC_PROMPT,
        "summaryPrompt": saved.get("summary_prompt") or summary.get("prompt") or DEFAULT_DIGEST_PROMPT,
        "targetChars": target_chars if 1 <= target_chars <= 100000 else DIGEST_DEFAULT_CHARS,
        "promptEvolution": {"enabled": False, "days": 7, "interval_days": 7,
                            **summary.get("prompt_evolution", {}), **saved.get("prompt_evolution", {})},
        "image": validate_analysis(_image(ctx, saved)), "groups": [str(v) for v in saved.get("groups", ctx.base_cfg.get("watch", {}).get("groups", []))],
        "filters": {**{key: bool(filters[key]) for key in FILTER_FLAGS},
                    "blocked_senders": prefs.blocked_senders(filters),
                    "min_text_length": max(0, int(filters.get("min_text_length") or 0))},
        "account": str(saved.get("qq_account") or ctx.cfg.get("napcat", {}).get("account") or ""),
        "napcatDir": saved.get("napcat_dir") or napcat.get("dir") or "",
        "qqPath": saved.get("qq_path") or napcat.get("qq_path") or "",
    }


def _save(ctx, changes):
    saved = prefs.update(changes, ctx.prefs_file)
    ctx.reload_config()
    return saved


def _api_changes(profiles, active):
    return {"api_profiles": profiles, "active_api_profile": active, "api": profiles[active]}


def _profiles(ctx, action, values, saved):
    profiles, active = api_settings.catalog(ctx.base_cfg, saved)
    changes = {}
    if action == "add":
        occupied = set(profiles) | set(saved.get("deleted_api_profiles", []))
        number = 1
        while f"custom_{number}" in occupied:
            number += 1
        active = f"custom_{number}"
        profiles[active] = api_settings.validate(values["profile"])
    else:
        active = values["id"]
        if active not in profiles:
            raise ValueError("此 API 配置已不存在，请重新加载设置")
        if action == "save":
            profiles[active] = api_settings.validate(values["profile"])
        if action == "delete":
            if len(profiles) == 1:
                raise ValueError("至少保留一套 API 配置")
            changes["deleted_api_profiles"] = [*saved.get("deleted_api_profiles", []), active]
            if _image_profile(ctx, saved) == active:
                changes["image_api_profile"] = ""
            del profiles[active]
            active = next(iter(profiles))
    return _api_state(ctx, _save(ctx, {**changes, **_api_changes(profiles, active)}))


def _credential(ctx, action, values, saved):
    profile_id = values["id"]
    profiles, _ = api_settings.catalog(ctx.base_cfg, saved)
    if profile_id not in profiles:
        raise ValueError("此 API 配置已不存在，请重新加载设置")
    url = api_settings.endpoint(values["base_url"])
    if action in ("save", "delete") and url != profiles[profile_id]["base_url"]:
        raise ValueError("请先保存当前 API 地址")
    configured = False
    if action == "save":
        key = values["key"].strip()
        if not key:
            raise ValueError("请输入新密钥")
        security.register(key)
        credentials.save(url, key, profile_id=profile_id)
        configured = True
    elif action == "delete":
        credentials.delete(url, profile_id=profile_id)
    else:
        configured = bool(credentials.read(url, profile_id=profile_id))
    return {"configured": configured, "target": credentials.target_name(url, profile_id)}


def _image_changes(ctx, values, saved, profiles):
    profile = values["imageProfile"]
    if profile and profile not in profiles:
        raise ValueError("请选择已保存的图片 API 配置")
    # 图片表单只更新可见参数。隐藏参数及独立编辑过的规则来自当前磁盘配置。
    image = validate_analysis({**_image(ctx, saved), **{name: values["image"][name] for name in IMAGE_FIELDS}})
    return {"image_analysis": image, "image_api_profile": profile}


def _report_changes(values, saved):
    try:
        chars = int(values["targetChars"])
        if not 1 <= chars <= 100000:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("摘要目标字数须为 1～100000 的整数") from None
    prompt = values["promptEvolution"]
    try:
        days, interval = int(prompt["days"]), int(prompt["interval_days"])
        if not 2 <= days <= 30 or not 1 <= interval <= 30:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("提示词材料须为 2～30 天，更新间隔须为 1～30 天") from None
    return {
        **({"topic_prompt": values["topicPrompt"].strip()} if "topicPrompt" in values else {}),
        "summary_prompt": values["summaryPrompt"].strip(), "summary_target_chars": chars,
        "prompt_evolution": {**saved.get("prompt_evolution", {}), "enabled": bool(prompt["enabled"]),
                             "days": days, "interval_days": interval},
    }


def _connection_changes(values, saved):
    account = values["account"].strip()
    if account and (not account.isascii() or not account.isdigit() or int(account) == 0):
        raise ValueError("目标 QQ 号须为非零数字账号")
    return {
        "qq_account": account, "napcat_dir": values["napcatDir"].strip(), "qq_path": values["qqPath"].strip(),
    }


def _reception_changes(values, saved):
    groups = values["groups"]
    if not isinstance(groups, list) or any(not isinstance(gid, str) or not gid.isascii() or not gid.isdigit() for gid in groups):
        raise ValueError("群号列表无效")
    filters = values["filters"]
    return {
        "groups": list(dict.fromkeys(groups)),
        "filters": {**saved.get("filters", {}), **{name: bool(filters[name]) for name in FILTER_FLAGS},
                    "blocked_senders": prefs.blocked_senders(filters),
                    "min_text_length": max(0, int(filters["min_text_length"] or 0))},
    }


def _save_settings(ctx, values, saved):
    parts = {"reports": _report_changes, "connection": _connection_changes, "reception": _reception_changes}
    if "section" in values:
        section = values["section"]
        if section not in parts:
            raise ValueError("未知设置子页")
        changes = parts[section](values, saved)
    else:
        # 旧界面仍可整页保存。子页只更新自己展示的字段，保留其它页的草稿与磁盘设置。
        profiles, active = api_settings.catalog(ctx.base_cfg, saved)
        if values["id"] != active:
            raise ValueError("当前 API 配置已切换，请重新加载设置")
        profiles[active] = api_settings.validate(values["profile"])
        changes = {**_api_changes(profiles, active), **_image_changes(ctx, values, saved, profiles)}
        for collect in parts.values():
            changes.update(collect(values, saved))
    return _settings(ctx, _save(ctx, changes))


def _groups(ctx, action, saved):
    sync, warning = {}, ""
    if action != "settings.groups":
        try:
            sync = ctx.request("/groups/refresh", method="POST", query={"prune": 1} if action.endswith("prune") else None,
                               timeout=20)
        except (OSError, RuntimeError) as exc:
            if action.endswith("prune"):
                raise
            warning = f"群列表同步失败，显示本地数据：{security.redact(str(exc))}"
    rows = []
    if abs_path(ctx.cfg, "db_path").exists():
        account = str(saved.get("qq_account") or ctx.cfg.get("napcat", {}).get("account") or "")
        with ctx.read_store() as store:
            rows = store.group_rows(account=account)
    return {"rows": rows, "account": sync.get("uin", ""), "warning": warning,
            "deleted": len(sync.get("deleted") or []), "protected": len(sync.get("protected") or [])}


def _save_icon(ctx, source, saved):
    from PIL import Image

    # 复制完整 ICO，或把 PNG 缩放成 Windows 所需的多尺寸图标。
    with Image.open(source) as image:
        if image.format == "ICO":
            image.load()
            content = source.read_bytes()
        elif image.format == "PNG":
            icon = image.convert("RGBA")
            side = max(icon.size)
            canvas = Image.new("RGBA", (side, side))
            canvas.paste(icon, ((side - icon.width) // 2, (side - icon.height) // 2))
            sizes = [(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
            frames = [canvas.resize(size, Image.Resampling.LANCZOS) for size in sizes]
            output = BytesIO()
            frames[-1].save(output, format="ICO", sizes=sizes, append_images=frames[:-1])
            content = output.getvalue()
        else:
            raise ValueError("请选择 PNG 或 ICO 图标")
    target = ctx.prefs_file.parent / "app-icon.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    _save(ctx, {"ui": {**saved.get("ui", {}), "icon_path": str(target)}})
    return {"iconPath": str(target)}


def handle(ctx, action, payload):
    # 读改写在同一偏好锁内；服务请求和图片试匹配可与其他页面并行。
    if action.startswith("settings.prompt."):
        return _prompt(ctx, action, payload)
    if action in ("settings.groups", "settings.groups.refresh", "settings.groups.prune"):
        return _groups(ctx, action, _read(ctx))
    if action == "settings.image.test":
        sample = payload["sample"][:12000]
        decision = screen_image(sample, "", keyword_rules=payload["rules"])
        return {"score": decision.score, "reason": decision.reason}
    if action == "settings.image.defaults":
        return default_keyword_rules()
    with ctx.prefs_lock:
        return _handle_prefs(ctx, action, payload)


def _prompt(ctx, action, payload):
    if action == "settings.prompt.status":
        return ctx.request("/prompt-evolution", timeout=10)
    if action == "settings.prompt.update":
        if payload.get("confirmed") is not True:
            raise ValueError("更新动态提示词需要确认 API 费用")
        result = ctx.request("/prompt-evolution/update", method="POST", payload={"confirmed": True}, timeout=10)
        return {**ctx.request("/prompt-evolution", timeout=10), "queued": bool(result.get("queued"))}
    if action == "settings.prompt.restore":
        ctx.request("/prompt-evolution/restore", method="POST", payload={"version_id": int(payload["id"])}, timeout=10)
        return ctx.request("/prompt-evolution", timeout=10)
    raise ValueError("未知动态提示词操作")


def _handle_prefs(ctx, action, payload):
    saved = _read(ctx)
    if action == "settings.get":
        ctx.reload_config()
        return _settings(ctx, saved)
    if action == "settings.save":
        return _save_settings(ctx, payload, saved)
    if action == "settings.theme":
        theme = payload["name"]
        if theme not in ("dark", "light"):
            raise ValueError("外观须为深色或浅色")
        _save(ctx, {"ui": {**saved.get("ui", {}), "theme": theme}})
        return {"theme": theme}
    if action == "settings.icon":
        return _save_icon(ctx, Path(payload["path"]), saved)
    if action in ("settings.profile.select", "settings.profile.save", "settings.profile.add", "settings.profile.delete"):
        return _profiles(ctx, action.rsplit(".", 1)[1], payload, saved)
    if action in ("settings.key.status", "settings.key.save", "settings.key.delete"):
        return _credential(ctx, action.rsplit(".", 1)[1], payload, saved)
    if action == "settings.api.import":
        return api_settings.validate(yaml_config.read(payload["path"]))
    if action == "settings.api.export":
        profile = api_settings.validate(payload["profile"])
        Path(payload["path"]).write_text(yaml_config.dumps(profile), encoding="utf-8", newline="\n")
        return {"saved": True}
    if action == "settings.image.save":
        profiles, _ = api_settings.catalog(ctx.base_cfg, saved)
        changes = _image_changes(ctx, payload, saved, profiles)
        _save(ctx, changes)
        return changes["image_analysis"]
    if action == "settings.image.rules":
        image = validate_analysis({**_image(ctx, saved), "keyword_rules": payload["rules"]})
        _save(ctx, {"image_analysis": image})
        return image["keyword_rules"]
    raise ValueError("未知设置操作")
