"""Paper rebalance workflow regression tests."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database.models import PaperAccount, PaperPosition
from app.paper_trading.rebalance import PaperRebalanceService, RebalanceError
from app.selection import (
    SelectionConfig,
    SelectionEvaluationService,
    SelectionService,
)
from tests.helpers import make_quote
from tests.test_selection import _seed_bars, _seed_future_bars, _seed_snapshot


def _seed_account_and_run(db_session):
    trading_day = date(2026, 2, 1)
    symbols = ["600001", "600002"]
    _seed_snapshot(db_session, trading_day, symbols)
    _seed_bars(db_session, symbols[0], trading_day, daily_growth=0.002)
    _seed_bars(db_session, symbols[1], trading_day, daily_growth=0.001)
    run = SelectionService(db_session).rank(
        trading_day, SelectionConfig(top_n=2)
    )
    account = PaperAccount(
        name="自动模拟账户",
        initial_cash=Decimal("100000"),
        available_cash=Decimal("100000"),
        frozen_cash=Decimal("0"),
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    quotes = {symbol: make_quote(symbol=symbol, price=10) for symbol in symbols}
    return account, run, quotes


def test_validation_gate_blocks_unproven_strategy(db_session):
    account, run, quotes = _seed_account_and_run(db_session)

    with pytest.raises(RebalanceError, match="历史验证未达标"):
        PaperRebalanceService(db_session).create_plan(
            account.id, run.run_id, quotes
        )


def test_validation_gate_ignores_different_selection_config(db_session):
    for index in range(3):
        trading_day = date(2025, 10 + index, 1)
        symbol = f"6010{index:02d}"
        _seed_snapshot(db_session, trading_day, [symbol])
        _seed_bars(db_session, symbol, trading_day, daily_growth=0.002)
        historical_run = SelectionService(db_session).rank(
            trading_day, SelectionConfig(top_n=1)
        )
        _seed_future_bars(
            db_session, symbol, trading_day, entry_open=10, exit_close=12
        )
        SelectionEvaluationService(db_session).evaluate(historical_run.run_id)
    account, current_run, quotes = _seed_account_and_run(db_session)

    with pytest.raises(RebalanceError, match="历史验证未达标"):
        PaperRebalanceService(db_session).create_plan(
            account.id, current_run.run_id, quotes
        )


def test_override_creates_idempotent_lot_sized_draft(db_session):
    account, run, quotes = _seed_account_and_run(db_session)
    service = PaperRebalanceService(db_session)

    first = service.create_plan(
        account.id, run.run_id, quotes, validation_override=True
    )
    second = service.create_plan(
        account.id, run.run_id, quotes, validation_override=True
    )

    assert first.id == second.id
    assert first.status == "DRAFT"
    assert first.proposal_json["validation"]["override"] is True
    assert len(first.proposal_json["orders"]) == 2
    assert all(item["side"] == "BUY" for item in first.proposal_json["orders"])
    assert all(item["quantity"] % 100 == 0 for item in first.proposal_json["orders"])


def test_explicit_execute_fills_once(db_session):
    account, run, quotes = _seed_account_and_run(db_session)
    service = PaperRebalanceService(db_session)
    plan = service.create_plan(
        account.id, run.run_id, quotes, validation_override=True
    )

    executed = service.execute_plan(plan.id, quotes)

    assert executed.status == "EXECUTED"
    assert executed.execution_json["filled"] == 2
    positions = db_session.scalars(
        select(PaperPosition).where(PaperPosition.account_id == account.id)
    ).all()
    assert {position.symbol for position in positions} == {"600001", "600002"}
    with pytest.raises(RebalanceError, match="不可执行"):
        service.execute_plan(plan.id, quotes)


def test_stale_quote_rejected_before_draft(db_session):
    account, run, quotes = _seed_account_and_run(db_session)
    quotes["600001"] = make_quote("600001", is_stale=True)

    with pytest.raises(RebalanceError, match="有效实时行情"):
        PaperRebalanceService(db_session).create_plan(
            account.id, run.run_id, quotes, validation_override=True
        )


def test_expired_plan_never_places_orders(db_session):
    account, run, quotes = _seed_account_and_run(db_session)
    service = PaperRebalanceService(db_session)
    plan = service.create_plan(
        account.id, run.run_id, quotes, validation_override=True
    )
    plan.created_at -= timedelta(minutes=16)
    db_session.commit()

    with pytest.raises(RebalanceError, match="超过 15 分钟"):
        service.execute_plan(plan.id, quotes)

    assert plan.status == "EXPIRED"
    assert db_session.scalars(select(PaperPosition)).all() == []


def test_rebalance_routes_registered():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "post" in paths["/api/paper/rebalance-plans"]
    assert "post" in paths["/api/paper/rebalance-plans/{plan_id}/execute"]
    assert "post" in paths["/api/paper/rebalance-plans/{plan_id}/cancel"]


def test_portfolio_snapshot_aggregates_multiple_lots(db_session):
    from app.paper_trading.portfolio import PortfolioService

    account = PaperAccount(
        name="分批账户",
        initial_cash=Decimal("100000"),
        available_cash=Decimal("97000"),
        frozen_cash=Decimal("0"),
    )
    db_session.add(account)
    db_session.flush()
    db_session.add_all(
        [
            PaperPosition(
                account_id=account.id,
                symbol="600001",
                quantity=100,
                available_quantity=100,
                avg_cost=Decimal("10"),
            ),
            PaperPosition(
                account_id=account.id,
                symbol="600001",
                quantity=200,
                available_quantity=200,
                avg_cost=Decimal("10"),
            ),
        ]
    )
    db_session.commit()

    snapshot = PortfolioService(db_session).calculate_snapshot(
        account, {"600001": make_quote("600001", price=10)}
    )

    assert snapshot.current_position_value["600001"] == 3000
    assert snapshot.total_asset == 100000
