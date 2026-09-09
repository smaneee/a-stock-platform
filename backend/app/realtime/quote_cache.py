"""行情内存缓存。

维护每只股票的最新行情与固定长度的滚动窗口（默认最近 300 根分钟线），
供指标增量计算使用，避免每次行情更新都重新计算全部历史数据。
"""
from __future__ import annotations

import threading
from collections import OrderedDict

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
        self._windows: dict[str, OrderedDict] = {}

    def update(self, quote: QuoteData) -> None:
        """更新单条行情。"""
        with self._lock:
            self._latest[quote.symbol] = quote
            window = self._windows.setdefault(quote.symbol, OrderedDict())
            key = quote.market_time or quote.received_at
            window[key] = quote
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
