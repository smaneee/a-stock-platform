"""S3 数据完整性审计测试：每个检查都能用构造数据触发（真造违规，验证真被抓到）。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from app.database.models import (
    ForwardObservation,
    PaperAccount,
    HistoricalBar,
    InvestmentResearchRun,
    PaperTrade,
    TradingDate,
)
from app.research.integrity import (
    MIN_SAMPLES_FOR_PASSED,
    check_bars_on_trading_days,
    check_evidence_contract,
    check_forward_observations,
    check_paper_trades_on_trading_days,
    check_research_run_time_validity,
    run_audit,
)

D1 = date(2026, 9, 10)
D2 = date(2026, 9, 11)
#: 2026-09-12/13 是周末 → 不是交易日
SATURDAY = date(2026, 9, 12)


def _seed_calendar(db, days=(D1, D2)) -> None:
    for day in days:
        db.add(TradingDate(trade_date=day))
    db.commit()


def _account(db) -> int:
    """paper_trades.account_id 有外键约束 → 先建一个账户。"""
    account = PaperAccount(name="审计测试账户", initial_cash=1_000_000,
                           available_cash=1_000_000, frozen_cash=0)
    db.add(account)
    db.commit()
    db.refresh(account)
    return account.id


# ── 成交必须在交易日 ───────────────────────────────────────────────────────


def test_trades_on_non_trading_day_are_caught(db_session):
    _seed_calendar(db_session)
    account_id = _account(db_session)
    db_session.add_all([
        PaperTrade(account_id=account_id, order_id=1, symbol="000333", side="buy",
                   quantity=100, price=10, executed_at=datetime(2026, 9, 11, 14, 30)),
        PaperTrade(account_id=account_id, order_id=2, symbol="000333", side="buy",
                   quantity=100, price=10, executed_at=datetime(2026, 9, 12, 14, 30)),
    ])
    db_session.commit()
    result = check_paper_trades_on_trading_days(db_session)
    assert result.checked == 2
    assert result.ok is False
    assert any("2026-09-12" in item for item in result.violations)


def test_trade_without_time_is_unverifiable_not_passing():
    """库里该列是 NOT NULL（NULL 不可构造）→ 用纯函数直接验证防御分支。"""
    from app.research.integrity import evaluate_trade_days

    violations = evaluate_trade_days(
        [(1, "000333", None), (2, "000333", datetime(2026, 9, 11, 14, 30))],
        {D1, D2},
    )
    assert len(violations) == 1
    assert "无法验证" in violations[0]
    assert "000333" in violations[0]


def test_trade_timestamps_are_not_nullable_in_schema(db_session):
    """schema 层面的保证：成交时间不允许为空（因此审计里那条分支是防御性的）。"""
    _seed_calendar(db_session)
    account_id = _account(db_session)
    db_session.add(PaperTrade(account_id=account_id, order_id=1, symbol="000333",
                              side="buy", quantity=100, price=10,
                              executed_at=datetime(2026, 9, 11, 14, 30)))
    db_session.commit()
    stored = db_session.query(PaperTrade).one()
    assert stored.executed_at is not None


# ── K 线必须在交易日 ───────────────────────────────────────────────────────


def test_bars_outside_calendar_are_caught(db_session):
    _seed_calendar(db_session)
    db_session.add_all([
        HistoricalBar(symbol="000333", period="daily", adjust="qfq", trade_date=D2,
                      open=1, high=1, low=1, close=1, volume=1, amount=1, source="t"),
        HistoricalBar(symbol="000333", period="daily", adjust="qfq", trade_date=SATURDAY,
                      open=1, high=1, low=1, close=1, volume=1, amount=1, source="t"),
    ])
    db_session.commit()
    result = check_bars_on_trading_days(db_session)
    assert result.ok is False
    assert any("2026-09-12" in item for item in result.violations)


# ── 前向观察计数 ───────────────────────────────────────────────────────────


def test_forward_observation_count_must_match_calendar(db_session):
    _seed_calendar(db_session, days=(D1, D2, date(2026, 9, 14)))
    db_session.add(ForwardObservation(
        freeze_tag="t-good", started_on=D1, target_trading_days=60,
        trading_days_counted=2, last_counted_day=date(2026, 9, 14),
        notes="正确：D1 之后到 09-14 之间有两个交易日",
    ))
    db_session.add(ForwardObservation(
        freeze_tag="t-bad", started_on=D1, target_trading_days=60,
        trading_days_counted=9, last_counted_day=D2,
        notes="错误：计数与日历不符",
    ))
    db_session.commit()
    result = check_forward_observations(db_session, today=date(2026, 9, 15))
    assert result.checked == 2
    assert any("t-bad" in item and "不一致" in item for item in result.violations)
    assert not any("t-good" in item for item in result.violations)


def test_forward_observation_last_day_cannot_be_in_the_future(db_session):
    _seed_calendar(db_session, days=(D1, D2))
    db_session.add(ForwardObservation(
        freeze_tag="t-future", started_on=D1, target_trading_days=60,
        trading_days_counted=1, last_counted_day=date(2026, 10, 1),
    ))
    db_session.commit()
    result = check_forward_observations(db_session, today=date(2026, 9, 15))
    assert any("晚于今天" in item for item in result.violations)


def test_counted_days_without_last_day_is_a_violation(db_session):
    _seed_calendar(db_session)
    db_session.add(ForwardObservation(
        freeze_tag="t-nolast", started_on=D1, target_trading_days=60,
        trading_days_counted=3, last_counted_day=None,
    ))
    db_session.commit()
    result = check_forward_observations(db_session, today=date(2026, 9, 15))
    assert any("t-nolast" in item for item in result.violations)


# ── 研究记录时点自洽（防未来数据） ─────────────────────────────────────────


def test_research_run_with_future_snapshot_is_caught(db_session):
    db_session.add(InvestmentResearchRun(
        symbol="000333", name="测试", snapshot_date=date(2026, 9, 20),
        report_date=date(2026, 6, 30), price=10, source="test", conclusion_key="fair",
        fingerprint="a" * 64, assumptions={}, analysis={}, reverse_valuation={},
        created_at=datetime(2026, 9, 15, 10, 0),
    ))
    db_session.commit()
    result = check_research_run_time_validity(db_session)
    assert result.ok is False
    assert any("未来数据" in item for item in result.violations)


def test_research_run_created_at_is_not_nullable_so_time_is_always_traceable(db_session):
    """``created_at`` 是 NOT NULL：ORM 层面写不进 NULL，因此缺少创建时间的记录**不可能存在**。

    这条用例固化该前提（而不是去测一个不可构造的分支）：即使显式传 None，
    数据库也会用默认值/约束兜住，从而保证"时点可追溯"不是一句空话。
    """
    row = InvestmentResearchRun(
        symbol="000333", name="测试", snapshot_date=date(2026, 9, 1),
        report_date=date(2026, 6, 30), price=10, source="test", conclusion_key="fair",
        fingerprint="b" * 64, assumptions={}, analysis={}, reverse_valuation={},
        created_at=None,
    )
    db_session.add(row)
    db_session.commit()
    stored = db_session.get(InvestmentResearchRun, row.id)
    assert stored.created_at is not None
    assert check_research_run_time_validity(db_session).ok is True


# ── 证据页：负结果必须保留、样本不足不得标"已通过" ─────────────────────


def _write_evidence(tmp_path, items) -> "object":
    path = tmp_path / "strategy-evidence.json"
    path.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    return path


def test_evidence_without_negative_results_is_a_violation(tmp_path):
    path = _write_evidence(tmp_path, [
        {"id": "a", "status": "passed_oos", "sample_size": 100, "evidence_source": "x",
         "data_cutoff": "2026-09-01"},
    ])
    result = check_evidence_contract(path)
    assert any("没有任何负结果" in item for item in result.violations)


def test_evidence_passed_without_enough_samples_is_a_violation(tmp_path):
    path = _write_evidence(tmp_path, [
        {"id": "ok", "status": "failed_oos", "failure_reason": "样本外为负",
         "evidence_source": "x", "data_cutoff": "2026-09-01"},
        {"id": "too-small", "status": "passed_oos", "sample_size": 5,
         "evidence_source": "x", "data_cutoff": "2026-09-01"},
        {"id": "no-samples", "status": "passed_oos", "evidence_source": "x",
         "data_cutoff": "2026-09-01"},
    ])
    result = check_evidence_contract(path)
    joined = " ".join(result.violations)
    assert "too-small" in joined and str(MIN_SAMPLES_FOR_PASSED) in joined
    assert "no-samples" in joined and "没有样本量字段" in joined


def test_evidence_negative_without_reason_is_a_violation(tmp_path):
    path = _write_evidence(tmp_path, [
        {"id": "silent-failure", "status": "failed_oos", "evidence_source": "x",
         "data_cutoff": "2026-09-01"},
    ])
    result = check_evidence_contract(path)
    assert any("没有写失败原因" in item for item in result.violations)


def test_evidence_missing_source_is_unverifiable(tmp_path):
    path = _write_evidence(tmp_path, [
        {"id": "no-source", "status": "inconclusive", "failure_reason": "样本不足",
         "data_cutoff": "2026-09-01"},
    ])
    result = check_evidence_contract(path)
    assert result.unverifiable >= 1
    assert any("evidence_source" in item for item in result.violations)


def test_production_ready_without_passed_status_is_a_violation(tmp_path):
    path = _write_evidence(tmp_path, [
        {"id": "liar", "status": "failed_oos", "failure_reason": "负", "production_ready": True,
         "evidence_source": "x", "data_cutoff": "2026-09-01"},
    ])
    result = check_evidence_contract(path)
    assert any("production_ready" in item for item in result.violations)


# ── 汇总 ───────────────────────────────────────────────────────────────────


def test_run_audit_reports_clean_when_nothing_wrong(tmp_path, db_session):
    _seed_calendar(db_session, days=(D1, D2))
    db_session.add(HistoricalBar(symbol="000333", period="daily", adjust="qfq",
                                 trade_date=D2, open=1, high=1, low=1, close=1,
                                 volume=1, amount=1, source="t"))
    db_session.commit()
    path = _write_evidence(tmp_path, [
        {"id": "failed-one", "status": "failed_oos", "failure_reason": "样本外为负",
         "evidence_source": "x", "data_cutoff": "2026-09-01"},
    ])
    report = run_audit(db_session, today=date(2026, 9, 15), evidence_path=path)
    assert report["clean"] is True, report["checks"]
    assert report["violation_count"] == 0
    assert len(report["checks"]) == 5
    assert "不视为通过" in report["note"]


def test_run_audit_aggregates_violations_and_stays_read_only(tmp_path, db_session):
    _seed_calendar(db_session)
    account_id = _account(db_session)
    db_session.add(PaperTrade(account_id=account_id, order_id=1, symbol="000333",
                              side="buy", quantity=100, price=10,
                              executed_at=datetime(2026, 9, 12, 14, 30)))  # 周六
    db_session.commit()
    before = db_session.query(PaperTrade).count()
    report = run_audit(db_session, today=date(2026, 9, 15), evidence_path=tmp_path / "missing.json")
    assert report["clean"] is False
    assert report["violation_count"] >= 2          # 成交违规 + 证据文件缺失
    assert db_session.query(PaperTrade).count() == before   # 审计不写库
