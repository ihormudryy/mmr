"""P3 Task 7 — append-only attribution evidence store (migrations 32-34).

Raw evidence lives in ``automation_decisions`` (and ``operator_action_refs``
for operator actions). Derived ``trade_attribution`` /
``execution_cost_attribution`` rows are rebuilt from raw evidence and may be
replaced for a trade_id; raw broker evidence is never overwritten.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Mapping, Optional, Sequence

from trader.data.schema_migrations import SchemaMigrator

ATTRIBUTION_MIGRATION_32 = 32
ATTRIBUTION_MIGRATION_33 = 33
ATTRIBUTION_MIGRATION_34 = 34
ATTRIBUTION_MIGRATION_VERSIONS = (
    ATTRIBUTION_MIGRATION_32,
    ATTRIBUTION_MIGRATION_33,
    ATTRIBUTION_MIGRATION_34,
)

ATTRIBUTION_MIGRATION_32_NAME = "p3_automation_decisions"
ATTRIBUTION_MIGRATION_33_NAME = "p3_trade_attribution"
ATTRIBUTION_MIGRATION_34_NAME = "p3_operator_action_refs"


def apply_attribution_migrations(migrator: SchemaMigrator) -> bool:
    """Apply journal migrations 32-34. Returns True if any newly applied."""
    applied = False
    applied |= migrator.apply(
        ATTRIBUTION_MIGRATION_32,
        ATTRIBUTION_MIGRATION_32_NAME,
        (
            """CREATE TABLE IF NOT EXISTS automation_decisions (
                evidence_key VARCHAR PRIMARY KEY,
                trade_id VARCHAR NOT NULL,
                event_kind VARCHAR NOT NULL,
                account_id VARCHAR,
                payload VARCHAR NOT NULL,
                source_timestamp TIMESTAMPTZ NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_automation_decisions_trade
                ON automation_decisions(trade_id)""",
            """CREATE INDEX IF NOT EXISTS idx_automation_decisions_kind
                ON automation_decisions(event_kind)""",
        ),
    )
    applied |= migrator.apply(
        ATTRIBUTION_MIGRATION_33,
        ATTRIBUTION_MIGRATION_33_NAME,
        (
            """CREATE TABLE IF NOT EXISTS trade_attribution (
                trade_id VARCHAR PRIMARY KEY,
                account_id VARCHAR,
                resolved BOOLEAN NOT NULL,
                payload VARCHAR NOT NULL,
                rebuilt_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS execution_cost_attribution (
                trade_id VARCHAR PRIMARY KEY,
                account_id VARCHAR,
                payload VARCHAR NOT NULL,
                rebuilt_at TIMESTAMPTZ NOT NULL
            )""",
        ),
    )
    applied |= migrator.apply(
        ATTRIBUTION_MIGRATION_34,
        ATTRIBUTION_MIGRATION_34_NAME,
        (
            """CREATE TABLE IF NOT EXISTS operator_action_refs (
                evidence_key VARCHAR PRIMARY KEY,
                trade_id VARCHAR NOT NULL,
                action_id VARCHAR NOT NULL,
                payload VARCHAR NOT NULL,
                source_timestamp TIMESTAMPTZ NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_operator_action_refs_trade
                ON operator_action_refs(trade_id)""",
        ),
    )
    return applied


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


class AttributionStore:
    """Persistence for raw attribution evidence and derived trade rows."""

    def __init__(self, db: Any):
        self.db = db

    def insert_evidence_in_tx(
        self,
        conn: Any,
        *,
        evidence_key: str,
        trade_id: str,
        event_kind: str,
        account_id: Optional[str],
        payload: Mapping[str, Any],
        source_timestamp: dt.datetime,
        recorded_at: dt.datetime,
    ) -> bool:
        """Append-only insert. Returns False if evidence_key already exists."""
        existing = conn.execute(
            "SELECT 1 FROM automation_decisions WHERE evidence_key = ?",
            [evidence_key],
        ).fetchone()
        if existing is not None:
            return False
        conn.execute(
            "INSERT INTO automation_decisions "
            "(evidence_key, trade_id, event_kind, account_id, payload, "
            "source_timestamp, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                evidence_key,
                trade_id,
                event_kind,
                account_id,
                json.dumps(dict(payload), sort_keys=True, default=str),
                _as_utc(source_timestamp),
                _as_utc(recorded_at),
            ],
        )
        if event_kind == "operator":
            action_id = str(payload.get("action_id") or evidence_key)
            conn.execute(
                "INSERT INTO operator_action_refs "
                "(evidence_key, trade_id, action_id, payload, "
                "source_timestamp, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    evidence_key,
                    trade_id,
                    action_id,
                    json.dumps(dict(payload), sort_keys=True, default=str),
                    _as_utc(source_timestamp),
                    _as_utc(recorded_at),
                ],
            )
        return True

    def list_evidence(self, trade_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT evidence_key, trade_id, event_kind, account_id, payload, "
            "source_timestamp, recorded_at "
            "FROM automation_decisions WHERE trade_id = ? "
            "ORDER BY source_timestamp ASC, evidence_key ASC",
            [trade_id],
            fetch="all",
        )
        out: list[dict[str, Any]] = []
        for row in rows or ():
            out.append({
                "evidence_key": row[0],
                "trade_id": row[1],
                "event_kind": row[2],
                "account_id": row[3],
                "payload": json.loads(row[4]),
                "source_timestamp": row[5],
                "recorded_at": row[6],
            })
        return out

    def list_trade_ids(self) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT trade_id FROM automation_decisions ORDER BY trade_id",
            fetch="all",
        )
        return [row[0] for row in (rows or ())]

    def save_derived_in_tx(
        self,
        conn: Any,
        *,
        trade_id: str,
        account_id: Optional[str],
        resolved: bool,
        attribution_payload: Mapping[str, Any],
        cost_payload: Mapping[str, Any],
        rebuilt_at: dt.datetime,
    ) -> None:
        """Replace derived rows for trade_id. Never touches raw evidence."""
        now = _as_utc(rebuilt_at)
        conn.execute("DELETE FROM trade_attribution WHERE trade_id = ?", [trade_id])
        conn.execute("DELETE FROM execution_cost_attribution WHERE trade_id = ?", [trade_id])
        conn.execute(
            "INSERT INTO trade_attribution "
            "(trade_id, account_id, resolved, payload, rebuilt_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                trade_id,
                account_id,
                resolved,
                json.dumps(dict(attribution_payload), sort_keys=True, default=str),
                now,
            ],
        )
        conn.execute(
            "INSERT INTO execution_cost_attribution "
            "(trade_id, account_id, payload, rebuilt_at) VALUES (?, ?, ?, ?)",
            [
                trade_id,
                account_id,
                json.dumps(dict(cost_payload), sort_keys=True, default=str),
                now,
            ],
        )

    def load_derived(self, trade_id: str) -> Optional[dict[str, Any]]:
        row = self.db.execute(
            "SELECT payload, resolved FROM trade_attribution WHERE trade_id = ?",
            [trade_id],
            fetch="one",
        )
        if row is None:
            return None
        payload = json.loads(row[0])
        payload["resolved"] = bool(row[1])
        return payload
