"""Frozen domain-contract tests. [M1-F1] Task 2.

These dataclasses are the FROZEN wire/journal contract every downstream plan
(M1-F2/F3, M1-R) consumes. M1-R constructs ``DomainEvent`` positionally and
reduces its read model by these field names, so any field
addition/removal/rename/reorder breaks the read model with a ``TypeError``.
The field-signature tests below are the tripwire for that drift.
"""
import dataclasses
import datetime as dt
import json

import pytest

from trader.domain.events import (
    DomainEvent,
    DomainMutation,
    EntityKey,
    ReadDomainEventsResult,
    SnapshotWithCursor,
)

UTC_NOW = dt.datetime(2026, 7, 15, 13, 0, tzinfo=dt.timezone.utc)


def _field_names(cls):
    return tuple(f.name for f in dataclasses.fields(cls))


# --- frozen field signatures (the tripwire) --------------------------------

def test_domain_event_has_exactly_the_twelve_frozen_fields_in_order():
    assert _field_names(DomainEvent) == (
        "event_id", "source_cursor", "entity_revision", "event_type",
        "entity_type", "entity_id", "operation", "account_id", "source",
        "source_timestamp", "correlation_id", "payload",
    )


def test_domain_event_has_no_received_timestamp_field():
    # received_timestamp is a journal DB column ONLY; it must never leak into
    # the domain contract (plan-index freeze).
    assert "received_timestamp" not in _field_names(DomainEvent)


def test_snapshot_with_cursor_has_exactly_three_fields():
    assert _field_names(SnapshotWithCursor) == (
        "source_cursor", "broker_generation", "entities",
    )


def test_read_domain_events_result_has_exactly_two_fields():
    assert _field_names(ReadDomainEventsResult) == ("events", "newest_cursor")


def test_entity_key_has_exactly_three_fields():
    assert _field_names(EntityKey) == ("entity_type", "entity_id", "account_id")


# --- immutability ----------------------------------------------------------

def test_domain_event_is_frozen():
    event = _make_event()
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.entity_id = "changed"


def test_domain_mutation_is_frozen():
    mutation = _make_upsert()
    with pytest.raises(dataclasses.FrozenInstanceError):
        mutation.payload = {}


def test_entity_key_is_frozen():
    key = EntityKey(entity_type="position", entity_id="DU123:265598",
                    account_id="DU123")
    with pytest.raises(dataclasses.FrozenInstanceError):
        key.entity_id = "changed"


# --- positional construction (M1-R relies on this) -------------------------

def test_domain_event_constructs_positionally():
    event = DomainEvent(
        "event-1", 5, 1, "position.updated", "position", "DU123:265598",
        "upsert", "DU123", "trader_service", UTC_NOW, None,
        {"quantity": 10},
    )
    assert event.event_id == "event-1"
    assert event.source_cursor == 5
    assert event.entity_revision == 1
    assert event.operation == "upsert"
    assert event.payload == {"quantity": 10}


def test_delete_event_payload_is_none():
    event = DomainEvent(
        "event-2", 6, 2, "position.deleted", "position", "DU123:265598",
        "delete", "DU123", "trader_service", UTC_NOW, None, None,
    )
    assert event.payload is None


def test_read_domain_events_result_events_is_a_tuple():
    e1 = _make_event(source_cursor=5)
    e2 = _make_event(source_cursor=9)
    result = ReadDomainEventsResult(events=(e1, e2), newest_cursor=9)
    assert isinstance(result.events, tuple)
    # M1-R does result.events[-1].source_cursor — prove that idiom works.
    assert result.events[-1].source_cursor == 9


def test_snapshot_with_cursor_carries_entities_by_type():
    snap = SnapshotWithCursor(
        source_cursor=17, broker_generation=0,
        entities={"position": [{"entity_id": "DU123:265598",
                                "entity_revision": 1}]},
    )
    assert snap.source_cursor == 17
    assert snap.broker_generation == 0
    assert snap.entities["position"][0]["entity_id"] == "DU123:265598"


# --- DomainMutation.__post_init__ invariants -------------------------------

def test_upsert_requires_a_complete_payload():
    with pytest.raises(ValueError, match="payload"):
        DomainMutation(
            event_type="position.updated", entity_type="position",
            entity_id="DU123:265598", operation="upsert", account_id="DU123",
            source="trader_service", source_timestamp=UTC_NOW,
            correlation_id=None, payload=None,
        )


def test_delete_tombstone_must_not_carry_a_payload():
    with pytest.raises(ValueError, match="payload"):
        DomainMutation(
            event_type="position.deleted", entity_type="position",
            entity_id="DU123:265598", operation="delete", account_id="DU123",
            source="trader_service", source_timestamp=UTC_NOW,
            correlation_id=None, payload={"quantity": 0},
        )


def test_naive_source_timestamp_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        DomainMutation(
            event_type="position.updated", entity_type="position",
            entity_id="DU123:265598", operation="upsert", account_id="DU123",
            source="trader_service",
            source_timestamp=dt.datetime(2026, 7, 15, 13, 0),  # naive
            correlation_id=None, payload={"quantity": 10},
        )


def test_valid_upsert_and_delete_construct():
    up = _make_upsert()
    assert up.operation == "upsert"
    assert up.payload == {"quantity": 10}
    down = DomainMutation(
        event_type="position.deleted", entity_type="position",
        entity_id="DU123:265598", operation="delete", account_id="DU123",
        source="trader_service", source_timestamp=UTC_NOW,
        correlation_id=None, payload=None,
    )
    assert down.payload is None


def test_non_utc_but_aware_timestamp_is_accepted():
    # The invariant is "aware", not "literally UTC"; a broker may stamp a
    # non-UTC zone. Comparisons normalize to UTC (RA-10), see below.
    eastern = dt.timezone(dt.timedelta(hours=-4))
    mutation = _make_upsert(
        source_timestamp=dt.datetime(2026, 7, 15, 9, 0, tzinfo=eastern))
    assert mutation.source_timestamp.tzinfo is not None


# --- RA-10 determinism -----------------------------------------------------

def test_source_timestamp_normalizes_to_utc_for_comparison():
    # Same instant, two zones: compare as UTC, never by wall-clock fields.
    eastern = dt.timezone(dt.timedelta(hours=-4))
    same_instant = dt.datetime(2026, 7, 15, 9, 0, tzinfo=eastern)
    a = _make_upsert(source_timestamp=UTC_NOW)
    b = _make_upsert(source_timestamp=same_instant)
    assert (a.source_timestamp.astimezone(dt.timezone.utc)
            == b.source_timestamp.astimezone(dt.timezone.utc))


def test_payloads_compare_by_canonical_json_not_dict_equality():
    a = _make_upsert(payload={"beta": 2.0, "alpha": 1.0})
    b = _make_upsert(payload={"alpha": 1.0, "beta": 2.0})

    def canonical(payload):
        return json.dumps(payload, sort_keys=True)

    assert canonical(a.payload) == canonical(b.payload)


# --- helpers ---------------------------------------------------------------

def _make_event(source_cursor=1):
    return DomainEvent(
        event_id="event-1", source_cursor=source_cursor, entity_revision=1,
        event_type="position.updated", entity_type="position",
        entity_id="DU123:265598", operation="upsert", account_id="DU123",
        source="trader_service", source_timestamp=UTC_NOW,
        correlation_id=None, payload={"quantity": 10},
    )


def _make_upsert(source_timestamp=UTC_NOW, payload=None):
    return DomainMutation(
        event_type="position.updated", entity_type="position",
        entity_id="DU123:265598", operation="upsert", account_id="DU123",
        source="trader_service", source_timestamp=source_timestamp,
        correlation_id=None, payload=payload if payload is not None else {"quantity": 10},
    )
