"""BOLL / KDJ / ATR / OBV / CCI / WR 指标与 /api/indicators 接口测试。

纯计算部分用确定性的小样本断言具体数值；接口部分用桩 ProviderManager，
不依赖外部网络。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_provider_manager
from app.api import indicators as indicators_api
from app.indicators.atr import atr, true_range
from app.indicators.boll import boll
from app.indicators.cci import cci
from app.indicators.kdj import kdj
from app.indicators.obv import obv
from app.indicators.suite import (
    SERIES_TITLES,
    compute_indicators,
    latest_values,
    to_json_series,
)
from app.indicators.wr import wr
from app.indicators.volume import amplitude_series, volume_ratio_series
from app.main import app
from app.market_data.base import QuoteData


def _is_nan(value: float | None) -> bool:
    return value is None or value != value


def test_boll_matches_manual_std():
    upper, middle, lower = boll([1.0, 2.0, 3.0, 4.0, 5.0], period=3, num_std=2.0)
    assert _is_nan(middle[0]) and _is_nan(middle[1])
    assert middle[2] == pytest.approx(2.0)
    assert middle[4] == pytest.approx(4.0)
    assert upper[2] == pytest.approx(4.0)
    assert lower[2] == pytest.approx(0.0)
    assert upper[4] == pytest.approx(6.0)
    assert lower[4] == pytest.approx(2.0)


def test_boll_flat_series_collapses_bands():
    upper, middle, lower = boll([10.0] * 30, period=20, num_std=2.0)
    assert middle[-1] == pytest.approx(10.0)
    assert upper[-1] == pytest.approx(10.0)
    assert lower[-1] == pytest.approx(10.0)


def test_boll_rejects_bad_params():
    with pytest.raises(ValueError):
        boll([1.0, 2.0], period=0)
    with pytest.raises(ValueError):
        boll([1.0, 2.0], period=2, num_std=-1.0)


def test_kdj_flat_series_is_neutral():
    values = [10.0] * 30
    k, d, j = kdj(values, values, values, period=9)
    assert len(k) == len(d) == len(j) == 30
    assert all(_is_nan(value) for value in k[:8])
    assert k[-1] == pytest.approx(50.0)
    assert d[-1] == pytest.approx(50.0)
    assert j[-1] == pytest.approx(50.0)


def test_kdj_range_and_length():
    closes = [float(i) for i in range(1, 61)]
    highs = [value + 0.5 for value in closes]
    lows = [value - 0.5 for value in closes]
    k, d, j = kdj(highs, lows, closes, period=9)
    assert len(k) == 60
    for value in k[8:]:
        assert 0.0 <= value <= 100.0
    for value in d[8:]:
        assert 0.0 <= value <= 100.0
    assert j[-1] > 50.0


def test_kdj_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        kdj([1.0, 2.0], [1.0], [1.0, 2.0])


def test_true_range_and_atr():
    highs = [10.0, 11.0, 12.0]
    lows = [9.0, 10.0, 11.0]
    closes = [9.5, 10.5, 11.5]
    assert true_range(highs, lows, closes) == pytest.approx([1.0, 1.5, 1.5])
    result = atr(highs, lows, closes, period=2)
    assert _is_nan(result[0])
    assert result[1] == pytest.approx(1.25)
    assert result[2] == pytest.approx(1.375)


def test_atr_rejects_bad_period():
    with pytest.raises(ValueError):
        atr([1.0], [1.0], [1.0], period=0)


def test_obv_accumulates_by_direction():
    closes = [10.0, 11.0, 10.0, 10.0]
    volumes = [100.0, 200.0, 300.0, 400.0]
    assert obv(closes, volumes) == pytest.approx([0.0, 200.0, -100.0, -100.0])


def test_obv_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        obv([1.0, 2.0], [1.0])


def test_cci_zero_when_no_deviation():
    values = [5.0] * 20
    result = cci(values, values, values, period=14)
    assert all(_is_nan(value) for value in result[:13])
    assert result[13] == pytest.approx(0.0)
    assert result[-1] == pytest.approx(0.0)


def test_cci_positive_on_sustained_uptrend():
    closes = [float(i) for i in range(1, 41)]
    highs = [value + 0.2 for value in closes]
    lows = [value - 0.2 for value in closes]
    result = cci(highs, lows, closes, period=14)
    assert result[-1] > 0


def test_wr_tongdaxin_scale_and_signed_variant():
    highs = [10.0, 10.0, 10.0]
    lows = [0.0, 0.0, 0.0]
    closes = [10.0, 5.0, 0.0]
    # period=1 时最高/最低即当根自身，三根都能给出有效值
    assert wr(highs, lows, closes, period=1) == pytest.approx([0.0, 50.0, 100.0])
    assert wr(highs, lows, closes, period=1, signed=True) == pytest.approx(
        [-100.0, -50.0, 0.0]
    )


def test_wr_flat_window_is_neutral():
    values = [8.0] * 20
    result = wr(values, values, values, period=14)
    assert result[-1] == pytest.approx(50.0)


def _sample_ohlcv(
    count: int = 120,
) -> tuple[list[float], list[float], list[float], list[float]]:
    closes = [10.0 + (index % 17) * 0.25 + index * 0.05 for index in range(count)]
    highs = [value + 0.3 for value in closes]
    lows = [value - 0.3 for value in closes]
    volumes = [1_000_000.0 + index * 1_000 for index in range(count)]
    return highs, lows, closes, volumes


def test_suite_covers_every_declared_series():
    highs, lows, closes, volumes = _sample_ohlcv()
    result = compute_indicators(highs, lows, closes, volumes)
    assert set(result) == set(SERIES_TITLES)
    for key, values in result.items():
        assert len(values) == len(closes), key


def test_suite_latest_values_are_finite_for_enough_history():
    highs, lows, closes, volumes = _sample_ohlcv()
    latest = latest_values(compute_indicators(highs, lows, closes, volumes))
    assert set(latest) == set(SERIES_TITLES)
    for key, value in latest.items():
        assert value is not None and value == value, key


def test_suite_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        compute_indicators([1.0], [1.0], [1.0], [])


def test_to_json_series_replaces_nan_with_none():
    highs, lows, closes, volumes = _sample_ohlcv(40)
    payload = to_json_series(compute_indicators(highs, lows, closes, volumes))
    assert payload["ma60"][0] is None
    assert payload["ma5"][-1] is not None


def test_amplitude_series_matches_manual_calculation():
    highs = [10.0, 11.0, 12.0]
    lows = [9.0, 10.0, 11.5]
    closes = [9.5, 10.5, 11.8]
    values = amplitude_series(highs, lows, closes)
    assert _is_nan(values[0])  # 首根没有昨收
    assert values[1] == pytest.approx((11.0 - 10.0) / 9.5 * 100)
    assert values[2] == pytest.approx((12.0 - 11.5) / 10.5 * 100)


def test_amplitude_series_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        amplitude_series([1.0], [], [1.0])


def test_volume_ratio_series_uses_rolling_average():
    volumes = [100.0, 100.0, 100.0, 100.0, 100.0, 200.0]
    values = volume_ratio_series(volumes, period=5)
    # 分母是「过去 5 根」，所以前 5 根都凑不齐
    assert [_is_nan(value) for value in values[:5]] == [True] * 5
    # 第 6 根 200 除以过去 5 根均量 100
    assert values[5] == pytest.approx(2.0)


def test_volume_ratio_series_handles_zero_average():
    values = volume_ratio_series([0.0, 0.0, 0.0, 0.0, 0.0, 5.0], period=5)
    # 均量为 0 时不能返回 inf，一律记 NaN
    assert _is_nan(values[5])
    assert all(_is_nan(value) for value in values)


class _StubProviderManager:
    """只实现 get_history 的桩 ProviderManager。"""

    def __init__(self, quotes: list[QuoteData] | None = None):
        self._quotes = list(quotes or [])
        self.calls: list[tuple[str, str]] = []

    async def get_history(self, symbol, period, start_time, end_time):
        self.calls.append((symbol, period))
        return list(self._quotes)

    async def close(self) -> None:
        return None


def _stub_bars(symbol: str = "600519", count: int = 60) -> list[QuoteData]:
    bars: list[QuoteData] = []
    start = datetime(2026, 1, 5, 15, 0)
    for index in range(count):
        price = 10.0 + index * 0.1
        bars.append(
            QuoteData(
                symbol=symbol,
                name="测试股",
                price=price,
                open=price - 0.05,
                high=price + 0.2,
                low=price - 0.2,
                previous_close=price - 0.1,
                volume=1_000_000.0,
                amount=price * 1_000_000.0,
                source="stub",
                market_time=start + timedelta(days=index),
            )
        )
    return bars


@pytest.fixture(autouse=True)
def _isolate_dependencies(monkeypatch):
    """桩依赖只在本文件内生效，并切断真实多源回退链（测试不联网）。

    默认让回退链返回空列表，需要验证兜底逻辑的用例用 monkeypatch 覆盖它。
    """

    async def _no_network(*args, **kwargs):
        return []

    monkeypatch.setattr(
        indicators_api, "fetch_history_from_sources", _no_network
    )
    yield
    app.dependency_overrides.pop(get_provider_manager, None)


def _client(manager) -> TestClient:
    app.dependency_overrides[get_provider_manager] = lambda: manager
    client = TestClient(app)
    client.__enter__()
    return client


def test_indicators_catalog_endpoint():
    client = _client(_StubProviderManager())
    try:
        resp = client.get("/api/indicators")
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["default_period"] == "daily"
    keys = {item["key"] for item in body["series"]}
    assert {"boll_upper", "kdj_k", "atr14", "obv", "cci14", "wr14"} <= keys
    assert body["count"] == len(keys)


def test_indicators_endpoint_returns_series():
    manager = _StubProviderManager(_stub_bars(count=60))
    client = _client(manager)
    try:
        resp = client.get("/api/indicators/600519", params={"limit": 60})
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "600519"
    assert body["period"] == "daily"
    assert body["source"] == "stub"
    assert body["count"] == 60
    assert len(body["dates"]) == 60
    assert len(body["close"]) == 60
    assert set(body["series"]) == set(SERIES_TITLES)
    assert len(body["series"]["ma5"]) == 60
    assert body["series"]["ma60"][0] is None
    assert body["latest"]["ma60"] is not None
    assert manager.calls == [("600519", "daily")]


def test_indicators_endpoint_prefers_local_cache():
    from app.database.models import HistoricalBar
    from app.database.session import SessionLocal

    db = SessionLocal()
    try:
        for index in range(60):
            db.add(
                HistoricalBar(
                    symbol="600519",
                    period="daily",
                    adjust="none",
                    trade_date=date(2026, 1, 5) + timedelta(days=index),
                    open=10.0,
                    high=11.0,
                    low=9.0,
                    close=10.5,
                    volume=1_000_000.0,
                    amount=10_500_000.0,
                    source="test",
                )
            )
        db.commit()
    finally:
        db.close()

    manager = _StubProviderManager(_stub_bars(count=60))
    client = _client(manager)
    try:
        resp = client.get("/api/indicators/600519", params={"limit": 60})
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "cache"
    assert body["count"] == 60
    assert manager.calls == []


def test_indicators_endpoint_rejects_bad_symbol():
    client = _client(_StubProviderManager())
    try:
        resp = client.get("/api/indicators/12345")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 422


def test_indicators_endpoint_rejects_bad_period():
    client = _client(_StubProviderManager(_stub_bars(count=60)))
    try:
        resp = client.get("/api/indicators/600519", params={"period": "yearly"})
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 422


def test_indicators_endpoint_rejects_out_of_range_limit():
    client = _client(_StubProviderManager(_stub_bars(count=60)))
    try:
        resp = client.get("/api/indicators/600519", params={"limit": 5})
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 422


def test_indicators_endpoint_503_without_any_data():
    client = _client(_StubProviderManager())
    try:
        resp = client.get("/api/indicators/600519", params={"limit": 60})
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 503


def test_indicators_endpoint_survives_provider_failure():
    class _BrokenManager:
        async def get_history(self, *args, **kwargs):
            raise RuntimeError("data source down")

        async def close(self) -> None:
            return None

    client = _client(_BrokenManager())
    try:
        resp = client.get("/api/indicators/600519", params={"limit": 60})
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 503


def _daily_rows() -> list[tuple[str, float, float, float, float]]:
    return [
        ("2026-09-01", 10.0, 9.0, 9.5, 100.0),
        ("2026-09-02", 11.0, 9.5, 10.5, 200.0),
        ("2026-09-03", 12.0, 10.0, 11.0, 300.0),
        ("2026-09-07", 13.0, 11.0, 12.5, 400.0),
        ("2026-09-08", 14.0, 12.0, 13.0, 500.0),
    ]


def test_aggregate_daily_daily_is_passthrough():
    rows = _daily_rows()
    assert indicators_api._aggregate_daily(rows, "daily") == rows


def test_aggregate_daily_weekly_groups_by_iso_week():
    weekly = indicators_api._aggregate_daily(_daily_rows(), "weekly")
    # 2026-09-01~03 是同一 ISO 周（周一到周三），09-07/08 是下一周
    assert weekly == [
        ("2026-09-03", 12.0, 9.0, 11.0, 600.0),
        ("2026-09-08", 14.0, 11.0, 13.0, 900.0),
    ]


def test_aggregate_daily_monthly_groups_by_calendar_month():
    rows = _daily_rows() + [("2026-10-09", 15.0, 14.0, 14.5, 100.0)]
    monthly = indicators_api._aggregate_daily(rows, "monthly")
    assert monthly == [
        ("2026-09-08", 14.0, 9.0, 13.0, 1500.0),
        ("2026-10-09", 15.0, 14.0, 14.5, 100.0),
    ]


def test_aggregate_daily_handles_empty_input():
    assert indicators_api._aggregate_daily([], "weekly") == []


def test_indicators_endpoint_falls_back_to_sources(monkeypatch):
    """ProviderManager 拿不到数据（北交所 / 东财限流）时走只读多源回退链。"""
    bars = _stub_bars(symbol="920819", count=60)
    seen: dict[str, object] = {}

    async def _fake_fetch(symbol, start, end, adjust=None, skip=()):
        seen["args"] = (symbol, start, end, adjust, skip)
        return bars

    monkeypatch.setattr(
        indicators_api, "fetch_history_from_sources", _fake_fetch
    )
    manager = _StubProviderManager()  # TDX / 东财都没有数据
    client = _client(manager)
    try:
        resp = client.get("/api/indicators/920819", params={"limit": 60})
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "920819"
    assert body["source"] == "stub"
    assert body["count"] == 60
    assert seen["args"][0] == "920819"
    # 回退链只提供日线，参数口径必须与回测默认一致
    assert seen["args"][3] == "none"


def test_indicators_endpoint_aggregates_weekly_from_sources(monkeypatch):
    """周线在回退链上按日线聚合，源标签沿用真实来源。"""
    bars = _stub_bars(symbol="920819", count=140)

    async def _fake_fetch(*args, **kwargs):
        return bars

    monkeypatch.setattr(
        indicators_api, "fetch_history_from_sources", _fake_fetch
    )
    client = _client(_StubProviderManager())
    try:
        resp = client.get(
            "/api/indicators/920819", params={"period": "weekly", "limit": 30}
        )
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["period"] == "weekly"
    assert 0 < body["count"] <= 30
    # 聚合后时间戳必须严格递增且落在组内最后一个交易日
    assert body["dates"] == sorted(body["dates"])
    assert len(set(body["dates"])) == len(body["dates"])


def test_indicators_endpoint_survives_fallback_failure(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("sources down")

    monkeypatch.setattr(indicators_api, "fetch_history_from_sources", _boom)
    client = _client(_StubProviderManager())
    try:
        resp = client.get("/api/indicators/920819", params={"limit": 60})
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 503


def _intraday_bars(days: int = 10, bars_per_day: int = 4) -> list[QuoteData]:
    """同一交易日内多根 K 线，时刻不同；用于验证分钟线标签。"""
    bars: list[QuoteData] = []
    for day in range(days):
        for slot in range(bars_per_day):
            when = datetime(2026, 8, 3, 10, 30) + timedelta(
                days=day, minutes=slot * 60
            )
            price = 10.0 + day * 0.1 + slot * 0.01
            bars.append(
                QuoteData(
                    symbol="600519",
                    name="测试股",
                    price=price,
                    open=price,
                    high=price * 1.01,
                    low=price * 0.99,
                    previous_close=price,
                    volume=1_000.0,
                    amount=price * 1_000.0,
                    source="stub",
                    market_time=when,
                )
            )
    return bars


def test_rows_from_quotes_keeps_time_for_intraday():
    """分钟线的时间戳必须带时刻，否则同一天的多根会重名。"""
    rows, source = indicators_api._rows_from_quotes(
        _intraday_bars(days=1, bars_per_day=4), "60m"
    )
    assert source == "stub"
    assert [row[0] for row in rows] == [
        "2026-08-03 10:30",
        "2026-08-03 11:30",
        "2026-08-03 12:30",
        "2026-08-03 13:30",
    ]
    daily_rows, _ = indicators_api._rows_from_quotes(_intraday_bars(days=1), "daily")
    assert [row[0] for row in daily_rows] == ["2026-08-03"] * 4


def test_indicators_endpoint_minute_labels_are_unique():
    manager = _StubProviderManager(_intraday_bars(days=12, bars_per_day=4))
    client = _client(manager)
    try:
        resp = client.get(
            "/api/indicators/600519", params={"period": "60m", "limit": 30}
        )
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 30
    labels = body["dates"]
    assert len(set(labels)) == len(labels)
    assert labels == sorted(labels)
    assert all(len(label) == 16 and ":" in label for label in labels)
