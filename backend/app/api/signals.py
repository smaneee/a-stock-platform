"""信号接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Signal
from app.database.session import get_db

router = APIRouter(prefix="/api", tags=["signals"])


@router.get("/signals")
def list_signals(
    symbol: str | None = Query(None, description="按股票代码过滤"),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict:
    """查询信号列表。"""
    stmt = select(Signal).order_by(Signal.created_at.desc()).limit(limit)
    if symbol:
        stmt = stmt.where(Signal.symbol == symbol)
    signals = db.scalars(stmt).all()
    return {
        "signals": [
            {
                "signal_id": s.signal_id,
                "symbol": s.symbol,
                "strategy_name": s.strategy_name,
                "direction": s.direction,
                "strength": float(s.strength),
                "reason": s.reason,
                "price": float(s.price),
                "source_time": s.source_time.isoformat(),
                "created_at": s.created_at.isoformat(),
                "strategy_version": s.strategy_version,
            }
            for s in signals
        ]
    }
