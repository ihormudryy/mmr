"""Materialized-state adapters for the fenced domain snapshot. [M1-F1] Task 4.

``DomainSnapshotService.snapshot_with_cursor()``
(``trader/domain/snapshot_service.py``) reads one consistent point-in-time
view of every registered entity type. Each entity type contributes exactly
one ``MaterializedAdapter`` telling the snapshot service how to select its
currently-active rows.

FROZEN CONTRACT: the ``MaterializedAdapter`` protocol is exactly three
members -- ``entity_type``, ``select_active(conn)``, ``checkpoint(conn)``.
``[M1-F2]``'s ``broker_materialized_adapters(store)`` constructs adapters
against these precise names; do not rename them, add required members, or
change ``select_active``/``checkpoint``'s signatures.

``GenericEntityAdapter`` is the Task-3-table-backed implementation: it reads
directly from ``domain_materialized_entities`` (the generic per-entity
ledger Task 3 already maintains for RA-6 revision bookkeeping) for entity
types that have no dedicated, richer table of their own. This is exactly the
shape this task's own tests need (a bare ``proposal`` entity with no
broker-specific table), and it stays useful for future entities that don't
earn a bespoke table. Broker entities (``[M1-F2]``: positions/orders/fills)
use their own dedicated tables/adapters instead (see
``broker_materialized_adapters`` in the broker-producers plan), because a
generic ledger row is deliberately schema-agnostic (one JSON payload column)
while a broker table has real, queryable typed columns.
"""
from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

import duckdb


@runtime_checkable
class MaterializedAdapter(Protocol):
    """One entity type's contribution to a fenced snapshot.

    FROZEN member names: ``entity_type``, ``select_active(conn)``,
    ``checkpoint(conn)``. ``[M1-F2]`` constructs adapters against these
    exact names -- do not rename, add required members, or change either
    method's signature.
    """

    entity_type: str

    def select_active(self, conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
        """Return every currently-active (non-deleted) row for this entity
        type as JSON-native dicts, each retaining ``entity_id`` and
        ``entity_revision`` (``[M1-R]`` installs its read model by these).
        Called inside the snapshot service's single read transaction --
        implementations must not open another connection or start their own
        transaction.
        """
        ...

    def checkpoint(self, conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
        """Return the full state to persist into a compaction checkpoint
        (Task 6). For most adapters this is identical to ``select_active``
        -- the checkpoint IS the active-state view as of that cursor.
        """
        ...


class GenericEntityAdapter:
    """``MaterializedAdapter`` backed directly by Task 3's generic
    ``domain_materialized_entities`` ledger, filtered to one ``entity_type``.

    Every domain-journaled entity already has a row in this ledger (Task 3's
    RA-6 revision bookkeeping writes/updates it on every
    ``DomainJournal.mutate`` call), so this adapter needs no entity-specific
    schema knowledge -- it works for ANY entity type that doesn't maintain
    its own richer table.
    """

    def __init__(self, entity_type: str):
        self.entity_type = entity_type

    def select_active(self, conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT entity_id, account_id, entity_revision, payload "
            "FROM domain_materialized_entities "
            "WHERE entity_type = ? AND NOT deleted",
            [self.entity_type],
        ).fetchall()
        result: list[dict[str, Any]] = []
        for entity_id, account_id, entity_revision, payload in rows:
            row: dict[str, Any] = dict(json.loads(payload)) if payload is not None else {}
            # Reserved keys are authoritative from the ledger columns, not
            # whatever the payload happens to carry -- overwrite last so a
            # stale/duplicate payload key can never shadow them.
            row["entity_id"] = entity_id
            row["entity_revision"] = entity_revision
            row["account_id"] = account_id
            result.append(row)
        return result

    def checkpoint(self, conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
        # Identical to select_active for this generic ledger-backed adapter
        # -- the checkpoint IS the active-state view at the fenced cursor
        # (mirrors [M1-F2]'s own _TableAdapter.checkpoint, which likewise
        # delegates to its select callable).
        return self.select_active(conn)
