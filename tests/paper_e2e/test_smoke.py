"""Layer A paper-stack smoke ladder."""
from __future__ import annotations

import time
from uuid import uuid4

import pytest


@pytest.mark.timeout(180)
def test_smoke_ladder(
    dashboard_client, typed_rpc, paper_stack, e2e_id, require_capability
):
    """Exercise the authenticated proposal path without default order placement."""
    require_capability("proposals")

    ready = dashboard_client.get("/readyz")
    assert ready.status_code == 200, f"readyz failed: {ready.status_code} {ready.text}"

    login = dashboard_client.login()
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"
    health = dashboard_client.get("/api/cc-health")
    assert health.status_code == 200, (
        f"login/cc-health failed: {health.status_code} {health.text}"
    )

    status = typed_rpc.query.call("get_status", {}, dict)
    assert status, "typed get_status returned no data"
    positions = typed_rpc.query.call("get_positions", {}, dict)
    assert positions is not None, "typed get_positions returned no data"

    instruments = []
    for attempt in range(2):
        resolved = typed_rpc.query.call(
            "discover_instrument",
            {"symbol": "AAPL", "exchange": "", "currency": "", "sec_type": "STK"},
            dict,
        )
        instruments = resolved.get("instruments") or []
        if instruments:
            break
        if attempt == 0:
            time.sleep(0.5)
    if not instruments:
        pytest.skip("IB resolve AAPL unavailable")
    conid = int(instruments[0]["instrument_id"])

    create_id = str(uuid4())
    command_headers = {
        **dashboard_client.csrf_headers(),
        "Origin": paper_stack.dashboard_url,
    }
    create = dashboard_client.post(
        "/api/commands/proposals",
        headers=command_headers,
        json={
            "command_id": create_id,
            "conid": conid,
            "action": "BUY",
            "quantity": 1,
            "reasoning": f"{e2e_id} Layer A smoke proposal",
        },
    )
    assert create.status_code == 202, (
        f"create proposal failed: {create.status_code} {create.text}"
    )

    command = typed_rpc.query.call("get_command", {"command_id": create_id}, dict)
    proposal_id = (command.get("outcome") or {}).get("id")
    assert proposal_id is not None, f"create proposal has no outcome: {command}"
    proposal = typed_rpc.query.call(
        "get_proposal", {"proposal_id": proposal_id}, dict
    )
    assert e2e_id in proposal.get("reasoning", "")

    if paper_stack.live_orders:
        require_capability("approval")
        approved = typed_rpc.command.call(
            "approve_proposal",
            {
                "command_id": str(uuid4()),
                "proposal_id": proposal_id,
                "expected_version": proposal["revision"],
            },
            dict,
        )
        assert approved, "approve proposal returned no receipt"
        paper_stack.register_opened_position(conid, 1)
    else:
        rejected = typed_rpc.command.call(
            "reject_proposal",
            {
                "command_id": str(uuid4()),
                "proposal_id": proposal_id,
                "reason": f"{e2e_id} Layer A smoke cleanup",
            },
            dict,
        )
        assert rejected, "reject proposal returned no receipt"
