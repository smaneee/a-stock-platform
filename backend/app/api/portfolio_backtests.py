"""组合回测接口。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import PortfolioBacktest
from app.database.session import get_db
from app.tasks.status import CANCELLED, FAILED, SUCCEEDED
from app.validation import validate_symbol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/portfolio-backtests", tags=["portfolio_backtests"])


class PortfolioBacktestCreate(BaseModel):
    """组合回测提交参数。"""

    symbols: list[str] = Field(..., min_length=1, max_length=20)
    strategy_name: str = Field(..., min_length=1, max_length=100)
    weights: dict[str, float] | None = None
    benchmark_symbol: str | None = Field(None, max_length=16)
    start_time: datetime
    end_time: datetime
    initial_cash: float = Field(100_000.0, gt=0)
    max_single_position: float = Field(0.2, gt=0, le=1.0)
    max_total_position: float = Field(0.95, gt=0, le=1.0)
    commission_rate: float = Field(0.0003, ge=0, le=0.01)
    slippage: float = Field(0.0005, ge=0, le=0.1)
    risk_free_rate: float = Field(0.02, ge=0, le=0.2)
    idempotency_key: str | None = Field(None, max_length=64)


def _serialize(task: PortfolioBacktest) -> dict:
    """把 ORM 行序列化为 API 返回。"""
    result = None
    if task.result:
        try:
            result = json.loads(task.result)
        except (ValueError, TypeError):
            result = None
    return {
        "id": task.id,
        "symbols": json.loads(task.symbols),
        "weights": json.loads(task.weights) if task.weights else None,
        "benchmark_symbol": task.benchmark_symbol,
        "strategy_name": task.strategy_name,
        "start_time": task.start_time.isoformat() if task.start_time else None,
        "end_time": task.end_time.isoformat() if task.end_time else None,
        "initial_cash": float(task.initial_cash),
        "max_single_position": float(task.max_single_position),
        "max_total_position": float(task.max_total_position),
        "commission_rate": float(task.commission_rate),
        "slippage": float(task.slippage),
        "status": task.status,
        "progress": task.progress,
        "result": result,
        "error_message": task.error_message,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


@router.post("", status_code=201)
async def create_portfolio_backtest(
    body: PortfolioBacktestCreate,
    db: Session = Depends(get_db),
) -> dict:
    """提交组合回测任务（落库为 queued，由 PortfolioBacktestWorker 异步执行）。"""
    # 校验所有 symbol 合法
    for sym in body.symbols:
        if not validate_symbol(sym):
            raise HTTPException(status_code=422, detail=f"非法股票代码: {sym}")
    if body.benchmark_symbol and not validate_symbol(body.benchmark_symbol):
        raise HTTPException(status_code=422, detail=f"非法基准代码: {body.benchmark_symbol}")

    # 权重归一化校验
    if body.weights:
        for sym, w in body.weights.items():
            if sym not in body.symbols:
                raise HTTPException(status_code=422, detail=f"权重中的 {sym} 不在 symbols 中")
            if w < 0:
                raise HTTPException(status_code=422, detail=f"权重不可为负: {sym}")

    # 幂等键去重
    if body.idempotency_key:
        existing = db.scalars(
            select(PortfolioBacktest).where(
                PortfolioBacktest.idempotency_key == body.idempotency_key
            )
        ).first()
        if existing is not None:
            return _serialize(existing)

    config_json = json.dumps({"risk_free_rate": body.risk_free_rate})
    task = PortfolioBacktest(
        symbols=json.dumps(body.symbols),
        weights=json.dumps(body.weights) if body.weights else None,
        benchmark_symbol=body.benchmark_symbol,
        strategy_name=body.strategy_name,
        start_time=body.start_time,
        end_time=body.end_time,
        initial_cash=Decimal(str(body.initial_cash)),
        max_single_position=Decimal(str(body.max_single_position)),
        max_total_position=Decimal(str(body.max_total_position)),
        commission_rate=Decimal(str(body.commission_rate)),
        slippage=Decimal(str(body.slippage)),
        idempotency_key=body.idempotency_key,
        config_json=config_json,
        status="queued",
        progress=0,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return _serialize(task)


@router.get("")
async def list_portfolio_backtests(
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    """列出最近的组合回测任务。"""
    tasks = db.scalars(
        select(PortfolioBacktest)
        .order_by(PortfolioBacktest.id.desc())
        .limit(limit)
    ).all()
    return {"tasks": [_serialize(t) for t in tasks]}


@router.get("/{task_id}")
async def get_portfolio_backtest(
    task_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """查询单个组合回测任务详情。"""
    task = db.get(PortfolioBacktest, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _serialize(task)


@router.post("/{task_id}/cancel", status_code=200)
async def cancel_portfolio_backtest(
    task_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """取消任务：queued/running 均可置为 cancelled（worker 轮询时检测）。"""
    task = db.get(PortfolioBacktest, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status in {SUCCEEDED, FAILED, CANCELLED}:
        raise HTTPException(
            status_code=409,
            detail=f"任务已结束（{task.status}），无法取消",
        )
    task.status = CANCELLED
    from app.time_utils import utc_now

    task.finished_at = utc_now()
    db.commit()
    db.refresh(task)
    return _serialize(task)
