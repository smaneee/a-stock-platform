"""估值引擎测试：手算对照 + 边界（算不出来必须报错，不能编数字）。"""
from __future__ import annotations

from app.fundamentals.valuation import (
    ValuationAssumptions,
    ValuationError,
    intrinsic_value,
    margin_of_safety,
    scenario_band,
    sensitivity_table,
    upside_ratio,
    weighted_value,
)

# 手算基准：营收 1000、零增长、自由现金流率 10%、折现率 10%、永续增长 0、3 年预测期、100 股
#   FCF = 100/年；PV = 100/1.1 + 100/1.21 + 100/1.331 = 248.6852
#   永续价值 = 100/0.1 = 1000 → 折现 = 1000/1.331 = 751.3148
#   EV = 248.6852 + 751.3148 = 1000.00（永续 100 @10% 恰为 1000）
#   → 每股 10.00 元
BASE = ValuationAssumptions(
    revenue=1000.0,
    revenue_growth=0.0,
    fcf_margin=0.10,
    discount_rate=0.10,
    terminal_growth=0.0,
    shares=100.0,
    net_debt=0.0,
    years=3,
    basis="单元测试假设（事实：无）",
)


def test_hand_computed_value_is_exactly_ten_per_share():
    out = intrinsic_value(BASE)
    assert out.enterprise_value == 1000.0
    assert out.equity_value == 1000.0
    assert out.per_share == 10.0
    assert out.cash_flows == (100.0, 100.0, 100.0)
    assert out.terminal_value == 1000.0
    # 永续部分占 75.1%（提醒：估值高度依赖永续假设）
    assert abs(out.terminal_value_share - 0.7513) < 0.001


def test_net_debt_reduces_equity_value():
    out = intrinsic_value(ValuationAssumptions(**{**BASE.__dict__, "net_debt": 200.0}))
    assert out.equity_value == 800.0
    assert out.per_share == 8.0
    # 净现金（负净负债）则相反
    rich = intrinsic_value(ValuationAssumptions(**{**BASE.__dict__, "net_debt": -200.0}))
    assert rich.per_share == 12.0


def test_growth_and_margin_move_value_in_the_right_direction():
    faster = intrinsic_value(ValuationAssumptions(**{**BASE.__dict__, "revenue_growth": 0.10}))
    higher_margin = intrinsic_value(ValuationAssumptions(**{**BASE.__dict__, "fcf_margin": 0.20}))
    higher_discount = intrinsic_value(ValuationAssumptions(**{**BASE.__dict__, "discount_rate": 0.15}))
    assert faster.per_share > 10.0
    assert higher_margin.per_share > 10.0
    assert higher_discount.per_share < 10.0


def test_impossible_assumptions_raise_instead_of_returning_a_number():
    cases = {
        "营收为 0": {"revenue": 0.0},
        "股本为 0": {"shares": 0.0},
        "预测期 0 年": {"years": 0},
        "增长率超界": {"revenue_growth": 3.0},
        "折现率为 0": {"discount_rate": 0.0},
        "折现率不高于永续增长": {"discount_rate": 0.05, "terminal_growth": 0.05},
        "自由现金流率为负": {"fcf_margin": -0.01},
    }
    for label, override in cases.items():
        try:
            intrinsic_value(ValuationAssumptions(**{**BASE.__dict__, **override}))
        except ValuationError:
            continue
        raise AssertionError(f"{label} 应当抛 ValuationError，实际算出了数字")


def test_scenario_band_orders_bear_base_bull_and_records_overrides():
    band = scenario_band(
        BASE,
        bear_overrides={"revenue_growth": -0.05, "fcf_margin": 0.05},
        bull_overrides={"revenue_growth": 0.15, "fcf_margin": 0.14},
    )
    assert band.errors == {}
    assert band.bear.output.per_share < band.base.output.per_share < band.bull.output.per_share
    assert band.bear.overrides == {"revenue_growth": -0.05, "fcf_margin": 0.05}
    assert band.base.overrides == {}
    lo, hi = band.range
    assert lo == band.bear.output.per_share and hi == band.bull.output.per_share
    payload = band.to_dict()
    assert [s["label"] for s in payload["scenarios"]] == ["悲观", "基准", "乐观"]


def test_scenario_band_reports_reason_instead_of_inventing_value():
    band = scenario_band(
        BASE,
        bear_overrides={"discount_rate": 0.0, "terminal_growth": 0.0},
        bull_overrides={"nonexistent_field": 1},
    )
    assert band.bear is None and band.bull is None
    assert band.base is not None
    assert "折现率" in band.errors["悲观"]
    assert "未知假设字段" in band.errors["乐观"]


def test_sensitivity_table_keeps_none_for_meaningless_cells():
    table = sensitivity_table(
        ValuationAssumptions(**{**BASE.__dict__, "terminal_growth": 0.05}),
        growth_values=[0.0, 0.10],
        discount_values=[0.04, 0.10, 0.20],
    )
    assert table["unit"] == "元/股"
    first = table["rows"][0]
    # 折现率 4% ≤ 永续增长 5% → 无意义，必须是 None 而不是 0
    assert first["discount_rate_cells"][0] is None
    assert all(v is not None for v in first["discount_rate_cells"][1:])
    # 折现率越高价值越低（同一行内单调）
    cells = first["discount_rate_cells"][1:]
    assert cells[0] > cells[1]


def test_upside_and_margin_of_safety_need_both_inputs():
    assert upside_ratio(12.0, 10.0) == 0.2
    assert margin_of_safety(12.0, 9.0) == 0.25
    assert upside_ratio(None, 10.0) is None
    assert upside_ratio(12.0, None) is None
    assert upside_ratio(12.0, 0.0) is None
    assert margin_of_safety(0.0, 10.0) is None


def test_weighted_value_requires_matching_explicit_weights():
    band = scenario_band(BASE, bear_overrides={"revenue_growth": -0.05},
                         bull_overrides={"revenue_growth": 0.10})
    scenarios = [band.bear, band.base, band.bull]
    assert weighted_value(scenarios, [0.25, 0.5, 0.25]) is not None
    assert weighted_value(scenarios, [0.5, 0.5]) is None       # 权重个数不匹配
    assert weighted_value(scenarios, [0.0, 0.0, 0.0]) is None   # 权重全零
    assert weighted_value([], []) is None
