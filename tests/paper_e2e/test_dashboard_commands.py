"""Live paper-stack coverage for Command Center control commands."""
from __future__ import annotations

from uuid import uuid4

import pytest


def _command_headers(dashboard_client, paper_stack) -> dict[str, str]:
    return {
        **dashboard_client.csrf_headers(),
        "Origin": paper_stack.dashboard_url,
    }


def _orders(payload: dict) -> list[dict]:
    for key in ("orders", "open_orders"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _e2e_order_ids(payload: dict, e2e_id: str) -> list[str]:
    """Return only IDs whose full broker/order record bears this run's marker."""
    ids: list[str] = []
    for order in _orders(payload):
        if e2e_id not in str(order):
            continue
        order_id = order.get("order_entity_id")
        if isinstance(order_id, str) and order_id:
            ids.append(order_id)
    return ids


def test_pause_then_resume_via_command_center(
    dashboard_client, paper_stack, typed_rpc, e2e_id, require_capability,
):
    """CSRF-protected pause is always followed by a resume in finally."""
    require_capability("trading_control")
    login = dashboard_client.login()
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"

    control = typed_rpc.query.call("get_trading_control", {}, dict)
    if control.get("new_exposure_paused"):
        pytest.skip("trading is already paused; refusing to alter operator state")
    readiness = typed_rpc.query.call("get_status", {}, dict).get(
        "semantic_readiness", {}
    )
    if readiness.get("ready") is not True:
        pytest.skip(
            "resume is fail-closed until semantic readiness is restored: "
            f"{readiness.get('failed', [])}"
        )

    paused = False
    try:
        pause = dashboard_client.post(
            "/api/commands/pause",
            headers=_command_headers(dashboard_client, paper_stack),
            json={
                "command_id": str(uuid4()),
                "reason": f"{e2e_id} Command Center pause exercise",
            },
        )
        assert pause.status_code == 202, (
            f"pause command failed: {pause.status_code} {pause.text}"
        )
        paused = True
    finally:
        if paused:
            control = typed_rpc.query.call("get_trading_control", {}, dict)
            resume = dashboard_client.post(
                "/api/commands/resume",
                headers=_command_headers(dashboard_client, paper_stack),
                json={
                    "command_id": str(uuid4()),
                    "expected_control_revision": control["revision"],
                    "reason": f"{e2e_id} Command Center pause cleanup",
                },
            )
            assert resume.status_code == 202, (
                f"resume command failed: {resume.status_code} {resume.text}"
            )
            after = typed_rpc.query.call("get_trading_control", {}, dict)
            assert after.get("new_exposure_paused") is False, (
                f"trading remained paused after cleanup: {after}"
            )


def test_cancel_routes_only_target_this_runs_orders(
    dashboard_client, paper_stack, typed_rpc, e2e_id,
):
    """Never cancel operator orders; default-safe runs normally skip this path."""
    login = dashboard_client.login()
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"

    order_ids = _e2e_order_ids(
        typed_rpc.query.call("get_open_orders", {}, dict), e2e_id
    )
    if not order_ids:
        pytest.skip("no E2E-created open orders to cancel")

    headers = _command_headers(dashboard_client, paper_stack)
    cancelled = dashboard_client.post(
        f"/api/commands/orders/{order_ids[0]}/cancel",
        headers=headers,
        json={"command_id": str(uuid4())},
    )
    assert cancelled.status_code == 202, (
        f"cancel order failed: {cancelled.status_code} {cancelled.text}"
    )

    remaining = _e2e_order_ids(
        typed_rpc.query.call("get_open_orders", {}, dict), e2e_id
    )
    if remaining:
        cancel_all = dashboard_client.post(
            "/api/commands/orders/cancel-all",
            headers=headers,
            json={"command_id": str(uuid4()), "order_entity_ids": remaining},
        )
        assert cancel_all.status_code == 202, (
            f"cancel all failed: {cancel_all.status_code} {cancel_all.text}"
        )
