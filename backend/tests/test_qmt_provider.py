"""QMT 行情数据源单元测试。

测试环境未安装 xtquant，这里用假 xtdata 覆盖代码/后缀映射、时间戳换算、
盘口取值与异常降级分支，全程不触网。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.market_data import qmt_provider as mod
from app.market_data.qmt_provider import QmtProvider


class _FakeXtdata:
    """最小可用的 xtdata 替身。"""

    def __init__(self, ticks=None, error: Exception | None = None):
        self.ticks = ticks or {}
        self.error = error
        self.requested: list[list[str]] = []

    def get_full_tick(self, symbols):
        self.requested.append(list(symbols))
        if self.error is not None:
            raise self.error
        return self.ticks


def _tick(**overrides):
    tick = {
        "instrumentName": "模拟股",
        "lastPrice": 10.5,
        "open": 10.0,
        "high": 10.8,
        "low": 9.9,
        "lastClose": 10.2,
        "volume": 1234,
        "amount": 5678,
        "bidPrice": [10.49, 100],
        "askPrice": [10.51, 200],
        "time": 1700000000000,
    }
    tick.update(overrides)
    return tick


@pytest.fixture
def qmt(monkeypatch):
    """强制判定 QMT 可用，便于覆盖解析分支。"""
    monkeypatch.setattr(mod, "_QMT_AVAILABLE", True)
    return monkeypatch


async def test_unavailable_returns_empty(monkeypatch):
    monkeypatch.setattr(mod, "_QMT_AVAILABLE", False)
    provider = QmtProvider()
    assert await provider.get_quotes(["600000"]) == {}
    assert await provider.get_quote("600000") is None
    assert await provider.health_check() is False


async def test_maps_symbol_suffixes_and_parses_fields(qmt):
    fake = _FakeXtdata(
        {
            "600000.SH": _tick(),
            "000001.SZ": _tick(instrumentName="平安银行", lastPrice=12.0),
            "920001.BJ": _tick(lastPrice=8.0),
            "430047.BJ": _tick(lastPrice=7.0),
            "600519.SH": _tick(lastPrice=1700.0),
        }
    )
    qmt.setattr(mod, "xtdata", fake)
    result = await QmtProvider().get_quotes(
        ["600000", "000001", "920001", "430047", "600519.SH"]
    )
    assert set(result) == {"600000", "000001", "920001", "430047", "600519.SH"}
    # 已有后缀的代码原样透传，其余按市场补全
    assert fake.requested[0] == [
        "600000.SH",
        "000001.SZ",
        "920001.BJ",
        "430047.BJ",
        "600519.SH",
    ]
    quote = result["600000"]
    assert quote.source == "qmt"
    assert quote.name == "模拟股"
    assert quote.price == 10.5
    assert quote.bid_price == 10.49  # 序列取首档
    assert quote.ask_price == 10.51
    assert quote.is_stale is False
    # 毫秒时间戳换算为 UTC 的无时区时间
    assert quote.market_time == datetime(2023, 11, 14, 22, 13, 20)
    assert result["920001"].price == 8.0


async def test_second_level_timestamp_and_scalar_bid(qmt):
    qmt.setattr(
        mod,
        "xtdata",
        _FakeXtdata({"600000.SH": _tick(time=1700000000, bidPrice=10.4, askPrice=None)}),
    )
    quote = (await QmtProvider().get_quotes(["600000"]))["600000"]
    assert quote.market_time == datetime(2023, 11, 14, 22, 13, 20)
    assert quote.bid_price == 10.4  # 非序列直接转 float
    assert quote.ask_price == 0.0  # None 兜底为 0


async def test_missing_time_uses_now_and_raw_key_fallback(qmt):
    # 上游若用未加后缀的 key 返回，也应按原始 symbol 命中
    qmt.setattr(mod, "xtdata", _FakeXtdata({"600000": _tick(time=None)}))
    quote = (await QmtProvider().get_quotes(["600000"]))["600000"]
    assert quote.market_time is not None


async def test_missing_or_empty_tick_is_skipped(qmt):
    qmt.setattr(mod, "xtdata", _FakeXtdata({"600000.SH": _tick(), "000001.SZ": {}}))
    result = await QmtProvider().get_quotes(["600000", "000001", "300750"])
    assert set(result) == {"600000"}


async def test_xtdata_error_returns_empty(qmt):
    qmt.setattr(mod, "xtdata", _FakeXtdata(error=RuntimeError("xtdown")))
    assert await QmtProvider().get_quotes(["600000"]) == {}


async def test_get_quote_returns_single(qmt):
    qmt.setattr(mod, "xtdata", _FakeXtdata({"600000.SH": _tick()}))
    quote = await QmtProvider().get_quote("600000")
    assert quote is not None
    assert quote.symbol == "600000"


async def test_get_quote_returns_none_when_tick_missing(qmt):
    qmt.setattr(mod, "xtdata", _FakeXtdata({}))
    assert await QmtProvider().get_quote("600000") is None


async def test_history_subscribe_and_health(qmt):
    provider = QmtProvider()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    assert await provider.get_history("600000", "daily", now, now) == []
    assert await provider.subscribe(["600000"], lambda q: None) is None
    assert await provider.health_check() is True
