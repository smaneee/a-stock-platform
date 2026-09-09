"""MACD 金叉死叉策略。"""
from __future__ import annotations

from datetime import datetime

from app.indicators.macd import macd
from app.market_data.base import QuoteData
from app.strategies.base import Signal, Strategy


class MacdCrossStrategy(Strategy):
    """MACD 金叉死叉策略。

    DIF 上穿 DEA 产生买入信号，DIF 下穿 DEA 产生卖出信号。
    """

    name = "macd_cross"
    description = "MACD 金叉产生买入信号，死叉产生卖出信号"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self._fast = fast
        self._slow = slow
        self._signal = signal

    def analyze(self, history: list[QuoteData]) -> Signal | None:
        if len(history) < self._slow + self._signal + 1:
            return None

        closes = [q.price for q in history]
        dif, dea, _ = macd(closes, self._fast, self._slow, self._signal)

        cur_dif = dif[-1]
        cur_dea = dea[-1]
        prev_dif = dif[-2]
        prev_dea = dea[-2]

        # 排除 NaN
        for v in (cur_dif, cur_dea, prev_dif, prev_dea):
            if v is None or v != v:
                return None

        latest = history[-1]
        source_time = latest.market_time or latest.received_at or datetime.utcnow()

        # 金叉：DIF 上穿 DEA
        if prev_dif <= prev_dea and cur_dif > cur_dea:
            return self._build_signal(
                symbol=latest.symbol,
                direction="BUY",
                reason=f"MACD 金叉：DIF({cur_dif:.4f}) 上穿 DEA({cur_dea:.4f})",
                price=latest.price,
                source_time=source_time,
            )
        # 死叉：DIF 下穿 DEA
        if prev_dif >= prev_dea and cur_dif < cur_dea:
            return self._build_signal(
                symbol=latest.symbol,
                direction="SELL",
                reason=f"MACD 死叉：DIF({cur_dif:.4f}) 下穿 DEA({cur_dea:.4f})",
                price=latest.price,
                source_time=source_time,
            )
        return None
