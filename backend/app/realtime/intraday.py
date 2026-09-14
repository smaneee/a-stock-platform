"""当日分时序列（实时曲线）。

两路数据按分钟合并，实时优先：

1. ``QuoteCache`` 的滚动窗口 —— 只覆盖自选股，随轮询（默认 3 秒）刷新；
   当前分钟的 K 线会被原地更新，所以曲线是真的在动；
2. 数据源的 1 分钟分时（东财 trends2，一次返回近 5 个交易日）—— 任意标的
   都能拿到当天完整曲线，用来补上程序启动前已经走完的时段。

口径与 ``QuoteData`` 一致：成交量单位「股」、成交额单位「元」、时间为交易所
本地时间。前端用「累计成交额 ÷ 累计成交量」就能得到分时均价，本模块不重复
计算均价。

本模块只读：不写数据库，不参与信号与策略计算。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from app.market_data.base import QuoteData
from app.market_data.provider_manager import ProviderManager
from app.realtime.quote_cache import QuoteCache

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 600
DEFAULT_TTL_SECONDS = 60.0
# 东财分时一次返回近 5 个交易日，窗口取宽一点保证「上一交易日」一定在内，
# 昨收才有得推断。
FETCH_LOOKBACK_DAYS = 7
FETCH_LOOKAHEAD_DAYS = 1


@dataclass(frozen=True)
class IntradayPoint:
    """分时曲线上的一个点（同一分钟只保留一个）。"""

    time: datetime
    price: float
    volume: float
    amount: float

    def to_dict(self) -> dict:
        return {
            "time": self.time.isoformat(),
            "price": round(self.price, 4),
            "volume": self.volume,
            "amount": self.amount,
        }


@dataclass(frozen=True)
class IntradaySeries:
    """某个标的、某个交易日的分时序列。"""

    symbol: str
    name: str
    trade_date: date
    previous_close: float
    points: list[IntradayPoint]
    source: str
    is_live: bool

    @property
    def last_price(self) -> float:
        return self.points[-1].price if self.points else 0.0

    @property
    def change(self) -> float:
        """相对昨收的涨跌额；昨收缺失时为 0。"""
        if self.previous_close <= 0 or not self.points:
            return 0.0
        return self.last_price - self.previous_close

    @property
    def change_pct(self) -> float:
        """相对昨收的涨跌幅（%）；昨收缺失时为 0。"""
        if self.previous_close <= 0 or not self.points:
            return 0.0
        return self.change / self.previous_close * 100.0

    def stats(self) -> dict:
        """当日统计：开高低收、涨跌幅、累计量额。"""
        prices = [point.price for point in self.points]
        return {
            "open": prices[0] if prices else 0.0,
            "high": max(prices) if prices else 0.0,
            "low": min(prices) if prices else 0.0,
            "last": self.last_price,
            "previous_close": self.previous_close,
            "change": self.change,
            "change_pct": self.change_pct,
            "volume": sum(point.volume for point in self.points),
            "amount": sum(point.amount for point in self.points),
        }

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "trade_date": self.trade_date.isoformat(),
            "source": self.source,
            "is_live": self.is_live,
            "stats": self.stats(),
            "points": [point.to_dict() for point in self.points],
        }


def _minute_of(when: datetime) -> datetime:
    """截断到分钟：同一分钟的多个快照只保留最后一个。"""
    return when.replace(second=0, microsecond=0)


def bars_to_points(bars: Iterable[QuoteData]) -> list[IntradayPoint]:
    """把行情 bar 按分钟归并成分时点，时间升序。

    实时轮询默认 3 秒一次，一分钟内会有多根快照；不归并的话曲线会带锯齿，
    也会与 ``QuoteCache`` 的分钟聚合口径对不上。
    """
    by_minute: dict[datetime, IntradayPoint] = {}
    for bar in bars:
        when = bar.market_time or bar.received_at
        if when is None:
            continue
        key = _minute_of(when)
        by_minute[key] = IntradayPoint(
            time=key,
            price=float(bar.price),
            volume=float(bar.volume or 0.0),
            amount=float(bar.amount or 0.0),
        )
    return [by_minute[key] for key in sorted(by_minute)]


def merge_points(
    baseline: Iterable[IntradayPoint], live: Iterable[IntradayPoint]
) -> list[IntradayPoint]:
    """按分钟合并两条序列，同一分钟以 live 为准。"""
    merged: dict[datetime, IntradayPoint] = {
        point.time: point for point in baseline
    }
    merged.update({point.time: point for point in live})
    return [merged[key] for key in sorted(merged)]


def points_on(
    points: Iterable[IntradayPoint], trade_date: date
) -> list[IntradayPoint]:
    """只保留指定交易日的点（数据源一次会返回近 5 个交易日）。"""
    return [point for point in points if point.time.date() == trade_date]


def infer_previous_close(bars: Iterable[QuoteData]) -> float:
    """推断昨收。

    优先取「最后一根 bar 所属交易日」的前收字段：东财分时一次返回多日，该字段
    在当日首根上是有效值。当日首根没有有效前收时（例如只取到一天数据，解析器
    把首根的 previous_close 置 0），退化成前一交易日的收盘价。
    """
    dated: list[tuple[QuoteData, datetime]] = []
    for bar in bars:
        when = bar.market_time or bar.received_at
        if when is not None:
            dated.append((bar, when))
    if not dated:
        return 0.0
    dated.sort(key=lambda item: item[1])
    last_date = dated[-1][1].date()
    for bar, when in dated:
        if when.date() == last_date and bar.previous_close > 0:
            return float(bar.previous_close)
    previous_dates = {when.date() for _, when in dated if when.date() < last_date}
    if not previous_dates:
        return 0.0
    previous_date = max(previous_dates)
    closes = [bar.price for bar, when in dated if when.date() == previous_date]
    return float(closes[-1]) if closes else 0.0


@dataclass(frozen=True)
class _Baseline:
    """数据源基线：近 5 个交易日的 1 分钟分时。"""

    points: list[IntradayPoint]
    name: str
    previous_close: float


class IntradayService:
    """组装当日分时曲线。

    1 分钟基线按 TTL 缓存（失败结果也缓存，避免数据源挂掉时被反复重试打爆），
    实时部分每次现取内存缓存，所以缓存不会让曲线变旧。
    """

    def __init__(
        self,
        quote_cache: QuoteCache,
        provider_manager: ProviderManager,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._quote_cache = quote_cache
        self._providers = provider_manager
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._baselines: dict[str, tuple[float, _Baseline]] = {}

    async def get(
        self,
        symbol: str,
        limit: int = DEFAULT_LIMIT,
        *,
        now: datetime | None = None,
    ) -> IntradaySeries:
        """返回该标的最近一个有数据的交易日的分时序列（可能为空点集）。"""
        now = now or datetime.now()
        live_points = bars_to_points(self._quote_cache.get_window(symbol))
        baseline = await self._baseline_for(symbol, now)

        trade_date = self._pick_trade_date(live_points, baseline.points, now)
        live_today = points_on(live_points, trade_date)
        baseline_today = points_on(baseline.points, trade_date)
        points = merge_points(baseline_today, live_today)
        if limit > 0:
            points = points[-limit:]

        return IntradaySeries(
            symbol=symbol,
            name=self._resolve_name(symbol, baseline),
            trade_date=trade_date,
            previous_close=self._resolve_previous_close(symbol, baseline, trade_date),
            points=points,
            source=self._resolve_source(live_today, baseline_today),
            is_live=bool(live_today) and trade_date == now.date(),
        )

    # ──────── 内部实现 ────────

    @staticmethod
    def _pick_trade_date(
        live: list[IntradayPoint],
        baseline: list[IntradayPoint],
        now: datetime,
    ) -> date:
        """取交易日：以数据源基线为准，基线不可用时才退回实时 / 今天。

        不能优先用实时的日期：通达信 ``servertime`` 只有时分秒，由本地日期补全，
        周末与节假日会产出「日期是今天、时间却是上一场收盘」的幽灵点；拿它当
        交易日会把基线里真正那一场的数据整段过滤掉，只剩一个孤零零的假点。
        数据源分时里出现的日期一定是真实交易场次，所以以它为准。
        """
        if baseline:
            return baseline[-1].time.date()
        if live:
            return live[-1].time.date()
        return now.date()

    def _resolve_name(self, symbol: str, baseline: _Baseline) -> str:
        latest = self._quote_cache.get_latest(symbol)
        if latest is not None and latest.name and latest.name != symbol:
            return latest.name
        return baseline.name or symbol

    def _resolve_previous_close(
        self, symbol: str, baseline: _Baseline, trade_date: date
    ) -> float:
        """昨收：实时行情里的字段最准，其次用数据源基线推断。"""
        latest = self._quote_cache.get_latest(symbol)
        if latest is not None and latest.previous_close > 0:
            when = latest.market_time or latest.received_at
            if when is None or when.date() == trade_date:
                return float(latest.previous_close)
        return baseline.previous_close

    @staticmethod
    def _resolve_source(
        live: list[IntradayPoint], baseline: list[IntradayPoint]
    ) -> str:
        if live and baseline:
            return "merged"
        if live:
            return "live"
        if baseline:
            return "baseline"
        return "empty"

    async def _baseline_for(self, symbol: str, now: datetime) -> _Baseline:
        with self._lock:
            cached = self._baselines.get(symbol)
            if cached is not None and cached[0] > time.monotonic():
                return cached[1]
        baseline = await self._fetch_baseline(symbol, now)
        with self._lock:
            self._baselines[symbol] = (
                time.monotonic() + self._ttl_seconds,
                baseline,
            )
        return baseline

    async def _fetch_baseline(self, symbol: str, now: datetime) -> _Baseline:
        bars: list[QuoteData] = []
        try:
            bars = await self._providers.get_history(
                symbol,
                "1m",
                now - timedelta(days=FETCH_LOOKBACK_DAYS),
                now + timedelta(days=FETCH_LOOKAHEAD_DAYS),
            )
        except Exception as exc:  # noqa: BLE001
            # 数据源不可用时退化成「只有实时部分」，不能让整个接口失败
            logger.warning("分时基线获取失败 %s: %s", symbol, exc)
        name = ""
        for bar in bars:
            if bar.name and bar.name != symbol:
                name = bar.name
                break
        return _Baseline(
            points=bars_to_points(bars),
            name=name,
            previous_close=infer_previous_close(bars),
        )
