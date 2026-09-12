"""信号引擎单元测试：策略执行、冷却去重、落库与广播。"""
from __future__ import annotations

from datetime import timedelta

from app.database.models import Signal as SignalModel
from app.database.session import SessionLocal
from app.realtime import signal_engine as mod
from app.realtime.quote_cache import QuoteCache
from app.realtime.signal_engine import SignalEngine
from app.strategies.base import Signal
from app.time_utils import utc_now

from tests.helpers import make_quote


class _Strategy:
    name = "fake"
    version = "1.0.0"

    def __init__(self, signal=None, *, factory=None, error: Exception | None = None):
        self._factory = factory or (lambda: signal)
        self._error = error
        self.calls = 0

    def analyze(self, history):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._factory()


class _WS:
    def __init__(self):
        self.signals: list[dict] = []

    async def broadcast_signal(self, signal):
        self.signals.append(signal)


def _signal(symbol="600000", direction="BUY") -> Signal:
    return Signal(
        symbol=symbol,
        strategy_name="fake",
        direction=direction,
        strength=1.0,
        reason="test",
        price=10.0,
        source_time=utc_now(),
    )


def _cache(minutes: int = 2) -> QuoteCache:
    """构造窗口内至少 2 根 K 线的缓存。"""
    cache = QuoteCache(window_size=10)
    start = utc_now() - timedelta(minutes=minutes)
    for i in range(minutes):
        cache.update(make_quote(market_time=start + timedelta(minutes=i), price=10.0 + i))
    return cache


def _engine(cache, strategies, *, ws=None, cooldown=60.0):
    ws = ws or _WS()
    return SignalEngine(cache, ws, lambda: strategies, cooldown), ws


async def test_no_strategies_returns_empty():
    engine, ws = _engine(_cache(), [])
    assert await engine.process_quotes({"600000": make_quote()}) == []
    assert ws.signals == []


async def test_window_shorter_than_two_is_skipped():
    cache = QuoteCache(window_size=10)
    cache.update(make_quote())
    strategy = _Strategy(_signal())
    engine, _ = _engine(cache, [strategy])
    assert await engine.process_quotes({"600000": make_quote()}) == []
    assert strategy.calls == 0


async def test_strategy_exception_is_isolated():
    bad = _Strategy(error=RuntimeError("boom"))
    good = _Strategy(_signal())
    engine, ws = _engine(_cache(), [bad, good])
    result = await engine.process_quotes({"600000": make_quote()})
    assert len(result) == 1
    assert len(ws.signals) == 1


async def test_none_signal_is_skipped():
    engine, ws = _engine(_cache(), [_Strategy(None)])
    assert await engine.process_quotes({"600000": make_quote()}) == []
    assert ws.signals == []


async def test_signal_is_broadcast_and_persisted():
    signal = _signal()
    engine, ws = _engine(_cache(), [_Strategy(signal)])
    result = await engine.process_quotes({"600000": make_quote()})
    assert [s.signal_id for s in result] == [signal.signal_id]
    assert ws.signals[0]["signal_id"] == signal.signal_id
    assert ws.signals[0]["direction"] == "BUY"
    with SessionLocal() as db:
        row = db.query(SignalModel).filter_by(signal_id=signal.signal_id).one()
        assert row.symbol == "600000"
        assert row.direction == "BUY"


async def test_cooldown_blocks_duplicate():
    engine, ws = _engine(_cache(), [_Strategy(factory=_signal)], cooldown=60.0)
    first = await engine.process_quotes({"600000": make_quote()})
    second = await engine.process_quotes({"600000": make_quote()})
    assert len(first) == 1
    assert second == []
    assert len(ws.signals) == 1


async def test_cooldown_boundary(monkeypatch):
    engine, _ = _engine(_cache(), [_Strategy(factory=_signal)], cooldown=60.0)
    clock = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["t"])
    assert len(await engine.process_quotes({"600000": make_quote()})) == 1
    clock["t"] = 1059.0
    assert await engine.process_quotes({"600000": make_quote()}) == []
    clock["t"] = 1060.0
    assert len(await engine.process_quotes({"600000": make_quote()})) == 1


async def test_distinct_symbols_have_separate_cooldown():
    strategies = [_Strategy(factory=lambda: _signal(symbol="600000"))]
    engine, ws = _engine(_cache(), strategies, cooldown=60.0)
    assert len(await engine.process_quotes({"600000": make_quote()})) == 1
    assert await engine.process_quotes({"600000": make_quote()}) == []
    assert len(ws.signals) == 1


async def test_save_failure_is_swallowed(monkeypatch):
    engine, ws = _engine(_cache(), [_Strategy(factory=_signal)])

    class _BadSession:
        def __init__(self):
            self.rolled = False
            self.closed = False

        def add(self, obj):
            self.obj = obj

        def commit(self):
            raise RuntimeError("db down")

        def rollback(self):
            self.rolled = True

        def close(self):
            self.closed = True

    bad = _BadSession()
    import app.database.session as session_mod

    monkeypatch.setattr(session_mod, "SessionLocal", lambda: bad)
    result = await engine.process_quotes({"600000": make_quote()})
    assert len(result) == 1
    assert len(ws.signals) == 1
    assert bad.rolled is True
    assert bad.closed is True
