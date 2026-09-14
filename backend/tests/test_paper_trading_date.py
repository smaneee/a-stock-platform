"""模拟盘「交易日兜底」测试（P1-02 遗留缺口）。

实测缺口：HTTP 下单/调仓不传 `trading_date` 时，`PaperBroker` 回退到
`date.today()`，于是周末/节假日下的买入批次被记成非交易日（实测
`acquisition_date=2026-09-13` 周六），既让 T+1 判定失去日历意义，也让
「次日可卖」在日历上无从核算。

修复口径：缺省交易日 = 交易日历上「今天或之前最近的一个交易日」；日历为空时
退回今天（不阻断下单）。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import insert, select

from app.database.models import PaperAccount, PaperPosition, TradingDate
from app.market_rules.calendar import TradingCalendar
from app.paper_trading.broker import PaperBroker

from tests.test_paper_loop import _q


def _seed_calendar(db, days: list[date]) -> None:
    bind = db.get_bind()
    with bind.begin() as conn:
        conn.execute(insert(TradingDate.__table__).values([{"trade_date": d} for d in days]))
    db.expire_all()


def _account(db, cash: float = 1_000_000.0) -> PaperAccount:
    account = PaperAccount(name="交易日兜底测试", initial_cash=cash, available_cash=cash)
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def test_default_trading_date_is_last_trading_day_when_today_is_not(db_session):
    """今天是周末/节假日时，缺省交易日必须是最近一个交易日。"""
    today = date.today()
    last_trading = today - timedelta(days=1)
    _seed_calendar(db_session, [last_trading - timedelta(days=1), last_trading])

    calendar = TradingCalendar(db_session)
    assert calendar.is_trading_day(today) is False, "本用例要求「今天」不是种子日历里的交易日"

    account = _account(db_session)
    broker = PaperBroker(db_session)
    quote = _q(price=10.0)
    order, error = broker.place_order(account.id, "600000", "BUY", 100, quote.price, quote=quote)
    assert order is not None, error
    assert order.status == "FILLED"

    position = db_session.scalars(
        select(PaperPosition).where(PaperPosition.account_id == account.id)
    ).first()
    assert position is not None
    assert position.acquisition_date == last_trading, (
        f"缺省交易日应为最近交易日 {last_trading}，实际 {position.acquisition_date}"
    )


def test_explicit_trading_date_wins(db_session):
    today = date.today()
    explicit = today - timedelta(days=2)
    _seed_calendar(db_session, [explicit, today - timedelta(days=1)])

    account = _account(db_session)
    broker = PaperBroker(db_session)
    quote = _q(price=10.0)
    order, error = broker.place_order(
        account.id, "600000", "BUY", 100, quote.price, quote=quote, trading_date=explicit
    )
    assert order is not None, error
    position = db_session.scalars(
        select(PaperPosition).where(PaperPosition.account_id == account.id)
    ).first()
    assert position is not None
    assert position.acquisition_date == explicit


def test_calendar_empty_falls_back_to_today(db_session):
    """日历为空时不能因为查不到交易日就拒绝下单。"""
    from app.paper_trading import broker as broker_module

    account = _account(db_session)
    broker = PaperBroker(db_session)
    quote = _q(price=10.0)
    assert broker_module._to_trading_date(None, db_session) == date.today()
    order, error = broker.place_order(account.id, "600000", "BUY", 100, quote.price, quote=quote)
    assert order is not None, error
    position = db_session.scalars(
        select(PaperPosition).where(PaperPosition.account_id == account.id)
    ).first()
    assert position is not None
    assert position.acquisition_date == date.today()


def test_sell_availability_uses_same_default(db_session):
    """卖出可用数量判定必须与成交使用同一个缺省交易日（否则会自相矛盾）。"""
    today = date.today()
    last_trading = today - timedelta(days=1)
    _seed_calendar(db_session, [last_trading - timedelta(days=1), last_trading])

    account = _account(db_session)
    broker = PaperBroker(db_session)
    quote = _q(price=10.0)
    # 批次建在 last_trading（缺省交易日）
    order, error = broker.place_order(account.id, "600000", "BUY", 100, quote.price, quote=quote)
    assert order is not None, error
    # 当天（非交易日）卖出：T+1 未到期 → 拒绝
    sell, sell_error = broker.place_order(account.id, "600000", "SELL", 100, quote.price, quote=quote)
    assert sell is None
    assert "可用" in sell_error or "持仓" in sell_error
