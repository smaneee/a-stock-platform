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
