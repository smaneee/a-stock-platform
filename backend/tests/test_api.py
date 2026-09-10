"""API 端点测试（使用 Mock 数据源，不依赖外部网络）。"""
from fastapi.testclient import TestClient

from app.main import app


class _NoopWorker:
    """禁用后台执行的假 worker，避免 API 测试被轮询线程干扰。"""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def test_health():
    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "不构成投资建议" in data["disclaimer"]
        # 详细健康检查应包含组件状态
        assert "database" in data
        assert "scheduler" in data
        assert "providers" in data
        assert data["database"]["ok"] is True
        assert data["scheduler"]["running"] is True


def test_liveness():
    """进程存活探针：永远返回 ok。"""
    with TestClient(app) as client:
        resp = client.get("/api/health/live")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_readiness_ok():
    """就绪探针：DB + 调度器都正常时返回 200。"""
    with TestClient(app) as client:
        resp = client.get("/api/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["database"] is True
        assert body["scheduler"] is True


def test_readiness_scheduler_down():
    """就绪探针：调度器标记为停止时返回 503。"""
    with TestClient(app) as client:
        # 临时把 scheduler 替换成不运行的假对象，避免破坏全局状态
        real_scheduler = client.app.state.scheduler
        from app.realtime.quote_scheduler import QuoteScheduler

        class _StoppedScheduler(QuoteScheduler):
            @property
            def is_running(self) -> bool:  # type: ignore[override]
                return False

        client.app.state.scheduler = _StoppedScheduler(
            provider_manager=real_scheduler._provider_manager,
            quote_cache=real_scheduler._cache,
            connection_manager=real_scheduler._ws,
            get_symbols=real_scheduler._get_symbols,
        )
        try:
            resp = client.get("/api/health/ready")
            assert resp.status_code == 503
            body = resp.json()["detail"]
            assert body["status"] == "not_ready"
            assert body["scheduler"] is False
        finally:
            client.app.state.scheduler = real_scheduler


def test_market_providers():
    with TestClient(app) as client:
        resp = client.get("/api/market/providers")
        assert resp.status_code == 200
        data = resp.json()
        assert "providers" in data


def test_get_quote_valid():
    with TestClient(app) as client:
        resp = client.get("/api/quotes/600000")
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "600000"
        assert data["price"] > 0


def test_get_quote_invalid_symbol():
    with TestClient(app) as client:
        resp = client.get("/api/quotes/abc")
        assert resp.status_code == 422


def test_batch_quotes():
    with TestClient(app) as client:
        resp = client.post("/api/quotes/batch", json={"symbols": ["600000", "000001"]})
        assert resp.status_code == 200
        quotes = resp.json()["quotes"]
        assert len(quotes) == 2


def test_batch_quotes_invalid():
    with TestClient(app) as client:
        resp = client.post("/api/quotes/batch", json={"symbols": ["abc"]})
        assert resp.status_code == 422


def test_watchlist_crud():
    with TestClient(app) as client:
        # 创建
        resp = client.post("/api/watchlists", json={"name": "我的自选"})
        assert resp.status_code == 201
        watchlist_id = resp.json()["id"]

        # 添加股票
        resp = client.post(
            f"/api/watchlists/{watchlist_id}/symbols",
            json={"symbol": "600000", "name": "浦发银行"},
        )
        assert resp.status_code == 200

        # 列出
        resp = client.get("/api/watchlists")
        assert resp.status_code == 200
        watchlists = resp.json()
        assert any(w["id"] == watchlist_id for w in watchlists)

        # 删除股票
        resp = client.delete(f"/api/watchlists/{watchlist_id}/symbols/600000")
        assert resp.status_code == 200


def test_strategies_list():
    with TestClient(app) as client:
        resp = client.get("/api/strategies")
        assert resp.status_code == 200
        strategies = resp.json()
        assert len(strategies) >= 4
        names = {s["name"] for s in strategies}
        assert "ma_cross" in names
        assert "breakout" in names
        assert "rsi_reversal" in names
        assert "macd_cross" in names


def test_strategy_enable_disable():
    with TestClient(app) as client:
        resp = client.get("/api/strategies")
        strategy_id = resp.json()[0]["id"]

        resp = client.post(f"/api/strategies/{strategy_id}/enable")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

        resp = client.post(f"/api/strategies/{strategy_id}/disable")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False


def test_paper_account_create_and_list():
    with TestClient(app) as client:
        resp = client.post("/api/paper/accounts", json={"name": "测试", "initial_cash": 100000})
        assert resp.status_code == 201

        resp = client.get("/api/paper/accounts")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1


def test_disclaimer_present():
    """所有接口必须包含免责声明（由应用描述体现）。"""
    assert "不构成投资建议" in app.description


# ──────── 回测任务接口 ────────


def test_backtest_create_and_query_flow(monkeypatch):
    """创建回测任务（202 queued），可查询详情与列表。"""
    monkeypatch.setattr(app.state, "backtest_worker", _NoopWorker())
    with TestClient(app) as client:
        resp = client.post(
            "/api/backtests",
            json={
                "symbol": "600000",
                "strategy_name": "ma_cross",
                "start_time": "2026-01-01T00:00:00",
                "end_time": "2026-01-10T00:00:00",
                "initial_cash": 100000,
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "queued"
        assert body["id"] > 0

        # 详情
        resp = client.get(f"/api/backtests/{body['id']}")
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["symbol"] == "600000"
        assert detail["status"] == "queued"

        # 列表
        resp = client.get("/api/backtests")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert any(item["id"] == body["id"] for item in items)


def test_backtest_validation_errors(monkeypatch):
    """非法代码、未知策略、非法时间范围分别返回 422/404/422。"""
    monkeypatch.setattr(app.state, "backtest_worker", _NoopWorker())
    with TestClient(app) as client:
        base = {
            "symbol": "600000",
            "strategy_name": "ma_cross",
            "start_time": "2026-01-01T00:00:00",
            "end_time": "2026-01-10T00:00:00",
        }
        resp = client.post("/api/backtests", json={**base, "symbol": "abc"})
        assert resp.status_code == 422

        resp = client.post("/api/backtests", json={**base, "strategy_name": "nope"})
        assert resp.status_code == 404

        resp = client.post(
            "/api/backtests",
            json={
                **base,
                "start_time": "2026-01-10T00:00:00",
                "end_time": "2026-01-01T00:00:00",
            },
        )
        assert resp.status_code == 422


def test_backtest_idempotency(monkeypatch):
    """同一幂等键重复创建应返回 duplicate，不产生新记录。"""
    monkeypatch.setattr(app.state, "backtest_worker", _NoopWorker())
    with TestClient(app) as client:
        payload = {
            "symbol": "000001",
            "strategy_name": "ma_cross",
            "start_time": "2026-01-01T00:00:00",
            "end_time": "2026-01-10T00:00:00",
            "idempotency_key": "demo-key-1",
        }
        first = client.post("/api/backtests", json=payload)
        assert first.status_code == 202
        first_id = first.json()["id"]

        second = client.post("/api/backtests", json=payload)
        assert second.status_code == 202
        assert second.json()["duplicate"] is True
        assert second.json()["id"] == first_id

        # 总数仍为 1
        items = client.get("/api/backtests").json()["items"]
        assert len([i for i in items if i["id"] == first_id]) == 1


def test_backtest_cancel_and_terminal_conflict(monkeypatch):
    """queued 任务可取消；已取消（终态）再取消返回 409。"""
    monkeypatch.setattr(app.state, "backtest_worker", _NoopWorker())
    with TestClient(app) as client:
        resp = client.post(
            "/api/backtests",
            json={
                "symbol": "600036",
                "strategy_name": "ma_cross",
                "start_time": "2026-01-01T00:00:00",
                "end_time": "2026-01-10T00:00:00",
            },
        )
        backtest_id = resp.json()["id"]

        cancel = client.post(f"/api/backtests/{backtest_id}/cancel")
        assert cancel.status_code == 200
        assert cancel.json()["status"] == "cancelled"

        again = client.post(f"/api/backtests/{backtest_id}/cancel")
        assert again.status_code == 409


def test_websocket_quote_connection_can_subscribe():
    """连接对象必须可注册，订阅路径不能在握手后崩溃。"""
    with TestClient(app) as client:
        with client.websocket_connect("/ws/quotes") as websocket:
            websocket.send_json({"action": "subscribe", "symbols": ["600000"]})
