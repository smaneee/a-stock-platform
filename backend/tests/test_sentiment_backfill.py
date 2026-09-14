"""历史涨停情绪离线回算测试：用合成日线覆盖正常、边界与异常输入。

不依赖网络与真实数据；所有涨跌停价都能手算核对。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.database.models import HistoricalBar, LimitUpSentiment, Security, TradingDate
from app.database.session import SessionLocal
from app.main import app
from app.market_data.sentiment_backfill import (
    DERIVED_SOURCE,
    SentimentBackfillService,
)

JAN_05 = date(2026, 1, 5)
JAN_06 = date(2026, 1, 6)
JAN_07 = date(2026, 1, 7)
JAN_08 = date(2026, 1, 8)
JAN_09 = date(2026, 1, 9)
WEEK = [JAN_05, JAN_06, JAN_07, JAN_08, JAN_09]

# 合成日线的收盘价都能手算涨跌停：
#   600001 主板 10%：首板 -> 二连板 -> 断板 -> 再首板
#   600002 主板 10%：炸板 -> 首板 -> 断板 -> 首板
#   300001 创业板 20%：首板 -> 断板 -> 首板
#   600003 ST 5%：默认整只剔除
_STANDARD_BARS: dict[str, list[tuple[date, float, float | None]]] = {
    "600001": [
        (JAN_05, 10.00, None),
        (JAN_06, 11.00, None),
        (JAN_07, 12.10, None),
        (JAN_08, 12.20, 12.30),
        (JAN_09, 13.42, None),
    ],
    "600002": [
        (JAN_05, 10.00, None),
        (JAN_06, 10.50, 11.00),
        (JAN_07, 11.55, None),
        (JAN_08, 11.60, 11.70),
        (JAN_09, 12.76, None),
    ],
    "300001": [
        (JAN_05, 10.00, None),
        (JAN_06, 12.00, None),
        (JAN_07, 12.50, 12.60),
        (JAN_08, 15.00, None),
        (JAN_09, 15.10, None),
    ],
    "600003": [
        (JAN_05, 10.00, None),
        (JAN_06, 10.50, None),
    ],
}


def _seed_calendar(db, days: list[date]) -> None:
    for day in days:
        db.add(TradingDate(trade_date=day))
    db.commit()


def _seed_security(
    db, symbol: str, *, name: str | None = None, is_st: bool = False
) -> None:
    db.add(
        Security(
            symbol=symbol,
            name=name or f"测试{symbol}",
            exchange="SH",
            board="main",
            is_st=is_st,
            trading_status="active",
        )
    )
    db.commit()


def _seed_bar(
    db, symbol: str, trade_date: date, *, close: float, high: float | None = None
) -> None:
    close_d = Decimal(str(close))
    high_d = Decimal(str(high if high is not None else close))
    db.add(
        HistoricalBar(
            symbol=symbol,
            period="daily",
            adjust="none",
            trade_date=trade_date,
            open=close_d,
            high=high_d,
            low=close_d,
            close=close_d,
            volume=100_000,
            amount=1_000_000,
            source="test",
            fetched_at=datetime.combine(trade_date, datetime.min.time()),
        )
    )
    db.commit()


def _seed_standard_market(db) -> None:
    _seed_calendar(db, WEEK)
    _seed_security(db, "600001")
    _seed_security(db, "600002")
    _seed_security(db, "300001")
    _seed_security(db, "600003", name="ST测试", is_st=True)
    for symbol, bars in _STANDARD_BARS.items():
        for trade_date, close, high in bars:
            _seed_bar(db, symbol, trade_date, close=close, high=high)


def _compute_week(db, **kwargs) -> dict:
    result = SentimentBackfillService(db).compute(
        JAN_05, JAN_09, coverage_floor=kwargs.pop("coverage_floor", 1), **kwargs
    )
    return {item.trade_date: item for item in result.items}


class TestComputation:
    def test_counts_seal_rate_splits_and_streaks(self, db_session):
        _seed_standard_market(db_session)
        by_day = _compute_week(db_session)

        # 首个交易日没有上一收盘价，整日不产出
        assert JAN_05 not in by_day

        day6 = by_day[JAN_06]
        assert day6.limit_up_count == 2
        assert day6.broken_board_count == 1
        assert day6.coverage_symbols == 3
        assert day6.seal_rate == pytest.approx(2 / 3)
        assert day6.broken_rate == pytest.approx(1 / 3)
        assert day6.max_streak == 1
        assert day6.first_board_count == 2
        assert day6.streak_2_count == 0

        day7 = by_day[JAN_07]
        assert day7.limit_up_count == 2
        assert day7.broken_board_count == 0
        assert day7.max_streak == 2
        assert day7.first_board_count == 1
        assert day7.streak_2_count == 1

        day8 = by_day[JAN_08]
        assert day8.limit_up_count == 1
        assert day8.max_streak == 1

        day9 = by_day[JAN_09]
        assert day9.limit_up_count == 2
        assert day9.first_board_count == 2

    def test_st_excluded_by_default_and_optional(self, db_session):
        _seed_standard_market(db_session)
        excluded = _compute_week(db_session)
        included = _compute_week(db_session, include_st=True)

        assert excluded[JAN_06].limit_up_count == 2
        assert excluded[JAN_06].coverage_symbols == 3
        assert included[JAN_06].limit_up_count == 3
        assert included[JAN_06].coverage_symbols == 4

    def test_suspension_breaks_streak(self, db_session):
        _seed_calendar(db_session, WEEK)
        _seed_security(db_session, "600009")
        _seed_bar(db_session, "600009", JAN_05, close=10.00)
        _seed_bar(db_session, "600009", JAN_06, close=11.00)
        # JAN_07 停牌：没有日线
        _seed_bar(db_session, "600009", JAN_08, close=12.10)

        by_day = _compute_week(db_session)

        assert by_day[JAN_06].max_streak == 1
        assert by_day[JAN_08].limit_up_count == 1
        # 上一根日线隔了一个交易日，连板被停牌打断
        assert by_day[JAN_08].max_streak == 1

    def test_limit_down_counted_separately(self, db_session):
        _seed_calendar(db_session, WEEK)
        _seed_security(db_session, "600010")
        _seed_bar(db_session, "600010", JAN_05, close=10.00)
        _seed_bar(db_session, "600010", JAN_06, close=9.00)

        day6 = _compute_week(db_session)[JAN_06]

        assert day6.limit_down_count == 1
        assert day6.limit_up_count == 0
        assert day6.broken_board_count == 0
        assert day6.seal_rate is None

    def test_low_coverage_days_are_reported_not_written(self, db_session):
        _seed_standard_market(db_session)
        result = SentimentBackfillService(db_session).compute(
            JAN_05, JAN_09, coverage_floor=100
        )

        assert result.items == []
        assert JAN_06 in result.low_coverage_days

    def test_rejects_reversed_range(self, db_session):
        _seed_standard_market(db_session)
        with pytest.raises(ValueError):
            SentimentBackfillService(db_session).compute(JAN_09, JAN_05)


class TestBackfillPersistence:
    def test_inserts_derived_rows(self, db_session):
        _seed_standard_market(db_session)

        report = SentimentBackfillService(db_session).backfill(
            JAN_05, JAN_09, coverage_floor=1
        )

        assert (report.inserted, report.updated, report.skipped_existing) == (4, 0, 0)
        assert report.deleted_stale == 0
        row = db_session.get(LimitUpSentiment, JAN_06)
        assert row is not None
        assert row.source == DERIVED_SOURCE
        assert row.limit_up_count == 2
        assert row.broken_board_count == 1
        assert row.coverage_symbols == 3
        assert row.seal_rate == pytest.approx(2 / 3)
        # 日线口径没有封单金额与强势/次新口径
        assert row.total_seal_amount == 0.0
        assert row.strong_count == 0
        assert row.sub_new_count == 0

    def test_keeps_eastmoney_rows_untouched(self, db_session):
        _seed_standard_market(db_session)
        db_session.add(
            LimitUpSentiment(
                trade_date=JAN_06, limit_up_count=99, source="eastmoney"
            )
        )
        db_session.commit()

        report = SentimentBackfillService(db_session).backfill(
            JAN_05, JAN_09, coverage_floor=1, overwrite_derived=True
        )

        assert report.inserted == 3
        assert report.skipped_existing == 1
        row = db_session.get(LimitUpSentiment, JAN_06)
        assert row.limit_up_count == 99
        assert row.source == "eastmoney"

    def test_derived_rows_need_overwrite(self, db_session):
        _seed_standard_market(db_session)
        db_session.add(
            LimitUpSentiment(trade_date=JAN_06, limit_up_count=7, source=DERIVED_SOURCE)
        )
        db_session.commit()

        kept = SentimentBackfillService(db_session).backfill(
            JAN_05, JAN_09, coverage_floor=1
        )
        assert kept.updated == 0
        assert db_session.get(LimitUpSentiment, JAN_06).limit_up_count == 7

        replaced = SentimentBackfillService(db_session).backfill(
            JAN_05, JAN_09, coverage_floor=1, overwrite_derived=True
        )
        # 第一次调用已把 01-07/08/09 写成回算行，所以第二次覆盖 4 天
        assert replaced.updated == 4
        assert db_session.get(LimitUpSentiment, JAN_06).limit_up_count == 2

    def test_dry_run_writes_nothing(self, db_session):
        _seed_standard_market(db_session)

        report = SentimentBackfillService(db_session).backfill(
            JAN_05, JAN_09, coverage_floor=1, dry_run=True
        )

        assert report.dry_run is True
        assert report.inserted == 4
        assert db_session.get(LimitUpSentiment, JAN_06) is None

    def test_removes_stale_derived_rows(self, db_session):
        _seed_standard_market(db_session)
        db_session.add(
            LimitUpSentiment(trade_date=JAN_05, limit_up_count=7, source=DERIVED_SOURCE)
        )
        db_session.commit()

        report = SentimentBackfillService(db_session).backfill(
            JAN_05, JAN_09, coverage_floor=1
        )

        assert report.stale_days == [JAN_05]
        assert report.deleted_stale == 1
        assert db_session.get(LimitUpSentiment, JAN_05) is None

    def test_raises_without_local_bars(self, db_session):
        _seed_calendar(db_session, WEEK)
        with pytest.raises(ValueError):
            SentimentBackfillService(db_session).backfill()


class TestBackfillApi:
    url = "/api/market/limit-up/sentiment/backfill"

    def test_returns_report_and_persists(self):
        with SessionLocal() as db:
            _seed_standard_market(db)

        response = TestClient(app).post(
            self.url,
            json={"start": "2026-01-05", "end": "2026-01-09", "coverage_floor": 1},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["inserted"] == 4
        assert body["days"] == 4
        assert body["dry_run"] is False
        assert body["include_st"] is False
        first = body["items"][0]
        assert first["trade_date"] == JAN_06.isoformat()
        assert first["limit_up_count"] == 2
        assert first["seal_rate"] == pytest.approx(2 / 3)
        with SessionLocal() as db:
            assert db.get(LimitUpSentiment, JAN_06) is not None

    def test_dry_run_does_not_persist(self):
        with SessionLocal() as db:
            _seed_standard_market(db)

        response = TestClient(app).post(
            self.url,
            json={
                "start": "2026-01-05",
                "end": "2026-01-09",
                "coverage_floor": 1,
                "dry_run": True,
            },
        )

        assert response.status_code == 200
        assert response.json()["inserted"] == 4
        with SessionLocal() as db:
            assert db.get(LimitUpSentiment, JAN_06) is None

    def test_rejects_reversed_range(self):
        with SessionLocal() as db:
            _seed_standard_market(db)

        response = TestClient(app).post(
            self.url, json={"start": "2026-01-09", "end": "2026-01-05"}
        )

        assert response.status_code == 422

    def test_rejects_out_of_range_coverage_floor(self):
        response = TestClient(app).post(self.url, json={"coverage_floor": 10_000_000})
        assert response.status_code == 422
