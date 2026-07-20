"""P4 Task 5 -- ``PromotionController`` (offline canary preparation) and the
authenticated, trader-side ``activate_live_canary``/``deactivate_live_canary``
commands.

Two halves, deliberately separated by an air gap:

* ``PromotionController.prepare_canary`` runs OFFLINE (no trader_service, no
  broker, no private key). It consumes ONLY a strategy's current, freshly
  re-projected PASSED paper-evidence window and produces a DISTINCT unsigned
  canary payload (``trader.promotion.canary_attestation.build_canary_payload``)
  -- an operator then signs that payload with their own Ed25519 private key,
  entirely outside this process.
* ``CanaryActivationService`` runs INSIDE trader_service and is registered on
  ``TradingCommandCoordinator`` exactly like every other production command
  (``pause_trading``/``resume_trading``/``approve_proposal``, ...). It never
  sees, needs, or could use a private key -- it only VERIFIES a
  caller-submitted, already-signed ``CanaryAttestation`` (public material
  only) against the trader's own trust store, then drives
  ``PromotionStageMachine`` through CANARY_AUTHORIZED -> CANARY_ACTIVE (or
  CANARY_ACTIVE -> CANARY_SUSPENDED for deactivation).

Neither command dispatches a broker order by itself -- they only toggle
whether the already-verified artifact is PERMITTED to trade the live
account. Every actual order the strategy places afterward still runs
through the existing, separate dispatch/risk-gate/breaker path; this module
only answers "is canary authority currently active for this strategy",
never "should this specific order go out."

Journal migration 43: append-only ``live_activation_authority`` -- every
lifecycle event (ISSUED / ACTIVATED / DEACTIVATED / REVOKED) for an
authority is a NEW row, never an UPDATE/DELETE, so the full history --
exact account, artifact, attestation identity, risk policy, allowlist,
gross allocation, operator, issue/expiry, and the activation command that
consumed it -- is permanently auditable.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.domain.identity import strategy_entity_id
from trader.promotion.allocation_attestation import (
    AllocationAttestation,
    AllocationAttestationVerifier,
    AllocationAuthorityError,
    ExpectedAllocationBindings,
    VerifiedAllocationAuthority,
    build_allocation_payload,
    allocation_attestation_from_wire,
)
from trader.promotion.canary_attestation import (
    CanaryAttestation,
    CanaryAuthorityError,
    CanaryAuthorityVerifier,
    ExpectedCanaryBindings,
    VerifiedCanaryAuthority,
    build_canary_payload,
    canary_attestation_from_wire,
)
from trader.promotion.evidence_store import EvidenceStore
from trader.promotion.paper_gate import PaperGate
from trader.promotion.scaling_gate import ScalingGate
from trader.promotion.stage import (
    CANARY_ACTIVE,
    CANARY_AUTHORIZED,
    CANARY_SUSPENDED,
    PAPER_PASSED,
    AuthorityExpiredError,
    AuthorityMismatchError,
    AuthorityRequiredError,
    EvidenceNotCleanError,
    IllegalStageTransition,
    PromotionStageMachine,
)
from trader.trading.command_coordinator import CommandValidationError

CANARY_AUTHORITY_MIGRATION_43 = 43
CANARY_AUTHORITY_MIGRATION_VERSIONS = (CANARY_AUTHORITY_MIGRATION_43,)
CANARY_AUTHORITY_MIGRATION_43_NAME = "p4_live_activation_authority"

EVENT_ISSUED = "ISSUED"
EVENT_ACTIVATED = "ACTIVATED"
EVENT_DEACTIVATED = "DEACTIVATED"
EVENT_REVOKED = "REVOKED"
AUTHORITY_EVENTS = (EVENT_ISSUED, EVENT_ACTIVATED, EVENT_DEACTIVATED, EVENT_REVOKED)

# Activation/deactivation may only be triggered by an explicit, human
# operator action -- never by any automated pipeline. The RPC handler
# (production_api.py) always stamps this source; the service independently
# re-checks it (defense-in-depth) so a coordinator.execute() call reachable
# from anywhere else in the codebase cannot silently activate live canary
# trading as a side effect.
REQUIRED_ACTIVATION_SOURCE = "operator"


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Migration 43 + the append-only authority ledger
# --------------------------------------------------------------------------- #
def apply_live_activation_authority_migration(migrator: SchemaMigrator) -> bool:
    """Journal migration 43: append-only canary-activation-authority ledger."""
    event_literals = ", ".join(f"'{event}'" for event in AUTHORITY_EVENTS)
    return migrator.apply(
        CANARY_AUTHORITY_MIGRATION_43,
        CANARY_AUTHORITY_MIGRATION_43_NAME,
        (
            "CREATE SEQUENCE IF NOT EXISTS live_activation_authority_seq START 1",
            f"""CREATE TABLE IF NOT EXISTS live_activation_authority (
                entry_id BIGINT PRIMARY KEY DEFAULT nextval('live_activation_authority_seq'),
                authority_digest VARCHAR NOT NULL,
                strategy_id VARCHAR NOT NULL,
                account_id VARCHAR NOT NULL,
                account_mode VARCHAR NOT NULL,
                artifact_digest VARCHAR NOT NULL,
                allowlist_digest VARCHAR NOT NULL,
                ruleset_digest VARCHAR NOT NULL,
                max_gross_allocation DOUBLE NOT NULL,
                permitted_instruments VARCHAR NOT NULL,
                public_key_id VARCHAR NOT NULL,
                operator VARCHAR NOT NULL,
                reason VARCHAR NOT NULL,
                issued_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                event VARCHAR NOT NULL CHECK (event IN ({event_literals})),
                command_id VARCHAR,
                recorded_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_live_activation_authority_digest
                ON live_activation_authority(authority_digest)""",
            """CREATE INDEX IF NOT EXISTS idx_live_activation_authority_strategy
                ON live_activation_authority(strategy_id)""",
        ),
    )


@dataclass(frozen=True)
class AuthorityLedgerEntry:
    entry_id: int
    authority_digest: str
    strategy_id: str
    account_id: str
    account_mode: str
    artifact_digest: str
    allowlist_digest: str
    ruleset_digest: str
    max_gross_allocation: float
    permitted_instruments: tuple
    public_key_id: str
    operator: str
    reason: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    event: str
    command_id: Optional[str]
    recorded_at: dt.datetime

    def to_payload(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "authority_digest": self.authority_digest,
            "strategy_id": self.strategy_id,
            "account_id": self.account_id,
            "account_mode": self.account_mode,
            "artifact_digest": self.artifact_digest,
            "allowlist_digest": self.allowlist_digest,
            "ruleset_digest": self.ruleset_digest,
            "max_gross_allocation": self.max_gross_allocation,
            "permitted_instruments": list(self.permitted_instruments),
            "public_key_id": self.public_key_id,
            "operator": self.operator,
            "reason": self.reason,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "event": self.event,
            "command_id": self.command_id,
            "recorded_at": self.recorded_at.isoformat(),
        }


def _row_to_entry(row: Sequence[Any]) -> AuthorityLedgerEntry:
    return AuthorityLedgerEntry(
        entry_id=row[0],
        authority_digest=row[1],
        strategy_id=row[2],
        account_id=row[3],
        account_mode=row[4],
        artifact_digest=row[5],
        allowlist_digest=row[6],
        ruleset_digest=row[7],
        max_gross_allocation=float(row[8]),
        permitted_instruments=tuple(json.loads(row[9])),
        public_key_id=row[10],
        operator=row[11],
        reason=row[12],
        issued_at=_as_utc(row[13]),
        expires_at=_as_utc(row[14]),
        event=row[15],
        command_id=row[16],
        recorded_at=_as_utc(row[17]),
    )


_SELECT_COLUMNS = (
    "entry_id, authority_digest, strategy_id, account_id, account_mode, "
    "artifact_digest, allowlist_digest, ruleset_digest, max_gross_allocation, "
    "permitted_instruments, public_key_id, operator, reason, issued_at, "
    "expires_at, event, command_id, recorded_at"
)


class LiveActivationAuthorityStore:
    """Append-only persistence for ``live_activation_authority`` (migration 43).

    Every method APPENDS a new row and commits it atomically with a
    ``promotion.live_activation_authority_recorded`` domain event via
    ``DomainJournal.mutate`` -- there is no update/delete anywhere in this
    class. "Current" state for a given authority is always derived by
    reading the latest row for its digest (``latest``), exactly like
    ``promotion_evidence_events`` -> ``EvidenceStore.project``.
    """

    def __init__(self, journal: Any, db: Any, now: Optional[Callable[[], dt.datetime]] = None):
        self.journal = journal
        self.db = db
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def _append(
        self,
        *,
        authority_digest: str,
        strategy_id: str,
        account_id: str,
        account_mode: str,
        artifact_digest: str,
        allowlist_digest: str,
        ruleset_digest: str,
        max_gross_allocation: float,
        permitted_instruments: Sequence[Any],
        public_key_id: str,
        operator: str,
        reason: str,
        issued_at: dt.datetime,
        expires_at: dt.datetime,
        event: str,
        command_id: Optional[str],
        now: dt.datetime,
    ) -> AuthorityLedgerEntry:
        if event not in AUTHORITY_EVENTS:
            raise ValueError(f"event must be one of {AUTHORITY_EVENTS}, got {event!r}")
        recorded_at = _as_utc(now)
        captured: list[AuthorityLedgerEntry] = []

        def write(conn: Any, _revision: int) -> None:
            row = conn.execute(
                f"INSERT INTO live_activation_authority "
                f"(authority_digest, strategy_id, account_id, account_mode, artifact_digest, "
                f"allowlist_digest, ruleset_digest, max_gross_allocation, permitted_instruments, "
                f"public_key_id, operator, reason, issued_at, expires_at, event, command_id, "
                f"recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                f"RETURNING {_SELECT_COLUMNS}",
                [
                    authority_digest, strategy_id, account_id, account_mode, artifact_digest,
                    allowlist_digest, ruleset_digest, float(max_gross_allocation),
                    json.dumps(list(permitted_instruments)), public_key_id, operator, reason,
                    _as_utc(issued_at), _as_utc(expires_at), event, command_id, recorded_at,
                ],
            ).fetchone()
            captured.append(_row_to_entry(row))

        mutation = DomainMutation(
            event_type="promotion.live_activation_authority_recorded",
            entity_type="live_activation_authority",
            entity_id=strategy_entity_id(strategy_id),
            operation="upsert",
            account_id=account_id,
            source="trader_service",
            source_timestamp=recorded_at,
            correlation_id=command_id or authority_digest,
            payload={
                "authority_digest": authority_digest,
                "strategy_id": strategy_id,
                "event": event,
                "command_id": command_id,
            },
        )
        import uuid

        self.journal.mutate(
            self.journal.connect(),
            mutation,
            write,
            event_id=f"canary-authority:{authority_digest}:{event}:{uuid.uuid4().hex}",
        )
        return captured[0]

    def record_issued(
        self,
        attestation: CanaryAttestation,
        verified: VerifiedCanaryAuthority,
        *,
        operator: str,
        reason: str,
        now: Optional[dt.datetime] = None,
    ) -> str:
        """Idempotent by digest: appends an ``ISSUED`` row the first time a
        given authority is seen; a repeat submission of the SAME
        (identical-digest) authority is a safe no-op."""
        digest = verified.payload_digest
        if self.latest(digest) is not None:
            return digest
        self._append(
            authority_digest=digest, strategy_id=verified.strategy_id,
            account_id=verified.account_id, account_mode=attestation.account_mode,
            artifact_digest=verified.artifact_digest, allowlist_digest=verified.allowlist_digest,
            ruleset_digest=verified.ruleset_digest, max_gross_allocation=verified.max_gross_allocation,
            permitted_instruments=verified.permitted_instruments, public_key_id=verified.public_key_id,
            operator=operator, reason=reason, issued_at=attestation.issued_at,
            expires_at=verified.expires_at, event=EVENT_ISSUED, command_id=None,
            now=now or self._now(),
        )
        return digest

    def record_activated(self, authority_digest: str, *, command_id: str,
                         now: Optional[dt.datetime] = None) -> None:
        base = self.latest(authority_digest)
        if base is None:
            raise ValueError(f"cannot activate unknown authority {authority_digest!r}")
        self._append(
            authority_digest=authority_digest, strategy_id=base.strategy_id,
            account_id=base.account_id, account_mode=base.account_mode,
            artifact_digest=base.artifact_digest, allowlist_digest=base.allowlist_digest,
            ruleset_digest=base.ruleset_digest, max_gross_allocation=base.max_gross_allocation,
            permitted_instruments=base.permitted_instruments, public_key_id=base.public_key_id,
            operator=base.operator, reason=base.reason, issued_at=base.issued_at,
            expires_at=base.expires_at, event=EVENT_ACTIVATED, command_id=command_id,
            now=now or self._now(),
        )

    def record_deactivated(self, authority_digest: str, *, command_id: str, reason: str,
                           now: Optional[dt.datetime] = None) -> None:
        base = self.latest(authority_digest)
        if base is None:
            raise ValueError(f"cannot deactivate unknown authority {authority_digest!r}")
        self._append(
            authority_digest=authority_digest, strategy_id=base.strategy_id,
            account_id=base.account_id, account_mode=base.account_mode,
            artifact_digest=base.artifact_digest, allowlist_digest=base.allowlist_digest,
            ruleset_digest=base.ruleset_digest, max_gross_allocation=base.max_gross_allocation,
            permitted_instruments=base.permitted_instruments, public_key_id=base.public_key_id,
            operator=base.operator, reason=reason, issued_at=base.issued_at,
            expires_at=base.expires_at, event=EVENT_DEACTIVATED, command_id=command_id,
            now=now or self._now(),
        )

    def record_revoked(self, authority_digest: str, *, reason: str,
                       now: Optional[dt.datetime] = None) -> None:
        base = self.latest(authority_digest)
        if base is None:
            raise ValueError(f"cannot revoke unknown authority {authority_digest!r}")
        self._append(
            authority_digest=authority_digest, strategy_id=base.strategy_id,
            account_id=base.account_id, account_mode=base.account_mode,
            artifact_digest=base.artifact_digest, allowlist_digest=base.allowlist_digest,
            ruleset_digest=base.ruleset_digest, max_gross_allocation=base.max_gross_allocation,
            permitted_instruments=base.permitted_instruments, public_key_id=base.public_key_id,
            operator=base.operator, reason=reason, issued_at=base.issued_at,
            expires_at=base.expires_at, event=EVENT_REVOKED, command_id=None,
            now=now or self._now(),
        )

    def latest(self, authority_digest: str) -> Optional[AuthorityLedgerEntry]:
        row = self.db.execute(
            f"SELECT {_SELECT_COLUMNS} FROM live_activation_authority "
            f"WHERE authority_digest = ? ORDER BY entry_id DESC LIMIT 1",
            [authority_digest],
            fetch="one",
        )
        return _row_to_entry(row) if row is not None else None

    def latest_for_strategy(self, strategy_id: str) -> Optional[AuthorityLedgerEntry]:
        row = self.db.execute(
            f"SELECT {_SELECT_COLUMNS} FROM live_activation_authority "
            f"WHERE strategy_id = ? ORDER BY entry_id DESC LIMIT 1",
            [strategy_id],
            fetch="one",
        )
        return _row_to_entry(row) if row is not None else None

    def history(self, authority_digest: str) -> list[AuthorityLedgerEntry]:
        rows = self.db.execute(
            f"SELECT {_SELECT_COLUMNS} FROM live_activation_authority "
            f"WHERE authority_digest = ? ORDER BY entry_id ASC",
            [authority_digest],
            fetch="all",
        )
        return [_row_to_entry(row) for row in rows or ()]

    def is_revoked(self, authority_digest: str) -> bool:
        record = self.latest(authority_digest)
        return record is not None and record.event == EVENT_REVOKED

    def revoked_digests(self) -> frozenset:
        rows = self.db.execute(
            "SELECT DISTINCT authority_digest FROM live_activation_authority "
            "WHERE event = ?", [EVENT_REVOKED], fetch="all",
        )
        candidates = {row[0] for row in rows or ()}
        return frozenset(d for d in candidates if self.is_revoked(d))


# --------------------------------------------------------------------------- #
# Offline preparation (no trader_service, no private key)
# --------------------------------------------------------------------------- #
class PromotionPreparationError(Exception):
    """Raised when ``prepare_canary`` cannot build a trustworthy unsigned
    canary payload for a strategy."""


class PromotionController:
    """Offline-safe preparation of a canary authority payload.

    ``prepare_canary`` reads ONLY the strategy's current stage and a FRESH
    re-projection of its evidence window -- it takes no other implicit
    state and mutates nothing (evidence projection here uses the read-only
    ``EvidenceStore.rebuild_window``, never the persisting ``project``, so
    merely preparing a canary payload never itself advances any durable
    state). It fails loudly if the current stage is not ``PAPER_PASSED``,
    or if the fresh projection no longer clears ``PaperGate`` (evidence can
    regress or go stale between when PAPER_PASSED was recorded and when an
    operator runs this -- the stale stage row alone is never trusted).
    """

    def __init__(
        self,
        evidence_store: EvidenceStore,
        stage_machine: PromotionStageMachine,
        now: Optional[Callable[[], dt.datetime]] = None,
    ):
        self._evidence_store = evidence_store
        self._stage_machine = stage_machine
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def prepare_canary(
        self,
        strategy_id: str,
        *,
        account_id: str,
        artifact_digest: str,
        allowlist_digest: str,
        ruleset_digest: str,
        max_gross_allocation: float,
        permitted_instruments: Sequence[Any],
        public_key_id: str,
        expires_at: dt.datetime,
        operator: str,
        reason: str,
        now: Optional[dt.datetime] = None,
    ) -> dict:
        resolved_now = _as_utc(now) if now is not None else _as_utc(self._now())

        current_stage = self._stage_machine.current_stage(strategy_id)
        if current_stage != PAPER_PASSED:
            raise PromotionPreparationError(
                f"strategy {strategy_id!r} is in stage {current_stage!r}, not PAPER_PASSED "
                f"-- canary preparation requires a currently-passed paper window"
            )

        window = self._evidence_store.rebuild_window(strategy_id, as_of=resolved_now)
        decision = PaperGate().evaluate(window)
        if not decision.passed:
            raise PromotionPreparationError(
                f"strategy {strategy_id!r} paper evidence no longer clears PaperGate "
                f"as of {resolved_now.isoformat()}: blockers={decision.blockers!r}"
            )

        paper_evidence_digest = _window_digest(window)

        return build_canary_payload(
            strategy_id=strategy_id,
            account_id=account_id,
            artifact_digest=artifact_digest,
            allowlist_digest=allowlist_digest,
            ruleset_digest=ruleset_digest,
            max_gross_allocation=max_gross_allocation,
            permitted_instruments=permitted_instruments,
            paper_evidence_digest=paper_evidence_digest,
            issued_at=resolved_now,
            expires_at=expires_at,
            operator=operator,
            reason=reason,
            public_key_id=public_key_id,
        )

    def prepare_allocation(
        self,
        strategy_id: str,
        *,
        target_stage: str,
        current_allocation_stage: Optional[str],
        account_id: str,
        account_mode: str,
        artifact_digest: str,
        allowlist_digest: str,
        ruleset_digest: str,
        max_gross_allocation: float,
        public_key_id: str,
        expires_at: dt.datetime,
        operator: str,
        reason: str,
        authority_started_at: Optional[dt.datetime] = None,
        capacity_review_passed: bool = False,
        now: Optional[dt.datetime] = None,
    ) -> dict:
        resolved_now = _as_utc(now) if now is not None else _as_utc(self._now())
        promotion_stage = self._stage_machine.current_stage(strategy_id)
        window = self._evidence_store.rebuild_window(strategy_id, as_of=resolved_now)
        scaling = ScalingGate().evaluate(
            promotion_stage=promotion_stage,
            current_allocation_stage=current_allocation_stage,
            window=window,
            target_stage=target_stage,
            authority_started_at=authority_started_at,
            capacity_review_passed=capacity_review_passed,
        )
        if not scaling.passed:
            raise PromotionPreparationError(
                f"strategy {strategy_id!r} does not clear ScalingGate for {target_stage!r}: "
                f"blockers={scaling.blockers!r}"
            )
        return build_allocation_payload(
            strategy_id=strategy_id,
            account_id=account_id,
            account_mode=account_mode,
            stage=target_stage,
            artifact_digest=artifact_digest,
            allowlist_digest=allowlist_digest,
            ruleset_digest=ruleset_digest,
            max_gross_allocation=max_gross_allocation,
            evidence_digest=scaling.evidence_digest,
            issued_at=resolved_now,
            expires_at=expires_at,
            operator=operator,
            reason=reason,
            public_key_id=public_key_id,
        )


def _window_digest(window) -> str:
    from trader.research.canonical import sha256_digest

    return sha256_digest("p4_paper_evidence_window", window.to_payload())


# --------------------------------------------------------------------------- #
# Trader-side authenticated activation / deactivation
# --------------------------------------------------------------------------- #
class CanaryActivationRefused(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


_CANARY_ERROR_CODES = {
    "CanaryUnknownKey": "UNTRUSTED_KEY",
    "CanaryBadSignature": "BAD_SIGNATURE",
    "CanaryExpired": "AUTHORITY_EXPIRED",
    "CanaryRevoked": "AUTHORITY_REVOKED",
    "CanaryBindingMismatch": "BINDING_MISMATCH",
    "CanaryPolicyViolation": "POLICY_VIOLATION",
}


def _canary_error_code(exc: CanaryAuthorityError) -> str:
    return _CANARY_ERROR_CODES.get(type(exc).__name__, "AUTHORITY_INVALID")


class CanaryActivationService:
    """Wires ``activate_live_canary``/``deactivate_live_canary`` as
    ``TradingCommandCoordinator`` actions (non-saga -- neither dispatches a
    broker order; see module docstring).

    Every collaborator is injected so production wiring (``command_stack.py``)
    and tests can supply real or fake adapters interchangeably:

    * ``expected_bindings``: returns the trader's OWN currently-verified
      artifact bindings (account id, artifact/allowlist/ruleset digest) --
      called FRESH on every activation attempt (never cached), so a
      writable-mount rejection or a changed policy is caught even if it
      happened after the authority was minted.
    * ``semantic_readiness_ready`` / ``broker_flat_reconciled`` /
      ``breaker_clear``: preflight gates. ALL must hold for activation;
      deactivation (risk-REDUCING) never depends on them.
    * ``revoked_digests``: current revocation set (defaults to the
      authority store's own ``revoked_digests()``).
    """

    def __init__(
        self,
        *,
        stage_machine: PromotionStageMachine,
        evidence_store: EvidenceStore,
        authority_store: LiveActivationAuthorityStore,
        verifier: CanaryAuthorityVerifier,
        expected_bindings: Callable[[], ExpectedCanaryBindings],
        semantic_readiness_ready: Callable[[], bool],
        broker_flat_reconciled: Callable[[], bool],
        breaker_clear: Callable[[], bool],
        revoked_digests: Optional[Callable[[], frozenset]] = None,
        now: Optional[Callable[[], dt.datetime]] = None,
    ):
        self._stage_machine = stage_machine
        self._evidence_store = evidence_store
        self._authority_store = authority_store
        self._verifier = verifier
        self._expected_bindings = expected_bindings
        self._semantic_readiness_ready = semantic_readiness_ready
        self._broker_flat_reconciled = broker_flat_reconciled
        self._breaker_clear = breaker_clear
        self._revoked_digests = revoked_digests or authority_store.revoked_digests
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    # -- coordinator actions -------------------------------------------------

    def activate(self, cmd) -> dict[str, Any]:
        body = cmd.body
        reason = str(body.get("reason", "")).strip()
        if not reason:
            raise _validation_error("REASON_REQUIRED", "activation requires an explicit reason")

        if cmd.source != REQUIRED_ACTIVATION_SOURCE:
            raise _validation_error(
                "AUTOMATIC_ACTIVATION_FORBIDDEN",
                "canary activation must be an explicit operator action, "
                f"got source={cmd.source!r}",
            )

        attestation_wire = body.get("attestation")
        if not isinstance(attestation_wire, dict):
            raise _validation_error("ATTESTATION_REQUIRED", "activation requires a signed attestation")

        try:
            attestation = canary_attestation_from_wire(attestation_wire)
        except (KeyError, ValueError, TypeError) as exc:
            raise _validation_error("ATTESTATION_MALFORMED", str(exc)) from exc

        strategy_id = attestation.strategy_id
        now = _as_utc(self._now())

        if not self._semantic_readiness_ready():
            raise _validation_error("READINESS_NOT_MET", "semantic readiness checks are not all green")
        if not self._broker_flat_reconciled():
            raise _validation_error(
                "BROKER_NOT_FLAT", "broker account is not flat and reconciled"
            )
        if not self._breaker_clear():
            raise _validation_error("BREAKER_NOT_CLEAR", "circuit breaker is not clear")

        try:
            expected = self._expected_bindings()
        except Exception as exc:  # noqa: BLE001 - re-run of the FULL artifact
            # verification chain (bundle integrity, read-only mount in live
            # mode, state/mode gate, expiry, revocation) lives behind this
            # callable; ANY failure there (e.g. a writable bundle mount, a
            # since-changed artifact) must block activation just as cleanly
            # as a failed preflight check, never surface as an opaque
            # internal error.
            raise _validation_error("ARTIFACT_VERIFICATION_FAILED", str(exc)) from exc

        try:
            verified = self._verifier.verify(
                attestation, expected, now=now, revoked_digests=self._revoked_digests(),
            )
        except CanaryAuthorityError as exc:
            raise _validation_error(_canary_error_code(exc), str(exc)) from exc

        digest = verified.payload_digest
        if self._authority_store.is_revoked(digest):
            raise _validation_error("AUTHORITY_REVOKED", f"canary authority {digest} is revoked")

        current_stage = self._stage_machine.current_stage(strategy_id)
        if current_stage == CANARY_ACTIVE:
            raise _validation_error(
                "ALREADY_ACTIVE", f"strategy {strategy_id!r} canary authority is already active"
            )
        if current_stage not in (PAPER_PASSED, CANARY_SUSPENDED, CANARY_AUTHORIZED):
            raise _validation_error(
                "ILLEGAL_STAGE", f"cannot activate canary from stage {current_stage!r}"
            )

        self._authority_store.record_issued(
            attestation, verified, operator=attestation.operator, reason=reason, now=now,
        )

        try:
            if current_stage in (PAPER_PASSED, CANARY_SUSPENDED):
                self._stage_machine.transition(
                    strategy_id, CANARY_AUTHORIZED, reason=reason, actor=cmd.source,
                    now=now, evidence_store=self._evidence_store,
                    authority_ref=digest, authority_expiry=verified.expires_at,
                )
            record = self._stage_machine.transition(
                strategy_id, CANARY_ACTIVE, reason=reason, actor=cmd.source, now=now,
                authority_ref=digest, authority_expiry=verified.expires_at,
            )
        except EvidenceNotCleanError as exc:
            raise _validation_error("EVIDENCE_NOT_CLEAN", str(exc)) from exc
        except (AuthorityRequiredError, AuthorityMismatchError, AuthorityExpiredError) as exc:
            raise _validation_error("AUTHORITY_INVALID", str(exc)) from exc
        except IllegalStageTransition as exc:
            raise _validation_error("ILLEGAL_STAGE", str(exc)) from exc

        self._authority_store.record_activated(digest, command_id=cmd.command_id, now=now)

        # Non-saga action (see module docstring): return a plain outcome
        # dict, not a ``CommandReceipt`` -- the coordinator's own
        # RECEIVED->RESOLVED fallback builds the receipt (and, on a raised
        # ``CommandValidationError`` above, the REJECTED one) itself.
        return {"strategy_id": strategy_id, "stage": record.stage, "authority_digest": digest}

    def deactivate(self, cmd) -> dict[str, Any]:
        body = cmd.body
        reason = str(body.get("reason", "")).strip()
        if not reason:
            raise _validation_error("REASON_REQUIRED", "deactivation requires an explicit reason")

        strategy_id = body.get("strategy_id")
        if not strategy_id:
            raise _validation_error("STRATEGY_ID_REQUIRED", "deactivation requires strategy_id")

        now = _as_utc(self._now())
        current_stage = self._stage_machine.current_stage(strategy_id)
        if current_stage != CANARY_ACTIVE:
            raise _validation_error(
                "NOT_ACTIVE",
                f"strategy {strategy_id!r} canary authority is not active (stage={current_stage!r})",
            )

        try:
            record = self._stage_machine.transition(
                strategy_id, CANARY_SUSPENDED, reason=reason, actor=cmd.source, now=now,
            )
        except IllegalStageTransition as exc:
            raise _validation_error("ILLEGAL_STAGE", str(exc)) from exc

        authority_digest = record.authority_ref
        if authority_digest is not None:
            self._authority_store.record_deactivated(
                authority_digest, command_id=cmd.command_id, reason=reason, now=now,
            )

        return {
            "strategy_id": strategy_id, "stage": record.stage, "authority_digest": authority_digest,
        }


_ALLOCATION_ERROR_CODES = {
    "AllocationUnknownKey": "UNTRUSTED_KEY",
    "AllocationBadSignature": "BAD_SIGNATURE",
    "AllocationExpired": "AUTHORITY_EXPIRED",
    "AllocationRevoked": "AUTHORITY_REVOKED",
    "AllocationBindingMismatch": "BINDING_MISMATCH",
    "AllocationPolicyViolation": "POLICY_VIOLATION",
}


def _allocation_error_code(exc: AllocationAuthorityError) -> str:
    return _ALLOCATION_ERROR_CODES.get(type(exc).__name__, "AUTHORITY_INVALID")


class AllocationActivationService:
    """Authenticated ``activate_allocation`` — verifies signed allocation authority."""

    def __init__(
        self,
        *,
        authority_store: Any,
        verifier: AllocationAttestationVerifier,
        expected_bindings: Callable[[], ExpectedAllocationBindings],
        semantic_readiness_ready: Callable[[], bool],
        broker_flat_reconciled: Callable[[], bool],
        breaker_clear: Callable[[], bool],
        now: Optional[Callable[[], dt.datetime]] = None,
    ):
        self._authority_store = authority_store
        self._verifier = verifier
        self._expected_bindings = expected_bindings
        self._semantic_readiness_ready = semantic_readiness_ready
        self._broker_flat_reconciled = broker_flat_reconciled
        self._breaker_clear = breaker_clear
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def activate(self, cmd) -> dict[str, Any]:
        body = cmd.body
        reason = str(body.get("reason", "")).strip()
        if not reason:
            raise _validation_error("REASON_REQUIRED", "activation requires an explicit reason")
        if cmd.source != REQUIRED_ACTIVATION_SOURCE:
            raise _validation_error(
                "AUTOMATIC_ACTIVATION_FORBIDDEN",
                f"allocation activation must be an explicit operator action, got source={cmd.source!r}",
            )
        wire = body.get("attestation")
        if not isinstance(wire, dict):
            raise _validation_error("ATTESTATION_REQUIRED", "activation requires a signed attestation")
        try:
            attestation = allocation_attestation_from_wire(wire)
        except (KeyError, ValueError, TypeError) as exc:
            raise _validation_error("ATTESTATION_MALFORMED", str(exc)) from exc

        now = _as_utc(self._now())
        if not self._semantic_readiness_ready():
            raise _validation_error("READINESS_NOT_MET", "semantic readiness checks are not all green")
        if not self._broker_flat_reconciled():
            raise _validation_error("BROKER_NOT_FLAT", "broker account is not flat and reconciled")
        if not self._breaker_clear():
            raise _validation_error("BREAKER_NOT_CLEAR", "circuit breaker is not clear")

        try:
            expected = self._expected_bindings()
        except Exception as exc:  # noqa: BLE001
            raise _validation_error("ARTIFACT_VERIFICATION_FAILED", str(exc)) from exc

        try:
            verified = self._verifier.verify(attestation, expected=expected, now=now)
        except AllocationAuthorityError as exc:
            raise _validation_error(_allocation_error_code(exc), str(exc)) from exc

        digest = verified.payload_digest
        if self._authority_store.is_revoked(digest):
            raise _validation_error("AUTHORITY_REVOKED", f"allocation authority {digest} is revoked")

        prior = self._authority_store.active_for(expected.account_id, expected.artifact_digest, now=now)
        if prior is not None and prior.authority_digest != digest:
            self._authority_store.record_superseded(
                prior.authority_digest,
                superseded_by_digest=digest,
                reason=reason,
                now=now,
            )

        self._authority_store.record_issued(
            attestation, verified, operator=attestation.operator, reason=reason, now=now,
        )
        self._authority_store.record_activated(digest, command_id=cmd.command_id, now=now)
        return {
            "strategy_id": verified.strategy_id,
            "stage": verified.stage,
            "authority_digest": digest,
        }


def _validation_error(code: str, message: str) -> CommandValidationError:
    return CommandValidationError(code, message)
