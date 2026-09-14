"""企业质量与证据置信度测试（分行业画像、缺失不填 0、两个维度独立）。"""
from __future__ import annotations

from datetime import date

from app.fundamentals.quality import (
    METRIC_SPECS,
    MIN_COVERAGE,
    STALE_LIMIT_DAYS,
    QualityInputs,
    assess_quality,
    evidence_confidence,
    profile_for,
)

GOOD = QualityInputs(
    roe=18.0,
    gross_margin=45.0,
    net_margin=18.0,
    debt_ratio=30.0,
    revenue_yoy=20.0,
    profit_yoy=25.0,
    ocf_to_profit=1.2,
    goodwill_to_equity=1.0,
    report_date=date(2026, 6, 30),
    as_of=date(2026, 9, 14),
    industry="白色家电",
    source="eastmoney",
)


def test_profile_selection_is_by_industry_with_explicit_reason():
    assert profile_for("银行")[0].key == "financial"
    assert profile_for("电力行业")[0].key == "utility"
    assert profile_for("半导体")[0].key == "growth"
    assert profile_for("白色家电")[0].key == "general"
    profile, reason = profile_for(None)
    assert profile.key == "general" and "缺失" in reason
    _, reason2 = profile_for("某不存在的行业")
    assert "未命中" in reason2


def test_financial_profile_excludes_meaningless_metrics():
    bank = QualityInputs(
        roe=13.0,
        net_margin=30.0,
        revenue_yoy=8.0,
        profit_yoy=9.0,
        ocf_to_profit=0.9,
        gross_margin=None,      # 银行没有毛利率，缺失不应算作数据缺口
        debt_ratio=None,        # 银行的资产负债率天然很高，本画像不使用
        report_date=date(2026, 6, 30),
        as_of=date(2026, 9, 14),
        industry="银行",
    )
    result = assess_quality(bank)
    assert result.profile_key == "financial"
    assert result.coverage == 1.0          # 被排除的指标不计入覆盖率
    assert result.missing == ()
    assert result.score is not None
    assert "gross_margin" not in result.subscores
    assert "不使用" in result.profile_explain


def test_missing_metric_is_excluded_not_scored_zero():
    full = assess_quality(GOOD)
    assert full.score == 100.0 and full.coverage == 1.0

    partial = assess_quality(QualityInputs(**{**GOOD.__dict__, "gross_margin": None,
                                             "goodwill_to_equity": None}))
    # 剔除缺失项后重新归一 → 分不应下降（0 分才会下降）
    assert partial.score == 100.0
    assert partial.coverage < 1.0
    assert set(partial.missing) == {"gross_margin", "goodwill_to_equity"}
    assert partial.subscores["gross_margin"]["value"] is None
    assert partial.subscores["gross_margin"]["score"] is None
    assert "未计入加权" in partial.subscores["gross_margin"]["note"]


def test_too_many_missing_metrics_yields_insufficient_evidence():
    thin = QualityInputs(roe=10.0, report_date=date(2026, 6, 30), as_of=date(2026, 9, 14),
                         industry="白色家电")
    result = assess_quality(thin)
    assert result.coverage < MIN_COVERAGE
    assert result.insufficient_evidence is True
    assert result.score is None
    assert result.grade == "证据不足"
    assert any("证据不足" in note for note in result.notes)


def test_direction_of_debt_ratio_is_reversed():
    low = assess_quality(QualityInputs(**{**GOOD.__dict__, "debt_ratio": 25.0}))
    high = assess_quality(QualityInputs(**{**GOOD.__dict__, "debt_ratio": 80.0}))
    assert low.subscores["debt_ratio"]["score"] > high.subscores["debt_ratio"]["score"]
    assert low.subscores["debt_ratio"]["direction"] == "越小越好"
    assert low.subscores["roe"]["direction"] == "越大越好"


def test_earnings_quality_warning_is_raised_and_origin_is_labelled():
    suspicious = assess_quality(
        QualityInputs(**{**GOOD.__dict__, "ocf_to_profit": 0.2, "profit_yoy": 35.0})
    )
    assert any("盈利质量存疑" in note for note in suspicious.notes)
    # 事实 / 模型推断 必须标注
    assert suspicious.subscores["roe"]["origin"] == "事实"
    assert suspicious.subscores["ocf_to_profit"]["origin"] == "模型推断"
    # 每个子分数都要能追溯阈值
    assert suspicious.subscores["roe"]["threshold_good"] == 15.0


def test_quality_score_is_never_presented_as_probability():
    result = assess_quality(GOOD)
    assert any("不是上涨概率" in note for note in result.notes)
    assert set(METRIC_SPECS) >= set(result.subscores)


def test_confidence_is_independent_from_quality_and_expires():
    fresh = evidence_confidence(
        coverage=1.0,
        report_date=date(2026, 6, 30),
        as_of=date(2026, 9, 14),
        industry_identified=True,
        has_statement_detail=True,
        sources=2,
    )
    assert fresh.expired is False
    assert fresh.label == "较高"
    assert fresh.components["freshness"] == 30.0
    assert "不是上涨概率" in fresh.to_dict()["note"]

    stale = evidence_confidence(
        coverage=1.0,
        report_date=date(2024, 12, 31),
        as_of=date(2026, 9, 14),
        industry_identified=True,
    )
    assert stale.expired is True
    assert stale.label == "过期"
    assert stale.score < fresh.score
    assert any("停止判断" in reason for reason in stale.reasons)

    thin = evidence_confidence(
        coverage=0.2,
        report_date=date(2026, 6, 30),
        as_of=date(2026, 9, 14),
        industry_identified=False,
    )
    assert thin.score < fresh.score
    assert any("行业未识别" in reason for reason in thin.reasons)
    assert any("未接入三表明细" in reason for reason in thin.reasons)


def test_confidence_without_dates_does_not_pretend_to_be_fresh():
    unknown = evidence_confidence(
        coverage=1.0, report_date=None, as_of=None, industry_identified=True
    )
    assert unknown.components["freshness"] == 0.0
    assert unknown.components["stale_days"] == -1.0
    assert unknown.expired is False          # 无法判断时效 ≠ 已过期
    assert any("缺失" in reason for reason in unknown.reasons)


def test_stale_limit_boundary():
    exactly = evidence_confidence(
        coverage=1.0,
        report_date=date(2026, 9, 14) - __import__("datetime").timedelta(days=STALE_LIMIT_DAYS),
        as_of=date(2026, 9, 14),
        industry_identified=True,
    )
    assert exactly.expired is False
    beyond = evidence_confidence(
        coverage=1.0,
        report_date=date(2026, 9, 14) - __import__("datetime").timedelta(days=STALE_LIMIT_DAYS + 1),
        as_of=date(2026, 9, 14),
        industry_identified=True,
    )
    assert beyond.expired is True
