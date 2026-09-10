"""放量突破策略：放量突破最近 N 根 K 线高点产生买入信号。"""
from __future__ import annotations

from app.market_data.base import QuoteData
from app.strategies.base import Signal, Strategy
from app.time_utils import utc_now


class BreakoutStrategy(Strategy):
    """放量突破策略。

    条件：当日最高价突破最近 N 根（不含当日）K 线最高价，且成交量放大。
    """

    name = "breakout"
    description = "放量突破最近 20 根 K 线高点产生买入信号"

    def __init__(self, lookback: int = 20, volume_ratio: float = 1.5):
        self._lookback = lookback
        self._volume_ratio = volume_ratio

    def analyze(self, history: list[QuoteData]) -> Signal | None:
        if len(history) < self._lookback + 2:
            return None

        latest = history[-1]
        # 不含当日的前 lookback 根 K 线
        previous_bars = history[-(self._lookback + 1):-1]
        prior_high = max(q.high for q in previous_bars)

        # 突破：当日最高价 > 前 N 根最高价
        if latest.high <= prior_high:
            return None

        # 放量：当日成交量 > 前 N 根平均成交量 * 倍数
        avg_volume = sum(q.volume for q in previous_bars) / len(previous_bars)
        if avg_volume <= 0:
            return None
        if latest.volume < avg_volume * self._volume_ratio:
            return None

        source_time = latest.market_time or latest.received_at or utc_now()
        return self._build_signal(
            symbol=latest.symbol,
            direction="BUY",
            reason=(
                f"放量突破：最高价 {latest.high:.2f} 突破前 {self._lookback} 根高点 "
                f"{prior_high:.2f}，成交量放大 {latest.volume / avg_volume:.1f} 倍"
            ),
            price=latest.price,
            source_time=source_time,
        )
