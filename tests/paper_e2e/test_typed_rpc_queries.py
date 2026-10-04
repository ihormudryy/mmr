"""Live paper-stack coverage for the public typed query surface."""
from __future__ import annotations

import pytest


def test_typed_query_matrix(typed_rpc, require_capability):
    """Call each safe production query through the public query facade."""
    status = typed_rpc.query.call("get_status", {}, dict)
    assert isinstance(status, dict) and status, "get_status returned no data"

    positions = typed_rpc.query.call("get_positions", {}, dict)
    assert isinstance(positions, dict), "get_positions must return an object"

    orders = typed_rpc.query.call("get_open_orders", {}, dict)
    assert isinstance(orders, dict), "get_open_orders must return an object"

    universes = typed_rpc.query.call("list_universes", {}, dict)
    assert isinstance(universes, dict), "list_universes must return an object"
    assert isinstance(universes.get("universes"), list), (
        f"list_universes has no universes list: {universes}"
    )

    instruments = typed_rpc.query.call(
        "discover_instrument",
        {"symbol": "AAPL", "exchange": "", "currency": "", "sec_type": "STK"},
        dict,
    )
    assert isinstance(instruments, dict), "discover_instrument must return an object"
    if not instruments.get("instruments"):
        pytest.skip("IB resolve AAPL unavailable")

    require_capability("proposals")
    proposals = typed_rpc.query.call("list_proposals", {}, dict)
    assert isinstance(proposals, dict), "list_proposals must return an object"
    assert isinstance(proposals.get("proposals"), list), (
        f"list_proposals has no proposals list: {proposals}"
    )


def test_trading_control_query_when_available(typed_rpc, require_capability):
    """Control state is only required when the stack advertises it."""
    require_capability("trading_control")
    control = typed_rpc.query.call("get_trading_control", {}, dict)
    assert isinstance(control, dict) and control, "get_trading_control returned no data"
    assert isinstance(control.get("revision"), int), (
        f"trading control has no revision: {control}"
    )
    assert isinstance(control.get("new_exposure_paused"), bool), (
        f"trading control has no pause state: {control}"
    )


def test_paper_automation_query_when_available(typed_rpc, require_capability):
    """Automation status is optional, but registered stacks return a payload."""
    require_capability("paper_automation")
    status = typed_rpc.query.call("get_paper_automation_status", {}, dict)
    assert isinstance(status, dict) and status, (
        "get_paper_automation_status returned no data"
    )
