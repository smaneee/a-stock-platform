"""历史数据本地化服务测试。"""
from datetime import date, datetime

import pytest

from app.database.models import HistoricalBar
from app.history.service import (
    ADJUST_HFQ,
    ADJUST_NONE,
    ADJUST_QFQ,
    HistoricalDataService,
)
from app.market_data.base import QuoteData
from app.time_utils import utc_now


def _mk_bar(symbol="600000", trade_date="2026-01-05", price=10.0):
    return QuoteData(
        symbol=symbol,
        name=symbol,
        price=price,
        open=price - 0.1,
        high=price + 0.2,
        low=price - 0.2,
        previous_close=0.0,
        volume=1000.0,
        amount=100000.0,
        source="akshare",
        market_time=datetime.strptime(trade_date, "%Y-%m-%d"),
    )


def _seed(db, symbol="600000", dates=("2026-01-05", "2026-01-06"), adjust=ADJUST_NONE):
    for d in dates:
        db.add(
            HistoricalBar(
                symbol=symbol,
                period="daily",
                adjust=adjust,
                trade_date=date.fromisoformat(d),
                open=10.0,
                high=10.5,
                low=9.5,
                close=10.2,
                volume=1000.0,
                amount=100000.0,
                source="akshare",
                fetched_at=utc_now(),
            )
        )
    db.commit()


def test_get_cached(db_session):
    _seed(db_session)
    svc = HistoricalDataService(db_session)
    bars = svc.get_cached(
        "600000",
        datetime(2026, 1, 1),
        datetime(2026, 1, 10),
    )
    assert len(bars) == 2
    assert bars[0].market_time.date() == date(2026, 1, 5)


def test_normalize_adjust():
    assert HistoricalDataService._normalize_adjust("qfq") == ADJUST_QFQ
    assert HistoricalDataService._normalize_adjust("HFQ") == ADJUST_HFQ
    assert HistoricalDataService._normalize_adjust("none") == ADJUST_NONE
    assert HistoricalDataService._normalize_adjust("bogus") == ADJUST_NONE
    assert HistoricalDataService._normalize_adjust("") == ADJUST_NONE


def test_missing_range():
    svc = HistoricalDataService.__new__(HistoricalDataService)  # 跳过 __init__
    assert svc._missing_range(date(2026, 1, 1), date(2026, 1, 10), set()) == (
        date(2026, 1, 1),
        date(2026, 1, 10),
    )
    existing = {date(2026, 1, 1), date(2026, 1, 10)}
    assert svc._missing_range(date(2026, 1, 1), date(2026, 1, 10), existing) == (
        None,
        None,
    )


def test_covers():
    bars = [_mk_bar("600000", "2026-01-05"), _mk_bar("600000", "2026-01-10")]
    assert HistoricalDataService._covers(bars, datetime(2026, 1, 5), datetime(2026, 1, 10)) is True
    assert HistoricalDataService._covers(bars, datetime(2026, 1, 5), datetime(2026, 1, 9)) is False


@pytest.mark.asyncio
async def test_get_history_cache_only(db_session):
    """缓存完整时直接返回，不触发同步。"""
    _seed(db_session, dates=("2026-01-05", "2026-01-06", "2026-01-07"))
    svc = HistoricalDataService(db_session)
    result = await svc.get_history(
        "600000",
        datetime(2026, 1, 5),
        datetime(2026, 1, 7),
    )
    assert result.source == "cache"
    assert result.is_complete is True
    assert len(result.bars) == 3


@pytest.mark.asyncio
async def test_get_history_sync_when_empty(db_session, monkeypatch):
    """缓存为空时触发同步（mock AKShare 返回值）。"""
    fake_bars = [_mk_bar("600000", "2026-01-05"), _mk_bar("600000", "2026-01-06")]

    async def fake_fetch(symbol, adjust, start, end):
        return fake_bars

    monkeypatch.setattr(
        "app.history.service._akshare_fetch",
        lambda symbol, adjust, start, end: fake_bars,
    )
    svc = HistoricalDataService(db_session)
    result = await svc.get_history(
        "600000",
        datetime(2026, 1, 5),
        datetime(2026, 1, 6),
    )
    assert result.source in ("akshare", "mixed")
    assert len(result.bars) == 2
    assert result.is_complete is True


@pytest.mark.asyncio
async def test_sync_incremental(db_session, monkeypatch):
    """增量同步：已有部分数据时只补缺失，不重复写入。"""
    _seed(db_session, dates=("2026-01-05",))

    fake_bars = [_mk_bar("600000", "2026-01-05"), _mk_bar("600000", "2026-01-06")]
    monkeypatch.setattr(
        "app.history.service._akshare_fetch",
        lambda symbol, adjust, start, end: fake_bars,
    )
    svc = HistoricalDataService(db_session)
    added = await svc.sync(
        "600000", datetime(2026, 1, 1), datetime(2026, 1, 10)
    )
    # 01-05 已存在，只新增 01-06
    assert added == 1


@pytest.mark.asyncio
async def test_sync_filters_bad_data(db_session, monkeypatch):
    """同步时过滤负成交量等脏数据。"""
    fake_bars = [
        _mk_bar("600000", "2026-01-05", price=10.0),
        # 负成交量，应被过滤
        QuoteData(
            symbol="600000", name="600000", price=10.0, open=10.0, high=10.5,
            low=9.5, previous_close=0.0, volume=-100.0, amount=1000.0,
            source="akshare", market_time=datetime.strptime("2026-01-06", "%Y-%m-%d"),
        ),
    ]
    monkeypatch.setattr(
        "app.history.service._akshare_fetch",
        lambda symbol, adjust, start, end: fake_bars,
    )
    svc = HistoricalDataService(db_session)
    added = await svc.sync("600000", datetime(2026, 1, 1), datetime(2026, 1, 10))
    assert added == 1  # 只有干净数据落库
