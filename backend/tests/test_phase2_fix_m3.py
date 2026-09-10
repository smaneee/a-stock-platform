"""M3 修正提交测试：组合回测 API + 后台 worker。

通过 API + TestClient 验证全链路行为，避免依赖 db_session 注入（与
全局 SessionLocal 使用不同内存 SQLite 实例）。
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.session import Base


def _build_test_db():
    """为 M3 测试构造独立的内存 SQLite + StaticPool，可被 API + worker 共享。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def patched_app(monkeypatch):
    """构造一个新 app，并把 SessionLocal + get_db 都指向测试内存数据库。"""
    from app.database import session as db_session_module
    from app.database.session import get_db
    from app.main import create_app

    engine = _build_test_db()
    TestSessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )

    # 直接覆盖 SessionLocal
    monkeypatch.setattr(db_session_module, "SessionLocal", TestSessionLocal)

    app = create_app()

    # 每个组件的 noop stub 必须与其真实接口签名一致：start 同步/异步、stop 异步
    class _NoopBacktestWorker:
        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        @property
        def is_running(self) -> bool:
            return True

    class _NoopSettlementScheduler:
        # SettlementScheduler.start() 在真实实现中是同步方法（APScheduler.start）
        # 测试替身必须保持同步签名，否则会出现 "coroutine was never awaited"
        def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        @property
        def is_running(self) -> bool:
            return True

    app.state.backtest_worker = _NoopBacktestWorker()
    app.state.portfolio_backtest_worker = _NoopBacktestWorker()
    app.state.settlement_scheduler = _NoopSettlementScheduler()

    def _override_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db

    return app, TestSessionLocal


@pytest.fixture
def client(patched_app):
    app, _ = patched_app
    with TestClient(app) as c:
        yield c


def _payload(**overrides):
    base = {
        "symbols": ["600000", "000001"],
        "strategy_name": "ma_cross",
        "weights": {"600000": 0.5, "000001": 0.5},
        "benchmark_symbol": "000300",
        "start_time": "2024-01-01T00:00:00+00:00",
        "end_time": "2024-06-01T00:00:00+00:00",
        "initial_cash": 100000.0,
        "idempotency_key": str(uuid.uuid4()),
    }
    base.update(overrides)
    return base


class TestPortfolioBacktestAPI:
    """组合回测 API：创建、查询、取消、幂等、列表。"""

    def test_create_returns_201_and_queued(self, client):
        resp = client.post(
            "/api/portfolio-backtests", json=_payload(idempotency_key="create-api")
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] > 0
        assert body["status"] == "queued"
        assert body["strategy_name"] == "ma_cross"

    def test_create_rejects_invalid_symbol(self, client):
        resp = client.post(
            "/api/portfolio-backtests",
            json=_payload(symbols=["6XX000"], idempotency_key="bad-sym"),
        )
        assert resp.status_code == 422

    def test_create_rejects_unknown_weight_symbol(self, client):
        resp = client.post(
            "/api/portfolio-backtests",
            json=_payload(weights={"999999": 1.0}, idempotency_key="bad-wt"),
        )
        assert resp.status_code == 422

    def test_idempotency_returns_existing_task(self, client):
        key = "duplicate-m3-test"
        first = client.post(
            "/api/portfolio-backtests", json=_payload(idempotency_key=key)
        ).json()
        second = client.post(
            "/api/portfolio-backtests", json=_payload(idempotency_key=key)
        ).json()
        assert second["id"] == first["id"]

    def test_get_returns_task_detail(self, client):
        created = client.post(
            "/api/portfolio-backtests",
            json=_payload(idempotency_key="get-m3"),
        ).json()
        resp = client.get(f"/api/portfolio-backtests/{created['id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == created["id"]
        assert body["status"] == "queued"

    def test_get_404_for_missing(self, client):
        resp = client.get("/api/portfolio-backtests/9999999")
        assert resp.status_code == 404

    def test_cancel_queued_task(self, client):
        created = client.post(
            "/api/portfolio-backtests",
            json=_payload(idempotency_key="cancel-m3"),
        ).json()
        resp = client.post(f"/api/portfolio-backtests/{created['id']}/cancel")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "cancelled"
        assert body["finished_at"] is not None

    def test_cancel_succeeded_returns_409(self, patched_app):
        """已 succeeded 的任务不允许取消。"""
        from app.database.models import PortfolioBacktest

        app, TestSessionLocal = patched_app
        db = TestSessionLocal()
        try:
            task = PortfolioBacktest(
                symbols=json.dumps(["600000"]),
                strategy_name="ma_cross",
                start_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
                end_time=datetime(2024, 6, 1, tzinfo=timezone.utc),
                initial_cash=100000,
                status="succeeded",
                idempotency_key="pre-succeeded",
            )
            db.add(task)
            db.commit()
            db.refresh(task)
            tid = task.id
        finally:
            db.close()
        with TestClient(app) as c:
            resp = c.post(f"/api/portfolio-backtests/{tid}/cancel")
            assert resp.status_code == 409

    def test_list_returns_recent(self, client):
        client.post(
            "/api/portfolio-backtests", json=_payload(idempotency_key="list-1")
        )
        client.post(
            "/api/portfolio-backtests", json=_payload(idempotency_key="list-2")
        )
        resp = client.get("/api/portfolio-backtests?limit=5")
        assert resp.status_code == 200
        tasks = resp.json()["tasks"]
        assert len(tasks) >= 2
        for t in tasks:
            assert "id" in t
            assert "status" in t


class TestPortfolioBacktestWorkerLifecycle:
    """PortfolioBacktestWorker 启动恢复遗留任务为 queued。"""

    def test_recover_stale_marks_queued(self, patched_app):
        from app.database.models import PortfolioBacktest
        from app.tasks.portfolio_worker import PortfolioBacktestWorker

        app, TestSessionLocal = patched_app
        db = TestSessionLocal()
        try:
            task = PortfolioBacktest(
                symbols=json.dumps(["600000"]),
                strategy_name="ma_cross",
                start_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
                end_time=datetime(2024, 6, 1, tzinfo=timezone.utc),
                initial_cash=100000,
                status="running",
                started_at=datetime.now(timezone.utc),
                idempotency_key="stale-m3",
            )
            db.add(task)
            db.commit()
            db.refresh(task)
            tid = task.id
        finally:
            db.close()

        worker = PortfolioBacktestWorker(session_factory=TestSessionLocal)
        recovered = worker._recover_stale_tasks()
        assert recovered == 1

        verify_db = TestSessionLocal()
        try:
            same = verify_db.get(PortfolioBacktest, tid)
            assert same is not None
            assert same.status == "queued"
            assert same.started_at is None
        finally:
            verify_db.close()

    def test_recover_no_op_when_no_stale(self, patched_app):
        from app.tasks.portfolio_worker import PortfolioBacktestWorker

        _, TestSessionLocal = patched_app
        worker = PortfolioBacktestWorker(session_factory=TestSessionLocal)
        assert worker._recover_stale_tasks() == 0
