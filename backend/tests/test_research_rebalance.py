"""Research-to-paper allocation draft tests."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.api.research import ResearchRebalanceDraftRequest, create_research_rebalance_draft
from app.database.models import (
    FundamentalSnapshot,
    HistoricalBar,
    InvestmentResearchRun,
    PaperAccount,
)
from app.research.rebalance import (
    ResearchDraftConstraints,
    ResearchDraftInputs,
    aligned_return_correlation,
    build_research_draft,
)
from tests.helpers import make_quote


def _constraints(**overrides) -> ResearchDraftConstraints:
    values = {
        "max_symbol_weight": 0.10,
        "max_industry_weight": 0.30,
        "max_loss_per_trade": 0.01,
        "stop_distance": 0.10,
        "max_portfolio_drawdown": 0.12,
        "max_correlation": 0.75,
        "max_liquidity_participation": 0.01,
    }
    values.update(overrides)
    return ResearchDraftConstraints(**values)


def _inputs(**overrides) -> ResearchDraftInputs:
    values = {
        "conclusion_key": "attractive",
        "symbol": "000333",
        "price": 10.0,
        "total_asset": 1_000_000.0,
        "current_quantity": 0,
        "current_weight": 0.0,
        "industry": "家用电器",
        "industry_weight": 0.05,
        "invested_weight": 0.40,
        "average_amount_20": 10_000_000.0,
        "max_correlation": 0.40,
        "correlation_observations": 59,
        "held_peer_count": 2,
    }
    values.update(overrides)
    return ResearchDraftInputs(**values)


def test_attractive_draft_is_capped_and_lot_sized():
    result = build_research_draft(_inputs(), _constraints())

    assert result["action"] == "INCREASE_DRAFT"
    assert result["target_weight"] == 0.10
    assert result["proposed_order"] == {
        "side": "BUY",
        "symbol": "000333",
        "quantity": 10_000,
        "indicative_price": 10.0,
        "indicative_value": 100_000.0,
    }
    assert result["execution_allowed"] is False


def test_high_correlation_blocks_new_allocation():
    result = build_research_draft(
        _inputs(max_correlation=0.91),
        _constraints(max_correlation=0.75),
    )

    assert result["action"] == "BLOCKED"
    assert result["target_weight"] == 0
    assert result["proposed_order"] is None
    assert next(item for item in result["checks"] if item["name"] == "correlation")[
        "status"
    ] == "block"


def test_non_attractive_conclusion_never_increases_position():
    result = build_research_draft(
        _inputs(
            conclusion_key="fair",
            current_quantity=5_000,
            current_weight=0.05,
        ),
        _constraints(),
    )

    assert result["target_weight"] == 0.05
    assert result["proposed_order"] is None
    assert result["action"] == "BLOCKED"


def test_missing_liquidity_blocks_increase():
    result = build_research_draft(
        _inputs(average_amount_20=None),
        _constraints(),
    )

    assert result["target_weight"] == 0
    assert result["proposed_order"] is None


def test_aligned_return_correlation_uses_common_dates_only():
    target = {"1": 10.0, "2": 11.0, "3": 12.1, "4": 13.31}
    peer = {"0": 5.0, "1": 20.0, "2": 22.0, "3": 24.2, "4": 26.62}

    value, observations = aligned_return_correlation(target, peer)

    assert observations == 3
    assert value is not None
    assert value > 0.99


def test_research_rebalance_route_is_registered():
    from app.main import app

    assert "post" in app.openapi()["paths"][
        "/api/research/runs/{run_id}/rebalance-draft"
    ]


def test_endpoint_reads_account_history_and_returns_no_execution(db_session):
    account = PaperAccount(
        name="研究模拟账户",
        initial_cash=Decimal("1000000"),
        available_cash=Decimal("1000000"),
        frozen_cash=Decimal("0"),
    )
    snapshot = FundamentalSnapshot(
        symbol="000333",
        name="测试公司",
        snapshot_date=date(2026, 9, 15),
        report_date=date(2026, 6, 30),
        price=10,
        industry="家用电器",
        source="test",
    )
    run = InvestmentResearchRun(
        symbol="000333",
        name="测试公司",
        snapshot_date=date(2026, 9, 15),
        report_date=date(2026, 6, 30),
        price=10,
        source="test",
        conclusion_key="attractive",
        fingerprint="a" * 64,
        assumptions={},
        analysis={"1_conclusion": {"conclusion": "进入观察名单"}},
        reverse_valuation={},
    )
    db_session.add_all([account, snapshot, run])
    for index in range(20):
        db_session.add(
            HistoricalBar(
                symbol="000333",
                period="daily",
                adjust="qfq",
                trade_date=date(2026, 8, 1) + timedelta(days=index),
                close=Decimal("10"),
                amount=10_000_000,
            )
        )
    db_session.commit()
    db_session.refresh(account)
    db_session.refresh(run)

    class Quotes:
        async def get_quotes(self, symbols):
            return {symbol: make_quote(symbol, price=10) for symbol in symbols}

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(provider_manager=Quotes()))
    )
    result = asyncio.run(
        create_research_rebalance_draft(
            run.id,
            ResearchRebalanceDraftRequest(account_id=account.id),
            request,
            db_session,
        )
    )

    assert result["action"] == "INCREASE_DRAFT"
    assert result["proposed_order"]["quantity"] == 10_000
    assert result["execution_allowed"] is False
    assert db_session.query(InvestmentResearchRun).count() == 1
