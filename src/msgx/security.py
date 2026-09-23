"""日志脱敏作为最后一道保护；请求层仍禁止记录原始响应和鉴权头。"""
import logging
import re

_secrets: set[str] = set()


def register(secret: str) -> None:
    if secret:
        _secrets.add(secret)


def redact(text: str) -> str:
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, "[已隐藏密钥]")
    text = re.sub(r"(?i)Bearer\s+[^\s\"',}]+", "Bearer [已隐藏密钥]", text)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[已隐藏密钥]", text)


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True
