"""成交参考价（盘口缺失兜底）测试。

东方财富批量行情接口与 AKShare 都不提供五档盘口，此时必须退化为最新价，
否则模拟盘/实盘调仓会把可买数量算成 0（回归防护）。
"""
import pytest

from app.live_trading.rebalance import LiveRebalanceError, LiveRebalanceService
from app.market_data.base import QuoteData
from app.paper_trading.rebalance import PaperRebalanceService


def _quote(**overrides) -> QuoteData:
    payload = {
        "symbol": "600519",
        "name": "贵州茅台",
        "price": 1275.16,
        "open": 1285.15,
        "high": 1286.15,
        "low": 1263.01,
        "previous_close": 1285.13,
        "bid_price": 1275.10,
        "ask_price": 1275.20,
    }
    payload.update(overrides)
    return QuoteData(**payload)


def test_execution_price_prefers_order_book():
    quote = _quote()
    assert quote.execution_price("SELL") == 1275.10
    assert quote.execution_price("BUY") == 1275.20
    assert quote.execution_price("buy") == 1275.20


def test_execution_price_falls_back_to_last_price():
    """东财/AKShare 场景：没有五档时按最新价成交参考。"""
    quote = _quote(bid_price=0.0, ask_price=0.0)
    assert quote.execution_price("SELL") == 1275.16
    assert quote.execution_price("BUY") == 1275.16


def test_execution_price_handles_nan_and_missing_price():
    quote = _quote(bid_price=float("nan"), ask_price=-1.0)
    assert quote.execution_price("BUY") == 1275.16
    empty = _quote(price=0.0, bid_price=0.0, ask_price=0.0)
    assert empty.execution_price("BUY") == 0.0


def test_paper_rebalance_order_uses_last_price_without_order_book():
    """模拟盘调仓：东财行情没有盘口时，参考价必须落到最新价（否则可买数量为 0）。"""
    quote = _quote(bid_price=0.0, ask_price=0.0)
    order = PaperRebalanceService._order("BUY", "600519", 100, quote)
    assert order["indicative_price"] == 1275.16
    assert order["indicative_value"] == round(1275.16 * 100, 2)


def test_live_rebalance_accepts_quote_without_order_book():
    """实盘调仓：盘口为 0 但最新价有效时不应再报「缺少有效实时盘口」。"""
    quote = _quote(bid_price=0.0, ask_price=0.0)
    LiveRebalanceService._validate_quotes(["600519"], {"600519": quote})


def test_live_rebalance_rejects_quote_without_any_price():
    quote = _quote(price=0.0, bid_price=0.0, ask_price=0.0)
    with pytest.raises(LiveRebalanceError):
        LiveRebalanceService._validate_quotes(["600519"], {"600519": quote})
