"""Selection ranking regression tests: normal, boundary, error and PIT safety."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.database.models import HistoricalBar, Security, UniverseMember, UniverseSnapshot
from app.selection import SelectionConfig, SelectionError, SelectionService


def _seed_snapshot(db, trading_day: date, symbols: list[str], *, st_symbol: str | None = None):
    snapshot = UniverseSnapshot(
        trading_day=trading_day,
        total_count=len(symbols),
        included_count=len(symbols),
        excluded_count=0,
        source_provider="test",
        source_synced_at=datetime.combine(trading_day, datetime.min.time()),
    )
    db.add(snapshot)
    db.flush()
    for rank, symbol in enumerate(symbols):
        is_st = symbol == st_symbol
        db.add(
            Security(
                symbol=symbol,
                name=("ST " if is_st else "") + symbol,
                exchange="SH",
                board="sh_main",
                is_st=is_st,
                trading_status="active",
            )
        )
        db.add(
            UniverseMember(
                snapshot_id=snapshot.id,
                symbol=symbol,
                security_id=symbol,
                is_included=True,
                sort_rank=rank,
                name=symbol,
                exchange="SH",
                board="sh_main",
                is_st=is_st,
                trading_status="active",
            )
        )
    db.commit()
    return snapshot


def _seed_bars(
    db,
    symbol: str,
    trading_day: date,
    *,
    daily_growth: float,
    amount: float = 1_000_000,
    count: int = 80,
):
    start = trading_day - timedelta(days=count - 1)
    price = 10.0
    for offset in range(count):
        current = start + timedelta(days=offset)
        price *= 1 + daily_growth
        db.add(
            HistoricalBar(
                symbol=symbol,
                period="daily",
                adjust="none",
                trade_date=current,
                open=Decimal(str(price)),
                high=Decimal(str(price * 1.01)),
                low=Decimal(str(price * 0.99)),
                close=Decimal(str(price)),
                volume=100_000,
                amount=amount,
                source="test",
                fetched_at=datetime.combine(current, datetime.min.time()),
            )
        )
    db.commit()


class TestSelectionService:
    def test_ranks_momentum_and_persists_result(self, db_session):
        trading_day = date(2026, 1, 31)
        symbols = ["600001", "600002", "600003"]
        _seed_snapshot(db_session, trading_day, symbols)
        _seed_bars(db_session, symbols[0], trading_day, daily_growth=0.004)
        _seed_bars(db_session, symbols[1], trading_day, daily_growth=0.002)
        _seed_bars(db_session, symbols[2], trading_day, daily_growth=0.001)

        result = SelectionService(db_session).rank(
            trading_day, SelectionConfig(top_n=2)
        )

        assert result.total_candidates == 3
        assert result.eligible_count == 3
        assert [item.symbol for item in result.candidates] == ["600001", "600002"]
        assert [item.rank for item in result.candidates] == [1, 2]
        assert result.candidates[0].score >= result.candidates[1].score
        assert SelectionService(db_session).get_run(result.run_id) == result

    def test_same_snapshot_config_and_data_is_idempotent(self, db_session):
        trading_day = date(2026, 2, 1)
        _seed_snapshot(db_session, trading_day, ["600001"])
        _seed_bars(db_session, "600001", trading_day, daily_growth=0.001)
        service = SelectionService(db_session)

        first = service.rank(trading_day)
        second = service.rank(trading_day)

        assert first.run_id == second.run_id
        assert first == second

    def test_future_bar_is_never_used(self, db_session):
        trading_day = date(2026, 2, 1)
        _seed_snapshot(db_session, trading_day, ["600001"])
        _seed_bars(db_session, "600001", trading_day, daily_growth=0.001)
        db_session.add(
            HistoricalBar(
                symbol="600001",
                period="daily",
                adjust="none",
                trade_date=trading_day + timedelta(days=1),
                open=999,
                high=999,
                low=999,
                close=999,
                volume=1,
                amount=1,
                source="future-test",
            )
        )
        db_session.commit()

        result = SelectionService(db_session).rank(trading_day)

        assert result.candidates[0].last_price < 999
        assert result.candidates[0].bar_count == 80

    def test_excludes_st_by_default(self, db_session):
        trading_day = date(2026, 2, 1)
        _seed_snapshot(
            db_session, trading_day, ["600001", "600002"], st_symbol="600001"
        )
        _seed_bars(db_session, "600001", trading_day, daily_growth=0.01)
        _seed_bars(db_session, "600002", trading_day, daily_growth=0.001)

        result = SelectionService(db_session).rank(trading_day)

        assert [item.symbol for item in result.candidates] == ["600002"]
        assert result.total_candidates == 1

    def test_rejects_missing_or_stale_history(self, db_session):
        trading_day = date(2026, 2, 1)
        _seed_snapshot(db_session, trading_day, ["600001"])
        _seed_bars(
            db_session,
            "600001",
            trading_day - timedelta(days=20),
            daily_growth=0.001,
        )

        with pytest.raises(SelectionError, match="history ingest|历史数据"):
            SelectionService(db_session).rank(trading_day)

    @pytest.mark.parametrize(
        "config",
        [
            SelectionConfig(top_n=0),
            SelectionConfig(min_bars=60),
            SelectionConfig(min_bars=100, lookback_days=80),
            SelectionConfig(adjust="invalid"),
        ],
    )
    def test_rejects_invalid_config(self, config):
        with pytest.raises(SelectionError):
            config.validate()

    def test_missing_snapshot_is_explicit_error(self, db_session):
        with pytest.raises(SelectionError, match="没有股票池快照"):
            SelectionService(db_session).rank(date(2026, 1, 1))


def test_selection_routes_registered():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "post" in paths["/api/selections/rank"]
    assert "get" in paths["/api/selections/{run_id}"]
