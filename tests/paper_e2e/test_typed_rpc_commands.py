"""Live paper-stack coverage for the public typed command surface."""
from __future__ import annotations

from uuid import uuid4

import pytest

from trader.messaging.typed_rpc import TypedRpcRemoteError


def _aapl_conid(typed_rpc) -> int:
    result = typed_rpc.query.call(
        "discover_instrument",
        {"symbol": "AAPL", "exchange": "", "currency": "", "sec_type": "STK"},
        dict,
    )
    instruments = result.get("instruments") or []
    if not instruments:
        pytest.skip("IB resolve AAPL unavailable")
    return int(instruments[0]["instrument_id"])


def _strategy_rows(dashboard_client) -> list[dict]:
    response = dashboard_client.get("/api/snapshot")
    assert response.status_code == 200, (
        f"strategy snapshot failed: {response.status_code} {response.text}"
    )
    rows = response.json().get("strategies")
    assert isinstance(rows, list), f"snapshot strategies must be a list: {rows}"
    if not rows:
        pytest.skip("no deployed strategies")
    return rows


def _strategy_name(row: dict) -> str:
    name = row.get("strategy_name") or row.get("name")
    assert isinstance(name, str) and name, f"strategy row has no name: {row}"
    return name


def _strategy_row(dashboard_client, strategy_name: str) -> dict:
    for row in _strategy_rows(dashboard_client):
        if _strategy_name(row) == strategy_name:
            return row
    raise AssertionError(f"strategy {strategy_name!r} disappeared from snapshot")


def _control_revision(row: dict) -> int:
    revision = row.get("control_revision")
    assert isinstance(revision, int) and revision >= 0, (
        f"strategy row has no control revision: {row}"
    )
    return revision


def _is_disabled(row: dict) -> bool:
    return str(row.get("strategy_state") or row.get("state") or "").upper() == "DISABLED"


def test_create_then_reject_proposal_via_typed_rpc(
    typed_rpc, e2e_id, require_capability,
):
    """Default-safe proposal flow never approves or places an order."""
    require_capability("proposals")
    proposal_id: int | None = None
    try:
        created = typed_rpc.command.call(
            "create_proposal",
            {
                "command_id": str(uuid4()),
                "conid": _aapl_conid(typed_rpc),
                "action": "BUY",
                "quantity": 1,
                "group": e2e_id,
                "reasoning": f"{e2e_id} typed RPC proposal; reject by default",
            },
            dict,
        )
        proposal_id = (created.get("outcome") or {}).get("id")
        assert isinstance(proposal_id, int), f"create proposal has no id: {created}"

        proposal = typed_rpc.query.call(
            "get_proposal", {"proposal_id": proposal_id}, dict
        )
        assert e2e_id in proposal.get("reasoning", ""), (
            f"proposal lacks this run's marker: {proposal}"
        )
    finally:
        if proposal_id is not None:
            rejected = typed_rpc.command.call(
                "reject_proposal",
                {
                    "command_id": str(uuid4()),
                    "proposal_id": proposal_id,
                    "reason": f"{e2e_id} typed RPC cleanup",
                },
                dict,
            )
            assert rejected, "reject_proposal returned no receipt"


def test_pause_then_resume_via_typed_rpc(
    typed_rpc, e2e_id, require_capability,
):
    """Pause is always restored in finally, even if an assertion fails."""
    require_capability("trading_control")
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
        receipt = typed_rpc.command.call(
            "pause_trading",
            {
                "command_id": str(uuid4()),
                "reason": f"{e2e_id} typed RPC pause exercise",
            },
            dict,
        )
        assert receipt, "pause_trading returned no receipt"
        paused = True
    finally:
        if paused:
            control = typed_rpc.query.call("get_trading_control", {}, dict)
            resumed = typed_rpc.command.call(
                "resume_trading",
                {
                    "command_id": str(uuid4()),
                    "expected_control_revision": control["revision"],
                    "reason": f"{e2e_id} typed RPC pause cleanup",
                },
                dict,
            )
            assert resumed, "resume_trading returned no receipt"
            after = typed_rpc.query.call("get_trading_control", {}, dict)
            assert after.get("new_exposure_paused") is False, (
                f"trading remained paused after cleanup: {after}"
            )


def test_strategy_enable_disable_via_typed_rpc(
    dashboard_client, typed_rpc, e2e_id, require_capability,
):
    """Exercise both controls while returning the strategy to its initial state."""
    require_capability("strategy_control")
    login = dashboard_client.login()
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"
    strategy_name = _strategy_name(_strategy_rows(dashboard_client)[0])
    originally_disabled = _is_disabled(_strategy_row(dashboard_client, strategy_name))

    def command(action: str) -> None:
        row = _strategy_row(dashboard_client, strategy_name)
        receipt = typed_rpc.command.call(
            action,
            {
                "command_id": str(uuid4()),
                "strategy_name": strategy_name,
                "expected_control_revision": _control_revision(row),
            },
            dict,
        )
        assert receipt, f"{action} returned no receipt"

    changed = False
    try:
        if originally_disabled:
            command("enable_strategy")
            changed = True
            command("disable_strategy")
            changed = False
        else:
            command("disable_strategy")
            changed = True
            command("enable_strategy")
            changed = False
    finally:
        if changed:
            command("disable_strategy" if originally_disabled else "enable_strategy")

    final_row = _strategy_row(dashboard_client, strategy_name)
    assert _is_disabled(final_row) is originally_disabled, (
        f"strategy state was not restored: {final_row}"
    )


def test_direct_order_bypass_is_not_registered(typed_rpc):
    """Direct execution must remain outside the production command surface."""
    with pytest.raises(TypedRpcRemoteError) as exc_info:
        typed_rpc.command.call("place_order_simple", {}, dict)
    assert exc_info.value.code == "METHOD_NOT_ALLOWED"
