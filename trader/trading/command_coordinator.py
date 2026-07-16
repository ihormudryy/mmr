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
   return value as ``outcome``. Any OTHER exception (a bug, a DuckDB
   ``IOException`` from the handler's own ``mutate()`` call, etc.) is not
   swallowed: the command is transitioned ``RECEIVED`` -> ``OUTCOME_UNKNOWN``
   with ``error_code="INTERNAL_ERROR"`` (the same "ambiguous, reconcile
   later" state Task 5/6/7's multi-step sagas land on for a lost
   acknowledgement), and the original exception is then re-raised so the RPC
   layer still surfaces the failure loudly. Without this, the RECEIVED row
   would be left behind forever: a client retry with the same ``command_id``
   hits the exact-retry replay path (step 1) and gets back a
   ``state="RECEIVED", retryable=True`` receipt WITHOUT the handler ever
   running again -- a false promise, since replay never re-invokes a
   handler once a ledger row exists. ``CommandLedger.transition_in_tx`` is a
   plain compare-and-swap on the row's CURRENT state (``WHERE command_id = ?
   AND state = ?``); there is no separate legal-from/to-state adjacency
   table to consult or update here.

``command_id`` is colon-free by construction (``CommandRequest.__post_init__``
raises ``ValueError`` otherwise) because ``encode_order_ref`` builds
``mmr:og-{command_id}`` (`M1-F2]`'s ``trader/trading/order_correlation.py``)
-- a colon inside ``command_id`` would corrupt that encoding.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Literal, Optional, Protocol

import duckdb

from trader.data.domain_journal import DomainJournal
from trader.data.proposal_repository import (
    ApprovalClaim,
    ApprovalClaimOutcome,
    ProposalRecord,
    ProposalRepository,
)
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.commands import CommandReceipt
from trader.domain.events import DomainMutation
from trader.domain.identity import command_entity_id
from trader.messaging.typed_rpc import canonical_json
from trader.trading.order_correlation import encode_order_ref
from trader.trading.proposal_command_service import (
    ExecutableQuote,
    PositionAuthority,
    QuoteAuthority,
    _ConcurrentProposalChange,
)
from trader.trading.trading_control import PauseStateUnavailable, TradingPausedError

# [M1-F3] Task 5: the maximum source-clock skew (in seconds) the approval
# saga tolerates on a live executable quote before treating the quote as
# arriving "from the future" and rejecting it with ``SOURCE_CLOCK_SKEW``.
# Distinct from ``typed_rpc.DEFAULT_CLOCK_SKEW_SECONDS`` (an RPC-transport
# concern): this one is about market-data timestamp sanity, not request auth.
MAX_SOURCE_CLOCK_SKEW_SECONDS = 30.0

# [M1-F3] owns trader-DB (journal file) migration versions 20-29; Task 1
# used 20 for trade_proposals. This task owns 21. Task 4 owns 22
# (``trading_control_state`` -- see ``trader/trading/trading_control.py``),
# whose ``set_trading_pause`` action is registered on THIS coordinator via
# ``register_action`` from ``trader/messaging/production_api.py`` --
# exactly the same ``requires_preflight``/``execute()`` machinery every
# other action already uses, no coordinator-side code changes needed.
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


# ---------------------------------------------------------------------------
# [M1-F3] Task 5: pure approval guards + order-dispatch ports.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandProblem:
    """A structured validation failure carried out of the pure guards.

    ``retryable`` means "transient -- mint a fresh command and retry"; the
    approval saga builds the terminal receipt from this explicitly rather
    than letting ``_row_to_receipt`` derive retryability from terminality.
    """

    code: str
    retryable: bool
    detail: Optional[str] = None


def classify_risk_direction(
    action: str, held_quantity: float, order_quantity: float
) -> RiskDirection:
    """Whether an order reduces or increases market exposure.

    A SELL no larger than a long holding, or a BUY no larger than a short
    holding, REDUCES risk (a close/cover) and is exempt from the pause gate.
    Everything else INCREASES exposure. Broker-verified quantities only --
    never caller-supplied.
    """
    if action == "SELL" and held_quantity > 0 and order_quantity <= held_quantity:
        return RiskDirection.REDUCING
    if action == "BUY" and held_quantity < 0 and order_quantity <= -held_quantity:
        return RiskDirection.REDUCING           # covering a short reduces risk
    return RiskDirection.INCREASING


def check_exposure_increasing_guards(
    record: ProposalRecord,
    quote: Optional[ExecutableQuote],
    now: dt.datetime,
    account_mode: str,
    outside_session_limit_enabled: bool = False,
) -> Optional[CommandProblem]:
    """Pure pre-dispatch guards for an exposure-INCREASING approval.

    Returns the first violated guard as a ``CommandProblem``, or ``None`` when
    the row may be dispatched. Live mode additionally demands a fresh,
    live-feed, session-compatible executable-side quote; paper mode only
    enforces the recorded price-drift band.
    """
    expected_side = "ask" if record.action == "BUY" else "bid"
    if quote is None or quote.side != expected_side or not quote.price or quote.price <= 0:
        return CommandProblem("EXECUTABLE_QUOTE_MISSING", retryable=True)
    if account_mode == "live":
        if quote.feed_type != "live":
            return CommandProblem("FEED_NOT_LIVE", retryable=True)
        if quote.session_state != "continuous":
            order_type = (record.execution or {}).get("order_type", "MARKET")
            if order_type == "MARKET" or not outside_session_limit_enabled:
                return CommandProblem("SESSION_INCOMPATIBLE", retryable=True)
        age = (now - quote.market_timestamp).total_seconds()
        if age > 5.0:
            return CommandProblem("QUOTE_STALE", retryable=True)
        if age < -MAX_SOURCE_CLOCK_SKEW_SECONDS:
            return CommandProblem("SOURCE_CLOCK_SKEW", retryable=True)
    drift_bps = abs(quote.price - record.reference_price) / record.reference_price * 10_000.0
    if drift_bps > record.max_price_drift_bps:
        return CommandProblem(
            "PRICE_DRIFT_EXCEEDED", retryable=False,
            detail=f"{drift_bps:.1f} bps > {record.max_price_drift_bps:.1f} bps guard",
        )
    return None


@dataclass(frozen=True)
class SubmittedOrders:
    """Result of a successful ``OrderDispatchPort.submit``."""

    order_group_id: str
    order_ref: str            # == encode_order_ref(order_group_id) == "mmr:og-<cmd>"
    order_ids: list[int]      # IB orderId per placed leg (entry first)


@dataclass(frozen=True)
class CancelAck:
    order_entity_id: str
    cancelled: bool


class BrokerRejectedError(Exception):
    """A clean ``SuccessFail.fail`` with NO live order left behind.

    Distinct from an ambiguous dispatch (timeout/disconnect): a broker
    rejection is a definite negative outcome, so the proposal is marked
    ``FAILED`` and the command ``REJECTED`` -- never ``OUTCOME_UNKNOWN``.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class OrderDispatchPort(Protocol):
    """The real-order boundary. Implemented by ``trading_runtime``."""

    def submit(
        self, proposal: ProposalRecord, order_ref: str, order_group_id: str
    ) -> SubmittedOrders: ...

    def cancel(self, order_entity_id: str, order_ref: str) -> CancelAck: ...

    def find_by_order_ref(self, account_id: str, order_ref: str) -> list: ...

    def enumeration_complete(self) -> bool: ...


class ReconcilerPort(Protocol):
    """Schedules an ambiguous (``OUTCOME_UNKNOWN``) command for reconciliation.

    The real ``OutcomeReconciler`` is Task 9; Task 5 only defines the port.
    """

    def schedule(self, command_id: str, now: dt.datetime) -> None: ...


class BrokerHealthPort(Protocol):
    """Whether the broker is enumerated and ready to accept a dispatch.

    In production this maps to ``broker_ingest.is_ready`` (the promoted
    broker-generation fence).
    """

    def is_ready(self) -> bool: ...


class ApprovalClaimFailed(Exception):
    """The atomic ``claim_for_approval_in_tx`` did not return ``CLAIMED``.

    Raised INSIDE the claim transaction (rolling it back) for every
    non-``CLAIMED``/non-``EXPIRED`` outcome. ``EXPIRED`` is handled separately
    -- its flip must COMMIT -- so it never travels via this exception.
    """

    def __init__(self, outcome: ApprovalClaimOutcome):
        self.outcome = outcome
        super().__init__(str(outcome.result))

    def code(self) -> str:
        return _CLAIM_ERROR_CODES.get(self.outcome.result, "PROPOSAL_NOT_FOUND")


_CLAIM_ERROR_CODES: dict[ApprovalClaim, str] = {
    ApprovalClaim.EXPIRED: "PROPOSAL_EXPIRED",
    ApprovalClaim.REVISION_MISMATCH: "REVISION_MISMATCH",
    ApprovalClaim.NOT_PENDING: "NOT_PENDING",
    ApprovalClaim.NOT_FOUND: "PROPOSAL_NOT_FOUND",
    ApprovalClaim.WRONG_ACCOUNT: "WRONG_ACCOUNT",
}


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
    saga: bool = False


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
        saga: bool = False,
    ) -> None:
        """Register a per-action handler.

        ``saga=True`` marks a handler that owns its OWN multi-step ledger
        transitions (the approval/cancel/strategy-forward sagas): it drives
        the command from ``RECEIVED`` to a terminal-or-ambiguous state itself
        and returns its own ``CommandReceipt``. ``execute()`` therefore skips
        the single-step ``RECEIVED -> RESOLVED`` fallback for a saga action
        and returns the handler's receipt verbatim; its broad-exception
        fallback transitions from the command's CURRENT ledger state (not an
        assumed ``RECEIVED``) to ``OUTCOME_UNKNOWN``. Single-step actions
        (``saga=False``, the default) keep their existing
        ``RECEIVED -> RESOLVED/REJECTED`` behaviour unchanged.
        """
        self._actions[action] = _ActionRegistration(handler, requires_preflight, saga)

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
            if registration.saga:
                # A saga handler is contracted to build its own receipts and
                # never raise this; if it does (a bug), fail closed from the
                # command's CURRENT state rather than assuming RECEIVED.
                self._fallback_outcome_unknown(request)
                raise
            row = self._transition(request, "RECEIVED", "REJECTED", error_code=exc.code)
            return _row_to_receipt(row)
        except Exception:
            # Fail loud at the surface (the original exception is
            # re-raised below) but leave a defined, reconcilable ledger
            # state behind instead of a silently wedged RECEIVED -- a
            # handler can fail AFTER already committing a side effect of
            # its own (e.g. a proposal insert inside its own mutate()
            # call), so this outcome can be characterized as neither a
            # clean REJECTED nor a clean RESOLVED. OUTCOME_UNKNOWN is
            # exactly the "ambiguous, reconcile later" state for this.
            if registration.saga:
                # A saga may have already advanced past RECEIVED
                # (VALIDATED/SUBMITTING) before failing -- transition from
                # wherever it actually is, guarded, never from an assumed
                # RECEIVED (which would CAS-miss and wedge the command).
                self._fallback_outcome_unknown(request)
            else:
                try:
                    self._transition(
                        request, "RECEIVED", "OUTCOME_UNKNOWN", error_code="INTERNAL_ERROR",
                    )
                except Exception:
                    # The ledger write itself failed too (e.g. the DB is
                    # genuinely down). Do not let that mask the original
                    # exception -- the caller must still see what actually
                    # went wrong, not a secondary bookkeeping failure.
                    pass
            raise

        if registration.saga:
            # The saga drove its own transitions to a terminal-or-ambiguous
            # state and returns its own receipt; do NOT force RESOLVED.
            assert isinstance(outcome, CommandReceipt)
            return outcome

        row = self._transition(request, "RECEIVED", "RESOLVED", outcome=outcome)
        return _row_to_receipt(row)

    def get_command(self, command_id: str) -> Optional[CommandReceipt]:
        row = self._ledger.get(command_id)
        return _row_to_receipt(row) if row is not None else None

    def _fallback_outcome_unknown(self, request: CommandRequest) -> None:
        """Guarded CAS from the command's CURRENT state to OUTCOME_UNKNOWN.

        Used when a saga handler raises unexpectedly: it may have already
        advanced the command past RECEIVED, so transitioning from an assumed
        RECEIVED would CAS-miss and wedge the row. A terminal/already-unknown
        state is left untouched, and any secondary ledger-write failure is
        swallowed so it never masks the original exception being re-raised.
        """
        try:
            current = self._ledger.get(request.command_id)
            if current is None or current.state in _TERMINAL_STATES or current.state == "OUTCOME_UNKNOWN":
                return
            self._transition(
                request, current.state, "OUTCOME_UNKNOWN", error_code="INTERNAL_ERROR",
            )
        except Exception:
            pass

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

        self._journal.mutate(
            self._journal.connect(),
            _command_updated_mutation(request, to_state, now, outcome=outcome, error_code=error_code),
            _write_transition,
            event_id=f"command:{request.command_id}:{to_state.lower()}",
        )
        return transitioned[0]

    @staticmethod
    def _as_utc(value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            raise ValueError("command timestamps must be timezone-aware")
        return value.astimezone(dt.timezone.utc)


# ---------------------------------------------------------------------------
# [M1-F3] Task 5: shared command-event helpers + the approval saga.
# ---------------------------------------------------------------------------

def _command_updated_mutation(
    request: CommandRequest,
    to_state: str,
    now: dt.datetime,
    *,
    outcome: Optional[dict[str, Any]] = None,
    error_code: Optional[str] = None,
) -> DomainMutation:
    """Build the ``command.updated`` mutation for one ledger transition.

    Shared by the coordinator's single-step ``_transition`` and the approval
    saga's own transitions/appends so every ``command.updated`` event for a
    given command has an identical shape and stays on one revision stream.
    """
    return DomainMutation(
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


def _noop_write(conn: duckdb.DuckDBPyConnection, revision: int) -> None:
    """A no-op ``write_materialized`` for a journal append whose durable row
    was already written directly on the transaction's ``conn`` (the
    command-ledger row transitioned by ``transition_in_tx``). The append
    exists only to record the ``command.updated`` event, advance the command
    entity revision, and drive the long-poll commit signal."""
    return None


def _assert_proposal_revision(expected: int) -> Callable[[duckdb.DuckDBPyConnection, int], None]:
    """A ``write_materialized`` callback asserting the journal's computed
    ``next_revision`` equals the revision the repository write already
    produced -- keeping ``trade_proposals.revision`` and the journal
    ``entity_revision`` in lockstep (exactly one append per revision bump).
    It does NOT re-write ``trade_proposals`` (the claim/link/submit already
    wrote the row directly on ``conn``); it only guards the invariant."""
    def _write(conn: duckdb.DuckDBPyConnection, revision: int) -> None:
        if revision != expected:
            raise _ConcurrentProposalChange(
                f"journal revision {revision} diverged from proposal revision {expected}"
            )
    return _write


class ApprovalCommandService:
    """The approval saga -- the ONE command that dispatches real orders.

    Registered on the coordinator via ``register_action("approve_proposal",
    svc.approve, requires_preflight=True, saga=True)``. When ``execute()``
    invokes ``approve``, the command ledger is already at ``RECEIVED``;
    ``approve`` drives it the rest of the way and returns its own
    ``CommandReceipt``:

        RECEIVED -> REJECTED                              (a pre-dispatch guard failed)
        RECEIVED -> VALIDATED -> SUBMITTING -> SUBMITTED  (happy path)
        ...      -> SUBMITTING -> REJECTED                (clean broker rejection)
        ...      -> SUBMITTING -> OUTCOME_UNKNOWN         (ambiguous dispatch)

    The claim (``VALIDATED -> SUBMITTING``) is one ``mutate_batch_work``
    transaction that -- for an INCREASING order -- re-checks the pause gate in
    the SAME tx (spec §9.4/I1), atomically flips the proposal
    ``PENDING -> APPROVED``, links the order group (no revision bump), and
    journals both a ``proposal.updated`` and a ``command.updated`` event. Only
    THEN is the bracket dispatched. A clean rejection marks the proposal
    ``FAILED``; an ambiguous dispatch (timeout/disconnect) leaves it
    ``APPROVED`` for the Task-9 reconciler and NEVER auto-retries.
    """

    def __init__(
        self,
        *,
        journal: DomainJournal,
        ledger: CommandLedger,
        repo: ProposalRepository,
        controls: Any,
        orders: OrderDispatchPort,
        positions: PositionAuthority,
        quotes: QuoteAuthority,
        risk_gate: Any,
        risk_producer: Any,
        reconciler: ReconcilerPort,
        broker: BrokerHealthPort,
        account_id: str,
        account_mode: str,
        now: Callable[[], dt.datetime] = _utcnow,
        outside_session_limit_enabled: bool = False,
    ):
        self._journal = journal
        self._ledger = ledger
        self._repo = repo
        self._controls = controls
        self._orders = orders
        self._positions = positions
        self._quotes = quotes
        self._risk_gate = risk_gate
        self._risk_producer = risk_producer
        self._reconciler = reconciler
        self._broker = broker
        self._account_id = account_id
        self._account_mode = account_mode
        self._now = now
        self._outside_session_limit_enabled = outside_session_limit_enabled

    # -- public saga entry point (the registered action handler) ----------

    def approve(self, cmd: CommandRequest) -> CommandReceipt:
        proposal_id = int(cmd.body["proposal_id"])
        record = self._repo.get(proposal_id)

        problem, direction, decision = self._validate(record, cmd)

        # Write-once risk decision, recorded regardless of approve/reject, in
        # its OWN transaction, BEFORE the claim tx. Never re-published on a
        # legitimate retry (the ledger exact-retry replay path returns before
        # this handler -- and thus this call -- ever runs again).
        self._risk_producer.publish_decision(
            cmd.command_id, decision, correlation_id=cmd.command_id
        )

        if problem is not None:
            self._transition_command(cmd, "RECEIVED", "REJECTED", error_code=problem.code)
            return self._receipt(cmd.command_id, "REJECTED", problem.code, problem.retryable)

        self._transition_command(cmd, "RECEIVED", "VALIDATED")

        order_group_id = f"og-{cmd.command_id}"
        claimed: list[ProposalRecord] = []

        def work(conn, append):
            # §9.4/I1: re-check the pause gate in the SAME tx (INCREASING only)
            # so a pause that committed first is guaranteed to be observed and
            # serializes against final dispatch.
            if direction is RiskDirection.INCREASING:
                self._controls.require_unpaused_in_tx(conn, record.account_id)
            outcome = self._repo.claim_for_approval_in_tx(
                conn, record.id, cmd.expected_version, self._account_id, self._now_utc()
            )
            if outcome.result is ApprovalClaim.CLAIMED:
                self._repo.link_order_group_in_tx(conn, record.id, order_group_id)
                linked = replace(outcome.record, order_group_id=order_group_id)
                append(
                    self._repo.mutation_for(linked, cmd.command_id),
                    _assert_proposal_revision(linked.revision),
                    f"proposal:{record.id}:{linked.revision}",
                )
                self._ledger.transition_in_tx(conn, cmd.command_id, "VALIDATED", "SUBMITTING")
                append(
                    _command_updated_mutation(cmd, "SUBMITTING", self._now_utc()),
                    _noop_write,
                    f"command:{cmd.command_id}:submitting",
                )
                claimed.append(linked)
            elif outcome.result is ApprovalClaim.EXPIRED:
                # The claim UPDATE flipped PENDING -> EXPIRED (revision + 1);
                # that flip MUST commit (the row is genuinely expired now), so
                # journal it rather than rolling back the way the other
                # non-CLAIMED outcomes (which wrote nothing) do.
                append(
                    self._repo.mutation_for(outcome.record, cmd.command_id),
                    _assert_proposal_revision(outcome.record.revision),
                    f"proposal:{record.id}:{outcome.record.revision}",
                )
            else:
                raise ApprovalClaimFailed(outcome)      # rollback; nothing was written

        try:
            self._journal.mutate_batch_work(self._journal.connect(), work)
        except (TradingPausedError, PauseStateUnavailable):
            self._transition_command(cmd, "VALIDATED", "REJECTED", error_code="TRADING_PAUSED")
            return self._receipt(cmd.command_id, "REJECTED", "TRADING_PAUSED", True)
        except ApprovalClaimFailed as ex:
            code = ex.code()
            self._transition_command(cmd, "VALIDATED", "REJECTED", error_code=code)
            return self._receipt(cmd.command_id, "REJECTED", code, False)

        if not claimed:
            # EXPIRED: the flip committed above; the command ends REJECTED.
            self._transition_command(cmd, "VALIDATED", "REJECTED", error_code="PROPOSAL_EXPIRED")
            return self._receipt(cmd.command_id, "REJECTED", "PROPOSAL_EXPIRED", False)

        claimed_record = claimed[0]

        # --- Irreversible boundary: dispatch the bracket to the broker. ---
        try:
            submitted = self._orders.submit(
                proposal=self._repo.get(record.id),
                order_ref=encode_order_ref(order_group_id),
                order_group_id=order_group_id,
            )
        except BrokerRejectedError as ex:
            self._fail_after_broker_rejection(cmd, record.id, claimed_record.revision, str(ex))
            return self._receipt(cmd.command_id, "REJECTED", "BROKER_REJECTED", False)
        except Exception:
            # Timeout / disconnect / lost ack. NEVER auto-retry an ambiguous
            # real-money dispatch (retryable=False): the proposal stays
            # APPROVED and the Task-9 reconciler resolves the true outcome.
            self._transition_command(
                cmd, "SUBMITTING", "OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS"
            )
            self._reconciler.schedule(cmd.command_id, self._now_utc())
            return self._receipt(cmd.command_id, "OUTCOME_UNKNOWN", "DISPATCH_AMBIGUOUS", False)

        # --- Finish: proposal APPROVED->EXECUTED, command SUBMITTING->SUBMITTED. ---
        outcome_payload = {"order_ids": submitted.order_ids, "order_group_id": order_group_id}

        def finish(conn, append):
            row = self._repo.mark_order_submitted_in_tx(
                conn, record.id, submitted.order_ids, claimed_record.revision, self._now_utc()
            )
            if row is None:
                raise _ConcurrentProposalChange("proposal changed before submit-link")
            append(
                self._repo.mutation_for(row, cmd.command_id),
                _assert_proposal_revision(row.revision),
                f"proposal:{record.id}:{row.revision}",
            )
            self._ledger.transition_in_tx(
                conn, cmd.command_id, "SUBMITTING", "SUBMITTED", outcome=outcome_payload
            )
            append(
                _command_updated_mutation(cmd, "SUBMITTED", self._now_utc(), outcome=outcome_payload),
                _noop_write,
                f"command:{cmd.command_id}:submitted",
            )

        self._journal.mutate_batch_work(self._journal.connect(), finish)
        return self._receipt(cmd.command_id, "SUBMITTED", None, False, outcome=outcome_payload)

    # -- validation (pure guards + broker-verified collaborators) ---------

    def _validate(
        self, record: Optional[ProposalRecord], cmd: CommandRequest
    ) -> tuple[Optional[CommandProblem], Optional[RiskDirection], dict[str, Any]]:
        def reject(code: str, retryable: bool):
            return (
                CommandProblem(code, retryable),
                None,
                {"decision": "reject", "code": code, "proposal_id": int(cmd.body["proposal_id"])},
            )

        if record is None:
            return reject("PROPOSAL_NOT_FOUND", False)
        if record.account_id != self._account_id:
            return reject("WRONG_ACCOUNT", False)
        if self._account_mode == "live" and not record.live_approval_eligible:
            return reject("LIVE_INELIGIBLE", False)
        if record.status != "PENDING":
            return reject("NOT_PENDING", False)
        if cmd.expected_version is not None and record.revision != cmd.expected_version:
            return reject("REVISION_MISMATCH", False)
        inflight = [
            r for r in self._ledger.unresolved_for_target("proposal", str(record.id))
            if r.command_id != cmd.command_id
        ]
        if inflight:
            return reject("COMMAND_IN_FLIGHT", True)
        if not self._broker.is_ready():
            return reject("BROKER_UNAVAILABLE", True)
        if not self._evaluate_risk(record).approved:
            return reject("RISK_REJECTED", False)

        held = float(self._positions.reducible_quantity(self._account_id, record.conid))
        qty = float(record.quantity or 0.0)
        direction = classify_risk_direction(record.action, held, qty)
        # Explicit reducible cap (correction #3): a SELL that overshoots the
        # held long is an exposure-increasing short in disguise -- reject it
        # BEFORE the exposure guards. classify_risk_direction alone can't
        # produce this code.
        if record.action == "SELL" and held > 0 and qty > held:
            problem, _, decision = reject("REDUCIBLE_QUANTITY_EXCEEDED", False)
            return problem, direction, decision
        if direction is RiskDirection.REDUCING:
            # Pause-exempt close/cover; a stale feed is tolerated (no guards).
            return None, direction, {
                "decision": "approve", "direction": "REDUCING", "proposal_id": record.id,
            }
        side = "ask" if record.action == "BUY" else "bid"
        quote = self._quotes.executable_quote(record.conid, side=side)
        problem = check_exposure_increasing_guards(
            record, quote, self._now_utc(), self._account_mode, self._outside_session_limit_enabled,
        )
        if problem is not None:
            return problem, direction, {
                "decision": "reject", "code": problem.code, "proposal_id": record.id,
            }
        return None, direction, {
            "decision": "approve", "direction": "INCREASING", "proposal_id": record.id,
        }

    def _evaluate_risk(self, record: ProposalRecord):
        from trader.objects import Action
        from trader.trading.strategy import Signal

        signal = Signal(
            source_name=f"approval:{record.source}",
            action=Action.BUY if record.action == "BUY" else Action.SELL,
            probability=1.0,
            risk=0.0,
            conid=int(record.conid or 0),
        )
        return self._risk_gate.evaluate(signal=signal)

    # -- finalizers -------------------------------------------------------

    def _fail_after_broker_rejection(
        self, cmd: CommandRequest, proposal_id: int, expected_revision: int, reason: str
    ) -> None:
        def work(conn, append):
            row = self._repo.mark_failed_in_tx(
                conn, proposal_id, reason, expected_revision, self._now_utc()
            )
            if row is None:
                raise _ConcurrentProposalChange("proposal changed before fail-mark")
            append(
                self._repo.mutation_for(row, cmd.command_id),
                _assert_proposal_revision(row.revision),
                f"proposal:{proposal_id}:{row.revision}",
            )
            self._ledger.transition_in_tx(
                conn, cmd.command_id, "SUBMITTING", "REJECTED", error_code="BROKER_REJECTED"
            )
            append(
                _command_updated_mutation(cmd, "REJECTED", self._now_utc(), error_code="BROKER_REJECTED"),
                _noop_write,
                f"command:{cmd.command_id}:rejected",
            )

        self._journal.mutate_batch_work(self._journal.connect(), work)

    # -- command-ledger single-step transition ---------------------------

    def _transition_command(
        self,
        cmd: CommandRequest,
        from_state: str,
        to_state: str,
        *,
        outcome: Optional[dict[str, Any]] = None,
        error_code: Optional[str] = None,
    ) -> None:
        now = self._now_utc()

        def _write(conn: duckdb.DuckDBPyConnection, _revision: int) -> None:
            self._ledger.transition_in_tx(
                conn, cmd.command_id, from_state, to_state,
                outcome=outcome, error_code=error_code, now=now,
            )

        self._journal.mutate(
            self._journal.connect(),
            _command_updated_mutation(cmd, to_state, now, outcome=outcome, error_code=error_code),
            _write,
            event_id=f"command:{cmd.command_id}:{to_state.lower()}",
        )

    @staticmethod
    def _receipt(
        command_id: str, state: str, error_code: Optional[str], retryable: bool,
        *, outcome: Optional[dict[str, Any]] = None,
    ) -> CommandReceipt:
        return CommandReceipt(
            command_id=command_id, correlation_id=command_id, state=state,
            outcome=outcome, error_code=error_code, retryable=retryable,
        )

    def _now_utc(self) -> dt.datetime:
        return _as_utc(self._now())
