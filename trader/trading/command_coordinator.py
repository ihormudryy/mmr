"""Command ledger, audit, and the ``TradingCommandCoordinator`` boundary.

[M1-F3] Task 3. ``TradingCommandCoordinator`` is the SOLE production
mutation boundary: every trading command (create/reject a proposal, later
approve/cancel/pause/strategy-forward) flows through ``execute()``, which
claims an idempotent ``command_ledger`` row and a mandatory ``command_audit``
record BEFORE any business validation or side effect runs, then hands the
request to a per-action handler and drives the ledger's saga-state
transitions to a terminal outcome.

Connection / transaction discipline (binding pre-flight resolution B1)
------------------------------------------------------------------------
``DomainJournal.mutate(conn, mutation, write_materialized)`` self-manages its
own ``BEGIN``/``COMMIT`` on a ``journal.connect()`` cursor -- wrapping it in
``DuckDBConnection.transaction`` double-``BEGIN``s and DuckDB rejects that.
Every command-ledger transition in this module therefore calls
``journal.mutate()`` exactly ONCE per transition, with the actual
``command_ledger``/``command_audit`` row writes (and, for the very first
transition, the mandatory preflight-nonce consumption) performed *inside*
the ``write_materialized`` callback that ``mutate()`` already runs inside its
own transaction. This gives the initial claim true atomicity across nonce
consumption, the ledger row insert, the audit record insert, and the
``command.updated`` RECEIVED journal event -- a failure anywhere in that
callback rolls back the whole thing and NOTHING is left half-persisted
(satisfying "failure to persist either fails the command closed" without
ever wrapping ``mutate()`` in an outer transaction).

The one exception is ``CommandLedger.claim_or_replay_in_tx``'s own lookup:
it is a pure, read-only ``SELECT`` with no journal event of its own, so it
runs directly on a fresh ``journal.connect()`` cursor with no explicit
transaction at all (a single autocommitted statement) -- this is the "pure
ledger claim with no journal event" case the pre-flight resolution calls out
as allowed to stand alone.

Command-ledger ordering (binding, spec-derived; order matters)
------------------------------------------------------------------
1. Exact-retry lookup by ``command_id`` + ``canonical_request_hash`` --
   PRECEDES preflight-nonce validation. A matching hash is an idempotent
   replay (returns the recorded receipt, nonce untouched); a mismatched hash
   is a ``COMMAND_CONFLICT`` (the caller reused an id for a different
   command).
2. Preflight-nonce validation (only for actions registered
   ``requires_preflight=True``).
3. The ledger row (state ``RECEIVED``) and the mandatory audit record are
   inserted -- BEFORE any handler-level validation or side effect runs.
4. The registered action handler runs. Raising ``CommandValidationError``
   transitions the command to ``REJECTED`` with the exception's code;
   returning normally transitions it to ``RESOLVED`` carrying the handler's
   return value as ``outcome``.

``command_id`` is colon-free by construction (``CommandRequest.__post_init__``
raises ``ValueError`` otherwise) because ``encode_order_ref`` builds
``mmr:og-{command_id}`` (`M1-F2]`'s ``trader/trading/order_correlation.py``)
-- a colon inside ``command_id`` would corrupt that encoding.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, Optional, Protocol

import duckdb

from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.commands import CommandReceipt
from trader.domain.events import DomainMutation
from trader.domain.identity import command_entity_id
from trader.messaging.typed_rpc import canonical_json

# [M1-F3] owns trader-DB (journal file) migration versions 20-29; Task 1
# used 20 for trade_proposals. This task owns 21.
COMMAND_LEDGER_MIGRATION_VERSION = 21
COMMAND_LEDGER_MIGRATION_NAME = "m1f3_command_ledger_audit"

# The seven saga states, verbatim (binding). Order here is documentation
# only -- the CHECK constraint below is the actual enforcement.
COMMAND_STATES = (
    "RECEIVED", "VALIDATED", "SUBMITTING", "SUBMITTED",
    "REJECTED", "OUTCOME_UNKNOWN", "RESOLVED",
)
_TERMINAL_STATES = frozenset({"RESOLVED", "REJECTED"})

RETENTION = dt.timedelta(days=30)

# The sequence is created BEFORE the table that consumes it (binding).
_COMMAND_LEDGER_STATEMENTS = (
    "CREATE SEQUENCE command_audit_seq START 1",
    f"""
    CREATE TABLE command_ledger (
        command_id VARCHAR PRIMARY KEY,
        request_hash VARCHAR NOT NULL,
        account_id VARCHAR,
        action VARCHAR NOT NULL,
        target_type VARCHAR NOT NULL,
        target_id VARCHAR NOT NULL,
        expected_version BIGINT,
        state VARCHAR NOT NULL CHECK (state IN (
            {", ".join(repr(s) for s in COMMAND_STATES)}
        )),
        outcome JSON,
        error_code VARCHAR,
        source VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE command_audit (
        audit_id BIGINT PRIMARY KEY DEFAULT nextval('command_audit_seq'),
        command_id VARCHAR NOT NULL,
        correlation_id VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        target_type VARCHAR NOT NULL,
        target_id VARCHAR NOT NULL,
        expected_version BIGINT,
        redacted_inputs JSON NOT NULL,
        validation_result VARCHAR,
        acknowledgement VARCHAR,
        outcome VARCHAR,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
)


def apply_command_ledger_migration(journal_migrator: SchemaMigrator) -> None:
    """Create ``command_ledger`` + ``command_audit`` in the journal DB.

    ``journal_migrator`` targets the dedicated ``journal_duckdb_path`` file
    (the same file ``apply_proposal_authority_migration`` (version 20)
    targets) -- co-locating the command ledger with the journal is what lets
    a command transition and its ``command.updated`` event commit in one
    ``DomainJournal.mutate()`` call. Idempotent via ``SchemaMigrator``.
    """
    journal_migrator.apply(
        version=COMMAND_LEDGER_MIGRATION_VERSION,
        name=COMMAND_LEDGER_MIGRATION_NAME,
        statements=list(_COMMAND_LEDGER_STATEMENTS),
    )


class RiskDirection(str, Enum):
    """Whether a command increases or reduces market exposure.

    Always computed by the coordinator/handler from broker-verified position
    state -- NEVER caller-supplied (there is no ``risk_direction`` field on
    ``CommandRequest`` or any typed request model). Exists here, ready for
    Task 5's approval saga, which is the first handler that needs it to pick
    the live-quote/drift-guard path (``INCREASING``) vs. the
    reducible-quantity-capped exempt path (``REDUCING``).
    """

    INCREASING = "INCREASING"
    REDUCING = "REDUCING"


class CommandValidationError(Exception):
    """Raised by a registered action handler to reject a command.

    Distinct from an unexpected internal failure: this is the expected,
    structured "business validation failed" outcome (e.g. a risk check, a
    stale proposal, an illegal state) and always carries a stable
    machine-readable ``code`` the caller can branch on.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class IllegalCommandTransition(Exception):
    """A guarded ``transition_in_tx`` CAS missed its expected ``from_state``.

    Either a producer bug (transitioning from the wrong state) or a genuine
    concurrent-writer race; either way this must never be silently ignored.
    """


@dataclass(frozen=True)
class CommandRequest:
    """One command envelope submitted to the coordinator.

    ``command_id`` is colon-free by construction: ``encode_order_ref``
    (``trader/trading/order_correlation.py``, ``[M1-F2]``) builds
    ``mmr:og-{command_id}``, so a colon inside ``command_id`` would corrupt
    that encoding. There is deliberately no ``skip_risk_gate`` field, here or
    anywhere else on the command surface.
    """

    command_id: str
    action: str
    account_id: Optional[str]
    target_type: str
    target_id: str
    expected_version: Optional[int]
    body: dict[str, Any]
    source: str
    preflight_nonce: Optional[str] = None

    def __post_init__(self) -> None:
        if ":" in self.command_id:
            raise ValueError(
                f"command_id must not contain ':' (encode_order_ref reserves it "
                f"for the mmr: orderRef prefix): {self.command_id!r}"
            )


def canonical_request_hash(request: CommandRequest) -> str:
    """SHA-256 over the canonical-JSON projection of the request's identity.

    Deliberately excludes ``source`` and ``preflight_nonce``: a nonce is
    single-use and would make an otherwise-identical retry look like a
    conflict, and ``source`` is provenance, not part of "is this logically
    the same command".
    """
    import hashlib

    return hashlib.sha256(canonical_json({
        "action": request.action,
        "account_id": request.account_id,
        "target_type": request.target_type,
        "target_id": request.target_id,
        "expected_version": request.expected_version,
        "body": request.body,
    })).hexdigest()


@dataclass(frozen=True)
class LedgerRow:
    """One durable ``command_ledger`` row."""

    command_id: str
    request_hash: str
    account_id: Optional[str]
    action: str
    target_type: str
    target_id: str
    expected_version: Optional[int]
    state: str
    outcome: Optional[dict[str, Any]]
    error_code: Optional[str]
    source: str
    created_at: dt.datetime
    updated_at: dt.datetime


@dataclass(frozen=True)
class LedgerClaim:
    kind: Literal["new", "replay", "conflict"]
    receipt: Optional[CommandReceipt]


class PreflightNonceGate(Protocol):
    """Consumes a preflight nonce inside the caller's transaction.

    Issuing nonces is ``[M1-C]``'s job; this coordinator only verifies and
    atomically consumes one. Returns ``True`` iff ``nonce`` was present,
    unexpired, and not already consumed (and is now consumed). Production
    wiring passes ``[M1-C]``'s real implementation; this module's own tests
    use fakes.
    """

    def consume_in_tx(self, conn: Any, nonce: Optional[str], request: CommandRequest) -> bool: ...


_LEDGER_COLUMNS = (
    "command_id", "request_hash", "account_id", "action", "target_type",
    "target_id", "expected_version", "state", "outcome", "error_code",
    "source", "created_at", "updated_at",
)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _row_to_ledger_row(row: tuple) -> LedgerRow:
    data = dict(zip(_LEDGER_COLUMNS, row))
    outcome = json.loads(data["outcome"]) if data["outcome"] is not None else None
    return LedgerRow(
        command_id=data["command_id"],
        request_hash=data["request_hash"],
        account_id=data["account_id"],
        action=data["action"],
        target_type=data["target_type"],
        target_id=data["target_id"],
        expected_version=data["expected_version"],
        state=data["state"],
        outcome=outcome,
        error_code=data["error_code"],
        source=data["source"],
        created_at=_as_utc(data["created_at"]),
        updated_at=_as_utc(data["updated_at"]),
    )


def _row_to_receipt(row: LedgerRow) -> CommandReceipt:
    return CommandReceipt(
        command_id=row.command_id,
        correlation_id=row.command_id,
        state=row.state,
        outcome=row.outcome,
        error_code=row.error_code,
        retryable=row.state not in _TERMINAL_STATES,
    )


def _conflict_receipt(command_id: str) -> CommandReceipt:
    return CommandReceipt(
        command_id=command_id,
        correlation_id=command_id,
        state="REJECTED",
        outcome=None,
        error_code="COMMAND_CONFLICT",
        retryable=False,
    )


class CommandLedger:
    """Persistence adapter for ``command_ledger`` in the journal file."""

    _SELECT = f"SELECT {', '.join(_LEDGER_COLUMNS)} FROM command_ledger"

    def __init__(self, journal: DomainJournal):
        self._journal = journal

    # -- claim / replay --------------------------------------------------

    def claim_or_replay_in_tx(self, conn: Any, request: CommandRequest) -> LedgerClaim:
        """Pure read-only lookup by ``command_id`` -- no journal event, no
        write. Safe to call on a bare autocommitted statement (the "pure
        ledger claim with no journal event" case the pre-flight resolution
        allows to stand alone outside ``mutate()``)."""
        row = self._select(conn, request.command_id)
        if row is None:
            return LedgerClaim("new", None)
        if row.request_hash == canonical_request_hash(request):
            return LedgerClaim("replay", _row_to_receipt(row))
        return LedgerClaim("conflict", _conflict_receipt(request.command_id))

    # -- mutating (caller supplies conn inside mutate()'s own transaction) --

    def insert_received_in_tx(
        self, conn: Any, request: CommandRequest, request_hash: str, now: dt.datetime,
    ) -> LedgerRow:
        row = conn.execute(
            """
            INSERT INTO command_ledger (
                command_id, request_hash, account_id, action, target_type,
                target_id, expected_version, state, outcome, error_code,
                source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'RECEIVED', NULL, NULL, ?, ?, ?)
            RETURNING """ + ", ".join(_LEDGER_COLUMNS),
            [
                request.command_id, request_hash, request.account_id, request.action,
                request.target_type, request.target_id, request.expected_version,
                request.source, now, now,
            ],
        ).fetchone()
        assert row is not None
        return _row_to_ledger_row(row)

    def transition_in_tx(
        self,
        conn: Any,
        command_id: str,
        from_state: str,
        to_state: str,
        *,
        outcome: Optional[dict[str, Any]] = None,
        error_code: Optional[str] = None,
        now: Optional[dt.datetime] = None,
    ) -> LedgerRow:
        now = now if now is not None else _utcnow()
        row = conn.execute(
            """
            UPDATE command_ledger
               SET state = ?, outcome = ?, error_code = ?, updated_at = ?
             WHERE command_id = ? AND state = ?
         RETURNING """ + ", ".join(_LEDGER_COLUMNS),
            [to_state, outcome, error_code, now, command_id, from_state],
        ).fetchone()
        if row is None:
            raise IllegalCommandTransition(
                f"cannot transition command {command_id!r} from {from_state!r} to "
                f"{to_state!r}: current state does not match {from_state!r}"
            )
        return _row_to_ledger_row(row)

    # -- reads -------------------------------------------------------------

    def get(self, command_id: str) -> Optional[LedgerRow]:
        return self._select(self._journal.connect(), command_id)

    def unresolved_for_target(self, target_type: str, target_id: str) -> list[LedgerRow]:
        rows = self._journal.connect().execute(
            f"{self._SELECT} WHERE target_type = ? AND target_id = ? "
            "AND state NOT IN ('RESOLVED', 'REJECTED') ORDER BY created_at",
            [target_type, target_id],
        ).fetchall()
        return [_row_to_ledger_row(row) for row in rows]

    def purge_expired(self, now: dt.datetime) -> int:
        """Delete terminal (``RESOLVED``/``REJECTED``) rows older than the
        30-day retention floor. ``OUTCOME_UNKNOWN`` (and every other
        non-terminal in-flight state) is NEVER purged -- an ambiguous
        outcome must stay visible until it is actually reconciled."""
        cutoff = _as_utc(now) - RETENTION
        conn = self._journal.connect()
        conn.execute("BEGIN TRANSACTION")
        try:
            deleted = conn.execute(
                "DELETE FROM command_ledger WHERE state IN ('RESOLVED', 'REJECTED') "
                "AND updated_at < ? RETURNING command_id",
                [cutoff],
            ).fetchall()
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except BaseException:
                pass
            raise
        return len(deleted)

    # -- test support --------------------------------------------------------

    def insert_for_test(
        self,
        command_id: str,
        *,
        state: str,
        updated_at: dt.datetime,
        created_at: Optional[dt.datetime] = None,
        request_hash: str = "test-hash",
        account_id: Optional[str] = "DU111111",
        action: str = "noop",
        target_type: str = "proposal",
        target_id: str = "1",
        expected_version: Optional[int] = None,
        source: str = "test",
    ) -> LedgerRow:
        conn = self._journal.connect()
        row = conn.execute(
            """
            INSERT INTO command_ledger (
                command_id, request_hash, account_id, action, target_type,
                target_id, expected_version, state, outcome, error_code,
                source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
            RETURNING """ + ", ".join(_LEDGER_COLUMNS),
            [
                command_id, request_hash, account_id, action, target_type,
                target_id, expected_version, state, source,
                _as_utc(created_at) if created_at is not None else _as_utc(updated_at),
                _as_utc(updated_at),
            ],
        ).fetchone()
        assert row is not None
        return _row_to_ledger_row(row)

    def _select(self, conn: Any, command_id: str) -> Optional[LedgerRow]:
        row = conn.execute(
            f"{self._SELECT} WHERE command_id = ?", [command_id]
        ).fetchone()
        return _row_to_ledger_row(row) if row is not None else None


class CommandAudit:
    """Persistence adapter for the mandatory ``command_audit`` record.

    ``record_in_tx`` is called from inside the RECEIVED transition's
    ``write_materialized`` callback (see module docstring): raising here
    rolls back the ledger insert and the RECEIVED journal event together,
    which is exactly the "failure to persist either fails the command
    closed" contract.
    """

    def __init__(self, journal: DomainJournal):
        self._journal = journal

    def record_in_tx(
        self,
        conn: Any,
        request: CommandRequest,
        *,
        correlation_id: Optional[str] = None,
        validation_result: Optional[str] = None,
        acknowledgement: Optional[str] = None,
        outcome: Optional[str] = None,
        now: Optional[dt.datetime] = None,
    ) -> None:
        now = now if now is not None else _utcnow()
        conn.execute(
            """
            INSERT INTO command_audit (
                command_id, correlation_id, action, target_type, target_id,
                expected_version, redacted_inputs, validation_result,
                acknowledgement, outcome, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                request.command_id, correlation_id or request.command_id, request.action,
                request.target_type, request.target_id, request.expected_version,
                request.body, validation_result, acknowledgement, outcome, now,
            ],
        )


ActionHandler = Callable[[CommandRequest], dict[str, Any]]


@dataclass(frozen=True)
class _ActionRegistration:
    handler: ActionHandler
    requires_preflight: bool


class TradingCommandCoordinator:
    """The sole production mutation boundary.

    ``execute()`` claims the ledger + audit record before any validation or
    side effect, dispatches to the per-action handler registered via
    ``register_action``, and drives the ledger to a terminal state. Handlers
    that own a multi-step saga (approval, cancel, strategy-forward -- later
    tasks) call ``transition`` themselves and return their own final
    outcome; the terminal fallback here (``RECEIVED`` -> ``RESOLVED``/
    ``REJECTED``) covers single-step actions.
    """

    def __init__(
        self,
        *,
        journal: DomainJournal,
        ledger: CommandLedger,
        audit: Any,
        nonces: PreflightNonceGate,
        now: Callable[[], dt.datetime] = _utcnow,
    ):
        self._journal = journal
        self._ledger = ledger
        self._audit = audit
        self._nonces = nonces
        self._now = now
        self._actions: dict[str, _ActionRegistration] = {}

    def register_action(
        self, action: str, handler: ActionHandler, *, requires_preflight: bool,
    ) -> None:
        self._actions[action] = _ActionRegistration(handler, requires_preflight)

    def execute(self, request: CommandRequest) -> CommandReceipt:
        if request.action not in self._actions:
            raise KeyError(f"no action handler registered for {request.action!r}")
        registration = self._actions[request.action]

        # Step 1 (binding order): exact-retry / conflict lookup PRECEDES
        # preflight-nonce validation. A read-only lookup -- no transaction,
        # no journal event.
        claim = self._ledger.claim_or_replay_in_tx(self._journal.connect(), request)
        if claim.kind != "new":
            assert claim.receipt is not None
            return claim.receipt

        # Steps 2-3: nonce validation, then the ledger row + mandatory audit
        # record, all inside ONE mutate() transaction so a failure anywhere
        # rolls back everything -- no ledger row is left behind on a nonce
        # failure or an audit-write failure (fail closed).
        request_hash = canonical_request_hash(request)
        received_at = self._as_utc(self._now())
        inserted: list[LedgerRow] = []

        def _write_received(conn: duckdb.DuckDBPyConnection, _revision: int) -> None:
            if registration.requires_preflight:
                if not self._nonces.consume_in_tx(conn, request.preflight_nonce, request):
                    raise CommandValidationError(
                        "PREFLIGHT_REQUIRED",
                        "missing, expired, or already-consumed preflight nonce",
                    )
            row = self._ledger.insert_received_in_tx(conn, request, request_hash, received_at)
            self._audit.record_in_tx(conn, request, correlation_id=request.command_id, now=received_at)
            inserted.append(row)

        received_mutation = DomainMutation(
            event_type="command.updated",
            entity_type="command",
            entity_id=command_entity_id(request.command_id),
            operation="upsert",
            account_id=request.account_id,
            source="trader_service",
            source_timestamp=received_at,
            correlation_id=request.command_id,
            payload={
                "state": "RECEIVED",
                "action": request.action,
                "target_type": request.target_type,
                "target_id": request.target_id,
            },
        )
        try:
            self._journal.mutate(
                self._journal.connect(),
                received_mutation,
                _write_received,
                event_id=f"command:{request.command_id}:received",
            )
        except CommandValidationError as exc:
            # No ledger row was kept -- the nonce failed before anything
            # committed. A retry with a fresh nonce can still succeed.
            return CommandReceipt(
                request.command_id, request.command_id, "REJECTED", None, exc.code, True
            )
        except Exception:
            # Fail closed: the ledger/audit write itself did not persist.
            # Nothing committed (the whole mutate() transaction rolled
            # back), so an identical retry of the same command_id is a
            # legitimate, safe next step once the audit sink recovers.
            return CommandReceipt(
                request.command_id, request.command_id, "REJECTED", None,
                "AUDIT_UNAVAILABLE", True,
            )

        # Step 4: the handler runs strictly AFTER RECEIVED has committed, so
        # `ledger.get(command_id)` is already visible to it (insert precedes
        # validation).
        try:
            outcome = registration.handler(request)
        except CommandValidationError as exc:
            row = self._transition(request, "RECEIVED", "REJECTED", error_code=exc.code)
            return _row_to_receipt(row)

        row = self._transition(request, "RECEIVED", "RESOLVED", outcome=outcome)
        return _row_to_receipt(row)

    def get_command(self, command_id: str) -> Optional[CommandReceipt]:
        row = self._ledger.get(command_id)
        return _row_to_receipt(row) if row is not None else None

    def _transition(
        self,
        request: CommandRequest,
        from_state: str,
        to_state: str,
        *,
        outcome: Optional[dict[str, Any]] = None,
        error_code: Optional[str] = None,
    ) -> LedgerRow:
        now = self._as_utc(self._now())
        transitioned: list[LedgerRow] = []

        def _write_transition(conn: duckdb.DuckDBPyConnection, _revision: int) -> None:
            transitioned.append(
                self._ledger.transition_in_tx(
                    conn, request.command_id, from_state, to_state,
                    outcome=outcome, error_code=error_code, now=now,
                )
            )

        mutation = DomainMutation(
            event_type="command.updated",
            entity_type="command",
            entity_id=command_entity_id(request.command_id),
            operation="upsert",
            account_id=request.account_id,
            source="trader_service",
            source_timestamp=now,
            correlation_id=request.command_id,
            payload={
                "state": to_state,
                "action": request.action,
                "target_type": request.target_type,
                "target_id": request.target_id,
                "outcome": outcome,
                "error_code": error_code,
            },
        )
        self._journal.mutate(
            self._journal.connect(),
            mutation,
            _write_transition,
            event_id=f"command:{request.command_id}:{to_state.lower()}",
        )
        return transitioned[0]

    @staticmethod
    def _as_utc(value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            raise ValueError("command timestamps must be timezone-aware")
        return value.astimezone(dt.timezone.utc)
