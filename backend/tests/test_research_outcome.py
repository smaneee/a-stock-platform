"""复盘归因测试：五类归因（判断错误/事实变化/估值变化/仓位问题/执行偏差）+ 随机波动与兜底。

方案 §三：「复盘先检查原论点是否成立，再检查收益；区分判断错误、估值过高、仓位问题、
执行偏差和随机波动」。
"""
from __future__ import annotations

from app.research.outcome import (
    NOISE_PRICE_BAND,
    OUTCOME_LABELS,
    classify_review_outcome,
    hit_invalidation_conditions,
)

RUN = {"price": 100.0, "conclusion_key": "fair"}


def _analysis(**metrics) -> dict:
    return {
        "3_dimensions": {
            "quality": {"subscores": {key: {"value": value} for key, value in metrics.items()}}
        }
    }


def _condition(metric="roe", operator="<", threshold=3.0, label="净资产收益率"):
    return {
        "metric": metric,
        "label": label,
        "operator": operator,
        "threshold": threshold,
        "source": "行业画像阈值（研究假设）",
        "evidence_id": f"fact:{metric}",
        "text": f"{label} {operator} {threshold}",
    }


# ── ① 论点失效 → 判断错误（最高优先级） ───────────────────────────────────


def test_invalidation_hit_is_thesis_failure():
    outcome = classify_review_outcome(
        run=RUN, current_snapshot={"price": 60.0}, current_analysis=_analysis(roe=1.2),
        structured_conditions=[_condition()],
    )
    assert outcome.key == "thesis_invalidated"
    assert outcome.thesis_level is True
    assert "1.2" in outcome.reasons[0] and "3.0" in outcome.reasons[0]
    assert outcome.evidence["hits"][0]["source"].startswith("行业画像阈值")


def test_invalidation_not_hit_when_metric_above_threshold():
    hits = hit_invalidation_conditions([_condition()], _analysis(roe=11.33))
    assert hits == []


def test_all_four_operators_are_supported():
    assert hit_invalidation_conditions([_condition("roe", "<", 3.0)], _analysis(roe=2.0))
    assert hit_invalidation_conditions([_condition("roe", "<=", 3.0)], _analysis(roe=3.0))
    assert hit_invalidation_conditions([_condition("debt_ratio", ">", 75.0)],
                                       _analysis(debt_ratio=80.0))
    assert hit_invalidation_conditions([_condition("debt_ratio", ">=", 75.0)],
                                       _analysis(debt_ratio=75.0))


def test_missing_metric_does_not_fake_a_hit():
    assert hit_invalidation_conditions([_condition()], _analysis()) == []


def test_price_condition_is_skipped_by_metric_checker():
    """价格类条件需要现价与基准价值，不在指标比对里处理（避免误判）。"""
    condition = _condition("price_vs_base_value", ">", 92.84)
    assert hit_invalidation_conditions([condition], _analysis(roe=1.0)) == []


# ── ② 事实变化 ─────────────────────────────────────────────────────────────


def test_fact_change_outranks_valuation_drift():
    outcome = classify_review_outcome(
        run=RUN, current_snapshot={"price": 101.0}, current_analysis=_analysis(),
        changes=[
            {"field": "report_date", "label": "财报报告期", "before": "2026-06-30",
             "after": "2026-09-30"},
            {"field": "price", "label": "现价", "before": 100.0, "after": 101.0},
        ],
    )
    assert outcome.key == "fact_changed"
    assert outcome.thesis_level is True
    assert any("报告期" in item for item in outcome.reasons)


# ── ③ 估值变化（事实未变） ────────────────────────────────────────────────


def test_price_only_change_is_valuation_drift_not_thesis_failure():
    outcome = classify_review_outcome(
        run=RUN, current_snapshot={"price": 130.0}, current_analysis=_analysis(roe=12.0),
        changes=[{"field": "price", "label": "现价", "before": 100.0, "after": 130.0}],
    )
    assert outcome.key == "valuation_drift"
    assert outcome.thesis_level is False
    assert abs(outcome.evidence["price_change"] - 0.30) < 1e-9


def test_scenario_value_change_counts_as_valuation_drift():
    outcome = classify_review_outcome(
        run=RUN, current_snapshot={"price": 100.0}, current_analysis=_analysis(),
        changes=[{"field": "base_per_share", "label": "基准情景每股价值",
                  "before": 92.84, "after": 110.0}],
    )
    assert outcome.key == "valuation_drift"


# ── ④ 仓位问题 ─────────────────────────────────────────────────────────────


def test_position_above_user_cap_is_a_position_problem():
    outcome = classify_review_outcome(
        run=RUN, current_snapshot={"price": 100.0}, current_analysis=_analysis(),
        position_weight=0.35, max_symbol_weight=0.15,
    )
    assert outcome.key == "position_constraint"
    assert outcome.thesis_level is False
    assert "35.00%" in outcome.reasons[0] and "15.00%" in outcome.reasons[0]


def test_position_within_cap_is_not_flagged():
    outcome = classify_review_outcome(
        run=RUN, current_snapshot={"price": 100.0}, current_analysis=_analysis(),
        position_weight=0.10, max_symbol_weight=0.15,
    )
    assert outcome.key != "position_constraint"


# ── ⑤ 执行偏差 ─────────────────────────────────────────────────────────────


def test_planned_vs_executed_mismatch_is_execution_gap():
    outcome = classify_review_outcome(
        run=RUN, current_snapshot={"price": 100.0}, current_analysis=_analysis(),
        planned_quantity=10000, executed_quantity=8000,
    )
    assert outcome.key == "execution_gap"
    assert "10000" in outcome.reasons[0] and "8000" in outcome.reasons[0]


def test_equal_quantities_are_not_execution_gap():
    outcome = classify_review_outcome(
        run=RUN, current_snapshot={"price": 100.0}, current_analysis=_analysis(),
        planned_quantity=10000, executed_quantity=10000,
    )
    assert outcome.key != "execution_gap"


# ── ⑥ 随机波动与兜底 ──────────────────────────────────────────────────────


def test_small_move_without_changes_is_noise():
    outcome = classify_review_outcome(
        run=RUN, current_snapshot={"price": 100.0 * (1 + NOISE_PRICE_BAND / 2)},
        current_analysis=_analysis(),
    )
    assert outcome.key == "noise"
    assert outcome.thesis_level is False


def test_large_move_without_field_changes_is_not_noise():
    outcome = classify_review_outcome(
        run=RUN, current_snapshot={"price": 140.0}, current_analysis=_analysis(),
    )
    assert outcome.key == "inconclusive"     # 有变化但无法归类 → 交人工复核
    assert "人工复核" in outcome.reasons[0]


def test_every_outcome_has_a_label_and_is_serializable():
    for key in OUTCOME_LABELS:
        assert OUTCOME_LABELS[key]
    outcome = classify_review_outcome(
        run=RUN, current_snapshot={"price": 100.0}, current_analysis=_analysis()
    )
    payload = outcome.to_dict()
    assert set(payload) == {"key", "label", "reasons", "evidence", "thesis_level"}
