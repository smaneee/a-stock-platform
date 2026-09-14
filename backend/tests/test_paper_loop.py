"""P1-02 模拟盘闭环测试：现金守恒、T+1、费用手算、风控、幂等、撤单。

全部使用 conftest 提供的内存 SQLite（DATABASE_URL=sqlite:///:memory:，
MARKET_PROVIDERS=mock，UNIVERSE_PROVIDERS=mock），不接触真实数据库、
不连接任何真实交易通道。

已知缺口用 ``pytest.mark.xfail(strict=True)`` 标注：断言的是**期望行为**，
当前实现不满足，因此记为 xfail；一旦修复会立即变成 XPASS 失败，防止悄悄
"通过"。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database.models import (
    PaperAccount,
    PaperOrder,
    PaperPosition,
    PaperTrade,
)
from app.market_data.base import QuoteData
from app.paper_trading.broker import PaperBroker
from app.paper_trading.portfolio import PortfolioService
from app.risk.limits import RiskLimits
from app.risk.risk_manager import AccountSnapshot, RiskManager
from app.time_utils import utc_now

COMMISSION_RATE = Decimal("0.0003")
MIN_COMMISSION = Decimal("5")
STAMP_TAX_RATE = Decimal("0.0005")


# ──────────────────────────── helpers ────────────────────────────


def _q(
    symbol: str = "600000",
    price: float = 10.0,
    *,
    previous_close: float | None = None,
    is_stale: bool = False,
) -> QuoteData:
    """盘口缺失口径（东财/AKShare 无五档）：bid=ask=最新价，成交价即最新价。

    与生产一致：eastmoney_provider 的 bid/ask 统一返回 0，由 QuoteData.execution_price
    按最新价兜底。用带买卖价差的 make_quote 会凭空引入 0.01 元的「半个价差」，
    与真实成交价不符。
    """
    return QuoteData(
        symbol=symbol,
        name=f"测试股{symbol}",
        price=price,
        open=price,
        high=price,
        low=price,
        previous_close=price if previous_close is None else previous_close,
        volume=1_000_000.0,
        amount=price * 1_000_000.0,
        bid_price=price,
        ask_price=price,
        source="mock",
        market_time=utc_now(),
        received_at=utc_now(),
        is_stale=is_stale,
    )


def _account(db, cash: float = 1_000_000.0) -> PaperAccount:
    account = PaperAccount(
        name="P1-02 测试账户",
        initial_cash=Decimal(str(cash)),
        available_cash=Decimal(str(cash)),
        frozen_cash=Decimal("0"),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _broker(db) -> PaperBroker:
    return PaperBroker(db)


def _positions(db, account_id: int) -> list[PaperPosition]:
    return list(
        db.scalars(select(PaperPosition).where(PaperPosition.account_id == account_id)).all()
    )


def _orders(db, account_id: int) -> list[PaperOrder]:
    return list(db.scalars(select(PaperOrder).where(PaperOrder.account_id == account_id)).all())


def _trades(db, account_id: int) -> list[PaperTrade]:
    return list(db.scalars(select(PaperTrade).where(PaperTrade.account_id == account_id)).all())


def _suspended_quote(symbol: str = "600000") -> QuoteData:
    """停牌口径：无最新价、无盘口（全 0）。"""
    return QuoteData(symbol=symbol, name="停牌股", price=0.0, previous_close=0.0,
                     bid_price=0.0, ask_price=0.0, source="mock", received_at=utc_now())


def _total_asset(db, account_id: int, quotes: dict | None = None) -> Decimal:
    """总资产 = 可用现金 + 冻结现金 + 持仓市值（市值按传入行情，缺行情退化到成本）。"""
    portfolio = PortfolioService(db)
    account = portfolio.get_account(account_id)
    snapshot = portfolio.calculate_snapshot(account, quotes or {})
    return Decimal(str(snapshot.total_asset))


# ─────────────────────── 1. 现金守恒 ───────────────────────


class TestCashConservation:
    def test_buy_then_sell_keeps_cash_exactly_minus_fees(self, db_session):
        """买入 → 结算 → 卖出后：现金 = 初始 − 买入成本 − 卖出净得差额。

        2026-09-14 起模拟盘与回测同口径收费：滑点 `RISK_SLIPPAGE=0.0005`（抬价/压价）
        + 过户费 `RISK_TRANSFER_FEE_RATE=0.00001`（**双边**）。手算（100 股 @ 委托价 10.00）：

        买入成交价 10.0050 → 成交额 1000.50 + 佣金 5.00 + 过户费 0.01 = **1005.51**；
        卖出成交价 9.9950 → 999.50 − 佣金 5.00 − 印花税 0.50 − 过户费 0.01 = **993.99**。
        """
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=10.0)
        broker.settle_t1(account.id)  # 空仓结算不应报错

        buy, err = broker.place_order(account.id, "600000", "BUY", 100, quote.price, quote=quote)
        assert buy is not None, err
        assert buy.status == "FILLED"

        db_session.refresh(account)
        # 1e6 − (1000.50 + 5.00 + 0.01)
        assert Decimal(account.available_cash) == Decimal("998994.49")
        assert Decimal(account.frozen_cash) == Decimal("0")
        trade_buy = _trades(db_session, account.id)[0]
        assert Decimal(trade_buy.price) == Decimal("10.0050")  # 滑点抬价 5bp
        assert Decimal(trade_buy.transfer_fee) == Decimal("0.01")  # 过户费双边
        # 买入费用已资本化进持仓成本：1005.51 / 100 = 10.0551 → 10.06
        position = _positions(db_session, account.id)[0]
        assert Decimal(position.avg_cost).quantize(Decimal("0.01")) == Decimal("10.06")
        # 无价格波动时：总资产 = 初始 − 买入费用 + 市值 = 998994.49 + 1000
        assert _total_asset(db_session, account.id, {"600000": quote}) == Decimal("999994.49")

        broker.settle_t1(account.id)
        sell, err = broker.place_order(account.id, "600000", "SELL", 100, quote.price, quote=quote)
        assert sell is not None, err
        trade_sell = _trades(db_session, account.id)[1]
        assert Decimal(trade_sell.price) == Decimal("9.9950")  # 滑点压价 5bp
        assert Decimal(trade_sell.transfer_fee) == Decimal("0.01")
        db_session.refresh(account)
        # 998994.49 + (999.50 − 5.00 − 0.50 − 0.01)
        assert Decimal(account.available_cash) == Decimal("999988.48")
        assert _positions(db_session, account.id) == []
        assert _total_asset(db_session, account.id, {"600000": quote}) == Decimal("999988.48")

        # 往返净变化 = 993.99 − 1005.51 = −11.52
        # （显性费用：佣金 5.00×2 + 印花税 0.50 + 过户费 0.01×2 = 10.52；其余 1.00 来自两侧滑点）
        assert Decimal(trade_sell.realized_pnl) == Decimal("-11.52")

    def test_snapshot_includes_frozen_cash(self, db_session):
        """冻结资金计入总资产，撤单前总资产不因冻结而减少。"""
        account = _account(db_session, 10_000.0)
        broker = _broker(db_session)
        order, err = broker.submit_order(
            account.id, "600000", "BUY", 100, quote=_q(symbol="600000", price=10.0)
        )
        assert order is not None, err
        db_session.refresh(account)
        # 冻结按含滑点上限价计提，并覆盖全部买入成本：1000.50 + 佣金 5.00 + 过户费 0.02（向上取整）
        assert Decimal(account.frozen_cash) == Decimal("1005.52")
        assert Decimal(account.available_cash) == Decimal("8994.48")
        assert _total_asset(db_session, account.id, {"600000": _q(symbol="600000", price=10.0)}) == Decimal("10000.00")


# ─────────────────────── 2. T+1 ───────────────────────


class TestT1:
    def test_same_day_buy_not_sellable_then_unlocked_by_settle(self, db_session):
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=10.0)

        buy, err = broker.place_order(account.id, "600000", "BUY", 100, quote.price, quote=quote)
        assert buy is not None, err
        position = _positions(db_session, account.id)[0]
        assert position.quantity == 100
        assert position.available_quantity == 0  # 当日买入：可卖 0

        sell, reason = broker.place_order(account.id, "600000", "SELL", 100, quote.price, quote=quote)
        assert sell is None
        assert "可用持仓" in reason

        unlocked = broker.settle_t1(account.id)
        assert unlocked == 1
        position = _positions(db_session, account.id)[0]
        assert position.available_quantity == position.quantity == 100

        sell, err = broker.place_order(account.id, "600000", "SELL", 100, quote.price, quote=quote)
        assert sell is not None, err

    def test_strict_t1_when_trading_date_is_passed(self, db_session):
        """显式传入交易日时，跨交易日可卖（不依赖 settle_t1）。"""
        account = _account(db_session, 500_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=10.0)

        buy, err = broker.place_order(account.id, "600000", "BUY", 100, quote.price,
                                      quote=quote, trading_date=date(2026, 9, 11))
        assert buy is not None, err
        position = _positions(db_session, account.id)[0]
        assert position.acquisition_date == date(2026, 9, 11)
        assert position.available_quantity == 0

        same_day, reason = broker.place_order(account.id, "600000", "SELL", 100, quote.price,
                                              quote=quote, trading_date=date(2026, 9, 11))
        assert same_day is None and "可用持仓" in reason

        next_day, err = broker.place_order(account.id, "600000", "SELL", 100, quote.price,
                                           quote=quote, trading_date=date(2026, 9, 14))
        assert next_day is not None, err

    def test_position_exposes_quantity_and_available_quantity(self, db_session):
        """持仓「持有数量 / 可卖数量」分列：quantity 与 available_quantity。

        注意：模型只有这两列，没有独立的 sellable/frozen 列，
        「可卖数量」就是 available_quantity。
        """
        columns = {c.name for c in PaperPosition.__table__.columns}
        assert {"quantity", "available_quantity"} <= columns
        assert not {"sellable_quantity", "frozen_quantity"} & columns


# ─────────────────────── 3. 成本手算 ───────────────────────


class TestCosts:
    def test_min_commission_on_small_buy(self, db_session):
        """小额成交佣金取最低佣金 5 元，而不是 0.03% 的乘积。"""
        account = _account(db_session, 100_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=4.0)  # 400 元成交额

        order, err = broker.place_order(account.id, "600000", "BUY", 100, quote.price, quote=quote)
        assert order is not None, err
        trade = _trades(db_session, account.id)[0]
        assert Decimal(trade.commission) == MIN_COMMISSION
        assert Decimal(trade.commission) != (Decimal("400") * COMMISSION_RATE)

    def test_commission_rate_above_minimum(self, db_session):
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=200.0)  # 20000 元 → 佣金 6 元 > 最低 5 元

        order, err = broker.place_order(account.id, "600000", "BUY", 100, quote.price, quote=quote)
        assert order is not None, err
        trade = _trades(db_session, account.id)[0]
        assert Decimal(trade.commission) == (Decimal("20000") * COMMISSION_RATE).quantize(Decimal("0.01"))
        assert Decimal(trade.commission) == Decimal("6.00")
        assert Decimal(trade.commission) > MIN_COMMISSION

    def test_stamp_tax_only_on_sell(self, db_session):
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=10.0)

        broker.place_order(account.id, "600000", "BUY", 1000, quote.price, quote=quote)
        buy_trade = _trades(db_session, account.id)[0]
        assert Decimal(buy_trade.stamp_tax) == Decimal("0")

        broker.settle_t1(account.id)
        broker.place_order(account.id, "600000", "SELL", 1000, quote.price, quote=quote)
        sell_trade = [t for t in _trades(db_session, account.id) if t.side == "SELL"][0]
        assert Decimal(sell_trade.stamp_tax) == (
            Decimal("10000") * STAMP_TAX_RATE
        ).quantize(Decimal("0.01"))
        assert Decimal(sell_trade.stamp_tax) == Decimal("5.00")

    def test_slippage_and_transfer_fee_are_charged_by_default(self, db_session):
        """模型现状：**滑点与过户费都默认计**，与回测引擎同口径。

        2026-09-14 变更：此前模拟盘既不计滑点也不计过户费（`PaperTrade` 连列都没有），
        相对回测（`slippage=0.0005`、`transfer_fee_rate=0.00001` 双边）系统性低估成本。
        现在：买入成交价 = 委托价 × 1.0005 = 10.0050（成交额 1000.50，非 1000.00），
        过户费 = 1000.50 × 0.00001 = 0.01。
        """
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=10.0)
        order, err = broker.place_order(account.id, "600000", "BUY", 100, quote.price, quote=quote)
        assert order is not None, err
        trade = _trades(db_session, account.id)[0]
        assert Decimal(trade.price) == Decimal("10.0050")  # 5bp 滑点
        assert Decimal(trade.price) * trade.quantity == Decimal("1000.5000")
        assert Decimal(trade.transfer_fee) == Decimal("0.01")  # 过户费已单列
        assert Decimal(trade.stamp_tax) == Decimal("0")        # 买入不收印花税

    def test_transfer_fee_is_charged_on_both_sides(self, db_session):
        """过户费**双边**收取：买卖各 0.001%（与回测 `try_fill` 同口径）。"""
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=10.0)
        broker.place_order(account.id, "600000", "BUY", 1000, quote.price, quote=quote)
        broker.settle_t1(account.id)
        broker.place_order(account.id, "600000", "SELL", 1000, quote.price, quote=quote)
        trades = _trades(db_session, account.id)
        buy_trade, sell_trade = trades[0], trades[1]
        expected_buy = (Decimal(buy_trade.price) * 1000 * Decimal("0.00001")).quantize(
            Decimal("0.01")
        )
        expected_sell = (Decimal(sell_trade.price) * 1000 * Decimal("0.00001")).quantize(
            Decimal("0.01")
        )
        assert Decimal(buy_trade.transfer_fee) == expected_buy
        assert Decimal(sell_trade.transfer_fee) == expected_sell
        assert expected_buy > 0 and expected_sell > 0

    def test_slippage_can_be_disabled_to_restore_legacy_prices(self, db_session):
        """`RISK_SLIPPAGE=0` 时恢复「成交价 == 委托价」（滑点可回退）。

        `RiskLimits` 是 frozen dataclass，改不了字段，因此这里**显式注入**
        一个 slippage=0 的 `RiskManager` —— 这也是生产里 `RISK_SLIPPAGE=0` 生效的路径
        （`limits_from_settings` 从配置读出后构造 RiskManager）。
        注意过户费仍按默认收取（它无法用滑点开关关掉，需单独设 `RISK_TRANSFER_FEE_RATE=0`）。
        """
        from dataclasses import replace

        from app.paper_trading.broker import PaperBroker
        from app.risk.risk_manager import RiskManager

        account = _account(db_session, 1_000_000.0)
        base = _broker(db_session)
        no_slip_limits = replace(base._risk.limits, slippage=0.0)
        broker = PaperBroker(db_session, risk_manager=RiskManager(no_slip_limits))

        quote = _q(symbol="600000", price=10.0)
        order, err = broker.place_order(account.id, "600000", "BUY", 100, quote.price, quote=quote)
        assert order is not None, err
        trade = _trades(db_session, account.id)[0]
        assert Decimal(trade.price) == Decimal("10.0000")
        db_session.refresh(account)
        # 1e6 − 1000 − 佣金 5.00 − 过户费 0.01
        assert Decimal(account.available_cash) == Decimal("998994.99")

    def test_tick_size_float_noise_is_tolerated(self, db_session):
        """回归：真实行情源的 float 尾差（5.5200000000000005）不应被误判为非 tick。"""
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600028", price=5.5200000000000005)
        order, err = broker.place_order(account.id, "600028", "BUY", 100, quote.price, quote=quote)
        assert order is not None, err
        assert Decimal(order.price) == Decimal("5.5200")

    def test_tick_size_misaligned_price_is_rejected(self, db_session):
        """真正的非 tick 价格（10.005）仍然必须被拒绝。"""
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=10.005)
        order, reason = broker.place_order(account.id, "600000", "BUY", 100, quote.price, quote=quote)
        assert order is None
        assert "最小报价单位" in reason


# ─────────────────────── 4. 风控拦截 ───────────────────────


class TestRiskBlocks:
    def test_single_symbol_position_cap(self, db_session):
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=160.0)  # 2000 股 = 320000 = 32%
        order, reason = broker.submit_order(account.id, "600000", "BUY", 2000, quote=quote)
        assert order is None
        assert "单只股票仓位将超过 20%" in reason
        rejected = [o for o in _orders(db_session, account.id) if o.status == "REJECTED"]
        assert len(rejected) == 1
        assert "单只股票仓位将超过 20%" in rejected[0].reject_reason
        db_session.refresh(account)
        assert Decimal(account.available_cash) == Decimal("1000000")  # 未冻结

    def test_total_position_cap(self, db_session):
        """4 只 16% 建仓后，第 5 只 16% 会突破 80% 总仓位上限。"""
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        for symbol in ("600001", "600002", "600003", "600004"):
            quote = _q(symbol=symbol, price=160.0)
            order, err = broker.place_order(account.id, symbol, "BUY", 1000, quote.price, quote=quote)
            assert order is not None, err
        assert len(_positions(db_session, account.id)) == 4
        quote = _q(symbol="600005", price=160.0)
        order, reason = broker.submit_order(account.id, "600005", "BUY", 1000, quote=quote)
        assert order is None
        assert "总仓位将超过 80%" in reason

    def test_insufficient_cash(self, db_session):
        account = _account(db_session, 10_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=160.0)
        order, reason = broker.submit_order(account.id, "600000", "BUY", 1000, quote=quote)
        assert order is None
        assert "可用资金不足" in reason

    def test_stale_quote_blocks_trade(self, db_session):
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=10.0, is_stale=True)
        order, reason = broker.submit_order(account.id, "600000", "BUY", 100, quote=quote)
        assert order is None
        assert "过期" in reason

    def test_bad_lot_size_is_rejected_without_order_row(self, db_session):
        """校验阶段拒绝（非风控阶段）不落 REJECTED 订单行——现状记录。"""
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=10.0)
        order, reason = broker.submit_order(account.id, "600000", "BUY", 150, quote=quote)
        assert order is None
        assert "整数手" in reason
        assert _orders(db_session, account.id) == []

    def test_risk_manager_daily_loss_block(self):
        risk = RiskManager(RiskLimits())
        snapshot = AccountSnapshot(
            total_asset=950_000.0, available_cash=500_000.0, initial_cash=1_000_000.0,
            current_position_value={}, daily_pnl=-40_000.0, peak_asset=1_000_000.0,
        )
        decision = risk.check_buy(snapshot, "600000", 1_000.0, _q(symbol="600000", price=10.0))
        assert not decision.allowed
        assert "单日亏损" in decision.reason

    def test_risk_manager_drawdown_block(self):
        risk = RiskManager(RiskLimits())
        snapshot = AccountSnapshot(
            total_asset=880_000.0, available_cash=500_000.0, initial_cash=1_000_000.0,
            current_position_value={}, daily_pnl=0.0, peak_asset=1_000_000.0,
        )
        decision = risk.check_buy(snapshot, "600000", 1_000.0, _q(symbol="600000", price=10.0))
        assert not decision.allowed
        assert "总回撤" in decision.reason

    def test_risk_manager_sell_exceeding_available(self):
        risk = RiskManager(RiskLimits())
        snapshot = AccountSnapshot(total_asset=1_000_000.0, available_cash=1_000_000.0)
        decision = risk.check_sell(snapshot, "600000", 200, 100, _q(symbol="600000", price=10.0))
        assert not decision.allowed
        assert "可用持仓" in decision.reason


# ─────────────────────── 5. 幂等 / 撤单 ───────────────────────


class TestIdempotencyAndCancel:
    def test_duplicate_signal_id_rejected(self, db_session):
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=10.0)
        signal = "rebalance:1:BUY:600000"

        first, err = broker.place_order(account.id, "600000", "BUY", 100, quote.price,
                                        quote=quote, signal_id=signal)
        assert first is not None, err
        second, reason = broker.place_order(account.id, "600000", "BUY", 100, quote.price,
                                            quote=quote, signal_id=signal)
        assert second is None
        assert "重复" in reason
        assert len(_trades(db_session, account.id)) == 1

    def test_paper_trade_signal_unique_constraint(self, db_session):
        account = _account(db_session, 1_000_000.0)
        for _ in range(2):
            db_session.add(PaperTrade(account_id=account.id, order_id=1, symbol="600000",
                                      side="BUY", quantity=100, price=Decimal("10"),
                                      signal_id="dup-signal"))
        with pytest.raises(Exception):
            db_session.commit()
        db_session.rollback()

    def test_cancel_releases_frozen_cash(self, db_session):
        account = _account(db_session, 10_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=10.0)

        order, err = broker.submit_order(account.id, "600000", "BUY", 100, quote=quote)
        assert order is not None, err
        assert order.status == "SUBMITTED"
        db_session.refresh(account)
        # 冻结按含滑点上限价计提，并覆盖佣金与过户费：1000.50 + 5.00 + 0.02 = 1005.52
        assert Decimal(account.frozen_cash) == Decimal("1005.52")
        assert Decimal(account.available_cash) == Decimal("8994.48")

        cancelled, err = broker.cancel_order(order.id)
        assert cancelled is not None, err
        assert cancelled.status == "CANCELLED"
        db_session.refresh(account)
        assert Decimal(account.frozen_cash) == Decimal("0")
        assert Decimal(account.available_cash) == Decimal("10000.00")  # 全额释放
        assert _positions(db_session, account.id) == []
        assert _trades(db_session, account.id) == []

        again, reason = broker.cancel_order(order.id)
        assert again is None
        assert "无法取消" in reason

    def test_cancel_filled_order_rejected(self, db_session):
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=10.0)
        order, err = broker.place_order(account.id, "600000", "BUY", 100, quote.price, quote=quote)
        assert order is not None, err
        cancelled, reason = broker.cancel_order(order.id)
        assert cancelled is None
        assert "FILLED" in reason


# ─────────────── 6. 可成交性护栏（原「已知缺口」，2026-09-14 已补齐并改名） ───────────────
#
# 这个类原先叫 TestKnownGaps：当时封板涨停确实会被全额成交，用例记录的是**缺口**。
# 护栏补上后，这些用例断言的是**正确行为**，继续叫「已知缺口」会误导读者，
# 因此改名为 TestExecutabilityGuards。


class TestExecutabilityGuards:
    def test_limit_up_sealed_board_should_not_fill(self, db_session):
        """封板涨停（收盘价 = 涨停价）应按保守假设拒绝成交。

        实测缺口（2026-09-13 HTTP 闭环）：002161 收盘封板（涨停价 8.04、未开板）
        曾被 PaperBroker 以 8.04 全额成交。现已加入可成交性护栏：缺少盘口排队证据时
        以涨停价买入视为不可成交。
        """
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="002161", price=8.04, previous_close=7.31)
        order, reason = broker.place_order(account.id, "002161", "BUY", 100, quote.price, quote=quote)
        assert order is None
        assert "涨停" in reason

    def test_limit_down_sealed_board_should_not_fill(self, db_session):
        """跌停价卖出同样按不可成交处理（对称保守假设）。"""
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        # 先建仓，再尝试以跌停价卖出
        buy_quote = _q(symbol="002161", price=7.50, previous_close=7.31)
        broker.place_order(account.id, "002161", "BUY", 1000, buy_quote.price, quote=buy_quote)
        from app.paper_trading.broker import PaperBroker  # noqa: F401
        broker.settle_t1(account.id)
        down_quote = _q(symbol="002161", price=6.58, previous_close=7.31)
        order, reason = broker.place_order(
            account.id, "002161", "SELL", 100, down_quote.price, quote=down_quote
        )
        assert order is None
        assert "跌停" in reason

    def test_suspended_symbol_should_not_fill_at_zero(self, db_session):
        """停牌标的（无最新价）不应产生成交。

        实测缺口：price=0 时 order_value=0 绕过资金/仓位检查，最终以 0 元生成持仓，
        只扣 5 元最低佣金。现已在可成交性护栏里直接拒绝。
        """
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        order, reason = broker.place_order(
            account.id, "600000", "BUY", 100, 0.0, quote=_suspended_quote("600000")
        )
        assert order is None
        assert "停牌" in reason or "无效行情" in reason

    def test_normal_price_still_fills(self, db_session):
        """护栏不能误伤正常价格（回归保护）。"""
        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        quote = _q(symbol="600000", price=10.05, previous_close=10.0)
        order, reason = broker.place_order(account.id, "600000", "BUY", 100, quote.price, quote=quote)
        assert order is not None, reason
        assert order.status == "FILLED"

    def test_settle_on_acquisition_day_does_not_unlock(self, db_session):
        """T+1 口径：以买入当日为结算日不得解冻，重复结算也不行。

        实测缺口（HTTP 2026-09-13）：旧实现无条件把 available_quantity 恢复为 quantity，
        于是「当日买入 + 当日结算」即可当天卖出；且命中幂等记录时直接 return，
        行为还依赖调用顺序。现统一为「只解冻 acquisition_date < 结算日 的批次」。
        """
        from app.paper_trading.settlement import DailySettlement

        account = _account(db_session, 1_000_000.0)
        broker = _broker(db_session)
        settlement = DailySettlement(db_session)
        td = date(2026, 9, 14)
        assert settlement.settle_account(account, {}, trading_date=td)["idempotent"] is False

        quote = _q(symbol="600000", price=10.0)
        buy, err = broker.place_order(account.id, "600000", "BUY", 100, quote.price,
                                      quote=quote, trading_date=td)
        assert buy is not None, err
        assert _positions(db_session, account.id)[0].available_quantity == 0

        repeat = settlement.settle_account(account, {}, trading_date=td)
        assert repeat["idempotent"] is True
        # 当日批次必须仍然冻结
        assert _positions(db_session, account.id)[0].available_quantity == 0

        # 次一交易日结算才解冻
        nxt = settlement.settle_account(account, {}, trading_date=date(2026, 9, 15))
        assert nxt["idempotent"] is False
        assert _positions(db_session, account.id)[0].available_quantity == 100
