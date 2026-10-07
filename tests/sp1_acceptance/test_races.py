"""Plan 6 Task 4: race and restart variants of the acceptance scenario through the composed stack.

Each test runs the scenario to a named step, injects one fault with a BrokerSim hook or a
restart, then checks the fault-drill invariants that apply: one order identity, no unsafe
retry, a durable breaker, reconciliation continues, flat proven on broker evidence.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter

import pytest

from tests.sp1_acceptance.test_acceptance_run import (
    AAPL, MSFT, RUN_ID, drive_session_to_flat, market, scenario,
)
from tests.sp1_fixtures import et, served_stack


def assert_no_duplicate_order_refs(served):
    """Every placed order is one identity: a child id or a bracket leg is never sent twice."""
    keys = Counter((group, order_type, action) for group, order_type, action, *_ in served.sim.placed)
    assert [key for key, n in keys.items() if n > 1] == []


def assert_breaker(served, *, tripped):
    assert (served.stack.circuit_breaker.store.get().state == "TRIPPED") is tripped


def run_until(served, tmp_path, step):
    market(served)
    results = scenario(served, tmp_path).run_until(step)
    assert all(r.passed for r in results), results
    return results


def continue_step(served, tmp_path, step, **changes):
    resumed = scenario(served, tmp_path, **changes)
    resumed._stop_after = step
    return resumed.run_from(step)[-1]


def full_run(served, tmp_path):
    """The whole run phase: S proven and settled, B left open for the session flatten."""
    market(served)
    served.sim.script_target_fills([1])
    results = scenario(served, tmp_path).run()
    assert all(r.passed for r in results), results
    return results


def stop_of(served, name):
    return f"og-aip-{RUN_ID}-e-{name.lower()}:stop"


def end_code(results, name):
    return next(r.code for r in results if r.name == name)


def partial_close_to_reprotecting(served, quantity=1):
    """Send PARTIAL_CLOSE A and drive the broker until the re-protect stop is out but the close is not DONE."""
    decision = f"{RUN_ID}-pc-a"
    body = {"decision_id": decision, "deployment_digest": None, "decider": "acceptance_harness",
            "action": "PARTIAL_CLOSE", "conid": AAPL, "side": "SELL", "stop_price": None, "target_price": None,
            "quantity": quantity, "policy_revision": None, "evidence_digest": "sha256:" + "e" * 64,
            "expires_at": (served.now() + dt.timedelta(minutes=10)).isoformat()}
    assert served.call("ai_supervisor", "submit_ai_paper_decision", body)["state"] == "OUTCOME_UNKNOWN"
    for _ in range(10):
        if any("-reprotect-stop-" in p[0] for p in served.sim.placed):
            break
        served.advance_and_promote(2)
    receipt = served.stack.liquidation_service.receipt_for(f"aip-{decision}")
    assert receipt.state not in ("DONE", "CLOSED") and any("-reprotect-stop-" in p[0] for p in served.sim.placed)
    return f"aip-{decision}"


# ---------------------------------------------------------------------------
# broker races during the partial close
# ---------------------------------------------------------------------------

def test_a_stop_that_fills_instead_of_cancelling_ends_closed_with_no_reduce(served, tmp_path):
    run_until(served, tmp_path, "enter_b")
    served.sim.fill_after_cancel(stop_of(served, "A"), quantity=3)             # late fill: the stop sold all 3
    step = continue_step(served, tmp_path, "partial_close_a")
    assert (step.passed, step.code, step.evidence["liquidation_state"]) == (False, "PARTIAL_ENDED_CLOSED", "CLOSED")
    assert not [p for p in served.sim.placed if p[1] == "MKT"]                  # the close sent no reduce
    assert served.sim.held[AAPL] == 0.0
    assert_no_duplicate_order_refs(served)
    assert_breaker(served, tripped=False)


def test_pending_cancel_that_never_lands_fails_safe_without_a_second_order(served, tmp_path):
    run_until(served, tmp_path, "enter_b")
    served.sim.pending_cancel(stop_of(served, "A"), lands=False)
    step = continue_step(served, tmp_path, "partial_close_a", step_timeout=900.0)
    assert step.code == "CLOSE_FAILED_SAFE", step
    assert not [p for p in served.sim.placed if p[1] == "MKT"]                  # never sold next to a live stop
    assert_breaker(served, tripped=True)
    assert_no_duplicate_order_refs(served)


# ---------------------------------------------------------------------------
# generations, restarts and the flatten
# ---------------------------------------------------------------------------

def test_staging_generation_mid_flatten_waits_and_then_proves_flat(served, tmp_path):
    full_run(served, tmp_path)
    served.sim.reconnect()
    state = drive_session_to_flat(served)
    assert state.state == "FLAT"
    end = scenario(served, tmp_path).finish()
    assert all(r.passed for r in end), end
    assert_no_duplicate_order_refs(served)


def test_restart_between_reprotecting_and_done_sends_no_duplicate_legs(served, tmp_path):
    run_until(served, tmp_path, "enter_b")
    root = partial_close_to_reprotecting(served)
    again = served.restart()
    try:
        for _ in range(4):
            again.tick()
            again.sim.promote()
        again.tick()
        assert len([p for p in again.sim.placed if "-reprotect-stop" in p[0]]) == 1
        assert len([p for p in again.sim.placed if "-reprotect-target" in p[0]]) == 1
        assert again.stack.liquidation_service.receipt_for(root).state == "DONE"
        assert_no_duplicate_order_refs(again)
        assert_breaker(again, tripped=False)
    finally:
        again.close()


def test_invisible_child_at_the_flatten_deadline_fails_the_run(served, tmp_path):       # Review Focus 3
    full_run(served, tmp_path)
    served.sim.hide_next_child()                                                    # the flatten's first reduce
    state = drive_session_to_flat(served, max_steps=30)                             # 15:30 .. 15:59
    assert state.state == "INCIDENT"                                                # the flatten root FAILED_SAFE
    end = scenario(served, tmp_path).finish()
    assert not all(r.passed for r in end) and end_code(end, "session_flat") == "FAILED_SAFE"
    assert end_code(end, "no_positions_or_orders") == "NOT_FLAT"
    assert_no_duplicate_order_refs(served)


# ---------------------------------------------------------------------------
# the kill line, pause and outage (Plans 3-5 behaviour through the scenario's stack)
# ---------------------------------------------------------------------------

@pytest.fixture
def served_with_kill(tmp_path, loop_thread, monkeypatch):
    """The same stack with a 20 % kill line and Telegram on through a fake transport."""
    from tests.scoreboard.telegram_fakes import FakePost, token_file
    telegram = {"enabled": True, "chat_id": 5, "token_secret_file": str(token_file(tmp_path))}
    stack = served_stack(tmp_path, loop_thread, monkeypatch, kill_pct=20.0, telegram=telegram, acceptance_probe=True)
    stack.stack.scoreboard.sender._post = FakePost()
    yield stack
    stack.close()


def drive_kill_to_flat(served, max_steps=30):
    for _ in range(max_steps):
        served.advance_and_promote(5)
        record = served.stack.experiments.store.latest()
        if record.kill_flat_state == "FLAT":
            return record
    return served.stack.experiments.store.latest()


def test_kill_during_reprotecting_flattens_and_writes_a_killed_row(served_with_kill, tmp_path):
    served = served_with_kill
    run_until(served, tmp_path, "enter_b")
    partial_close_to_reprotecting(served)
    served.sim.net_liquidation = 79_000.0                                        # 21 % below the 100k start
    served.sim.promote()
    served.tick()
    record = drive_kill_to_flat(served)
    assert (record.state, record.kill_flat_state) == ("KILLED", "FLAT")
    assert {c: q for c, q in served.sim.held.items() if q} == {}
    served.stack.scoreboard.service.refresh()
    sessions = served.call("cli", "get_scoreboard", {})["sessions"]
    assert sessions[-1]["end_state"] == "KILLED"
    outbox = [row[0] for row in served.trader.journal_db.execute(
        "SELECT event_id FROM telegram_outbox ORDER BY created_at", fetch="all")]
    assert f"kill_started:{record.experiment_id}:1" in outbox
    assert_no_duplicate_order_refs(served)


def test_close_works_while_paused(served, tmp_path):
    run_until(served, tmp_path, "enter_b")
    experiment_id = served.experiment_id
    paused = served.call("cli", "pause_experiment", {"command_id": "pause-1", "experiment_id": experiment_id,
                                                     "reason": "operator pause"})
    assert paused["outcome"]["state"] == "PAUSED", paused
    step = continue_step(served, tmp_path, "close_a")
    assert step.passed and step.evidence["liquidation_state"] == "CLOSED", step
    assert served.sim.held[AAPL] == 0.0 and served.sim.held[MSFT] == 1.0


def test_broker_outage_pauses_and_never_flattens(served, tmp_path):            # Plan 4 K23
    run_until(served, tmp_path, "enter_b")
    placed = len(served.sim.placed)
    served.sim.stage_generation()                                             # every capture: GENERATION_STAGING
    for _ in range(32):
        served.advance(10)
        served.tick()
    record = served.stack.experiments.store.latest()
    assert (record.state, record.pause_cause) == ("PAUSED", "BROKER_DATA_OUTAGE")
    assert len(served.sim.placed) == placed                                   # no flatten, no order at all
    assert served.sim.held == {AAPL: 3.0, MSFT: 1.0}
