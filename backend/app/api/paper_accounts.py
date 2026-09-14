"""模拟交易接口。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

#: 北京时间（UTC+8，无夏令时）——与 app.market_rules.session_state.CST 同义，
#: 这里独立定义以避免 API 层反向依赖策略层
_CN_TZ = timezone(timedelta(hours=8))

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
                "realized_pnl": float(t.realized_pnl),
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
    trading_date: str | None = Query(
        None,
        description="交易日 (YYYY-MM-DD)；缺省按当前北京时间自动选择（已收盘用当日，否则上一交易日）。",
    ),
    force: bool = Query(False, description="是否强制重算（覆盖幂等记录）"),
    db: Session = Depends(get_db),
) -> dict:
    """日终结算：T+1 解冻 + 记录资产快照 + 写入幂等结算记录。

    同一 (account_id, trading_date) 重复结算只返回首次结果，确保可重入。
    """
    from datetime import date as _date

    from app.market_rules.calendar import TradingCalendar

    portfolio = PortfolioService(db)
    account = portfolio.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账户不存在")

    parsed_date: _date | None = None
    if trading_date:
        try:
            parsed_date = _date.fromisoformat(trading_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="trading_date 必须是 YYYY-MM-DD")
        # 未来日期一律拒绝：否则可以提前解冻 T+1 批次（实测缺口）
        today_cn = datetime.now(_CN_TZ).date()
        if parsed_date > today_cn:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"trading_date={parsed_date.isoformat()} 晚于今天（{today_cn.isoformat()}），"
                    "结算只能发生在已经过去的交易日"
                ),
            )
        calendar = TradingCalendar(db)
        if not calendar.is_trading_day(parsed_date):
            raise HTTPException(status_code=400, detail=f"{trading_date} 非交易日")

    positions = db.scalars(
        select(PaperPosition).where(PaperPosition.account_id == account_id)
    ).all()
    symbols = [p.symbol for p in positions]
    quotes: dict[str, QuoteData] = {}
    if symbols:
        provider_manager = request.app.state.provider_manager
        quotes = await provider_manager.get_quotes(symbols)

    settlement = DailySettlement(db)
    summary = settlement.settle_account(
        account, quotes, trading_date=parsed_date, force=force
    )
    return summary


# ──────────── 前向观察计时（P1-02） ────────────


class ForwardStartRequest(BaseModel):
    freeze_tag: str = Field(
        "", max_length=64, description="工程冻结标识；留空用当前默认冻结标签"
    )
    started_on: str | None = Field(None, description="计时起点 (YYYY-MM-DD)，默认今天（北京）")
    target_trading_days: int = Field(60, ge=1, le=500)
    account_id: int | None = None
    baseline_equity: float | None = None
    notes: str = Field("", max_length=255)


@router.get("/forward-observation")
def forward_observation(
    freeze_tag: str | None = Query(None, description="留空用当前默认冻结标签"),
    db: Session = Depends(get_db),
) -> dict:
    """前向模拟观察进度（研发计划 P1-02）。

    未登记起点时返回 ``registered=false`` 并说明原因，**不伪造天数**；
    达到目标天数不等于策略通过。
    """
    from app.paper_trading.forward import CURRENT_FREEZE_TAG, ForwardObservationService

    service = ForwardObservationService(db)
    return {
        "current_freeze_tag": CURRENT_FREEZE_TAG,
        "current": service.status(freeze_tag),
        "all": [service._serialize(row) for row in service.list_all()],
    }


@router.post("/forward-observation/start", status_code=201)
def forward_observation_start(
    body: ForwardStartRequest, db: Session = Depends(get_db)
) -> dict:
    """登记一次工程冻结的前向计时起点（按 freeze_tag 幂等）。

    改动成交/风控逻辑后必须用**新的** freeze_tag 重新登记，旧记录保留不清零。
    """
    from datetime import date as _date

    from app.paper_trading.forward import CURRENT_FREEZE_TAG, ForwardObservationService

    anchor = None
    if body.started_on:
        try:
            anchor = _date.fromisoformat(body.started_on)
        except ValueError:
            raise HTTPException(status_code=422, detail="started_on 必须是 YYYY-MM-DD")
    service = ForwardObservationService(db)
    return service.start(
        freeze_tag=body.freeze_tag or CURRENT_FREEZE_TAG,
        started_on=anchor,
        target_trading_days=body.target_trading_days,
        account_id=body.account_id,
        baseline_equity=body.baseline_equity,
        notes=body.notes,
    )


@router.post("/forward-observation/count")
def forward_observation_count(
    trading_date: str | None = Query(None, description="要计入的交易日 (YYYY-MM-DD)，默认今天（北京）"),
    freeze_tag: str | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    """把一个交易日计入前向观察（幂等；拒绝未来日与非交易日）。"""
    from datetime import date as _date

    from app.paper_trading.forward import CURRENT_FREEZE_TAG, ForwardObservationService

    target = None
    if trading_date:
        try:
            target = _date.fromisoformat(trading_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="trading_date 必须是 YYYY-MM-DD")
    service = ForwardObservationService(db)
    result = service.count_day(
        target, freeze_tag=freeze_tag or CURRENT_FREEZE_TAG
    )
    if result.get("counted") is False and "晚于今天" in str(result.get("reason", "")):
        raise HTTPException(status_code=422, detail=result["reason"])
    return result
