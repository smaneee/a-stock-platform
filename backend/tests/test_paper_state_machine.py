"""模拟交易订单状态机、现金冻结与日终结算测试。"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database.models import (
    AssetRecord,
    PaperAccount,
    PaperOrder,
    PaperPosition,
    PaperTrade,
)
from app.paper_trading.broker import (
    CANCELLED,
    FILLED,
    REJECTED,
    SUBMITTED,
    PaperBroker,
)
from app.paper_trading.portfolio import PortfolioService
from app.paper_trading.settlement import DailySettlement

from tests.helpers import make_quote


def _create_account(db, cash=100_000.0) -> PaperAccount:
    account = PaperAccount(
        name="测试账户",
        initial_cash=Decimal(str(cash)),
        available_cash=Decimal(str(cash)),
        frozen_cash=Decimal("0"),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _buy_quote(price=10.0):
    # previous_close 与 price 一致：避免价格恰好落在涨跌停价上，
    # 让用例测的是它声称的那件事（资金/状态机），而不是被可成交性护栏拦下。
    return make_quote(symbol="600000", price=price, previous_close=price)


# ──────── 订单状态机 ────────


def test_submit_order_freezes_cash(db_session):
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = _buy_quote(10.0)

    order, error = broker.submit_order(
        account.id, "600000", "BUY", 100, quote=quote
    )
    assert order is not None, error
    assert order.status == SUBMITTED

    db_session.refresh(account)
    # 冻结 = 含滑点上限价成交额 + 佣金 + 过户费。`_buy_quote(10.0)` 的可成交价是 **10.01**
    # （make_quote 的口径），10.01 × 1.0005 = 10.015005 → 向上取到 4 位 = 10.0151，
    # 100 股 → 1001.51 + 最低佣金 5.00 + 过户费 0.02（1001.51×0.00001 向上取整到分）
    # = 1006.53（2026-09-14 起模拟盘与回测同计 5bp 滑点与 0.001% 双边过户费）
    assert float(account.frozen_cash) == pytest.approx(1006.53)
    assert float(account.available_cash) == pytest.approx(100_000.0 - 1006.53)


def test_cancel_order_releases_cash(db_session):
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = _buy_quote(10.0)

    order, _ = broker.submit_order(account.id, "600000", "BUY", 100, quote=quote)
    assert order.status == SUBMITTED

    order, error = broker.cancel_order(order.id)
    assert order is not None, error
    assert order.status == CANCELLED

    db_session.refresh(account)
    assert float(account.frozen_cash) == 0.0
    assert float(account.available_cash) == pytest.approx(100_000.0)


def test_fill_order_settles_and_creates_position(db_session):
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = _buy_quote(10.0)

    order, _ = broker.submit_order(account.id, "600000", "BUY", 100, quote=quote)
    order, error = broker.fill_order(order.id, quote)
    assert order is not None, error
    assert order.status == FILLED

    db_session.refresh(account)
    assert float(account.frozen_cash) == 0.0

    position = db_session.scalars(
        select(PaperPosition).where(PaperPosition.account_id == account.id)
    ).first()
    assert position.quantity == 100
    assert position.available_quantity == 0  # T+1 冻结


def test_rejected_order_persists_reason(db_session):
    account = _create_account(db_session, cash=100.0)
    broker = PaperBroker(db_session)
    quote = _buy_quote(100.0)

    order, error = broker.submit_order(account.id, "600000", "BUY", 100, quote=quote)
    assert order is None
    assert "资金不足" in error

    # 被拒委托落库并带原因
    rejected = db_session.scalars(
        select(PaperOrder).where(PaperOrder.status == REJECTED)
    ).first()
    assert rejected is not None
    assert "资金不足" in rejected.reject_reason


def test_fill_order_wrong_state_rejected(db_session):
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = _buy_quote(10.0)

    order, _ = broker.submit_order(account.id, "600000", "BUY", 100, quote=quote)
    broker.cancel_order(order.id)

    order, error = broker.fill_order(order.id, quote)
    assert order is None
    assert "无法成交" in error


def test_cancel_order_wrong_state_rejected(db_session):
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = _buy_quote(10.0)

    order, error = broker.place_order(account.id, "600000", "BUY", 100, 10.0, quote=quote)
    assert order is not None, error
    assert order.status == FILLED

    order, error = broker.cancel_order(order.id)
    assert order is None
    assert "无法取消" in error


# ──────── 盈亏 ────────


def test_sell_records_realized_pnl(db_session):
    account = _create_account(db_session)
    broker = PaperBroker(db_session)

    buy_quote = _buy_quote(10.0)
    broker.place_order(account.id, "600000", "BUY", 100, 10.0, quote=buy_quote)
    broker.settle_t1(account.id)

    sell_quote = make_quote(symbol="600000", price=11.0)
    order, error = broker.place_order(account.id, "600000", "SELL", 100, 11.0, quote=sell_quote)
    assert order is not None, error

    trade = db_session.scalars(
        select(PaperTrade).where(PaperTrade.side == "SELL")
    ).first()
    assert float(trade.realized_pnl) > 0

    # 全部卖出后批次被删除（数量为 0 时不留记录）
    position = db_session.scalars(
        select(PaperPosition).where(PaperPosition.account_id == account.id)
    ).first()
    assert position is None
    # 已平仓盈亏记录在 PaperTrade 上
    assert float(trade.realized_pnl) > 0


def test_unrealized_pnl(db_session):
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = _buy_quote(10.0)
    broker.place_order(account.id, "600000", "BUY", 100, 10.0, quote=quote)

    portfolio = PortfolioService(db_session)
    # 现价 11.0，未实现盈亏为正
    up_quote = make_quote(symbol="600000", price=11.0)
    unrealized = portfolio.unrealized_pnl(account.id, {"600000": up_quote})
    assert unrealized > 0


# ──────── 日终结算 ────────


def test_settlement_unfreezes_and_records_snapshot(db_session):
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = _buy_quote(10.0)
    broker.place_order(account.id, "600000", "BUY", 100, 10.0, quote=quote)

    # 结算前：当日买入不可卖
    position = db_session.scalars(
        select(PaperPosition).where(PaperPosition.account_id == account.id)
    ).first()
    assert position.available_quantity == 0

    settlement = DailySettlement(db_session)
    # T+1 口径：以**买入当天**为结算日不得解冻（否则等于允许当日回转交易）
    same_day = settlement.settle_account(
        account, {"600000": quote}, trading_date=position.acquisition_date
    )
    db_session.refresh(position)
    assert position.available_quantity == 0, "买入当日结算不得解冻（T+1）"
    assert same_day["positions_settled"] == 0

    # 以次一交易日结算才解冻
    next_day = position.acquisition_date + timedelta(days=1)
    summary = settlement.settle_account(account, {"600000": quote}, trading_date=next_day)
    assert summary["account_id"] == account.id

    db_session.refresh(position)
    assert position.available_quantity == 100

    records = db_session.scalars(
        select(AssetRecord).where(AssetRecord.account_id == account.id)
    ).all()
    # 两次结算（买入当日不解冻 + 次一交易日解冻）各留一条资产快照
    assert len(records) == 2
    assert all(float(record.total_asset) > 0 for record in records)
