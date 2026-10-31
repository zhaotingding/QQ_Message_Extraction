"""窗口与轻量启动器共用的图标选择规则。"""
from __future__ import annotations

from pathlib import Path

from . import prefs, yaml_config

DEFAULT_ICON = Path(__file__).resolve().parents[2] / "assets" / "app-icon.ico"  # 公共资源属于源码，不随个人资料根迁移。


def selected_icon(root: Path, saved: dict | None = None, preferences_file: Path | None = None) -> Path | None:
    file = preferences_file or prefs.prefs_path(root)  # GUI 可明确传入当前偏好位置。
    if saved is None:
        saved = yaml_config.read(file) if file.exists() else {}  # 构建入口读取相同的已保存个人选择。
    raw = (saved.get("ui") or {}).get("icon_path")
    if raw:
        custom = Path(raw).expanduser()  # 允许个人保留自己的绝对路径或仓库相对路径。
        custom = custom if custom.is_absolute() else root / custom
        if custom.is_file():
            return custom
    local = file.parent / "app-icon.ico"  # 导入图标的本机副本优先于项目默认图标。
    if local.is_file():
        return local
    return DEFAULT_ICON if DEFAULT_ICON.is_file() else None  # 干净克隆也能使用已入库的公共图标。
