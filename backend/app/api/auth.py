"""访问控制接口：会话状态、本机免密令牌、口令登录与登出。

安全边界：

* ``/api/auth/local-token`` **只**对回环来源签发，且必须同源（或没有 Origin 头），
  避免被浏览器里的第三方页面跨站读取（CORS 本身已阻止读取响应，这里再收紧一层）。
* 登录口令只从 ``ACCESS_PASSWORD`` 读取，不写数据库、不写日志；失败信息不区分
  「口令错误」与「未启用鉴权」，避免探测。
* 令牌通过 HttpOnly cookie 与 ``X-Auth-Token`` 头双通道下发，前端可任选其一。
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.security.auth import SESSION_COOKIE, is_loopback

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


def _guard(request: Request):
    return getattr(request.app.state, "access_guard", None)


def _session_payload(guard, request: Request) -> dict:
    if guard is None or not guard.enabled:
        return {"auth_required": False, "authenticated": True, "expires_at": None}
    token = guard.token_from_request(request)
    ok = guard.verify(token)
    return {
        "auth_required": True,
        "authenticated": ok,
        "expires_at": None,
        "client_loopback": is_loopback(request.client.host if request.client else None),
    }


def _same_origin(request: Request) -> bool:
    """Origin 头缺失（同源导航/原生客户端）或与本机回环同源时才放行。"""
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = urlparse(origin).hostname
    if host is None:
        return False
    if is_loopback(host):
        return True
    return host == (request.url.hostname or "")


@router.get("/session")
async def auth_session(request: Request) -> dict:
    """前端启动时调用：是否需要登录、当前令牌是否有效。"""
    return _session_payload(_guard(request), request)


@router.get("/local-token")
async def auth_local_token(request: Request) -> JSONResponse:
    """本机（回环）免密换取会话令牌，保证双击启动后无感可用。"""
    guard = _guard(request)
    if guard is None or not guard.enabled:
        return JSONResponse({"token": None, "expires_at": None, "auth_required": False})
    client = request.client.host if request.client else None
    if not is_loopback(client):
        logger.warning("拒绝非回环来源索取本机令牌: %s", client)
        return JSONResponse(
            status_code=403,
            content={"detail": "本机令牌只对 127.0.0.1 签发，请使用访问口令登录。", "code": "local_only"},
        )
    if not _same_origin(request):
        logger.warning("拒绝跨站 Origin 索取本机令牌: %s", request.headers.get("origin"))
        return JSONResponse(
            status_code=403,
            content={"detail": "拒绝跨站请求。", "code": "cross_origin"},
        )
    token, exp = guard.issue()
    resp = JSONResponse({"token": token, "expires_at": exp, "auth_required": True})
    _set_cookie(resp, token, exp)
    return resp


@router.post("/login")
async def auth_login(request: Request, body: LoginRequest) -> JSONResponse:
    """用访问口令登录，成功返回令牌（含 HttpOnly cookie）。"""
    guard = _guard(request)
    if guard is None or not guard.enabled:
        return JSONResponse({"token": None, "expires_at": None, "auth_required": False})
    if not guard.authenticate(body.password):
        logger.warning(
            "访问口令校验失败: client=%s", request.client.host if request.client else "unknown"
        )
        return JSONResponse(
            status_code=401,
            content={"detail": "访问口令不正确。", "code": "bad_password"},
        )
    token, exp = guard.issue()
    resp = JSONResponse({"token": token, "expires_at": exp, "auth_required": True})
    _set_cookie(resp, token, exp)
    return resp


@router.post("/logout")
async def auth_logout() -> JSONResponse:
    """清除 cookie（无状态令牌由客户端丢弃即可）。"""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def _set_cookie(resp: JSONResponse, token: str, exp: int) -> None:
    max_age = max(0, exp - int(time.time()))
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        path="/",
    )
