# tests/test_close_reconciliation.py
"""SP1 plan 1 Task 17 (R17): commands that start or join a close resolve from that exact root."""
import datetime as dt
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerPositionRow, BrokerRiskSnapshot
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_coordinator import (
    CommandLedger, CommandRequest, OutcomeReconciler, apply_command_ledger_migration,
)
from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration
from trader.trading.liquidation_service import (
    JoinRow, LiquidationReceipt, LiquidationRunStore, LiquidationService, apply_liquidation_migration,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT = "DU111111"
DEADLINE = NOW + dt.timedelta(minutes=5)


class _Alerts:
    def __init__(self): self.raised = []
    def raise_alert(self, command_id, detail): self.raised.append((command_id, detail))


@pytest.fixture
def env(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "close-recon.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_command_ledger_migration(migrator)
    apply_exit_owner_migration(migrator)
    apply_liquidation_migration(migrator)
    ledger = CommandLedger(journal)
    store = LiquidationRunStore(db)
    reconciler = OutcomeReconciler(
        journal=journal, ledger=ledger,
        orders=SimpleNamespace(find_by_order_ref=lambda *a: [], enumeration_complete=lambda: False),
        strategy=SimpleNamespace(), alerts=_Alerts(), now=lambda: NOW, closes=store,
    )
    return SimpleNamespace(db=db, journal=journal, ledger=ledger, store=store, reconciler=reconciler,
                           alerts=reconciler._alerts)


def _command(env, command_id, *, action="execute_automated_intent", state="OUTCOME_UNKNOWN"):
    request = CommandRequest(command_id=command_id, action=action, account_id=ACCOUNT, target_type="intent",
                             target_id=command_id, expected_version=None, body={}, source="strategy_service")

    def work(conn, _append):
        env.ledger.insert_received_in_tx(conn, request, f"hash-{command_id}", NOW)
        previous = "RECEIVED"
        for step in ("SUBMITTING", "OUTCOME_UNKNOWN")[: ("SUBMITTING", "OUTCOME_UNKNOWN").index(state) + 1]:
            env.ledger.transition_in_tx(conn, command_id, previous, step, now=NOW)
            previous = step
    env.journal.mutate_batch_work(env.journal.connect(), work)


def _root(env, root_id, state, *, scope="conid", goal="zero", superseded_by=None, cleanup_pending=False):
    env.store.transaction(lambda conn: env.store.insert_run_in_tx(conn, LiquidationReceipt(
        ACCOUNT, root_id, state, DEADLINE, generation_id=9, scope=scope, conid=None if scope == "account" else 1,
        goal=goal, superseded_by=superseded_by, cleanup_pending=cleanup_pending), NOW))


def _join(env, command_id, root_id, goal, *, quantity=None, outcome="JOINED"):
    env.store.transaction(lambda conn: env.store.record_join_in_tx(conn, JoinRow(
        command_id, root_id, ACCOUNT, None if goal == "account" else 1, outcome, goal, quantity), NOW))


def _state(env, command_id):
    return env.ledger.get(command_id).state


def test_sell_that_joined_a_time_exit_resolves_when_that_root_is_closed(env):
    _root(env, "exit-1", "CLOSED")
    _join(env, "sell-1", "exit-1", "zero")
    _command(env, "sell-1")
    assert env.reconciler.reconcile_once("sell-1", NOW).resolved is True
    row = env.ledger.get("sell-1")
    assert (row.state, row.outcome["close_root_id"], row.outcome["liquidation_state"]) == ("RESOLVED", "exit-1", "CLOSED")
    assert env.ledger.unresolved_for_account(ACCOUNT) == []        # #28: reconciliation is ready again


def test_sell_that_upgraded_a_partial_root_resolves_when_it_is_closed(env):
    _root(env, "p-1", "CLOSED")
    _join(env, "sell-2", "p-1", "zero", outcome="UPGRADED")
    _command(env, "sell-2")
    assert env.reconciler.reconcile_once("sell-2", NOW).resolved is True


def test_sell_that_joined_an_account_flatten_resolves_on_flat(env):
    _root(env, "flat-1", "FLAT", scope="account")
    _join(env, "sell-3", "flat-1", "zero", outcome="JOINED_FLATTEN")
    _command(env, "sell-3")
    assert env.reconciler.reconcile_once("sell-3", NOW).resolved is True


def test_superseded_root_is_followed_to_the_account_root(env):
    _root(env, "p-1", "SUPERSEDED", goal="partial", superseded_by="flat-1")
    _root(env, "flat-1", "FLAT", scope="account")
    _join(env, "sell-4", "p-1", "partial", quantity=4.0, outcome="CLAIMED")
    _command(env, "sell-4")
    assert env.reconciler.reconcile_once("sell-4", NOW).resolved is True
    assert env.ledger.get("sell-4").outcome["close_root_id"] == "flat-1"


@pytest.mark.parametrize("state,cleanup_pending", [("VERIFYING", False), ("CLOSED", True)])
def test_open_or_uncleaned_root_leaves_the_command_unknown(env, state, cleanup_pending):
    _root(env, "exit-1", state, cleanup_pending=cleanup_pending)
    _join(env, "sell-5", "exit-1", "zero")
    _command(env, "sell-5")
    assert env.reconciler.reconcile_once("sell-5", NOW).resolved is False
    assert _state(env, "sell-5") == "OUTCOME_UNKNOWN"


@pytest.mark.parametrize("state,code", [("FAILED_SAFE", "CLOSE_FAILED_SAFE"), ("REDUCE_FAILED", "REDUCE_FAILED")])
def test_a_root_that_failed_rejects_the_command_with_an_operator_alert(env, state, code):
    """R33 / D12: never a success, and never OUTCOME_UNKNOWN for ever (that blocks reconciliation)."""
    _root(env, "exit-1", state, goal="partial" if state == "REDUCE_FAILED" else "zero")
    _join(env, "sell-5", "exit-1", "zero")
    _command(env, "sell-5")
    assert env.reconciler.reconcile_once("sell-5", NOW).resolved is True
    row = env.ledger.get("sell-5")
    assert (row.state, row.error_code, row.outcome["liquidation_state"]) == ("REJECTED", code, state)
    assert [c for c, _detail in env.alerts.raised] == ["sell-5"]
    assert env.ledger.unresolved_for_account(ACCOUNT) == []


def test_done_resolves_a_partial_request_but_fails_a_full_one(env):
    _root(env, "p-1", "DONE", goal="partial")
    _join(env, "sell-6", "p-1", "partial", quantity=4.0, outcome="CLAIMED")
    _join(env, "sell-7", "p-1", "zero")
    _command(env, "sell-6")
    _command(env, "sell-7")
    assert env.reconciler.reconcile_once("sell-6", NOW).resolved is True
    assert env.reconciler.reconcile_once("sell-7", NOW).resolved is True
    assert (_state(env, "sell-6"), env.ledger.get("sell-7").error_code) == ("RESOLVED", "CLOSE_GOAL_NOT_MET")


def test_root_that_ended_before_close_pending_was_recorded_resolves_the_submitting_command(env):
    _root(env, "exit-1", "CLOSED")
    _join(env, "sell-8", "exit-1", "zero")
    _command(env, "sell-8", state="SUBMITTING")
    assert env.reconciler.reconcile_once("sell-8", NOW).resolved is True
    assert _state(env, "sell-8") == "RESOLVED"


def test_restart_requeues_and_resolves_joined_commands(env):
    _root(env, "exit-1", "CLOSED")
    _join(env, "sell-9", "exit-1", "zero")
    _command(env, "sell-9")
    assert "sell-9" in env.reconciler.rescan_on_startup()
    env.reconciler.run_due(NOW)
    assert _state(env, "sell-9") == "RESOLVED"


def test_bracket_entry_without_a_close_root_is_left_alone(env):
    _command(env, "buy-1")
    assert env.reconciler.reconcile_once("buy-1", NOW).resolved is False


def test_joined_flatten_command_resolves_from_its_root(env):
    _root(env, "session-flatten-1", "FLAT", scope="account")
    _join(env, "flatten-ui-1", "session-flatten-1", "account", outcome="JOINED_FLATTEN")
    _command(env, "flatten-ui-1", action="liquidate_account")
    assert env.reconciler.reconcile_once("flatten-ui-1", NOW).resolved is True


def _snapshot(generation, quantity):
    positions = () if not quantity else (BrokerPositionRow(
        account_id=ACCOUNT, conid=1, symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD",
        quantity=quantity, average_cost=None, market_price=None, market_value=None, unrealized_pnl=None,
        realized_pnl=None, daily_pnl=None, deleted=False, revision=1, source_timestamp=NOW),)
    return BrokerRiskSnapshot(generation_id=generation, source_cursor=generation, promoted_at=NOW,
                              account_id=ACCOUNT, account_mode="paper", net_liquidation=100_000,
                              daily_pnl=0, positions=positions, working_orders=())


def _service(env, snapshots, rows):
    broker = SimpleNamespace(last=0)

    def capture(_account):
        snapshot = snapshots.pop(0) if len(snapshots) > 1 else snapshots[0]
        broker.last = snapshot.generation_id
        return snapshot
    broker.capture = capture
    dispatch = SimpleNamespace(
        reduce=lambda p, s, q, cid: rows.setdefault(cid, [SimpleNamespace(
            status="Filled", filled_quantity=q, total_quantity=q)]),
        find_orders=lambda a, cid: rows.get(cid, []), get_order=lambda e: None,
        enumeration_complete=lambda: True, newest_generation=lambda: broker.last)
    return LiquidationService(broker, dispatch, store=env.store, registry=ExitOwnerRegistry(env.db),
                              now=lambda: NOW, journal=env.journal, ledger=env.ledger,
                              schedule_reconcile=lambda command_id: env.reconciler.schedule(command_id, NOW))


def test_flatten_cleanup_hands_every_joined_flatten_command_to_the_reconciler(env):
    """R10 + R17 + R33: two /flatten commands, one root; the reconciler resolves both once FLAT is proven."""
    service = _service(env, [_snapshot(1, 10.0), _snapshot(2, 0.0), _snapshot(3, 0.0)], {})
    for command_id in ("flatten-a", "flatten-b"):
        _command(env, command_id, action="liquidate_account")
        service.start(ACCOUNT, command_id, DEADLINE)
    service.rescan()
    assert service.rescan().state == "FLAT"
    assert (_state(env, "flatten-a"), _state(env, "flatten-b")) == ("OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN")
    env.reconciler.run_due(NOW)
    assert (_state(env, "flatten-a"), _state(env, "flatten-b")) == ("RESOLVED", "RESOLVED")
    assert env.ledger.get("flatten-b").outcome["close_root_id"] == "flatten-a"


def test_a_command_still_submitting_when_its_root_ends_is_left_to_its_producer(env):
    """R33: one resolver per row. Cleanup skips a SUBMITTING row; the producer moves it and schedules it."""
    service = _service(env, [_snapshot(1, 0.0), _snapshot(2, 0.0)], {})
    _command(env, "sell-1", state="SUBMITTING")
    service.start(ACCOUNT, "sell-1", DEADLINE, scope="conid", conid=1)    # flat at once: no order
    service.rescan()
    assert service.receipt_for("sell-1").state == "CLOSED"
    env.reconciler.run_due(NOW)
    assert _state(env, "sell-1") == "SUBMITTING"                          # nobody raced the producer

    def to_unknown(conn, _append):
        env.ledger.transition_in_tx(conn, "sell-1", "SUBMITTING", "OUTCOME_UNKNOWN", now=NOW)
    env.journal.mutate_batch_work(env.journal.connect(), to_unknown)
    env.reconciler.schedule("sell-1", NOW)
    env.reconciler.run_due(NOW)
    assert _state(env, "sell-1") == "RESOLVED"
    env.reconciler.schedule("sell-1", NOW)                                # a late schedule is a no-op
    assert env.reconciler.reconcile_once("sell-1", NOW).resolved is True and _state(env, "sell-1") == "RESOLVED"


def test_a_joined_sell_on_a_reduce_failed_root_is_rejected_after_a_restart(env):
    """R2-2 / #29: the original partial and the joined SELL both fail; restart requeues them."""
    _root(env, "p-1", "REDUCE_FAILED", goal="partial")
    _join(env, "p-1", "p-1", "partial", quantity=4.0, outcome="CLAIMED")
    _join(env, "sell-2", "p-1", "partial", quantity=2.0, outcome="JOINED")
    _command(env, "p-1")
    _command(env, "sell-2")
    restarted = OutcomeReconciler(journal=env.journal, ledger=env.ledger,
                                  orders=SimpleNamespace(find_by_order_ref=lambda *a: [], enumeration_complete=lambda: False),
                                  strategy=SimpleNamespace(), alerts=_Alerts(), now=lambda: NOW, closes=env.store)
    assert set(restarted.rescan_on_startup()) >= {"p-1", "sell-2"}
    restarted.run_due(NOW)
    assert [env.ledger.get(c).error_code for c in ("p-1", "sell-2")] == ["REDUCE_FAILED", "REDUCE_FAILED"]
