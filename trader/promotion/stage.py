"""P4 Task 1 — the durable promotion stage state machine.

``strategy_promotion_state`` holds exactly one current row per strategy;
every ``PromotionStageMachine.transition`` call is validated against a
frozen, monotonic edge set and committed atomically with its domain event
via ``DomainJournal.mutate`` (mirroring ``AttributionLedger``/
``ProtectiveOrderSaga`` in P3). The full transition history therefore lives
in ``domain_event_journal``, not duplicated here — this table is a
materialized "current stage" view, the same relationship
``trade_attribution`` has to ``automation_decisions``.

There is no timer, cron, or elapsed-time path anywhere in this module:
every transition requires an explicit ``reason`` and ``actor`` from a
caller (paper gate, live metrics, canary risk, promotion controller, or an
operator action). Reading ``current_stage`` — or letting evidence
accumulate via ``EvidenceStore`` — never mutates state.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.domain.identity import strategy_entity_id
from trader.promotion.evidence_store import EvidenceStore, EvidenceWindow

STAGE_MIGRATION_42 = 42
STAGE_MIGRATION_VERSIONS = (STAGE_MIGRATION_42,)
STAGE_MIGRATION_42_NAME = "p4_strategy_promotion_state"

# Frozen stage names — exact strings, verbatim per plan Task 1.
PAPER_COLLECTING = "PAPER_COLLECTING"
PAPER_FAILED = "PAPER_FAILED"
PAPER_PASSED = "PAPER_PASSED"
CANARY_AUTHORIZED = "CANARY_AUTHORIZED"
CANARY_ACTIVE = "CANARY_ACTIVE"
CANARY_SUSPENDED = "CANARY_SUSPENDED"
CANARY_PASSED = "CANARY_PASSED"

STAGES = (
    PAPER_COLLECTING, PAPER_FAILED, PAPER_PASSED,
    CANARY_AUTHORIZED, CANARY_ACTIVE, CANARY_SUSPENDED, CANARY_PASSED,
)

# Monotonic, frozen edge set. ``None`` is the bootstrap "no row yet" state
# — every strategy starts life in PAPER_COLLECTING. There is deliberately
# NO edge from any PAPER_* stage directly to CANARY_ACTIVE/CANARY_SUSPENDED/
# CANARY_PASSED (no direct paper-to-live path — every live activation must
# pass through CANARY_AUTHORIZED first), and NO edge from CANARY_SUSPENDED
# back to CANARY_ACTIVE (a suspension always requires a fresh promotion
# review, i.e. a brand-new CANARY_AUTHORIZED authority — a breaker reset
# alone can never reactivate).
_ALLOWED_EDGES: dict[Optional[str], frozenset[str]] = {
    None: frozenset({PAPER_COLLECTING}),
    PAPER_COLLECTING: frozenset({PAPER_FAILED, PAPER_PASSED}),
    PAPER_FAILED: frozenset({PAPER_COLLECTING}),
    PAPER_PASSED: frozenset({CANARY_AUTHORIZED}),
    CANARY_AUTHORIZED: frozenset({CANARY_ACTIVE, PAPER_PASSED}),
    CANARY_ACTIVE: frozenset({CANARY_SUSPENDED, CANARY_PASSED}),
    CANARY_SUSPENDED: frozenset({CANARY_AUTHORIZED}),
    CANARY_PASSED: frozenset(),
}

# Stages that certify evidence as "good enough to move forward" and
# therefore MANDATE a store-backed, freshly-projected, clean evidence window
# (not stale, no breaker trip, no cost breach, no drawdown breach since the
# last correction). Tasks 2/3/6 own the concrete simultaneous floors (day/
# session/round-trip/instrument counts); this machine only refuses to
# rubber-stamp a passed/authorized/canary-passed stage without real, current,
# STORE-PROJECTED evidence -- omission is a rejection, never trusted, and a
# caller-constructed ``EvidenceWindow`` is never accepted as sufficient proof
# on its own (that would let a forged clean window rubber-stamp the gate --
# see ``EvidenceStore``/``_require_clean_evidence`` below). Software
# completion alone (a bare reason/actor with no evidence_store) can never
# mark a paper or live gate passed.
_REQUIRES_CLEAN_EVIDENCE = frozenset({PAPER_PASSED, CANARY_AUTHORIZED, CANARY_PASSED})

_WINDOW_COMPARISON_FIELDS = (
    "strategy_id", "window_reset_at", "first_event_at", "last_event_at",
    "calendar_days", "session_ids", "round_trip_ids", "instrument_ids",
    "corrections", "breaker_trips", "cost_breaches", "drawdown_breaches",
    "stale", "event_count",
)


def _window_matches_projection(window: EvidenceWindow, projected: EvidenceWindow) -> bool:
    """True iff a caller-supplied ``EvidenceWindow``'s substantive content is
    identical to the store's own fresh projection (every field except
    ``as_of``, which legitimately differs by call time). Used only as a
    defense-in-depth integrity check when a caller *chooses* to also pass
    ``evidence_window=`` alongside the mandatory ``evidence_store=`` -- the
    projection, never the caller's copy, is what actually gets evaluated for
    cleanliness."""
    return all(
        getattr(window, field) == getattr(projected, field)
        for field in _WINDOW_COMPARISON_FIELDS
    )


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


class IllegalStageTransition(Exception):
    """Raised for any edge outside the frozen, monotonic transition set —
    including any attempt at a direct paper-to-live path or a bare
    suspended-to-active reactivation."""

    def __init__(self, strategy_id: str, from_stage: Optional[str], to_stage: str):
        self.strategy_id = strategy_id
        self.from_stage = from_stage
        self.to_stage = to_stage
        super().__init__(
            f"strategy {strategy_id!r} cannot transition {from_stage!r} -> {to_stage!r}"
        )


class EvidenceNotCleanError(Exception):
    """Raised when a transition into a clean-evidence-required stage
    (PAPER_PASSED/CANARY_AUTHORIZED/CANARY_PASSED) cannot be trusted:
    ``evidence_store`` is missing entirely (a bare ``evidence_window=`` is
    never sufficient on its own -- that is the exact forged-window bypass
    this error exists to close), a caller-supplied ``evidence_window``
    disagrees with the store's own fresh projection, or the store-projected
    window is stale/carries breaker/cost/drawdown evidence. Any one of these
    fails the whole transition closed -- omission is never trusted."""

    def __init__(self, strategy_id: str, to_stage: str, reasons: tuple[str, ...]):
        self.strategy_id = strategy_id
        self.to_stage = to_stage
        self.reasons = reasons
        super().__init__(
            f"strategy {strategy_id!r} evidence is not clean for {to_stage!r}: "
            f"{', '.join(reasons)}"
        )


class AuthorityRequiredError(Exception):
    """Raised when authorizing or activating canary without a supplied (or
    previously recorded) authority reference and expiry."""

    def __init__(self, strategy_id: str):
        super().__init__(
            f"strategy {strategy_id!r} requires authority_ref and authority_expiry"
        )


class AuthorityMismatchError(Exception):
    """Raised when activation is attempted with an authority reference or
    expiry that differs from the one recorded at CANARY_AUTHORIZED — the
    authority has "changed" and must not silently be honored."""

    def __init__(self, strategy_id: str):
        super().__init__(
            f"strategy {strategy_id!r} activation authority does not match the "
            f"authorized authority"
        )


class AuthorityExpiredError(Exception):
    """Raised when activation is attempted after the recorded authority's
    expiry has passed."""

    def __init__(self, strategy_id: str, expiry: dt.datetime, now: dt.datetime):
        self.expiry = expiry
        self.now = now
        super().__init__(
            f"strategy {strategy_id!r} authority expired at {expiry.isoformat()} "
            f"(now {now.isoformat()})"
        )


def apply_stage_migration(migrator: SchemaMigrator) -> bool:
    """Journal migration 42: durable current-stage row per strategy."""
    stage_literals = ", ".join(f"'{stage}'" for stage in STAGES)
    return migrator.apply(
        STAGE_MIGRATION_42,
        STAGE_MIGRATION_42_NAME,
        (
            f"""CREATE TABLE IF NOT EXISTS strategy_promotion_state (
                strategy_id VARCHAR PRIMARY KEY,
                stage VARCHAR NOT NULL CHECK (stage IN ({stage_literals})),
                prior_stage VARCHAR,
                reason VARCHAR NOT NULL,
                actor VARCHAR NOT NULL,
                authority_ref VARCHAR,
                authority_expiry TIMESTAMPTZ,
                evidence_digest VARCHAR,
                revision BIGINT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )""",
        ),
    )


@dataclass(frozen=True)
class StageRecord:
    """A single, current, durable snapshot of a strategy's promotion stage."""
    strategy_id: str
    stage: str
    prior_stage: Optional[str]
    reason: str
    actor: str
    authority_ref: Optional[str]
    authority_expiry: Optional[dt.datetime]
    evidence_digest: Optional[str]
    revision: int
    updated_at: dt.datetime

    def to_payload(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "stage": self.stage,
            "prior_stage": self.prior_stage,
            "reason": self.reason,
            "actor": self.actor,
            "authority_ref": self.authority_ref,
            "authority_expiry": (
                self.authority_expiry.isoformat() if self.authority_expiry is not None else None
            ),
            "evidence_digest": self.evidence_digest,
            "revision": self.revision,
            "updated_at": self.updated_at.isoformat(),
        }


class PromotionStageMachineStore:
    """Persistence for the current-stage row per strategy."""

    def __init__(self, db: Any):
        self.db = db

    def load(self, strategy_id: str) -> Optional[StageRecord]:
        row = self.db.execute(
            "SELECT strategy_id, stage, prior_stage, reason, actor, authority_ref, "
            "authority_expiry, evidence_digest, revision, updated_at "
            "FROM strategy_promotion_state WHERE strategy_id = ?",
            [strategy_id],
            fetch="one",
        )
        if row is None:
            return None
        return StageRecord(
            strategy_id=row[0],
            stage=row[1],
            prior_stage=row[2],
            reason=row[3],
            actor=row[4],
            authority_ref=row[5],
            authority_expiry=_as_utc(row[6]) if row[6] is not None else None,
            evidence_digest=row[7],
            revision=row[8],
            updated_at=_as_utc(row[9]),
        )

    def save_in_tx(self, conn: Any, record: StageRecord) -> None:
        conn.execute(
            "DELETE FROM strategy_promotion_state WHERE strategy_id = ?",
            [record.strategy_id],
        )
        conn.execute(
            "INSERT INTO strategy_promotion_state "
            "(strategy_id, stage, prior_stage, reason, actor, authority_ref, "
            "authority_expiry, evidence_digest, revision, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                record.strategy_id,
                record.stage,
                record.prior_stage,
                record.reason,
                record.actor,
                record.authority_ref,
                record.authority_expiry,
                record.evidence_digest,
                record.revision,
                record.updated_at,
            ],
        )


class PromotionStageMachine:
    """Durable, explicit-only promotion stage transitions.

    ``transition`` is the sole write path. It (1) validates the requested
    edge against the frozen ``_ALLOWED_EDGES`` map, (2) for a target stage
    that requires clean evidence (PAPER_PASSED/CANARY_AUTHORIZED/
    CANARY_PASSED), MANDATES a caller-supplied ``evidence_store`` and
    recomputes ``evidence_store.project(strategy_id, as_of=now)`` itself --
    a caller-constructed ``EvidenceWindow`` is never trusted as sufficient
    proof by itself, closing the "forged clean window" bypass -- refusing to
    advance if the store is missing, an optionally-supplied ``evidence_window``
    disagrees with the store's own projection, or the projection is stale or
    carries breaker/cost/drawdown evidence, (3) for canary authorization/
    activation, records and then verifies authority reference + expiry
    exactly, and (4) persists the new ``StageRecord`` and its domain event
    atomically.
    """

    def __init__(self, journal: Any, db: Any, now: Optional[Callable[[], dt.datetime]] = None):
        self.journal = journal
        self.db = db
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self._store = PromotionStageMachineStore(db)

    @property
    def store(self) -> PromotionStageMachineStore:
        return self._store

    def current_stage(self, strategy_id: str) -> Optional[str]:
        record = self._store.load(strategy_id)
        return record.stage if record is not None else None

    def transition(
        self,
        strategy_id: str,
        to_stage: str,
        *,
        reason: str,
        actor: str,
        now: Optional[dt.datetime] = None,
        evidence_store: Optional[EvidenceStore] = None,
        evidence_window: Optional[EvidenceWindow] = None,
        authority_ref: Optional[str] = None,
        authority_expiry: Optional[dt.datetime] = None,
        evidence_digest: Optional[str] = None,
    ) -> StageRecord:
        if to_stage not in STAGES:
            raise ValueError(f"unknown stage {to_stage!r}; must be one of {STAGES}")
        if not reason:
            raise ValueError("reason is required")
        if not actor:
            raise ValueError("actor is required")

        resolved_now = _as_utc(now) if now is not None else _as_utc(self._now())
        current = self._store.load(strategy_id)
        from_stage = current.stage if current is not None else None

        allowed = _ALLOWED_EDGES.get(from_stage, frozenset())
        if to_stage not in allowed:
            raise IllegalStageTransition(strategy_id, from_stage, to_stage)

        if to_stage in _REQUIRES_CLEAN_EVIDENCE:
            self._require_clean_evidence(
                strategy_id, to_stage, evidence_store, evidence_window, resolved_now,
            )

        next_authority_ref = current.authority_ref if current is not None else None
        next_authority_expiry = current.authority_expiry if current is not None else None

        if to_stage == CANARY_AUTHORIZED:
            next_authority_ref, next_authority_expiry = self._record_authority(
                strategy_id, authority_ref, authority_expiry,
            )
        elif to_stage == CANARY_ACTIVE:
            next_authority_ref, next_authority_expiry = self._verify_authority_for_activation(
                strategy_id, current, authority_ref, authority_expiry, resolved_now,
            )
        elif to_stage in (PAPER_COLLECTING, PAPER_FAILED, PAPER_PASSED):
            # Leaving the canary lineage clears any stale authority so a
            # later CANARY_AUTHORIZED transition can never inherit a
            # reference bound to an earlier, unrelated review.
            next_authority_ref = None
            next_authority_expiry = None

        record = StageRecord(
            strategy_id=strategy_id,
            stage=to_stage,
            prior_stage=from_stage,
            reason=reason,
            actor=actor,
            authority_ref=next_authority_ref,
            authority_expiry=next_authority_expiry,
            evidence_digest=evidence_digest,
            revision=(current.revision if current is not None else 0) + 1,
            updated_at=resolved_now,
        )

        self._persist(record)
        return record

    # -- validation helpers -------------------------------------------------

    @staticmethod
    def _require_clean_evidence(
        strategy_id: str,
        to_stage: str,
        evidence_store: Optional[EvidenceStore],
        evidence_window: Optional[EvidenceWindow],
        now: dt.datetime,
    ) -> None:
        reasons: list[str] = []
        if evidence_store is None:
            # A caller-constructed EvidenceWindow is NEVER sufficient proof
            # by itself, no matter how clean it claims to be -- without a
            # store to project from there is nothing to bind that claim to.
            # This is the exact forged-window bypass this check exists to
            # close: omission (or a bare evidence_window with no store) is
            # always a rejection, never trusted.
            reasons.append("missing_evidence_store")
        else:
            projected = evidence_store.project(strategy_id, as_of=now)
            if evidence_window is not None and not _window_matches_projection(evidence_window, projected):
                # A caller-supplied window that disagrees with the store's
                # own projection is untrustworthy regardless of its own
                # content -- the projection below, not the caller's copy,
                # is what actually gets evaluated for cleanliness.
                reasons.append("evidence_window_mismatch")
            if projected.stale:
                reasons.append("stale_evidence")
            if projected.breaker_trips:
                reasons.append("breaker_trip")
            if projected.cost_breaches:
                reasons.append("cost_breach")
            if projected.drawdown_breaches:
                reasons.append("drawdown_breach")
        if reasons:
            raise EvidenceNotCleanError(strategy_id, to_stage, tuple(reasons))

    @staticmethod
    def _record_authority(
        strategy_id: str,
        authority_ref: Optional[str],
        authority_expiry: Optional[dt.datetime],
    ) -> tuple[str, dt.datetime]:
        if authority_ref is None or authority_expiry is None:
            raise AuthorityRequiredError(strategy_id)
        return authority_ref, _as_utc(authority_expiry)

    @staticmethod
    def _verify_authority_for_activation(
        strategy_id: str,
        current: Optional[StageRecord],
        authority_ref: Optional[str],
        authority_expiry: Optional[dt.datetime],
        now: dt.datetime,
    ) -> tuple[str, dt.datetime]:
        stored_ref = current.authority_ref if current is not None else None
        stored_expiry = current.authority_expiry if current is not None else None
        if stored_ref is None or stored_expiry is None:
            raise AuthorityRequiredError(strategy_id)
        if authority_ref is None or authority_expiry is None:
            raise AuthorityRequiredError(strategy_id)
        if authority_ref != stored_ref or _as_utc(authority_expiry) != stored_expiry:
            raise AuthorityMismatchError(strategy_id)
        if stored_expiry <= now:
            raise AuthorityExpiredError(strategy_id, stored_expiry, now)
        return stored_ref, stored_expiry

    # -- persistence ----------------------------------------------------------

    def _persist(self, record: StageRecord) -> None:
        mutation = DomainMutation(
            event_type="promotion.stage_transitioned",
            entity_type="strategy_promotion_state",
            entity_id=strategy_entity_id(record.strategy_id),
            operation="upsert",
            account_id=None,
            source="trader_service",
            source_timestamp=record.updated_at,
            correlation_id=record.strategy_id,
            payload=record.to_payload(),
        )

        def write(conn: Any, _revision: int) -> None:
            self._store.save_in_tx(conn, record)

        self.journal.mutate(
            self.journal.connect(),
            mutation,
            write,
            event_id=f"promo-stage:{record.strategy_id}:{record.revision}",
        )
