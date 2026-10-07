"""Plan 3 Task 8: ai_paper CLOSE / PARTIAL_CLOSE through the broker-proven safe close (Plan 1)."""
from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.automation.ai_paper_fixtures import ACCOUNT, CONID, NOW, pos, snapshot
from tests.automation.ai_paper_world import World
from tests.automation.discretionary_world import discretionary_world
from tests.test_liquidation_service import _Protection
from trader.automation.ai_paper_experiment import ExperimentView
from trader.automation.reduction_close import start_broker_proven_close
from trader.trading.command_coordinator import CLOSE_RESOLVED_ACTIONS, OutcomeReconciler
from trader.trading.exit_owner import ExitInProgress

DEADLINE = NOW + dt.timedelta(minutes=5)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path, real_liquidation=True)
    w.owned(CONID, 300.0)
    return w


def close_body(world, **changes):
    body = world.body(action="CLOSE", side="SELL", deployment_digest=None, policy_revision=None,
                      stop_price=None, quantity=None)
    body.update(changes)
    return body


def partial_body(world, quantity=100, **changes):
    return close_body(world, action="PARTIAL_CLOSE", quantity=quantity, **changes)


def test_close_never_builds_a_bracket(world, monkeypatch):
    def fail(*a, **k):
        pytest.fail("close went through build_bracket_plan")
    monkeypatch.setattr("trader.automation.protective_order_saga.build_bracket_plan", fail)
    receipt = world.submit(close_body(world))
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")
    root = receipt.outcome["close_root_id"]
    assert world.liquidation.receipt_for(root).scope == "conid"
    assert world.decisions.row("dec-00000001").close_root_id == root
    assert world.scheduled == [receipt.command_id] and world.dispatch.plans == []


def test_close_works_while_paused_and_after_a_daily_loss_breach(world):
    world.experiments.view = ExperimentView("exp1", "PAUSED")
    world.start_session()
    world.policy.latch("DAILY_LOSS", "x")
    world.broker.set(daily_pnl=-9_000.0)
    world.controls.set(ACCOUNT, True, None, "pause-1", "test", NOW)               # trading pause too (R32)
    assert world.submit(close_body(world)).error_code == "CLOSE_PENDING"


@pytest.mark.parametrize("kind", ["close", "partial"])
def test_a_reduction_of_a_denylisted_symbol_still_passes(world, kind):        # owner answer 4
    world.filter_file.write(denylist=["AAPL"], deny_exchanges=["NASDAQ"])
    world.filter_loads = 0
    body = close_body(world) if kind == "close" else partial_body(world)
    assert world.submit(body).error_code == "CLOSE_PENDING"
    assert world.filter_loads == 0                                               # a reduction never reads the filter


def test_close_without_a_broker_position_is_refused(world):
    world.broker.set(positions=())
    assert world.submit(close_body(world)).error_code == "NOT_A_REDUCTION"


@pytest.mark.parametrize("quantity", [301, 1_000])
def test_partial_larger_than_the_position_is_refused(world, quantity):
    assert world.submit(partial_body(world, quantity)).error_code == "NOT_A_REDUCTION"


def test_partial_equal_to_the_position_is_a_full_close(world):
    receipt = world.submit(partial_body(world, 300))
    assert world.liquidation.receipt_for(receipt.outcome["close_root_id"]).goal == "zero"


@pytest.mark.parametrize("prices", [{"stop_price": 97.5}, {"target_price": 120.0}])
def test_partial_close_with_prices_is_refused(world, prices):          # spec 6.4: no protection edits
    receipt = world.submit(partial_body(world, 100, **prices))
    assert (receipt.state, receipt.error_code) == ("REJECTED", "DECISION_INVALID")
    assert world.liquidation_runs() == set()


def test_partial_close_reprotects_at_the_existing_stop_and_target(world):
    world.liquidation.attach_protection(_Protection(stop_price=95.0, target_price=110.0))
    receipt = world.submit(partial_body(world, 100))
    run = world.liquidation.receipt_for(receipt.outcome["close_root_id"])
    assert (run.goal, run.goal_quantity, run.stop_price, run.target_price) == ("partial", 100.0, 95.0, 110.0)


def test_a_position_the_experiment_never_entered_is_not_closed(tmp_path):      # spec 6.4
    w = World(tmp_path, real_liquidation=True)
    w.held(CONID, 300.0)                                     # e.g. a manual proposal: no ENTER of this experiment
    assert w.submit(close_body(w)).error_code == "POSITION_NOT_OWNED"
    assert w.submit(partial_body(w, decision_id="dec-00000002")).error_code == "POSITION_NOT_OWNED"
    assert w.liquidation_runs() == set()


def test_an_entry_of_another_experiment_does_not_own_the_position(world):
    world.experiments.view = ExperimentView("exp2", "ARMED")
    assert world.submit(close_body(world)).error_code == "POSITION_NOT_OWNED"


def test_an_entry_that_never_filled_does_not_own_the_position(tmp_path):
    w = World(tmp_path, real_liquidation=True)
    assert w.submit(decision_id="dec-owned-01").state == "SUBMITTED"
    w.broker.show_working_entry("og-aip-dec-owned-01", quantity=499, filled=0.0)
    w.held(CONID, 300.0)
    assert w.submit(close_body(w)).error_code == "POSITION_NOT_OWNED"


def test_the_decision_row_records_its_experiment(world):
    assert world.decisions.row("dec-owned-00").experiment_id == "exp1"
    world.submit(close_body(world))
    assert world.decisions.row("dec-00000001").experiment_id == "exp1"


def test_buy_side_close_of_a_long_is_not_a_reduction(world):
    assert world.submit(close_body(world, side="BUY")).error_code == "NOT_A_REDUCTION"


def test_close_joins_the_kill_flatten_while_killed(world):
    world.liquidation.start(ACCOUNT, "kill-root", DEADLINE)                     # account owner, as Plan 4's kill does
    world.experiments.view = ExperimentView("exp1", "KILLED")
    receipt = world.submit(partial_body(world, 100))
    assert receipt.outcome["close_root_id"] == "kill-root"
    assert world.liquidation_runs() == {"kill-root"}                            # no new run, no new work


def test_killed_without_a_flatten_yet_is_refused_retryable(world):
    world.experiments.view = ExperimentView("exp1", "KILLED")
    receipt = world.submit(close_body(world))
    assert (receipt.error_code, receipt.retryable) == ("KILL_FLATTEN_PENDING", True)
    assert world.liquidation_runs() == set()


@pytest.mark.parametrize("state,code", [("STOPPED", "EXPERIMENT_STOPPED"), (None, "NO_EXPERIMENT")])
def test_close_is_refused_after_stopped_or_without_an_experiment(world, state, code):
    world.experiments.view = None if state is None else ExperimentView("exp1", state)
    assert world.submit(close_body(world)).error_code == code


def test_close_joins_a_time_exit_root(world):                                    # spec 5.1 / R14
    time_exit = world.liquidation.start(ACCOUNT, "time-exit-1", DEADLINE, scope="conid", conid=CONID)
    assert world.submit(close_body(world)).outcome["close_root_id"] == time_exit.cause_command_id


def test_partial_during_another_owners_close_is_exit_in_progress(world):
    world.liquidation.start(ACCOUNT, "time-exit-1", DEADLINE, scope="conid", conid=CONID)
    assert world.submit(partial_body(world)).error_code == "EXIT_IN_PROGRESS"


def test_new_decision_on_a_conid_with_a_pending_close_is_refused(world):
    world.submit(close_body(world))
    assert world.submit(decision_id="dec-00000002").error_code == "OUTCOME_UNKNOWN_PENDING"
    assert world.submit(partial_body(world, decision_id="dec-00000003")).error_code == "OUTCOME_UNKNOWN_PENDING"


def test_reductions_carry_no_attribution(world):                                # R16
    assert world.submit(close_body(world, policy_revision=1)).error_code == "DECISION_INVALID"


def _reconciler(world):
    alerts = []
    return OutcomeReconciler(
        journal=world.journal, ledger=world.ledger, orders=SimpleNamespace(), strategy=SimpleNamespace(),
        alerts=SimpleNamespace(raise_alert=lambda *a: alerts.append(a)), now=world.clock,
        closes=world.liquidation), alerts


def _finish_root(world, root, state):
    store = world.liquidation._store
    run = store.receipt(root)
    store.transaction(lambda conn: store.update_run_in_tx(conn, replace(run, state=state), NOW))
    world.exit_owners.release(root, NOW)


def test_reconciler_resolves_the_close_command_from_its_root(world):
    receipt = world.submit(close_body(world))
    _finish_root(world, receipt.outcome["close_root_id"], "CLOSED")
    reconciler, alerts = _reconciler(world)
    assert reconciler.reconcile_once(receipt.command_id, NOW).resolved
    assert world.ledger.get(receipt.command_id).state == "RESOLVED" and alerts == []
    assert "submit_ai_paper_decision" in CLOSE_RESOLVED_ACTIONS


def test_entry_after_the_close_is_resolved_is_not_blocked(world):
    receipt = world.submit(close_body(world))
    _finish_root(world, receipt.outcome["close_root_id"], "CLOSED")
    _reconciler(world)[0].reconcile_once(receipt.command_id, NOW)
    world.broker.set(positions=())
    # The owning ENTER's saga still counts its fill in flight (no exit leg filled), so ask for one share.
    assert world.submit(decision_id="dec-00000002", quantity=1).state == "SUBMITTED"


# --- the shared function on its own (the one-strategy SELL keeps its tests in
# tests/automation/test_automated_command_boundary.py) ----------------------

class _Liquidation:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def start(self, account_id, command_id, deadline, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(cause_command_id="root-1", state="REQUESTED", generation_id=3, detail="")


def _close(liquidation, *, held=300.0, side="SELL", quantity=None, **kw):
    broker = SimpleNamespace(capture=lambda account: snapshot(positions=(pos(CONID, held),) if held else ()))
    return start_broker_proven_close(liquidation=liquidation, broker=broker, account_id=ACCOUNT,
                                     command_id="c-1", conid=CONID, side=side, quantity=quantity,
                                     deadline=DEADLINE, **kw)


def test_shared_close_maps_every_outcome():
    assert _close(_Liquidation()).error_code == "CLOSE_PENDING"
    assert _close(_Liquidation(ExitInProgress("other"))).outcome == {"close_root_id": "other"}
    assert _close(_Liquidation(RuntimeError("socket"))).error_code == "DISPATCH_AMBIGUOUS"
    assert _close(_Liquidation(), held=0.0).error_code == "NOT_A_REDUCTION"


def test_shared_close_passes_prices_only_for_a_partial():
    liquidation = _Liquidation()
    _close(liquidation, quantity=100.0, stop_price=97.5)
    _close(liquidation, quantity=300.0, stop_price=97.5)
    assert liquidation.calls[0]["quantity"] == 100.0 and liquidation.calls[0]["stop_price"] == 97.5
    assert liquidation.calls[1]["quantity"] is None and "stop_price" not in liquidation.calls[1]


# --- SP2 Plan 3 Task 11: spec 6.4 behaviour that already exists, pinned -----------------------------

def test_close_after_the_entry_cutoff_is_admitted(world):              # spec 6.4 / 5.2: after the cutoff
    world.clock.advance(hours=4, minutes=40)                             # 15:40 ET: after the 15:30 entry cutoff
    assert world.submit(close_body(world)).error_code == "CLOSE_PENDING"


def test_close_joins_the_session_flatten_while_armed(world):            # flatten precedence
    world.liquidation.start(ACCOUNT, "session-flatten-1", DEADLINE)    # account owner, as the session flatten
    assert world.submit(close_body(world)).outcome["close_root_id"] == "session-flatten-1"
    assert world.liquidation_runs() == {"session-flatten-1"}


def test_a_reduction_ignores_the_discretionary_scope_rule(tmp_path):    # the rule is for entries only
    w = discretionary_world(tmp_path, real_liquidation=True)
    w.owned(CONID, 300.0)
    w.contracts.fail_with("must not be called")
    w.quotes.set(bid=1.0, ask=1.01)
    assert w.submit(close_body(w)).error_code == "CLOSE_PENDING"
    assert w.contracts.calls == 0
