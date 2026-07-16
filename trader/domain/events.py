"""Immutable domain-event contracts. [M1-F1] Task 2 — FROZEN.

These dataclasses are the shared wire/journal contract consumed by every
downstream plan (M1-F2 broker producers, M1-F3 command authority, M1-R read
model). "Frozen" holds in two senses:

1. ``@dataclass(frozen=True)`` — runtime immutability.
2. *Contract-frozen* — field names, order, and count must not drift. M1-R
   constructs ``DomainEvent`` positionally from the journal-row projection and
   reduces its read model by these field names, so adding, removing, renaming,
   or reordering a field breaks the read model with a ``TypeError``.

TRAP: ``DomainEvent`` has NO ``received_timestamp`` field. That column exists
ONLY on the ``domain_event_journal`` DuckDB table (the journal stamps arrival
time on write); it is deliberately absent from the domain contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Union

# The only two operations a mutation/event may carry. An upsert always
# carries the complete normalized entity row; a delete is a tombstone with a
# higher entity revision and no payload.
Operation = Literal["upsert", "delete"]

# A JSON-native value. Payloads are persisted as a JSON column and re-parsed
# by the read model, so every payload leaf must round-trip through JSON. This
# alias documents that constraint on ``DomainMutation.payload``.
JSONValue = Union[
    None, bool, int, float, str,
    "list[JSONValue]", "dict[str, JSONValue]",
]


@dataclass(frozen=True)
class EntityKey:
    """The canonical key for one materialized entity.

    Imported by [M1-F2] broker producers; an import failure here breaks every
    broker producer, so the three-field shape is frozen.
    """
    entity_type: str
    entity_id: str
    account_id: str | None


@dataclass(frozen=True)
class DomainMutation:
    """A requested state change, before it is journaled.

    ``DomainJournal.mutate`` (Task 3) turns a validated mutation into a
    persisted ``DomainEvent`` (assigning ``event_id``, ``source_cursor``, and
    ``entity_revision``). The ``__post_init__`` invariants are the fail-loud
    gate: a naive timestamp, a payload-less upsert, or a payload-bearing
    delete tombstone is a producer bug and must never reach the journal.
    """
    event_type: str
    entity_type: str
    entity_id: str
    operation: Operation
    account_id: str | None
    source: str
    source_timestamp: datetime
    correlation_id: str | None
    payload: dict[str, JSONValue] | None

    def __post_init__(self) -> None:
        if self.source_timestamp.tzinfo is None:
            raise ValueError("source_timestamp must be timezone-aware UTC")
        if self.operation == "upsert" and self.payload is None:
            raise ValueError("upsert requires a complete payload")
        if self.operation == "delete" and self.payload is not None:
            raise ValueError("delete tombstone must not carry a payload")


@dataclass(frozen=True)
class DomainEvent:
    """A journaled, cursor-ordered domain event. FROZEN 12-field contract.

    Field order is load-bearing: M1-R constructs this positionally from the
    journal-row projection. Do NOT add, remove, rename, or reorder fields
    without first updating every consuming plan (see the plan-index
    cross-plan interface freeze).
    """
    event_id: str
    source_cursor: int
    entity_revision: int
    event_type: str
    entity_type: str
    entity_id: str
    operation: Operation
    account_id: str | None
    source: str
    source_timestamp: datetime
    correlation_id: str | None
    payload: dict[str, Any] | None  # None for delete tombstones


@dataclass(frozen=True)
class SnapshotWithCursor:
    """A fenced materialized snapshot and its maximum included cursor.

    ``broker_generation`` is 0 while the readiness gate is dormant ([M1-F1]);
    [M1-F2] promotes real generations and activates the gate. Every row dict
    in ``entities`` retains ``entity_id`` and ``entity_revision`` (M1-R
    installs the read model by them).
    """
    source_cursor: int
    broker_generation: int
    entities: dict[str, list[dict[str, Any]]]


@dataclass(frozen=True)
class ReadDomainEventsResult:
    """The result of one long-poll feed read.

    ``events`` is a TUPLE (not a list): M1-R does
    ``result.events[-1].source_cursor`` and treats the result as immutable.
    """
    events: tuple[DomainEvent, ...]
    newest_cursor: int
