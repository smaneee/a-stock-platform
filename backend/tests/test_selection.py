"""Selection ranking regression tests: normal, boundary, error and PIT safety."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.database.models import HistoricalBar, Security, UniverseMember, UniverseSnapshot
from app.selection import (
    SelectionConfig,
    SelectionError,
    SelectionEvaluationService,
    SelectionService,
)


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


def _seed_future_bars(
    db,
    symbol: str,
    trading_day: date,
    *,
    entry_open: float,
    exit_close: float,
    count: int = 20,
):
    for offset in range(1, count + 1):
        current = trading_day + timedelta(days=offset)
        close = exit_close if offset == count else entry_open
        db.add(
            HistoricalBar(
                symbol=symbol,
                period="daily",
                adjust="none",
                trade_date=current,
                open=Decimal(str(entry_open)),
                high=Decimal(str(max(entry_open, close))),
                low=Decimal(str(min(entry_open, close))),
                close=Decimal(str(close)),
                volume=100_000,
                amount=1_000_000,
                source="future-test",
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

    def test_load_bars_is_column_based_capped_and_point_in_time(self, db_session):
        """_load_bars：只读所需列、按日期升序、截到 lookback_days、不含未来数据。

        全市场一次排名要读约 127 万行，用列查询替代 ORM 实体构造后实测
        36.9s -> 12.9s，且排名结果与优化前逐字段一致。
        """
        trading_day = date(2026, 3, 31)
        _seed_snapshot(db_session, trading_day, ["600001"])
        _seed_bars(db_session, "600001", trading_day, daily_growth=0.001, count=300)
        _seed_future_bars(
            db_session, "600001", trading_day, entry_open=20.0, exit_close=25.0
        )

        bars = SelectionService(db_session)._load_bars(
            ["600001"], trading_day, SelectionConfig(lookback_days=180)
        )["600001"]

        assert len(bars) == 180
        dates = [bar.trade_date for bar in bars]
        assert dates == sorted(dates)
        assert dates[-1] == trading_day
        assert all(day <= trading_day for day in dates)
        # close / amount / fetched_at 由列查询直接给出，仍可正常读取
        assert bars[-1].close is not None
        assert bars[-1].amount is not None
        assert bars[-1].fetched_at is not None
        # 性能回归保护：不再 hydrate 成 ORM 实体
        assert not isinstance(bars[0], HistoricalBar)

    def test_missing_snapshot_is_explicit_error(self, db_session):
        with pytest.raises(SelectionError, match="没有股票池快照"):
            SelectionService(db_session).rank(date(2026, 1, 1))


class TestSelectionEvaluation:
    def test_uses_next_day_open_and_future_horizon_close(self, db_session):
        trading_day = date(2026, 2, 1)
        _seed_snapshot(db_session, trading_day, ["600001", "600002"])
        for symbol in ["600001", "600002"]:
            _seed_bars(db_session, symbol, trading_day, daily_growth=0.001)
        run = SelectionService(db_session).rank(
            trading_day, SelectionConfig(top_n=2)
        )
        _seed_future_bars(
            db_session, "600001", trading_day, entry_open=10, exit_close=12
        )
        _seed_future_bars(
            db_session, "600002", trading_day, entry_open=20, exit_close=18
        )

        result = SelectionEvaluationService(db_session).evaluate(run.run_id)

        assert result.evaluation_horizon == 20
        assert result.evaluation_coverage == 1.0
        assert result.mean_forward_return == pytest.approx(0.05)
        assert result.median_forward_return == pytest.approx(0.05)
        assert result.forward_win_rate == 0.5
        evaluated = {item.symbol: item for item in result.candidates}
        assert evaluated["600001"].entry_date == trading_day + timedelta(days=1)
        assert evaluated["600001"].exit_date == trading_day + timedelta(days=20)
        assert evaluated["600001"].forward_return == pytest.approx(0.2)
        assert evaluated["600002"].forward_return == pytest.approx(-0.1)

    def test_insufficient_coverage_does_not_mutate_run(self, db_session):
        trading_day = date(2026, 2, 1)
        _seed_snapshot(db_session, trading_day, ["600001", "600002"])
        for symbol in ["600001", "600002"]:
            _seed_bars(db_session, symbol, trading_day, daily_growth=0.001)
        run = SelectionService(db_session).rank(
            trading_day, SelectionConfig(top_n=2)
        )
        _seed_future_bars(
            db_session, "600001", trading_day, entry_open=10, exit_close=12
        )

        with pytest.raises(SelectionError, match="覆盖率"):
            SelectionEvaluationService(db_session).evaluate(run.run_id)

        unchanged = SelectionService(db_session).get_run(run.run_id)
        assert unchanged.evaluated_at is None
        assert all(item.forward_return is None for item in unchanged.candidates)

    @pytest.mark.parametrize("horizon", [0, 121])
    def test_rejects_invalid_horizon(self, db_session, horizon):
        with pytest.raises(SelectionError, match="horizon_days"):
            SelectionEvaluationService(db_session).evaluate(
                999, horizon_days=horizon
            )

    def test_summarizes_multiple_runs_with_rank_ic_and_turnover(self, db_session):
        first_day = date(2026, 1, 1)
        second_day = date(2026, 2, 1)
        run_ids = []
        cases = [
            (first_day, ["600001", "600002"], [12.0, 9.0]),
            (second_day, ["600003", "600004"], [9.0, 12.0]),
        ]
        for trading_day, symbols, exits in cases:
            _seed_snapshot(db_session, trading_day, symbols)
            _seed_bars(db_session, symbols[0], trading_day, daily_growth=0.002)
            _seed_bars(db_session, symbols[1], trading_day, daily_growth=0.001)
            run = SelectionService(db_session).rank(
                trading_day, SelectionConfig(top_n=2)
            )
            for symbol, exit_close in zip(symbols, exits):
                _seed_future_bars(
                    db_session,
                    symbol,
                    trading_day,
                    entry_open=10,
                    exit_close=exit_close,
                )
            SelectionEvaluationService(db_session).evaluate(run.run_id)
            run_ids.append(run.run_id)

        summary = SelectionEvaluationService(db_session).summarize()

        assert len(run_ids) == 2
        assert summary.evaluated_runs == 2
        assert summary.candidate_observations == 4
        assert summary.average_rank_ic == pytest.approx(0.0)
        assert summary.average_turnover == 1.0
        assert summary.average_coverage == 1.0

    def test_empty_summary_is_explicit_zero_state(self, db_session):
        summary = SelectionEvaluationService(db_session).summarize()
        assert summary.evaluated_runs == 0
        assert summary.average_rank_ic is None
        assert summary.average_turnover is None


def test_selection_routes_registered():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "post" in paths["/api/selections/rank"]
    assert "get" in paths["/api/selections"]
    assert "get" in paths["/api/selections/{run_id}"]
    assert "post" in paths["/api/selections/{run_id}/evaluate"]
    assert "get" in paths["/api/selections/evaluations/summary"]
