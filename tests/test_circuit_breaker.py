import datetime as dt

import pytest

from trader.data.circuit_breaker_store import (
    CircuitBreakerStore,
    apply_circuit_breaker_migration,
)
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.circuit_breaker import (
    BreakerResetRefused,
    BreakerSignal,
    CircuitBreaker,
)

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
ACCOUNT = "DU111111"


@pytest.fixture()
def breaker_parts(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_circuit_breaker_migration(migrator)
    store = CircuitBreakerStore(journal, ACCOUNT)
    store.seed(T0)
    return db, journal, store


def _breaker(store, now, *, ready=True, reconciled=True):
    return CircuitBreaker(
        store,
        now=lambda: now[0],
        reset_ready=lambda: ready,
        reconciliation_complete=lambda: reconciled,
        session_key=lambda value: value.date().isoformat(),
    )


@pytest.mark.parametrize("kind", [
    "DAILY_LOSS_BREACH", "PROTECTIVE_ORDER_FAILURE", "RECONCILIATION_DIVERGENCE",
])
def test_immediate_signals_trip_and_survive_restart(breaker_parts, kind):
    _db, _journal, store = breaker_parts
    clock = [T0]
    state = _breaker(store, clock).record(BreakerSignal(kind, T0, "critical", "same"))
    assert state.state == "TRIPPED"
    assert CircuitBreakerStore(store.journal, ACCOUNT).get().state == "TRIPPED"


@pytest.mark.parametrize("kind,count,window", [
    ("QUOTE_FAILURE", 3, 300),
    ("AMBIGUOUS_OUTCOME", 3, 300),
    ("RUNTIME_EXCEPTION", 5, 600),
])
def test_rolling_thresholds_trip_only_at_boundary(breaker_parts, kind, count, window):
    _db, _journal, store = breaker_parts
    clock = [T0]
    breaker = _breaker(store, clock)
    for index in range(count - 1):
        clock[0] = T0 + dt.timedelta(seconds=index)
        assert breaker.record(BreakerSignal(kind, clock[0], key=str(index))).state == "CLEAR"
    clock[0] = T0 + dt.timedelta(seconds=count)
    assert breaker.record(BreakerSignal(kind, clock[0], key="last")).state == "TRIPPED"


def test_old_failures_fall_outside_window(breaker_parts):
    _db, _journal, store = breaker_parts
    clock = [T0]
    breaker = _breaker(store, clock)
    breaker.record(BreakerSignal("QUOTE_FAILURE", T0, key="1"))
    breaker.record(BreakerSignal("QUOTE_FAILURE", T0 + dt.timedelta(seconds=1), key="2"))
    clock[0] = T0 + dt.timedelta(seconds=301)
    assert breaker.record(BreakerSignal("QUOTE_FAILURE", clock[0], key="3")).state == "CLEAR"


def test_disconnect_grace_and_reconnect(breaker_parts):
    _db, _journal, store = breaker_parts
    clock = [T0]
    breaker = _breaker(store, clock, ready=False)
    breaker.record(BreakerSignal("BROKER_DISCONNECTED", T0, key="down"))
    clock[0] = T0 + dt.timedelta(seconds=20)
    assert breaker.record(BreakerSignal("BROKER_DISCONNECTED", clock[0], key="still-down")).state == "CLEAR"
    breaker.record(BreakerSignal("BROKER_RECONNECTED", clock[0], key="up"))
    clock[0] = T0 + dt.timedelta(seconds=50)
    assert breaker.record(BreakerSignal("BROKER_DISCONNECTED", clock[0], key="down-again")).state == "CLEAR"
    clock[0] = T0 + dt.timedelta(seconds=81)
    assert breaker.record(BreakerSignal("BROKER_DISCONNECTED", clock[0], key="long-down")).state == "TRIPPED"


def test_duplicate_signal_is_idempotent(breaker_parts):
    db, _journal, store = breaker_parts
    clock = [T0]
    breaker = _breaker(store, clock)
    signal = BreakerSignal("DAILY_LOSS_BREACH", T0, key="loss-1")
    first = breaker.record(signal)
    second = breaker.record(signal)
    assert second.revision == first.revision
    assert db.execute("SELECT count(*) FROM automation_incidents", fetch="one")[0] == 1


def test_reset_requires_operator_reason_readiness_reconciliation_and_next_session(breaker_parts):
    _db, _journal, store = breaker_parts
    clock = [T0]
    breaker = _breaker(store, clock)
    breaker.record(BreakerSignal("DAILY_LOSS_BREACH", T0, key="loss"))
    with pytest.raises(BreakerResetRefused, match="next session"):
        breaker.reset("reset-1", "reviewed", "operator:alice")

    clock[0] = T0 + dt.timedelta(days=1)
    blocked = _breaker(store, clock, ready=False)
    with pytest.raises(BreakerResetRefused, match="readiness"):
        blocked.reset("reset-2", "reviewed", "operator:alice")
    blocked = _breaker(store, clock, reconciled=False)
    with pytest.raises(BreakerResetRefused, match="reconciliation"):
        blocked.reset("reset-3", "reviewed", "operator:alice")

    state = _breaker(store, clock).reset("reset-4", "reviewed", "operator:alice")
    assert state.state == "CLEAR"
    assert state.reset_command_id == "reset-4"

