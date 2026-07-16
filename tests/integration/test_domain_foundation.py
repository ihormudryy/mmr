"""[M1-F1] Task 6 -- the foundation integration gate.

This is the capstone that closes the M1-F1 event-foundation workstream: it
drives ``DomainJournal`` (Task 3), ``DomainSnapshotService`` (Task 4),
``DomainFeedService`` (Task 5), and ``DomainJournal.compact`` (Task 6) all
together against ONE shared DuckDB file, then injects a crash at each of
``mutate()``'s four durability boundaries and asserts recovery leaves the
journal and materialized state consistent: both-or-neither (nothing
half-written), no torn row, and the cursor stream intact (idempotent retry
still returns the original event, never a duplicate).

Crash-injection technique: every crash below is injected by monkeypatching
a ``DomainJournal`` PRIVATE method (or, for the "commit" boundary, wrapping
the raw ``conn`` passed into ``mutate()``) so that the REAL implementation
still runs and then raises, or a specific SQL statement is intercepted.
This requires NO changes to ``mutate()``'s frozen signature -- exactly how
Task 3's own crash-point test
(``test_domain_journal.py::test_materialized_write_and_event_are_atomic``)
injects its crash purely via the caller-supplied ``write_materialized``
callback, with no dedicated crash-injection parameter on ``mutate()``
itself.

"Recovery" is simulated by constructing a BRAND NEW ``DomainJournal``
wrapping the SAME underlying file (a fresh ``duckdb.connect()``, zeroed
``_latest_committed_cursor``, fresh locks) and calling ``migrate()`` on it
-- this is the same "process restart" a real ``trader_service`` restart
would produce, without needing an actual subprocess.
"""
from __future__ import annotations

import datetime as dt
import time

import pytest

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.materialized_state import GenericEntityAdapter
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.domain.feed_service import CursorExpired, DomainFeedService
from trader.domain.snapshot_service import DomainSnapshotService

UTC_NOW = dt.datetime(2026, 7, 16, 12, 0, tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------- #
# Shared fixtures / helpers
# --------------------------------------------------------------------- #

@pytest.fixture
def bootstrap(tmp_duckdb_path):
    db = DuckDBConnection.get_instance(tmp_duckdb_path)
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    # Scratch table standing in for a caller's own rich materialized write
    # (e.g. a future broker_positions row) -- mirrors
    # test_domain_journal.py's `_DomainStore` harness.
    db.execute("CREATE TABLE test_position_writes (entity_id VARCHAR, revision BIGINT)")
    return db, migrator, journal


def _reopen(db, migrator):
    """Simulate a process restart: a brand new DomainJournal wrapping the
    SAME underlying file, with zeroed in-memory state, that must resync
    entirely from what is durably on disk.
    """
    recovered = DomainJournal(db)
    recovered.migrate(migrator)
    return recovered


def _mutation(entity_id, quantity):
    return DomainMutation(
        event_type="position.updated",
        entity_type="position",
        entity_id=entity_id,
        operation="upsert",
        account_id="DU123",
        source="trader_service",
        source_timestamp=UTC_NOW,
        correlation_id=None,
        payload={"quantity": quantity},
    )


class _CommitCrashingConn:
    """Forwards everything to the real cursor except a literal ``COMMIT``
    statement, which raises instead of ever reaching the real connection --
    simulates the process dying in the exact instant
    ``conn.execute("COMMIT")`` is invoked, before DuckDB's own atomic
    commit has a chance to apply.

    Distinct from the "journal-insert" crash below: that one crashes AFTER
    a real statement has run but while still inside application code,
    before ``mutate()`` even reaches the commit line. This one crashes
    exactly when the commit line itself executes, proving ``mutate()``'s
    ``except BaseException: ROLLBACK`` also correctly covers a failure
    raised BY the commit call itself, not just failures raised by
    application code before it.
    """

    def __init__(self, real):
        self._real = real

    def execute(self, query, *args, **kwargs):
        if isinstance(query, str) and query.strip().upper() == "COMMIT":
            raise RuntimeError("crash at commit")
        return self._real.execute(query, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


# --------------------------------------------------------------------- #
# End-to-end composition: write -> fenced snapshot -> long-poll tail ->
# compaction, all on one shared journal.
# --------------------------------------------------------------------- #

def test_end_to_end_journal_snapshot_feed_and_compaction_compose_consistently(bootstrap):
    db, migrator, journal = bootstrap
    snapshot_service = DomainSnapshotService(journal)
    snapshot_service.register_adapter(GenericEntityAdapter("position"))
    feed = DomainFeedService(journal)

    def write(entity_id, quantity, event_id):
        conn = journal.connect()
        return journal.mutate(conn, _mutation(entity_id, quantity), lambda c, r: None, event_id=event_id)

    write("AAA", 1, "e1")
    write("BBB", 2, "e2")

    snapshot = snapshot_service.snapshot_with_cursor()
    assert {row["entity_id"] for row in snapshot.entities["position"]} == {"AAA", "BBB"}
    baseline_cursor = snapshot.source_cursor

    write("CCC", 3, "e3")

    tail = feed.read_domain_events(baseline_cursor, 100, 0)
    assert [e.entity_id for e in tail.events] == ["CCC"]
    assert tail.newest_cursor == tail.events[-1].source_cursor

    # Retention must not disturb any of the above: `now` is real "today",
    # nothing here is anywhere near 30 days old, so this is a no-op
    # deletion-wise but still must produce a durable checkpoint and leave
    # every prior read consistent.
    compaction = journal.compact(
        now=dt.datetime.now(dt.timezone.utc), active_cursors={"dashboard": baseline_cursor}
    )
    assert compaction.deleted_count == 0
    assert compaction.oldest_retained_cursor <= baseline_cursor
    assert journal.latest_checkpoint() is not None

    # A fresh snapshot after compaction still reflects everything.
    post_compaction_snapshot = snapshot_service.snapshot_with_cursor()
    assert {row["entity_id"] for row in post_compaction_snapshot.entities["position"]} == {
        "AAA", "BBB", "CCC",
    }

    # And the earlier tail read is still valid post-compaction -- no
    # CursorExpired, no gap, no duplicate.
    tail_again = feed.read_domain_events(baseline_cursor, 100, 0)
    assert [e.entity_id for e in tail_again.events] == ["CCC"]


# --------------------------------------------------------------------- #
# Boundary 1: crash at materialized-write (before journal insert) --
# nothing at all has been recorded yet.
# --------------------------------------------------------------------- #

def test_crash_at_materialized_write_leaves_no_trace_and_recovers_cleanly(bootstrap):
    db, migrator, journal = bootstrap
    mutation = _mutation("DU123:1", 10)

    def crashing_write_materialized(conn, revision):
        conn.execute("INSERT INTO test_position_writes VALUES (?, ?)", ["DU123:1", revision])
        raise RuntimeError("crash at materialized-write")

    conn = journal.connect()
    with pytest.raises(RuntimeError, match="materialized-write"):
        journal.mutate(conn, mutation, crashing_write_materialized, event_id="e1")

    # Both-or-neither on the SAME instance: nothing durable anywhere.
    assert journal.get_entity("position", "DU123:1") is None
    assert journal.read_after(0, 100) == []
    assert db.execute("SELECT * FROM test_position_writes", fetch="all") == []

    # ...nor on a fresh instance simulating a restart -- cursor stream and
    # materialized state are consistent across the "crash".
    recovered = _reopen(db, migrator)
    assert recovered.get_entity("position", "DU123:1") is None
    assert recovered.read_after(0, 100) == []

    # The journal is still fully usable: a clean retry with the SAME
    # event_id succeeds normally and starts the entity at revision 1 (the
    # crashed attempt left no revision behind to collide with).
    good_conn = recovered.connect()
    event = recovered.mutate(good_conn, mutation, lambda c, r: None, event_id="e1")
    assert event.entity_revision == 1
    assert recovered.get_entity("position", "DU123:1")["payload"] == {"quantity": 10}


# --------------------------------------------------------------------- #
# Boundary 2: crash at journal-insert (after materialized write, before
# commit) -- the INSERT into domain_event_journal genuinely runs (burning
# a cursor value via nextval()), but the crash happens before COMMIT, so
# nothing durable survives -- proving atomicity extends past the insert
# itself, not just up to the materialized-ledger write.
# --------------------------------------------------------------------- #

def test_crash_at_journal_insert_rolls_back_ledger_and_caller_write_together(bootstrap, monkeypatch):
    db, migrator, journal = bootstrap
    mutation = _mutation("DU123:2", 20)

    def write_materialized(conn, revision):
        conn.execute("INSERT INTO test_position_writes VALUES (?, ?)", ["DU123:2", revision])

    original_insert = journal._insert_journal_row

    def crash_after_journal_insert(conn, event_id, mut, revision, received_ts):
        original_insert(conn, event_id, mut, revision, received_ts)  # the INSERT genuinely runs
        raise RuntimeError("crash at journal-insert")

    monkeypatch.setattr(journal, "_insert_journal_row", crash_after_journal_insert)

    conn = journal.connect()
    with pytest.raises(RuntimeError, match="journal-insert"):
        journal.mutate(conn, mutation, write_materialized, event_id="e2")

    assert journal.get_entity("position", "DU123:2") is None
    assert journal.read_after(0, 100) == []
    assert db.execute("SELECT * FROM test_position_writes", fetch="all") == []

    recovered = _reopen(db, migrator)
    assert recovered.get_entity("position", "DU123:2") is None
    assert recovered.read_after(0, 100) == []

    # A fresh event_id proves the journal is healthy post-crash. Per RA-4
    # the aborted INSERT's nextval() call burned a cursor value, so we
    # only assert relative ordering, never an absolute cursor.
    good_conn = recovered.connect()
    event = recovered.mutate(good_conn, mutation, lambda c, r: None, event_id="e2-retry")
    assert event.entity_revision == 1


# --------------------------------------------------------------------- #
# Boundary 3: crash at commit -- the commit statement itself never
# applies. DuckDB's commit is atomic (all-or-nothing), so the OBSERVABLE
# data outcome is identical to boundary 2 (nothing durable); this test
# exists to prove the code path is ALSO covered when the failure is
# raised by the commit call itself, not by application code before it.
# --------------------------------------------------------------------- #

def test_crash_at_commit_leaves_nothing_durable_and_recovers_cleanly(bootstrap):
    db, migrator, journal = bootstrap
    mutation = _mutation("DU123:3", 30)

    def write_materialized(conn, revision):
        conn.execute("INSERT INTO test_position_writes VALUES (?, ?)", ["DU123:3", revision])

    wrapped_conn = _CommitCrashingConn(journal.connect())
    with pytest.raises(RuntimeError, match="crash at commit"):
        journal.mutate(wrapped_conn, mutation, write_materialized, event_id="e3")

    assert journal.get_entity("position", "DU123:3") is None
    assert journal.read_after(0, 100) == []
    assert db.execute("SELECT * FROM test_position_writes", fetch="all") == []

    recovered = _reopen(db, migrator)
    assert recovered.get_entity("position", "DU123:3") is None
    assert recovered.read_after(0, 100) == []

    good_conn = recovered.connect()
    event = recovered.mutate(good_conn, mutation, lambda c, r: None, event_id="e3-retry")
    assert event.entity_revision == 1


# --------------------------------------------------------------------- #
# Boundary 4: crash at post-commit-signal -- the event IS durable (COMMIT
# succeeded and `_write_lock` has already been released), but the signal
# to wake long-poll readers never fires. Recovery must surface the event
# (it is NOT lost) and heal the missed wakeup on restart.
# --------------------------------------------------------------------- #

def test_crash_at_post_commit_signal_surfaces_the_durable_event_on_recovery(bootstrap, monkeypatch):
    db, migrator, journal = bootstrap
    mutation = _mutation("DU123:4", 40)

    original_signal = journal._signal_commit
    state = {"crashed_once": False}

    def crash_once_then_real(source_cursor):
        if not state["crashed_once"]:
            state["crashed_once"] = True
            raise RuntimeError("crash at post-commit-signal")
        return original_signal(source_cursor)

    monkeypatch.setattr(journal, "_signal_commit", crash_once_then_real)

    watermark_before = journal._latest_committed_cursor
    conn = journal.connect()
    with pytest.raises(RuntimeError, match="post-commit-signal"):
        journal.mutate(conn, mutation, lambda c, r: None, event_id="e4")

    # The write DID durably commit -- the crash happened strictly AFTER
    # `with self._write_lock:` exited (see domain_journal.py's `mutate`
    # docstring) -- so it must be fully visible on the SAME instance...
    entity = journal.get_entity("position", "DU123:4")
    assert entity is not None
    assert entity["payload"] == {"quantity": 40}
    committed = journal.read_after(0, 100)
    assert [e.event_id for e in committed] == ["e4"]
    durable_cursor = committed[0].source_cursor

    # ...but the in-memory watermark was NOT bumped -- the signal never ran.
    assert journal._latest_committed_cursor == watermark_before

    # Recovery: a fresh instance (simulated restart) resyncs the watermark
    # from durable state via `_sync_latest_committed_cursor` inside
    # `migrate()` -- the missed signal is healed, not lost forever.
    recovered = _reopen(db, migrator)
    assert recovered._latest_committed_cursor >= durable_cursor

    # A waiter on the RECOVERED instance for a cursor just before the
    # healed event must not hang -- it returns almost immediately with the
    # resynced watermark, proving the lost wakeup doesn't strand anyone
    # post-recovery.
    started = time.monotonic()
    woke_at = recovered.wait_for_cursor_after(durable_cursor - 1, deadline=time.monotonic() + 5.0)
    elapsed = time.monotonic() - started
    assert woke_at >= durable_cursor
    assert elapsed < 1.0

    # Idempotent retry on the ORIGINAL (crashed-once) instance with the
    # SAME event_id is the caller-side mitigation for "did my write
    # actually succeed?" uncertainty (RA-7) -- it must return the SAME
    # event, never raise EventIdentityConflict, and never create a
    # duplicate row (cursor stream stays intact, no torn row).
    retry_conn = journal.connect()
    retried = journal.mutate(retry_conn, mutation, lambda c, r: None, event_id="e4")
    assert retried.source_cursor == durable_cursor
    assert len(journal.read_after(0, 100)) == 1

    # This second call's signal (no longer crashing) DID advance the
    # watermark -- the original instance is fully healthy again too.
    assert journal._latest_committed_cursor >= durable_cursor


# --------------------------------------------------------------------- #
# A stuck reader must still get a real CursorExpired (not silently lose
# data) if a compaction cycle force-expires it in between crash-recovery
# cycles -- ties Task 5's feed contract to Task 6's retention together
# end-to-end, as a closing sanity check for the whole assembly.
# --------------------------------------------------------------------- #

def test_a_stuck_reader_gets_cursor_expired_not_silence_after_compaction(bootstrap):
    db, migrator, journal = bootstrap
    feed = DomainFeedService(journal)

    conn = journal.connect()
    stuck_at = journal.mutate(conn, _mutation("DU123:5", 50), lambda c, r: None, event_id="e5")

    far_future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=40)
    journal.compact(now=far_future, active_cursors={})  # nobody "active" -- stuck_at is force-expired

    with pytest.raises(CursorExpired):
        feed.read_domain_events(stuck_at.source_cursor - 1, 100, 0)
