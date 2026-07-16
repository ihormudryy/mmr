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
        assert "old-token" not in caplog.text

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
