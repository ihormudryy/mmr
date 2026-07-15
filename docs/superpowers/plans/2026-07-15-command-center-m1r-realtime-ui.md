# M1-R Read-Only Realtime Command Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the read-only realtime command center: session-authenticated snapshot + SSE delivery over the fenced journal foundation, a bounded in-process read model, a conflated quote plane, the Layout A operations-first UI at `/cc`, boolean liveness/readiness plus authenticated dependency health, and the performance harness that later gates go for `[M1-C]`.

**Architecture:** FastAPI lifespan owns exactly one `DashboardEventBridge`, one loop-owned reducer (`DashboardState`), and one SSE fan-out registry. The bridge consumes the typed long-poll journal feed on a dedicated thread, installs fenced baselines from `snapshot_with_cursor`, and crosses into the ASGI loop only through `loop.call_soon_threadsafe`. Quotes arrive on a separate PubSub subscriber thread and are conflated per instrument at 4 Hz; they never enter the replay ring or any client FIFO. The browser loads `GET /api/snapshot`, then tails `GET /api/events` (sse-starlette); on SSE loss it degrades to 5-second snapshot polling behind a persistent banner. Everything in this plan is read-only — no mutation route exists until `[M1-C]`.

**Tech Stack:** CPython 3.12.13, FastAPI, Uvicorn (one worker), sse-starlette (locked runtime dependency), Jinja2, vanilla browser JavaScript (EventSource), pyzmq SUB socket for quotes, typed JSON RPC clients from `[G0]`, pytest, pytest-asyncio, httpx, Playwright, psutil.

## Global Constraints

- The source specification is `docs/superpowers/specs/2026-07-15-realtime-trading-command-center-design.md`; this plan covers `[M1-R]` (spec §4.1, §5.1–5.2, §6–§8.4 read-only, §10 read side, §11, §12, §13.2–13.3).
- Consumes frozen interfaces from the plan index without redefining them: `DomainEvent`, `SnapshotWithCursor`, typed methods `snapshot_with_cursor`, `read_domain_events(after_cursor, limit, wait_ms)`, `get_quotes_snapshot` (`[M1-F1]`), `TypedRpcClient.call(method, body, response_model)` on query port `42101` and feed port `42103` (`[G0]`), and the `[M1-F2]` guarantee that every open position has PubSub quote coverage. Development may run against fakes per the delivery graph, but the release gate runs against real fenced producers.
- The dashboard process NEVER opens a DuckDB file and never executes `dill` payloads: startup asserts `MMR_DILL_STRICT=1` and no module under `web/command_center/` imports `trader.data`.
- One Uvicorn worker owns `DashboardState`; startup fails on any multi-worker configuration. Graceful shutdown drains SSE connections within an explicit five-second bound; lifespan teardown never waits on an active SSE response.
- Producer threads (bridge, quote plane) cross into the ASGI loop only via `loop.call_soon_threadsafe`; only loop-owned code mutates state, client FIFOs, or quote maps. Every SSE generator unregisters its client in `finally`.
- Bounds: per-client FIFO of 1,000 domain/control events plus a separate latest-value quote map; quote-free replay ring of at most 10,000 domain/control events or five minutes; at most 500 terminal proposals, 500 terminal orders, and 500 fills, each with a 24-hour TTL; cleanup every 60 seconds; quote delivery clamped to 2–5 Hz with 4 Hz default.
- Reducers replace whole entities by key, reject `entity_revision` regressions idempotently, and apply `delete` tombstones. `quotes.snapshot` is a client-local control frame without an SSE `id`; individual quote batches also carry no SSE `id` and can never advance `Last-Event-ID`.
- Session auth (spec §10 read side): `POST /session` exchanges `DASHBOARD_TOKEN(_FILE)` for a signed `HttpOnly` `SameSite=Strict` cookie with a 12-hour absolute lifetime and a server epoch; constant-time compares; five failed attempts per minute; startup fails without both token and session secret; `MMR_WEB_TOKEN` is a one-release deprecated alias only when the canonical token is unset and live commands are disabled; `?token=` is never accepted. All data pages/APIs require the session except the login flow and boolean `/healthz` + `/readyz`.
- Security headers on every response: restrictive CSP, `frame-ancestors 'none'`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`.
- The legacy dashboard stays untouched at `/` (it gains only the session gate via middleware and keeps its own handlers/template); the command center page is `GET /cc`.
- An RPC failure, missing risk projection, or degraded source renders as explicit `unavailable`/stale state — never as a green pass and never as silently fresh data.
- Commits are tagged `feat(m1-r):` / `test(m1-r):` and each task's tests pass before its commit.

---

### Task 1: Session authentication, security headers, and startup credential gate

**Files:**
- Create: `web/command_center/__init__.py` (package marker only in this task)
- Create: `web/command_center/session.py`
- Create: `tests/test_dashboard_session.py`

**Interfaces:**
- Produces: `load_dashboard_credentials(env: Mapping[str, str] = os.environ) -> DashboardCredentials` and `CredentialConfigError(RuntimeError)`.
- Produces: `SessionManager(credentials: DashboardCredentials, *, lifetime_seconds: int = 43200, clock: Callable[[], float] = time.time)` with `exchange(supplied_token: str) -> str | None` and `verify(cookie_value: str) -> bool`.
- Produces: `FailedAttemptLimiter(max_attempts: int = 5, window_seconds: float = 60.0, clock=time.monotonic)` with `allow() -> bool` and `record_failure() -> None`.
- Produces: `build_require_session(manager: SessionManager) -> Callable[[Request], str]` — THE session dependency `[M1-C]` mutation routes must reuse.
- Produces: `create_session_router(manager: SessionManager, limiter: FailedAttemptLimiter, *, cookie_secure: bool = False) -> APIRouter` serving `POST /session`, `POST /logout`, `GET /cc/login`.
- Produces: `SessionSecurityMiddleware(app, manager_provider: Callable[[], SessionManager | None])` enforcing the session on every non-exempt path and stamping security headers.
- Consumes: nothing from other plans (pure stdlib + FastAPI).

- [ ] **Step 1: Write failing session tests**

Create `tests/test_dashboard_session.py`:

```python
import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from web.command_center.session import (
    SESSION_COOKIE,
    CredentialConfigError,
    DashboardCredentials,
    FailedAttemptLimiter,
    SessionManager,
    SessionSecurityMiddleware,
    build_require_session,
    create_session_router,
    load_dashboard_credentials,
)

SECRET = "s" * 64
TOKEN = "correct-horse-battery-staple"


def _creds() -> DashboardCredentials:
    return DashboardCredentials(token=TOKEN, session_secret=SECRET.encode(), legacy_alias_used=False)


def _app(manager: SessionManager, limiter: FailedAttemptLimiter | None = None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionSecurityMiddleware, manager_provider=lambda: manager)
    app.include_router(create_session_router(manager, limiter or FailedAttemptLimiter()))
    require_session = build_require_session(manager)

    @app.get("/api/snapshot")
    def snapshot(_session: str = Depends(require_session)):
        return {"data": "account stuff"}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


class TestCredentialLoading:
    def test_startup_fails_without_token(self):
        with pytest.raises(CredentialConfigError, match="DASHBOARD_TOKEN"):
            load_dashboard_credentials(env={"DASHBOARD_SESSION_SECRET": SECRET})

    def test_startup_fails_without_session_secret(self):
        with pytest.raises(CredentialConfigError, match="DASHBOARD_SESSION_SECRET"):
            load_dashboard_credentials(env={"DASHBOARD_TOKEN": TOKEN})

    def test_token_file_takes_precedence(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("file-token\n")
        creds = load_dashboard_credentials(env={
            "DASHBOARD_TOKEN_FILE": str(token_file),
            "DASHBOARD_SESSION_SECRET": SECRET,
        })
        assert creds.token == "file-token"

    def test_legacy_and_canonical_together_fail(self):
        with pytest.raises(CredentialConfigError, match="not both"):
            load_dashboard_credentials(env={
                "DASHBOARD_TOKEN": TOKEN,
                "MMR_WEB_TOKEN": "old",
                "DASHBOARD_SESSION_SECRET": SECRET,
            })

    def test_legacy_alias_accepted_when_live_commands_disabled(self, caplog):
        creds = load_dashboard_credentials(env={
            "MMR_WEB_TOKEN": "old-token",
            "DASHBOARD_SESSION_SECRET": SECRET,
        })
        assert creds.token == "old-token"
        assert creds.legacy_alias_used is True
        assert "old-token" not in caplog.text  # never log the value

    def test_legacy_alias_refused_when_live_commands_enabled(self):
        with pytest.raises(CredentialConfigError, match="MMR_WEB_TOKEN"):
            load_dashboard_credentials(env={
                "MMR_WEB_TOKEN": "old-token",
                "DASHBOARD_SESSION_SECRET": SECRET,
                "DASHBOARD_LIVE_COMMANDS_ENABLED": "true",
            })


class TestSessionExchange:
    def test_login_sets_httponly_samesite_strict_cookie(self):
        client = TestClient(_app(SessionManager(_creds())))
        response = client.post("/session", data={"token": TOKEN})
        assert response.status_code in (200, 303)
        raw = response.headers["set-cookie"].lower()
        assert "httponly" in raw and "samesite=strict" in raw

    def test_wrong_token_is_401_and_sixth_attempt_is_429(self):
        limiter = FailedAttemptLimiter()
        client = TestClient(_app(SessionManager(_creds()), limiter))
        for _ in range(5):
            assert client.post("/session", data={"token": "nope"}).status_code == 401
        assert client.post("/session", data={"token": "nope"}).status_code == 429

    def test_query_token_never_authenticates(self):
        client = TestClient(_app(SessionManager(_creds())))
        assert client.get(f"/api/snapshot?token={TOKEN}").status_code == 401

    def test_cookie_expires_after_twelve_hours(self):
        fake_now = [1_700_000_000.0]
        manager = SessionManager(_creds(), clock=lambda: fake_now[0])
        cookie = manager.exchange(TOKEN)
        assert manager.verify(cookie)
        fake_now[0] += 12 * 3600 + 1
        assert not manager.verify(cookie)

    def test_rotated_secret_invalidates_existing_sessions(self):
        cookie = SessionManager(_creds()).exchange(TOKEN)
        rotated = SessionManager(DashboardCredentials(
            token=TOKEN, session_secret=b"r" * 64, legacy_alias_used=False))
        assert not rotated.verify(cookie)


class TestEnforcementAndHeaders:
    def test_data_api_requires_session_but_healthz_is_open(self):
        client = TestClient(_app(SessionManager(_creds())))
        assert client.get("/api/snapshot").status_code == 401
        assert client.get("/healthz").status_code == 200

    def test_session_cookie_grants_access(self):
        manager = SessionManager(_creds())
        client = TestClient(_app(manager))
        client.post("/session", data={"token": TOKEN})
        assert client.get("/api/snapshot").status_code == 200

    def test_security_headers_on_every_response(self):
        client = TestClient(_app(SessionManager(_creds())))
        response = client.get("/healthz")
        csp = response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_logout_clears_cookie(self):
        manager = SessionManager(_creds())
        client = TestClient(_app(manager))
        client.post("/session", data={"token": TOKEN})
        client.post("/logout")
        assert client.get("/api/snapshot").status_code == 401
```

- [ ] **Step 2: Run the session tests and verify failure**

Run: `uv run --frozen pytest tests/test_dashboard_session.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'web.command_center'`.

- [ ] **Step 3: Implement `web/command_center/session.py`**

Create the package marker `web/command_center/__init__.py` (empty for now; Task 5 fills it) and implement:

```python
"""Read-side session authentication for the command center (spec §10).

The login token is exchanged once for a signed HttpOnly cookie; the raw token
is never accepted in a URL, never logged, and never stored client-side.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("web.command_center.session")

SESSION_COOKIE = "mmr_dashboard_session"
SESSION_LIFETIME_SECONDS = 12 * 3600

_EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/session", "/logout", "/cc/login"})
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
            "set DASHBOARD_TOKEN(_FILE) or the deprecated MMR_WEB_TOKEN, not both")
    if not token and legacy:
        if _truthy(env.get("DASHBOARD_LIVE_COMMANDS_ENABLED")):
            raise CredentialConfigError(
                "MMR_WEB_TOKEN is a read-only deprecated alias: it cannot be used "
                "with live commands enabled — set DASHBOARD_TOKEN_FILE and rotate "
                "the old credential first")
        logger.warning(
            "MMR_WEB_TOKEN is deprecated and accepted for one release only; "
            "set DASHBOARD_TOKEN_FILE and rotate the old credential")
        token, legacy_alias_used = legacy, True
    if not token:
        raise CredentialConfigError(
            "dashboard login token missing: set DASHBOARD_TOKEN_FILE (or DASHBOARD_TOKEN)")
    secret = _read_secret(env, "DASHBOARD_SESSION_SECRET")
    if not secret or len(secret.encode()) < 32:
        raise CredentialConfigError(
            "DASHBOARD_SESSION_SECRET(_FILE) missing or shorter than 32 bytes")
    return DashboardCredentials(
        token=token, session_secret=secret.encode(), legacy_alias_used=legacy_alias_used)


class FailedAttemptLimiter:
    """Five failed login attempts per rolling minute (spec §10)."""

    def __init__(self, max_attempts: int = 5, window_seconds: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self._max = max_attempts
        self._window = window_seconds
        self._clock = clock
        self._failures: deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        while self._failures and now - self._failures[0] > self._window:
            self._failures.popleft()
        return len(self._failures) < self._max

    def record_failure(self) -> None:
        self._failures.append(self._clock())


class SessionManager:
    """Signed cookie sessions with a server epoch and absolute lifetime."""

    def __init__(self, credentials: DashboardCredentials, *,
                 lifetime_seconds: int = SESSION_LIFETIME_SECONDS,
                 clock: Callable[[], float] = time.time):
        self._token = credentials.token
        self._secret = credentials.session_secret
        self._lifetime = lifetime_seconds
        self._clock = clock
        # Rotating either secret and restarting changes the epoch and thereby
        # invalidates every outstanding session (spec §10).
        self._epoch = hashlib.sha256(
            self._secret + b":" + self._token.encode()).hexdigest()[:16]

    def exchange(self, supplied_token: str) -> Optional[str]:
        if not hmac.compare_digest(
                (supplied_token or "").encode(), self._token.encode()):
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
    """FastAPI dependency: [M1-C] mutation routes MUST reuse this exact seam."""

    def require_session(request: Request) -> str:
        cookie = request.cookies.get(SESSION_COOKIE, "")
        if not cookie or not manager.verify(cookie):
            raise HTTPException(status_code=401, detail="session required")
        return cookie

    return require_session


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


def create_session_router(manager: SessionManager, limiter: FailedAttemptLimiter, *,
                          cookie_secure: bool = False) -> APIRouter:
    router = APIRouter()

    @router.get("/cc/login", response_class=HTMLResponse)
    def login_page() -> str:
        return _LOGIN_PAGE

    @router.post("/session")
    def login(request: Request, token: str = Form("")):
        if not limiter.allow():
            raise HTTPException(status_code=429, detail="too many failed attempts")
        cookie = manager.exchange(token)
        if cookie is None:
            limiter.record_failure()
            raise HTTPException(status_code=401, detail="invalid token")
        wants_html = "text/html" in (request.headers.get("accept") or "")
        response = (RedirectResponse("/cc", status_code=303) if wants_html
                    else JSONResponse({"ok": True}))
        response.set_cookie(
            SESSION_COOKIE, cookie, max_age=SESSION_LIFETIME_SECONDS, path="/",
            httponly=True, samesite="strict", secure=cookie_secure)
        return response

    @router.post("/logout")
    def logout() -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return router


class SessionSecurityMiddleware(BaseHTTPMiddleware):
    """Session enforcement + security headers for every route, legacy included.

    ``manager_provider`` is a callable so the app can construct the middleware
    before credentials are loaded in lifespan; until a manager exists every
    non-exempt request is refused (fail closed, never open).
    """

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
```

Note the test app in Step 1 returns 401 for `/api/snapshot` without a session because the middleware short-circuits; the `?token=` test passes because nothing ever reads a token query parameter.

- [ ] **Step 4: Run the session tests**

Run: `uv run --frozen pytest tests/test_dashboard_session.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/command_center/__init__.py web/command_center/session.py tests/test_dashboard_session.py
git commit -m "feat(m1-r): session login, headers, and startup credential gate"
```

### Task 2: `DashboardState` — bounded read model, reducer, retention, replay ring

**Files:**
- Create: `web/command_center/state.py`
- Create: `tests/test_dashboard_state.py`

**Interfaces:**
- Consumes: `DomainEvent` and `SnapshotWithCursor` from `trader.domain.events` (`[M1-F1]`, frozen in the plan index). Snapshot rows must each carry `entity_id` and `entity_revision`; a row without them fails baseline installation (fail loudly).
- Produces: `DashboardState(*, clock=time.time, monotonic=time.monotonic)` with:
  - `install_baseline(snapshot: SnapshotWithCursor, stream_id: str) -> None`
  - `apply(event: DomainEvent) -> dict | None` (returns the browser envelope, or `None` for an idempotently ignored stale revision)
  - `apply_quotes(batch: dict[str, dict]) -> None`
  - `replay_after(stream_id: str, sequence: int) -> list[dict] | None` (`None` = resync required)
  - `snapshot_view() -> dict` (bounded JSON-native read model)
  - `maybe_cleanup(now: float | None = None) -> None` and attributes `stream_id`, `sequence`, `has_baseline`, `last_event_at`, `quotes`
- Produces: module constants `SCHEMA_VERSION = 1`, `REPLAY_RING_MAX_EVENTS = 10_000`, `REPLAY_RING_MAX_AGE_SECONDS = 300.0`, `TERMINAL_CAP = 500`, `TERMINAL_TTL_SECONDS = 86_400`, `CLEANUP_INTERVAL_SECONDS = 60.0`.

- [ ] **Step 1: Write failing reducer and retention tests**

Create `tests/test_dashboard_state.py`:

```python
import datetime as dt

import pytest

from trader.domain.events import DomainEvent, SnapshotWithCursor
from web.command_center.state import (
    REPLAY_RING_MAX_EVENTS,
    TERMINAL_CAP,
    TERMINAL_TTL_SECONDS,
    DashboardState,
)

UTC = dt.timezone.utc


def _event(**overrides) -> DomainEvent:
    base = dict(
        event_id="evt-1",
        source_cursor=1,
        entity_revision=1,
        event_type="position.updated",
        entity_type="position",
        entity_id="DU123:265598",
        operation="upsert",
        account_id="DU123",
        source="trader_service",
        source_timestamp=dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        correlation_id=None,
        payload={"quantity": 100, "symbol": "AAPL", "currency": "USD"},
    )
    base.update(overrides)
    return DomainEvent(**base)


@pytest.fixture
def state():
    fake = {"wall": 1_752_580_800.0, "mono": 1000.0}
    s = DashboardState(clock=lambda: fake["wall"], monotonic=lambda: fake["mono"])
    s.install_baseline(
        SnapshotWithCursor(source_cursor=0, broker_generation=1, entities={}),
        stream_id="stream-a",
    )
    s._test_clock = fake  # test-only handle for advancing time
    return s


class TestReducer:
    def test_upsert_replaces_whole_entity_and_assigns_sequence(self, state):
        env1 = state.apply(_event(payload={"quantity": 100, "stale_field": True}))
        env2 = state.apply(_event(
            event_id="evt-2", source_cursor=2, entity_revision=2,
            payload={"quantity": 50}))
        assert (env1["sequence"], env2["sequence"]) == (1, 2)
        row = state.snapshot_view()["positions"][0]
        assert row["quantity"] == 50
        assert "stale_field" not in row  # replace, never merge

    def test_revision_regression_is_idempotently_ignored(self, state):
        state.apply(_event(entity_revision=5, source_cursor=5))
        assert state.apply(_event(event_id="evt-old", entity_revision=4, source_cursor=6)) is None
        assert state.sequence == 1

    def test_tombstone_removes_entity(self, state):
        state.apply(_event())
        env = state.apply(_event(
            event_id="evt-del", source_cursor=2, entity_revision=2,
            operation="delete", payload=None))
        assert env["operation"] == "delete"
        assert state.snapshot_view()["positions"] == []

    def test_envelope_carries_full_contract(self, state):
        env = state.apply(_event(correlation_id="cmd-9"))
        for key in ("schema_version", "stream_id", "sequence", "event_id",
                    "source_cursor", "entity_revision", "event_type", "entity_type",
                    "entity_id", "operation", "account_id", "source",
                    "source_timestamp", "received_timestamp", "correlation_id", "payload"):
            assert key in env
        assert env["stream_id"] == "stream-a"


class TestLifecycleCollections:
    def test_terminal_proposal_moves_out_of_active(self, state):
        state.apply(_event(
            entity_type="proposal", entity_id="42", event_type="proposal.updated",
            payload={"status": "PENDING", "symbol": "AMD"}))
        assert len(state.snapshot_view()["proposals"]["active"]) == 1
        state.apply(_event(
            event_id="evt-2", source_cursor=2, entity_revision=2,
            entity_type="proposal", entity_id="42", event_type="proposal.updated",
            payload={"status": "REJECTED", "symbol": "AMD"}))
        view = state.snapshot_view()
        assert view["proposals"]["active"] == []
        assert view["proposals"]["terminal"][0]["status"] == "REJECTED"

    def test_terminal_collections_cap_at_500(self, state):
        for i in range(TERMINAL_CAP + 40):
            state.apply(_event(
                event_id=f"evt-{i}", source_cursor=i + 1, entity_type="fill",
                entity_id=f"DU123:exec-{i}", event_type="fill.received",
                payload={"exec_id": f"exec-{i}", "status": "FILLED"}))
        assert len(state.snapshot_view()["fills"]) == TERMINAL_CAP

    def test_terminal_ttl_is_24_hours(self, state):
        state.apply(_event(
            entity_type="fill", entity_id="DU123:exec-1",
            event_type="fill.received", payload={"exec_id": "exec-1"}))
        state._test_clock["mono"] += TERMINAL_TTL_SECONDS + 61
        state.maybe_cleanup()
        assert state.snapshot_view()["fills"] == []


class TestReplayRing:
    def test_replay_after_returns_events_strictly_after_sequence(self, state):
        for i in range(5):
            state.apply(_event(event_id=f"evt-{i}", source_cursor=i + 1,
                               entity_revision=i + 1))
        replay = state.replay_after("stream-a", 3)
        assert [env["sequence"] for env in replay] == [4, 5]

    def test_stream_mismatch_requires_resync(self, state):
        state.apply(_event())
        assert state.replay_after("stream-STALE", 0) is None

    def test_aged_out_cursor_requires_resync(self, state):
        for i in range(REPLAY_RING_MAX_EVENTS + 10):
            state.apply(_event(event_id=f"evt-{i}", source_cursor=i + 1,
                               entity_revision=i + 1))
        assert state.replay_after("stream-a", 0) is None
        assert len(state.replay_after("stream-a", 15)) == REPLAY_RING_MAX_EVENTS - 5

    def test_quotes_never_enter_ring_or_sequence(self, state):
        state.apply_quotes({"265598": {"instrument_id": "265598", "last": 199.5}})
        assert state.sequence == 0
        assert state.replay_after("stream-a", 0) == []
        assert state.snapshot_view()["quotes"]["265598"]["last"] == 199.5


class TestBaseline:
    def test_baseline_installs_rows_and_rotates_stream(self, state):
        snapshot = SnapshotWithCursor(
            source_cursor=88, broker_generation=2,
            entities={"position": [
                {"entity_id": "DU123:4815747", "entity_revision": 7, "quantity": 10}]})
        state.apply(_event())
        state.install_baseline(snapshot, stream_id="stream-b")
        assert state.stream_id == "stream-b"
        assert state.sequence == 0
        assert state.replay_after("stream-a", 0) is None
        assert state.snapshot_view()["positions"][0]["quantity"] == 10
        # revision seeded from baseline: an older journal event must be ignored
        assert state.apply(_event(entity_id="DU123:4815747", entity_revision=6)) is None

    def test_baseline_row_without_identity_fails_loudly(self, state):
        bad = SnapshotWithCursor(
            source_cursor=1, broker_generation=1,
            entities={"position": [{"quantity": 10}]})
        with pytest.raises(ValueError, match="entity_id"):
            state.install_baseline(bad, stream_id="stream-c")
```

- [ ] **Step 2: Run the state tests and verify failure**

Run: `uv run --frozen pytest tests/test_dashboard_state.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'web.command_center.state'`.

- [ ] **Step 3: Implement `web/command_center/state.py`**

```python
"""Loop-owned dashboard read model (spec §5.2, §6, §7).

Only reducer callbacks scheduled on the ASGI loop mutate this state, so a
synchronous read (snapshot_view / replay_after) can never observe a partially
applied event. It is a UI read model, not a second source of truth.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from collections import OrderedDict, deque
from typing import Optional

from trader.domain.events import DomainEvent, SnapshotWithCursor

logger = logging.getLogger("web.command_center.state")

SCHEMA_VERSION = 1
REPLAY_RING_MAX_EVENTS = 10_000
REPLAY_RING_MAX_AGE_SECONDS = 300.0
TERMINAL_CAP = 500
TERMINAL_TTL_SECONDS = 24 * 3600
CLEANUP_INTERVAL_SECONDS = 60.0

ACTIVE_PROPOSAL_STATUSES = {"PENDING", "APPROVED"}
TERMINAL_ORDER_STATUSES = {
    "FILLED", "CANCELLED", "CANCELLED_AFTER_PARTIAL", "REJECTED", "INACTIVE"}


def _utc_iso(epoch_seconds: float) -> str:
    return dt.datetime.fromtimestamp(epoch_seconds, tz=dt.timezone.utc).isoformat()


class _TerminalStore:
    """Insertion-ordered terminal records: cap 500, TTL 24 h."""

    def __init__(self, cap: int = TERMINAL_CAP, ttl: float = TERMINAL_TTL_SECONDS):
        self._cap = cap
        self._ttl = ttl
        self._rows: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    def put(self, entity_id: str, row: dict, now: float) -> None:
        self._rows.pop(entity_id, None)
        self._rows[entity_id] = (now, row)
        while len(self._rows) > self._cap:
            self._rows.popitem(last=False)

    def remove(self, entity_id: str) -> None:
        self._rows.pop(entity_id, None)

    def cleanup(self, now: float) -> None:
        for key in [k for k, (ts, _) in self._rows.items() if now - ts > self._ttl]:
            del self._rows[key]

    def values(self) -> list[dict]:
        return [row for (_, row) in self._rows.values()]


class DashboardState:
    def __init__(self, *, clock=time.time, monotonic=time.monotonic):
        self._clock = clock
        self._monotonic = monotonic
        self.stream_id: str = uuid.uuid4().hex
        self.sequence: int = 0
        self.has_baseline: bool = False
        self.last_event_at: Optional[str] = None
        self.quotes: dict[str, dict] = {}
        self._revisions: dict[tuple[str, str], int] = {}
        self._ring: deque[tuple[int, float, dict]] = deque()
        self._last_cleanup: float = monotonic()
        self._reset_collections()

    def _reset_collections(self) -> None:
        self.accounts: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}
        self.proposals_active: dict[str, dict] = {}
        self.orders_active: dict[str, dict] = {}
        self.strategies: dict[str, dict] = {}
        self.risk: dict[str, dict] = {}
        self.reconciliation: dict[str, dict] = {}
        self.trading_control: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.proposals_terminal = _TerminalStore()
        self.orders_terminal = _TerminalStore()
        self.fills = _TerminalStore()

    # -- baseline -----------------------------------------------------------
    def install_baseline(self, snapshot: SnapshotWithCursor, stream_id: str) -> None:
        for entity_type, rows in snapshot.entities.items():
            for row in rows:
                if "entity_id" not in row or "entity_revision" not in row:
                    raise ValueError(
                        f"snapshot row for {entity_type!r} lacks entity_id/"
                        f"entity_revision — refusing an unfenced baseline")
        self.stream_id = stream_id
        self.sequence = 0
        self._ring.clear()
        self._revisions.clear()
        self._reset_collections()
        now = self._monotonic()
        for entity_type, rows in snapshot.entities.items():
            for row in rows:
                entity_id = str(row["entity_id"])
                self._place(entity_type, entity_id, dict(row), now)
                self._revisions[(entity_type, entity_id)] = int(row["entity_revision"])
        self.has_baseline = True

    # -- reducer ------------------------------------------------------------
    def apply(self, event: DomainEvent) -> Optional[dict]:
        key = (event.entity_type, event.entity_id)
        if event.entity_revision <= self._revisions.get(key, 0):
            return None  # replayed or stale journal row: idempotent ignore
        self._revisions[key] = event.entity_revision
        now = self._monotonic()
        if event.operation == "delete":
            self._remove(event.entity_type, event.entity_id)
        else:
            row = dict(event.payload or {})
            row.setdefault("entity_id", event.entity_id)
            row.setdefault("entity_revision", event.entity_revision)
            self._place(event.entity_type, event.entity_id, row, now)
        envelope = self._envelope(event)
        self._ring.append((envelope["sequence"], now, envelope))
        self._prune_ring(now)
        self.last_event_at = envelope["source_timestamp"]
        self.maybe_cleanup(now)
        return envelope

    def apply_quotes(self, batch: dict[str, dict]) -> None:
        """Latest-value only. Quotes never enter the ring or advance sequence."""
        self.quotes.update(batch)

    def _envelope(self, event: DomainEvent) -> dict:
        self.sequence += 1
        return {
            "schema_version": SCHEMA_VERSION,
            "stream_id": self.stream_id,
            "sequence": self.sequence,
            "event_id": event.event_id,
            "source_cursor": event.source_cursor,
            "entity_revision": event.entity_revision,
            "event_type": event.event_type,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "operation": event.operation,
            "account_id": event.account_id,
            "source": event.source,
            "source_timestamp": event.source_timestamp.isoformat(),
            "received_timestamp": _utc_iso(self._clock()),
            "correlation_id": event.correlation_id,
            "payload": event.payload,
        }

    def _place(self, entity_type: str, entity_id: str, row: dict, now: float) -> None:
        status = str(row.get("status", "")).upper()
        if entity_type == "proposal":
            if status in ACTIVE_PROPOSAL_STATUSES:
                self.proposals_active[entity_id] = row
                self.proposals_terminal.remove(entity_id)
            else:
                self.proposals_active.pop(entity_id, None)
                self.proposals_terminal.put(entity_id, row, now)
        elif entity_type == "order":
            if status in TERMINAL_ORDER_STATUSES:
                self.orders_active.pop(entity_id, None)
                self.orders_terminal.put(entity_id, row, now)
            else:
                self.orders_active[entity_id] = row
        elif entity_type == "fill":
            self.fills.put(entity_id, row, now)
        elif entity_type == "account":
            self.accounts[entity_id] = row
        elif entity_type == "position":
            self.positions[entity_id] = row
        elif entity_type == "strategy":
            self.strategies[entity_id] = row
        elif entity_type == "risk":
            self.risk[entity_id] = row
        elif entity_type == "reconciliation":
            self.reconciliation[entity_id] = row
        elif entity_type == "trading_control":
            self.trading_control[entity_id] = row
        elif entity_type == "command":
            self.commands[entity_id] = row
        else:
            logger.warning("unknown entity_type %r ignored", entity_type)

    def _remove(self, entity_type: str, entity_id: str) -> None:
        for coll in (self.accounts, self.positions, self.proposals_active,
                     self.orders_active, self.strategies, self.risk,
                     self.reconciliation, self.trading_control, self.commands):
            coll.pop(entity_id, None)
        if entity_type == "proposal":
            self.proposals_terminal.remove(entity_id)
        elif entity_type == "order":
            self.orders_terminal.remove(entity_id)
        elif entity_type == "fill":
            self.fills.remove(entity_id)
        self.quotes.pop(entity_id, None)

    # -- replay + views ------------------------------------------------------
    def replay_after(self, stream_id: str, sequence: int) -> Optional[list[dict]]:
        if stream_id != self.stream_id:
            return None
        if sequence >= self.sequence:
            return []
        if not self._ring or self._ring[0][0] > sequence + 1:
            return None  # aged out of the bounded ring
        return [env for (seq, _, env) in self._ring if seq > sequence]

    def snapshot_view(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "stream_id": self.stream_id,
            "sequence": self.sequence,
            "generated_at": _utc_iso(self._clock()),
            "has_baseline": self.has_baseline,
            "last_event_at": self.last_event_at,
            "accounts": list(self.accounts.values()),
            "positions": list(self.positions.values()),
            "quotes": dict(self.quotes),
            "proposals": {"active": list(self.proposals_active.values()),
                          "terminal": self.proposals_terminal.values()},
            "orders": {"active": list(self.orders_active.values()),
                       "terminal": self.orders_terminal.values()},
            "fills": self.fills.values(),
            "strategies": list(self.strategies.values()),
            "risk": dict(self.risk),
            "reconciliation": list(self.reconciliation.values()),
            "trading_control": list(self.trading_control.values()),
            "commands": list(self.commands.values()),
        }

    # -- retention -----------------------------------------------------------
    def _prune_ring(self, now: float) -> None:
        while len(self._ring) > REPLAY_RING_MAX_EVENTS:
            self._ring.popleft()
        while self._ring and now - self._ring[0][1] > REPLAY_RING_MAX_AGE_SECONDS:
            self._ring.popleft()

    def maybe_cleanup(self, now: Optional[float] = None) -> None:
        now = self._monotonic() if now is None else now
        if now - self._last_cleanup < CLEANUP_INTERVAL_SECONDS:
            return
        self._last_cleanup = now
        for store in (self.proposals_terminal, self.orders_terminal, self.fills):
            store.cleanup(now)
        self._prune_ring(now)
```

- [ ] **Step 4: Run the state tests**

Run: `uv run --frozen pytest tests/test_dashboard_state.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/command_center/state.py tests/test_dashboard_state.py
git commit -m "feat(m1-r): bounded dashboard read model and reducer"
```

### Task 3: `DashboardEventBridge` — fenced baseline, long-poll tail, lifecycle, backoff

**Files:**
- Create: `web/command_center/bridge.py`
- Create: `tests/test_dashboard_bridge.py`

**Interfaces:**
- Consumes: `TypedRpcClient.call(method, body, response_model)` (`[G0]`) over query `tcp://<trader>:42101` and feed `tcp://<trader>:42103`; typed methods `snapshot_with_cursor` (raises/returns error code `SNAPSHOT_NOT_READY` until a complete broker generation exists — `[M1-F1]` Task 4), `read_domain_events(after_cursor, limit, wait_ms) -> ReadDomainEventsResult` with fields `events: tuple[DomainEvent, ...]` and `newest_cursor: int` (`[M1-F1]` Task 5), and `get_quotes_snapshot() -> {"quotes": {instrument_id: quote_row}}`.
- Consumes: `DashboardState` (Task 2) and the fan-out protocol `publish(envelope)` / `broadcast_resync()` (satisfied by `SseFanout` in Task 5; tests use a recorder fake).
- Produces: `BridgeLifecycle(str, Enum)` with `STARTING`, `SYNCHRONIZING`, `LIVE`, `DEGRADED`, `DISCONNECTED`.
- Produces: `DashboardEventBridge(query_client, feed_client, state, fanout, loop, *, poll_limit=500, wait_ms=10_000, backoff_min=0.5, backoff_max=30.0, disconnected_after=60.0, rng=random.random, monotonic=time.monotonic)` with `start() -> None`, `stop(timeout: float = 5.0) -> None`, `lifecycle -> BridgeLifecycle`, `health() -> dict`.
- Guarantees: baseline installed only from a fenced snapshot; the journal is tailed strictly after the snapshot cursor; every resync rotates `stream_id` and broadcasts resync to connected clients; reconnects use exponential backoff with jitter; all state mutation crosses via `loop.call_soon_threadsafe`.

- [ ] **Step 1: Write failing bridge tests**

Create `tests/test_dashboard_bridge.py`:

```python
import asyncio
import datetime as dt
import queue
import threading
import time

import pytest

from trader.domain.events import DomainEvent, ReadDomainEventsResult, SnapshotWithCursor
from web.command_center.bridge import BridgeLifecycle, DashboardEventBridge
from web.command_center.state import DashboardState

UTC = dt.timezone.utc


def _event(cursor: int, revision: int = 1, entity_id: str = "DU123:265598") -> DomainEvent:
    return DomainEvent(
        event_id=f"evt-{cursor}", source_cursor=cursor, entity_revision=revision,
        event_type="position.updated", entity_type="position", entity_id=entity_id,
        operation="upsert", account_id="DU123", source="trader_service",
        source_timestamp=dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        correlation_id=None, payload={"quantity": cursor})


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeQueryClient:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)  # SnapshotWithCursor | Exception
        self.closed = False

    def call(self, method, body, response_model):
        if method == "snapshot_with_cursor":
            item = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
            if isinstance(item, Exception):
                raise item
            return item
        if method == "get_quotes_snapshot":
            return {"quotes": {"265598": {"instrument_id": "265598", "last": 100.0}}}
        raise AssertionError(f"unexpected query method {method}")

    def close(self):
        self.closed = True


class ScriptedFeed:
    """Feed client returning scripted batches; blocks like a real long-poll."""

    def __init__(self):
        self.items: "queue.Queue[object]" = queue.Queue()
        self.calls: list[dict] = []
        self.closed = False

    def call(self, method, body, response_model):
        assert method == "read_domain_events"
        self.calls.append(dict(body))
        item = self.items.get(timeout=2)
        if isinstance(item, Exception):
            raise item
        events = tuple(e for e in item if e.source_cursor > body["after_cursor"])
        newest = max([e.source_cursor for e in events], default=body["after_cursor"])
        return ReadDomainEventsResult(events=events, newest_cursor=newest)

    def close(self):
        self.closed = True


class RecordingFanout:
    def __init__(self):
        self.published: list[dict] = []
        self.resyncs = 0

    def publish(self, envelope):
        self.published.append(envelope)

    def publish_quotes(self, batch):
        pass

    def broadcast_resync(self):
        self.resyncs += 1


@pytest.fixture
def loop_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


def _snapshot(cursor: int = 10) -> SnapshotWithCursor:
    return SnapshotWithCursor(
        source_cursor=cursor, broker_generation=1,
        entities={"position": [
            {"entity_id": "DU123:265598", "entity_revision": 3, "quantity": 5}]})


def _bridge(loop, query, feed, state=None, fanout=None, **kwargs):
    state = state or DashboardState()
    fanout = fanout or RecordingFanout()
    bridge = DashboardEventBridge(
        query, feed, state, fanout, loop,
        backoff_min=0.01, backoff_max=0.05, **kwargs)
    return bridge, state, fanout


class TestBaselineAndTail:
    def test_fenced_baseline_installs_before_tailing(self, loop_thread):
        feed = ScriptedFeed()
        bridge, state, _ = _bridge(loop_thread, FakeQueryClient([_snapshot(10)]), feed)
        bridge.start()
        try:
            assert wait_until(lambda: state.has_baseline)
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            assert wait_until(lambda: feed.calls and feed.calls[0]["after_cursor"] == 10)
        finally:
            feed.items.put(ConnectionError("stop"))
            bridge.stop()

    def test_events_apply_and_cursor_advances(self, loop_thread):
        feed = ScriptedFeed()
        bridge, state, fanout = _bridge(loop_thread, FakeQueryClient([_snapshot(10)]), feed)
        bridge.start()
        try:
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            feed.items.put([_event(11, revision=4), _event(12, revision=5)])
            assert wait_until(lambda: len(fanout.published) == 2)
            assert wait_until(lambda: len(feed.calls) >= 2
                              and feed.calls[-1]["after_cursor"] == 12)
            assert state.positions["DU123:265598"]["quantity"] == 12
        finally:
            feed.items.put(ConnectionError("stop"))
            bridge.stop()

    def test_stale_revision_is_ignored_not_published(self, loop_thread):
        feed = ScriptedFeed()
        bridge, state, fanout = _bridge(loop_thread, FakeQueryClient([_snapshot(10)]), feed)
        bridge.start()
        try:
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            feed.items.put([_event(11, revision=2)])  # baseline already at revision 3
            assert wait_until(lambda: len(feed.calls) >= 2)
            assert fanout.published == []
            assert state.positions["DU123:265598"]["quantity"] == 5
        finally:
            feed.items.put(ConnectionError("stop"))
            bridge.stop()


class TestFailureHandling:
    def test_snapshot_not_ready_keeps_synchronizing_without_partial_state(self, loop_thread):
        query = FakeQueryClient([RuntimeError("SNAPSHOT_NOT_READY"), _snapshot(10)])
        feed = ScriptedFeed()
        bridge, state, _ = _bridge(loop_thread, query, feed)
        bridge.start()
        try:
            assert wait_until(lambda: state.has_baseline)
            assert bridge.health()["reconnects"] >= 1
        finally:
            feed.items.put(ConnectionError("stop"))
            bridge.stop()

    def test_feed_failure_degrades_then_resyncs_with_new_stream_id(self, loop_thread):
        feed = ScriptedFeed()
        bridge, state, fanout = _bridge(loop_thread, FakeQueryClient([_snapshot(10)]), feed)
        bridge.start()
        try:
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            first_stream = state.stream_id
            feed.items.put(ConnectionError("journal gone"))
            assert wait_until(lambda: state.stream_id != first_stream)
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            assert fanout.resyncs >= 2  # initial baseline + post-failure resync
        finally:
            feed.items.put(ConnectionError("stop"))
            bridge.stop()

    def test_health_reports_safe_error_and_age(self, loop_thread):
        feed = ScriptedFeed()
        bridge, _, _ = _bridge(loop_thread, FakeQueryClient([_snapshot(10)]), feed)
        bridge.start()
        try:
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            feed.items.put(ConnectionError("journal gone"))
            assert wait_until(
                lambda: bridge.health()["sources"]["journal"]["last_error"] is not None)
            health = bridge.health()
            assert "ConnectionError" in health["sources"]["journal"]["last_error"]
            assert set(health) >= {"lifecycle", "reconnects", "cursor", "sources"}
        finally:
            feed.items.put(ConnectionError("stop"))
            bridge.stop()

    def test_stop_joins_thread_and_closes_nothing_it_does_not_own(self, loop_thread):
        feed = ScriptedFeed()
        bridge, _, _ = _bridge(loop_thread, FakeQueryClient([_snapshot(10)]), feed)
        bridge.start()
        assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
        feed.items.put(ConnectionError("stop"))
        bridge.stop()
        assert not bridge.is_alive()
```

- [ ] **Step 2: Run the bridge tests and verify failure**

Run: `uv run --frozen pytest tests/test_dashboard_bridge.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'web.command_center.bridge'`.

- [ ] **Step 3: Implement `web/command_center/bridge.py`**

```python
"""Journal bridge: the dashboard's only consumer of domain updates (spec §5.1, §11).

Runs on a dedicated thread. Never touches DuckDB, ZMQ raw-object RPC, or dill:
its only inputs are the typed query socket (fenced snapshot + quote baseline)
and the typed long-poll feed socket. State mutation crosses into the ASGI loop
exclusively via loop.call_soon_threadsafe.
"""
from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from trader.domain.events import ReadDomainEventsResult, SnapshotWithCursor

logger = logging.getLogger("web.command_center.bridge")


class BridgeLifecycle(str, Enum):
    STARTING = "starting"
    SYNCHRONIZING = "synchronizing"
    LIVE = "live"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"


def _safe_error(exc: BaseException) -> str:
    """Class + truncated message; never interpolates secrets or payloads."""
    return f"{type(exc).__name__}: {str(exc)[:200]}"


@dataclass
class _SourceHealth:
    state: str = "unknown"
    last_success: Optional[float] = None
    last_error: Optional[str] = None
    reconnects: int = 0


class DashboardEventBridge:
    def __init__(self, query_client, feed_client, state, fanout, loop, *,
                 poll_limit: int = 500, wait_ms: int = 10_000,
                 backoff_min: float = 0.5, backoff_max: float = 30.0,
                 disconnected_after: float = 60.0,
                 rng=random.random, monotonic=time.monotonic):
        self._query = query_client
        self._feed = feed_client
        self._state = state
        self._fanout = fanout
        self._loop = loop
        self._poll_limit = poll_limit
        self._wait_ms = wait_ms
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self._disconnected_after = disconnected_after
        self._rng = rng
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cursor: Optional[int] = None
        self._reconnects = 0
        self.lifecycle = BridgeLifecycle.STARTING
        self._sources: dict[str, _SourceHealth] = {
            "journal": _SourceHealth(), "snapshot": _SourceHealth()}

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="cc-event-bridge", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        backoff = self._backoff_min
        while not self._stop.is_set():
            try:
                self.lifecycle = BridgeLifecycle.SYNCHRONIZING
                self._resync_baseline()
                backoff = self._backoff_min
                self.lifecycle = BridgeLifecycle.LIVE
                self._tail()
            except Exception as exc:  # noqa: BLE001 — every failure degrades, retries
                if self._stop.is_set():
                    break
                self._record_error("journal", exc)
                self._reconnects += 1
                delay = min(backoff, self._backoff_max) * (0.5 + self._rng())
                logger.warning("bridge degraded (%s); retrying in %.2fs",
                               _safe_error(exc), delay)
                self._stop.wait(delay)
                backoff = min(backoff * 2, self._backoff_max)

    # -- fenced baseline --------------------------------------------------------
    def _resync_baseline(self) -> None:
        baseline: SnapshotWithCursor = self._query.call(
            "snapshot_with_cursor", {}, SnapshotWithCursor)
        quotes = self._query.call("get_quotes_snapshot", {}, dict)
        stream_id = uuid.uuid4().hex  # every resync rotates the stream
        installed = threading.Event()

        def _install() -> None:
            # Existing clients must resnapshot: their sequence space is gone.
            self._fanout.broadcast_resync()
            self._state.install_baseline(baseline, stream_id)
            self._state.apply_quotes(dict(quotes.get("quotes") or {}))
            installed.set()

        self._loop.call_soon_threadsafe(_install)
        if not installed.wait(timeout=10):
            raise TimeoutError("reducer loop did not install baseline within 10s")
        self._cursor = baseline.source_cursor
        self._record_success("snapshot")

    # -- long-poll tail ---------------------------------------------------------
    def _tail(self) -> None:
        while not self._stop.is_set():
            result: ReadDomainEventsResult = self._feed.call(
                "read_domain_events",
                {"after_cursor": self._cursor, "limit": self._poll_limit,
                 "wait_ms": self._wait_ms},
                ReadDomainEventsResult)
            self._record_success("journal")
            if not result.events:
                continue  # ten-second empty heartbeat
            self._cursor = result.events[-1].source_cursor
            events = tuple(result.events)
            applied = threading.Event()

            def _apply() -> None:
                for event in events:
                    envelope = self._state.apply(event)
                    if envelope is not None:  # stale revisions ignored idempotently
                        self._fanout.publish(envelope)
                applied.set()

            self._loop.call_soon_threadsafe(_apply)
            applied.wait(timeout=10)  # natural backpressure: one batch in flight

    # -- health -------------------------------------------------------------------
    def _record_success(self, source: str) -> None:
        health = self._sources[source]
        health.state = "ok"
        health.last_success = self._monotonic()
        health.last_error = None

    def _record_error(self, source: str, exc: BaseException) -> None:
        health = self._sources[source]
        health.state = "error"
        health.last_error = _safe_error(exc)
        health.reconnects += 1
        if (health.last_success is not None
                and self._monotonic() - health.last_success > self._disconnected_after):
            self.lifecycle = BridgeLifecycle.DISCONNECTED
        else:
            self.lifecycle = BridgeLifecycle.DEGRADED

    def health(self) -> dict:
        now = self._monotonic()
        sources = {}
        for name, h in self._sources.items():
            age = None if h.last_success is None else round(now - h.last_success, 3)
            sources[name] = {"state": h.state, "last_success_age_seconds": age,
                             "last_error": h.last_error, "reconnects": h.reconnects}
        return {"lifecycle": self.lifecycle.value, "reconnects": self._reconnects,
                "cursor": self._cursor, "sources": sources}
```

- [ ] **Step 4: Run the bridge tests**

Run: `uv run --frozen pytest tests/test_dashboard_bridge.py -q`

Expected: PASS (no flakes across `-q --count`-free repeat runs; timings use `wait_until`, never bare sleeps).

- [ ] **Step 5: Commit**

```bash
git add web/command_center/bridge.py tests/test_dashboard_bridge.py
git commit -m "feat(m1-r): journal bridge with fenced baseline and backoff"
```

### Task 4: Quote plane — conflated PubSub subscriber off the ASGI loop

**Files:**
- Create: `web/command_center/quotes.py`
- Modify: `tests/test_dashboard_bridge.py` (append quote-plane test classes)

**Interfaces:**
- Consumes: trader ticker PubSub on port `42002` (`[M1-F2]` guarantees every open position is covered). Payload decoding is a seam: the default decoder refuses to run unless `MMR_DILL_STRICT=1` so an `EXT_OBJECT` dill payload can never execute in the dashboard process.
- Produces: `QuotePlane(address: str, port: int, loop, deliver: Callable[[dict[str, dict]], None], *, hz: float = 4.0, topic: str = "", decode: Callable[[bytes], object] | None = None)` with `start()`, `stop(timeout: float = 5.0)`, `ingest(obj) -> None`, `flush_due(now: float) -> bool`, and counter attribute `dropped: int`.
- Produces: `normalize_ticker(obj) -> dict | None` and `clamp_hz(hz: float) -> float` (clamps to 2.0–5.0).
- Guarantees: conflation is latest-value per canonical instrument; delivery crosses to the loop via `loop.call_soon_threadsafe(deliver, batch)` at most once per `1/hz` seconds; malformed payloads are dropped and counted, never guessed.

- [ ] **Step 1: Append failing quote-plane tests**

Append to `tests/test_dashboard_bridge.py`:

```python
import msgpack
import zmq

from web.command_center.quotes import QuotePlane, clamp_hz, normalize_ticker


class _CollectingLoop:
    """Minimal loop stand-in: records deliveries synchronously."""

    def __init__(self):
        self.batches: list[dict] = []

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class TestQuoteNormalization:
    def test_dict_ticker_normalizes(self):
        quote = normalize_ticker({
            "conId": 265598, "bid": 199.4, "ask": 199.6, "last": 199.5,
            "time": "2026-07-15T14:30:00+00:00", "feed": "live"})
        assert quote == {
            "instrument_id": "265598", "bid": 199.4, "ask": 199.6, "last": 199.5,
            "market_timestamp": "2026-07-15T14:30:00+00:00", "feed_type": "live"}

    def test_unknown_shape_returns_none(self):
        assert normalize_ticker({"nothing": "useful"}) is None
        assert normalize_ticker(b"garbage") is None

    def test_hz_clamped_to_two_to_five(self):
        assert clamp_hz(0.5) == 2.0
        assert clamp_hz(4.0) == 4.0
        assert clamp_hz(60.0) == 5.0


class TestQuoteConflation:
    def _plane(self, hz=4.0):
        loop = _CollectingLoop()
        plane = QuotePlane("tcp://127.0.0.1", 0, loop,
                           lambda batch: loop.batches.append(batch), hz=hz,
                           decode=msgpack.unpackb)
        return plane, loop

    def test_conflation_keeps_latest_value_only(self):
        plane, loop = self._plane()
        for last in (100.0, 101.0, 102.5):
            plane.ingest({"conId": 265598, "bid": last - 0.1, "ask": last + 0.1,
                          "last": last})
        assert plane.flush_due(now=10.0)
        assert len(loop.batches) == 1
        assert loop.batches[0]["265598"]["last"] == 102.5

    def test_flush_respects_rate(self):
        plane, loop = self._plane(hz=4.0)  # interval 0.25s
        plane.ingest({"conId": 1, "last": 1.0})
        assert plane.flush_due(now=10.0)
        plane.ingest({"conId": 1, "last": 2.0})
        assert not plane.flush_due(now=10.1)   # too soon
        assert plane.flush_due(now=10.26)
        assert [b["1"]["last"] for b in loop.batches] == [1.0, 2.0]

    def test_malformed_payloads_dropped_and_counted(self):
        plane, loop = self._plane()
        plane.ingest({"useless": True})
        plane.ingest(12345)
        assert plane.dropped == 2
        assert not plane.flush_due(now=10.0)
        assert loop.batches == []


class TestQuoteSocketRoundTrip:
    def test_subscriber_thread_receives_published_quote(self):
        ctx = zmq.Context()
        pub = ctx.socket(zmq.PUB)
        port = pub.bind_to_random_port("tcp://127.0.0.1")
        loop = _CollectingLoop()
        received = threading.Event()

        def deliver(batch):
            loop.batches.append(batch)
            received.set()

        plane = QuotePlane("tcp://127.0.0.1", port, loop, deliver,
                           hz=5.0, decode=msgpack.unpackb)
        plane.start()
        try:
            deadline = time.monotonic() + 3
            payload = msgpack.packb({"conId": 4815747, "last": 172.4})
            while not received.is_set() and time.monotonic() < deadline:
                pub.send_multipart([b"", payload])  # re-send until SUB connects
                time.sleep(0.05)
            assert received.is_set()
            assert loop.batches[-1]["4815747"]["last"] == 172.4
        finally:
            plane.stop()
            pub.close(0)
            ctx.term()
```

- [ ] **Step 2: Run the quote tests and verify failure**

Run: `uv run --frozen pytest tests/test_dashboard_bridge.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'web.command_center.quotes'` (Task 3 tests keep passing).

- [ ] **Step 3: Implement `web/command_center/quotes.py`**

```python
"""Conflated quote plane (spec §5.1, §6 quote.updated row, §7 bounds).

A dedicated SUB thread drains ticker PubSub, conflates to a latest-value map
per canonical instrument, and hands batches to the loop at 2-5 Hz. Quotes are
ephemeral: they never touch the journal, the replay ring, or client FIFOs.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

import zmq

logger = logging.getLogger("web.command_center.quotes")

DEFAULT_QUOTE_HZ = 4.0


def clamp_hz(hz: float) -> float:
    return min(5.0, max(2.0, float(hz)))


def _default_decode(payload: bytes):
    if os.environ.get("MMR_DILL_STRICT") != "1":
        raise RuntimeError(
            "command center requires MMR_DILL_STRICT=1 — the dashboard never "
            "executes dill payloads (set it in the dashboard service environment)")
    from trader.messaging.clientserver import unpack
    return unpack(payload)


_CONID_KEYS = ("conId", "conid", "con_id", "instrument_id")
_TIME_KEYS = ("time", "market_time", "timestamp", "date")


def normalize_ticker(obj) -> Optional[dict]:
    """Normalize a PubSub ticker payload to the quote row of spec §5.4.

    Handles plain dicts and attribute-style objects. Anything without a
    resolvable canonical conId is dropped (never keyed by display symbol).
    """
    get = obj.get if isinstance(obj, dict) else (
        lambda key, default=None: getattr(obj, key, default))
    try:
        con_id = next((get(k) for k in _CONID_KEYS if get(k) is not None), None)
        if con_id is None:
            return None
        market_time = next((get(k) for k in _TIME_KEYS if get(k) is not None), None)
        return {
            "instrument_id": str(int(con_id)),
            "bid": _num(get("bid")),
            "ask": _num(get("ask")),
            "last": _num(get("last")),
            "market_timestamp": str(market_time) if market_time is not None else None,
            "feed_type": get("feed") or get("feed_type"),
        }
    except (TypeError, ValueError, AttributeError):
        return None


def _num(value) -> Optional[float]:
    if value is None:
        return None
    number = float(value)
    return number if number == number else None  # NaN -> None


class QuotePlane:
    def __init__(self, address: str, port: int, loop,
                 deliver: Callable[[dict], None], *,
                 hz: float = DEFAULT_QUOTE_HZ, topic: str = "",
                 decode: Optional[Callable[[bytes], object]] = None):
        self._endpoint = f"{address}:{port}"
        self._loop = loop
        self._deliver = deliver
        self._interval = 1.0 / clamp_hz(hz)
        self._topic = topic
        self._decode = decode or _default_decode
        self._pending: dict[str, dict] = {}
        self._last_flush = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.dropped = 0

    # Testable units — the thread loop is just poll -> ingest -> flush_due.
    def ingest(self, obj) -> None:
        quote = normalize_ticker(obj)
        if quote is None:
            self.dropped += 1
            return
        self._pending[quote["instrument_id"]] = quote  # conflate: latest wins

    def flush_due(self, now: float) -> bool:
        if not self._pending or now - self._last_flush < self._interval:
            return False
        batch, self._pending = self._pending, {}
        self._last_flush = now
        self._loop.call_soon_threadsafe(self._deliver, batch)
        return True

    # -- thread ---------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="cc-quote-plane", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        ctx = zmq.Context()
        sock = ctx.socket(zmq.SUB)
        sock.connect(self._endpoint)
        sock.setsockopt_string(zmq.SUBSCRIBE, self._topic)
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                for _sock, _mask in poller.poll(timeout=50):
                    frames = sock.recv_multipart(zmq.NOBLOCK)
                    if len(frames) < 2:
                        continue
                    try:
                        self.ingest(self._decode(frames[1]))
                    except Exception:  # noqa: BLE001 — malformed payloads drop
                        self.dropped += 1
                self.flush_due(time.monotonic())
        finally:
            sock.close(0)
            ctx.term()
```

- [ ] **Step 4: Run the combined bridge + quote tests**

Run: `uv run --frozen pytest tests/test_dashboard_bridge.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/command_center/quotes.py tests/test_dashboard_bridge.py
git commit -m "feat(m1-r): conflated quote plane off the ASGI loop"
```

### Task 5: SSE fan-out, `/api/snapshot`, `/api/events`, and app wiring

**Files:**
- Create: `web/command_center/sse.py`
- Create: `web/command_center/routes_read.py`
- Modify: `web/command_center/__init__.py` (CommandCenter container + lifespan)
- Modify: `web/app.py` (app factory, static mount, router/middleware wiring, single-worker + graceful-shutdown enforcement)
- Modify: `pyproject.toml` + `uv.lock` (add `sse-starlette==2.1.3` as a locked runtime dependency)
- Modify: `tests/test_web_dashboard.py` (fixture only: authenticated test app via `create_app`)
- Create: `tests/test_dashboard_sse.py`
- Create: `tests/test_dashboard_snapshot_api.py`

**Interfaces:**
- Consumes: `DashboardState` (Task 2), `DashboardEventBridge` (Task 3), `QuotePlane` (Task 4), session module (Task 1), `TypedRpcClient` (`[G0]`).
- Produces: `SseClient` with `fifo: deque[dict]`, `quote_map: dict[str, dict]`, `wake: asyncio.Event`, `resync: bool`, `closed: bool`.
- Produces: `SseFanout(state: DashboardState)` with `register(last_event_id: str | None) -> tuple[SseClient, list[dict] | None, dict[str, dict]]`, `unregister(client: SseClient) -> None`, `publish(envelope: dict) -> None`, `publish_quotes(batch: dict[str, dict]) -> None`, `broadcast_resync() -> None`, `client_count() -> int`, and constant `FIFO_LIMIT = 1000`. All methods run only on the ASGI loop — registration is therefore atomic with respect to the reducer.
- Produces: `CommandCenter(config: CommandCenterConfig, *, credentials_loader=load_dashboard_credentials, query_client_factory=None, feed_client_factory=None, quote_plane_factory=None, command_gateway_factory=None)` with attributes `state`, `fanout`, `bridge`, `quote_plane`, `session_manager`, `limiter`, `require_session`, `command_gateway` and async context manager method `lifespan(app)`. `command_gateway_factory` is the reserved seam `[M1-C]` fills with `DashboardCommandGateway`; `[M1-R]` leaves it `None`.
- Produces: `CommandCenterConfig.from_env()` reading `MMR_TYPED_QUERY_ENDPOINT` (default `tcp://127.0.0.1:42101`), `MMR_TYPED_FEED_ENDPOINT` (default `tcp://127.0.0.1:42103`), `MMR_PUBSUB_ADDRESS`/`MMR_PUBSUB_PORT` (default `tcp://127.0.0.1`/`42002`), `CC_QUOTE_HZ` (default 4.0), `DASHBOARD_COOKIE_SECURE`.
- Produces: `create_read_router(cc: CommandCenter, templates: Jinja2Templates) -> APIRouter` serving `GET /api/snapshot` (also THE degraded polling fallback) and `GET /api/events`.
- Produces: `create_app(cc: CommandCenter | None = None) -> FastAPI` in `web/app.py`; module-level `app = create_app()` is preserved for `python3 -m web.app`.

- [ ] **Step 1: Write failing fan-out and handshake tests**

Create `tests/test_dashboard_sse.py`:

```python
import asyncio
import datetime as dt
import json

import pytest

from trader.domain.events import DomainEvent, SnapshotWithCursor
from web.command_center.sse import FIFO_LIMIT, SseFanout
from web.command_center.state import DashboardState

UTC = dt.timezone.utc


def _event(cursor: int, revision: int) -> DomainEvent:
    return DomainEvent(
        event_id=f"evt-{cursor}", source_cursor=cursor, entity_revision=revision,
        event_type="position.updated", entity_type="position",
        entity_id="DU123:265598", operation="upsert", account_id="DU123",
        source="trader_service",
        source_timestamp=dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        correlation_id=None, payload={"quantity": cursor})


@pytest.fixture
def state():
    s = DashboardState()
    s.install_baseline(
        SnapshotWithCursor(source_cursor=0, broker_generation=1, entities={}),
        stream_id="stream-a")
    return s


@pytest.fixture
def fanout(state):
    return SseFanout(state)


def _apply_and_publish(state, fanout, event):
    envelope = state.apply(event)
    if envelope is not None:
        fanout.publish(envelope)
    return envelope


class TestHandshake:
    def test_replay_cutover_is_exact_no_gap_no_duplicate(self, state, fanout):
        for i in range(1, 4):
            _apply_and_publish(state, fanout, _event(i, i))
        client, replay, quotes = fanout.register("stream-a:1")
        _apply_and_publish(state, fanout, _event(4, 4))
        replay_seqs = [env["sequence"] for env in replay]
        live_seqs = [env["sequence"] for env in client.fifo]
        assert replay_seqs == [2, 3]
        assert live_seqs == [4]

    def test_register_captures_current_quote_map(self, state, fanout):
        fanout.publish_quotes({"265598": {"instrument_id": "265598", "last": 9.9}})
        _, _, quotes = fanout.register(None)
        assert quotes["265598"]["last"] == 9.9

    def test_stale_stream_or_aged_cursor_returns_none_replay(self, state, fanout):
        _apply_and_publish(state, fanout, _event(1, 1))
        _, replay, _ = fanout.register("stream-OLD:1")
        assert replay is None


class TestFifoBounds:
    def test_fifo_overflow_clears_client_and_marks_resync(self, state, fanout):
        client, _, _ = fanout.register(None)
        for i in range(1, FIFO_LIMIT + 2):
            _apply_and_publish(state, fanout, _event(i, i))
        assert client.resync is True
        assert len(client.fifo) == 0

    def test_quotes_never_consume_fifo_capacity(self, state, fanout):
        client, _, _ = fanout.register(None)
        for i in range(5000):
            fanout.publish_quotes({str(i): {"instrument_id": str(i), "last": 1.0}})
        assert client.resync is False
        assert len(client.fifo) == 0
        assert len(client.quote_map) == 5000

    def test_slow_client_never_blocks_others(self, state, fanout):
        slow, _, _ = fanout.register(None)
        fast, _, _ = fanout.register(None)
        for i in range(1, FIFO_LIMIT + 2):
            _apply_and_publish(state, fanout, _event(i, i))
        assert slow.resync is True
        assert fast.resync is True  # both overflowed independently
        fresh, replay, _ = fanout.register(f"stream-a:{state.sequence}")
        assert replay == [] and fresh.resync is False


class TestResyncBroadcast:
    def test_broadcast_resync_flags_every_client(self, state, fanout):
        a, _, _ = fanout.register(None)
        b, _, _ = fanout.register(None)
        fanout.broadcast_resync()
        assert a.resync and b.resync

    def test_unregister_removes_client(self, state, fanout):
        client, _, _ = fanout.register(None)
        fanout.unregister(client)
        assert fanout.client_count() == 0
        _apply_and_publish(state, fanout, _event(1, 1))
        assert len(client.fifo) == 0
```

- [ ] **Step 2: Write failing endpoint tests**

Create `tests/test_dashboard_snapshot_api.py`:

```python
import asyncio
import datetime as dt
import json

import httpx
import pytest

from trader.domain.events import DomainEvent, SnapshotWithCursor
from web.command_center import CommandCenter, CommandCenterConfig
from web.command_center.session import DashboardCredentials

UTC = dt.timezone.utc
SECRET = "s" * 64
TOKEN = "test-token"


class _NullBridge:
    def health(self):
        return {"lifecycle": "live", "reconnects": 0, "cursor": 1,
                "sources": {"journal": {"state": "ok",
                                        "last_success_age_seconds": 0.1,
                                        "last_error": None, "reconnects": 0}}}

    def stop(self, timeout=5.0):
        pass


class _NullQuotePlane:
    dropped = 0

    def start(self):
        pass

    def stop(self, timeout=5.0):
        pass


@pytest.fixture
def cc(monkeypatch):
    monkeypatch.setenv("MMR_DILL_STRICT", "1")
    center = CommandCenter(
        CommandCenterConfig(),
        credentials_loader=lambda: DashboardCredentials(
            token=TOKEN, session_secret=SECRET.encode(), legacy_alias_used=False),
        query_client_factory=lambda: None,
        feed_client_factory=lambda: None,
        bridge_factory=lambda *a, **k: _NullBridge(),
        quote_plane_factory=lambda loop, deliver: _NullQuotePlane(),
    )
    return center


@pytest.fixture
def app(cc):
    from web.app import create_app
    return create_app(cc)


def _seed(cc, positions=1):
    cc.state.install_baseline(
        SnapshotWithCursor(source_cursor=0, broker_generation=1, entities={
            "position": [{"entity_id": f"DU123:{i}", "entity_revision": 1,
                          "quantity": 10 * i, "currency": "USD"}
                         for i in range(1, positions + 1)],
            "account": [{"entity_id": "DU123", "entity_revision": 1,
                         "net_liquidation": 50_000.0, "mode": "paper"}],
        }),
        stream_id="stream-t")


async def _login(client):
    response = await client.post("/session", data={"token": TOKEN})
    assert response.status_code in (200, 303)


@pytest.fixture
def client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://cc")


class TestSnapshotApi:
    @pytest.mark.asyncio
    async def test_snapshot_requires_session(self, client, cc):
        _seed(cc)
        async with client as c:
            assert (await c.get("/api/snapshot")).status_code == 401

    @pytest.mark.asyncio
    async def test_snapshot_503_before_baseline(self, client, cc):
        async with client as c:
            await _login(c)
            assert (await c.get("/api/snapshot")).status_code == 503

    @pytest.mark.asyncio
    async def test_snapshot_shape_and_polling_fallback(self, client, cc):
        _seed(cc)
        async with client as c:
            await _login(c)
            body = (await c.get("/api/snapshot")).json()
            for key in ("schema_version", "stream_id", "sequence", "generated_at",
                        "accounts", "positions", "quotes", "proposals", "orders",
                        "fills", "strategies", "risk", "health"):
                assert key in body
            assert body["accounts"][0]["net_liquidation"] == 50_000.0
            # polling fallback sees new events on the next poll
            envelope = cc.state.apply(DomainEvent(
                event_id="evt-9", source_cursor=9, entity_revision=2,
                event_type="position.updated", entity_type="position",
                entity_id="DU123:1", operation="upsert", account_id="DU123",
                source="trader_service",
                source_timestamp=dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
                correlation_id=None, payload={"quantity": 77}))
            cc.fanout.publish(envelope)
            second = (await c.get("/api/snapshot")).json()
            assert second["sequence"] == body["sequence"] + 1
            assert second["positions"][0]["quantity"] == 77


class TestEventsEndpoint:
    @pytest.mark.asyncio
    async def test_sse_handshake_replays_then_snapshots_quotes(self, client, cc):
        _seed(cc)
        envelope = cc.state.apply(DomainEvent(
            event_id="evt-1", source_cursor=1, entity_revision=2,
            event_type="position.updated", entity_type="position",
            entity_id="DU123:1", operation="upsert", account_id="DU123",
            source="trader_service",
            source_timestamp=dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
            correlation_id=None, payload={"quantity": 5}))
        cc.fanout.publish(envelope)
        cc.fanout.publish_quotes({"1": {"instrument_id": "1", "last": 42.0}})
        async with client as c:
            await _login(c)
            frames = []
            async with c.stream("GET", "/api/events?after=stream-t:0") as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    frames.append(line)
                    if line.startswith("event: quotes.snapshot"):
                        break
            joined = "\n".join(frames)
            assert "id: stream-t:1" in joined            # replayed domain event
            assert "event: quotes.snapshot" in joined
            snap_index = joined.index("event: quotes.snapshot")
            assert "id:" not in joined[snap_index:]       # control frame has no SSE id

    @pytest.mark.asyncio
    async def test_stale_last_event_id_gets_resync_required(self, client, cc):
        _seed(cc)
        async with client as c:
            await _login(c)
            frames = []
            async with c.stream("GET", "/api/events",
                                headers={"last-event-id": "dead-stream:5"}) as response:
                async for line in response.aiter_lines():
                    frames.append(line)
                    if line.startswith("event: resync_required"):
                        break
            assert any(line.startswith("event: resync_required") for line in frames)

    @pytest.mark.asyncio
    async def test_generator_unregisters_client_on_disconnect(self, client, cc):
        _seed(cc)
        async with client as c:
            await _login(c)
            async with c.stream("GET", "/api/events") as response:
                async for line in response.aiter_lines():
                    if line.startswith("event: quotes.snapshot"):
                        break
            await asyncio.sleep(0.05)  # let the finally block run
        assert cc.fanout.client_count() == 0
```

- [ ] **Step 3: Run both new test files and verify failure**

Run: `uv run --frozen pytest tests/test_dashboard_sse.py tests/test_dashboard_snapshot_api.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'web.command_center.sse'` and `ImportError: cannot import name 'CommandCenter'`.

- [ ] **Step 4: Add the locked runtime dependency**

Add to `pyproject.toml` `[project] dependencies`:

```toml
"sse-starlette==2.1.3",
```

Run: `uv lock`

Expected: lockfile resolves; `uv run --frozen python -c "import sse_starlette"` succeeds after `uv sync --frozen --extra test`.

- [ ] **Step 5: Implement `web/command_center/sse.py`**

```python
"""SSE fan-out registry and per-client bounds (spec §7).

Every method runs on the ASGI loop only; the reducer schedules publish calls
via call_soon_threadsafe, so register() is atomic with respect to event
application — the replay cutover can neither miss nor duplicate an event.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Optional

from web.command_center.state import DashboardState

logger = logging.getLogger("web.command_center.sse")

FIFO_LIMIT = 1000


class SseClient:
    __slots__ = ("fifo", "quote_map", "wake", "resync", "closed")

    def __init__(self):
        self.fifo: deque[dict] = deque()
        self.quote_map: dict[str, dict] = {}
        self.wake = asyncio.Event()
        self.resync = False
        self.closed = False


def _parse_event_id(value: str) -> Optional[tuple[str, int]]:
    try:
        stream_id, seq = value.rsplit(":", 1)
        return stream_id, int(seq)
    except (ValueError, AttributeError):
        return None


class SseFanout:
    def __init__(self, state: DashboardState):
        self._state = state
        self._clients: set[SseClient] = set()

    def register(self, last_event_id: Optional[str]
                 ) -> tuple[SseClient, Optional[list[dict]], dict[str, dict]]:
        client = SseClient()
        replay: Optional[list[dict]] = []
        if last_event_id:
            parsed = _parse_event_id(last_event_id)
            replay = (None if parsed is None
                      else self._state.replay_after(parsed[0], parsed[1]))
        self._clients.add(client)
        return client, replay, dict(self._state.quotes)

    def unregister(self, client: SseClient) -> None:
        client.closed = True
        self._clients.discard(client)

    def client_count(self) -> int:
        return len(self._clients)

    def publish(self, envelope: dict) -> None:
        for client in list(self._clients):
            if client.resync:
                continue
            if len(client.fifo) >= FIFO_LIMIT:
                # A slow tab never blocks source consumption: drop its state,
                # force resynchronization, keep everyone else flowing.
                client.fifo.clear()
                client.quote_map.clear()
                client.resync = True
            else:
                client.fifo.append(envelope)
            client.wake.set()

    def publish_quotes(self, batch: dict[str, dict]) -> None:
        self._state.apply_quotes(batch)
        for client in self._clients:
            if client.resync:
                continue
            client.quote_map.update(batch)  # latest-value, no FIFO capacity
            client.wake.set()

    def broadcast_resync(self) -> None:
        for client in list(self._clients):
            client.fifo.clear()
            client.quote_map.clear()
            client.resync = True
            client.wake.set()
```

- [ ] **Step 6: Implement `web/command_center/routes_read.py`**

```python
"""Read-only routes: snapshot (also the degraded polling fallback), SSE, page."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

logger = logging.getLogger("web.command_center.routes")

SSE_PING_SECONDS = 10


def create_read_router(cc, templates) -> APIRouter:
    router = APIRouter()
    require_session = cc.require_session

    @router.get("/api/snapshot")
    async def api_snapshot(_session: str = Depends(require_session)):
        if not cc.state.has_baseline:
            return JSONResponse({"detail": "snapshot not ready"}, status_code=503)
        view = cc.state.snapshot_view()
        view["health"] = cc.bridge.health() if cc.bridge else {
            "lifecycle": "starting", "reconnects": 0, "cursor": None, "sources": {}}
        return JSONResponse(view)

    @router.get("/api/events")
    async def api_events(request: Request, _session: str = Depends(require_session)):
        after = request.headers.get("last-event-id") or request.query_params.get("after")
        client, replay, quote_baseline = cc.fanout.register(after)

        async def stream():
            try:
                if replay is None:
                    yield {"event": "resync_required", "data": json.dumps(
                        {"reason": "replay_unavailable", "stream_id": cc.state.stream_id})}
                    return
                for env in replay:
                    yield {"id": f"{env['stream_id']}:{env['sequence']}",
                           "event": env["event_type"], "data": json.dumps(env)}
                # Client-local control frame: deliberately NO SSE id (spec §6/§7).
                yield {"event": "quotes.snapshot", "data": json.dumps(
                    {"stream_id": cc.state.stream_id, "quotes": quote_baseline})}
                while not client.closed:
                    await client.wake.wait()
                    client.wake.clear()
                    if client.resync:
                        yield {"event": "resync_required", "data": json.dumps(
                            {"reason": "overflow_or_stream_change",
                             "stream_id": cc.state.stream_id})}
                        return
                    while client.fifo:
                        env = client.fifo.popleft()
                        yield {"id": f"{env['stream_id']}:{env['sequence']}",
                               "event": env["event_type"], "data": json.dumps(env)}
                    if client.quote_map:
                        batch, client.quote_map = client.quote_map, {}
                        yield {"event": "quote.updated",
                               "data": json.dumps({"quotes": batch})}  # no id
            finally:
                cc.fanout.unregister(client)

        return EventSourceResponse(stream(), ping=SSE_PING_SECONDS)

    @router.get("/cc", response_class=HTMLResponse)
    async def command_center_page(request: Request,
                                  _session: str = Depends(require_session)):
        import os
        return templates.TemplateResponse(request, "command_center.html", {
            "degraded_after_ms": int(os.environ.get("CC_DEGRADED_AFTER_MS", "15000")),
            "poll_interval_ms": int(os.environ.get("CC_POLL_INTERVAL_MS", "5000")),
        })

    return router
```

(The `/cc` template lands in Task 6; until then the route 500s only if requested, which no Task 5 test does.)

- [ ] **Step 7: Implement the `CommandCenter` container in `web/command_center/__init__.py`**

```python
"""Command center wiring: FastAPI lifespan owns exactly one bridge, one
reducer (DashboardState), and one fan-out registry (spec §5.1, §4.3)."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable, Optional

from web.command_center.bridge import DashboardEventBridge
from web.command_center.quotes import QuotePlane
from web.command_center.session import (
    DashboardCredentials,
    FailedAttemptLimiter,
    SessionManager,
    build_require_session,
    load_dashboard_credentials,
)
from web.command_center.sse import SseFanout
from web.command_center.state import DashboardState

logger = logging.getLogger("web.command_center")

GRACEFUL_SHUTDOWN_SECONDS = 5


@dataclass(frozen=True)
class CommandCenterConfig:
    typed_query_endpoint: str = "tcp://127.0.0.1:42101"
    typed_feed_endpoint: str = "tcp://127.0.0.1:42103"
    pubsub_address: str = "tcp://127.0.0.1"
    pubsub_port: int = 42002
    quote_hz: float = 4.0
    cookie_secure: bool = False

    @classmethod
    def from_env(cls, env=os.environ) -> "CommandCenterConfig":
        return cls(
            typed_query_endpoint=env.get("MMR_TYPED_QUERY_ENDPOINT",
                                         cls.typed_query_endpoint),
            typed_feed_endpoint=env.get("MMR_TYPED_FEED_ENDPOINT",
                                        cls.typed_feed_endpoint),
            pubsub_address=env.get("MMR_PUBSUB_ADDRESS", cls.pubsub_address),
            pubsub_port=int(env.get("MMR_PUBSUB_PORT", cls.pubsub_port)),
            quote_hz=float(env.get("CC_QUOTE_HZ", cls.quote_hz)),
            cookie_secure=env.get("DASHBOARD_COOKIE_SECURE", "").lower()
            in ("1", "true", "yes"),
        )


def _assert_single_worker(env=os.environ) -> None:
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        value = (env.get(var) or "").strip()
        if value and value != "1":
            raise RuntimeError(
                f"command center requires exactly one worker; {var}={value}")


def _assert_dill_strict(env=os.environ) -> None:
    if env.get("MMR_DILL_STRICT") != "1":
        raise RuntimeError(
            "command center requires MMR_DILL_STRICT=1: the dashboard process "
            "never executes dill payloads")


def _default_query_client(config: CommandCenterConfig):
    from trader.messaging.typed_rpc import TypedRpcClient
    return TypedRpcClient(config.typed_query_endpoint, role="query")


def _default_feed_client(config: CommandCenterConfig):
    from trader.messaging.typed_rpc import TypedRpcClient
    return TypedRpcClient(config.typed_feed_endpoint, role="feed")


class CommandCenter:
    def __init__(self, config: CommandCenterConfig, *,
                 credentials_loader: Callable[[], DashboardCredentials]
                 = load_dashboard_credentials,
                 query_client_factory=None,
                 feed_client_factory=None,
                 bridge_factory=None,
                 quote_plane_factory=None,
                 command_gateway_factory=None):
        self.config = config
        self._credentials_loader = credentials_loader
        self._query_client_factory = (query_client_factory
                                      or (lambda: _default_query_client(config)))
        self._feed_client_factory = (feed_client_factory
                                     or (lambda: _default_feed_client(config)))
        self._bridge_factory = bridge_factory or DashboardEventBridge
        self._quote_plane_factory = quote_plane_factory or (
            lambda loop, deliver: QuotePlane(
                config.pubsub_address, config.pubsub_port, loop, deliver,
                hz=config.quote_hz))
        # Reserved seam for [M1-C]: DashboardCommandGateway lives here.
        self._command_gateway_factory = command_gateway_factory
        self.command_gateway = None
        self.state = DashboardState()
        self.fanout = SseFanout(self.state)
        self.session_manager: Optional[SessionManager] = None
        self.limiter = FailedAttemptLimiter()
        self.bridge: Optional[DashboardEventBridge] = None
        self.quote_plane: Optional[QuotePlane] = None
        self._query_client = None
        self._feed_client = None

    def ensure_session_manager(self) -> SessionManager:
        """Raises CredentialConfigError on missing credentials — called at
        lifespan startup so a misconfigured dashboard fails to start."""
        if self.session_manager is None:
            self.session_manager = SessionManager(self._credentials_loader())
        return self.session_manager

    @property
    def require_session(self):
        manager = self.ensure_session_manager()
        return build_require_session(manager)

    @asynccontextmanager
    async def lifespan(self, app):
        _assert_single_worker()
        _assert_dill_strict()
        self.ensure_session_manager()
        loop = asyncio.get_running_loop()
        self._query_client = self._query_client_factory()
        self._feed_client = self._feed_client_factory()
        self.bridge = self._bridge_factory(
            self._query_client, self._feed_client, self.state, self.fanout, loop)
        if hasattr(self.bridge, "start"):
            self.bridge.start()
        self.quote_plane = self._quote_plane_factory(loop, self.fanout.publish_quotes)
        self.quote_plane.start()
        if self._command_gateway_factory is not None:  # [M1-C] wires this
            self.command_gateway = self._command_gateway_factory(self)
        try:
            yield
        finally:
            # Uvicorn has already drained or cancelled SSE responses within its
            # 5-second graceful-shutdown bound; generators unregister in finally.
            remaining = self.fanout.client_count()
            if remaining:
                logger.warning("lifespan teardown with %d SSE clients still "
                               "registered", remaining)
            self.quote_plane.stop()
            self.bridge.stop()
            for client in (self._query_client, self._feed_client):
                if client is not None and hasattr(client, "close"):
                    client.close()
```

The test fixture in Step 2 passes `bridge_factory` — note the constructor accepts it (positional call signature `(query, feed, state, fanout, loop)`).

- [ ] **Step 8: Wire `web/app.py`**

Keep every existing legacy handler byte-identical. Change only construction, the static mount, and `main()`:

```python
from fastapi.staticfiles import StaticFiles

from web.command_center import CommandCenter, CommandCenterConfig, GRACEFUL_SHUTDOWN_SECONDS
from web.command_center.routes_read import create_read_router
from web.command_center.session import SessionSecurityMiddleware, create_session_router


def create_app(cc: CommandCenter | None = None) -> FastAPI:
    center = cc or CommandCenter(CommandCenterConfig.from_env())
    application = FastAPI(title='MMR Dashboard', lifespan=center.lifespan)
    application.state.command_center = center
    application.mount('/static', StaticFiles(
        directory=str(Path(__file__).parent / 'static')), name='static')
    application.add_middleware(
        SessionSecurityMiddleware,
        manager_provider=lambda: center.session_manager)
    # Session endpoints construct lazily so import-time never needs credentials;
    # lifespan startup (ensure_session_manager) is the hard failure point.
    application.include_router(create_session_router(
        center.ensure_session_manager() if os.environ.get('DASHBOARD_TOKEN')
        or os.environ.get('DASHBOARD_TOKEN_FILE') or os.environ.get('MMR_WEB_TOKEN')
        or cc is not None else _DeferredManager(center),
        center.limiter, cookie_secure=center.config.cookie_secure))
    application.include_router(create_read_router(center, _TEMPLATES))
    _register_legacy_routes(application)
    return application
```

Implementation note (do it the simple way, not the conditional above): make `create_session_router` accept a `manager_provider: Callable[[], SessionManager]` instead of a manager instance, resolve it per-request inside the handlers, and pass `center.ensure_session_manager`. Update Task 1's router signature accordingly while keeping its tests green (`create_session_router(manager, limiter)` still works by wrapping: `manager_provider = manager if callable(manager) else (lambda: manager)` — a non-callable `SessionManager` is wrapped). Move the existing module-level `@app.*` legacy routes into `_register_legacy_routes(application)` mechanically (same decorators, now on the passed app) without touching handler bodies, and keep:

```python
app = create_app()


def main():
    import uvicorn
    port = int(os.environ.get('WEB_PORT', '7424'))
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    logger.info('MMR dashboard starting on 0.0.0.0:%d (single worker)', port)
    uvicorn.run(app, host='0.0.0.0', port=port, log_level='info',
                workers=1, timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS)
```

Create the empty directory `web/static/` with a `.gitkeep` (Task 6 adds the JS file).

Update the `tests/test_web_dashboard.py` fixture to construct an authenticated client (legacy tests otherwise now 401 at the middleware):

```python
@pytest.fixture
def client(stub_cc):
    from web.app import create_app
    app = create_app(stub_cc)
    test_client = TestClient(app)
    test_client.post("/session", data={"token": TEST_TOKEN})
    return test_client
```

where `stub_cc` builds `CommandCenter` with the null factories from `tests/test_dashboard_snapshot_api.py` (extract `_NullBridge`/`_NullQuotePlane` into `tests/cc_fakes.py` if both files need them) and the fixed test credentials. Legacy assertions themselves stay unchanged.

- [ ] **Step 9: Run the new endpoint tests, the SSE tests, and the legacy dashboard regressions**

Run: `uv run --frozen pytest tests/test_dashboard_sse.py tests/test_dashboard_snapshot_api.py tests/test_dashboard_session.py tests/test_web_dashboard.py -q`

Expected: PASS — including the legacy dashboard tests through the new session gate.

- [ ] **Step 10: Commit**

```bash
git add web/command_center/__init__.py web/command_center/sse.py web/command_center/routes_read.py web/command_center/session.py web/app.py web/static pyproject.toml uv.lock tests/test_dashboard_sse.py tests/test_dashboard_snapshot_api.py tests/test_web_dashboard.py
git commit -m "feat(m1-r): snapshot API and SSE delivery with bounded replay"
```

### Task 6: Layout A operations-first UI — template, JS store, freshness/degraded UX

**Files:**
- Create: `web/templates/command_center.html`
- Create: `web/static/command_center.js`
- Modify: `tests/test_dashboard_snapshot_api.py` (append page-render tests)

**Interfaces:**
- Consumes: `/api/snapshot`, `/api/events` (Task 5), the browser envelope contract (Task 2), health payload shape (Task 3).
- Produces: the read-only Layout A page at `GET /cc` (spec §4.2, §8.1–8.4 read panels, §11 client behavior):
  1. status bar — `LIVE`/`PAPER` badge, exact account ID, per-dependency health chips, latest authoritative event time;
  2. account summary cards — net liquidation (must render for a flat account), daily P&L, exposure, buying power, margin cushion;
  3. positions as the dominant panel — quantity, avg/current price, instrument + account currency, base-currency conversion, unrealized/daily P&L, per-row freshness;
  4. persistent right rail — pending proposals (read-only cards) with a full sizing-reasoning drawer;
  5. below — working orders with order-group aggregation that still shows every leg, recent executions, strategy rows derived from dispatchable state, risk/reconciliation panel that renders `unavailable` explicitly.
- Produces: client store + reducers in `command_center.js` mirroring the server reducer (replace by entity key, tombstones, active/terminal moves), SSE consumption with `Last-Event-ID`/`?after=` resume, resync handling, 15-second-disconnect → 5-second polling fallback with a persistent degraded banner, return to SSE only after a coherent snapshot, per-second freshness ticker, non-color-only staleness/alert markers, and keyboard/focus-managed drawers.
- All alerts carry a text/glyph marker in addition to color (spec §13.2 non-color-only requirement).

- [ ] **Step 1: Write failing page tests**

Append to `tests/test_dashboard_snapshot_api.py`:

```python
class TestCommandCenterPage:
    @pytest.mark.asyncio
    async def test_page_requires_session(self, client, cc):
        _seed(cc)
        async with client as c:
            response = await c.get("/cc")
            assert response.status_code == 303
            assert response.headers["location"] == "/cc/login"

    @pytest.mark.asyncio
    async def test_page_renders_layout_a_regions(self, client, cc):
        _seed(cc)
        async with client as c:
            await _login(c)
            html = (await c.get("/cc")).text
            for element_id in ("status-bar", "mode-badge", "account-id",
                               "dependency-chips", "last-event-time",
                               "account-cards", "positions-panel", "action-rail",
                               "orders-panel", "fills-panel", "strategies-panel",
                               "risk-panel", "degraded-banner", "drawer"):
                assert f'id="{element_id}"' in html
            assert '/static/command_center.js' in html
            assert 'data-degraded-after-ms="15000"' in html
            assert 'data-poll-interval-ms="5000"' in html

    @pytest.mark.asyncio
    async def test_risk_panel_defaults_to_unavailable_not_green(self, client, cc):
        _seed(cc)
        async with client as c:
            await _login(c)
            html = (await c.get("/cc")).text
            assert 'data-state="unavailable"' in html
            assert 'Risk unavailable' in html
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_dashboard_snapshot_api.py -q`

Expected: FAIL — `/cc` raises `jinja2.exceptions.TemplateNotFound: command_center.html`.

- [ ] **Step 3: Create `web/templates/command_center.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MMR Command Center</title>
  <style>
    :root { --bg:#0e1116; --panel:#171b22; --border:#262c36; --fg:#d7dce3;
      --dim:#7d8794; --pos:#3fb950; --neg:#f85149; --accent:#58a6ff;
      --warn:#d29922; --chip:#21262d; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--fg);
      font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    #degraded-banner { padding:10px 24px; background:rgba(210,153,34,.15);
      color:var(--warn); border-bottom:1px solid var(--border); font-weight:600; }
    #status-bar { display:flex; gap:16px; align-items:center; flex-wrap:wrap;
      padding:12px 24px; background:var(--panel); border-bottom:1px solid var(--border); }
    .badge { padding:2px 10px; border-radius:999px; font-weight:700; font-size:12px;
      background:var(--chip); }
    .badge.live { background:rgba(248,81,73,.2); color:var(--neg); }
    .badge.paper { background:rgba(88,166,255,.2); color:var(--accent); }
    #dependency-chips { display:flex; gap:8px; flex-wrap:wrap; }
    .chip { padding:2px 8px; border-radius:999px; font-size:11px; font-weight:600;
      background:var(--chip); color:var(--dim); }
    .chip[data-state="ok"] { background:rgba(63,185,80,.15); color:var(--pos); }
    .chip[data-state="error"], .chip[data-state="unknown"] {
      background:rgba(248,81,73,.15); color:var(--neg); }
    #last-event-time { margin-left:auto; color:var(--dim); font-size:12px; }
    #account-cards { display:grid; grid-template-columns:repeat(auto-fit, minmax(170px,1fr));
      gap:14px; padding:16px 24px; }
    .card { background:var(--panel); border:1px solid var(--border); border-radius:10px;
      padding:12px 16px; }
    .card .k { color:var(--dim); font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
    .card .v { font-size:20px; font-weight:700; font-variant-numeric:tabular-nums; }
    main.grid { display:grid; gap:18px; padding:0 24px 24px;
      grid-template-columns:minmax(0, 3fr) minmax(280px, 1fr); }
    section, aside { background:var(--panel); border:1px solid var(--border);
      border-radius:10px; overflow:hidden; }
    section > h2, aside > h2 { margin:0; padding:11px 16px; font-size:12px;
      text-transform:uppercase; letter-spacing:.6px; color:var(--dim);
      border-bottom:1px solid var(--border); }
    #positions-panel { grid-column:1; grid-row:1 / span 2; }
    #action-rail { grid-column:2; grid-row:1 / span 2; }
    .lower { grid-column:1 / -1; display:grid; gap:18px;
      grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); }
    table { width:100%; border-collapse:collapse; }
    th, td { padding:7px 14px; text-align:right; white-space:nowrap; font-size:13px; }
    th:first-child, td:first-child { text-align:left; }
    thead th { color:var(--dim); font-weight:500; font-size:11px;
      border-bottom:1px solid var(--border); }
    tbody tr { border-top:1px solid var(--border); }
    .pos { color:var(--pos); } .neg { color:var(--neg); } .dim { color:var(--dim); }
    .num { font-variant-numeric:tabular-nums; }
    .age { font-size:11px; color:var(--dim); }
    tr.stale .age::before, .stale-marker::before { content:"⚠ "; color:var(--warn); }
    .proposal-card { border-bottom:1px solid var(--border); padding:12px 16px; }
    .proposal-card:focus { outline:2px solid var(--accent); outline-offset:-2px; }
    #risk-body { padding:14px 16px; }
    #risk-body[data-state="unavailable"] { color:var(--warn); }
    #risk-body[data-state="warning"] { color:var(--neg); }
    .order-group { border-bottom:1px solid var(--border); }
    .order-group .group-head { padding:8px 16px; font-weight:600; }
    .order-group .leg { padding:4px 16px 4px 32px; font-size:12px; color:var(--dim);
      display:flex; justify-content:space-between; }
    #drawer { position:fixed; inset:0; background:rgba(0,0,0,.6); z-index:50;
      display:flex; align-items:flex-start; justify-content:center; padding:6vh 16px; }
    #drawer[hidden] { display:none; }
    #drawer-box { background:var(--panel); border:1px solid var(--border);
      border-radius:10px; width:min(720px,100%); max-height:82vh; overflow:auto; }
    #drawer-close { float:right; margin:8px; background:transparent; border:none;
      color:var(--dim); font-size:18px; cursor:pointer; }
    #drawer-content { padding:8px 20px 20px; }
  </style>
</head>
<body data-degraded-after-ms="{{ degraded_after_ms }}"
      data-poll-interval-ms="{{ poll_interval_ms }}">
  <div id="degraded-banner" role="alert" hidden>
    ⚠ Realtime stream degraded — polling snapshots every 5 s. Data may be stale.
  </div>
  <header id="status-bar">
    <span id="mode-badge" class="badge">…</span>
    <span id="account-id" class="num">—</span>
    <span id="dependency-chips" aria-label="Dependency health"></span>
    <span id="last-event-time" title="Latest authoritative event time">
      last event: <span class="v">—</span></span>
  </header>
  <div id="account-cards" aria-label="Account summary"></div>
  <main class="grid">
    <section id="positions-panel" aria-label="Positions">
      <h2>Positions</h2>
      <table>
        <thead><tr>
          <th>Instrument</th><th>Qty</th><th>Avg</th><th>Last</th><th>Ccy</th>
          <th>Mkt value</th><th>Base value</th><th>Unrl P&amp;L</th>
          <th>Day P&amp;L</th><th>Fresh</th>
        </tr></thead>
        <tbody id="positions-body"></tbody>
      </table>
    </section>
    <aside id="action-rail" aria-label="Pending proposals">
      <h2>Action queue <span class="dim">(read-only)</span></h2>
      <div id="proposal-cards"></div>
    </aside>
    <div class="lower">
      <section id="orders-panel"><h2>Working orders</h2>
        <div id="order-groups"></div></section>
      <section id="fills-panel"><h2>Recent executions</h2>
        <table><thead><tr><th>Time</th><th>Instrument</th><th>Side</th>
          <th>Qty</th><th>Price</th><th>Commission</th></tr></thead>
          <tbody id="fills-body"></tbody></table></section>
      <section id="strategies-panel"><h2>Strategies</h2>
        <table><thead><tr><th>Strategy</th><th>State</th><th>Enabled</th>
          <th>Last activity</th><th>Error</th></tr></thead>
          <tbody id="strategies-body"></tbody></table></section>
      <section id="risk-panel"><h2>Risk &amp; reconciliation</h2>
        <div id="risk-body" data-state="unavailable">Risk unavailable — waiting
          for an authoritative risk projection.</div></section>
    </div>
  </main>
  <div id="drawer" role="dialog" aria-modal="true" aria-label="Detail" hidden>
    <div id="drawer-box">
      <button id="drawer-close" aria-label="Close detail">×</button>
      <div id="drawer-content"></div>
    </div>
  </div>
  <script src="/static/command_center.js" defer></script>
</body>
</html>
```

- [ ] **Step 4: Create `web/static/command_center.js`**

```javascript
/* MMR Command Center client (read-only [M1-R]).
 * Store + reducer mirror the server contract: replace by entity key, apply
 * tombstones, reject nothing client-side (the server already rejected
 * revision regressions). SSE with Last-Event-ID resume; 15 s disconnect ->
 * 5 s snapshot polling behind a persistent banner; back to SSE only after a
 * coherent snapshot. */
'use strict';

const CFG = {
  degradedAfterMs: parseInt(document.body.dataset.degradedAfterMs, 10) || 15000,
  pollIntervalMs: parseInt(document.body.dataset.pollIntervalMs, 10) || 5000,
  staleAfterS: 30,
};

const DOMAIN_EVENT_TYPES = [
  'account.updated', 'position.updated', 'proposal.updated', 'command.updated',
  'trading_control.updated', 'order.updated', 'fill.received', 'fill.updated',
  'strategy.updated', 'risk.updated', 'reconciliation.updated', 'service.health',
];
const ACTIVE_PROPOSAL = new Set(['PENDING', 'APPROVED']);
const TERMINAL_ORDER = new Set(
  ['FILLED', 'CANCELLED', 'CANCELLED_AFTER_PARTIAL', 'REJECTED', 'INACTIVE']);
const DISPATCHABLE_STRATEGY = new Set(['RUNNING', 'WAITING_HISTORICAL_DATA']);

const store = {
  view: null, streamId: null, sequence: 0,
  quotes: {}, quoteReceivedAt: {},
  connection: { mode: 'connecting', degradedSince: null },
};

/* ---------------- reducer (mirrors web/command_center/state.py) ---------- */
function collectionFor(v, type) {
  return {
    account: v.accounts, position: v.positions, strategy: v.strategies,
    reconciliation: v.reconciliation, trading_control: v.trading_control,
    command: v.commands,
  }[type];
}

function replaceById(list, row) {
  const i = list.findIndex(r => r.entity_id === row.entity_id);
  if (i >= 0) list[i] = row; else list.push(row);
}

function removeById(list, id) {
  const i = list.findIndex(r => r.entity_id === id);
  if (i >= 0) list.splice(i, 1);
}

function applyEvent(env) {
  if (env.stream_id !== store.streamId) { resync(); return; }
  store.sequence = env.sequence;
  const v = store.view;
  if (!v) return;
  const row = env.operation === 'upsert'
    ? Object.assign({ entity_id: env.entity_id,
                      entity_revision: env.entity_revision }, env.payload)
    : null;
  const type = env.entity_type;
  if (type === 'proposal') {
    removeById(v.proposals.active, env.entity_id);
    removeById(v.proposals.terminal, env.entity_id);
    if (row) (ACTIVE_PROPOSAL.has(String(row.status || '').toUpperCase())
      ? v.proposals.active : v.proposals.terminal).push(row);
  } else if (type === 'order') {
    removeById(v.orders.active, env.entity_id);
    removeById(v.orders.terminal, env.entity_id);
    if (row) (TERMINAL_ORDER.has(String(row.status || '').toUpperCase())
      ? v.orders.terminal : v.orders.active).push(row);
  } else if (type === 'fill') {
    removeById(v.fills, env.entity_id);
    if (row) v.fills.push(row);
  } else if (type === 'risk') {
    if (row) v.risk[env.entity_id] = row; else delete v.risk[env.entity_id];
  } else {
    const list = collectionFor(v, type);
    if (list) { if (row) replaceById(list, row); else removeById(list, env.entity_id); }
  }
  v.last_event_at = env.source_timestamp;
  renderAll();
}

function applyQuotes(batch) {
  const now = Date.now();
  for (const [id, quote] of Object.entries(batch)) {
    store.quotes[id] = quote;
    store.quoteReceivedAt[id] = now;
  }
  renderPositions();
}

function applySnapshot(view) {
  store.view = view;
  store.streamId = view.stream_id;
  store.sequence = view.sequence;
  store.quotes = view.quotes || {};
  const now = Date.now();
  Object.keys(store.quotes).forEach(id => { store.quoteReceivedAt[id] = now; });
  renderAll();
}

/* ---------------- connection management ---------------------------------- */
let es = null, pollTimer = null, disconnectedAt = null;

async function fetchSnapshot() {
  const response = await fetch('/api/snapshot', { credentials: 'same-origin' });
  if (response.status === 401) { window.location.href = '/cc/login'; return null; }
  if (!response.ok) return null;
  return response.json();
}

async function resync() {
  if (es) { es.close(); es = null; }
  const view = await fetchSnapshot();
  if (view) { applySnapshot(view); connectSse(); }
  else setTimeout(resync, CFG.pollIntervalMs);
}

function connectSse() {
  if (es) es.close();
  const after = store.streamId ? `?after=${store.streamId}:${store.sequence}` : '';
  es = new EventSource('/api/events' + after);
  es.onopen = () => { disconnectedAt = null; stopPolling(); setBanner(false); };
  es.onerror = () => { if (disconnectedAt === null) disconnectedAt = Date.now(); };
  DOMAIN_EVENT_TYPES.forEach(t =>
    es.addEventListener(t, e => applyEvent(JSON.parse(e.data))));
  es.addEventListener('quote.updated',
    e => applyQuotes(JSON.parse(e.data).quotes));
  es.addEventListener('quotes.snapshot', e => {
    store.quotes = JSON.parse(e.data).quotes || {};
    const now = Date.now();
    Object.keys(store.quotes).forEach(id => { store.quoteReceivedAt[id] = now; });
    renderPositions();
  });
  es.addEventListener('resync_required', () => resync());
}

function startPolling() {
  if (pollTimer) return;
  setBanner(true);
  store.connection.mode = 'polling';
  pollTimer = setInterval(async () => {
    const view = await fetchSnapshot();
    if (view) {
      applySnapshot(view);
      // Return to SSE only after this coherent snapshot (spec §11).
      stopPolling();
      connectSse();
    }
  }, CFG.pollIntervalMs);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  store.connection.mode = 'sse';
}

function setBanner(visible) {
  document.getElementById('degraded-banner').hidden = !visible;
}

setInterval(() => {
  if (es && es.readyState !== EventSource.OPEN && disconnectedAt !== null
      && Date.now() - disconnectedAt >= CFG.degradedAfterMs && !pollTimer) {
    es.close(); es = null;
    startPolling();
  }
}, 1000);

/* ---------------- rendering ---------------------------------------------- */
const fmt = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 });
const money = (x, ccy) => (x === null || x === undefined || Number.isNaN(x))
  ? '—' : `${fmt.format(x)}${ccy ? ' ' + ccy : ''}`;
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
const ageOf = iso => iso ? (Date.now() - Date.parse(iso)) / 1000 : null;
const fmtAge = s => s === null ? 'no data' : s < 60 ? `${Math.round(s)}s`
  : s < 3600 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`;

function renderStatusBar() {
  const v = store.view; if (!v) return;
  const account = v.accounts[0] || {};
  const badge = document.getElementById('mode-badge');
  const mode = String(account.mode || 'unknown').toLowerCase();
  badge.textContent = mode.toUpperCase();
  badge.className = 'badge ' + (mode === 'live' ? 'live' : 'paper');
  document.getElementById('account-id').textContent =
    account.entity_id || account.account_id || '—';
  const chips = document.getElementById('dependency-chips');
  const sources = (v.health && v.health.sources) || {};
  chips.innerHTML = Object.entries(sources).map(([name, s]) =>
    `<span class="chip" data-state="${esc(s.state)}">${esc(name)}: ${esc(s.state)}` +
    (s.last_success_age_seconds !== null && s.last_success_age_seconds !== undefined
      ? ` (${fmtAge(s.last_success_age_seconds)})` : '') + '</span>').join('');
  chips.innerHTML += `<span class="chip" data-state="${
    v.health && v.health.lifecycle === 'live' ? 'ok' : 'error'}">bridge: ${
    esc(v.health ? v.health.lifecycle : 'unknown')}</span>`;
  document.querySelector('#last-event-time .v').textContent =
    v.last_event_at ? `${v.last_event_at} (${fmtAge(ageOf(v.last_event_at))} ago)` : '—';
}

function renderAccountCards() {
  const v = store.view; if (!v) return;
  const account = v.accounts[0] || {};
  // Net liquidation must render for a flat account too (spec §8.1): the value
  // comes from the account entity, never derived from positions.
  const cards = [
    ['Net liquidation', money(account.net_liquidation, account.currency)],
    ['Daily P&L', money(account.daily_pnl, account.currency)],
    ['Exposure', money(account.gross_exposure, account.currency)],
    ['Buying power', money(account.buying_power, account.currency)],
    ['Margin cushion', account.margin_cushion !== undefined && account.margin_cushion !== null
      ? `${fmt.format(account.margin_cushion * 100)}%` : '—'],
  ];
  document.getElementById('account-cards').innerHTML = cards.map(([k, val]) =>
    `<div class="card"><div class="k">${k}</div><div class="v">${esc(val)}</div></div>`
  ).join('');
}

function renderPositions() {
  const v = store.view; if (!v) return;
  const body = document.getElementById('positions-body');
  body.innerHTML = v.positions.map(p => {
    const conid = String(p.conid ?? (p.entity_id || '').split(':').pop());
    const quote = store.quotes[conid];
    const last = quote ? quote.last : null;
    const quoteAge = store.quoteReceivedAt[conid]
      ? (Date.now() - store.quoteReceivedAt[conid]) / 1000 : null;
    const stale = quoteAge === null || quoteAge > CFG.staleAfterS;
    const pnl = p.unrealized_pnl;
    return `<tr class="${stale ? 'stale' : ''}" data-entity="${esc(p.entity_id)}">
      <td>${esc(p.symbol || conid)}</td>
      <td class="num">${fmt.format(p.quantity ?? 0)}</td>
      <td class="num">${money(p.avg_cost)}</td>
      <td class="num">${money(last)}</td>
      <td>${esc(p.currency || '')}</td>
      <td class="num">${money(p.market_value, p.currency)}</td>
      <td class="num">${p.base_market_value !== undefined
        ? money(p.base_market_value, p.base_currency) : '— (no conversion)'}</td>
      <td class="num ${pnl >= 0 ? 'pos' : 'neg'}">${money(pnl)}</td>
      <td class="num">${money(p.daily_pnl)}</td>
      <td><span class="age">${fmtAge(quoteAge)}</span></td>
    </tr>`;
  }).join('');
}

function renderProposals() {
  const v = store.view; if (!v) return;
  const rail = document.getElementById('proposal-cards');
  const cards = v.proposals.active.map(p => {
    const age = ageOf(p.created_at);
    return `<div class="proposal-card" tabindex="0" role="button"
        data-proposal="${esc(p.entity_id)}"
        aria-label="Proposal ${esc(p.entity_id)} details">
      <strong>#${esc(p.entity_id)} ${esc(p.action || p.side || '')}
        ${esc(p.symbol || '')}</strong>
      <div class="dim">qty ${esc(p.quantity ?? 'auto')} · notional
        ${money(p.amount, p.currency)} · conf ${esc(p.confidence ?? '—')}</div>
      <div class="dim">status ${esc(p.status)} · expires ${esc(p.expires_at || '—')}
        · <span class="age">${fmtAge(age)} old</span></div>
    </div>`;
  });
  rail.innerHTML = cards.join('')
    || '<div class="proposal-card dim">No pending proposals.</div>';
}

function renderOrders() {
  const v = store.view; if (!v) return;
  const orders = [...v.orders.active, ...v.orders.terminal];
  const groups = new Map();
  for (const o of orders) {
    const gid = o.order_group_id || `solo:${o.entity_id}`;
    if (!groups.has(gid)) groups.set(gid, []);
    groups.get(gid).push(o);
  }
  // Aggregate group status without hiding per-leg state (spec §8.4).
  document.getElementById('order-groups').innerHTML =
    [...groups.entries()].map(([gid, legs]) => {
      const statuses = [...new Set(legs.map(l => String(l.status || '')))];
      const filled = legs.reduce((n, l) => n + (l.filled_quantity || 0), 0);
      return `<div class="order-group">
        <div class="group-head">${esc(gid)} — ${legs.length} leg(s),
          ${esc(statuses.join(' / '))}, filled ${fmt.format(filled)}</div>
        ${legs.map(l => `<div class="leg"><span>${esc(l.symbol || l.conid || '')}
          ${esc(l.action || '')} ${fmt.format(l.quantity ?? 0)}
          @ ${esc(l.order_type || '')}</span>
          <span>${esc(l.status || '')} · filled ${fmt.format(l.filled_quantity || 0)}
          ${l.avg_fill_price ? '@ ' + money(l.avg_fill_price) : ''}</span></div>`
        ).join('')}
      </div>`;
    }).join('') || '<div class="order-group group-head dim">No orders.</div>';
}

function renderFills() {
  const v = store.view; if (!v) return;
  document.getElementById('fills-body').innerHTML = v.fills.slice(-50).reverse()
    .map(f => `<tr><td>${esc(f.time || '')}</td><td>${esc(f.symbol || f.conid || '')}</td>
      <td>${esc(f.side || '')}</td><td class="num">${fmt.format(f.quantity ?? 0)}</td>
      <td class="num">${money(f.price)}</td>
      <td class="num">${money(f.commission)}</td></tr>`).join('');
}

function renderStrategies() {
  const v = store.view; if (!v) return;
  document.getElementById('strategies-body').innerHTML = v.strategies.map(s => {
    const state = String(s.runtime_state || s.state || '').toUpperCase();
    const enabled = DISPATCHABLE_STRATEGY.has(state);
    return `<tr><td>${esc(s.name || s.entity_id)}</td><td>${esc(state)}</td>
      <td>${enabled ? '● enabled' : '○ not dispatchable'}</td>
      <td><span class="age">${fmtAge(ageOf(s.last_activity_at))}</span></td>
      <td class="${s.last_error ? 'neg' : 'dim'}">${
        s.last_error ? '⚠ ' + esc(s.last_error) : '—'}</td></tr>`;
  }).join('');
}

function renderRisk() {
  const v = store.view; if (!v) return;
  const el = document.getElementById('risk-body');
  const projections = Object.entries(v.risk || {})
    .filter(([key]) => key.startsWith('projection:'));
  const journal = v.health && v.health.sources && v.health.sources.journal;
  const degraded = !journal || journal.state !== 'ok';
  if (projections.length === 0 || degraded) {
    // Never render green from missing data (spec §8.4).
    el.dataset.state = 'unavailable';
    el.textContent = '⚠ Risk unavailable — '
      + (degraded ? 'journal source degraded.' : 'no authoritative risk projection.');
    return;
  }
  const rows = projections.map(([key, r]) => {
    const warnings = r.warnings || [];
    return `<div><strong>${esc(key)}</strong> — ${
      warnings.length ? '⚠ ' + warnings.map(esc).join('; ')
                      : 'no active warnings'}</div>`;
  });
  const anyWarning = projections.some(([, r]) => (r.warnings || []).length);
  el.dataset.state = anyWarning ? 'warning' : 'ok';
  el.innerHTML = rows.join('')
    + (v.reconciliation || []).map(r =>
      `<div class="dim">reconciliation ${esc(r.entity_id)}: ${
        esc(r.discrepancy_count ?? 0)} discrepancies</div>`).join('');
}

function renderAll() {
  renderStatusBar(); renderAccountCards(); renderPositions(); renderProposals();
  renderOrders(); renderFills(); renderStrategies(); renderRisk();
}

/* ---------------- drawer (keyboard + focus managed) ----------------------- */
let drawerInvoker = null;

function openDrawer(html, invoker) {
  drawerInvoker = invoker || null;
  document.getElementById('drawer-content').innerHTML = html;
  document.getElementById('drawer').hidden = false;
  document.getElementById('drawer-close').focus();
}

function closeDrawer() {
  document.getElementById('drawer').hidden = true;
  if (drawerInvoker) { drawerInvoker.focus(); drawerInvoker = null; }
}

document.getElementById('drawer-close').addEventListener('click', closeDrawer);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !document.getElementById('drawer').hidden) closeDrawer();
});

document.getElementById('proposal-cards').addEventListener('click', e => {
  const card = e.target.closest('[data-proposal]');
  if (card) showProposalDrawer(card.dataset.proposal, card);
});
document.getElementById('proposal-cards').addEventListener('keydown', e => {
  const card = e.target.closest('[data-proposal]');
  if (card && (e.key === 'Enter' || e.key === ' ')) {
    e.preventDefault();
    showProposalDrawer(card.dataset.proposal, card);
  }
});

function showProposalDrawer(id, invoker) {
  const p = store.view.proposals.active.find(x => String(x.entity_id) === String(id))
    || store.view.proposals.terminal.find(x => String(x.entity_id) === String(id));
  if (!p) return;
  // Full sizing-reasoning chain, not a one-line preview (spec §8.3).
  const sizing = p.sizing_result || {};
  const reasoning = Array.isArray(sizing.reasoning) ? sizing.reasoning
    : (sizing.reasoning ? [sizing.reasoning] : []);
  openDrawer(`<h3>Proposal #${esc(p.entity_id)} — ${esc(p.action || '')}
      ${esc(p.symbol || '')}</h3>
    <p>status <strong>${esc(p.status)}</strong> · source ${esc(p.source || '—')}
      · confidence ${esc(p.confidence ?? '—')} · expires ${esc(p.expires_at || '—')}</p>
    <h4>Position sizing reasoning</h4>
    ${reasoning.length
      ? '<ol>' + reasoning.map(step => `<li>${esc(step)}</li>`).join('') + '</ol>'
      : '<p class="dim">No sizing reasoning recorded.</p>'}
    <h4>Rationale</h4>
    <p>${esc(p.reasoning || '—')}</p>`, invoker);
}

/* ---------------- freshness ticker ---------------------------------------- */
setInterval(() => { renderStatusBar(); renderPositions(); }, 1000);

/* ---------------- boot ----------------------------------------------------- */
resync();
```

- [ ] **Step 5: Run the page tests**

Run: `uv run --frozen pytest tests/test_dashboard_snapshot_api.py -q`

Expected: PASS.

- [ ] **Step 6: Manual smoke against fakes**

Run (uses the Task 7 harness module once it exists; until then verify by eye):

```bash
DASHBOARD_TOKEN=dev DASHBOARD_SESSION_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))') \
MMR_DILL_STRICT=1 uv run --frozen python -m web.app
```

Open `http://127.0.0.1:7424/cc` → redirected to `/cc/login`; after login the grid renders with the bridge `synchronizing`/`degraded` chips visible (no trader running) and the risk panel explicitly `Risk unavailable`. The legacy dashboard still renders at `/` after the same login.

- [ ] **Step 7: Commit**

```bash
git add web/templates/command_center.html web/static/command_center.js tests/test_dashboard_snapshot_api.py
git commit -m "feat(m1-r): layout A read-only command center UI"
```

### Task 7: Health endpoints, performance/soak harness, and browser-test gate

**Files:**
- Create: `web/command_center/health.py`
- Create: `scripts/soak_harness.py`
- Create: `tests/browser/test_command_center.py`
- Create: `tests/cc_fakes.py` (extract `NullBridge`/`NullQuotePlane` shared by the API and browser tests)
- Modify: `web/command_center/state.py` (transport-lag tracking only)
- Modify: `web/app.py` (include the health router)
- Modify: `tests/test_dashboard_snapshot_api.py` (append health-endpoint tests)

**Interfaces:**
- Consumes: `CommandCenter` (Task 5), `DashboardEventBridge.health()` (Task 3), the pinned `browser-test` extra (`playwright==1.55.0`, `[G0]`), `psutil` (already a runtime dependency).
- Produces: `create_health_router(cc: CommandCenter) -> APIRouter` serving unauthenticated boolean `GET /readyz` and authenticated `GET /api/health` (per-dependency state, source-data age, transport lag, feed types, reconnect counts, last safe error). The existing legacy `GET /healthz` (`{"ok": true}`) is untouched and remains the boolean liveness probe.
- Produces: `DashboardState.last_transport_lag_ms: float | None` (source→reducer lag of the newest applied event).
- Produces: `scripts/soak_harness.py` — the spec §13.3 measurement harness (drives 100 instruments × 4 Hz quotes + 20 domain events/s against a fake feed with 3 SSE tabs, prints a JSON report, exits non-zero on threshold violation: p95 > 500 ms, post-warm-up RSS growth > 20%, avg CPU ≥ 1 core). The eight-hour execution itself belongs to `[COMPAT]`; this task delivers the harness plus a smoke invocation.

- [ ] **Step 1: Write failing health-endpoint tests**

Append to `tests/test_dashboard_snapshot_api.py`:

```python
class TestHealthEndpoints:
    @pytest.mark.asyncio
    async def test_healthz_and_readyz_are_boolean_only_and_unauthenticated(self, client, cc):
        async with client as c:
            assert (await c.get("/healthz")).json() == {"ok": True}
            before = await c.get("/readyz")
            assert before.status_code == 503
            assert set(before.json()) == {"ready"}          # no dependency detail
            _seed(cc)
            after = await c.get("/readyz")
            assert after.status_code == 200
            assert after.json() == {"ready": True}

    @pytest.mark.asyncio
    async def test_api_health_requires_session(self, client, cc):
        _seed(cc)
        async with client as c:
            assert (await c.get("/api/health")).status_code == 401

    @pytest.mark.asyncio
    async def test_api_health_detail_and_redaction(self, client, cc):
        _seed(cc)
        async with client as c:
            await _login(c)
            body = (await c.get("/api/health")).json()
            assert set(body) >= {"lifecycle", "reconnects", "cursor", "stream_id",
                                 "sequence", "last_event_at", "transport_lag_ms",
                                 "sse_clients", "sources", "quote_plane"}
            journal = body["sources"]["journal"]
            assert set(journal) == {"state", "last_success_age_seconds",
                                    "last_error", "reconnects"}
            assert set(body["quote_plane"]) == {"instruments", "feed_types", "dropped"}
            encoded = json.dumps(body).lower()
            assert TOKEN.lower() not in encoded
            assert SECRET.lower() not in encoded

    @pytest.mark.asyncio
    async def test_transport_lag_recorded_after_event(self, client, cc):
        _seed(cc)
        envelope = cc.state.apply(DomainEvent(
            event_id="evt-lag", source_cursor=2, entity_revision=3,
            event_type="position.updated", entity_type="position",
            entity_id="DU123:1", operation="upsert", account_id="DU123",
            source="trader_service",
            source_timestamp=dt.datetime.now(UTC) - dt.timedelta(milliseconds=120),
            correlation_id=None, payload={"quantity": 1}))
        cc.fanout.publish(envelope)
        async with client as c:
            await _login(c)
            lag = (await c.get("/api/health")).json()["transport_lag_ms"]
            assert lag is not None and lag >= 100.0
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_dashboard_snapshot_api.py -q`

Expected: FAIL — `/readyz` returns 404 (no health router yet) and `DashboardState` has no `last_transport_lag_ms`.

- [ ] **Step 3: Implement `web/command_center/health.py` and the lag counter**

```python
"""Liveness, readiness, and authenticated dependency health (spec §12).

/healthz (legacy, untouched) and /readyz expose booleans only; every
dependency detail lives behind the session on /api/health.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse


def create_health_router(cc) -> APIRouter:
    router = APIRouter()
    require_session = cc.require_session

    @router.get("/readyz")
    async def readyz():
        # Ready once a coherent fenced snapshot exists (only the bridge can
        # install one, so this implies the bridge initialized). Boolean only
        # to unauthenticated callers — no dependency detail leaks here.
        ready = bool(cc.state.has_baseline)
        return JSONResponse({"ready": ready}, status_code=200 if ready else 503)

    @router.get("/api/health")
    async def api_health(_session: str = Depends(require_session)):
        bridge_health = (cc.bridge.health() if cc.bridge else
                         {"lifecycle": "starting", "reconnects": 0,
                          "cursor": None, "sources": {}})
        feed_types = sorted({q.get("feed_type") for q in cc.state.quotes.values()
                             if q.get("feed_type")})
        return {
            "lifecycle": bridge_health["lifecycle"],
            "reconnects": bridge_health["reconnects"],
            "cursor": bridge_health["cursor"],
            "stream_id": cc.state.stream_id,
            "sequence": cc.state.sequence,
            "last_event_at": cc.state.last_event_at,
            "transport_lag_ms": cc.state.last_transport_lag_ms,
            "sse_clients": cc.fanout.client_count(),
            "sources": bridge_health["sources"],  # per-dependency state, age,
                                                  # last safe error, reconnects
            "quote_plane": {
                "instruments": len(cc.state.quotes),
                "feed_types": feed_types,
                "dropped": getattr(cc.quote_plane, "dropped", None),
            },
        }

    return router
```

In `web/command_center/state.py`, add `self.last_transport_lag_ms: Optional[float] = None` to `__init__` and change `_envelope` to compute the received time once:

```python
        received = self._clock()
        self.last_transport_lag_ms = max(
            0.0, received * 1000.0 - event.source_timestamp.timestamp() * 1000.0)
```

with the envelope's `received_timestamp` becoming `_utc_iso(received)`.

In `web/app.py` `create_app()`, add after the read router:

```python
    from web.command_center.health import create_health_router
    application.include_router(create_health_router(center))
```

(`/readyz` is already in the middleware's exempt set from Task 1, so it stays unauthenticated and boolean.)

- [ ] **Step 4: Run the health tests**

Run: `uv run --frozen pytest tests/test_dashboard_snapshot_api.py tests/test_dashboard_state.py -q`

Expected: PASS.

- [ ] **Step 5: Implement `scripts/soak_harness.py`**

```python
#!/usr/bin/env python3
"""Command-center performance/soak harness (spec §13.3).

Drives the real app path (fake journal feed -> DashboardEventBridge -> reducer
-> SseFanout -> /api/events) entirely in-process:

  100 active instruments x 4 quote updates/s, 20 domain events/s, 3 SSE tabs.

Measures producer-commit -> client-reducer latency (the tab's JSON parse
stands in for the browser reducer application) against the 500 ms p95 target
and samples process RSS/CPU for the soak thresholds: RSS growth <= 20% after
the warm-up hour, average CPU < 1 core. The 8-hour execution belongs to
[COMPAT]; smoke:

  MMR_DILL_STRICT=1 uv run --frozen python scripts/soak_harness.py --minutes 2
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import threading
import time
from pathlib import Path

import httpx
import psutil

from trader.domain.events import (
    DomainEvent,
    ReadDomainEventsResult,
    SnapshotWithCursor,
)

TOKEN = "soak-token"
SECRET = "s" * 64
P95_TARGET_MS = 500.0
RSS_GROWTH_LIMIT_PCT = 20.0
CPU_LIMIT_CORES = 1.0
WARMUP_CAP_SECONDS = 3600.0


class FakeJournalFeed:
    """Blocking long-poll feed over an in-memory journal (commit == append)."""

    def __init__(self):
        self._events: list[DomainEvent] = []
        self._cond = threading.Condition()
        self.closed = False

    def append(self, event: DomainEvent) -> None:
        with self._cond:
            self._events.append(event)
            self._cond.notify_all()

    def call(self, method, body, response_model):
        assert method == "read_domain_events"
        after, limit = body["after_cursor"], body["limit"]
        deadline = time.monotonic() + body["wait_ms"] / 1000.0
        with self._cond:
            while True:
                pending = [e for e in self._events if e.source_cursor > after][:limit]
                if pending or self.closed:
                    newest = pending[-1].source_cursor if pending else after
                    return ReadDomainEventsResult(
                        events=tuple(pending), newest_cursor=newest)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ReadDomainEventsResult(events=(), newest_cursor=after)
                self._cond.wait(timeout=remaining)

    def close(self):
        with self._cond:
            self.closed = True
            self._cond.notify_all()


class FakeQuery:
    def __init__(self, instruments: int):
        self._instruments = instruments

    def call(self, method, body, response_model):
        if method == "snapshot_with_cursor":
            return SnapshotWithCursor(source_cursor=0, broker_generation=1, entities={
                "account": [{"entity_id": "DU111", "entity_revision": 1,
                             "mode": "paper", "net_liquidation": 1_000_000.0,
                             "currency": "USD"}],
                "position": [{"entity_id": f"DU111:{conid}", "entity_revision": 1,
                              "conid": conid, "symbol": f"SYM{conid}",
                              "quantity": 100, "currency": "USD"}
                             for conid in range(1, self._instruments + 1)]})
        if method == "get_quotes_snapshot":
            return {"quotes": {}}
        raise AssertionError(method)

    def close(self):
        pass


class DomainProducer(threading.Thread):
    def __init__(self, feed: FakeJournalFeed, instruments: int, eps: float,
                 stop: threading.Event):
        super().__init__(daemon=True, name="soak-domain-producer")
        self._feed, self._instruments = feed, instruments
        self._eps, self._stop = eps, stop
        self.emitted = 0

    def run(self):
        cursor, revisions = 0, {}
        interval = 1.0 / self._eps
        while not self._stop.is_set():
            cursor += 1
            conid = (cursor % self._instruments) + 1
            entity = f"DU111:{conid}"
            revisions[entity] = revisions.get(entity, 1) + 1
            self._feed.append(DomainEvent(
                event_id=f"evt-{cursor}", source_cursor=cursor,
                entity_revision=revisions[entity],
                event_type="position.updated", entity_type="position",
                entity_id=entity, operation="upsert", account_id="DU111",
                source="soak",
                source_timestamp=dt.datetime.now(dt.timezone.utc),
                correlation_id=None,
                payload={"quantity": cursor, "conid": conid,
                         "harness_emitted_at": time.time()}))  # commit time
            self.emitted += 1
            self._stop.wait(interval)


class SyntheticQuotePlane(threading.Thread):
    """Same deliver contract as QuotePlane; emits synthetic conflated batches."""

    def __init__(self, loop, deliver, instruments: int, hz: float,
                 stop: threading.Event):
        super().__init__(daemon=True, name="soak-quote-producer")
        self._loop, self._deliver = loop, deliver
        self._instruments, self._interval = instruments, 1.0 / hz
        self._stop = stop
        self.dropped = 0
        self.emitted = 0

    def run(self):
        tick = 0
        while not self._stop.is_set():
            tick += 1
            batch = {str(conid): {"instrument_id": str(conid), "bid": 99.9,
                                  "ask": 100.1, "last": 100.0 + (tick % 7) * 0.01,
                                  "market_timestamp": None, "feed_type": "synthetic"}
                     for conid in range(1, self._instruments + 1)}
            self._loop.call_soon_threadsafe(self._deliver, batch)
            self.emitted += self._instruments
            self._stop.wait(self._interval)

    def stop(self, timeout: float = 5.0):
        self._stop.set()


async def run_tab(client: httpx.AsyncClient, latencies: list[float],
                  stop_at: float) -> None:
    async with client.stream("GET", "/api/events") as response:
        async for line in response.aiter_lines():
            if time.time() >= stop_at:
                return
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[5:])
            emitted = (payload.get("payload") or {}).get("harness_emitted_at")
            if emitted is not None:
                latencies.append((time.time() - emitted) * 1000.0)


def _pct(sorted_ms: list[float], q: float) -> float | None:
    if not sorted_ms:
        return None
    return sorted_ms[min(len(sorted_ms) - 1, int(len(sorted_ms) * q))]


def _report(latencies: list[float], rss: list[tuple[float, int]],
            warmup: float, cpu_cores: float, args) -> tuple[dict, list[str]]:
    ordered = sorted(latencies)
    post = [r for t, r in rss if t >= warmup] or [r for _, r in rss]
    growth = ((post[-1] - post[0]) / post[0] * 100.0) if post and post[0] else 0.0
    p95 = _pct(ordered, 0.95)
    violations = []
    if p95 is None or p95 > P95_TARGET_MS:
        violations.append(f"p95 {p95} ms exceeds {P95_TARGET_MS} ms target")
    if growth > RSS_GROWTH_LIMIT_PCT:
        violations.append(f"RSS grew {growth:.1f}% after warm-up (limit 20%)")
    if cpu_cores >= CPU_LIMIT_CORES:
        violations.append(f"avg CPU {cpu_cores:.2f} cores (limit < 1)")
    report = {
        "config": {"minutes": args.minutes, "instruments": args.instruments,
                   "quote_hz": args.quote_hz, "domain_eps": args.domain_eps,
                   "tabs": args.tabs},
        "domain_events_received": len(latencies),
        "latency_ms": {"p50": _pct(ordered, 0.50), "p95": p95,
                       "p99": _pct(ordered, 0.99),
                       "max": ordered[-1] if ordered else None},
        "rss_bytes": {"first_post_warmup": post[0] if post else None,
                      "final": post[-1] if post else None,
                      "growth_pct": round(growth, 2)},
        "cpu_avg_cores": round(cpu_cores, 3),
        "violations": violations,
    }
    return report, violations


async def amain(args) -> int:
    os.environ.setdefault("MMR_DILL_STRICT", "1")
    from web.app import create_app
    from web.command_center import CommandCenter, CommandCenterConfig
    from web.command_center.session import DashboardCredentials

    feed = FakeJournalFeed()
    stop = threading.Event()

    cc = CommandCenter(
        CommandCenterConfig(),
        credentials_loader=lambda: DashboardCredentials(
            token=TOKEN, session_secret=SECRET.encode(), legacy_alias_used=False),
        query_client_factory=lambda: FakeQuery(args.instruments),
        feed_client_factory=lambda: feed,
        quote_plane_factory=lambda loop, deliver: SyntheticQuotePlane(
            loop, deliver, args.instruments, args.quote_hz, stop))
    app = create_app(cc)

    process = psutil.Process()
    latencies: list[float] = []
    rss_samples: list[tuple[float, int]] = []
    duration = args.minutes * 60.0
    warmup = min(WARMUP_CAP_SECONDS, duration * 0.2)
    started = time.time()
    stop_at = started + duration

    async with cc.lifespan(app):
        producer = DomainProducer(feed, args.instruments, args.domain_eps, stop)
        producer.start()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://soak",
                                     timeout=None) as client:
            await client.post("/session", data={"token": TOKEN})
            tabs = [asyncio.create_task(run_tab(client, latencies, stop_at))
                    for _ in range(args.tabs)]
            process.cpu_percent()  # prime the interval counter
            while time.time() < stop_at:
                await asyncio.sleep(min(10.0, max(0.1, stop_at - time.time())))
                rss_samples.append(
                    (time.time() - started, process.memory_info().rss))
            cpu_cores = process.cpu_percent() / 100.0
            stop.set()
            feed.close()
            for tab in tabs:
                tab.cancel()
            await asyncio.gather(*tabs, return_exceptions=True)

    report, violations = _report(latencies, rss_samples, warmup, cpu_cores, args)
    print(json.dumps(report, indent=2))
    if args.report:
        Path(args.report).expanduser().write_text(json.dumps(report, indent=2))
    return 1 if violations else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Command-center perf/soak harness (spec §13.3)")
    parser.add_argument("--minutes", type=float, default=5.0)
    parser.add_argument("--instruments", type=int, default=100)
    parser.add_argument("--quote-hz", type=float, default=4.0)
    parser.add_argument("--domain-eps", type=float, default=20.0)
    parser.add_argument("--tabs", type=int, default=3)
    parser.add_argument("--report", default="")
    return asyncio.run(amain(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Smoke-run the harness**

Run:

```bash
MMR_DILL_STRICT=1 uv run --frozen python scripts/soak_harness.py --minutes 2 \
  --instruments 100 --quote-hz 4 --domain-eps 20 --tabs 3 \
  --report ~/.local/share/mmr/reports/soak_smoke.json
```

Expected: exit 0; the printed JSON shows `domain_events_received` ≈ 2 min × 60 s × 20 eps × 3 tabs ≈ 7,200, `latency_ms.p95` well under 500, and `violations: []`. The eight-hour run (`--minutes 480`) is executed and recorded by the `[COMPAT]` plan, not here.

- [ ] **Step 7: Write the Playwright browser gate**

Create `tests/cc_fakes.py` (move `_NullBridge`/`_NullQuotePlane` out of `tests/test_dashboard_snapshot_api.py`, renamed `NullBridge`/`NullQuotePlane`, and import them back there), then create `tests/browser/test_command_center.py`:

```python
"""Browser gate for the read-only command center (spec §13.2).

Runs only in the pinned Playwright/Chromium image (CI browser job) or when
MMR_BROWSER_TESTS=1 locally after `playwright install chromium`.
"""
import os
import socket
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MMR_BROWSER_TESTS") != "1",
    reason="browser gate runs in the pinned Playwright image (set MMR_BROWSER_TESTS=1)")

playwright_api = pytest.importorskip("playwright.sync_api")

TOKEN = "browser-token"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _seed(cc):
    from trader.domain.events import SnapshotWithCursor
    cc.state.install_baseline(
        SnapshotWithCursor(source_cursor=0, broker_generation=1, entities={
            "account": [{"entity_id": "DU111", "entity_revision": 1,
                         "mode": "paper", "net_liquidation": 250_000.0,
                         "currency": "USD"}],
            "position": [{"entity_id": "DU111:265598", "entity_revision": 1,
                          "conid": 265598, "symbol": "AAPL", "quantity": 100,
                          "avg_cost": 180.0, "currency": "USD",
                          "unrealized_pnl": 1_250.0}],
            "proposal": [{"entity_id": "42", "entity_revision": 1,
                          "status": "PENDING", "action": "BUY", "symbol": "AMD",
                          "quantity": 50, "confidence": 0.7,
                          "reasoning": "Breakout above resistance",
                          "sizing_result": {"reasoning": [
                              "base 2% of equity", "confidence scale 0.7",
                              "ATR volatility adjustment 0.8"]}}],
        }),
        stream_id="browser-stream")


@pytest.fixture(scope="module")
def server():
    os.environ.setdefault("MMR_DILL_STRICT", "1")
    os.environ["CC_DEGRADED_AFTER_MS"] = "1000"   # spec default 15000; shrunk for test speed
    os.environ["CC_POLL_INTERVAL_MS"] = "500"     # spec default 5000
    from fastapi.responses import JSONResponse
    from web.app import create_app
    from web.command_center import CommandCenter, CommandCenterConfig
    from web.command_center.session import DashboardCredentials
    from tests.cc_fakes import NullBridge, NullQuotePlane

    cc = CommandCenter(
        CommandCenterConfig(),
        credentials_loader=lambda: DashboardCredentials(
            token=TOKEN, session_secret=b"s" * 64, legacy_alias_used=False),
        query_client_factory=lambda: None,
        feed_client_factory=lambda: None,
        bridge_factory=lambda *a, **k: NullBridge(),
        quote_plane_factory=lambda loop, deliver: NullQuotePlane())
    app = create_app(cc)
    broken = {"sse": False}

    @app.middleware("http")
    async def _breaker(request, call_next):
        if broken["sse"] and request.url.path == "/api/events":
            return JSONResponse({"detail": "broken"}, status_code=503)
        return await call_next(request)

    @app.post("/_test/break-sse")
    async def _break():
        broken["sse"] = True
        cc.fanout.broadcast_resync()   # kick live clients into reconnect
        return {"ok": True}

    @app.post("/_test/fix-sse")
    async def _fix():
        broken["sse"] = False
        return {"ok": True}

    import uvicorn
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not uv_server.started and time.time() < deadline:
        time.sleep(0.05)
    _seed(cc)  # before any client connects; the loop has no state readers yet
    yield f"http://127.0.0.1:{port}", cc
    uv_server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def page(server):
    base_url, _cc = server
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context().new_page()
        page.goto(f"{base_url}/cc")           # redirects to /cc/login
        page.fill("#token", TOKEN)
        page.click("button[type=submit]")
        page.wait_for_selector("#positions-body tr")
        yield page
        browser.close()


class TestLayoutA:
    def test_regions_render_with_positions_dominant(self, page):
        for selector in ("#status-bar", "#account-cards", "#positions-panel",
                         "#action-rail", "#orders-panel", "#fills-panel",
                         "#strategies-panel", "#risk-panel"):
            assert page.is_visible(selector), selector
        positions = page.locator("#positions-panel").bounding_box()
        rail = page.locator("#action-rail").bounding_box()
        assert positions["width"] > rail["width"]  # dominant workspace

    def test_status_bar_shows_mode_badge_and_exact_account(self, page):
        assert page.inner_text("#mode-badge") == "PAPER"
        assert "DU111" in page.inner_text("#account-id")

    def test_account_card_shows_net_liquidation_from_account_entity(self, page):
        assert "250,000" in page.inner_text("#account-cards")


class TestFreshnessAndAlerts:
    def test_position_row_shows_freshness_age(self, page):
        age = page.inner_text("#positions-body tr .age")
        assert age  # "no data" without quotes; the row carries the stale class
        assert "stale" in (page.get_attribute("#positions-body tr", "class") or "")

    def test_risk_unavailable_alert_is_not_color_only(self, page):
        text = page.inner_text("#risk-body")
        assert "Risk unavailable" in text and "⚠" in text
        assert page.get_attribute("#risk-body", "data-state") == "unavailable"


class TestDrawerKeyboardFocus:
    def test_enter_opens_drawer_focuses_close_escape_returns_focus(self, page):
        card = page.locator(".proposal-card[data-proposal]").first
        card.focus()
        page.keyboard.press("Enter")
        assert page.is_visible("#drawer")
        assert page.evaluate("document.activeElement.id") == "drawer-close"
        detail = page.inner_text("#drawer-content").lower()
        assert "sizing" in detail and "atr volatility" in detail  # full chain
        page.keyboard.press("Escape")
        assert page.is_hidden("#drawer")
        assert page.evaluate("!!document.activeElement.dataset.proposal")


class TestDegradedFallback:
    def test_sse_loss_shows_banner_polls_then_recovers(self, page, server):
        base_url, _cc = server
        page.request.post(f"{base_url}/_test/break-sse")
        page.wait_for_selector("#degraded-banner:not([hidden])", timeout=10_000)
        assert "degraded" in page.inner_text("#degraded-banner").lower()
        # polling keeps the page alive: snapshot data still renders
        assert page.is_visible("#positions-body tr")
        page.request.post(f"{base_url}/_test/fix-sse")
        # back to SSE only after a coherent snapshot; banner then clears
        page.wait_for_selector("#degraded-banner[hidden]", state="attached",
                               timeout=15_000)
```

- [ ] **Step 8: Run the browser gate**

Run:

```bash
uv sync --python "$(cat .python-version)" --frozen --extra test --extra browser-test
uv run --frozen playwright install chromium
MMR_BROWSER_TESTS=1 uv run --frozen pytest tests/browser/test_command_center.py -q
```

Expected: PASS (7 tests). Without `MMR_BROWSER_TESTS=1` the file reports SKIPPED, so the default suite stays green on machines without Chromium. CI runs this file inside the pinned Playwright/Chromium image as its own required job (per the `[G0]` browser-job setup).

- [ ] **Step 9: Run the full M1-R gate and the canonical suite**

Run: `uv run --frozen pytest tests/test_dashboard_session.py tests/test_dashboard_state.py tests/test_dashboard_bridge.py tests/test_dashboard_sse.py tests/test_dashboard_snapshot_api.py tests/test_web_dashboard.py -q`

Expected: PASS.

Run: `uv run --frozen pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py`

Expected: PASS with no new failures or warnings introduced by M1-R.

- [ ] **Step 10: Commit**

```bash
git add web/command_center/health.py web/command_center/state.py web/app.py scripts/soak_harness.py tests/browser/test_command_center.py tests/cc_fakes.py tests/test_dashboard_snapshot_api.py
git commit -m "feat(m1-r): readiness health, soak harness, and browser gate"
```

