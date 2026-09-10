"""模拟券商（Paper Broker）。

落实 A 股基本约束（100股股、T+1、手续费、印花税）与订单状态机：
委托经 SUBMITTED → FILLED / CANCELLED / REJECTED，买入冻结现金、成交结算、
取消/拒绝释放冻结。

T+1 严格按批次日期 enforcement：买入创建新 PaperPosition 批次
（acquisition_date = 交易日），可用数量 0；只有 acquisition_date 早于当前
交易日的批次才可卖出。日终结算（settle_t1）将所有批次可用数量恢复为持有量。

规则与板块/ST/新股上市判定统一经 SecurityMasterService + MarketRuleEngine，
回测与组合回测共享同一引擎。
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, date
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
from app.market_rules.rules import MarketRuleEngine, TICK_SIZE
from app.market_rules.security_master import SecurityMasterService
from app.paper_trading.portfolio import PortfolioService
from app.risk.risk_manager import RiskManager

logger = logging.getLogger(__name__)
settings = get_settings()

_MONEY_QUANT = Decimal("0.01")
_PRICE_QUANT = Decimal("0.0001")

SUBMITTED = "SUBMITTED"
FILLED = "FILLED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"


def _to_trading_date(d: date | None) -> date:
    return d if d is not None else date.today()


class PaperBroker:
    """模拟券商。"""

    def __init__(
        self,
        db: Session,
        risk_manager: RiskManager | None = None,
        rule_engine: MarketRuleEngine | None = None,
        security_master: SecurityMasterService | None = None,
    ):
        self._db = db
        self._risk = risk_manager or RiskManager()
        self._portfolio = PortfolioService(db)
        self._rule_engine = rule_engine or MarketRuleEngine()
        self._security_master = security_master or SecurityMasterService(db)

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
        trading_date: date | None = None,
    ) -> tuple[PaperOrder | None, str]:
        """下单并立即尝试成交（市价模型）。"""
        order, error = self.submit_order(
            account_id=account_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            quote=quote,
            signal_id=signal_id,
            trading_date=trading_date,
        )
        if order is None:
            return None, error

        submitted_id = order.id
        order, error = self.fill_order(submitted_id, quote, trading_date=trading_date)
        if order is None:
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
        trading_date: date | None = None,
    ) -> tuple[PaperOrder | None, str]:
        side = side.upper()
        account, reason = self._validate(
            account_id, symbol, side, quantity, quote, signal_id, trading_date
        )
        if account is None:
            return None, reason

        market_price = self._market_price(side, quote)
        snapshot = self._portfolio.calculate_snapshot(account, {symbol: quote})

        if side == "BUY":
            order_value = quantity * market_price
            decision = self._risk.check_buy(snapshot, symbol, order_value, quote)
        else:
            # 风控可用数量与 _validate 对齐：已 settle + T+1 严格合规
            td = trading_date or date.today()
            positions = self._db.scalars(
                select(PaperPosition).where(
                    PaperPosition.account_id == account_id,
                    PaperPosition.symbol == symbol,
                )
            ).all()
            sellable = 0
            for p in positions:
                if p.available_quantity > 0:
                    sellable += p.available_quantity
                elif p.acquisition_date is not None and p.acquisition_date < td:
                    sellable += p.quantity
            decision = self._risk.check_sell(snapshot, symbol, quantity, sellable, quote)

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
        self,
        order_id: int,
        quote: QuoteData | None = None,
        trading_date: date | None = None,
    ) -> tuple[PaperOrder | None, str]:
        td = _to_trading_date(trading_date)
        order = self._db.get(PaperOrder, order_id)
        if order is None:
            return None, "订单不存在"
        if order.status != SUBMITTED:
            return None, f"订单状态为 {order.status}，无法成交"

        account = self._db.get(PaperAccount, order.account_id)
        if account is None:
            return None, "账户不存在"

        if quote is None:
            return None, "缺少行情数据"
        reason = self._validate_quote(order.symbol, quote)
        if reason:
            return None, reason

        try:
            trade = self._settle_fill(order, account, td)
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

    def settle_t1(
        self,
        account_id: int,
        trading_date: date | None = None,
    ) -> int:
        """日终结算：将所有批次的可用数量恢复为持有量（T+1 到期）。"""
        _ = trading_date  # 保留参数位置以兼容未来按交易日过滤
        positions = self._db.scalars(
            select(PaperPosition).where(PaperPosition.account_id == account_id)
        ).all()
        for position in positions:
            position.available_quantity = position.quantity
        self._db.commit()
        return len(positions)

    # ──────── 内部：成交结算 ────────

    def _settle_fill(
        self, order: PaperOrder, account: PaperAccount, trading_date: date
    ) -> PaperTrade:
        security = self._security_master.ensure(order.symbol)
        # 规则解析（用于后续扩展：tick 校验、价格过滤等）
        _rules = self._rule_engine.get_rules_from_security(security, trading_date)

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
            freeze = self._freeze_amount("BUY", float(order.price), quantity)
            account.frozen_cash = (account.frozen_cash - freeze).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )
            self._create_buy_batch(
                account.id, order.symbol, trading_date, quantity,
                total_cost=value + commission,
            )
        else:
            total_proceeds = value - commission - stamp_tax
            account.available_cash = (account.available_cash + total_proceeds).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )
            realized_pnl = self._deduct_sell_batches(
                account.id, order.symbol, quantity, trading_date,
                price=price_decimal, commission=commission, stamp_tax=stamp_tax,
            )

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

    def _create_buy_batch(
        self,
        account_id: int,
        symbol: str,
        trading_date: date,
        quantity: int,
        total_cost: Decimal,
    ) -> PaperPosition:
        """创建新买入批次（T+1 冻结当日）。"""
        position = PaperPosition(
            account_id=account_id,
            symbol=symbol,
            quantity=quantity,
            available_quantity=0,
            avg_cost=total_cost / Decimal(quantity),
            realized_pnl=Decimal("0"),
            acquisition_date=trading_date,
        )
        self._db.add(position)
        self._db.flush()
        return position

    def _deduct_sell_batches(
        self,
        account_id: int,
        symbol: str,
        quantity: int,
        trading_date: date,
        price: Decimal,
        commission: Decimal,
        stamp_tax: Decimal,
    ) -> Decimal:
        """按 FIFO 从最早批次扣减持仓。

        批次可被扣减的条件（满足其一即可）：
        1) 已 settle_t1 解冻（available_quantity > 0）；
        2) 严格 T+1：acquisition_date 不为空且严格小于当前交易日。
        """
        # 阶段 1：所有可被卖的批次（按规则命中其一）
        candidates = self._db.scalars(
            select(PaperPosition)
            .where(
                PaperPosition.account_id == account_id,
                PaperPosition.symbol == symbol,
            )
            .order_by(PaperPosition.acquisition_date.asc().nullslast(), PaperPosition.id.asc())
        ).all()

        batches = []
        for b in candidates:
            if b.available_quantity > 0:
                # 已 settle 解冻：从 available_quantity 中扣减
                batches.append((b, b.available_quantity, "available"))
            elif b.acquisition_date is not None and b.acquisition_date < trading_date:
                # 严格 T+1：从 quantity 中扣减（available_quantity 视为补齐 quantity）
                b.available_quantity = b.quantity  # 视为当日已解冻，按 quantity 扣减
                batches.append((b, b.quantity, "t1"))

        if not batches:
            raise ValueError("无可卖批次（T+1 未到、账户无持仓或未结算）")

        remaining = quantity
        realized = Decimal("0")
        for batch, deductable, _kind in batches:
            if remaining <= 0:
                break
            take = min(deductable, remaining)
            if take <= 0:
                continue
            batch_cost = batch.avg_cost * Decimal(take)
            proceeds = price * Decimal(take)
            fee_share = (commission + stamp_tax) * Decimal(take) / Decimal(quantity)
            pnl = (proceeds - batch_cost - fee_share).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )
            batch.available_quantity -= take
            batch.quantity -= take
            batch.realized_pnl = (batch.realized_pnl + pnl).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )
            realized += pnl
            remaining -= take
            if batch.quantity <= 0:
                self._db.delete(batch)

        if remaining > 0:
            raise ValueError(
                f"可卖数量不足：还需 {remaining} 股（T+1 解冻后才能卖）"
            )
        return realized

    # ──────── 内部：校验与计算 ────────

    def _validate(
        self,
        account_id: int,
        symbol: str,
        side: str,
        quantity: int,
        quote: QuoteData | None,
        signal_id: str | None,
        trading_date: date | None = None,
    ) -> tuple[PaperAccount | None, str]:
        if side not in ("BUY", "SELL"):
            return None, "无效的交易方向"
        account = self._portfolio.get_account(account_id)
        if account is None:
            return None, "账户不存在"

        if side == "BUY":
            ok, reason = MarketRuleEngine.validate_buy_quantity(quantity)
            if not ok:
                return None, reason
        else:
            # 卖出端可用数量：已 settle 解冻的可直接用 available_quantity；
            # 未 settle 但满足严格 T+1（acquisition_date < 交易日）可按 quantity 计入。
            td = trading_date or date.today()
            positions = self._db.scalars(
                select(PaperPosition).where(
                    PaperPosition.account_id == account_id,
                    PaperPosition.symbol == symbol,
                )
            ).all()
            sellable = 0
            for p in positions:
                if p.available_quantity > 0:
                    sellable += p.available_quantity
                elif p.acquisition_date is not None and p.acquisition_date < td:
                    sellable += p.quantity
            ok, reason = MarketRuleEngine.validate_sell_quantity(quantity, sellable)
            if not ok:
                return None, reason

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
        if quote.price > 0:
            tick = TICK_SIZE
            try:
                price_dec = Decimal(str(quote.price))
                remainder = price_dec % tick
            except Exception:
                remainder = Decimal("0")
            if remainder != Decimal("0"):
                return f"行情价格不在最小报价单位 {tick} 上"
        return ""

    @staticmethod
    def _market_price(side: str, quote: QuoteData) -> float:
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
        if side != "BUY":
            return Decimal("0")
        value = Decimal(str(price)) * quantity
        return (value + self._commission(value)).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)