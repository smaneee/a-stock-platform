"""策略接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Strategy
from app.database.session import get_db
from app.strategies import registry

router = APIRouter(prefix="/api", tags=["strategies"])


def ensure_strategies(db: Session) -> None:
    """将注册表中的策略同步到数据库（按 name 唯一）。"""
    existing = {s.name for s in db.scalars(select(Strategy)).all()}
    for strategy in registry.get_all_strategies():
        if strategy.name not in existing:
            db.add(
                Strategy(
                    name=strategy.name,
                    description=strategy.description,
                    enabled=False,
                    version=strategy.version,
                )
            )
    db.commit()


@router.get("/strategies")
def list_strategies(db: Session = Depends(get_db)) -> list[dict]:
    """列出所有策略及其启用状态。"""
    ensure_strategies(db)
    strategies = db.scalars(select(Strategy).order_by(Strategy.id)).all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "enabled": s.enabled,
            "version": s.version,
        }
        for s in strategies
    ]


@router.post("/strategies/{strategy_id}/enable")
def enable_strategy(strategy_id: int, db: Session = Depends(get_db)) -> dict:
    """启用策略。"""
    strategy = db.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    strategy.enabled = True
    db.commit()
    return {"id": strategy.id, "name": strategy.name, "enabled": True}


@router.post("/strategies/{strategy_id}/disable")
def disable_strategy(strategy_id: int, db: Session = Depends(get_db)) -> dict:
    """禁用策略。"""
    strategy = db.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    strategy.enabled = False
    db.commit()
    return {"id": strategy.id, "name": strategy.name, "enabled": False}
