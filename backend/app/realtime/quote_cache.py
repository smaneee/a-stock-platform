"""行情内存缓存。

维护每只股票的最新行情与固定长度的滚动窗口（默认最近 300 根分钟线），
供指标增量计算使用，避免每次行情更新都重新计算全部历史数据。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime

from app.config import get_settings
from app.market_data.base import QuoteData

settings = get_settings()


class QuoteCache:
    """线程安全的行情缓存。"""

    def __init__(self, window_size: int | None = None):
        self._window_size = window_size or settings.rolling_window_size
        self._lock = threading.RLock()
        # 最新行情：symbol -> QuoteData
        self._latest: dict[str, QuoteData] = {}
        # 滚动窗口：symbol -> OrderedDict[market_time, QuoteData]
        self._windows: dict[str, OrderedDict[datetime, QuoteData]] = {}
        # 实时源通常返回当日累计量，用它换算每次轮询的增量。
        self._cumulative: dict[str, tuple[object, str, float, float]] = {}

    def update(self, quote: QuoteData) -> None:
        """更新最新快照，并按自然分钟聚合为 K 线。"""
        with self._lock:
            self._latest[quote.symbol] = quote
            window = self._windows.setdefault(quote.symbol, OrderedDict())
            timestamp = quote.market_time or quote.received_at
            key = timestamp.replace(second=0, microsecond=0)

            previous_cumulative = self._cumulative.get(quote.symbol)
            current_marker = (timestamp.date(), quote.source)
            volume_delta = 0.0
            amount_delta = 0.0
            if previous_cumulative and previous_cumulative[:2] == current_marker:
                volume_delta = max(0.0, quote.volume - previous_cumulative[2])
                amount_delta = max(0.0, quote.amount - previous_cumulative[3])
            self._cumulative[quote.symbol] = (
                current_marker[0],
                current_marker[1],
                quote.volume,
                quote.amount,
            )

            current_bar = window.get(key)
            if current_bar is None:
                previous_close = next(reversed(window.values())).price if window else quote.previous_close
                window[key] = quote.model_copy(
                    update={
                        "open": quote.price,
                        "high": quote.price,
                        "low": quote.price,
                        "previous_close": previous_close,
                        "volume": volume_delta,
                        "amount": amount_delta,
                        "market_time": key,
                    }
                )
            else:
                window[key] = current_bar.model_copy(
                    update={
                        "price": quote.price,
                        "high": max(current_bar.high, quote.price),
                        "low": min(current_bar.low, quote.price),
                        "volume": current_bar.volume + volume_delta,
                        "amount": current_bar.amount + amount_delta,
                        "bid_price": quote.bid_price,
                        "ask_price": quote.ask_price,
                        "received_at": quote.received_at,
                        "is_stale": quote.is_stale,
                    }
                )
            # 修剪滚动窗口
            while len(window) > self._window_size:
                window.popitem(last=False)

    def update_many(self, quotes: dict[str, QuoteData]) -> None:
        """批量更新行情。"""
        for quote in quotes.values():
            if quote is not None:
                self.update(quote)

    def get_latest(self, symbol: str) -> QuoteData | None:
        with self._lock:
            return self._latest.get(symbol)

    def get_latest_many(self, symbols: list[str]) -> dict[str, QuoteData]:
        with self._lock:
            return {s: self._latest[s] for s in symbols if s in self._latest}

    def get_window(self, symbol: str) -> list[QuoteData]:
        """返回某只股票的滚动窗口数据（时间升序）。"""
        with self._lock:
            window = self._windows.get(symbol)
            if not window:
                return []
            return list(window.values())

    def window_size(self, symbol: str) -> int:
        with self._lock:
            return len(self._windows.get(symbol, {}))

    def clear(self) -> None:
        with self._lock:
            self._latest.clear()
            self._windows.clear()
            self._cumulative.clear()
