"""局域网访问控制：无状态会话令牌 + 写接口保护。

设计约束（来自研发计划 P0-04 与第 5.6 节安全要求）：

1. 本机默认零配置：绑定回环地址时，前端可直接向 ``/api/auth/local-token`` 换取
   会话令牌，不需要用户输入口令 —— 保证「双击快捷方式即可用」不退化。
2. 一旦监听非回环地址（局域网／手机访问），必须显式提供 ``ACCESS_PASSWORD``，
   否则应用启动即失败（fail closed），不会出现「以为要登录、实际裸奔」。
3. 令牌是无状态 HMAC 签名串（``<exp>.<sig>``），只驻留内存与客户端 cookie：
   不写数据库、不写日志、不进源代码。密钥来自 ``SESSION_SECRET`` 或访问口令。
4. 令牌比较全部使用 :func:`hmac.compare_digest`，避免时序侧信道。

本模块不处理券商凭据：实盘账号与密码永远不进入这里。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# 回环来源：本机浏览器、TestClient、launcher 探针都落在这里
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost", "testclient"}

# 令牌有效期：默认 12 小时（一个交易日内无需反复登录）
DEFAULT_TTL_SECONDS = 12 * 3600

SESSION_COOKIE = "astock_session"
SESSION_HEADER = "X-Auth-Token"

# 无需令牌即可访问的路径前缀（其余 /api/* 全部拦截）
EXEMPT_PREFIXES = ("/api/health", "/api/auth/")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def is_loopback(host: str | None) -> bool:
    """判断来源是否为本机回环地址。"""
    if not host:
        return False
    return host in LOOPBACK_HOSTS


@dataclass
class AccessGuard:
    """会话令牌签发与校验。

    ``enabled`` 为 False 时整条鉴权链路短路，行为与改造前完全一致
    （仅本机开发场景）。
    """

    password: str = ""
    secret: str = ""
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    _warned: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if not self.secret:
            # 未单独配置密钥时以访问口令派生，避免出现「口令对、签名密钥为空」
            self.secret = hashlib.sha256(
                (self.password or "astock-local").encode("utf-8")
            ).hexdigest()

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    # ───────────── 令牌 ─────────────

    def _sign(self, payload: str) -> str:
        mac = hmac.new(self.secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256)
        return _b64e(mac.digest())

    def issue(self, now: float | None = None) -> tuple[str, int]:
        """签发令牌，返回 ``(token, 过期时间戳)``。"""
        exp = int((now if now is not None else time.time()) + self.ttl_seconds)
        payload = _b64e(json.dumps({"exp": exp, "n": secrets.token_hex(8)}).encode("utf-8"))
        return f"{payload}.{self._sign(payload)}", exp

    def verify(self, token: str | None, now: float | None = None) -> bool:
        """校验令牌签名与有效期。"""
        if not token or "." not in token:
            return False
        payload, _, signature = token.rpartition(".")
        if not payload or not signature:
            return False
        if not hmac.compare_digest(signature, self._sign(payload)):
            return False
        try:
            data = json.loads(_b64d(payload))
            exp = int(data["exp"])
        except (ValueError, KeyError, TypeError):
            return False
        return exp > (now if now is not None else time.time())

    def authenticate(self, password: str | None) -> bool:
        """校验访问口令（常量时间比较）。"""
        if not self.enabled or password is None:
            return False
        return hmac.compare_digest(str(password), self.password)

    # ───────────── 请求令牌提取 ─────────────

    @staticmethod
    def token_from_request(request: Request) -> str | None:
        header = request.headers.get(SESSION_HEADER)
        if header:
            return header.strip()
        auth = request.headers.get("authorization")
        if auth and auth.lower().startswith("bearer "):
            return auth[7:].strip()
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie:
            return cookie
        return request.query_params.get("token")


class AccessControlMiddleware(BaseHTTPMiddleware):
    """对所有 ``/api/*`` 强制会话令牌（健康检查与登录接口除外）。"""

    def __init__(self, app, guard: AccessGuard):
        super().__init__(app)
        self.guard = guard

    async def dispatch(self, request: Request, call_next):
        guard = self.guard
        if not guard.enabled:
            return await call_next(request)

        path = request.url.path
        if not path.startswith("/api") or path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)

        token = guard.token_from_request(request)
        if guard.verify(token):
            return await call_next(request)

        client = request.client.host if request.client else "unknown"
        logger.warning(
            "拒绝未授权 API 访问: %s %s from %s", request.method, path, client
        )
        return JSONResponse(
            status_code=401,
            content={
                "detail": "需要访问令牌：请在页面上输入访问口令，或改用本机 127.0.0.1 访问。",
                "code": "auth_required",
            },
        )
