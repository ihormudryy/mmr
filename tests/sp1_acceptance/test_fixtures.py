"""Plan 6 Task 1: the shared composed stack, its served form and the broker race hooks."""
from __future__ import annotations

import pytest

from tests.sp1_fixtures import CONID
from trader.automation.risk_limits import PAPER_LIMITS
from trader.data.broker_state import BrokerRiskSnapshotError


def _publish(served, command_id="pol-1"):
    return served.call("ai_supervisor", "publish_ai_risk_policy",
                       {"command_id": command_id, "limits": PAPER_LIMITS.to_json(), "reason": "r"})


def test_served_stack_answers_signed_reads_and_writes(served):
    assert served.call("cli", "get_status", {})["ib_connected"] is True
    assert _publish(served)["outcome"]["revision"] == 1
    assert served.call("ai_supervisor", "get_experiment", {})["experiment"]["state"] == "ARMED"


def test_pending_cancel_that_never_lands_stays_pending_across_generations(composed_sim):
    composed_sim.add_order("og-x:stop", "og-x", "stop", "SELL", "STP", 3)
    composed_sim.pending_cancel("og-x:stop", lands=False)
    for _ in range(3):
        composed_sim.promote()
    assert composed_sim.orders["og-x:stop"].status == "PendingCancel"


def test_pending_cancel_that_lands_is_cancelled_one_promote_later(composed_sim):
    composed_sim.add_order("og-x:stop", "og-x", "stop", "SELL", "STP", 3)
    composed_sim.pending_cancel("og-x:stop")
    composed_sim.promote()
    assert composed_sim.orders["og-x:stop"].status == "PendingCancel"
    composed_sim.promote()
    assert composed_sim.orders["og-x:stop"].status == "Cancelled"


def test_fill_after_cancel_turns_the_cancel_into_a_fill(composed_sim):
    composed_sim.held[CONID] = 3.0
    composed_sim.add_order("og-x:stop", "og-x", "stop", "SELL", "STP", 3)
    composed_sim.fill_after_cancel("og-x:stop", quantity=3)
    composed_sim.promote()
    assert composed_sim.orders["og-x:stop"].status == "Submitted"      # no cancel was sent yet
    composed_sim.cancelOrder(composed_sim.ib_trades["og-x:stop"].order)
    composed_sim.promote()
    row = composed_sim.orders["og-x:stop"]
    assert (row.status, row.filled_quantity, composed_sim.held[CONID]) == ("Filled", 3.0, 0.0)


def test_hidden_child_is_absent_from_the_generation_until_revealed(composed_sim):
    store, db = composed_sim.trader.broker_state_store, composed_sim.trader.journal_db
    composed_sim.add_order("og-x:entry", "og-x", "entry", "BUY", "LMT", 3)
    composed_sim.hide("og-x:entry")
    composed_sim.promote()
    assert db.transaction(lambda conn: store.get_order_in_tx(conn, "og-x:entry")) is None
    composed_sim.reveal("og-x:entry")
    composed_sim.promote()
    assert db.transaction(lambda conn: store.get_order_in_tx(conn, "og-x:entry")) is not None


def test_reconnect_opens_a_new_generation_only_after_staging(composed_sim):
    store, db = composed_sim.trader.broker_state_store, composed_sim.trader.journal_db
    first = composed_sim.promote()
    composed_sim.reconnect()
    assert composed_sim.trader.broker_ingest.is_ready is False
    with pytest.raises(BrokerRiskSnapshotError) as exc:
        db.transaction(lambda conn: store.capture_risk_snapshot_in_tx(conn, "DU111111"))
    assert exc.value.code == "GENERATION_STAGING"
    second = composed_sim.promote()
    assert second > first and composed_sim.trader.broker_ingest.is_ready is True
    assert db.transaction(lambda conn: store.capture_risk_snapshot_in_tx(conn, "DU111111")).generation_id == second


def test_auto_fill_fills_a_marketable_entry_and_activates_its_bracket(composed_sim):
    from types import SimpleNamespace
    composed_sim.quote(CONID, 229.9, 230.0)
    composed_sim.auto_fill()
    parent = SimpleNamespace(permId=1, orderId=1, action="BUY", totalQuantity=3.0, ocaGroup="", ocaType=0,
                             parentId=0, orderType="LMT", lmtPrice=230.23, auxPrice=None, displaySize=0)
    composed_sim.add_order("og-y:entry", "og-y", "entry", "BUY", "LMT", 3, order=parent)
    stop = SimpleNamespace(permId=2, orderId=2, action="SELL", totalQuantity=3.0, ocaGroup="oca-og-y", ocaType=2,
                           parentId=1, orderType="STP", lmtPrice=None, auxPrice=225.4, displaySize=0)
    target = SimpleNamespace(permId=3, orderId=3, action="SELL", totalQuantity=3.0, ocaGroup="oca-og-y",
                             ocaType=2, parentId=1, orderType="LMT", lmtPrice=234.6, auxPrice=None, displaySize=0)
    composed_sim.add_order("og-y:stop", "og-y", "stop", "SELL", "STP", 3, status="PreSubmitted", order=stop)
    composed_sim.add_order("og-y:take_profit", "og-y", "take_profit", "SELL", "LMT", 3, order=target)
    composed_sim.promote()
    assert composed_sim.orders["og-y:entry"].status == "Filled" and composed_sim.held[CONID] == 3.0
    assert composed_sim.orders["og-y:take_profit"].status == "Submitted"        # not marketable: stays
    target.lmtPrice, target.displaySize = 229.9, 1
    composed_sim.script_target_fills([1])
    composed_sim.promote()
    assert composed_sim.orders["og-y:take_profit"].filled_quantity == 1.0
    assert composed_sim.orders["og-y:stop"].total_quantity == 2.0              # OCA type 2 shrank the sibling
    assert composed_sim.held[CONID] == 2.0


def test_restart_keeps_the_journal_and_the_broker(served):
    _publish(served)
    again = served.restart()
    try:
        assert again.call("ai_supervisor", "get_ai_risk_policy", {})["latest_published_revision"] == 1
        assert again.sim is served.sim
    finally:
        again.close()
