"""Loopback browser sessions, isolated from server-to-server credentials.

Bootstrap capabilities are one-use, memory-only and exchanged in a POST body.
The launcher control credential is protected by Windows current-user DPAPI.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Callable, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


Audience = Literal["product", "admin"]
CONTROL_HEADER = "X-MarketListener-Local-Control"
PRODUCT_CSRF_HEADER = "X-MarketListener-CSRF"
CONSOLE_CSRF_HEADER = "X-MarketListener-Console-Token"
_DPAPI_HEADER = b"MarketListener-Local-Control-v1\x00"
_ENTROPY = b"MarketListener/local-control/v1"
_LOOPBACK = {"127.0.0.1", "::1"}


def control_path(audience: Audience, port: int) -> Path:
    if audience not in {"product", "admin"} or not 1 <= port <= 65535:
        raise ValueError("Invalid local control scope")
    base = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not base:
        raise RuntimeError("本机受控启动凭据需要Windows当前用户DPAPI")
    return Path(base) / "MarketListener" / "secrets" / f"local-{audience}-{port}.dpapi"


def load_control_token(audience: Audience, port: int, *, create: bool = True) -> str:
    """Only production launchers call this; app fixtures inject memory credentials."""
    import win32crypt

    target = control_path(audience, port)
    if not target.is_file():
        if not create:
            raise RuntimeError("本机会话入口尚未初始化，请先通过快捷入口启动")
        token = secrets.token_urlsafe(48)
        protected = win32crypt.CryptProtectData(
            json.dumps({"audience": audience, "port": port, "token": token}).encode(),
            "MarketListener local launcher", _ENTROPY, None, None, 1,
        )
        from .atomic_files import publish_new_bytes

        publish_new_bytes(target, _DPAPI_HEADER + protected)
    try:
        raw = target.read_bytes()
        if not raw.startswith(_DPAPI_HEADER) or len(raw) > 16_384:
            raise ValueError("invalid control envelope")
        _, plain = win32crypt.CryptUnprotectData(raw[len(_DPAPI_HEADER):], _ENTROPY, None, None, 1)
        value = json.loads(plain)
        token = value.get("token")
        if value.get("audience") != audience or value.get("port") != port or not isinstance(token, str) or len(token) < 40:
            raise ValueError("invalid control scope")
        return token
    except Exception:
        raise RuntimeError("本机控制凭据无法由当前Windows用户解密") from None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@dataclass
class BrowserSession:
    csrf: str
    host: str
    expires_at: float
    last_seen_at: float


class LocalSessionManager:
    def __init__(self, audience: Audience, port: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        if audience not in {"product", "admin"} or not 1 <= port <= 65535:
            raise ValueError("Invalid browser session scope")
        self.audience = audience
        self.port = port
        self.cookie_name = f"ML_{audience.title()}_Session_{port}"
        self.csrf_header = PRODUCT_CSRF_HEADER if audience == "product" else CONSOLE_CSRF_HEADER
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        self.origins = {"http://" + host for host in self.hosts}
        self.clock = clock
        self.lock = threading.RLock()
        self.bootstrap_tokens: dict[str, tuple[str, float]] = {}
        self.sessions: dict[str, BrowserSession] = {}

    def _prune(self) -> None:
        now = self.clock()
        self.bootstrap_tokens = {key: item for key, item in self.bootstrap_tokens.items() if item[1] > now}
        self.sessions = {key: item for key, item in self.sessions.items() if item.expires_at > now and item.last_seen_at + 1800 > now}

    def issue_bootstrap(self, host: str) -> str:
        if host not in self.hosts:
            raise ValueError("Invalid local bootstrap host")
        with self.lock:
            self._prune()
            if len(self.bootstrap_tokens) >= 128:
                raise RuntimeError("本机引导请求过多，请稍后重试")
            token = secrets.token_urlsafe(32)
            self.bootstrap_tokens[_digest(token)] = (host, self.clock() + 120)
            return token

    def exchange(self, token: str, host: str) -> tuple[str, BrowserSession] | None:
        with self.lock:
            self._prune()
            item = self.bootstrap_tokens.get(_digest(token))
            if item is None or item[0] != host:
                return None
            self.bootstrap_tokens.pop(_digest(token))
            return self._create_session_unlocked(host)

    def _create_session_unlocked(self, host: str) -> tuple[str, BrowserSession] | None:
        if host not in self.hosts:
            return None
        if len(self.sessions) >= 128:
            return None
        cookie = secrets.token_urlsafe(32)
        session = BrowserSession(secrets.token_urlsafe(32), host, self.clock() + 8 * 3600, self.clock())
        self.sessions[_digest(cookie)] = session
        return cookie, session

    def create_session(self, host: str) -> tuple[str, BrowserSession] | None:
        with self.lock:
            self._prune()
            return self._create_session_unlocked(host)

    def session(self, request: Request) -> BrowserSession | None:
        token = request.cookies.get(self.cookie_name, "")
        with self.lock:
            self._prune()
            value = self.sessions.get(_digest(token))
            if value is not None and value.host == request.headers.get("host"):
                value.last_seen_at = self.clock()
                return value
            return None

    def revoke(self, request: Request) -> None:
        with self.lock:
            self.sessions.pop(_digest(request.cookies.get(self.cookie_name, "")), None)


def local_request_error(request: Request, manager: LocalSessionManager) -> JSONResponse | None:
    protected = {"host", "origin", manager.csrf_header.lower(), CONTROL_HEADER.lower()}
    for name in protected:
        if len(request.headers.getlist(name)) > 1:
            return JSONResponse({"detail": "本机入口拒绝重复身份头"}, status_code=403)
    if request.client is None or request.client.host not in _LOOPBACK or request.headers.get("host") not in manager.hosts:
        return JSONResponse({"detail": "本机入口拒绝非回环地址或无效Host"}, status_code=403)
    if any(name == "forwarded" or name.startswith("x-forwarded-") for name in request.headers):
        return JSONResponse({"detail": "本机入口不接受代理身份头"}, status_code=403)
    if request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if (origin is not None and origin != "http://" + request.headers.get("host", "")) or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "接口拒绝跨站请求"}, status_code=403)
    return None


def install_local_sessions(
    app: FastAPI, manager: LocalSessionManager, *, control_token: str | None = None,
    internal_authorizer: Callable[[Request], bool] | None = None,
    admin_local_access: bool = False,
) -> None:
    """Install outside the internal API boundary; that boundary still checks scope."""
    control = control_token or secrets.token_urlsafe(48)
    if admin_local_access and manager.audience != "admin":
        raise ValueError("Direct local access is restricted to the admin audience")
    app.state.local_sessions = manager

    def internal(request: Request) -> bool:
        # A browser must never be able to adopt the supervisor's server identity.
        return bool(internal_authorizer and not request.headers.get("origin") and not request.headers.get("sec-fetch-site") and internal_authorizer(request))

    @app.middleware("http")
    async def authorize_local_requests(request: Request, call_next):
        rejected = local_request_error(request, manager)
        if rejected is not None:
            return rejected
        path = request.url.path
        special = {"/api/local-session/bootstrap", "/api/local-session/issue", "/api/ready", "/api/admin/ready"}
        if admin_local_access:
            special.add("/api/local-session/open")
        if path.startswith("/api/") and path not in special and not internal(request):
            session = manager.session(request)
            if session is None:
                return JSONResponse({"detail": "本机管理会话尚未建立，请刷新页面。" if manager.audience == "admin"
                                     else "请通过本机快捷入口重新打开MarketListener"}, status_code=401)
            request.state.local_session = session
            if request.method not in {"GET", "HEAD"}:
                supplied = request.headers.get(manager.csrf_header, "")
                if request.headers.get("origin") != "http://" + request.headers.get("host", "") or not _equal(supplied, session.csrf):
                    return JSONResponse({"detail": "会话写请求未获授权"}, status_code=403)
        response = await call_next(request)
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        ancestors = "'self'" if path == "/api/industry/atlas" else "'none'"
        response.headers["Content-Security-Policy"] = f"frame-ancestors {ancestors}; base-uri 'self'; object-src 'none'"
        return response

    @app.post("/api/local-session/issue", include_in_schema=False)
    async def issue(request: Request) -> dict:
        from fastapi import HTTPException

        supplied = request.headers.get(CONTROL_HEADER, "")
        if request.headers.get("origin") or request.headers.get("sec-fetch-site") or not _equal(supplied, control):
            raise HTTPException(403, "受控本机启动请求未获授权")
        return {"bootstrap": manager.issue_bootstrap(request.headers["host"]), "audience": manager.audience, "expiresIn": 120}

    @app.post("/api/local-session/bootstrap", include_in_schema=False)
    async def bootstrap(request: Request) -> JSONResponse:
        if request.query_params or request.headers.get("origin") != "http://" + request.headers.get("host", ""):
            return JSONResponse({"detail": "本机会话引导请求无效"}, status_code=403)
        length = request.headers.get("content-length", "")
        if not length.isdecimal() or int(length) > 1024:
            return JSONResponse({"detail": "本机会话引导正文无效"}, status_code=400)
        try:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1024:
                    return JSONResponse({"detail": "本机会话引导正文过大"}, status_code=400)
            value = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"detail": "本机会话引导正文无效"}, status_code=400)
        token = value.get("bootstrap") if isinstance(value, dict) else None
        if not isinstance(token, str) or len(token) > 128:
            return JSONResponse({"detail": "本机会话引导正文无效"}, status_code=400)
        result = manager.exchange(token, request.headers["host"])
        if result is None:
            return JSONResponse({"detail": "本机引导已过期或已使用，请从快捷入口重新打开"}, status_code=401)
        cookie, session = result
        response = JSONResponse({"audience": manager.audience, "csrfToken": session.csrf, "expiresIn": 8 * 3600, "idleExpiresIn": 1800})
        response.set_cookie(manager.cookie_name, cookie, httponly=True, samesite="strict", max_age=8 * 3600, path="/")
        return response

    if admin_local_access:
        @app.post("/api/local-session/open", include_in_schema=False)
        async def open_local_admin(request: Request) -> JSONResponse:
            if (request.query_params or request.headers.get("origin") !=
                    "http://" + request.headers.get("host", "") or
                    request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"):
                return JSONResponse({"detail": "本机管理会话请求无效"}, status_code=403)
            length = request.headers.get("content-length", "")
            if not length.isdecimal() or not 0 < int(length) <= 64:
                return JSONResponse({"detail": "本机管理会话正文无效"}, status_code=400)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 64:
                    return JSONResponse({"detail": "本机管理会话正文过大"}, status_code=400)
            try:
                value = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                return JSONResponse({"detail": "本机管理会话正文无效"}, status_code=400)
            if value != {}:
                return JSONResponse({"detail": "本机管理会话正文无效"}, status_code=400)
            session = manager.session(request)
            cookie = None
            if session is None:
                result = manager.create_session(request.headers["host"])
                if result is None:
                    return JSONResponse({"detail": "管理台会话暂不可用"}, status_code=503)
                cookie, session = result
            response = JSONResponse({"audience": "admin", "csrfToken": session.csrf,
                                     "expiresIn": max(0, int(session.expires_at - manager.clock())),
                                     "idleExpiresIn": 1800})
            if cookie is not None:
                response.set_cookie(manager.cookie_name, cookie, httponly=True, samesite="strict",
                                    max_age=8 * 3600, path="/")
            return response

    @app.get("/api/local-session", include_in_schema=False)
    async def session_info(request: Request) -> dict:
        session = request.state.local_session
        return {"audience": manager.audience, "csrfToken": session.csrf, "expiresIn": max(0, int(session.expires_at - manager.clock())), "idleExpiresIn": 1800}

    @app.post("/api/local-session/logout", include_in_schema=False)
    async def logout(request: Request) -> JSONResponse:
        manager.revoke(request)
        response = JSONResponse({"status": "SIGNED_OUT"})
        response.delete_cookie(manager.cookie_name, path="/", httponly=True, samesite="strict")
        return response


__all__ = ("CONTROL_HEADER", "CONSOLE_CSRF_HEADER", "PRODUCT_CSRF_HEADER",
           "LocalSessionManager", "install_local_sessions", "load_control_token")
