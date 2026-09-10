"""模拟券商（Paper Broker）。

处理模拟委托与成交，落实 A 股基本约束（100 股整数手、T+1、手续费），
并通过风控管理器进行下单前的风控检查。
不实现任何真实券商下单。
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    PaperAccount,
    PaperOrder,
    PaperPosition,
    PaperTrade,
)
from app.config import get_settings
from app.market_data.base import QuoteData
from app.paper_trading.portfolio import PortfolioService
from app.risk.risk_manager import RiskManager

logger = logging.getLogger(__name__)
settings = get_settings()

_MONEY_QUANT = Decimal("0.01")
_PRICE_QUANT = Decimal("0.0001")


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
                select(PaperTrade).where(
                    PaperTrade.account_id == account_id,
                    PaperTrade.signal_id == signal_id,
                )
            ).first()
            if existing:
                return None, "该信号已成交，不可重复下单"

        # 行情校验（数据过期、代码错配或异常价格均禁止成交）
        if quote is None:
            return None, "缺少行情数据"
        if quote.is_stale:
            return None, "行情数据过期，禁止成交"
        if quote.symbol != symbol:
            return None, "行情代码与委托代码不一致"
        received_at = quote.received_at
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=UTC)
        quote_age = (datetime.now(UTC) - received_at).total_seconds()
        if quote_age > settings.max_quote_age_seconds or quote_age < -5:
            return None, "行情时间异常或已过期，禁止成交"

        # 第一版只支持按当前盘口立即模拟成交，不接受客户端自报成交价。
        market_price = quote.ask_price if side == "BUY" else quote.bid_price
        if not math.isfinite(market_price) or market_price <= 0:
            market_price = quote.price
        if not math.isfinite(market_price) or market_price <= 0:
            return None, "行情价格无效，禁止成交"
        price = market_price

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
            return None, "成交失败，请检查服务日志"

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

        price_decimal = Decimal(str(price)).quantize(_PRICE_QUANT, rounding=ROUND_HALF_UP)
        value = price_decimal * quantity
        commission = max(
            value * Decimal(str(self._risk.limits.commission_rate)),
            Decimal(str(self._risk.limits.min_commission)),
        ).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
        stamp_tax = (
            value * Decimal(str(self._risk.limits.stamp_tax_rate))
            if side == "SELL"
            else Decimal("0")
        ).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)

        if side == "BUY":
            total_cost = value + commission
            account.available_cash = (account.available_cash - total_cost).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )
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
            old_total_cost = position.avg_cost * position.quantity
            new_quantity = position.quantity + quantity
            position.avg_cost = ((old_total_cost + total_cost) / new_quantity).quantize(
                _PRICE_QUANT, rounding=ROUND_HALF_UP
            )
            position.quantity = new_quantity
            # T+1：当日买入的股票不可卖，available_quantity 不增加
        else:
            total_proceeds = value - commission - stamp_tax
            account.available_cash = (account.available_cash + total_proceeds).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )
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
            price=price_decimal,
            commission=commission,
            stamp_tax=stamp_tax,
            signal_id=signal_id,
            executed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        return order, trade
