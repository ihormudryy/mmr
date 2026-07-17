"""[M1-F3] Task 7 -- strategy revisions, receipt ledger, and the
acknowledgement outbox (strategy-service side).

This module owns the strategy_service's OWN DuckDB-backed bookkeeping for
coordinator-forwarded strategy-control commands (``enable_strategy``,
``disable_strategy``, ``update_strategy_params``): a per-strategy
``control_revision`` (CAS guard for the NEXT forwarded command) and
``state_revision`` (bumped every time the strategy's observable state
actually changes), an idempotent command-receipt ledger keyed by
``command_id`` (so a coordinator retry after a lost reply never re-applies
the mutation), a transactional acknowledgement outbox (so the trader can
learn about every state_revision bump even if a specific RPC reply is
lost), and a staged-config-swap ledger (``strategy_config_revisions``) that
makes a YAML hot-swap for ``update_strategy_params`` crash-safe: a process
that dies between staging and committing recovers to the PRIOR
configuration on the next startup via ``recover_on_startup``.

Migration versioning: this is a dedicated migration sequence (version 1,
``m1f3_strategy_revisions``) against whatever DuckDB file the caller
supplies -- typically the SAME ``duckdb_path`` file ``StrategyRuntime``
already uses for ``strategy_state`` (enabled/disabled persistence). This is
NOT the trader's journal file and does NOT collide with [M1-F1]'s
versions 1-9 or [M1-F3]'s trader-side versions 20-29 (those apply against
``journal_duckdb_path``, a completely different file) -- ``SchemaMigrator``
is storage-agnostic and each DuckDB file gets its own independent
``schema_migrations`` ledger.

B3 correction (binding, from the m1f3 briefing): ``state_revision`` is
carried in the acknowledgement PAYLOAD, never claimed to equal the
trader's own journal ``entity_revision`` by direct assignment (``mutate()``
always computes that itself as ``current + 1`` -- there is no
``entity_revision`` field on ``DomainMutation``). The trader-side consumer
(``trader/trading/command_coordinator.py``'s ``StrategyControlCommandService``)
achieves "entity_revision == state_revision" only as an emergent property
of both counters starting fresh and advancing in lockstep, guarded with an
assert-only ``write_materialized`` callback (mirrors
``command_coordinator.py``'s own ``_assert_proposal_revision`` idiom) --
never by forcing the value.
"""
from __future__ import annotations

import datetime as dt
import logging as _stdlib_logging
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

logger = _stdlib_logging.getLogger(__name__)

# This module owns its own fresh migration sequence (version 1) against
# whatever DuckDB file the caller points it at -- independent of [M1-F1]'s
# journal-file versions 1-9 and [M1-F3]'s trader-side journal-file versions
# 20-29 (different file entirely).
STRATEGY_REVISIONS_MIGRATION_VERSION = 1
STRATEGY_REVISIONS_MIGRATION_NAME = "m1f3_strategy_revisions"

# Sequences are created BEFORE the tables that consume their defaults
# (binding convention, mirrors command_coordinator.py's command_audit_seq).
_STRATEGY_REVISIONS_STATEMENTS = (
    "CREATE SEQUENCE IF NOT EXISTS strategy_ack_seq START 1",
    "CREATE SEQUENCE IF NOT EXISTS strategy_config_rev_seq START 1",
    """
    CREATE TABLE IF NOT EXISTS strategy_revisions (
        strategy_name VARCHAR PRIMARY KEY,
        state_revision BIGINT NOT NULL,
        control_revision BIGINT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_command_receipts (
        command_id VARCHAR PRIMARY KEY,
        strategy_name VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        state VARCHAR NOT NULL CHECK (state IN ('COMMITTED', 'ROLLED_BACK')),
        control_revision BIGINT NOT NULL,
        state_revision BIGINT NOT NULL,
        error VARCHAR,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_ack_outbox (
        ack_id BIGINT PRIMARY KEY DEFAULT nextval('strategy_ack_seq'),
        strategy_name VARCHAR NOT NULL,
        state_revision BIGINT NOT NULL,
        control_revision BIGINT NOT NULL,
        payload JSON NOT NULL,
        acknowledged BOOLEAN NOT NULL DEFAULT false,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_config_revisions (
        revision_id BIGINT PRIMARY KEY DEFAULT nextval('strategy_config_rev_seq'),
        strategy_name VARCHAR NOT NULL,
        expected_control_revision BIGINT NOT NULL,
        prior_config JSON NOT NULL,
        proposed_config JSON NOT NULL,
        state VARCHAR NOT NULL CHECK (state IN ('PREPARED', 'COMMITTED', 'ROLLED_BACK')),
        command_id VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        resolved_at TIMESTAMPTZ
    )
    """,
)


def apply_strategy_revisions_migration(migrator: SchemaMigrator) -> None:
    """Create the strategy-revisions tables (idempotent, safe every startup)."""
    migrator.apply(
        version=STRATEGY_REVISIONS_MIGRATION_VERSION,
        name=STRATEGY_REVISIONS_MIGRATION_NAME,
        statements=list(_STRATEGY_REVISIONS_STATEMENTS),
    )


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass(frozen=True)
class StrategyCommandReceipt:
    """[M1-F3] Task 7 -- FROZEN field order (per the task brief).

    Returned by ``StrategyRuntime.apply_control_command`` (new or replayed)
    and by ``StrategyRevisionStore.get_receipt``/``record_receipt_in_tx``.
    """

    command_id: str
    strategy_name: str
    action: str
    state: Literal["COMMITTED", "ROLLED_BACK"]
    control_revision: int
    state_revision: int
    error: Optional[str] = None


@dataclass(frozen=True)
class OutboxRow:
    """One durable, not-yet-acknowledged ``strategy_ack_outbox`` row."""

    ack_id: int
    strategy_name: str
    state_revision: int
    control_revision: int
    payload: dict[str, Any]
    created_at: dt.datetime


_RECEIPT_COLUMNS = (
    "command_id", "strategy_name", "action", "state",
    "control_revision", "state_revision", "error",
)


class StrategyRevisionStore:
    """Persistence adapter for strategy-service revisions/receipts/outbox.

    ``db`` is a ``DuckDBConnection`` -- typically the SAME file
    ``StrategyRuntime`` already uses for ``strategy_state``
    (``self.duckdb_path``), never the trader's dedicated
    ``journal_duckdb_path`` file. Exposed as the public ``self.db`` attribute
    so callers (``StrategyRuntime._record``, tests) can run their own
    ``db.transaction(...)`` units of work against the same file/lock.
    """

    def __init__(self, db: DuckDBConnection, now: Callable[[], dt.datetime] = _utcnow):
        self.db = db
        self._now = now

    def migrate(self) -> None:
        apply_strategy_revisions_migration(SchemaMigrator(self.db))

    # -- control / state revisions ----------------------------------------

    def _ensure_row_in_tx(self, conn: Any, strategy_name: str) -> tuple[int, int]:
        """Return ``(state_revision, control_revision)``, creating a fresh
        ``(0, 0)`` row on first reference. Must run inside the caller's
        transaction (``conn``) -- never opens its own."""
        row = conn.execute(
            "SELECT state_revision, control_revision FROM strategy_revisions "
            "WHERE strategy_name = ?",
            [strategy_name],
        ).fetchone()
        if row is not None:
            return row[0], row[1]
        now = self._now()
        conn.execute(
            "INSERT INTO strategy_revisions "
            "(strategy_name, state_revision, control_revision, updated_at) "
            "VALUES (?, 0, 0, ?)",
            [strategy_name, now],
        )
        return 0, 0

    def control_revision(self, strategy_name: str) -> int:
        def _tx(conn):
            _, control = self._ensure_row_in_tx(conn, strategy_name)
            return control
        return self.db.transaction(_tx)

    def state_revision(self, strategy_name: str) -> int:
        def _tx(conn):
            state, _ = self._ensure_row_in_tx(conn, strategy_name)
            return state
        return self.db.transaction(_tx)

    def bump_control_revision_in_tx(self, conn: Any, strategy_name: str) -> int:
        """Bump and return the new ``control_revision``. Caller-owned
        transaction (used from inside ``StrategyRuntime.apply_control_command``'s
        commit closure)."""
        _, control = self._ensure_row_in_tx(conn, strategy_name)
        new_control = control + 1
        conn.execute(
            "UPDATE strategy_revisions SET control_revision = ?, updated_at = ? "
            "WHERE strategy_name = ?",
            [new_control, self._now(), strategy_name],
        )
        return new_control

    def bump_state_revision_in_tx(
        self, conn: Any, strategy_name: str, payload: dict[str, Any],
    ) -> int:
        """Bump ``state_revision`` and write the acknowledgement-outbox row
        for it, atomically, on the caller's transaction. If the caller's
        transaction later rolls back, NEITHER the bump nor the outbox row
        survive (proven by ``test_state_revision_and_outbox_commit_together``)."""
        state, control = self._ensure_row_in_tx(conn, strategy_name)
        new_state = state + 1
        now = self._now()
        conn.execute(
            "UPDATE strategy_revisions SET state_revision = ?, updated_at = ? "
            "WHERE strategy_name = ?",
            [new_state, now, strategy_name],
        )
        conn.execute(
            "INSERT INTO strategy_ack_outbox "
            "(strategy_name, state_revision, control_revision, payload, "
            " acknowledged, created_at) VALUES (?, ?, ?, ?, false, ?)",
            [strategy_name, new_state, control, payload, now],
        )
        return new_state

    # -- command receipts ---------------------------------------------------

    def get_receipt(self, command_id: str) -> Optional[StrategyCommandReceipt]:
        row = self.db.execute(
            f"SELECT {', '.join(_RECEIPT_COLUMNS)} FROM strategy_command_receipts "
            "WHERE command_id = ?",
            [command_id],
            fetch="one",
        )
        if row is None:
            return None
        return StrategyCommandReceipt(*row)

    def record_receipt_in_tx(
        self,
        conn: Any,
        command_id: str,
        strategy_name: str,
        action: str,
        state: Literal["COMMITTED", "ROLLED_BACK"],
        control_revision: int,
        state_revision: int,
        error: Optional[str] = None,
    ) -> StrategyCommandReceipt:
        conn.execute(
            "INSERT INTO strategy_command_receipts "
            "(command_id, strategy_name, action, state, control_revision, "
            " state_revision, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [command_id, strategy_name, action, state, control_revision,
             state_revision, error, self._now()],
        )
        return StrategyCommandReceipt(
            command_id=command_id, strategy_name=strategy_name, action=action,
            state=state, control_revision=control_revision,
            state_revision=state_revision, error=error,
        )

    # -- staged config swap (update_strategy_params crash-safety) -----------

    def prepare_config_revision(
        self,
        strategy_name: str,
        expected_control_revision: int,
        prior: dict[str, Any],
        proposed: dict[str, Any],
        command_id: str,
    ) -> int:
        def _tx(conn):
            row = conn.execute(
                "INSERT INTO strategy_config_revisions "
                "(strategy_name, expected_control_revision, prior_config, "
                " proposed_config, state, command_id, created_at) "
                "VALUES (?, ?, ?, ?, 'PREPARED', ?, ?) RETURNING revision_id",
                [strategy_name, expected_control_revision, prior, proposed,
                 command_id, self._now()],
            ).fetchone()
            return row[0]
        return self.db.transaction(_tx)

    def mark_committed(self, revision_id: int) -> None:
        def _tx(conn):
            conn.execute(
                "UPDATE strategy_config_revisions SET state = 'COMMITTED', "
                "resolved_at = ? WHERE revision_id = ?",
                [self._now(), revision_id],
            )
        self.db.transaction(_tx)

    def mark_rolled_back(self, revision_id: int, error: Optional[str] = None) -> None:
        """Mark a staged config revision ROLLED_BACK. ``error`` has no
        dedicated column on ``strategy_config_revisions`` (the frozen DDL) --
        the durable error text lives on the command receipt
        (``strategy_command_receipts.error``) instead; it's logged here
        purely for operational visibility."""
        if error:
            logger.warning(
                "strategy config revision %s rolled back: %s", revision_id, error,
            )

        def _tx(conn):
            conn.execute(
                "UPDATE strategy_config_revisions SET state = 'ROLLED_BACK', "
                "resolved_at = ? WHERE revision_id = ?",
                [self._now(), revision_id],
            )
        self.db.transaction(_tx)

    def config_revision_states(self, strategy_name: str) -> list[str]:
        rows = self.db.execute(
            "SELECT state FROM strategy_config_revisions WHERE strategy_name = ? "
            "ORDER BY revision_id",
            [strategy_name],
            fetch="all",
        )
        return [row[0] for row in rows]

    def config_revision_state(self, revision_id: int) -> Optional[str]:
        row = self.db.execute(
            "SELECT state FROM strategy_config_revisions WHERE revision_id = ?",
            [revision_id],
            fetch="one",
        )
        return row[0] if row is not None else None

    def get_config_revision(self, revision_id: int) -> Optional[dict[str, Any]]:
        """Full row read-back (used by startup recovery to restore
        ``prior_config`` into the live YAML)."""
        row = self.db.execute(
            "SELECT revision_id, strategy_name, expected_control_revision, "
            "prior_config, proposed_config, state, command_id "
            "FROM strategy_config_revisions WHERE revision_id = ?",
            [revision_id],
            fetch="one",
        )
        if row is None:
            return None
        (revision_id_, strategy_name, expected_control_revision,
         prior_config, proposed_config, state, command_id) = row
        return {
            "revision_id": revision_id_,
            "strategy_name": strategy_name,
            "expected_control_revision": expected_control_revision,
            "prior_config": prior_config,
            "proposed_config": proposed_config,
            "state": state,
            "command_id": command_id,
        }

    def recover_on_startup(self) -> list[int]:
        """Roll back every ``PREPARED`` staged-config row to ``ROLLED_BACK``.

        Must run before the service reports ready (called from
        ``StrategyRuntime.recover_startup_config`` at the top of ``run()``).
        A ``PREPARED`` row surviving to startup means the process crashed
        between staging the YAML swap and committing it -- the safe
        recovery is "never applied", never "assume it committed". Returns
        the list of recovered ``revision_id``s so the caller can also
        restore each one's ``prior_config`` into the live YAML.
        """
        def _tx(conn):
            rows = conn.execute(
                "SELECT revision_id FROM strategy_config_revisions "
                "WHERE state = 'PREPARED' ORDER BY revision_id"
            ).fetchall()
            ids = [row[0] for row in rows]
            now = self._now()
            for revision_id in ids:
                conn.execute(
                    "UPDATE strategy_config_revisions SET state = 'ROLLED_BACK', "
                    "resolved_at = ? WHERE revision_id = ?",
                    [now, revision_id],
                )
            return ids
        return self.db.transaction(_tx)

    # -- acknowledgement outbox ----------------------------------------------

    def unacknowledged_outbox(self, limit: int) -> list[OutboxRow]:
        rows = self.db.execute(
            "SELECT ack_id, strategy_name, state_revision, control_revision, "
            "payload, created_at FROM strategy_ack_outbox "
            "WHERE acknowledged = false ORDER BY ack_id LIMIT ?",
            [limit],
            fetch="all",
        )
        result = []
        for ack_id, strategy_name, state_revision, control_revision, payload, created_at in rows:
            result.append(OutboxRow(
                ack_id=ack_id, strategy_name=strategy_name,
                state_revision=state_revision, control_revision=control_revision,
                payload=payload, created_at=created_at,
            ))
        return result

    def mark_acknowledged(self, ack_id: int) -> None:
        self.db.execute(
            "UPDATE strategy_ack_outbox SET acknowledged = true WHERE ack_id = ?",
            [ack_id],
        )
