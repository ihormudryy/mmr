"""[M1-F3] Task 1 — proposal schema migration + exclusive versioned cutover.

Cross-file relocation (binding pre-flight resolution B2)
---------------------------------------------------------
The command-authority `trade_proposals` table does NOT live in
`mmr.duckdb` (the [S0] `ProposalStore` file) anymore. It lives in the
dedicated `journal_duckdb_path` file alongside `DomainJournal` (see
`trader/data/domain_journal.py`), because a durable proposal mutation and
its `proposal.updated` journal event must commit in ONE transaction
(`DomainJournal.mutate` self-manages BEGIN/COMMIT on a `journal.connect()`
connection and must never be nested inside `DuckDBConnection.transaction`
— see the plan's pre-flight resolution).

Consequence: this migration is a genuine CROSS-FILE relocation, not an
in-place `ALTER`/rename-swap:

1. `apply_proposal_authority_migration` registers migration version 20
   (`m1f3_proposal_command_authority`) on the JOURNAL db's
   `SchemaMigrator` — `[M1-F3]` owns versions 20-29 (`[M1-F1]` owns 1-9,
   `[M1-F2]` owns 10-19; see `schema_migrations.py`'s owned-range
   docstring). This CREATEs the guard-complete 29-column
   `trade_proposals` table in the journal file, then relocates every
   existing row from the legacy `mmr.duckdb` file via a read-only
   `ATTACH` + `INSERT ... SELECT` + `DETACH` — verified empirically
   (DuckDB 1.4.4) to work inside the single explicit transaction
   `SchemaMigrator.apply()` already wraps its statement list in.
2. Migrated rows get `live_approval_eligible = false` and `revision = 1`
   unconditionally. The account/mode/reference-price/reference-side/
   reference-feed/drift-guard columns are NEVER backfilled — a migrated
   row simply never had those guards computed, and inventing them here
   would misrepresent a legacy manual/strategy proposal as a live-quote-
   verified one. Only two values parsable straight out of the original
   `metadata` JSON are copied: `conid` (if it casts to an integer) and
   `expires_at` (if, and only if, it carries an explicit UTC offset AND
   parses as a valid TIMESTAMPTZ — see `_AWARE_EXPIRY`). The full
   original `metadata` blob is ALSO copied verbatim (not replaced) — this
   is load-bearing, not just an audit nicety: `[M1-F3]` Task 2's
   `expire_stale_pending_in_tx` distinguishes "no expiry anywhere" (valid
   forever, `[S0]` legacy-manual semantics) from "an expiry was present
   but could not be migrated" (already-invalid, must fail closed) by
   checking `json_extract_string(metadata, '$.expires_at')` ALONGSIDE the
   new `expires_at` column — both cases leave the new column NULL, only
   the metadata blob preserves the distinction.
3. The legacy `mmr.duckdb` `trade_proposals` table is left completely
   untouched (no ALTER, no new columns, no rows removed) — it is
   READ-ONLY-attached, never written to, during relocation. Instead, the
   cutover FREEZES it against further writes from the `[S0]`
   `ProposalStore` path by recording the SAME version number (20) in the
   legacy db's OWN, independent `schema_migrations` ledger (a zero-DDL
   marker — `SchemaMigrator.apply(20, ..., statements=[])`). Two separate
   files, two separate ledgers, one shared version number for one
   logical cutover event. `ProposalStore._cutover_applied()` reads that
   marker to decide whether to raise `ProposalStoreFrozen`.

Idempotent: a second call to `apply_proposal_authority_migration` is a
no-op on both files (each `SchemaMigrator.apply` skips an already-recorded
version), so rows are never duplicated and the freeze marker is never
double-written.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

# [M1-F3] owns migration versions 20-29 on the JOURNAL db's SchemaMigrator
# ([M1-F1] = 1-9, [M1-F2] = 10-19 — see schema_migrations.py). This exact
# version number is ALSO reused, unaltered, as the zero-DDL freeze marker
# recorded in the LEGACY mmr.duckdb's own, independent schema_migrations
# ledger (see module docstring, point 3) — two files, one logical cutover.
PROPOSAL_AUTHORITY_MIGRATION_VERSION = 20
PROPOSAL_AUTHORITY_MIGRATION_NAME = "m1f3_proposal_command_authority"
_LEGACY_FREEZE_MIGRATION_NAME = "m1f3_proposal_command_authority_legacy_freeze"

# The explicit, stable column list for the migrated `trade_proposals` table
# (journal_duckdb_path file): 17 legacy columns (byte-for-byte the [S0]
# mmr.duckdb column set, same order) + 12 new command-authority guard
# columns. `proposal_store.py`'s `_LEGACY_COLUMNS` pins the legacy table's
# own SELECT list independently — the two lists are deliberately decoupled
# so neither table's positional decoding depends on the other's schema.
PROPOSAL_COLUMNS: tuple[str, ...] = (
    "id", "symbol", "action", "quantity", "amount", "execution", "reasoning",
    "confidence", "thesis", "source", "metadata", "status", "created_at",
    "updated_at", "order_ids", "rejection_reason", "sec_type",
    "account_id", "account_mode", "conid", "reference_price",
    "reference_timestamp", "reference_quote_side", "reference_feed_type",
    "max_price_drift_bps", "expires_at", "live_approval_eligible", "revision",
    "order_group_id",
)
assert len(PROPOSAL_COLUMNS) == 29, (
    "PROPOSAL_COLUMNS must carry exactly 29 columns (17 legacy + 12 guard) "
    f"— got {len(PROPOSAL_COLUMNS)}"
)


@dataclass(frozen=True)
class ProposalRecord:
    """One row of the migrated, guard-complete `trade_proposals` table.

    Field names/order mirror `PROPOSAL_COLUMNS` exactly. This task defines
    only the type and a row decoder; `[M1-F3]` Task 2's `ProposalRepository`
    builds the CRUD/claim/journal methods on top of it.
    """
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
        """Decode one row of `SELECT {', '.join(PROPOSAL_COLUMNS)} ...`.

        Positions are pinned by `PROPOSAL_COLUMNS`, never physical table
        order — callers must select exactly that column list, in that
        order, for this decoder to be correct.
        """
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


# Same expiry-awareness guard as [S0]'s ProposalStore (preserved exactly —
# see the Global Constraint on preserving claim/expire semantics): an
# expiry is only ever migrated into the new typed `expires_at` column when
# its string carries an explicit UTC offset/Z suffix AND parses cleanly.
_EXPIRY_JSON = "json_extract_string(metadata, '$.expires_at')"
_AWARE_EXPIRY = (
    f"regexp_matches({_EXPIRY_JSON}, '(Z|[+-][0-9]{{2}}:[0-9]{{2}})$') "
    f"AND TRY_CAST({_EXPIRY_JSON} AS TIMESTAMPTZ) IS NOT NULL"
)

# Fresh CREATE (no rename-swap needed — this is a brand-new table in a
# brand-new file, never an in-place migration of the legacy table).
_CREATE_TABLE_SQL = """
    CREATE TABLE trade_proposals (
        id INTEGER PRIMARY KEY,
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


def _quote_literal(path: str) -> str:
    """Escape a filesystem path for embedding as a single-quoted DuckDB
    SQL string literal (double any embedded single quote)."""
    return path.replace("'", "''")


def _relocation_statements(legacy_db_path: str) -> list[str]:
    """ATTACH the legacy `mmr.duckdb` file read-only, copy every existing
    row into the freshly-created journal `trade_proposals` table, and
    DETACH — verified to run correctly inside an explicit transaction
    (DuckDB 1.4.4).
    """
    quoted = _quote_literal(legacy_db_path)
    return [
        f"ATTACH '{quoted}' AS legacy (READ_ONLY)",
        f"""
        INSERT INTO trade_proposals (
            id, symbol, action, quantity, amount, execution, reasoning, confidence,
            thesis, source, metadata, status, created_at, updated_at, order_ids,
            rejection_reason, sec_type, conid, expires_at, live_approval_eligible,
            revision
        )
        SELECT
            id, symbol, action, quantity, amount, execution, reasoning, confidence,
            thesis, source, metadata, status, created_at, updated_at, order_ids,
            rejection_reason, sec_type,
            TRY_CAST(json_extract_string(metadata, '$.conid') AS INTEGER),
            CASE WHEN {_AWARE_EXPIRY}
                 THEN TRY_CAST({_EXPIRY_JSON} AS TIMESTAMPTZ)
                 ELSE NULL END,
            false,
            1
        FROM legacy.trade_proposals
        """,
        "DETACH legacy",
    ]


def apply_proposal_authority_migration(
    journal_migrator: SchemaMigrator,
    legacy_db_path: str,
) -> None:
    """Exclusive versioned cutover (spec Sec 5.5; binding pre-flight
    resolution B2). See the module docstring for the full cross-file
    relocation rationale.

    ``journal_migrator`` must wrap the dedicated ``journal_duckdb_path``
    file's ``DuckDBConnection`` (never ``mmr.duckdb``). ``legacy_db_path``
    is the ``mmr.duckdb`` file path the [S0] ``ProposalStore`` still
    points at — it is read via a read-only ``ATTACH`` and otherwise left
    completely untouched.

    Idempotent: a second call is a no-op on both files.
    """
    statements = [_CREATE_TABLE_SQL, *_relocation_statements(legacy_db_path)]
    journal_migrator.apply(
        version=PROPOSAL_AUTHORITY_MIGRATION_VERSION,
        name=PROPOSAL_AUTHORITY_MIGRATION_NAME,
        statements=statements,
    )
    _freeze_legacy_writers(legacy_db_path)


def _freeze_legacy_writers(legacy_db_path: str) -> None:
    """Record the cutover version in the LEGACY db's own, independent
    `schema_migrations` ledger with zero DDL — a marker only.
    `ProposalStore._cutover_applied()` reads this back to decide whether
    to raise `ProposalStoreFrozen`. Idempotent via `SchemaMigrator.apply`'s
    own ledger gating; safe to call on every invocation of
    `apply_proposal_authority_migration`.
    """
    legacy_db = DuckDBConnection.get_instance(legacy_db_path)
    SchemaMigrator(legacy_db).apply(
        version=PROPOSAL_AUTHORITY_MIGRATION_VERSION,
        name=_LEGACY_FREEZE_MIGRATION_NAME,
        statements=[],
    )
