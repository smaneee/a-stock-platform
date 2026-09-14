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
from datetime import UTC, datetime, date
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP

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
from app.risk.limits import limits_from_settings
from app.risk.risk_manager import RiskManager

logger = logging.getLogger(__name__)
settings = get_settings()

_MONEY_QUANT = Decimal("0.01")
_PRICE_QUANT = Decimal("0.0001")

SUBMITTED = "SUBMITTED"
FILLED = "FILLED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"


def _to_trading_date(d: date | None, db: Session | None = None) -> date:
    """缺省交易日：**最近一个交易日**，而不是 `date.today()`。

    实测缺口（P1-02）：HTTP 下单/调仓不传 `trading_date` 时会回退到 `date.today()`，
    在周末/节假日调用会把买入批次记成周六（实测 acquisition_date=2026-09-13 周六），
    既污染 T+1 判定，也让「可卖数量」在日历上无意义。这里改为：若给了日期就用；
    否则用交易日历上「今天或之前最近的一个交易日」；日历为空时退回今天。
    """
    if d is not None:
        return d
    today = date.today()
    if db is None:
        return today
    try:
        from app.market_rules.calendar import TradingCalendar

        calendar = TradingCalendar(db)
        if not calendar.is_empty() and not calendar.is_trading_day(today):
            return calendar.last_trading_day_on_or_before(today)
    except Exception:  # noqa: BLE001 - 日历不可用时退回今天，不影响下单
        return today
    return today


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
        # 风控阈值来自配置（RISK_*），未显式注入时按 .env 构建
        self._risk = risk_manager or RiskManager(limits_from_settings(settings))
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
            td = _to_trading_date(trading_date, self._db)
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
        td = _to_trading_date(trading_date, self._db)
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
            trade = self._settle_fill(order, account, td, quote)
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

    def _fill_price(
        self, order: PaperOrder, trading_date: date, quote: QuoteData | None = None
    ) -> Decimal:
        """成交价 = 委托价 ± 滑点，且**不得越过涨跌停价**（与回测引擎同口径）。

        为什么需要（P1-02 登记缺口）：模拟盘此前直接用 `order.price` 成交，
        而回测引擎按 `slippage` 抬价/压价 —— 两套假设会让模拟盘业绩系统性偏乐观、
        且与回测不可比。滑点方向与回测一致：买入抬价、卖出压价。

        为什么必须夹到板价：触及涨跌停的委托已被 `_validate_executability` 拒绝，
        但**接近**板价的委托加滑点后可能越过板价，而按板价成交在真实市场里不可能
        （涨停买不到、跌停卖不掉）。因此做 `min/max` 截断，与 `try_fill` 一致。
        """
        slippage = float(self._risk.limits.slippage)
        price = float(order.price)
        if slippage > 0:
            price = price * (1 + slippage) if order.side == "BUY" else price * (1 - slippage)
        price_decimal = Decimal(str(price)).quantize(_PRICE_QUANT, rounding=ROUND_HALF_UP)

        previous_close = float(getattr(quote, "previous_close", 0.0) or 0.0)
        if previous_close > 0:
            security = self._security_master.ensure(order.symbol)
            rules = self._rule_engine.get_rules_from_security(security, trading_date)
            if rules.has_price_limit:
                prev = Decimal(str(previous_close))
                limit_up, limit_down = rules.limit_up(prev), rules.limit_down(prev)
                if order.side == "BUY" and limit_up is not None:
                    price_decimal = min(price_decimal, limit_up.quantize(_PRICE_QUANT))
                if order.side == "SELL" and limit_down is not None:
                    price_decimal = max(price_decimal, limit_down.quantize(_PRICE_QUANT))
        return price_decimal

    def _settle_fill(
        self,
        order: PaperOrder,
        account: PaperAccount,
        trading_date: date,
        quote: QuoteData | None = None,
    ) -> PaperTrade:
        security = self._security_master.ensure(order.symbol)
        # 规则解析（与 _validate_executability 同一引擎：板块/ST/新股决定板价）
        _rules = self._rule_engine.get_rules_from_security(security, trading_date)

        price_decimal = self._fill_price(order, trading_date, quote)
        quantity = order.quantity
        value = price_decimal * quantity
        commission = self._commission(value).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
        stamp_tax = (
            value * Decimal(str(self._risk.limits.stamp_tax_rate))
            if order.side == "SELL"
            else Decimal("0")
        ).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
        # 过户费：**双边**收取（与回测 ExecutionConfig.transfer_fee_rate 同口径）
        transfer_fee = (
            value * Decimal(str(self._risk.limits.transfer_fee_rate))
        ).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)

        realized_pnl = Decimal("0")

        if order.side == "BUY":
            # 冻结额按**含滑点**的挂单价上限计提（见 _freeze_amount），因此实际成本
            # 不会超过冻结额；差额退回可用资金。此前实现把「冻结额 = 实际成本」写死，
            # 一旦引入滑点就会出现「持仓成本 > 现金支出」的账目缺口。
            freeze = self._freeze_amount("BUY", float(order.price), quantity)
            actual_cost = value + commission + transfer_fee
            account.frozen_cash = (account.frozen_cash - freeze).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )
            refund = (freeze - actual_cost).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
            account.available_cash = (account.available_cash + refund).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )
            self._create_buy_batch(
                account.id, order.symbol, trading_date, quantity,
                total_cost=actual_cost,
            )
        else:
            total_proceeds = value - commission - stamp_tax - transfer_fee
            account.available_cash = (account.available_cash + total_proceeds).quantize(
                _MONEY_QUANT, rounding=ROUND_HALF_UP
            )
            realized_pnl = self._deduct_sell_batches(
                account.id, order.symbol, quantity, trading_date,
                price=price_decimal, commission=commission, stamp_tax=stamp_tax,
                transfer_fee=transfer_fee,
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
            transfer_fee=transfer_fee,
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
        transfer_fee: Decimal = Decimal("0"),
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
            fee_share = (commission + stamp_tax + transfer_fee) * Decimal(take) / Decimal(quantity)
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
            td = _to_trading_date(trading_date, self._db)
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
        reason = self._validate_executability(symbol, side, quote, trading_date)
        if reason:
            return None, reason
        return account, ""

    def _validate_executability(
        self,
        symbol: str,
        side: str,
        quote: QuoteData,
        trading_date: date | None,
    ) -> str:
        """可成交性硬校验：停牌/无效价格与涨跌停。

        背景（P1-02 实测缺陷）：

        * ``price=0`` 的停牌行情能绕过资金与仓位检查，以 0 元成交并生成持仓；
        * 2026-09-11 封板未开的涨停股 002161 被按涨停价 8.04 全额买入 —— 缺少盘口
          排队证据时，这是**系统性乐观**假设。

        保守口径（与回测引擎一致）：缺少盘口证据时，**以涨停价买入 / 以跌停价卖出
        视为不可成交**。区分一字板与盘中开板需要分时/盘口数据，这里不具备，因此按
        最保守的处理并给出明确原因，而不是静默放行。
        """
        if quote.price <= 0:
            return "停牌或无有效行情（最新价为 0），禁止成交"

        previous_close = getattr(quote, "previous_close", 0.0) or 0.0
        if previous_close <= 0:
            # 没有昨收就无法判断涨跌停。这里不静默假设一个昨收，交由上层风控继续把关。
            return ""

        security = self._security_master.ensure(symbol)
        rules = self._rule_engine.get_rules_from_security(
            security, _to_trading_date(trading_date, self._db)
        )
        if not rules.has_price_limit:
            return ""
        prev = Decimal(str(previous_close))
        limit_up = rules.limit_up(prev)
        limit_down = rules.limit_down(prev)
        if limit_up is None or limit_down is None:
            return ""

        price = float(quote.price)
        if side == "BUY" and price >= float(limit_up) - 1e-9:
            return (
                f"价格 {price:.2f} 已达涨停价 {float(limit_up):.2f}：缺少盘口排队证据时"
                "视为不可成交（保守假设）"
            )
        if side == "SELL" and price <= float(limit_down) + 1e-9:
            return (
                f"价格 {price:.2f} 已达跌停价 {float(limit_down):.2f}：缺少盘口排队证据时"
                "视为不可成交（保守假设）"
            )
        return ""

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
                # 行情源的 price 是 float（真实盘口会出现 5.5200000000000005 这类
                # 二进制浮点尾差），直接 Decimal(str(price)) % 0.01 会把合法价格误判
                # 为「不在最小报价单位上」而拒绝全部下单。这里改为比较「与最近整数
                # tick 的偏离量」，容差 1e-6：10.005 仍被拒绝，5.5200000000000005 通过。
                ticks = Decimal(str(quote.price)) / tick
                remainder = abs(ticks - ticks.to_integral_value(rounding=ROUND_HALF_UP))
            except Exception:  # noqa: BLE001
                remainder = Decimal("0")
            if remainder > Decimal("1e-6"):
                return f"行情价格不在最小报价单位 {tick} 上"
        return ""

    @staticmethod
    def _market_price(side: str, quote: QuoteData) -> float:
        # 与调仓共用同一套「盘口缺失按最新价兜底」规则
        return quote.execution_price(side)

    def _commission(self, value: Decimal) -> Decimal:
        rate = Decimal(str(self._risk.limits.commission_rate))
        min_comm = Decimal(str(self._risk.limits.min_commission))
        return max(value * rate, min_comm)

    def _freeze_amount(self, side: str, price: float, quantity: int) -> Decimal:
        """买入冻结额：按**含滑点**的最坏成交价计提，保证实际成本不会超过冻结额。

        不含滑点会在引入滑点后造成「冻结额 < 实际成本」→ 成交时现金无处可扣，
        账目出现缺口（持仓成本高于现金支出）。因此冻结按上限价计提，成交后差额退回。
        """
        if side != "BUY":
            return Decimal("0")
        slippage = float(self._risk.limits.slippage)
        fee_rate = float(self._risk.limits.transfer_fee_rate)
        worst_price = price * (1 + slippage) if slippage > 0 else price
        # 向上取整到 4 位小数：成交价会被 quantize 到 4 位，若这里向下取整，
        # 极端尾差下可能出现「实际成本 > 冻结额」，成交时现金无处可扣。
        worst = Decimal(str(worst_price)).quantize(_PRICE_QUANT, rounding=ROUND_UP)
        value = worst * quantity
        # 冻结额必须覆盖**全部**买入成本：成交额 + 佣金 + 过户费
        # （漏掉过户费会导致冻结额比实际成本少几分钱，退款计算变成负数）
        transfer_fee = (value * Decimal(str(fee_rate))).quantize(
            _MONEY_QUANT, rounding=ROUND_UP
        )
        return (value + self._commission(value) + transfer_fee).quantize(
            _MONEY_QUANT, rounding=ROUND_HALF_UP
        )
