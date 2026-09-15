"""S1 验收测试：证据绑定、结构化决策卡、增仓门禁、措辞无关性、越界拦截 100%。"""
from __future__ import annotations

import pytest

from app.explain.deepseek import EvidencePack
from app.research.thesis import (
    CRITICAL_MISSING_KEYS,
    parse_critic_output,
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
    # 同一条证据可被两方同时引用：必须保留在两边，并把交集显式列出
    assert set(card.supporting_evidence_ids) & set(card.opposing_evidence_ids) == set()
    # 再用一个"两边都引用 fact:roe"的场景验证交集被记录而不是被抹掉
    overlap = build_thesis_card(
        _analysis(), PACK,
        model_text="净资产收益率 11.33%（fact:roe）",
        variant_text="净资产收益率 11.33% 并不高（fact:roe）",
    )
    assert overlap.supporting_evidence_ids == ["fact:roe"]
    assert overlap.opposing_evidence_ids == ["fact:roe"]
    assert overlap.shared_evidence_ids == ["fact:roe"]
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


def _bound_critic():
    """一份通过结构化契约的反方输出（3 条，id 全部真实存在）。"""
    from app.research.thesis import CriticReport, OpposingOpinion, bind_critic_output

    return bind_critic_output(
        CriticReport(opposing=[
            OpposingOpinion(claim="净资产收益率只有 11.33%，不算高", evidence_id="fact:roe"),
            OpposingOpinion(claim="资产负债率 64.89% 偏高", evidence_id="fact:debt_ratio"),
            OpposingOpinion(claim="现金流覆盖并不算厚", evidence_id="fact:ocf_to_profit"),
        ]),
        PACK,
    )


def test_gate_allows_when_data_complete_and_citations_valid():
    card = build_thesis_card(
        _analysis(), PACK,
        model_text="质量分 59.9（calc:quality_score）",
        critic_binding=_bound_critic(),
    )
    gate = position_gate(card)
    assert card.citations_valid is True
    assert card.opposing_incomplete is False
    assert gate["allowed"] is True
    assert gate["blockers"] == []


def test_gate_blocks_when_critic_is_absent_even_with_clean_citations():
    """没有可核对的反方意见 = 没人替我们找错 → 不允许增仓。"""
    card = build_thesis_card(_analysis(), PACK, model_text="质量分 59.9（calc:quality_score）")
    assert card.citations_valid is True
    assert card.opposing_incomplete is True
    gate = position_gate(card)
    assert gate["allowed"] is False
    assert any("反方审查未通过结构化契约" in item for item in gate["blockers"])


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
    # 数字越界必须单独说清楚（而不是笼统地说"引用有问题"）
    assert any("证据包外的数字" in item and "45.2" in item for item in gate["blockers"])


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

# ── 结构缺口（反方没标 id 不能被静默忽略） ──────────────────────────────


def test_missing_opposing_ids_is_reported_as_a_gap():
    card = build_thesis_card(_analysis(), PACK, variant_text="估值可能过于乐观，护城河在收窄。")
    assert card.opposing_evidence_ids == []
    assert any("找不到可回指" in gap for gap in card.gaps)


def test_absent_critic_is_also_reported():
    card = build_thesis_card(_analysis(), PACK)
    assert any("未生成独立反方意见" in gap for gap in card.gaps)


def test_gap_list_is_empty_when_both_sides_cite_ids():
    card = build_thesis_card(
        _analysis(), PACK,
        model_text="净资产收益率 11.33%（fact:roe）",
        variant_text="资产负债率 64.89% 偏高（fact:debt_ratio）",
    )
    assert card.gaps == []
    assert card.supporting_evidence_ids and card.opposing_evidence_ids


def test_supporting_ids_are_never_invented_from_the_pack():
    """没有模型文字时不得凭空把证据包前几条当成"支持证据"。"""
    card = build_thesis_card(_analysis(), PACK)
    assert card.supporting_evidence_ids == []
    assert any("未标注任何支持证据 id" in gap for gap in card.gaps)

# ── 反方结构化契约（S1 收尾） ─────────────────────────────────────────────


def _critic_json(items, cannot=None):
    import json as _json
    return _json.dumps({"opposing": items, "cannot_answer": cannot or []}, ensure_ascii=False)


def test_parse_critic_output_accepts_plain_json():
    report, error = parse_critic_output(_critic_json([
        {"claim": "毛利率偏低", "evidence_id": "fact:debt_ratio", "why_it_matters": "盈利质量"},
        {"claim": "增速放缓", "evidence_id": "fact:roe", "why_it_matters": "成长性"},
        {"claim": "现金流覆盖不足", "evidence_id": "fact:ocf_to_profit", "why_it_matters": "现金"},
    ]))
    assert error is None and report is not None and len(report.opposing) == 3


def test_parse_critic_output_tolerates_code_fence_and_prose():
    text = "分析如下：\n```json\n" + _critic_json([
        {"claim": "a", "evidence_id": "fact:roe"},
        {"claim": "b", "evidence_id": "fact:roe"},
        {"claim": "c", "evidence_id": "fact:roe"},
    ]) + "\n```\n以上。"
    report, error = parse_critic_output(text)
    assert error is None and report is not None


def test_parse_critic_output_rejects_garbage_and_too_few():
    assert parse_critic_output("口头意见，没有结构化输出")[0] is None
    assert "JSON" in parse_critic_output("口头意见，没有结构化输出")[1]
    few, error = parse_critic_output(_critic_json([{"claim": "a", "evidence_id": "fact:roe"}]))
    assert few is None and "少于" in error


def test_bind_critic_output_flags_invalid_ids_and_keeps_valid_ones():
    from app.research.thesis import CriticReport, OpposingOpinion, bind_critic_output

    report = CriticReport(opposing=[
        OpposingOpinion(claim="净资产收益率偏低", evidence_id="fact:roe"),
        OpposingOpinion(claim="编造的依据", evidence_id="fact:not_exists"),
        OpposingOpinion(claim="负债偏高", evidence_id="fact:debt_ratio"),
    ])
    binding = bind_critic_output(report, PACK)
    assert binding["opposing_incomplete"] is True
    assert binding["invalid_evidence_ids"] == ["fact:not_exists"]
    # 合法的那两条仍然保留，便于人工复核
    assert set(binding["opposing_evidence_ids"]) == {"fact:roe", "fact:debt_ratio"}
    assert any(item["evidence_exists"] is False for item in binding["opinions"])


def test_card_uses_binding_and_marks_incomplete():
    from app.research.thesis import CriticReport, OpposingOpinion, bind_critic_output

    report = CriticReport(opposing=[
        OpposingOpinion(claim="净资产收益率偏低", evidence_id="fact:roe"),
        OpposingOpinion(claim="负债偏高", evidence_id="fact:debt_ratio"),
        OpposingOpinion(claim="现金流覆盖不足", evidence_id="fact:ocf_to_profit"),
    ])
    binding = bind_critic_output(report, PACK)
    card = build_thesis_card(
        _analysis(), PACK, critic_binding=binding,
        model_text="质量分 59.9（calc:quality_score）",
    )
    assert card.opposing_incomplete is False
    assert len(card.opposing_opinions) == 3
    assert set(card.opposing_evidence_ids) == {"fact:roe", "fact:debt_ratio", "fact:ocf_to_profit"}
    assert card.gaps == []
    assert position_gate(card)["allowed"] is True


def test_incomplete_critic_blocks_increase_with_specific_reason():
    from app.research.thesis import bind_critic_output

    binding = bind_critic_output(None, PACK, error="输出里没有 JSON 对象")
    card = build_thesis_card(_analysis(), PACK, critic_binding=binding)
    assert card.opposing_incomplete is True
    gate = position_gate(card)
    assert gate["allowed"] is False
    assert any("反方审查未通过结构化契约" in item for item in gate["blockers"])
    assert any("结构化契约" in gap for gap in card.gaps)


def test_cannot_answer_is_preserved_even_when_opposing_is_incomplete():
    from app.research.thesis import CriticReport, OpposingOpinion, bind_critic_output

    report = CriticReport(
        opposing=[OpposingOpinion(claim="x", evidence_id="fact:unknown")],
        cannot_answer=["缺少一致预期数据，无法判断市场共识"],
    )
    binding = bind_critic_output(report, PACK)
    card = build_thesis_card(_analysis(), PACK, critic_binding=binding)
    assert card.cannot_answer == ["缺少一致预期数据，无法判断市场共识"]
    assert card.opposing_incomplete is True

def test_evidence_ids_survive_stray_whitespace():
    """模型把 id 写成 "calc:dcf_ 悲观" 时不该被抽成不存在的 "calc:dcf_"。"""
    from app.research.thesis import normalize_evidence_text

    assert extract_evidence_ids("见 calc:dcf_ 悲观 与 fact: roe") == ["calc:dcf_悲观", "fact:roe"]
    assert normalize_evidence_text("calc:dcf_ 悲观") == "calc:dcf_悲观"
    # 规整只是去掉空白，不会把编造的 id 变成合法
    card = build_thesis_card(_analysis(), PACK, model_text="依据 calc:dcf_ 不存在 与 fact:made_up")
    assert card.citations_valid is False
    assert "fact:made_up" in card.citation_report["invalid_ids"]
