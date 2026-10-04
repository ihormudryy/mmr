"""P5 Task 7 — signed portfolio risk authority (migration 53).

Absence leaves single-strategy behavior unchanged. Presence is required before
enabling a second live strategy under the combined portfolio risk budget.
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation

PORTFOLIO_RISK_AUTHORITY_MIGRATION_53 = 53
PORTFOLIO_RISK_AUTHORITY_MIGRATION_VERSIONS = (PORTFOLIO_RISK_AUTHORITY_MIGRATION_53,)
PORTFOLIO_RISK_AUTHORITY_MIGRATION_53_NAME = "p5_portfolio_risk_authority"

EVENT_ISSUED = "ISSUED"
EVENT_ACTIVATED = "ACTIVATED"
EVENT_REVOKED = "REVOKED"
EVENT_SUPERSEDED = "SUPERSEDED"

_ACTIVE_EVENTS = frozenset({EVENT_ISSUED, EVENT_ACTIVATED})
_AUTHORITY_EVENTS = frozenset({EVENT_ISSUED, EVENT_ACTIVATED, EVENT_REVOKED, EVENT_SUPERSEDED})


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def apply_portfolio_risk_authority_migrations(migrator: SchemaMigrator) -> bool:
    event_literals = ", ".join(f"'{e}'" for e in sorted(_AUTHORITY_EVENTS))
    return migrator.apply(
        PORTFOLIO_RISK_AUTHORITY_MIGRATION_53,
        PORTFOLIO_RISK_AUTHORITY_MIGRATION_53_NAME,
        (
            "CREATE SEQUENCE IF NOT EXISTS portfolio_risk_authorities_seq START 1",
            f"""CREATE TABLE portfolio_risk_authorities (
                entry_id BIGINT PRIMARY KEY DEFAULT nextval('portfolio_risk_authorities_seq'),
                authority_digest VARCHAR NOT NULL,
                account_id VARCHAR NOT NULL,
                max_strategies INTEGER NOT NULL,
                max_combined_gross DOUBLE NOT NULL,
                daily_loss_limit DOUBLE NOT NULL,
                operator VARCHAR NOT NULL,
                reason VARCHAR NOT NULL,
                issued_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                event VARCHAR NOT NULL CHECK (event IN ({event_literals})),
                command_id VARCHAR,
                recorded_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_portfolio_risk_authorities_account
                ON portfolio_risk_authorities(account_id)""",
            """CREATE INDEX IF NOT EXISTS idx_portfolio_risk_authorities_digest
                ON portfolio_risk_authorities(authority_digest)""",
            "CREATE SEQUENCE IF NOT EXISTS portfolio_risk_utilization_seq START 1",
            """CREATE TABLE portfolio_risk_utilization_events (
                event_id BIGINT PRIMARY KEY DEFAULT nextval('portfolio_risk_utilization_seq'),
                account_id VARCHAR NOT NULL,
                authority_digest VARCHAR NOT NULL,
                combined_gross DOUBLE NOT NULL,
                daily_loss_pct DOUBLE NOT NULL,
                strategy_count INTEGER NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_portfolio_risk_utilization_account
                ON portfolio_risk_utilization_events(account_id)""",
        ),
    )


@dataclass(frozen=True)
class PortfolioRiskAuthorityRecord:
    entry_id: int
    authority_digest: str
    account_id: str
    max_strategies: int
    max_combined_gross: float
    daily_loss_limit: float
    operator: str
    reason: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    event: str
    command_id: Optional[str]
    recorded_at: dt.datetime

    @property
    def max_gross_allocation(self) -> float:
        return self.max_combined_gross


_SELECT = (
    "entry_id, authority_digest, account_id, max_strategies, max_combined_gross, "
    "daily_loss_limit, operator, reason, issued_at, expires_at, event, command_id, recorded_at"
)


def _row_to_record(row: Sequence[Any]) -> PortfolioRiskAuthorityRecord:
    return PortfolioRiskAuthorityRecord(
        entry_id=row[0],
        authority_digest=row[1],
        account_id=row[2],
        max_strategies=int(row[3]),
        max_combined_gross=float(row[4]),
        daily_loss_limit=float(row[5]),
        operator=row[6],
        reason=row[7],
        issued_at=_as_utc(row[8]),
        expires_at=_as_utc(row[9]),
        event=row[10],
        command_id=row[11],
        recorded_at=_as_utc(row[12]),
    )


class PortfolioRiskAuthorityStore:
    """Append-only portfolio risk authority ledger (migration 53)."""

    def __init__(
        self,
        journal: DomainJournal,
        db: Any,
        now: Optional[Callable[[], dt.datetime]] = None,
    ):
        self.journal = journal
        self.db = db
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def record_issued(
        self,
        *,
        authority_digest: str,
        account_id: str,
        max_strategies: int,
        max_combined_gross: float,
        daily_loss_limit: float,
        operator: str,
        reason: str,
        issued_at: dt.datetime,
        expires_at: dt.datetime,
        now: Optional[dt.datetime] = None,
    ) -> PortfolioRiskAuthorityRecord:
        return self._append(
            authority_digest=authority_digest,
            account_id=account_id,
            max_strategies=max_strategies,
            max_combined_gross=max_combined_gross,
            daily_loss_limit=daily_loss_limit,
            operator=operator,
            reason=reason,
            issued_at=issued_at,
            expires_at=expires_at,
            event=EVENT_ISSUED,
            now=now or self._now(),
        )

    def record_activated(
        self,
        authority_digest: str,
        *,
        command_id: str,
        now: Optional[dt.datetime] = None,
    ) -> PortfolioRiskAuthorityRecord:
        base = self.latest(authority_digest)
        if base is None:
            raise ValueError(f"unknown portfolio authority {authority_digest!r}")
        return self._append(
            authority_digest=authority_digest,
            account_id=base.account_id,
            max_strategies=base.max_strategies,
            max_combined_gross=base.max_combined_gross,
            daily_loss_limit=base.daily_loss_limit,
            operator=base.operator,
            reason=base.reason,
            issued_at=base.issued_at,
            expires_at=base.expires_at,
            event=EVENT_ACTIVATED,
            command_id=command_id,
            now=now or self._now(),
        )

    def record_revoked(
        self,
        authority_digest: str,
        *,
        reason: str,
        now: Optional[dt.datetime] = None,
    ) -> PortfolioRiskAuthorityRecord:
        base = self.latest(authority_digest)
        if base is None:
            raise ValueError(f"unknown portfolio authority {authority_digest!r}")
        return self._append(
            authority_digest=authority_digest,
            account_id=base.account_id,
            max_strategies=base.max_strategies,
            max_combined_gross=base.max_combined_gross,
            daily_loss_limit=base.daily_loss_limit,
            operator=base.operator,
            reason=reason,
            issued_at=base.issued_at,
            expires_at=base.expires_at,
            event=EVENT_REVOKED,
            now=now or self._now(),
        )

    def latest(self, authority_digest: str) -> Optional[PortfolioRiskAuthorityRecord]:
        row = self.db.execute(
            f"SELECT {_SELECT} FROM portfolio_risk_authorities "
            f"WHERE authority_digest = ? ORDER BY entry_id DESC LIMIT 1",
            [authority_digest],
            fetch="one",
        )
        return _row_to_record(row) if row is not None else None

    def active_for(
        self,
        account_id: str,
        *,
        now: Optional[dt.datetime] = None,
    ) -> Optional[PortfolioRiskAuthorityRecord]:
        resolved_now = _as_utc(now or self._now())
        rows = self.db.execute(
            f"SELECT {_SELECT} FROM portfolio_risk_authorities "
            f"WHERE account_id = ? ORDER BY entry_id DESC",
            [account_id],
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
            if latest.event in (EVENT_REVOKED, EVENT_SUPERSEDED):
                continue
            if _as_utc(latest.expires_at) <= resolved_now:
                continue
            if latest.event not in _ACTIVE_EVENTS:
                continue
            return latest
        return None

    def record_utilization(
        self,
        *,
        account_id: str,
        authority_digest: str,
        combined_gross: float,
        daily_loss_pct: float,
        strategy_count: int,
        now: Optional[dt.datetime] = None,
    ) -> None:
        recorded_at = _as_utc(now or self._now())
        self.db.execute(
            "INSERT INTO portfolio_risk_utilization_events "
            "(account_id, authority_digest, combined_gross, daily_loss_pct, "
            "strategy_count, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                account_id,
                authority_digest,
                float(combined_gross),
                float(daily_loss_pct),
                int(strategy_count),
                recorded_at,
            ],
            fetch="none",
        )

    def _append(
        self,
        *,
        authority_digest: str,
        account_id: str,
        max_strategies: int,
        max_combined_gross: float,
        daily_loss_limit: float,
        operator: str,
        reason: str,
        issued_at: dt.datetime,
        expires_at: dt.datetime,
        event: str,
        command_id: Optional[str] = None,
        now: dt.datetime,
    ) -> PortfolioRiskAuthorityRecord:
        if event not in _AUTHORITY_EVENTS:
            raise ValueError(f"event must be one of {_AUTHORITY_EVENTS}, got {event!r}")
        recorded_at = _as_utc(now)
        captured: list[PortfolioRiskAuthorityRecord] = []

        def write(conn: Any, _revision: int) -> None:
            row = conn.execute(
                f"INSERT INTO portfolio_risk_authorities "
                f"(authority_digest, account_id, max_strategies, max_combined_gross, "
                f"daily_loss_limit, operator, reason, issued_at, expires_at, event, "
                f"command_id, recorded_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING {_SELECT}",
                [
                    authority_digest,
                    account_id,
                    int(max_strategies),
                    float(max_combined_gross),
                    float(daily_loss_limit),
                    operator,
                    reason,
                    _as_utc(issued_at),
                    _as_utc(expires_at),
                    event,
                    command_id,
                    recorded_at,
                ],
            ).fetchone()
            captured.append(_row_to_record(row))

        mutation = DomainMutation(
            event_type="promotion.portfolio_risk_authority_recorded",
            entity_type="portfolio_risk_authority",
            entity_id=f"account:{account_id}",
            operation="upsert",
            account_id=account_id,
            source="trader_service",
            source_timestamp=recorded_at,
            correlation_id=command_id or authority_digest,
            payload={
                "authority_digest": authority_digest,
                "event": event,
                "command_id": command_id,
            },
        )
        self.journal.mutate(
            self.journal.connect(),
            mutation,
            write,
            event_id=f"portfolio-risk-authority:{authority_digest}:{event}:{uuid.uuid4().hex}",
        )
        return captured[0]
