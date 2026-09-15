"""S4 验收测试：自动复核与组合联动的四条硬要求。

| 验收要求 | 本文件对应用例 |
| --- | --- |
| 重复事件不重复提醒 | `test_unresolved_condition_is_updated_not_duplicated`、`test_acknowledged_then_recurring_condition_opens_a_new_item` |
| 模型失败不会解锁增仓 | `test_model_failure_does_not_unlock_position_increase` |
| 历史记录不可覆盖 | `test_frozen_run_is_immutable_and_has_no_update_endpoint` |
| 生成研究与草案前后订单数量不变 | `test_research_run_creation_does_not_touch_paper_orders` |
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.database.models import InvestmentResearchRun, PaperOrder, PaperTrade
from app.database.session import get_db
from app.fundamentals.eastmoney_fundamentals import parse_fundamentals
from app.fundamentals.repository import upsert_snapshots
from app.research.service import acknowledge_reminder, list_reminders, scan_reminders

MEIDE = {
    "f12": "000333", "f14": "美的集团", "f2": 200.0, "f20": 663904738662,
    "f21": 599166776423, "f9": 12.55, "f23": 3.13, "f37": 11.33,
    "f40": 261052379000.0, "f41": 3.4561222865, "f45": 26446037000.0,
    "f46": 1.661997971068, "f49": 25.2557645483, "f57": 64.8900151015,
    "f100": "白色家电", "f112": 3.466361973, "f113": 27.816806308,
    "f129": 10.2225117134, "f135": 225792941000.0, "f221": 20260630,
}
DAY1 = date(2026, 9, 15)
DAY2 = date(2026, 9, 16)


def _seed(db, price: float = 200.0) -> None:
    upsert_snapshots(db, parse_fundamentals([{**MEIDE, "f2": price}]), snapshot_date=DAY1)


def _create_run(db, growth: float = 0.06):
    """直接写一条冻结记录（含通过契约的决策卡），避开模型调用。"""
    from app.database.models import FundamentalSnapshot
    from app.fundamentals.repository import latest_snapshot

    snapshot = latest_snapshot(db, "000333")
    analysis = {
        "1_conclusion": {"conclusion_key": "research_candidate", "evidence_confidence_score": 85.0},
        "5_scenarios": {"scenarios": [{"label": "基准", "result": {"per_share": 92.84}}]},
        "6_open_items": {"invalidation_conditions": ["净资产收益率跌破 3.0% → 论点失效"],
                         "review_triggers": ["下一期定期报告发布后"]},
        "3_dimensions": {"valuation": {"applicability": {"applicable": True}}},
    }
    row = InvestmentResearchRun(
        symbol="000333", name="美的集团", snapshot_date=snapshot.snapshot_date,
        report_date=snapshot.report_date, price=snapshot.price, source=snapshot.source,
        conclusion_key="research_candidate", explanation_status="not_requested",
        fingerprint="c" * 64,
        assumptions={
            "valuation": {
                "revenue": 522104758000.0, "revenue_growth": growth,
                "fcf_margin": 0.1338, "discount_rate": 0.10, "terminal_growth": 0.02,
                "shares": 7629500000.0, "net_debt": -14272378000.0, "years": 5,
                "basis": "S4 验收假设（估计）", "revenue_basis": "annualized",
            },
            "horizon": "3 年以上",
        },
        analysis=analysis, reverse_valuation={}, explanation=None,
        thesis_card={
            "symbol": "000333", "name": "美的集团", "strategy_type": "quality_value",
            "strategy_label": "长期质量价值", "horizon": "3 年以上",
            "return_source": "企业盈利增长与估值修复", "as_of": "报告期 2026-06-30",
            "thesis": "", "variant_view": "", "supporting_evidence_ids": [],
            "opposing_evidence_ids": [], "assumptions": {},
            "valuation_method": {"available": True, "applicable": True},
            "scenario_result_ids": [], "invalidation_conditions": [],
            "review_triggers": [], "missing_data": [],
            "decision": "research_candidate", "decision_label": "研究候选（非买入建议）",
            "confidence_basis": {}, "model_version": "fundamentals-decision-v1",
            "prompt_version": "investment-thesis-s1", "citations_valid": True,
            "citation_report": {}, "gaps": [],
            "opposing_incomplete": False, "opposing_opinions": [], "cannot_answer": [],
            "shared_evidence_ids": [], "model_usage": {}, "evidence_refs": [],
            "text_origin": "deterministic_skeleton",
        },
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ── ① 重复事件不重复提醒 ───────────────────────────────────────────────────


def test_unresolved_condition_is_updated_not_duplicated(db_session):
    """同一个未确认的条件连续多天检出 → 只有一条待办，文案被更新。"""
    _seed(db_session)
    _create_run(db_session)

    first = scan_reminders(db_session, today=DAY1)
    assert first["reminders_created"] >= 1        # 价格偏离必然触发（200 vs 基准 92.84）
    assert first["reminders_updated"] == 0
    created_first = first["reminders_created"]
    triggers = {item["trigger_name"] for item in list_reminders(db_session)["items"]}
    assert "price_deviation" in triggers

    second = scan_reminders(db_session, today=DAY2)
    assert second["reminders_created"] == 0, "未确认的同一条件不得再新增一条"
    assert second["reminders_updated"] == created_first

    todo = list_reminders(db_session)
    assert todo["count"] == created_first
    assert todo["unacknowledged_count"] == created_first


def test_acknowledged_then_recurring_condition_opens_a_new_item(db_session):
    """确认过的条件若再次触发 → 允许新开一条，便于看出"处理过、又发生了"。"""
    _seed(db_session)
    _create_run(db_session)
    first = scan_reminders(db_session, today=DAY1)
    opened = first["reminders_created"]
    assert opened >= 1
    for item in list_reminders(db_session)["items"]:
        assert acknowledge_reminder(db_session, item["id"])["ok"] is True
    assert list_reminders(db_session)["count"] == 0

    again = scan_reminders(db_session, today=DAY2)
    assert again["reminders_created"] == opened, "已确认过的条件再次触发 → 允许新开一条"
    history = list_reminders(db_session, include_acknowledged=True)
    assert history["count"] == opened * 2
    assert [item["acknowledged"] for item in history["items"]].count(True) == opened


def test_scan_writes_nothing_outside_the_reminder_table(db_session):
    """自动扫描只能写提醒表：不得动研究记录、成交、订单。"""
    _seed(db_session)
    run = _create_run(db_session)
    before = (db_session.query(InvestmentResearchRun).count(),
              db_session.query(PaperTrade).count(),
              db_session.query(PaperOrder).count())
    scan_reminders(db_session, today=DAY1)
    after = (db_session.query(InvestmentResearchRun).count(),
             db_session.query(PaperTrade).count(),
             db_session.query(PaperOrder).count())
    assert before == after
    assert db_session.get(InvestmentResearchRun, run.id).fingerprint == "c" * 64


# ── ② 模型失败不会解锁增仓 ────────────────────────────────────────────────


def test_model_failure_does_not_unlock_position_increase():
    from app.research.thesis import bind_critic_output, card_from_payload, position_gate

    base_card = {
        "symbol": "000333", "name": "美的集团", "strategy_type": "quality_value",
        "strategy_label": "长期质量价值", "horizon": "3 年以上",
        "return_source": "企业盈利增长与估值修复", "as_of": "报告期 2026-06-30",
        "thesis": "", "variant_view": "", "supporting_evidence_ids": ["fact:roe"],
        "opposing_evidence_ids": [], "assumptions": {},
        "valuation_method": {"available": True, "applicable": True},
        "scenario_result_ids": [], "invalidation_conditions": [], "review_triggers": [],
        "missing_data": [], "decision": "research_candidate",
        "decision_label": "研究候选（非买入建议）", "confidence_basis": {},
        "model_version": "v", "prompt_version": "p", "citations_valid": True,
        "citation_report": {}, "gaps": [], "model_usage": {}, "evidence_refs": [],
        "text_origin": "model_primary_and_critic",
    }
    # 反方调用失败（没有结构化输出）→ 卡片判为不完整 → 门禁必须拦截
    binding = bind_critic_output(None, __import__("app.explain.deepseek", fromlist=["EvidencePack"]).EvidencePack(
        "000333", "美的集团", "now"), error="模型调用超时")
    card = card_from_payload({**base_card, **{
        "opposing_incomplete": True, "critic_report": binding,
    }})
    gate = position_gate(card)
    assert gate["allowed"] is False
    assert any("反方审查未通过结构化契约" in item for item in gate["blockers"])


# ── ③ 历史记录不可覆盖 ────────────────────────────────────────────────────


@pytest.fixture
def client(db_session):
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def test_frozen_run_is_immutable_and_has_no_update_endpoint(client, db_session):
    _seed(db_session)
    run = _create_run(db_session)
    before = client.get(f"/api/research/runs/{run.id}").json()

    # 没有任何写接口可以改历史记录
    routes = [
        getattr(route, "path", "")
        for route in __import__("app.main", fromlist=["app"]).app.routes
    ]
    # 研究记录只有读接口 + 复核/草案两个只读动作，没有任何改名/改写端点
    write_like = [
        path for path in routes
        if path.startswith("/api/research/runs")
        and path.rstrip("/").endswith(("update", "edit", "delete", "patch"))
    ]
    assert write_like == [], write_like

    # 复核是只读的：跑一次复核后记录内容逐字节不变
    client.get(f"/api/research/runs/{run.id}/review")
    after = client.get(f"/api/research/runs/{run.id}").json()
    assert before == after


# ── ④ 生成研究与草案前后订单数量不变 ─────────────────────────────────────


def test_research_run_creation_does_not_touch_paper_orders(client, db_session):
    _seed(db_session)
    orders_before = db_session.query(PaperOrder).count()
    trades_before = db_session.query(PaperTrade).count()

    resp = client.post(
        "/api/research/runs?symbol=000333",
        json={
            "valuation": {
                "revenue": 522104758000.0, "revenue_growth": 0.06, "fcf_margin": 0.1338,
                "discount_rate": 0.10, "terminal_growth": 0.02, "shares": 7629500000.0,
                "net_debt": -14272378000.0, "years": 5, "basis": "S4 验收（估计）",
                "revenue_basis": "annualized",
            },
            "horizon": "3 年以上",
            "include_explanation": False,
        },
    )
    assert resp.status_code == 200, resp.text
    assert db_session.query(PaperOrder).count() == orders_before
    assert db_session.query(PaperTrade).count() == trades_before
    # 冻结记录里必须带上决策卡（后续草案门禁据此判断）
    assert resp.json()["thesis_card"] is not None

def test_scan_survives_a_run_with_incomplete_frozen_assumptions(db_session):
    """历史记录假设字段不全时，定时扫描必须如实跳过而不是整轮崩掉。"""
    _seed(db_session)
    run = _create_run(db_session)
    run.assumptions = {"valuation": {"revenue_growth": 0.06}}   # 模拟早期版本/人工补录
    db_session.commit()

    result = scan_reminders(db_session, today=DAY1)
    assert result["symbols_scanned"] == 0
    assert result["reminders_created"] == 0
    assert len(result["skipped"]) == 1
    assert "没有" in result["skipped"][0]["reason"]
