"""Live paper-stack coverage for typed watchlist (universe) operations."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.paper_e2e._probe import derive_html_form_csrf


def _delete_universe(typed_rpc, name: str) -> None:
    """Best-effort cleanup for a test that fails before session teardown."""
    try:
        typed_rpc.command.call("delete_universe", {"name": name}, dict)
    except Exception:  # noqa: BLE001 - teardown must preserve test failures.
        pass


def _create_with_aapl(typed_rpc, name: str) -> None:
    typed_rpc.command.call("create_universe", {"name": name}, dict)
    added = typed_rpc.command.call(
        "add_universe_symbols", {"name": name, "symbols": ["AAPL"]}, dict
    )
    assert "AAPL" in {entry["symbol"] for entry in added["added"]}


def _session_secret() -> str:
    secret = (os.environ.get("DASHBOARD_SESSION_SECRET") or "").strip()
    if secret:
        return secret
    path = (os.environ.get("DASHBOARD_SESSION_SECRET_FILE") or "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def test_universe_crud_via_typed_rpc(typed_rpc, e2e_id):
    """Create, populate, inspect, remove, and delete this run's universe."""
    try:
        created = typed_rpc.command.call("create_universe", {"name": e2e_id}, dict)
        assert created["name"] == e2e_id

        added = typed_rpc.command.call(
            "add_universe_symbols", {"name": e2e_id, "symbols": ["AAPL"]}, dict
        )
        assert "AAPL" in {entry["symbol"] for entry in added["added"]}

        universe = typed_rpc.query.call("get_universe", {"name": e2e_id}, dict)
        assert universe["name"] == e2e_id
        assert "AAPL" in universe["symbols"]

        universes = typed_rpc.query.call("list_universes", {}, dict)
        assert e2e_id in {entry["name"] for entry in universes["universes"]}

        removed = typed_rpc.command.call(
            "remove_universe_symbol", {"name": e2e_id, "symbol": "AAPL"}, dict
        )
        assert removed["ok"] is True
        assert typed_rpc.query.call("get_universe", {"name": e2e_id}, dict)["symbols"] == []

        deleted = typed_rpc.command.call("delete_universe", {"name": e2e_id}, dict)
        assert deleted["ok"] is True
    finally:
        _delete_universe(typed_rpc, e2e_id)


def test_watchlist_members_http_read(dashboard_client, typed_rpc, e2e_id):
    """The session-authenticated HTTP members endpoint exposes typed additions."""
    try:
        _create_with_aapl(typed_rpc, e2e_id)

        login = dashboard_client.login()
        assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"
        response = dashboard_client.get(f"/watchlists/{e2e_id}/members")
        assert response.status_code == 200, (
            f"members request failed: {response.status_code} {response.text}"
        )
        members = response.json()
        assert members["name"] == e2e_id
        assert "AAPL" in members["symbols"]
    finally:
        _delete_universe(typed_rpc, e2e_id)


def test_legacy_watchlist_create_form(dashboard_client, typed_rpc, e2e_id):
    """The legacy HTML watchlist-create form remains compatible with sessions."""
    csrf_secret = _session_secret()
    if len(csrf_secret) < 32:
        pytest.skip("DASHBOARD_SESSION_SECRET is unavailable or too short")
    try:
        login = dashboard_client.login()
        assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"
        response = dashboard_client.post(
            "/watchlists/create",
            data={"name": e2e_id, "csrf_token": derive_html_form_csrf(csrf_secret)},
            follow_redirects=False,
        )
        assert response.status_code == 303, (
            f"watchlist create failed: {response.status_code} {response.text}"
        )
        assert typed_rpc.query.call("get_universe", {"name": e2e_id}, dict)["name"] == e2e_id
    finally:
        _delete_universe(typed_rpc, e2e_id)


@pytest.mark.paper_e2e_restart
@pytest.mark.timeout(300)
def test_portfolio_survives_trader_restart(
    dashboard_client, paper_stack, typed_rpc, e2e_id
):
    """Persisted universe members are available after the trader restarts."""
    if not paper_stack.restart:
        pytest.skip("set MMR_PAPER_E2E_RESTART=1 to restart the trader container")
    try:
        _create_with_aapl(typed_rpc, e2e_id)
        subprocess.run(
            ["docker", "compose", "-f", "docker-compose.yml", "restart", "trader"],
            check=True,
            cwd=Path(__file__).resolve().parents[2],
            timeout=120,
        )

        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                health = dashboard_client.get("/healthz")
                status = typed_rpc.query.call("get_status", {}, dict)
                if health.status_code == 200 and health.json().get("ok") is True and status:
                    break
            except Exception:  # noqa: BLE001 - trader is expected to be restarting.
                pass
            time.sleep(2)
        else:
            pytest.fail("trader did not become healthy within 180 seconds")

        universe = typed_rpc.query.call("get_universe", {"name": e2e_id}, dict)
        assert "AAPL" in universe["symbols"]
    finally:
        _delete_universe(typed_rpc, e2e_id)
