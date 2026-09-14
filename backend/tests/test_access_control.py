"""访问控制与静态托管测试（P0-04）。

覆盖：

* 令牌签发/校验/篡改/过期（单元）；
* 关闭鉴权时行为与改造前一致（不引入回归）；
* 开启鉴权后：无令牌 401、本机免密取令牌可用、口令登录可用、错误口令 401；
* 非回环来源索取本机令牌 403、跨站 Origin 索取本机令牌 403；
* 监听非回环地址却不鉴权 → 启动直接失败（fail closed）；
* 静态托管：资源文件直出、SPA 深链回落 index.html、/api 不会被 SPA 吞掉。
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from app.security.auth import AccessGuard, is_loopback


# ───────────────────── 1. AccessGuard 单元 ─────────────────────


def test_guard_disabled_without_password():
    guard = AccessGuard(password="")
    assert guard.enabled is False
    token, _ = guard.issue()
    # 未启用时即使拿着签名正确的令牌也不代表「已鉴权」——中间件直接短路
    assert guard.verify(token) is True
    assert guard.authenticate("") is False


def test_guard_issue_and_verify_roundtrip():
    guard = AccessGuard(password="s3cret")
    assert guard.enabled is True
    token, exp = guard.issue()
    assert guard.verify(token) is True
    assert exp > time.time()
    assert guard.authenticate("s3cret") is True
    assert guard.authenticate("wrong") is False
    assert guard.authenticate(None) is False


def test_guard_rejects_tampered_and_malformed_tokens():
    guard = AccessGuard(password="s3cret")
    token, _ = guard.issue()
    payload, _, signature = token.rpartition(".")
    assert guard.verify(payload + "." + ("A" if not signature.startswith("A") else "B") + signature[1:]) is False
    assert guard.verify(payload + ".") is False
    assert guard.verify(".") is False
    assert guard.verify("garbage") is False
    assert guard.verify(None) is False
    assert guard.verify("") is False


def test_guard_rejects_expired_token():
    guard = AccessGuard(password="s3cret", ttl_seconds=60)
    token, _ = guard.issue(now=time.time() - 3600)
    assert guard.verify(token) is False
    # 边界：刚好过期
    token2, _ = guard.issue(now=time.time() - 61)
    assert guard.verify(token2) is False


def test_guard_secret_changes_signature():
    a = AccessGuard(password="x", secret="alpha")
    b = AccessGuard(password="x", secret="beta")
    token, _ = a.issue()
    assert b.verify(token) is False
    assert a.verify(token) is True


def test_is_loopback_variants():
    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert is_loopback("localhost")
    assert is_loopback("testclient")
    assert not is_loopback("192.168.1.20")
    assert not is_loopback(None)


# ───────────────────── 2. 中间件行为 ─────────────────────


def _mount_guard(app: FastAPI, guard: AccessGuard) -> None:
    """把 guard 与中间件装到一个最小应用上（避免为每个用例重建完整后端）。"""
    from app.security.auth import AccessControlMiddleware

    app.state.access_guard = guard
    app.add_middleware(AccessControlMiddleware, guard=guard)
    from app.api.auth import router as auth_router

    app.include_router(auth_router)

    @app.get("/api/ping")
    async def ping():
        return {"ok": True}

    @app.get("/api/health/ready")
    async def ready():
        return {"status": "ready"}

    @app.post("/api/watchlists")
    async def write_thing(request: Request):
        return JSONResponse({"created": True})


def _mini_app(guard: AccessGuard) -> FastAPI:
    app = FastAPI()
    _mount_guard(app, guard)
    return app


def test_disabled_guard_allows_everything():
    client = TestClient(_mini_app(AccessGuard(password="")))
    assert client.get("/api/ping").status_code == 200
    assert client.post("/api/watchlists").status_code == 200


def test_enabled_guard_blocks_api_without_token():
    client = TestClient(_mini_app(AccessGuard(password="pw")))
    r = client.get("/api/ping")
    assert r.status_code == 401
    assert r.json()["code"] == "auth_required"
    assert client.post("/api/watchlists").status_code == 401
    # 健康检查与鉴权接口始终放行，否则启动器无法探活、用户无法登录
    assert client.get("/api/health/ready").status_code == 200
    assert client.get("/api/auth/session").status_code == 200


def test_enabled_guard_blocks_wrong_token():
    guard = AccessGuard(password="pw")
    client = TestClient(_mini_app(guard))
    other = AccessGuard(password="pw", secret="another-secret")
    bad_token, _ = other.issue()
    assert client.get("/api/ping", headers={"X-Auth-Token": bad_token}).status_code == 401
    assert client.get("/api/ping", headers={"Authorization": "Bearer nonsense"}).status_code == 401
    assert client.get("/api/ping?token=nonsense").status_code == 401


def test_enabled_guard_accepts_valid_token_via_header_param_and_cookie():
    guard = AccessGuard(password="pw")
    client = TestClient(_mini_app(guard))
    token, _ = guard.issue()
    assert client.get("/api/ping", headers={"X-Auth-Token": token}).status_code == 200
    assert client.get(f"/api/ping?token={token}").status_code == 200
    client.cookies.set("astock_session", token)
    assert client.get("/api/ping").status_code == 200
    # 写接口同样受保护（这正是「不要把交易/删除/同步接口暴露到局域网」）
    assert client.post("/api/watchlists").status_code == 200


def test_local_token_from_loopback_and_login_flow():
    guard = AccessGuard(password="pw")
    client = TestClient(_mini_app(guard))

    local = client.get("/api/auth/local-token")
    assert local.status_code == 200
    token = local.json()["token"]
    assert guard.verify(token)
    assert client.get("/api/ping").status_code == 200  # cookie 已写入

    bad = client.post("/api/auth/login", json={"password": "nope"})
    assert bad.status_code == 401
    assert bad.json()["code"] == "bad_password"

    ok = client.post("/api/auth/login", json={"password": "pw"})
    assert ok.status_code == 200
    assert guard.verify(ok.json()["token"])

    assert client.get("/api/auth/session").json()["authenticated"] is True


def test_local_token_rejected_for_non_loopback_client():
    guard = AccessGuard(password="pw")
    app = _mini_app(guard)
    # Starlette TestClient 支持自定义 client 元组，用来模拟局域网设备
    client = TestClient(app, client=("192.168.1.50", 51000))
    r = client.get("/api/auth/local-token")
    assert r.status_code == 403
    assert r.json()["code"] == "local_only"
    # 但可以正常用口令登录
    assert client.post("/api/auth/login", json={"password": "pw"}).status_code == 200


def test_local_token_rejected_for_cross_origin():
    guard = AccessGuard(password="pw")
    client = TestClient(_mini_app(guard))
    r = client.get("/api/auth/local-token", headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403
    assert r.json()["code"] == "cross_origin"
    # 同源（回环）Origin 允许
    ok = client.get("/api/auth/local-token", headers={"Origin": "http://127.0.0.1:8000"})
    assert ok.status_code == 200


def test_logout_clears_cookie():
    guard = AccessGuard(password="pw")
    client = TestClient(_mini_app(guard))
    client.get("/api/auth/local-token")
    assert client.get("/api/ping").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.clear()
    assert client.get("/api/ping").status_code == 401


# ───────────────────── 3. 启动 fail-closed ─────────────────────


def test_create_app_refuses_non_loopback_without_auth(monkeypatch):
    from app import main as main_module

    patched = main_module.settings.model_copy(
        update={"host": "0.0.0.0", "require_auth": False, "access_password": ""}
    )
    monkeypatch.setattr(main_module, "settings", patched)
    with pytest.raises(RuntimeError, match="不是回环地址"):
        main_module.create_app()


def test_create_app_refuses_auth_without_password(monkeypatch):
    from app import main as main_module

    patched = main_module.settings.model_copy(
        update={"host": "0.0.0.0", "require_auth": True, "access_password": ""}
    )
    monkeypatch.setattr(main_module, "settings", patched)
    with pytest.raises(RuntimeError, match="ACCESS_PASSWORD"):
        main_module.create_app()


def test_create_app_allows_lan_with_auth(monkeypatch):
    from app import main as main_module

    patched = main_module.settings.model_copy(
        update={"host": "0.0.0.0", "require_auth": True, "access_password": "pw-123"}
    )
    monkeypatch.setattr(main_module, "settings", patched)
    app = main_module.create_app()
    assert app.state.access_guard.enabled is True


# ───────────────────── 4. 静态托管 ─────────────────────


def test_spa_mount_serves_assets_and_falls_back(tmp_path):
    from app.main import _mount_spa

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>INDEX</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")

    app = FastAPI()

    @app.get("/api/ping")
    async def ping():
        return {"ok": True}

    # 与生产一致：静态托管在业务路由之后挂载
    assert _mount_spa(app, dist) is True

    client = TestClient(app)
    assert "INDEX" in client.get("/").text
    assert "INDEX" in client.get("/watchlist").text          # 深链回落
    assert "console.log" in client.get("/assets/app.js").text  # 资源直出
    assert client.get("/api/ping").json() == {"ok": True}      # API 不被 SPA 吞掉
    assert client.get("/api/does-not-exist").status_code == 404


def test_spa_mount_missing_dist_is_non_fatal(tmp_path):
    from app.main import _mount_spa

    app = FastAPI()
    assert _mount_spa(app, tmp_path / "nope") is False


def test_spa_mount_blocks_path_traversal(tmp_path):
    from app.main import _mount_spa

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>INDEX</html>", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET", encoding="utf-8")

    app = FastAPI()
    _mount_spa(app, dist)
    client = TestClient(app)
    r = client.get("/../secret.txt")
    assert "TOP-SECRET" not in r.text
