"""Durable automation circuit-breaker persistence (migration 24)."""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation

CIRCUIT_BREAKER_MIGRATION_VERSION = 24
CIRCUIT_BREAKER_MIGRATION_NAME = "p1_automation_circuit_breaker"

_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS automation_circuit_breaker (
        account_id VARCHAR PRIMARY KEY,
        state VARCHAR NOT NULL CHECK (state IN ('CLEAR', 'TRIPPED')),
        reason_code VARCHAR,
        reason VARCHAR,
        revision BIGINT NOT NULL,
        tripped_at TIMESTAMPTZ,
        reset_at TIMESTAMPTZ,
        reset_command_id VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS automation_incidents (
        incident_id VARCHAR PRIMARY KEY,
        account_id VARCHAR NOT NULL,
        signal_kind VARCHAR NOT NULL,
        detail VARCHAR NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    )""",
)


def apply_circuit_breaker_migration(migrator: SchemaMigrator) -> None:
    migrator.apply(CIRCUIT_BREAKER_MIGRATION_VERSION, CIRCUIT_BREAKER_MIGRATION_NAME, _STATEMENTS)


@dataclass(frozen=True)
class BreakerState:
    account_id: str
    state: str
    reason_code: Optional[str]
    reason: Optional[str]
    revision: int
    tripped_at: Optional[dt.datetime]
    reset_at: Optional[dt.datetime]
    reset_command_id: Optional[str]

    def to_payload(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "state": self.state,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "revision": self.revision,
            "tripped_at": self.tripped_at.isoformat() if self.tripped_at else None,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "reset_command_id": self.reset_command_id,
        }


_COLUMNS = (
    "account_id", "state", "reason_code", "reason", "revision", "tripped_at",
    "reset_at", "reset_command_id",
)


def _state(row) -> BreakerState:
    return BreakerState(**dict(zip(_COLUMNS, row)))


class CircuitBreakerStore:
    def __init__(self, journal: DomainJournal, account_id: str):
        self.journal = journal
        self.account_id = account_id

    def seed(self, now: dt.datetime) -> BreakerState:
        self.journal.db.transaction(lambda conn: conn.execute(
            "INSERT INTO automation_circuit_breaker VALUES (?, 'CLEAR', NULL, NULL, 1, "
            "NULL, NULL, NULL) ON CONFLICT DO NOTHING",
            [self.account_id],
        ))
        return self.get()

    def get(self) -> BreakerState:
        row = self.journal.connect().execute(
            f"SELECT {', '.join(_COLUMNS)} FROM automation_circuit_breaker WHERE account_id = ?",
            [self.account_id],
        ).fetchone()
        if row is None:
            raise RuntimeError(f"automation breaker not seeded for {self.account_id}")
        return _state(row)

    @staticmethod
    def incident_id(account_id: str, kind: str, occurred_at: dt.datetime, key: str) -> str:
        raw = f"{account_id}|{kind}|{key or occurred_at.isoformat()}".encode()
        return hashlib.sha256(raw).hexdigest()

    def apply_signal(
        self,
        *,
        kind: str,
        detail: str,
        occurred_at: dt.datetime,
        key: str,
        recorded_at: dt.datetime,
        trip_reason: Callable[[Any], Optional[tuple[str, str]]],
    ) -> BreakerState:
        incident_id = self.incident_id(self.account_id, kind, occurred_at, key)

        def work(conn, append):
            existing = conn.execute(
                "SELECT 1 FROM automation_incidents WHERE incident_id = ?", [incident_id]
            ).fetchone()
            if existing is not None:
                row = conn.execute(
                    f"SELECT {', '.join(_COLUMNS)} FROM automation_circuit_breaker "
                    "WHERE account_id = ?", [self.account_id],
                ).fetchone()
                return _state(row)
            conn.execute(
                "INSERT INTO automation_incidents VALUES (?, ?, ?, ?, ?, ?)",
                [incident_id, self.account_id, kind, detail, occurred_at, recorded_at],
            )
            current = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM automation_circuit_breaker "
                "WHERE account_id = ?", [self.account_id],
            ).fetchone()
            if current is None:
                raise RuntimeError("automation breaker is not seeded")
            current_state = _state(current)
            reason = trip_reason(conn)
            if current_state.state == "CLEAR" and reason is not None:
                code, message = reason
                conn.execute(
                    "UPDATE automation_circuit_breaker SET state='TRIPPED', reason_code=?, "
                    "reason=?, revision=revision+1, tripped_at=?, reset_at=NULL, "
                    "reset_command_id=NULL WHERE account_id=?",
                    [code, message, recorded_at, self.account_id],
                )
            updated = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM automation_circuit_breaker "
                "WHERE account_id = ?", [self.account_id],
            ).fetchone()
            result = _state(updated)
            mutation = DomainMutation(
                event_type="automation.incident",
                entity_type="automation_breaker",
                entity_id=self.account_id,
                operation="upsert",
                account_id=self.account_id,
                source="trader_service",
                source_timestamp=recorded_at,
                correlation_id=incident_id,
                payload={"incident_id": incident_id, "signal_kind": kind, **result.to_payload()},
            )
            append(mutation, lambda _conn, _revision: None, incident_id)
            return result

        return self.journal.mutate_batch_work(self.journal.connect(), work)

    def count_since(self, conn, kind: str, since: dt.datetime) -> int:
        return conn.execute(
            "SELECT count(*) FROM automation_incidents WHERE account_id=? "
            "AND signal_kind=? AND occurred_at>=?",
            [self.account_id, kind, since],
        ).fetchone()[0]

    def disconnect_started(self, conn) -> Optional[dt.datetime]:
        reconnected = conn.execute(
            "SELECT max(occurred_at) FROM automation_incidents WHERE account_id=? "
            "AND signal_kind='BROKER_RECONNECTED'", [self.account_id],
        ).fetchone()[0]
        row = conn.execute(
            "SELECT min(occurred_at) FROM automation_incidents WHERE account_id=? "
            "AND signal_kind='BROKER_DISCONNECTED' AND (? IS NULL OR occurred_at>?)",
            [self.account_id, reconnected, reconnected],
        ).fetchone()
        return row[0] if row else None

    def reset(self, command_id: str, reason: str, now: dt.datetime) -> BreakerState:
        def work(conn, append):
            current = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM automation_circuit_breaker WHERE account_id=?",
                [self.account_id],
            ).fetchone()
            state = _state(current)
            if state.state == "CLEAR":
                return state
            conn.execute(
                "UPDATE automation_circuit_breaker SET state='CLEAR', reason_code=NULL, reason=?, "
                "revision=revision+1, reset_at=?, reset_command_id=? WHERE account_id=?",
                [reason, now, command_id, self.account_id],
            )
            updated = self.get_in_tx(conn)
            mutation = DomainMutation(
                event_type="automation.reset", entity_type="automation_breaker",
                entity_id=self.account_id, operation="upsert", account_id=self.account_id,
                source="trader_service", source_timestamp=now, correlation_id=command_id,
                payload=updated.to_payload(),
            )
            append(mutation, lambda _conn, _revision: None, f"breaker-reset:{command_id}")
            return updated
        return self.journal.mutate_batch_work(self.journal.connect(), work)

    def get_in_tx(self, conn) -> BreakerState:
        return _state(conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM automation_circuit_breaker WHERE account_id=?",
            [self.account_id],
        ).fetchone())
