"""均线交叉策略：MA5 上穿 MA20（金叉）产生买入信号。"""
from __future__ import annotations

from app.indicators.moving_average import latest_valid, ma
from app.market_data.base import QuoteData
from app.strategies.base import Signal, Strategy
from app.time_utils import utc_now


class MaCrossStrategy(Strategy):
    """MA5 上穿 MA20 策略。"""

    name = "ma_cross"
    description = "MA5 上穿 MA20 产生买入信号，MA5 下穿 MA20 产生卖出信号"

    def analyze(self, history: list[QuoteData]) -> Signal | None:
        if len(history) < 21:
            return None

        closes = [q.price for q in history]
        ma5 = ma(closes, 5)
        ma20 = ma(closes, 20)

        # 最新值与前一值
        cur_ma5 = latest_valid(ma5)
        cur_ma20 = latest_valid(ma20)
        if cur_ma5 is None or cur_ma20 is None:
            return None

        prev_ma5 = ma5[-2] if len(ma5) >= 2 else None
        prev_ma20 = ma20[-2] if len(ma20) >= 2 else None
        if prev_ma5 is None or prev_ma20 is None:
            return None

        latest = history[-1]
        source_time = latest.market_time or latest.received_at or utc_now()

        # 金叉：前一日 MA5 <= MA20，当日 MA5 > MA20
        if prev_ma5 <= prev_ma20 and cur_ma5 > cur_ma20:
            return self._build_signal(
                symbol=latest.symbol,
                direction="BUY",
                reason=f"MA5({cur_ma5:.2f}) 上穿 MA20({cur_ma20:.2f})，形成金叉",
                price=latest.price,
                source_time=source_time,
            )

        # 死叉：前一日 MA5 >= MA20，当日 MA5 < MA20
        if prev_ma5 >= prev_ma20 and cur_ma5 < cur_ma20:
            return self._build_signal(
                symbol=latest.symbol,
                direction="SELL",
                reason=f"MA5({cur_ma5:.2f}) 下穿 MA20({cur_ma20:.2f})，形成死叉",
                price=latest.price,
                source_time=source_time,
            )

        return None
