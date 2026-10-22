"""消息萃取（msgx）。

版本号的唯一来源：pyproject.toml 通过 dynamic version 从这里读取，
发版时只需改这一处，再更新 CHANGELOG.md 并打对应标签。
"""

__version__ = "0.9.9"  # 包元数据、命令行和界面的唯一版本来源。
DISPLAY_VERSION = __version__  # 界面沿用同一版本，避免发布时漏改窗口标题。
