"""[M1-F3] Task 1 — clean proposal-authority schema bootstrap.

The command-authority ``trade_proposals`` table lives exclusively in the
trader-service-owned ``journal_duckdb_path`` database. Production is a fresh
deployment, so this module deliberately has no legacy-table relocation,
cross-file attachment, or compatibility freeze path. Keeping one file and one
writer is what lets Task 2 commit a proposal mutation and its
``proposal.updated`` journal event in the same transaction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation


# [M1-F3] owns migration versions 20-29 on the dedicated journal database.
PROPOSAL_AUTHORITY_MIGRATION_VERSION = 20
PROPOSAL_AUTHORITY_MIGRATION_NAME = "m1f3_proposal_command_authority"

# Stable projection contract for every repository query and journal payload.
PROPOSAL_COLUMNS: tuple[str, ...] = (
    "id", "symbol", "action", "quantity", "amount", "execution", "reasoning",
    "confidence", "thesis", "source", "metadata", "status", "created_at",
    "updated_at", "order_ids", "rejection_reason", "sec_type",
    "account_id", "account_mode", "conid", "reference_price",
    "reference_timestamp", "reference_quote_side", "reference_feed_type",
    "max_price_drift_bps", "expires_at", "live_approval_eligible", "revision",
    "order_group_id",
)
assert len(PROPOSAL_COLUMNS) == 29


@dataclass(frozen=True)
class ProposalRecord:
    """One guard-complete proposal row from the journal authority."""

    id: int
    symbol: str
    action: str
    quantity: Optional[float]
    amount: Optional[float]
    execution: dict[str, Any]
    reasoning: str
    confidence: float
    thesis: str
    source: str
    metadata: dict[str, Any]
    status: str
    created_at: datetime
    updated_at: datetime
    order_ids: list[int]
    rejection_reason: str
    sec_type: str
    account_id: Optional[str]
    account_mode: Optional[str]
    conid: Optional[int]
    reference_price: Optional[float]
    reference_timestamp: Optional[datetime]
    reference_quote_side: Optional[str]
    reference_feed_type: Optional[str]
    max_price_drift_bps: Optional[float]
    expires_at: Optional[datetime]
    live_approval_eligible: bool
    revision: int
    order_group_id: Optional[str]

    @classmethod
    def from_row(cls, row: tuple) -> "ProposalRecord":
        """Decode a row selected in ``PROPOSAL_COLUMNS`` order."""
        data = dict(zip(PROPOSAL_COLUMNS, row))
        return cls(
            id=data["id"],
            symbol=data["symbol"],
            action=data["action"],
            quantity=data["quantity"],
            amount=data["amount"],
            execution=json.loads(data["execution"]) if data["execution"] else {},
            reasoning=data["reasoning"] or "",
            confidence=data["confidence"] if data["confidence"] is not None else 0.0,
            thesis=data["thesis"] or "",
            source=data["source"] or "manual",
            metadata=json.loads(data["metadata"]) if data["metadata"] else {},
            status=data["status"],
            created_at=_as_utc(data["created_at"]),
            updated_at=_as_utc(data["updated_at"]),
            order_ids=json.loads(data["order_ids"]) if data["order_ids"] else [],
            rejection_reason=data["rejection_reason"] or "",
            sec_type=data["sec_type"] or "STK",
            account_id=data["account_id"],
            account_mode=data["account_mode"],
            conid=data["conid"],
            reference_price=data["reference_price"],
            reference_timestamp=_as_utc(data["reference_timestamp"]),
            reference_quote_side=data["reference_quote_side"],
            reference_feed_type=data["reference_feed_type"],
            max_price_drift_bps=data["max_price_drift_bps"],
            expires_at=_as_utc(data["expires_at"]),
            live_approval_eligible=bool(data["live_approval_eligible"]),
            revision=data["revision"],
            order_group_id=data["order_group_id"],
        )

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-native proposal projection carried by the journal."""
        return {
            "id": self.id,
            "symbol": self.symbol,
            "action": self.action,
            "quantity": self.quantity,
            "amount": self.amount,
            "execution": self.execution,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "thesis": self.thesis,
            "source": self.source,
            "metadata": self.metadata,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "order_ids": self.order_ids,
            "rejection_reason": self.rejection_reason,
            "sec_type": self.sec_type,
            "account_id": self.account_id,
            "account_mode": self.account_mode,
            "conid": self.conid,
            "reference_price": self.reference_price,
            "reference_timestamp": _iso(self.reference_timestamp),
            "reference_quote_side": self.reference_quote_side,
            "reference_feed_type": self.reference_feed_type,
            "max_price_drift_bps": self.max_price_drift_bps,
            "expires_at": _iso(self.expires_at),
            "live_approval_eligible": self.live_approval_eligible,
            "revision": self.revision,
            "order_group_id": self.order_group_id,
        }


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized else None


def _stored_timestamp(value: datetime) -> datetime:
    """Store UTC instants in the legacy-compatible TIMESTAMP columns."""
    normalized = _as_utc(value)
    assert normalized is not None
    return normalized.replace(tzinfo=None)


@dataclass(frozen=True)
class ProposalDraft:
    """Validated creation data before the repository assigns revision one."""

    id: int
    symbol: str
    action: str
    quantity: Optional[float]
    amount: Optional[float]
    execution: dict[str, Any]
    reasoning: str
    confidence: float
    thesis: str
    source: str
    metadata: dict[str, Any]
    sec_type: str
    account_id: str
    account_mode: str
    conid: int
    reference_price: float
    reference_timestamp: datetime
    reference_quote_side: str
    reference_feed_type: str
    max_price_drift_bps: float
    expires_at: datetime
    live_approval_eligible: bool
    created_at: datetime


class ApprovalClaim(str, Enum):
    CLAIMED = "CLAIMED"
    EXPIRED = "EXPIRED"
    REVISION_MISMATCH = "REVISION_MISMATCH"
    NOT_PENDING = "NOT_PENDING"
    NOT_FOUND = "NOT_FOUND"
    WRONG_ACCOUNT = "WRONG_ACCOUNT"


@dataclass(frozen=True)
class ApprovalClaimOutcome:
    result: ApprovalClaim
    record: Optional[ProposalRecord]


_CREATE_SEQUENCE_SQL = "CREATE SEQUENCE trade_proposals_id_seq START 1"

_CREATE_TABLE_SQL = """
    CREATE TABLE trade_proposals (
        id INTEGER PRIMARY KEY DEFAULT nextval('trade_proposals_id_seq'),
        symbol VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        quantity DOUBLE,
        amount DOUBLE,
        execution VARCHAR DEFAULT '{}',
        reasoning VARCHAR DEFAULT '',
        confidence DOUBLE DEFAULT 0.0,
        thesis VARCHAR DEFAULT '',
        source VARCHAR DEFAULT 'manual',
        metadata VARCHAR DEFAULT '{}',
        status VARCHAR DEFAULT 'PENDING',
        created_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        order_ids VARCHAR DEFAULT '[]',
        rejection_reason VARCHAR DEFAULT '',
        sec_type VARCHAR DEFAULT 'STK',
        account_id VARCHAR,
        account_mode VARCHAR,
        conid INTEGER,
        reference_price DOUBLE,
        reference_timestamp TIMESTAMPTZ,
        reference_quote_side VARCHAR,
        reference_feed_type VARCHAR,
        max_price_drift_bps DOUBLE,
        expires_at TIMESTAMPTZ,
        live_approval_eligible BOOLEAN NOT NULL DEFAULT false,
        revision BIGINT NOT NULL DEFAULT 1,
        order_group_id VARCHAR
    )
"""


def apply_proposal_authority_migration(journal_migrator: SchemaMigrator) -> None:
    """Create the fresh, guarded proposal authority exactly once.

    ``journal_migrator`` targets the dedicated ``journal_duckdb_path`` file.
    Production begins with an empty authority: proposal imports and direct
    writes to the retired SDK-owned store are intentionally unsupported.
    """
    journal_migrator.apply(
        version=PROPOSAL_AUTHORITY_MIGRATION_VERSION,
        name=PROPOSAL_AUTHORITY_MIGRATION_NAME,
        statements=[_CREATE_SEQUENCE_SQL, _CREATE_TABLE_SQL],
    )


class ProposalRepository:
    """Journal-file persistence adapter for guard-complete proposals.

    Mutating methods accept the connection supplied by ``DomainJournal.mutate``.
    They never begin or commit their own transaction: the service supplies the
    materialized write callback so the proposal row and journal event are one
    durable unit of work.
    """

    _SELECT = f"SELECT {', '.join(PROPOSAL_COLUMNS)} FROM trade_proposals"

    def __init__(self, journal: DomainJournal):
        self._journal = journal

    def reserve_id(self) -> int:
        """Reserve an entity identity before creating its journal mutation.

        DuckDB sequences may have gaps after a failed transaction; gaps are
        harmless and preferable to inventing a second identity allocator.
        """
        row = self._journal.connect().execute(
            "SELECT nextval('trade_proposals_id_seq')"
        ).fetchone()
        assert row is not None
        return int(row[0])

    def get(self, proposal_id: int) -> Optional[ProposalRecord]:
        row = self._journal.connect().execute(
            f"{self._SELECT} WHERE id = ?", [proposal_id]
        ).fetchone()
        return ProposalRecord.from_row(row) if row else None

    def list(self, status: Optional[str], limit: int) -> list[ProposalRecord]:
        conn = self._journal.connect()
        if status is None:
            rows = conn.execute(
                f"{self._SELECT} ORDER BY created_at DESC LIMIT ?", [limit]
            ).fetchall()
        else:
            rows = conn.execute(
                f"{self._SELECT} WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                [status, limit],
            ).fetchall()
        return [ProposalRecord.from_row(row) for row in rows]

    def pending_duplicate_in_tx(
        self, conn: Any, source: str, conid: int, action: str
    ) -> bool:
        row = conn.execute(
            "SELECT 1 FROM trade_proposals "
            "WHERE status = 'PENDING' AND source = ? AND conid = ? AND action = ? "
            "LIMIT 1",
            [source, conid, action],
        ).fetchone()
        return row is not None

    def insert_pending_in_tx(
        self, conn: Any, draft: ProposalDraft, revision: int
    ) -> ProposalRecord:
        row = conn.execute(
            """
            INSERT INTO trade_proposals (
                id, symbol, action, quantity, amount, execution, reasoning,
                confidence, thesis, source, metadata, status, created_at,
                updated_at, order_ids, rejection_reason, sec_type, account_id,
                account_mode, conid, reference_price, reference_timestamp,
                reference_quote_side, reference_feed_type, max_price_drift_bps,
                expires_at, live_approval_eligible, revision, order_group_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, '[]', '',
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            RETURNING """ + ", ".join(PROPOSAL_COLUMNS),
            [
                draft.id, draft.symbol, draft.action, draft.quantity, draft.amount,
                json.dumps(draft.execution, sort_keys=True), draft.reasoning,
                draft.confidence, draft.thesis, draft.source,
                json.dumps(draft.metadata, sort_keys=True),
                _stored_timestamp(draft.created_at), _stored_timestamp(draft.created_at),
                draft.sec_type, draft.account_id, draft.account_mode, draft.conid,
                draft.reference_price, _as_utc(draft.reference_timestamp),
                draft.reference_quote_side, draft.reference_feed_type,
                draft.max_price_drift_bps, _as_utc(draft.expires_at),
                draft.live_approval_eligible, revision,
            ],
        ).fetchone()
        assert row is not None
        return ProposalRecord.from_row(row)

    def reject_in_tx(
        self, conn: Any, proposal_id: int, reason: str, now: datetime, expected_revision: int
    ) -> Optional[ProposalRecord]:
        row = conn.execute(
            """
            UPDATE trade_proposals
               SET status = 'REJECTED', rejection_reason = ?, updated_at = ?,
                   revision = revision + 1
             WHERE id = ? AND status = 'PENDING' AND revision = ?
         RETURNING """ + ", ".join(PROPOSAL_COLUMNS),
            [reason, _stored_timestamp(now), proposal_id, expected_revision],
        ).fetchone()
        return ProposalRecord.from_row(row) if row else None

    def expire_in_tx(
        self, conn: Any, proposal_id: int, now: datetime, expected_revision: int
    ) -> Optional[ProposalRecord]:
        row = conn.execute(
            """
            UPDATE trade_proposals
               SET status = 'EXPIRED', updated_at = ?, revision = revision + 1
             WHERE id = ? AND status = 'PENDING' AND revision = ?
               AND ((expires_at IS NOT NULL AND expires_at <= ?)
                    OR (expires_at IS NULL
                        AND json_extract_string(metadata, '$.expires_at') IS NOT NULL))
         RETURNING """ + ", ".join(PROPOSAL_COLUMNS),
            [_stored_timestamp(now), proposal_id, expected_revision, _as_utc(now)],
        ).fetchone()
        return ProposalRecord.from_row(row) if row else None

    def stale_pending(self, now: datetime) -> list[ProposalRecord]:
        rows = self._journal.connect().execute(
            f"{self._SELECT} WHERE status = 'PENDING' "
            "AND ((expires_at IS NOT NULL AND expires_at <= ?) "
            "OR (expires_at IS NULL "
            "AND json_extract_string(metadata, '$.expires_at') IS NOT NULL)) "
            "ORDER BY id",
            [_as_utc(now)],
        ).fetchall()
        return [ProposalRecord.from_row(row) for row in rows]

    def claim_for_approval_in_tx(
        self,
        conn: Any,
        proposal_id: int,
        expected_revision: int,
        account_id: str,
        now: datetime,
    ) -> ApprovalClaimOutcome:
        row = conn.execute(
            """
            UPDATE trade_proposals
               SET status = CASE
                       WHEN expires_at IS NULL
                            AND json_extract_string(metadata, '$.expires_at') IS NOT NULL
                           THEN 'EXPIRED'
                       WHEN expires_at IS NOT NULL AND expires_at <= ? THEN 'EXPIRED'
                       ELSE 'APPROVED'
                   END,
                   updated_at = ?, revision = revision + 1
             WHERE id = ? AND status = 'PENDING' AND revision = ?
               AND account_id = ?
         RETURNING """ + ", ".join(PROPOSAL_COLUMNS),
            [_as_utc(now), _stored_timestamp(now), proposal_id, expected_revision, account_id],
        ).fetchone()
        if row:
            record = ProposalRecord.from_row(row)
            return ApprovalClaimOutcome(
                ApprovalClaim.CLAIMED if record.status == "APPROVED" else ApprovalClaim.EXPIRED,
                record,
            )
        current = self.get(proposal_id)
        if current is None:
            return ApprovalClaimOutcome(ApprovalClaim.NOT_FOUND, None)
        if current.account_id != account_id:
            return ApprovalClaimOutcome(ApprovalClaim.WRONG_ACCOUNT, current)
        if current.status != "PENDING":
            return ApprovalClaimOutcome(ApprovalClaim.NOT_PENDING, current)
        return ApprovalClaimOutcome(ApprovalClaim.REVISION_MISMATCH, current)

    def link_order_group_in_tx(
        self, conn: Any, proposal_id: int, order_group_id: str
    ) -> None:
        """Bind ``order_group_id`` to an ``APPROVED`` proposal WITHOUT bumping
        ``revision``.

        [M1-F3] Task 5: this runs inside the approval saga's claim transaction
        so the proposal→group binding is durable *before* the irreversible
        dispatch (it is what lets the Task-9 reconciler correlate broker rows
        back to a proposal on the ``OUTCOME_UNKNOWN`` path). It deliberately
        does not touch ``revision`` and gets no journal event of its own --
        the caller folds ``order_group_id`` into the claim's single
        ``proposal.updated`` event (built from the linked record), keeping the
        one-append-per-revision-bump lockstep intact.
        """
        conn.execute(
            "UPDATE trade_proposals SET order_group_id = ? "
            "WHERE id = ? AND status = 'APPROVED'",
            [order_group_id, proposal_id],
        )

    def mark_order_submitted_in_tx(
        self,
        conn: Any,
        proposal_id: int,
        order_ids: list[int],
        expected_revision: int,
        now: datetime,
    ) -> Optional[ProposalRecord]:
        """Flip ``APPROVED`` → ``EXECUTED`` recording the placed ``order_ids``.

        The single "submit-link" write of the approval saga: ``status`` becomes
        ``EXECUTED`` (the storage-layer name for a submitted/working order --
        [S0] maps it for display), ``order_ids`` is set, and ``revision`` is
        bumped exactly once. Guarded on ``status = 'APPROVED' AND revision = ?``
        so a concurrent change (or a double-submit) returns ``None`` rather than
        clobbering. ``order_group_id`` is intentionally left untouched (it was
        linked in the claim tx).
        """
        row = conn.execute(
            """
            UPDATE trade_proposals
               SET status = 'EXECUTED', order_ids = ?, updated_at = ?,
                   revision = revision + 1
             WHERE id = ? AND status = 'APPROVED' AND revision = ?
         RETURNING """ + ", ".join(PROPOSAL_COLUMNS),
            [json.dumps(list(order_ids)), _stored_timestamp(now), proposal_id, expected_revision],
        ).fetchone()
        return ProposalRecord.from_row(row) if row else None

    def mark_failed_in_tx(
        self,
        conn: Any,
        proposal_id: int,
        reason: str,
        expected_revision: int,
        now: datetime,
    ) -> Optional[ProposalRecord]:
        """Flip ``APPROVED`` → ``FAILED`` after a clean broker rejection.

        Used ONLY on the approval saga's ``BrokerRejectedError`` path -- a
        clean ``SuccessFail.fail`` before any live order left the door. An
        ambiguous dispatch (timeout/disconnect) must NOT call this: the
        proposal stays ``APPROVED`` for the Task-9 reconciler. Guarded on
        ``status = 'APPROVED' AND revision = ?``; bumps ``revision`` once.
        """
        row = conn.execute(
            """
            UPDATE trade_proposals
               SET status = 'FAILED', rejection_reason = ?, updated_at = ?,
                   revision = revision + 1
             WHERE id = ? AND status = 'APPROVED' AND revision = ?
         RETURNING """ + ", ".join(PROPOSAL_COLUMNS),
            [reason, _stored_timestamp(now), proposal_id, expected_revision],
        ).fetchone()
        return ProposalRecord.from_row(row) if row else None

    def mutation_for(
        self, record: ProposalRecord, correlation_id: Optional[str]
    ) -> DomainMutation:
        """Build the complete proposal event consumed by the dashboard feed."""
        return DomainMutation(
            event_type="proposal.updated",
            entity_type="proposal",
            entity_id=str(record.id),
            operation="upsert",
            account_id=record.account_id,
            source="trader_service",
            source_timestamp=record.updated_at,
            correlation_id=correlation_id,
            payload=record.to_payload(),
        )
