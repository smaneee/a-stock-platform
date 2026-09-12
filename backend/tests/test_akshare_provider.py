"""AKShare 行情数据源单元测试（假 ak 对象，全程不触网）。"""
from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from app.market_data import akshare_provider as mod
from app.market_data.akshare_provider import AkshareProvider


class _FakeAk:
    def __init__(self, frame=None, error: Exception | None = None):
        self.frame = frame if frame is not None else _frame()
        self.error = error
        self.kwargs: dict | None = None

    def stock_zh_a_hist(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.frame


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"日期": "2024-01-02", "开盘": 10.0, "收盘": 10.5, "最高": 10.8,
             "最低": 9.9, "成交量": 1000, "成交额": 10000},
            {"日期": "2024-01-03", "开盘": 10.5, "收盘": 11.0, "最高": 11.2,
             "最低": 10.4, "成交量": 2000, "成交额": 22000},
        ]
    )


async def test_get_quotes_is_empty():
    provider = AkshareProvider()
    assert await provider.get_quotes(["600000"]) == {}
    assert await provider.get_quote("600000") is None


async def test_history_returns_empty_when_unavailable(monkeypatch):
    monkeypatch.setattr(mod, "_AKSHARE_AVAILABLE", False)
    now = datetime(2024, 1, 1, tzinfo=UTC)
    assert await AkshareProvider().get_history("600000", "daily", now, now) == []


async def test_history_parses_rows_and_passes_arguments(monkeypatch):
    monkeypatch.setattr(mod, "_AKSHARE_AVAILABLE", True)
    fake = _FakeAk()
    monkeypatch.setattr(mod, "ak", fake)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 2, 1, tzinfo=UTC)
    quotes = await AkshareProvider().get_history("600000", "daily", start, end)
    assert len(quotes) == 2
    assert fake.kwargs == {
        "symbol": "600000",
        "period": "daily",
        "start_date": "20240101",
        "end_date": "20240201",
        "adjust": "",
    }
    first = quotes[0]
    assert first.symbol == "600000"
    assert first.source == "akshare"
    assert first.price == 10.5
    assert first.open == 10.0
    assert first.high == 10.8
    assert first.low == 9.9
    assert first.volume == 1000.0
    assert first.amount == 10000.0
    assert first.previous_close == 0.0
    assert first.market_time == datetime(2024, 1, 2)
    assert first.is_stale is False


@pytest.mark.parametrize(
    ("period", "expected"),
    [("1m", "1"), ("5m", "5"), ("weekly", "weekly"), ("unknown", "daily")],
)
async def test_period_mapping(monkeypatch, period, expected):
    monkeypatch.setattr(mod, "_AKSHARE_AVAILABLE", True)
    fake = _FakeAk()
    monkeypatch.setattr(mod, "ak", fake)
    now = datetime(2024, 1, 1, tzinfo=UTC)
    await AkshareProvider().get_history("600000", period, now, now)
    assert fake.kwargs["period"] == expected


async def test_history_swallows_upstream_error(monkeypatch):
    monkeypatch.setattr(mod, "_AKSHARE_AVAILABLE", True)
    monkeypatch.setattr(mod, "ak", _FakeAk(error=RuntimeError("eastmoney down")))
    now = datetime(2024, 1, 1, tzinfo=UTC)
    assert await AkshareProvider().get_history("600000", "daily", now, now) == []


async def test_history_defaults_missing_columns_to_zero(monkeypatch):
    monkeypatch.setattr(mod, "_AKSHARE_AVAILABLE", True)
    frame = pd.DataFrame([{"日期": "2024-01-02", "收盘": 10.5}])
    monkeypatch.setattr(mod, "ak", _FakeAk(frame=frame))
    now = datetime(2024, 1, 1, tzinfo=UTC)
    quote = (await AkshareProvider().get_history("600000", "daily", now, now))[0]
    assert quote.open == 0.0
    assert quote.high == 0.0
    assert quote.amount == 0.0


async def test_subscribe_and_health(monkeypatch):
    provider = AkshareProvider()
    assert await provider.subscribe(["600000"], lambda q: None) is None
    monkeypatch.setattr(mod, "_AKSHARE_AVAILABLE", True)
    assert await provider.health_check() is True
    monkeypatch.setattr(mod, "_AKSHARE_AVAILABLE", False)
    assert await provider.health_check() is False
