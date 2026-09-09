"""输入校验工具。"""
from __future__ import annotations

import re

# A 股代码：6 位数字，沪市 60/68、深市 00/30、北交所 8x/4x/92
_SYMBOL_PATTERN = re.compile(r"^(60|68|00|30|8\d|4\d|92)\d{4}$")


def validate_symbol(symbol: str) -> bool:
    """校验 A 股股票代码格式。"""
    if not symbol:
        return False
    return bool(_SYMBOL_PATTERN.match(symbol.strip()))


def sanitize_symbols(symbols: list[str]) -> list[str]:
    """过滤出合法的股票代码。"""
    return [s.strip() for s in symbols if validate_symbol(s)]
