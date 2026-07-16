"""Risk and reconciliation domain-event producers. [M1-F2]"""
from __future__ import annotations

import datetime as dt
import json
import threading
from typing import Any, Callable, Optional

from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.domain.identity import (
    reconciliation_run_entity_id,
    risk_decision_entity_id,
    risk_policy_entity_id,
    risk_projection_entity_id,
)


RISK_STATE_MIGRATION_VERSION = 11
_RISK_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS risk_state (
        risk_id VARCHAR PRIMARY KEY,
        kind VARCHAR NOT NULL CHECK (kind IN ('policy', 'projection', 'decision')),
        account_id VARCHAR,
        payload JSON NOT NULL,
        revision BIGINT NOT NULL,
        source_timestamp TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS reconciliation_runs (
        run_id VARCHAR PRIMARY KEY,
        trigger VARCHAR NOT NULL CHECK (trigger IN ('startup', 'scheduled', 'command', 'operator')),
        source_cursor BIGINT,
        discrepancies JSON NOT NULL,
        resolutions JSON NOT NULL,
        started_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ NOT NULL,
        revision BIGINT NOT NULL
    )""",
]


class RiskDecisionImmutable(RuntimeError):
    """A command's risk decision is historical fact and cannot be rewritten."""


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class RiskProducer:
    def __init__(
        self,
        db: Any,
        journal: Any,
        account_id: str,
        compute_projection: Callable[[], dict],
        debounce_seconds: float = 0.1,
        timer_factory: Callable[..., Any] = threading.Timer,
        clock: Optional[Callable[[], dt.datetime]] = None,
    ):
        if debounce_seconds <= 0 or debounce_seconds > 0.1:
            raise ValueError("projection debounce must be greater than zero and at most 100 ms")
        self.db = db
        self.journal = journal
        self.account_id = account_id
        self.compute_projection = compute_projection
        self.debounce_seconds = debounce_seconds
        self.timer_factory = timer_factory
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._timer_lock = threading.Lock()
        self._pending_timer: Any = None

    def migrate(self, migrator: SchemaMigrator) -> None:
        migrator.apply(RISK_STATE_MIGRATION_VERSION, "risk_state_tables", _RISK_STATEMENTS)

    @staticmethod
    def _get_in_tx(conn: Any, risk_id: str) -> Any:
        return conn.execute(
            "SELECT payload FROM risk_state WHERE risk_id = ?", [risk_id]
        ).fetchone()

    def _append_write(
        self,
        conn: Any,
        append: Callable[..., Any],
        risk_id: str,
        kind: str,
        payload: dict,
        correlation_id: Optional[str],
    ) -> Any:
        now = self.clock()
        mutation = DomainMutation(
            event_type="risk.updated",
            entity_type="risk",
            entity_id=risk_id,
            operation="upsert",
            account_id=self.account_id,
            source="trader_service",
            source_timestamp=now,
            correlation_id=correlation_id,
            payload={"kind": kind, "risk_id": risk_id, **payload},
        )

        def write(write_conn: Any, revision: int) -> None:
            write_conn.execute("DELETE FROM risk_state WHERE risk_id = ?", [risk_id])
            write_conn.execute(
                "INSERT INTO risk_state (risk_id, kind, account_id, payload, revision, "
                "source_timestamp, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [risk_id, kind, self.account_id, json.dumps(payload, sort_keys=True), revision, now, now],
            )

        return append(mutation, write)

    def _publish_replaceable(
        self, risk_id: str, kind: str, payload: dict, correlation_id: Optional[str] = None
    ) -> Any:
        def work(conn: Any, append: Callable[..., Any]) -> Any:
            current = self._get_in_tx(conn, risk_id)
            if current is not None and _json_value(current[0]) == payload:
                return None
            return self._append_write(conn, append, risk_id, kind, payload, correlation_id)

        return self.journal.mutate_batch_work(self.journal.connect(), work)

    def publish_policy(self, policy_id: str, payload: dict) -> Any:
        return self._publish_replaceable(risk_policy_entity_id(policy_id), "policy", payload)

    def publish_decision(
        self, command_id: str, payload: dict, correlation_id: Optional[str] = None
    ) -> Any:
        risk_id = risk_decision_entity_id(command_id)

        def work(conn: Any, append: Callable[..., Any]) -> Any:
            current = self._get_in_tx(conn, risk_id)
            if current is not None:
                if _json_value(current[0]) == payload:
                    return None
                raise RiskDecisionImmutable(f"risk decision for command {command_id} already recorded")
            return self._append_write(
                conn, append, risk_id, "decision", payload, correlation_id or command_id
            )

        return self.journal.mutate_batch_work(self.journal.connect(), work)

    def mark_projection_dirty(self) -> None:
        with self._timer_lock:
            if self._pending_timer is not None:
                return
            self._pending_timer = self.timer_factory(self.debounce_seconds, self.flush_projection)
            self._pending_timer.start()

    def flush_projection(self) -> Any:
        with self._timer_lock:
            self._pending_timer = None
        try:
            payload = self.compute_projection()
        except Exception:
            return None
        return self._publish_replaceable(
            risk_projection_entity_id(self.account_id), "projection", payload
        )


class ReconciliationProducer:
    def __init__(
        self, db: Any, journal: Any, clock: Optional[Callable[[], dt.datetime]] = None
    ):
        self.db = db
        self.journal = journal
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    def migrate(self, migrator: SchemaMigrator) -> None:
        migrator.apply(RISK_STATE_MIGRATION_VERSION, "risk_state_tables", _RISK_STATEMENTS)

    def publish_run(
        self,
        run_id: str,
        trigger: str,
        source_cursor: Optional[int],
        discrepancies: list,
        resolutions: list,
        started_at: dt.datetime,
        completed_at: dt.datetime,
    ) -> Any:
        payload = {
            "run_id": run_id,
            "trigger": trigger,
            "source_cursor": source_cursor,
            "discrepancies": discrepancies,
            "resolutions": resolutions,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
        }
        mutation = DomainMutation(
            event_type="reconciliation.updated",
            entity_type="reconciliation",
            entity_id=reconciliation_run_entity_id(run_id),
            operation="upsert",
            account_id=None,
            source="trader_service",
            source_timestamp=completed_at,
            correlation_id=None,
            payload=payload,
        )

        def write(conn: Any, revision: int) -> None:
            conn.execute("DELETE FROM reconciliation_runs WHERE run_id = ?", [run_id])
            conn.execute(
                "INSERT INTO reconciliation_runs (run_id, trigger, source_cursor, discrepancies, "
                "resolutions, started_at, completed_at, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    run_id,
                    trigger,
                    source_cursor,
                    json.dumps(discrepancies, sort_keys=True),
                    json.dumps(resolutions, sort_keys=True),
                    started_at,
                    completed_at,
                    revision,
                ],
            )

        return self.journal.mutate(self.journal.connect(), mutation, write)
