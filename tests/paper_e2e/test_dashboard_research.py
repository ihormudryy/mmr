"""Live paper-stack coverage for research reads and proposal rejection."""
from __future__ import annotations

import time
from uuid import uuid4

import httpx
import pytest


def _assert_research_response(response: httpx.Response) -> dict:
    """Accept data or a structured entitlement response, never HTML or 5xx."""
    assert response.status_code < 500, (
        f"research request failed: {response.status_code} {response.text}"
    )
    assert "application/json" in response.headers.get("content-type", ""), (
        f"research response was not JSON: {response.headers.get('content-type')}"
    )
    body = response.json()
    assert isinstance(body, dict), f"research response must be an object: {body}"
    if response.status_code == 200:
        assert "data" in body, f"research success has no data envelope: {body}"
    else:
        error = body.get("error")
        assert isinstance(error, dict), f"research soft failure is unstructured: {body}"
        assert isinstance(error.get("code"), str) and error["code"]
        assert isinstance(error.get("message"), str) and error["message"]
    return body


@pytest.mark.timeout(180)
def test_research_presets_read(dashboard_client):
    """Authenticated presets are available as a JSON list or object."""
    login = dashboard_client.login()
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"

    body = _assert_research_response(
        dashboard_client.get("/api/research/presets")
    )
    assert isinstance(body["data"], (list, dict))


@pytest.mark.timeout(180)
@pytest.mark.parametrize("path", [
    "/api/research/ideas",
    "/api/research/movers",
    "/api/research/snapshot?symbol=AAPL",
    "/api/research/news?ticker=AAPL",
])
def test_research_gets_return_data_or_structured_entitlement_error(
    dashboard_client, path
):
    """Provider-backed reads must not surface HTML or an unhandled server error."""
    login = dashboard_client.login()
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"

    _assert_research_response(dashboard_client.get(path))


@pytest.mark.timeout(180)
def test_research_get_requires_session(paper_stack):
    """A fresh client cannot call research endpoints without a session cookie."""
    with httpx.Client(base_url=paper_stack.dashboard_url, timeout=10.0) as client:
        response = client.get("/api/research/presets")

    assert response.status_code in (401, 403), (
        f"unauthenticated research read unexpectedly succeeded: "
        f"{response.status_code} {response.text}"
    )


@pytest.mark.timeout(180)
def test_resolve_then_propose_and_reject_via_command_api(
    dashboard_client, typed_rpc, paper_stack, e2e_id, require_capability,
):
    """Resolve AAPL, create only through the command API, then reject by default."""
    require_capability("proposals")
    login = dashboard_client.login()
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"

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

    headers = {
        **dashboard_client.csrf_headers(),
        "Origin": paper_stack.dashboard_url,
    }
    create_id = str(uuid4())
    created = dashboard_client.post(
        "/api/commands/proposals",
        headers=headers,
        json={
            "command_id": create_id,
            "conid": conid,
            "action": "BUY",
            "quantity": 1,
            "group": e2e_id,
            "reasoning": f"{e2e_id} research proposal; reject by default",
        },
    )
    assert created.status_code == 202, (
        f"create proposal failed: {created.status_code} {created.text}"
    )

    command = typed_rpc.query.call("get_command", {"command_id": create_id}, dict)
    proposal_id = (command.get("outcome") or {}).get("id")
    assert proposal_id is not None, f"create proposal has no outcome: {command}"
    proposal = typed_rpc.query.call("get_proposal", {"proposal_id": proposal_id}, dict)
    assert e2e_id in proposal.get("group", "")
    assert e2e_id in proposal.get("reasoning", "")

    rejected = dashboard_client.post(
        f"/api/commands/proposals/{proposal_id}/reject",
        headers=headers,
        json={
            "command_id": str(uuid4()),
            "reason": f"{e2e_id} research E2E cleanup",
        },
    )
    assert rejected.status_code == 202, (
        f"reject proposal failed: {rejected.status_code} {rejected.text}"
    )
    after = typed_rpc.query.call("get_proposal", {"proposal_id": proposal_id}, dict)
    assert after.get("status") == "REJECTED", f"proposal was not rejected: {after}"
