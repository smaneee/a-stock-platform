"""交易日历服务测试。"""
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

import app.market_rules.calendar as calendar_module
from app.database.models import TradingDate
from app.market_rules.calendar import TradingCalendar, _xshg_sessions


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


# ---------------------------------------------------------------------------
# exchange_calendars XSHG 兜底源
#
# 背景：exchange_calendars 4.x 移除了 valid_days，旧实现直接 AttributeError
# 并被上层 except 静默吞掉，兜底源形同虚设；同时 XSHG 日历只覆盖到
# 2026-12-31，默认 lookahead 180 天会越界触发 DateOutOfBounds。
# ---------------------------------------------------------------------------

_SESSIONS = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
_BOUND_START = datetime(2006, 9, 11)
_BOUND_END = datetime(2026, 12, 31)


def _fake_calendar(sessions, first=None, last=None, with_range=True):
    """构造 exchange_calendars >= 4 风格的假日历（有 sessions_in_range，无 valid_days）。"""
    fake = SimpleNamespace(first_session=first, last_session=last)
    if with_range:

        def sessions_in_range(start: str, end: str):
            lo, hi = date.fromisoformat(start), date.fromisoformat(end)
            return [d for d in sessions if lo <= d <= hi]

        fake.sessions_in_range = sessions_in_range
    return fake


def test_xshg_sessions_in_range():
    cal = _fake_calendar(_SESSIONS, _BOUND_START, _BOUND_END)
    assert _xshg_sessions(cal, date(2026, 1, 5), date(2026, 1, 6)) == [
        date(2026, 1, 5),
        date(2026, 1, 6),
    ]


def test_xshg_sessions_clips_to_calendar_bounds():
    """超出 last_session 的区间必须被裁剪，而不是抛 DateOutOfBounds。"""
    cal = _fake_calendar(_SESSIONS, _BOUND_START, _BOUND_END)
    assert _xshg_sessions(cal, date(2026, 1, 5), date(2027, 6, 1)) == _SESSIONS


def test_xshg_sessions_clips_start_before_calendar():
    cal = _fake_calendar(_SESSIONS, _BOUND_START, _BOUND_END)
    assert _xshg_sessions(cal, date(1990, 1, 1), date(2026, 1, 6)) == [
        date(2026, 1, 5),
        date(2026, 1, 6),
    ]


def test_xshg_sessions_returns_empty_when_fully_out_of_bounds():
    cal = _fake_calendar(_SESSIONS, _BOUND_START, _BOUND_END)
    assert _xshg_sessions(cal, date(2030, 1, 1), date(2030, 2, 1)) == []


def test_xshg_sessions_supports_valid_days_fallback():
    """老版本仅有 valid_days 时也要可用。"""
    days = pd.DatetimeIndex(["2026-01-05", "2026-01-06", "2026-01-10"])
    cal = SimpleNamespace(first_session=None, last_session=None, valid_days=days)
    assert _xshg_sessions(cal, date(2026, 1, 1), date(2026, 1, 6)) == [
        date(2026, 1, 5),
        date(2026, 1, 6),
    ]


def test_xshg_sessions_raises_without_supported_api():
    cal = SimpleNamespace(first_session=None, last_session=None)
    with pytest.raises(RuntimeError):
        _xshg_sessions(cal, date(2026, 1, 1), date(2026, 1, 6))


def test_xshg_sessions_with_real_calendar():
    """回归：4.x 的 XSHG 日历没有 valid_days，且默认 lookahead 会越界。"""
    ec = pytest.importorskip("exchange_calendars")
    real = ec.get_calendar("XSHG")
    days = _xshg_sessions(
        real, date.today() - timedelta(days=30), date.today() + timedelta(days=180)
    )
    assert days, "XSHG 兜底源不应返回空"
    assert all(isinstance(d, date) and not isinstance(d, datetime) for d in days)
    assert max(days) <= real.last_session.date()


def test_sync_trading_calendar_falls_back_to_xshg(db_session, monkeypatch):
    """AKShare 日历不可达时，用 exchange_calendars 兜底填充本地日历。"""

    def _boom(*args, **kwargs):
        raise ConnectionError("akshare down")

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(tool_trade_date_hist_sina=_boom),
    )
    result = calendar_module.sync_trading_calendar(
        db_session, lookback_days=30, lookahead_days=10
    )
    assert result["source"] == "exchange_calendars"
    assert result["added"] > 0
    assert result["total"] == result["added"]
    # 兜底源写入的是真实交易日：不应出现周末
    rows = db_session.query(TradingDate).all()
    assert rows
    assert all(row.trade_date.weekday() < 5 for row in rows)
