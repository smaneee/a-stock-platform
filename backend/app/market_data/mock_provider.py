"""模拟行情数据源。

用于非交易时段的演示与测试，生成随机游走的行情数据。
不依赖任何外部网络，永远可用。
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import Callable

from app.market_data.base import MarketDataProvider, QuoteData
from app.time_utils import utc_now


class MockProvider(MarketDataProvider):
    """基于随机游走生成模拟行情的数据源。"""

    name = "mock"

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)
        # 每只股票维护一个当前价格，用于产生连续变化
        self._prices: dict[str, float] = {}
        self._volumes: dict[str, float] = {}
        self._amounts: dict[str, float] = {}
        self._names: dict[str, str] = {}
        self._history: dict[str, list[QuoteData]] = {}
        self._subscribers: dict[str, list[Callable[[QuoteData], None]]] = {}

    async def get_quote(self, symbol: str) -> QuoteData | None:
        result = await self.get_quotes([symbol])
        return result.get(symbol)

    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        now = utc_now()
        result: dict[str, QuoteData] = {}
        for symbol in symbols:
            result[symbol] = self._generate_quote(symbol, now)
        return result

    async def get_history(
        self,
        symbol: str,
        period: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[QuoteData]:
        # 缓存历史数据，避免每次重新生成导致不一致
        key = f"{symbol}:{period}"
        if key not in self._history:
            self._history[key] = self._generate_history(symbol, period, start_time, end_time)
        return self._history[key]

    async def subscribe(self, symbols: list[str], callback: Callable[[QuoteData], None]) -> None:
        for symbol in symbols:
            self._subscribers.setdefault(symbol, []).append(callback)

    async def health_check(self) -> bool:
        return True

    def _generate_quote(self, symbol: str, now: datetime) -> QuoteData:
        """生成一只股票的当前行情，价格基于上一次做随机游走。"""
        previous = self._prices.get(symbol)
        if previous is None:
            previous = round(self._rng.uniform(10, 100), 2)
        change = self._rng.uniform(-0.03, 0.03)
        price = max(1.0, round(previous * (1 + change), 2))
        self._prices[symbol] = price

        prev_close = price / (1 + change)
        open_price = round(prev_close * (1 + self._rng.uniform(-0.01, 0.01)), 2)
        high = round(max(open_price, price) * (1 + self._rng.uniform(0, 0.01)), 2)
        low = round(min(open_price, price) * (1 - self._rng.uniform(0, 0.01)), 2)
        volume_delta = float(self._rng.randint(1_000, 50_000))
        self._volumes[symbol] = self._volumes.get(symbol, 0.0) + volume_delta
        self._amounts[symbol] = self._amounts.get(symbol, 0.0) + price * volume_delta

        return QuoteData(
            symbol=symbol,
            name=self._names.get(symbol, f"模拟股{symbol}"),
            price=price,
            open=open_price,
            high=high,
            low=low,
            previous_close=round(prev_close, 2),
            volume=self._volumes[symbol],
            amount=self._amounts[symbol],
            bid_price=round(price - 0.01, 2),
            ask_price=round(price + 0.01, 2),
            source=self.name,
            market_time=now,
            received_at=now,
            is_stale=False,
        )

    def _generate_history(
        self,
        symbol: str,
        period: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[QuoteData]:
        """生成历史 K 线数据。"""
        data: list[QuoteData] = []
        # 默认分钟线，简化处理
        current = start_time
        base_price = round(self._rng.uniform(10, 100), 2)
        while current <= end_time:
            change = self._rng.uniform(-0.02, 0.02)
            price = round(base_price * (1 + change), 2)
            base_price = price
            quote = QuoteData(
                symbol=symbol,
                name=self._names.get(symbol, f"模拟股{symbol}"),
                price=price,
                open=round(price * (1 + self._rng.uniform(-0.005, 0.005)), 2),
                high=round(price * (1 + self._rng.uniform(0, 0.01)), 2),
                low=round(price * (1 - self._rng.uniform(0, 0.01)), 2),
                previous_close=round(price / (1 + change), 2),
                volume=float(self._rng.randint(100_000, 5_000_000)),
                amount=float(price * self._rng.randint(100_000, 5_000_000)),
                bid_price=round(price - 0.01, 2),
                ask_price=round(price + 0.01, 2),
                source=self.name,
                market_time=current,
                received_at=current,
                is_stale=False,
            )
            data.append(quote)
            current += timedelta(minutes=1)
        return data
