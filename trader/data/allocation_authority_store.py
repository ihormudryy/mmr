"""P5 Task 1 — append-only signed allocation authority ledger."""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.domain.identity import strategy_entity_id
from trader.promotion.allocation_attestation import (
    AllocationAttestation,
    VerifiedAllocationAuthority,
    allocation_attestation_to_wire,
    allocation_payload_digest,
)

ALLOCATION_AUTHORITY_MIGRATION_50 = 50
ALLOCATION_AUTHORITY_MIGRATION_51 = 51
ALLOCATION_AUTHORITY_MIGRATION_VERSIONS = (
    ALLOCATION_AUTHORITY_MIGRATION_50,
    ALLOCATION_AUTHORITY_MIGRATION_51,
)
ALLOCATION_AUTHORITY_MIGRATION_50_NAME = "p5_allocation_authorities"
ALLOCATION_AUTHORITY_MIGRATION_51_NAME = "p5_allocation_authority_events"

EVENT_ISSUED = "ISSUED"
EVENT_ACTIVATED = "ACTIVATED"
EVENT_DEACTIVATED = "DEACTIVATED"
EVENT_REVOKED = "REVOKED"
EVENT_SUPERSEDED = "SUPERSEDED"

AUTHORITY_EVENTS = frozenset({
    EVENT_ISSUED,
    EVENT_ACTIVATED,
    EVENT_DEACTIVATED,
    EVENT_REVOKED,
    EVENT_SUPERSEDED,
})


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def apply_allocation_authority_migrations(migrator: SchemaMigrator) -> bool:
    """Apply journal migrations 50-51. Returns True if any newly applied."""
    event_literals = ", ".join(f"'{event}'" for event in sorted(AUTHORITY_EVENTS))
    applied = False
    applied |= migrator.apply(
        ALLOCATION_AUTHORITY_MIGRATION_50,
        ALLOCATION_AUTHORITY_MIGRATION_50_NAME,
        (
            "CREATE SEQUENCE IF NOT EXISTS allocation_authorities_seq START 1",
            """CREATE TABLE IF NOT EXISTS allocation_authorities (
                entry_id BIGINT PRIMARY KEY DEFAULT nextval('allocation_authorities_seq'),
                authority_digest VARCHAR NOT NULL,
                strategy_id VARCHAR NOT NULL,
                account_id VARCHAR NOT NULL,
                account_mode VARCHAR NOT NULL,
                stage VARCHAR NOT NULL,
                artifact_digest VARCHAR NOT NULL,
                allowlist_digest VARCHAR NOT NULL,
                ruleset_digest VARCHAR NOT NULL,
                max_gross_allocation DOUBLE NOT NULL,
                evidence_digest VARCHAR NOT NULL,
                public_key_id VARCHAR NOT NULL,
                operator VARCHAR NOT NULL,
                reason VARCHAR NOT NULL,
                issued_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                event VARCHAR NOT NULL CHECK (event IN ({events})),
                superseded_by_digest VARCHAR,
                command_id VARCHAR,
                recorded_at TIMESTAMPTZ NOT NULL
            )""".format(events=event_literals),
            """CREATE INDEX IF NOT EXISTS idx_allocation_authorities_digest
                ON allocation_authorities(authority_digest)""",
            """CREATE INDEX IF NOT EXISTS idx_allocation_authorities_account_artifact
                ON allocation_authorities(account_id, artifact_digest)""",
        ),
    )
    applied |= migrator.apply(
        ALLOCATION_AUTHORITY_MIGRATION_51,
        ALLOCATION_AUTHORITY_MIGRATION_51_NAME,
        (
            "CREATE SEQUENCE IF NOT EXISTS allocation_authority_events_seq START 1",
            """CREATE TABLE IF NOT EXISTS allocation_authority_events (
                event_id BIGINT PRIMARY KEY DEFAULT nextval('allocation_authority_events_seq'),
                authority_digest VARCHAR NOT NULL,
                account_id VARCHAR NOT NULL,
                artifact_digest VARCHAR NOT NULL,
                event VARCHAR NOT NULL CHECK (event IN ({events})),
                superseded_by_digest VARCHAR,
                command_id VARCHAR,
                recorded_at TIMESTAMPTZ NOT NULL
            )""".format(events=event_literals),
            """CREATE INDEX IF NOT EXISTS idx_allocation_authority_events_digest
                ON allocation_authority_events(authority_digest)""",
            """CREATE INDEX IF NOT EXISTS idx_allocation_authority_events_account_artifact
                ON allocation_authority_events(account_id, artifact_digest)""",
        ),
    )
    return applied


@dataclass(frozen=True)
class AllocationAuthorityRecord:
    entry_id: int
    authority_digest: str
    strategy_id: str
    account_id: str
    account_mode: str
    stage: str
    artifact_digest: str
    allowlist_digest: str
    ruleset_digest: str
    max_gross_allocation: float
    evidence_digest: str
    public_key_id: str
    operator: str
    reason: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    event: str
    superseded_by_digest: Optional[str]
    command_id: Optional[str]
    recorded_at: dt.datetime


_SELECT_COLUMNS = (
    "entry_id, authority_digest, strategy_id, account_id, account_mode, stage, "
    "artifact_digest, allowlist_digest, ruleset_digest, max_gross_allocation, "
    "evidence_digest, public_key_id, operator, reason, issued_at, expires_at, "
    "event, superseded_by_digest, command_id, recorded_at"
)


def _row_to_record(row: Sequence[Any]) -> AllocationAuthorityRecord:
    return AllocationAuthorityRecord(
        entry_id=row[0],
        authority_digest=row[1],
        strategy_id=row[2],
        account_id=row[3],
        account_mode=row[4],
        stage=row[5],
        artifact_digest=row[6],
        allowlist_digest=row[7],
        ruleset_digest=row[8],
        max_gross_allocation=float(row[9]),
        evidence_digest=row[10],
        public_key_id=row[11],
        operator=row[12],
        reason=row[13],
        issued_at=_as_utc(row[14]),
        expires_at=_as_utc(row[15]),
        event=row[16],
        superseded_by_digest=row[17],
        command_id=row[18],
        recorded_at=_as_utc(row[19]),
    )


class AllocationAuthorityStore:
    """Append-only persistence for signed allocation authorities (migrations 50-51).

    ``allocation_authorities`` holds the full authority snapshot on every lifecycle
    transition; ``allocation_authority_events`` mirrors event names for fast
    account/artifact lookups. Current state is always derived from the latest row
    for a digest — there is no UPDATE/DELETE anywhere in this class.
    """

    def __init__(
        self,
        journal: DomainJournal,
        db: Any,
        now: Optional[Callable[[], dt.datetime]] = None,
    ):
        self.journal = journal
        self.db = db
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def _append_event(
        self,
        conn: Any,
        *,
        authority_digest: str,
        account_id: str,
        artifact_digest: str,
        event: str,
        superseded_by_digest: Optional[str],
        command_id: Optional[str],
        recorded_at: dt.datetime,
    ) -> None:
        conn.execute(
            "INSERT INTO allocation_authority_events "
            "(authority_digest, account_id, artifact_digest, event, "
            "superseded_by_digest, command_id, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                authority_digest,
                account_id,
                artifact_digest,
                event,
                superseded_by_digest,
                command_id,
                recorded_at,
            ],
        )

    def _append(
        self,
        *,
        authority_digest: str,
        strategy_id: str,
        account_id: str,
        account_mode: str,
        stage: str,
        artifact_digest: str,
        allowlist_digest: str,
        ruleset_digest: str,
        max_gross_allocation: float,
        evidence_digest: str,
        public_key_id: str,
        operator: str,
        reason: str,
        issued_at: dt.datetime,
        expires_at: dt.datetime,
        event: str,
        superseded_by_digest: Optional[str] = None,
        command_id: Optional[str] = None,
        now: dt.datetime,
    ) -> AllocationAuthorityRecord:
        if event not in AUTHORITY_EVENTS:
            raise ValueError(f"event must be one of {AUTHORITY_EVENTS}, got {event!r}")
        recorded_at = _as_utc(now)
        captured: list[AllocationAuthorityRecord] = []

        def write(conn: Any, _revision: int) -> None:
            row = conn.execute(
                f"INSERT INTO allocation_authorities "
                f"(authority_digest, strategy_id, account_id, account_mode, stage, "
                f"artifact_digest, allowlist_digest, ruleset_digest, max_gross_allocation, "
                f"evidence_digest, public_key_id, operator, reason, issued_at, expires_at, "
                f"event, superseded_by_digest, command_id, recorded_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                f"RETURNING {_SELECT_COLUMNS}",
                [
                    authority_digest,
                    strategy_id,
                    account_id,
                    account_mode,
                    stage,
                    artifact_digest,
                    allowlist_digest,
                    ruleset_digest,
                    float(max_gross_allocation),
                    evidence_digest,
                    public_key_id,
                    operator,
                    reason,
                    _as_utc(issued_at),
                    _as_utc(expires_at),
                    event,
                    superseded_by_digest,
                    command_id,
                    recorded_at,
                ],
            ).fetchone()
            captured.append(_row_to_record(row))
            self._append_event(
                conn,
                authority_digest=authority_digest,
                account_id=account_id,
                artifact_digest=artifact_digest,
                event=event,
                superseded_by_digest=superseded_by_digest,
                command_id=command_id,
                recorded_at=recorded_at,
            )

        mutation = DomainMutation(
            event_type="promotion.allocation_authority_recorded",
            entity_type="allocation_authority",
            entity_id=strategy_entity_id(strategy_id),
            operation="upsert",
            account_id=account_id,
            source="trader_service",
            source_timestamp=recorded_at,
            correlation_id=command_id or authority_digest,
            payload={
                "authority_digest": authority_digest,
                "strategy_id": strategy_id,
                "stage": stage,
                "event": event,
                "command_id": command_id,
            },
        )
        self.journal.mutate(
            self.journal.connect(),
            mutation,
            write,
            event_id=f"allocation-authority:{authority_digest}:{event}:{uuid.uuid4().hex}",
        )
        return captured[0]

    def record_issued(
        self,
        attestation: AllocationAttestation,
        verified: VerifiedAllocationAuthority,
        *,
        operator: str,
        reason: str,
        now: Optional[dt.datetime] = None,
    ) -> str:
        digest = verified.payload_digest
        if self.latest(digest) is not None:
            return digest
        self._append(
            authority_digest=digest,
            strategy_id=verified.strategy_id,
            account_id=verified.account_id,
            account_mode=attestation.account_mode,
            stage=verified.stage,
            artifact_digest=verified.artifact_digest,
            allowlist_digest=verified.allowlist_digest,
            ruleset_digest=verified.ruleset_digest,
            max_gross_allocation=verified.max_gross_allocation,
            evidence_digest=verified.evidence_digest,
            public_key_id=verified.public_key_id,
            operator=operator,
            reason=reason,
            issued_at=attestation.issued_at,
            expires_at=verified.expires_at,
            event=EVENT_ISSUED,
            now=now or self._now(),
        )
        return digest

    def record_activated(
        self,
        authority_digest: str,
        *,
        command_id: str,
        now: Optional[dt.datetime] = None,
    ) -> None:
        base = self.latest(authority_digest)
        if base is None:
            raise ValueError(f"cannot activate unknown authority {authority_digest!r}")
        self._append(
            authority_digest=authority_digest,
            strategy_id=base.strategy_id,
            account_id=base.account_id,
            account_mode=base.account_mode,
            stage=base.stage,
            artifact_digest=base.artifact_digest,
            allowlist_digest=base.allowlist_digest,
            ruleset_digest=base.ruleset_digest,
            max_gross_allocation=base.max_gross_allocation,
            evidence_digest=base.evidence_digest,
            public_key_id=base.public_key_id,
            operator=base.operator,
            reason=base.reason,
            issued_at=base.issued_at,
            expires_at=base.expires_at,
            event=EVENT_ACTIVATED,
            command_id=command_id,
            now=now or self._now(),
        )

    def record_deactivated(
        self,
        authority_digest: str,
        *,
        command_id: str,
        reason: str,
        now: Optional[dt.datetime] = None,
    ) -> None:
        base = self.latest(authority_digest)
        if base is None:
            raise ValueError(f"cannot deactivate unknown authority {authority_digest!r}")
        self._append(
            authority_digest=authority_digest,
            strategy_id=base.strategy_id,
            account_id=base.account_id,
            account_mode=base.account_mode,
            stage=base.stage,
            artifact_digest=base.artifact_digest,
            allowlist_digest=base.allowlist_digest,
            ruleset_digest=base.ruleset_digest,
            max_gross_allocation=base.max_gross_allocation,
            evidence_digest=base.evidence_digest,
            public_key_id=base.public_key_id,
            operator=base.operator,
            reason=reason,
            issued_at=base.issued_at,
            expires_at=base.expires_at,
            event=EVENT_DEACTIVATED,
            command_id=command_id,
            now=now or self._now(),
        )

    def record_revoked(
        self,
        authority_digest: str,
        *,
        reason: str,
        now: Optional[dt.datetime] = None,
    ) -> None:
        base = self.latest(authority_digest)
        if base is None:
            raise ValueError(f"cannot revoke unknown authority {authority_digest!r}")
        self._append(
            authority_digest=authority_digest,
            strategy_id=base.strategy_id,
            account_id=base.account_id,
            account_mode=base.account_mode,
            stage=base.stage,
            artifact_digest=base.artifact_digest,
            allowlist_digest=base.allowlist_digest,
            ruleset_digest=base.ruleset_digest,
            max_gross_allocation=base.max_gross_allocation,
            evidence_digest=base.evidence_digest,
            public_key_id=base.public_key_id,
            operator=base.operator,
            reason=reason,
            issued_at=base.issued_at,
            expires_at=base.expires_at,
            event=EVENT_REVOKED,
            now=now or self._now(),
        )

    def record_superseded(
        self,
        authority_digest: str,
        *,
        superseded_by_digest: str,
        reason: str,
        now: Optional[dt.datetime] = None,
    ) -> None:
        base = self.latest(authority_digest)
        if base is None:
            raise ValueError(f"cannot supersede unknown authority {authority_digest!r}")
        self._append(
            authority_digest=authority_digest,
            strategy_id=base.strategy_id,
            account_id=base.account_id,
            account_mode=base.account_mode,
            stage=base.stage,
            artifact_digest=base.artifact_digest,
            allowlist_digest=base.allowlist_digest,
            ruleset_digest=base.ruleset_digest,
            max_gross_allocation=base.max_gross_allocation,
            evidence_digest=base.evidence_digest,
            public_key_id=base.public_key_id,
            operator=base.operator,
            reason=reason,
            issued_at=base.issued_at,
            expires_at=base.expires_at,
            event=EVENT_SUPERSEDED,
            superseded_by_digest=superseded_by_digest,
            now=now or self._now(),
        )

    def latest(self, authority_digest: str) -> Optional[AllocationAuthorityRecord]:
        row = self.db.execute(
            f"SELECT {_SELECT_COLUMNS} FROM allocation_authorities "
            f"WHERE authority_digest = ? ORDER BY entry_id DESC LIMIT 1",
            [authority_digest],
            fetch="one",
        )
        return _row_to_record(row) if row is not None else None

    def history(self, authority_digest: str) -> list[AllocationAuthorityRecord]:
        rows = self.db.execute(
            f"SELECT {_SELECT_COLUMNS} FROM allocation_authorities "
            f"WHERE authority_digest = ? ORDER BY entry_id ASC",
            [authority_digest],
            fetch="all",
        )
        return [_row_to_record(row) for row in rows or ()]

    def is_revoked(self, authority_digest: str) -> bool:
        record = self.latest(authority_digest)
        return record is not None and record.event == EVENT_REVOKED

    def is_superseded(self, authority_digest: str) -> bool:
        record = self.latest(authority_digest)
        return record is not None and record.event == EVENT_SUPERSEDED

    def revoked_digests(self) -> frozenset[str]:
        rows = self.db.execute(
            "SELECT DISTINCT authority_digest FROM allocation_authorities "
            "WHERE event = ?",
            [EVENT_REVOKED],
            fetch="all",
        )
        candidates = {row[0] for row in rows or ()}
        return frozenset(d for d in candidates if self.is_revoked(d))

    def active_for(
        self,
        account_id: str,
        artifact_digest: str,
        *,
        now: Optional[dt.datetime] = None,
    ) -> Optional[AllocationAuthorityRecord]:
        """Return the currently active authority for an account/artifact pair."""
        resolved_now = _as_utc(now or self._now())
        rows = self.db.execute(
            f"SELECT {_SELECT_COLUMNS} FROM allocation_authorities "
            f"WHERE account_id = ? AND artifact_digest = ? "
            f"ORDER BY entry_id DESC",
            [account_id, artifact_digest],
            fetch="all",
        )
        seen: set[str] = set()
        for row in rows or ():
            record = _row_to_record(row)
            if record.authority_digest in seen:
                continue
            seen.add(record.authority_digest)
            latest = self.latest(record.authority_digest)
            if latest is None:
                continue
            if latest.event in (EVENT_REVOKED, EVENT_SUPERSEDED, EVENT_DEACTIVATED):
                continue
            if _as_utc(latest.expires_at) <= resolved_now:
                continue
            if latest.event not in (EVENT_ISSUED, EVENT_ACTIVATED):
                continue
            return latest
        return None

    def authority_digest_for(
        self,
        attestation: AllocationAttestation,
    ) -> str:
        return allocation_payload_digest(allocation_attestation_to_wire(attestation))
