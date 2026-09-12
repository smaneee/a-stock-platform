"""涨停板情绪池落库与情绪因子接口测试（全部用桩服务，不联网）。"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import get_limit_up_service
from app.database.models import LimitUpPoolMember, LimitUpSentiment
from app.main import app
from app.market_data.eastmoney_limit_up import POOLS, LimitUpResult
from app.market_data.limit_up_store import LimitUpSentimentStore


def _item(
    symbol: str,
    *,
    boards: int | None = None,
    seal_amount: float = 1.0e8,
    amount: float = 2.0e8,
) -> dict:
    """造一行「情绪池输出名」口径的数据（与上游解析结果一致）。"""
    row: dict = {
        "symbol": symbol,
        "name": f"测试{symbol}",
        "price": 10.0,
        "change_pct": 10.0,
        "amount": amount,
        "turnover_rate": 5.0,
        "seal_amount": seal_amount,
        "first_seal_time": "09:31:00",
        "last_seal_time": "14:00:00",
        "industry": "测试行业",
        "is_new_high": True,
    }
    if boards is not None:
        row["boards"] = boards
        row["limit_up_stat"] = f"{boards}天{boards}板"
    return row


class _StubLimitUpService:
    """只实现 query_all 的桩服务。"""

    def __init__(self, pools: dict[str, list[dict]] | None = None):
        self._pools = pools or {}
        self.calls: list[tuple[str, date | None]] = []

    async def query_all(self, pool, *, trade_date=None, max_pages=20):
        self.calls.append((pool, trade_date))
        items = [dict(item) for item in self._pools.get(pool, [])]
        return LimitUpResult(
            pool=POOLS[pool],
            trade_date=trade_date.isoformat() if trade_date else "",
            total=len(items),
            page=1,
            items=items,
        )

    async def close(self) -> None:
        return None


def _pools() -> dict[str, list[dict]]:
    return {
        "limit-up": [
            _item("000001", boards=1, seal_amount=3.0e8),
            _item("000002", boards=3, seal_amount=2.0e8),
            _item("000003", boards=5, seal_amount=1.0e8),
        ],
        "broken-board": [_item("000004"), _item("000005")],
        "limit-down": [_item("000006")],
        "strong": [_item("000007"), _item("000008")],
        "sub-new": [_item("000009")],
    }


async def test_capture_computes_sentiment_and_members(db_session):
    store = LimitUpSentimentStore(db_session, _StubLimitUpService(_pools()))
    result = await store.capture(date(2026, 9, 11))

    assert result is not None
    assert result.trade_date == date(2026, 9, 11)
    assert result.pool_counts["limit-up"] == 3
    assert result.member_count == 9

    row = db_session.get(LimitUpSentiment, date(2026, 9, 11))
    assert row is not None
    assert row.limit_up_count == 3
    assert row.limit_down_count == 1
    assert row.broken_board_count == 2
    assert row.strong_count == 2
    assert row.sub_new_count == 1
    # 封板率 = 3 / (3 + 2)
    assert row.seal_rate == pytest.approx(0.6)
    assert row.broken_rate == pytest.approx(0.4)
    assert row.max_streak == 5
    assert row.first_board_count == 1
    assert row.streak_3_count == 1
    assert row.streak_5plus_count == 1
    assert row.total_seal_amount == pytest.approx(6.0e8)
    assert row.total_limit_up_amount == pytest.approx(6.0e8)

    members = list(db_session.scalars(select(LimitUpPoolMember)).all())
    assert len(members) == 9
    top = next(item for item in members if item.symbol == "000003")
    assert top.pool == "limit-up"
    assert top.boards == 5
    assert top.seal_amount == pytest.approx(1.0e8)
    assert top.limit_up_stat == "5天5板"
    assert top.payload is not None


async def test_capture_is_idempotent(db_session):
    store = LimitUpSentimentStore(db_session, _StubLimitUpService(_pools()))
    await store.capture(date(2026, 9, 11))
    await store.capture(date(2026, 9, 11))
    db_session.commit()

    assert db_session.scalar(select(LimitUpSentiment).where(
        LimitUpSentiment.trade_date == date(2026, 9, 11)
    )) is not None
    members = list(db_session.scalars(select(LimitUpPoolMember)).all())
    assert len(members) == 9

    # 第二次换一组数据，明细应被整组覆盖而不是追加
    store2 = LimitUpSentimentStore(
        db_session, _StubLimitUpService({"limit-up": [_item("000010", boards=2)]})
    )
    await store2.capture(date(2026, 9, 11))
    members = list(db_session.scalars(select(LimitUpPoolMember)).all())
    assert [item.symbol for item in members] == ["000010"]
    row = db_session.get(LimitUpSentiment, date(2026, 9, 11))
    assert row.limit_up_count == 1
    assert row.broken_board_count == 0
    assert row.seal_rate == pytest.approx(1.0)


async def test_capture_skips_day_without_data(db_session):
    store = LimitUpSentimentStore(db_session, _StubLimitUpService({}))
    assert await store.capture(date(2026, 9, 11)) is None
    assert list(db_session.scalars(select(LimitUpSentiment)).all()) == []
    assert list(db_session.scalars(select(LimitUpPoolMember)).all()) == []


async def test_backfill_skips_weekend_days(db_session):
    service = _StubLimitUpService(_pools())
    store = LimitUpSentimentStore(db_session, service)
    # 2026-09-05 / 09-06 是周六周日
    results = await store.backfill(3, end=date(2026, 9, 7), delay=0)

    assert [item.trade_date for item in results] == [
        date(2026, 9, 4),
        date(2026, 9, 7),
    ]
    requested = sorted({call[1] for call in service.calls})
    assert date(2026, 9, 5) not in requested
    assert date(2026, 9, 6) not in requested


async def test_history_filters_and_orders(db_session):
    store = LimitUpSentimentStore(db_session, _StubLimitUpService(_pools()))
    for day in (date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)):
        await store.capture(day)

    rows = store.history(date(2026, 9, 10), date(2026, 9, 11), limit=10)
    assert [row.trade_date for row in rows] == [
        date(2026, 9, 10),
        date(2026, 9, 11),
    ]
    assert store.latest().trade_date == date(2026, 9, 11)
    assert store.has_data() is True


@pytest.fixture(autouse=True)
def _clear_dependency_overrides():
    """桩依赖只在本文件内生效，避免污染后续测试文件共享的 app 实例。"""
    yield
    app.dependency_overrides.pop(get_limit_up_service, None)


def _client(service) -> TestClient:
    app.dependency_overrides[get_limit_up_service] = lambda: service
    client = TestClient(app)
    client.__enter__()
    return client


def test_sentiment_endpoint_returns_empty_before_capture():
    client = _client(_StubLimitUpService())
    try:
        resp = client.get("/api/market/limit-up/sentiment")
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["items"] == []
    assert body["latest"] is None


def test_capture_then_read_sentiment_curve():
    client = _client(_StubLimitUpService(_pools()))
    try:
        captured = client.post(
            "/api/market/limit-up/capture", json={"trade_date": "2026-09-11"}
        )
        assert captured.status_code == 200
        assert captured.json()["count"] == 1
        listed = client.get("/api/market/limit-up/sentiment")
    finally:
        client.__exit__(None, None, None)

    assert listed.status_code == 200
    body = listed.json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["trade_date"] == "2026-09-11"
    assert item["limit_up_count"] == 3
    assert item["broken_board_count"] == 2
    assert item["seal_rate"] == pytest.approx(0.6)
    assert item["max_streak"] == 5
    assert body["latest"]["trade_date"] == "2026-09-11"


def test_capture_returns_404_when_upstream_has_no_data():
    client = _client(_StubLimitUpService())
    try:
        resp = client.post(
            "/api/market/limit-up/capture", json={"trade_date": "2026-09-11"}
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 404


def test_capture_backfill_days_fills_recent_weekdays():
    client = _client(_StubLimitUpService(_pools()))
    try:
        resp = client.post(
            "/api/market/limit-up/capture",
            json={"trade_date": "2026-09-11", "backfill_days": 3},
        )
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    dates = [item["trade_date"] for item in resp.json()["items"]]
    # 2026-09-08 ~ 09-11 都是工作日，周末两天被跳过
    assert dates == ["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]


def test_sentiment_endpoint_validates_limit():
    client = _client(_StubLimitUpService())
    try:
        resp = client.get("/api/market/limit-up/sentiment", params={"limit": 0})
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 422


def test_pool_endpoint_accepts_trade_date():
    class _PooledService(_StubLimitUpService):
        def __init__(self):
            super().__init__(_pools())
            self.queries: list[tuple] = []

        async def query(self, pool, *, limit=50, page=1, order=None, trade_date=None):
            self.queries.append((pool, trade_date))
            items = [dict(item) for item in self._pools.get(pool, [])]
            return LimitUpResult(
                pool=POOLS[pool],
                trade_date=trade_date.isoformat() if trade_date else "",
                total=len(items),
                page=1,
                items=items[:limit],
            )

    service = _PooledService()
    client = _client(service)
    try:
        resp = client.get(
            "/api/market/limit-up/limit-up", params={"trade_date": "2026-09-10"}
        )
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    assert resp.json()["trade_date"] == "2026-09-10"
    assert service.queries == [("limit-up", date(2026, 9, 10))]
