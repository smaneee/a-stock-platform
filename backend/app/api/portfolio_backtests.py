"""组合回测接口。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import PortfolioBacktest
from app.database.session import get_db
from app.portfolio.sentiment import SentimentGateConfig
from app.tasks.status import CANCELLED, FAILED, SUCCEEDED
from app.validation import validate_benchmark_symbol, validate_symbol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/portfolio-backtests", tags=["portfolio_backtests"])


class SentimentGateRequest(BaseModel):
    """市场情绪闸门参数（可选）。

    只用信号日之前 ``lag_days`` 个交易日的涨停情绪，``lag_days >= 1`` 保证不引入
    未来函数；闸门只拦截新开仓，SELL 始终放行。
    """

    enabled: bool = False
    lag_days: int = Field(1, ge=1, le=10, description="使用信号日之前第 N 个交易日的情绪")
    min_seal_rate: float | None = Field(None, ge=0, le=1, description="封板率下限")
    max_broken_rate: float | None = Field(None, ge=0, le=1, description="炸板率上限")
    min_max_streak: int | None = Field(None, ge=0, le=30, description="最高连板高度下限")
    min_limit_up_count: int | None = Field(None, ge=0, le=1000, description="涨停家数下限")
    on_missing: Literal["allow", "block"] = Field(
        "allow", description="情绪数据缺失时的处理：allow 放行 / block 拦截"
    )
    scale_exposure: bool = Field(False, description="按封板率缩放新开仓仓位")
    min_exposure: float = Field(0.3, ge=0, le=1, description="缩放后的仓位系数下限")

    def to_gate_config(self) -> SentimentGateConfig:
        """转换为引擎使用的配置对象。"""
        return SentimentGateConfig(**self.model_dump())


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
    max_participation_rate: float = Field(
        0.0,
        ge=0,
        le=1.0,
        description="单日成交量参与率上限（D5）；0=不限制（默认，保持历史行为）",
    )
    allow_partial_fill: bool = Field(
        True, description="超出参与率上限时按可成交量部分成交；False 则整笔拒绝"
    )
    idempotency_key: str | None = Field(None, max_length=64)
    sentiment_gate: SentimentGateRequest | None = Field(
        None, description="可选的涨停板市场情绪闸门"
    )


def _config_gate(config_json: str | None) -> dict | None:
    """从 config_json 里取出情绪闸门参数；兼容旧任务的缺失 / 损坏字段。"""
    if not config_json:
        return None
    try:
        parsed = json.loads(config_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    gate = parsed.get("sentiment_gate")
    return gate if isinstance(gate, dict) else None


def _config_execution(config_json: str | None) -> dict:
    """从 config_json 里取出 D5 成交配置；旧任务缺该字段时返回默认值。"""
    default = {"max_participation_rate": 0.0, "allow_partial_fill": True}
    if not config_json:
        return default
    try:
        parsed = json.loads(config_json)
    except (TypeError, ValueError):
        return default
    if not isinstance(parsed, dict):
        return default
    raw = parsed.get("execution")
    if not isinstance(raw, dict):
        return default
    return {
        "max_participation_rate": float(
            raw.get("max_participation_rate", default["max_participation_rate"]) or 0.0
        ),
        "allow_partial_fill": bool(
            raw.get("allow_partial_fill", default["allow_partial_fill"])
        ),
    }


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
        "sentiment_gate": _config_gate(task.config_json),
        "execution": _config_execution(task.config_json),
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
    if body.benchmark_symbol and not validate_benchmark_symbol(body.benchmark_symbol):
        raise HTTPException(
            status_code=422,
            detail=(
                f"非法基准代码: {body.benchmark_symbol}；指数需带交易所前缀，"
                "如 sh000300（沪深300）/ sz399006（创业板指），"
                "可用指数见 GET /api/market/indices"
            ),
        )

    # 权重归一化校验
    if body.weights:
        for sym, w in body.weights.items():
            if sym not in body.symbols:
                raise HTTPException(status_code=422, detail=f"权重中的 {sym} 不在 symbols 中")
            if w < 0:
                raise HTTPException(status_code=422, detail=f"权重不可为负: {sym}")

    # 幂等键去重
    if body.sentiment_gate is not None:
        gate_config = body.sentiment_gate.to_gate_config()
        try:
            gate_config.validate()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if (
            gate_config.enabled
            and not gate_config.has_threshold()
            and not gate_config.scale_exposure
        ):
            raise HTTPException(
                status_code=422,
                detail="启用情绪闸门时至少要配置一个阈值条件，或打开 scale_exposure",
            )

    if body.idempotency_key:
        existing = db.scalars(
            select(PortfolioBacktest).where(
                PortfolioBacktest.idempotency_key == body.idempotency_key
            )
        ).first()
        if existing is not None:
            return _serialize(existing)

    config_json = json.dumps(
        {
            "risk_free_rate": body.risk_free_rate,
            "sentiment_gate": (
                body.sentiment_gate.model_dump() if body.sentiment_gate else None
            ),
            "execution": {
                "max_participation_rate": body.max_participation_rate,
                "allow_partial_fill": body.allow_partial_fill,
            },
        }
    )
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
