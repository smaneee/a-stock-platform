"""Real-money plan approval and execution safety tests."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database.models import LiveRebalancePlan
from app.live_trading.qmt_broker import (
    LiveAccountSnapshot,
    LiveOrderStatus,
    LivePosition,
)
from app.live_trading.reconcile_worker import LiveReconcileWorker
from app.live_trading.qmt_broker import QmtBrokerTimeout
from app.live_trading.rebalance import LiveRebalanceError, LiveRebalanceService
from app.selection import SelectionConfig, SelectionEvaluationService, SelectionService
from tests.helpers import make_quote
from tests.test_selection import _seed_bars, _seed_future_bars, _seed_snapshot


class _RecordingBroker:
    def __init__(self):
        self.orders = []

    async def place_limit_order(self, symbol, side, quantity, price, remark):
        self.orders.append((symbol, side, quantity, price, remark))
        return 10_000 + len(self.orders)


class _TimeoutBroker:
    async def place_limit_order(self, *args):
        raise QmtBrokerTimeout("outcome unknown")


class _ReconcileBroker:
    def __init__(self, orders):
        self.orders = orders
        self.connected = False
        self.closed = False

    async def connect(self):
        self.connected = True

    async def query_orders(self, remark_prefix=None):
        if remark_prefix:
            return [item for item in self.orders if item.remark.startswith(remark_prefix)]
        return self.orders

    async def close(self):
        self.closed = True


def _seed_validated_strategy(db_session):
    for month in (1, 2, 3):
        trading_day = date(2025, month, 1)
        symbols = [f"60{month}001", f"60{month}002"]
        _seed_snapshot(db_session, trading_day, symbols)
        _seed_bars(db_session, symbols[0], trading_day, daily_growth=0.002)
        _seed_bars(db_session, symbols[1], trading_day, daily_growth=0.001)
        run = SelectionService(db_session).rank(
            trading_day, SelectionConfig(top_n=2)
        )
        _seed_future_bars(
            db_session, symbols[0], trading_day, entry_open=10, exit_close=12
        )
        _seed_future_bars(
            db_session, symbols[1], trading_day, entry_open=10, exit_close=11
        )
        SelectionEvaluationService(db_session).evaluate(run.run_id)
    current_day = date(2025, 4, 1)
    current_symbols = ["604001", "604002"]
    _seed_snapshot(db_session, current_day, current_symbols)
    _seed_bars(db_session, current_symbols[0], current_day, daily_growth=0.002)
    _seed_bars(db_session, current_symbols[1], current_day, daily_growth=0.001)
    current = SelectionService(db_session).rank(
        current_day, SelectionConfig(top_n=2)
    )
    quotes = {symbol: make_quote(symbol, price=10) for symbol in current_symbols}
    return current, quotes


@pytest.mark.asyncio
async def test_live_plan_requires_two_step_one_time_approval(db_session):
    run, quotes = _seed_validated_strategy(db_session)
    account = LiveAccountSnapshot(cash=100_000, total_asset=100_000, positions={})
    service = LiveRebalanceService(
        db_session, "real-account", today=date(2025, 4, 1)
    )
    plan = service.create_plan(run.run_id, account, quotes)

    with pytest.raises(LiveRebalanceError, match="确认文本不匹配"):
        service.approve(plan.id, "yes")
    approved, token = service.approve(plan.id, service.ACKNOWLEDGEMENT)
    assert approved.status == "APPROVED"
    assert approved.approval_token_hash != token

    broker = _RecordingBroker()
    executed = await service.execute(plan.id, token, broker, account, quotes)

    assert executed.status == "SUBMITTED"
    assert executed.execution_json["submitted"] == 2
    assert len(broker.orders) == 2
    assert all(order[2] % 100 == 0 for order in broker.orders)
    with pytest.raises(LiveRebalanceError, match="不可执行"):
        await service.execute(plan.id, token, broker, account, quotes)


@pytest.mark.asyncio
async def test_account_change_forces_new_review(db_session):
    run, quotes = _seed_validated_strategy(db_session)
    account = LiveAccountSnapshot(cash=100_000, total_asset=100_000, positions={})
    service = LiveRebalanceService(
        db_session, "real-account", today=date(2025, 4, 1)
    )
    plan = service.create_plan(run.run_id, account, quotes)
    _, token = service.approve(plan.id, service.ACKNOWLEDGEMENT)
    changed = LiveAccountSnapshot(
        cash=99_000,
        total_asset=100_000,
        positions={
            "600000": LivePosition("600000", quantity=100, available_quantity=100)
        },
    )

    with pytest.raises(LiveRebalanceError, match="持仓已变化"):
        await service.execute(plan.id, token, _RecordingBroker(), changed, quotes)

    assert plan.status == "APPROVED"


def test_live_plan_cannot_override_missing_validation(db_session):
    trading_day = date(2026, 2, 1)
    _seed_snapshot(db_session, trading_day, ["600001"])
    _seed_bars(db_session, "600001", trading_day, daily_growth=0.002)
    run = SelectionService(db_session).rank(
        trading_day, SelectionConfig(top_n=1)
    )
    account = LiveAccountSnapshot(cash=100_000, total_asset=100_000, positions={})

    with pytest.raises(LiveRebalanceError, match="实盘禁止绕过历史验证"):
        LiveRebalanceService(
            db_session, "real-account", today=trading_day
        ).create_plan(
            run.run_id,
            account,
            {"600001": make_quote("600001", price=10)},
        )


@pytest.mark.asyncio
async def test_timeout_is_recorded_unknown_and_stops_batch(db_session):
    run, quotes = _seed_validated_strategy(db_session)
    account = LiveAccountSnapshot(cash=100_000, total_asset=100_000, positions={})
    service = LiveRebalanceService(
        db_session, "real-account", today=date(2025, 4, 1)
    )
    plan = service.create_plan(run.run_id, account, quotes)
    _, token = service.approve(plan.id, service.ACKNOWLEDGEMENT)

    executed = await service.execute(
        plan.id, token, _TimeoutBroker(), account, quotes
    )

    assert executed.status == "PARTIAL"
    assert executed.execution_json["orders"][0]["status"] == "UNKNOWN"
    assert len(executed.execution_json["orders"]) == 1


@pytest.mark.asyncio
async def test_reconcile_resolves_unknown_order_by_qmt_remark(db_session):
    run, quotes = _seed_validated_strategy(db_session)
    account = LiveAccountSnapshot(cash=100_000, total_asset=100_000, positions={})
    service = LiveRebalanceService(
        db_session, "real-account", today=date(2025, 4, 1)
    )
    plan = service.create_plan(run.run_id, account, quotes)
    _, token = service.approve(plan.id, service.ACKNOWLEDGEMENT)
    executed = await service.execute(
        plan.id, token, _TimeoutBroker(), account, quotes
    )
    first = executed.execution_json["orders"][0]

    reconciled = service.reconcile(
        plan.id,
        [
            LiveOrderStatus(
                order_id=88001,
                symbol=first["symbol"],
                side=first["side"],
                quantity=first["quantity"],
                price=first["indicative_price"],
                traded_volume=first["quantity"],
                traded_price=first["indicative_price"],
                raw_status=56,
                status="FILLED",
                status_message="已成",
                remark=f"live-plan-{plan.id}",
            )
        ],
    )

    assert reconciled.status == "FILLED"
    order = reconciled.execution_json["orders"][0]
    assert order["order_id"] == 88001
    assert order["broker_status"] == "FILLED"
    assert order["broker_traded_volume"] == first["quantity"]


@pytest.mark.asyncio
async def test_live_reconcile_worker_noops_when_real_trading_disabled(db_session):
    factory = sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False)
    called = False

    def _factory():
        nonlocal called
        called = True
        return _ReconcileBroker([])

    worker = LiveReconcileWorker(
        session_factory=factory,
        settings=Settings(_env_file=None, real_trading_enabled=False),
        broker_factory=_factory,
    )

    assert await worker.reconcile_once() == 0
    assert called is False


@pytest.mark.asyncio
async def test_live_reconcile_worker_updates_active_plan(db_session):
    factory = sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False)
    run, _ = _seed_validated_strategy(db_session)
    settings = Settings(
        _env_file=None,
        real_trading_enabled=True,
        qmt_userdata_path="configured",
        qmt_account_id="real-account",
    )
    account_fingerprint = LiveRebalanceService(
        db_session, "real-account"
    ).account_fingerprint
    plan = LiveRebalancePlan(
        selection_run_id=run.run_id,
        account_fingerprint=account_fingerprint,
        status="PARTIAL",
        account_snapshot_json={"cash": 100_000, "total_asset": 100_000, "positions": {}},
        proposal_json={"orders": []},
        execution_json={
            "orders": [
                {
                    "side": "BUY",
                    "symbol": "600001",
                    "quantity": 100,
                    "indicative_price": 10.0,
                    "status": "UNKNOWN",
                    "order_id": None,
                }
            ],
            "submitted": 0,
            "total": 1,
        },
    )
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    broker = _ReconcileBroker(
        [
            LiveOrderStatus(
                order_id=99001,
                symbol="600001",
                side="BUY",
                quantity=100,
                price=10.0,
                traded_volume=100,
                traded_price=10.0,
                raw_status=56,
                status="FILLED",
                status_message="已成",
                remark=f"live-plan-{plan.id}",
            )
        ]
    )
    worker = LiveReconcileWorker(
        session_factory=factory,
        settings=settings,
        broker_factory=lambda: broker,
    )

    assert await worker.reconcile_once() == 1

    db_session.expire_all()
    updated = db_session.get(LiveRebalancePlan, plan.id)
    assert broker.connected is True
    assert broker.closed is True
    assert updated.status == "FILLED"
    assert updated.execution_json["orders"][0]["broker_status"] == "FILLED"


def test_live_order_routes_are_explicitly_gated():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "post" in paths["/api/live/rebalance-plans"]
    assert "post" in paths["/api/live/rebalance-plans/{plan_id}/approve"]
    assert "post" in paths["/api/live/rebalance-plans/{plan_id}/reconcile"]
    execute = paths["/api/live/rebalance-plans/{plan_id}/execute"]["post"]
    header = next(
        item for item in execute["parameters"] if item["name"] == "X-Trade-Approval-Token"
    )
    assert header["required"] is True
    live_key = next(
        item for item in execute["parameters"] if item["name"] == "X-Live-Trading-Key"
    )
    assert live_key["required"] is True


def test_live_api_key_uses_constant_time_gate(monkeypatch):
    from fastapi import HTTPException

    from app.api import live_trading
    from app.config import Settings

    expected = "a" * 32
    settings = Settings(
        _env_file=None,
        real_trading_enabled=True,
        live_trading_api_token=expected,
    )
    monkeypatch.setattr(live_trading, "get_settings", lambda: settings)

    with pytest.raises(HTTPException) as error:
        live_trading._require_enabled("wrong")
    assert error.value.status_code == 403
    live_trading._require_enabled(expected)


def test_live_buys_never_assume_unfilled_sell_proceeds(db_session):
    run, quotes = _seed_validated_strategy(db_session)
    account = LiveAccountSnapshot(
        cash=10_000,
        total_asset=100_000,
        positions={
            "600000": LivePosition("600000", quantity=9_000, available_quantity=9_000)
        },
    )
    quotes["600000"] = make_quote("600000", price=10)

    plan = LiveRebalanceService(
        db_session, "real-account", today=date(2025, 4, 1)
    ).create_plan(
        run.run_id, account, quotes
    )

    buy_value = sum(
        item["indicative_value"]
        for item in plan.proposal_json["orders"]
        if item["side"] == "BUY"
    )
    assert buy_value <= account.cash * 0.998
    assert any(item["side"] == "SELL" for item in plan.proposal_json["orders"])


def test_stale_selection_run_is_never_allowed_live(db_session):
    run, quotes = _seed_validated_strategy(db_session)
    account = LiveAccountSnapshot(cash=100_000, total_asset=100_000, positions={})

    with pytest.raises(LiveRebalanceError, match="最近 10 天"):
        LiveRebalanceService(
            db_session, "real-account", today=date(2025, 5, 1)
        ).create_plan(run.run_id, account, quotes)
