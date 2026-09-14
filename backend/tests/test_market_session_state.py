"""P0-02 真实市场状态：交易时段判定 + /api/market/session 扩展字段测试。

覆盖研发计划的关键验收用例：

1. 周末打开首页：明确显示「今日休市」和最近交易日，不显示「实时可交易」；
2. 时段边界：开盘前 / 集合竞价 / 上午 / 午休 / 下午 / 收盘各自正确，且只有
   连续竞价时段 ``tradable_now`` 为真；
3. 休市时候选语义必须是「下一交易日研究候选」；
4. 行情新鲜度信封不把接收时间冒充交易所时间：源时间缺失 → 行情年龄为未知；
5. 休市时段不计算行情年龄（避免出现「行情老了 8 小时」这类误导数字）。
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.database.models import Security, TradingDate, UniverseMember, UniverseSnapshot
from app.database.session import SessionLocal, engine
from app.market_data.base import QuoteData
from app.market_rules.session_state import (
    CST,
    CALL_AUCTION,
    CLOSED,
    MORNING,
    NON_TRADING_DAY,
    NOON_BREAK,
    AFTERNOON,
    PRE_OPEN,
    candidate_label,
    now_cst,
    resolve_phase,
    to_cst,
)
from app.database.models import TradingDate


def at(hour: int, minute: int) -> datetime:
    """构造一个「某个交易日中的北京时间时刻」。"""
    base = date(2026, 9, 14)  # 周一
    return datetime(base.year, base.month, base.day, hour, minute, tzinfo=CST)


# ───────────── 1. 时段判定（纯函数） ─────────────


@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        (0, 0, PRE_OPEN),
        (9, 14, PRE_OPEN),
        (9, 15, CALL_AUCTION),
        (9, 25, CALL_AUCTION),
        (9, 29, CALL_AUCTION),
        (9, 30, MORNING),
        (11, 29, MORNING),
        (11, 30, NOON_BREAK),
        (12, 59, NOON_BREAK),
        (13, 0, AFTERNOON),
        (14, 59, AFTERNOON),
        (15, 0, CLOSED),
        (23, 59, CLOSED),
    ],
)
def test_resolve_phase_boundaries(hour, minute, expected):
    assert resolve_phase(at(hour, minute), is_trading_day=True) is expected


def test_phase_tradable_flags():
    # 只有连续竞价可成交；集合竞价接受委托但不可成交
    assert MORNING.tradable and AFTERNOON.tradable
    for phase in (PRE_OPEN, CALL_AUCTION, NOON_BREAK, CLOSED, NON_TRADING_DAY):
        assert phase.tradable is False, phase.key
    assert CALL_AUCTION.orders_accepted is True
    assert PRE_OPEN.orders_accepted is False
    assert NOON_BREAK.orders_accepted is False


def test_non_trading_day_wins_over_clock():
    """周末/节假日即使处在 10:00，也必须是「今日休市」。"""
    for hour, minute in ((9, 40), (10, 0), (13, 30), (14, 0)):
        assert resolve_phase(at(hour, minute), is_trading_day=False) is NON_TRADING_DAY


def test_to_cst_normalises_timezones():
    utc_moment = datetime(2026, 9, 14, 2, 0, tzinfo=timezone.utc)
    assert to_cst(utc_moment).hour == 10
    assert to_cst(utc_moment).utcoffset() == timedelta(hours=8)
    naive = datetime(2026, 9, 14, 10, 0)
    assert to_cst(naive).hour == 10  # naive 按北京时间处理，不做猜测
    assert to_cst(None) is None


def test_all_phases_have_notes():
    for phase in (PRE_OPEN, CALL_AUCTION, MORNING, NOON_BREAK, AFTERNOON, CLOSED, NON_TRADING_DAY):
        assert phase.label and phase.note and phase.key


# ───────────── 2. 候选语义 ─────────────


def test_candidate_label_market_open_vs_closed():
    label = candidate_label(MORNING, "2026-09-14", "2026-09-15")
    assert "盘中" in label and "实时" in label

    closed_label = candidate_label(NON_TRADING_DAY, "2026-09-13", "2026-09-14")
    assert "下一交易日" in closed_label and "2026-09-14" in closed_label
    assert "实时" not in closed_label

    noon = candidate_label(NOON_BREAK, "2026-09-14", "2026-09-15")
    assert "下一交易日" in noon

    # 日历未覆盖下一交易日时不得编造日期
    assert "日历未覆盖" in candidate_label(CLOSED, "2026-09-14", None)


# ───────────── 3. 接口层 ─────────────


def _seed_calendar(days: list[date]) -> None:
    with engine.begin() as conn:
        conn.execute(insert(TradingDate.__table__).values([{"trade_date": d} for d in days]))


def _seed_universe(day: date) -> None:
    db = SessionLocal()
    try:
        specs = [
            ("600519", "贵州茅台", "SH", "active"),
            ("000001", "平安银行", "SZ", "active"),
            ("000002", "万科A", "SZ", "active"),
            ("600001", "邯郸钢铁", "SH", "suspended"),
        ]
        for symbol, name, exchange, status in specs:
            db.add(Security(symbol=symbol, name=name, exchange=exchange, trading_status=status))
            db.flush()
        # Security 主键就是 symbol 字符串，UniverseMember.security_id 指向它
        ids = {symbol: symbol for symbol, *_ in specs}
        snapshot = UniverseSnapshot(
            trading_day=day,
            total_count=4,
            included_count=2,
            excluded_count=2,
            source_provider="test",
        )
        db.add(snapshot)
        db.flush()
        db.add_all(
            [
                UniverseMember(snapshot_id=snapshot.id, security_id=ids["600519"], symbol="600519",
                               exchange="SH", is_included=True, is_st=False, trading_status="active"),
                UniverseMember(snapshot_id=snapshot.id, security_id=ids["000001"], symbol="000001",
                               exchange="SZ", is_included=True, is_st=False, trading_status="active"),
                UniverseMember(snapshot_id=snapshot.id, security_id=ids["000002"], symbol="000002",
                               exchange="SZ", is_included=False, is_st=True, trading_status="active"),
                UniverseMember(snapshot_id=snapshot.id, security_id=ids["600001"], symbol="600001",
                               exchange="SH", is_included=False, is_st=False, trading_status="suspended"),
            ]
        )
        db.commit()
    finally:
        db.close()


def test_market_session_exposes_phase_and_universe_state():
    from app.main import app

    today = date.today()
    last = today - timedelta(days=2)
    _seed_calendar([last, today, today + timedelta(days=1)])
    _seed_universe(last)

    with TestClient(app) as client:
        resp = client.get("/api/market/session")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    for key in (
        "server_time", "timezone", "phase", "phase_label", "phase_note",
        "is_open", "orders_accepted", "tradable_now", "candidates_are",
        "universe", "account_permissions",
    ):
        assert key in body, key

    assert body["timezone"].startswith("Asia/Shanghai")
    assert "+08:00" in body["server_time"]
    assert body["phase"] in {
        "pre_open", "call_auction", "morning", "noon_break", "afternoon",
        "closed", "non_trading_day",
    }
    # 时段与可成交标记必须自洽
    assert body["tradable_now"] == (body["phase"] in {"morning", "afternoon"})
    assert body["is_open"] == body["tradable_now"]

    universe = body["universe"]
    assert universe["snapshot_day"] == last.isoformat()
    assert universe["included"] == 2
    assert universe["excluded"] == 2
    assert universe["total"] == 4
    assert universe["st_count"] == 1
    assert universe["by_trading_status"]["suspended"] == 1

    perms = body["account_permissions"]
    assert perms["paper_trading_available"] is True
    # 默认必须关闭实盘；且响应体里不得出现任何账号/密钥字段
    assert perms["live_trading_enabled"] is False
    flat = resp.text
    for forbidden in ("qmt_account_id", "password", "api_key", "token"):
        assert forbidden not in flat


def test_market_session_rejects_when_calendar_missing(monkeypatch):
    """日历为空时必须 503（而不是返回假的「交易日」结论）。

    真实启动时 lifespan 会自动同步日历，所以这里直接把 is_empty 钉成 True，
    精确覆盖该防御分支。
    """
    from app.main import app
    from app.market_rules.calendar import TradingCalendar

    monkeypatch.setattr(TradingCalendar, "is_empty", lambda self: True, raising=True)
    with TestClient(app) as client:
        resp = client.get("/api/market/session")
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "calendar_unavailable"


def test_weekend_session_marks_next_trading_day_candidate():
    """关键验收用例 1：周末访问必须得到「下一交易日研究候选」。"""
    from app.main import app

    saturday = date(2026, 9, 12)
    friday = date(2026, 9, 11)
    monday = date(2026, 9, 14)
    _seed_calendar([friday, monday, date(2026, 9, 15)])

    with TestClient(app) as client:
        resp = client.get(f"/api/market/session?day={saturday.isoformat()}")
    body = resp.json()
    assert body["is_trading_day"] is False
    assert body["phase"] == "non_trading_day"
    assert body["tradable_now"] is False
    assert body["last_trading_day"] == friday.isoformat()
    assert body["next_trading_day"] == monday.isoformat()
    assert monday.isoformat() in body["candidates_are"]
    assert "实时" not in body["candidates_are"]


# ───────────── 4. 行情新鲜度信封 ─────────────


class _FakeProviderManager:
    def __init__(self, quote):
        self._quote = quote

    async def get_quote(self, symbol: str):
        return self._quote


def _freshness(quote):
    from app.api.market import _serialize_quote_freshness

    return _serialize_quote_freshness(quote, MORNING, "600519", 12.5, 15.0)


def test_freshness_envelope_reports_source_and_age():
    quote = QuoteData(
        symbol="600519",
        source="tdx",
        market_time=now_cst().replace(tzinfo=None) - timedelta(seconds=3),
        received_at=now_cst().replace(tzinfo=None),
    )
    env = _freshness(quote)
    assert env["source"] == "tdx"
    assert env["source_time_status"] == "known"
    assert env["quote_age_status"] == "known"
    assert env["quote_age_seconds"] is not None and env["quote_age_seconds"] >= 0
    assert env["request_elapsed_ms"] == 12.5
    assert env["stale_threshold_seconds"] == 15.0
    assert "+08:00" in env["server_time"]


def test_freshness_marks_missing_source_time_as_unknown():
    """源行情时间缺失时不得用接收时间冒充交易所时间。"""
    quote = QuoteData(
        symbol="600519",
        source="eastmoney",
        market_time=None,
        received_at=now_cst().replace(tzinfo=None),
    )
    env = _freshness(quote)
    assert env["source_time"] is None
    assert env["source_time_status"] == "unknown"
    assert env["quote_age_seconds"] is None
    assert env["quote_age_status"] == "source_time_missing"
    assert "接收时间" in env["client_display_note"]


def test_freshness_skips_age_when_market_not_open():
    quote = QuoteData(
        symbol="600519",
        source="tdx",
        market_time=now_cst().replace(tzinfo=None) - timedelta(hours=30),
        received_at=now_cst().replace(tzinfo=None),
    )
    from app.api.market import _serialize_quote_freshness

    env = _serialize_quote_freshness(quote, NON_TRADING_DAY, "600519", 5.0, 15.0)
    assert env["quote_age_seconds"] is None
    assert env["quote_age_status"] == "not_applicable_market_not_open"
    assert env["source_time_status"] == "known"


def test_freshness_endpoint_returns_envelope_even_without_provider():
    from app.main import app

    today = date.today()
    _seed_calendar([today])
    with TestClient(app) as client:
        resp = client.get("/api/market/freshness?symbol=600519")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "600519"
    assert "quote_age_status" in body
    assert body["stale_threshold_seconds"] > 0
    assert "+08:00" in body["server_time"] or body["server_time"].count("-") >= 2
