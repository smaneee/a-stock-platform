"""行情轮询调度器单元测试：生命周期、行情校验与轮询管道。"""
from __future__ import annotations

from app.realtime.quote_cache import QuoteCache
from app.realtime.quote_scheduler import QuoteScheduler

from tests.helpers import make_quote


class _FakeScheduler:
    def __init__(self):
        self.jobs: list[tuple] = []
        self.started = False
        self.running = False
        self.shutdown_calls: list[bool] = []

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append((func, trigger, kwargs))

    def start(self):
        self.started = True
        self.running = True

    def shutdown(self, wait=True):
        self.shutdown_calls.append(wait)
        self.running = False


class _FakeWS:
    def __init__(self):
        self.quotes = []

    async def broadcast_quote(self, quote):
        self.quotes.append(quote)


class _FakePM:
    def __init__(self, quotes):
        self.quotes = quotes
        self.symbols = None

    async def get_quotes(self, symbols):
        self.symbols = list(symbols)
        return self.quotes


def _scheduler(quotes, symbols, on_quotes=None):
    ws = _FakeWS()
    pm = _FakePM(quotes)
    cache = QuoteCache(window_size=10)
    sched = QuoteScheduler(pm, cache, ws, lambda: list(symbols), on_quotes)
    fake = _FakeScheduler()
    sched._scheduler = fake
    return sched, fake, ws, cache, pm


def test_start_registers_job_and_is_idempotent():
    sched, fake, *_ = _scheduler({}, ["600000"])
    sched.start()
    assert fake.started is True
    assert len(fake.jobs) == 1
    assert sched.is_running is True
    sched.start()
    assert len(fake.jobs) == 1  # 重复启动不重复注册
    func, trigger, kwargs = fake.jobs[0]
    assert func == sched.poll_once
    assert trigger == "interval"
    assert kwargs["id"] == "quote_poll"
    assert kwargs["max_instances"] == 1
    assert kwargs["coalesce"] is True


def test_shutdown_stops_scheduler():
    sched, fake, *_ = _scheduler({}, ["600000"])
    sched.start()
    sched.shutdown()
    assert fake.shutdown_calls == [False]
    assert sched.is_running is False


def test_shutdown_without_start_is_noop():
    sched, fake, *_ = _scheduler({}, ["600000"])
    sched.shutdown()
    assert fake.shutdown_calls == []
    assert sched.is_running is False


async def test_poll_once_without_symbols_returns_empty():
    sched, _, _, _, pm = _scheduler({}, [])
    assert await sched.poll_once() == {}
    assert pm.symbols is None  # 无自选股时不应发起请求


async def test_poll_once_caches_valid_and_broadcasts():
    quote = make_quote(symbol="600000", price=10.5)
    sched, _, ws, cache, pm = _scheduler({"600000": quote}, ["600000"])
    result = await sched.poll_once()
    assert set(result) == {"600000"}
    assert cache.get_latest("600000") is quote
    assert ws.quotes == [quote]
    assert pm.symbols == ["600000"]


async def test_poll_once_skips_none_and_invalid_but_pushes_stale():
    valid = make_quote(symbol="600000")
    invalid = make_quote(symbol="000001", price=0.0)
    stale = make_quote(symbol="600519", is_stale=True)
    quotes = {"600000": valid, "000001": invalid, "600519": stale, "300750": None}
    sched, _, ws, cache, _ = _scheduler(quotes, ["600000", "000001", "600519", "300750"])
    result = await sched.poll_once()
    assert set(result) == {"600000"}
    assert cache.get_latest("000001") is None
    assert cache.get_latest("600519") is None
    assert {q.symbol for q in ws.quotes} == {"600000", "600519"}


async def test_poll_once_invokes_strategy_callback():
    seen: dict = {}

    async def on_quotes(quotes):
        seen.update(quotes)

    quote = make_quote(symbol="600000")
    sched, *_ = _scheduler({"600000": quote}, ["600000"], on_quotes=on_quotes)
    await sched.poll_once()
    assert set(seen) == {"600000"}


async def test_poll_once_swallows_callback_error():
    async def boom(quotes):
        raise RuntimeError("strategy down")

    quote = make_quote(symbol="600000")
    sched, _, _, _, _ = _scheduler({"600000": quote}, ["600000"], on_quotes=boom)
    result = await sched.poll_once()
    assert set(result) == {"600000"}  # 回调失败不影响本次行情返回


def test_validate_rejects_bad_quotes():
    assert QuoteScheduler._validate(make_quote()) is True
    assert QuoteScheduler._validate(make_quote(symbol="")) is False
    assert QuoteScheduler._validate(make_quote(price=0.0)) is False
    assert QuoteScheduler._validate(make_quote(high=1.0, low=2.0)) is False
    assert QuoteScheduler._validate(make_quote(high=0.0, low=0.0)) is True
