"""基准指数登记表与基准代码校验测试（纯本地，不联网）。"""
from __future__ import annotations

from app.market_data.indices import (
    DEFAULT_BENCHMARKS,
    INDEX_META,
    index_name,
    is_index_symbol,
    list_indices,
    normalize_index_symbol,
    resolve_index,
)
from app.validation import validate_benchmark_symbol, validate_symbol


def test_registry_entries_are_consistent():
    for symbol, meta in INDEX_META.items():
        assert meta.symbol == symbol
        # 规范代码必须是 交易所前缀 + 6 位数字
        assert symbol[:2] in ("sh", "sz")
        assert symbol[2:].isdigit() and len(symbol[2:]) == 6
        assert meta.tdx_code == symbol[2:]
        assert meta.tdx_market in (0, 1)
        assert meta.name


def test_default_benchmarks_exist_in_registry():
    assert DEFAULT_BENCHMARKS
    for symbol in DEFAULT_BENCHMARKS:
        assert symbol in INDEX_META


def test_resolve_index_accepts_case_and_whitespace():
    meta = resolve_index("  SH000300 ")
    assert meta is not None
    assert meta.symbol == "sh000300"
    assert meta.tdx_market == 1
    assert meta.tdx_code == "000300"


def test_resolve_index_rejects_unknown_and_bare_codes():
    assert resolve_index("sh999999") is None
    assert resolve_index("000300") is None
    assert resolve_index("") is None
    assert resolve_index(None) is None
    assert resolve_index("600519") is None


def test_is_index_symbol_and_name():
    assert is_index_symbol("sz399006") is True
    assert is_index_symbol("399006") is False
    assert index_name("sz399006") == "创业板指"
    assert index_name("600519") == ""


def test_normalize_index_symbol_handles_none():
    assert normalize_index_symbol(None) == ""
    assert normalize_index_symbol(" Sh000001 ") == "sh000001"


def test_list_indices_shape():
    items = list_indices()
    assert len(items) == len(INDEX_META)
    assert {"symbol", "name", "code"} <= set(items[0])


def test_validate_benchmark_symbol_accepts_stock_and_index():
    assert validate_benchmark_symbol("600519") is True
    assert validate_benchmark_symbol("sh000300") is True
    assert validate_benchmark_symbol("SZ399001") is True


def test_validate_benchmark_symbol_rejects_bare_index_codes():
    # 深市指数号段（399xxx）不是合法个股代码，必须显式带 sz 前缀才放行
    assert validate_benchmark_symbol("399006") is False
    assert validate_benchmark_symbol("399001") is False
    # 裸 000300 只按「深市个股代码格式」被放行，不会被当成指数
    assert validate_benchmark_symbol("000300") is True
    assert resolve_index("000300") is None
    assert validate_benchmark_symbol("") is False
    assert validate_benchmark_symbol("sh00030") is False
    assert validate_benchmark_symbol("us000300") is False


def test_validate_benchmark_symbol_matches_validate_symbol_for_stocks():
    for symbol in ("600519", "000001", "300750", "688981", "830799"):
        assert validate_benchmark_symbol(symbol) == validate_symbol(symbol) is True
