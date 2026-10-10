"""Issue #124: a pause stops an AI entry that is not yet being sent, and names one that already is.

The saga's SUBMITTING row is the point of no return. A pause before it is seen by the send gate on that same
transaction; a pause after it cannot stop the send, so its receipt lists the entry (``entries_in_flight``).
"""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from tests.automation.ai_paper_fixtures import ACCOUNT, NOW
from tests.automation.ai_paper_world import World
from trader.automation.ai_paper_config import AiPaperConfig
from trader.automation.ai_paper_decision import AI_PAPER_ACTION, command_id_for
from trader.automation.experiment_service import ArmingLock, ExperimentService
from trader.automation.experiments import ExperimentStore
from trader.trading.command_coordinator import CommandValidationError

ENTRY_COMMAND_ID = command_id_for("dec-00000001")


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    world.arm_experiment()
    return world


def _service(world) -> ExperimentService:
    return ExperimentService(store=world.experiment_store, ports=SimpleNamespace(), lock=ArmingLock(),
                             config=AiPaperConfig(enabled=True), account_id=ACCOUNT, account_mode="paper",
                             now=world.clock)


def _pause_command(world, command_id="pause-cmd-1"):
    experiment_id = world.experiment_store.latest().experiment_id
    return SimpleNamespace(principal="cli", command_id=command_id,
                           body={"experiment_id": experiment_id, "reason": "news risk"})


def _saga(world):
    (raw,) = world.db.execute("SELECT payload FROM automated_order_sagas", fetch="one")
    payload = json.loads(raw)
    return payload["state"], payload["error_code"]


def _pause_transition(world):
    record = world.experiment_store.latest()
    (row,) = [t for t in world.experiment_store.transitions(record.experiment_id) if t["to_state"] == "PAUSED"]
    return row


def _pause_after_the_final_gate(world):
    real = world.guard.revalidate

    def gate_then_pause(*args, **kwargs):
        permit = real(*args, **kwargs)                 # the experiment was ARMED when the guard looked
        world.pause_experiment()
        return permit
    world.guard.revalidate = gate_then_pause


def _pause_during_the_send(world):
    real = world.dispatch.submit_bracket

    def pause_then_send(**kwargs):
        world.pause_experiment()
        return real(**kwargs)
    world.dispatch.submit_bracket = pause_then_send


def test_a_pause_between_the_final_gate_and_the_submitting_row_stops_the_entry(world):
    _pause_after_the_final_gate(world)
    receipt = world.submit()
    assert (receipt.state, receipt.error_code) == ("REJECTED", "EXPERIMENT_NOT_ARMED")
    assert world.dispatch.plans == []
    assert _saga(world) == ("CLOSED", "EXPERIMENT_NOT_ARMED")
    assert _pause_transition(world)["detail"]["entries_in_flight"] == []


def test_an_entry_with_no_experiment_row_is_refused_on_the_submitting_transaction(tmp_path):
    world = World(tmp_path)
    world.arm_experiment()
    world.db.execute("DELETE FROM experiments")
    receipt = world.submit()
    assert (receipt.state, receipt.error_code) == ("REJECTED", "NO_EXPERIMENT")
    assert world.dispatch.plans == []


def test_an_entry_of_an_armed_experiment_is_sent(world):
    assert world.submit().state == "SUBMITTED"
    assert len(world.dispatch.plans) == 1


@pytest.mark.parametrize("request_action,body_action", [
    (AI_PAPER_ACTION, "CLOSE"), (AI_PAPER_ACTION, "PARTIAL_CLOSE"), ("approve_proposal", "ENTER")])
def test_only_an_ai_enter_is_gated_by_a_pause(world, request_action, body_action):
    world.pause_experiment()
    request = SimpleNamespace(action=request_action, body={"action": body_action})
    assert world.db.transaction(lambda conn: world._experiment_gate(conn, request)) is None


def test_a_pause_while_the_entry_is_being_sent_names_the_entry(world):
    _pause_during_the_send(world)
    receipt = world.submit()
    assert receipt.state == "SUBMITTED" and len(world.dispatch.plans) == 1
    assert world.experiment_store.latest().state == "PAUSED"
    assert _pause_transition(world)["detail"]["entries_in_flight"] == [ENTRY_COMMAND_ID]


def test_the_pause_receipt_lists_the_entry_being_sent(world):
    service = _service(world)
    receipts = []
    real = world.dispatch.submit_bracket

    def pause_then_send(**kwargs):
        receipts.append(service.pause(_pause_command(world)))
        return real(**kwargs)
    world.dispatch.submit_bracket = pause_then_send
    world.submit()
    (receipt,) = receipts
    assert receipt["state"] == "PAUSED" and receipt["entries_in_flight"] == [ENTRY_COMMAND_ID]
    again = service.pause(_pause_command(world, "pause-cmd-2"))     # a repeat replays what the pause found
    assert again["state"] == "PAUSED" and again["entries_in_flight"] == [ENTRY_COMMAND_ID]


def test_a_pause_with_no_entry_in_flight_has_an_empty_list(world):
    receipt = _service(world).pause(_pause_command(world))
    assert receipt["state"] == "PAUSED" and receipt["entries_in_flight"] == []
    assert _pause_transition(world)["detail"]["entries_in_flight"] == []


def test_a_finished_send_is_not_in_flight(world):
    assert world.submit().state == "SUBMITTED"
    assert _service(world).pause(_pause_command(world))["entries_in_flight"] == []


def test_a_submitting_row_older_than_this_process_is_not_in_flight(world):
    restarted = ExperimentStore(world.db, ACCOUNT, world.clock, process_started_at=NOW + dt.timedelta(seconds=1))

    def pause_through_restarted_store(**kwargs):
        record = restarted.latest()
        restarted.transition(record.experiment_id, expected=frozenset({"ARMED"}), to="PAUSED",
                             principal="cli", command_id="pause-1", reason="x")
        return real(**kwargs)
    real = world.dispatch.submit_bracket
    world.dispatch.submit_bracket = pause_through_restarted_store
    world.submit()
    assert restarted.latest().state == "PAUSED"
    assert _pause_transition(world)["detail"]["entries_in_flight"] == []


def test_another_accounts_entry_is_not_listed(world):
    other = ExperimentStore(world.db, "DU999", world.clock)
    listed = []
    real = world.dispatch.submit_bracket

    def look_during_the_send(**kwargs):
        listed.extend(world.db.transaction(other._entries_being_sent_in_tx))
        listed.extend(world.db.transaction(world.experiment_store._entries_being_sent_in_tx))
        return real(**kwargs)
    world.dispatch.submit_bracket = look_during_the_send
    world.submit()
    assert listed == [ENTRY_COMMAND_ID]                 # only this account's store lists it


def test_the_outage_pause_records_the_entries_in_flight(world):
    """The kill monitor pauses through the same store transition."""
    store = world.experiment_store
    real = world.dispatch.submit_bracket

    def outage_pause_then_send(**kwargs):
        record = store.latest()
        store.transition(record.experiment_id, expected=frozenset({"ARMED"}), to="PAUSED",
                         principal="kill_monitor", command_id=None, reason="broker data unavailable for 300 s",
                         changes={"pause_cause": "BROKER_DATA_OUTAGE", "pause_generation_id": 7})
        return real(**kwargs)
    world.dispatch.submit_bracket = outage_pause_then_send
    world.submit()
    detail = _pause_transition(world)["detail"]
    assert detail["entries_in_flight"] == [ENTRY_COMMAND_ID] and detail["pause_cause"] == "BROKER_DATA_OUTAGE"


def test_a_store_without_the_saga_tables_pauses_with_no_entries(tmp_path):
    from tests.automation.experiment_fixtures import NOW as EXPERIMENT_NOW, armed_record
    from trader.automation.experiments import apply_experiment_migration
    from trader.data.duckdb_store import DuckDBConnection
    from trader.data.schema_migrations import SchemaMigrator

    db = DuckDBConnection.get_instance(str(tmp_path / "bare.duckdb"))
    apply_experiment_migration(SchemaMigrator(db))
    store = ExperimentStore(db, "DU1", lambda: EXPERIMENT_NOW)
    record = store.insert_armed(armed_record(), principal="cli", reason="go")
    paused = store.transition(record.experiment_id, expected=frozenset({"ARMED"}), to="PAUSED",
                              principal="cli", command_id=None, reason="x")
    assert store.entries_in_flight_at_pause(paused.experiment_id, paused.revision) == []


def test_a_killed_experiment_cannot_be_paused_into_a_receipt(world):
    store = world.experiment_store
    record = store.latest()
    store.transition(record.experiment_id, expected=frozenset({"ARMED"}), to="KILLED", principal="kill_monitor",
                     command_id=None, reason="kill line hit")
    with pytest.raises(CommandValidationError) as refused:
        _service(world).pause(_pause_command(world))
    assert refused.value.code == "EXPERIMENT_KILLED"


def test_the_command_stack_composes_the_withdrawal_and_the_experiment_gates(world):
    from trader.trading.command_stack import _AiPaperParts, _ai_paper_send_gate_in_tx

    parts = _AiPaperParts(config=None, policy=world.policy, entry_filter=world.entry_filter,
                          deployments=world.deployments, scope_checks=None, filter_refusal=None,
                          versions=world.versions, activity=world.activity)
    request = SimpleNamespace(action=AI_PAPER_ACTION, body=world.body())
    gate = _ai_paper_send_gate_in_tx(parts, world.experiment_store)
    assert world.db.transaction(lambda conn: gate(conn, request)) is None

    world.pause_experiment()
    assert world.db.transaction(lambda conn: gate(conn, request)) == "EXPERIMENT_NOT_ARMED"
    world.versions.withdraw(world.version_digest, reason="operator", principal="cli", command_id="w1")
    assert world.db.transaction(lambda conn: gate(conn, request)) == "DEPLOYMENT_NOT_ACTIVE"   # first refusal wins


def test_a_stack_without_experiments_keeps_the_withdrawal_gate_only(world):
    from trader.trading.command_stack import _AiPaperParts, _ai_paper_send_gate_in_tx

    parts = _AiPaperParts(config=None, policy=world.policy, entry_filter=world.entry_filter,
                          deployments=world.deployments, scope_checks=None, filter_refusal=None,
                          versions=world.versions, activity=world.activity)
    gate = _ai_paper_send_gate_in_tx(parts, None)
    request = SimpleNamespace(action=AI_PAPER_ACTION, body=world.body())
    world.pause_experiment()
    assert world.db.transaction(lambda conn: gate(conn, request)) is None
    assert _ai_paper_send_gate_in_tx(None, world.experiment_store) is None


# Issue #124 round 1: the pause and the SUBMITTING row are ordered by the domain journal's write lock.

def test_every_write_of_a_journal_store_holds_the_journal_write_lock(world):
    store = world.experiment_store
    held = []
    real_get = store._get_in_tx

    def note_lock(conn, experiment_id):
        held.append(world.journal._write_lock.locked())
        return real_get(conn, experiment_id)
    store._get_in_tx = note_lock
    record = store.latest()
    store.raise_peak(record.experiment_id, 150_000.0)
    store.update_kill_progress(record.experiment_id, expected_state="ARMED", changes={"kill_round": 0})
    world.pause_experiment()
    store.transition(record.experiment_id, expected=frozenset({"PAUSED"}), to="STOPPED", principal="cli",
                     command_id=None, reason="done")
    assert len(held) >= 4 and all(held)
    assert not world.journal._write_lock.locked()


def test_a_store_without_a_journal_writes_under_the_database_lock_only(tmp_path):
    from tests.automation.experiment_fixtures import NOW as EXPERIMENT_NOW, armed_record
    from trader.automation.experiments import apply_experiment_migration
    from trader.data.duckdb_store import DuckDBConnection
    from trader.data.schema_migrations import SchemaMigrator

    db = DuckDBConnection.get_instance(str(tmp_path / "plain.duckdb"))
    apply_experiment_migration(SchemaMigrator(db))
    store = ExperimentStore(db, "DU1", lambda: EXPERIMENT_NOW)
    record = store.insert_armed(armed_record(), principal="cli", reason="go")
    assert store.raise_peak(record.experiment_id, 120_000.0) == 120_000.0


def test_a_pause_started_during_the_submitting_transaction_waits_and_then_names_the_entry(world):
    import threading
    seen = {}

    def pause_meanwhile(conn, request):
        racer = threading.Thread(target=lambda: seen.update(paused=world.pause_experiment()))
        racer.start()
        racer.join(timeout=0.3)
        seen["waited"] = racer.is_alive()               # blocked on the journal write lock
        seen["racer"] = racer
    world.on_intent_check(pause_meanwhile)
    receipt = world.submit()
    seen["racer"].join(timeout=10)
    assert seen["waited"] is True and receipt.state == "SUBMITTED" and len(world.dispatch.plans) == 1
    assert _pause_transition(world)["detail"]["entries_in_flight"] == [ENTRY_COMMAND_ID]


def test_the_stack_builds_its_experiment_store_on_the_domain_journal(world):
    from trader.trading.command_stack import _build_experiment_parts

    trader = SimpleNamespace(journal_db=world.db, ib_account=ACCOUNT, domain_journal=world.journal)
    parts = _build_experiment_parts(trader, "paper", world.clock)
    assert parts.store._journal is world.journal
    assert _build_experiment_parts(trader, "live", world.clock) is None


# Issue #124 round 2: a send is in flight until it returns, in whatever state broker events moved the saga to.

def _entry_submitted_event(world):
    from trader.automation.protective_order_saga import BrokerOrderEvent
    return world.saga.on_broker_event(BrokerOrderEvent(
        order_group_id="og-" + ENTRY_COMMAND_ID, leg="entry", status="Submitted", filled_quantity=0.0,
        total_quantity=499.0, order_id=1, event_id="evt-submitted", source_timestamp=world.clock()))


def _after_the_broker_work_before_the_return(world, then):
    """The fake send finishes its broker work, the broker's Submitted event is ingested, then ``then`` runs."""
    real = world.dispatch.submit_bracket

    def send(**kwargs):
        submitted = real(**kwargs)
        assert _entry_submitted_event(world).state == "ENTRY_WORKING"
        world.clock.advance(seconds=10)
        then()
        return submitted
    world.dispatch.submit_bracket = send


def test_a_pause_names_an_entry_the_broker_acknowledged_before_the_send_returned(world):
    _after_the_broker_work_before_the_return(world, world.pause_experiment)
    assert world.submit().state == "SUBMITTED"
    assert _pause_transition(world)["detail"]["entries_in_flight"] == [ENTRY_COMMAND_ID]


def test_a_withdrawal_is_refused_while_an_acknowledged_entry_has_not_returned_from_its_send(world):
    from trader.automation.ai_deployments import DeploymentRefused
    seen = {}

    def try_withdraw():
        try:
            world.versions.withdraw(world.version_digest, reason="operator", principal="cli", command_id="w-ack")
        except DeploymentRefused as refused:
            seen["code"] = refused.code
    _after_the_broker_work_before_the_return(world, try_withdraw)
    assert world.submit().state == "SUBMITTED"
    assert seen["code"] == "WITHDRAWAL_ENTRY_IN_FLIGHT"
    assert world.versions.withdraw(world.version_digest, reason="operator", principal="cli", command_id="w-after")


def test_an_acknowledged_entry_whose_send_returned_is_not_in_flight(world):
    assert world.submit().state == "SUBMITTED"
    assert _entry_submitted_event(world).state == "ENTRY_WORKING"
    assert _service(world).pause(_pause_command(world))["entries_in_flight"] == []


def test_an_unreturned_send_of_an_earlier_process_is_not_listed_even_after_a_later_event(world):
    restarted = ExperimentStore(world.db, ACCOUNT, world.clock, process_started_at=NOW + dt.timedelta(seconds=5))

    def pause_through_restarted_store():
        record = restarted.latest()
        restarted.transition(record.experiment_id, expected=frozenset({"ARMED"}), to="PAUSED",
                             principal="cli", command_id="pause-1", reason="x")
    _after_the_broker_work_before_the_return(world, pause_through_restarted_store)   # updated_at is now NOW+10 s
    world.submit()
    assert _pause_transition(world)["detail"]["entries_in_flight"] == []


def test_a_withdrawal_ignores_an_unreturned_send_of_an_earlier_process(world):
    world.versions._process_started_at = NOW + dt.timedelta(seconds=5)
    seen = {}
    _after_the_broker_work_before_the_return(
        world, lambda: seen.update(withdrawn=world.versions.withdraw(
            world.version_digest, reason="operator", principal="cli", command_id="w-old")))
    world.submit()
    assert seen == {"withdrawn": True}
