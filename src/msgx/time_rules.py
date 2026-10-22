"""消息统计、费用自然日和摘要窗口共用北京时间；外部接口的 UTC 计费另行处理。"""
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))


def beijing_day(ts: int | float) -> str:
    """持久化自然日不受克隆用户的系统时区影响。"""
    return datetime.fromtimestamp(ts, BEIJING).date().isoformat()
