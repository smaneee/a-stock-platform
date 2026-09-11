"""Recoverable history-ingest job API."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import HistoryIngestBatch, UniverseMember, UniverseSnapshot
from app.database.session import get_db
from app.tasks.status import CANCELLED, QUEUED, RUNNING
from app.validation import sanitize_symbols

router = APIRouter(prefix="/api/history-ingest", tags=["history-ingest"])


class HistoryIngestRequest(BaseModel):
    trading_day: date
    lookback_days: int = Field(default=365, ge=90, le=2000)
    adjust: str = Field(default="none", pattern="^(none|qfq|hfq)$")
    symbols: list[str] | None = Field(default=None, max_length=5000)


def _serialize(task: HistoryIngestBatch) -> dict:
    failures = dict(task.failed_symbols or {})
    return {
        "id": task.id,
        "snapshot_id": task.snapshot_id,
        "status": task.status,
        "progress": task.progress,
        "start_date": task.start_date.isoformat(),
        "end_date": task.end_date.isoformat(),
        "adjust": task.adjust,
        "requested_symbols": task.requested_symbols,
        "completed_symbols": task.completed_symbols,
        "coverage_ratio": task.coverage_ratio,
        "total_bars": task.total_bars,
        "failed_count": len(failures),
        "failed_symbols": dict(list(failures.items())[:100]),
        "last_error": task.last_error,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


@router.post("", status_code=202)
def create_history_ingest(
    payload: HistoryIngestRequest,
    db: Session = Depends(get_db),
) -> dict:
    snapshot = db.scalar(
        select(UniverseSnapshot).where(
            UniverseSnapshot.trading_day == payload.trading_day
        )
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="指定交易日没有股票池快照")

    stmt = (
        select(UniverseMember.symbol)
        .where(UniverseMember.snapshot_id == snapshot.id)
        .where(UniverseMember.is_included.is_(True))
        .where(UniverseMember.trading_status == "active")
        .order_by(UniverseMember.symbol)
    )
    available = list(db.scalars(stmt).all())
    if payload.symbols is not None:
        requested_set = set(sanitize_symbols(payload.symbols))
        available_set = set(available)
        unknown = sorted(requested_set - available_set)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"以下股票不在该日可交易股票池: {unknown[:20]}",
            )
        symbols = sorted(requested_set)
    else:
        symbols = available
    if not symbols:
        raise HTTPException(status_code=422, detail="没有可入库的股票")

    task = HistoryIngestBatch(
        snapshot_id=snapshot.id,
        source="provider_manager",
        status=QUEUED,
        start_date=payload.trading_day - timedelta(days=payload.lookback_days),
        end_date=payload.trading_day,
        adjust=payload.adjust,
        requested_symbols=len(symbols),
        completed_symbols=0,
        total_bars=0,
        coverage_ratio=0.0,
        progress=0,
        cancel_requested=False,
        requested_symbol_list=symbols,
        covered_symbols=[],
        failed_symbols={},
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return _serialize(task)


@router.get("")
def list_history_ingest(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    tasks = db.scalars(
        select(HistoryIngestBatch)
        .order_by(HistoryIngestBatch.id.desc())
        .limit(limit)
    ).all()
    return {"items": [_serialize(task) for task in tasks]}


@router.get("/{task_id}")
def get_history_ingest(task_id: int, db: Session = Depends(get_db)) -> dict:
    task = db.get(HistoryIngestBatch, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="历史入库任务不存在")
    return _serialize(task)


@router.post("/{task_id}/cancel")
def cancel_history_ingest(task_id: int, db: Session = Depends(get_db)) -> dict:
    task = db.get(HistoryIngestBatch, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="历史入库任务不存在")
    if task.status == QUEUED:
        task.status = CANCELLED
        task.cancel_requested = True
    elif task.status == RUNNING:
        task.cancel_requested = True
    db.commit()
    db.refresh(task)
    return _serialize(task)
