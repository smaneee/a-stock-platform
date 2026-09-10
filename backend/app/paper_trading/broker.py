"""模拟券商（Paper Broker）。

落实 A 股基本约束（100 股整数手、T+1、手续费、印花税）与订单状态机：
委托经 SUBMITTED → FILLED / CANCELLED / REJECTED，买入冻结现金、成交结算、
取消/拒绝释放冻结。不实现任何真实券商下单。
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

# 订单状态机
SUBMITTED = "SUBMITTED"
FILLED = "FILLED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"


class PaperBroker:
    """模拟券商。"""

    def __init__(self, db: Session, risk_manager: RiskManager | None = None):
        self._db = db
        self._risk = risk_manager or RiskManager()
        self._portfolio = PortfolioService(db)

    # ──────── 一键市价下单（兼容旧接口） ────────

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
        """下单并立即尝试成交（市价模型），返回 (订单, 错误信息)。"""
        order, error = self.submit_order(
            account_id=account_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            quote=quote,
            signal_id=signal_id,
        )
        if order is None:
            return None, error

        submitted_id = order.id
        order, error = self.fill_order(submitted_id, quote)
        if order is None:
            # 成交失败：取消该委托以释放冻结
            self.cancel_order(submitted_id)
            return None, error
        return order, ""

    # ──────── 订单状态机 ────────

    def submit_order(
        self,
        account_id: int,
        symbol: str,
        side: str,
        quantity: int,
        quote: QuoteData | None = None,
        signal_id: str | None = None,
    ) -> tuple[PaperOrder | None, str]:
        """提交委托：校验 + 风控 + 冻结现金，返回 SUBMITTED 订单。

        未通过校验/风控时创建 REJECTED 订单（含原因），返回 (None, reason)。
        """
        side = side.upper()
        account, reason = self._validate(account_id, symbol, side, quantity, quote, signal_id)
        if account is None:
            return None, reason

        market_price = self._market_price(side, quote)
        snapshot = self._portfolio.calculate_snapshot(account, {symbol: quote})

        if side == "BUY":
            order_value = quantity * market_price
            decision = self._risk.check_buy(snapshot, symbol, order_value, quote)
        else:
            position = self._portfolio.get_position(account_id, symbol)
            available = position.available_quantity if position else 0
            decision = self._risk.check_sell(snapshot, symbol, quantity, available, quote)

        if not decision.allowed:
            order = PaperOrder(
                account_id=account_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=Decimal(str(market_price)).quantize(_PRICE_QUANT, rounding=ROUND_HALF_UP),
                status=REJECTED,
                reject_reason=decision.reason,
                signal_id=signal_id,
            )
            self._db.add(order)
            self._db.commit()
            return None, decision.reason

        # 买入冻结现金
        freeze = self._freeze_amount(side, market_price, quantity)
        if freeze > 0:
            account.available_cash = (account.available_cash - freeze).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )
            account.frozen_cash = (account.frozen_cash + freeze).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )

        order = PaperOrder(
            account_id=account_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=Decimal(str(market_price)).quantize(_PRICE_QUANT, rounding=ROUND_HALF_UP),
            status=SUBMITTED,
            signal_id=signal_id,
        )
        self._db.add(order)
        self._db.commit()
        self._db.refresh(order)
        return order, ""

    def fill_order(
        self, order_id: int, quote: QuoteData | None = None
    ) -> tuple[PaperOrder | None, str]:
        """成交 SUBMITTED 订单：结算冻结资金、更新持仓、生成成交记录。"""
        order = self._db.get(PaperOrder, order_id)
        if order is None:
            return None, "订单不存在"
        if order.status != SUBMITTED:
            return None, f"订单状态为 {order.status}，无法成交"

        account = self._db.get(PaperAccount, order.account_id)
        if account is None:
            return None, "账户不存在"

        # 成交前再次校验行情（新鲜、代码一致）
        if quote is None:
            return None, "缺少行情数据"
        reason = self._validate_quote(order.symbol, quote)
        if reason:
            return None, reason

        try:
            trade = self._settle_fill(order, account)
        except Exception as exc:  # noqa: BLE001
            self._db.rollback()
            logger.error("成交失败: %s", exc)
            return None, "成交失败，请检查服务日志"

        order.status = FILLED
        self._db.add(trade)
        self._db.commit()
        self._db.refresh(order)
        return order, ""

    def cancel_order(self, order_id: int) -> tuple[PaperOrder | None, str]:
        """取消 SUBMITTED 订单并释放冻结现金。"""
        order = self._db.get(PaperOrder, order_id)
        if order is None:
            return None, "订单不存在"
        if order.status != SUBMITTED:
            return None, f"订单状态为 {order.status}，无法取消"

        account = self._db.get(PaperAccount, order.account_id)
        if account is not None and order.side == "BUY":
            freeze = self._freeze_amount(order.side, float(order.price), order.quantity)
            account.frozen_cash = (account.frozen_cash - freeze).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )
            account.available_cash = (account.available_cash + freeze).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )

        order.status = CANCELLED
        self._db.commit()
        self._db.refresh(order)
        return order, ""

    # ──────── 日终结算 ────────

    def settle_t1(self, account_id: int) -> None:
        """日终结算：将全部持仓解冻为可卖（T+1 到期）。"""
        positions = self._db.scalars(
            select(PaperPosition).where(PaperPosition.account_id == account_id)
        ).all()
        for position in positions:
            position.available_quantity = position.quantity
        self._db.commit()

    # ──────── 内部：成交结算 ────────

    def _settle_fill(self, order: PaperOrder, account: PaperAccount) -> PaperTrade:
        """结算成交：更新现金、持仓与盈亏，返回成交记录。"""
        price_decimal = order.price.quantize(_PRICE_QUANT, rounding=ROUND_HALF_UP)
        quantity = order.quantity
        value = price_decimal * quantity
        commission = self._commission(value).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
        stamp_tax = (
            value * Decimal(str(self._risk.limits.stamp_tax_rate))
            if order.side == "SELL"
            else Decimal("0")
        ).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)

        realized_pnl = Decimal("0")

        if order.side == "BUY":
            # 释放冻结，结算买入
            freeze = self._freeze_amount("BUY", float(order.price), quantity)
            account.frozen_cash = (account.frozen_cash - freeze).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )
            # 现金已在提交时扣除，此处仅更新持仓
            position = self._get_or_create_position(account.id, order.symbol)
            old_total_cost = position.avg_cost * position.quantity
            new_quantity = position.quantity + quantity
            total_cost = value + commission
            position.avg_cost = ((old_total_cost + total_cost) / new_quantity).quantize(
                _PRICE_QUANT, rounding=ROUND_HALF_UP
            )
            position.quantity = new_quantity
            # T+1：当日买入不可卖
        else:
            total_proceeds = value - commission - stamp_tax
            account.available_cash = (account.available_cash + total_proceeds).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )
            position = self._portfolio.get_position(account.id, order.symbol)
            if position is None:
                raise ValueError("持仓不存在")
            realized_pnl = (
                (price_decimal - position.avg_cost) * quantity - commission - stamp_tax
            ).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
            position.quantity -= quantity
            position.available_quantity -= quantity
            position.realized_pnl = (
                position.realized_pnl + realized_pnl
            ).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)

        return PaperTrade(
            account_id=account.id,
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=price_decimal,
            commission=commission,
            stamp_tax=stamp_tax,
            realized_pnl=realized_pnl,
            signal_id=order.signal_id,
            executed_at=datetime.now(UTC).replace(tzinfo=None),
        )

    def _get_or_create_position(self, account_id: int, symbol: str) -> PaperPosition:
        position = self._portfolio.get_position(account_id, symbol)
        if position is None:
            position = PaperPosition(
                account_id=account_id,
                symbol=symbol,
                quantity=0,
                available_quantity=0,
                avg_cost=0,
                realized_pnl=0,
            )
            self._db.add(position)
            self._db.flush()
        return position

    # ──────── 内部：校验与计算 ────────

    def _validate(
        self,
        account_id: int,
        symbol: str,
        side: str,
        quantity: int,
        quote: QuoteData | None,
        signal_id: str | None,
    ) -> tuple[PaperAccount | None, str]:
        """基础校验，返回 (账户, 错误信息)。"""
        if side not in ("BUY", "SELL"):
            return None, "无效的交易方向"
        account = self._portfolio.get_account(account_id)
        if account is None:
            return None, "账户不存在"
        if side == "BUY" and quantity % 100 != 0:
            return None, "买入数量必须为 100 股整数手"
        if quantity <= 0:
            return None, "数量必须大于 0"
        if signal_id:
            existing = self._db.scalars(
                select(PaperTrade).where(
                    PaperTrade.account_id == account_id,
                    PaperTrade.signal_id == signal_id,
                )
            ).first()
            if existing:
                return None, "该信号已成交，不可重复下单"
        if quote is None:
            return None, "缺少行情数据"
        reason = self._validate_quote(symbol, quote)
        if reason:
            return None, reason
        return account, ""

    def _validate_quote(self, symbol: str, quote: QuoteData) -> str:
        """校验行情，返回错误信息（空串表示通过）。"""
        if quote.is_stale:
            return "行情数据过期，禁止成交"
        if quote.symbol != symbol:
            return "行情代码与委托代码不一致"
        received_at = quote.received_at
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=UTC)
        quote_age = (datetime.now(UTC) - received_at).total_seconds()
        if quote_age > settings.max_quote_age_seconds or quote_age < -5:
            return "行情时间异常或已过期，禁止成交"
        return ""

    @staticmethod
    def _market_price(side: str, quote: QuoteData) -> float:
        """取盘口价作为模拟成交价，异常时回退最新价。"""
        market_price = quote.ask_price if side == "BUY" else quote.bid_price
        if not math.isfinite(market_price) or market_price <= 0:
            market_price = quote.price
        if not math.isfinite(market_price) or market_price <= 0:
            return 0.0
        return market_price

    def _commission(self, value: Decimal) -> Decimal:
        rate = Decimal(str(self._risk.limits.commission_rate))
        min_comm = Decimal(str(self._risk.limits.min_commission))
        return max(value * rate, min_comm)

    def _freeze_amount(self, side: str, price: float, quantity: int) -> Decimal:
        """买入冻结金额 = 成交额 + 佣金；卖出不冻结现金。"""
        if side != "BUY":
            return Decimal("0")
        value = Decimal(str(price)) * quantity
        return (value + self._commission(value)).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
