"""自动复核提醒测试：扫描落库、幂等、确认、与单条复核结果一致、调度器配置。"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.database.models import ResearchReviewReminder
from app.database.session import get_db
from app.fundamentals.eastmoney_fundamentals import parse_fundamentals
from app.fundamentals.repository import upsert_snapshots
from app.research.service import (
    acknowledge_reminder,
    latest_runs_by_symbol,
    list_reminders,
    review_run,
    scan_reminders,
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


def _seed_snapshot(db, price: float = 87.02, report_date: int = 20260630):
    upsert_snapshots(
        db, parse_fundamentals([{**MEIDE, "f2": price, "f221": report_date}]),
        snapshot_date=TODAY,
    )


def _create_run(db, symbol: str = "000333", growth: float = 0.06):
    """直接写一条冻结研究记录（避开 HTTP，便于纯服务层测试）。"""
    from app.api.fundamentals import AnalysisRequest, PortfolioContextRequest, ValuationRequest
    from app.api.research import post_analysis
    from app.database.models import InvestmentResearchRun
    from app.fundamentals.repository import latest_snapshot

    valuation = ValuationRequest(
        revenue=522104758000.0, revenue_growth=growth, fcf_margin=0.09,
        discount_rate=0.10, terminal_growth=0.02, shares=7629500000.0,
        net_debt=0.0, years=5, basis="测试假设（估计）", revenue_basis="annualized",
    )
    analysis = post_analysis(symbol, AnalysisRequest(
        valuation=valuation, portfolio=PortfolioContextRequest(horizon="3 年以上")), db)
    snapshot = latest_snapshot(db, symbol)
    row = InvestmentResearchRun(
        symbol=symbol, name=snapshot.name, snapshot_date=snapshot.snapshot_date,
        report_date=snapshot.report_date, price=snapshot.price, source=snapshot.source,
        conclusion_key=str(analysis["1_conclusion"]["conclusion_key"]),
        explanation_status="not_requested", fingerprint=f"fp-{growth}-{symbol}",
        assumptions={"valuation": valuation.model_dump(), "horizon": "3 年以上"},
        analysis=analysis, reverse_valuation={}, explanation=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_scan_creates_reminders_only_when_triggered(db_session):
    # 现价 200 远高于基准情景价值（约 93）→ 价格偏离必须触发
    _seed_snapshot(db_session, price=200.0)
    _create_run(db_session)
    result = scan_reminders(db_session, today=TODAY)
    assert result["symbols_scanned"] == 1
    assert result["reminders_created"] == 1
    reminders = list_reminders(db_session)
    assert reminders["count"] == 1
    assert reminders["items"][0]["trigger_name"] == "price_deviation"
    assert reminders["items"][0]["severity"] == "high"
    assert "偏离" in reminders["items"][0]["detail"]


def test_scan_creates_nothing_when_price_is_close_to_value(db_session):
    """阈值内不应产生提醒（否则提醒会被噪声淹没）。"""
    _seed_snapshot(db_session, price=90.0)
    _create_run(db_session)
    result = scan_reminders(db_session, today=TODAY)
    assert result["symbols_scanned"] == 1
    assert result["reminders_created"] == 0
    assert list_reminders(db_session)["count"] == 0


def test_scan_is_idempotent_within_the_same_day(db_session):
    _seed_snapshot(db_session, price=200.0)
    _create_run(db_session)
    first = scan_reminders(db_session, today=TODAY)
    second = scan_reminders(db_session, today=TODAY)
    assert first["reminders_created"] == 1
    assert second["reminders_created"] == 0           # 同日重复扫描不新增
    assert list_reminders(db_session)["count"] == 1


def test_new_report_period_produces_a_second_reminder(db_session):
    _seed_snapshot(db_session, price=200.0)
    _create_run(db_session)
    scan_reminders(db_session, today=TODAY)
    # 上游出现新报告期 → 触发第二个条件
    _seed_snapshot(db_session, price=200.0, report_date=20260930)
    result = scan_reminders(db_session, today=TODAY)
    assert result["reminders_created"] == 1
    names = {item["trigger_name"] for item in list_reminders(db_session)["items"]}
    assert {"price_deviation", "new_report_period"} <= names


def test_acknowledge_removes_from_todo_but_keeps_history(db_session):
    _seed_snapshot(db_session, price=200.0)
    _create_run(db_session)
    scan_reminders(db_session, today=TODAY)
    reminder_id = list_reminders(db_session)["items"][0]["id"]

    assert acknowledge_reminder(db_session, reminder_id)["ok"] is True
    todo = list_reminders(db_session)
    assert todo["count"] == 0
    assert todo["unacknowledged_count"] == 0
    # 历史仍在（include_acknowledged=True），且不可重复确认出错
    history = list_reminders(db_session, include_acknowledged=True)
    assert history["count"] == 1
    assert history["items"][0]["acknowledged"] is True
    assert history["items"][0]["acknowledged_at"]
    assert acknowledge_reminder(db_session, reminder_id)["ok"] is True


def test_acknowledge_unknown_id_reports_failure(db_session):
    result = acknowledge_reminder(db_session, 999999)
    assert result["ok"] is False
    assert "未找到" in result["reason"]


def test_missing_snapshot_is_skipped_not_silently_clean(db_session):
    _seed_snapshot(db_session, price=87.02)
    row = _create_run(db_session)
    # 删掉快照（模拟数据缺失）→ 扫描必须如实跳过并说明原因
    from app.database.models import FundamentalSnapshot

    db_session.query(FundamentalSnapshot).delete()
    db_session.commit()
    result = scan_reminders(db_session, today=TODAY)
    assert result["symbols_scanned"] == 0
    assert result["reminders_created"] == 0
    assert len(result["skipped"]) == 1
    assert "没有" in result["skipped"][0]["reason"]
    assert row.symbol == result["skipped"][0]["symbol"]


def test_latest_runs_by_symbol_keeps_only_newest(db_session):
    _seed_snapshot(db_session, price=87.02)
    first = _create_run(db_session, growth=0.06)
    second = _create_run(db_session, growth=0.10)
    latest = latest_runs_by_symbol(db_session)
    assert [row.id for row in latest] == [second.id]
    assert first.id != second.id


def test_service_review_matches_api_review(db_session):
    """服务层与 API 层必须给出同一结论（防止两条实现漂移）。"""
    _seed_snapshot(db_session, price=87.02)
    row = _create_run(db_session)
    service_result = review_run(db_session, row, today=TODAY)

    from app.api.research import review_investment_research_run

    api_result = review_investment_research_run(row.id, db_session)
    assert service_result["needs_review"] == api_result["needs_review"]
    assert (
        {t["name"] for t in service_result["fired_triggers"]}
        == {t["name"] for t in api_result["fired_triggers"]}
    )


# ── 接口 ───────────────────────────────────────────────────────────────────


@pytest.fixture
def client(db_session):
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    # 现价刻意远离基准情景价值（约 93），让价格偏离条件在接口测试里稳定触发
    _seed_snapshot(db_session, price=200.0)
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def test_reminder_endpoints_scan_list_and_acknowledge(client, db_session):
    _create_run(db_session)
    scan = client.post("/api/research/reminders/scan")
    assert scan.status_code == 200
    assert scan.json()["reminders_created"] == 1

    listed = client.get("/api/research/reminders").json()
    assert listed["count"] == 1
    reminder_id = listed["items"][0]["id"]

    ack = client.post(f"/api/research/reminders/{reminder_id}/acknowledge")
    assert ack.status_code == 200 and ack.json()["ok"] is True
    assert client.get("/api/research/reminders").json()["count"] == 0
    assert client.get(
        "/api/research/reminders?include_acknowledged=true"
    ).json()["count"] == 1

    missing = client.post("/api/research/reminders/999999/acknowledge")
    assert missing.status_code == 404


def test_empty_scan_is_reported_honestly(client):
    body = client.post("/api/research/reminders/scan").json()
    assert body["symbols_scanned"] == 0
    assert body["reminders_created"] == 0
    assert client.get("/api/research/reminders").json()["count"] == 0


# ── 调度器 ─────────────────────────────────────────────────────────────────


def test_scheduler_describe_reports_config_without_starting():
    from app.config import Settings
    from app.tasks.research_reminder_scheduler import ResearchReminderScheduler

    settings = Settings()
    scheduler = ResearchReminderScheduler(db_factory=lambda: None, settings=settings)
    info = scheduler.describe()
    assert info["enabled"] is True          # 只读 + 只写提醒表 → 默认开启
    assert info["hour"] == 15 and info["minute"] == 45
    assert info["running"] is False
    assert "不下单" in info["scope"]


def test_scheduler_respects_disable_switch():
    from app.config import Settings
    from app.tasks.research_reminder_scheduler import ResearchReminderScheduler

    scheduler = ResearchReminderScheduler(
        db_factory=lambda: None,
        settings=Settings(research_reminder_auto_enabled=False),
    )
    scheduler.start()
    assert scheduler.is_running is False
