"""Daily pipeline regression tests."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import sessionmaker

from app.database.models import DailyPipelineRun, HistoryIngestBatch, PaperAccount, PaperRebalancePlan
from app.tasks.daily_pipeline import DailyPipelineService, DailyPipelineWorker
from app.tasks.status import QUEUED, RUNNING, SUCCEEDED
from tests.helpers import make_quote
from tests.test_selection import _seed_bars, _seed_snapshot


class _QuoteProvider:
    async def get_quotes(self, symbols):
        return {symbol: make_quote(symbol, price=10.0) for symbol in symbols}


@pytest.mark.asyncio
async def test_daily_pipeline_waits_for_history_ingest(db_session):
    trading_day = date(2026, 1, 15)
    _seed_snapshot(db_session, trading_day, ["600001", "600002"])
    run = DailyPipelineService(db_session).create_run(trading_day)

    advanced = await DailyPipelineService(db_session).advance(run.id)

    assert advanced.status == "waiting_history"
    assert advanced.stage == "waiting_history"
    assert advanced.history_task_id is not None
    task = db_session.get(HistoryIngestBatch, advanced.history_task_id)
    assert task.status == "queued"
    assert task.requested_symbols == 2


@pytest.mark.asyncio
async def test_daily_pipeline_resumes_after_history_success(db_session):
    trading_day = date(2026, 1, 15)
    _seed_snapshot(db_session, trading_day, ["600001", "600002"])
    _seed_bars(db_session, "600001", trading_day, daily_growth=0.002)
    _seed_bars(db_session, "600002", trading_day, daily_growth=0.001)
    service = DailyPipelineService(db_session)
    run = await service.advance(service.create_run(trading_day).id)
    task = db_session.get(HistoryIngestBatch, run.history_task_id)
    task.status = SUCCEEDED
    task.progress = 100
    task.completed_symbols = 2
    task.coverage_ratio = 1.0
    task.covered_symbols = ["600001", "600002"]
    db_session.commit()

    resumed = await DailyPipelineService(db_session).advance(run.id)

    assert resumed.status == SUCCEEDED
    assert resumed.stage == "succeeded"
    assert resumed.selection_run_id is not None
    assert resumed.progress == 100


def test_daily_pipeline_worker_recovers_running_runs(db_session):
    run = DailyPipelineRun(
        trading_day=date(2026, 1, 15),
        status=RUNNING,
        stage="selection_rank",
        config_json={"selection": {}, "history": {}},
        progress=65,
    )
    db_session.add(run)
    db_session.commit()
    factory = sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False)

    recovered = DailyPipelineWorker(session_factory=factory)._recover_stale_runs()

    db_session.expire_all()
    restored = db_session.get(DailyPipelineRun, run.id)
    assert recovered == 1
    assert restored.status == QUEUED
    assert restored.stage == "queued"


def test_daily_pipeline_worker_waiting_history_does_not_block_queued(db_session):
    waiting = DailyPipelineRun(
        trading_day=date(2026, 1, 15),
        status="waiting_history",
        stage="waiting_history",
        config_json={"selection": {}, "history": {}},
        progress=35,
    )
    queued = DailyPipelineRun(
        trading_day=date(2026, 1, 16),
        status=QUEUED,
        stage="queued",
        config_json={"selection": {}, "history": {}},
        progress=0,
    )
    db_session.add_all([waiting, queued])
    db_session.commit()
    factory = sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False)

    claimed = DailyPipelineWorker(session_factory=factory)._claim_next()

    assert claimed == queued.id


@pytest.mark.asyncio
async def test_daily_pipeline_can_execute_paper_rebalance_with_override(db_session):
    trading_day = date(2026, 1, 15)
    _seed_snapshot(db_session, trading_day, ["600001", "600002"])
    _seed_bars(db_session, "600001", trading_day, daily_growth=0.002)
    _seed_bars(db_session, "600002", trading_day, daily_growth=0.001)
    account = PaperAccount(
        name="pipeline-paper",
        initial_cash=Decimal("100000"),
        available_cash=Decimal("100000"),
        frozen_cash=Decimal("0"),
    )
    db_session.add(account)
    db_session.commit()
    service = DailyPipelineService(db_session, provider_manager=_QuoteProvider())
    run = await service.advance(
        service.create_run(
            trading_day,
            paper_account_id=account.id,
            auto_execute_paper=True,
            paper_validation_override=True,
        ).id
    )
    task = db_session.get(HistoryIngestBatch, run.history_task_id)
    task.status = SUCCEEDED
    task.progress = 100
    task.completed_symbols = 2
    task.coverage_ratio = 1.0
    task.covered_symbols = ["600001", "600002"]
    db_session.commit()

    done = await DailyPipelineService(
        db_session, provider_manager=_QuoteProvider()
    ).advance(run.id)

    plan = db_session.get(PaperRebalancePlan, done.paper_plan_id)
    assert done.status == SUCCEEDED
    assert plan is not None
    assert plan.status == "EXECUTED"
    assert plan.validation_override is True
