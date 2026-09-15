"""S1 验收测试：证据绑定、结构化决策卡、增仓门禁、措辞无关性、越界拦截 100%。"""
from __future__ import annotations

import pytest

from app.explain.deepseek import EvidencePack
from app.research.thesis import (
    CRITICAL_MISSING_KEYS,
    DECISION_LABELS,
    STRATEGY_HORIZON,
    Decision,
    StrategyType,
    build_thesis_card,
    card_from_payload,
    extract_evidence_ids,
    position_gate,
    validate_evidence_ids,
)

PACK = EvidencePack(
    symbol="000333",
    name="美的集团",
    generated_at="2026-09-15T11:00:00+08:00",
    facts=[
        {"id": "fact:roe", "label": "净资产收益率", "value": 11.33, "unit": "%",
         "origin": "事实", "source": "财报"},
        {"id": "fact:ocf_to_profit", "label": "经营现金流/净利润", "value": 1.42, "unit": "倍",
         "origin": "模型推断", "source": "本平台计算"},
        {"id": "fact:debt_ratio", "label": "资产负债率", "value": 64.89, "unit": "%",
         "origin": "事实", "source": "财报"},
    ],
    calculations=[
        {"id": "calc:quality_score", "label": "企业质量分", "value": 59.9, "unit": "分"},
        {"id": "calc:evidence_confidence", "label": "证据置信度", "value": 85.0, "unit": "分"},
        {"id": "calc:dcf_基准", "label": "基准情景每股价值", "per_share": 92.84, "unit": "元/股"},
        {"id": "calc:dcf_悲观", "label": "悲观情景每股价值", "per_share": 48.07, "unit": "元/股"},
    ],
)


def _analysis(**overrides) -> dict:
    base = {
        "symbol": "000333",
        "name": "美的集团",
        "1_conclusion": {
            "conclusion_key": "fair",
            "evidence_confidence_score": 85.0,
        },
        "2_data_asof": {
            "report_date": "2026-06-30",
            "staleness_days": 77.0,
            "completeness": {"missing_inputs": []},
        },
        "3_dimensions": {
            "quality": {"score": 59.9, "coverage": 1.0},
            "valuation": {
                "available": True,
                "applicability": {"applicable": True, "model": "两阶段 DCF",
                                  "caveat": "适用于经营现金流稳定的企业"},
                "basis": "示例假设（估计）",
            },
        },
        "5_scenarios": {
            "scenarios": [
                {"label": "悲观", "result": {"per_share": 48.07}},
                {"label": "基准", "result": {"per_share": 92.84}},
            ]
        },
        "6_open_items": {
            "invalidation_conditions": ["净资产收益率跌破 3.0% → 论点失效"],
            "review_triggers": ["下一期定期报告发布后"],
        },
        "7_provenance": {"inputs_snapshot": {"symbol": "000333", "price": 86.64}},
    }
    base.update(overrides)
    return base


# ── 结构化卡片 ─────────────────────────────────────────────────────────────


def test_card_contains_every_field_required_by_the_plan():
    """方案 §二「建议结构化输出」列出的字段必须一个不少。"""
    card = build_thesis_card(_analysis(), PACK, fetched_at="2026-09-15T11:05:00+08:00")
    payload = card.model_dump()
    required = {
        "strategy_type", "horizon", "as_of", "thesis", "variant_view",
        "supporting_evidence_ids", "opposing_evidence_ids", "assumptions",
        "valuation_method", "scenario_result_ids", "invalidation_conditions",
        "review_triggers", "missing_data", "decision", "confidence_basis",
        "model_version", "prompt_version",
    }
    assert required <= set(payload)
    assert card.strategy_type is StrategyType.QUALITY_VALUE
    assert card.horizon == STRATEGY_HORIZON[StrategyType.QUALITY_VALUE]
    assert "报告期 2026-06-30" in card.as_of
    assert card.decision is Decision.WATCH and card.decision_label == DECISION_LABELS[Decision.WATCH]
    assert card.scenario_result_ids == ["calc:dcf_悲观", "calc:dcf_基准"]
    assert card.invalidation_conditions and card.review_triggers
    assert card.confidence_basis["not_a_probability"]
    assert card.model_version and card.prompt_version


def test_card_rejects_unknown_extra_fields():
    """结构校验必须严格：多出来的字段直接报错，不做静默兼容。"""
    card = build_thesis_card(_analysis(), PACK)
    payload = card.model_dump()
    payload["unexpected_field"] = 1
    with pytest.raises(Exception):
        card_from_payload(payload)


def test_strategy_type_and_horizon_come_from_explicit_choice():
    card = build_thesis_card(_analysis(), PACK, strategy_type=StrategyType.TREND)
    assert card.strategy_type is StrategyType.TREND
    assert card.horizon == STRATEGY_HORIZON[StrategyType.TREND]
    assert "不依赖基本面估值" in card.return_source


# ── 引用与数字校验（越界/无效引用拦截率 100%） ────────────────────────────


def test_invalid_evidence_id_fails_the_card():
    card = build_thesis_card(
        _analysis(), PACK,
        model_text="净资产收益率 11.33%（fact:roe），行业排名 fact:not_exists 领先。",
    )
    assert card.citations_valid is False
    assert "fact:not_exists" in card.citation_report["invalid_ids"]


def test_valid_ids_and_numbers_pass():
    card = build_thesis_card(
        _analysis(), PACK,
        model_text="净资产收益率 11.33%（fact:roe），质量分 59.9（calc:quality_score）。",
        variant_text="资产负债率 64.89% 偏高（fact:debt_ratio）。",
    )
    assert card.citations_valid is True
    assert "fact:roe" in card.supporting_evidence_ids
    assert "fact:debt_ratio" in card.opposing_evidence_ids
    # 支持与反对不得重复引用同一条
    assert not set(card.supporting_evidence_ids) & set(card.opposing_evidence_ids)
    assert card.evidence_refs and card.evidence_refs[0].period == "2026-06-30"


def test_fixed_adversarial_set_is_blocked_one_hundred_percent():
    """固定反例集：越界数字 + 无效引用，必须 100% 被拦。"""
    cases = [
        ("净资产收益率将提升到 88.8%", None, "number"),
        ("目标价 250 元，上涨空间 190%", None, "number"),
        ("依据 fact:pe_ttm 与 fact:industry_rank", None, "id"),
        ("参考 calc:dcf_乐观 的未来现金流", None, "id"),
        ("综合来看，胜率 92% 且必涨", None, "number"),
        ("按 fact:roe 计算，未来三年复合增速 45.2%", None, "number"),
        ("内部模型给出 fact:hidden_alpha 信号", None, "id"),
        ("市值将达 1,234.5 亿元", None, "number"),
        ("对比 fact:roe 与 fact:gross_margin", None, "id"),
        ("预计分红率 78.9%", None, "number"),
    ]
    blocked = 0
    for text, _, kind in cases:
        card = build_thesis_card(_analysis(), PACK, model_text=text)
        if card.citations_valid is False:
            blocked += 1
            if kind == "id":
                assert card.citation_report["invalid_ids"]
            else:
                assert card.citation_report["unverified_numbers"]
    assert blocked == len(cases), f"拦截率 {blocked}/{len(cases)}，必须 100%"


def test_evidence_id_extraction_keeps_order_and_dedupes():
    ids = extract_evidence_ids("fact:roe 与 calc:quality_score，再看 fact:roe")
    assert ids == ["fact:roe", "calc:quality_score"]


def test_validate_evidence_ids_reports_known_set_size():
    report = validate_evidence_ids(["fact:roe", "calc:nope"], PACK)
    assert report["checked_ids"] == 2
    assert report["known_ids"] == len(PACK.facts) + len(PACK.calculations)
    assert report["invalid_ids"] == ["calc:nope"]


# ── 增仓门禁 ───────────────────────────────────────────────────────────────


def test_gate_allows_when_data_complete_and_citations_valid():
    card = build_thesis_card(_analysis(), PACK, model_text="净资产收益率 11.33%（fact:roe）")
    gate = position_gate(card)
    assert card.citations_valid is True
    assert gate["allowed"] is True
    assert gate["blockers"] == []


def test_missing_critical_data_blocks_increase_even_when_other_metrics_look_good():
    analysis = _analysis()
    analysis["2_data_asof"]["completeness"]["missing_inputs"] = ["显式估值假设（增长率/自由现金流率/折现率/永续增长率/股本）"]
    card = build_thesis_card(analysis, PACK)
    assert set(card.missing_data) & set(CRITICAL_MISSING_KEYS)
    gate = position_gate(card)
    assert gate["allowed"] is False
    assert any("关键数据缺失" in item for item in gate["blockers"])


def test_invalid_citation_blocks_increase():
    card = build_thesis_card(_analysis(), PACK, model_text="增速将达 45.2%")
    gate = position_gate(card)
    assert card.citations_valid is False
    assert gate["allowed"] is False
    assert any("不存在的证据 id" in item or "引用校验" in item for item in gate["blockers"])


def test_not_applicable_valuation_blocks_increase():
    analysis = _analysis()
    analysis["1_conclusion"]["conclusion_key"] = "model_not_applicable"
    analysis["3_dimensions"]["valuation"]["applicability"] = {
        "applicable": False, "model": "两阶段 DCF", "caveat": "银行不适用收入折现",
    }
    card = build_thesis_card(analysis, PACK)
    assert card.decision is Decision.NOT_APPLICABLE
    gate = position_gate(card)
    assert gate["allowed"] is False
    assert any("不适用" in item for item in gate["blockers"])


def test_insufficient_evidence_blocks_increase():
    analysis = _analysis()
    analysis["1_conclusion"]["conclusion_key"] = "insufficient"
    card = build_thesis_card(analysis, PACK)
    assert card.decision is Decision.INSUFFICIENT_DATA
    assert position_gate(card)["allowed"] is False


# ── 措辞无关性（同一证据包 → 同一数值） ───────────────────────────────────


def test_card_numbers_do_not_depend_on_model_wording():
    """方案 §四 S1 验收：同一证据包的计算结果不随模型措辞变化。"""
    neutral = build_thesis_card(_analysis(), PACK, model_text="净资产收益率 11.33%（fact:roe）。")
    enthusiastic = build_thesis_card(
        _analysis(), PACK,
        model_text="非常看好！净资产收益率 11.33%（fact:roe），估值极具吸引力。",
    )
    for field in (
        "strategy_type", "horizon", "as_of", "decision", "decision_label",
        "scenario_result_ids", "invalidation_conditions", "review_triggers",
        "missing_data", "confidence_basis", "valuation_method",
    ):
        assert getattr(neutral, field) == getattr(enthusiastic, field), field
    # 只有文字段允许不同
    assert neutral.thesis != enthusiastic.thesis
