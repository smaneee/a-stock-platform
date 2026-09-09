"""模拟券商（Paper Broker）。

处理模拟委托与成交，落实 A 股基本约束（100 股整数手、T+1、手续费），
并通过风控管理器进行下单前的风控检查。
不实现任何真实券商下单。
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    PaperAccount,
    PaperOrder,
    PaperPosition,
    PaperTrade,
)
from app.market_data.base import QuoteData
from app.paper_trading.portfolio import PortfolioService
from app.risk.risk_manager import RiskManager

logger = logging.getLogger(__name__)


class PaperBroker:
    """模拟券商。"""

    def __init__(self, db: Session, risk_manager: RiskManager | None = None):
        self._db = db
        self._risk = risk_manager or RiskManager()
        self._portfolio = PortfolioService(db)

    def place_order(
        self,
        account_id: int,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        quote: QuoteData | None = None,
        signal_id: str | None = None,
    ) -> tuple[PaperOrder | None, str]:
        """下单并尝试成交，返回 (订单, 错误信息)。"""
        side = side.upper()
        if side not in ("BUY", "SELL"):
            return None, "无效的交易方向"

        account = self._portfolio.get_account(account_id)
        if account is None:
            return None, "账户不存在"

        # 数量校验
        if side == "BUY" and quantity % 100 != 0:
            return None, "买入数量必须为 100 股整数手"
        if quantity <= 0:
            return None, "数量必须大于 0"

        # 幂等检查：同一信号不能重复成交
        if signal_id:
            existing = self._db.scalars(
                select(PaperTrade).where(PaperTrade.signal_id == signal_id)
            ).first()
            if existing:
                return None, "该信号已成交，不可重复下单"

        # 行情校验（数据过期禁止成交）
        if quote is None:
            return None, "缺少行情数据"
        if quote.is_stale:
            return None, "行情数据过期，禁止成交"

        snapshot = self._portfolio.calculate_snapshot(account, {symbol: quote})

        if side == "BUY":
            order_value = quantity * price
            decision = self._risk.check_buy(snapshot, symbol, order_value, quote)
        else:
            position = self._portfolio.get_position(account_id, symbol)
            available = position.available_quantity if position else 0
            decision = self._risk.check_sell(snapshot, symbol, quantity, available, quote)

        if not decision.allowed:
            # 记录被拒委托
            order = PaperOrder(
                account_id=account_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                status="REJECTED",
                signal_id=signal_id,
            )
            self._db.add(order)
            self._db.commit()
            return None, decision.reason

        # 执行成交
        try:
            order, trade = self._execute(account, symbol, side, quantity, price, signal_id)
        except Exception as exc:  # noqa: BLE001
            self._db.rollback()
            logger.error("成交失败: %s", exc)
            return None, f"成交失败: {exc}"

        self._db.add(order)
        self._db.add(trade)
        self._db.commit()
        return order, ""

    def settle_t1(self, account_id: int) -> None:
        """日终结算：将全部持仓解冻为可卖（T+1 到期）。"""
        positions = self._db.scalars(
            select(PaperPosition).where(PaperPosition.account_id == account_id)
        ).all()
        for position in positions:
            position.available_quantity = position.quantity
        self._db.commit()


    def _execute(
        self,
        account: PaperAccount,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        signal_id: str | None,
    ) -> tuple[PaperOrder, PaperTrade]:
        """执行成交，更新账户与持仓。"""
        order = PaperOrder(
            account_id=account.id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            status="FILLED",
            signal_id=signal_id,
        )
        # 立即 flush 以获取自增 order.id，供成交记录引用
        self._db.add(order)
        self._db.flush()

        value = quantity * price
        commission = max(value * self._risk.limits.commission_rate, self._risk.limits.min_commission)
        stamp_tax = self._risk.stamp_tax(value) if side == "SELL" else 0.0

        if side == "BUY":
            total_cost = value + commission
            account.available_cash = Decimal(str(float(account.available_cash) - total_cost))
            account.frozen_cash = Decimal(str(float(account.frozen_cash)))
            # 更新持仓
            position = self._portfolio.get_position(account.id, symbol)
            if position is None:
                position = PaperPosition(
                    account_id=account.id,
                    symbol=symbol,
                    quantity=0,
                    available_quantity=0,
                    avg_cost=0,
                )
                self._db.add(position)
                self._db.flush()
            # 更新平均成本
            old_total_cost = float(position.avg_cost) * position.quantity
            new_quantity = position.quantity + quantity
            position.avg_cost = Decimal(str((old_total_cost + total_cost) / new_quantity))
            position.quantity = new_quantity
            # T+1：当日买入的股票不可卖，available_quantity 不增加
        else:
            total_proceeds = value - commission - stamp_tax
            account.available_cash = Decimal(str(float(account.available_cash) + total_proceeds))
            position = self._portfolio.get_position(account.id, symbol)
            if position is None:
                raise ValueError("持仓不存在")
            position.quantity -= quantity
            position.available_quantity -= quantity

        trade = PaperTrade(
            account_id=account.id,
            order_id=order.id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            commission=Decimal(str(commission)),
            stamp_tax=Decimal(str(stamp_tax)),
            signal_id=signal_id,
            executed_at=datetime.utcnow(),
        )
        return order, trade
