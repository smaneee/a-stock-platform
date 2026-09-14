"""当日分时序列（实时曲线）测试。

覆盖纯函数的分钟归并 / 合并 / 昨收推断，``IntradayService`` 的实时与基线拼接、
交易日选择、TTL 缓存、数据源故障降级，以及 ``GET /api/quotes/{symbol}/intraday``
的返回形态。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_intraday_service
from app.database.models import Security
from app.database.session import SessionLocal
from app.main import app
from app.realtime.intraday import (
    IntradayPoint,
    IntradaySeries,
    IntradayService,
    bars_to_points,
    infer_previous_close,
    merge_points,
    points_on,
)
from app.realtime.quote_cache import QuoteCache

from tests.helpers import make_quote

DAY = datetime(2026, 9, 11)
PREV_DAY = datetime(2026, 9, 10)


def _bar(
    when: datetime,
    price: float,
    *,
    volume: float = 1_000.0,
    amount: float = 10_000.0,
    previous_close: float = 0.0,
    name: str = "测试股600000",
):
    return make_quote(
        symbol="600000",
        name=name,
        price=price,
        volume=volume,
        amount=amount,
        previous_close=previous_close,
        market_time=when,
    )


def _cache_with(bars) -> QuoteCache:
    cache = QuoteCache(window_size=500)
    for bar in bars:
        cache.update(bar)
    return cache


class _FakeProviderManager:
    """只实现 get_history 的数据源替身，按请求区间过滤并计数。"""

    def __init__(self, bars=None, *, raise_error: bool = False) -> None:
        self.bars = list(bars or [])
        self.raise_error = raise_error
        self.calls = 0

    async def get_history(self, symbol, period, start_time, end_time):
        self.calls += 1
        if self.raise_error:
            raise RuntimeError("数据源不可用")
        return [
            bar
            for bar in self.bars
            if start_time <= (bar.market_time or bar.received_at) <= end_time
        ]


# ──────────────── 纯函数 ────────────────


def test_bars_to_points_merges_same_minute():
    """3 秒级快照必须归并成分钟点，否则曲线带锯齿。"""
    start = datetime(2026, 9, 11, 9, 30, 1)
    points = bars_to_points(
        [_bar(start, 10.0), _bar(start + timedelta(seconds=30), 10.2)]
    )
    assert len(points) == 1
    assert points[0].time == datetime(2026, 9, 11, 9, 30)
    assert points[0].price == 10.2


def test_bars_to_points_sorts_by_time():
    points = bars_to_points(
        [_bar(datetime(2026, 9, 11, 9, 32), 11.0), _bar(datetime(2026, 9, 11, 9, 31), 10.0)]
    )
    assert [p.price for p in points] == [10.0, 11.0]


def test_bars_to_points_skips_missing_time():
    bar = _bar(datetime(2026, 9, 11, 9, 30), 10.0).model_copy(
        update={"market_time": None, "received_at": None}
    )
    assert bars_to_points([bar]) == []


def test_merge_points_live_wins_on_same_minute():
    t1 = datetime(2026, 9, 11, 9, 30)
    t2 = datetime(2026, 9, 11, 9, 31)
    t3 = datetime(2026, 9, 11, 9, 32)
    baseline = [
        IntradayPoint(t1, 10.0, 100.0, 1_000.0),
        IntradayPoint(t2, 10.5, 200.0, 2_100.0),
    ]
    live = [
        IntradayPoint(t2, 10.8, 300.0, 3_240.0),
        IntradayPoint(t3, 11.0, 50.0, 550.0),
    ]
    merged = merge_points(baseline, live)
    assert [p.time for p in merged] == [t1, t2, t3]
    assert merged[1].price == 10.8
    assert merged[0].price == 10.0


def test_points_on_filters_other_days():
    points = bars_to_points(
        [_bar(DAY, 10.0), _bar(PREV_DAY, 9.0)]
    )
    assert [p.time.date() for p in points_on(points, date(2026, 9, 11))] == [
        date(2026, 9, 11)
    ]


def test_infer_previous_close_uses_last_day_field():
    bars = [
        _bar(PREV_DAY, 9.0),
        _bar(DAY, 10.0, previous_close=9.95),
        _bar(DAY + timedelta(minutes=1), 10.1, previous_close=9.95),
    ]
    assert infer_previous_close(bars) == pytest.approx(9.95)


def test_infer_previous_close_falls_back_to_previous_day_close():
    """当日首根的前收为 0（只取到一天数据）时，退化成前一交易日收盘价。"""
    bars = [
        _bar(PREV_DAY, 9.0),
        _bar(PREV_DAY + timedelta(minutes=1), 9.3),
        _bar(DAY, 10.0, previous_close=0.0),
    ]
    assert infer_previous_close(bars) == pytest.approx(9.3)


def test_infer_previous_close_empty():
    assert infer_previous_close([]) == 0.0


# ──────────────── IntradayService ────────────────


@pytest.mark.asyncio
async def test_get_merges_live_and_baseline():
    """实时部分覆盖基线同一分钟，并且补上更早的时段。"""
    cache = _cache_with(
        [_bar(datetime(2026, 9, 11, 9, 31), 10.5, previous_close=9.9)]
    )
    providers = _FakeProviderManager(
        [
            _bar(datetime(2026, 9, 11, 9, 30), 10.0, previous_close=9.9),
            _bar(datetime(2026, 9, 11, 9, 31), 10.4, previous_close=9.9),
        ]
    )
    service = IntradayService(cache, providers)

    series = await service.get("600000", now=datetime(2026, 9, 11, 10, 0))

    assert series.trade_date == date(2026, 9, 11)
    assert series.source == "merged"
    assert series.is_live is True
    assert series.previous_close == pytest.approx(9.9)
    assert [p.time.strftime("%H:%M") for p in series.points] == [
        "09:30",
        "09:31",
    ]
    # 同一分钟以实时为准
    assert series.points[1].price == pytest.approx(10.5)


@pytest.mark.asyncio
async def test_get_uses_baseline_when_no_live():
    """非交易日 / 未轮询的标的：返回最近一个交易日的整段分时。"""
    providers = _FakeProviderManager(
        [
            _bar(datetime(2026, 9, 10, 14, 59), 9.0),
            _bar(datetime(2026, 9, 11, 9, 30), 10.0, previous_close=9.9),
            _bar(datetime(2026, 9, 11, 9, 31), 10.1, previous_close=9.9),
        ]
    )
    service = IntradayService(QuoteCache(window_size=10), providers)

    series = await service.get("600000", now=datetime(2026, 9, 12, 10, 0))

    assert series.trade_date == date(2026, 9, 11)
    assert series.source == "baseline"
    assert series.is_live is False
    assert [p.time.date() for p in series.points] == [date(2026, 9, 11)] * 2
    assert series.previous_close == pytest.approx(9.9)


@pytest.mark.asyncio
async def test_get_ignores_ghost_live_point_on_non_trading_day():
    """周末的实时点带的是本地日期，不能拿它当交易日。

    回归：通达信 servertime 只有时分秒、由本地日期补全，周六会产生
    「日期是今天、时间却是上一场收盘」的幽灵点。早先按实时优先取交易日，
    结果基线里真正那一场被整段过滤，曲线上只剩一个孤零零的假点。
    """
    cache = _cache_with(
        [_bar(datetime(2026, 9, 12, 15, 30), 9.26, previous_close=9.35)]
    )
    providers = _FakeProviderManager(
        [
            _bar(datetime(2026, 9, 11, 9, 30), 9.4, previous_close=9.35),
            _bar(datetime(2026, 9, 11, 9, 31), 9.42, previous_close=9.35),
        ]
    )
    service = IntradayService(cache, providers)

    series = await service.get("600000", now=datetime(2026, 9, 12, 17, 55))

    assert series.trade_date == date(2026, 9, 11)
    assert series.source == "baseline"
    assert series.is_live is False
    assert len(series.points) == 2


@pytest.mark.asyncio
async def test_get_prefers_live_previous_close():
    cache = _cache_with(
        [_bar(datetime(2026, 9, 11, 9, 31), 10.5, previous_close=9.88)]
    )
    providers = _FakeProviderManager(
        [_bar(datetime(2026, 9, 11, 9, 30), 10.0, previous_close=9.5)]
    )
    service = IntradayService(cache, providers)

    series = await service.get("600000", now=datetime(2026, 9, 11, 10, 0))

    assert series.previous_close == pytest.approx(9.88)


@pytest.mark.asyncio
async def test_get_uses_baseline_name_and_previous_close():
    providers = _FakeProviderManager(
        [_bar(datetime(2026, 9, 11, 9, 30), 10.0, previous_close=9.5, name="浦发银行")]
    )
    service = IntradayService(QuoteCache(window_size=10), providers)

    series = await service.get("600000", now=datetime(2026, 9, 11, 10, 0))

    assert series.name == "浦发银行"
    assert series.previous_close == pytest.approx(9.5)


@pytest.mark.asyncio
async def test_get_tolerates_provider_failure():
    """数据源挂掉时退化成只有实时部分，接口不能整体失败。"""
    cache = _cache_with([_bar(datetime(2026, 9, 11, 9, 31), 10.5)])
    service = IntradayService(cache, _FakeProviderManager(raise_error=True))

    series = await service.get("600000", now=datetime(2026, 9, 11, 10, 0))

    assert series.source == "live"
    assert len(series.points) == 1


@pytest.mark.asyncio
async def test_get_returns_empty_series_when_nothing():
    service = IntradayService(
        QuoteCache(window_size=10), _FakeProviderManager([])
    )

    series = await service.get("600000", now=datetime(2026, 9, 12, 10, 0))

    assert series.points == []
    assert series.source == "empty"
    assert series.change_pct == 0.0
    assert series.stats()["last"] == 0.0


@pytest.mark.asyncio
async def test_baseline_is_cached_within_ttl():
    providers = _FakeProviderManager([_bar(DAY, 10.0)])
    service = IntradayService(QuoteCache(window_size=10), providers, ttl_seconds=60)

    await service.get("600000", now=datetime(2026, 9, 11, 10, 0))
    await service.get("600000", now=datetime(2026, 9, 11, 10, 0))

    assert providers.calls == 1


@pytest.mark.asyncio
async def test_baseline_refetched_after_ttl():
    providers = _FakeProviderManager([_bar(DAY, 10.0)])
    service = IntradayService(QuoteCache(window_size=10), providers, ttl_seconds=0)

    await service.get("600000", now=datetime(2026, 9, 11, 10, 0))
    await service.get("600000", now=datetime(2026, 9, 11, 10, 0))

    assert providers.calls == 2


@pytest.mark.asyncio
async def test_get_applies_limit_keeping_newest():
    bars = [
        _bar(DAY + timedelta(minutes=i), 10.0 + i * 0.01) for i in range(10)
    ]
    service = IntradayService(QuoteCache(window_size=10), _FakeProviderManager(bars))

    series = await service.get("600000", limit=3, now=datetime(2026, 9, 11, 10, 0))

    assert len(series.points) == 3
    assert series.points[-1].price == pytest.approx(10.09)
    assert series.points[0].time == DAY + timedelta(minutes=7)


def test_change_and_stats_use_previous_close():
    series = IntradaySeries(
        symbol="600000",
        name="测试股",
        trade_date=date(2026, 9, 11),
        previous_close=10.0,
        points=[
            IntradayPoint(datetime(2026, 9, 11, 9, 30), 10.2, 100.0, 1_020.0),
            IntradayPoint(datetime(2026, 9, 11, 9, 31), 9.8, 200.0, 1_960.0),
            IntradayPoint(datetime(2026, 9, 11, 14, 0), 10.5, 300.0, 3_150.0),
        ],
        source="live",
        is_live=True,
    )

    assert series.change == pytest.approx(0.5)
    assert series.change_pct == pytest.approx(5.0)
    stats = series.stats()
    assert stats["open"] == pytest.approx(10.2)
    assert stats["high"] == pytest.approx(10.5)
    assert stats["low"] == pytest.approx(9.8)
    assert stats["last"] == pytest.approx(10.5)
    assert stats["volume"] == pytest.approx(600.0)
    assert stats["amount"] == pytest.approx(6_130.0)
    # 序列化后时间必须是 ISO 字符串，前端才画得出来
    payload = series.to_dict()
    assert payload["points"][0]["time"] == "2026-09-11T09:30:00"
    assert payload["trade_date"] == "2026-09-11"


# ──────────────── 接口 ────────────────


class _StubIntradayService:
    def __init__(self, series: IntradaySeries) -> None:
        self.series = series

    async def get(self, symbol, limit=600, *, now=None) -> IntradaySeries:
        return self.series


def _series(points: list[IntradayPoint]) -> IntradaySeries:
    return IntradaySeries(
        symbol="600000",
        name="测试股",
        trade_date=date(2026, 9, 11),
        previous_close=10.0,
        points=points,
        source="live",
        is_live=True,
    )


@pytest.fixture
def api_client():
    """把分时服务替换成替身，测试结束后清掉依赖覆盖。"""
    created: list[TestClient] = []

    def _make(service) -> TestClient:
        app.dependency_overrides[get_intraday_service] = lambda: service
        client = TestClient(app)
        client.__enter__()
        created.append(client)
        return client

    yield _make
    for client in created:
        client.__exit__(None, None, None)
    app.dependency_overrides.pop(get_intraday_service, None)


def test_intraday_endpoint_ok(api_client):
    service = _StubIntradayService(
        _series([IntradayPoint(datetime(2026, 9, 11, 9, 30), 10.2, 1.0, 10.2)])
    )
    client = api_client(service)

    response = client.get("/api/quotes/600000/intraday")

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "600000"
    assert body["source"] == "live"
    assert body["stats"]["last"] == pytest.approx(10.2)
    assert len(body["points"]) == 1


def test_intraday_endpoint_404_without_points(api_client):
    client = api_client(_StubIntradayService(_series([])))

    response = client.get("/api/quotes/600000/intraday")

    assert response.status_code == 404


def test_intraday_endpoint_rejects_bad_symbol(api_client):
    client = api_client(_StubIntradayService(_series([])))

    response = client.get("/api/quotes/abc/intraday")

    assert response.status_code == 422


def test_intraday_endpoint_falls_back_to_security_master(api_client):
    """数据源分时没带名称时（name 退化成代码），用本地股票主数据补上。"""
    db = SessionLocal()
    try:
        db.add(
            Security(symbol="600000", name="浦发银行", board="main", exchange="sh")
        )
        db.commit()
    finally:
        db.close()

    points = [IntradayPoint(datetime(2026, 9, 11, 9, 30), 10.2, 1.0, 10.2)]
    service = _StubIntradayService(
        IntradaySeries(
            symbol="600000",
            name="600000",
            trade_date=date(2026, 9, 11),
            previous_close=10.0,
            points=points,
            source="baseline",
            is_live=False,
        )
    )
    client = api_client(service)

    response = client.get("/api/quotes/600000/intraday")

    assert response.status_code == 200
    assert response.json()["name"] == "浦发银行"
