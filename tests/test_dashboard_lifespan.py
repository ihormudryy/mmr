"""The ops probes must survive a command-center misconfiguration.

[M1-R] Task 5 fix wave: the always-on liveness/readiness probes (`/healthz`,
`/readyz`) and the app's ability to boot MUST NOT depend on the command
center's startup guards (dill-strict + dashboard credentials) succeeding. A
command-center startup failure degrades it to inert -- logged loudly -- while
the app still boots and the probes still serve. Dashboard routes still fail
loud per request; dill-strict is still enforced at the quote-decode seam.

Boots the app through FastAPI's TestClient *as a context manager* so the ASGI
lifespan actually runs (a bare TestClient never fires startup/shutdown).
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import web.app as webapp
import web.command_center.quotes as quotes
from web.app import create_app
from web.command_center.session import CredentialConfigError


def _unconfigure(monkeypatch):
    """Env with NO dill-strict and NO dashboard credentials -- the exact
    shape that used to abort ASGI startup (and kill the ops probes)."""
    monkeypatch.delenv("MMR_DILL_STRICT", raising=False)
    for var in ("DASHBOARD_TOKEN", "DASHBOARD_TOKEN_FILE", "MMR_WEB_TOKEN",
                "DASHBOARD_SESSION_SECRET", "DASHBOARD_SESSION_SECRET_FILE"):
        monkeypatch.delenv(var, raising=False)


def test_probes_serve_when_command_center_degrades(monkeypatch, caplog):
    _unconfigure(monkeypatch)
    # `_READY` is module-global; pin it True so a prior context-managed
    # TestClient's shutdown flip can't leak in and make this assertion flaky.
    # monkeypatch reverts it after the test.
    monkeypatch.setattr(webapp, "_READY", True)
    app = create_app()

    with caplog.at_level(logging.ERROR, logger="web.command_center"):
        # Context-managed => the lifespan runs. Before the fix this raised at
        # __enter__ because _assert_dill_strict() aborted startup.
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/healthz").json() == {"ok": True}
            assert client.get("/readyz").status_code == 200
            assert client.get("/readyz").json() == {"ready": True}

    center = app.state.command_center
    # The command center degraded to inert: it never started the quote plane
    # (so it never touched a dill payload) and left no bridge behind.
    assert center.quote_plane is None
    assert center.bridge is None
    # ...and it degraded LOUDLY, not silently.
    assert any(
        rec.levelno >= logging.ERROR and "DEGRADED" in rec.getMessage().upper()
        for rec in caplog.records), "command center must log its degrade loudly"


def test_dill_strict_still_enforced_at_quote_decode_seam(monkeypatch):
    """Relaxing the *startup* assertion to degrade-not-abort must not open a
    dill execution hole: the quote-decode seam still refuses without opt-in."""
    _unconfigure(monkeypatch)
    with pytest.raises(RuntimeError, match="MMR_DILL_STRICT"):
        quotes._default_decode(b"\x81\xa5conId\xcd\x01\x02")


def test_gated_route_redirects_not_500_when_unconfigured(monkeypatch):
    """MINOR: a gated dashboard request must never 500 from the middleware
    because no session manager can be built -- it redirects to /cc/login (or
    401 for /api/*). The per-request fail-loud stays in require_session."""
    _unconfigure(monkeypatch)
    app = create_app()
    center = app.state.command_center
    # Sanity: with no credentials, ensure_session_manager truly can't build one.
    try:
        center.ensure_session_manager()
        built = True
    except CredentialConfigError:
        built = False
    assert built is False

    # Bare TestClient (no lifespan needed) -- the middleware is what we test.
    client = TestClient(app)
    api = client.get("/api/snapshot")
    assert api.status_code == 401  # /api/* -> 401, never 500
    page = client.get("/cc", follow_redirects=False)
    assert page.status_code == 303  # HTML route -> redirect to login
    assert page.headers["location"] == "/cc/login"
