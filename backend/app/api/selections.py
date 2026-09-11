"""Point-in-time selection API. Results are research candidates, not orders."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import SelectionRun
from app.database.session import get_db
from app.selection import (
    SelectionConfig,
    SelectionError,
    SelectionEvaluationService,
    SelectionResult,
    SelectionService,
)

router = APIRouter(prefix="/api/selections", tags=["selections"])


class SelectionRequest(BaseModel):
    trading_day: date
    top_n: int = Field(default=5, ge=1, le=50)
    min_bars: int = Field(default=61, ge=61, le=500)
    lookback_days: int = Field(default=180, ge=61, le=1000)
    max_stale_days: int = Field(default=10, ge=1, le=30)
    exclude_st: bool = True
    adjust: str = Field(default="none", pattern="^(none|qfq|hfq)$")


def _serialize(result: SelectionResult) -> dict:
    return {
        "run_id": result.run_id,
        "trading_day": result.trading_day.isoformat(),
        "total_candidates": result.total_candidates,
        "eligible_count": result.eligible_count,
        "evaluation_horizon": result.evaluation_horizon,
        "evaluation_coverage": result.evaluation_coverage,
        "mean_forward_return": result.mean_forward_return,
        "median_forward_return": result.median_forward_return,
        "forward_win_rate": result.forward_win_rate,
        "evaluated_at": result.evaluated_at.isoformat() if result.evaluated_at else None,
        "candidates": [
            {
                "symbol": item.symbol,
                "name": item.name,
                "exchange": item.exchange,
                "board": item.board,
                "rank": item.rank,
                "score": item.score,
                "momentum_20": item.momentum_20,
                "momentum_60": item.momentum_60,
                "volatility_20": item.volatility_20,
                "max_drawdown_60": item.max_drawdown_60,
                "average_amount_20": item.average_amount_20,
                "last_price": item.last_price,
                "bar_count": item.bar_count,
                "entry_date": item.entry_date.isoformat() if item.entry_date else None,
                "exit_date": item.exit_date.isoformat() if item.exit_date else None,
                "forward_return": item.forward_return,
            }
            for item in result.candidates
        ],
        "disclaimer": "仅为量化研究候选，不构成投资建议，不会自动提交真实订单。",
    }


@router.post("/rank")
def rank_stocks(payload: SelectionRequest, db: Session = Depends(get_db)) -> dict:
    try:
        result = SelectionService(db).rank(
            payload.trading_day,
            SelectionConfig(
                top_n=payload.top_n,
                min_bars=payload.min_bars,
                lookback_days=payload.lookback_days,
                max_stale_days=payload.max_stale_days,
                exclude_st=payload.exclude_st,
                adjust=payload.adjust,
            ),
        )
    except SelectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _serialize(result)


@router.get("/evaluations/summary")
def get_evaluation_summary(
    limit: int = 50,
    min_coverage_ratio: float = 0.8,
    db: Session = Depends(get_db),
) -> dict:
    try:
        summary = SelectionEvaluationService(db).summarize(
            limit=limit,
            min_coverage_ratio=min_coverage_ratio,
        )
    except SelectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "evaluated_runs": summary.evaluated_runs,
        "candidate_observations": summary.candidate_observations,
        "mean_forward_return": summary.mean_forward_return,
        "median_forward_return": summary.median_forward_return,
        "forward_win_rate": summary.forward_win_rate,
        "average_coverage": summary.average_coverage,
        "average_rank_ic": summary.average_rank_ic,
        "average_turnover": summary.average_turnover,
    }


@router.get("")
def list_selection_runs(
    limit: int = 50,
    db: Session = Depends(get_db),
) -> dict:
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="limit 必须在 1..200")
    runs = db.scalars(
        select(SelectionRun)
        .order_by(SelectionRun.trading_day.desc(), SelectionRun.id.desc())
        .limit(limit)
    ).all()
    return {"items": [_serialize(SelectionService(db).get_run(run.id)) for run in runs]}


@router.get("/{run_id}")
def get_selection_run(run_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        return _serialize(SelectionService(db).get_run(run_id))
    except SelectionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{run_id}/evaluate")
def evaluate_selection_run(
    run_id: int,
    horizon_days: int = 20,
    min_coverage_ratio: float = 0.8,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = SelectionEvaluationService(db).evaluate(
            run_id,
            horizon_days=horizon_days,
            min_coverage_ratio=min_coverage_ratio,
        )
    except SelectionError as exc:
        status_code = 404 if "不存在" in str(exc) else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return _serialize(result)
