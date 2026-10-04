"""Live paper-stack coverage for Scaling reads and gated arm paths."""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest


_SCALING_KEYS = {
    "status",
    "lifecycle",
    "message",
    "stage",
    "max_gross_allocation",
    "expires_at",
    "event",
    "authorities",
}
_AUTOMATION_LIFECYCLES = {
    "armed",
    "armed_unpersisted",
    "degraded",
    "disabled",
    "failed",
    "restart_required",
}


def _command_id() -> str:
    return str(uuid4())


def _preflight_activation(
    typed_rpc, *, action: str, params: dict
) -> tuple[str, str, str]:
    """Mint a nonce bound to the exact typed activation payload."""
    command_id = _command_id()
    fingerprint = uuid4().hex
    ticket = typed_rpc.command.call(
        "preflight_command",
        {
            "command_id": command_id,
            "action": action,
            "params": params,
            "session_fingerprint": fingerprint,
        },
        dict,
    )
    nonce = ticket.get("nonce")
    assert isinstance(nonce, str) and nonce, f"preflight returned no nonce: {ticket}"
    return command_id, nonce, fingerprint


def _allocation_attestation() -> dict:
    """Load an operator-provisioned, stack-bound allocation attestation."""
    configured = os.environ.get("MMR_PAPER_E2E_ALLOCATION_ATTESTATION_FILE", "")
    if not configured:
        pytest.skip(
            "set MMR_PAPER_E2E_ALLOCATION_ATTESTATION_FILE to exercise "
            "allocation activation"
        )
    try:
        value = json.loads(Path(configured).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        pytest.skip(f"allocation attestation is unavailable: {exc}")
    if not isinstance(value, dict):
        pytest.skip("allocation attestation JSON must be an object")
    return value


def _require_live_orders(paper_stack) -> None:
    if not paper_stack.live_orders:
        pytest.skip("set MMR_PAPER_E2E_LIVE_ORDERS=1 to arm paper controls")


def test_scaling_snapshot_never_infers_green_without_authority_events(
    dashboard_client,
):
    """An empty authority stream is unknown, not a healthy allocation state."""
    login = dashboard_client.login()
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"

    response = dashboard_client.get("/api/snapshot")
    assert response.status_code == 200, (
        f"scaling snapshot failed: {response.status_code} {response.text}"
    )
    scaling = response.json().get("scaling")
    assert isinstance(scaling, dict), f"snapshot scaling must be an object: {scaling}"
    assert _SCALING_KEYS <= scaling.keys(), f"scaling shape changed: {scaling}"
    assert isinstance(scaling["status"], str) and scaling["status"]
    assert isinstance(scaling["message"], str) and scaling["message"]
    assert isinstance(scaling["authorities"], list)
    assert scaling["max_gross_allocation"] is None or isinstance(
        scaling["max_gross_allocation"], (int, float)
    )

    if not scaling["authorities"]:
        assert scaling["status"] == "unknown", (
            "absence of allocation events must not be presented as healthy: "
            f"{scaling}"
        )
        assert scaling["lifecycle"] == "unknown"
        assert scaling["max_gross_allocation"] is None


def test_paper_automation_status_is_typed_and_never_arms_by_reading(
    paper_stack, dashboard_client, typed_rpc, require_capability,
):
    """Read the unarmed/refusal state only; this test never calls activate."""
    require_capability("paper_automation")
    typed_status = typed_rpc.query.call("get_paper_automation_status", {}, dict)
    assert isinstance(typed_status, dict) and typed_status
    assert typed_status.get("lifecycle") in _AUTOMATION_LIFECYCLES
    assert isinstance(typed_status.get("armed_unpersisted"), bool)
    assert isinstance(typed_status.get("command_authority_ready"), bool)
    assert typed_status.get("account_mode") == "paper"

    login = dashboard_client.login()
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"
    snapshot = dashboard_client.get("/api/snapshot")
    assert snapshot.status_code == 200, (
        f"automation snapshot failed: {snapshot.status_code} {snapshot.text}"
    )
    http_status = snapshot.json().get("paper_automation")
    assert isinstance(http_status, dict), (
        "snapshot paper_automation must expose the typed status when the "
        f"capability is present: {http_status}"
    )
    assert http_status.get("lifecycle") == typed_status.get("lifecycle")
    assert paper_stack.paper_automation_armed is False
    assert paper_stack.allocation_armed is False


@pytest.mark.paper_e2e_live_orders
@pytest.mark.timeout(300)
def test_live_allocation_activation_is_preflight_bound_and_teardown_suspends(
    paper_stack, dashboard_client, typed_rpc, require_capability, e2e_id,
):
    """Activate only an operator-provided authority; session teardown suspends it."""
    _require_live_orders(paper_stack)
    require_capability("allocation")
    attestation = _allocation_attestation()
    login = dashboard_client.login()
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"
    snapshot = dashboard_client.get("/api/snapshot")
    assert snapshot.status_code == 200, (
        f"allocation snapshot failed: {snapshot.status_code} {snapshot.text}"
    )
    scaling = snapshot.json().get("scaling") or {}
    if scaling.get("status") not in {"unknown", "inactive", "suspended"}:
        pytest.skip(
            "allocation already has operator state; refusing to mutate it: "
            f"{scaling.get('status')!r}"
        )
    reason = f"{e2e_id} paper e2e allocation activation"
    command_id, nonce, fingerprint = _preflight_activation(
        typed_rpc,
        action="activate_allocation",
        params={"attestation": attestation, "reason": reason},
    )

    # Set this before the mutating call: a transport timeout can leave the
    # outcome unknown, but must still trigger risk-reducing teardown.
    paper_stack.allocation_armed = True
    activated = typed_rpc.command.call(
        "activate_allocation",
        {
            "command_id": command_id,
            "attestation": attestation,
            "reason": reason,
            "preflight_nonce": nonce,
            "session_fingerprint": fingerprint,
        },
        dict,
    )
    assert activated, "activate_allocation returned no receipt"


@pytest.mark.paper_e2e_live_orders
@pytest.mark.timeout(300)
def test_live_paper_automation_activation_is_preflight_bound_and_teardown_deactivates(
    paper_stack, typed_rpc, require_capability, e2e_id,
):
    """Arm an explicitly opted-in strategy only; session teardown deactivates it."""
    _require_live_orders(paper_stack)
    require_capability("paper_automation")
    strategy_name = os.environ.get("MMR_PAPER_E2E_AUTOMATION_STRATEGY", "").strip()
    if not strategy_name:
        pytest.skip(
            "set MMR_PAPER_E2E_AUTOMATION_STRATEGY to arm a specific paper strategy"
        )

    before = typed_rpc.query.call("get_paper_automation_status", {}, dict)
    if before.get("lifecycle") != "disabled":
        pytest.skip(
            "paper automation is already configured; refusing to mutate "
            f"operator state: {before.get('lifecycle')!r}"
        )
    if before.get("command_authority_ready") is not True:
        pytest.skip("paper automation command authority is not ready")

    reason = f"{e2e_id} paper e2e automation activation"
    command_id, nonce, fingerprint = _preflight_activation(
        typed_rpc,
        action="activate_paper_automation",
        params={"strategy_name": strategy_name, "reason": reason},
    )
    # As above, ensure teardown disarms even when the response is lost after
    # the service has accepted the activation.
    paper_stack.paper_automation_armed = True
    activated = typed_rpc.command.call(
        "activate_paper_automation",
        {
            "command_id": command_id,
            "strategy_name": strategy_name,
            "reason": reason,
            "preflight_nonce": nonce,
            "session_fingerprint": fingerprint,
        },
        dict,
    )
    assert activated, "activate_paper_automation returned no receipt"
