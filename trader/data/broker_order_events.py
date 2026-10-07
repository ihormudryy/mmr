"""Broker order status history (SP1 Plan 6 ruling 14, migration 81).

Broker ingest appends one row per change of a projected broker order, in the
same journal transaction that writes the projection. ``cursor`` counts 1, 2, 3
... per broker generation with no gaps: a live callback belongs to the newest
promoted generation, a staged one to the generation that promotes it.

``broker_order_evidence_in_tx`` is the typed evidence read: the newest promoted
generation's orders, OCA fields included, each with its own recorded events.
A newer generation still staging returns ``{"capture_error": "GENERATION_STAGING"}``,
never partial data.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

BROKER_ORDER_EVENTS_MIGRATION_VERSION = 81

_DDL = """CREATE TABLE IF NOT EXISTS broker_order_events (
    generation_id BIGINT NOT NULL,
    "cursor" BIGINT NOT NULL,
    order_entity_id VARCHAR NOT NULL,
    order_id BIGINT,
    perm_id BIGINT,
    "at" TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL,
    filled_quantity DOUBLE NOT NULL,
    remaining_quantity DOUBLE NOT NULL,
    total_quantity DOUBLE NOT NULL,
    oca_group VARCHAR,
    oca_type INTEGER,
    PRIMARY KEY (generation_id, "cursor")
)"""

_WORKING = ("PendingSubmit", "ApiPending", "PreSubmitted", "Submitted", "PendingCancel")
_LEGS = {"entry": "entry", "stop": "stop", "take_profit": "take_profit", "exit": "close"}


def apply_migration_81_broker_order_events(migrator: Any) -> bool:
    return migrator.apply(BROKER_ORDER_EVENTS_MIGRATION_VERSION, "p6_broker_order_events", [_DDL])


def record_order_event_in_tx(conn: Any, generation_id: Optional[int], order_entity_id: str,
                             perm_id: Optional[int], order_id: Optional[int], row: Any, at: dt.datetime) -> int:
    """Append one event for ``row`` (a ``BrokerOrderRow``); returns its cursor."""
    generation = int(generation_id or 0)
    cursor = conn.execute(
        'SELECT COALESCE(MAX("cursor"), 0) + 1 FROM broker_order_events WHERE generation_id = ?',
        [generation]).fetchone()[0]
    total, filled = float(row.total_quantity), float(row.filled_quantity)
    conn.execute(
        'INSERT INTO broker_order_events (generation_id, "cursor", order_entity_id, order_id, perm_id, "at", '
        "status, filled_quantity, remaining_quantity, total_quantity, oca_group, oca_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [generation, int(cursor), order_entity_id, int(order_id) if order_id else None,
         int(perm_id) if perm_id else None, at, row.status, filled, max(total - filled, 0.0), total,
         row.oca_group, row.oca_type])
    return int(cursor)


def _iso(value: Any) -> Optional[str]:
    return None if value is None else value.isoformat()


def _events_in_tx(conn: Any, generation_id: int) -> list[dict]:
    rows = conn.execute(
        'SELECT "cursor", order_entity_id, "at", status, filled_quantity, remaining_quantity, total_quantity, '
        'oca_group, oca_type FROM broker_order_events WHERE generation_id = ? ORDER BY "cursor"',
        [generation_id]).fetchall()
    return [{"cursor": int(r[0]), "order_entity_id": r[1], "at": _iso(r[2]), "status": r[3],
             "filled_quantity": float(r[4]), "remaining_quantity": float(r[5]), "total_quantity": float(r[6]),
             "oca_group": r[7], "oca_type": r[8]} for r in rows]


def _aliases_in_tx(conn: Any, entity_id: str) -> dict:
    rows = conn.execute(
        "SELECT alias_type, alias_value FROM broker_order_aliases WHERE order_entity_id = ?", [entity_id]).fetchall()
    found = {kind: value for kind, value in rows}

    def number(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return {"perm_id": number(found.get("perm_id")), "order_id": number(found.get("client_order_id"))}


def broker_order_evidence_in_tx(conn: Any, store: Any, account_id: str, conid: Optional[int], *,
                                source: str) -> dict:
    promoted = conn.execute(
        "SELECT generation_id, completed_at FROM broker_sync_generations WHERE status = 'promoted' "
        "ORDER BY generation_id DESC LIMIT 1").fetchone()
    if promoted is None:
        return {"capture_error": "NO_PROMOTED_GENERATION"}
    generation_id, completed_at = int(promoted[0]), promoted[1]
    staging = conn.execute(
        "SELECT 1 FROM broker_sync_generations WHERE status = 'staging' AND generation_id > ? LIMIT 1",
        [generation_id]).fetchone()
    if staging is not None:
        return {"capture_error": "GENERATION_STAGING"}
    events = _events_in_tx(conn, generation_id)
    by_entity: dict[str, list[dict]] = {}
    for event in events:
        by_entity.setdefault(event.pop("order_entity_id"), []).append(event)
    rows = {row.order_entity_id: row for row in store.select_active_orders_in_tx(conn)
            if row.account_id == account_id}
    for entity_id in by_entity:
        if entity_id not in rows:
            row = store.get_order_in_tx(conn, entity_id)
            if row is not None and row.account_id == account_id:
                rows[entity_id] = row
    orders = []
    for entity_id, row in sorted(rows.items()):
        if conid is not None and row.conid != conid:
            continue
        orders.append({**_aliases_in_tx(conn, entity_id), "order_entity_id": entity_id, "conid": int(row.conid),
                       "order_group_id": row.order_group_id, "leg": _LEGS.get(row.leg or "", "other"),
                       "action": row.action, "order_type": row.order_type, "status": row.status,
                       "total_quantity": float(row.total_quantity), "filled_quantity": float(row.filled_quantity),
                       "remaining_quantity": max(float(row.total_quantity) - float(row.filled_quantity), 0.0),
                       "oca_group": row.oca_group, "oca_type": row.oca_type, "parent_id": None,
                       "deleted": bool(row.deleted), "status_events": by_entity.get(entity_id, [])})
    cursors = [event["cursor"] for entity_events in by_entity.values() for event in entity_events]
    return {"generation_id": generation_id, "promoted": True, "as_of": _iso(completed_at), "source": source,
            "events_gapless": sorted(cursors) == list(range(1, len(cursors) + 1)), "orders": orders}
