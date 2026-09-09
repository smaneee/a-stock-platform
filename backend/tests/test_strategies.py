"""策略触发测试。"""
from datetime import datetime, timedelta

from app.strategies.breakout import BreakoutStrategy
from app.strategies.ma_cross import MaCrossStrategy
from app.strategies.macd_cross import MacdCrossStrategy
from app.strategies.rsi_reversal import RsiReversalStrategy

from tests.helpers import make_history, make_quote


def test_ma_cross_insufficient_data():
    strategy = MaCrossStrategy()
    history = make_history(n=20)
    assert strategy.analyze(history) is None


def test_ma_cross_golden_cross():
    """前 21 根持平，最后一根跳涨，触发 MA5 上穿 MA20 金叉。"""
    strategy = MaCrossStrategy()
    start = datetime.utcnow() - timedelta(days=22)
    history = []
    for i in range(21):
        history.append(make_quote(symbol="600000", price=10.0, market_time=start + timedelta(minutes=i)))
    # 最后一根跳涨
    history.append(
        make_quote(
            symbol="600000",
            price=11.0,
            open=10.0,
            high=11.2,
            low=10.0,
            previous_close=10.0,
            market_time=start + timedelta(minutes=21),
        )
    )
    signal = strategy.analyze(history)
    assert signal is not None
    assert signal.direction == "BUY"
    assert signal.strategy_name == "ma_cross"
    assert signal.reason  # 必须有解释原因
    assert signal.price > 0


def test_breakout_insufficient_data():
    strategy = BreakoutStrategy()
    history = make_history(n=20)
    assert strategy.analyze(history) is None


def test_breakout_signal():
    """放量突破前 20 根高点。"""
    strategy = BreakoutStrategy(lookback=20, volume_ratio=1.5)
    start = datetime.utcnow() - timedelta(days=22)
    history = []
    for i in range(21):
        history.append(
            make_quote(
                symbol="600000",
                price=10.0,
                high=10.0,
                low=9.8,
                volume=1_000_000.0,
                market_time=start + timedelta(minutes=i),
            )
        )
    # 最后一根放量突破
    history.append(
        make_quote(
            symbol="600000",
            price=11.5,
            high=11.5,
            low=10.5,
            volume=5_000_000.0,  # 放量
            market_time=start + timedelta(minutes=21),
        )
    )
    signal = strategy.analyze(history)
    assert signal is not None
    assert signal.direction == "BUY"
    assert signal.reason


def test_rsi_insufficient_data():
    strategy = RsiReversalStrategy()
    history = make_history(n=14)
    assert strategy.analyze(history) is None


def test_rsi_returns_none_or_signal():
    """RSI 策略在合理数据下不抛异常。"""
    strategy = RsiReversalStrategy()
    history = make_history(n=40)
    result = strategy.analyze(history)
    assert result is None or result.direction in ("BUY", "SELL")


def test_macd_insufficient_data():
    strategy = MacdCrossStrategy()
    history = make_history(n=30)
    assert strategy.analyze(history) is None


def test_macd_returns_none_or_signal():
    """MACD 策略在合理数据下不抛异常。"""
    strategy = MacdCrossStrategy()
    # 先跌后涨的 V 型数据
    start = datetime.utcnow() - timedelta(days=50)
    history = []
    for i in range(50):
        if i < 25:
            price = 20.0 - i * 0.3
        else:
            price = 12.5 + (i - 25) * 0.3
        history.append(make_quote(symbol="600000", price=price, market_time=start + timedelta(minutes=i)))
    result = strategy.analyze(history)
    assert result is None or result.direction in ("BUY", "SELL")
