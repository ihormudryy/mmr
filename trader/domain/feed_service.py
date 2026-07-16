"""Cursor long-poll domain-event feed. [M1-F1] Task 5.

``DomainFeedService.read_domain_events`` is the tailing half of the fenced
snapshot/tail pair: a consumer establishes a baseline via
``DomainSnapshotService.snapshot_with_cursor()`` (Task 4) and then calls this
repeatedly with ``after_cursor`` advancing to ``result.newest_cursor`` each
time, to consume every event committed after that baseline with no gap and
no overlap.

Long-poll wakeup design (binding -- see the M1-F1 briefing's hazard #3)
--------------------------------------------------------------------------
``DomainJournal`` (Task 3) owns a single ``threading.Condition`` plus an
in-memory ``_latest_committed_cursor`` counter (see
``trader/data/domain_journal.py``'s ``_signal_commit`` /
``wait_for_cursor_after``). ``DomainJournal.mutate`` bumps that counter and
calls ``Condition.notify_all()`` ONLY after its transaction has durably
COMMITTED, and only after releasing its own ``_write_lock`` -- never before
commit (a pre-commit signal would be a false/lost wakeup: a reader could act
on a transaction that then rolls back, or race a writer that hasn't actually
finished yet).

This method's wait loop (delegated to ``DomainJournal.wait_for_cursor_after``)
re-checks the predicate ``_latest_committed_cursor > after_cursor`` in a
``while`` loop UNDER the condition lock every time it wakes -- guarding
against spurious wakeups, since a condition variable's ``wait()`` may return
with no corresponding ``notify()`` at all -- and recomputes its remaining
budget from ``time.monotonic()`` on each iteration (immune to wall-clock
adjustments). No DB I/O happens while that lock is held: the actual
``read_after()`` journal query below runs strictly after
``wait_for_cursor_after`` returns, whether it returned because the predicate
became true or because the deadline passed.

Empty heartbeat (binding): when the post-wait ``read_after()`` query finds
nothing, ``newest_cursor`` on the returned ``ReadDomainEventsResult`` is the
journal's actual latest known committed cursor, floored at ``after_cursor``
so it can never regress the caller's own bookkeeping -- NOT a bare echo of
``after_cursor``. In today's system (no compaction/deletion path exists
yet -- that's Task 6) the two values coincide in every reachable case, since
nothing can concurrently remove a row between the wakeup and the read; the
``max(after_cursor, cursor_at_wake)`` form is written defensively now so it
stays correct once Task 6 introduces concurrent deletion, rather than
needing a second pass over this method later. A reader that always got a
bare ``after_cursor`` echo back on every timeout would have no way to tell
"genuinely nothing new" apart from "something raced past me" once that
becomes possible.

CursorExpired (RA-5, binding)
--------------------------------
Raised when ``after_cursor`` has fallen behind the retained window --
defined strictly as ``after_cursor < oldest_retained_cursor`` from the MOST
RECENT ``domain_snapshot_checkpoints`` row. Task 6 compaction is the only
writer of that table, and per its own contract a successful deletion always
has a corresponding successful checkpoint write (if checkpoint creation
fails, compaction retains the data instead of deleting it -- see the Task 6
brief). So "no checkpoint row yet" means "compaction has never trimmed
anything, ever" -- not "trimmed silently".

Deliberately NOT falling back to ``MIN(source_cursor)`` over the live
journal when no checkpoint exists (unlike a literal reading of RA-5's "or
MIN(source_cursor)" phrasing might suggest): cursor values are
monotonic-but-SPARSE (RA-4 -- ``nextval()`` burns values on rollback), so an
un-compacted journal's oldest surviving row can easily sit above 1 purely
from ordinary rollback churn, with zero retention having occurred. Treating
that as an expiry floor would raise ``CursorExpired`` for a client that never
even could have held a cursor pointing at the missing (never-committed)
values in the first place -- which is exactly the failure mode this same
rule's other half warns against: "never assume ``after_cursor + 1`` exists".
The checkpoint-only check has no such false-positive risk and is fully
sufficient given Task 6's checkpoint-before-delete invariant.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from trader.data.domain_journal import DomainJournal
from trader.domain.events import DomainEvent, ReadDomainEventsResult

# Wire code [M1-R]/the RPC wiring in `production_api.py` maps `CursorExpired`
# onto -- mirrors `snapshot_service.SNAPSHOT_NOT_READY`'s convention so a
# caller can branch on `TypedRpcRemoteError.code` without string-matching.
CURSOR_EXPIRED = "CURSOR_EXPIRED"

# limit/wait_ms clamps (binding): a caller-supplied value outside these
# bounds is silently clamped, never rejected -- an oversized wait_ms could
# otherwise hold a connection/thread open indefinitely, and a zero/negative
# limit would make read_after() always return nothing even when a
# qualifying wakeup just occurred.
MIN_LIMIT = 1
MAX_LIMIT = 1000
MIN_WAIT_MS = 0
MAX_WAIT_MS = 10_000


class CursorExpired(RuntimeError):
    """``after_cursor`` has fallen behind the journal's retained window.

    The caller cannot resume tailing from this cursor -- the events between
    it and ``oldest_retained_cursor`` have been compacted away. It must
    re-establish a baseline via
    ``DomainSnapshotService.snapshot_with_cursor()`` and resume tailing from
    that snapshot's ``source_cursor`` instead.
    """

    def __init__(self, after_cursor: int, oldest_retained_cursor: int):
        self.after_cursor = after_cursor
        self.oldest_retained_cursor = oldest_retained_cursor
        super().__init__(
            f"after_cursor={after_cursor} is behind the oldest retained cursor "
            f"({oldest_retained_cursor}); re-establish a baseline via "
            "snapshot_with_cursor() and resume tailing from its source_cursor"
        )


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def domain_event_to_wire(event: DomainEvent) -> Dict[str, Any]:
    """JSON-safe dict form of one ``DomainEvent``, for the typed RPC wire.

    ``source_timestamp`` is the only non-JSON-native field on the frozen
    12-field contract (see ``trader/domain/events.py``) -- rendered as an
    ISO-8601 string. Every other field is already JSON-native.
    """
    return {
        "event_id": event.event_id,
        "source_cursor": event.source_cursor,
        "entity_revision": event.entity_revision,
        "event_type": event.event_type,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "operation": event.operation,
        "account_id": event.account_id,
        "source": event.source,
        "source_timestamp": event.source_timestamp.isoformat(),
        "correlation_id": event.correlation_id,
        "payload": event.payload,
    }


class DomainFeedService:
    """Blocking cursor tail over ``DomainJournal``.

    Constructed with the same ``DomainJournal`` instance the rest of the
    process uses (mirrors ``DomainSnapshotService``) -- never a second,
    independent connection to the dedicated journal file.
    """

    def __init__(self, journal: DomainJournal):
        self._journal = journal

    def read_domain_events(self, after_cursor: int, limit: int, wait_ms: int) -> ReadDomainEventsResult:
        """Return events with ``source_cursor > after_cursor``, waiting up
        to ``wait_ms`` milliseconds for at least one to exist if none do yet.

        ``limit`` is clamped to 1..1000 and ``wait_ms`` to 0..10000 BEFORE
        either is used for anything. Raises ``CursorExpired`` if
        ``after_cursor`` is behind the journal's retained window (checked
        up front, before any waiting -- an already-invalid cursor should
        fail immediately, not after burning the full wait budget).
        """
        limit = _clamp(limit, MIN_LIMIT, MAX_LIMIT)
        wait_ms = _clamp(wait_ms, MIN_WAIT_MS, MAX_WAIT_MS)

        self._raise_if_expired(after_cursor)

        deadline = time.monotonic() + (wait_ms / 1000.0)
        cursor_at_wake = self._journal.wait_for_cursor_after(after_cursor, deadline)

        events = tuple(self._journal.read_after(after_cursor, limit))
        if events:
            newest_cursor = events[-1].source_cursor
        else:
            # Empty heartbeat -- see module docstring's "Empty heartbeat"
            # section for why this is not a bare echo of after_cursor.
            newest_cursor = max(after_cursor, cursor_at_wake)

        return ReadDomainEventsResult(events=events, newest_cursor=newest_cursor)

    def _raise_if_expired(self, after_cursor: int) -> None:
        conn = self._journal.connect()
        row = conn.execute(
            "SELECT oldest_retained_cursor FROM domain_snapshot_checkpoints "
            "ORDER BY checkpoint_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            # No checkpoint ever written -- Task 6 compaction has never
            # run, so there is no retention floor to fall behind. See the
            # module docstring for why this deliberately does NOT fall back
            # to MIN(source_cursor).
            return
        oldest_retained_cursor: Optional[int] = row[0]
        if oldest_retained_cursor is not None and after_cursor < oldest_retained_cursor:
            raise CursorExpired(after_cursor, oldest_retained_cursor)
