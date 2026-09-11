"""Point-in-time selection API. Results are research candidates, not orders."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.selection import SelectionConfig, SelectionError, SelectionResult, SelectionService

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


@router.get("/{run_id}")
def get_selection_run(run_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        return _serialize(SelectionService(db).get_run(run_id))
    except SelectionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
