"""实时买点雷达（GET /api/realtime/picks）的单元与集成测试。

覆盖三块：
1. 行情/日线合并规则 —— 休市、盘中、跨日都不能重复计入或漏计当日 K 线；
2. 参数自检与情绪档位等纯函数；
3. 用内存 SQLite + 桩行情跑通整条扫描链路，以及接口的成功/503 路径。
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    HistoricalBar,
    LimitUpSentiment,
    TradingDate,
    UniverseMember,
    UniverseSnapshot,
)
from app.database.session import Base
from app.market_data.base import QuoteData
from app.realtime.screener import (
    RISK_LABELS,
    ScreenerConfig,
    ScreenerService,
    ScreenerUnavailable,
    SymbolSeries,
    _prescreen_key,
    _same_value,
    _same_volume,
    _split_even,
    _stance_of,
)

DAY_LAST = date(2026, 9, 11)
DAY_NEXT = date(2026, 9, 14)


def _series(closes: list[float], *, day0: date | None = None) -> SymbolSeries:
    """按收盘价序列造一条日线（开=前收，高=收×1.01，低=收×0.99）。"""
    start = day0 or (DAY_LAST - timedelta(days=len(closes) - 1))
    days, opens, highs, lows, vols, amounts = [], [], [], [], [], []
    for index, close in enumerate(closes):
        days.append(start + timedelta(days=index))
        opens.append(closes[index - 1] if index else close)
        highs.append(close * 1.01)
        lows.append(close * 0.99)
        vols.append(1_000_000.0 + index * 1_000.0)
        amounts.append(close * (1_000_000.0 + index * 1_000.0))
    return SymbolSeries(
        symbol="600000",
        days=tuple(days),
        opens=tuple(opens),
        highs=tuple(highs),
        lows=tuple(lows),
        closes=tuple(closes),
        volumes=tuple(vols),
        amounts=tuple(amounts),
    )


def _quote(series: SymbolSeries, price: float, volume: float, amount: float, **extra):
    return QuoteData(
        symbol=series.symbol,
        name="测试股",
        price=price,
        open=price,
        high=price,
        low=price,
        previous_close=series.closes[-2] if len(series) >= 2 else price,
        volume=volume,
        amount=amount,
        source="stub",
        **extra,
    )


# ─────────────────── 1. 合并规则 ───────────────────


def test_same_value_relative_tolerance():
    assert _same_value(10.0, 10.0)
    assert _same_value(17.65, 17.650000000000002)
    assert not _same_value(10.0, 10.001)


def test_same_volume_allows_one_lot_rounding():
    """通达信是「手×100」，本地日线来自其它源，同一根 K 线允许差 1 手。"""
    assert _same_volume(18_292_400.0, 18_292_500.0)
    assert not _same_volume(18_292_500.0, 18_392_500.0)


def test_identical_quote_does_not_duplicate_bar():
    series = _series([10.0, 10.2, 10.4])
    quote = _quote(series, 10.4, series.volumes[-1], series.amounts[-1])

    merged, live = series.replaced_or_extended(quote, DAY_LAST)

    assert merged is series
    assert live is False
    assert len(merged) == 3


def test_new_day_is_appended():
    series = _series([10.0, 10.2, 10.4])
    quote = _quote(series, 10.9, 2_000_000.0, 21_800_000.0)

    merged, live = series.replaced_or_extended(quote, DAY_NEXT)

    assert live is True
    assert len(merged) == 4
    assert merged.days[-1] == DAY_NEXT
    assert merged.closes[-1] == 10.9
    # 原序列不被就地修改（frozen dataclass + 元组）
    assert len(series) == 3


def test_intraday_bar_of_same_day_is_replaced():
    """盘中已入库当天 K 线时，用更新鲜的行情覆盖，不能出现同一天两根。"""
    series = _series([10.0, 10.2, 10.4])
    series = series.replaced_or_extended(
        _quote(series, 10.5, 900_000.0, 9_400_000.0), DAY_LAST
    )[0]
    quote = _quote(series, 10.7, 1_500_000.0, 16_000_000.0)

    merged, live = series.replaced_or_extended(quote, DAY_LAST)

    assert live is True
    assert len(merged) == 3
    assert merged.days[-1] == DAY_LAST
    assert merged.closes[-1] == 10.7
    assert merged.volumes[-1] == 1_500_000.0


def test_stale_quote_never_creates_a_bar():
    """本地缓存兜底的过期行情不能拼出当日 K 线。"""
    series = _series([10.0, 10.2, 10.4])
    quote = _quote(series, 10.9, 2_000_000.0, 21_800_000.0, is_stale=True)

    merged, live = series.replaced_or_extended(quote, DAY_NEXT)

    assert merged is series
    assert live is False


def test_zero_volume_quote_is_ignored():
    """盘前集合竞价 / 停牌：volume<=0 构不成有效 K 线。"""
    series = _series([10.0, 10.2, 10.4])

    merged, live = series.replaced_or_extended(
        _quote(series, 10.4, 0.0, 0.0), DAY_NEXT
    )

    assert merged is series
    assert live is False


def test_tail_keeps_last_bars():
    series = _series([float(i) for i in range(1, 11)])

    tail = series.tail(3)

    assert tail.closes == (8.0, 9.0, 10.0)
    assert series.tail(50) is series


# ─────────────────── 2. 参数与纯函数 ───────────────────


def test_config_defaults_are_valid():
    ScreenerConfig().validate()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_n": 0},
        {"top_n": 101},
        {"lookback_days": 60},
        {"lookback_days": 801},
        {"refine_pool": 5, "top_n": 10},
        {"min_amount_20": -1.0},
        {"min_price": 0.0},
        {"min_price": 20.0, "max_price": 10.0},
        {"min_change_pct": 5.0, "max_change_pct": 5.0},
        {"min_triggers": 9},
        {"stop_atr_multiple": 0.0},
        {"target_atr_multiple": -1.0},
        {"risk_budget_pct": 0.0},
        {"max_weight_pct": 101.0},
        {"sentiment_lag_days": 0},
    ],
)
def test_config_validate_rejects_bad_values(kwargs):
    with pytest.raises(ValueError):
        ScreenerConfig(**kwargs).validate()


@pytest.mark.parametrize(
    ("seal_rate", "expected"),
    [
        (0.80, "strong"),
        (0.75, "strong"),
        (0.65, "healthy"),
        (0.60, "healthy"),
        (0.50, "neutral"),
        (0.45, "neutral"),
        (0.44, "weak"),
        (None, "neutral"),
    ],
)
def test_stance_thresholds(seal_rate, expected):
    assert _stance_of(seal_rate)[0] == expected


def test_split_even_covers_all_symbols():
    symbols = [f"{i:06d}" for i in range(10)]

    chunks = _split_even(symbols, 4)

    assert sum(len(chunk) for chunk in chunks) == 10
    assert sorted(s for chunk in chunks for s in chunk) == symbols
    assert _split_even(symbols, 1) == [symbols]
    assert _split_even([], 4) == [[]]


def test_prescreen_key_prefers_uptrend_then_proximity():
    from app.realtime.screener import _Pass1

    base = dict(
        symbol="600000",
        name="A",
        exchange="SH",
        board="main",
        is_st=False,
        series=_series([10.0] * 70),
        live=False,
        price=10.0,
        previous_close=10.0,
        change_pct=0.0,
        ma20=10.0,
        ma60=10.0,
        momentum_20=0.0,
        momentum_60=0.0,
        amount_20=1e8,
        high20=10.5,
        low20=9.5,
        volatility_20=0.2,
        volume_ratio_20=1.0,
    )
    uptrend = _Pass1(**{**base, "price": 11.0, "ma20": 10.0, "ma60": 9.0})
    hugging = _Pass1(**{**base, "price": 10.0, "ma20": 10.0, "ma60": 9.0})
    extended = _Pass1(**{**base, "price": 15.0, "ma20": 10.0, "ma60": 9.0})
    downtrend = _Pass1(**{**base, "price": 8.0, "ma20": 9.0, "ma60": 10.0})

    assert _prescreen_key(hugging) > _prescreen_key(extended)
    assert _prescreen_key(uptrend)[0] == _prescreen_key(hugging)[0]
    assert _prescreen_key(downtrend) < _prescreen_key(hugging)


# ─────────────────── 3. 整链路 + 接口 ───────────────────


def _make_session_factory():
    """独立内存 SQLite（不启用 FK pragma，因此无需再伪造 Securities 行）。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _closes(count: int = 80) -> list[float]:
    """先涨后小幅回踩：制造「站上均线 + RSI 不超买」的健康形态。"""
    values = [10.0 + 0.05 * index for index in range(count - 12)]
    peak = values[-1]
    for index in range(1, 13):
        values.append(round(peak - 0.06 * index, 4))
    return values


def _seed(session_factory, symbols: list[str], closes: list[float]) -> None:
    session = session_factory()
    try:
        for day in (
            date(2026, 9, 7),
            date(2026, 9, 8),
            date(2026, 9, 9),
            date(2026, 9, 10),
            DAY_LAST,
            DAY_NEXT,
        ):
            session.add(TradingDate(trade_date=day))
        snapshot = UniverseSnapshot(
            trading_day=DAY_LAST,
            total_count=len(symbols),
            included_count=len(symbols),
            excluded_count=0,
            source_provider="test",
        )
        session.add(snapshot)
        session.flush()
        start = DAY_LAST - timedelta(days=len(closes) - 1)
        for symbol in symbols:
            session.add(
                UniverseMember(
                    snapshot_id=snapshot.id,
                    symbol=symbol,
                    security_id=symbol,
                    is_included=True,
                    name=f"测试{symbol[-2:]}",
                    exchange="SH",
                    board="main",
                    is_st=False,
                    trading_status="active",
                )
            )
            for index, close in enumerate(closes):
                session.add(
                    HistoricalBar(
                        symbol=symbol,
                        period="daily",
                        adjust="none",
                        trade_date=start + timedelta(days=index),
                        open=closes[index - 1] if index else close,
                        high=close * 1.01,
                        low=close * 0.99,
                        close=close,
                        # 成交额要显著高于 min_amount_20（默认 5000 万），
                        # 否则会被流动性过滤掉，测不到后面的打分逻辑
                        volume=10_000_000.0 + index * 1_000.0,
                        amount=close * (10_000_000.0 + index * 1_000.0),
                    )
                )
        session.add(
            LimitUpSentiment(
                trade_date=DAY_LAST,
                limit_up_count=40,
                broken_board_count=18,
                seal_rate=40 / 58,
                broken_rate=18 / 58,
                max_streak=4,
            )
        )
        session.add(
            LimitUpSentiment(
                trade_date=date(2026, 9, 10),
                limit_up_count=35,
                broken_board_count=22,
                seal_rate=35 / 57,
                broken_rate=22 / 57,
                max_streak=4,
            )
        )
        session.commit()
    finally:
        session.close()


def _last_bar(closes: list[float]) -> tuple[float, float]:
    """按 ``_seed`` 的写入规则算最后一根 K 线的 (成交量, 成交额)。"""
    volume = 10_000_000.0 + (len(closes) - 1) * 1_000.0
    return volume, closes[-1] * volume


def _stub_fetcher(spec: dict[str, dict]):
    """spec: symbol -> QuoteData 覆盖字段。"""

    async def fetch(symbols):
        return {
            symbol: QuoteData(
                symbol=symbol,
                name=f"测试{symbol[-2:]}",
                source="stub",
                **spec[symbol],
            )
            for symbol in symbols
            if symbol in spec
        }

    return fetch


def _holiday_spec(symbols: list[str], closes: list[float]) -> dict[str, dict]:
    """休市：行情与最后一根日线逐字段一致（只差 1 手的单位换算）。"""
    volume, amount = _last_bar(closes)
    price = closes[-1]
    return {
        symbol: {
            "price": price,
            "open": price,
            "high": price,
            "low": price,
            "previous_close": closes[-2],
            "volume": volume - 100.0,
            "amount": amount,
        }
        for symbol in symbols
    }


def _service(session_factory, fetcher):
    return ScreenerService(
        session_factory=session_factory,
        quote_fetcher=fetcher,
        tdx_pool_size=0,
        clock=lambda: date(2026, 9, 12),
    )


@pytest.mark.asyncio
async def test_run_scan_on_holiday_uses_last_close():
    """休市日：行情与日线一致 → 不新增 K 线，买点按下一交易日评估。"""
    factory = _make_session_factory()
    symbols = ["600000", "600519", "000001"]
    closes = _closes()
    _seed(factory, symbols, closes)
    holding = closes[-1]
    service = _service(factory, _stub_fetcher(_holiday_spec(symbols, closes)))

    result = await service.run(ScreenerConfig(top_n=3, min_triggers=2))

    assert result.session_day == DAY_LAST
    assert result.signal_day == DAY_NEXT
    assert result.live is False
    assert result.universe_size == 3
    assert result.quoted == 3
    assert result.screened == 3
    assert result.sentiment.available is True
    assert result.sentiment.sentiment_date == DAY_LAST
    assert result.sentiment.stance == "healthy"
    assert len(result.picks) >= 1
    pick = result.picks[0]
    assert pick.rank == 1
    assert pick.bar_count == len(closes)
    assert pick.last_bar_date == DAY_LAST
    assert pick.live is False
    assert pick.score > 0
    assert pick.triggers
    assert pick.entry_low <= pick.entry_high
    assert pick.stop_loss < pick.entry_low
    assert pick.target_price > pick.entry_high
    assert 0 < pick.suggested_weight_pct <= result.config.max_weight_pct
    assert all(flag in RISK_LABELS for flag in pick.risk_flags)
    assert result.notes[-1] == "分析结果仅用于研究，不构成投资建议"
    assert pick.price == pytest.approx(holding, abs=0.01)

    from types import SimpleNamespace

    from app.api.realtime import _to_response

    response = _to_response(
        result,
        SimpleNamespace(report=None, status=lambda: {"state": "idle"}),
    )
    assert response.evidence.status == "not_passed"
    assert response.evidence.recommendation_allowed is False
    assert response.evidence.mean_excess_pct == -0.39
    assert response.picks[0].symbol == pick.symbol


@pytest.mark.asyncio
async def test_run_scan_marks_live_when_quote_carries_new_day():
    factory = _make_session_factory()
    symbols = ["600000"]
    closes = _closes()
    _seed(factory, symbols, closes)
    live_price = round(closes[-1] * 1.02, 4)
    service = _service(
        factory,
        _stub_fetcher(
            {
                symbol: {
                    "price": live_price,
                    "open": live_price,
                    "high": live_price,
                    "low": live_price,
                    "previous_close": closes[-1],
                    "volume": 9_999_999.0,
                    "amount": live_price * 9_999_999.0,
                }
                for symbol in symbols
            }
        ),
    )

    result = await service.run(ScreenerConfig(top_n=1, min_triggers=1))

    assert result.live is True
    pick = result.picks[0]
    assert pick.live is True
    assert pick.bar_count == len(closes)  # 替换而非追加（最后一根就是同日）


@pytest.mark.asyncio
async def test_run_scan_withholds_picks_when_history_misses_previous_trading_day():
    """历史窗口断档时，即使实时行情齐全也不能发布看似最新的排名。"""
    factory = _make_session_factory()
    symbols = ["600000"]
    closes = _closes()
    _seed(factory, symbols, closes)
    with factory() as db:
        db.add(TradingDate(trade_date=date(2026, 9, 15)))
        db.commit()
    live_price = round(closes[-1] * 1.02, 4)
    service = ScreenerService(
        session_factory=factory,
        quote_fetcher=_stub_fetcher(
            {
                "600000": {
                    "price": live_price,
                    "open": live_price,
                    "high": live_price,
                    "low": live_price,
                    "previous_close": closes[-1],
                    "volume": 9_999_999.0,
                    "amount": live_price * 9_999_999.0,
                }
            }
        ),
        tdx_pool_size=0,
        clock=lambda: date(2026, 9, 15),
    )

    result = await service.run(ScreenerConfig(top_n=1, min_triggers=1))

    assert result.required_bars_day == DAY_NEXT
    assert result.bars_last_day == DAY_LAST
    assert result.data_fresh is False
    assert result.picks == []
    assert any("已停止发布候选" in note for note in result.notes)


@pytest.mark.asyncio
async def test_run_scan_rejects_quotes_marked_stale():
    factory = _make_session_factory()
    closes = _closes()
    _seed(factory, ["600000"], closes)
    price = closes[-1]
    service = _service(
        factory,
        _stub_fetcher(
            {
                "600000": {
                    "price": price,
                    "open": price,
                    "high": price,
                    "low": price,
                    "previous_close": closes[-2],
                    "volume": 9_999_999.0,
                    "amount": price * 9_999_999.0,
                    "is_stale": True,
                }
            }
        ),
    )

    with pytest.raises(ScreenerUnavailable, match="全部为过期缓存"):
        await service.run(ScreenerConfig(top_n=1))


@pytest.mark.asyncio
async def test_second_scan_returns_short_cache_without_refetching_quotes():
    factory = _make_session_factory()
    closes = _closes()
    symbols = ["600000"]
    _seed(factory, symbols, closes)
    calls = 0
    spec = _holiday_spec(symbols, closes)

    async def fetch(requested):
        nonlocal calls
        calls += 1
        return await _stub_fetcher(spec)(requested)

    service = _service(factory, fetch)
    config = ScreenerConfig(top_n=1, min_triggers=1)

    first = await service.run(config)
    second = await service.run(config)

    assert first.served_from_cache is False
    assert second.served_from_cache is True
    assert second.cache_age_seconds >= 0
    assert calls == 1


@pytest.mark.asyncio
async def test_failed_background_refresh_keeps_marked_last_success(monkeypatch):
    import app.realtime.screener as screener_module

    monkeypatch.setattr(screener_module, "RESULT_CACHE_TTL_SECONDS", -1.0)
    factory = _make_session_factory()
    closes = _closes()
    symbols = ["600000"]
    _seed(factory, symbols, closes)
    calls = 0
    spec = _holiday_spec(symbols, closes)

    async def fetch(requested):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("upstream offline")
        return await _stub_fetcher(spec)(requested)

    service = _service(factory, fetch)
    config = ScreenerConfig(top_n=1, min_triggers=1)
    first = await service.run(config)
    stale = await service.run(config)
    for _ in range(100):
        if service._refresh_errors:  # noqa: SLF001 - 等待后台失败被可观测状态接住
            break
        await asyncio.sleep(0.01)
    after_failure = await service.run(config)

    assert stale.served_from_cache is True
    assert stale.refresh_in_progress is True
    assert after_failure.picks == first.picks
    assert after_failure.served_from_cache is True
    assert "upstream offline" in (after_failure.refresh_error or "")


@pytest.mark.asyncio
async def test_run_raises_when_no_quote_available():
    factory = _make_session_factory()
    closes = _closes()
    _seed(factory, ["600000"], closes)

    service = _service(factory, _stub_fetcher({}))

    with pytest.raises(ScreenerUnavailable):
        await service.run(ScreenerConfig(top_n=1))


@pytest.mark.asyncio
async def test_run_raises_when_universe_empty():
    factory = _make_session_factory()
    service = _service(factory, _stub_fetcher({}))

    with pytest.raises(ScreenerUnavailable):
        await service.run(ScreenerConfig(top_n=1))


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_api_rejects_invalid_params():
    from app.api.deps import get_screener_service
    from app.main import app

    app.dependency_overrides[get_screener_service] = lambda: object()
    try:
        client = _client()
        assert client.get("/api/realtime/picks?top_n=0").status_code == 422
        assert client.get("/api/realtime/picks?min_triggers=99").status_code == 422
    finally:
        app.dependency_overrides.pop(get_screener_service, None)


def test_api_returns_503_when_data_unavailable():
    from app.api.deps import get_screener_service
    from app.main import app

    class _Failing:
        async def run(self, config):
            raise ScreenerUnavailable("股票池为空")

    app.dependency_overrides[get_screener_service] = _Failing
    try:
        response = _client().get("/api/realtime/picks")
    finally:
        app.dependency_overrides.pop(get_screener_service, None)

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "screener_unavailable"


def test_research_evidence_never_promotes_single_positive_report():
    """单次正超额也不满足计划要求的多折独立发布门槛。"""
    from types import SimpleNamespace

    from app.api.realtime import _research_evidence

    report = SimpleNamespace(
        control="none",
        generated_at="2026-09-13T12:00:00",
        config=SimpleNamespace(primary_horizon=3),
        horizons=[SimpleNamespace(horizon=3, mean_excess_pct=0.25)],
    )
    service = SimpleNamespace(
        report=report,
        status=lambda: {"state": "done"},
    )

    evidence = _research_evidence(service)

    assert evidence.status == "not_passed"
    assert evidence.recommendation_allowed is False
    assert evidence.mean_excess_pct == 0.25
    assert "不能推翻" in evidence.summary


def test_research_evidence_marks_non_positive_excess_as_not_passed():
    from types import SimpleNamespace

    from app.api.realtime import _research_evidence

    report = SimpleNamespace(
        control="none",
        generated_at="2026-09-13T12:00:00",
        config=SimpleNamespace(primary_horizon=3),
        horizons=[SimpleNamespace(horizon=3, mean_excess_pct=-0.39)],
    )
    service = SimpleNamespace(
        report=report,
        status=lambda: {"state": "done"},
    )

    evidence = _research_evidence(service)

    assert evidence.status == "not_passed"
    assert evidence.recommendation_allowed is False
    assert "-0.390%" in evidence.summary
