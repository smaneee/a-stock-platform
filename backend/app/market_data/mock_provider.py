"""模拟行情数据源。

用于非交易时段的演示与测试，生成确定性的行情数据。
不依赖任何外部网络，永远可用。

历史数据生成规则：
- daily：每个交易日生成一根 K 线（含周末/节假日跳过逻辑）
- 分钟级周期（1m/5m/15m/30m/60m）：每分钟生成一根 K 线，只在连续竞价时段
  （9:30-11:30 / 13:00-15:00），午休不产生 K 线，分时曲线的形状与真实一致
- 缓存键包含 symbol+period+start_time+end_time，避免短区间缓存污染长区间请求
- OHLC 自洽：low <= open/close <= high
- amount = close * volume
- 同 seed 下重复生成产生相同序列
"""
from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, time, timedelta
from typing import Callable

from app.market_data.base import MarketDataProvider, QuoteData
from app.time_utils import utc_now


# A 股交易日简化判定：周一至周五，无视节假日
def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


# 分钟级周期命名与真实数据源（东财 / 通达信）保持一致
MINUTE_PERIODS = frozenset({"1m", "5m", "15m", "30m", "60m", "minute"})


def _is_session(when: time) -> bool:
    """连续竞价时段：9:30-11:30 / 13:00-15:00，午休不产生 K 线。"""
    return time(9, 30) <= when <= time(11, 30) or time(13, 0) <= when <= time(15, 0)


def _trading_days(start: date, end: date) -> list[date]:
    """返回 [start, end] 区间内所有「简化」交易日（剔除周末）。"""
    days: list[date] = []
    cur = start
    while cur <= end:
        if _is_weekday(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


class MockProvider(MarketDataProvider):
    """基于固定 seed 的随机游走生成模拟行情数据源。"""

    name = "mock"

    def __init__(self, seed: int | None = None):
        # 默认 seed 让 E2E 模式完全可复现
        self._base_seed = seed if seed is not None else 0x5EED
        self._rng = random.Random(self._base_seed)
        # 行情状态
        self._prices: dict[str, float] = {}
        self._volumes: dict[str, float] = {}
        self._amounts: dict[str, float] = {}
        self._names: dict[str, str] = {}
        # 历史缓存：key = (symbol, period, start_date, end_date)
        self._history: dict[tuple, list[QuoteData]] = {}
        self._subscribers: dict[str, list[Callable[[QuoteData], None]]] = {}

    def _seeded_rng(self, symbol: str) -> random.Random:
        """基于 base seed + symbol 派生独立 RNG，保证不同 symbol 互不污染。"""
        h = hashlib.sha256(f"{self._base_seed}:{symbol}".encode()).hexdigest()
        return random.Random(int(h[:16], 16))

    # ──────────────── 行情（实时） ────────────────

    async def get_quote(self, symbol: str) -> QuoteData | None:
        result = await self.get_quotes([symbol])
        return result.get(symbol)

    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        now = utc_now()
        result: dict[str, QuoteData] = {}
        for symbol in symbols:
            result[symbol] = self._generate_quote(symbol, now)
        return result

    async def subscribe(self, symbols: list[str], callback: Callable[[QuoteData], None]) -> None:
        for symbol in symbols:
            self._subscribers.setdefault(symbol, []).append(callback)

    async def health_check(self) -> bool:
        return True

    def _generate_quote(self, symbol: str, now: datetime) -> QuoteData:
        """生成一只股票的当前行情。"""
        rng = self._seeded_rng(symbol)
        previous = self._prices.get(symbol)
        if previous is None:
            previous = round(rng.uniform(10, 100), 2)
        change = rng.uniform(-0.03, 0.03)
        close = max(1.0, round(previous * (1 + change), 2))
        open_price = round(previous * (1 + rng.uniform(-0.005, 0.005)), 2)
        high = round(max(open_price, close) * (1 + rng.uniform(0, 0.005)), 2)
        low = round(min(open_price, close) * (1 - rng.uniform(0, 0.005)), 2)
        # 调整以保证 low <= open/close <= high
        high = max(high, open_price, close)
        low = min(low, open_price, close)
        volume_delta = float(rng.randint(1_000, 50_000))
        self._prices[symbol] = close
        self._volumes[symbol] = self._volumes.get(symbol, 0.0) + volume_delta

        return QuoteData(
            symbol=symbol,
            name=self._names.get(symbol, f"模拟股{symbol}"),
            price=close,
            open=open_price,
            high=high,
            low=low,
            previous_close=round(previous, 2),
            volume=volume_delta,
            amount=close * volume_delta,
            bid_price=round(close - 0.01, 2),
            ask_price=round(close + 0.01, 2),
            source=self.name,
            market_time=now,
            received_at=now,
            is_stale=False,
        )

    # ──────────────── 历史 ────────────────

    async def get_history(
        self,
        symbol: str,
        period: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[QuoteData]:
        # 缓存键必须包含起止时间，避免短区间缓存污染长区间请求
        start_d = start_time.date() if isinstance(start_time, datetime) else start_time
        end_d = end_time.date() if isinstance(end_time, datetime) else end_time
        key = (symbol, period, start_d, end_d)
        if key not in self._history:
            self._history[key] = self._generate_history(
                symbol, period, start_time, end_time
            )
        return self._history[key]

    def _generate_history(
        self,
        symbol: str,
        period: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[QuoteData]:
        """生成历史 K 线数据。"""
        if period in MINUTE_PERIODS:
            return self._generate_minute_history(symbol, start_time, end_time)
        # daily 与其它日线级别周期（周/月/未知）都按日线生成
        return self._generate_daily_history(symbol, start_time, end_time)

    def _generate_daily_history(
        self, symbol: str, start_time: datetime, end_time: datetime
    ) -> list[QuoteData]:
        """按交易日每天生成一根 K 线。"""
        rng = self._seeded_rng(symbol)
        base_price = round(rng.uniform(10, 100), 2)
        start_d = start_time.date() if isinstance(start_time, datetime) else start_time
        end_d = end_time.date() if isinstance(end_time, datetime) else end_time

        bars: list[QuoteData] = []
        seen_dates: set[date] = set()
        for d in _trading_days(start_d, end_d):
            if d in seen_dates:
                continue
            seen_dates.add(d)
            change = rng.uniform(-0.02, 0.02)
            close = round(base_price * (1 + change), 2)
            base_price = close
            open_p = round(close * (1 + rng.uniform(-0.005, 0.005)), 2)
            high = round(close * (1 + rng.uniform(0, 0.01)), 2)
            low = round(close * (1 - rng.uniform(0, 0.01)), 2)
            # 强制 OHLC 自洽
            high = max(high, open_p, close)
            low = min(low, open_p, close)
            volume = float(rng.randint(100_000, 5_000_000))
            market_dt = datetime.combine(d, time(15, 0))
            bars.append(
                QuoteData(
                    symbol=symbol,
                    name=self._names.get(symbol, f"模拟股{symbol}"),
                    price=close,
                    open=open_p,
                    high=high,
                    low=low,
                    previous_close=round(close / (1 + change), 2) if change else close,
                    volume=volume,
                    amount=close * volume,
                    bid_price=round(close - 0.01, 2),
                    ask_price=round(close + 0.01, 2),
                    source=self.name,
                    market_time=market_dt,
                    received_at=market_dt,
                    is_stale=False,
                )
            )
        return bars

    def _generate_minute_history(
        self, symbol: str, start_time: datetime, end_time: datetime
    ) -> list[QuoteData]:
        """按交易日内的分钟生成 K 线。"""
        rng = self._seeded_rng(symbol)
        base_price = round(rng.uniform(10, 100), 2)
        bars: list[QuoteData] = []
        cur = start_time
        seen_dt: set[datetime] = set()
        while cur <= end_time:
            if _is_weekday(cur.date()) and _is_session(cur.time()):
                if cur not in seen_dt:
                    seen_dt.add(cur)
                    change = rng.uniform(-0.005, 0.005)
                    close = round(base_price * (1 + change), 2)
                    base_price = close
                    open_p = round(close * (1 + rng.uniform(-0.002, 0.002)), 2)
                    high = round(close * (1 + rng.uniform(0, 0.003)), 2)
                    low = round(close * (1 - rng.uniform(0, 0.003)), 2)
                    high = max(high, open_p, close)
                    low = min(low, open_p, close)
                    volume = float(rng.randint(10_000, 200_000))
                    bars.append(
                        QuoteData(
                            symbol=symbol,
                            name=self._names.get(symbol, f"模拟股{symbol}"),
                            price=close,
                            open=open_p,
                            high=high,
                            low=low,
                            previous_close=round(close / (1 + change), 2) if change else close,
                            volume=volume,
                            amount=close * volume,
                            bid_price=round(close - 0.01, 2),
                            ask_price=round(close + 0.01, 2),
                            source=self.name,
                            market_time=cur,
                            received_at=cur,
                            is_stale=False,
                        )
                    )
            cur += timedelta(minutes=1)
        return bars
