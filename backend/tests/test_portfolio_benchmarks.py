"""基准分列测试（P0-05 的 D7）。

研发计划 §5.3.5 / §11.7：「同池等权只衡量池内排序能力；另列预先选定市场指数与
含现金基准」。D7 缺口：原实现只有「有没有市场指数」两种状态，没有同池等权与
含现金基准，于是「跑赢基准」既可能指跑赢市场、也可能指跑赢池内平均，口径混淆。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.portfolio import benchmarks as bm


def _axis(n: int) -> list[date]:
    start = date(2026, 1, 5)
    return [start + timedelta(days=i) for i in range(n)]


# ───────────── 同池等权 ─────────────


def test_equal_weight_is_average_of_normalized_closes():
    days = _axis(3)
    series = {
        "A": {days[0]: 10.0, days[1]: 11.0, days[2]: 12.0},   # +20%
        "B": {days[0]: 20.0, days[1]: 19.0, days[2]: 20.0},   # 0%
    }
    curve = bm.equal_weight_buy_and_hold(series, days, 100_000.0)
    assert len(curve) == 3
    assert curve[0] == pytest.approx(100_000.0)                    # 首日归一化
    assert curve[1] == pytest.approx(100_000.0 * (1.10 + 0.95) / 2)
    assert curve[2] == pytest.approx(100_000.0 * (1.20 + 1.00) / 2)


def test_equal_weight_skips_symbols_without_quote_that_day():
    """停牌/未上市的标的当日不参与，避免用未来价格回填。"""
    days = _axis(3)
    series = {
        "A": {days[0]: 10.0, days[1]: 11.0, days[2]: 12.0},
        "B": {days[2]: 30.0},  # 第 3 天才开始有行情
    }
    curve = bm.equal_weight_buy_and_hold(series, days, 1000.0)
    assert curve[0] == pytest.approx(1000.0)
    assert curve[1] == pytest.approx(1000.0 * 1.10)   # 只有 A
    # 第 3 天：A 归一化 1.2，B 以其首日 30 归一化 1.0 → 平均 1.1
    assert curve[2] == pytest.approx(1000.0 * (1.2 + 1.0) / 2)


def test_equal_weight_handles_all_suspended_day_without_break():
    days = _axis(3)
    series = {"A": {days[0]: 10.0, days[2]: 12.0}}  # 第 2 天停牌
    curve = bm.equal_weight_buy_and_hold(series, days, 1000.0)
    assert len(curve) == 3
    assert curve[1] == curve[0]  # 沿用上一点而不是断点/归零


def test_equal_weight_empty_inputs():
    assert bm.equal_weight_buy_and_hold({}, _axis(2), 1000.0) == []
    assert bm.equal_weight_buy_and_hold({"A": {}}, [], 1000.0) == []
    assert bm.equal_weight_buy_and_hold({"A": {_axis(1)[0]: 10.0}}, _axis(1), 0.0) == []


def test_equal_weight_ignores_non_positive_prices():
    days = _axis(2)
    series = {"A": {days[0]: 0.0, days[1]: 10.0}}  # 首日 0 价不得当归一化基准
    curve = bm.equal_weight_buy_and_hold(series, days, 1000.0)
    # 第一天没有有效基准 → 不参与；第二天成为首日 → 归一化 1.0
    assert curve[0] == pytest.approx(1000.0 * 1.0) or curve[0] == pytest.approx(1000.0)
    assert curve[-1] == pytest.approx(1000.0)


# ───────────── 含现金基准与口径标注 ─────────────


def test_cash_curve_is_flat():
    days = _axis(4)
    curve = bm.cash_curve(days, 250_000.0)
    assert curve == [250_000.0] * 4
    assert bm.return_of(curve, 250_000.0) == 0.0
    assert bm.cash_curve([], 1000.0) == []


def test_return_of_matches_curve_endpoints():
    assert bm.return_of([100.0, 130.0], 100.0) == pytest.approx(0.30)
    assert bm.return_of([100.0, 80.0], 100.0) == pytest.approx(-0.20)
    assert bm.return_of([], 100.0) == 0.0
    assert bm.return_of([100.0], 0.0) == 0.0


def test_labels_make_benchmark_semantics_explicit():
    without = bm.benchmark_labels(False)
    assert without["has_market_index"] is False
    assert "不能表述为「跑赢市场」" in without["market_index"]
    assert "排序能力" in without["equal_weight"]
    assert "收益恒为 0" in without["cash"]

    with_index = bm.benchmark_labels(True)
    assert with_index["has_market_index"] is True
    assert "市场指数" in with_index["market_index"]


def test_portfolio_result_exposes_split_benchmarks():
    from app.portfolio.engine import PortfolioResult

    result = PortfolioResult(
        equity_curve=[100.0, 110.0],
        equal_weight_curve=[100.0, 105.0],
        equal_weight_return=0.05,
        cash_return=0.0,
        excess_vs_equal_weight=0.05,
        excess_vs_cash=0.10,
        benchmark_labels=bm.benchmark_labels(False),
    )
    payload = result.to_dict()
    for key in (
        "equal_weight_curve", "cash_curve", "equal_weight_return", "cash_return",
        "excess_vs_equal_weight", "excess_vs_cash", "benchmark_labels",
    ):
        assert key in payload, key
    assert payload["excess_vs_equal_weight"] == pytest.approx(0.05)
    assert payload["benchmark_labels"]["has_market_index"] is False
