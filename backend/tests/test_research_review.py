"""研究记录复核测试：差异对比与复核触发条件（纯函数 + 接口）。"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.database.session import get_db
from app.fundamentals.eastmoney_fundamentals import parse_fundamentals
from app.fundamentals.repository import upsert_snapshots
from app.research.review import (
    PRICE_DEVIATION_TRIGGER,
    STALE_REPORT_DAYS,
    STALE_RUN_DAYS,
    build_review,
    diff_runs,
    evaluate_triggers,
)

MEIDE = {
    "f12": "000333", "f14": "美的集团", "f2": 87.02, "f20": 663904738662,
    "f21": 599166776423, "f9": 12.55, "f23": 3.13, "f37": 11.33,
    "f40": 261052379000.0, "f41": 3.4561222865, "f45": 26446037000.0,
    "f46": 1.661997971068, "f49": 25.2557645483, "f57": 64.8900151015,
    "f100": "白色家电", "f112": 3.466361973, "f113": 27.816806308,
    "f129": 10.2225117134, "f135": 225792941000.0, "f221": 20260630,
}

TODAY = date(2026, 9, 15)


def _run(**overrides) -> dict:
    """一条冻结研究记录（``_detail()`` 形状）。"""
    base = {
        "id": 1,
        "symbol": "000333",
        "name": "美的集团",
        "snapshot_date": "2026-09-14",
        "report_date": "2026-06-30",
        "price": 87.02,
        "source": "eastmoney",
        "conclusion_key": "fair",
        "explanation_status": "ok",
        "assumptions": {
            "valuation": {
                "revenue": 522104758000.0, "revenue_growth": 0.06, "fcf_margin": 0.09,
                "discount_rate": 0.10, "terminal_growth": 0.02, "shares": 7629500000.0,
                "net_debt": 0.0, "years": 5,
            },
            "horizon": "3 年以上",
        },
        "analysis": {
            "1_conclusion": {"conclusion_key": "fair", "evidence_confidence_score": 85.0},
            "3_dimensions": {
                "quality": {"score": 55.1},
                "valuation": {"upside_vs_price": {"基准": 0.07}},
            },
            "4_evidence": {
                "support": [{"evidence": "净资产收益率 11.33%"}],
                "oppose": [{"evidence": "资产负债率 64.89%"}],
            },
            "5_scenarios": {"scenarios": [
                {"label": "悲观", "result": {"per_share": 48.07}},
                {"label": "基准", "result": {"per_share": 92.84}},
                {"label": "乐观", "result": {"per_share": 157.93}},
            ]},
            "6_open_items": {"invalidation_conditions": ["归母净利润同比转负（当前为正）→ 需重新评估"]},
        },
    }
    base.update(overrides)
    return base


def _snapshot(**overrides) -> dict:
    base = {
        "symbol": "000333", "name": "美的集团",
        "snapshot_date": "2026-09-15", "report_date": "2026-06-30",
        "price": 87.02, "source": "eastmoney",
    }
    base.update(overrides)
    return base


# ── 差异对比 ───────────────────────────────────────────────────────────────


def test_identical_runs_report_no_change():
    diff = diff_runs(_run(), _run())
    assert diff["changed_count"] == 0
    assert diff["changes"] == []
    assert diff["summary"] == "与上次研究相比没有变化"


def test_price_and_conclusion_changes_are_high_importance():
    current = _run(
        id=2, price=120.0, conclusion_key="overvalued",
        analysis={**_run()["analysis"],
                  "1_conclusion": {"conclusion_key": "overvalued", "evidence_confidence_score": 85.0}},
    )
    diff = diff_runs(_run(), current)
    fields = {c["field"]: c for c in diff["changes"]}
    assert fields["price"]["before"] == 87.02 and fields["price"]["after"] == 120.0
    assert fields["price"]["direction"] == "up"
    assert fields["price"]["importance"] == "high"
    assert fields["conclusion_key"]["importance"] == "high"
    assert diff["material_changes"]
    assert all(c["importance"] == "high" for c in diff["material_changes"])


def test_assumption_change_is_detected_from_nested_valuation():
    changed = _run()
    changed["assumptions"]["valuation"]["discount_rate"] = 0.12
    changed["assumptions"]["valuation"]["fcf_margin"] = 0.12
    diff = diff_runs(_run(), changed)
    fields = {c["field"]: c for c in diff["changes"]}
    assert fields["discount_rate"]["before"] == 0.10
    assert fields["discount_rate"]["after"] == 0.12
    assert fields["fcf_margin"]["direction"] == "up"


def test_scenario_values_and_evidence_sets_are_compared():
    changed = _run()
    changed["analysis"]["5_scenarios"]["scenarios"][1]["result"]["per_share"] = 60.0
    changed["analysis"]["4_evidence"]["support"] = [{"evidence": "新证据：自由现金流 349 亿"}]
    changed["analysis"]["6_open_items"]["invalidation_conditions"] = ["现价高于基准价值 → 安全边际消失"]
    diff = diff_runs(_run(), changed)
    fields = {c["field"]: c for c in diff["changes"]}
    assert fields["base_per_share"]["before"] == 92.84
    assert fields["base_per_share"]["after"] == 60.0
    assert fields["support_evidence"]["direction"] == "changed"
    assert "新增 1 条、移除 1 条" in fields["support_evidence"]["note"]
    assert fields["invalidation_conditions"]["note"].startswith("新增 1 条")


# ── 复核触发条件 ───────────────────────────────────────────────────────────


def _triggers(run: dict | None = None, *, snapshot: dict | None = None,
              analysis: dict | None = None, today: date = TODAY):
    run = run or _run()
    snapshot = snapshot or _snapshot()
    analysis = analysis or {"1_conclusion": {"conclusion_key": run["conclusion_key"]}}
    return {t.name: t for t in evaluate_triggers(
        run, current_snapshot=snapshot, current_analysis=analysis, today=today)}


def test_no_trigger_when_nothing_changed_same_day():
    triggers = _triggers()
    assert [t.name for t in triggers.values() if t.fired] == []


def test_new_report_period_fires_high():
    triggers = _triggers(snapshot=_snapshot(report_date="2026-09-30"))
    fired = triggers["new_report_period"]
    assert fired.fired is True and fired.severity == "high"
    assert "2026-06-30" in fired.detail and "2026-09-30" in fired.detail


def test_price_deviation_threshold_fires_both_directions():
    band = 92.84
    up = _triggers(snapshot=_snapshot(price=round(band * (1 + PRICE_DEVIATION_TRIGGER), 2)))
    assert up["price_deviation"].fired is True
    down = _triggers(snapshot=_snapshot(price=round(band * (1 - PRICE_DEVIATION_TRIGGER), 2)))
    assert down["price_deviation"].fired is True
    inside = _triggers(snapshot=_snapshot(price=round(band * 1.05, 2)))
    assert inside["price_deviation"].fired is False
    # 缺现价时不能假装"没触发"，而是说明无法判断
    unknown = _triggers(snapshot=_snapshot(price=None))
    assert unknown["price_deviation"].fired is False
    assert "无法判断" in unknown["price_deviation"].detail


def test_conclusion_change_and_expiry_fire():
    changed = _triggers(analysis={"1_conclusion": {"conclusion_key": "attractive"}})
    assert changed["conclusion_changed"].fired is True
    expired = _triggers(today=date(2026, 6, 30) + timedelta(days=STALE_REPORT_DAYS + 1))
    assert expired["report_expired"].fired is True
    stale_run = _triggers(today=date(2026, 9, 14) + timedelta(days=STALE_RUN_DAYS + 1))
    assert stale_run["run_stale"].fired is True
    assert stale_run["run_stale"].severity == "medium"


def test_missing_report_date_is_reported_not_assumed_fresh():
    run = _run(report_date=None)
    triggers = _triggers(run, snapshot=_snapshot(report_date=None))
    assert triggers["report_expired"].fired is False
    assert "记录未含报告期" in triggers["report_expired"].detail


# ── 合成复核结论 ───────────────────────────────────────────────────────────


def test_build_review_marks_needs_review_only_when_triggered():
    calm = build_review(_run(), previous=_run(), current_snapshot=_snapshot(),
                        current_analysis={"1_conclusion": {"conclusion_key": "fair"}}, today=TODAY)
    assert calm["needs_review"] is False
    assert calm["has_previous_run"] is True
    assert calm["fired_triggers"] == []

    loud = build_review(_run(), previous=_run(),
                        current_snapshot=_snapshot(price=200.0),
                        current_analysis={"1_conclusion": {"conclusion_key": "overvalued"}},
                        today=TODAY)
    assert loud["needs_review"] is True
    names = {t["name"] for t in loud["fired_triggers"]}
    assert {"price_deviation", "conclusion_changed"} <= names
    assert "不代表观点对错" in loud["note"]


def test_build_review_without_previous_run_compares_against_recomputed_current():
    review = build_review(_run(), previous=None, current_snapshot=_snapshot(price=95.0),
                          current_analysis={"1_conclusion": {"conclusion_key": "fair"}},
                          today=TODAY)
    assert review["has_previous_run"] is False
    assert review["diff"]["baseline"] == "no_previous_run"
    fields = {c["field"] for c in review["diff"]["changes"]}
    assert "price" in fields


# ── 接口 ───────────────────────────────────────────────────────────────────


@pytest.fixture
def client(db_session):
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    upsert_snapshots(db_session, parse_fundamentals([MEIDE]), snapshot_date=date(2026, 9, 15))
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def _create_run(client, *, growth: float = 0.06, price_note: str = "") -> dict:
    body = {
        "valuation": {
            "revenue": 522104758000.0, "revenue_growth": growth, "fcf_margin": 0.09,
            "discount_rate": 0.10, "terminal_growth": 0.02, "shares": 7629500000.0,
            "net_debt": -14272378000.0, "years": 5, "basis": "测试假设（估计）",
            "revenue_basis": "annualized",
        },
        "horizon": "3 年以上",
        "include_explanation": False,
    }
    resp = client.post("/api/research/runs?symbol=000333", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_review_endpoint_404_for_unknown_run(client):
    assert client.get("/api/research/runs/9999/review").status_code == 404


def test_review_endpoint_reviews_single_run_without_previous(client):
    created = _create_run(client)
    resp = client.get(f"/api/research/runs/{created['id']}/review")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run"]["id"] == created["id"]
    assert body["has_previous_run"] is False
    assert "current" in body and body["current"]["snapshot"]["symbol"] == "000333"
    assert body["all_triggers"], "必须返回全部触发条件（含未触发）"
    assert "不构成投资建议" in body["disclaimer"]


def test_review_endpoint_detects_assumption_change_between_runs(client):
    first = _create_run(client, growth=0.06)
    second = _create_run(client, growth=0.10)
    assert second["id"] != first["id"]

    body = client.get(f"/api/research/runs/{second['id']}/review").json()
    assert body["has_previous_run"] is True
    assert body["previous_run"]["id"] == first["id"]
    fields = {c["field"]: c for c in body["diff"]["changes"]}
    # 第二次把增长率从 6% 提到 10% → 必须被识别为假设变化
    assert fields["revenue_growth"]["before"] == 0.06
    assert fields["revenue_growth"]["after"] == 0.10
    # 且基准情景价值必须随之上升（方向一致性）
    assert fields["base_per_share"]["direction"] == "up"

    # 两次记录都不可变
    again = client.get(f"/api/research/runs/{first['id']}").json()
    assert again["assumptions"]["valuation"]["revenue_growth"] == 0.06


# ── SQLite 写锁重试（由 2026-09-15 实测的 500 驱动） ─────────────────────────


def test_commit_retries_on_sqlite_lock_then_succeeds(monkeypatch):
    from app.api import research as research_api
    from app.database.session import get_db as _get_db  # noqa: F401 - 说明依赖来源

    calls = {"n": 0}

    class _FakeSession:
        def commit(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError("COMMIT", {}, Exception("database is locked"))

        def rollback(self):
            calls["rolled_back"] = calls.get("rolled_back", 0) + 1

    monkeypatch.setattr(research_api.time, "sleep", lambda _s: None)
    research_api._commit_with_lock_retry(_FakeSession())
    assert calls["n"] == 2            # 第一次被锁挡回，第二次成功
    assert calls["rolled_back"] == 1  # 失败后必须回滚，避免脏事务


def test_commit_does_not_swallow_real_errors(monkeypatch):
    from app.api import research as research_api

    class _BadSession:
        def commit(self):
            raise OperationalError("COMMIT", {}, Exception("no such table: nope"))

        def rollback(self):
            pass

    monkeypatch.setattr(research_api.time, "sleep", lambda _s: None)
    with pytest.raises(OperationalError):
        research_api._commit_with_lock_retry(_BadSession())


def test_commit_gives_up_after_max_attempts(monkeypatch):
    from app.api import research as research_api

    calls = {"n": 0}

    class _AlwaysLocked:
        def commit(self):
            calls["n"] += 1
            raise OperationalError("COMMIT", {}, Exception("database is locked"))

        def rollback(self):
            pass

    monkeypatch.setattr(research_api.time, "sleep", lambda _s: None)
    with pytest.raises(OperationalError):
        research_api._commit_with_lock_retry(_AlwaysLocked(), attempts=3)
    assert calls["n"] == 3

