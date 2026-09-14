"""技术指标接口。

- ``GET /api/indicators``           指标目录（可选序列、支持的周期、默认参数）
- ``GET /api/indicators/{symbol}``  单只股票最近 N 根 K 线的全套技术指标

覆盖 MA / EMA / MACD / RSI / BOLL / KDJ / ATR / OBV / CCI / WR。

数据来源优先级：

1. 本地 ``historical_bars`` 缓存（日线及以上优先 ``adjust=qfq`` 前复权，缺失时回退
   ``none`` —— 与实时扫描、回测同口径，见 ``resolve_bar_adjust``）；
2. 缓存根数不足时经 ``ProviderManager`` 向行情数据源实时拉取（数据源为未复权）。

响应里带 ``bars_adjust`` / ``bars_adjust_label``，调用方必须按它判断口径，
不得假定指标一定是某个固定口径。

本模块只读：不写数据库、不改动任何现有信号逻辑。
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_provider_manager
from app.database.models import HistoricalBar
from app.database.session import get_db
from app.history.service import (
    ADJUST_NONE,
    fetch_history_from_sources,
    provider_covered_sources,
)
from app.indicators.suite import (
    SERIES_TITLES,
    compute_indicators,
    latest_values,
    to_json_series,
)
from app.market_data.provider_manager import ProviderManager
from app.realtime.screener import (
    ADJUST_LABELS,
    resolve_bar_adjust_cached,
)
from app.validation import validate_symbol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/indicators", tags=["indicators"])

# 支持的 K 线周期（与行情数据源 KLT / TDX KLINE_TYPE 映射保持一致）
SUPPORTED_PERIODS = ("daily", "weekly", "monthly", "60m", "30m", "15m", "5m", "1m")
DEFAULT_PERIOD = "daily"
DEFAULT_LIMIT = 250
MIN_LIMIT = 30
MAX_LIMIT = 800
# 少于 2 根 K 线无法构成任何指标，直接返回 503
MIN_BARS = 2
# 回退链（AKShare / BaoStock）单次只读拉取的超时
_FALLBACK_TIMEOUT_SECONDS = 30.0
# 本地缓存的复权口径：优先前复权（与实时扫描 / 回测同口径），缺失时回退不复权。
# 旧实现写死 ``none``，于是「指标页看到的均线」与「扫描排名用的均线」在除权日会
# 不一致 —— 计划 §7.4 要求指标口径与前复权策略口径一致。
CACHE_ADJUST = "none"  # 兜底口径（前复权不可用时）

# 多源回退链只提供日线，因此仅日/周/月这类由日线聚合的周期走它兜底
_DAILY_LIKE = ("daily", "weekly", "monthly")

# A 股每个交易日约 240 分钟，用于估算分钟线需要回溯的自然日天数
_MINUTES_BY_PERIOD = {"60m": 60, "30m": 30, "15m": 15, "5m": 5, "1m": 1}
# 日 / 周 / 月线的「自然日 / 交易日」折算系数
_CALENDAR_FACTOR = {"daily": 1.6, "weekly": 7.5, "monthly": 31.0}


class IndicatorSeriesInfo(BaseModel):
    key: str
    title: str


class IndicatorCatalogResponse(BaseModel):
    periods: list[str]
    default_period: str
    default_limit: int
    min_limit: int
    max_limit: int
    count: int
    series: list[IndicatorSeriesInfo]


class IndicatorResponse(BaseModel):
    symbol: str
    period: str
    source: str
    count: int
    dates: list[str]
    close: list[float]
    titles: dict[str, str]
    series: dict[str, list[float | None]] = Field(
        ..., description="指标名 -> 与 dates 等长的序列，不足处为 null"
    )
    latest: dict[str, float | None] = Field(
        ..., description="每个指标最后一个有效值"
    )
    bars_adjust: str = Field(
        "none", description="本次参与计算的日线复权口径：qfq（前复权，本地缓存优先）/ none"
    )
    bars_adjust_label: str = Field("不复权", description="复权口径中文标签")


def _lookback_start(period: str, limit: int, end: datetime) -> datetime:
    """按周期与根数估算需要回溯的自然日起点。"""
    if period in _MINUTES_BY_PERIOD:
        bars_per_day = max(1, 240 // _MINUTES_BY_PERIOD[period])
        # 乘 2 覆盖周末与节假日，再加 5 天冗余
        return end - timedelta(days=math.ceil(limit / bars_per_day) * 2 + 5)
    factor = _CALENDAR_FACTOR[period]
    return end - timedelta(days=math.ceil(limit * factor) + 30)


def _bar_label(when: datetime, period: str) -> str:
    """K 线时间戳标签：分钟线必须带时刻，否则同一天的多根会重名。"""
    if period in _MINUTES_BY_PERIOD:
        return when.strftime("%Y-%m-%d %H:%M")
    return when.date().isoformat()


def _cached_bars(
    db: Session, symbol: str, period: str, limit: int, adjust: str = CACHE_ADJUST
) -> list[tuple[str, float, float, float, float]]:
    """从本地缓存读取最近 limit 根 K 线，按时间升序返回。

    ``historical_bars.trade_date`` 是 DATE 列，存不下分钟级时刻，因此只用于
    日线及以上的周期；分钟线一律走数据源，避免同一天的多根 K 线挤在同一个日期上。

    ``adjust`` 由 ``resolve_bar_adjust`` 决定：优先 ``qfq``，缺失或落后时回退
    ``none`` —— 口径必须显式传入，不能让调用方默认读到某个写死的口径。
    """
    stmt = (
        select(HistoricalBar)
        .where(
            HistoricalBar.symbol == symbol,
            HistoricalBar.period == period,
            HistoricalBar.adjust == adjust,
        )
        .order_by(HistoricalBar.trade_date.desc())
        .limit(limit)
    )
    bars = list(db.scalars(stmt).all())
    bars.reverse()
    return [
        (
            bar.trade_date.isoformat(),
            float(bar.high or 0),
            float(bar.low or 0),
            float(bar.close or 0),
            float(bar.volume or 0),
        )
        for bar in bars
    ]


def _rows_from_quotes(
    quotes: list, period: str = "daily"
) -> tuple[list[tuple[str, float, float, float, float]], str]:
    """把数据源返回的 K 线转成 (时间, 高, 低, 收, 量) 并按时间升序。"""
    rows: list[tuple[str, float, float, float, float]] = []
    for quote in sorted(quotes, key=lambda item: item.market_time or datetime.min):
        when = quote.market_time
        if when is None:
            continue
        rows.append(
            (
                _bar_label(when, period),
                float(quote.high or 0),
                float(quote.low or 0),
                float(quote.price or 0),
                float(quote.volume or 0),
            )
        )
    source = quotes[0].source if quotes else ""
    return rows, source or "provider"


def _aggregate_daily(
    rows: list[tuple[str, float, float, float, float]], period: str
) -> list[tuple[str, float, float, float, float]]:
    """把日线聚合成周线 / 月线（回退链只有日线时使用）。

    周线按 ISO 周（年 + 周号）、月线按自然月分组；组内时间戳取最后一个交易日，
    高/低取组内极值、收盘取最后一天、成交量求和，与行情源的周月线口径一致。
    """
    if period == "daily" or not rows:
        return rows
    buckets: dict[tuple[int, ...], list] = {}
    order: list[tuple[int, ...]] = []
    for row in rows:
        day = date.fromisoformat(row[0])
        if period == "weekly":
            year, week, _ = day.isocalendar()
            key: tuple[int, ...] = (year, week)
        else:
            key = (day.year, day.month)
        current = buckets.get(key)
        if current is None:
            buckets[key] = [row[0], row[1], row[2], row[3], row[4]]
            order.append(key)
            continue
        current[0] = row[0]
        current[1] = max(current[1], row[1])
        current[2] = min(current[2], row[2])
        current[3] = row[3]
        current[4] += row[4]
    return [
        (buckets[key][0], buckets[key][1], buckets[key][2], buckets[key][3], buckets[key][4])
        for key in order
    ]


async def _daily_fallback(
    provider_manager: ProviderManager, symbol: str, period: str, limit: int
) -> tuple[list[tuple[str, float, float, float, float]], str]:
    """ProviderManager 无数据时走只读多源回退链取日线（覆盖北交所）。

    周 / 月线按放大后的根数取日线再聚合，保证聚合后仍有 ``limit`` 根。
    """
    factor = 1 if period == "daily" else (5 if period == "weekly" else 21)
    daily_limit = limit * factor + 30
    end = datetime.now().date()
    start = end - timedelta(days=math.ceil(daily_limit * _CALENDAR_FACTOR["daily"]) + 30)
    try:
        quotes = await asyncio.wait_for(
            fetch_history_from_sources(
                symbol,
                start,
                end,
                ADJUST_NONE,
                provider_covered_sources(provider_manager),
            ),
            timeout=_FALLBACK_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - 回退链失败不能变成 500
        logger.warning("指标接口回退链拉取 %s 失败: %s", symbol, exc)
        return [], ""
    rows, source = _rows_from_quotes(quotes, "daily")
    return _aggregate_daily(rows, period), source or "akshare"


@router.get("", response_model=IndicatorCatalogResponse)
async def indicator_catalog() -> IndicatorCatalogResponse:
    """返回可用指标序列与周期，供前端渲染图例与选择器。"""
    series = [
        IndicatorSeriesInfo(key=key, title=title)
        for key, title in SERIES_TITLES.items()
    ]
    return IndicatorCatalogResponse(
        periods=list(SUPPORTED_PERIODS),
        default_period=DEFAULT_PERIOD,
        default_limit=DEFAULT_LIMIT,
        min_limit=MIN_LIMIT,
        max_limit=MAX_LIMIT,
        count=len(series),
        series=series,
    )


@router.get("/{symbol}", response_model=IndicatorResponse)
async def get_indicators(
    symbol: str = Path(..., description="6 位 A 股代码，如 600519"),
    period: str = Query(DEFAULT_PERIOD, description="K 线周期"),
    limit: int = Query(
        DEFAULT_LIMIT,
        ge=MIN_LIMIT,
        le=MAX_LIMIT,
        description="参与计算的 K 线根数",
    ),
    db: Session = Depends(get_db),
    provider_manager: ProviderManager = Depends(get_provider_manager),
) -> IndicatorResponse:
    """计算单只股票最近 N 根 K 线的全套技术指标。"""
    if not validate_symbol(symbol):
        raise HTTPException(status_code=422, detail="非法股票代码")
    if period not in SUPPORTED_PERIODS:
        raise HTTPException(
            status_code=422,
            detail=f"不支持的周期 {period!r}，可选 {', '.join(SUPPORTED_PERIODS)}",
        )

    # 本地缓存是 DATE 列，只能代表日线及以上周期。
    # 口径：日线及以上优先前复权（与实时扫描/回测一致），缺失时回退不复权；
    # 分钟线没有本地缓存，走数据源（数据源返回未复权）。
    cache_adjust = CACHE_ADJUST
    if period in _DAILY_LIKE:
        # 用**进程级带 TTL 的**解析：直连 `resolve_bar_adjust` 会让每个请求都付
        # 一次 13,359,225 行的 `GROUP BY adjust`（实测 p50 6.06 秒，接口总耗时 7.7 秒）。
        cache_adjust, _, _ = resolve_bar_adjust_cached(db, "daily")
    rows = (
        _cached_bars(db, symbol, period, limit, cache_adjust)
        if period in _DAILY_LIKE
        else []
    )
    source = "cache" if rows else ""

    if len(rows) < limit:
        now = datetime.now()
        # 日线及以上取「当天结束」，否则当天那根 K 线会因 15:00 的时间戳被挡在区间外
        end = (
            now
            if period in _MINUTES_BY_PERIOD
            else datetime.combine(now.date(), datetime.max.time())
        )
        try:
            quotes = await provider_manager.get_history(
                symbol, period, _lookback_start(period, limit, end), end
            )
        except Exception as exc:  # noqa: BLE001 - 数据源异常不能变成 500
            logger.warning("指标接口获取 %s(%s) 历史行情失败: %s", symbol, period, exc)
            quotes = []
        live_rows, live_source = _rows_from_quotes(quotes, period)
        if len(live_rows) > len(rows):
            rows, source = live_rows, live_source

    if len(rows) < MIN_BARS and period in _DAILY_LIKE:
        # 通达信未收录的代码段、以及东财 K 线在本机被限流时，用入库路径的
        # 同一套只读回退链兜底，保证沪深京所有标的都能算指标。
        fallback_rows, fallback_source = await _daily_fallback(
            provider_manager, symbol, period, limit
        )
        if len(fallback_rows) > len(rows):
            rows, source = fallback_rows, fallback_source

    if len(rows) < MIN_BARS:
        raise HTTPException(
            status_code=503,
            detail="未获取到足够的 K 线数据（数据源不可用且本地无缓存）",
        )

    rows = rows[-limit:]
    dates = [row[0] for row in rows]
    result = compute_indicators(
        highs=[row[1] for row in rows],
        lows=[row[2] for row in rows],
        closes=[row[3] for row in rows],
        volumes=[row[4] for row in rows],
    )
    latest = {
        key: None if value is None else round(value, 4)
        for key, value in latest_values(result).items()
    }
    # 实际口径：只有整段都取自本地缓存时才是 cache_adjust；一旦换成数据源/回退链
    # 的行情，那些数据源返回的是未复权，标注必须是 none，不能沿用缓存口径。
    used_adjust = cache_adjust if source == "cache" else CACHE_ADJUST

    return IndicatorResponse(
        symbol=symbol,
        period=period,
        source=source,
        count=len(rows),
        dates=dates,
        close=[round(row[3], 4) for row in rows],
        titles=dict(SERIES_TITLES),
        series=to_json_series(result),
        latest=latest,
        bars_adjust=used_adjust,
        bars_adjust_label=ADJUST_LABELS.get(used_adjust, used_adjust),
    )
