"""Read-only live-trading readiness endpoint."""
from __future__ import annotations

import importlib.util
import hmac
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.models import LiveRebalancePlan
from app.database.session import get_db
from app.live_trading.qmt_broker import QmtBrokerError, QmtLiveBroker
from app.live_trading.rebalance import LiveRebalanceError, LiveRebalanceService

router = APIRouter(prefix="/api/live", tags=["live-trading"])


@router.get("/status")
def live_trading_status() -> dict:
    settings = get_settings()
    path_configured = bool(settings.qmt_userdata_path)
    path_exists = path_configured and Path(settings.qmt_userdata_path).is_dir()
    account_configured = bool(settings.qmt_account_id)
    api_token_configured = len(settings.live_trading_api_token) >= 32
    sdk_available = importlib.util.find_spec("xtquant") is not None
    ready = bool(
        settings.real_trading_enabled
        and path_exists
        and account_configured
        and sdk_available
        and api_token_configured
    )
    return {
        "enabled": settings.real_trading_enabled,
        "ready": ready,
        "provider": "qmt",
        "path_configured": path_configured,
        "path_exists": path_exists,
        "account_configured": account_configured,
        "sdk_available": sdk_available,
        "api_token_configured": api_token_configured,
        "order_api_enabled": ready,
        "message": "实盘接口已就绪，仍需逐方案批准" if ready else "实盘保持锁定或配置不完整",
    }


class LivePlanCreate(BaseModel):
    selection_run_id: int
    target_investment_ratio: float = Field(default=0.8, ge=0.1, le=0.8)
    max_symbol_weight: float = Field(default=0.2, ge=0.05, le=0.2)


class LivePlanApproval(BaseModel):
    acknowledgement: str


def _require_enabled(live_trading_key: str) -> None:
    settings = get_settings()
    if not settings.real_trading_enabled:
        raise HTTPException(status_code=403, detail="REAL_TRADING_ENABLED=false")
    if len(settings.live_trading_api_token) < 32:
        raise HTTPException(status_code=503, detail="LIVE_TRADING_API_TOKEN 未安全配置")
    if not hmac.compare_digest(live_trading_key, settings.live_trading_api_token):
        raise HTTPException(status_code=403, detail="实盘访问密钥无效")


def _serialize(plan: LiveRebalancePlan) -> dict:
    return {
        "id": plan.id,
        "selection_run_id": plan.selection_run_id,
        "status": plan.status,
        "account_snapshot": plan.account_snapshot_json,
        "proposal": plan.proposal_json,
        "execution": plan.execution_json,
        "error_message": plan.error_message,
        "created_at": plan.created_at.isoformat(),
        "approved_at": plan.approved_at.isoformat() if plan.approved_at else None,
        "approval_expires_at": plan.approval_expires_at.isoformat()
        if plan.approval_expires_at
        else None,
        "executed_at": plan.executed_at.isoformat() if plan.executed_at else None,
    }


@router.get("/rebalance-plans")
def list_live_plans(
    live_trading_key: str = Header(..., alias="X-Live-Trading-Key"),
    db: Session = Depends(get_db),
) -> dict:
    _require_enabled(live_trading_key)
    settings = get_settings()
    service = LiveRebalanceService(db, settings.qmt_account_id)
    plans = db.scalars(
        select(LiveRebalancePlan)
        .where(LiveRebalancePlan.account_fingerprint == service.account_fingerprint)
        .order_by(LiveRebalancePlan.created_at.desc())
        .limit(50)
    ).all()
    return {"items": [_serialize(plan) for plan in plans]}


@router.post("/rebalance-plans", status_code=201)
async def create_live_plan(
    body: LivePlanCreate,
    request: Request,
    live_trading_key: str = Header(..., alias="X-Live-Trading-Key"),
    db: Session = Depends(get_db),
) -> dict:
    _require_enabled(live_trading_key)
    settings = get_settings()
    broker = QmtLiveBroker(settings)
    try:
        await broker.connect()
        account = await broker.query_account()
        service = LiveRebalanceService(db, settings.qmt_account_id)
        symbols = service.required_symbols(body.selection_run_id, account)
        quotes = await request.app.state.provider_manager.get_quotes(symbols)
        plan = service.create_plan(
            body.selection_run_id,
            account,
            quotes,
            target_investment_ratio=body.target_investment_ratio,
            max_symbol_weight=body.max_symbol_weight,
        )
        return _serialize(plan)
    except (QmtBrokerError, LiveRebalanceError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await broker.close()


@router.post("/rebalance-plans/{plan_id}/approve")
def approve_live_plan(
    plan_id: int,
    body: LivePlanApproval,
    live_trading_key: str = Header(..., alias="X-Live-Trading-Key"),
    db: Session = Depends(get_db),
) -> dict:
    _require_enabled(live_trading_key)
    try:
        plan, token = LiveRebalanceService(
            db, get_settings().qmt_account_id
        ).approve(plan_id, body.acknowledgement)
        return {"plan": _serialize(plan), "approval_token": token}
    except LiveRebalanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/rebalance-plans/{plan_id}/execute")
async def execute_live_plan(
    plan_id: int,
    request: Request,
    approval_token: str = Header(..., alias="X-Trade-Approval-Token"),
    live_trading_key: str = Header(..., alias="X-Live-Trading-Key"),
    db: Session = Depends(get_db),
) -> dict:
    _require_enabled(live_trading_key)
    settings = get_settings()
    broker = QmtLiveBroker(settings)
    try:
        await broker.connect()
        account = await broker.query_account()
        plan = db.get(LiveRebalancePlan, plan_id)
        if plan is None:
            raise LiveRebalanceError("实盘方案不存在")
        symbols = sorted(
            {item["symbol"] for item in plan.proposal_json.get("orders", [])}
        )
        quotes = await request.app.state.provider_manager.get_quotes(symbols)
        executed = await LiveRebalanceService(
            db, settings.qmt_account_id
        ).execute(plan_id, approval_token, broker, account, quotes)
        return _serialize(executed)
    except (QmtBrokerError, LiveRebalanceError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        await broker.close()


@router.post("/rebalance-plans/{plan_id}/reconcile")
async def reconcile_live_plan(
    plan_id: int,
    live_trading_key: str = Header(..., alias="X-Live-Trading-Key"),
    db: Session = Depends(get_db),
) -> dict:
    _require_enabled(live_trading_key)
    settings = get_settings()
    broker = QmtLiveBroker(settings)
    try:
        await broker.connect()
        orders = await broker.query_orders(remark_prefix=f"live-plan-{plan_id}")
        plan = LiveRebalanceService(
            db, settings.qmt_account_id
        ).reconcile(plan_id, orders)
        return _serialize(plan)
    except (QmtBrokerError, LiveRebalanceError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        await broker.close()
