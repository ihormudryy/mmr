"""Tests for journal retention/compaction ([M1-F1] Task 6) -- the capstone
that bounds ``domain_event_journal`` growth and produces the checkpoint
``DomainFeedService``'s ``CursorExpired`` check (Task 5) reads.

FROZEN CONTRACTS under test:
- ``DomainJournal.compact(now, active_cursors) -> CompactionResult``
  (``oldest_retained_cursor``, ``newest_cursor``, ``deleted_count``,
  ``completed_at``).
- ``DomainJournal.latest_checkpoint() -> Optional[CheckpointRecord]``.

Retention rule (binding, plan Global Constraint): an event survives if
EITHER it is within the 30-day floor (``RETENTION_FLOOR``) of ``now``, OR
it is at/after a LIVE ``active_cursors`` entry -- one that ITSELF still
falls within that same floor. A cursor already behind the floor is
FORCE-EXPIRED: it is excluded entirely (a dead/stuck client must never pin
the journal open forever).

Every test here controls ``received_timestamp`` via a RAW SQL insert
(``_insert_raw_event``) rather than going through ``DomainJournal.mutate``,
which always stamps the REAL wall clock -- there is no public knob to make
an event look 45 days old through the ordinary API. This mirrors
``test_domain_journal.py``'s own
``test_source_cursor_is_sparse_after_a_rolled_back_write``, which uses the
same raw-SQL technique to engineer a schema-level condition the public API
doesn't expose. Per that file's own convention, no test below ever asserts
an absolute cursor value -- only relative/observed ones (RA-4: cursors are
monotonic but SPARSE).
"""
import datetime as dt
import threading
import time

import pytest

from trader.data.domain_journal import (
    CompactionResult,
    DomainJournal,
    RETENTION_FLOOR,
)
from trader.data.duckdb_store import DuckDBConnection
from trader.data.materialized_state import GenericEntityAdapter
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.domain.feed_service import CursorExpired, DomainFeedService
from trader.domain.snapshot_service import DomainSnapshotService

# A fixed synthetic "now" for compact() calls -- entirely decoupled from
# the real wall clock (all `received_timestamp` values below are also
# synthetic, via raw inserts), so these tests are deterministic regardless
# of when the suite actually runs.
NOW = dt.datetime(2026, 8, 15, tzinfo=dt.timezone.utc)


def _insert_raw_event(journal, *, event_id, entity_id, received_timestamp):
    """Insert a `domain_event_journal` row with a FULLY CONTROLLED
    `received_timestamp`, bypassing `mutate()` (which always stamps the
    real wall clock). Returns the assigned `source_cursor`.
    """
    conn = journal.connect()
    conn.execute("BEGIN TRANSACTION")
    row = conn.execute(
        "INSERT INTO domain_event_journal "
        "(event_id, entity_revision, event_type, entity_type, entity_id, "
        " operation, account_id, source, source_timestamp, received_timestamp, "
        " correlation_id, payload) VALUES "
        "(?, 1, 'position.updated', 'position', ?, 'upsert', NULL, 'test', ?, ?, NULL, '{}') "
        "RETURNING source_cursor",
        [event_id, entity_id, received_timestamp, received_timestamp],
    ).fetchone()
    conn.execute("COMMIT")
    return row[0]


@pytest.fixture
def journal(tmp_duckdb_path):
    db = DuckDBConnection.get_instance(tmp_duckdb_path)
    migrator = SchemaMigrator(db)
    j = DomainJournal(db)
    j.migrate(migrator)
    return j


@pytest.fixture
def make_journal(tmp_path):
    """Factory for INDEPENDENT journals on distinct files -- used by the
    subsumption test, which must run two isolated compactions (one with a
    live active cursor, one with ``{}``) over identical starting state and
    compare their outcomes.
    """
    from uuid import uuid4

    def _make():
        path = str(tmp_path / f"journal_{uuid4().hex[:8]}.duckdb")
        db = DuckDBConnection.get_instance(path)
        migrator = SchemaMigrator(db)
        j = DomainJournal(db)
        j.migrate(migrator)
        return j

    return _make


# --------------------------------------------------------------------- #
# Brief's verbatim retention test, adapted to a REAL observed cursor
# instead of a hardcoded absolute value (RA-4 -- see module docstring).
#
# NOTE (T6 adversarial-panel fix): the brief's original assertion
# `oldest_retained_cursor <= dashboard_cursor` is trivially true for ANY
# retention point and proved nothing about the active cursor. Retention is
# EXACTLY the time floor (the min within-floor row), and `active_cursors`
# is subsumed by it (see `compact()`'s docstring and
# `test_live_within_floor_active_cursor_is_subsumed_by_the_time_floor`).
# This test now honestly verifies what it can: a checkpoint is written and
# retention equals the time floor.
# --------------------------------------------------------------------- #

def test_compaction_writes_a_checkpoint_and_retention_equals_the_time_floor(journal):
    # A single within-floor row -- so the time floor IS this row's cursor.
    within_floor_cursor = _insert_raw_event(
        journal, event_id="e-dashboard", entity_id="p-dashboard",
        received_timestamp=NOW - dt.timedelta(days=5),
    )

    result = journal.compact(now=NOW, active_cursors={"dashboard": within_floor_cursor})

    assert isinstance(result, CompactionResult)
    # Retention is the time floor exactly (the only within-floor row) --
    # NOT because the active cursor "preserved" anything.
    assert result.oldest_retained_cursor == within_floor_cursor
    assert result.deleted_count == 0
    assert journal.latest_checkpoint() is not None
    assert journal.latest_checkpoint().oldest_retained_cursor == within_floor_cursor


# --------------------------------------------------------------------- #
# Additional coverage
# --------------------------------------------------------------------- #

def test_active_cursor_older_than_floor_is_force_expired_and_ignored(journal):
    stuck_cursor = _insert_raw_event(
        journal, event_id="e-old", entity_id="p-old",
        received_timestamp=NOW - dt.timedelta(days=45),
    )
    fresh_cursor = _insert_raw_event(
        journal, event_id="e-fresh", entity_id="p-fresh",
        received_timestamp=NOW - dt.timedelta(days=5),
    )

    result = journal.compact(now=NOW, active_cursors={"dead_client": stuck_cursor})

    # The dead client's 45-day-old cursor must NOT pin retention back to
    # it -- the 30-day floor (fresh_cursor's position) wins regardless.
    assert result.oldest_retained_cursor == fresh_cursor
    assert result.oldest_retained_cursor > stuck_cursor
    remaining = journal.read_after(0, 100)
    assert [e.event_id for e in remaining] == ["e-fresh"]
    assert result.deleted_count == 1


def test_event_exactly_at_cutoff_boundary_is_retained(journal):
    # RA-5-style strict inequality: an event exactly AT the 30-day floor
    # (received_timestamp == now - RETENTION_FLOOR) survives -- the floor
    # is inclusive, only STRICTLY older rows are candidates for deletion.
    boundary_cursor = _insert_raw_event(
        journal, event_id="e-boundary", entity_id="p-boundary",
        received_timestamp=NOW - RETENTION_FLOOR,
    )

    result = journal.compact(now=NOW, active_cursors={})

    assert result.oldest_retained_cursor == boundary_cursor
    assert result.deleted_count == 0
    assert [e.event_id for e in journal.read_after(0, 100)] == ["e-boundary"]


def test_event_older_than_floor_with_no_active_cursor_is_deleted(journal):
    _insert_raw_event(
        journal, event_id="e-old", entity_id="p-old",
        received_timestamp=NOW - RETENTION_FLOOR - dt.timedelta(seconds=1),
    )
    fresh_cursor = _insert_raw_event(
        journal, event_id="e-fresh", entity_id="p-fresh",
        received_timestamp=NOW - dt.timedelta(days=1),
    )

    result = journal.compact(now=NOW, active_cursors={})

    assert result.deleted_count == 1
    assert result.oldest_retained_cursor == fresh_cursor
    assert [e.event_id for e in journal.read_after(0, 100)] == ["e-fresh"]


def test_below_floor_active_cursor_does_not_extend_retention(journal):
    # A below-floor active cursor is FORCE-EXPIRED: it must NOT extend
    # retention to protect the stale rows at/after it (the old, vacuous
    # framing was "never deletes an event at/after a live active cursor" --
    # false: c2 IS at/after the passed cursor and IS deleted, precisely
    # because that cursor is force-expired).
    _insert_raw_event(journal, event_id="e1", entity_id="p1", received_timestamp=NOW - dt.timedelta(days=40))
    c2 = _insert_raw_event(journal, event_id="e2", entity_id="p2", received_timestamp=NOW - dt.timedelta(days=35))
    c3 = _insert_raw_event(journal, event_id="e3", entity_id="p3", received_timestamp=NOW - dt.timedelta(days=1))

    # c2 (35 days old) is itself past the floor -- force-expired -- so
    # passing it as an active cursor protects nothing; both e1 AND e2
    # (the row AT the passed cursor) are deleted, only c3 (within floor)
    # survives.
    result = journal.compact(now=NOW, active_cursors={"dashboard": c2})

    remaining_ids = [e.event_id for e in journal.read_after(0, 100)]
    assert remaining_ids == ["e3"]
    assert result.oldest_retained_cursor == c3
    assert result.deleted_count == 2  # e1 AND e2 -- the below-floor cursor did not save e2


def test_live_within_floor_active_cursor_is_subsumed_by_the_time_floor(make_journal):
    # The ONE genuinely distinguishing test for `active_cursors`: a LIVE
    # (within-floor) active cursor produces the EXACT SAME retention as
    # `{}`, BECAUSE the time floor already protects everything at/after it.
    # This honestly encodes the subsumption invariant -- it does NOT
    # pretend the parameter is load-bearing. Two independent journals with
    # identical starting state are compacted (one with the live cursor, one
    # with `{}`) and their outcomes compared.
    def seed(j):
        _insert_raw_event(j, event_id="e-old", entity_id="p-old", received_timestamp=NOW - dt.timedelta(days=45))
        live = _insert_raw_event(j, event_id="e-live", entity_id="p-live", received_timestamp=NOW - dt.timedelta(days=3))
        return live

    j_with = make_journal()
    live_cursor = seed(j_with)
    result_with = j_with.compact(now=NOW, active_cursors={"dashboard": live_cursor})

    j_without = make_journal()
    live_cursor_2 = seed(j_without)
    result_without = j_without.compact(now=NOW, active_cursors={})

    # Identical starting state ⇒ identical cursor assignment (both journals
    # are fresh sequences) ⇒ the live cursor and `{}` yield the SAME
    # retention point and deletion count.
    assert live_cursor == live_cursor_2
    assert result_with.oldest_retained_cursor == result_without.oldest_retained_cursor
    assert result_with.deleted_count == result_without.deleted_count
    # ...and that shared retention point IS the time floor (the live row),
    # which is what actually protected it -- not the parameter.
    assert result_with.oldest_retained_cursor == live_cursor
    assert [e.event_id for e in j_with.read_after(0, 100)] == ["e-live"]


def test_compact_on_empty_journal_writes_a_checkpoint_and_deletes_nothing(journal):
    result = journal.compact(now=NOW, active_cursors={})

    assert result.deleted_count == 0
    assert result.newest_cursor == 0
    # Defect-2 fix: an empty journal must retain-from 0, NOT 1. The prior
    # `newest_cursor + 1` clamp set this to 1 and spuriously expired a
    # cold-start reader.
    assert result.oldest_retained_cursor == 0
    assert journal.latest_checkpoint() is not None
    assert journal.latest_checkpoint().oldest_retained_cursor == 0

    # And a cold-start reader (snapshot over the empty journal yields
    # source_cursor 0) tailing from 0 must NOT get a spurious CursorExpired.
    feed = DomainFeedService(journal)
    heartbeat = feed.read_domain_events(0, 100, 0)  # must not raise
    assert heartbeat.events == ()


def test_compact_all_stale_journal_retains_newest_row_and_does_not_expire_a_caught_up_reader(journal):
    # Defect-2 fix (all-stale symmetric case): every row is older than the
    # 30-day floor and no live active cursor exists. The prior
    # `newest_cursor + 1` clamp deleted the NEWEST row too and expired a
    # reader caught up to it. The newest row must survive and a reader at
    # `newest_cursor` must not be expired.
    _insert_raw_event(journal, event_id="e-old1", entity_id="p1", received_timestamp=NOW - dt.timedelta(days=60))
    newest_cursor = _insert_raw_event(
        journal, event_id="e-old2", entity_id="p2", received_timestamp=NOW - dt.timedelta(days=45)
    )

    result = journal.compact(now=NOW, active_cursors={})

    assert result.newest_cursor == newest_cursor
    assert result.oldest_retained_cursor == newest_cursor
    # The newest (still-stale) row survives; only strictly-older rows go.
    assert [e.event_id for e in journal.read_after(0, 100)] == ["e-old2"]
    assert result.deleted_count == 1

    # A reader caught up to `newest_cursor` tails from there without a
    # spurious CursorExpired.
    feed = DomainFeedService(journal)
    heartbeat = feed.read_domain_events(newest_cursor, 100, 0)  # must not raise
    assert heartbeat.events == ()


def test_latest_checkpoint_is_none_before_any_compaction(journal):
    assert journal.latest_checkpoint() is None


def test_latest_checkpoint_reflects_the_most_recent_of_multiple_compactions(journal):
    first = journal.compact(now=NOW, active_cursors={})
    second = journal.compact(now=NOW + dt.timedelta(days=1), active_cursors={})

    checkpoint = journal.latest_checkpoint()
    assert checkpoint is not None
    assert checkpoint.oldest_retained_cursor == second.oldest_retained_cursor
    assert checkpoint.created_at >= first.completed_at


def test_compact_rejects_a_naive_now(journal):
    with pytest.raises(ValueError, match="timezone-aware"):
        journal.compact(now=dt.datetime(2026, 8, 15), active_cursors={})


def test_compact_carries_forward_broker_generation_instead_of_resetting_it(journal):
    # Simulate a prior [M1-F2] promotion by inserting a checkpoint row
    # directly (compact() itself never invents a non-zero generation in
    # F1 -- see module docstring). A subsequent retention sweep must NOT
    # reset this back to 0.
    conn = journal.connect()
    conn.execute(
        "INSERT INTO domain_snapshot_checkpoints "
        "(created_at, newest_cursor, oldest_retained_cursor, broker_generation) "
        "VALUES (?, 0, 0, 7)",
        [NOW - dt.timedelta(days=10)],
    )

    journal.compact(now=NOW, active_cursors={})

    checkpoint = journal.latest_checkpoint()
    assert checkpoint.broker_generation == 7


def test_compact_succeeds_repeatedly_and_the_journal_stays_usable_after_checkpoint(journal):
    _insert_raw_event(journal, event_id="e1", entity_id="p1", received_timestamp=NOW - dt.timedelta(days=1))

    journal.compact(now=NOW, active_cursors={})

    # A normal write must still work fine after CHECKPOINT has run.
    conn = journal.connect()
    mutation = DomainMutation(
        event_type="position.updated", entity_type="position", entity_id="p2",
        operation="upsert", account_id=None, source="test",
        source_timestamp=NOW, correlation_id=None, payload={"quantity": 1},
    )
    event = journal.mutate(conn, mutation, lambda c, r: None, event_id="e2")
    assert event.entity_revision == 1

    # And a second compaction (checkpoint-after-checkpoint) must also work.
    result2 = journal.compact(now=NOW + dt.timedelta(days=1), active_cursors={})
    assert result2.deleted_count >= 0


def test_compact_blocks_until_an_in_flight_snapshot_read_completes(journal):
    # Proves the `fenced_read_lock` coordination in domain_journal.py /
    # snapshot_service.py actually serializes compact() against a
    # fenced snapshot read -- this task's binding concurrency requirement
    # ("ensure compaction can't run concurrently with a long-poll read
    # holding a transaction").
    service = DomainSnapshotService(journal)
    service.register_adapter(GenericEntityAdapter("position"))
    _insert_raw_event(journal, event_id="e1", entity_id="p1", received_timestamp=NOW - dt.timedelta(days=1))

    entered_read = threading.Event()
    release_snapshot = threading.Event()

    def hold_open():
        entered_read.set()
        release_snapshot.wait(timeout=2)

    snapshot_thread = threading.Thread(
        target=service.snapshot_with_cursor, kwargs={"on_read_started": hold_open}
    )
    snapshot_thread.start()
    assert entered_read.wait(timeout=1), "snapshot never entered its read transaction"

    compact_done = threading.Event()

    def run_compact():
        journal.compact(now=NOW, active_cursors={})
        compact_done.set()

    compact_thread = threading.Thread(target=run_compact)
    compact_thread.start()

    assert not compact_done.wait(timeout=0.3), (
        "compact() proceeded while a fenced snapshot read was still open -- "
        "fenced_read_lock did not serialize them"
    )

    release_snapshot.set()
    assert compact_done.wait(timeout=2), "compact() never completed after the snapshot released"
    snapshot_thread.join(timeout=2)
    compact_thread.join(timeout=2)


# --------------------------------------------------------------------- #
# T6 obligations carried from the T5 verification panel (see task brief):
# these were "behavior-equivalent" without a real deletion path -- now
# that compact() exists, they must be genuinely distinguishing.
# --------------------------------------------------------------------- #

def test_cursor_expired_after_compaction_force_expires_a_stale_position(journal):
    old_cursor = _insert_raw_event(
        journal, event_id="e-old", entity_id="p-old",
        received_timestamp=NOW - dt.timedelta(days=45),
    )
    fresh_cursor = _insert_raw_event(
        journal, event_id="e-fresh", entity_id="p-fresh",
        received_timestamp=NOW - dt.timedelta(days=1),
    )
    journal.compact(now=NOW, active_cursors={})  # old_cursor's row is force-expired/deleted

    feed = DomainFeedService(journal)
    with pytest.raises(CursorExpired) as excinfo:
        feed.read_domain_events(old_cursor, 100, 0)

    # A real, durable raise -- NOT a heartbeat -- distinguishing this from
    # every pre-Task-6 CursorExpired test, which only ever simulated the
    # checkpoint row by hand.
    assert excinfo.value.after_cursor == old_cursor
    assert excinfo.value.oldest_retained_cursor == fresh_cursor


def test_empty_heartbeat_after_compaction_reports_newest_cursor_not_a_bare_echo(journal):
    """Before Task 6, `newest_cursor = max(after_cursor, cursor_at_wake)`
    was provably equivalent to a bare `after_cursor` echo in every
    reachable case -- feed_service.py's own docstring: "nothing can
    concurrently remove a row between the wakeup and the read." Real
    prefix-deletion compaction can never by itself produce a case where
    `after_cursor` is still valid (not expired) yet the row backing
    `cursor_at_wake` has been deleted (deleting that row necessarily also
    pushes `oldest_retained_cursor` past `after_cursor`, so
    `CursorExpired` would fire first) -- the only way this gap is
    reachable is a genuine RACE between a commit's signal and a
    concurrent compaction. Simulating the signal directly (mirrors
    test_domain_feed.py::test_spurious_wakeup_does_not_cause_premature_empty_return's
    use of the same internal hook) proves the formula is correct without
    depending on winning a real thread-timing race.
    """
    conn = journal.connect()
    mutation = DomainMutation(
        event_type="position.updated", entity_type="position", entity_id="p1",
        operation="upsert", account_id=None, source="test",
        source_timestamp=NOW, correlation_id=None, payload={"quantity": 1},
    )
    caught_up = journal.mutate(conn, mutation, lambda c, r: None, event_id="e1")

    # Simulate: a later commit's signal landed (bumping the watermark),
    # and that row has SINCE been compacted away -- the in-memory
    # watermark never regresses (compaction only trims durable rows, not
    # `_latest_committed_cursor`), so a reader waking up after this must
    # still learn about it rather than silently reporting a bare echo.
    phantom_cursor = caught_up.source_cursor + 100
    journal._signal_commit(phantom_cursor)

    feed = DomainFeedService(journal)
    result = feed.read_domain_events(caught_up.source_cursor, 100, 0)

    assert result.events == ()
    assert result.newest_cursor == phantom_cursor
    assert result.newest_cursor > caught_up.source_cursor
