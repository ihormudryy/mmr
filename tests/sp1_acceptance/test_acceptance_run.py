"""Plan 6 Task 3: the acceptance scenario through the composed stack over signed typed RPC.

The real production registry, coordinator, ai_paper admission, protective saga,
safe close, session controller and scoreboard; only the broker is simulated.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.sp1_fixtures import CONID as AAPL, MSFT, et
from trader.acceptance.journal import RunJournal
from trader.acceptance.ports import RpcAcceptancePort
from trader.acceptance.scenario import AcceptanceScenario, AcceptanceSettings, SimulatedCrash

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "acc-20260717-0a1b2c"


def settings(**changes):
    base = AcceptanceSettings(
        run_id=RUN_ID, account_id="DU111111", conid_a=AAPL, conid_b=MSFT,
        strategy_bytes=(ROOT / "strategies" / "opening_range_breakout.py").read_bytes())
    return replace(base, **changes)


def port(served, *, operator=True):
    return RpcAcceptancePort(served.client("ai_research"), served.client("ai_supervisor"),
                             served.client("ai_supervisor", "query"),
                             operator_client=served.client("cli", "command") if operator else None,
                             now=served.now, sleep=served.advance_and_promote)


def scenario(served, tmp_path, **changes):
    chosen = settings(**changes)
    return AcceptanceScenario(port(served), chosen, RunJournal(tmp_path / "acceptance" / chosen.run_id))


def market(served):
    served.sim.auto_fill()
    served.sim.quote(AAPL, 229.9, 230.0)
    served.sim.quote(MSFT, 499.9, 500.0)


def drive_session_to_flat(served, *, start=(15, 30), max_steps=60):
    """Run the session controller from the AI cutoff through the flatten until it is FLAT (or give up)."""
    hour, minute = start
    state = None
    for step in range(max_steps):
        at = et(hour, minute) + __import__("datetime").timedelta(minutes=step)
        state = served.run_session(at)
        served.sim.promote()
        served.tick()
        if state is not None and state.state in ("FLAT", "FAILED_SAFE") and at >= et(15, 45):
            break
    served.stack.scoreboard.service.refresh()
    return state


def entries_placed(served):
    return [p for p in served.sim.placed if p[0].startswith("og-aip-") and p[1] == "LMT" and p[2] == "BUY"]


def test_the_spec_scenario_runs_to_s_protected(served, tmp_path):
    """Steps 1-7 and S's linked entry, checked on their own before the probe."""
    market(served)
    run = scenario(served, tmp_path).run_until("enter_s")
    assert all(r.passed for r in run), run
    assert [r.name for r in run] == ["preflight", "register", "publish", "enter_a", "enter_b", "partial_close_a",
                                     "close_a", "enter_s"]
    partial = next(r for r in run if r.name == "partial_close_a").evidence
    assert {leg["oca_type"] for leg in partial["legs"]} == {2}
    assert len({leg["oca_group"] for leg in partial["legs"]}) == 1
    assert {leg["remaining_quantity"] for leg in partial["legs"]} == {2.0}
    assert served.sim.held == {AAPL: 3.0, MSFT: 1.0}


def test_the_harness_seeds_nothing(served, tmp_path):
    market(served)
    scenario(served, tmp_path).run_until("enter_a")
    actions = {row[0]: row[1] for row in served.trader.journal_db.execute(
        "SELECT action, source FROM command_ledger", fetch="all")}       # source is the signing principal
    assert actions["register_ai_deployment"] == "ai_research"
    assert actions["publish_ai_risk_policy"] == "ai_supervisor"
    assert actions["submit_ai_paper_decision"] == "ai_supervisor"
    (principal,), = served.trader.journal_db.execute("SELECT principal FROM ai_deployments", fetch="all")
    assert principal == "ai_research"


def test_resume_after_a_crash_replays_and_sends_no_second_order(served, tmp_path):     # Review Focus 1
    market(served)
    crashing = scenario(served, tmp_path)
    crashing.crash_after("enter_a")
    with pytest.raises(SimulatedCrash):
        crashing.run()
    resumed = scenario(served, tmp_path)
    resumed.run_until("enter_s")
    assert len(entries_placed(served)) == 3                    # A, B and S, once each


def test_crash_before_the_receipt_is_persisted_replays_the_stored_body(served, tmp_path):      # ruling 15
    market(served)
    crashing = scenario(served, tmp_path)
    crashing.crash_before_receipt("enter_a")
    with pytest.raises(SimulatedCrash):
        crashing.run()
    served.advance(minutes=3)                                  # a recomputed expires_at would differ
    resumed = scenario(served, tmp_path)
    resumed._stop_after = "enter_s"
    results = resumed.resume()
    assert all(r.passed for r in results), results
    assert len(entries_placed(served)) == 3


def test_resume_does_not_run_the_flat_account_preflight(served, tmp_path):            # ruling 15
    market(served)
    crashing = scenario(served, tmp_path)
    crashing.crash_after("enter_a")
    with pytest.raises(SimulatedCrash):
        crashing.run()
    resumed = scenario(served, tmp_path)
    resumed._stop_after = "enter_b"
    codes = [r.code for r in resumed.resume()]
    assert "POSITIONS_OPEN" not in codes and all(code is None for code in codes)


def test_unfilled_entry_fails_the_step_without_a_retry(served, tmp_path):            # Review Focus 5
    served.sim.auto_fill(order_types=("MKT",))                 # limits never fill
    served.sim.quote(AAPL, 229.9, 230.0)
    served.sim.quote(MSFT, 499.9, 500.0)
    run = scenario(served, tmp_path).run()
    assert (run[-1].name, run[-1].code) == ("enter_a", "ENTRY_NOT_PROTECTED")
    assert len([p for p in served.sim.placed if p[1] == "LMT" and "aip" in p[0]]) == 1


def test_the_report_is_signed_and_lists_every_step(served, tmp_path):
    from trader.acceptance.report import AcceptanceReport, build_report
    from trader.research.signing import AttestationSigner
    market(served)
    run = scenario(served, tmp_path).run_until("enter_s")
    evidence = served.call("ai_supervisor", "get_broker_order_evidence", {})
    report = build_report(run_id=RUN_ID, steps=run, oca_shrink="NOT_RUN", evidence_source=evidence["source"],
                          order_evidence=evidence["orders"])
    signer = AttestationSigner.generate()
    report.sign(signer, key_source="ephemeral")
    path = report.write(tmp_path / "report.json")
    loaded = AcceptanceReport.load(path)
    loaded.verify(signer.public_key)
    assert [s["name"] for s in loaded.steps] == [r.name for r in run]
    assert loaded.evidence_source == "synthetic" and loaded.passed is False


def test_the_session_flatten_and_finish_prove_flat_on_broker_evidence(served, tmp_path):
    """Stopped before the probe: the 15:45 flatten closes B and S; finish passes all but oca_shrink."""
    market(served)
    run = scenario(served, tmp_path).run_until("enter_s")
    assert all(r.passed for r in run), run
    state = drive_session_to_flat(served)
    assert state.state == "FLAT", state
    assert {c: q for c, q in served.sim.held.items() if q} == {}
    end = {r.name: r for r in scenario(served, tmp_path).finish()}
    assert all(end[name].passed for name in ("session_flat", "equity_row_flat", "no_positions_or_orders",
                                               "round_trips", "no_incidents")), end
    assert (end["oca_shrink"].passed, end["oca_shrink"].code) == (False, "OCA_SHRINK_NOT_RUN")
    trips = end["round_trips"].evidence["trips"]
    assert [(t["conid"], t["closed_quantity"]) for t in trips if t["conid"] == AAPL] == [(AAPL, 3.0), (AAPL, 3.0)]
    assert served.call("cli", "verify_scoreboard", {})["ok"] is True


def test_the_spec_scenario_passes_end_to_end(served, tmp_path):
    market(served)
    served.sim.script_target_fills([1])                                      # S: one share fills, then nothing
    run = scenario(served, tmp_path).run()
    assert all(r.passed for r in run), run
    assert [r.name for r in run][-3:] == ["enter_s", "shrink_proof", "settle_s"]
    assert run[-2].evidence["oca_shrink"] == "PROVEN" and run[-1].evidence["settled_by"] == "close"
    assert served.principals_for("acceptance_mark_start", "acceptance_shrink_probe") == {"cli"}
    assert {c: q for c, q in served.sim.held.items() if q} == {MSFT: 1.0}   # B left for the session flatten
    state = drive_session_to_flat(served)
    assert state.state == "FLAT"
    end = scenario(served, tmp_path).finish()
    assert all(r.passed for r in end), end
    report = served.call("ai_supervisor", "get_scoreboard", {})
    assert report["sessions"][-1]["end_state"] == "FLAT" and report["sessions"][-1]["open_positions"] == 0
    assert served.call("cli", "verify_scoreboard", {})["ok"] is True
