"""前复权（qfq）因子与全市场回填的测试。

覆盖四块：
1. 解析：只认 category=1、每 10 股 → 每股、同日合并、earliest 过滤；
2. 定价公式：纯派现 / 送转 / 配股 / 三者的组合，以及「前复权价连续」这一硬约束；
3. 序列：最新交易日因子恒为 1、因子随日期单调、未来事件与不可解事件的处理；
4. 回填服务：写库正确、幂等、失败时绝不写「假前复权」、不碰不复权数据。
"""
from __future__ import annotations

import asyncio
import math
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import HistoricalBar
from app.database.session import Base
from app.history.adjust import (
    XdxrEvent,
    adjustment_factor,
    compute_qfq_factors,
    ex_reference_price,
    parse_xdxr_events,
)
from app.history.qfq_backfill import QFQ_SOURCE, QfqBackfillService
from app.history.qfq_service import QfqBackfillRunner
from app.realtime.screener import (
    FALLBACK_BAR_ADJUST,
    PREFERRED_BAR_ADJUST,
    ScreenerService,
    resolve_bar_adjust,
)

DAY0 = date(2026, 6, 1)


def _days(count: int) -> list[date]:
    return [DAY0 + timedelta(days=index) for index in range(count)]


def _row(category: int, year: int, month: int, day: int, **kwargs: object) -> dict:
    row = {
        "category": category,
        "year": year,
        "month": month,
        "day": day,
        "fenhong": 0.0,
        "songzhuangu": 0.0,
        "peigu": 0.0,
        "peigujia": 0.0,
    }
    row.update(kwargs)
    return row


# ─────────────────── 1. 解析 ───────────────────


def test_parse_converts_per_ten_shares_to_per_share() -> None:
    events = parse_xdxr_events(
        [_row(1, 2026, 6, 3, fenhong=5.0, songzhuangu=2.0, peigu=1.0, peigujia=8.0)]
    )
    assert len(events) == 1
    event = events[0]
    assert event.trade_date == date(2026, 6, 3)
    assert event.cash_per_share == pytest.approx(0.5)
    assert event.share_ratio == pytest.approx(0.2)
    assert event.rights_ratio == pytest.approx(0.1)
    assert event.rights_price == pytest.approx(8.0)


def test_parse_keeps_only_ex_dividend_category() -> None:
    rows = [
        _row(5, 2026, 6, 1, panqianliutong=100.0),
        _row(1, 2026, 6, 3, fenhong=10.0),
        _row(2, 2026, 6, 4, fenhong=10.0),
    ]
    events = parse_xdxr_events(rows)
    assert [event.trade_date for event in events] == [date(2026, 6, 3)]


def test_parse_merges_same_day_records() -> None:
    rows = [
        _row(1, 2026, 6, 3, fenhong=10.0, peigu=2.0, peigujia=5.0),
        _row(1, 2026, 6, 3, fenhong=2.0, songzhuangu=4.0, peigu=1.0, peigujia=8.0),
    ]
    events = parse_xdxr_events(rows)
    assert len(events) == 1
    event = events[0]
    assert event.cash_per_share == pytest.approx(1.2)
    assert event.share_ratio == pytest.approx(0.4)
    assert event.rights_ratio == pytest.approx(0.3)
    # 配股价按配股数加权：(0.2*5 + 0.1*8) / 0.3
    assert event.rights_price == pytest.approx((0.2 * 5.0 + 0.1 * 8.0) / 0.3)


def test_parse_drops_zero_equity_records() -> None:
    assert parse_xdxr_events([_row(1, 2026, 6, 3)]) == []


def test_parse_drops_events_before_earliest() -> None:
    rows = [_row(1, 2026, 5, 1, fenhong=10.0), _row(1, 2026, 6, 3, fenhong=10.0)]
    events = parse_xdxr_events(rows, earliest=date(2026, 5, 1))
    assert [event.trade_date for event in events] == [date(2026, 6, 3)]


def test_parse_skips_impossible_calendar_dates() -> None:
    assert parse_xdxr_events([_row(1, 2026, 2, 30, fenhong=10.0)]) == []


# ─────────────────── 2. 定价公式 ───────────────────


def test_pure_cash_dividend_factor() -> None:
    event = XdxrEvent(trade_date=date(2026, 6, 3), cash_per_share=0.5)
    assert ex_reference_price(10.0, event) == pytest.approx(9.5)
    assert adjustment_factor(10.0, event) == pytest.approx(0.95)


def test_stock_dividend_factor() -> None:
    event = XdxrEvent(trade_date=date(2026, 6, 3), share_ratio=1.0)
    assert ex_reference_price(20.0, event) == pytest.approx(10.0)
    assert adjustment_factor(20.0, event) == pytest.approx(0.5)


def test_rights_issue_factor() -> None:
    event = XdxrEvent(
        trade_date=date(2026, 6, 3), rights_ratio=0.3, rights_price=5.0
    )
    expected_reference = (10.0 + 0.3 * 5.0) / 1.3
    assert ex_reference_price(10.0, event) == pytest.approx(expected_reference)
    assert adjustment_factor(10.0, event) == pytest.approx(expected_reference / 10.0)


def test_combined_event_factor() -> None:
    event = XdxrEvent(
        trade_date=date(2026, 6, 3),
        cash_per_share=0.5,
        share_ratio=0.5,
        rights_ratio=0.2,
        rights_price=4.0,
    )
    expected = (10.0 - 0.5 + 0.2 * 4.0) / (10.0 * (1.0 + 0.5 + 0.2))
    assert adjustment_factor(10.0, event) == pytest.approx(expected)


def test_invalid_inputs_return_neutral_factor() -> None:
    event = XdxrEvent(trade_date=date(2026, 6, 3), cash_per_share=999.0)
    assert ex_reference_price(0.0, event) == 0.0
    # 派现大于前收盘（脏数据）→ 参考价非正 → 不调整
    assert adjustment_factor(1.0, event) == 1.0


# ─────────────────── 3. 因子序列 ───────────────────


def test_qfq_series_is_continuous_across_a_cash_dividend() -> None:
    days = _days(3)
    closes = [10.0, 10.0, 9.5]
    events = parse_xdxr_events([_row(1, 2026, 6, 3, fenhong=5.0)])
    series = compute_qfq_factors(days, closes, events)
    assert series.factors == pytest.approx((0.95, 0.95, 1.0))
    adjusted = [close * factor for close, factor in zip(closes, series.factors)]
    assert adjusted == pytest.approx([9.5, 9.5, 9.5])
    assert series.applied_events == 1


def test_qfq_return_equals_total_return_with_dividend() -> None:
    days = _days(3)
    closes = [10.0, 10.0, 9.5]
    events = parse_xdxr_events([_row(1, 2026, 6, 3, fenhong=5.0)])
    series = compute_qfq_factors(days, closes, events)
    adjusted = [close * factor for close, factor in zip(closes, series.factors)]
    qfq_return = adjusted[2] / adjusted[1] - 1.0
    total_return = (9.5 + 0.5) / 10.0 - 1.0
    assert qfq_return == pytest.approx(total_return)


def test_latest_bar_factor_is_always_one() -> None:
    days = _days(4)
    closes = [10.0, 10.0, 9.5, 9.6]
    events = parse_xdxr_events([_row(1, 2026, 6, 3, fenhong=5.0)])
    series = compute_qfq_factors(days, closes, events)
    assert series.factors[-1] == pytest.approx(1.0)


def test_factors_never_increase_over_time() -> None:
    days = _days(5)
    closes = [10.0, 10.0, 9.5, 9.6, 9.7]
    events = parse_xdxr_events(
        [_row(1, 2026, 6, 3, fenhong=5.0), _row(1, 2026, 6, 5, fenhong=1.0)]
    )
    factors = compute_qfq_factors(days, closes, events).factors
    assert all(left <= right + 1e-12 for left, right in zip(factors, factors[1:]))
    assert factors[-1] == pytest.approx(1.0)


def test_event_after_last_bar_is_pending_not_applied() -> None:
    days = _days(2)
    closes = [10.0, 10.0]
    event = XdxrEvent(trade_date=date(2026, 6, 10), cash_per_share=0.5)
    series = compute_qfq_factors(days, closes, [event])
    assert series.factors == pytest.approx((1.0, 1.0))
    assert series.applied_events == 0
    assert series.pending_events == 1


def test_event_without_earlier_bar_is_reported_unresolved() -> None:
    days = _days(2)
    closes = [10.0, 10.0]
    event = XdxrEvent(trade_date=days[0], cash_per_share=0.5)
    series = compute_qfq_factors(days, closes, [event])
    assert series.applied_events == 0
    assert series.unresolved_events == (event,)
    assert series.factors == pytest.approx((1.0, 1.0))


def test_no_events_means_all_factors_are_one() -> None:
    series = compute_qfq_factors(_days(3), [1.0, 2.0, 3.0], [])
    assert series.is_unadjusted
    assert series.factors == pytest.approx((1.0, 1.0, 1.0))


def test_compute_uses_calendar_order_not_input_order() -> None:
    days = list(reversed(_days(3)))
    closes = list(reversed([10.0, 10.0, 9.5]))
    events = parse_xdxr_events([_row(1, 2026, 6, 3, fenhong=5.0)])
    series = compute_qfq_factors(days, closes, events)
    assert series.days == tuple(_days(3))
    assert series.factors == pytest.approx((0.95, 0.95, 1.0))


def test_compute_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError):
        compute_qfq_factors(_days(3), [1.0, 2.0], [])


# ─────────────────── 4. 回填服务 ───────────────────


class _FakeApi:
    """最小可用的 tdxpy 替身：只实现回填用到的四个方法。"""

    def __init__(self, rows: dict, failing: set[str] | None = None) -> None:
        self._rows = rows
        self._failing = set(failing or ())
        self.connected_host: str | None = None
        self.disconnect_calls = 0

    def connect(self, host: str, port: int, time_out: float | None = None) -> bool:
        self.connected_host = host
        return True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected_host = None

    def get_security_quotes(self, pairs: list) -> list:
        return [{"price": 1.0}]

    def get_xdxr_info(self, market: int, symbol: str) -> list:
        if symbol in self._failing:
            raise RuntimeError("模拟服务端断连")
        return list(self._rows.get(symbol, []))


def _make_session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _seed(
    factory,
    symbol: str,
    days: list[date],
    closes: list[float],
    adjust: str = "none",
) -> None:
    with factory() as db:
        for day, close in zip(days, closes):
            db.add(
                HistoricalBar(
                    symbol=symbol,
                    period="daily",
                    adjust=adjust,
                    trade_date=day,
                    open=Decimal(str(close)),
                    high=Decimal(str(close)),
                    low=Decimal(str(close)),
                    close=Decimal(str(close)),
                    volume=1000.0,
                    amount=100_000.0,
                    source="tdx",
                )
            )
        db.commit()


def _closes(factory, symbol: str, adjust: str) -> list[tuple[date, float]]:
    with factory() as db:
        rows = db.execute(
            select(HistoricalBar.trade_date, HistoricalBar.close)
            .where(
                HistoricalBar.symbol == symbol,
                HistoricalBar.adjust == adjust,
            )
            .order_by(HistoricalBar.trade_date)
        ).all()
    return [(row[0], float(row[1])) for row in rows]


def test_backfill_service_writes_adjusted_prices() -> None:
    factory = _make_session_factory()
    days = _days(3)
    _seed(factory, "600519", days, [10.0, 10.0, 9.5])
    api = _FakeApi({"600519": [_row(1, 2026, 6, 3, fenhong=5.0)]})
    service = QfqBackfillService(
        factory, api_factory=lambda: api, hosts=("fake",)
    )

    report = service.run()

    assert (report.symbols_ok, report.symbols_failed) == (1, 0)
    assert report.rows_written == 3
    assert report.events_applied == 1
    assert [close for _day, close in _closes(factory, "600519", "qfq")] == pytest.approx(
        [9.5, 9.5, 9.5]
    )
    with factory() as db:
        sources = set(
            db.execute(
                select(HistoricalBar.source).where(HistoricalBar.adjust == "qfq")
            ).scalars()
        )
    assert sources == {QFQ_SOURCE}


def test_backfill_does_not_touch_unadjusted_rows() -> None:
    factory = _make_session_factory()
    days = _days(3)
    _seed(factory, "600519", days, [10.0, 10.0, 9.5])
    api = _FakeApi({"600519": [_row(1, 2026, 6, 3, fenhong=5.0)]})
    QfqBackfillService(factory, api_factory=lambda: api, hosts=("fake",)).run()
    assert [close for _day, close in _closes(factory, "600519", "none")] == pytest.approx(
        [10.0, 10.0, 9.5]
    )


def test_backfill_is_idempotent() -> None:
    factory = _make_session_factory()
    days = _days(3)
    _seed(factory, "600519", days, [10.0, 10.0, 9.5])
    api = _FakeApi({"600519": [_row(1, 2026, 6, 3, fenhong=5.0)]})
    service = QfqBackfillService(factory, api_factory=lambda: api, hosts=("fake",))

    service.run()
    first = _closes(factory, "600519", "qfq")
    service.run()
    second = _closes(factory, "600519", "qfq")

    assert first == second
    with factory() as db:
        count = db.execute(
            select(HistoricalBar.id).where(HistoricalBar.adjust == "qfq")
        ).all()
    assert len(count) == 3


def test_backfill_failure_writes_nothing_for_that_symbol() -> None:
    factory = _make_session_factory()
    days = _days(3)
    _seed(factory, "600519", days, [10.0, 10.0, 9.5])
    api = _FakeApi({}, failing={"600519"})
    service = QfqBackfillService(
        factory, api_factory=lambda: api, hosts=("fake",)
    )

    report = service.run()

    assert report.symbols_failed == 1
    assert report.rows_written == 0
    # 关键回归：拿不到除权除息时不能写出「全 1.0 因子」的假前复权数据
    assert _closes(factory, "600519", "qfq") == []


def test_backfill_without_events_copies_raw_prices() -> None:
    factory = _make_session_factory()
    days = _days(3)
    _seed(factory, "000001", days, [8.0, 8.1, 8.2])
    api = _FakeApi({"000001": []})
    service = QfqBackfillService(
        factory, api_factory=lambda: api, hosts=("fake",)
    )

    report = service.run()

    assert report.symbols_ok == 1
    assert [close for _day, close in _closes(factory, "000001", "qfq")] == pytest.approx(
        [8.0, 8.1, 8.2]
    )


def test_backfill_skips_symbols_without_local_bars() -> None:
    factory = _make_session_factory()
    api = _FakeApi({})
    service = QfqBackfillService(
        factory, api_factory=lambda: api, hosts=("fake",)
    )

    report = service.run(symbols=["600519"])

    assert report.symbols_total == 1
    assert report.symbols_ok == 0
    assert report.symbols_empty == 1
    assert report.symbols_failed == 0
    assert report.rows_written == 0


def test_backfill_reports_unresolved_events_per_symbol() -> None:
    factory = _make_session_factory()
    days = _days(3)
    # 第一根 K 线收盘价是 0（脏数据）→ 事件日之前没有可用的前收盘价
    _seed(factory, "600519", days, [0.0, 10.0, 9.5])
    api = _FakeApi({"600519": [_row(1, 2026, 6, 2, fenhong=5.0)]})
    service = QfqBackfillService(
        factory, api_factory=lambda: api, hosts=("fake",)
    )

    report = service.run()

    assert report.symbols_ok == 1
    assert report.unresolved_symbols == (("600519", 1),)


def test_backfill_ignores_events_before_the_local_history() -> None:
    factory = _make_session_factory()
    days = _days(2)
    _seed(factory, "600519", days, [10.0, 10.0])
    # 事件日 = 第一根 K 线日期：它只会影响更早的 K 线，本地没有那段历史
    api = _FakeApi({"600519": [_row(1, 2026, 6, 1, fenhong=5.0)]})
    service = QfqBackfillService(
        factory, api_factory=lambda: api, hosts=("fake",)
    )

    report = service.run()

    assert report.symbols_ok == 1
    assert report.events_applied == 0
    assert report.unresolved_symbols == ()
    assert [close for _day, close in _closes(factory, "600519", "qfq")] == pytest.approx(
        [10.0, 10.0]
    )


def test_backfill_uses_latest_host_then_reuses_connection() -> None:
    factory = _make_session_factory()
    _seed(factory, "600519", _days(2), [10.0, 10.0])
    api = _FakeApi({"600519": []})
    service = QfqBackfillService(
        factory, api_factory=lambda: api, hosts=("fake",)
    )

    service.run()

    assert api.connected_host is None  # 收尾时断开
    assert api.disconnect_calls >= 1


def test_backfill_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        QfqBackfillService(workers=0)
    with pytest.raises(ValueError):
        QfqBackfillService(batch_size=0)


def test_adjustment_factor_is_one_for_tiny_previous_close() -> None:
    event = XdxrEvent(trade_date=date(2026, 6, 3), cash_per_share=0.1)
    assert math.isclose(adjustment_factor(0.0, event), 1.0)


# ─────────────────── 5. 复权口径选择 ───────────────────


def test_resolve_prefers_qfq_when_available() -> None:
    factory = _make_session_factory()
    _seed(factory, "600519", _days(3), [10.0, 10.0, 9.5])
    _seed(factory, "600519", _days(3), [9.5, 9.5, 9.5], adjust=PREFERRED_BAR_ADJUST)
    with factory() as db:
        adjust, _max_id, last_day = resolve_bar_adjust(db)
    assert adjust == PREFERRED_BAR_ADJUST
    assert last_day == _days(3)[-1]


def test_resolve_falls_back_when_qfq_missing() -> None:
    factory = _make_session_factory()
    _seed(factory, "600519", _days(3), [10.0, 10.0, 9.5])
    with factory() as db:
        adjust, max_id, last_day = resolve_bar_adjust(db)
    assert adjust == FALLBACK_BAR_ADJUST
    assert max_id > 0
    assert last_day == _days(3)[-1]


def test_resolve_falls_back_when_qfq_is_stale() -> None:
    factory = _make_session_factory()
    days = _days(3)
    _seed(factory, "600519", days, [10.0, 10.0, 9.5])
    # 前复权只回填了第一天：比不复权落后，必须回退，否则会拿更短的历史算因子
    _seed(factory, "600519", days[:1], [9.5], adjust=PREFERRED_BAR_ADJUST)
    with factory() as db:
        adjust, _max_id, last_day = resolve_bar_adjust(db)
    assert adjust == FALLBACK_BAR_ADJUST
    assert last_day == days[-1]


def test_resolve_handles_empty_database() -> None:
    factory = _make_session_factory()
    with factory() as db:
        adjust, max_id, last_day = resolve_bar_adjust(db)
    assert (adjust, max_id, last_day) == (FALLBACK_BAR_ADJUST, 0, None)


def test_resolve_uses_qfq_when_only_qfq_exists() -> None:
    factory = _make_session_factory()
    _seed(factory, "600519", _days(3), [9.5, 9.5, 9.5], adjust=PREFERRED_BAR_ADJUST)
    with factory() as db:
        adjust, _max_id, last_day = resolve_bar_adjust(db)
    assert adjust == PREFERRED_BAR_ADJUST
    assert last_day == _days(3)[-1]


def test_screener_series_loader_reports_the_adjust_used() -> None:
    factory = _make_session_factory()
    _seed(factory, "600519", _days(3), [10.0, 10.0, 9.5])
    _seed(factory, "600519", _days(3), [9.5, 9.5, 9.5], adjust=PREFERRED_BAR_ADJUST)
    service = ScreenerService(session_factory=factory)

    with factory() as db:
        series_map, last_day, adjust = service._load_series(db, 120)

    assert adjust == PREFERRED_BAR_ADJUST
    assert last_day == _days(3)[-1]
    # 读到的必须是复权后的价格
    assert series_map["600519"].closes == pytest.approx((9.5, 9.5, 9.5))


# ─────────────────── 6. 任务调度与 API ───────────────────


def test_runner_runs_backfill_and_exposes_status() -> None:
    factory = _make_session_factory()
    _seed(factory, "600519", _days(3), [10.0, 10.0, 9.5])
    api = _FakeApi({"600519": [_row(1, 2026, 6, 3, fenhong=5.0)]})
    runner = QfqBackfillRunner(
        QfqBackfillService(factory, api_factory=lambda: api, hosts=("fake",))
    )

    async def scenario() -> None:
        status = await runner.start(["600519"])
        assert status["state"] == "running"
        report = await runner.wait()
        assert report is not None
        assert report.symbols_ok == 1
        final = runner.status()
        assert final["state"] == "done"
        assert final["report"]["rows_written"] == 3

    asyncio.run(scenario())


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_api_history_adjust_status_and_start() -> None:
    from app.api.deps import get_qfq_backfill_runner
    from app.main import app

    started: dict[str, object] = {}

    class _FakeRunner:
        def status(self) -> dict:
            return {
                "state": "idle",
                "progress": {"done": 0, "total": 0},
                "started_at": None,
                "finished_at": None,
                "error": None,
                "has_report": False,
                "report": None,
            }

        async def start(self, symbols=None) -> dict:
            started["symbols"] = symbols
            return {**self.status(), "state": "running"}

    app.dependency_overrides[get_qfq_backfill_runner] = _FakeRunner
    try:
        client = _client()
        assert client.get("/api/history-adjust").json()["state"] == "idle"
        response = client.post("/api/history-adjust", params={"symbols": "600519, 000001"})
        assert response.status_code == 202
        assert started["symbols"] == ["600519", "000001"]
        assert client.post("/api/history-adjust", params={"symbols": "abc"}).status_code == 422
    finally:
        app.dependency_overrides.pop(get_qfq_backfill_runner, None)
