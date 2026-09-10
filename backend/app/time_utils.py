"""统一的时间函数。

数据库当前使用跨 SQLite/PostgreSQL 的无时区 DateTime，因此统一保存 UTC naive 值；
对外比较时再显式附加 UTC，避免使用已弃用的 datetime.utcnow()。
"""
from datetime import UTC, datetime


def utc_now() -> datetime:
    """返回表示 UTC 的无时区 datetime，兼容现有数据库字段。"""
    return datetime.now(UTC).replace(tzinfo=None)
