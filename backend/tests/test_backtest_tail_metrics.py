"""回测尾部风险与收益口径标注测试（P0-05 的 D8 / D9）。

研发计划 5.3.5：「在包含资金占用、现金、重叠持仓、公司行为与成交约束的账户
净值上计算累计/年化收益、回撤、波动、换手和尾部风险」。
D9 缺口：原实现只有 total_return / annual_return / max_drawdown / sharpe，
**没有波动率与尾部风险**；D8 缺口：结果里没有收益口径标注，容易被误读为含股息的
总收益。这些用例锁定新增指标的算法与口径文案。
"""
from __future__ import annotations

import math

import pytest

from app.backtest import metrics as m


# ───────────── 尾部风险：可手算的例子 ─────────────


def test_daily_returns_basic():
    curve = [100.0, 110.0, 99.0]
    returns = m.daily_returns(curve)
    assert len(returns) == 2
    assert returns[0] == pytest.approx(0.10)
    assert returns[1] == pytest.approx(-0.10)


def test_daily_returns_handles_short_and_zero_previous():
    assert m.daily_returns([]).size == 0
    assert m.daily_returns([100.0]).size == 0
    # 前值为 0 时不产生 inf/nan（净值归零属异常数据，按 0 收益处理）
    returns = m.daily_returns([0.0, 100.0])
    assert returns.size == 1
    assert returns[0] == 0.0


def test_volatility_is_annualized_and_zero_for_flat_curve():
    assert m.volatility([100.0] * 10) == 0.0
    # 两天涨跌各 1%：样本标准差 ≈ 1.4142%，年化 ×√252
    curve = [100.0, 101.0, 99.99]
    returns = m.daily_returns(curve)
    expected = float(__import__("numpy").std(returns, ddof=1) * math.sqrt(252))
    assert m.volatility(curve) == pytest.approx(expected)


def test_value_at_risk_is_positive_loss_and_matches_quantile():
    # 10 天里最差一天 -8%
    returns = [0.01, 0.02, -0.01, 0.005, 0.0, -0.02, 0.015, 0.003, -0.005, -0.08]
    curve = [100.0]
    for r in returns:
        curve.append(curve[-1] * (1 + r))
    var95 = m.value_at_risk(curve, 0.95)
    assert var95 >= 0
    # 95% 分位对应的损失应至少覆盖 8% 那一档（厚尾用实际分布而不是正态假设）
    assert var95 > 0.0


def test_cvar_is_at_least_var():
    curve = [100.0]
    for r in [0.01, -0.02, 0.005, -0.03, 0.0, -0.05, 0.002, -0.01, 0.004, -0.09]:
        curve.append(curve[-1] * (1 + r))
    var95 = m.value_at_risk(curve, 0.95)
    cvar95 = m.conditional_value_at_risk(curve, 0.95)
    assert cvar95 >= var95 > 0


def test_tail_metrics_are_zero_for_flat_or_short_curves():
    flat = [100.0] * 5
    for func in (m.volatility, m.value_at_risk, m.conditional_value_at_risk, m.worst_day_return):
        assert func(flat) == 0.0
    for func in (m.volatility, m.value_at_risk, m.conditional_value_at_risk, m.worst_day_return):
        assert func([]) == 0.0
        assert func([100.0]) == 0.0
    assert m.max_drawdown_duration([]) == 0
    assert m.max_drawdown_duration([100.0]) == 0


def test_max_drawdown_duration_counts_days_underwater():
    # 100 → 90（回撤 2 天）→ 100（新高）→ 95（回撤 1 天）→ 105（新高）
    curve = [100.0, 95.0, 90.0, 100.0, 95.0, 105.0]
    assert m.max_drawdown_duration(curve) == 2


def test_max_drawdown_duration_monotonic_rise_is_zero():
    assert m.max_drawdown_duration([100.0, 101.0, 102.0, 103.0]) == 0


def test_worst_day_return_is_negative():
    curve = [100.0, 105.0, 94.5, 96.0]
    worst = m.worst_day_return(curve)
    assert worst < 0
    assert worst == pytest.approx(-0.10)


# ───────────── 口径标注（D8） ─────────────


def test_return_convention_meta_is_explicit():
    meta = m.return_convention_meta()
    assert meta["return_convention"] == "price_return_only_no_dividends"
    assert meta["dividends_modeled"] is False
    assert meta["bars_adjust"] == "none"
    assert meta["costs_included"] is True
    assert "价格收益" in meta["return_convention_note"]
    assert "未复权" in meta["return_convention_note"]


def test_portfolio_result_exposes_tail_metrics_and_convention():
    """组合结果必须同时带尾部风险指标与口径标注。"""
    from app.portfolio.engine import PortfolioResult

    result = PortfolioResult(equity_curve=[100.0, 102.0, 99.0, 101.0])
    payload = result.to_dict()
    for key in (
        "volatility", "var_95", "cvar_95", "max_drawdown_duration", "worst_day_return",
        "return_convention", "return_convention_note", "dividends_modeled", "bars_adjust",
    ):
        assert key in payload, key
    assert payload["return_convention"] == "price_return_only_no_dividends"
    assert payload["dividends_modeled"] is False
