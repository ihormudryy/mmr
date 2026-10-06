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
        return list(self.rows.get(child_id, []))

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


def _legacy_db(tmp_path):
    """A journal as it was before SP1: migration 25 only, with old runs."""
    db = DuckDBConnection.get_instance(str(tmp_path / "liq.duckdb"))
    migrator = SchemaMigrator(db)
    migrator.apply(25, "p1_liquidation_runs", ("""CREATE TABLE IF NOT EXISTS liquidation_runs (
        cause_command_id VARCHAR PRIMARY KEY, account_id VARCHAR NOT NULL, state VARCHAR NOT NULL,
        deadline TIMESTAMPTZ NOT NULL, generation_id BIGINT, detail VARCHAR NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL)""",))
    for root, state, minute in (("old-flat", "FLAT", 1), ("old-failed", "FAILED_SAFE", 2),
                                ("open-a", "OUTCOME_UNKNOWN", 3), ("open-b", "VERIFYING", 4),
                                ("open-c", "REQUESTED", 5)):
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
        (child, "UNKNOWN", 5) for child in ("open-a-liquidation-reduce-1", "open-b-liquidation-reduce-1",
                                            "open-c-liquidation-reduce-1", "old-failed-liquidation-reduce-1")}
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

_OLD_REDUCES = {"open-a-liquidation-reduce-1", "open-b-liquidation-reduce-1",
                "open-c-liquidation-reduce-1", "old-failed-liquidation-reduce-1"}


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
    assert [(c.child_id, c.state) for c in receipt.children] == [("old-failed-liquidation-reduce-1", "UNKNOWN")]
    assert s.dispatch.calls == []
    s.service.rescan()                                       # 6: complete and newer, no row: ABSENT
    s.service.rescan()                                       # 7: newer than that observation: reduce
    assert s.dispatch.calls == [("reduce", 1, "SELL", 10.0, "flat-new-reduce-1-1")]
