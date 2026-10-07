"""Plan 6 Task 7: the live OCA shrink proof (ruling 13), its probe command and its durable mark (ruling 23).

Synthetic results prove the logic only; they never count as the live proof.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.sp1_acceptance.fakes import AAPL, NOW, FakePort, stop, target
from tests.sp1_acceptance.test_acceptance_run import RUN_ID, market, scenario
from trader.acceptance.report import build_report
from trader.acceptance.scenario import AcceptanceScenario, build_entry_s
from trader.acceptance.shrink_proof import run_shrink_proof
from trader.messaging.typed_rpc import TypedRpcRemoteError

LEGS = {"take_profit": "og-s:take_profit", "stop": "og-s:stop"}


# ---------------------------------------------------------------------------
# the decision table, one scripted reading after another
# ---------------------------------------------------------------------------

class ReadingPort:
    """Scripted evidence readings (the last one repeats) and the position read with each."""

    def __init__(self, readings):
        self.readings = list(readings)
        self.clock = NOW
        self.methods = []

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += dt.timedelta(seconds=seconds)

    def evidence(self, conid=None):
        self.methods.append("get_broker_order_evidence")
        self.current = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        return self.current["evidence"]

    def supervisor(self, method, body):
        self.methods.append(method)
        assert method == "get_positions"
        return {"positions": [{"instrument_id": AAPL, "position": self.current["position"]}]}


def reading(target_events=(), stop_events=(), *, position=3.0, generation=7, gapless=True, oca=2,
            target_status="Submitted", stop_status="Submitted", capture_error=None):
    if capture_error:
        return {"evidence": {"capture_error": capture_error}, "position": position}

    def events(rows):
        return [{"cursor": c, "status": s, "filled_quantity": f, "remaining_quantity": r, "total_quantity": t,
                 "oca_type": oca} for c, s, f, r, t in rows]
    legs = [{"order_entity_id": LEGS["take_profit"], "leg": "take_profit", "status": target_status,
             "oca_group": "oca-s", "oca_type": oca, "status_events": events(target_events)},
            {"order_entity_id": LEGS["stop"], "leg": "stop", "status": stop_status, "oca_group": "oca-s",
             "oca_type": oca, "status_events": events(stop_events)}]
    return {"evidence": {"generation_id": generation, "events_gapless": gapless, "orders": legs},
            "position": position}


def prove(readings, settings, journal):
    port = ReadingPort(readings)
    return run_shrink_proof(port, settings, journal, legs=LEGS, conid=AAPL), port


PARTIAL = (1, "Submitted", 1.0, 2.0, 3.0)
SHRUNK = (2, "Submitted", 0.0, 2.0, 2.0)
UNSHRUNK = (2, "Submitted", 0.0, 3.0, 3.0)


def test_a_partial_fill_with_a_shrunk_sibling_is_proven(settings, journal):
    result, _ = prove([reading([PARTIAL], [SHRUNK], position=2.0)], settings, journal)
    assert (result.passed, result.evidence["oca_shrink"]) == (True, "PROVEN")
    assert result.evidence["pair"]["target_event"]["filled_quantity"] == 1.0
    assert [e["kind"] for e in journal.entries()] == ["reading"]                  # every distinct reading journaled


def test_a_partial_fill_with_an_unchanged_sibling_fails_and_stops(settings, journal):
    result, port = prove([reading([PARTIAL], [UNSHRUNK], position=2.0)], settings, journal)
    assert (result.passed, result.code, result.evidence["oca_shrink"]) == (False, "OCA_SIBLING_NOT_SHRUNK", "FAILED")
    assert "submit_ai_paper_decision" not in port.methods
    assert port.clock - NOW < dt.timedelta(seconds=10)                            # stops polling at once


def test_a_whole_fill_in_one_event_is_unproven_never_a_pass(settings, journal):
    whole = (1, "Filled", 3.0, 0.0, 3.0)
    result, _ = prove([reading([whole], [], position=0.0, target_status="Filled", stop_status="Cancelled")],
                      settings, journal)
    assert (result.passed, result.code, result.evidence["oca_shrink"]) == (False, "OCA_SHRINK_UNPROVEN", "UNPROVEN")


def test_no_fill_in_60_seconds_is_unproven_and_sends_no_retry(settings, journal):
    result, port = prove([reading()], settings, journal)
    assert (result.code, result.evidence["oca_shrink"]) == ("OCA_SHRINK_UNPROVEN", "UNPROVEN")
    assert port.clock - NOW >= dt.timedelta(seconds=60)
    assert set(port.methods) == {"get_broker_order_evidence", "get_positions"}


def test_oca_type_other_than_2_fails(settings, journal):
    result, _ = prove([reading([PARTIAL], [SHRUNK], position=2.0, oca=1)], settings, journal)
    assert (result.code, result.evidence["oca_shrink"]) == ("OCA_TYPE_NOT_2", "FAILED")


def test_a_staging_generation_reading_is_ignored_not_counted(settings, journal):
    result, _ = prove([reading(capture_error="GENERATION_STAGING"), reading([PARTIAL], [SHRUNK], position=2.0)],
                      settings, journal)
    assert result.evidence["oca_shrink"] == "PROVEN" and result.evidence["readings"] == 1


def test_a_cursor_gap_makes_the_transition_unobservable(settings, journal):
    result, _ = prove([reading([PARTIAL], [SHRUNK], position=2.0, gapless=False)], settings, journal)
    assert result.evidence["oca_shrink"] == "UNPROVEN"


def test_a_fill_and_a_shrink_in_two_generations_are_never_paired(settings, journal):
    readings = [reading([PARTIAL], [], position=2.0, generation=7),
                reading([], [(1, "Submitted", 0.0, 2.0, 2.0)], position=2.0, generation=8)]
    result, _ = prove(readings, settings, journal)
    assert result.evidence["oca_shrink"] == "UNPROVEN"


def test_slices_after_the_pair_still_prove_against_the_latest_fill(settings, journal):
    events = [PARTIAL, (3, "Filled", 3.0, 0.0, 3.0)]
    result, _ = prove([reading(events, [SHRUNK, (4, "Cancelled", 0.0, 0.0, 2.0)], position=0.0,
                               target_status="Filled", stop_status="Cancelled")], settings, journal)
    assert result.evidence["oca_shrink"] == "PROVEN"


def test_a_synthetic_proven_never_marks_the_real_session_accepted():
    assert build_report(oca_shrink="PROVEN", evidence_source="synthetic").passed is False


# ---------------------------------------------------------------------------
# the S steps in the scenario (scripted port)
# ---------------------------------------------------------------------------

def test_s_entry_body_has_stop_and_target_on_the_right_sides(settings):
    body = build_entry_s(settings, ask=200.0)
    assert (body["decision_id"], body["action"], body["quantity"], body["stop_price"], body["target_price"]) == (
        f"{settings.run_id}-e-s", "ENTER", 3, 196.0, 204.0)


def test_the_probe_is_not_called_until_both_oca_legs_are_working(fake_port, settings, journal):
    fake_port.evidence_script_for_enter_s(stop=True, target=False)               # stop-only bracket
    r = AcceptanceScenario(fake_port, settings, journal).run()
    assert r[-1].code == "S_NOT_PROTECTED_WITH_TARGET"
    assert "acceptance_mark_start" not in fake_port.methods() and "acceptance_shrink_probe" not in fake_port.methods()


def test_unproven_whole_fill_flat_settles_without_a_close_and_the_run_fails(fake_port, settings, journal):
    fake_port.evidence_script_whole_fill_in_one_event()
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert [r.name for r in results][-2:] == ["shrink_proof", "settle_s"] and results[-2].code == "OCA_SHRINK_UNPROVEN"
    assert results[-1].passed and results[-1].evidence["settled_by"] == "already_flat"
    assert f"{settings.run_id}-c-s" not in fake_port.decision_ids()
    outcome = AcceptanceScenario.outcome(results)
    assert outcome.passed is False and outcome.oca_shrink == "UNPROVEN"
    assert fake_port.calls.count(("operator", "acceptance_shrink_probe")) == 1
    assert fake_port.decision_ids().count(f"{settings.run_id}-e-s") == 1


def test_unproven_no_fill_still_held_sends_the_single_close_and_the_run_fails(fake_port, settings, journal):
    fake_port.evidence_script_no_fill_in_60s(held=3)
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert results[-2].code == "OCA_SHRINK_UNPROVEN" and results[-1].name == "settle_s" and results[-1].passed
    assert fake_port.decision_ids().count(f"{settings.run_id}-c-s") == 1
    assert fake_port.calls[-1] == ("supervisor", "submit_ai_paper_decision")
    outcome = AcceptanceScenario.outcome(results)
    assert outcome.passed is False and outcome.oca_shrink == "UNPROVEN"
    assert fake_port.calls.count(("operator", "acceptance_shrink_probe")) == 1


def test_sibling_not_shrunk_stops_at_once_without_settle(fake_port, settings, journal):
    fake_port.evidence_script([target(filled=1, remaining=2), stop(remaining=3)])
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert [r.name for r in results][-1] == "shrink_proof" and results[-1].code == "OCA_SIBLING_NOT_SHRUNK"
    assert f"{settings.run_id}-c-s" not in fake_port.decision_ids()               # abort A4 flattens instead


def test_a_probe_refusal_stops_the_run_without_settle(fake_port, settings, journal):
    fake_port._acceptance_shrink_probe = lambda body: {"state": "REJECTED", "error_code": "PROBE_MARK_MISSING"}
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert (results[-1].name, results[-1].code) == ("shrink_proof", "PROBE_MARK_MISSING")


def test_a_resume_after_the_mark_never_probes_again(fake_port, settings, journal):
    scenario_ = AcceptanceScenario(fake_port, settings, journal)
    scenario_.crash_before_receipt("shrink_proof")
    from trader.acceptance.scenario import SimulatedCrash
    with pytest.raises(SimulatedCrash):
        scenario_.run()
    results = AcceptanceScenario(fake_port, settings, journal).run()
    proof = next(r for r in results if r.name == "shrink_proof")
    assert proof.code == "OCA_SHRINK_UNPROVEN" and fake_port.methods().count("acceptance_shrink_probe") == 0


# ---------------------------------------------------------------------------
# the trader-side probe through the composed stack
# ---------------------------------------------------------------------------

def enter_s(served, tmp_path):
    market(served)
    run = scenario(served, tmp_path).run_until("enter_s")
    assert all(r.passed for r in run), run
    return run[-1].evidence


def mark_body(served, **changes):
    body = {"command_id": f"{RUN_ID}-mark", "experiment_id": served.experiment_id, "run_id": RUN_ID,
            "conid": AAPL, "decision_id": f"{RUN_ID}-e-s"}
    body.update(changes)
    return body


def probe_body(served, **changes):
    return {**mark_body(served, command_id=f"{RUN_ID}-probe"), "display_size": 1, **changes}


def mark_start(served, **changes):
    out = served.call("cli", "acceptance_mark_start", mark_body(served, **changes))
    assert out["state"] == "RESOLVED", out
    return out


def probe(served, **changes):
    return served.call("cli", "acceptance_shrink_probe", probe_body(served, **changes))


def target_trade(served):
    return next(t for e, t in served.sim.ib_trades.items() if e == f"og-aip-{RUN_ID}-e-s:take_profit")


def stop_row(served):
    return served.sim.orders[f"og-aip-{RUN_ID}-e-s:stop"]


def test_s_gets_two_linked_working_legs_through_the_composed_stack(served, tmp_path):
    enter_s(served, tmp_path)
    rows = served.call("ai_supervisor", "get_broker_order_evidence", {"conid": AAPL})["orders"]
    legs = {r["leg"]: r for r in rows if r["leg"] in ("stop", "take_profit") and r["status"] == "Submitted"}
    assert set(legs) == {"stop", "take_profit"} and legs["stop"]["oca_group"] == legs["take_profit"]["oca_group"] != ""
    assert {leg["oca_type"] for leg in legs.values()} == {2}
    assert all(leg["perm_id"] and leg["total_quantity"] == 3 for leg in legs.values())


def test_the_probe_sends_the_whole_target_order_with_only_price_and_display_size_changed(served, tmp_path):
    enter_s(served, tmp_path)
    mark_start(served)
    before = target_trade(served).order
    stop_before = stop_row(served)
    placed = len(served.sim.placed)
    out = probe(served)
    assert out["state"] == "RESOLVED", out
    sent = served.sim.modified[-1]                                                   # the outgoing Order, not a delta
    assert sent is not before
    assert (sent.orderId, sent.permId, sent.ocaGroup, sent.ocaType, sent.totalQuantity, sent.action,
            sent.orderType, sent.tif, sent.parentId) == (
        before.orderId, before.permId, before.ocaGroup, 2, 3.0, before.action, before.orderType, before.tif,
        before.parentId)
    assert (sent.displaySize, sent.lmtPrice) == (1, served.sim.bid(AAPL))
    assert len(served.sim.placed) == placed                                          # no new order
    assert stop_row(served) == stop_before                                           # stop untouched
    assert served.stack.acceptance_probe.marks.get(served.experiment_id)["consumed_at"] is not None


def test_a_modify_that_clears_the_oca_link_is_refused_before_any_wait(served, tmp_path):
    enter_s(served, tmp_path)
    mark_start(served)
    served.sim.modify_clears_oca()
    out = probe(served)
    assert (out["state"], out["error_code"]) == ("REJECTED", "PROBE_OCA_LOST")


def test_the_harness_does_not_wait_after_probe_oca_lost(served, tmp_path):
    enter_s(served, tmp_path)
    served.sim.modify_clears_oca()
    clock = served.now()
    results = scenario(served, tmp_path).run_from("shrink_proof")
    assert [(r.name, r.code) for r in results] == [("shrink_proof", "PROBE_OCA_LOST")]
    assert results[0].evidence["oca_shrink"] == "FAILED" and served.now() == clock


@pytest.mark.parametrize("fault,code", [("not_enabled", "PROBE_NOT_ENABLED"), ("live", "PROBE_NOT_PAPER"),
                                        ("not_armed", "PROBE_NOT_ARMED"), ("position_4", "PROBE_POSITION_MISMATCH"),
                                        ("no_group", "PROBE_OCA_NOT_FOUND"), ("type_1", "PROBE_OCA_TYPE_NOT_2")])
def test_probe_refusals(served, tmp_path, fault, code):
    enter_s(served, tmp_path)
    mark_start(served)
    service = served.stack.acceptance_probe
    s_target, s_stop = f"og-aip-{RUN_ID}-e-s:take_profit", f"og-aip-{RUN_ID}-e-s:stop"
    if fault == "not_enabled":
        service._config = replace(service._config, acceptance_probe=False)
    elif fault == "live":
        service._account_mode = "live"
    elif fault == "not_armed":
        served.call("cli", "pause_experiment", {"command_id": "pause-1", "experiment_id": served.experiment_id,
                                                "reason": "test"})
    elif fault == "position_4":
        served.sim.held[AAPL] = 4.0
    elif fault == "no_group":
        served.sim.set_status(s_target, "Cancelled")
    elif fault == "type_1":
        for entity in (s_target, s_stop):
            served.sim.orders[entity] = replace(served.sim.orders[entity], oca_type=1)
    served.sim.promote()
    out = probe(served)
    assert (out["state"], out["error_code"]) == ("REJECTED", code)
    assert served.sim.modified == []


def test_a_missing_mark_refuses_the_probe(served, tmp_path):
    enter_s(served, tmp_path)
    out = probe(served)
    assert out["error_code"] == "PROBE_MARK_MISSING" and served.sim.modified == []


@pytest.mark.parametrize("field,value", [("experiment_id", "exp-" + "b" * 20), ("run_id", "acc-20260717-ffffff"),
                                         ("conid", 272093), ("decision_id", "acc-20260717-ffffff-e-s")])
def test_a_wrong_mark_refuses_the_probe(served, tmp_path, field, value):
    enter_s(served, tmp_path)
    mark_start(served)
    assert probe(served, **{field: value})["error_code"] == "PROBE_MARK_MISMATCH"
    assert served.sim.modified == []


def test_a_position_not_entered_by_the_marked_decision_refuses_the_probe(served, tmp_path):
    market(served)
    run = scenario(served, tmp_path).run_until("close_a")                  # A closed; S never entered
    assert all(r.passed for r in run)
    mark_start(served)
    served.sim.held[AAPL] = 3.0                                             # 3 shares from elsewhere
    served.sim.promote()
    assert probe(served)["error_code"] == "PROBE_MARK_MISMATCH"


def test_an_expired_consumed_or_replaced_mark_is_stale(served, tmp_path):
    enter_s(served, tmp_path)
    mark_start(served)
    served.advance(seconds=25 * 3600)
    served.sim.promote()
    assert probe(served)["error_code"] == "PROBE_MARK_STALE"


def test_a_consumed_mark_is_stale(served, tmp_path):
    enter_s(served, tmp_path)
    mark_start(served)
    assert probe(served)["state"] == "RESOLVED"
    assert probe(served, command_id=f"{RUN_ID}-probe2")["error_code"] == "PROBE_MARK_STALE"


def test_a_mark_of_a_stopped_experiment_is_stale_for_the_next_one(served, tmp_path):
    market(served)
    old = served.experiment_id
    mark_start(served)
    served.call("cli", "stop_experiment", {"command_id": "stop-1", "experiment_id": old, "reason": "test"})
    served.start_experiment("start-2")
    assert served.experiment_id != old
    assert probe(served, experiment_id=old)["error_code"] == "PROBE_MARK_STALE"


def test_only_acceptance_mark_start_writes_the_mark_and_only_cli_may(served, tmp_path):
    assert served.stack.acceptance_probe.marks.marks() == []           # start_experiment wrote none
    for principal in ("ai_supervisor", "ai_research", "dashboard"):
        for method, body in (("acceptance_mark_start", mark_body(served)),
                             ("acceptance_shrink_probe", probe_body(served))):
            with pytest.raises(TypedRpcRemoteError) as exc:
                served.client(principal, "command").call(method, body, dict)
            assert exc.value.code == "PERMISSION_DENIED"
    assert served.stack.acceptance_probe.marks.marks() == []


def test_mark_start_refusals(served, tmp_path):
    assert served.call("cli", "acceptance_mark_start", mark_body(
        served, command_id=f"{RUN_ID}-mark0", decision_id=f"{RUN_ID}-e-a"))["error_code"] == "PROBE_MARK_MISMATCH"
    mark_start(served)
    assert served.call("cli", "acceptance_mark_start",
                       mark_body(served, command_id=f"{RUN_ID}-mark2"))["error_code"] == "PROBE_MARK_EXISTS"


def test_a_partial_fill_then_no_more_fills_closes_the_residual_and_the_run_continues(served, tmp_path):
    enter_s(served, tmp_path)
    served.sim.script_target_fills([1])
    results = scenario(served, tmp_path).run_from("shrink_proof")
    assert [r.name for r in results] == ["shrink_proof", "settle_s"] and results[0].evidence["oca_shrink"] == "PROVEN"
    assert results[1].passed and results[1].evidence["settled_by"] == "close"
    assert served.sim.held[AAPL] == 0.0
    evidence = served.call("ai_supervisor", "get_broker_order_evidence", {"conid": AAPL})
    assert [o for o in evidence["orders"] if o["status"] in ("Submitted", "PreSubmitted")] == []
    assert AcceptanceScenario.outcome(results).passed is True
    assert evidence["source"] == "synthetic"                                     # the logic only, never the proof


def test_settle_s_fails_the_run_phase_when_s_is_not_flat(served, tmp_path):
    enter_s(served, tmp_path)
    served.sim.never_close(AAPL)
    assert scenario(served, tmp_path).run_from("settle_s")[0].code == "S_NOT_FLAT"


# ---------------------------------------------------------------------------
# migrations 80 / 81 and the ingest's event history
# ---------------------------------------------------------------------------

def test_migrations_80_and_81_apply_once_on_a_real_connection(tmp_path):
    from trader.data.broker_order_events import apply_migration_81_broker_order_events
    from trader.data.duckdb_store import DuckDBConnection
    from trader.data.schema_migrations import SchemaMigrator
    from trader.trading.acceptance_probe import apply_migration_80_acceptance_marks
    migrator = SchemaMigrator(DuckDBConnection(str(tmp_path / "journal.duckdb")))
    migrator.apply(70, "experiments_stub", ["CREATE TABLE IF NOT EXISTS experiments_stub (id INTEGER)"])
    assert apply_migration_80_acceptance_marks(migrator) is True and apply_migration_81_broker_order_events(migrator) is True
    assert {70, 80, 81} <= migrator.applied_versions()
    assert apply_migration_80_acceptance_marks(migrator) is False and apply_migration_81_broker_order_events(migrator) is False
    for table in ("acceptance_marks", "broker_order_events"):
        assert migrator.db.execute(f"SELECT COUNT(*) FROM {table}", fetch="one")[0] == 0


def test_the_command_stack_applies_80_and_81(served):
    applied = {row[0] for row in served.trader.journal_db.execute("SELECT version FROM schema_migrations",
                                                                  fetch="all")}
    assert {80, 81} <= applied


def _ingest_env(tmp_path):
    from trader.data.broker_state import BrokerStateStore
    from trader.data.domain_journal import DomainJournal
    from trader.data.duckdb_store import DuckDBConnection
    from trader.data.schema_migrations import SchemaMigrator
    from trader.trading.broker_ingest import BrokerIngest
    db = DuckDBConnection.get_instance(str(tmp_path / "ingest.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    store = BrokerStateStore(db)
    store.migrate(migrator)
    ingest = BrokerIngest(db=db, journal=journal, store=store, account_id="DU123", account_mode="paper",
                          session_epoch="s1", clock=lambda: NOW)
    return SimpleNamespace(db=db, store=store, ingest=ingest)


def _target_trade(filled, total=3.0, status="Submitted"):
    order = SimpleNamespace(orderId=7, permId=70, parentId=6, orderRef="mmr:og-aip-x-e-s", account="DU123",
                            action="SELL", orderType="LMT", totalQuantity=total, lmtPrice=204.0, auxPrice=0.0,
                            tif="DAY", ocaGroup="oca-og-aip-x-e-s", ocaType=2)
    return SimpleNamespace(order=order, orderStatus=SimpleNamespace(status=status, filled=filled, avgFillPrice=0.0),
                           contract=SimpleNamespace(conId=AAPL, symbol="AAPL"))


def test_ingest_appends_one_event_per_change_with_a_gapless_cursor(tmp_path):
    from trader.data.broker_order_events import broker_order_evidence_in_tx
    env = _ingest_env(tmp_path)
    gid = env.db.transaction(lambda conn: env.store.open_generation_in_tx(conn, ("account",), NOW))
    env.db.transaction(lambda conn: env.store.mark_generation_promoted_in_tx(conn, gid, 0, NOW))
    for filled in (0.0, 0.0, 1.0, 2.0):                                     # one repeat: no change, no event
        env.ingest.on_order_status(_target_trade(filled))
        env.ingest.drain_once()
    reply = env.db.transaction(lambda conn: broker_order_evidence_in_tx(conn, env.store, "DU123", AAPL,
                                                                         source="synthetic"))
    [row] = reply["orders"]
    assert [(e["cursor"], e["filled_quantity"], e["remaining_quantity"]) for e in row["status_events"]] == [
        (1, 0.0, 3.0), (2, 1.0, 2.0), (3, 2.0, 1.0)]
    assert reply["events_gapless"] is True and reply["generation_id"] == gid
    assert all(e["oca_type"] == 2 for e in row["status_events"])


def test_staged_events_belong_to_the_generation_that_promotes_them_and_staging_is_never_read(tmp_path):
    from trader.data.broker_order_events import broker_order_evidence_in_tx
    env = _ingest_env(tmp_path)
    first = env.db.transaction(lambda conn: env.store.open_generation_in_tx(conn, ("account",), NOW))
    env.db.transaction(lambda conn: env.store.mark_generation_promoted_in_tx(conn, first, 0, NOW))
    env.ingest.on_order_status(_target_trade(0.0))
    env.ingest.drain_once()
    second = env.ingest.begin_generation(("account",))
    env.ingest.on_order_status(_target_trade(1.0))
    env.ingest.drain_once()                                                  # staged, not applied
    read = lambda: env.db.transaction(lambda conn: broker_order_evidence_in_tx(       # noqa: E731
        conn, env.store, "DU123", AAPL, source="synthetic"))
    assert read() == {"capture_error": "GENERATION_STAGING"}
    env.ingest.mark_source_complete("account")
    env.ingest.promote_generation()
    reply = read()
    assert reply["generation_id"] == second
    [row] = reply["orders"]
    assert [(e["cursor"], e["filled_quantity"]) for e in row["status_events"]] == [(1, 1.0)]
