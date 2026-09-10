"""组合回测与可信评估测试。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.market_data.base import QuoteData
from app.portfolio import metrics as port
from app.portfolio.config import PortfolioConfig
from app.portfolio.engine import PortfolioBacktestEngine, PortfolioResult
from app.portfolio.sensitivity import run_sensitivity
from app.portfolio.validation import split_in_out_sample, walk_forward_splits
from app.strategies.base import Signal, Strategy
from tests.helpers import make_quote


def make_daily_bars(
    symbol: str,
    n: int,
    base_price: float = 10.0,
    start: date = date(2026, 1, 5),
    step: float = 0.05,
) -> list[QuoteData]:
    """生成逐日递增的 K 线（每个交易日一根）。"""
    bars = []
    for i in range(n):
        d = start + timedelta(days=i)
        price = base_price + i * step
        bars.append(
            make_quote(
                symbol=symbol,
                price=price,
                open=price - 0.02,
                high=price + 0.1,
                low=price - 0.1,
                previous_close=price - 0.05,
                volume=1_000_000.0,
                market_time=datetime(d.year, d.month, d.day, 9, 30),
            )
        )
    return bars


class ScheduleStrategy(Strategy):
    """按已见 K 线数量触发信号的确定性策略。"""

    name = "schedule"

    def __init__(self, buy_at: int = 5, sell_at: int = 12):
        self.buy_at = buy_at
        self.sell_at = sell_at

    def analyze(self, history: list[QuoteData]) -> Signal | None:
        if not history:
            return None
        latest = history[-1]
        n = len(history)
        if n == self.buy_at:
            return self._build_signal(
                symbol=latest.symbol, direction="BUY", reason="触发买入",
                price=latest.price, source_time=latest.market_time or latest.received_at,
            )
        if n == self.sell_at:
            return self._build_signal(
                symbol=latest.symbol, direction="SELL", reason="触发卖出",
                price=latest.price, source_time=latest.market_time or latest.received_at,
            )
        return None


# ──────── 指标 ────────


def test_turnover():
    assert port.turnover([1000.0], [100000.0]) == pytest.approx(0.01)
    assert port.turnover([], []) == 0.0
    assert port.turnover([1000.0], []) == 0.0


def test_concentration_equal_weight():
    # 两标的各 50% -> HHI = 0.25 + 0.25 = 0.5
    assert port.concentration([[0.5, 0.5]]) == pytest.approx(0.5)
    # 单一持仓 -> HHI = 1.0
    assert port.concentration([[1.0, 0.0]]) == pytest.approx(1.0)


def test_concentration_empty():
    assert port.concentration([]) == 0.0
    assert port.concentration([[]]) == 0.0


def test_excess_return_and_beta():
    assert port.excess_return(0.3, 0.1) == pytest.approx(0.2)
    pr = port.daily_returns([100, 110, 121, 133.1])
    rb = port.daily_returns([100, 105, 110.25, 115.76])
    # 组合日收益恒为 10%，基准恒为 5%，beta = cov/var，方差非零
    b = port.beta(pr, rb)
    assert b > 0


def test_tracking_error_and_information_ratio():
    pr = port.daily_returns([100, 110, 121, 133.1])
    rb = port.daily_returns([100, 105, 110.25, 115.7625])
    # 恒定 10% 与恒定 5%，跟踪误差应为 0
    assert port.tracking_error(pr, rb) == pytest.approx(0.0, abs=1e-9)


# ──────── 权重归一化 ────────


def test_weights_equal_by_default():
    engine = PortfolioBacktestEngine(strategies={"a": ScheduleStrategy(), "b": ScheduleStrategy()})
    assert engine.weights == {"a": 0.5, "b": 0.5}


def test_weights_normalized_and_filtered():
    engine = PortfolioBacktestEngine(
        strategies={"a": ScheduleStrategy(), "b": ScheduleStrategy()},
        weights={"a": 2.0, "b": 2.0, "c": 100.0},
    )
    assert engine.weights == {"a": 0.5, "b": 0.5}


# ──────── 组合回测 ────────


def test_portfolio_buy_sell_flow():
    hist_a = make_daily_bars("600000", n=30)
    hist_b = make_daily_bars("000001", n=30, base_price=20.0)
    engine = PortfolioBacktestEngine(
        strategies={"600000": ScheduleStrategy(buy_at=5, sell_at=12), "000001": ScheduleStrategy(buy_at=5, sell_at=12)},
        config=PortfolioConfig(initial_cash=1_000_000.0),
    )
    result = engine.run({"600000": hist_a, "000001": hist_b})

    assert result.trade_count == 4  # 每标的各 1 买 1 卖
    assert len(result.equity_curve) == 30
    assert result.total_return != 0.0
    assert result.turnover >= 0.0
    assert 0.0 < result.concentration <= 1.0


def test_portfolio_no_future_data():
    """信号在 T 日收盘产生，应在 T+1 开盘价成交（含滑点）。"""
    hist = make_daily_bars("600000", n=20)
    engine = PortfolioBacktestEngine(
        strategies={"600000": ScheduleStrategy(buy_at=5, sell_at=12)},
        config=PortfolioConfig(initial_cash=1_000_000.0),
    )
    result = engine.run({"600000": hist})
    buys = [t for t in result.trades if t["side"] == "BUY"]
    assert buys
    # buy_at=5 -> 第 5 根 K 线（index 4）收盘产生信号，第 6 根（index 5）开盘成交
    next_open = hist[5].open
    slip = engine.config.execution.slippage
    assert buys[0]["price"] == pytest.approx(next_open * (1 + slip))
    assert buys[0]["time"].startswith(hist[5].market_time.isoformat()[:10])


def test_portfolio_position_limits():
    """单标的仓位不得超过 max_single_position。"""
    hist_a = make_daily_bars("600000", n=30, base_price=10.0)
    hist_b = make_daily_bars("000001", n=30, base_price=10.0)
    engine = PortfolioBacktestEngine(
        strategies={"600000": ScheduleStrategy(buy_at=5, sell_at=100), "000001": ScheduleStrategy(buy_at=5, sell_at=100)},
        config=PortfolioConfig(initial_cash=1_000_000.0, max_single_position=0.15),
    )
    result = engine.run({"600000": hist_a, "000001": hist_b})
    # 单标的买入金额不得超过 15% 初始资金
    for t in result.trades:
        if t["side"] == "BUY":
            notional = t["price"] * t["quantity"]
            assert notional <= 1_000_000.0 * 0.15 + 1.0


def test_portfolio_benchmark_curve():
    hist = make_daily_bars("600000", n=20)
    bench = make_daily_bars("000300", n=20, base_price=100.0, step=1.0)
    engine = PortfolioBacktestEngine(
        strategies={"600000": ScheduleStrategy(buy_at=5, sell_at=12)},
        benchmark=bench,
        config=PortfolioConfig(initial_cash=100_000.0),
    )
    result = engine.run({"600000": hist})
    assert len(result.benchmark_curve) == len(result.equity_curve)
    # 基准归一化起点等于初始资金
    assert result.benchmark_curve[0] == pytest.approx(100_000.0)
    # 基准单调上涨（step=1 递增），最终大于初始
    assert result.benchmark_curve[-1] > result.benchmark_curve[0]
    assert result.benchmark_return > 0


def test_portfolio_empty_histories():
    engine = PortfolioBacktestEngine(strategies={"600000": ScheduleStrategy()})
    result = engine.run({})
    assert isinstance(result, PortfolioResult)
    assert result.equity_curve == []


# ──────── 样本内/外与滚动前推 ────────


def test_split_in_out_sample():
    hist = make_daily_bars("600000", n=10)
    in_s, out_s = split_in_out_sample({"600000": hist}, ratio=0.7)
    assert len(in_s["600000"]) == 7
    assert len(out_s["600000"]) == 3
    # 样本外日期都晚于样本内
    assert min(b.market_time for b in out_s["600000"]) > max(b.market_time for b in in_s["600000"])


def test_split_in_out_sample_empty():
    in_s, out_s = split_in_out_sample({})
    assert in_s == {} and out_s == {}


def test_walk_forward_splits():
    hist = make_daily_bars("600000", n=30)
    folds = walk_forward_splits({"600000": hist}, n_folds=3)
    assert len(folds) == 2
    for train, out in folds:
        assert train["600000"]
        assert out["600000"]


# ──────── 敏感性 ────────


def test_sensitivity_grid():
    hist = make_daily_bars("600000", n=30)
    engine = PortfolioBacktestEngine(
        strategies={"600000": ScheduleStrategy(buy_at=5, sell_at=12)},
        config=PortfolioConfig(initial_cash=1_000_000.0),
    )
    results = run_sensitivity(
        engine, {"600000": hist},
        commission_rates=[0.0003, 0.001],
        slippages=[0.0005, 0.001],
    )
    # 2 佣金档 + 2 滑点档
    assert len(results) == 4
    for r in results:
        assert "commission_rate" in r
        assert "slippage" in r
        assert "total_return" in r
