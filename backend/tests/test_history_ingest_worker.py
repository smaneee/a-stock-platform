"""Recoverable history ingest queue tests."""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace

from sqlalchemy.orm import sessionmaker

from app.api.history_ingest import (
    HistoryIngestRequest,
    cancel_history_ingest,
    create_history_ingest,
)
from app.database.models import HistoryIngestBatch, Security, UniverseMember, UniverseSnapshot
from app.tasks.history_ingest_worker import HistoryIngestWorker


def _seed_snapshot(db, symbols: list[str], trading_day: date = date(2026, 1, 30)):
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
        db.add(
            Security(
                symbol=symbol,
                name=symbol,
                exchange="SH",
                board="sh_main",
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
                trading_status="active",
            )
        )
    db.commit()
    return snapshot


def _factory(db_session):
    return sessionmaker(
        bind=db_session.get_bind(),
        autoflush=False,
        expire_on_commit=False,
    )


def test_create_job_persists_exact_snapshot_members(db_session):
    snapshot = _seed_snapshot(db_session, ["600001", "600002"])

    payload = HistoryIngestRequest(
        trading_day=snapshot.trading_day,
        lookback_days=365,
        symbols=["600002"],
    )
    response = create_history_ingest(payload, db_session)

    task = db_session.get(HistoryIngestBatch, response["id"])
    assert task.status == "queued"
    assert task.snapshot_id == snapshot.id
    assert task.requested_symbol_list == ["600002"]
    assert task.requested_symbols == 1


def test_cancel_queued_job_is_immediate(db_session):
    snapshot = _seed_snapshot(db_session, ["600001"])
    created = create_history_ingest(
        HistoryIngestRequest(trading_day=snapshot.trading_day), db_session
    )

    response = cancel_history_ingest(created["id"], db_session)

    assert response["status"] == "cancelled"
    assert db_session.get(HistoryIngestBatch, created["id"]).cancel_requested is True


def test_worker_recovers_running_job(db_session):
    snapshot = _seed_snapshot(db_session, ["600001"])
    created = create_history_ingest(
        HistoryIngestRequest(trading_day=snapshot.trading_day), db_session
    )
    task = db_session.get(HistoryIngestBatch, created["id"])
    task.status = "running"
    db_session.commit()

    recovered = HistoryIngestWorker(_factory(db_session))._recover_stale_tasks()

    db_session.expire_all()
    assert recovered == 1
    assert db_session.get(HistoryIngestBatch, task.id).status == "queued"


def test_worker_records_per_symbol_partial_coverage(db_session, monkeypatch):
    snapshot = _seed_snapshot(db_session, ["600001", "600002"])
    created = create_history_ingest(
        HistoryIngestRequest(trading_day=snapshot.trading_day), db_session
    )
    task = db_session.get(HistoryIngestBatch, created["id"])
    task.status = "running"
    db_session.commit()

    class FakeHistoryService:
        def __init__(self, db, provider_manager=None):
            pass

        async def get_history(self, symbol, start, end, adjust):
            complete = symbol == "600001"
            return SimpleNamespace(
                bars=[SimpleNamespace()] * (80 if complete else 12),
                is_complete=complete,
                quality=SimpleNamespace(missing_dates=[] if complete else [date(2026, 1, 2)]),
            )

    monkeypatch.setattr(
        "app.tasks.history_ingest_worker.HistoricalDataService",
        FakeHistoryService,
    )
    worker = HistoryIngestWorker(_factory(db_session), max_concurrency=1)

    asyncio.run(worker._execute(task.id))

    db_session.expire_all()
    finished = db_session.get(HistoryIngestBatch, task.id)
    assert finished.status == "partial"
    assert finished.progress == 100
    assert finished.completed_symbols == 1
    assert finished.covered_symbols == ["600001"]
    assert "600002" in finished.failed_symbols
    assert finished.total_bars == 80


def test_claim_next_uses_oldest_queued_job(db_session):
    snapshot = _seed_snapshot(db_session, ["600001"])
    first = create_history_ingest(
        HistoryIngestRequest(trading_day=snapshot.trading_day), db_session
    )
    second = create_history_ingest(
        HistoryIngestRequest(trading_day=snapshot.trading_day), db_session
    )

    claimed = HistoryIngestWorker(_factory(db_session))._claim_next()

    assert claimed == first["id"]
    db_session.expire_all()
    assert db_session.get(HistoryIngestBatch, first["id"]).status == "running"
    assert db_session.get(HistoryIngestBatch, second["id"]).status == "queued"
