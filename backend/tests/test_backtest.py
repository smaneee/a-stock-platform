"""回测引擎与成交模拟测试。"""
from datetime import datetime, timedelta

from app.backtest.engine import BacktestEngine
from app.backtest.execution import ExecutionConfig, ExecutionSimulator
from app.strategies.base import Signal, Strategy
from app.strategies.ma_cross import MaCrossStrategy

from tests.helpers import make_history, make_quote


def test_backtest_runs():
    """回测能正常运行并产生结果。"""
    strategy = MaCrossStrategy()
    engine = BacktestEngine(strategy=strategy, initial_cash=100_000.0)
    history = make_history(n=120, base_price=10.0)
    result = engine.run(history)
    assert len(result.equity_curve) == len(history)
    assert result.equity_curve[0] == 100_000.0
    # 指标应正常计算
    assert result.total_return == result.total_return  # 非 NaN
    assert result.max_drawdown >= 0


def test_backtest_empty_history():
    strategy = MaCrossStrategy()
    engine = BacktestEngine(strategy=strategy)
    result = engine.run([])
    assert result.trade_count == 0
    assert result.equity_curve == []


def test_execution_halted():
    """停牌（零成交量）无法成交。"""
    sim = ExecutionSimulator()
    bar = make_quote(symbol="600000", volume=0.0, open=10.0)
    result = sim.try_fill("BUY", 100, bar)
    assert result.filled is False
    assert "停牌" in result.reason


def test_execution_limit_up():
    """一字涨停无法买入。"""
    sim = ExecutionSimulator()
    # 昨收 10，涨停价 11，开盘即涨停
    bar = make_quote(symbol="600000", open=11.0, previous_close=10.0, volume=1_000_000.0)
    result = sim.try_fill("BUY", 100, bar)
    assert result.filled is False
    assert "涨停" in result.reason


def test_execution_limit_down():
    """一字跌停无法卖出。"""
    sim = ExecutionSimulator()
    bar = make_quote(symbol="600000", open=9.0, previous_close=10.0, volume=1_000_000.0)
    result = sim.try_fill("SELL", 100, bar)
    assert result.filled is False
    assert "跌停" in result.reason


def test_execution_board_specific_limit():
    """创业板(20%)在 +15% 开盘时不应被 10% 规则误拒。"""
    sim = ExecutionSimulator()
    # 300750 创业板，昨收 10，+15% 开盘价 11.5，仍在 20% 涨停内
    bar = make_quote(
        symbol="300750", name="宁德时代", open=11.5, previous_close=10.0, volume=1_000_000.0
    )
    result = sim.try_fill("BUY", 100, bar)
    assert result.filled is True

    # 主板 600000 在 +15% 开盘则应被 10% 涨停拒绝
    bar_main = make_quote(
        symbol="600000", name="浦发银行", open=11.5, previous_close=10.0, volume=1_000_000.0
    )
    result_main = sim.try_fill("BUY", 100, bar_main)
    assert result_main.filled is False
    assert "涨停" in result_main.reason


def test_execution_st_limit():
    """ST 股票 5% 涨停限制。"""
    sim = ExecutionSimulator()
    # ST 股票昨收 10，+6% 开盘 10.6 应被 5% 涨停拒绝
    bar = make_quote(
        symbol="600000", name="ST测试", open=10.6, previous_close=10.0, volume=1_000_000.0
    )
    result = sim.try_fill("BUY", 100, bar)
    assert result.filled is False
    assert "涨停" in result.reason


def test_execution_lot_size():
    """非 100 股整数手拒绝成交。"""
    sim = ExecutionSimulator()
    bar = make_quote(symbol="600000", open=10.0, volume=1_000_000.0)
    result = sim.try_fill("BUY", 150, bar)
    assert result.filled is False
    assert "整数手" in result.reason


def test_execution_normal_fill():
    """正常成交（含手续费与滑点）。"""
    sim = ExecutionSimulator(ExecutionConfig(slippage=0.0))
    bar = make_quote(symbol="600000", open=10.0, volume=1_000_000.0)
    result = sim.try_fill("BUY", 100, bar)
    assert result.filled is True
    assert result.price == 10.0
    assert result.commission > 0  # 最低佣金 5 元


def test_execution_sell_stamp_tax():
    """卖出需缴纳印花税。"""
    sim = ExecutionSimulator(ExecutionConfig(slippage=0.0))
    bar = make_quote(symbol="600000", open=10.0, volume=1_000_000.0)
    result = sim.try_fill("SELL", 100, bar)
    assert result.filled is True
    assert result.stamp_tax > 0


class _BuyThenSellStrategy(Strategy):
    name = "buy_then_sell"

    def analyze(self, history):
        latest = history[-1]
        if len(history) == 1:
            direction = "BUY"
        elif len(history) == 2:
            direction = "SELL"
        else:
            return None
        return Signal(
            symbol=latest.symbol,
            strategy_name=self.name,
            direction=direction,
            reason="测试 T+1",
            price=latest.price,
            source_time=latest.market_time,
        )


def test_intraday_next_bar_does_not_unlock_t1_position():
    """分钟回测中，下一分钟不等于下一个交易日。"""
    start = datetime(2026, 9, 9, 9, 30)
    history = [
        make_quote(market_time=start + timedelta(minutes=index), price=10 + index * 0.01)
        for index in range(3)
    ]
    result = BacktestEngine(_BuyThenSellStrategy()).run(history)
    assert result.trade_count == 1
    assert result.trades[0]["side"] == "BUY"
