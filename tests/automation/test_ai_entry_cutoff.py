"""Plan 3 Task 10 (R26): AI entries are cancelled at the entry cutoff, an ambiguous cancel is
reconciled from the broker, and a partial fill keeps protection sized to the fill.

Runs on Plan 1's composed stack (tests/sp1_fixtures.py) with ai_paper on:
the real command stack, session controller, liquidation service and protective saga over a
simulated broker.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tests.sp1_fixtures import ACCOUNT, CONID, Composed as _Composed, LoopThread as _LoopThread, et as _et
from trader.automation.ai_entry_cutoff import cutoff_child_id, is_ai_entry
from trader.automation.protective_order_saga import SagaState

CUTOFF = _et(15, 30)


def later(seconds):
    return CUTOFF + dt.timedelta(seconds=seconds)


class ComposedAi(_Composed):
    def __init__(self, tmp_path, loop_thread, clock, sim=None):
        super().__init__(tmp_path, loop_thread, clock, sim=sim, ai_paper=True)
        self.ai = self.stack.ai_paper

    def start_ai_session(self):
        self.sim.promote()
        return self.ai.policy.ensure_session(self.ai.decisions._broker.capture(ACCOUNT))

    def cutoff_state(self):
        return self.ai.policy.current().cutoff_cancel_state

    def working_ai_entry(self, decision_id, quantity=10.0):
        entity = f"og-aip-{decision_id}:entry"
        self.sim.add_order(entity, f"og-aip-{decision_id}", "entry", "BUY", "LMT", quantity)
        self.sim.promote()
        return entity

    def ai_bracket(self, decision_id, *, entry_qty, filled, stop, target, children=None):
        """A durable AI saga with its entry partly filled and its children still sized for the entry."""
        command_id = f"aip-{decision_id}"
        og = f"og-{command_id}"
        children = entry_qty if children is None else children
        self.saga._persist(SagaState(
            command_id=command_id, order_group_id=og, order_ref=f"mmr:{og}", state="PARTIALLY_FILLED",
            account_id=ACCOUNT, conid=CONID, side="BUY", requested_quantity=Decimal(str(entry_qty)),
            filled_quantity=Decimal(str(filled)), protection_quantity=Decimal(str(children)),
            protection_working=True, stop_working=True, target_working=True, entry_working=True, revision=3,
            plan_json={"legs": [{"role": "entry", "limit_price": "100.10"},
                                {"role": "stop", "stop_price": str(stop)},
                                {"role": "take_profit", "limit_price": str(target)}]}),
            self.clock[0], from_state=None)
        self.sim.held[CONID] = filled
        self.sim.add_order(f"{og}:entry", og, "entry", "BUY", "LMT", entry_qty)
        self.sim.set_status(f"{og}:entry", "Submitted", filled=filled)
        self.sim.add_order(f"{og}:stop", og, "stop", "SELL", "STP", children, status="PreSubmitted")
        self.sim.add_order(f"{og}:take_profit", og, "take_profit", "SELL", "LMT", children)
        self.sim.promote()
        return og

    def drive_cutoff_cancel(self, og):
        self.run_session(CUTOFF)
        assert f"{og}:entry" in self.sim.cancelled
        self.cancel_landed(f"{og}:entry")
        self.sim.promote()
        self.run_session(later(5))
        assert self.cutoff_state() == "DONE"

    def drive_until_done(self, root, max_rounds=10):
        for _ in range(max_rounds):
            receipt = self.liquidation.receipt_for(root)
            if receipt is not None and receipt.state in ("DONE", "CLOSED", "FLAT", "FAILED_SAFE", "REDUCE_FAILED"):
                return receipt
            for entity in list(self.sim.cancelled):
                if self.sim.orders[entity].status not in ("Cancelled", "Filled"):
                    self.cancel_landed(entity)
            self.sim.promote()
            self.tick()
        return self.liquidation.receipt_for(root)

    def reprotect_root(self):
        from trader.automation.session_controller import SessionController
        from tests.sp1_fixtures import FRIDAY
        return f"{SessionController.cancel_command_id(ACCOUNT, FRIDAY)}-aip-reprotect-{CONID}"


@pytest.fixture
def loop_thread():
    lt = _LoopThread()
    yield lt
    lt.stop()


@pytest.fixture
def composed_ai(tmp_path, loop_thread):
    stack = ComposedAi(tmp_path, loop_thread, [_et(11, 0)])
    stack.start_ai_session()
    yield stack
    stack.liquidation.worker.shutdown()


def test_ai_entry_is_cancelled_at_the_cutoff_not_five_minutes_later(composed_ai):          # rule 1
    composed_ai.working_ai_entry("dec-00000001")
    composed_ai.sim.add_order("og-entry-1:entry", "og-entry-1", "entry", "BUY", "LMT", 10)    # old-path entry
    composed_ai.sim.promote()
    composed_ai.run_session(CUTOFF)
    assert composed_ai.sim.cancelled == ["og-aip-dec-00000001:entry"]                         # old path waits
    assert composed_ai.cutoff_state() == "ISSUED"
    composed_ai.run_session(_et(15, 35))
    assert "og-entry-1:entry" in composed_ai.sim.cancelled


def test_the_hook_does_nothing_without_an_ai_session_today(tmp_path, loop_thread):
    stack = ComposedAi(tmp_path, loop_thread, [_et(11, 0)])
    try:
        stack.sim.add_order("og-aip-dec-00000001:entry", "og-aip-dec-00000001", "entry", "BUY", "LMT", 10)
        stack.sim.promote()
        stack.run_session(CUTOFF)
        assert stack.sim.cancelled == []                    # the 15:35 session cancel stays the backstop
    finally:
        stack.liquidation.worker.shutdown()


@pytest.mark.parametrize("outcome", ["raises", "pending_cancel_on_newer_generation",
                                     "still_working_on_newer_generation"])
def test_an_ambiguous_cancel_is_reconciled_from_the_broker(composed_ai, outcome, monkeypatch):    # rule 2
    entry = composed_ai.working_ai_entry("dec-00000001")
    if outcome == "raises":
        real = composed_ai.sim.cancelOrder

        def fail_once(order):
            monkeypatch.setattr(composed_ai.sim, "cancelOrder", real)
            raise ConnectionError("socket closed during cancel")
        monkeypatch.setattr(composed_ai.sim, "cancelOrder", fail_once)
    composed_ai.run_session(CUTOFF)
    assert composed_ai.cutoff_state() == ("AMBIGUOUS" if outcome == "raises" else "ISSUED")
    composed_ai.run_session(later(1))                          # same generation: nothing is judged or resent
    sent_before = composed_ai.sim.cancelled.count(entry)
    if outcome == "pending_cancel_on_newer_generation":
        composed_ai.sim.set_status(entry, "PendingCancel")
    composed_ai.sim.promote()
    composed_ai.run_session(later(5))
    assert composed_ai.cutoff_state() == "AMBIGUOUS"
    resent = composed_ai.sim.cancelled.count(entry) - sent_before
    assert resent == (0 if outcome == "pending_cancel_on_newer_generation" else 1)   # same cancel, same child
    composed_ai.cancel_landed(entry)
    composed_ai.sim.promote()
    composed_ai.run_session(later(10))
    assert composed_ai.cutoff_state() == "DONE"
    assert [p for p in composed_ai.sim.placed if p[1] == "MKT"] == []                # no reduce, no new entry


def test_cutoff_child_ids_are_deterministic_and_colon_free():
    child = cutoff_child_id("session-cancel-abc", "og-aip-dec-00000001:entry")
    assert child == "session-cancel-abc-aip-og-aip-dec-00000001-entry" and ":" not in child


def test_only_our_ai_entries_are_selected():
    from tests.automation.ai_paper_fixtures import order
    assert is_ai_entry(order(group="og-aip-dec-1", leg="entry"))
    assert not is_ai_entry(order(group="og-aip-dec-1", leg="stop"))
    assert not is_ai_entry(order(group="og-entry-1", leg="entry"))
    assert not is_ai_entry(order(group="og-aip-dec-1", leg="entry", is_external=True))


def test_a_partial_fill_keeps_protection_sized_to_the_fill(composed_ai):                # rule 3
    og = composed_ai.ai_bracket("dec-00000001", entry_qty=10, filled=4, stop=95.0, target=120.0)
    composed_ai.drive_cutoff_cancel(og)                                                  # entry rest cancelled
    receipt = composed_ai.drive_until_done(composed_ai.reprotect_root())
    assert (receipt.goal, receipt.state) == ("reprotect", "DONE")
    legs = [p for p in composed_ai.sim.placed if "-reprotect-" in p[0]]
    assert sorted((p[1], p[3], p[4]) for p in legs) == [("LMT", 4.0, 120.0), ("STP", 4.0, 95.0)]
    assert composed_ai.sim.held[CONID] == 4.0                                            # re-protected, not closed
    assert [p for p in composed_ai.sim.placed if p[1] == "MKT"] == []
    assert composed_ai.saga.resume("aip-dec-00000001").state == "PROTECTED"


def test_children_already_shrunk_by_the_broker_send_nothing(composed_ai):
    og = composed_ai.ai_bracket("dec-00000001", entry_qty=10, filled=4, stop=95.0, target=120.0, children=4)
    composed_ai.drive_cutoff_cancel(og)
    assert composed_ai.liquidation.receipt_for(composed_ai.reprotect_root()) is None
    assert composed_ai.sim.placed == []


def test_a_failed_reprotect_escalates_to_a_full_close_and_the_breaker(composed_ai):
    # A stop above the market price cannot protect the long: the re-protect gives up and closes.
    og = composed_ai.ai_bracket("dec-00000001", entry_qty=10, filled=4, stop=105.0, target=120.0)
    composed_ai.drive_cutoff_cancel(og)
    receipt = composed_ai.drive_until_done(composed_ai.reprotect_root(), max_rounds=3)
    assert receipt.goal == "zero" and receipt.escalated
    assert composed_ai.stack.circuit_breaker.store.get().state == "TRIPPED"
    assert [(p[1], p[2], p[3]) for p in composed_ai.sim.placed if p[1] == "MKT"] == [("MKT", "SELL", 4.0)]


def test_the_flatten_still_closes_the_rest_by_the_close(composed_ai):
    og = composed_ai.ai_bracket("dec-00000001", entry_qty=10, filled=4, stop=95.0, target=120.0, children=4)
    composed_ai.drive_cutoff_cancel(og)
    composed_ai.run_session(_et(15, 46))
    composed_ai.cancel_landed(f"{og}:stop", f"{og}:take_profit")
    composed_ai.sim.promote()
    composed_ai.run_session(_et(15, 47))                                                 # the flatten reduces
    assert [(p[1], p[2], p[3]) for p in composed_ai.sim.placed if p[1] == "MKT"] == [("MKT", "SELL", 4.0)]


def test_cutoff_state_survives_a_restart(composed_ai, tmp_path):
    entry = composed_ai.working_ai_entry("dec-00000001")
    composed_ai.run_session(CUTOFF)
    composed_ai.liquidation.worker.shutdown()
    restarted = ComposedAi(tmp_path, composed_ai.loop_thread, composed_ai.clock, sim=composed_ai.sim)
    try:
        assert restarted.cutoff_state() == "ISSUED"
        restarted.run_session(later(5))                    # no newer generation: no second cancel round
        assert (restarted.sim.cancelled.count(entry), restarted.cutoff_state()) == (1, "ISSUED")
    finally:
        restarted.liquidation.worker.shutdown()


def test_the_old_path_oversized_close_leaves_ai_legs_to_the_reprotect(composed_ai):
    from trader.automation.session_controller import SessionController
    from tests.test_safe_close_integration import FRIDAY
    composed_ai.ai_bracket("dec-00000001", entry_qty=10, filled=4, stop=95.0, target=120.0)
    composed_ai.run_session(_et(15, 35))                       # step 2b would close a conid with oversized legs
    protect_root = f"{SessionController.cancel_command_id(ACCOUNT, FRIDAY)}-protect-{CONID}"
    assert composed_ai.liquidation.receipt_for(protect_root) is None
    assert [p for p in composed_ai.sim.placed if p[1] == "MKT"] == []
