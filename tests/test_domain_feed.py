"""Tests for the cursor long-poll feed ([M1-F1] Task 5) -- the tailing half
of the fenced snapshot/tail pair every downstream consumer (dashboard,
[M1-R]) rides on.

FROZEN CONTRACTS under test:
- `DomainFeedService.read_domain_events(after_cursor, limit, wait_ms) ->
  ReadDomainEventsResult` (`events: tuple[DomainEvent, ...]`,
  `newest_cursor: int`, per `trader/domain/events.py`, Task 2).
- `CursorExpired` (RA-5): `after_cursor < oldest_retained_cursor`.
- RPC registration: `read_domain_events` on role `feed` (port 42103),
  `snapshot_with_cursor` on role `query` (port 42101), via
  `production_api.build_production_registry`.

Long-poll wakeup design under test (see feed_service.py's module docstring
and domain_journal.py's `_signal_commit`/`wait_for_cursor_after` for the
full rationale):
- `DomainJournal` bumps its in-memory `_latest_committed_cursor` and calls
  `notify_all()` ONLY after a transaction has durably COMMITTED and only
  after `_write_lock` is released -- never before commit (a pre-commit
  signal is a lost/false wakeup).
- The reader's wait loop re-checks its predicate under the condition lock
  in a `while` (guards spurious wakeups) and never performs DB I/O while
  holding that lock.
- An empty (timed-out) read reports the journal's actual newest known
  cursor, not a bare echo of `after_cursor` -- see
  `test_empty_heartbeat_reports_actual_newest_cursor`.
"""
from __future__ import annotations

import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from trader.data.domain_journal import RETENTION_FLOOR, DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.domain.feed_service import CursorExpired, DomainFeedService
from trader.domain.snapshot_service import DomainSnapshotService, SnapshotNotReady
from trader.messaging.production_api import build_production_registry
from trader.messaging.typed_rpc import HmacServiceAuthenticator

UTC_NOW = dt.datetime(2026, 7, 16, 12, 0, tzinfo=dt.timezone.utc)

HMAC_KEY = b"k" * 32


class _Writer:
    """Test harness: writes `account` entities straight through
    `DomainJournal.mutate`. No dedicated materialized table of its own --
    Task 3's own generic `domain_materialized_entities` ledger is all a bare
    `account` entity needs (mirrors `test_domain_snapshot.py`'s `_Writer`).
    """

    def __init__(self, journal: DomainJournal):
        self.journal = journal

    def account(self, net_liquidation: float, event_id: str):
        conn = self.journal.connect()
        mutation = DomainMutation(
            event_type="account.updated",
            entity_type="account",
            entity_id="DU123",
            operation="upsert",
            account_id="DU123",
            source="test",
            source_timestamp=UTC_NOW,
            correlation_id=None,
            payload={"net_liquidation": net_liquidation},
        )
        return self.journal.mutate(conn, mutation, lambda _conn, _rev: None, event_id=event_id)


@pytest.fixture
def journal(tmp_duckdb_path):
    db = DuckDBConnection.get_instance(tmp_duckdb_path)
    migrator = SchemaMigrator(db)
    j = DomainJournal(db)
    j.migrate(migrator)
    return j


@pytest.fixture
def writer(journal):
    return _Writer(journal)


@pytest.fixture
def feed_service(journal):
    return DomainFeedService(journal)


@pytest.fixture
def executor():
    with ThreadPoolExecutor(max_workers=4) as pool:
        yield pool


def _insert_checkpoint(journal: DomainJournal, oldest_retained_cursor: int, newest_cursor: int) -> None:
    conn = journal.connect()
    conn.execute(
        "INSERT INTO domain_snapshot_checkpoints "
        "(created_at, newest_cursor, oldest_retained_cursor, broker_generation) "
        "VALUES (?, ?, ?, ?)",
        [UTC_NOW, newest_cursor, oldest_retained_cursor, 0],
    )


@pytest.fixture
def feed_service_past_retention(tmp_duckdb_path):
    """A DIFFERENT journal (not the plain `journal`/`feed_service` fixtures)
    that already carries a compaction-style checkpoint recording
    `oldest_retained_cursor=10`. Deliberately NOT shared with the plain
    `feed_service` fixture: seeding every test's journal with a retention
    floor would make `after_cursor=0` (the totally normal "nothing consumed
    yet" genesis value used by `test_long_poll_wakes_after_commit`) look
    expired too, since 0 < 10. Real compaction (Task 6, not yet
    implemented) always writes this same checkpoint row before deleting
    anything -- this fixture stands in for "Task 6 has run once already".
    """
    db = DuckDBConnection.get_instance(tmp_duckdb_path)
    migrator = SchemaMigrator(db)
    j = DomainJournal(db)
    j.migrate(migrator)
    _insert_checkpoint(j, oldest_retained_cursor=10, newest_cursor=20)
    return DomainFeedService(j)


# --------------------------------------------------------------------- #
# Brief's verbatim wake-up + cursor-expiry tests
# --------------------------------------------------------------------- #

def test_long_poll_wakes_after_commit(feed_service, writer, executor):
    future = executor.submit(feed_service.read_domain_events, 0, 100, 10_000)
    time.sleep(0.05)  # give the reader a chance to actually enter wait()
    writer.account(net_liquidation=50_000, event_id="a1")
    result = future.result(timeout=1)
    assert [event.event_id for event in result.events] == ["a1"]


def test_cursor_before_retention_requires_snapshot(feed_service_past_retention):
    with pytest.raises(CursorExpired):
        feed_service_past_retention.read_domain_events(7, 100, 0)


# --------------------------------------------------------------------- #
# Additional wakeup-correctness coverage
# --------------------------------------------------------------------- #

def test_events_already_present_return_without_waiting(feed_service, writer):
    writer.account(net_liquidation=10_000, event_id="a1")
    started = time.monotonic()
    result = feed_service.read_domain_events(0, 100, 10_000)
    elapsed = time.monotonic() - started
    assert [e.event_id for e in result.events] == ["a1"]
    # Must return almost immediately -- if _latest_committed_cursor weren't
    # synced from the durable journal at migrate()-time, this would block
    # for the full 10s wait_ms despite the data already being there.
    assert elapsed < 2.0


def test_multiple_waiters_all_wake_on_one_commit(feed_service, writer, executor):
    # notify_all() (not notify()) must wake every waiter whose predicate is
    # satisfied by the same commit, not just one of them.
    future_a = executor.submit(feed_service.read_domain_events, 0, 100, 10_000)
    future_b = executor.submit(feed_service.read_domain_events, 0, 100, 10_000)
    time.sleep(0.05)
    writer.account(net_liquidation=1_000, event_id="a1")
    result_a = future_a.result(timeout=1)
    result_b = future_b.result(timeout=1)
    assert [e.event_id for e in result_a.events] == ["a1"]
    assert [e.event_id for e in result_b.events] == ["a1"]


def test_waiter_with_unmet_predicate_is_not_woken_by_an_unrelated_commit(feed_service, writer, executor):
    writer.account(net_liquidation=1_000, event_id="a1")
    first = writer.account(net_liquidation=2_000, event_id="a2")
    # This waiter is already caught up to a2's cursor -- a commit at a2's
    # cursor must not (spuriously) satisfy it; only a THIRD, later commit
    # should.
    future = executor.submit(feed_service.read_domain_events, first.source_cursor, 100, 10_000)
    time.sleep(0.1)
    assert not future.done()
    writer.account(net_liquidation=3_000, event_id="a3")
    result = future.result(timeout=1)
    assert [e.event_id for e in result.events] == ["a3"]


def test_spurious_wakeup_does_not_cause_premature_empty_return(feed_service, writer, journal, executor):
    # Directly fire notify_all() with NO real commit behind it -- simulates
    # the OS-level spurious wakeup a condition variable's wait() may return
    # from unprompted. The reader's `while` re-check must swallow this and
    # keep waiting for the real commit that follows.
    future = executor.submit(feed_service.read_domain_events, 0, 100, 5_000)
    time.sleep(0.05)
    with journal._commit_condition:
        journal._commit_condition.notify_all()
    time.sleep(0.05)
    assert not future.done(), "spurious notify_all() must not produce a premature return"
    writer.account(net_liquidation=1_000, event_id="real-commit")
    result = future.result(timeout=1)
    assert [e.event_id for e in result.events] == ["real-commit"]


def test_wait_ms_zero_with_no_data_returns_immediately(feed_service):
    started = time.monotonic()
    result = feed_service.read_domain_events(0, 100, 0)
    elapsed = time.monotonic() - started
    assert result.events == ()
    assert elapsed < 1.0


def test_wait_ms_negative_is_clamped_to_zero(feed_service):
    started = time.monotonic()
    result = feed_service.read_domain_events(0, 100, -500)
    elapsed = time.monotonic() - started
    assert result.events == ()
    assert elapsed < 1.0


# --------------------------------------------------------------------- #
# Empty heartbeat semantics
# --------------------------------------------------------------------- #

def test_empty_heartbeat_reports_actual_newest_cursor_not_zero(feed_service, writer):
    event = writer.account(net_liquidation=10_000, event_id="a1")
    result = feed_service.read_domain_events(event.source_cursor, 100, 0)
    assert result.events == ()
    assert result.newest_cursor == event.source_cursor


def test_cursor_ahead_of_newest_is_a_heartbeat_not_an_error(feed_service):
    # A cursor with no established retention floor (no checkpoint row) is
    # never "expired", even one that's absurdly far ahead of anything the
    # journal has ever seen -- this is a normal (if unusual) heartbeat, per
    # RA-5's "don't conflate ahead-of-newest with expired".
    result = feed_service.read_domain_events(999_999, 100, 0)
    assert result.events == ()
    assert result.newest_cursor == 999_999


def test_empty_journal_heartbeat_returns_cursor_zero(feed_service):
    result = feed_service.read_domain_events(0, 100, 0)
    assert result.events == ()
    assert result.newest_cursor == 0


# --------------------------------------------------------------------- #
# limit/wait_ms clamping
# --------------------------------------------------------------------- #

def test_limit_is_clamped_to_at_least_one(feed_service, writer):
    writer.account(net_liquidation=1_000, event_id="a1")
    writer.account(net_liquidation=2_000, event_id="a2")
    result = feed_service.read_domain_events(0, 0, 0)  # limit=0 clamped to 1
    assert len(result.events) == 1


def test_limit_is_clamped_to_at_most_1000(feed_service, writer):
    for i in range(5):
        writer.account(net_liquidation=float(i), event_id=f"a{i}")
    result = feed_service.read_domain_events(0, 5_000, 0)  # clamped to 1000
    assert len(result.events) == 5  # only 5 exist; clamp just caps the ask


def test_events_are_ordered_by_cursor_and_respect_limit(feed_service, writer):
    for i in range(5):
        writer.account(net_liquidation=float(i), event_id=f"a{i}")
    result = feed_service.read_domain_events(0, 2, 0)
    assert len(result.events) == 2
    cursors = [e.source_cursor for e in result.events]
    assert cursors == sorted(cursors)
    assert result.newest_cursor == result.events[-1].source_cursor


# --------------------------------------------------------------------- #
# CursorExpired
# --------------------------------------------------------------------- #

def test_cursor_expired_carries_after_and_oldest_retained(feed_service_past_retention):
    with pytest.raises(CursorExpired) as excinfo:
        feed_service_past_retention.read_domain_events(7, 100, 0)
    assert excinfo.value.after_cursor == 7
    assert excinfo.value.oldest_retained_cursor == 10


def test_cursor_at_exactly_oldest_retained_is_not_expired(feed_service_past_retention):
    # Strict inequality (RA-5: after_cursor < oldest_retained_cursor) -- a
    # cursor exactly AT the retained floor still has everything after it.
    result = feed_service_past_retention.read_domain_events(10, 100, 0)
    assert result.events == ()


def test_no_checkpoint_means_never_expired_regardless_of_after_cursor(feed_service):
    # No checkpoint row has ever been written (Task 6 compaction hasn't
    # run) -- there is no retention floor, so nothing can be "expired".
    result = feed_service.read_domain_events(0, 100, 0)  # must not raise
    assert result.events == ()


# --------------------------------------------------------------------- #
# Regression ([M1-F1] whole-branch review, Fix 1): a compaction that
# commits IN the lock-free window between the up-front expiry check and
# the `read_after()` query must never silently hand back a partial
# survivor set -- it must be detected and turned into `CursorExpired`.
# --------------------------------------------------------------------- #

def _insert_raw_event(journal: DomainJournal, *, event_id: str, entity_id: str, received_timestamp: dt.datetime) -> int:
    """Insert a `domain_event_journal` row with a FULLY CONTROLLED
    `received_timestamp`, bypassing `mutate()` (which always stamps the
    real wall clock) -- mirrors `test_domain_retention.py`'s helper of the
    same name. Needed here to engineer an event old enough for a REAL
    `compact()` call to delete it (`writer.account()`'s timestamp is always
    "now", so it can never land outside the 30-day floor). Returns the
    assigned `source_cursor`.
    """
    conn = journal.connect()
    conn.execute("BEGIN TRANSACTION")
    row = conn.execute(
        "INSERT INTO domain_event_journal "
        "(event_id, entity_revision, event_type, entity_type, entity_id, "
        " operation, account_id, source, source_timestamp, received_timestamp, "
        " correlation_id, payload) VALUES "
        "(?, 1, 'account.updated', 'account', ?, 'upsert', NULL, 'test', ?, ?, NULL, '{}') "
        "RETURNING source_cursor",
        [event_id, entity_id, received_timestamp, received_timestamp],
    ).fetchone()
    conn.execute("COMMIT")
    return row[0]


def test_compaction_racing_between_expiry_check_and_read_raises_cursor_expired_not_silent_partial_survivors(
    feed_service, journal, monkeypatch
):
    """`_raise_if_expired` (up front) and `read_after` (the actual query)
    are two independent lock-free MVCC reads with a window between them.
    If a `compact()` commits IN that window it can advance
    `oldest_retained_cursor` past `after_cursor` and physically delete
    events the caller has never seen -- without a re-check, `read_after`
    would return only whatever survivors happen to still be there, which
    is indistinguishable from a normal (if partial) result: the caller
    would advance its cursor past the deleted events having received
    `CursorExpired` from neither the up-front check (which ran BEFORE
    compaction) nor anything afterward (there was no second check) --
    permanent, undetectable silent event loss.

    Engineers the race directly rather than depending on winning a real
    thread-timing race: an old event (outside the 30-day retention floor)
    and a fresh one both commit before any checkpoint exists, so the
    up-front `_raise_if_expired(0)` sees no checkpoint row and does not
    raise (matches the "no checkpoint -- never expired" contract). Then
    `journal.read_after` is monkeypatched to run a REAL `compact()` (which
    deletes the old event and writes a checkpoint recording
    `oldest_retained_cursor` at the fresh event's cursor) as a side effect
    immediately before running the real query -- faithfully reproducing
    "a compaction commits in the window between the check and the read".

    Against the pre-fix code (single up-front check only) this test FAILS
    with "DID NOT RAISE `CursorExpired`" -- the call instead returns
    `events=(e-fresh,)`, silently omitting `e-old` which `after_cursor=0`
    never saw. The fix (re-checking `_raise_if_expired` after `read_after`
    returns) makes it PASS.
    """
    old_cursor = _insert_raw_event(
        journal, event_id="e-old", entity_id="DU-old",
        received_timestamp=UTC_NOW - RETENTION_FLOOR - dt.timedelta(days=15),
    )
    fresh_cursor = _insert_raw_event(
        journal, event_id="e-fresh", entity_id="DU-fresh",
        received_timestamp=UTC_NOW - dt.timedelta(days=1),
    )
    assert old_cursor < fresh_cursor

    original_read_after = journal.read_after

    def racing_read_after(after_cursor, limit):
        # The race: a compaction commits HERE -- after the up-front expiry
        # check (already run by the caller, and it passed since no
        # checkpoint existed yet) but BEFORE the actual query below runs.
        journal.compact(now=UTC_NOW, active_cursors={})
        return original_read_after(after_cursor, limit)

    monkeypatch.setattr(journal, "read_after", racing_read_after)

    with pytest.raises(CursorExpired) as excinfo:
        feed_service.read_domain_events(0, 100, 0)

    assert excinfo.value.after_cursor == 0
    assert excinfo.value.oldest_retained_cursor == fresh_cursor


# --------------------------------------------------------------------- #
# limit/wait_ms clamp constants sanity (pure function, no I/O)
# --------------------------------------------------------------------- #

def test_clamp_helper_bounds():
    from trader.domain.feed_service import MAX_LIMIT, MAX_WAIT_MS, MIN_LIMIT, MIN_WAIT_MS, _clamp
    assert _clamp(0, MIN_LIMIT, MAX_LIMIT) == 1
    assert _clamp(-10, MIN_LIMIT, MAX_LIMIT) == 1
    assert _clamp(5000, MIN_LIMIT, MAX_LIMIT) == 1000
    assert _clamp(50, MIN_LIMIT, MAX_LIMIT) == 50
    assert _clamp(-1, MIN_WAIT_MS, MAX_WAIT_MS) == 0
    assert _clamp(999_999, MIN_WAIT_MS, MAX_WAIT_MS) == 10_000


# ----------------------------------------------------------------------- #
# RPC registration: read_domain_events on `feed`, snapshot_with_cursor on
# `query`, neither on `command`.
# ----------------------------------------------------------------------- #

@pytest.fixture
def authenticator():
    return HmacServiceAuthenticator(HMAC_KEY, now=lambda: 1_700_000_000.0)


@pytest.fixture
def snapshot_service(journal):
    service = DomainSnapshotService(journal)
    return service


class TestProductionRegistryWiring:
    def test_read_domain_events_registers_on_feed_role_only(self, authenticator, feed_service):
        registry = build_production_registry(object(), authenticator, feed_service=feed_service)
        assert registry.contains("feed", "read_domain_events")
        assert not registry.contains("query", "read_domain_events")
        assert not registry.contains("command", "read_domain_events")

    def test_snapshot_with_cursor_registers_on_query_role_only(self, authenticator, snapshot_service):
        registry = build_production_registry(object(), authenticator, snapshot_service=snapshot_service)
        assert registry.contains("query", "snapshot_with_cursor")
        assert not registry.contains("feed", "snapshot_with_cursor")
        assert not registry.contains("command", "snapshot_with_cursor")

    def test_neither_method_registers_when_services_are_omitted(self, authenticator):
        # Existing call sites/tests (e.g. test_production_rpc_security.py's
        # `_FakeTrader()`) must keep working unchanged -- these two new
        # methods are opt-in via keyword args, not unconditionally wired.
        registry = build_production_registry(object(), authenticator)
        assert not registry.contains("feed", "read_domain_events")
        assert not registry.contains("query", "snapshot_with_cursor")

    def test_read_domain_events_handler_returns_wire_safe_events(self, authenticator, feed_service, writer):
        writer.account(net_liquidation=10_000, event_id="a1")
        registry = build_production_registry(object(), authenticator, feed_service=feed_service)
        registration = registry.resolve("feed", "read_domain_events")
        body = registration.handler({"after_cursor": 0, "limit": 100, "wait_ms": 0})
        assert body["events"][0]["event_id"] == "a1"
        assert isinstance(body["events"][0]["source_timestamp"], str)  # JSON-safe, not a datetime
        assert body["newest_cursor"] == body["events"][0]["source_cursor"]

    def test_read_domain_events_handler_maps_cursor_expired_to_wire_code(self, authenticator, feed_service_past_retention):
        from trader.domain.feed_service import CURSOR_EXPIRED
        from trader.messaging.typed_rpc import _DispatchProblem

        registry = build_production_registry(object(), authenticator, feed_service=feed_service_past_retention)
        registration = registry.resolve("feed", "read_domain_events")
        with pytest.raises(_DispatchProblem) as excinfo:
            registration.handler({"after_cursor": 7, "limit": 100, "wait_ms": 0})
        assert excinfo.value.code == CURSOR_EXPIRED

    def test_snapshot_with_cursor_handler_returns_wire_safe_body(self, authenticator, snapshot_service, writer):
        writer.account(net_liquidation=10_000, event_id="a1")
        registry = build_production_registry(object(), authenticator, snapshot_service=snapshot_service)
        registration = registry.resolve("query", "snapshot_with_cursor")
        body = registration.handler({})
        assert body["broker_generation"] == 0
        assert "entities" in body

    def test_snapshot_not_ready_is_defined_and_importable_for_the_dormant_gate(self):
        # BLOCKER-2: F1 never raises this, but the wire-code mapping in
        # production_api.py must be able to import/reference it now so F2
        # only has to activate the raise, not add new wiring.
        assert issubclass(SnapshotNotReady, RuntimeError)
