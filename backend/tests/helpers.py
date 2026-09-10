"""测试辅助函数。"""
from __future__ import annotations

from datetime import timedelta

from app.market_data.base import QuoteData
from app.time_utils import utc_now


def make_quote(
    symbol: str = "600000",
    price: float = 10.0,
    open: float = 9.9,
    high: float = 10.2,
    low: float = 9.8,
    previous_close: float = 9.95,
    volume: float = 1_000_000.0,
    amount: float = 10_000_000.0,
    market_time: datetime | None = None,
    is_stale: bool = False,
) -> QuoteData:
    """构造行情数据。"""
    return QuoteData(
        symbol=symbol,
        name=f"测试股{symbol}",
        price=price,
        open=open,
        high=high,
        low=low,
        previous_close=previous_close,
        volume=volume,
        amount=amount,
        bid_price=price - 0.01,
        ask_price=price + 0.01,
        source="mock",
        market_time=market_time or utc_now(),
        received_at=utc_now(),
        is_stale=is_stale,
    )


def make_history(
    symbol: str = "600000",
    n: int = 100,
    base_price: float = 10.0,
    start=None,
) -> list[QuoteData]:
    """生成递增的历史行情序列。"""
    if start is None:
        start = utc_now() - timedelta(days=n)
    history = []
    for i in range(n):
        price = base_price + i * 0.05
        history.append(
            make_quote(
                symbol=symbol,
                price=price,
                open=price - 0.02,
                high=price + 0.1,
                low=price - 0.1,
                previous_close=price - 0.05,
                volume=1_000_000.0 + i * 1000,
                market_time=start + timedelta(minutes=i),
            )
        )
    return history
