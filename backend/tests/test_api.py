"""API 端点测试（使用 Mock 数据源，不依赖外部网络）。"""
from fastapi.testclient import TestClient

from app.main import app


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


def test_websocket_quote_connection_can_subscribe():
    """连接对象必须可注册，订阅路径不能在握手后崩溃。"""
    with TestClient(app) as client:
        with client.websocket_connect("/ws/quotes") as websocket:
            websocket.send_json({"action": "subscribe", "symbols": ["600000"]})
