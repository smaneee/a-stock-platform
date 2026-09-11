"""每日流水线自动调度测试。"""
from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database.models import DailyPipelineRun, PaperAccount, TradingDate
from app.tasks.daily_pipeline_scheduler import DailyPipelineScheduler


def _factory(db_session):
    return sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False)


def _settings(**overrides) -> Settings:
    values = dict(
        database_url="sqlite:///:memory:",
        daily_pipeline_auto_enabled=True,
        daily_pipeline_auto_hour=15,
        daily_pipeline_auto_minute=35,
    )
    values.update(overrides)
    return Settings(**values)


def _seed_trading_day(db_session, day: date) -> None:
    db_session.add(TradingDate(trade_date=day))
    db_session.commit()


def _seed_account(db_session) -> PaperAccount:
    account = PaperAccount(
        name="auto-schedule",
        initial_cash=Decimal("100000"),
        available_cash=Decimal("100000"),
        frozen_cash=Decimal("0"),
    )
    db_session.add(account)
    db_session.commit()
    return account


def test_scheduler_skips_non_trading_day(db_session):
    scheduler = DailyPipelineScheduler(db_factory=_factory(db_session), settings=_settings())

    result = scheduler.create_run_for(date(2026, 1, 17))

    assert result is None
    assert db_session.query(DailyPipelineRun).count() == 0


def test_scheduler_creates_run_on_trading_day(db_session):
    day = date(2026, 1, 15)
    _seed_trading_day(db_session, day)
    account = _seed_account(db_session)
    scheduler = DailyPipelineScheduler(
        db_factory=_factory(db_session),
        settings=_settings(
            daily_pipeline_auto_paper_account_id=account.id,
            daily_pipeline_auto_execute_paper=True,
        ),
    )

    run = scheduler.create_run_for(day)

    assert run is not None
    assert run.status == "queued"
    assert run.trading_day == day
    assert run.paper_account_id == account.id
    assert run.auto_execute_paper is True


def test_scheduler_is_idempotent_for_same_day(db_session):
    day = date(2026, 1, 15)
    _seed_trading_day(db_session, day)
    scheduler = DailyPipelineScheduler(db_factory=_factory(db_session), settings=_settings())

    first = scheduler.create_run_for(day)
    second = scheduler.create_run_for(day)

    assert first.id == second.id
    assert db_session.query(DailyPipelineRun).count() == 1


def test_scheduler_downgrades_auto_execute_without_account(db_session):
    day = date(2026, 1, 15)
    _seed_trading_day(db_session, day)
    scheduler = DailyPipelineScheduler(
        db_factory=_factory(db_session),
        settings=_settings(daily_pipeline_auto_execute_paper=True),
    )

    run = scheduler.create_run_for(day)

    assert run is not None
    assert run.auto_execute_paper is False
    assert run.paper_account_id is None


def test_scheduler_describe_reports_configuration(db_session):
    scheduler = DailyPipelineScheduler(
        db_factory=_factory(db_session),
        settings=_settings(daily_pipeline_auto_paper_account_id=7),
    )

    info = scheduler.describe()

    assert info["enabled"] is True
    assert (info["hour"], info["minute"]) == (15, 35)
    assert info["paper_account_id"] == 7
    assert info["running"] is False
    assert info["next_run_at"] is None


def test_scheduler_disabled_does_not_start(db_session):
    scheduler = DailyPipelineScheduler(
        db_factory=_factory(db_session),
        settings=_settings(daily_pipeline_auto_enabled=False),
    )

    asyncio.run(_start_and_stop(scheduler))

    assert scheduler.is_running is False
    assert scheduler.describe()["enabled"] is False


def test_scheduler_start_stop_lifecycle_with_next_run(db_session):
    scheduler = DailyPipelineScheduler(db_factory=_factory(db_session), settings=_settings())

    async def lifecycle():
        scheduler.start()
        try:
            assert scheduler.is_running is True
            info = scheduler.describe()
            assert info["running"] is True
            assert info["next_run_at"] is not None
        finally:
            await scheduler.stop()

    asyncio.run(lifecycle())

    assert scheduler.is_running is False


async def _start_and_stop(scheduler: DailyPipelineScheduler) -> None:
    scheduler.start()
    await scheduler.stop()
