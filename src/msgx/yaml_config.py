"""应用配置共用的 YAML 解析与序列化；协议和缓存继续使用自己的格式。"""
from pathlib import Path

import yaml


class _Dumper(yaml.SafeDumper):
    pass


def _string(dumper, value):
    # 多行提示词保持可读；数字样式账号、正则和路径仍按字符串输出。
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_Dumper.add_representer(str, _string)


def loads(text: str) -> dict:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f"（第 {mark.line + 1} 行，第 {mark.column + 1} 列）" if mark else ""
        # 解析器原异常可能包含密钥所在的整行，反馈只保留错误位置。
        raise ValueError(f"YAML 配置格式无效{location}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("YAML 配置的顶层必须是键值映射")
    return data


def read(path: str | Path) -> dict:
    return loads(Path(path).read_text(encoding="utf-8-sig"))


def dumps(data: dict) -> str:
    return yaml.dump(data, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=100)
