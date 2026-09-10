"""RSI 超买超卖预警策略。"""
from __future__ import annotations

from app.indicators.rsi import rsi
from app.market_data.base import QuoteData
from app.strategies.base import Signal, Strategy
from app.time_utils import utc_now


class RsiReversalStrategy(Strategy):
    """RSI 超买超卖预警策略。

    RSI 下穿超买阈值产生卖出预警，上穿超卖阈值产生买入预警。
    """

    name = "rsi_reversal"
    description = "RSI 超买(>70)产生卖出预警，超卖(<30)产生买入预警"

    def __init__(self, period: int = 14, overbought: float = 70.0, oversold: float = 30.0):
        self._period = period
        self._overbought = overbought
        self._oversold = oversold

    def analyze(self, history: list[QuoteData]) -> Signal | None:
        if len(history) < self._period + 1:
            return None

        closes = [q.price for q in history]
        rsi_values = rsi(closes, self._period)

        cur = rsi_values[-1]
        prev = rsi_values[-2]
        if cur is None or cur != cur or prev is None or prev != prev:
            return None

        latest = history[-1]
        source_time = latest.market_time or latest.received_at or utc_now()

        # 超买：RSI 从上方下穿超买线
        if prev >= self._overbought > cur:
            return self._build_signal(
                symbol=latest.symbol,
                direction="SELL",
                reason=f"RSI({cur:.1f}) 下穿超买线 {self._overbought}，警惕回调",
                price=latest.price,
                source_time=source_time,
                strength=0.8,
            )
        # 超卖：RSI 从下方上穿超卖线
        if prev <= self._oversold < cur:
            return self._build_signal(
                symbol=latest.symbol,
                direction="BUY",
                reason=f"RSI({cur:.1f}) 上穿超卖线 {self._oversold}，关注反弹",
                price=latest.price,
                source_time=source_time,
                strength=0.8,
            )
        return None
