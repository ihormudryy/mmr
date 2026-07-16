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
from datetime import datetime
from typing import Any, Optional

from trader.data.schema_migrations import SchemaMigrator


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
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            order_ids=json.loads(data["order_ids"]) if data["order_ids"] else [],
            rejection_reason=data["rejection_reason"] or "",
            sec_type=data["sec_type"] or "STK",
            account_id=data["account_id"],
            account_mode=data["account_mode"],
            conid=data["conid"],
            reference_price=data["reference_price"],
            reference_timestamp=data["reference_timestamp"],
            reference_quote_side=data["reference_quote_side"],
            reference_feed_type=data["reference_feed_type"],
            max_price_drift_bps=data["max_price_drift_bps"],
            expires_at=data["expires_at"],
            live_approval_eligible=bool(data["live_approval_eligible"]),
            revision=data["revision"],
            order_group_id=data["order_group_id"],
        )


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
