"""东方财富行情数据源测试。"""
from datetime import datetime, timedelta

import httpx
import pytest

from app.market_data.base import MarketDataProvider, QuoteData
from app.market_data.eastmoney_provider import (
    EastmoneyProvider,
    EastmoneyHostPool,
    kline_available,
    parse_history_rows,
    parse_quote_rows,
    to_eastmoney_secid,
    validate_quote,
)
from app.market_data.provider_manager import ProviderManager
from app.time_utils import utc_now

_NOW_TS = int(datetime.now().timestamp())


# ──────── secid 转换 ────────


def test_to_eastmoney_secid():
    assert to_eastmoney_secid("600000") == "1.600000"
    assert to_eastmoney_secid("688981") == "1.688981"
    assert to_eastmoney_secid("510300") == "1.510300"
    assert to_eastmoney_secid("900901") == "1.900901"  # 沪 B
    assert to_eastmoney_secid("000001") == "0.000001"
    assert to_eastmoney_secid("300750") == "0.300750"
    assert to_eastmoney_secid("920000") == "0.920000"  # 北交所，9 开头但不是沪 B
    assert to_eastmoney_secid("430047") == "0.430047"
    assert to_eastmoney_secid("sh600000") == "1.600000"
    assert to_eastmoney_secid("sz000001") == "0.000001"
    assert to_eastmoney_secid("bj920000") == "0.920000"


# ──────── 实时行情解析 ────────


def _row(**overrides) -> dict:
    row = {
        "f12": "600000",
        "f14": "浦发银行",
        "f2": 10.5,
        "f5": 12345,
        "f6": 1.3e8,
        "f15": 10.6,
        "f16": 10.3,
        "f17": 10.45,
        "f18": 10.4,
        "f19": 10.49,
        "f39": 10.51,
        "f124": _NOW_TS,
    }
    row.update(overrides)
    return row


def test_parse_quote_rows_ok():
    quotes = parse_quote_rows({"data": {"diff": [_row()]}})
    quote = quotes["600000"]
    assert quote.name == "浦发银行"
    assert quote.price == 10.5
    assert quote.previous_close == 10.4
    assert quote.open == 10.45
    assert quote.high == 10.6
    assert quote.low == 10.3
    assert quote.volume == 12345 * 100  # 手转股
    assert quote.amount == 1.3e8
    # 批量行情接口没有五档盘口字段，盘口一律留空交由上层按最新价兜底
    assert quote.bid_price == 0.0
    assert quote.ask_price == 0.0
    assert quote.execution_price("BUY") == 10.5
    assert quote.source == "eastmoney"
    assert quote.is_stale is False
    assert quote.market_time is not None
    assert quote.market_time.date() == datetime.now().date()


def test_parse_quote_rows_accepts_normal_change():
    """涨跌幅在 ±20% 内的正常行情应通过。"""
    quotes = parse_quote_rows({"data": {"diff": [_row(f2=11.0)]}})
    assert quotes["600000"].price == 11.0


def test_parse_quote_rows_skips_suspended():
    """停牌时东财用 '-' 表示价格，应跳过该行。"""
    assert parse_quote_rows({"data": {"diff": [_row(f2="-")]}}) == {}


def test_parse_quote_rows_rejects_extreme_gap():
    """单日涨跌幅超过 ±20% 视为异常（与腾讯数据源同规则）。"""
    assert parse_quote_rows({"data": {"diff": [_row(f2=20.0)]}}) == {}
    assert parse_quote_rows({"data": {"diff": [_row(f2=1.0)]}}) == {}


def test_parse_quote_rows_rejects_inverted_high_low():
    assert parse_quote_rows({"data": {"diff": [_row(f15=10.0, f16=10.8)]}}) == {}


def test_parse_quote_rows_rejects_html_name():
    assert parse_quote_rows({"data": {"diff": [_row(f14="<script>")]}}) == {}


def test_parse_quote_rows_handles_dict_diff():
    """部分节点按序号返回字典而不是数组。"""
    quotes = parse_quote_rows({"data": {"diff": {"0": _row()}}})
    assert set(quotes) == {"600000"}


def test_parse_quote_rows_handles_empty_payload():
    assert parse_quote_rows({}) == {}
    assert parse_quote_rows({"data": None}) == {}
    assert parse_quote_rows({"data": {"diff": None}}) == {}
    assert parse_quote_rows({"data": {"diff": ["not-a-dict"]}}) == {}


def test_parse_quote_rows_ignores_implausible_timestamp():
    """时间戳明显不合理时不应把它当成行情时间。"""
    quotes = parse_quote_rows({"data": {"diff": [_row(f124=123)]}})
    assert quotes["600000"].market_time is None


# ──────── 历史行情解析 ────────


def test_parse_history_rows_daily():
    payload = {
        "data": {
            "name": "浦发银行",
            "klines": [
                "2026-08-03,9.50,9.63,9.70,9.45,123456,118000000.0,2.6,1.4,0.13,0.42",
                "2026-08-04,9.63,9.55,9.68,9.50,98765,95000000.0,1.9,-0.8,-0.08,0.34",
            ],
        }
    }
    bars = parse_history_rows(payload, "600000")
    assert len(bars) == 2
    first = bars[0]
    assert first.market_time == datetime(2026, 8, 3)
    assert first.open == 9.50
    assert first.price == 9.63
    assert first.high == 9.70
    assert first.low == 9.45
    assert first.volume == 123456 * 100
    assert first.amount == 118000000.0
    assert first.name == "浦发银行"
    assert first.source == "eastmoney"
    # 前收盘由上一根补齐，便于直接算涨跌幅
    assert bars[1].previous_close == 9.63


def test_parse_history_rows_trends_minute():
    payload = {
        "data": {
            "name": "浦发银行",
            "trends": ["2026-09-11 09:30,9.35,9.36,9.37,9.34,3778,3530000.0,9.35"],
        }
    }
    bars = parse_history_rows(payload, "600000", "1m")
    assert len(bars) == 1
    assert bars[0].market_time == datetime(2026, 9, 11, 9, 30)
    assert bars[0].price == 9.36
    assert bars[0].volume == 3778 * 100


def test_parse_history_rows_skips_bad_rows():
    payload = {
        "data": {
            "klines": [
                "not-a-row",
                "2026-08-03,0,0,0,0,0,0,0,0,0,0",  # 无有效价格
            ]
        }
    }
    assert parse_history_rows(payload, "600000") == []
    assert parse_history_rows({}, "600000") == []


# ──────── Provider 级校验 ────────


def _quote(**overrides) -> QuoteData:
    payload = {
        "symbol": "600000",
        "name": "正常",
        "price": 10.0,
        "open": 10.0,
        "high": 10.5,
        "low": 9.5,
        "previous_close": 10.0,
        "volume": 0.0,
        "amount": 0.0,
        "bid_price": 10.0,
        "ask_price": 10.0,
        "source": "eastmoney",
        "market_time": None,
        "received_at": utc_now(),
        "is_stale": False,
    }
    payload.update(overrides)
    return QuoteData(**payload)


def test_validate_quote_rules():
    assert validate_quote(_quote()) is True
    assert validate_quote(_quote(price=0)) is False
    assert validate_quote(_quote(high=9.0, low=10.0)) is False
    assert validate_quote(_quote(price=20.0, previous_close=10.0)) is False
    assert validate_quote(_quote(name="<script>")) is False
    assert validate_quote(_quote(name="贵州茅台")) is True


# ──────── Provider 请求行为 ────────


def _provider_with(handler, **kwargs) -> EastmoneyProvider:
    provider = EastmoneyProvider(**kwargs)
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


@pytest.mark.asyncio
async def test_provider_get_quotes_batches_requests():
    """超过 batch_size 时按批请求，secid 带正确市场前缀。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        secids = request.url.params["secids"]
        seen.append(secids)
        rows = [_row(f12=secid.split(".")[1]) for secid in secids.split(",")]
        return httpx.Response(200, json={"data": {"diff": rows}})

    provider = _provider_with(handler, batch_size=2)
    try:
        quotes = await provider.get_quotes(["600000", "000001", "300750"])
    finally:
        await provider.close()

    assert seen == ["1.600000,0.000001", "0.300750"]
    assert set(quotes) == {"600000", "000001", "300750"}


@pytest.mark.asyncio
async def test_provider_get_quotes_returns_empty_on_failure():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("blocked", request=request)

    # 单主机池：验证同一主机内的有限重试，不掺入主机切换
    provider = _provider_with(
        handler, max_retries=2, quote_hosts=("push2.eastmoney.com",)
    )
    try:
        assert await provider.get_quotes(["600000"]) == {}
    finally:
        await provider.close()
    # 连接类故障（被限流）直接判定该主机不可用并切换，不再反复重试同一台主机
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_provider_retries_same_host_on_http_error():
    """非连接类错误（如 5xx）在主机的重试预算内重试。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    provider = _provider_with(
        handler, max_retries=2, quote_hosts=("push2.eastmoney.com",)
    )
    try:
        assert await provider.get_quotes(["600000"]) == {}
    finally:
        await provider.close()
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_provider_health_check():
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"diff": [_row()]}})

    def bad(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("blocked", request=request)

    provider = _provider_with(ok)
    try:
        assert await provider.health_check() is True
    finally:
        await provider.close()

    provider = _provider_with(bad, max_retries=1)
    try:
        assert await provider.health_check() is False
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_provider_get_history_daily_request_and_filter():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url.copy_with(query=None))
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "data": {
                    "name": "浦发银行",
                    "klines": [
                        "2026-08-03,9.50,9.63,9.70,9.45,100,1000.0,0,0,0,0",
                        "2026-08-04,9.63,9.55,9.68,9.50,100,1000.0,0,0,0,0",
                    ],
                }
            },
        )

    provider = _provider_with(handler)
    try:
        bars = await provider.get_history(
            "600000", "daily", datetime(2026, 8, 4), datetime(2026, 8, 4, 23, 59)
        )
    finally:
        await provider.close()

    assert [bar.market_time for bar in bars] == [datetime(2026, 8, 4)]
    assert captured["url"].endswith("/api/qt/stock/kline/get")
    assert captured["params"]["secid"] == "1.600000"
    assert captured["params"]["klt"] == "101"
    assert captured["params"]["ut"]  # 缺 token 会被边缘节点断连
    assert captured["params"]["beg"] == "20260804"


@pytest.mark.asyncio
async def test_provider_get_history_minute_uses_trends():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url.copy_with(query=None))
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "data": {
                    "name": "浦发银行",
                    "trends": ["2026-09-11 09:30,9.35,9.36,9.37,9.34,3778,3530000.0,9.35"],
                }
            },
        )

    provider = _provider_with(handler)
    try:
        bars = await provider.get_history(
            "600000", "1m", datetime(2026, 9, 11), datetime(2026, 9, 11, 23, 59)
        )
    finally:
        await provider.close()

    assert len(bars) == 1
    assert captured["url"].endswith("/api/qt/stock/trends2/get")
    assert captured["params"]["ndays"] == "5"


@pytest.mark.asyncio
async def test_provider_get_history_unsupported_period():
    provider = EastmoneyProvider()
    try:
        result = await provider.get_history(
            "600000", "2h", datetime(2026, 8, 1), datetime(2026, 8, 5)
        )
    finally:
        await provider.close()
    assert result == []


@pytest.mark.asyncio
async def test_provider_get_history_empty_on_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("blocked", request=request)

    provider = _provider_with(handler, max_retries=1)
    try:
        bars = await provider.get_history(
            "600000", "daily", datetime(2026, 8, 1), datetime(2026, 8, 5)
        )
    finally:
        await provider.close()
    assert bars == []


# ──────── 熔断 ────────


@pytest.mark.asyncio
async def test_provider_circuit_breaker_skips_after_repeated_failures():
    """东财被限流连续失败后进入熔断，不再对每次轮询发起请求。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("blocked", request=request)

    provider = _provider_with(
        handler, max_retries=1, quote_hosts=("push2.eastmoney.com",)
    )
    try:
        for _ in range(3):
            assert await provider.get_quotes(["600000"]) == {}
        assert calls["n"] == 3
        assert provider._is_disabled() is True

        # 熔断期内实时与历史请求都直接跳过，不再打网络
        assert await provider.get_quotes(["600000"]) == {}
        assert (
            await provider.get_history(
                "600000", "daily", datetime(2026, 8, 1), datetime(2026, 8, 5)
            )
            == []
        )
        assert calls["n"] == 3
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_provider_circuit_breaker_resets_after_success():
    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            raise httpx.ConnectError("blocked", request=request)
        return httpx.Response(200, json={"data": {"diff": [_row()]}})

    provider = _provider_with(handler, max_retries=1)
    try:
        assert await provider.get_quotes(["600000"]) == {}
        assert provider._consecutive_failures == 1

        state["fail"] = False
        assert set(await provider.get_quotes(["600000"])) == {"600000"}
        assert provider._consecutive_failures == 0
        assert provider._is_disabled() is False
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_provider_circuit_breaker_expires():
    """熔断到期后自动恢复。"""
    provider = EastmoneyProvider()
    try:
        provider._consecutive_failures = 3
        provider._disabled_until = datetime.now() - timedelta(seconds=1)
        assert provider._is_disabled() is False
        assert provider._consecutive_failures == 0
    finally:
        await provider.close()


# ──────── ProviderManager 同上游去重 ────────


def _bar(day: int = 3) -> QuoteData:
    return _quote(
        volume=100.0,
        amount=1000.0,
        market_time=datetime(2026, 8, day),
    )


class _StubProvider(MarketDataProvider):
    """测试桩：记录调用次数，可配置抛错或返回数据。"""

    def __init__(self, name, upstream="", *, exc=None, data=None):
        self.name = name
        self.upstream = upstream
        self._exc = exc
        self._data = data
        self.calls = 0

    async def get_quote(self, symbol):
        return (await self.get_quotes([symbol])).get(symbol)

    async def get_quotes(self, symbols):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return dict(self._data or {})

    async def get_history(self, symbol, period, start_time, end_time):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return list(self._data or [])

    async def subscribe(self, symbols, callback):
        return None

    async def health_check(self):
        return True


@pytest.mark.asyncio
async def test_manager_skips_same_upstream_after_failure():
    """东财失败后不再打同上游的 akshare，直接交给下一个上游。"""
    eastmoney = _StubProvider("eastmoney", "eastmoney", exc=RuntimeError("blocked"))
    akshare = _StubProvider("akshare", "eastmoney", data=[_bar()])
    baostock = _StubProvider("baostock", "baostock", data=[_bar(5)])
    manager = ProviderManager([eastmoney, akshare, baostock])

    bars = await manager.get_history(
        "600000", "daily", datetime(2026, 8, 1), datetime(2026, 8, 10)
    )

    assert len(bars) == 1
    assert eastmoney.calls == 1
    assert akshare.calls == 0  # 同上游跳过
    assert baostock.calls == 1


@pytest.mark.asyncio
async def test_manager_keeps_other_upstream_after_failure():
    """不同上游不受去重影响。"""
    eastmoney = _StubProvider("eastmoney", "eastmoney", exc=RuntimeError("blocked"))
    tencent = _StubProvider("tencent", "tencent", data={"600000": _quote()})
    manager = ProviderManager([eastmoney, tencent])

    quotes = await manager.get_quotes(["600000"])

    assert set(quotes) == {"600000"}
    assert eastmoney.calls == 1
    assert tencent.calls == 1


@pytest.mark.asyncio
async def test_manager_skips_same_upstream_for_incomplete_quotes():
    """返回不完整结果同样视为该上游本次失败。"""
    eastmoney = _StubProvider("eastmoney", "eastmoney", data={"600000": _quote()})
    akshare = _StubProvider("akshare", "eastmoney", data={"000001": _quote(symbol="000001")})
    tencent = _StubProvider(
        "tencent",
        "tencent",
        data={"600000": _quote(), "000001": _quote(symbol="000001")},
    )
    manager = ProviderManager([eastmoney, akshare, tencent])

    quotes = await manager.get_quotes(["600000", "000001"])

    assert set(quotes) == {"600000", "000001"}
    assert akshare.calls == 0
    assert tencent.calls == 1


@pytest.mark.asyncio
async def test_manager_falls_back_to_cache_when_all_fail():
    """全部上游失败时返回缓存数据并标记 is_stale。"""
    provider = _StubProvider("tencent", "tencent", data={"600000": _quote()})
    manager = ProviderManager([provider])
    assert set(await manager.get_quotes(["600000"])) == {"600000"}

    provider._exc = RuntimeError("down")
    stale = await manager.get_quotes(["600000"])
    assert stale["600000"].is_stale is True


# ──────── 主机故障转移 ────────


def test_host_pool_orders_preferred_host_first():
    pool = EastmoneyHostPool(("a.eastmoney.com", "b.eastmoney.com"))
    assert pool.ordered() == ["a.eastmoney.com", "b.eastmoney.com"]
    pool.mark_up("b.eastmoney.com")
    assert pool.ordered() == ["b.eastmoney.com", "a.eastmoney.com"]


def test_host_pool_skips_cooling_host():
    pool = EastmoneyHostPool(("a.eastmoney.com", "b.eastmoney.com"), cooldown_seconds=60)
    pool.mark_down("a.eastmoney.com")
    assert pool.ordered() == ["b.eastmoney.com"]


def test_host_pool_still_tries_when_every_host_is_cooling():
    """全部主机都在冷却期时不能返回空列表，否则整条链路永久失效。"""
    pool = EastmoneyHostPool(("a.eastmoney.com", "b.eastmoney.com"))
    pool.mark_down("a.eastmoney.com")
    pool.mark_down("b.eastmoney.com")
    assert set(pool.ordered()) == {"a.eastmoney.com", "b.eastmoney.com"}


def test_host_pool_rejects_empty_hosts():
    with pytest.raises(ValueError):
        EastmoneyHostPool(())


def test_kline_available_detects_delay_node_without_history():
    """延迟节点 rc=0 但 dktotal=0 / klines 为空 → 判定该主机给不了历史数据。"""
    assert kline_available({"data": {"klines": ["2026-09-10,1,2,3,0.5,1,1,0,0,0,0"]}})
    assert kline_available({"data": {"dktotal": 120, "klines": []}}) is True
    assert kline_available({"data": {"dktotal": 0, "klines": []}}) is False
    assert kline_available({"data": None}) is False


@pytest.mark.asyncio
async def test_provider_fails_over_to_backup_host_for_quotes():
    """首选主机被限流（连接重置）后自动切到备用主机并返回行情。"""
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "push2.eastmoney.com":
            raise httpx.ConnectError("blocked", request=request)
        secids = request.url.params["secids"]
        rows = [_row(f12=secid.split(".")[1]) for secid in secids.split(",")]
        return httpx.Response(200, json={"data": {"diff": rows}})

    provider = _provider_with(handler)
    try:
        assert set(await provider.get_quotes(["600000"])) == {"600000"}
        assert hosts == ["push2.eastmoney.com", "push2delay.eastmoney.com"]

        # 备用主机成功后成为首选，不再重复撞不可达的主机
        hosts.clear()
        assert set(await provider.get_quotes(["000001"])) == {"000001"}
        assert hosts == ["push2delay.eastmoney.com"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_provider_history_skips_host_without_kline_data():
    """首选主机没有历史库（延迟节点）时换下一台主机取 K 线。"""
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "push2his.eastmoney.com":
            return httpx.Response(
                200,
                json={
                    "rc": 0,
                    "data": {"code": "600000", "dktotal": 0, "klines": []},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "name": "浦发银行",
                    "klines": ["2026-08-04,9.63,9.55,9.68,9.50,100,1000.0,0,0,0,0"],
                }
            },
        )

    provider = _provider_with(handler)
    try:
        bars = await provider.get_history(
            "600000", "daily", datetime(2026, 8, 4), datetime(2026, 8, 4, 23, 59)
        )
    finally:
        await provider.close()

    assert [bar.market_time for bar in bars] == [datetime(2026, 8, 4)]
    assert hosts == ["push2his.eastmoney.com", "push2delay.eastmoney.com"]


@pytest.mark.asyncio
async def test_provider_reports_failure_when_all_hosts_down():
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        raise httpx.ConnectError("blocked", request=request)

    provider = _provider_with(handler)
    try:
        assert await provider.get_quotes(["600000"]) == {}
    finally:
        await provider.close()

    assert hosts == ["push2.eastmoney.com", "push2delay.eastmoney.com"]
    assert provider._consecutive_failures == 1
