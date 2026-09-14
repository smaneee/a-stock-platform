"""通达信数据源测试：全部使用鸭子类型的假 tdxpy API，不联网。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.market_data.tdx_provider import (
    KLINE_TYPE_BY_PERIOD,
    MAX_KLINE_COUNT,
    TdxProvider,
    _bar_volume,
    _volume_number,
    parse_quote_row,
    to_tdx_market,
    validate_quote,
)

HOST_A = "10.0.0.1"
HOST_B = "10.0.0.2"
PROBE_CODE = "600519"


def _quote_row(
    code: str,
    market: int,
    *,
    price: float = 10.0,
    last_close: float = 10.0,
    vol: float = 1000,
    bid1: float = 9.99,
    ask1: float = 10.01,
) -> dict:
    return {
        "market": market,
        "code": code,
        "price": price,
        "last_close": last_close,
        "open": price,
        "high": max(price, last_close) * 1.01,
        "low": min(price, last_close) * 0.99,
        "vol": vol,
        "amount": price * vol * 100,
        "bid1": bid1,
        "ask1": ask1,
        "servertime": "15:00:00.000",
    }


def _bar_row(when: datetime, price: float) -> dict:
    return {
        "open": price,
        "close": price,
        "high": price * 1.01,
        "low": price * 0.99,
        "vol": 100.0,
        "amount": price * 10_000.0,
        "year": when.year,
        "month": when.month,
        "day": when.day,
        "hour": 15,
        "minute": 0,
    }


class _FakeCluster:
    """一组假服务器的共享状态。"""

    def __init__(
        self,
        serving_hosts,
        *,
        quote_rows=None,
        bars=None,
        probe=True,
        probe_blocked_hosts=(),
        bar_failures=0,
    ):
        self.serving_hosts = set(serving_hosts)
        # 连得上但探活返回空的主机（模拟「只服务特定客户」的服务器）
        self.probe_blocked_hosts = set(probe_blocked_hosts)
        self.quote_rows = dict(quote_rows or {})
        if probe:
            self.quote_rows.setdefault(PROBE_CODE, _quote_row(PROBE_CODE, 1))
        self.bars = list(bars or [])
        self.connect_attempts: list[str] = []
        self.quote_calls = 0
        self.bar_calls = 0
        self.index_bar_calls: list[tuple[int, str]] = []
        self.disconnects = 0
        # 前 N 次 K 线请求模拟「服务端掐断连接」，用于验证重连重试
        self.bar_failures = bar_failures

    def factory(self):
        return _FakeApi(self)

    def chunk(self, start: int, count: int) -> list[dict]:
        """模拟真实分页：offset 0 是最新一页，页内按时间升序。"""
        total = len(self.bars)
        end = max(0, total - start)
        begin = max(0, total - start - count)
        return self.bars[begin:end]


class _FakeApi:
    """鸭子类型的 tdxpy API：connect / disconnect / get_security_*。"""

    def __init__(self, cluster: _FakeCluster):
        self._cluster = cluster
        self._connected = False
        self._host: str | None = None

    def connect(self, host, port=7709, time_out=5.0):
        self._cluster.connect_attempts.append(host)
        if host not in self._cluster.serving_hosts:
            return False
        self._connected = True
        self._host = host
        return True

    def disconnect(self):
        self._connected = False
        self._cluster.disconnects += 1

    def get_security_quotes(self, pairs):
        if not self._connected:
            raise RuntimeError("not connected")
        self._cluster.quote_calls += 1
        if self._host in self._cluster.probe_blocked_hosts and list(pairs) == [
            (1, PROBE_CODE)
        ]:
            return []
        rows = []
        for _market, code in pairs:
            row = self._cluster.quote_rows.get(code)
            if row is not None:
                rows.append(row)
        return rows

    def get_security_bars(self, kline_type, market, code, start, count):
        if not self._connected:
            raise RuntimeError("not connected")
        self._cluster.bar_calls += 1
        if self._cluster.bar_failures > 0:
            self._cluster.bar_failures -= 1
            self._connected = False  # 服务端掐断，连接随即不可用
            raise RuntimeError("接收数据异常，请稍后再试。")
        return self._cluster.chunk(start, min(count, MAX_KLINE_COUNT))

    def get_index_bars(self, kline_type, market, code, start, count):
        if not self._connected:
            raise RuntimeError("not connected")
        self._cluster.bar_calls += 1
        self._cluster.index_bar_calls.append((market, code))
        return self._cluster.chunk(start, min(count, MAX_KLINE_COUNT))


def _provider(cluster: _FakeCluster, **kwargs) -> TdxProvider:
    kwargs.setdefault("hosts", (HOST_A, HOST_B))
    return TdxProvider(api_factory=cluster.factory, **kwargs)


def test_to_tdx_market_mapping():
    assert to_tdx_market("600519") == 1
    assert to_tdx_market("sh600519") == 1
    assert to_tdx_market("000001") == 0
    assert to_tdx_market("300750") == 0
    assert to_tdx_market("688981") == 1
    # 北交所走市场号 3（服务端回包里的 market 是 2）
    assert to_tdx_market("830799") == 3
    assert to_tdx_market("430139") == 3
    assert to_tdx_market("920008") == 3
    assert to_tdx_market("bj430139") == 3
    assert to_tdx_market("bj920008") == 3
    assert to_tdx_market("abcdef") is None


def test_parse_quote_row_rejects_bad_rows():
    good = parse_quote_row(_quote_row("600519", 1))
    assert good is not None and validate_quote(good)
    assert parse_quote_row(_quote_row("600519", 1, price=0.0)) is None
    assert parse_quote_row(_quote_row("600519", 1, price=20.0, last_close=10.0)) is None


async def test_get_quotes_parses_and_converts_units():
    cluster = _FakeCluster(
        (HOST_A,),
        quote_rows={
            "000001": _quote_row(
                "000001", 0, price=11.74, last_close=11.70, vol=1234
            )
        },
    )
    provider = _provider(cluster)
    quotes = await provider.get_quotes(["000001"])
    await provider.close()

    assert set(quotes) == {"000001"}
    quote = quotes["000001"]
    assert quote.price == pytest.approx(11.74)
    assert quote.previous_close == pytest.approx(11.70)
    # 通达信 vol 单位是「手」，出口必须换算成「股」
    assert quote.volume == pytest.approx(123_400)
    assert quote.bid_price == pytest.approx(9.99)
    assert quote.ask_price == pytest.approx(10.01)
    assert quote.source == "tdx"


async def test_get_quotes_chunks_beyond_protocol_limit():
    symbols = [f"60{i:04d}" for i in range(170)]
    rows = {symbol: _quote_row(symbol, 1) for symbol in symbols}
    cluster = _FakeCluster((HOST_A,), quote_rows=rows)
    provider = _provider(cluster)
    quotes = await provider.get_quotes(symbols)
    await provider.close()

    assert len(quotes) == len(symbols)
    # 1 次探活 + ceil(170 / 80) = 3 次批量
    assert cluster.quote_calls == 1 + 3


async def test_get_quotes_covers_beijing_codes():
    """北交所不再被跳过：与沪深混批时应一次拿全。"""
    symbols = ["600519", "430139", "830799", "920008"]
    rows = {
        "600519": _quote_row("600519", 1),
        "430139": _quote_row("430139", 2),
        "830799": _quote_row("830799", 2),
        "920008": _quote_row("920008", 2),
    }
    cluster = _FakeCluster((HOST_A,), quote_rows=rows)
    provider = _provider(cluster)
    quotes = await provider.get_quotes(symbols)
    await provider.close()

    assert set(quotes) == set(symbols)
    # 1 次探活 + 1 次批量（不同市场可同批请求）
    assert cluster.quote_calls == 2


async def test_failover_when_first_host_does_not_serve_data():
    cluster = _FakeCluster((HOST_A, HOST_B), probe_blocked_hosts=(HOST_A,))
    provider = _provider(cluster)
    quotes = await provider.get_quotes(["600519"])
    await provider.close()

    assert "600519" in quotes
    assert cluster.connect_attempts[0] == HOST_A
    assert HOST_B in cluster.connect_attempts


async def test_all_hosts_down_returns_empty_without_raising():
    cluster = _FakeCluster(())
    provider = _provider(cluster)
    assert await provider.get_quotes(["600519"]) == {}
    assert await provider.get_history(
        "600519", "daily", datetime(2026, 1, 1), datetime(2026, 2, 1)
    ) == []
    await provider.close()


async def test_get_history_filters_and_orders_single_page():
    dates = [datetime(2023, 1, 1) + timedelta(days=index) for index in range(900)]
    bars = [
        _bar_row(when, 10.0 + index * 0.01) for index, when in enumerate(dates)
    ]
    cluster = _FakeCluster((HOST_A,), bars=bars)
    provider = _provider(cluster)
    # 调用方约定：日线区间用当天的 00:00:00 ~ 23:59:59.999999（见 history_ingest_worker）
    end = datetime.combine(dates[899].date(), datetime.max.time())
    quotes = await provider.get_history("600519", "daily", dates[850], end)
    await provider.close()

    assert len(quotes) == 50
    assert [quote.market_time.date() for quote in quotes] == [
        when.date() for when in dates[850:900]
    ]
    # 区间内第一根的昨收取自区间外的前一根
    assert quotes[0].previous_close == pytest.approx(10.0 + 849 * 0.01)
    assert quotes[0].volume == pytest.approx(100.0 * 100)
    assert cluster.bar_calls == 1


def test_bar_volume_normalizes_units_by_period():
    """日线成交量是「手」，分钟线直接是「股」，出口都要归到「股」。"""
    assert _bar_volume(1000, KLINE_TYPE_BY_PERIOD["daily"]) == pytest.approx(100_000)
    assert _bar_volume(1000, KLINE_TYPE_BY_PERIOD["monthly"]) == pytest.approx(100_000)
    for period in ("1m", "5m", "15m", "30m", "60m"):
        assert _bar_volume(1000, KLINE_TYPE_BY_PERIOD[period]) == pytest.approx(1000)


def test_volume_number_zeroes_denormal_protocol_values():
    """回归：通达信把「无数据」编码成 5.877e-39，泄漏到接口就是脏数据。"""
    assert _volume_number(5.877471754111438e-39) == 0.0
    assert _volume_number(0) == 0.0
    assert _volume_number(None) == 0.0
    assert _volume_number("abc") == 0.0
    assert _volume_number(1234.0) == pytest.approx(1234.0)
    # 分钟线里这类值要和真正的 0 一样被抹平
    assert _bar_volume(5.877471754111438e-39, KLINE_TYPE_BY_PERIOD["1m"]) == 0.0
    quote = parse_quote_row(_quote_row("600519", 1, vol=5.877471754111438e-39))
    assert quote is not None and quote.volume == 0.0


async def test_get_history_minute_bars_keep_share_volume():
    """回归：分钟线 vol 已是「股」，再乘 100 会把分时均价算成百分之一。"""
    rows = [
        {
            "open": 9.25,
            "close": 9.25,
            "high": 9.26,
            "low": 9.24,
            # 实测 600000 的 1 分钟线：vol=股、amount=元，两者相除就是均价
            "vol": 100_000.0,
            "amount": 925_300.0,
            "year": 2026,
            "month": 9,
            "day": 11,
            "hour": 9,
            "minute": 31,
        }
    ]
    cluster = _FakeCluster((HOST_A,), bars=rows)
    provider = _provider(cluster)
    quotes = await provider.get_history(
        "600519", "1m", datetime(2026, 9, 11), datetime(2026, 9, 11, 23, 59, 59)
    )
    await provider.close()

    assert len(quotes) == 1
    assert quotes[0].volume == pytest.approx(100_000.0)
    assert quotes[0].amount == pytest.approx(925_300.0)
    assert quotes[0].amount / quotes[0].volume == pytest.approx(9.253)


async def test_get_history_walks_multiple_pages():
    dates = [datetime(2020, 1, 1) + timedelta(days=index) for index in range(1700)]
    bars = [_bar_row(when, 5.0 + index * 0.001) for index, when in enumerate(dates)]
    cluster = _FakeCluster((HOST_A,), bars=bars)
    provider = _provider(cluster)
    end = datetime.combine(dates[-1].date(), datetime.max.time())
    quotes = await provider.get_history("600519", "daily", dates[0], end)
    await provider.close()

    assert len(quotes) == 1700
    assert quotes[0].market_time.date() == dates[0].date()
    assert quotes[-1].market_time.date() == dates[-1].date()
    assert cluster.bar_calls == 3


async def test_get_history_rejects_unsupported_period():
    cluster = _FakeCluster((HOST_A,))
    provider = _provider(cluster)
    assert await provider.get_history(
        "600519", "yearly", datetime(2026, 1, 1), datetime(2026, 2, 1)
    ) == []
    await provider.close()
    # 周期不支持时不发请求
    assert cluster.bar_calls == 0


async def test_get_history_requests_beijing_market():
    """北交所日线要真的走市场号 3，而不是被当成「不支持」提前返回。"""
    bars = [_bar_row(datetime(2026, 1, 5) + timedelta(days=index), 10.0) for index in range(10)]
    cluster = _FakeCluster((HOST_A,), bars=bars)
    provider = _provider(cluster)
    quotes = await provider.get_history(
        "920008", "daily", datetime(2026, 1, 1), datetime(2026, 2, 1)
    )
    await provider.close()

    assert cluster.bar_calls == 1
    assert [quote.symbol for quote in quotes] == ["920008"] * len(quotes)


async def test_get_history_index_uses_index_bars():
    """指数必须走 get_index_bars(沪市市场号 1)，不能借用个股接口。"""
    bars = [
        _bar_row(datetime(2026, 1, 5) + timedelta(days=index), 4500.0 + index)
        for index in range(10)
    ]
    cluster = _FakeCluster((HOST_A,), bars=bars)
    provider = _provider(cluster)
    quotes = await provider.get_history(
        "sh000300", "daily", datetime(2026, 1, 1), datetime(2026, 2, 1)
    )
    await provider.close()

    assert cluster.index_bar_calls == [(1, "000300")]
    assert cluster.bar_calls == 1
    assert quotes
    assert {quote.symbol for quote in quotes} == {"sh000300"}
    assert quotes[0].price == 4500.0


async def test_get_history_stock_never_uses_index_bars():
    """个股走 get_security_bars，不应触发指数接口。"""
    bars = [_bar_row(datetime(2026, 1, 5), 10.0)]
    cluster = _FakeCluster((HOST_A,), bars=bars)
    provider = _provider(cluster)
    await provider.get_history(
        "000001", "daily", datetime(2026, 1, 1), datetime(2026, 2, 1)
    )
    await provider.close()

    assert cluster.index_bar_calls == []
    assert cluster.bar_calls == 1


async def test_name_resolver_fills_missing_names():
    cluster = _FakeCluster((HOST_A,))
    seen: list[list[str]] = []

    def resolver(symbols):
        seen.append(list(symbols))
        return {symbol: "贵州茅台" for symbol in symbols}

    provider = _provider(cluster, name_resolver=resolver)
    quotes = await provider.get_quotes(["600519", "000001"])
    await provider.close()

    assert quotes["600519"].name == "贵州茅台"
    assert seen == [["600519"]]


async def test_health_check_reflects_availability():
    healthy = _provider(_FakeCluster((HOST_A,)))
    assert await healthy.health_check() is True
    await healthy.close()

    down = _provider(_FakeCluster(()))
    assert await down.health_check() is False
    await down.close()


async def test_call_reconnects_and_retries_after_dropped_connection():
    """服务端掐断连接时换一条新连接重试一次，而不是直接当成「没有数据」。"""
    cluster = _FakeCluster(
        (HOST_A,),
        bars=[_bar_row(datetime(2026, 9, 11), 10.0)],
        bar_failures=1,
    )
    provider = _provider(cluster)
    # 日线区间用当天 00:00:00 ~ 23:59:59.999999，否则 15:00 的 K 线会被过滤掉
    end = datetime.combine(datetime(2026, 9, 11).date(), datetime.max.time())
    hist = await provider.get_history(
        "600519", "daily", datetime(2026, 9, 1), end
    )
    await provider.close()

    assert len(hist) == 1
    assert cluster.bar_calls == 2  # 首次失败 + 重连后成功
    assert cluster.connect_attempts.count(HOST_A) >= 2


async def test_call_returns_default_when_retry_also_fails():
    """重试用尽后仍失败，返回 default（不抛异常），由上层回退其它数据源。"""
    cluster = _FakeCluster(
        (HOST_A,),
        bars=[_bar_row(datetime(2026, 9, 11), 10.0)],
        bar_failures=99,
    )
    provider = _provider(cluster)
    end = datetime.combine(datetime(2026, 9, 11).date(), datetime.max.time())
    hist = await provider.get_history(
        "600519", "daily", datetime(2026, 9, 1), end
    )
    await provider.close()

    assert hist == []
    assert cluster.bar_calls == 2  # 首次 + 一次重试，不会无限重试
