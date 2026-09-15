"""基本面接口测试（TestClient + 内存库；不联网）。"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.database.session import get_db
from app.fundamentals import eastmoney_fundamentals as ef
from app.fundamentals.eastmoney_fundamentals import FetchStats, parse_fundamentals
from app.fundamentals.repository import upsert_snapshots
from app.fundamentals.statement_details import StatementDetail
from app.api.research import _merge_dual_review

#: 000333 的真实接口返回值（已用 akshare 财务摘要交叉核对）
MEIDE = {
    "f12": "000333", "f14": "美的集团", "f2": 87.02, "f20": 663904738662,
    "f21": 599166776423, "f9": 12.55, "f23": 3.13, "f37": 11.33,
    "f40": 261052379000.0, "f41": 3.4561222865, "f45": 26446037000.0,
    "f46": 1.661997971068, "f49": 25.2557645483, "f57": 64.8900151015,
    "f100": "白色家电", "f112": 3.466361973, "f113": 27.816806308,
    "f129": 10.2225117134, "f135": 225792941000.0, "f221": 20260630,
}

VALUATION_BODY = {
    "revenue": 261052379000.0,
    "revenue_growth": 0.06,
    "fcf_margin": 0.09,
    "discount_rate": 0.10,
    "terminal_growth": 0.02,
    "shares": 7629500000.0,
    "net_debt": 0.0,
    "years": 5,
    "basis": "单元测试假设（估计）",
    "bear_overrides": {"revenue_growth": 0.0, "fcf_margin": 0.06},
    "bull_overrides": {"revenue_growth": 0.12, "fcf_margin": 0.12},
}


@pytest.fixture
def client(db_session):
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def seeded(db_session):
    upsert_snapshots(db_session, parse_fundamentals([MEIDE]), snapshot_date=date(2026, 9, 14))
    return db_session


def test_coverage_on_empty_database_is_honest(client):
    body = client.get("/api/fundamentals/coverage").json()
    assert body["rows"] == 0 and body["symbols"] == 0
    assert body["latest_snapshot_date"] is None
    assert "不做插补" in body["note"]


def test_missing_symbol_returns_404_not_a_guess(client):
    resp = client.get("/api/fundamentals/000333")
    assert resp.status_code == 404
    assert "不会" in resp.json()["detail"]


def test_symbol_endpoint_returns_snapshot_quality_and_confidence(client, seeded):
    resp = client.get("/api/fundamentals/000333")
    assert resp.status_code == 200
    body = resp.json()
    snapshot = body["snapshot"]
    assert snapshot["symbol"] == "000333"
    assert snapshot["report_date"] == "2026-06-30"
    assert snapshot["currency"] == "CNY"
    assert snapshot["snapshot_date"] == "2026-09-14"
    # 派生值必须标注为模型推断
    assert "模型推断" in snapshot["derived"]["origin"]
    assert abs(snapshot["derived"]["pe_from_statements"] / 12.55 - 1.0) < 0.001
    # 质量与置信度分开返回
    assert body["quality"]["score"] is not None
    assert body["evidence_confidence"]["score"] > 0
    assert "不是上涨概率" in body["evidence_confidence"]["note"]
    assert any("显式假设" in note for note in body["notes"])


def test_statement_refresh_enriches_quality_and_confidence(client, seeded, monkeypatch):
    async def fake_statement(_symbol):
        return StatementDetail(
            symbol="000333",
            report_date=date(2026, 6, 30),
            operating_cash_flow=37_552_090_000.0,
            capital_expenditure=2_634_210_000.0,
            monetary_funds=90_642_783_000.0,
            short_loan=47_305_038_000.0,
            long_loan=15_417_300_000.0,
            bonds_payable=6_665_757_000.0,
            noncurrent_liab_due_year=5_063_161_000.0,
            lease_liabilities=1_919_149_000.0,
            goodwill=31_576_620_000.0,
            statement_equity=225_792_941_000.0,
        )

    monkeypatch.setattr("app.api.fundamentals.fetch_statement_detail", fake_statement)
    response = client.post("/api/fundamentals/000333/statement-detail/refresh")
    assert response.status_code == 200
    body = response.json()
    detail = body["statement_detail"]
    assert detail["free_cash_flow"] == 34_917_880_000.0
    assert detail["identified_net_debt"] < 0
    assert detail["goodwill_to_equity"] > 0
    assert body["quality"]["coverage"] == 1.0
    assert body["evidence_confidence"]["score"] > 85.0

    loaded = client.get("/api/fundamentals/000333").json()
    assert loaded["snapshot"]["statement_detail"]["ocf_to_profit"] > 1.0
    analysis = client.post(
        "/api/fundamentals/000333/analysis", json={"valuation": VALUATION_BODY}
    ).json()
    assert not any("三表明细" in item for item in analysis["6_open_items"]["unverified"])
    assert "已识别净负债" in analysis["3_dimensions"]["portfolio_risk"]["leverage"]


def test_analysis_without_assumptions_refuses_value_judgement(client, seeded):
    resp = client.post("/api/fundamentals/000333/analysis", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["1_conclusion"]["conclusion_key"] == "need_valuation"
    assert body["3_dimensions"]["valuation"]["available"] is False
    assert body["wording_guard"] == []


def test_analysis_with_assumptions_produces_scenarios_and_no_promise(client, seeded):
    resp = client.post(
        "/api/fundamentals/000333/analysis",
        json={
            "valuation": VALUATION_BODY,
            "portfolio": {
                "max_loss_per_trade": 0.02,
                "max_symbol_weight": 0.15,
                "industry_weight": 0.2,
                "max_industry_weight": 0.3,
                "horizon": "3 年以上",
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [s["label"] for s in body["5_scenarios"]["scenarios"]] == ["悲观", "基准", "乐观"]
    assert body["wording_guard"] == []
    assert body["1_conclusion"]["horizon"] == "3 年以上"
    assert body["3_dimensions"]["market_expectation"]["available"] is False


def test_valuation_endpoint_rejects_impossible_assumptions(client, seeded):
    bad = {**VALUATION_BODY, "discount_rate": 0.02, "terminal_growth": 0.05}
    resp = client.post("/api/fundamentals/000333/valuation", json=bad)
    assert resp.status_code == 422
    assert "永续增长" in resp.json()["detail"]


def test_valuation_endpoint_computes_band_upside_and_sensitivity(client, seeded):
    resp = client.post(
        "/api/fundamentals/000333/valuation",
        json={**VALUATION_BODY, "sensitivity_growth": [0.0, 0.06],
              "sensitivity_discount": [0.08, 0.10]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["errors"] == {}
    assert body["price"] == 87.02
    assert set(body["upside_vs_price"]) == {"悲观", "基准", "乐观"}
    assert body["margin_of_safety_base"] is not None
    assert len(body["sensitivity"]["rows"]) == 2
    assert body["basis"] == VALUATION_BODY["basis"]
    assert "不代为假设" in body["basis_note"]


def test_reverse_valuation_returns_price_implied_growth(client, seeded):
    body = {
        key: value
        for key, value in VALUATION_BODY.items()
        if key not in {"revenue_growth", "bear_overrides", "bull_overrides"}
    }
    resp = client.post("/api/fundamentals/000333/reverse-valuation", json=body)
    assert resp.status_code == 200
    result = resp.json()
    assert result["status"] == "solved"
    assert result["target_price"] == 87.02
    assert result["implied_growth"] is not None
    assert result["revenue_caliber"]["annualization_factor_applied"] == 2.0
    assert "不是增长预测" in result["note"]


def test_refresh_stores_rows_and_reports_completeness(client, db_session, monkeypatch):
    rows, stats = parse_fundamentals([MEIDE]), FetchStats()
    stats.pages_fetched = 1
    stats.rows_raw = 1
    stats.rows_parsed = 1
    stats.total_reported = 1

    async def fake_fetch(**kwargs):
        return rows, stats

    monkeypatch.setattr("app.api.fundamentals.fetch_market_fundamentals", fake_fetch)
    resp = client.post("/api/fundamentals/refresh", json={"max_pages": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["fetched"]["complete"] is True
    assert body["stored"]["inserted"] == 1
    assert body["coverage"]["rows"] == 1


def test_refresh_refuses_to_store_or_conclude_when_upstream_empty(client, monkeypatch):
    async def empty_fetch(**kwargs):
        stats = FetchStats()
        stats.failures.append("page 1 全部主机失败: RemoteProtocolError")
        return [], stats

    monkeypatch.setattr("app.api.fundamentals.fetch_market_fundamentals", empty_fetch)
    resp = client.post("/api/fundamentals/refresh", json={"max_pages": 1})
    assert resp.status_code == 502
    assert "不做任何入库" in resp.json()["detail"]


def test_partial_fetch_is_reported_as_incomplete(client, monkeypatch):
    rows = parse_fundamentals([MEIDE])
    stats = FetchStats()
    stats.pages_fetched = 1
    stats.rows_parsed = 1
    stats.total_reported = 5913
    stats.failures.append("page 2 全部主机失败")

    async def partial_fetch(**kwargs):
        return rows, stats

    monkeypatch.setattr("app.api.fundamentals.fetch_market_fundamentals", partial_fetch)
    body = client.post("/api/fundamentals/refresh", json={"max_pages": 2}).json()
    assert body["fetched"]["complete"] is False
    assert body["fetched"]["failures"]


def test_research_run_freezes_inputs_analysis_and_fingerprint(client, seeded):
    response = client.post(
        "/api/research/runs?symbol=000333",
        json={
            "valuation": VALUATION_BODY,
            "horizon": "3 年以上",
            "question": "只引用已有证据",
            "include_explanation": False,
        },
    )
    assert response.status_code == 200
    created = response.json()
    assert created["symbol"] == "000333"
    assert created["assumptions"]["valuation"]["discount_rate"] == 0.10
    assert created["analysis"]["1_conclusion"]["conclusion_key"]
    assert created["reverse_valuation"]["status"] == "solved"
    assert created["explanation_status"] == "not_requested"
    assert len(created["fingerprint"]) == 64
    assert "不提供修改接口" in created["immutability_note"]

    listing = client.get("/api/research/runs?symbol=000333").json()
    assert listing["count"] == 1
    assert listing["items"][0]["fingerprint"] == created["fingerprint"]
    loaded = client.get(f"/api/research/runs/{created['id']}").json()
    assert loaded["analysis"] == created["analysis"]


def test_research_run_requires_existing_snapshot(client):
    response = client.post(
        "/api/research/runs?symbol=999999",
        json={"valuation": VALUATION_BODY, "include_explanation": False},
    )
    assert response.status_code == 404


def test_dual_review_only_passes_when_both_independent_reviews_pass():
    passed = {"status": "ok", "text": "有据主审", "validation": {"passed": True}}
    critic = {"status": "ok", "text": "有据反方", "validation": {"passed": True}}
    merged = _merge_dual_review(passed, critic)
    assert merged["status"] == "ok"
    assert merged["validation"]["passed"] is True
    assert "主审意见" in merged["text"] and "独立反方意见" in merged["text"]

    failed = _merge_dual_review(passed, {"status": "error", "text": ""})
    assert failed["status"] == "error"
    assert failed["validation"]["passed"] is False


def test_request_fields_only_verified_ones():
    assert "f132" not in ef.REQUEST_FIELDS.split(",")
    assert "f9" in ef.REQUEST_FIELDS.split(",")
