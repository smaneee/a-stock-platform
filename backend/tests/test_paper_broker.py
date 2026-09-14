"""模拟交易（Paper Broker）测试。"""
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database.models import PaperAccount, PaperPosition
from app.paper_trading.broker import PaperBroker

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


def test_buy_order(db_session):
    """正常买入。"""
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = make_quote(symbol="600000", price=10.0)

    order, error = broker.place_order(
        account.id, "600000", "BUY", 100, 10.0, quote=quote
    )
    assert order is not None, error
    assert order.status == "FILLED"

    position = db_session.scalars(
        select(PaperPosition).where(PaperPosition.account_id == account.id)
    ).first()
    assert position.quantity == 100
    # T+1：当日买入不可卖
    assert position.available_quantity == 0


def test_buy_and_sell_after_settle(db_session):
    """买入 → 日终结算（T+1 解冻）→ 卖出。"""
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = make_quote(symbol="600000", price=10.0)

    order, _ = broker.place_order(account.id, "600000", "BUY", 100, 10.0, quote=quote)
    assert order is not None

    # 日终结算，解冻
    broker.settle_t1(account.id)

    order, error = broker.place_order(account.id, "600000", "SELL", 100, 10.0, quote=quote)
    assert order is not None, error

    # 全部卖出后批次被删除（仓位为 0 不留记录）
    positions = db_session.scalars(
        select(PaperPosition).where(PaperPosition.account_id == account.id)
    ).all()
    assert positions == []


def test_sell_before_settle_rejected(db_session):
    """T+1：买入当日不可卖。"""
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = make_quote(symbol="600000", price=10.0)

    broker.place_order(account.id, "600000", "BUY", 100, 10.0, quote=quote)
    # 不结算，直接卖出
    order, error = broker.place_order(account.id, "600000", "SELL", 100, 10.0, quote=quote)
    assert order is None
    assert "持仓" in error or "可用" in error


def test_buy_insufficient_cash(db_session):
    """资金不足拒绝买入。"""
    account = _create_account(db_session, cash=100.0)
    broker = PaperBroker(db_session)
    # previous_close 与价格一致：确保这是「资金不足」用例，而不是先被涨跌停护栏拦下
    quote = make_quote(symbol="600000", price=100.0, previous_close=100.0)

    order, error = broker.place_order(account.id, "600000", "BUY", 100, 100.0, quote=quote)
    assert order is None
    assert "资金不足" in error


def test_duplicate_signal_rejected(db_session):
    """同一信号幂等，不可重复成交。"""
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = make_quote(symbol="600000", price=10.0)

    signal_id = "test-signal-123"
    order, _ = broker.place_order(
        account.id, "600000", "BUY", 100, 10.0, quote=quote, signal_id=signal_id
    )
    assert order is not None

    # 同一信号再次下单
    order, error = broker.place_order(
        account.id, "600000", "BUY", 100, 10.0, quote=quote, signal_id=signal_id
    )
    assert order is None
    assert "重复" in error


def test_buy_lot_size(db_session):
    """买入必须为 100 股整数手。"""
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = make_quote(symbol="600000", price=10.0)

    order, error = broker.place_order(account.id, "600000", "BUY", 150, 10.0, quote=quote)
    assert order is None
    assert "整数手" in error


def test_buy_stale_quote_rejected(db_session):
    """行情过期拒绝成交。"""
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = make_quote(symbol="600000", price=10.0, is_stale=True)

    order, error = broker.place_order(account.id, "600000", "BUY", 100, 10.0, quote=quote)
    assert order is None
    assert "过期" in error


def test_client_cannot_choose_an_arbitrary_fill_price(db_session):
    """客户端传入低价不能绕过资金与仓位风控。"""
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = make_quote(symbol="600000", price=10.0)

    order, error = broker.place_order(
        account.id, "600000", "BUY", 100, 0.01, quote=quote
    )
    assert order is not None, error
    assert float(order.price) == pytest.approx(quote.ask_price)


def test_received_quote_too_old_is_rejected(db_session):
    account = _create_account(db_session)
    broker = PaperBroker(db_session)
    quote = make_quote(symbol="600000", price=10.0)
    quote.received_at -= timedelta(minutes=1)

    order, error = broker.place_order(
        account.id, "600000", "BUY", 100, 10.0, quote=quote
    )
    assert order is None
    assert "过期" in error
