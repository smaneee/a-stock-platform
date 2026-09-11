"""User-confirmed paper rebalance endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import PaperRebalancePlan
from app.database.session import get_db
from app.paper_trading.rebalance import PaperRebalanceService, RebalanceError

router = APIRouter(prefix="/api/paper/rebalance-plans", tags=["paper-rebalance"])


class RebalancePlanCreate(BaseModel):
    account_id: int
    selection_run_id: int
    target_investment_ratio: float = Field(default=0.8, ge=0.1, le=0.8)
    max_symbol_weight: float = Field(default=0.2, ge=0.05, le=0.2)
    validation_override: bool = False


def _serialize(plan: PaperRebalancePlan) -> dict:
    return {
        "id": plan.id,
        "account_id": plan.account_id,
        "selection_run_id": plan.selection_run_id,
        "status": plan.status,
        "target_investment_ratio": plan.target_investment_ratio,
        "max_symbol_weight": plan.max_symbol_weight,
        "validation_override": plan.validation_override,
        "proposal": plan.proposal_json,
        "execution": plan.execution_json,
        "error_message": plan.error_message,
        "created_at": plan.created_at.isoformat(),
        "executed_at": plan.executed_at.isoformat() if plan.executed_at else None,
    }


@router.post("", status_code=201)
async def create_rebalance_plan(
    body: RebalancePlanCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    service = PaperRebalanceService(db)
    try:
        symbols = service.required_symbols(body.account_id, body.selection_run_id)
        quotes = await request.app.state.provider_manager.get_quotes(symbols)
        plan = service.create_plan(
            body.account_id,
            body.selection_run_id,
            quotes,
            target_investment_ratio=body.target_investment_ratio,
            max_symbol_weight=body.max_symbol_weight,
            validation_override=body.validation_override,
        )
    except RebalanceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _serialize(plan)


@router.get("")
def list_rebalance_plans(account_id: int, db: Session = Depends(get_db)) -> dict:
    plans = db.scalars(
        select(PaperRebalancePlan)
        .where(PaperRebalancePlan.account_id == account_id)
        .order_by(PaperRebalancePlan.created_at.desc())
        .limit(50)
    ).all()
    return {"items": [_serialize(plan) for plan in plans]}


@router.post("/{plan_id}/execute")
async def execute_rebalance_plan(
    plan_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    service = PaperRebalanceService(db)
    plan = db.get(PaperRebalancePlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="调仓方案不存在")
    symbols = sorted({item["symbol"] for item in plan.proposal_json.get("orders", [])})
    quotes = await request.app.state.provider_manager.get_quotes(symbols)
    try:
        return _serialize(service.execute_plan(plan_id, quotes))
    except RebalanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{plan_id}/cancel")
def cancel_rebalance_plan(plan_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        return _serialize(PaperRebalanceService(db).cancel_plan(plan_id))
    except RebalanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
