"""Opt-in paper buy → sell round-trip (requires market data + LIVE_ORDERS)."""
from __future__ import annotations

import time
from uuid import uuid4

import pytest

from trader.messaging.typed_rpc import TypedRpcRemoteError


def _require_live_orders(paper_stack) -> None:
    if not paper_stack.live_orders:
        pytest.skip("set MMR_PAPER_E2E_LIVE_ORDERS=1 to place paper orders")


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


def _position_qty(typed_rpc, conid: int) -> float:
    payload = typed_rpc.query.call("get_positions", {}, dict)
    rows = payload.get("positions") or []
    total = 0.0
    for row in rows:
        instrument_id = row.get("instrument_id", row.get("conid"))
        if instrument_id is None:
            continue
        if int(instrument_id) == conid:
            total += float(row.get("position") or 0.0)
    return total


def _wait_for_qty(
    typed_rpc, conid: int, expected: float, *, timeout_s: float = 90.0
) -> float:
    deadline = time.monotonic() + timeout_s
    last = _position_qty(typed_rpc, conid)
    while time.monotonic() < deadline:
        last = _position_qty(typed_rpc, conid)
        if abs(last - expected) < 1e-9:
            return last
        time.sleep(1.0)
    raise AssertionError(
        f"position for conId {conid} did not reach {expected}: last={last}"
    )


def _assert_receipt_ok(receipt: dict, *, action: str) -> dict:
    assert isinstance(receipt, dict) and receipt, f"{action} returned no receipt"
    if receipt.get("state") == "REJECTED":
        code = receipt.get("error_code") or "REJECTED"
        message = (receipt.get("outcome") or {}).get("message") or ""
        if code == "QUOTE_UNAVAILABLE":
            pytest.skip(
                f"{action} needs an IB executable quote (realtime or delayed): "
                f"{message or code}"
            )
        if code in {"TRADING_PAUSED", "RECONCILIATION_INCOMPLETE", "TRADER_NOT_READY"}:
            pytest.skip(f"{action} blocked by control/readiness: {code} {message}")
        raise AssertionError(f"{action} rejected: {code} {receipt}")
    return receipt


def _create_and_approve(
    typed_rpc,
    *,
    conid: int,
    action: str,
    quantity: float,
    e2e_id: str,
    label: str,
) -> int:
    created = typed_rpc.command.call(
        "create_proposal",
        {
            "command_id": str(uuid4()),
            "conid": conid,
            "action": action,
            "quantity": quantity,
            "group": e2e_id,
            "reasoning": f"{e2e_id} {label}",
        },
        dict,
    )
    _assert_receipt_ok(created, action=f"create_proposal {action}")
    proposal_id = (created.get("outcome") or {}).get("id")
    assert isinstance(proposal_id, int), f"create has no proposal id: {created}"

    proposal = typed_rpc.query.call("get_proposal", {"proposal_id": proposal_id}, dict)
    approved = typed_rpc.command.call(
        "approve_proposal",
        {
            "command_id": str(uuid4()),
            "proposal_id": proposal_id,
            "expected_version": proposal["revision"],
        },
        dict,
    )
    _assert_receipt_ok(approved, action=f"approve_proposal {action}")
    return proposal_id


@pytest.mark.paper_e2e_live_orders
@pytest.mark.timeout(300)
def test_buy_then_sell_round_trip_restores_baseline(
    paper_stack, typed_rpc, e2e_id, require_capability,
):
    """Approve a 1-share BUY, wait for the fill, then SELL back to baseline."""
    _require_live_orders(paper_stack)
    require_capability("proposals")
    require_capability("approval")

    control = typed_rpc.query.call("get_trading_control", {}, dict)
    if control.get("new_exposure_paused"):
        pytest.skip("trading is paused; refusing to place live paper orders")
    readiness = typed_rpc.query.call("get_status", {}, dict).get(
        "semantic_readiness", {}
    )
    if readiness.get("ready") is not True:
        pytest.skip(
            "semantic readiness blocks live orders: "
            f"{readiness.get('failed', [])}"
        )

    conid = _aapl_conid(typed_rpc)
    baseline = _position_qty(typed_rpc, conid)
    opened = False
    try:
        _create_and_approve(
            typed_rpc,
            conid=conid,
            action="BUY",
            quantity=1.0,
            e2e_id=e2e_id,
            label="live buy leg",
        )
        _wait_for_qty(typed_rpc, conid, baseline + 1.0)
        # Teardown closes by this delta, not the absolute book size.
        paper_stack.register_opened_position(conid, 1.0)
        opened = True

        _create_and_approve(
            typed_rpc,
            conid=conid,
            action="SELL",
            quantity=1.0,
            e2e_id=e2e_id,
            label="live sell leg",
        )
        _wait_for_qty(typed_rpc, conid, baseline)
        paper_stack.opened_positions.pop(conid, None)
        paper_stack.opened_conids.discard(conid)
        opened = False
    except TypedRpcRemoteError as exc:
        if exc.code == "QUOTE_UNAVAILABLE":
            pytest.skip(f"IB quote unavailable for round-trip: {exc.message}")
        raise
    finally:
        if opened:
            # Leave registration for session teardown; do not widen cleanup.
            pass

    assert abs(_position_qty(typed_rpc, conid) - baseline) < 1e-9
