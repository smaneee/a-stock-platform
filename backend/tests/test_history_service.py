"""历史数据本地化服务测试。"""
import sys
import types
from datetime import date, datetime

import pandas as pd
import pytest

from app.database.models import HistoricalBar
from app.history.service import (
    _BAOSTOCK_ADJUSTFLAG,
    ADJUST_HFQ,
    ADJUST_NONE,
    ADJUST_QFQ,
    HistoricalDataService,
    _akshare_sina_fetch,
    _baostock_fetch,
    _coerce_day,
    _exchange_prefix,
    _fetch_from_sources,
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


# ---------------------------------------------------------------------------
# 多源历史回退链（EastMoney -> Sina -> BaoStock）
#
# 背景：AKShare 的 EastMoney 通道（stock_zh_a_hist）实测经常 RemoteDisconnected，
# 旧实现没有备选源，导致历史入库整体失败。下列测试锁定回退语义。
# ---------------------------------------------------------------------------


def _stub(bars=None, exc=None):
    """构造 fetcher 替身：要么返回 bars，要么抛 exc。"""

    def _fetch(symbol, adjust, start, end):
        if exc is not None:
            raise exc
        return list(bars or [])

    return _fetch


def _patch_sources(monkeypatch, akshare=None, sina=None, baostock=None):
    """替换回退链上的三个 fetcher；未指定的默认返回空。"""
    monkeypatch.setattr("app.history.service._akshare_fetch", akshare or _stub(None))
    monkeypatch.setattr("app.history.service._akshare_sina_fetch", sina or _stub(None))
    monkeypatch.setattr("app.history.service._baostock_fetch", baostock or _stub(None))


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("600000", "sh"),
        ("688981", "sh"),
        ("000001", "sz"),
        ("002594", "sz"),
        ("300750", "sz"),
        ("430047", "bj"),
        ("830799", "bj"),
    ],
)
def test_exchange_prefix(symbol, expected):
    assert _exchange_prefix(symbol) == expected


@pytest.mark.parametrize("symbol", ["123", "", "abc"])
def test_exchange_prefix_unknown_raises(symbol):
    with pytest.raises(ValueError):
        _exchange_prefix(symbol)


def test_coerce_day_normalizes_all_supported_types():
    assert _coerce_day(date(2026, 1, 5)) == date(2026, 1, 5)
    assert _coerce_day(datetime(2026, 1, 5, 15, 0)) == date(2026, 1, 5)
    assert _coerce_day("2026-01-05") == date(2026, 1, 5)
    assert _coerce_day("20260105") == date(2026, 1, 5)
    # pandas Timestamp 必须收敛成 date，否则落库与去重口径不一致
    assert _coerce_day(pd.Timestamp("2026-01-05")) == date(2026, 1, 5)


def test_baostock_adjustflag_mapping():
    # BaoStock: 1=后复权 2=前复权 3=不复权
    assert _BAOSTOCK_ADJUSTFLAG == {ADJUST_NONE: "3", ADJUST_QFQ: "2", ADJUST_HFQ: "1"}


def _install_akshare(monkeypatch, df):
    """把假 akshare 模块塞进 sys.modules，并记录收到的参数。"""
    calls: dict = {}

    def stock_zh_a_daily(**kwargs):
        calls.update(kwargs)
        return df

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        types.SimpleNamespace(stock_zh_a_daily=stock_zh_a_daily),
    )
    return calls


def _sina_frame():
    # 列名口径：akshare.stock_zh_a_daily 返回英文列，date 是 datetime.date
    return pd.DataFrame(
        {
            "date": [date(2026, 1, 5), date(2026, 1, 6)],
            "open": [10.0, 10.1],
            "high": [10.5, 10.6],
            "low": [9.5, 9.6],
            "close": [10.2, 10.3],
            "volume": [1000.0, 2000.0],
            "amount": [10200.0, 20600.0],
        }
    )


def test_akshare_sina_fetch_maps_columns(monkeypatch):
    calls = _install_akshare(monkeypatch, _sina_frame())
    bars = _akshare_sina_fetch(
        "600000", ADJUST_QFQ, date(2026, 1, 1), date(2026, 1, 10)
    )
    assert calls["symbol"] == "sh600000"
    assert calls["start_date"] == "20260101"
    assert calls["end_date"] == "20260110"
    assert calls["adjust"] == ADJUST_QFQ
    assert [b.market_time.date() for b in bars] == [date(2026, 1, 5), date(2026, 1, 6)]
    assert [b.price for b in bars] == [10.2, 10.3]
    assert [b.volume for b in bars] == [1000.0, 2000.0]
    assert {b.source for b in bars} == {"akshare_sina"}


def test_akshare_sina_fetch_uses_empty_adjust_for_none(monkeypatch):
    calls = _install_akshare(monkeypatch, _sina_frame())
    _akshare_sina_fetch("000001", ADJUST_NONE, date(2026, 1, 1), date(2026, 1, 10))
    assert calls["symbol"] == "sz000001"
    assert calls["adjust"] == ""  # 新浪不复权用空字符串


def test_akshare_sina_fetch_empty_frame_returns_empty(monkeypatch):
    _install_akshare(monkeypatch, pd.DataFrame())
    assert (
        _akshare_sina_fetch("600000", ADJUST_NONE, date(2026, 1, 1), date(2026, 1, 10))
        == []
    )


class _FakeResultSet:
    """BaoStock 结果集替身（游标式 next()/get_row_data()）。"""

    def __init__(self, rows, error_code="0"):
        self.error_code = error_code
        self.error_msg = "fake"
        self._rows = list(rows)
        self._idx = -1

    def next(self):
        self._idx += 1
        return self._idx < len(self._rows)

    def get_row_data(self):
        return self._rows[self._idx]


class _FakeBaoStock:
    """最小可用的 baostock 模块替身。"""

    def __init__(self, rows, login_error="0", query_error="0"):
        self._rows = rows
        self._login_error = login_error
        self._query_error = query_error
        self.calls: dict = {}
        self.logged_out = False

    def login(self):
        return types.SimpleNamespace(error_code=self._login_error, error_msg="fake")

    def logout(self):
        self.logged_out = True

    def query_history_k_data_plus(self, code, fields, **kwargs):
        self.calls = {"code": code, "fields": fields, **kwargs}
        return _FakeResultSet(self._rows, self._query_error)


def _install_baostock(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "baostock", fake)
    return fake


def test_baostock_fetch_parses_rows_and_skips_suspended_days(monkeypatch):
    fake = _FakeBaoStock(
        [
            ["2026-01-05", "10.0", "10.5", "9.5", "10.2", "1000", "10200"],
            # 停牌日：价格为空字符串，必须跳过而不是落成 0 价
            ["2026-01-06", "", "", "", "", "0", "0"],
        ]
    )
    _install_baostock(monkeypatch, fake)
    bars = _baostock_fetch("600000", ADJUST_QFQ, date(2026, 1, 1), date(2026, 1, 10))
    assert fake.calls["code"] == "sh.600000"
    assert fake.calls["adjustflag"] == "2"  # qfq
    assert fake.calls["start_date"] == "2026-01-01"
    assert fake.calls["end_date"] == "2026-01-10"
    assert [b.market_time.date() for b in bars] == [date(2026, 1, 5)]
    assert bars[0].source == "baostock"
    assert bars[0].price == 10.2
    assert fake.logged_out is True


def test_baostock_fetch_login_failure_raises(monkeypatch):
    _install_baostock(monkeypatch, _FakeBaoStock([], login_error="10001001"))
    with pytest.raises(RuntimeError, match="登录失败"):
        _baostock_fetch("600000", ADJUST_NONE, date(2026, 1, 1), date(2026, 1, 10))


def test_baostock_fetch_query_failure_raises(monkeypatch):
    fake = _install_baostock(
        monkeypatch, _FakeBaoStock([], query_error="10004011")
    )
    with pytest.raises(RuntimeError, match="查询失败"):
        _baostock_fetch("830799", ADJUST_NONE, date(2026, 1, 1), date(2026, 1, 10))
    # 北交所只认 sh./sz. 前缀，BaoStock 必然拒绝
    assert fake.calls["code"] == "bj.830799"


_CHAIN_ARGS = ("600000", ADJUST_NONE, date(2026, 1, 1), date(2026, 1, 10))


def test_fetch_from_sources_falls_back_to_sina(monkeypatch):
    bars = [_mk_bar("600000", "2026-01-05")]
    _patch_sources(
        monkeypatch,
        akshare=_stub(exc=ConnectionError("eastmoney down")),
        sina=_stub(bars),
        baostock=_stub(exc=RuntimeError("不应被调用")),
    )
    assert _fetch_from_sources(*_CHAIN_ARGS) == bars


def test_fetch_from_sources_skips_empty_then_uses_baostock(monkeypatch):
    bars = [_mk_bar("600000", "2026-01-06")]
    _patch_sources(
        monkeypatch,
        akshare=_stub([]),
        sina=_stub(exc=RuntimeError("sina down")),
        baostock=_stub(bars),
    )
    assert _fetch_from_sources(*_CHAIN_ARGS) == bars


def test_fetch_from_sources_all_errors_raises(monkeypatch):
    _patch_sources(
        monkeypatch,
        akshare=_stub(exc=ConnectionError("a")),
        sina=_stub(exc=RuntimeError("b")),
        baostock=_stub(exc=RuntimeError("c")),
    )
    with pytest.raises(RuntimeError) as err:
        _fetch_from_sources(*_CHAIN_ARGS)
    message = str(err.value)
    assert "akshare" in message
    assert "akshare_sina" in message
    assert "baostock" in message


def test_fetch_from_sources_all_empty_returns_empty(monkeypatch):
    """三个源都正常返回空 → 该标的无数据（停牌/退市），不算失败。"""
    _patch_sources(monkeypatch)
    assert _fetch_from_sources(*_CHAIN_ARGS) == []


def test_fetch_from_sources_empty_beats_errors(monkeypatch):
    """有源明确返回空时不应报错，否则会误报为数据源故障。"""
    _patch_sources(
        monkeypatch,
        akshare=_stub(exc=ConnectionError("a")),
        sina=_stub([]),
        baostock=_stub(exc=RuntimeError("c")),
    )
    assert _fetch_from_sources(*_CHAIN_ARGS) == []


@pytest.mark.asyncio
async def test_sync_uses_fallback_chain(db_session, monkeypatch):
    """EastMoney 失败时 sync 仍能落库（Sina 命中）。"""
    monkeypatch.setattr("app.history.service._RETRY_BASE_SECONDS", 0.0)
    bars = [_mk_bar("600000", "2026-01-05")]
    _patch_sources(monkeypatch, akshare=_stub(exc=ConnectionError("down")), sina=_stub(bars))
    svc = HistoricalDataService(db_session)
    added = await svc.sync("600000", datetime(2026, 1, 1), datetime(2026, 1, 10))
    assert added == 1
    assert len(svc.get_cached("600000", datetime(2026, 1, 1), datetime(2026, 1, 10))) == 1


@pytest.mark.asyncio
async def test_sync_all_sources_empty_returns_zero(db_session, monkeypatch):
    """所有源都没数据时 sync 返回 0，而不是抛错。"""
    _patch_sources(monkeypatch)
    svc = HistoricalDataService(db_session)
    added = await svc.sync("600000", datetime(2026, 1, 1), datetime(2026, 1, 10))
    assert added == 0


@pytest.mark.asyncio
async def test_get_history_all_sources_error_falls_back_to_cache(db_session, monkeypatch):
    """所有源都故障时 get_history 不抛异常，返回空缓存并标记不完整。"""
    monkeypatch.setattr("app.history.service._RETRY_BASE_SECONDS", 0.0)
    _patch_sources(
        monkeypatch,
        akshare=_stub(exc=ConnectionError("a")),
        sina=_stub(exc=RuntimeError("b")),
        baostock=_stub(exc=RuntimeError("c")),
    )
    svc = HistoricalDataService(db_session)
    result = await svc.get_history("600000", datetime(2026, 1, 5), datetime(2026, 1, 6))
    assert result.source == "cache"
    assert result.bars == []
    assert result.is_complete is False


class _FakeProviderManager:
    """只实现历史接口的 ProviderManager 替身。"""

    def __init__(self, names=("akshare",), history=None):
        self.providers = [types.SimpleNamespace(name=n) for n in names]
        self._history = list(history or [])
        self.calls = 0

    async def get_history(self, symbol, period, start_time, end_time):
        self.calls += 1
        return list(self._history)


def _counting_akshare(counter, bars=None, exc=None):
    def _fetch(symbol, adjust, start, end):
        counter["akshare"] += 1
        if exc is not None:
            raise exc
        return list(bars or [])

    return _fetch


def test_provider_covered_sources_without_manager():
    svc = HistoricalDataService.__new__(HistoricalDataService)
    svc._provider_manager = None
    assert svc._provider_covered_sources() == ()


def test_provider_covered_sources_detects_akshare():
    svc = HistoricalDataService.__new__(HistoricalDataService)
    svc._provider_manager = _FakeProviderManager(names=("tencent", "akshare"))
    assert svc._provider_covered_sources() == ("akshare",)


def test_provider_covered_sources_ignores_non_akshare():
    svc = HistoricalDataService.__new__(HistoricalDataService)
    svc._provider_manager = _FakeProviderManager(names=("tencent", "qmt"))
    assert svc._provider_covered_sources() == ()


def test_fetch_from_sources_skips_requested_sources(monkeypatch):
    counter = {"akshare": 0}
    bars = [_mk_bar("600000", "2026-01-05")]
    _patch_sources(
        monkeypatch,
        akshare=_counting_akshare(counter, exc=ConnectionError("should be skipped")),
        sina=_stub(bars),
    )
    result = _fetch_from_sources(*_CHAIN_ARGS, skip=("akshare",))
    assert result == bars
    assert counter["akshare"] == 0


@pytest.mark.asyncio
async def test_sync_skips_eastmoney_when_manager_covers_it(db_session, monkeypatch):
    """manager 已含 akshare provider 时，回退链不再重复打 EastMoney。"""
    monkeypatch.setattr("app.history.service._RETRY_BASE_SECONDS", 0.0)
    counter = {"akshare": 0}
    bars = [_mk_bar("600000", "2026-01-05")]
    _patch_sources(
        monkeypatch,
        akshare=_counting_akshare(counter, exc=ConnectionError("eastmoney down")),
        sina=_stub(bars),
    )
    manager = _FakeProviderManager(names=("tencent", "akshare"))
    svc = HistoricalDataService(db_session, provider_manager=manager)
    added = await svc.sync("600000", datetime(2026, 1, 1), datetime(2026, 1, 10))
    assert added == 1
    assert manager.calls == 1  # manager 打了一次 EastMoney
    assert counter["akshare"] == 0  # 回退链没有重复打


@pytest.mark.asyncio
async def test_sync_still_uses_eastmoney_without_akshare_provider(
    db_session, monkeypatch
):
    """manager 只有不支持历史的 tencent 时，回退链仍要打 EastMoney。"""
    monkeypatch.setattr("app.history.service._RETRY_BASE_SECONDS", 0.0)
    counter = {"akshare": 0}
    bars = [_mk_bar("600000", "2026-01-05")]
    monkeypatch.setattr(
        "app.history.service._akshare_fetch", _counting_akshare(counter, bars=bars)
    )
    manager = _FakeProviderManager(names=("tencent",))
    svc = HistoricalDataService(db_session, provider_manager=manager)
    added = await svc.sync("600000", datetime(2026, 1, 1), datetime(2026, 1, 10))
    assert added == 1
    assert counter["akshare"] == 1
