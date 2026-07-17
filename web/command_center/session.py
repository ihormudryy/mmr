"""Read-side session authentication for the command center (spec §10).

The login token is exchanged once for a signed HttpOnly cookie; the raw token
is never accepted in a URL, never logged, and never stored client-side.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("web.command_center.session")

SESSION_COOKIE = "mmr_dashboard_session"
SESSION_LIFETIME_SECONDS = 12 * 3600

# `/api/health` is exempt from the *session cookie* gate on purpose: it runs
# its OWN token gate (`web.app._check_access` / `MMR_WEB_TOKEN`) -- 200/degraded
# when unconfigured, 401 only when that token is set. Letting the session
# middleware also demand a cookie for it would clobber that gate (a blanket 401
# whenever no session manager exists) and break the always-on ops-detail probe.
# Only `/api/health` is exempted here -- every other `/api/*` route stays gated.
_EXEMPT_PATHS = frozenset(
    {"/healthz", "/readyz", "/api/health", "/session", "/logout", "/cc/login"})
_STRICT_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)
# The untouched legacy page at / uses inline scripts; it keeps every other header.
_LEGACY_CSP = _STRICT_CSP.replace("script-src 'self'", "script-src 'self' 'unsafe-inline'")


class CredentialConfigError(RuntimeError):
    """Dashboard startup misconfiguration — fail loudly, never run open."""


@dataclass(frozen=True)
class DashboardCredentials:
    token: str
    session_secret: bytes
    legacy_alias_used: bool


def _read_secret(env: Mapping[str, str], name: str) -> str:
    path = (env.get(f"{name}_FILE") or "").strip()
    if path:
        return Path(path).read_text().strip()
    return (env.get(name) or "").strip()


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def load_dashboard_credentials(env: Mapping[str, str] = os.environ) -> DashboardCredentials:
    token = _read_secret(env, "DASHBOARD_TOKEN")
    legacy = (env.get("MMR_WEB_TOKEN") or "").strip()
    legacy_alias_used = False
    if token and legacy:
        raise CredentialConfigError(
            "set DASHBOARD_TOKEN(_FILE) or the deprecated MMR_WEB_TOKEN, not both"
        )
    if not token and legacy:
        if _truthy(env.get("DASHBOARD_LIVE_COMMANDS_ENABLED")):
            raise CredentialConfigError(
                "MMR_WEB_TOKEN is a read-only deprecated alias: it cannot be used "
                "with live commands enabled — set DASHBOARD_TOKEN_FILE and rotate "
                "the old credential first"
            )
        logger.warning(
            "MMR_WEB_TOKEN is deprecated and accepted for one release only; "
            "set DASHBOARD_TOKEN_FILE and rotate the old credential"
        )
        token, legacy_alias_used = legacy, True
    if not token:
        raise CredentialConfigError(
            "dashboard login token missing: set DASHBOARD_TOKEN_FILE (or DASHBOARD_TOKEN)"
        )
    secret = _read_secret(env, "DASHBOARD_SESSION_SECRET")
    if not secret or len(secret.encode()) < 32:
        raise CredentialConfigError(
            "DASHBOARD_SESSION_SECRET(_FILE) missing or shorter than 32 bytes"
        )
    return DashboardCredentials(
        token=token,
        session_secret=secret.encode(),
        legacy_alias_used=legacy_alias_used,
    )


class FailedAttemptLimiter:
    """Five failed login attempts per rolling minute, tracked per client
    (spec §10).

    Keyed by an opaque per-caller identifier (``create_session_router`` uses
    the request's client host) so one attacker hammering ``/session`` with a
    bad token only locks out *that* client, not every operator sharing the
    dashboard. The default (unkeyed) bucket is kept for callers that don't
    pass a key, preserving the original single-bucket behaviour.

    The per-key map itself is bounded (``MAX_TRACKED_CLIENTS``, LRU-evicted)
    so an attacker rotating source addresses can't grow it without bound.
    Individual deque ops are already atomic under the GIL; the coarse lock
    only guards the shared ``_failures`` map (insert/evict) added here.
    """

    MAX_TRACKED_CLIENTS = 10_000

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._max = max_attempts
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: "OrderedDict[str, deque[float]]" = OrderedDict()

    def _bucket(self, key: str, now: float) -> deque:
        """Fetch (creating if needed) and prune the bucket for `key`.
        Caller must hold `self._lock`."""
        bucket = self._failures.get(key)
        if bucket is None:
            bucket = deque()
            self._failures[key] = bucket
        else:
            self._failures.move_to_end(key)
        while bucket and now - bucket[0] > self._window:
            bucket.popleft()
        return bucket

    def allow(self, key: str = "") -> bool:
        now = self._clock()
        with self._lock:
            bucket = self._bucket(key, now)
            return len(bucket) < self._max

    def record_failure(self, key: str = "") -> None:
        now = self._clock()
        with self._lock:
            bucket = self._bucket(key, now)
            bucket.append(now)
            while len(self._failures) > self.MAX_TRACKED_CLIENTS:
                self._failures.popitem(last=False)

    def reset(self, key: str = "") -> None:
        """Clear a client's failure history (called on successful login)."""
        with self._lock:
            self._failures.pop(key, None)


class SessionManager:
    """Signed cookie sessions with a server epoch and absolute lifetime."""

    def __init__(
        self,
        credentials: DashboardCredentials,
        *,
        lifetime_seconds: int = SESSION_LIFETIME_SECONDS,
        clock: Callable[[], float] = time.time,
    ):
        self._token = credentials.token
        self._secret = credentials.session_secret
        self._lifetime = lifetime_seconds
        self._clock = clock
        self._epoch = hashlib.sha256(
            self._secret + b":" + self._token.encode()
        ).hexdigest()[:16]

    def exchange(self, supplied_token: str) -> Optional[str]:
        if not hmac.compare_digest((supplied_token or "").encode(), self._token.encode()):
            return None
        return self._sign(int(self._clock()))

    def _sign(self, issued_at: int) -> str:
        payload = f"{self._epoch}.{issued_at}"
        signature = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def verify(self, cookie_value: str) -> bool:
        try:
            _epoch, issued_raw, _sig = (cookie_value or "").split(".")
            issued_at = int(issued_raw)
        except (ValueError, AttributeError):
            return False
        expected = self._sign(issued_at)
        if not hmac.compare_digest(cookie_value, expected):
            return False
        age = self._clock() - issued_at
        return 0 <= age <= self._lifetime


def build_require_session(manager: SessionManager) -> Callable[[Request], str]:
    """FastAPI dependency that M1-C mutation routes must reuse."""

    def require_session(request: Request) -> str:
        cookie = request.cookies.get(SESSION_COOKIE, "")
        if not cookie or not manager.verify(cookie):
            raise HTTPException(status_code=401, detail="session required")
        return cookie

    return require_session


# [M1-C] Task 4: process-local, generated once at import time -- the SAME
# pattern `web/command_center/routes_commands.py`'s `_CSRF_SECRET` already
# established for deriving a session-bound value without changing this
# module's session-identity shape. A `DashboardSession` dataclass with its
# own `session_id`/`epoch` fields (what an earlier plan for this function
# assumed) never landed here (see `build_require_session` above): the
# session identity every route actually threads through is the raw signed
# cookie `str` this module already returns. Keying on THAT string still
# gives a value that is stable for one browser session (repeat calls with
# the same cookie derive the same fingerprint) and distinct across sessions
# (a different login -- different `issued_at`/signature -- derives a
# different fingerprint), without ever being reversible back to the cookie.
_FINGERPRINT_SECRET = secrets.token_bytes(32)


def session_fingerprint(session: str) -> str:
    """Opaque per-session value the trader binds preflight nonces to
    (spec 9.1 session binding).

    Derived, not the raw cookie: leaking this value cannot replay the
    session, and the trader (a separate process, on the other side of the
    command gateway) never learns the browser's session credential. The
    trader only ever needs this value to match itself across the
    preflight -> confirm -> approve ceremony for one browser session, never
    to independently recompute it from a shared secret.
    """
    return hmac.new(_FINGERPRINT_SECRET, f"preflight:{session}".encode(),
                    hashlib.sha256).hexdigest()


_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>MMR Command Center — Login</title>
<style>body{background:#0e1116;color:#d7dce3;font:15px/1.5 system-ui;display:grid;
place-items:center;height:100vh;margin:0}form{background:#171b22;border:1px solid #262c36;
border-radius:10px;padding:28px;display:grid;gap:12px;min-width:320px}
input,button{font:inherit;padding:8px 10px;border-radius:6px;border:1px solid #262c36}
input{background:#0e1116;color:#d7dce3}button{background:#1f6feb;color:#fff;border:none;cursor:pointer}
</style></head><body>
<form method="post" action="/session">
  <strong>MMR Command Center</strong>
  <label for="token">Dashboard token</label>
  <input id="token" name="token" type="password" autocomplete="current-password" autofocus>
  <button type="submit">Sign in</button>
</form></body></html>"""


def create_session_router(
    manager: SessionManager | Callable[[], SessionManager],
    limiter: FailedAttemptLimiter,
    *,
    cookie_secure: bool = False,
) -> APIRouter:
    """Build the login/logout router.

    ``manager`` may be a plain ``SessionManager`` instance (the original
    Task 1 shape -- still exactly how ``tests/test_dashboard_session.py``
    constructs this router) OR a zero-arg callable that lazily resolves one
    (e.g. ``CommandCenter.ensure_session_manager``, which constructs-and-
    caches the manager from configured credentials on first call). Routes
    resolve the provider inside each request handler, never at router-build
    time, so building this router never itself needs credentials to be
    configured -- ``ensure_session_manager`` stays the one hard failure
    point, whenever a request (or lifespan startup) first asks for it.
    """
    manager_provider: Callable[[], SessionManager] = (
        manager if callable(manager) else (lambda: manager)
    )
    router = APIRouter()

    @router.get("/cc/login", response_class=HTMLResponse)
    def login_page() -> str:
        return _LOGIN_PAGE

    @router.post("/session")
    def login(request: Request, token: str = Form("")):
        client_key = request.client.host if request.client else "unknown"
        if not limiter.allow(client_key):
            raise HTTPException(status_code=429, detail="too many failed attempts")
        cookie = manager_provider().exchange(token)
        if cookie is None:
            limiter.record_failure(client_key)
            raise HTTPException(status_code=401, detail="invalid token")
        limiter.reset(client_key)
        wants_html = "text/html" in (request.headers.get("accept") or "")
        response = (
            RedirectResponse("/cc", status_code=303)
            if wants_html
            else JSONResponse({"ok": True})
        )
        response.set_cookie(
            SESSION_COOKIE,
            cookie,
            max_age=SESSION_LIFETIME_SECONDS,
            path="/",
            httponly=True,
            samesite="strict",
            secure=cookie_secure,
        )
        return response

    @router.post("/logout")
    def logout() -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return router


class SessionSecurityMiddleware(BaseHTTPMiddleware):
    """Session enforcement and security headers for every dashboard route."""

    def __init__(self, app, manager_provider: Callable[[], Optional[SessionManager]]):
        super().__init__(app)
        self._manager_provider = manager_provider

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        exempt = path in _EXEMPT_PATHS or path.startswith("/static/")
        if not exempt:
            manager = self._manager_provider()
            cookie = request.cookies.get(SESSION_COOKIE, "")
            if manager is None or not cookie or not manager.verify(cookie):
                if path.startswith("/api/"):
                    response = JSONResponse({"detail": "session required"}, status_code=401)
                else:
                    response = RedirectResponse("/cc/login", status_code=303)
                self._apply_headers(response, path)
                return response
        response = await call_next(request)
        self._apply_headers(response, path)
        return response

    @staticmethod
    def _apply_headers(response, path: str) -> None:
        strict = path == "/cc" or path.startswith(("/cc/", "/api/", "/session", "/logout", "/static/"))
        response.headers["Content-Security-Policy"] = _STRICT_CSP if strict else _LEGACY_CSP
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
