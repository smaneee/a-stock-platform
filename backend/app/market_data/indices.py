"""基准指数代码表。

组合回测的 benchmark 通常是指数而不是个股，但指数代码和个股代码会撞车：
``000001`` 既可以是上证指数（沪市指数）也可以是平安银行（深市股票），
``000300`` 既可以是沪深300 又落在深市个股号段里。所以本项目统一用
**带交易所前缀**的形式表达指数：``sh000300`` / ``sz399001``。

- 前缀 ``sh`` / ``sz`` 与通达信、新浪、AKShare 的指数代码习惯一致；
- 只有 ``INDEX_META`` 里登记过的代码才被当作指数，``sz000001`` 这类会被
  误读成平安银行的写法不会被吞掉；
- 通达信指数 K 线必须走 ``get_index_bars``：个股的 ``get_security_bars``
  请求 (1, "000300") 会返回字段错位的乱码（实测 datetime 变成
  ``120812-41-14``），两者在协议层是两个不同接口。
"""
from __future__ import annotations

from dataclasses import dataclass

# 通达信市场号，取值与 tdx_provider.MARKET_SH / MARKET_SZ 一致
TDX_MARKET_SZ = 0
TDX_MARKET_SH = 1


@dataclass(frozen=True)
class IndexMeta:
    """指数元数据。"""

    symbol: str  # 规范代码，如 sh000300
    tdx_market: int  # 通达信市场号
    tdx_code: str  # 通达信 6 位代码
    name: str  # 中文简称


_INDEX_DEFS: tuple[tuple[str, int, str], ...] = (
    ("sh000001", TDX_MARKET_SH, "上证指数"),
    ("sh000016", TDX_MARKET_SH, "上证50"),
    ("sh000300", TDX_MARKET_SH, "沪深300"),
    ("sh000688", TDX_MARKET_SH, "科创50"),
    ("sh000905", TDX_MARKET_SH, "中证500"),
    ("sz399001", TDX_MARKET_SZ, "深证成指"),
    ("sz399005", TDX_MARKET_SZ, "中小100"),
    ("sz399006", TDX_MARKET_SZ, "创业板指"),
)

INDEX_META: dict[str, IndexMeta] = {
    symbol: IndexMeta(
        symbol=symbol, tdx_market=market, tdx_code=symbol[2:], name=name
    )
    for symbol, market, name in _INDEX_DEFS
}

# 组合回测默认基准顺序（保持插入顺序，供前端下拉使用）
DEFAULT_BENCHMARKS: tuple[str, ...] = (
    "sh000300",
    "sh000001",
    "sz399001",
    "sz399006",
)


def normalize_index_symbol(symbol: str | None) -> str:
    """把指数代码规范成小写去空格形式；非字符串返回空串。"""
    return (symbol or "").strip().lower()


def is_index_symbol(symbol: str | None) -> bool:
    """判断是否为本项目登记过的基准指数代码。"""
    return normalize_index_symbol(symbol) in INDEX_META


def resolve_index(symbol: str | None) -> IndexMeta | None:
    """解析指数代码；未登记时返回 None。"""
    return INDEX_META.get(normalize_index_symbol(symbol))


def index_name(symbol: str | None) -> str:
    """指数中文简称；未登记时返回空串。"""
    meta = resolve_index(symbol)
    return meta.name if meta else ""


def list_indices() -> list[dict]:
    """返回全部登记指数（供只读接口 / 前端下拉使用）。"""
    return [
        {"symbol": meta.symbol, "name": meta.name, "code": meta.tdx_code}
        for meta in INDEX_META.values()
    ]
