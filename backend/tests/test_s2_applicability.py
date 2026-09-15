"""S2 验收测试：估值适用性判定、价格单调不变式、结构化失效条件、反向估值敏感性。"""
from __future__ import annotations

from datetime import date

import pytest

from app.fundamentals.applicability import (
    ApplicabilityInputs,
    HIGH_LEVERAGE_RATIO,
    assess_applicability,
)
from app.fundamentals.decision import (
    CONCLUSION_RANK,
    DecisionInputs,
    build_analysis,
    conclusion_rank,
    structured_invalidation_conditions,
)
from app.fundamentals.quality import QualityInputs, assess_quality, evidence_confidence
from app.fundamentals.valuation import (
    ValuationAssumptions,
    ValuationError,
    implied_growth_grid,
    implied_revenue_growth,
    margin_of_safety,
    scenario_band,
)

BASE_ASSUMPTIONS = ValuationAssumptions(
    revenue=522104758000.0,
    revenue_growth=0.06,
    fcf_margin=0.1338,
    discount_rate=0.10,
    terminal_growth=0.02,
    shares=7629500000.0,
    net_debt=-14272378000.0,
    years=5,
    basis="S2 测试假设（估计）",
)


# ── 适用性判定（拒绝 vs 警示 vs 未知） ────────────────────────────────────


def test_financial_industry_is_rejected_outright():
    report = assess_applicability(
        ApplicabilityInputs(industry="银行Ⅱ", free_cash_flow=1e9, equity=2e11, net_debt=0.0)
    )
    assert report.applicable is False
    assert report.verdict == "rejected"
    assert "剩余收益" in report.rejected_reason


def test_negative_free_cash_flow_is_rejected_with_reason():
    report = assess_applicability(
        ApplicabilityInputs(industry="软件开发", free_cash_flow=-1.2e8, equity=3e9, net_debt=0.0)
    )
    assert report.verdict == "rejected"
    assert "自由现金流为" in report.rejected_reason
    assert any("清算" in item for item in report.caveats)


def test_negative_equity_is_rejected():
    report = assess_applicability(
        ApplicabilityInputs(industry="房地产开发", free_cash_flow=1e8, equity=-5e8, net_debt=1e9)
    )
    assert report.verdict == "rejected"
    assert "资不抵债" in report.rejected_reason


def test_missing_cash_flow_returns_unknown_not_a_guess():
    report = assess_applicability(
        ApplicabilityInputs(industry="白色家电", free_cash_flow=None, equity=2e11)
    )
    assert report.verdict == "unknown"
    assert report.applicable is False
    assert any("自由现金流" in item for item in report.missing_data)
    assert "缺数据" in report.caveats[0]


def test_cyclical_industry_requires_normalization_but_allows_conclusion():
    report = assess_applicability(
        ApplicabilityInputs(industry="钢铁行业", free_cash_flow=5e8, equity=1e10, net_debt=2e9)
    )
    assert report.applicable is True
    assert report.requires_normalization is True
    assert report.verdict == "warning"
    assert any("正常化" in item for item in report.caveats)


def test_high_leverage_is_a_warning_not_a_rejection():
    report = assess_applicability(
        ApplicabilityInputs(
            industry="白色家电", free_cash_flow=3e10, equity=1e10, net_debt=2e10
        )
    )
    assert report.applicable is True
    assert report.verdict == "warning"
    assert report.data_used["net_debt_to_equity"] > HIGH_LEVERAGE_RATIO


def test_clean_case_has_no_warning():
    report = assess_applicability(
        ApplicabilityInputs(
            industry="白色家电", free_cash_flow=3.5e10, equity=2.26e11, net_debt=-1.4e10,
            net_profit=2.6e10, report_date="2026-06-30",
        )
    )
    assert report.verdict == "ok"
    assert report.caveats == []


# ── S2 验收不变式：只提高价格，吸引力不能上升 ─────────────────────────────


def _analysis_at_price(price: float, base_value: float = 92.84) -> dict:
    """同一基本面、不同价格下的分析结论（用结论键的排序体现吸引力）。"""
    upside = base_value / price - 1.0
    if upside >= 0.20:
        key = "attractive"
    elif upside < -0.10:
        key = "overvalued"
    else:
        key = "fair"
    return {"1_conclusion": {"conclusion_key": key}}


def test_conclusion_rank_is_monotone_in_price():
    """价格从低到高，吸引力序只能下降或持平 —— 不允许"越贵越值得买"。"""
    prices = [40, 60, 80, 92.84, 110, 150, 260]
    ranks = [
        conclusion_rank(_analysis_at_price(price)["1_conclusion"]["conclusion_key"])
        for price in prices
    ]
    assert ranks == sorted(ranks, reverse=True), ranks
    assert ranks[0] == CONCLUSION_RANK["attractive"]
    assert ranks[-1] == CONCLUSION_RANK["overvalued"]


def test_margin_of_safety_never_rises_with_price():
    values = [margin_of_safety(100.0, price) for price in (50, 80, 100, 120, 200)]
    assert all(a >= b for a, b in zip(values, values[1:])), values
    # 价格高于价值时安全边际为负
    assert margin_of_safety(100.0, 120.0) < 0


def test_unknown_conclusion_key_is_never_treated_as_positive():
    assert conclusion_rank("attractive") > conclusion_rank("不存在的键")
    assert conclusion_rank(None) == 0
    assert conclusion_rank("insufficient") == 0


# ── 结构化失效条件（有阈值、有来源、有证据 id） ───────────────────────────


def _quality(**overrides):
    params = dict(
        roe=18.0, gross_margin=45.0, net_margin=18.0, debt_ratio=30.0, revenue_yoy=20.0,
        profit_yoy=25.0, ocf_to_profit=1.2, report_date=date(2026, 6, 30),
        as_of=date(2026, 9, 15), industry="白色家电", source="eastmoney",
    )
    params.update(overrides)
    return assess_quality(QualityInputs(**params))


def _inputs(**overrides) -> DecisionInputs:
    params = dict(
        symbol="000333", name="美的集团", quality=_quality(),
        confidence=evidence_confidence(
            coverage=1.0, report_date=date(2026, 6, 30), as_of=date(2026, 9, 15),
            industry_identified=True,
        ),
        price=86.64, report_date=date(2026, 6, 30), industry="白色家电", source="eastmoney",
        valuation=scenario_band(BASE_ASSUMPTIONS, bear_overrides={"revenue_growth": 0.0},
                                bull_overrides={"revenue_growth": 0.12}),
    )
    params.update(overrides)
    return DecisionInputs(**params)


def test_structured_conditions_have_threshold_source_and_evidence_id():
    conditions = structured_invalidation_conditions(_inputs())
    assert conditions, "至少要产出一条可验证条件"
    for item in conditions:
        assert item["operator"] in ("<", "<=", ">", ">=")
        assert item["source"], item
        assert item["evidence_id"], item
        assert item["threshold"] is not None
        assert item["current"] is not None
    # 阈值来源必须区分"研究假设/画像阈值/平台计算"，不能混为一谈
    sources = {item["source"] for item in conditions}
    assert any("研究假设" in source or "画像阈值" in source for source in sources)
    assert any(item["evidence_id"] == "calc:dcf_基准" for item in conditions)


def test_structured_conditions_skip_metrics_that_are_missing():
    """缺失指标不得凭空生成条件（缺数据就是缺数据）。"""
    inputs = _inputs(quality=_quality(roe=None, debt_ratio=None, profit_yoy=None,
                                      ocf_to_profit=None))
    conditions = structured_invalidation_conditions(inputs)
    metrics = {item["metric"] for item in conditions}
    assert "roe" not in metrics and "debt_ratio" not in metrics
    assert metrics <= {"price_vs_base_value"}


def test_analysis_payload_carries_structured_conditions_and_applicability():
    payload = build_analysis(
        _inputs(applicability=assess_applicability(ApplicabilityInputs(
            industry="白色家电", free_cash_flow=3.5e10, equity=2.26e11, net_debt=-1.4e10,
        )).to_dict())
    )
    open_items = payload["6_open_items"]
    assert open_items["invalidation_conditions"]
    assert open_items["structured_conditions"]
    rules = payload["3_dimensions"]["valuation"]["applicability_rules"]
    assert rules["verdict"] == "ok"


# ── 反向估值：解释 + 敏感性 ───────────────────────────────────────────────


def test_implied_growth_reproduces_target_price():
    result = implied_revenue_growth(BASE_ASSUMPTIONS, target_price=92.84)
    assert result["status"] == "solved"
    assert abs(result["matched_price"] - 92.84) < 0.01
    assert result["fixed_assumptions"]["discount_rate"] == 0.10


def test_implied_growth_reports_out_of_range_instead_of_extrapolating():
    low = implied_revenue_growth(BASE_ASSUMPTIONS, target_price=1.0)
    high = implied_revenue_growth(BASE_ASSUMPTIONS, target_price=1e9)
    assert low["status"] == "below_range" and low["implied_growth"] is None
    assert high["status"] == "above_range" and high["implied_growth"] is None


def test_implied_growth_is_higher_when_discount_rate_is_higher():
    """折现率越高，同一价格要求的增长率越高（单调性，可手算核对方向）。"""
    cheap = implied_revenue_growth(BASE_ASSUMPTIONS, target_price=92.84)
    expensive = implied_revenue_growth(
        ValuationAssumptions(**{**BASE_ASSUMPTIONS.__dict__, "discount_rate": 0.12}),
        target_price=92.84,
    )
    assert cheap["status"] == "solved" and expensive["status"] == "solved"
    assert expensive["implied_growth"] > cheap["implied_growth"]


def test_implied_growth_grid_exposes_range_and_unsolved_cells():
    grid = implied_growth_grid(
        BASE_ASSUMPTIONS, target_price=92.84, discount_values=[0.08, 0.10, 0.14, 0.02]
    )
    assert grid["solved_count"] >= 2
    assert grid["implied_growth_range"][0] < grid["implied_growth_range"][1]
    assert any(row["status"] != "solved" for row in grid["rows"])  # 2% 折现率≈永续 2% → 无解
    assert "单点" in grid["note"]


def test_implied_growth_rejects_impossible_inputs():
    with pytest.raises(ValuationError):
        implied_revenue_growth(BASE_ASSUMPTIONS, target_price=0)
    with pytest.raises(ValuationError):
        implied_revenue_growth(BASE_ASSUMPTIONS, target_price=10, lower_bound=0.5, upper_bound=0.2)
