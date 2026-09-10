"""交易日历服务测试。"""
from datetime import date

import pytest

from app.database.models import TradingDate
from app.market_rules.calendar import TradingCalendar


def _seed(db, *days):
    for d in days:
        db.add(TradingDate(trade_date=d))
    db.commit()


def test_is_trading_day(db_session):
    _seed(db_session, date(2026, 1, 5), date(2026, 1, 6))
    cal = TradingCalendar(db_session)
    assert cal.is_trading_day(date(2026, 1, 5)) is True
    assert cal.is_trading_day(date(2026, 1, 6)) is True
    # 未落库的日期（周末/节假日）非交易日
    assert cal.is_trading_day(date(2026, 1, 10)) is False  # 周六
    assert cal.is_trading_day(date(2026, 1, 1)) is False  # 元旦


def test_next_trading_day_skip_weekend(db_session):
    # 周五(1/9) -> 周一(1/12)，跳过周六周日
    _seed(db_session, date(2026, 1, 9), date(2026, 1, 12))
    cal = TradingCalendar(db_session)
    assert cal.next_trading_day(date(2026, 1, 9)) == date(2026, 1, 12)


def test_next_trading_day_skip_holiday(db_session):
    # 五一长假：4/30 -> 5/6
    _seed(db_session, date(2026, 4, 30), date(2026, 5, 6))
    cal = TradingCalendar(db_session)
    assert cal.next_trading_day(date(2026, 4, 30)) == date(2026, 5, 6)


def test_prev_trading_day(db_session):
    _seed(db_session, date(2026, 1, 9), date(2026, 1, 12))
    cal = TradingCalendar(db_session)
    assert cal.prev_trading_day(date(2026, 1, 12)) == date(2026, 1, 9)


def test_last_trading_day_on_or_before(db_session):
    _seed(db_session, date(2026, 1, 9), date(2026, 1, 12))
    cal = TradingCalendar(db_session)
    # 交易日当天返回自身
    assert cal.last_trading_day_on_or_before(date(2026, 1, 9)) == date(2026, 1, 9)
    # 周六回退到周五
    assert cal.last_trading_day_on_or_before(date(2026, 1, 10)) == date(2026, 1, 9)


def test_next_trading_day_empty_calendar_raises(db_session):
    cal = TradingCalendar(db_session)
    with pytest.raises(ValueError):
        cal.next_trading_day(date(2026, 1, 5))


def test_count(db_session):
    _seed(db_session, date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7))
    cal = TradingCalendar(db_session)
    assert cal.count() == 3
