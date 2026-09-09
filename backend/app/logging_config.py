"""日志配置模块。

统一配置控制台与文件日志，并包含敏感信息脱敏过滤器，
确保 Token、Cookie、账号、密钥等信息不写入日志。
"""
import logging
import re
import sys
from logging.handlers import RotatingFileHandler

# 敏感字段模式：key=value 或 "key": "value" 形式
_SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|cookie|password|passwd|authorization)\s*[=:]\s*\S+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
]


class _SensitiveFilter(logging.Filter):
    """日志脱敏过滤器，将敏感值替换为 ***。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for pattern in _SENSITIVE_PATTERNS:
                record.msg = pattern.sub(lambda m: _mask(m.group(0)), record.msg)
        return True


def _mask(text: str) -> str:
    """对匹配到的敏感文本进行脱敏，保留前缀。"""
    if "=" in text:
        key, _, _ = text.partition("=")
        return f"{key}=***"
    if ":" in text:
        key, _, _ = text.partition(":")
        return f"{key}: ***"
    return "***"


def setup_logging(level: str = "INFO") -> None:
    """初始化日志系统，避免重复添加 handler。"""
    root = logging.getLogger()
    if root.handlers:
        return

    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台 handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(_SensitiveFilter())
    root.addHandler(console)

    # 文件 handler（带轮转）
    file_handler = RotatingFileHandler(
        "app.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_SensitiveFilter())
    root.addHandler(file_handler)
