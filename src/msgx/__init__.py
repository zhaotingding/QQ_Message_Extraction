"""消息萃取（msgx）。

版本号的唯一来源：pyproject.toml 通过 dynamic version 从这里读取，
发版时更新此处与 CHANGELOG.md；Git 标签按明确的发布请求创建。
"""

__version__ = "1.0.3"  # 包元数据、命令行和界面的唯一版本来源。
DISPLAY_VERSION = __version__  # 界面沿用同一版本，避免发布时漏改窗口标题。
