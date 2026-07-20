"""P4 Task 6 — canary capital-safety incident response and drawdown.

``CanaryRiskController.observe`` consumes authoritative broker snapshots and
canary-scoped attribution, updates durable high-water marks, and on breach or
capital-safety incident executes a fixed, safe action sequence:

persist incident → trip breaker → reject new exposure → cancel entries →
reduce → reconcile → suspend artifact when required.

A drawdown or safety suspension requires a fresh promotion review; clearing
the breaker alone cannot reactivate the artifact.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional, Sequence

from trader.data.domain_journal import DomainJournal
from trader.data.broker_state import BrokerRiskSnapshot
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.trading.circuit_breaker import BreakerSignal, CircuitBreaker

CANARY_RISK_MIGRATION_44 = 44
CANARY_RISK_MIGRATION_VERSIONS = (CANARY_RISK_MIGRATION_44,)
CANARY_RISK_MIGRATION_44_NAME = "p4_canary_risk"

MAX_CANARY_DRAWDOWN_FRACTION = 0.03
MAX_CANARY_DAILY_LOSS_FRACTION = 0.005

CAPITAL_SAFETY_INCIDENT_KINDS = frozenset({
    "MISSING_PROTECTION",
    "DUPLICATE_SUBMISSION",
    "ACCOUNT_MISMATCH",
    "MISSED_FLAT",
    "UNEXPLAINED_POSITION",
    "DRAWDOWN_BREACH",
    "DAILY_LOSS_BREACH",
})

_BREAKER_KIND_FOR_INCIDENT = {
    "MISSING_PROTECTION": "PROTECTIVE_ORDER_FAILURE",
    "DUPLICATE_SUBMISSION": "DUPLICATE_SUBMISSION",
    "ACCOUNT_MISMATCH": "ACCOUNT_MISMATCH",
    "MISSED_FLAT": "MISSED_FLAT_DEADLINE",
    "UNEXPLAINED_POSITION": "UNEXPLAINED_POSITION",
    "DRAWDOWN_BREACH": "DRAWDOWN_BREACH",
    "DAILY_LOSS_BREACH": "DAILY_LOSS_BREACH",
}

_RESPONSE_ACTIONS = (
    "PERSIST_INCIDENT",
    "TRIP_BREAKER",
    "REJECT_NEW_EXPOSURE",
    "CANCEL_ENTRIES",
    "REDUCE",
    "RECONCILE",
    "SUSPEND_ARTIFACT",
)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def apply_canary_risk_migration(migrator: SchemaMigrator) -> bool:
    kinds = ", ".join(f"'{k}'" for k in sorted(CAPITAL_SAFETY_INCIDENT_KINDS))
    return migrator.apply(
        CANARY_RISK_MIGRATION_44,
        CANARY_RISK_MIGRATION_44_NAME,
        (
            """CREATE TABLE IF NOT EXISTS canary_high_water_marks (
                strategy_id VARCHAR NOT NULL,
                account_id VARCHAR NOT NULL,
                high_water_mark DOUBLE NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                revision BIGINT NOT NULL,
                PRIMARY KEY (strategy_id, account_id)
            )""",
            f"""CREATE TABLE IF NOT EXISTS capital_safety_incidents (
                incident_id VARCHAR PRIMARY KEY,
                strategy_id VARCHAR NOT NULL,
                account_id VARCHAR NOT NULL,
                kind VARCHAR NOT NULL CHECK (kind IN ({kinds})),
                detail VARCHAR NOT NULL,
                occurred_at TIMESTAMPTZ NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_capital_safety_incidents_strategy
                ON capital_safety_incidents(strategy_id)""",
        ),
    )


@dataclass(frozen=True)
class CanaryAttributionView:
    """Canary-scoped attribution summary derived from P3 ``TradeAttribution`` rows."""

    net_pnl_after_cost: float
    unresolved_trade_count: int
    commission_total: float = 0.0


def build_canary_attribution_view(
    trades: Sequence[object],
    *,
    unresolved_trade_ids: Sequence[str] = (),
) -> CanaryAttributionView:
    """Build a canary attribution view from resolved trade attribution rows."""
    from trader.automation.attribution import TradeAttribution

    net = Decimal("0")
    commissions = Decimal("0")
    for trade in trades:
        if not isinstance(trade, TradeAttribution):
            raise TypeError(f"expected TradeAttribution, got {type(trade)!r}")
        if not trade.resolved:
            continue
        if trade.net_pnl is not None:
            net += trade.net_pnl
        if trade.total_commission is not None:
            commissions += trade.total_commission
    return CanaryAttributionView(
        net_pnl_after_cost=float(net),
        unresolved_trade_count=len(tuple(unresolved_trade_ids)),
        commission_total=float(commissions),
    )


@dataclass(frozen=True)
class CapitalSafetyIncident:
    kind: str
    detail: str
    occurred_at: dt.datetime
    key: str = ""

    def __post_init__(self) -> None:
        if self.kind not in CAPITAL_SAFETY_INCIDENT_KINDS:
            raise ValueError(f"unknown capital safety incident kind {self.kind!r}")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")


@dataclass(frozen=True)
class CanaryRiskState:
    high_water_mark: float
    drawdown_fraction: float
    daily_loss_fraction: float
    paused: bool
    suspend_required: bool
    reactivation_requires_promotion_review: bool
    incident_count: int
    actions: tuple[str, ...]
    incidents: tuple[CapitalSafetyIncident, ...] = ()


class CanaryRiskStore:
    def __init__(self, journal: DomainJournal, strategy_id: str, account_id: str):
        self.journal = journal
        self.strategy_id = strategy_id
        self.account_id = account_id

    @staticmethod
    def incident_id(strategy_id: str, account_id: str, kind: str, occurred_at: dt.datetime, key: str) -> str:
        raw = f"{strategy_id}|{account_id}|{kind}|{key or occurred_at.isoformat()}".encode()
        return hashlib.sha256(raw).hexdigest()

    def get_high_water_mark(self) -> Optional[float]:
        row = self.journal.connect().execute(
            "SELECT high_water_mark FROM canary_high_water_marks "
            "WHERE strategy_id = ? AND account_id = ?",
            [self.strategy_id, self.account_id],
        ).fetchone()
        return float(row[0]) if row else None

    def update_high_water_mark(self, value: float, now: dt.datetime) -> float:
        current = self.get_high_water_mark()
        new_hwm = max(current or 0.0, value)

        def work(conn, append):
            existing = conn.execute(
                "SELECT revision FROM canary_high_water_marks WHERE strategy_id=? AND account_id=?",
                [self.strategy_id, self.account_id],
            ).fetchone()
            changed = False
            if existing is None:
                conn.execute(
                    "INSERT INTO canary_high_water_marks VALUES (?, ?, ?, ?, ?)",
                    [self.strategy_id, self.account_id, new_hwm, now, 1],
                )
                revision = 1
                changed = True
            elif new_hwm > (current or 0.0):
                revision = int(existing[0]) + 1
                conn.execute(
                    "UPDATE canary_high_water_marks SET high_water_mark=?, updated_at=?, revision=? "
                    "WHERE strategy_id=? AND account_id=?",
                    [new_hwm, now, revision, self.strategy_id, self.account_id],
                )
                changed = True
            else:
                revision = int(existing[0])
            if changed:
                mutation = DomainMutation(
                    event_type="canary.high_water_mark_updated",
                    entity_type="canary_risk",
                    entity_id=f"{self.strategy_id}:{self.account_id}",
                    operation="upsert",
                    account_id=self.account_id,
                    source="trader_service",
                    source_timestamp=now,
                    correlation_id=f"hwm:{self.strategy_id}:{revision}",
                    payload={
                        "strategy_id": self.strategy_id,
                        "high_water_mark": new_hwm,
                        "revision": revision,
                    },
                )
                append(mutation, lambda _c, _r: None, f"hwm:{self.strategy_id}:{revision}")
            return new_hwm

        return self.journal.mutate_batch_work(self.journal.connect(), work)

    def append_incident(self, incident: CapitalSafetyIncident, now: dt.datetime) -> bool:
        incident_id = self.incident_id(
            self.strategy_id, self.account_id, incident.kind, incident.occurred_at, incident.key,
        )

        def work(conn, append):
            exists = conn.execute(
                "SELECT 1 FROM capital_safety_incidents WHERE incident_id=?",
                [incident_id],
            ).fetchone()
            if exists is not None:
                return False
            conn.execute(
                "INSERT INTO capital_safety_incidents VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    incident_id,
                    self.strategy_id,
                    self.account_id,
                    incident.kind,
                    incident.detail,
                    incident.occurred_at,
                    now,
                ],
            )
            mutation = DomainMutation(
                event_type="canary.capital_safety_incident",
                entity_type="canary_risk",
                entity_id=incident_id,
                operation="upsert",
                account_id=self.account_id,
                source="trader_service",
                source_timestamp=now,
                correlation_id=incident_id,
                payload={
                    "incident_id": incident_id,
                    "strategy_id": self.strategy_id,
                    "kind": incident.kind,
                    "detail": incident.detail,
                },
            )
            append(mutation, lambda _c, _r: None, incident_id)
            return True

        return self.journal.mutate_batch_work(self.journal.connect(), work)

    def incident_count(self) -> int:
        row = self.journal.connect().execute(
            "SELECT count(*) FROM capital_safety_incidents WHERE strategy_id=? AND account_id=?",
            [self.strategy_id, self.account_id],
        ).fetchone()
        return int(row[0])


class CanaryRiskController:
    def __init__(
        self,
        *,
        store: CanaryRiskStore,
        breaker: CircuitBreaker,
        strategy_id: str,
        expected_account_id: Optional[str] = None,
    ):
        self.store = store
        self.breaker = breaker
        self.strategy_id = strategy_id
        self.expected_account_id = expected_account_id or store.account_id

    def observe(
        self,
        broker: BrokerRiskSnapshot,
        attribution: CanaryAttributionView,
        *,
        now: dt.datetime,
        permitted_conids: Sequence[int],
    ) -> CanaryRiskState:
        now = _as_utc(now)
        if broker.account_id != self.expected_account_id:
            return self.record_incident(
                CapitalSafetyIncident(
                    kind="ACCOUNT_MISMATCH",
                    detail=f"expected {self.expected_account_id}, got {broker.account_id}",
                    occurred_at=now,
                ),
                broker=broker,
                now=now,
            )

        hwm = self.store.update_high_water_mark(float(broker.net_liquidation), now)
        equity = float(broker.net_liquidation)
        drawdown = max(0.0, (hwm - equity) / hwm) if hwm > 0 else 0.0
        broker_loss = max(0.0, -float(broker.daily_pnl))
        commission_loss = max(0.0, float(attribution.commission_total))
        daily_loss = (broker_loss + commission_loss) / equity if equity > 0 else 0.0

        incidents: list[CapitalSafetyIncident] = []
        for row in broker.positions:
            if int(row.conid) not in set(int(c) for c in permitted_conids):
                incidents.append(CapitalSafetyIncident(
                    kind="UNEXPLAINED_POSITION",
                    detail=f"conid {row.conid} not in allowlist",
                    occurred_at=now,
                    key=f"pos:{row.conid}",
                ))

        if drawdown >= MAX_CANARY_DRAWDOWN_FRACTION:
            incidents.append(CapitalSafetyIncident(
                kind="DRAWDOWN_BREACH",
                detail=f"drawdown={drawdown:.6f}",
                occurred_at=now,
                key=f"dd:{drawdown:.6f}",
            ))
        if daily_loss >= MAX_CANARY_DAILY_LOSS_FRACTION:
            incidents.append(CapitalSafetyIncident(
                kind="DAILY_LOSS_BREACH",
                detail=f"daily_loss={daily_loss:.6f}",
                occurred_at=now,
                key=f"dl:{daily_loss:.6f}",
            ))

        if incidents:
            state = self._empty_state(hwm, drawdown, daily_loss)
            for inc in incidents:
                state = self._merge(self.record_incident(inc, broker=broker, now=now), state)
            return state

        return CanaryRiskState(
            high_water_mark=hwm,
            drawdown_fraction=drawdown,
            daily_loss_fraction=daily_loss,
            paused=False,
            suspend_required=False,
            reactivation_requires_promotion_review=False,
            incident_count=self.store.incident_count(),
            actions=(),
        )

    def record_incident(
        self,
        incident: CapitalSafetyIncident,
        *,
        broker: BrokerRiskSnapshot,
        now: dt.datetime,
    ) -> CanaryRiskState:
        now = _as_utc(now)
        self.store.append_incident(incident, now)
        breaker_kind = _BREAKER_KIND_FOR_INCIDENT[incident.kind]
        self.breaker.record(BreakerSignal(kind=breaker_kind, occurred_at=incident.occurred_at, detail=incident.detail, key=incident.key))

        suspend = incident.kind in {
            "DRAWDOWN_BREACH",
            "DAILY_LOSS_BREACH",
            "MISSING_PROTECTION",
            "UNEXPLAINED_POSITION",
            "MISSED_FLAT",
        }
        hwm = self.store.get_high_water_mark() or float(broker.net_liquidation)
        equity = float(broker.net_liquidation)
        drawdown = max(0.0, (hwm - equity) / hwm) if hwm > 0 else 0.0
        daily_loss = max(0.0, -(float(broker.daily_pnl))) / equity if equity > 0 else 0.0

        return CanaryRiskState(
            high_water_mark=hwm,
            drawdown_fraction=drawdown,
            daily_loss_fraction=daily_loss,
            paused=True,
            suspend_required=suspend,
            reactivation_requires_promotion_review=suspend,
            incident_count=self.store.incident_count(),
            actions=_RESPONSE_ACTIONS,
            incidents=(incident,),
        )

    @staticmethod
    def _empty_state(hwm: float, drawdown: float, daily_loss: float) -> CanaryRiskState:
        return CanaryRiskState(
            high_water_mark=hwm,
            drawdown_fraction=drawdown,
            daily_loss_fraction=daily_loss,
            paused=False,
            suspend_required=False,
            reactivation_requires_promotion_review=False,
            incident_count=0,
            actions=(),
        )

    @staticmethod
    def _merge(latest: CanaryRiskState, prior: CanaryRiskState) -> CanaryRiskState:
        return CanaryRiskState(
            high_water_mark=latest.high_water_mark,
            drawdown_fraction=max(latest.drawdown_fraction, prior.drawdown_fraction),
            daily_loss_fraction=max(latest.daily_loss_fraction, prior.daily_loss_fraction),
            paused=latest.paused or prior.paused,
            suspend_required=latest.suspend_required or prior.suspend_required,
            reactivation_requires_promotion_review=(
                latest.reactivation_requires_promotion_review or prior.reactivation_requires_promotion_review
            ),
            incident_count=latest.incident_count,
            actions=latest.actions or prior.actions,
            incidents=prior.incidents + latest.incidents,
        )
