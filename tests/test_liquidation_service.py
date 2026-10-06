import datetime as dt
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerOrderRow, BrokerPositionRow, BrokerRiskSnapshot
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration
from trader.trading.liquidation_service import (
    ChildRef, DispatchRefused, LiquidationBusy, LiquidationReceipt, LiquidationRunStore, LiquidationService,
    RunStateError, apply_liquidation_migration,
)
from trader.trading.order_correlation import matches_legacy_reduce


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT = "DU123"
DEADLINE = NOW + dt.timedelta(minutes=5)


def _position(quantity=10.0, conid=1, market_price=None):
    return BrokerPositionRow(
        account_id=ACCOUNT, conid=conid, symbol="AAPL", sec_type="STK", exchange="SMART",
        currency="USD", quantity=quantity, average_cost=None, market_price=market_price,
        market_value=None, unrealized_pnl=None, realized_pnl=None, daily_pnl=None,
        deleted=False, revision=1, source_timestamp=NOW,
    )


def _order(entity="external-1", group=None, conid=1, leg=None, filled=0.0, total=10.0, order_type="LMT"):
    return BrokerOrderRow(
        order_entity_id=entity, account_id=ACCOUNT, conid=conid, symbol="AAPL",
        order_group_id=group, leg=leg, is_external=group is None, action="SELL", order_type=order_type,
        total_quantity=total, filled_quantity=filled, avg_fill_price=None, limit_price=100,
        stop_price=None, tif="DAY", status="Submitted", deleted=False, revision=1,
        source_timestamp=NOW,
    )


def _snapshot(generation, positions=(), working=()):
    return BrokerRiskSnapshot(
        generation_id=generation, source_cursor=generation, promoted_at=NOW,
        account_id=ACCOUNT, account_mode="paper", net_liquidation=100_000,
        daily_pnl=0, positions=tuple(positions), working_orders=tuple(working),
    )


def _row(status, filled=0.0, total=10.0, entity=None, action="SELL", oca_group=None, oca_type=None):
    return SimpleNamespace(status=status, filled_quantity=filled, total_quantity=total, deleted=False,
                           order_entity_id=entity, action=action, oca_group=oca_group, oca_type=oca_type)


class _Broker:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0
        self.last = 0          # generation of the last captured snapshot

    def capture(self, account_id):
        self.calls += 1
        value = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        if isinstance(value, Exception):
            raise value
        self.last = value.generation_id
        return value


class _Dispatch:
    """Records orders; answers evidence lookups from dicts the test fills.

    ``newest_generation`` is the last captured generation plus ``staging``:
    set ``staging = 1`` to model a broker sync that opened before a send.
    ``complete`` is ``enumeration_complete()``.
    """
    def __init__(self, broker):
        self.broker = broker
        self.calls = []
        self.rows: dict[str, list] = {}       # child id -> broker rows found by its order ref
        self.sequences: dict[str, list] = {}  # child id -> successive answers; the last one repeats
        self.entities: dict[str, object] = {}  # order entity id -> broker row (cancel targets)
        self.refuse: set[str] = set()          # methods that raise DispatchRefused before the boundary
        self.fail_after_send: set[str] = set() # methods that raise after the order was sent
        self.staging = 0
        self.complete = True

    def _record(self, name, call):
        if name in self.refuse:
            raise DispatchRefused("REDUCE_ONLY_REFUSED", name)
        self.calls.append(call)
        if name in self.fail_after_send:
            raise TimeoutError(f"{name} acknowledgement timed out")

    def cancel(self, order, child_id):
        self._record("cancel", ("cancel", order.order_entity_id, child_id))

    def reduce(self, position, side, quantity, child_id):
        self._record("reduce", ("reduce", position.conid, side, quantity, child_id))

    def reduce_partial(self, position, side, quantity, child_id):
        self._record("reduce_partial", ("reduce_partial", position.conid, side, quantity, child_id))

    def place_exit_leg(self, position, *, leg, quantity, price, oca_group, child_id):
        self._record("place_exit_leg", ("place_exit_leg", position.conid, leg, quantity, price, oca_group, child_id))

    def find_orders(self, account_id, child_id):
        answers = self.sequences.get(child_id)
        if answers:
            return list(answers.pop(0) if len(answers) > 1 else answers[0])
        return list(self.rows.get(child_id, []))

    def find_orders_with_prefix(self, account_id, prefix):
        return [row for ref, rows in self.rows.items() if matches_legacy_reduce(ref, prefix) for row in rows]

    def get_order(self, order_entity_id):
        return self.entities.get(order_entity_id)

    def enumeration_complete(self):
        return self.complete

    def newest_generation(self):
        return self.broker.last + self.staging


class _Breaker:
    def __init__(self): self.calls = []
    def trip_liquidation(self, cause, detail): self.calls.append((cause, detail))


class _LedgerRow:
    def __init__(self, state): self.state = state


class _Ledger:
    def __init__(self, states=None):
        self.rows = {command_id: _LedgerRow(state) for command_id, state in (states or {}).items()}
        self.transitions = []
    def get(self, command_id): return self.rows.get(command_id)
    def transition_in_tx(self, _conn, command_id, before, after, **kwargs):
        self.transitions.append((command_id, before, after, kwargs))


class _Journal:
    def connect(self): return object()
    def mutate(self, conn, mutation, write, event_id): write(conn, 1)


class _Crash(BaseException):
    """Simulated process death: never caught by the service."""


class _Stack:
    """A service over a real DuckDB journal; restart() rebuilds it on the same file."""
    def __init__(self, tmp_path, snapshots, *, protection=None, journal=None, ledger=None, deadline_seconds=300.0,
                 lock_timeout_seconds=60.0):
        self.db = DuckDBConnection.get_instance(str(tmp_path / "liq.duckdb"))
        migrator = SchemaMigrator(self.db)
        apply_exit_owner_migration(migrator)
        apply_liquidation_migration(migrator)
        self.store = LiquidationRunStore(self.db)
        self.registry = ExitOwnerRegistry(self.db)
        self.broker = _Broker(snapshots)
        self.dispatch = _Dispatch(self.broker)
        self.breaker = _Breaker()
        self.scheduled: list[str] = []
        self.clock = {"now": NOW}
        self.protection, self.journal, self.ledger = protection, journal, ledger
        self.deadline_seconds = deadline_seconds
        self.lock_timeout_seconds = lock_timeout_seconds
        self.service = self._build()

    def _build(self):
        return LiquidationService(
            self.broker, self.dispatch, store=self.store, registry=self.registry,
            now=lambda: self.clock["now"], breaker=self.breaker, journal=self.journal, ledger=self.ledger,
            schedule_reconcile=self.scheduled.append, protection=self.protection,
            deadline_seconds=self.deadline_seconds, lock_timeout_seconds=self.lock_timeout_seconds)

    def restart(self):
        self.store = LiquidationRunStore(self.db)
        self.registry = ExitOwnerRegistry(self.db)
        self.service = self._build()
        return self.service

    def push(self, *snapshots):
        self.broker.snapshots = list(snapshots)


def _stack(tmp_path, snapshots, **kwargs):
    return _Stack(tmp_path, snapshots, **kwargs)


class _Protection:
    """Records every call; ``crash_on`` makes the next call of that name die."""
    def __init__(self, stop_price=95.0, target_price=None):
        self.calls = []
        self.info = (stop_price, target_price)
        self.crash_on: set[str] = set()

    def _call(self, name, *args):
        if name in self.crash_on:
            self.crash_on.discard(name)
            raise _Crash()
        self.calls.append((name, *args))

    def handover(self, *, account_id, conid, close_root_id, cancels, generation, now):
        from trader.trading.liquidation_service import HandoverInfo
        self._call("handover", conid, close_root_id, tuple(c.order_entity_id for c in cancels))
        return HandoverInfo(*self.info)

    def handover_account(self, *, account_id, close_root_id, cancels, generation, now):
        self._call("handover_account", close_root_id, tuple(c.order_entity_id for c in cancels))

    def expect_reprotect(self, *, close_root_id, groups, now):
        self._call("expect_reprotect", close_root_id, tuple(groups))

    def release_after_partial(self, *, close_root_id, remaining_quantity, stop_group, stop_status,
                              target_group, target_status, now):
        self._call("release_after_partial", close_root_id, remaining_quantity, stop_group, target_group)

    def close_after_full(self, *, close_root_id, now):
        self._call("close_after_full", close_root_id)


# ---------------------------------------------------------------------------
# Task 4: data model, store, account scope
# ---------------------------------------------------------------------------

def test_cancels_external_orders_before_submitting_any_reduction(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()], [_order()]), _snapshot(2, [_position()])])
    receipt = s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    assert receipt.state == "VERIFYING"
    s.dispatch.entities["external-1"] = _row("Cancelled")
    receipt = s.service.rescan()
    assert receipt.state == "VERIFYING"
    assert [call[0] for call in s.dispatch.calls] == ["cancel", "reduce"]
    assert s.dispatch.calls[1][2:4] == ("SELL", 10.0)


def test_submitted_reduction_is_not_treated_as_flat_without_new_broker_snapshot(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    receipt = s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    assert receipt.state == "VERIFYING"
    assert len(s.dispatch.calls) == 1
    assert s.breaker.calls


def test_flat_requires_newer_generation_and_a_terminal_reduce_child(tmp_path):
    """R19: FLAT only after a newer empty snapshot and a terminal reduce child."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, [])])
    assert s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1)).state == "VERIFYING"
    child = s.service.receipt_for("root-1").children[0]
    assert (child.child_id, child.kind, child.state, child.fence_generation, child.sent_generation) == (
        "root-1-reduce-1-1", "reduce", "UNKNOWN", 1, 1)
    s.dispatch.rows["root-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    assert s.service.rescan().state == "VERIFYING"      # generation 2 observes the fill
    assert s.service.rescan().state == "FLAT"           # generation 3 is newer than that fill


def test_flat_hands_waiting_commands_to_the_reconciler_and_resolves_nothing(tmp_path):
    """D12: the reconciler is the only resolver; a SUBMITTING command is left to its producer."""
    ledger = _Ledger({"root-1": "OUTCOME_UNKNOWN", "root-2": "SUBMITTING"})
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, [])],
               journal=_Journal(), ledger=ledger)
    s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    s.service.start(ACCOUNT, "root-2", NOW + dt.timedelta(minutes=1))       # joins root-1
    s.dispatch.rows["root-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "FLAT"
    assert s.scheduled == ["root-1"]
    assert ledger.transitions == []


def test_liquidate_records_the_pending_command_and_schedules_it(tmp_path):
    ledger = _Ledger({"flatten-ui-1": "RECEIVED"})
    s = _stack(tmp_path, [_snapshot(1, [_position()])], journal=_Journal(), ledger=ledger)
    receipt = s.service.liquidate(SimpleNamespace(account_id=ACCOUNT, command_id="flatten-ui-1"))
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "LIQUIDATION_PENDING")
    assert ledger.transitions[0][:3] == ("flatten-ui-1", "RECEIVED", "OUTCOME_UNKNOWN")
    assert s.scheduled == ["flatten-ui-1"]


def test_disconnect_is_outcome_unknown_and_keeps_breaker_tripped(tmp_path):
    s = _stack(tmp_path, [RuntimeError("IB down")])
    receipt = s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    assert receipt.state == "OUTCOME_UNKNOWN"
    assert not s.dispatch.calls
    assert s.breaker.calls


def test_timeout_never_claims_flat_or_submits_after_deadline(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    receipt = s.service.start(ACCOUNT, "root-1", NOW)
    assert receipt.state == "FAILED_SAFE"
    assert not s.dispatch.calls
    assert s.breaker.calls
    assert s.registry.get("root-1").state == "FAILED_SAFE"


def test_repeated_root_is_idempotent_while_waiting_for_broker_resolution(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    assert [call[0] for call in s.dispatch.calls] == ["reduce"]


@pytest.mark.parametrize(("quantity", "side"), [(10.0, "SELL"), (-7.0, "BUY")])
def test_reduction_never_flips_position(tmp_path, quantity, side):
    s = _stack(tmp_path, [_snapshot(1, [_position(quantity)])])
    s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    _kind, _conid, actual_side, actual_quantity, _child = s.dispatch.calls[0]
    assert actual_side == side
    assert actual_quantity == abs(quantity)


def test_migration_36_adds_safe_close_columns_children_and_joins(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "m.duckdb"))
    migrator = SchemaMigrator(db)
    apply_liquidation_migration(migrator)
    apply_liquidation_migration(migrator)
    assert {35, 36} <= migrator.applied_versions()
    assert db.execute("SELECT name FROM schema_migrations WHERE version = 36", fetch="one") == ("sp1_liquidation_safe_close",)
    run_cols = {r[0] for r in db.execute("DESCRIBE liquidation_runs", fetch="all")}
    assert {"scope", "conid", "goal", "goal_quantity", "phase", "opened_generation", "stop_price",
            "target_price", "remaining_quantity", "escalated", "superseded_by", "cleanup_pending"} <= run_cols
    child_cols = {r[0] for r in db.execute("DESCRIBE liquidation_children", fetch="all")}
    assert {"child_id", "owner_root_id", "kind", "attempt", "state", "fence_generation",
            "target_order_entity_id", "filled_at_send", "observed_generation", "sent_generation",
            "order_entity_id"} <= child_cols
    join_cols = {r[0] for r in db.execute("DESCRIBE liquidation_joins", fetch="all")}
    assert {"command_id", "root_id", "outcome", "requested_goal"} <= join_cols


_LEGACY_RUNS = (("old-flat", "FLAT", 1), ("old-failed", "FAILED_SAFE", 2), ("open-a", "OUTCOME_UNKNOWN", 3),
                ("open-b", "VERIFYING", 4), ("open-c", "REQUESTED", 5))


def _legacy_db(tmp_path, runs=_LEGACY_RUNS):
    """A journal as it was before SP1: migration 25 only, with old runs (root, state, minute)."""
    db = DuckDBConnection.get_instance(str(tmp_path / "liq.duckdb"))
    migrator = SchemaMigrator(db)
    migrator.apply(25, "p1_liquidation_runs", ("""CREATE TABLE IF NOT EXISTS liquidation_runs (
        cause_command_id VARCHAR PRIMARY KEY, account_id VARCHAR NOT NULL, state VARCHAR NOT NULL,
        deadline TIMESTAMPTZ NOT NULL, generation_id BIGINT, detail VARCHAR NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL)""",))
    for root, state, minute in runs:
        db.execute("INSERT INTO liquidation_runs VALUES (?, ?, ?, ?, 1, 'x', ?)",
                   [root, ACCOUNT, state, DEADLINE, NOW + dt.timedelta(minutes=minute)], fetch="none")
    return db


def test_migration_36_adopts_runs_that_were_open_before_the_upgrade(tmp_path):
    """D8: every old run gets a join row; the oldest open run owns the account; the rest are superseded."""
    db = _legacy_db(tmp_path)
    apply_liquidation_migration(SchemaMigrator(db))
    store, registry = LiquidationRunStore(db), ExitOwnerRegistry(db)
    assert all(store.root_for(r) == r for r in ("old-flat", "old-failed", "open-a", "open-b", "open-c"))
    assert registry.account_owner(ACCOUNT).root_id == "open-a"
    assert (registry.get("open-b").state, registry.get("open-c").state) == ("SUPERSEDED", "SUPERSEDED")
    assert registry.get("old-flat") is None and registry.get("old-failed") is None
    assert (store.receipt("open-a").phase, store.receipt("open-b").state,
            store.receipt("open-b").superseded_by) == ("legacy", "SUPERSEDED", "open-a")


def test_an_adopted_run_settles_its_old_reduce_before_any_new_reduce_across_two_restarts(tmp_path):
    """R2-5 / R29: the old service may have sent a reduce; it is an UNKNOWN child, never 'nothing sent'."""
    _legacy_db(tmp_path)
    s = _stack(tmp_path, [_snapshot(5, [_position()]), _snapshot(5, [_position()])])
    s.service.rescan()                                      # adopt: old reduce of conid 1 is UNKNOWN
    adopted = s.service.receipt_for("open-a")
    assert {(c.child_id, c.state, c.sent_generation) for c in adopted.children} == {
        (child, "UNKNOWN", 5) for child in ("open-a-liquidation-reduce-*", "open-b-liquidation-reduce-*",
                                            "open-c-liquidation-reduce-*", "old-failed-liquidation-reduce-*")}
    s.service.rescan()
    s.restart()
    s.push(_snapshot(6, [_position()]))
    s.dispatch.rows["open-a-liquidation-reduce-1"] = [_row("Submitted", entity="open-a-liquidation-reduce-1:exit")]
    assert "still working" in s.service.rescan().detail
    s.restart()
    s.push(_snapshot(7, []), _snapshot(8, []))
    s.dispatch.rows["open-a-liquidation-reduce-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "FLAT"
    assert s.dispatch.calls == []                           # the old reduce closed it; nothing new was sent
    assert s.registry.account_owner(ACCOUNT) is None and s.registry.get("open-b").state == "SUPERSEDED"
    assert s.service.root_for("open-b") == "open-b"         # a retry of the superseded command is bound
    assert s.service.close_resolution("open-b").root_id == "open-a"
    resolution = s.service.close_resolution("open-a")              # the old command resolves, with what sold
    assert (resolution.success, resolution.outcome["filled_quantity"]) == (True, 10.0)


def test_an_invisible_old_reduce_blocks_the_adopted_run_until_its_deadline(tmp_path):
    _legacy_db(tmp_path)
    s = _stack(tmp_path, [_snapshot(5, [_position()]), _snapshot(6, [_position()])])
    s.dispatch.complete = False                             # nothing proves the old reduce absent
    s.service.rescan()
    assert "outcome unknown" in s.service.rescan().detail
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert s.dispatch.calls == []


def test_an_old_reduce_proven_absent_lets_the_adopted_run_close_the_position(tmp_path):
    _legacy_db(tmp_path)
    s = _stack(tmp_path, [_snapshot(5, [_position()]), _snapshot(6, [_position()]), _snapshot(7, [_position()])])
    s.service.rescan()                                      # adopt, fenced on 5
    assert s.service.rescan().children[0].state == "ABSENT"  # generation 6: complete, newer, no row
    s.service.rescan()                                      # generation 7 is newer than that observation
    assert s.dispatch.calls == [("reduce", 1, "SELL", 10.0, "open-a-reduce-1-1")]


def test_store_round_trips_a_run_with_children(tmp_path):
    s = _stack(tmp_path, [_snapshot(1)])
    receipt = LiquidationReceipt(ACCOUNT, "root-7", "REPROTECTING", DEADLINE, generation_id=3, detail="x",
                                 scope="conid", conid=1, goal="partial", goal_quantity=4.0, phase="reprotect",
                                 opened_generation=1, stop_price=95.0, target_price=120.0, escalated=True)
    child = ChildRef("root-7-reprotect-stop-1-1", "root-7", "root-7", ACCOUNT, 1, "reprotect-stop", 1,
                     "UNKNOWN", 3, side="SELL", quantity=6.0, price=95.0, oca_group="root-7-reprotect-1-1",
                     sent_generation=4, order_entity_id="root-7-reprotect-stop-1-1:stop")

    def write(conn):
        s.store.insert_run_in_tx(conn, receipt, NOW)
        s.store.insert_child_in_tx(conn, child, NOW)
    s.store.transaction(write)
    assert s.store.receipt("root-7") == LiquidationReceipt(**{**receipt.__dict__, "children": (child,)})


def test_store_refuses_to_overwrite_a_terminal_or_superseded_run_or_lower_a_goal(tmp_path):
    s = _stack(tmp_path, [_snapshot(1)])
    for state in ("SUPERSEDED", "FLAT"):
        root = f"r-{state}"
        s.store.transaction(lambda conn: s.store.insert_run_in_tx(
            conn, LiquidationReceipt(ACCOUNT, root, state, DEADLINE), NOW))
        with pytest.raises(RunStateError):
            s.store.transaction(lambda conn: s.store.update_run_in_tx(
                conn, LiquidationReceipt(ACCOUNT, root, "CANCELLING", DEADLINE), NOW))
        assert s.store.receipt(root).state == state
    s.store.transaction(lambda conn: s.store.insert_run_in_tx(
        conn, LiquidationReceipt(ACCOUNT, "r-zero", "VERIFYING", DEADLINE, scope="conid", conid=1), NOW))
    with pytest.raises(RunStateError):                       # a goal never goes back to partial
        s.store.transaction(lambda conn: s.store.update_run_in_tx(conn, LiquidationReceipt(
            ACCOUNT, "r-zero", "VERIFYING", DEADLINE, scope="conid", conid=1, goal="partial"), NOW))


def test_two_account_producers_on_one_generation_make_one_root_and_one_reduce(tmp_path):
    """R10 / #23: the second flatten joins the first and creates nothing."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    first = s.service.start(ACCOUNT, "flat-a", DEADLINE)
    joined = s.service.start(ACCOUNT, "flat-b", DEADLINE)
    assert joined.cause_command_id == "flat-a"
    assert s.service.receipt_for("flat-b") is None
    assert s.service.root_for("flat-b") == "flat-a"
    assert first.cause_command_id == "flat-a"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_unseen_child_without_a_complete_enumeration_never_gets_a_second_reduce(tmp_path):
    """D1 (reverses round 1): no row and no complete enumeration keeps the child UNKNOWN to the deadline."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()]),
                          _snapshot(3, [_position()]), _snapshot(4, [_position()])])
    s.dispatch.complete = False
    s.service.start(ACCOUNT, "flat-1", NOW + dt.timedelta(seconds=30))
    for _ in range(3):
        receipt = s.service.rescan()
        assert "outcome unknown" in receipt.detail
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_invisible_child_on_a_generation_opened_before_its_send_stays_unknown(tmp_path):
    """#21: generation 2 began before the send, so its empty lookup proves nothing."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()])])
    s.dispatch.staging = 1                                    # a sync opened generation 2 before the send
    s.service.start(ACCOUNT, "flat-1", NOW + dt.timedelta(seconds=30))
    s.dispatch.staging = 0
    assert s.service.receipt_for("flat-1").children[0].sent_generation == 2
    receipt = s.service.rescan()
    assert (receipt.state, receipt.children[0].state) == ("VERIFYING", "UNKNOWN")
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_absence_needs_a_complete_enumeration_opened_after_the_send(tmp_path):
    """D1: absence is proven only by a complete generation newer than the send fence; then a new attempt."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()]),
                          _snapshot(3, [_position()]), _snapshot(4, [_position()])])
    s.dispatch.staging = 1
    s.service.start(ACCOUNT, "flat-1", DEADLINE)                       # sent fence 2
    s.dispatch.staging = 0
    assert s.service.rescan().children[0].state == "UNKNOWN"           # generation 2 is not newer
    receipt = s.service.rescan()                                       # generation 3: complete, nothing
    assert receipt.children[0].state == "ABSENT"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]              # ABSENT may have filled: wait
    s.service.rescan()                                                 # generation 4 is newer
    assert [c[4] for c in s.dispatch.calls] == ["flat-1-reduce-1-1", "flat-1-reduce-1-2"]


def test_filled_callback_after_position_capture_never_allows_a_second_reduce(tmp_path):
    """#21: the fill is seen after the snapshot that still shows the position."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()]), _snapshot(3, [])])
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    assert s.service.rescan().state == "VERIFYING"            # generation 2: position still 10, fill just seen
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]
    assert s.service.rescan().state == "FLAT"                 # generation 3 proves it
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_a_terminal_status_counts_on_the_same_generation(tmp_path):
    """D2: a Filled row is evidence at once; no newer generation is needed to see it."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(1, [_position()]), _snapshot(2, [])])
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0, entity="flat-1-reduce-1-1:exit")]
    receipt = s.service.rescan()                               # still generation 1
    assert (receipt.children[0].state, receipt.children[0].observed_generation) == ("FILLED", 1)
    assert s.service.rescan().state == "FLAT"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_own_working_reduce_is_never_cancelled_by_its_root(tmp_path):
    """#16: the root's own reduce shows up as a working order; it blocks, it is not cancelled."""
    own = _order("flat-1-reduce-1-1:exit", group="flat-1-reduce-1-1", leg="exit", order_type="MKT")
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()], [own]),
                          _snapshot(3, []), _snapshot(4, [])])
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Submitted", entity=own.order_entity_id)]
    assert "still working" in s.service.rescan().detail
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0, entity=own.order_entity_id)]
    s.service.rescan()
    assert s.service.rescan().state == "FLAT"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_a_known_working_order_is_cancelled_while_another_child_is_unknown(tmp_path):
    """#21: an unknown outcome forbids a new reduce, never a cancel of an identified working order."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()], [_order("ext-2")])])
    s.dispatch.staging = 1
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.staging = 0
    receipt = s.service.rescan()
    assert "outcome unknown" in receipt.detail
    assert [c[0:2] for c in s.dispatch.calls] == [("reduce", 1), ("cancel", "ext-2")]


def test_hand_over_is_updated_before_every_cancel_batch(tmp_path):
    """D6: an order that appears later is handed over before its cancel, so the saga expects it."""
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()], [_order("ext-2")])],
               protection=protection)
    protection.calls = s.dispatch.calls                        # one log for both, in call order
    s.dispatch.staging = 1
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.staging = 0
    s.service.rescan()
    assert [c[0] for c in s.dispatch.calls] == ["handover_account", "reduce", "handover_account", "cancel"]
    assert s.dispatch.calls[2] == ("handover_account", "flat-1", ("ext-2",))


def test_failed_safe_root_does_not_block_rescan_of_a_newer_root(tmp_path):
    """R19: the new root advances; the old root stays FAILED_SAFE."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()])])
    s.service.start(ACCOUNT, "old", NOW)                       # deadline already passed: no snapshot consumed
    assert s.service.receipt_for("old").state == "FAILED_SAFE"
    s.service.start(ACCOUNT, "new", DEADLINE)                  # consumes generation 1: reduce
    advanced = s.service.rescan()                              # consumes generation 2
    assert advanced.cause_command_id == "new"
    assert advanced.generation_id == 2
    assert s.service.receipt_for("old").state == "FAILED_SAFE"


def test_new_flatten_inherits_the_unknown_child_of_a_failed_safe_root(tmp_path):
    """R9: a later owner obeys R5 for the unknown children it inherits."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    s.dispatch.staging = 1
    s.service.start(ACCOUNT, "flat-1", NOW + dt.timedelta(seconds=30))
    s.dispatch.staging = 0
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    s.push(_snapshot(2, [_position()]))
    receipt = s.service.start(ACCOUNT, "flat-2", NOW + dt.timedelta(minutes=5))
    assert [(c.child_id, c.owner_root_id, c.state) for c in receipt.children] == [
        ("flat-1-reduce-1-1", "flat-2", "UNKNOWN")]
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_flat_releases_the_account_owner_and_a_later_flatten_claims_again_after_restart(tmp_path):
    """R8 / #23: FLAT releases the owner; after a restart a new flatten is a new root."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, [])])
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "FLAT"
    assert s.registry.get("flat-1").state == "RELEASED"
    assert s.service.receipt_for("flat-1").cleanup_pending is False
    service = s.restart()
    s.push(_snapshot(4, [_position(5.0)]))
    receipt = service.start(ACCOUNT, "flat-2", DEADLINE)
    assert receipt.cause_command_id == "flat-2"
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 5.0, "flat-2-reduce-1-1")


def test_crash_after_the_broker_call_never_sends_a_second_reduce(tmp_path):
    """R20: the order left, the process died before the fence was stored; the restart only observes it."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    real_reduce = s.dispatch.reduce

    def sent_then_crash(*args):
        real_reduce(*args)
        raise _Crash()
    s.dispatch.reduce = sent_then_crash
    with pytest.raises(_Crash):
        s.service.start(ACCOUNT, "flat-1", DEADLINE)
    assert s.store.receipt("flat-1").children[0].sent_generation is None
    s.dispatch.reduce = real_reduce
    service = s.restart()
    s.push(_snapshot(2, [_position()]), _snapshot(3, [_position()]), _snapshot(4, []))
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Submitted")]
    assert "still working" in service.rescan().detail
    assert service.receipt_for("flat-1").children[0].sent_generation == 2   # fenced by the restart
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    service.rescan()                                        # generation 3: the fill is seen
    assert service.rescan().state == "FLAT"                 # generation 4
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_crash_between_journal_and_broker_call_is_proven_absent_before_a_new_attempt(tmp_path):
    """R20: the child was journaled, the broker call never ran; a complete newer enumeration proves it."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])

    def crash(*_args):
        raise _Crash()
    s.service._still_dispatchable = crash
    with pytest.raises(_Crash):
        s.service.start(ACCOUNT, "flat-1", DEADLINE)
    service = s.restart()
    s.push(_snapshot(2, [_position()]), _snapshot(3, [_position()]), _snapshot(4, [_position()]))
    assert service.rescan().children[0].state == "UNKNOWN"     # fenced on generation 2 by the restart
    assert service.rescan().children[0].state == "ABSENT"      # generation 3 is complete and newer
    service.rescan()
    assert s.dispatch.calls == [("reduce", 1, "SELL", 10.0, "flat-1-reduce-1-2")]


def test_crash_after_a_cancel_was_journaled_sends_a_new_cancel(tmp_path):
    """#20 livelock: a cancel cut off before its send is NOT_SENT after restart; a new cancel goes out."""
    s = _stack(tmp_path, [_snapshot(1, [_position()], [_order()])])

    def crash(*_args):
        raise _Crash()
    s.service._still_dispatchable = crash
    with pytest.raises(_Crash):
        s.service.start(ACCOUNT, "root-1", DEADLINE)
    service = s.restart()
    s.push(_snapshot(2, [_position()], [_order()]))
    s.dispatch.entities["external-1"] = _order()               # the order is still working
    receipt = service.rescan()
    assert [(c.child_id, c.state) for c in receipt.children] == [
        ("root-1-cancel-1-1", "NOT_SENT"), ("root-1-cancel-1-2", "UNKNOWN")]
    assert s.dispatch.calls == [("cancel", "external-1", "root-1-cancel-1-2")]


def test_refusal_before_the_boundary_is_not_sent_and_may_be_retried_with_a_new_id(tmp_path):
    """R2/R3: only a proven pre-submit refusal is NOT_SENT; the next attempt has a new id."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()])])
    s.dispatch.refuse.add("reduce")
    receipt = s.service.start(ACCOUNT, "flat-1", DEADLINE)
    assert receipt.children[0].state == "NOT_SENT"
    s.dispatch.refuse.clear()
    s.service.rescan()
    assert s.dispatch.calls == [("reduce", 1, "SELL", 10.0, "flat-1-reduce-1-2")]


def test_timeout_after_the_boundary_stays_unknown_and_is_never_resent(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()]), _snapshot(3, [_position()])])
    s.dispatch.fail_after_send.add("reduce")
    receipt = s.service.start(ACCOUNT, "flat-1", DEADLINE)
    assert (receipt.children[0].state, receipt.children[0].sent_generation) == ("UNKNOWN", 1)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Submitted")]   # it did reach the broker
    s.service.rescan()
    s.service.rescan()
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_an_unexpected_send_error_is_logged_and_the_child_stays_unknown(tmp_path, caplog):
    """D13: every exception is logged; only DispatchRefused proves nothing left."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])

    def broken(*_args):
        raise AttributeError("a bug after placeOrder")
    s.dispatch.reduce = broken
    with caplog.at_level(logging.ERROR, logger="trader.trading.liquidation_service"):
        receipt = s.service.start(ACCOUNT, "flat-1", DEADLINE)
    assert receipt.children[0].state == "UNKNOWN"
    assert any("flat-1-reduce-1-1" in r.getMessage() and r.exc_info for r in caplog.records)


def test_a_root_superseded_between_journal_and_send_sends_nothing(tmp_path):
    """R7: the owner is re-read right before the broker call."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    real = s.service._still_dispatchable

    def superseded_first(root_id, goal):
        s.db.execute("UPDATE exit_owners SET state = 'SUPERSEDED' WHERE root_id = ?", [root_id], fetch="none")
        return real(root_id, goal)
    s.service._still_dispatchable = superseded_first
    receipt = s.service.start(ACCOUNT, "flat-1", DEADLINE)
    assert receipt.children[0].state == "NOT_SENT"
    assert s.dispatch.calls == []


@pytest.mark.parametrize("step", ["terminal", "protection"])
def test_restart_after_a_crash_at_the_end_finishes_the_root(tmp_path, step):
    """R8 / R20 / D3: terminal state and owner release commit together; the saga step is recovered."""
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, [])], protection=protection)
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    if step == "terminal":
        real = s.registry.finish_in_tx

        def crash_on_release(conn, root_id, state, now):
            raise _Crash()
        s.registry.finish_in_tx = crash_on_release
    else:
        protection.crash_on.add("close_after_full")
    with pytest.raises(_Crash):
        s.service.rescan()
    stored = s.store.receipt("flat-1")
    if step == "terminal":                                   # the whole terminal transaction rolled back
        assert (stored.state, s.registry.get("flat-1").state) == ("VERIFYING", "ACTIVE")
    else:                                                    # terminal and release committed together
        assert (stored.state, stored.cleanup_pending, s.registry.get("flat-1").state) == ("FLAT", True, "RELEASED")
    service = s.restart()
    service.rescan()
    assert (s.store.receipt("flat-1").state, s.store.receipt("flat-1").cleanup_pending) == ("FLAT", False)
    assert s.registry.get("flat-1").state == "RELEASED"
    assert ("close_after_full", "flat-1") in protection.calls


def test_a_flatten_during_unfinished_cleanup_starts_a_new_root(tmp_path):
    """D3: a finished root no longer owns the account, even before its cleanup ran."""
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, []),
                          _snapshot(4, [_position(3.0)])], protection=protection)
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    protection.crash_on.add("close_after_full")
    with pytest.raises(_Crash):
        s.service.rescan()
    receipt = s.service.start(ACCOUNT, "flat-2", DEADLINE)
    assert receipt.cause_command_id == "flat-2"
    assert s.service.root_for("flat-2") == "flat-2"
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 3.0, "flat-2-reduce-1-1")


# ---------------------------------------------------------------------------
# Kept from master (PR #42): the timed lock guards a caller that bypasses the worker
# ---------------------------------------------------------------------------

class _FirstCaptureBlocks(_Broker):
    """The first capture waits for ``release``; later captures return at once."""
    def __init__(self, snapshots):
        super().__init__(snapshots)
        self.first_entered, self.release = threading.Event(), threading.Event()

    def capture(self, account_id):
        if self.calls == 0:
            self.first_entered.set()
            assert self.release.wait(5.0)
        return super().capture(account_id)


def _hold_lock(service):
    """Hold the service lock from another thread until ``release`` is set."""
    held, release = threading.Event(), threading.Event()

    def hold():
        with service._lock:
            held.set()
            release.wait(5.0)
    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(2.0)
    return holder, release


def test_start_and_rescan_serialize_across_threads(tmp_path):
    s = _stack(tmp_path, [])
    s.broker = _FirstCaptureBlocks([_snapshot(1, [_position()])])
    s.dispatch = _Dispatch(s.broker)
    s.service = s._build()
    results = {}
    a = threading.Thread(target=lambda: results.setdefault("a", s.service.start(ACCOUNT, "root-1", DEADLINE)))
    a.start()
    assert s.broker.first_entered.wait(2.0)
    b = threading.Thread(target=lambda: results.setdefault("b", s.service.rescan()))
    b.start()
    b.join(0.2)              # without the lock B would size a reduce now, while A is still capturing
    s.broker.release.set()
    a.join(2.0)
    b.join(2.0)
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]
    assert results["a"].state == results["b"].state == "VERIFYING"


def test_busy_start_commits_its_claim_and_a_later_rescan_advances_it(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()])], lock_timeout_seconds=0.05)
    holder, release = _hold_lock(s.service)
    try:
        started = time.monotonic()
        with pytest.raises(LiquidationBusy):
            s.service.start(ACCOUNT, "saga-root", DEADLINE)
        with pytest.raises(LiquidationBusy):
            s.service.rescan()
        assert time.monotonic() - started < 1.0
        assert s.dispatch.calls == []
    finally:
        release.set()
        holder.join(2.0)
    assert s.registry.account_owner(ACCOUNT).root_id == "saga-root"
    receipt = s.service.rescan()
    assert (receipt.cause_command_id, receipt.state) == ("saga-root", "VERIFYING")
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_busy_flatten_records_the_pending_root_and_surfaces_busy(tmp_path):
    ledger = _Ledger({"flatten-1": "RECEIVED"})
    s = _stack(tmp_path, [_snapshot(1, [_position()])], journal=_Journal(), ledger=ledger,
               lock_timeout_seconds=0.05)
    holder, release = _hold_lock(s.service)
    try:
        with pytest.raises(LiquidationBusy):
            s.service.liquidate(SimpleNamespace(account_id=ACCOUNT, command_id="flatten-1"))
    finally:
        release.set()
        holder.join(2.0)
    assert ledger.transitions[0][:3] == ("flatten-1", "RECEIVED", "OUTCOME_UNKNOWN")
    assert ledger.transitions[0][3]["outcome"]["liquidation_state"] == "REQUESTED"
    assert s.scheduled == ["flatten-1"]
    assert s.service.rescan().state == "VERIFYING"


def test_root_bound_to_one_account_rejects_another_account(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    s.service.start(ACCOUNT, "root-1", DEADLINE)
    with pytest.raises(ValueError):
        s.service.start("DU999", "root-1", DEADLINE)


def test_rescan_returns_none_when_only_failed_safe_roots_remain(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    s.service.start(ACCOUNT, "old", NOW)                       # deadline already passed: FAILED_SAFE
    captured = s.broker.calls
    assert s.service.rescan() is None
    assert s.broker.calls == captured                          # nothing left to advance: no capture


# ---------------------------------------------------------------------------
# Round-2 verification fixes (N2, R2-5): every pre-upgrade run's old reduce is tracked
# ---------------------------------------------------------------------------

_OLD_REDUCES = {"open-a-liquidation-reduce-*", "open-b-liquidation-reduce-*",
                "open-c-liquidation-reduce-*", "old-failed-liquidation-reduce-*"}


def test_a_superseded_legacy_runs_invisible_reduce_blocks_the_adopted_root(tmp_path):
    """N2: before the upgrade two flattens could each send a reduce. The adopted root settles its
    own old reduce, but the superseded and FAILED_SAFE runs' old reduces still block a new one."""
    _legacy_db(tmp_path)
    s = _stack(tmp_path, [_snapshot(5, [_position()]), _snapshot(6, [_position()]), _snapshot(7, [_position()])])
    s.dispatch.complete = False                              # nothing proves an unseen old reduce absent
    s.dispatch.rows["open-a-liquidation-reduce-1"] = [_row("Cancelled", entity="open-a-liquidation-reduce-1:exit")]
    s.service.rescan()                                       # adopt at 5
    assert {c.child_id for c in s.service.receipt_for("open-a").children} == _OLD_REDUCES
    s.service.rescan()                                       # 6: open-a's old reduce is CANCELLED
    s.service.rescan()                                       # 7: the others are still unknown
    assert s.dispatch.calls == []
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert s.dispatch.calls == []


def test_a_legacy_run_whose_deadline_passed_fails_safe_and_a_new_flatten_inherits_its_old_reduces(tmp_path):
    """R2-5 gap: a real upgrade finds the old deadline passed. Nothing is sent, the breaker trips,
    and the next flatten inherits every old reduce as UNKNOWN before it may send its own."""
    _legacy_db(tmp_path)
    s = _stack(tmp_path, [_snapshot(5, [_position()])])
    s.dispatch.complete = False
    s.clock["now"] = DEADLINE + dt.timedelta(minutes=1)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert s.dispatch.calls == [] and s.breaker.calls
    assert s.registry.get("open-a").state == "FAILED_SAFE"
    receipt = s.service.start(ACCOUNT, "flat-new", s.clock["now"] + dt.timedelta(minutes=5))
    assert {(c.child_id, c.owner_root_id, c.state) for c in receipt.children} == {
        (child, "flat-new", "UNKNOWN") for child in _OLD_REDUCES}
    assert s.dispatch.calls == []


def test_old_failed_safe_runs_reduces_are_tracked_even_with_no_run_open_at_the_upgrade(tmp_path):
    """N2: only FAILED_SAFE runs survived the upgrade; the first flatten after it still tracks them."""
    db = _legacy_db(tmp_path)
    db.execute("DELETE FROM liquidation_runs WHERE cause_command_id LIKE 'open-%'", fetch="none")
    s = _stack(tmp_path, [_snapshot(5, [_position()]), _snapshot(6, [_position()]), _snapshot(7, [_position()])])
    receipt = s.service.start(ACCOUNT, "flat-new", DEADLINE)
    assert [(c.child_id, c.state) for c in receipt.children] == [("old-failed-liquidation-reduce-*", "UNKNOWN")]
    assert s.dispatch.calls == []
    s.service.rescan()                                       # 6: complete and newer, no row: ABSENT
    s.service.rescan()                                       # 7: newer than that observation: reduce
    assert s.dispatch.calls == [("reduce", 1, "SELL", 10.0, "flat-new-reduce-1-1")]

# ---------------------------------------------------------------------------
# Round-3 fix (#20, ruling 42): a pre-upgrade run's reduces are one wildcard child
# ---------------------------------------------------------------------------

def _pre_sp1_open(db, run_id):
    return db.execute("SELECT pre_sp1_open FROM liquidation_runs WHERE cause_command_id = ?",
                      [run_id], fetch="one")[0]


def test_an_old_reduce_of_a_flat_position_blocks_the_adopted_run_until_it_settles(tmp_path):
    """Ruling 42: the old reduce of a position already at 0 is not in working_orders yet. The run's
    wildcard child blocks; its mark stays until a complete, newer enumeration settles it."""
    _legacy_db(tmp_path, (("flat-1", "VERIFYING", 1),))
    s = _stack(tmp_path, [_snapshot(5, [_position(0.0)]), _snapshot(6, [_position(0.0)])])
    s.dispatch.complete = False                              # the old order is not visible yet
    s.service.rescan()                                       # adopt at 5
    receipt = s.service.rescan()                             # 6
    assert [(c.child_id, c.conid, c.state) for c in receipt.children] == [
        ("flat-1-liquidation-reduce-*", None, "UNKNOWN")]
    assert receipt.state not in ("FLAT", "CLOSED") and "outcome unknown" in receipt.detail
    assert _pre_sp1_open(s.db, "flat-1")
    s.push(_snapshot(7, [_position(0.0)]))
    s.dispatch.rows["flat-1-liquidation-reduce-1"] = [_row("Submitted", entity="flat-1-liquidation-reduce-1:exit")]
    assert "still working" in s.service.rescan().detail     # visible and working: waited on, not cancelled
    assert _pre_sp1_open(s.db, "flat-1")
    s.push(_snapshot(8, [_position(0.0)]))
    s.dispatch.rows["flat-1-liquidation-reduce-1"] = [_row("Cancelled", entity="flat-1-liquidation-reduce-1:exit")]
    s.dispatch.complete = True
    assert s.service.rescan().state == "FLAT"               # 8: complete and newer than the fence
    assert s.service.receipt_for("flat-1").children[0].state == "CANCELLED"
    assert not _pre_sp1_open(s.db, "flat-1")
    assert s.dispatch.calls == []


def test_an_old_reduce_that_stays_invisible_blocks_to_the_deadline(tmp_path):
    _legacy_db(tmp_path, (("flat-1", "VERIFYING", 1),))
    s = _stack(tmp_path, [_snapshot(5, [_position(0.0)]), _snapshot(6, [_position(0.0)]),
                          _snapshot(7, [_position(0.0)])])
    s.dispatch.complete = False
    s.service.rescan()
    s.service.rescan()
    assert "outcome unknown" in s.service.rescan().detail
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert s.dispatch.calls == [] and _pre_sp1_open(s.db, "flat-1")


# ---------------------------------------------------------------------------
# Round-3 fix (#20, ruling 43): a fill fence outlives the root that observed it
# ---------------------------------------------------------------------------

def test_a_fill_seen_on_the_deadline_tick_fences_the_next_root(tmp_path):
    """Ruling 43: root-1's reduce sold 6 on the tick its deadline passed; the position cache still says 10.
    root-2 claimed on that generation sends nothing; a newer generation shows the real remainder."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    s.service.start(ACCOUNT, "root-1", DEADLINE)
    s.dispatch.rows["root-1-reduce-1-1"] = [_row("Cancelled", filled=6.0)]
    s.push(_snapshot(2, [_position()]))
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    assert s.service.rescan().state == "FAILED_SAFE"         # gen 2: the fill is observed, then the deadline
    receipt = s.service.start(ACCOUNT, "root-2", s.clock["now"] + dt.timedelta(minutes=5))
    assert "newer than the last observed fill" in receipt.detail
    assert s.dispatch.calls == [("reduce", 1, "SELL", 10.0, "root-1-reduce-1-1")]
    s.push(_snapshot(3, [_position(4.0)]))
    s.service.rescan()
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 4.0, "root-2-reduce-1-1")


def test_a_settled_legacy_fill_fences_the_next_root_across_a_restart(tmp_path):
    """Ruling 43: the old run's late reduce filled 10 of 20; the root that settled it dies at its deadline.
    After a restart the next root waits for a generation newer than that fill."""
    _legacy_db(tmp_path, (("flat-1", "FAILED_SAFE", 1),))
    s = _stack(tmp_path, [_snapshot(5, [_position(20.0)])])
    s.service.start(ACCOUNT, "root-1", DEADLINE)
    s.dispatch.rows["flat-1-liquidation-reduce-1"] = [_row("Filled", filled=10.0)]
    s.push(_snapshot(6, [_position(20.0)]))
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert not _pre_sp1_open(s.db, "flat-1")                 # settled FILLED on generation 6
    s.restart()
    receipt = s.service.start(ACCOUNT, "root-2", s.clock["now"] + dt.timedelta(minutes=5))
    assert "newer than the last observed fill" in receipt.detail and s.dispatch.calls == []
    s.push(_snapshot(7, [_position(10.0)]))
    s.service.rescan()
    assert s.dispatch.calls == [("reduce", 1, "SELL", 10.0, "root-2-reduce-1-1")]


# ---------------------------------------------------------------------------
# Task 5: conid-scoped full close
# ---------------------------------------------------------------------------

def _stop_order(entity="stop-1", conid=1, group="og-entry-1", quantity=10.0):
    return BrokerOrderRow(
        order_entity_id=entity, account_id=ACCOUNT, conid=conid, symbol="AAPL",
        order_group_id=group, leg="stop", is_external=False, action="SELL", order_type="STP",
        total_quantity=quantity, filled_quantity=0, avg_fill_price=None, limit_price=None,
        stop_price=95.0, tif="DAY", status="Submitted", deleted=False, revision=1,
        source_timestamp=NOW,
    )


def test_full_close_hands_over_then_cancels_only_that_conids_orders(tmp_path):
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_position(), _position(5.0, conid=2)],
                                    [_stop_order(), _stop_order("stop-2", conid=2, group="og-entry-2")])],
               protection=protection)
    receipt = s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    assert protection.calls[0] == ("handover", 1, "close-1", ("stop-1",))
    assert (receipt.state, receipt.phase) == ("VERIFYING", "cancel")
    assert s.dispatch.calls == [("cancel", "stop-1", "close-1-cancel-1-1")]
    child = receipt.children[0]
    assert (child.kind, child.target_order_entity_id, child.fence_generation) == ("cancel", "stop-1", 1)
    assert s.breaker.calls == []


def test_full_close_reduces_only_after_the_cancel_is_terminal(tmp_path):
    """D2: the stop's own row is the evidence. Cancelled with no fill allows the reduce, even on the same generation."""
    s = _stack(tmp_path, [
        _snapshot(1, [_position()], [_stop_order()]),
        _snapshot(1, [_position()], [_stop_order()]),
        _snapshot(1, [_position()], []),
    ], protection=_Protection())
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.entities["stop-1"] = _stop_order()                # the cancel has not landed yet
    assert "still working" in s.service.rescan().detail
    assert [c[0] for c in s.dispatch.calls] == ["cancel"]
    s.dispatch.entities["stop-1"] = _row("Cancelled")
    receipt = s.service.rescan()
    assert receipt.phase == "reduce"
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 10.0, "close-1-reduce-1-1")


def test_full_close_waits_while_the_cancelled_stop_is_invisible(tmp_path):
    """#21: the stop's row is gone on a generation that opened before the cancel; that proves nothing."""
    s = _stack(tmp_path, [_snapshot(1, [_position()], [_stop_order()]), _snapshot(2, [_position()], [])],
               protection=_Protection())
    s.dispatch.staging = 1
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.staging = 0
    receipt = s.service.rescan()
    assert "outcome unknown" in receipt.detail
    assert [c[0] for c in s.dispatch.calls] == ["cancel"]


def test_invisible_reduce_child_on_a_newer_generation_gets_no_second_scoped_reduce(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()])], protection=_Protection())
    s.dispatch.staging = 1
    s.service.start(ACCOUNT, "close-1", NOW + dt.timedelta(seconds=30), scope="conid", conid=1)
    s.dispatch.staging = 0
    s.service.rescan()
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_filled_callback_after_position_capture_gets_no_second_scoped_reduce(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()]), _snapshot(3, [])],
               protection=_Protection())
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.rows["close-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "CLOSED"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_full_close_ends_closed_and_releases_owner_and_saga(tmp_path):
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, [])], protection=protection)
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.rows["close-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "CLOSED"
    assert protection.calls[-1] == ("close_after_full", "close-1")
    assert s.registry.get("close-1").state == "RELEASED"
    assert s.breaker.calls == []


def test_close_ends_closed_without_reduce_when_the_stop_filled_in_the_cancel_race(tmp_path):
    """Review focus 3."""
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_position()], [_stop_order()]), _snapshot(2, [], []), _snapshot(3, [], [])],
               protection=protection)
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.entities["stop-1"] = _row("Filled", filled=10.0)
    assert s.service.rescan().state == "VERIFYING"           # the stop fill is a fill: wait one more generation
    assert s.service.rescan().state == "CLOSED"
    assert [c[0] for c in s.dispatch.calls] == ["cancel"]
    assert ("close_after_full", "close-1") in protection.calls


def test_full_close_of_short_reduces_with_buy(tmp_path):
    """Review focus 1."""
    s = _stack(tmp_path, [_snapshot(1, [_position(-7.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    assert s.dispatch.calls == [("reduce", 1, "BUY", 7.0, "close-1-reduce-1-1")]


def test_full_close_ignores_another_conids_position(tmp_path):
    other = _position(5.0, conid=2)
    s = _stack(tmp_path, [_snapshot(1, [_position(), other]), _snapshot(2, [other]), _snapshot(3, [other])],
               protection=_Protection())
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.rows["close-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "CLOSED"
    assert all(c[1] == 1 for c in s.dispatch.calls)


def test_scoped_close_deadline_is_failed_safe_and_trips_breaker(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()])], protection=_Protection())
    receipt = s.service.start(ACCOUNT, "close-1", NOW, scope="conid", conid=1)
    assert receipt.state == "FAILED_SAFE"
    assert s.dispatch.calls == []
    assert s.breaker.calls == [("close-1", receipt.detail)]


def test_cancel_rejected_by_the_broker_ends_failed_safe_without_reduce(tmp_path):
    """Spec test list: cancel rejected. The stop stays working; no reduce is ever sent."""
    s = _stack(tmp_path, [_snapshot(1, [_position()], [_stop_order()]), _snapshot(2, [_position()], [_stop_order()])],
               protection=_Protection())
    s.service.start(ACCOUNT, "close-1", NOW + dt.timedelta(seconds=30), scope="conid", conid=1)
    s.dispatch.entities["stop-1"] = _row("Submitted")
    assert "still working" in s.service.rescan().detail
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert [c[0] for c in s.dispatch.calls] == ["cancel"]


def test_routine_scoped_progress_never_trips_the_breaker(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()], [_stop_order()]), _snapshot(2, [_position()], []),
                          _snapshot(3, []), _snapshot(4, [])], protection=_Protection())
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.entities["stop-1"] = _row("Cancelled")
    s.service.rescan()
    s.dispatch.rows["close-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "CLOSED"
    assert s.breaker.calls == []


def test_start_refuses_rebinding_root_to_another_scope(tmp_path):
    """Review focus 4."""
    s = _stack(tmp_path, [_snapshot(1, [_position(), _position(5.0, conid=2)])], protection=_Protection())
    s.service.start(ACCOUNT, "root-1", DEADLINE)
    with pytest.raises(ValueError):
        s.service.start(ACCOUNT, "root-1", DEADLINE, scope="conid", conid=1)
    s.service.start(ACCOUNT, "root-2", DEADLINE, scope="conid", conid=2)
    with pytest.raises(ValueError):
        s.service.start(ACCOUNT, "root-2", DEADLINE, scope="conid", conid=1)


def test_an_old_reduce_of_a_flat_position_blocks_a_scoped_close_of_that_conid(tmp_path):
    _legacy_db(tmp_path, (("flat-1", "FAILED_SAFE", 1),))
    s = _stack(tmp_path, [_snapshot(5, [_position(0.0)]), _snapshot(6, [_position(0.0)])])
    s.dispatch.complete = False
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    receipt = s.service.rescan()
    assert [(c.child_id, c.owner_root_id, c.state) for c in receipt.children] == [
        ("flat-1-liquidation-reduce-*", "close-1", "UNKNOWN")]
    assert receipt.state != "CLOSED" and _pre_sp1_open(s.db, "flat-1")
    s.push(_snapshot(7, [_position(0.0)]))
    s.dispatch.rows["flat-1-liquidation-reduce-1"] = [_row("Submitted", entity="flat-1-liquidation-reduce-1:exit")]
    assert "still working" in s.service.rescan().detail
    s.push(_snapshot(8, [_position(-10.0)]), _snapshot(9, [_position(-10.0)]))
    s.dispatch.rows["flat-1-liquidation-reduce-1"] = [
        _row("Filled", filled=10.0, entity="flat-1-liquidation-reduce-1:exit")]
    s.dispatch.complete = True
    s.service.rescan()                                       # 8: settled FILLED; the late fill opened a short
    assert not _pre_sp1_open(s.db, "flat-1")
    assert s.dispatch.calls == []                            # 8 is not newer than the fill it observed
    s.service.rescan()                                       # 9: the close reduces the short it now sees
    assert s.dispatch.calls == [("reduce", 1, "BUY", 10.0, "close-1-reduce-1-1")]


def test_the_wildcard_blocks_a_scoped_close_of_another_conid(tmp_path):
    """Ruling 42: the scoped root on conid 2 never looks at conid 1. The old run's reduce of conid 2
    is settled, but its reduce of conid 1 is not, so the run is not settled and nothing is reduced."""
    _legacy_db(tmp_path, (("flat-1", "FAILED_SAFE", 1),))
    held = [_position(0.0), _position(10.0, conid=2)]
    s = _stack(tmp_path, [_snapshot(5, held), _snapshot(6, held), _snapshot(7, held)])
    s.dispatch.complete = False
    s.dispatch.rows["flat-1-liquidation-reduce-2"] = [_row("Cancelled", entity="flat-1-liquidation-reduce-2:exit")]
    s.service.start(ACCOUNT, "close-2", DEADLINE, scope="conid", conid=2)
    s.service.rescan()
    s.service.rescan()
    assert s.dispatch.calls == []
    assert _pre_sp1_open(s.db, "flat-1")


# ---------------------------------------------------------------------------
# Task 6: partial close, re-protect, escalation
# ---------------------------------------------------------------------------

from trader.trading.liquidation_service import LiquidationRefused  # noqa: E402


def _priced(quantity=10.0, conid=1, price=100.0):
    return _position(quantity, conid=conid, market_price=price)


def _leg_row(status="Submitted", total=6.0, filled=0.0, group="p-1-reprotect-1-1", oca_type=2, action="SELL",
             entity=None):
    """A re-protect leg's own broker row, with the OCA link the broker reports (Task 18)."""
    return _row(status, filled=filled, total=total, oca_group=group, oca_type=oca_type, action=action, entity=entity)


def _to_reprotect(tmp_path, *, target=120.0, stop=95.0, held=10.0, q=4.0, extra=()):
    """Partial close of `q` that reaches REPROTECTING with the stop leg sent (generation 3)."""
    left = held - q
    protection = _Protection(stop_price=stop, target_price=target)
    s = _stack(tmp_path, [_snapshot(1, [_priced(held)]), _snapshot(1, [_priced(held)]), _snapshot(2, [_priced(left)]),
                          _snapshot(3, [_priced(left)]), *extra], protection=protection)
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=q)      # gen 1: admit + partial reduce
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=q, total=q)]
    s.service.rescan()                                                                 # gen 2: fill observed
    s.service.rescan()                                                                 # gen 3: fresh -> stop leg
    return s, protection


def test_partial_quantity_edge_cases(tmp_path):
    """Review focus 2 / R15."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0), _priced(10.5, conid=2)])], protection=_Protection())
    with pytest.raises(LiquidationRefused) as ex:
        s.service.start(ACCOUNT, "p-a", DEADLINE, scope="conid", conid=1, quantity=0.4)
    assert ex.value.code == "PARTIAL_QUANTITY_INVALID"
    for q in (10.0, 12.0):
        with pytest.raises(LiquidationRefused) as ex:
            s.service.start(ACCOUNT, f"p-{q:g}", DEADLINE, scope="conid", conid=1, quantity=q)
        assert ex.value.code == "QUANTITY_ABOVE_POSITION"
    assert s.registry.owner_for(ACCOUNT, 1) is None
    receipt = s.service.start(ACCOUNT, "p-b", DEADLINE, scope="conid", conid=1, quantity=4.7)
    assert (receipt.goal, receipt.goal_quantity) == ("partial", 4.0)
    full = s.service.start(ACCOUNT, "p-c", DEADLINE, scope="conid", conid=2, quantity=10.0)
    assert (full.goal, full.goal_quantity) == ("zero", None)          # 0.5 share would remain


def test_live_position_at_or_below_q_at_dispatch_is_fully_closed(tmp_path):
    """R15: the stop sold 7 of 10 during the cancel; the live 3 <= q=4, so close all 3."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)], [_stop_order()]), _snapshot(1, [_priced(10.0)], [_stop_order()]), _snapshot(2, [_priced(3.0)]),
                          _snapshot(3, [_priced(3.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.entities["stop-1"] = _row("Cancelled", filled=7.0)
    s.service.rescan()
    receipt = s.service.rescan()
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 3.0, "p-1-reduce-1-1")
    assert receipt.goal == "zero"
    assert s.registry.get("p-1").goal == "zero"


def test_partial_close_sends_stop_then_target_and_ends_done(tmp_path):
    """R13: the target is sent only after the stop is accepted, sized from the live position."""
    s, protection = _to_reprotect(tmp_path, extra=(_snapshot(4, [_priced(6.0)]), _snapshot(5, [_priced(6.0)])))
    assert s.dispatch.calls[0] == ("reduce_partial", 1, "SELL", 4.0, "p-1-reduce-1-1")
    assert ("expect_reprotect", "p-1", ("p-1-reprotect-stop-1-1", "p-1-reprotect-target-1-1")) in protection.calls
    assert s.dispatch.calls[-1] == ("place_exit_leg", 1, "stop", 6.0, 95.0, "p-1-reprotect-1-1",
                                    "p-1-reprotect-stop-1-1")
    receipt = s.service.receipt_for("p-1")
    assert (receipt.state, receipt.phase) == ("VERIFYING", "reprotect")
    assert [c.state for c in receipt.children if c.kind == "reprotect-target"] == ["PLANNED"]
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    s.service.rescan()                                                    # gen 4: stop working -> target
    assert s.dispatch.calls[-1] == ("place_exit_leg", 1, "target", 6.0, 120.0, "p-1-reprotect-1-1",
                                    "p-1-reprotect-target-1-1")
    s.dispatch.rows["p-1-reprotect-target-1-1"] = [_leg_row()]
    receipt = s.service.rescan()                                          # gen 5: both working
    assert receipt.state == "DONE"
    assert protection.calls[-1] == ("release_after_partial", "p-1", 6.0, "p-1-reprotect-stop-1-1",
                                    "p-1-reprotect-target-1-1")
    assert s.registry.get("p-1").state == "RELEASED"
    assert s.breaker.calls == []
    outcome = s.service.close_resolution("p-1").outcome                  # what was asked, sold and kept
    assert (outcome["requested_quantity"], outcome["filled_quantity"], outcome["remaining_quantity"]) == (4.0, 4.0, 6.0)


def _both_legs_working_next(tmp_path, *stop_answers):
    """Partial close at generation 5 with both legs sent; the stop's row answers come in order."""
    s, _ = _to_reprotect(tmp_path, extra=(_snapshot(4, [_priced(6.0)]), _snapshot(5, [_priced(6.0)]),
                                          _snapshot(6, [_priced(6.0)])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    s.service.rescan()                                                    # gen 4: stop working -> target
    s.dispatch.rows["p-1-reprotect-target-1-1"] = [_leg_row()]
    s.dispatch.sequences["p-1-reprotect-stop-1-1"] = [list(answer) for answer in stop_answers]
    return s


def test_a_stop_cancelled_after_it_was_observed_never_ends_done(tmp_path):
    """#22: the stop was WORKING when observed, then ingest saw it Cancelled; DONE is not decided from
    the old child status with the fresh row's OCA link."""
    s = _both_legs_working_next(tmp_path, [_leg_row()], [_leg_row("Cancelled")])
    receipt = s.service.rescan()                                          # gen 5
    assert receipt.state != "DONE" and s.service.close_resolution("p-1") is None
    stop = next(c for c in receipt.children if c.kind == "reprotect-stop")
    assert stop.state == "CANCELLED"                                     # observed again, not finished
    receipt = s.service.rescan()                                          # gen 6: a failed re-protect
    assert receipt.state != "DONE" and receipt.goal == "zero"


def test_a_stop_fill_after_it_was_observed_never_ends_done(tmp_path):
    """#22: ingest saw 2 of the stop's 6 fill after it was observed; outstanding is 4, not 6."""
    s = _both_legs_working_next(tmp_path, [_leg_row()], [_leg_row(filled=2.0)])
    receipt = s.service.rescan()
    assert receipt.state != "DONE" and s.service.close_resolution("p-1") is None
    stop = next(c for c in receipt.children if c.kind == "reprotect-stop")
    assert (stop.filled_quantity, stop.outstanding_quantity) == (2.0, 4.0)


def test_a_leg_row_that_changes_while_done_is_decided_is_read_again(tmp_path):
    """#22: the rows DONE was decided on must still be the rows at the terminal write."""
    s = _both_legs_working_next(tmp_path, [_leg_row()], [_leg_row()], [_leg_row("Cancelled")])
    receipt = s.service.rescan()
    assert receipt.state != "DONE" and s.service.close_resolution("p-1") is None


def test_terminal_partial_fill_reprotects_the_actual_remainder(tmp_path):
    """#22: SELL 4 fills 2 then is cancelled; the remaining 8 is protected, not the planned 6."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(8.0)]), _snapshot(3, [_priced(8.0)])],
               protection=_Protection(target_price=None))
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Cancelled", filled=2.0, total=4.0)]
    s.service.rescan()
    s.service.rescan()
    assert s.dispatch.calls[-1] == ("place_exit_leg", 1, "stop", 8.0, 95.0, "p-1-reprotect-1-1",
                                    "p-1-reprotect-stop-1-1")


def test_done_checks_outstanding_quantity_and_sizes_the_target_from_the_live_position(tmp_path):
    """#22 P2 / R13: the stop filled 2 of 6 before the target was sent."""
    s, protection = _to_reprotect(tmp_path, extra=(
        _snapshot(4, [_priced(4.0)]), _snapshot(5, [_priced(4.0)]), _snapshot(6, [_priced(4.0)])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row(total=6.0, filled=2.0)]
    s.service.rescan()                       # gen 4: the stop fill is new -> wait for a newer generation
    assert s.dispatch.calls[-1][2] == "stop"
    s.service.rescan()                       # gen 5: target sized from the live 4
    assert s.dispatch.calls[-1] == ("place_exit_leg", 1, "target", 4.0, 120.0, "p-1-reprotect-1-1",
                                    "p-1-reprotect-target-1-1")
    s.dispatch.rows["p-1-reprotect-target-1-1"] = [_leg_row(total=4.0)]
    receipt = s.service.rescan()             # gen 6: outstanding 6-2=4 and 4 match the position
    assert (receipt.state, receipt.remaining_quantity) == ("DONE", 4.0)
    assert protection.calls[-1][2] == 4.0


def test_recovery_after_restart_sends_only_the_planned_target(tmp_path):
    """Review focus 5 / R20: the stop left, the process died before its fence; the restart sends only the target."""
    protection = _Protection(stop_price=95.0, target_price=120.0)
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]),
                          _snapshot(3, [_priced(6.0)])], protection=protection)
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    real_leg = s.dispatch.place_exit_leg

    def sent_then_crash(*args, **kwargs):
        real_leg(*args, **kwargs)
        raise _Crash()
    s.dispatch.place_exit_leg = sent_then_crash
    with pytest.raises(_Crash):
        s.service.rescan()                                              # generation 3: the stop leaves
    s.dispatch.place_exit_leg = real_leg
    service = s.restart()
    s.push(_snapshot(4, [_priced(6.0)]))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    service.rescan()
    assert [c[2] for c in s.dispatch.calls if c[0] == "place_exit_leg"] == ["stop", "target"]


def test_crash_while_the_target_is_promoted_never_sends_it_again(tmp_path):
    """R20 / R30: the target was journaled UNKNOWN, the process died before its broker call."""
    s, _protection = _to_reprotect(tmp_path, extra=(_snapshot(4, [_priced(6.0)]),))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]

    def crash(*_args, **_kwargs):
        raise _Crash()
    s.dispatch.place_exit_leg = crash
    with pytest.raises(_Crash):
        s.service.rescan()                                              # generation 4: target promoted
    service = s.restart()
    s.push(_snapshot(5, [_priced(6.0)]), _snapshot(6, [_priced(6.0)]))
    receipt = service.rescan()                                          # fenced on 5; nothing proves it yet
    assert [c.state for c in receipt.children if c.kind == "reprotect-target"] == ["UNKNOWN"]
    service.rescan()                                                    # generation 6: proven absent
    s.push(_snapshot(7, [_priced(6.0)]))
    receipt = service.rescan()                                          # generation 7: escalate, no resend
    assert receipt.escalated is True and receipt.goal == "zero"
    assert [c[2] for c in s.dispatch.calls if c[0] == "place_exit_leg"] == ["stop"]


def test_unknown_reprotect_leg_is_never_resent(tmp_path):
    """R3 / R30: a stop whose send timed out stays UNKNOWN; once proven absent it escalates, never resent."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]), _snapshot(3, [_priced(6.0)]),
                          _snapshot(4, [_priced(6.0)]), _snapshot(5, [_priced(6.0)]), _snapshot(6, [_priced(6.0)])],
               protection=_Protection(target_price=None))
    s.dispatch.fail_after_send.add("place_exit_leg")
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    s.dispatch.staging = 1
    s.service.rescan()                                  # gen 3: stop sent (fence 4), ack timed out
    s.dispatch.staging = 0
    receipt = s.service.rescan()                        # gen 4: no row, not newer than the fence
    assert [c.state for c in receipt.children if c.kind == "reprotect-stop"] == ["UNKNOWN"]
    assert [c.state for c in s.service.rescan().children if c.kind == "reprotect-stop"] == ["ABSENT"]   # gen 5
    receipt = s.service.rescan()                        # gen 6: escalate; the stop is never placed again
    assert receipt.escalated is True
    assert len([c for c in s.dispatch.calls if c[0] == "place_exit_leg"]) == 1


def test_a_reprotect_leg_refused_before_the_broker_escalates_and_is_never_retried(tmp_path):
    """R30 / spec 5.1: re-protect failure means no retry with fresh ids; escalate to a full close."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]),
                          _snapshot(3, [_priced(6.0)]), _snapshot(4, [_priced(6.0)])],
               protection=_Protection(target_price=None))
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    s.dispatch.refuse.add("place_exit_leg")
    s.service.rescan()                                  # gen 3: stop refused before the broker -> NOT_SENT
    receipt = s.service.rescan()                        # gen 4: escalate, no retry with a fresh id
    assert receipt.escalated is True and receipt.goal == "zero"
    assert any("REPROTECT_FAILED: stop leg NOT_SENT" in detail for _root, detail in s.breaker.calls)
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-reduce-1-2")


def test_stop_fill_before_target_is_sent_ends_closed_without_a_target(tmp_path):
    """R13: the stop filled fully before the target went out."""
    s, protection = _to_reprotect(tmp_path, extra=(_snapshot(4, []), _snapshot(5, [])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row("Filled", filled=6.0)]
    s.service.rescan()
    receipt = s.service.rescan()
    assert receipt.state == "CLOSED"
    assert [c[2] for c in s.dispatch.calls if c[0] == "place_exit_leg"] == ["stop"]
    assert [c.state for c in receipt.children if c.kind == "reprotect-target"] == ["NOT_SENT"]
    assert protection.calls[-1] == ("close_after_full", "p-1")


@pytest.mark.parametrize("order", ["target_first", "stop_first"])
def test_target_fill_that_cancels_its_oca_stop_ends_closed_without_a_failure(tmp_path, order):
    """R26 / R2-1: a normal exit is not a re-protect failure, whichever callback lands first."""
    s, protection = _to_reprotect(tmp_path, extra=(
        _snapshot(4, [_priced(6.0)]), _snapshot(5, [_priced(6.0)]), _snapshot(6, []), _snapshot(7, [])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    s.service.rescan()                                                    # gen 4: target sent
    rows = {"p-1-reprotect-target-1-1": [_leg_row("Filled", filled=6.0)],
            "p-1-reprotect-stop-1-1": [_leg_row("Cancelled")]}
    first, second = list(rows) if order == "target_first" else list(rows)[::-1]
    s.dispatch.rows[first] = rows[first]
    s.service.rescan()                                                    # gen 5: one callback seen
    s.dispatch.rows[second] = rows[second]
    for _ in range(2):
        s.service.rescan()                                                # gens 6-7: flat, then behind the fill
    receipt = s.service.receipt_for("p-1")
    assert (receipt.state, receipt.escalated) == ("CLOSED", False)
    assert s.breaker.calls == []
    assert not any(c[0] in ("reduce", "cancel") for c in s.dispatch.calls)
    assert protection.calls[-1] == ("close_after_full", "p-1")


def test_partial_target_fill_waits_for_the_stop_to_match_the_remainder(tmp_path):
    """R2-1: the target filled 2 of 6; the stop shrinks to 4 (OCA type 2); then DONE for 4."""
    s, protection = _to_reprotect(tmp_path, extra=(
        _snapshot(4, [_priced(6.0)]), _snapshot(5, [_priced(4.0)]), _snapshot(6, [_priced(4.0)]),
        _snapshot(7, [_priced(4.0)])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    s.service.rescan()                                                    # gen 4: target sent
    s.dispatch.rows["p-1-reprotect-target-1-1"] = [_leg_row(filled=2.0)]
    s.service.rescan()                                                    # gen 5: the fill is new
    assert "outstanding 6.0 != position 4.0" in s.service.rescan().detail  # gen 6: stop still for 6
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row(total=4.0)]
    receipt = s.service.rescan()                                          # gen 7
    assert (receipt.state, receipt.remaining_quantity) == ("DONE", 4.0)
    assert s.breaker.calls == []


def test_a_leg_the_broker_does_not_link_by_oca_escalates(tmp_path):
    """R13 / R38: DONE reads the OCA group and type from the broker row, not from the journal."""
    s, _protection = _to_reprotect(tmp_path, target=None, extra=(_snapshot(4, [_priced(6.0)]),))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row(oca_type=0)]
    receipt = s.service.rescan()
    assert receipt.escalated is True
    assert any("not a linked protective leg" in detail for _root, detail in s.breaker.calls)


def test_stop_that_goes_pending_submit_then_inactive_escalates(tmp_path):
    """R13: PendingSubmit is a local echo, not acceptance; Inactive after it is a failed re-protect."""
    s, _protection = _to_reprotect(tmp_path, extra=(_snapshot(4, [_priced(6.0)]), _snapshot(5, [_priced(6.0)])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row("PendingSubmit")]
    receipt = s.service.rescan()
    assert [c.state for c in receipt.children if c.kind == "reprotect-stop"] == ["UNKNOWN"]
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row("Inactive")]
    receipt = s.service.rescan()
    assert receipt.escalated is True and receipt.goal == "zero"
    assert any("REPROTECT_FAILED" in detail for _root, detail in s.breaker.calls)
    assert [c[2] for c in s.dispatch.calls if c[0] == "place_exit_leg"] == ["stop"]


def test_target_rejected_escalates_and_cancels_the_working_stop(tmp_path):
    stop_leg = _order("rs", group="p-1-reprotect-stop-1-1", total=6.0, order_type="STP")
    s, _protection = _to_reprotect(tmp_path, extra=(
        _snapshot(4, [_priced(6.0)], [stop_leg]), _snapshot(5, [_priced(6.0)], [stop_leg])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    s.service.rescan()                                   # gen 4: target sent
    s.dispatch.rows["p-1-reprotect-target-1-1"] = [_leg_row("Rejected")]
    receipt = s.service.rescan()                         # gen 5: target rejected
    assert receipt.escalated is True
    assert s.dispatch.calls[-1] == ("cancel", "rs", "p-1-cancel-1-1")


def test_reprotect_deadline_escalates_instead_of_failing_safe(tmp_path):
    """R31: no child is UNKNOWN (the stop works), so the missed deadline escalates once."""
    s, _protection = _to_reprotect(tmp_path, extra=(_snapshot(4, [_priced(6.0)]),))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    receipt = s.service.rescan()
    assert receipt.state != "FAILED_SAFE"
    assert receipt.escalated is True and receipt.goal == "zero"
    assert receipt.deadline == s.clock["now"] + dt.timedelta(seconds=300)
    assert s.breaker.calls


def test_position_zero_during_reprotect_cancels_the_residual_leg_and_ends_closed(tmp_path):
    target_leg = _order("rt", group="p-1-reprotect-target-1-1", total=6.0)
    s, protection = _to_reprotect(tmp_path, extra=(
        _snapshot(4, [_priced(6.0)]), _snapshot(5, [], [target_leg]), _snapshot(6, [], [target_leg]),
        _snapshot(7, [])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    s.service.rescan()                                                    # gen 4: target sent
    s.dispatch.rows["p-1-reprotect-target-1-1"] = [_leg_row()]
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row("Filled", filled=6.0)]
    s.service.rescan()                                                    # gen 5: stop fill seen
    s.service.rescan()                                                    # gen 6: cancel the residual target
    assert s.dispatch.calls[-1] == ("cancel", "rt", "p-1-cancel-1-1")
    s.dispatch.entities["rt"] = _row("Cancelled", total=6.0)
    s.dispatch.rows["p-1-reprotect-target-1-1"] = [_leg_row("Cancelled")]
    receipt = s.service.rescan()                                          # gen 7
    assert receipt.state == "CLOSED"
    assert protection.calls[-1] == ("close_after_full", "p-1")


def test_partial_close_of_short_reprotects_above_price(tmp_path):
    """Review focus 1."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(-10.0)]), _snapshot(1, [_priced(-10.0)]), _snapshot(2, [_priced(-6.0)]), _snapshot(3, [_priced(-6.0)])],
               protection=_Protection(stop_price=105.0, target_price=None))
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    assert s.dispatch.calls[0] == ("reduce_partial", 1, "BUY", 4.0, "p-1-reduce-1-1")
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    s.service.rescan()
    assert s.dispatch.calls[-1] == ("place_exit_leg", 1, "stop", 6.0, 105.0, "p-1-reprotect-1-1",
                                    "p-1-reprotect-stop-1-1")


def test_non_protective_stop_escalates_to_full_close_and_trips_breaker(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]), _snapshot(3, [_priced(6.0)])],
               protection=_Protection(stop_price=101.0, target_price=None))     # above market on a long
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    receipt = s.service.rescan()
    assert receipt.escalated is True and receipt.goal == "zero"
    assert any("STOP_NOT_PROTECTIVE" in detail for _root, detail in s.breaker.calls)
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-reduce-1-2")


def test_upgrade_to_zero_during_reprotect_cancels_replacement_exits_and_closes(tmp_path):
    stop_leg = _order("rs", group="p-1-reprotect-stop-1-1", total=6.0, order_type="STP")
    s, protection = _to_reprotect(tmp_path, extra=(
        _snapshot(4, [_priced(6.0)], [stop_leg]), _snapshot(5, [_priced(6.0)]), _snapshot(6, []), _snapshot(7, [])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    receipt = s.service.upgrade_to_zero("p-1")
    assert (receipt.goal, receipt.phase) == ("zero", "cancel")
    assert [c.state for c in receipt.children if c.kind == "reprotect-target"] == ["NOT_SENT"]
    s.service.rescan()                                                     # gen 4: cancel the replacement stop
    assert s.dispatch.calls[-1] == ("cancel", "rs", "p-1-cancel-1-1")
    s.dispatch.entities["rs"] = _row("Cancelled", total=6.0)
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row("Cancelled")]   # the same order, by its own ref
    s.service.rescan()                                                     # gen 5: reduce the remainder
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-reduce-1-2")
    s.dispatch.rows["p-1-reduce-1-2"] = [_row("Filled", filled=6.0, total=6.0)]
    s.service.rescan()
    assert s.service.rescan().state == "CLOSED"
    assert not any(c[0] == "release_after_partial" for c in protection.calls)
    assert [c[2] for c in s.dispatch.calls if c[0] == "place_exit_leg"] == ["stop"]


def test_restart_after_a_goal_upgrade_never_reprotects(tmp_path):
    """R20: crash right after the upgrade transaction; the restarted root closes everything."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]),
                          _snapshot(3, [_priced(6.0)]), _snapshot(4, []), _snapshot(5, [])],
               protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.service.upgrade_to_zero("p-1")
    service = s.restart()
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    service.rescan()
    service.rescan()
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-reduce-1-2")
    s.dispatch.rows["p-1-reduce-1-2"] = [_row("Filled", filled=6.0, total=6.0)]
    service.rescan()
    assert service.rescan().state == "CLOSED"
    assert not any(c[0] == "place_exit_leg" for c in s.dispatch.calls)


def test_reprotect_deadline_with_an_unknown_leg_is_failed_safe_with_no_order(tmp_path):
    """R5 / R31: an UNKNOWN child at the deadline forbids every new order."""
    s, _protection = _to_reprotect(tmp_path, extra=(_snapshot(4, [_priced(6.0)]),))
    s.dispatch.complete = False
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    sent = len(s.dispatch.calls)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert len(s.dispatch.calls) == sent


def test_partial_deadline_in_the_reduce_phase_escalates_to_a_full_close(tmp_path):
    """R31 / D10: protection is already cancelled; a working partial reduce at the deadline is escalated once."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(8.0)]),
                          _snapshot(3, [_priced(8.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Submitted", filled=2.0, total=4.0, entity="p-1-reduce-1-1:exit")]
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    receipt = s.service.rescan()                                         # gen 2: reduce WORKING, nothing UNKNOWN
    assert (receipt.state, receipt.escalated, receipt.goal) == ("CANCELLING", True, "zero")
    assert receipt.deadline == s.clock["now"] + dt.timedelta(seconds=300)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()                                                   # gen 3: the fill is seen
    s.push(_snapshot(4, [_priced(6.0)]))
    s.service.rescan()                                                   # gen 4: close the live 6
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-reduce-1-2")


def test_partial_deadline_with_an_unknown_reduce_is_failed_safe_with_no_order(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(10.0)])],
               protection=_Protection())
    s.dispatch.complete = False
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert [c[0] for c in s.dispatch.calls] == ["reduce_partial"]


def test_invisible_partial_reduce_never_reprotects_or_reduces_again(tmp_path):
    """#22: while the partial reduce is unknown, no leg and no second reduce goes out."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(10.0)]),
                          _snapshot(3, [_priced(10.0)])], protection=_Protection())
    s.dispatch.complete = False
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    for _ in range(2):
        assert "outcome unknown" in s.service.rescan().detail
    assert [c[0] for c in s.dispatch.calls] == ["reduce_partial"]


@pytest.mark.parametrize("status", ["Rejected", "Cancelled"])
def test_a_partial_reduce_that_sold_nothing_reprotects_and_ends_reduce_failed(tmp_path, status):
    """R25 / R2-2: protection comes back, but the close is a failure, never DONE."""
    protection = _Protection(stop_price=95.0, target_price=None)
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(10.0)]),
                          _snapshot(3, [_priced(10.0)]), _snapshot(4, [_priced(10.0)])], protection=protection)
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row(status, filled=0.0, total=4.0)]
    s.service.rescan()                                                   # gen 2: stop for the untouched 10
    assert s.dispatch.calls[-1] == ("place_exit_leg", 1, "stop", 10.0, 95.0, "p-1-reprotect-1-1",
                                    "p-1-reprotect-stop-1-1")
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row(total=10.0)]
    receipt = s.service.rescan()                                         # gen 3: the stop works
    assert (receipt.state, receipt.remaining_quantity) == ("REDUCE_FAILED", 10.0)
    assert protection.calls[-1][:3] == ("release_after_partial", "p-1", 10.0)
    assert s.registry.get("p-1").state == "RELEASED"
    resolution = s.service.close_resolution("p-1")
    assert (resolution.success, resolution.error_code) == (False, "REDUCE_FAILED")
    assert (resolution.outcome["requested_quantity"], resolution.outcome["filled_quantity"],
            resolution.outcome["remaining_quantity"]) == (4.0, 0.0, 10.0)


def test_restart_before_the_done_cleanup_finishes_the_release(tmp_path):
    """R8 / R20: DONE and the owner release committed; the saga release is recovered."""
    s, protection = _to_reprotect(tmp_path, target=None, extra=(_snapshot(4, [_priced(6.0)]),))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    protection.crash_on.add("release_after_partial")
    with pytest.raises(_Crash):
        s.service.rescan()
    stored = s.store.receipt("p-1")
    assert (stored.state, stored.cleanup_pending, s.registry.get("p-1").state) == ("DONE", True, "RELEASED")
    s.restart().rescan()
    assert s.store.receipt("p-1").cleanup_pending is False
    assert protection.calls[-1][:2] == ("release_after_partial", "p-1")


def test_partial_retry_after_the_fill_returns_its_root(tmp_path):
    """R36 / D15: a retried partial command finds its root before any admission check."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)])], protection=_Protection())
    first = s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.7)
    s.push(_snapshot(2, []))                                             # the position is gone now
    captures = s.broker.calls
    again = s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.7)
    assert again.cause_command_id == first.cause_command_id == "p-1"
    assert s.service.root_for("p-1") == "p-1"
    assert s.broker.calls == captures + 1                               # the tick only, no admission capture


def test_partial_request_against_an_owner_is_exit_in_progress_before_admission(tmp_path):
    from trader.trading.exit_owner import ExitInProgress
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "c-1", DEADLINE, scope="conid", conid=1)
    captures = s.broker.calls
    with pytest.raises(ExitInProgress):
        s.service.start(ACCOUNT, "p-2", DEADLINE, scope="conid", conid=1, quantity=12.0)
    assert s.broker.calls == captures


def test_upgrade_rolls_back_with_its_run_change(tmp_path):
    """R6 / R20: the registry goal and the run goal change in one transaction or not at all."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)], [_stop_order()])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)

    def broken(conn, root_id, detail):
        raise _Crash()
    s.service._upgrade_run_in_tx = broken
    with pytest.raises(_Crash):
        s.service.upgrade_to_zero("p-1")
    assert (s.registry.get("p-1").goal, s.store.receipt("p-1").goal) == ("partial", "partial")


def test_a_stale_run_write_never_lowers_the_goal(tmp_path):
    """#24: a write from an old receipt changes only its named fields on the current row."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)], [_stop_order()])], protection=_Protection())
    stale = s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.service.upgrade_to_zero("p-1")
    s.service._set(stale, "VERIFYING", detail="late write")
    assert (s.store.receipt("p-1").goal, s.store.receipt("p-1").detail) == ("zero", "late write")


def test_dispatch_stops_when_the_owner_goal_and_the_run_goal_disagree(tmp_path):
    """R7 / #24: a registry/cursor split is caught before any broker call."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)], [_stop_order()]), _snapshot(1, [_priced(10.0)], [_stop_order()]),
                          _snapshot(2, [_priced(10.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.entities["stop-1"] = _row("Cancelled")
    s.db.execute("UPDATE exit_owners SET goal = 'zero' WHERE root_id = 'p-1'", fetch="none")
    s.service.rescan()
    assert [c[0] for c in s.dispatch.calls] == ["cancel"]


def test_an_inherited_reduce_never_counts_as_this_partials_reduce(tmp_path):
    """A partial close that inherits an old reduce still sends its own partial reduce."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)])], protection=_Protection())
    s.dispatch.staging = 1
    s.service.start(ACCOUNT, "c-1", NOW + dt.timedelta(seconds=30), scope="conid", conid=1)
    s.dispatch.staging = 0
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    s.dispatch.complete = False
    assert s.service.rescan().state == "FAILED_SAFE"                     # c-1's reduce is UNKNOWN
    s.dispatch.complete = True
    s.push(_snapshot(2, [_priced(10.0)]), _snapshot(2, [_priced(10.0)]), _snapshot(3, [_priced(10.0)]))
    s.dispatch.rows["c-1-reduce-1-1"] = [_row("Cancelled", filled=0.0)]
    s.service.start(ACCOUNT, "p-2", NOW + dt.timedelta(minutes=5), scope="conid", conid=1, quantity=4.0)
    assert s.dispatch.calls[-1] == ("reduce_partial", 1, "SELL", 4.0, "p-2-reduce-1-1")
# ---------------------------------------------------------------------------
# Task 7: account flatten takes over scoped closes
# ---------------------------------------------------------------------------

def test_kill_during_reprotect_cancels_replacement_exits_and_reaches_flat(tmp_path):
    """Spec test: kill during REPROTECTING with a working replacement stop."""
    stop_leg = _order("rs", group="p-1-reprotect-stop-1-1", total=6.0, order_type="STP")
    s, protection = _to_reprotect(tmp_path, target=None, extra=(
        _snapshot(4, [_priced(6.0)], [stop_leg]), _snapshot(5, [_priced(6.0)]), _snapshot(6, []), _snapshot(7, [])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    receipt = s.service.start(ACCOUNT, "kill-1", DEADLINE)                 # gen 4
    p1 = s.service.receipt_for("p-1")
    assert (p1.state, p1.superseded_by) == ("SUPERSEDED", "kill-1")
    assert s.registry.get("p-1").state == "SUPERSEDED"
    assert s.registry.account_owner(ACCOUNT).root_id == "kill-1"
    assert ("handover_account", "kill-1", ("rs",)) in protection.calls
    assert [c.child_id for c in receipt.children if c.kind == "reprotect-stop"] == ["p-1-reprotect-stop-1-1"]
    assert s.dispatch.calls[-1] == ("cancel", "rs", "kill-1-cancel-1-1")   # cancelled, not waited on
    s.dispatch.entities["rs"] = _row("Cancelled", total=6.0)
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row("Cancelled")]
    s.service.rescan()                                                     # gen 5: reduce the rest
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "kill-1-reduce-1-1")
    s.dispatch.rows["kill-1-reduce-1-1"] = [_row("Filled", filled=6.0, total=6.0)]
    s.service.rescan()
    assert s.service.receipt_for("kill-1").state == "VERIFYING"
    assert s.service.rescan().state == "FLAT"                               # gen 7
    assert s.service.receipt_for("p-1").state == "SUPERSEDED"
    assert protection.calls[-1] == ("close_after_full", "kill-1")
    assert s.registry.get("kill-1").state == "RELEASED"


def test_flatten_never_reduces_while_an_inherited_reprotect_leg_is_invisible(tmp_path):
    """#23: a submitted replacement stop that find_orders does not show yet blocks the reduce."""
    s, _protection = _to_reprotect(tmp_path, target=None, extra=(_snapshot(4, [_priced(6.0)]),))
    s.dispatch.complete = False                                                       # nothing proves it absent
    receipt = s.service.start(ACCOUNT, "kill-1", NOW + dt.timedelta(seconds=30))     # gen 4 = leg fence + 1
    assert "outcome unknown" in receipt.detail
    assert not any(c[0] == "reduce" for c in s.dispatch.calls)
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert not any(c[0] == "reduce" for c in s.dispatch.calls)


def test_flatten_deadline_with_an_inherited_unknown_reduce_is_failed_safe_not_a_second_order(tmp_path):
    """Spec test: a submitted child not yet visible blocks the flatten; deadline = FAILED_SAFE."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(10.0)])],
               protection=_Protection())
    s.dispatch.complete = False                                         # the partial reduce stays invisible
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    receipt = s.service.start(ACCOUNT, "kill-1", NOW + dt.timedelta(seconds=30))
    assert [(c.child_id, c.state) for c in receipt.children] == [("p-1-reduce-1-1", "UNKNOWN")]
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert [c[0] for c in s.dispatch.calls] == ["reduce_partial"]


def test_account_flatten_during_partial_close_reconciles_its_children_before_reducing(tmp_path):
    """Spec test: the partial reduce is still working when the flatten starts."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(8.0)]),
                          _snapshot(3, [_priced(6.0)]), _snapshot(4, [_priced(6.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Submitted", filled=2.0, total=4.0)]
    receipt = s.service.start(ACCOUNT, "kill-1", DEADLINE)                     # gen 2: still working
    assert "still working" in receipt.detail
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()                                                        # gen 3: fill seen
    s.service.rescan()                                                        # gen 4: fresh
    assert [c[0] for c in s.dispatch.calls] == ["reduce_partial", "reduce"]
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "kill-1-reduce-1-1")


def test_superseded_close_stops_at_once_and_never_reprotects(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]),
                          _snapshot(3, [_priced(6.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.start(ACCOUNT, "kill-1", DEADLINE)
    s.service.rescan()
    assert not any(c[0] == "place_exit_leg" for c in s.dispatch.calls)
    assert s.service.receipt_for("p-1").state == "SUPERSEDED"


def test_restart_after_supersede_never_dispatches_for_the_superseded_root(tmp_path):
    """R20: crash after the takeover transaction, before the account hand-over."""
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]),
                          _snapshot(3, [_priced(6.0)]), _snapshot(4, [_priced(6.0)])], protection=protection)
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    protection.crash_on.add("handover_account")
    with pytest.raises(_Crash):
        s.service.start(ACCOUNT, "kill-1", DEADLINE)
    assert s.store.receipt("p-1").state == "SUPERSEDED"
    service = s.restart()
    service.rescan()
    service.rescan()
    assert ("handover_account", "kill-1", ()) in protection.calls
    assert not any(c[0] == "place_exit_leg" for c in s.dispatch.calls)
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "kill-1-reduce-1-1")


def test_later_scoped_close_inherits_the_unknown_child_of_a_failed_safe_close(tmp_path):
    """R9 scoped: the new close of the conid waits for the old unknown reduce."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "c-1", NOW + dt.timedelta(seconds=30), scope="conid", conid=1)
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    s.push(_snapshot(2, [_priced(10.0)]))
    receipt = s.service.start(ACCOUNT, "c-2", NOW + dt.timedelta(minutes=5), scope="conid", conid=1)
    assert receipt.cause_command_id == "c-2"
    assert [c.child_id for c in receipt.children] == ["c-1-reduce-1-1"]
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_a_working_leg_that_appeared_after_the_capture_is_cancelled_and_blocks(tmp_path):
    """R23 / #21: the snapshot missed the replacement stop; its own row shows it working."""
    protection = _Protection(stop_price=95.0, target_price=None)
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]),
                          _snapshot(3, [_priced(6.0)]), _snapshot(4, [_priced(6.0)])], protection=protection)
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    s.service.rescan()                                                  # gen 3: stop leg sent
    leg = "p-1-reprotect-stop-1-1:stop"
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row(entity=leg)]
    receipt = s.service.start(ACCOUNT, "kill-1", DEADLINE)              # gen 4: no working order in the snapshot
    assert ("handover_account", "kill-1", (leg,)) in protection.calls
    assert s.dispatch.calls[-1] == ("cancel", leg, "kill-1-cancel-1-1")
    assert "still working" in receipt.detail
    assert not any(c[0] == "reduce" for c in s.dispatch.calls)


def test_account_claim_supersede_and_inheritance_commit_together_or_not_at_all(tmp_path):
    """R6 / R20: a failure inside the takeover transaction leaves the scoped close untouched."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)], [_stop_order()])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)

    def broken(conn, **_kwargs):
        raise _Crash()
    s.store.inherit_children_in_tx = broken
    with pytest.raises(_Crash):
        s.service.start(ACCOUNT, "kill-1", DEADLINE)
    assert (s.registry.get("p-1").state, s.registry.account_owner(ACCOUNT)) == ("ACTIVE", None)
    assert s.store.receipt("p-1").state != "SUPERSEDED" and s.store.receipt("kill-1") is None
    assert s.service.root_for("kill-1") is None


def test_account_takeover_waits_for_a_generation_newer_than_the_scoped_roots_fill(tmp_path):
    """Ruling 43: the scoped close's reduce filled on generation 2 and the cache still says 10.
    The account flatten that takes over on generation 2 sends nothing until a newer one."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "c-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.rows["c-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.push(_snapshot(2, [_priced(10.0)]))
    s.service.rescan()                                        # gen 2: the fill is observed
    s.service.start(ACCOUNT, "kill-1", DEADLINE)
    assert s.store.receipt("c-1").state == "SUPERSEDED"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]
    s.push(_snapshot(3, []), _snapshot(4, []))
    s.service.rescan()
    assert s.service.receipt_for("kill-1").state == "FLAT"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


# ---------------------------------------------------------------------------
# Task 8: scoped claims join, upgrade or refuse
# ---------------------------------------------------------------------------

from trader.trading.exit_owner import ExitInProgress  # noqa: E402


def test_second_full_close_joins_and_returns_the_owners_receipt(tmp_path):
    """Spec test: a time exit and an AI close on the same conid make one root."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])], protection=_Protection())
    s.service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    joined = s.service.start(ACCOUNT, "ai-close-1", DEADLINE, scope="conid", conid=1)
    assert joined.cause_command_id == "exit-1"
    assert s.service.receipt_for("ai-close-1") is None
    assert s.service.root_for("ai-close-1") == "exit-1"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_time_exit_during_partial_close_upgrades_goal_and_ends_closed(tmp_path):
    """Spec test: zero position and no residual exits, not DONE."""
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]),
                          _snapshot(3, [_priced(6.0)]), _snapshot(4, []), _snapshot(5, [])], protection=protection)
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    upgraded = s.service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    assert (upgraded.cause_command_id, upgraded.goal) == ("p-1", "zero")
    assert s.registry.get("p-1").goal == "zero"
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    s.service.rescan()
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-reduce-1-2")
    s.dispatch.rows["p-1-reduce-1-2"] = [_row("Filled", filled=6.0, total=6.0)]
    s.service.rescan()
    assert s.service.rescan().state == "CLOSED"
    assert not any(c[0] == "place_exit_leg" for c in s.dispatch.calls)
    assert protection.calls[-1] == ("close_after_full", "p-1")


def test_partial_against_an_existing_owner_raises_and_records_nothing(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    with pytest.raises(ExitInProgress):
        s.service.start(ACCOUNT, "p-2", DEADLINE, scope="conid", conid=1, quantity=3.0)
    assert s.service.root_for("p-2") is None


def test_full_close_during_active_flatten_joins_it_and_leaves_the_superseded_owner_alone(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)])],
               protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    joined = s.service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    assert joined.cause_command_id == "flat-1"
    p1 = s.service.receipt_for("p-1")
    assert (p1.state, p1.goal, p1.goal_quantity) == ("SUPERSEDED", "partial", 4.0)
    assert s.registry.get("p-1").state == "SUPERSEDED"


def test_upgrade_never_touches_a_superseded_run(tmp_path):
    """#24: an upgrade that arrives after the takeover cannot revive the close."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)])],
               protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    receipt = s.service.upgrade_to_zero("p-1")
    assert (receipt.state, receipt.goal) == ("SUPERSEDED", "partial")


def test_claim_and_run_commit_together_or_not_at_all(tmp_path):
    """R6 / #24: a failure after the claim insert leaves no owner, no join and no run."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])], protection=_Protection())
    real_insert = s.store.insert_run_in_tx

    def broken_insert(conn, receipt, now):
        raise RuntimeError("disk full")
    s.store.insert_run_in_tx = broken_insert
    with pytest.raises(RuntimeError):
        s.service.start(ACCOUNT, "c-1", DEADLINE, scope="conid", conid=1)
    assert s.registry.get("c-1") is None
    assert s.service.root_for("c-1") is None
    s.store.insert_run_in_tx = real_insert
    assert s.service.start(ACCOUNT, "c-1", DEADLINE, scope="conid", conid=1).cause_command_id == "c-1"


def test_joined_request_retried_after_restart_returns_the_same_root(tmp_path):
    """#24: a retry of a joined command finds its durable root, not a new one."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])], protection=_Protection())
    s.service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    s.service.start(ACCOUNT, "ai-close-1", DEADLINE, scope="conid", conid=1)
    service = s.restart()
    again = service.start(ACCOUNT, "ai-close-1", DEADLINE, scope="conid", conid=1)
    assert again.cause_command_id == "exit-1"
    with pytest.raises(ValueError):
        service.start(ACCOUNT, "ai-close-1", DEADLINE, scope="conid", conid=2)
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_closed_root_releases_the_owner_and_a_later_close_is_a_new_root_after_restart(tmp_path):
    """#23: after CLOSED and a restart, the next close of the conid starts fresh."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, [])], protection=_Protection())
    s.service.start(ACCOUNT, "c-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.rows["c-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "CLOSED"
    service = s.restart()
    s.push(_snapshot(4, [_position(3.0)]))
    receipt = service.start(ACCOUNT, "c-2", DEADLINE, scope="conid", conid=1)
    assert receipt.cause_command_id == "c-2"
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 3.0, "c-2-reduce-1-1")


def test_upgraded_claim_and_run_change_commit_together_or_not_at_all(tmp_path):
    """R6 / R20: a failure after the registry upgrade, inside the claim transaction, undoes it."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)], [_stop_order()])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)

    def broken(conn, root_id, detail):
        raise _Crash()
    s.service._upgrade_run_in_tx = broken
    with pytest.raises(_Crash):
        s.service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    assert (s.registry.get("p-1").goal, s.store.receipt("p-1").goal) == ("partial", "partial")
    assert s.service.root_for("exit-1") is None


def test_a_full_close_during_an_unfinished_done_cleanup_starts_a_new_root(tmp_path):
    """R24 / #24: DONE released the owner already, so a later close never joins the finished root."""
    s, protection = _to_reprotect(tmp_path, target=None, extra=(_snapshot(4, [_priced(6.0)]),))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    protection.crash_on.add("release_after_partial")
    with pytest.raises(_Crash):
        s.service.rescan()                                             # DONE, cleanup cut off
    receipt = s.service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    assert receipt.cause_command_id == "exit-1"
    assert (s.service.receipt_for("p-1").state, s.service.receipt_for("p-1").goal) == ("DONE", "partial")
    assert s.registry.owner_for(ACCOUNT, 1).root_id == "exit-1"
