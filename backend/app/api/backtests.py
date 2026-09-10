"""历史回测接口（任务化）。

回测创建后进入数据库队列，由后台 BacktestWorker 执行。
支持幂等键、进度查询、取消。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Backtest
from app.database.session import get_db
from app.strategies import registry
from app.tasks.status import CANCELLABLE_STATES, CANCELLED, QUEUED
from app.time_utils import utc_now
from app.validation import validate_symbol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["backtests"])


class BacktestRequest(BaseModel):
    symbol: str
    strategy_name: str
    start_time: datetime
    end_time: datetime
    initial_cash: float = Field(100_000.0, gt=0)
    idempotency_key: str | None = Field(None, max_length=64, description="幂等键，避免重复创建")


@router.post("/backtests", status_code=202)
def create_backtest(
    body: BacktestRequest,
    db: Session = Depends(get_db),
) -> dict:
    """创建回测任务，入队由后台 worker 执行。"""
    if not validate_symbol(body.symbol):
        raise HTTPException(status_code=422, detail="非法股票代码")
    if body.start_time >= body.end_time:
        raise HTTPException(status_code=422, detail="开始时间必须早于结束时间")
    strategy = registry.get_strategy(body.strategy_name)
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")

    # 幂等：同一 idempotency_key 不重复创建
    if body.idempotency_key:
        existing = db.scalars(
            select(Backtest).where(Backtest.idempotency_key == body.idempotency_key)
        ).first()
        if existing:
            return {"id": existing.id, "status": existing.status, "duplicate": True}

    backtest = Backtest(
        symbol=body.symbol,
        strategy_name=body.strategy_name,
        start_time=body.start_time,
        end_time=body.end_time,
        initial_cash=body.initial_cash,
        status=QUEUED,
        idempotency_key=body.idempotency_key,
    )
    db.add(backtest)
    db.commit()
    db.refresh(backtest)
    return {"id": backtest.id, "status": backtest.status}


@router.get("/backtests")
def list_backtests(
    db: Session = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """列出回测任务（按创建时间倒序）。"""
    limit = max(1, min(limit, 200))
    rows = db.scalars(
        select(Backtest).order_by(Backtest.id.desc()).offset(offset).limit(limit)
    ).all()
    return {"items": [_summary(b) for b in rows], "count": len(rows)}


@router.get("/backtests/{backtest_id}")
def get_backtest(backtest_id: int, db: Session = Depends(get_db)) -> dict:
    """查询回测任务详情（含进度、时间戳、错误摘要）。"""
    backtest = db.get(Backtest, backtest_id)
    if backtest is None:
        raise HTTPException(status_code=404, detail="回测任务不存在")
    result = json.loads(backtest.result) if backtest.result else None
    data = _summary(backtest)
    data["result"] = result
    return data


@router.post("/backtests/{backtest_id}/cancel")
def cancel_backtest(backtest_id: int, db: Session = Depends(get_db)) -> dict:
    """取消尚未完成的任务。"""
    backtest = db.get(Backtest, backtest_id)
    if backtest is None:
        raise HTTPException(status_code=404, detail="回测任务不存在")
    if backtest.status not in CANCELLABLE_STATES:
        raise HTTPException(status_code=409, detail=f"任务已处于终态 {backtest.status}，无法取消")
    backtest.status = CANCELLED
    backtest.finished_at = utc_now()
    db.commit()
    return {"id": backtest.id, "status": backtest.status}


def _summary(b: Backtest) -> dict:
    return {
        "id": b.id,
        "symbol": b.symbol,
        "strategy_name": b.strategy_name,
        "start_time": b.start_time.isoformat(),
        "end_time": b.end_time.isoformat(),
        "status": b.status,
        "progress": b.progress or 0,
        "error_message": b.error_message,
        "started_at": b.started_at.isoformat() if b.started_at else None,
        "finished_at": b.finished_at.isoformat() if b.finished_at else None,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }
