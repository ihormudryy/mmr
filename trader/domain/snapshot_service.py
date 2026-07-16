"""Fenced materialized snapshot service. [M1-F1] Task 4.

``DomainSnapshotService.snapshot_with_cursor()`` returns a single consistent
point-in-time view across every registered ``MaterializedAdapter``, plus the
exact journal cursor that view corresponds to (``SnapshotWithCursor``, Task
2). Consumers install this snapshot as their baseline and then tail
``DomainJournal.read_after(source_cursor, ...)`` (Task 5's long-poll feed)
for everything after it -- no gap, no overlap.

Fencing mechanism (binding, see the plan's Architecture pre-flight
resolution)
--------------------------------------------------------------------------
The snapshot is fenced by ONE DuckDB read transaction opened on a cursor off
``DomainJournal``'s single shared, long-lived ``duckdb.connect()`` instance
(``journal.connect()`` -- never a fresh file connection, never
``execute_atomic``/the per-db lock, either of which would serialize
snapshot reads against every other DB access for the whole transaction).

This rests entirely on DuckDB's snapshot isolation for concurrent cursors of
ONE shared connection instance (see ``domain_journal.py``'s module
docstring, BLOCKER-1). Verified empirically against DuckDB 1.4.4 (two
cursors off one ``duckdb.connect()`` instance): DuckDB does NOT pin a
transaction's snapshot at ``BEGIN TRANSACTION`` -- it pins it at that
transaction's FIRST statement that touches a table. A write committed on
cursor B *before* cursor A's first read is visible to A; the identical write
committed *after* cursor A's first read is invisible to every one of A's
later reads, even though B's commit fully completed and was never blocked or
serialized behind A's still-open transaction.

Consequently, the ``broker_generation`` read below is deliberately the
FIRST statement this method issues after ``BEGIN TRANSACTION`` -- it is what
pins the snapshot every subsequent read (every adapter's ``select_active``,
the closing ``MAX(source_cursor)``) observes. All of them then see the
identical snapshot, so the returned ``source_cursor`` bounds exactly the
revisions visible in the returned entities, with no gap and no overlap,
regardless of what commits land on other cursors of the same connection
instance while this transaction is still open. This is a genuine MVCC
guarantee, not an artifact of serialization: nothing in this method takes a
lock (in particular, NOT ``DomainJournal._write_lock``, which only guards
``mutate()``'s own critical section) that would force a concurrent writer to
wait -- see ``test_domain_snapshot.py``'s fence test, which proves the
injected writer's commit lands durably (a real, completed, independent
transaction) yet still isn't visible inside the reader's still-open one.

This method DOES take ``journal.fenced_read_lock`` (Task 6) for the full
span of its transaction -- that lock is a SEPARATE primitive from
``_write_lock`` and exists solely to serialize this held-open,
multi-statement read transaction against ``DomainJournal.compact()``'s
own DELETE + ``CHECKPOINT`` (verified empirically that interleaving those
is unreliable against DuckDB 1.4.4 -- see ``domain_journal.py``'s module
docstring). Taking ``fenced_read_lock`` here is safe precisely because it
is never the SAME lock ``mutate()`` takes: a caller-supplied
``on_read_started`` hook that synchronously calls back into ``mutate()``
on this same thread (this task's own fence test) only ever contends for
``_write_lock``, never ``fenced_read_lock``, so no self-deadlock is
possible.

Broker-generation gate (binding, BLOCKER-2 -- DORMANT in [M1-F1])
--------------------------------------------------------------------------
``[M1-F1]`` does not promote broker generations (``[M1-F2]`` does). So
``broker_generation`` here is read straight from
``domain_snapshot_checkpoints`` (0 when that table is empty, which it always
is in F1 -- only Task 6/``[M1-F2]`` ever write a row to it) and this method
NEVER gates on it. ``SnapshotNotReady`` and the ``SNAPSHOT_NOT_READY`` wire
code below are defined now for ``[M1-F2]`` to activate once it wires
``register_broker_generation_reader(BrokerStateStore.latest_promoted_generation_in_tx)``
and finds no promoted generation. Until then, this class has no attribute,
method, or code path that can raise ``SnapshotNotReady``.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from trader.data.domain_journal import DomainJournal, read_latest_broker_generation
from trader.data.materialized_state import MaterializedAdapter
from trader.domain.events import SnapshotWithCursor

# Wire-code [M1-F2] maps SnapshotNotReady onto once it activates the gate.
# Mirrors the `RpcProblem.code` / `TypedRpcRemoteError.code` convention in
# `trader/messaging/typed_rpc.py`: whichever RPC wiring registers
# `snapshot_with_cursor` on the typed query socket should catch
# `SnapshotNotReady` and re-raise it as a `_DispatchProblem(SNAPSHOT_NOT_READY, ...)`
# so it reaches the wire as `RpcProblem(code=SNAPSHOT_NOT_READY, ...)` instead
# of falling through to the generic scrubbed `INTERNAL_ERROR` catch-all.
SNAPSHOT_NOT_READY = "SNAPSHOT_NOT_READY"


class SnapshotNotReady(RuntimeError):
    """No complete broker-sync generation exists yet.

    DEFINED now for ``[M1-F2]`` to activate; DORMANT in ``[M1-F1]`` --
    nothing in this module raises it today (see the module docstring's
    "Broker-generation gate" section). ``[M1-F2]`` will raise it from
    inside the same read transaction ``snapshot_with_cursor`` opens, once
    its registered broker-generation reader reports no promoted generation.
    """


class DomainSnapshotService:
    """Produces fenced ``SnapshotWithCursor`` reads over registered adapters.

    Constructed with the same ``DomainJournal`` instance the rest of the
    process uses (``trader_service``'s single ``journal_duckdb_path``
    owner) -- never a second, independent connection to that file.
    """

    def __init__(self, journal: DomainJournal):
        self._journal = journal
        self._adapters: list[MaterializedAdapter] = []

    def register_adapter(self, adapter: MaterializedAdapter) -> None:
        """Register one entity type's ``MaterializedAdapter``.

        ``entities`` in the returned snapshot is keyed by ``entity_type``,
        so registration order doesn't affect the result. Registering the
        same ``entity_type`` twice is a caller bug (the second adapter's
        entry simply shadows the first's in ``entities``); this is not
        rejected here since production wiring (``[M1-F2]``/``[M1-F3]``)
        registers each entity type exactly once at startup.
        """
        self._adapters.append(adapter)

    def snapshot_with_cursor(
        self,
        on_read_started: Optional[Callable[[], None]] = None,
    ) -> SnapshotWithCursor:
        """Return one fenced snapshot: the exact journal cursor plus every
        registered adapter's active rows, all read inside a single
        transaction opened on ``journal.connect()``.

        ``on_read_started`` is test-support only (this task's interleaving
        fence test) -- production callers never pass it. When supplied, it
        fires immediately after this transaction's first read (the read
        that pins the DuckDB snapshot -- see the module docstring),
        simulating a writer that commits a new revision on an independent
        cursor while this transaction is still open. Because the fencing
        rests on DuckDB's snapshot isolation rather than any lock this
        method holds, that writer commits successfully and immediately --
        it is never blocked or serialized behind this read -- yet its new
        revision does not appear in the entities/cursor returned below.
        """
        with self._journal.fenced_read_lock:
            conn = self._journal.connect()
            conn.execute("BEGIN TRANSACTION")
            try:
                # First read of the transaction -- pins the snapshot every
                # subsequent read below observes (verified empirically
                # against DuckDB 1.4.4; see the module docstring's "Fencing
                # mechanism" section). Do not reorder this behind
                # on_read_started.
                broker_generation = self._read_broker_generation(conn)

                if on_read_started is not None:
                    on_read_started()

                entities: dict[str, list[dict[str, Any]]] = {}
                for adapter in self._adapters:
                    entities[adapter.entity_type] = adapter.select_active(conn)

                cursor_row = conn.execute(
                    "SELECT MAX(source_cursor) FROM domain_event_journal"
                ).fetchone()
                source_cursor = cursor_row[0] if cursor_row and cursor_row[0] is not None else 0

                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except BaseException:
                    # A rollback failure (e.g. a failed BEGIN left no open
                    # transaction) must never mask the real exception raised
                    # below.
                    pass
                raise

        return SnapshotWithCursor(
            source_cursor=source_cursor,
            broker_generation=broker_generation,
            entities=entities,
        )

    def _read_broker_generation(self, conn: Any) -> int:
        # Dormant gate (BLOCKER-2): read straight off the checkpoints
        # table, 0 when it's empty -- true for the whole of [M1-F1], since
        # only Task 6/[M1-F2] ever write a row here. Never raises
        # SnapshotNotReady -- see the module docstring. Shared with
        # DomainJournal.compact() (Task 6) via read_latest_broker_generation
        # so a retention sweep's own checkpoint write always carries this
        # same value forward instead of duplicating the query.
        return read_latest_broker_generation(conn)
