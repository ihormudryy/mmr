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
import itertools
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Literal, Optional, Protocol

import duckdb

from trader.data.broker_state import BrokerOrderRow
from trader.data.domain_journal import DomainJournal, EventIdentityConflict
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
from trader.messaging.typed_rpc import TypedRpcRemoteError, canonical_json
from trader.strategy.strategy_revisions import StrategyCommandReceipt
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

# [M1-F3] Task 9 (spec §9.5, verbatim): the reconciliation attempt schedule for
# an ``OUTCOME_UNKNOWN`` command -- fire immediately, then every 5 s for the
# first minute, then every 30 s for the next 14 minutes. The cumulative sum is
# exactly the 15-minute critical-alert boundary; after the table is exhausted a
# command is NEVER converted to failure by time alone -- it stays registered at
# the periodic 30-second session-reconciliation cadence and keeps blocking
# same-target commands (``COMMAND_IN_FLIGHT``) until it is actually resolved.
RECONCILE_DELAYS: tuple[float, ...] = (0.0,) + (5.0,) * 12 + (30.0,) * 28
CRITICAL_AFTER_SECONDS = 900.0

# Cumulative offset (seconds from ``started``) of each scheduled attempt:
# _RECONCILE_OFFSETS[i] == sum(RECONCILE_DELAYS[: i + 1]).
_RECONCILE_OFFSETS: tuple[float, ...] = tuple(itertools.accumulate(RECONCILE_DELAYS))

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

    Implemented by Task 9's ``OutcomeReconciler``. The sagas (approval/cancel/
    strategy) call ``schedule`` on their inline ``OUTCOME_UNKNOWN`` branch;
    ``TradingCommandCoordinator`` calls it on the generic saga-exception
    fallback so EVERY wedge is registered for reconciliation (RJ2 addendum §2).
    """

    def schedule(self, command_id: str, now: dt.datetime) -> None: ...


@dataclass(frozen=True)
class ReconcileResult:
    """One reconciliation attempt's outcome (Task 9).

    ``resolved`` is True once the command reaches a terminal ``RESOLVED`` (or is
    already terminal/gone). ``critical`` is True once an unresolved command has
    passed the 15-minute boundary and a ``CriticalAlertPort`` alert has fired --
    an unresolved command is NEVER converted to failure by time alone.
    """

    command_id: str
    resolved: bool
    critical: bool


class CriticalAlertPort(Protocol):
    """Escalation seam for a command still unresolved after
    ``CRITICAL_AFTER_SECONDS`` (consumed by ``[M1-R]`` health rendering)."""

    def raise_alert(self, command_id: str, detail: str) -> None: ...


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

    ``parent_command_id`` [F3 Task 6]: set only on the per-order CHILD
    commands ``CancelCommandService.cancel_orders`` fans a root
    ``cancel_orders`` command out into (``f"{root}-{index}"`` -- colon-free,
    per Task 6 addendum §4). It is deliberately NOT folded into
    ``command_id`` itself (each child still needs its own unique ledger
    row/primary key); ``correlation_id`` below is the derived value every
    ``command.updated`` journal event and audit record actually uses, so a
    child's own events correlate back to the root that dispatched it rather
    than to the child's own (otherwise-unrelated) command_id.
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
    parent_command_id: Optional[str] = None

    def __post_init__(self) -> None:
        if ":" in self.command_id:
            raise ValueError(
                f"command_id must not contain ':' (encode_order_ref reserves it "
                f"for the mmr: orderRef prefix): {self.command_id!r}"
            )

    @property
    def correlation_id(self) -> str:
        """The correlation carried on this request's journal/audit events.

        Defaults to the command's own ``command_id`` (self-correlated, the
        behaviour every existing action already had) unless
        ``parent_command_id`` is set.
        """
        return self.parent_command_id or self.command_id


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
        # OUTCOME_UNKNOWN is non-terminal but must NEVER report retryable -- an
        # ambiguous real-money dispatch is never auto-retried (the saga builds
        # its own receipt with retryable=False; a later replay / get_command
        # read must match rather than derive True from non-terminality).
        retryable=row.state not in _TERMINAL_STATES and row.state != "OUTCOME_UNKNOWN",
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

    def reconcilable(self) -> list[LedgerRow]:
        """Every in-flight command a coordinator restart must requeue for
        reconciliation (Task 9 ``rescan_on_startup``, spec §9.5).

        A row is reconcilable iff it has committed a dispatch-or-ambiguity
        transition -- ``SUBMITTING`` (claimed, dispatch may or may not have
        reached the broker: the classic crash-between-claim-and-ack window) or
        ``OUTCOME_UNKNOWN`` (a persisted ambiguous outcome). ``RECEIVED``/
        ``VALIDATED`` never dispatched, and the terminal states are done."""
        rows = self._journal.connect().execute(
            f"{self._SELECT} WHERE state IN ('SUBMITTING', 'OUTCOME_UNKNOWN') "
            "ORDER BY created_at",
        ).fetchall()
        return [_row_to_ledger_row(row) for row in rows]

    def pre_dispatch_orphans(self) -> list[LedgerRow]:
        """Every ``VALIDATED`` row a coordinator restart must recover
        ([M1-F3] Task 9 MEDIUM-4 crash recovery).

        A hard crash between an approve/cancel/strategy saga's
        ``RECEIVED -> VALIDATED`` commit and its claim/dispatch transaction
        orphans a ``VALIDATED`` row that ``reconcilable()`` never requeues
        (it only covers ``SUBMITTING``/``OUTCOME_UNKNOWN``). Such a row would
        block its target forever via ``unresolved_for_target`` ->
        ``COMMAND_IN_FLIGHT``. Every saga transitions ``VALIDATED ->
        SUBMITTING`` ATOMICALLY with its first durable side effect (the
        proposal claim, the in-tx nonce consume, or the SUBMITTING commit that
        precedes any dispatch), so a row still at ``VALIDATED`` provably
        dispatched no order and claimed no proposal -- making it safe to
        terminalize on startup. ``RECEIVED`` is deliberately EXCLUDED: a
        non-saga handler (create/reject/pause) may commit its side effect while
        the ledger row is still ``RECEIVED`` (the ``RECEIVED -> RESOLVED``
        fallback runs after the handler), so blindly terminalizing it would
        falsely fail a mutation that actually committed."""
        rows = self._journal.connect().execute(
            f"{self._SELECT} WHERE state = 'VALIDATED' ORDER BY created_at",
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
        outcome: Optional[dict[str, Any]] = None,
    ) -> LedgerRow:
        conn = self._journal.connect()
        row = conn.execute(
            """
            INSERT INTO command_ledger (
                command_id, request_hash, account_id, action, target_type,
                target_id, expected_version, state, outcome, error_code,
                source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            RETURNING """ + ", ".join(_LEDGER_COLUMNS),
            [
                command_id, request_hash, account_id, action, target_type,
                target_id, expected_version, state, outcome, source,
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
        reconciler: Optional[ReconcilerPort] = None,
    ):
        self._journal = journal
        self._ledger = ledger
        self._audit = audit
        self._nonces = nonces
        self._now = now
        # [M1-F3] Task 9 (RJ2 addendum §2): optional reconciliation hook. When
        # set, EVERY command the coordinator parks at OUTCOME_UNKNOWN via its
        # own fallback (a non-saga handler bug, or a saga that raised before
        # reaching its own inline DISPATCH_AMBIGUOUS schedule) is scheduled for
        # reconciliation here -- so a wedge is revisited in-process, not only
        # after a restart's rescan_on_startup. Default None/no-op keeps every
        # existing coordinator test (built without a reconciler) unchanged.
        self._reconciler = reconciler
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
            self._audit.record_in_tx(conn, request, correlation_id=request.correlation_id, now=received_at)
            inserted.append(row)

        received_mutation = DomainMutation(
            event_type="command.updated",
            entity_type="command",
            entity_id=command_entity_id(request.command_id),
            operation="upsert",
            account_id=request.account_id,
            source="trader_service",
            source_timestamp=received_at,
            correlation_id=request.correlation_id,
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
        except EventIdentityConflict:
            # Lost a concurrent claim race with a DIFFERING source_timestamp:
            # another caller journaled RECEIVED for this command_id between
            # our lock-free lookup above and mutate()'s locked idempotency
            # check, and the timestamp mismatch made the replay guard raise
            # instead of silently matching. Our transaction rolled back --
            # the WINNER owns the dispatch. Return its recorded receipt.
            return self._losing_claim_receipt(request)
        except Exception:
            # Fail closed: the ledger/audit write itself did not persist.
            # Nothing committed (the whole mutate() transaction rolled
            # back), so an identical retry of the same command_id is a
            # legitimate, safe next step once the audit sink recovers.
            return CommandReceipt(
                request.command_id, request.command_id, "REJECTED", None,
                "AUDIT_UNAVAILABLE", True,
            )

        if not inserted:
            # Lost the same race with an IDENTICAL mutation (equal
            # source_timestamps): mutate()'s locked ``_select_journal_row``
            # check found the winner's committed ``command:<id>:received``
            # event, treated our stable event_id as an idempotent replay,
            # and SKIPPED ``_write_received`` entirely. We inserted nothing
            # -- the winner owns the dispatch, and running the handler here
            # anyway would execute the action twice for one command_id
            # (double proposal mutation; after Task 5, a double ORDER
            # submission). Return the winner's recorded receipt instead.
            return self._losing_claim_receipt(request)

        # Step 4: the handler runs strictly AFTER RECEIVED has committed, so
        # `ledger.get(command_id)` is already visible to it (insert precedes
        # validation). Reaching here also means THIS caller inserted the
        # RECEIVED row (``inserted`` is non-empty) -- exclusive dispatch
        # ownership is what makes the handler run at most once per
        # command_id even under concurrent duplicate submission.
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
                parked = False
                try:
                    self._transition(
                        request, "RECEIVED", "OUTCOME_UNKNOWN", error_code="INTERNAL_ERROR",
                    )
                    parked = True
                except Exception:
                    # The ledger write itself failed too (e.g. the DB is
                    # genuinely down). Do not let that mask the original
                    # exception -- the caller must still see what actually
                    # went wrong, not a secondary bookkeeping failure.
                    pass
                if parked:
                    # RJ2 §2: schedule the wedge so it is reconciled in-process,
                    # not only after a restart.
                    self._schedule_reconcile(request.command_id)
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

    def _losing_claim_receipt(self, request: CommandRequest) -> CommandReceipt:
        """Receipt for the LOSER of a concurrent duplicate-command_id race.

        Called only after mutate() proved another caller already journaled
        RECEIVED for this command_id (either by silently replaying our stable
        event_id or by raising ``EventIdentityConflict``). The winner's
        transaction committed the ledger row atomically with that event, so
        re-running the claim must find it: ``replay`` for an identical
        request (return the recorded receipt -- possibly still non-terminal
        ``RECEIVED`` if the winner is mid-dispatch), ``conflict`` for a
        same-id-different-hash duplicate.
        """
        claim = self._ledger.claim_or_replay_in_tx(self._journal.connect(), request)
        if claim.kind == "new" or claim.receipt is None:
            # The journal event exists without its ledger row -- impossible
            # unless journal/ledger atomicity was violated. Fail loud.
            raise RuntimeError(
                f"command {request.command_id!r}: journal RECEIVED event exists "
                "without a command_ledger row -- journal/ledger atomicity violated"
            )
        return claim.receipt

    def _fallback_outcome_unknown(self, request: CommandRequest) -> None:
        """Guarded CAS from the command's CURRENT state to OUTCOME_UNKNOWN.

        Used when a saga handler raises unexpectedly: it may have already
        advanced the command past RECEIVED, so transitioning from an assumed
        RECEIVED would CAS-miss and wedge the row. A terminal/already-unknown
        state is left untouched, and any secondary ledger-write failure is
        swallowed so it never masks the original exception being re-raised.
        """
        parked = False
        try:
            current = self._ledger.get(request.command_id)
            if current is None or current.state in _TERMINAL_STATES or current.state == "OUTCOME_UNKNOWN":
                return
            self._transition(
                request, current.state, "OUTCOME_UNKNOWN", error_code="INTERNAL_ERROR",
            )
            parked = True
        except Exception:
            pass
        if parked:
            # RJ2 §2: a fallback-parked wedge must be scheduled for
            # reconciliation just like the sagas' own inline OUTCOME_UNKNOWN
            # branch does -- otherwise it would rely on a restart's
            # rescan_on_startup to ever be revisited.
            self._schedule_reconcile(request.command_id)

    def _schedule_reconcile(self, command_id: str) -> None:
        """Register ``command_id`` for reconciliation via the optional hook.

        No-op (and swallow any hook error) when no reconciler is wired or the
        hook itself raises: scheduling is a best-effort convenience on top of
        the durable ledger row, which ``rescan_on_startup`` will always
        requeue regardless -- a hook failure must never mask the original
        exception being re-raised by the caller."""
        if self._reconciler is None:
            return
        try:
            self._reconciler.schedule(command_id, self._as_utc(self._now()))
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
        correlation_id=request.correlation_id,
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
                # Dispatch the in-hand claimed_record (the linked record already
                # carrying order_group_id), NOT a redundant re-read: a re-read
                # here would be strictly before any order is sent, yet a failure
                # of it would be misclassified as an ambiguous dispatch that
                # never happened -- and it adds a needless round-trip + race
                # inside the irreversible-dispatch window.
                proposal=claimed_record,
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

        try:
            self._journal.mutate_batch_work(self._journal.connect(), finish)
        except Exception:
            # The orders are already LIVE at the broker (submit returned), so a
            # failure of the post-dispatch finish tx (a _ConcurrentProposalChange
            # from mark_order_submitted_in_tx returning None, or a DuckDB
            # IOException) must NOT discard the in-hand order_ids or drop the
            # command into a generic INTERNAL_ERROR with no reconciliation.
            # Mirror the dispatch-timeout path, persisting the known outcome so
            # the Task-9 reconciler can resolve the live order. The proposal
            # stays APPROVED (the claim committed); never auto-retry ambiguous
            # real money (retryable=False).
            self._transition_command(
                cmd, "SUBMITTING", "OUTCOME_UNKNOWN",
                error_code="DISPATCH_AMBIGUOUS", outcome=outcome_payload,
            )
            self._reconciler.schedule(cmd.command_id, self._now_utc())
            return self._receipt(
                cmd.command_id, "OUTCOME_UNKNOWN", "DISPATCH_AMBIGUOUS", False,
                outcome=outcome_payload,
            )
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

        # Guard order pins §9.2: "The proposal exists, is PENDING, and matches
        # expected_version" is the primary identity check, evaluated BEFORE the
        # account/mode guards (WRONG_ACCOUNT is otherwise re-verified in the
        # claim-tx compare-and-set). These are independent early returns.
        if record is None:
            return reject("PROPOSAL_NOT_FOUND", False)
        if record.status != "PENDING":
            return reject("NOT_PENDING", False)
        if cmd.expected_version is not None and record.revision != cmd.expected_version:
            return reject("REVISION_MISMATCH", False)
        if record.account_id != self._account_id:
            return reject("WRONG_ACCOUNT", False)
        if self._account_mode == "live" and not record.live_approval_eligible:
            return reject("LIVE_INELIGIBLE", False)
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


# ---------------------------------------------------------------------------
# [M1-F3] Task 6: working-order cancel authority.
#
# Consumes [M1-F2]'s REAL materialized broker-order store
# (``trader/data/broker_state.py``) -- ``OrderStateView`` wraps
# ``BrokerStateStore.get_order_in_tx`` behind a conn-free read seam and
# returns the real ``BrokerOrderRow``, never a parallel dataclass (Task 6
# addendum §1).
# ---------------------------------------------------------------------------

class OrderStateView(Protocol):
    """Read seam onto [M1-F2]'s materialized ``broker_orders`` store.

    ``get_order`` is deliberately conn-free -- unlike
    ``BrokerStateStore.get_order_in_tx``, which requires an open connection
    -- so a production adapter can wrap that method later (T8/T9 wiring)
    while tests supply a trivial fake backed by real ``BrokerOrderRow``
    instances.
    """

    def get_order(self, order_entity_id: str) -> Optional[BrokerOrderRow]: ...


# Mirrors ``book.py:42`` (``BookSubject._ACTIVE_STATUSES``) -- kept
# textually identical. That frozenset is the single source of truth for
# "still working" IB order statuses; anything else (Filled, Cancelled,
# ApiCancelled, Inactive) is terminal. There is no ``is_terminal`` field on
# ``BrokerOrderRow`` (Task 6 addendum §3): terminality is derived here from
# ``status``/``deleted`` rather than importing book.py's private class
# attribute.
_ACTIVE_ORDER_STATUSES = frozenset({
    "PendingSubmit", "ApiPending", "PreSubmitted", "Submitted", "PendingCancel",
})


def _is_terminal_order(order: BrokerOrderRow) -> bool:
    return order.deleted or order.status not in _ACTIVE_ORDER_STATUSES


def classify_cancel(order: Optional[BrokerOrderRow]) -> RiskDirection:
    """Whether cancelling ``order`` reduces or increases market exposure.

    [Task 6 addendum §2]: ``order_correlation.classify_leg`` is the ONLY
    producer of ``leg``; its values are ``"entry"`` (no parent), ``"stop"``,
    ``"take_profit"``, ``f"child-{client_order_id}"``, or ``None``
    (external / no group). Cancelling an entry removes PENDING exposure
    (REDUCING). Cancelling a protective leg strips protection from an
    already-open position (INCREASING). A ``None`` row, a ``None`` leg, or
    any non-entry leg is treated as protective -- fail safe toward requiring
    the ceremony, never toward a silent unprotected cancel.
    """
    if order is not None and order.leg == "entry":
        return RiskDirection.REDUCING
    return RiskDirection.INCREASING


class CancelCommandService:
    """The cancel saga -- working-order cancel authority.

    Registered on the coordinator via
    ``register_action("cancel_order", svc.cancel_order,
    requires_preflight=False, saga=True)`` and ``register_action(
    "cancel_orders", svc.cancel_orders, requires_preflight=False)``.

    Unlike ``ApprovalCommandService``, the preflight-nonce requirement here
    is NOT static: it is derived from ``classify_cancel``'s risk direction,
    which can only be evaluated once the target order's row is loaded. The
    coordinator's own top-level nonce gate is therefore deliberately
    bypassed (``requires_preflight=False``) for both actions and
    re-implemented inside ``cancel_order`` itself, consuming the nonce
    guarded inside the SAME claiming transaction as the VALIDATED ->
    SUBMITTING transition -- mirroring ``ApprovalCommandService.approve``'s
    in-tx pause re-check for an INCREASING approval.

    ``cancel_order`` drives its own ledger transitions (mirrors the
    CURRENT, post-fix ``ApprovalCommandService.approve`` -- commit
    86ef4cc -- for its dispatch + post-dispatch handling):

        RECEIVED -> REJECTED                             (missing order: ORDER_NOT_FOUND)
        RECEIVED -> RESOLVED                              (terminal order: no-op)
        RECEIVED -> VALIDATED -> REJECTED                 (ceremony required, nonce missing/invalid)
        RECEIVED -> VALIDATED -> SUBMITTING -> SUBMITTED  (happy path)
        ...      -> SUBMITTING -> OUTCOME_UNKNOWN         (ambiguous dispatch / post-dispatch tx failure)

    An ambiguous dispatch (broker cancel timeout/disconnect) or a failure of
    the post-dispatch finish tx (the cancel is already live at the broker)
    both degrade to ``OUTCOME_UNKNOWN``/``DISPATCH_AMBIGUOUS``,
    ``retryable=False``, and call ``self._reconciler.schedule(...)`` -- never
    a raw exception or a generic ``INTERNAL_ERROR`` with nothing scheduled.

    ``cancel_orders`` is NOT a saga: it dedupes the list (first-seen order),
    classifies every target, and mints one colon-free, deterministic child
    ``command_id`` per order (``f"{root}-{index}"`` over the DEDUPED sequence
    -- Task 6 addendum §4: the base brief's ``f"{root}:{order_entity_id}"``
    scheme is a colon-in-command_id invariant violation caught by
    ``CommandRequest.__post_init__``/``encode_order_ref``), carries the
    ``order_entity_id`` in the child's ``target_id``/``body`` (never in the
    command_id), and executes each child through the SAME coordinator
    (``coordinator.execute(child)``) so every child gets its own ledger row,
    audit record, and the full ``cancel_order`` saga treatment. Every child's
    ``parent_command_id`` is the root's ``command_id``, so its
    ``command.updated`` journal events all carry the root as ``correlation_id``
    (addendum §4: "child correlation_id = the root command_id").

    The preflight ceremony authorizes the BATCH once (§9.7: "cancel-all
    expands under ONE correlation id and ONE confirmation"; §9.1 binds the
    nonce to a single command). If ANY deduped target classifies INCREASING,
    the root consumes ``cmd.preflight_nonce`` EXACTLY once, transactionally,
    BEFORE fanning out -- a failed consume rejects the WHOLE batch
    (``PREFLIGHT_REQUIRED``, nothing fanned out) rather than dispatching a
    partial set. The per-child ``cancel_order`` sees ``parent_command_id`` set
    and skips its own (single-use) consume. Each child's receipt is captured
    into the root outcome so per-leg truth is never masked; the outcome is
    ``{"child_command_ids": [...], "children": {order_entity_id: {"command_id",
    "state", "error_code", "classification"}}, "partial_failure": <bool>}``,
    where ``partial_failure`` is true if any child ended non-SUBMITTED (or its
    ``execute`` raised, which is isolated per child so a later order still
    dispatches). Returning a plain dict lets the coordinator's existing
    single-step ``RECEIVED -> RESOLVED`` fallback finish the root command.
    """

    def __init__(
        self,
        *,
        journal: DomainJournal,
        ledger: CommandLedger,
        orders_view: OrderStateView,
        dispatch: OrderDispatchPort,
        nonces: PreflightNonceGate,
        risk_producer: Any,
        reconciler: ReconcilerPort,
        coordinator: TradingCommandCoordinator,
        now: Callable[[], dt.datetime] = _utcnow,
    ):
        self._journal = journal
        self._ledger = ledger
        self._orders_view = orders_view
        self._dispatch = dispatch
        self._nonces = nonces
        self._risk_producer = risk_producer
        self._reconciler = reconciler
        self._coordinator = coordinator
        self._now = now

    # -- public saga entry point: single-order cancel ----------------------

    def cancel_order(self, cmd: CommandRequest) -> CommandReceipt:
        order_entity_id = cmd.body["order_entity_id"]
        order = self._orders_view.get_order(order_entity_id)

        if order is None:
            # Never blind-cancel: a missing row is unclassifiable, so this
            # is a definite rejection, not an ambiguous outcome.
            self._transition_command(cmd, "RECEIVED", "REJECTED", error_code="ORDER_NOT_FOUND")
            return self._receipt(cmd.command_id, "REJECTED", "ORDER_NOT_FOUND", True)

        if _is_terminal_order(order):
            outcome = {"noop": True, "authoritative_status": order.status}
            self._transition_command(cmd, "RECEIVED", "RESOLVED", outcome=outcome)
            return self._receipt(cmd.command_id, "RESOLVED", None, False, outcome=outcome)

        direction = classify_cancel(order)
        # Write-once risk decision for this cancel's classification, mirroring
        # ApprovalCommandService.approve's publish_decision call: a bare method
        # call, not itself journaled, recorded before the claiming transaction.
        # The risk_id (first positional) stays the per-command command_id so a
        # fan-out sibling never collides on it; the correlation is
        # ``cmd.correlation_id`` so a CHILD's decision correlates to the ROOT
        # cancel_orders command, not to the child's own id (Fix 5).
        self._risk_producer.publish_decision(
            cmd.command_id,
            {"decision": "cancel", "direction": direction.value, "order_entity_id": order_entity_id},
            correlation_id=cmd.correlation_id,
        )

        self._transition_command(cmd, "RECEIVED", "VALIDATED")

        def work(conn: duckdb.DuckDBPyConnection, append) -> None:
            # The preflight ceremony is derived from classification (Task 6
            # addendum): only an INCREASING (protective-leg or unclassifiable)
            # cancel needs the nonce, consumed inside this SAME transaction as
            # the VALIDATED -> SUBMITTING transition so a failed ceremony
            # never leaves SUBMITTING committed. A fan-out CHILD
            # (``parent_command_id`` set) is exempt: the root
            # ``cancel_orders`` already performed the ONE ceremony for the
            # whole batch (Fix 1), and the nonce is single-use -- so only a
            # STANDALONE cancel (``parent_command_id is None``, which every
            # wire-level ``cancel_order`` is) consumes its own nonce here.
            if direction is RiskDirection.INCREASING and cmd.parent_command_id is None:
                if not self._nonces.consume_in_tx(conn, cmd.preflight_nonce, cmd):
                    raise CommandValidationError(
                        "PREFLIGHT_REQUIRED",
                        "missing, expired, or already-consumed preflight nonce",
                    )
            self._ledger.transition_in_tx(conn, cmd.command_id, "VALIDATED", "SUBMITTING")
            append(
                _command_updated_mutation(cmd, "SUBMITTING", self._now_utc()),
                _noop_write,
                f"command:{cmd.command_id}:submitting",
            )

        try:
            self._journal.mutate_batch_work(self._journal.connect(), work)
        except CommandValidationError as exc:
            self._transition_command(cmd, "VALIDATED", "REJECTED", error_code=exc.code)
            return self._receipt(cmd.command_id, "REJECTED", exc.code, True)

        # --- Irreversible boundary: dispatch the cancel to the broker. ---
        try:
            self._dispatch.cancel(order_entity_id, order_ref=cmd.command_id)
        except Exception:
            # Timeout / disconnect / lost ack. NEVER auto-retry an ambiguous
            # real-money dispatch (retryable=False): the Task-9 reconciler
            # resolves the true outcome by re-reading the order's
            # authoritative status from [M1-F2]'s broker_orders store.
            self._transition_command(
                cmd, "SUBMITTING", "OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS",
            )
            self._reconciler.schedule(cmd.command_id, self._now_utc())
            return self._receipt(cmd.command_id, "OUTCOME_UNKNOWN", "DISPATCH_AMBIGUOUS", False)

        # --- Finish: command SUBMITTING -> SUBMITTED. ---
        outcome: dict[str, Any] = {"order_entity_id": order_entity_id}
        if direction is RiskDirection.INCREASING:
            # Names the position the cancel leaves unprotected so the
            # confirmation surface can call it out explicitly.
            outcome["unprotected_conid"] = order.conid

        def finish(conn: duckdb.DuckDBPyConnection, append) -> None:
            self._ledger.transition_in_tx(
                conn, cmd.command_id, "SUBMITTING", "SUBMITTED", outcome=outcome,
            )
            append(
                _command_updated_mutation(cmd, "SUBMITTED", self._now_utc(), outcome=outcome),
                _noop_write,
                f"command:{cmd.command_id}:submitted",
            )

        try:
            self._journal.mutate_batch_work(self._journal.connect(), finish)
        except Exception:
            # The cancel is already LIVE at the broker (dispatch returned), so
            # a failure of the post-dispatch finish tx must NOT discard that
            # fact or drop the command into a generic INTERNAL_ERROR with no
            # reconciliation -- mirrors approve's guarded ``finish`` (fix2,
            # FINISH-TX-AFTER-DISPATCH).
            self._transition_command(
                cmd, "SUBMITTING", "OUTCOME_UNKNOWN",
                error_code="DISPATCH_AMBIGUOUS", outcome=outcome,
            )
            self._reconciler.schedule(cmd.command_id, self._now_utc())
            return self._receipt(
                cmd.command_id, "OUTCOME_UNKNOWN", "DISPATCH_AMBIGUOUS", False, outcome=outcome,
            )

        return self._receipt(cmd.command_id, "SUBMITTED", None, False, outcome=outcome)

    # -- public non-saga entry point: bulk cancel expansion -----------------

    def cancel_orders(self, cmd: CommandRequest) -> dict[str, Any]:
        order_entity_ids = cmd.body.get("order_entity_ids")
        if not order_entity_ids:
            raise CommandValidationError(
                "ORDER_ENTITY_IDS_REQUIRED", "order_entity_ids must be a non-empty list",
            )

        # Fix 4: dedupe (preserving first-seen order) BEFORE classification and
        # fan-out -- a repeated id must never mint two child commands or two
        # broker cancel dispatches for the same order. The child index keys off
        # this deduped sequence.
        order_entity_ids = list(dict.fromkeys(order_entity_ids))

        # Classify every (deduped) target up front. A batch needs the preflight
        # ceremony iff ANY target classifies INCREASING (protective / missing /
        # unclassifiable -- see ``classify_cancel``).
        classifications: dict[str, str] = {
            order_entity_id: classify_cancel(self._orders_view.get_order(order_entity_id)).value
            for order_entity_id in order_entity_ids
        }
        batch_needs_ceremony = any(
            value == RiskDirection.INCREASING.value for value in classifications.values()
        )

        # Fix 1: the ceremony authorizes the WHOLE batch ONCE. Consume the
        # root's single-use nonce EXACTLY once, in its OWN journal transaction
        # (the same ``mutate_batch_work`` mechanism ``cancel_order`` uses for
        # its per-command consume), so a failed ceremony rolls the consume back
        # and NOTHING is fanned out. On failure REJECT the whole batch: the
        # raised ``CommandValidationError`` drives the root (a non-saga command)
        # to REJECTED/PREFLIGHT_REQUIRED via ``execute()``'s handler-raise path.
        # The per-child ``cancel_order`` then SKIPS its own consume (it sees
        # ``parent_command_id`` set), so the one nonce covers every child.
        if batch_needs_ceremony:
            def _consume_root_nonce(conn: duckdb.DuckDBPyConnection, _append) -> None:
                if not self._nonces.consume_in_tx(conn, cmd.preflight_nonce, cmd):
                    raise CommandValidationError(
                        "PREFLIGHT_REQUIRED",
                        "missing, expired, or already-consumed preflight nonce",
                    )
            self._journal.mutate_batch_work(self._journal.connect(), _consume_root_nonce)

        child_ids: list[str] = []
        children: dict[str, dict[str, Any]] = {}
        partial_failure = False
        for index, order_entity_id in enumerate(order_entity_ids):
            child_id = f"{cmd.command_id}-{index}"
            child_ids.append(child_id)
            child = CommandRequest(
                command_id=child_id,
                action="cancel_order",
                account_id=cmd.account_id,
                target_type="order",
                target_id=order_entity_id,
                expected_version=None,
                body={"order_entity_id": order_entity_id},
                source=cmd.source,
                preflight_nonce=cmd.preflight_nonce,
                parent_command_id=cmd.command_id,
            )
            classification = classifications[order_entity_id]
            # Fix 3: isolate each child. An exception out of one child's
            # ``execute`` must NOT abort the batch (dropping every not-yet-reached
            # order with no record) -- record it as FAILED and continue.
            try:
                receipt = self._coordinator.execute(child)
            except Exception:  # deliberately broad: per-child fault isolation
                children[order_entity_id] = {
                    "command_id": child_id,
                    "state": "FAILED",
                    "error_code": "CHILD_EXECUTE_FAILED",
                    "classification": classification,
                }
                partial_failure = True
                continue
            # Fix 2: capture each child's real receipt so the root outcome
            # reflects per-child truth (no masked partial failure). A child that
            # ends anything other than SUBMITTED flips ``partial_failure``.
            children[order_entity_id] = {
                "command_id": child_id,
                "state": receipt.state,
                "error_code": receipt.error_code,
                "classification": classification,
            }
            if receipt.state != "SUBMITTED":
                partial_failure = True

        return {
            "child_command_ids": child_ids,
            "children": children,
            "partial_failure": partial_failure,
        }

    # -- command-ledger single-step transition (mirrors ApprovalCommandService) --

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


# ---------------------------------------------------------------------------
# [M1-F3] Task 7: strategy-control forwarding -- enable_strategy /
# disable_strategy / update_strategy_params.
#
# STRICTLY one-way (trader -> strategy_service): ``StrategyControlPort.forward``
# is the only call the trader ever makes toward strategy_service on this path,
# and the reply from that ONE call already carries strategy_service's own
# committed-or-rolled-back outcome. There is deliberately no callback FROM
# strategy_service back into the trader anywhere on this path (see CLAUDE.md
# memory: dashboard-strategy-controls -- a real deadlock already hit once when
# a strategy_service RPC handler tried to call back into trader_service while
# still handling an inbound request).
# ---------------------------------------------------------------------------

class StrategySnapshotPort(Protocol):
    """Read seam onto the trader's own view of which strategies exist --
    consulted BEFORE a strategy-control command is ever forwarded."""

    def exists(self, strategy_name: str) -> bool: ...


class StrategyExposureOwnershipPort(Protocol):
    """Whether disabling ``strategy_name`` leaves every position it manages
    with a resolved alternate owner for exits (spec §9.3: never silently
    orphan a protective order).

    [M1-F2]'s full ownership-tracking machinery
    (``QuoteSubscriptionManager.set_owner_refs`` and friends) is not yet
    available -- see the m1f3 briefing's F2 hard-block. This port is
    OPTIONAL on ``StrategyControlCommandService``; when not supplied the
    guard is skipped entirely (STUB/DEFER, mirroring Task 2/7's documented
    allowance for the same ``set_owner_refs`` wiring). When supplied, a
    disable that would leave an unresolved owner is rejected outright
    rather than silently proceeding.
    """

    def remaining_owner_resolved(self, strategy_name: str) -> bool: ...


class StrategyControlPort(Protocol):
    """The one-way trader -> strategy_service forwarding boundary.

    ``forward`` sends a validated strategy-control ``CommandRequest`` to
    strategy_service and returns ITS ``StrategyCommandReceipt`` once
    strategy_service's own local transaction has committed (or rolled back)
    -- ``StrategyRuntime.apply_control_command`` on the receiving end is
    fully self-contained and never calls back into the trader while handling
    it. ``get_receipt`` is a pure read, used by reconciliation (Task 9) to
    recover the true outcome of a command whose ``forward()`` call timed out
    (this coordinator degrades that case to ``OUTCOME_UNKNOWN``, never a
    silent/false ``RESOLVED``).
    """

    def forward(self, request: CommandRequest) -> StrategyCommandReceipt: ...

    def get_receipt(self, command_id: str) -> Optional[StrategyCommandReceipt]: ...


class _StrategyRevisionDrift(Exception):
    """The journal's freshly computed ``entity_revision`` for a ``strategy``
    entity diverged from the ``state_revision`` strategy_service reported for
    this command. Would only fire if some OTHER writer journaled a
    ``strategy.updated`` event for this entity outside
    ``StrategyControlCommandService.acknowledge_state`` -- a producer bug,
    never expected in normal operation (mirrors ``_ConcurrentProposalChange``'s
    role for ``_assert_proposal_revision``)."""


def _assert_state_revision(expected: int) -> Callable[[duckdb.DuckDBPyConnection, int], None]:
    """A ``write_materialized`` callback asserting the journal's freshly
    computed ``entity_revision`` for entity_type ``"strategy"`` equals the
    ``state_revision`` strategy_service reported for this command.

    Per the m1f3 briefing's B3 correction: this does NOT (cannot) force
    ``entity_revision = state_revision`` -- ``DomainMutation`` has no such
    field and ``DomainJournal.mutate`` always computes the next revision
    itself. Equality holds only because both counters start fresh at 0 and
    advance in lockstep, one bump per acknowledged strategy-control command;
    this assertion is the guard that would catch it drifting, not the
    mechanism that makes it hold.
    """
    def _write(conn: duckdb.DuckDBPyConnection, revision: int) -> None:
        if revision != expected:
            raise _StrategyRevisionDrift(
                f"journal revision {revision} diverged from strategy "
                f"state_revision {expected}"
            )
    return _write


def acknowledge_strategy_state(
    journal: DomainJournal,
    strategy_name: str,
    state_revision: int,
    control_revision: int,
    payload: dict[str, Any],
    *,
    correlation_id: str,
    account_id: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> int:
    """Journal ``strategy.updated`` for this ``state_revision`` and return the
    journaled ``entity_revision``.

    Extracted from ``StrategyControlCommandService.acknowledge_state`` so the
    production trader can accept strategy_service's state announcements
    (``record_state_acknowledged``) on its typed command socket WITHOUT
    constructing the full strategy-control forwarding saga — the ack only
    needs the journal (see ``production_api.register_strategy_state_ingest``).

    Idempotent by construction: ``event_id`` is deterministic
    (``f"strategy:{name}:state:{state_revision}"``), so a duplicate
    acknowledgement for a revision already journaled with an IDENTICAL
    payload is a no-op replay inside ``DomainJournal.mutate`` itself. A
    duplicate whose payload matches but whose ``source_timestamp`` differs
    (the ordinary case for a genuinely later retry) raises
    ``EventIdentityConflict`` from ``mutate()`` — caught here and treated as
    "already durably acknowledged", returning the entity's current revision
    rather than propagating a spurious failure for what is, from the
    caller's perspective, still success."""
    mutation = DomainMutation(
        event_type="strategy.updated",
        entity_type="strategy",
        entity_id=strategy_name,
        operation="upsert",
        account_id=account_id,
        source="trader_service",
        source_timestamp=now if now is not None else _utcnow(),
        correlation_id=correlation_id,
        payload=payload,
    )
    try:
        event = journal.mutate(
            journal.connect(),
            mutation,
            _assert_state_revision(state_revision),
            event_id=f"strategy:{strategy_name}:state:{state_revision}",
        )
        return event.entity_revision
    except EventIdentityConflict:
        entity = journal.get_entity("strategy", strategy_name)
        if entity is not None:
            return entity["entity_revision"]
        raise


class StrategyControlCommandService:
    """[M1-F3] Task 7 -- the forwarding saga for ``enable_strategy``,
    ``disable_strategy``, and ``update_strategy_params``.

    Registered on the coordinator via ``register_action("enable_strategy",
    svc.enable_strategy, requires_preflight=False, saga=True)`` (and the
    disable/update_strategy_params siblings). Drives its own ledger
    transitions:

        RECEIVED -> REJECTED                              (unknown strategy, or
                                                             an exposure-owning
                                                             disable with no
                                                             resolved owner)
        RECEIVED -> VALIDATED -> SUBMITTING -> RESOLVED    (happy path -- BOTH
                                                             a strategy-side
                                                             COMMITTED and a
                                                             ROLLED_BACK resolve
                                                             here; a rejected
                                                             params update is a
                                                             clean, definite
                                                             outcome, not an
                                                             ambiguous one)
        ...      -> SUBMITTING -> OUTCOME_UNKNOWN          (forward() itself
                                                             timed out/raised --
                                                             Task 9's reconciler
                                                             resolves the truth
                                                             via
                                                             StrategyControlPort
                                                             .get_receipt)

    The trader journals a ``strategy.updated`` domain event ONLY after
    ``forward()`` returns -- i.e. only once strategy_service has actually
    acknowledged the command (never speculatively before dispatch, never on
    a bare timeout).
    """

    def __init__(
        self,
        *,
        journal: DomainJournal,
        ledger: CommandLedger,
        port: StrategyControlPort,
        snapshot: StrategySnapshotPort,
        reconciler: ReconcilerPort,
        ownership: Optional[StrategyExposureOwnershipPort] = None,
        now: Callable[[], dt.datetime] = _utcnow,
    ):
        self._journal = journal
        self._ledger = ledger
        self._port = port
        self._snapshot = snapshot
        self._reconciler = reconciler
        self._ownership = ownership
        self._now = now

    # -- public saga entry points (the registered action handlers) --------

    def enable_strategy(self, cmd: CommandRequest) -> CommandReceipt:
        return self._forward(cmd)

    def disable_strategy(self, cmd: CommandRequest) -> CommandReceipt:
        return self._forward(cmd)

    def update_strategy_params(self, cmd: CommandRequest) -> CommandReceipt:
        return self._forward(cmd)

    # -- shared acknowledgement (also called directly by the trader's typed
    #    record_state_acknowledged command -- see production_api.py) --------

    def acknowledge_state(
        self,
        strategy_name: str,
        state_revision: int,
        control_revision: int,
        payload: dict[str, Any],
        *,
        correlation_id: str,
        account_id: Optional[str] = None,
    ) -> int:
        """Journal ``strategy.updated`` for this ``state_revision`` and
        return the journaled ``entity_revision``.

        Idempotent by construction: ``event_id`` is deterministic
        (``f"strategy:{name}:state:{state_revision}"``), so a duplicate
        acknowledgement for a revision already journaled with an IDENTICAL
        payload is a no-op replay inside ``DomainJournal.mutate`` itself. A
        duplicate whose payload matches but whose ``source_timestamp``
        differs (the ordinary case for a genuinely later retry) raises
        ``EventIdentityConflict`` from ``mutate()`` -- caught here and
        treated as "already durably acknowledged", returning the entity's
        current revision rather than propagating a spurious failure for
        what is, from the caller's perspective, still success.
        """
        return acknowledge_strategy_state(
            self._journal, strategy_name, state_revision, control_revision,
            payload, correlation_id=correlation_id, account_id=account_id,
            now=self._now_utc(),
        )

    # -- internals ----------------------------------------------------------

    def _forward(self, cmd: CommandRequest) -> CommandReceipt:
        strategy_name = cmd.body.get("strategy_name")
        if not strategy_name or not self._snapshot.exists(strategy_name):
            self._transition_command(cmd, "RECEIVED", "REJECTED", error_code="STRATEGY_NOT_FOUND")
            return self._receipt(cmd.command_id, "REJECTED", "STRATEGY_NOT_FOUND", False)

        if cmd.action == "disable_strategy" and self._ownership is not None:
            if not self._ownership.remaining_owner_resolved(strategy_name):
                self._transition_command(
                    cmd, "RECEIVED", "REJECTED", error_code="EXPOSURE_OWNERSHIP_UNRESOLVED",
                )
                return self._receipt(
                    cmd.command_id, "REJECTED", "EXPOSURE_OWNERSHIP_UNRESOLVED", True,
                )

        self._transition_command(cmd, "RECEIVED", "VALIDATED")
        self._transition_command(cmd, "VALIDATED", "SUBMITTING")

        try:
            strategy_receipt = self._port.forward(cmd)
        except TypedRpcRemoteError as exc:
            # A DETERMINISTIC strategy-side rejection carrying a declared
            # ``.code`` (e.g. a stale-CAS CONTROL_REVISION_CONFLICT raised by
            # apply_control_command BEFORE any strategy-side mutation/receipt)
            # is a clean REJECT, NOT an ambiguous dispatch -- strategy_service's
            # true state is definitively "unchanged". Mirrors how the approval
            # saga routes BrokerRejectedError->REJECTED and cancel routes
            # CommandValidationError->REJECTED. retryable=True: the operator
            # re-issues with a fresh control_revision. No reconciler is
            # scheduled -- there is nothing ambiguous to reconcile.
            self._transition_command(
                cmd, "SUBMITTING", "REJECTED", error_code=exc.code,
            )
            return self._receipt(cmd.command_id, "REJECTED", exc.code, True)
        except Exception:
            # Timeout / disconnect / lost ack (no declared code). NEVER
            # auto-retry an ambiguous dispatch (retryable=False): Task 9's
            # reconciler resolves the true outcome via
            # StrategyControlPort.get_receipt(command_id). Ambiguity is
            # reserved STRICTLY for this case, where strategy_service's true
            # state genuinely cannot be inferred.
            self._transition_command(
                cmd, "SUBMITTING", "OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS",
            )
            self._reconciler.schedule(cmd.command_id, self._now_utc())
            return self._receipt(cmd.command_id, "OUTCOME_UNKNOWN", "DISPATCH_AMBIGUOUS", False)

        payload = {
            "strategy_name": strategy_receipt.strategy_name,
            "action": strategy_receipt.action,
            "strategy_state": strategy_receipt.state,
            "control_revision": strategy_receipt.control_revision,
            "state_revision": strategy_receipt.state_revision,
            "error": strategy_receipt.error,
        }
        # Only a strategy-side COMMITTED actually bumped state_revision --
        # journal strategy.updated (asserting entity_revision == state_revision)
        # ONLY in that case. A ROLLED_BACK outcome minted no new revision on
        # the strategy side (nothing about the strategy's observable state
        # changed), so there is no new strategy.updated event to journal for
        # it -- the command_ledger's own command.updated transition below
        # (carrying this same outcome payload, including the error) is
        # already the complete audit trail for a failed attempt.
        entity_revision = None
        if strategy_receipt.state == "COMMITTED":
            entity_revision = self.acknowledge_state(
                strategy_receipt.strategy_name,
                strategy_receipt.state_revision,
                strategy_receipt.control_revision,
                payload,
                correlation_id=cmd.correlation_id,
                account_id=cmd.account_id,
            )
        outcome = dict(payload, entity_revision=entity_revision)
        # Both a strategy-side COMMITTED and a ROLLED_BACK resolve the
        # COMMAND here -- a rejected params update (e.g. the replacement
        # strategy failed to instantiate) is a clean, definite outcome, not
        # an ambiguous one; the caller sees it via outcome["error"].
        self._transition_command(cmd, "SUBMITTING", "RESOLVED", outcome=outcome)
        return self._receipt(cmd.command_id, "RESOLVED", None, False, outcome=outcome)

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


# ---------------------------------------------------------------------------
# [M1-F3] Task 9: OUTCOME_UNKNOWN reconciliation.
#
# The reconciler is the ONLY component that resolves an ambiguous real-money
# dispatch. It NEVER re-submits or re-cancels anything: it reads the
# authoritative broker view ([M1-F2]'s materialized ``broker_orders`` via
# ``OrderDispatchPort.find_by_order_ref`` / ``enumeration_complete``) and
# strategy_service's committed receipt (``StrategyControlPort.get_receipt``),
# and either RESOLVES the command (recording submission evidence on the
# proposal) or -- only with a fenced, COMPLETE broker enumeration proving the
# order never reached the broker -- records a clean never-submitted failure.
# A command it cannot resolve is NEVER converted to failure by time alone;
# past the 15-minute boundary it raises a single critical operator alert and
# stays OUTCOME_UNKNOWN, continuing to block same-target commands.
# ---------------------------------------------------------------------------


@dataclass
class _ReconcilePlan:
    """In-memory schedule state for one command under reconciliation."""

    command_id: str
    started: dt.datetime
    next_due: dt.datetime
    attempt: int = 0
    alerted: bool = False


class OutcomeReconciler:
    """Resolves ``SUBMITTING``/``OUTCOME_UNKNOWN`` commands against authority.

    Ports (all fakeable; production wiring is the coordinated integration
    step, not T9-core):
    - ``orders``: ``OrderDispatchPort`` -- ``find_by_order_ref`` /
      ``enumeration_complete`` only (submit/cancel are NEVER called).
    - ``strategy``: ``StrategyControlPort`` -- ``get_receipt`` only.
    - ``alerts``: ``CriticalAlertPort`` -- raised once past 15 minutes.
    - ``repo``: ``ProposalRepository`` -- marks a proposal EXECUTED/FAILED as
      submission evidence, in the SAME journaled transaction as the command
      transition. Optional (a cancel target carries no proposal).
    - ``orders_view``: ``OrderStateView`` -- [M1-F3] MEDIUM-2: the authoritative
      read seam onto [M1-F2]'s materialized ``broker_orders`` store, used to
      resolve a ``cancel_order`` wedge by the TARGET order's real status. A
      ``cancel_order`` creates NO ``og-*`` group, so the order-ref lookup the
      approve path uses is always empty for it and must NEVER resolve a cancel.
      Optional: when unwired (dormant) the cancel branch stays OUTCOME_UNKNOWN
      rather than rubber-stamping RESOLVED (fail-safe).

    Command-type awareness ([M1-F3] Task 9 HIGH-1, verbatim): ``reconcile_once``
    discriminates by the command's ACTION, not by ``target_type`` alone --
    ``approve_proposal``, ``create_proposal`` and ``reject_proposal`` all stamp
    ``target_type="proposal"`` yet resolve by entirely different authority (the
    order-ref lookup, the created-proposal journal event, and the proposal's
    REJECTED status respectively). FAIL-SAFE throughout: an outcome that cannot
    be POSITIVELY confirmed leaves the command OUTCOME_UNKNOWN (-> the 15-minute
    critical alert); the reconciler never guesses, never blind-mutates a
    proposal, and never blind-cancels/resubmits.
    """

    def __init__(
        self,
        *,
        journal: DomainJournal,
        ledger: CommandLedger,
        orders: OrderDispatchPort,
        strategy: StrategyControlPort,
        alerts: CriticalAlertPort,
        repo: Optional[ProposalRepository] = None,
        orders_view: Optional[OrderStateView] = None,
        now: Callable[[], dt.datetime] = _utcnow,
    ):
        self._journal = journal
        self._ledger = ledger
        self._orders = orders
        self._strategy = strategy
        self._alerts = alerts
        self._repo = repo
        self._orders_view = orders_view
        self._now = now
        self._plans: dict[str, _ReconcilePlan] = {}

    # -- scheduling --------------------------------------------------------

    def schedule(self, command_id: str, now: dt.datetime) -> None:
        """Register (idempotently) a command for reconciliation.

        The first attempt is due immediately (``RECONCILE_DELAYS[0] == 0``). A
        re-schedule of an already-tracked command is a no-op -- it must not
        reset the clock and thereby postpone the 15-minute critical boundary.
        """
        if command_id in self._plans:
            return
        started = _as_utc(now)
        self._plans[command_id] = _ReconcilePlan(
            command_id=command_id, started=started, next_due=started,
        )

    def run_due(self, now: dt.datetime) -> list[ReconcileResult]:
        """Reconcile every command whose next attempt is due at ``now``.

        Walks ``RECONCILE_DELAYS`` cumulatively (attempts at 0 s, 5-60 s, then
        90-900 s). After the table is exhausted the plan stays registered at
        the periodic 30-second session-reconciliation cadence -- a command is
        never dropped or failed by time alone.
        """
        now = _as_utc(now)
        results: list[ReconcileResult] = []
        for command_id in list(self._plans.keys()):
            plan = self._plans.get(command_id)
            if plan is None or plan.next_due > now:
                continue
            results.append(self.reconcile_once(command_id, now))
            plan = self._plans.get(command_id)
            if plan is None:
                continue  # resolved (or gone) -- reconcile_once dropped it
            plan.attempt += 1
            if plan.attempt < len(RECONCILE_DELAYS):
                plan.next_due = plan.started + dt.timedelta(
                    seconds=_RECONCILE_OFFSETS[plan.attempt]
                )
            else:
                plan.next_due = now + dt.timedelta(seconds=30.0)
        return results

    def rescan_on_startup(self) -> list[str]:
        """Requeue every in-flight ledger row (coordinator crash recovery,
        spec §9.5). Covers the crash-between-claim-and-ack window a live
        ``schedule`` call could never have reached.

        [M1-F3] MEDIUM-4: ALSO recovers orphaned pre-dispatch ``VALIDATED``
        rows -- a hard crash between a saga's ``RECEIVED -> VALIDATED`` commit
        and its claim/dispatch tx leaves a row that ``reconcilable()`` never
        requeues and that would otherwise block its target forever
        (``COMMAND_IN_FLIGHT``). A ``VALIDATED``-but-unclaimed command
        dispatched no order and claimed no proposal, so it is safe to
        terminalize it (``REJECTED``/``CRASH_ORPHANED``) up front, unblocking
        the target. Each terminalization is isolated so one racing/failed row
        never aborts the whole rescan."""
        requeued: list[str] = []
        for row in self._ledger.reconcilable():
            self.schedule(row.command_id, self._now_utc())
            requeued.append(row.command_id)
        for row in self._ledger.pre_dispatch_orphans():
            if self._terminalize_pre_dispatch_orphan(row):
                requeued.append(row.command_id)
        return requeued

    # -- one reconciliation attempt ----------------------------------------

    def reconcile_once(self, command_id: str, now: dt.datetime) -> ReconcileResult:
        now = _as_utc(now)
        row = self._ledger.get(command_id)
        if row is None or row.state not in ("SUBMITTING", "OUTCOME_UNKNOWN"):
            # Already terminal (or gone) -- drop it and report resolved.
            self._plans.pop(command_id, None)
            return ReconcileResult(command_id, resolved=True, critical=False)

        # HIGH-1 / MEDIUM-2/3: discriminate by the command's ACTION, never by
        # target_type alone. approve/create/reject all stamp
        # target_type="proposal" but resolve by entirely different authority;
        # a cancel reads the target order, a pause the control row, a
        # strategy-control its forwarded receipt. FAIL-SAFE: a command whose
        # outcome cannot be POSITIVELY determined is left unresolved (never
        # rubber-stamped), and the block below escalates it after 15 minutes.
        if self._try_resolve(row, now):
            return ReconcileResult(command_id, True, False)

        # Unresolved: escalate to a critical alert once past the boundary, but
        # NEVER convert to failure by time alone.
        plan = self._plans.get(command_id)
        if plan is None:
            plan = _ReconcilePlan(command_id=command_id, started=now, next_due=now)
            self._plans[command_id] = plan
        if (now - plan.started).total_seconds() >= CRITICAL_AFTER_SECONDS and not plan.alerted:
            plan.alerted = True
            self._alerts.raise_alert(
                command_id,
                f"{row.action} unresolved after 15 minutes -- "
                f"operator reconciliation required",
            )
        return ReconcileResult(command_id, False, plan.alerted)

    # -- action-aware positive determination (HIGH-1 / MEDIUM-2/3) ---------

    def _try_resolve(self, row: LedgerRow, now: dt.datetime) -> bool:
        """Positively determine a command's true outcome from the authority
        appropriate to its ACTION and RESOLVE it, or return ``False`` to leave
        it OUTCOME_UNKNOWN. FAIL-SAFE: an outcome that cannot be POSITIVELY
        confirmed is never guessed -- the command stays unknown (and, past the
        boundary, becomes a critical operator alert)."""
        action = row.action
        if action == "approve_proposal":
            return self._reconcile_approve(row, now)
        if action == "create_proposal":
            return self._reconcile_create(row, now)
        if action == "reject_proposal":
            return self._reconcile_reject(row, now)
        if action == "cancel_order":
            return self._reconcile_cancel(row, now)
        if action == "cancel_orders":
            return self._reconcile_cancel_orders(row, now)
        if action == "set_trading_pause":
            return self._reconcile_pause(row, now)
        if action in ("enable_strategy", "disable_strategy", "update_strategy_params"):
            return self._reconcile_strategy(row, now)
        # Unmapped action: cannot positively determine an outcome -> stay
        # OUTCOME_UNKNOWN (fail-safe), never rubber-stamp RESOLVED.
        return False

    def _reconcile_approve(self, row: LedgerRow, now: dt.datetime) -> bool:
        """The approve saga is the ONE command that dispatches a real order,
        stamping ``order_ref = mmr:og-{command_id}``. Resolve via that lookup:
        a found order marks the proposal EXECUTED; only a fenced, COMPLETE
        broker enumeration proving absence records a clean never-submitted
        failure. This is the pre-existing (correct) approve reconciliation."""
        order_ref = encode_order_ref(f"og-{row.command_id}")
        found = self._orders.find_by_order_ref(row.account_id, order_ref)
        if found:
            self._resolve_order(row, found, now)
            return True
        if self._orders.enumeration_complete():
            self._resolve_never_submitted(row, now)
            return True
        return False

    def _reconcile_create(self, row: LedgerRow, now: dt.datetime) -> bool:
        """A wedged ``create_proposal`` never dispatched an order, so the
        order path is irrelevant. Resolve ONLY when the proposal was positively
        confirmed created (its ``proposal.*`` journal event correlated to this
        command exists); otherwise stay unknown -- NEVER mark anything
        FAILED."""
        proposal_id = self._created_proposal_id(row.command_id)
        if proposal_id is None:
            return False
        self._resolve_command_only(
            row, {"proposal_id": proposal_id, "created": True}, now
        )
        return True

    def _reconcile_reject(self, row: LedgerRow, now: dt.datetime) -> bool:
        """A wedged ``reject_proposal`` never dispatched an order. Resolve ONLY
        when the target proposal is positively confirmed REJECTED; otherwise
        stay unknown. Crucially NEVER run this through the order path (which
        would ``_resolve_never_submitted`` -> mark a concurrently-APPROVED
        proposal FAILED)."""
        if self._repo is None or not row.target_id:
            return False
        try:
            proposal_id = int(row.target_id)
        except (TypeError, ValueError):
            return False
        record = self._repo.get(proposal_id)
        if record is not None and record.status == "REJECTED":
            self._resolve_command_only(
                row, {"proposal_id": proposal_id, "status": "REJECTED"}, now
            )
            return True
        return False

    def _reconcile_cancel(self, row: LedgerRow, now: dt.datetime) -> bool:
        """MEDIUM-2: a ``cancel_order`` creates NO ``og-*`` group, so the
        order-ref lookup is always empty and must NEVER resolve it. Read the
        TARGET order's authoritative status from the injected
        ``OrderStateView``: RESOLVED only when the order is TERMINAL
        (Cancelled/ApiCancelled/Filled/Inactive/deleted); if still active (the
        cancel didn't take), the order is unreadable, or no view is wired
        (dormant), stay OUTCOME_UNKNOWN -- never rubber-stamp."""
        if self._orders_view is None:
            return False
        order = self._orders_view.get_order(row.target_id)
        if order is None:
            return False
        if _is_terminal_order(order):
            self._resolve_command_only(
                row,
                {"order_entity_id": row.target_id, "authoritative_status": order.status},
                now,
            )
            return True
        return False

    def _reconcile_cancel_orders(self, row: LedgerRow, now: dt.datetime) -> bool:
        """MEDIUM-3: the ``cancel_orders`` ROOT is a non-saga fan-out that
        dispatches NOTHING itself -- each child ``cancel_order`` is an
        independently-reconciled ledger row carrying its own authoritative
        outcome. A wedged root therefore has no ambiguous real-money action of
        its own; resolve it to a defined terminal so it never becomes an
        eternal critical alert (fail-safe: no order state is hidden)."""
        self._resolve_command_only(row, {"reconciled": "cancel_orders_root"}, now)
        return True

    def _reconcile_pause(self, row: LedgerRow, now: dt.datetime) -> bool:
        """MEDIUM-3: ``set_trading_pause`` is a single-step mutation whose
        control row records ``updated_by_command_id``. Resolve ONLY when the
        control row was last written by THIS command (positive proof the
        intended state committed); otherwise stay unknown."""
        state = self._pause_state_for(row.target_id)
        if state is None:
            return False
        paused, updated_by = state
        if updated_by == row.command_id:
            self._resolve_command_only(
                row, {"account_id": row.target_id, "new_exposure_paused": paused}, now
            )
            return True
        return False

    def _reconcile_strategy(self, row: LedgerRow, now: dt.datetime) -> bool:
        """Strategy-control forwarding: resolve via strategy_service's own
        committed receipt (``get_receipt`` by the root command_id). A missing
        or non-terminal receipt stays unknown -- the mutation is never
        re-forwarded."""
        receipt = self._strategy.get_receipt(row.command_id)
        if receipt is not None and receipt.state in ("COMMITTED", "ROLLED_BACK"):
            self._resolve_strategy(row, receipt, now)
            return True
        return False

    def _created_proposal_id(self, command_id: str) -> Optional[int]:
        """The id of the proposal a wedged ``create_proposal`` actually
        committed, or ``None``. A committed create journals a ``proposal.*``
        event correlated to the creating command_id (see
        ``ProposalCommandService.create_proposal``); its ABSENCE means the
        create never durably committed, so the command stays OUTCOME_UNKNOWN --
        fail-safe, NEVER marked FAILED."""
        try:
            found = self._journal.connect().execute(
                "SELECT entity_id FROM domain_event_journal "
                "WHERE entity_type = 'proposal' AND correlation_id = ? "
                "ORDER BY source_cursor LIMIT 1",
                [command_id],
            ).fetchone()
        except Exception:
            return None
        if found is None:
            return None
        try:
            return int(found[0])
        except (TypeError, ValueError):
            return None

    def _pause_state_for(self, account_id: str) -> Optional[tuple[bool, Optional[str]]]:
        """``(new_exposure_paused, updated_by_command_id)`` for the account's
        ``trading_control_state`` row, or ``None`` when the row/table is
        unavailable. Read directly off the journal DB (the same file the
        control store writes to); any read failure -> ``None`` -> stay unknown
        (fail-safe)."""
        try:
            row = self._journal.connect().execute(
                "SELECT new_exposure_paused, updated_by_command_id "
                "FROM trading_control_state WHERE account_id = ?",
                [account_id],
            ).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        return bool(row[0]), row[1]

    def _terminalize_pre_dispatch_orphan(self, row: LedgerRow) -> bool:
        """Terminalize a crash-orphaned ``VALIDATED`` row to
        ``REJECTED``/``CRASH_ORPHANED`` (MEDIUM-4). Returns ``True`` on a
        committed terminalization. Guarded per-row: a lost CAS (raced away from
        VALIDATED) or any write failure is swallowed so one bad row never
        aborts the whole startup rescan."""
        now = self._now_utc()

        def work(conn: duckdb.DuckDBPyConnection, append) -> None:
            self._ledger.transition_in_tx(
                conn, row.command_id, "VALIDATED", "REJECTED",
                error_code="CRASH_ORPHANED", now=now,
            )
            append(
                self._command_mutation(row, "REJECTED", now, error_code="CRASH_ORPHANED"),
                _noop_write, f"command:{row.command_id}:rejected",
            )

        try:
            self._journal.mutate_batch_work(self._journal.connect(), work)
        except Exception:
            return False
        self._plans.pop(row.command_id, None)
        return True

    # -- resolution transitions --------------------------------------------

    def _resolve_command_only(
        self, row: LedgerRow, outcome: dict[str, Any], now: dt.datetime
    ) -> None:
        """Transition the command ledger row to RESOLVED (with ``outcome``) and
        journal its ``command.updated`` event -- WITHOUT touching any proposal
        or order. Used by the create/reject/cancel/pause/cancel_orders
        reconciliations, whose authority is the mutation/order/control state
        itself, not a proposal to be marked."""
        def work(conn: duckdb.DuckDBPyConnection, append) -> None:
            self._ledger.transition_in_tx(
                conn, row.command_id, row.state, "RESOLVED", outcome=outcome, now=now,
            )
            append(
                self._command_mutation(row, "RESOLVED", now, outcome=outcome),
                _noop_write, f"command:{row.command_id}:resolved",
            )

        self._journal.mutate_batch_work(self._journal.connect(), work)
        self._plans.pop(row.command_id, None)

    def _resolve_order(self, row: LedgerRow, found: list, now: dt.datetime) -> None:
        """OUTCOME_UNKNOWN/SUBMITTING -> RESOLVED with the found broker aliases;
        marks the associated proposal EXECUTED (submission evidence) in the same
        journaled transaction."""
        order_ids = self._collect_order_ids(found)
        outcome = {"order_ids": order_ids, "order_group_id": f"og-{row.command_id}"}
        proposal_id = self._associated_proposal_id(row)

        def work(conn: duckdb.DuckDBPyConnection, append) -> None:
            self._mark_proposal(
                conn, append, row, proposal_id, "EXECUTED", order_ids=order_ids, now=now,
            )
            self._ledger.transition_in_tx(
                conn, row.command_id, row.state, "RESOLVED", outcome=outcome, now=now,
            )
            append(
                self._command_mutation(row, "RESOLVED", now, outcome=outcome),
                _noop_write, f"command:{row.command_id}:resolved",
            )

        self._journal.mutate_batch_work(self._journal.connect(), work)
        self._plans.pop(row.command_id, None)

    def _resolve_never_submitted(self, row: LedgerRow, now: dt.datetime) -> None:
        """Requires a fenced, COMPLETE broker enumeration ([M1-F2]) proving the
        order never reached the broker before recording ``{"submitted": False}``
        and failing the proposal cleanly -- NEVER a blind resubmission."""
        outcome = {"submitted": False}
        proposal_id = self._associated_proposal_id(row)

        def work(conn: duckdb.DuckDBPyConnection, append) -> None:
            self._mark_proposal(conn, append, row, proposal_id, "FAILED", now=now)
            self._ledger.transition_in_tx(
                conn, row.command_id, row.state, "RESOLVED", outcome=outcome, now=now,
            )
            append(
                self._command_mutation(row, "RESOLVED", now, outcome=outcome),
                _noop_write, f"command:{row.command_id}:resolved",
            )

        self._journal.mutate_batch_work(self._journal.connect(), work)
        self._plans.pop(row.command_id, None)

    def _resolve_strategy(self, row: LedgerRow, receipt, now: dt.datetime) -> None:
        """Journals the acknowledged ``strategy.updated`` (Task 7 contract) and
        the command outcome together. A reconcile-scoped ``event_id`` keeps this
        idempotent against ``acknowledge_state``'s own state-revision events."""
        payload = {
            "strategy_name": receipt.strategy_name,
            "action": receipt.action,
            "strategy_state": receipt.state,
            "control_revision": receipt.control_revision,
            "state_revision": receipt.state_revision,
            "error": receipt.error,
        }
        outcome = dict(payload)

        def work(conn: duckdb.DuckDBPyConnection, append) -> None:
            append(
                DomainMutation(
                    event_type="strategy.updated",
                    entity_type="strategy",
                    entity_id=receipt.strategy_name,
                    operation="upsert",
                    account_id=row.account_id,
                    source="trader_service",
                    source_timestamp=now,
                    correlation_id=row.command_id,
                    payload=payload,
                ),
                _noop_write,
                f"strategy:{receipt.strategy_name}:reconcile:{row.command_id}",
            )
            self._ledger.transition_in_tx(
                conn, row.command_id, row.state, "RESOLVED", outcome=outcome, now=now,
            )
            append(
                self._command_mutation(row, "RESOLVED", now, outcome=outcome),
                _noop_write, f"command:{row.command_id}:resolved",
            )

        self._journal.mutate_batch_work(self._journal.connect(), work)
        self._plans.pop(row.command_id, None)

    # -- helpers -----------------------------------------------------------

    def _mark_proposal(
        self, conn, append, row: LedgerRow, proposal_id: Optional[int],
        target_status: str, *, order_ids: Optional[list[int]] = None, now: dt.datetime,
    ) -> None:
        """Flip the associated proposal to EXECUTED/FAILED (submission evidence)
        inside the caller's transaction, appending its ``proposal.updated``
        event. A no-op when there is no associated proposal (e.g. a cancel), or
        when it is no longer APPROVED (already resolved by another path)."""
        if proposal_id is None or self._repo is None:
            return
        record = self._repo.get(proposal_id)
        if record is None or record.status != "APPROVED":
            return
        if target_status == "EXECUTED":
            prow = self._repo.mark_order_submitted_in_tx(
                conn, proposal_id, order_ids or [], record.revision, now,
            )
        else:
            prow = self._repo.mark_failed_in_tx(
                conn, proposal_id, "reconciled: order never submitted", record.revision, now,
            )
        if prow is not None:
            append(
                self._repo.mutation_for(prow, row.command_id),
                _assert_proposal_revision(prow.revision),
                f"proposal:{proposal_id}:{prow.revision}",
            )

    @staticmethod
    def _associated_proposal_id(row: LedgerRow) -> Optional[int]:
        """The proposal a command's reconciliation should mark, or None.

        The approve saga's target_type is "proposal" with the proposal id in
        ``target_id`` (production-faithful). A recorded ``outcome.proposal_id``
        (the plan's order-target test path) takes precedence. A cancel
        (target_type="order", a broker-order entity id) has neither -> None."""
        if row.outcome and row.outcome.get("proposal_id") is not None:
            return int(row.outcome["proposal_id"])
        if row.target_type == "proposal" and row.target_id:
            try:
                return int(row.target_id)
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _collect_order_ids(found: list) -> list[int]:
        ids: list[int] = []
        for item in found:
            item_ids = getattr(item, "order_ids", None)
            if item_ids:
                ids.extend(item_ids)
        return ids

    @staticmethod
    def _command_mutation(
        row: LedgerRow, to_state: str, now: dt.datetime, *,
        outcome: Optional[dict[str, Any]] = None, error_code: Optional[str] = None,
    ) -> DomainMutation:
        return DomainMutation(
            event_type="command.updated",
            entity_type="command",
            entity_id=command_entity_id(row.command_id),
            operation="upsert",
            account_id=row.account_id,
            source="trader_service",
            source_timestamp=now,
            correlation_id=row.command_id,
            payload={
                "state": to_state,
                "action": row.action,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "outcome": outcome,
                "error_code": error_code,
            },
        )

    def _now_utc(self) -> dt.datetime:
        return _as_utc(self._now())
