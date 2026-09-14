"""投资论点与 7 项统一输出测试：不承诺收益、允许暂不行动、仓位不由评分决定。"""
from __future__ import annotations

from datetime import date

from app.fundamentals.decision import (
    CONCLUSIONS,
    FORBIDDEN_PHRASES,
    DecisionInputs,
    PortfolioContext,
    build_analysis,
    model_applicability,
    position_ceiling,
)
from app.fundamentals.quality import QualityInputs, assess_quality, evidence_confidence
from app.fundamentals.valuation import ValuationAssumptions, scenario_band

AS_OF = date(2026, 9, 14)


def _quality(**overrides):
    base = dict(
        roe=18.0,
        gross_margin=45.0,
        net_margin=18.0,
        debt_ratio=30.0,
        revenue_yoy=20.0,
        profit_yoy=25.0,
        ocf_to_profit=1.2,
        report_date=date(2026, 6, 30),
        as_of=AS_OF,
        industry="白色家电",
        source="eastmoney",
    )
    base.update(overrides)
    return assess_quality(QualityInputs(**base))


def _confidence(report_date=date(2026, 6, 30), coverage=1.0, industry=True):
    return evidence_confidence(
        coverage=coverage,
        report_date=report_date,
        as_of=AS_OF,
        industry_identified=industry,
        has_statement_detail=False,
        sources=1,
    )


def _band(*, growth=0.08, margin=0.09, discount=0.10, terminal=0.02, price=1000.0,
          shares=100.0):
    base = ValuationAssumptions(
        revenue=price,
        revenue_growth=growth,
        fcf_margin=margin,
        discount_rate=discount,
        terminal_growth=terminal,
        shares=shares,
        basis="测试假设（估计）",
    )
    return scenario_band(
        base,
        bear_overrides={"revenue_growth": -0.05, "fcf_margin": margin - 0.03},
        bull_overrides={"revenue_growth": growth + 0.05, "fcf_margin": margin + 0.03},
    )


def _inputs(**overrides) -> DecisionInputs:
    base = dict(
        symbol="000333",
        name="美的集团",
        quality=_quality(),
        confidence=_confidence(),
        price=87.02,
        report_date=date(2026, 6, 30),
        fetched_at="2026-09-14T14:00:00",
        industry="白色家电",
        source="eastmoney",
    )
    base.update(overrides)
    return DecisionInputs(**base)


def test_without_valuation_assumptions_it_refuses_to_give_a_value_judgement():
    payload = build_analysis(_inputs())
    assert payload["1_conclusion"]["conclusion_key"] == "need_valuation"
    assert payload["3_dimensions"]["valuation"]["available"] is False
    assert "未提供显式估值假设" in payload["3_dimensions"]["valuation"]["reason"]
    assert payload["5_scenarios"]["scenarios"] == []
    assert "系统不代填" in payload["5_scenarios"]["reason"]


def test_expired_or_thin_evidence_stops_the_judgement():
    expired = build_analysis(
        _inputs(confidence=_confidence(report_date=date(2024, 12, 31)))
    )
    assert expired["1_conclusion"]["conclusion_key"] == "expired"
    assert "停止判断" in expired["1_conclusion"]["conclusion"]

    thin = build_analysis(
        _inputs(
            quality=_quality(roe=None, gross_margin=None, net_margin=None, debt_ratio=None,
                             revenue_yoy=None, profit_yoy=None),
            confidence=_confidence(coverage=0.1),
        )
    )
    assert thin["1_conclusion"]["conclusion_key"] == "insufficient"
    assert "暂不行动" in thin["1_conclusion"]["conclusion"]
    assert thin["4_evidence"]["support"] == []


def test_valuation_drives_attractive_or_overvalued_conclusion():
    band = _band()
    value = band.base.output.per_share
    assert value > 0

    # 现价远低于价值 → 有安全边际
    cheap = build_analysis(
        _inputs(price=value * 0.5, valuation=band, valuation_basis="估计")
    )
    assert cheap["1_conclusion"]["conclusion_key"] == "attractive"

    # 现价远高于价值 → 吸引力有限
    rich = build_analysis(
        _inputs(price=value * 3.0, valuation=band, valuation_basis="估计")
    )
    assert rich["1_conclusion"]["conclusion_key"] == "overvalued"

    # 现价略低于价值（安全边际不足 20%）→ 区间内、无安全边际
    mid = build_analysis(
        _inputs(price=value * 0.95, valuation=band, valuation_basis="估计")
    )
    assert mid["1_conclusion"]["conclusion_key"] == "fair"


def test_output_contains_no_return_promise_wording():
    payload = build_analysis(_inputs(price=10.0, valuation=_band(), valuation_basis="估计"))
    assert payload["wording_guard"] == []
    text = str(payload)
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in text, phrase
    assert payload["1_conclusion"]["conclusion_key"] in CONCLUSIONS
    assert "不构成投资建议" in payload["disclaimer"]


def test_attractiveness_and_confidence_are_reported_separately():
    payload = build_analysis(_inputs(price=10.0, valuation=_band(), valuation_basis="估计"))
    conclusion = payload["1_conclusion"]
    assert conclusion["attractiveness_quality_score"] is not None
    assert conclusion["evidence_confidence_score"] is not None
    assert "不是上涨概率" in conclusion["separation_note"]
    assert payload["3_dimensions"]["quality"]["score"] == conclusion["attractiveness_quality_score"]


def test_evidence_lists_are_capped_at_three_and_labelled_by_origin():
    payload = build_analysis(_inputs(price=10.0, valuation=_band(), valuation_basis="估计"))
    support = payload["4_evidence"]["support"]
    oppose = payload["4_evidence"]["oppose"]
    assert 0 < len(support) <= 3
    assert 0 < len(oppose) <= 3
    legend = payload["4_evidence"]["origin_legend"]
    for item in support + oppose:
        assert item["origin"] in legend, item
        assert item["evidence"]


def test_open_items_include_invalidation_and_review_triggers():
    payload = build_analysis(_inputs(price=10.0, valuation=_band(), valuation_basis="估计"))
    open_items = payload["6_open_items"]
    assert len(open_items["invalidation_conditions"]) >= 2
    assert any("净资产收益率跌破" in c for c in open_items["invalidation_conditions"])
    assert any("现价高于基准情景价值" in c for c in open_items["invalidation_conditions"])
    assert len(open_items["review_triggers"]) >= 3
    assert "禁止因为亏损而把交易改称长期投资" in open_items["horizon_discipline"]
    assert any("未接入" in item for item in open_items["unverified"])


def test_portfolio_and_market_expectation_are_marked_not_available():
    payload = build_analysis(_inputs())
    assert payload["3_dimensions"]["market_expectation"]["available"] is False
    assert "未接入" in payload["3_dimensions"]["market_expectation"]["reason"]
    assert payload["3_dimensions"]["portfolio_risk"]["permanent_loss_risk"].startswith("未量化")
    assert payload["2_data_asof"]["completeness"]["missing_inputs"]


def test_position_ceiling_needs_explicit_user_constraints():
    none_given = position_ceiling(_inputs(), stop_distance=0.08)
    assert none_given["ceiling_pct"] is None
    assert "未提供组合与个人约束" in none_given["reason"]

    partial = position_ceiling(
        _inputs(portfolio=PortfolioContext(max_symbol_weight=0.1)), stop_distance=0.08
    )
    assert partial["ceiling_pct"] is None
    assert "行业集中度约束" in partial["reason"]

    full = position_ceiling(
        _inputs(
            portfolio=PortfolioContext(
                max_loss_per_trade=0.02,
                max_symbol_weight=0.15,
                industry_weight=0.20,
                max_industry_weight=0.30,
                horizon="3 年以上",
            )
        ),
        stop_distance=0.10,
    )
    # min(15%, 2%/10%=20%, 30%-20%=10%) = 10%
    assert full["ceiling_pct"] == 10.0
    assert full["components"]["风险承受约束"] == 0.2
    assert full["components"]["行业剩余额度"] == 0.1


def test_position_ceiling_flags_liquidity_need_and_horizon_from_user():
    payload = build_analysis(
        _inputs(
            price=10.0,
            valuation=_band(),
            valuation_basis="估计",
            portfolio=PortfolioContext(
                max_loss_per_trade=0.02,
                max_symbol_weight=0.15,
                industry_weight=0.20,
                max_industry_weight=0.30,
                horizon="1 年以内",
                liquidity_needed_soon=True,
            ),
        )
    )
    assert payload["1_conclusion"]["horizon"] == "1 年以内"
    ceiling = payload["3_dimensions"]["portfolio_risk"]["position_ceiling"]
    assert ceiling["liquidity_penalty"] is not None
    assert ceiling["ceiling_pct"] == 10.0


def test_default_horizon_is_not_assumed():
    payload = build_analysis(_inputs())
    assert "未指定" in payload["1_conclusion"]["horizon"]
    assert "不覆盖短线交易规则" in payload["1_conclusion"]["horizon"]


def test_model_applicability_refuses_banks():
    """实测教训：把收入折现套到银行上会差 15 倍，必须主动声明不适用。"""
    bank = model_applicability("financial")
    assert bank["applicable"] is False
    assert "不适用于银行" in bank["caveat"]
    assert model_applicability("general")["applicable"] is True
    assert model_applicability("unknown-profile")["applicable"] is True

    payload = build_analysis(
        _inputs(industry="银行", price=10.0, valuation=_band(), valuation_basis="估计")
    )
    assert payload["3_dimensions"]["valuation"]["applicability"]["applicable"] is False
    # 关键：模型不适用时**不允许**因为"看起来便宜"就给出 attractive 结论
    assert payload["1_conclusion"]["conclusion_key"] == "model_not_applicable"
    assert "不适用" in payload["1_conclusion"]["conclusion"]


def test_provenance_is_reproducible_and_versioned():
    payload = build_analysis(_inputs())
    provenance = payload["7_provenance"]
    assert provenance["model_version"] == "fundamentals-decision-v1"
    assert "同样的输入" in provenance["reproduce"]
    assert set(provenance["formulas"]) >= {
        "durability_metrics",
        "annualization",
        "dcf",
        "margin_of_safety",
    }
