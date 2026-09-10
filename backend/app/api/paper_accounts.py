"""模拟交易接口。"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import PaperAccount, PaperOrder, PaperPosition, PaperTrade
from app.database.session import get_db
from app.market_data.base import QuoteData
from app.paper_trading.broker import PaperBroker
from app.paper_trading.portfolio import PortfolioService
from app.paper_trading.settlement import DailySettlement
from app.validation import validate_symbol

router = APIRouter(prefix="/api/paper", tags=["paper"])


class AccountCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    initial_cash: float = Field(100_000.0, gt=0)


class OrderCreate(BaseModel):
    account_id: int
    symbol: str
    side: Literal["BUY", "SELL", "buy", "sell"]
    quantity: int = Field(..., gt=0)
    price: float | None = Field(
        None,
        description="已弃用；第一版始终按服务端获取的当前盘口价模拟成交",
    )
    signal_id: str | None = Field(None, max_length=64)


@router.get("/accounts")
def list_accounts(db: Session = Depends(get_db)) -> list[dict]:
    """列出所有模拟账户。"""
    accounts = db.scalars(select(PaperAccount)).all()
    return [
        {
            "id": a.id,
            "name": a.name,
            "initial_cash": float(a.initial_cash),
            "available_cash": float(a.available_cash),
            "frozen_cash": float(a.frozen_cash),
        }
        for a in accounts
    ]


@router.post("/accounts", status_code=201)
def create_account(body: AccountCreate, db: Session = Depends(get_db)) -> dict:
    """创建模拟账户。"""
    account = PaperAccount(
        name=body.name,
        initial_cash=Decimal(str(body.initial_cash)),
        available_cash=Decimal(str(body.initial_cash)),
        frozen_cash=Decimal("0"),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return {"id": account.id, "name": account.name, "initial_cash": body.initial_cash}


@router.post("/orders")
async def place_order(
    body: OrderCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """模拟下单。"""
    if not validate_symbol(body.symbol):
        raise HTTPException(status_code=422, detail="非法股票代码")

    provider_manager = request.app.state.provider_manager
    quote = await provider_manager.get_quote(body.symbol)
    if quote is None:
        raise HTTPException(status_code=400, detail="无法获取行情，拒绝下单")

    broker = PaperBroker(db)
    order, error = broker.place_order(
        account_id=body.account_id,
        symbol=body.symbol,
        side=body.side,
        quantity=body.quantity,
        price=quote.price,
        quote=quote,
        signal_id=body.signal_id,
    )

    if order is None:
        raise HTTPException(status_code=400, detail=error)

    return {
        "order_id": order.id,
        "symbol": order.symbol,
        "side": order.side,
        "quantity": order.quantity,
        "price": float(order.price),
        "status": order.status,
    }


@router.get("/orders")
def list_orders(
    account_id: int,
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict:
    """查询委托单（含状态与拒绝原因）。"""
    orders = db.scalars(
        select(PaperOrder)
        .where(PaperOrder.account_id == account_id)
        .order_by(PaperOrder.created_at.desc())
        .limit(limit)
    ).all()
    return {
        "orders": [
            {
                "id": o.id,
                "symbol": o.symbol,
                "side": o.side,
                "quantity": o.quantity,
                "price": float(o.price),
                "status": o.status,
                "reject_reason": o.reject_reason,
                "signal_id": o.signal_id,
                "created_at": o.created_at.isoformat(),
            }
            for o in orders
        ]
    }


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, db: Session = Depends(get_db)) -> dict:
    """取消尚未成交的委托，释放冻结资金。"""
    broker = PaperBroker(db)
    order, error = broker.cancel_order(order_id)
    if order is None:
        raise HTTPException(status_code=409, detail=error)
    return {"id": order.id, "status": order.status}


@router.get("/positions")
def list_positions(
    account_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """查询持仓。"""
    positions = db.scalars(
        select(PaperPosition).where(PaperPosition.account_id == account_id)
    ).all()
    return {
        "positions": [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "available_quantity": p.available_quantity,
                "avg_cost": float(p.avg_cost),
                "realized_pnl": float(p.realized_pnl),
            }
            for p in positions
        ]
    }


@router.get("/trades")
def list_trades(
    account_id: int,
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict:
    """查询成交记录。"""
    trades = db.scalars(
        select(PaperTrade)
        .where(PaperTrade.account_id == account_id)
        .order_by(PaperTrade.executed_at.desc())
        .limit(limit)
    ).all()
    return {
        "trades": [
            {
                "id": t.id,
                "symbol": t.symbol,
                "side": t.side,
                "quantity": t.quantity,
                "price": float(t.price),
                "commission": float(t.commission),
                "stamp_tax": float(t.stamp_tax),
                "signal_id": t.signal_id,
                "executed_at": t.executed_at.isoformat(),
            }
            for t in trades
        ]
    }


@router.get("/accounts/{account_id}/assets")
async def account_assets(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """查询账户资产、持仓盈亏与资产曲线。"""
    portfolio = PortfolioService(db)
    account = portfolio.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账户不存在")

    # 拉取持仓最新行情，计算未实现盈亏
    positions = db.scalars(
        select(PaperPosition).where(PaperPosition.account_id == account_id)
    ).all()
    symbols = [p.symbol for p in positions]
    quotes: dict[str, QuoteData] = {}
    if symbols:
        provider_manager = request.app.state.provider_manager
        quotes = await provider_manager.get_quotes(symbols)

    unrealized = portfolio.unrealized_pnl(account_id, quotes)
    curve = portfolio.get_asset_curve(account_id)
    return {
        "account_id": account_id,
        "available_cash": float(account.available_cash),
        "frozen_cash": float(account.frozen_cash),
        "unrealized_pnl": unrealized,
        "asset_curve": curve,
    }


@router.post("/accounts/{account_id}/settle")
async def settle_account(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """日终结算：T+1 解冻 + 记录资产快照。"""
    portfolio = PortfolioService(db)
    account = portfolio.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账户不存在")

    positions = db.scalars(
        select(PaperPosition).where(PaperPosition.account_id == account_id)
    ).all()
    symbols = [p.symbol for p in positions]
    quotes: dict[str, QuoteData] = {}
    if symbols:
        provider_manager = request.app.state.provider_manager
        quotes = await provider_manager.get_quotes(symbols)

    settlement = DailySettlement(db)
    summary = settlement.settle_account(account, quotes)
    return summary
