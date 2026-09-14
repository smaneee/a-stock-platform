"""GET /api/market/session：今天是不是交易日。

用户反馈「今天不是交易日，为什么股票池显示可交易」——股票池的「可交易」是
排除规则的结果（退市 / 停牌 / 长期停牌 / ST），与当天下不下单无关。本接口
把「今天能不能成交」单独暴露出来，供前端标注休市状态。
"""
from __future__ import annotations

from datetime import date

import pytest


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _seed_calendar(days: list[date]) -> None:
    """把 app 引擎里的交易日历重置为指定日期（启动同步可能已写入真实数据）。"""
    from app.database.models import TradingDate
    from app.database.session import SessionLocal

    db = SessionLocal()
    try:
        db.query(TradingDate).delete()
        for item in days:
            db.add(TradingDate(trade_date=item))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    # 不用 with：不触发 lifespan（避免测试里跑真实的交易日历网络同步）。
    # 交易日历由 _seed_calendar 显式准备。
    return _client()


def test_weekend_reports_not_trading_day(client):
    """周六查询：is_trading_day=False，回落到上一个交易日。"""
    _seed_calendar([date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 14)])

    response = client.get("/api/market/session?day=2026-09-12")

    assert response.status_code == 200
    data = response.json()
    assert data["day"] == "2026-09-12"
    assert data["is_trading_day"] is False
    assert data["last_trading_day"] == "2026-09-11"
    assert data["next_trading_day"] == "2026-09-14"
    assert data["calendar_total"] == 3


def test_trading_day_reports_itself(client):
    """交易日查询：is_trading_day=True，last 就是当天。"""
    _seed_calendar([date(2026, 9, 11), date(2026, 9, 14)])

    data = client.get("/api/market/session?day=2026-09-11").json()

    assert data["is_trading_day"] is True
    assert data["last_trading_day"] == "2026-09-11"
    assert data["next_trading_day"] == "2026-09-14"


def test_default_day_is_today(client):
    """不传 day 时按今天算（今天是不是交易日都返回 200）。"""
    today = date.today()
    _seed_calendar([today])

    data = client.get("/api/market/session").json()

    assert data["day"] == today.isoformat()
    assert data["is_trading_day"] is True


def test_next_trading_day_none_when_calendar_ends(client):
    """日历覆盖到最近交易日为止时，next_trading_day 返回 None 而不是 500。"""
    _seed_calendar([date(2026, 9, 11)])

    data = client.get("/api/market/session?day=2026-09-12").json()

    assert data["next_trading_day"] is None


def test_empty_calendar_returns_503(client):
    """交易日历为空 → 503，禁止静默给出错误结论。"""
    _seed_calendar([])

    response = client.get("/api/market/session?day=2026-09-12")

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "calendar_unavailable"
