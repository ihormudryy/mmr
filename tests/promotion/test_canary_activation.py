"""P4 Task 5 -- signed live-canary activation authority.

Contract under test:

* ``build_canary_payload`` enforces exactly-one-instrument, allocation in
  ``(0, 6%]``, and unconditionally pins ``account_mode="live"`` /
  ``eligibility_state="CANARY_ELIGIBLE"`` (never caller-supplied).
* ``CanaryAuthorityVerifier.verify`` fails closed on: untrusted key,
  tampered/bad signature, expiry, revocation, wrong/changed bindings
  (account/artifact/allowlist/ruleset), and -- defense in depth -- a
  hand-built (never through ``build_canary_payload``) payload that still
  carries a valid signature but violates structural policy (two
  instruments, over-cap allocation, wrong eligibility state/account mode).
* ``PromotionController.prepare_canary`` only produces an unsigned payload
  from a strategy CURRENTLY in ``PAPER_PASSED`` whose evidence, freshly
  re-projected, still clears ``PaperGate`` -- never from a stale stage row.
* ``CanaryActivationService.activate``/``deactivate`` are authenticated
  ``TradingCommandCoordinator`` actions: activation requires an explicit
  operator source + reason, all three preflight gates (semantic readiness,
  broker flat/reconciled, breaker clear), and independent re-verification
  of the attestation against the trader's OWN fresh artifact bindings --
  never a cached copy. Deactivation only needs a reason + the strategy
  currently being ``CANARY_ACTIVE``.
* Migration 43: append-only ``live_activation_authority`` -- every
  lifecycle event is a NEW row.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.promotion.canary_attestation import (
    CANARY_ACCOUNT_MODE,
    CANARY_ELIGIBLE,
    CANARY_PAYLOAD_PREFIX,
    MAX_CANARY_GROSS_ALLOCATION,
    CanaryAccountInvalid,
    CanaryAllocationExceeded,
    CanaryAttestation,
    CanaryAuthorityVerifier,
    CanaryBadSignature,
    CanaryBindingMismatch,
    CanaryExpired,
    CanaryMultiStrategy,
    CanaryPolicyViolation,
    CanaryRevoked,
    CanaryUnknownKey,
    ExpectedCanaryBindings,
    build_canary_payload,
    canary_attestation_from_wire,
    canary_attestation_to_wire,
    canary_payload_digest,
    sign_canary_payload,
)
from trader.promotion.controller import (
    CANARY_AUTHORITY_MIGRATION_VERSIONS,
    CanaryActivationService,
    LiveActivationAuthorityStore,
    PromotionController,
    PromotionPreparationError,
    apply_live_activation_authority_migration,
)
from trader.promotion.evidence_store import EvidenceEvent, EvidenceStore, apply_evidence_migrations
from trader.promotion.stage import (
    CANARY_ACTIVE,
    CANARY_AUTHORIZED,
    CANARY_SUSPENDED,
    PAPER_COLLECTING,
    PAPER_PASSED,
    PromotionStageMachine,
    apply_stage_migration,
)
from trader.research.attestation import ATTESTATION_PAYLOAD_PREFIX
from trader.research.signing import AttestationSigner
from trader.trading.command_coordinator import (
    CommandLedger,
    CommandRequest,
    CommandValidationError,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
STRATEGY = "orb_breakout"
ACCOUNT = "DU9000001"


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------
def _db(tmp_path: Path, name: str = "canary.duckdb"):
    db = DuckDBConnection.get_instance(str(tmp_path / name))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    return db, migrator, journal


def _machine_and_evidence(tmp_path: Path, *, name: str = "canary.duckdb", now=lambda: NOW):
    db, migrator, journal = _db(tmp_path, name)
    apply_stage_migration(migrator)
    apply_evidence_migrations(migrator)
    apply_live_activation_authority_migration(migrator)
    machine = PromotionStageMachine(journal=journal, db=db, now=now)
    evidence = EvidenceStore(journal=journal, db=db, now=now)
    authority_store = LiveActivationAuthorityStore(journal=journal, db=db, now=now)
    return machine, evidence, authority_store, journal, db, migrator


# 20 session dates spanning exactly 30 elapsed calendar days -- copied from
# test_stage_machine.py's floor-satisfying seed so PAPER_PASSED is reachable.
_PAPER_GATE_SESSION_OFFSETS = (0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 18, 19, 20, 21, 29)


def _seed_clean_evidence(evidence, strategy_id=STRATEGY, ts=NOW, suffix=""):
    tag = suffix or "1"
    start = ts - dt.timedelta(days=29)
    for i, offset in enumerate(_PAPER_GATE_SESSION_OFFSETS):
        evidence.append(EvidenceEvent(
            source_event_id=f"{strategy_id}-sess-{tag}-{i}",
            strategy_id=strategy_id, event_kind="session",
            payload={"session_id": f"s{tag}{i:02d}"},
            source_timestamp=start + dt.timedelta(days=offset),
        ))
    for i in range(50):
        evidence.append(EvidenceEvent(
            source_event_id=f"{strategy_id}-rt-{tag}-{i}",
            strategy_id=strategy_id, event_kind="round_trip",
            payload={
                "round_trip_id": f"rt-{tag}-{i}",
                "instrument_id": str(1000 + (i % 5)),
                "pnl_after_cost": 10.0,
            },
            source_timestamp=ts,
        ))


def _promote_to_paper_passed(machine, evidence, strategy_id=STRATEGY, ts=NOW):
    _seed_clean_evidence(evidence, strategy_id=strategy_id, ts=ts)
    machine.transition(strategy_id, PAPER_COLLECTING, reason="bootstrap", actor="system", now=ts)
    machine.transition(
        strategy_id, PAPER_PASSED, reason="floors met", actor="paper_gate",
        evidence_store=evidence, now=ts,
    )


def _keypair():
    priv = ed25519.Ed25519PrivateKey.generate()
    return AttestationSigner(priv)


def _controller(machine, evidence, now=lambda: NOW):
    return PromotionController(evidence_store=evidence, stage_machine=machine, now=now)


def _unsigned_payload(
    controller,
    *,
    strategy_id=STRATEGY,
    account_id=ACCOUNT,
    artifact_digest="artifact-digest-1",
    allowlist_digest="allow-digest-1",
    ruleset_digest="rule-digest-1",
    max_gross_allocation=0.05,
    permitted_instruments=("AAPL",),
    public_key_id="ed25519-fake",
    expires_at=None,
    operator="operator:alice",
    reason="ready for canary",
    now=None,
):
    return controller.prepare_canary(
        strategy_id,
        account_id=account_id,
        artifact_digest=artifact_digest,
        allowlist_digest=allowlist_digest,
        ruleset_digest=ruleset_digest,
        max_gross_allocation=max_gross_allocation,
        permitted_instruments=permitted_instruments,
        public_key_id=public_key_id,
        expires_at=expires_at or (NOW + dt.timedelta(days=7)),
        operator=operator,
        reason=reason,
        now=now,
    )


def _signed_attestation(controller, signer, **kwargs) -> CanaryAttestation:
    unsigned = _unsigned_payload(controller, public_key_id=signer.public_key_id, **kwargs)
    return sign_canary_payload(signer, unsigned)


def _expected_bindings(
    *,
    account_id=ACCOUNT,
    artifact_digest="artifact-digest-1",
    allowlist_digest="allow-digest-1",
    ruleset_digest="rule-digest-1",
):
    return ExpectedCanaryBindings(
        account_id=account_id, artifact_digest=artifact_digest,
        allowlist_digest=allowlist_digest, ruleset_digest=ruleset_digest,
    )


def _service(machine, evidence, authority_store, verifier, *, expected_bindings=None,
             ready=True, flat=True, breaker_clear=True, now=lambda: NOW):
    return CanaryActivationService(
        stage_machine=machine,
        evidence_store=evidence,
        authority_store=authority_store,
        verifier=verifier,
        expected_bindings=expected_bindings or _expected_bindings,
        semantic_readiness_ready=lambda: ready,
        broker_flat_reconciled=lambda: flat,
        breaker_clear=lambda: breaker_clear,
        now=now,
    )


def _activate_cmd(attestation: CanaryAttestation, *, reason="operator approved canary",
                   source="operator", command_id="activate-1"):
    return CommandRequest(
        command_id=command_id, action="activate_live_canary", account_id=ACCOUNT,
        target_type="strategy", target_id=attestation.strategy_id, expected_version=None,
        body={"attestation": canary_attestation_to_wire(attestation), "reason": reason},
        source=source,
    )


def _deactivate_cmd(strategy_id=STRATEGY, *, reason="operator paused canary",
                     source="operator", command_id="deactivate-1"):
    return CommandRequest(
        command_id=command_id, action="deactivate_live_canary", account_id=ACCOUNT,
        target_type="strategy", target_id=strategy_id, expected_version=None,
        body={"strategy_id": strategy_id, "reason": reason}, source=source,
    )


# ---------------------------------------------------------------------------
# build_canary_payload -- structural invariants enforced at mint time
# ---------------------------------------------------------------------------
def test_build_canary_payload_pins_live_and_canary_eligible():
    payload = build_canary_payload(
        strategy_id=STRATEGY, account_id=ACCOUNT, artifact_digest="ad", allowlist_digest="al",
        ruleset_digest="rd", max_gross_allocation=0.05, permitted_instruments=("AAPL",),
        paper_evidence_digest="ped", issued_at=NOW, expires_at=NOW + dt.timedelta(days=7),
        operator="operator:alice", reason="ready", public_key_id="ed25519-fake",
    )
    assert payload["account_mode"] == CANARY_ACCOUNT_MODE == "live"
    assert payload["eligibility_state"] == CANARY_ELIGIBLE == "CANARY_ELIGIBLE"


def test_build_canary_payload_rejects_allocation_over_cap():
    with pytest.raises(CanaryAllocationExceeded):
        build_canary_payload(
            strategy_id=STRATEGY, account_id=ACCOUNT, artifact_digest="ad", allowlist_digest="al",
            ruleset_digest="rd", max_gross_allocation=MAX_CANARY_GROSS_ALLOCATION + 0.001,
            permitted_instruments=("AAPL",), paper_evidence_digest="ped", issued_at=NOW,
            expires_at=NOW + dt.timedelta(days=7), operator="operator:alice", reason="ready",
            public_key_id="ed25519-fake",
        )


def test_build_canary_payload_rejects_zero_or_negative_allocation():
    with pytest.raises(CanaryAllocationExceeded):
        build_canary_payload(
            strategy_id=STRATEGY, account_id=ACCOUNT, artifact_digest="ad", allowlist_digest="al",
            ruleset_digest="rd", max_gross_allocation=0.0, permitted_instruments=("AAPL",),
            paper_evidence_digest="ped", issued_at=NOW, expires_at=NOW + dt.timedelta(days=7),
            operator="operator:alice", reason="ready", public_key_id="ed25519-fake",
        )


def test_build_canary_payload_rejects_two_instruments():
    with pytest.raises(CanaryMultiStrategy):
        build_canary_payload(
            strategy_id=STRATEGY, account_id=ACCOUNT, artifact_digest="ad", allowlist_digest="al",
            ruleset_digest="rd", max_gross_allocation=0.05, permitted_instruments=("AAPL", "MSFT"),
            paper_evidence_digest="ped", issued_at=NOW, expires_at=NOW + dt.timedelta(days=7),
            operator="operator:alice", reason="ready", public_key_id="ed25519-fake",
        )


def test_build_canary_payload_rejects_zero_instruments():
    with pytest.raises(CanaryMultiStrategy):
        build_canary_payload(
            strategy_id=STRATEGY, account_id=ACCOUNT, artifact_digest="ad", allowlist_digest="al",
            ruleset_digest="rd", max_gross_allocation=0.05, permitted_instruments=(),
            paper_evidence_digest="ped", issued_at=NOW, expires_at=NOW + dt.timedelta(days=7),
            operator="operator:alice", reason="ready", public_key_id="ed25519-fake",
        )


def test_build_canary_payload_rejects_missing_account_id():
    with pytest.raises(CanaryAccountInvalid):
        build_canary_payload(
            strategy_id=STRATEGY, account_id="", artifact_digest="ad", allowlist_digest="al",
            ruleset_digest="rd", max_gross_allocation=0.05, permitted_instruments=("AAPL",),
            paper_evidence_digest="ped", issued_at=NOW, expires_at=NOW + dt.timedelta(days=7),
            operator="operator:alice", reason="ready", public_key_id="ed25519-fake",
        )


def test_canary_payload_namespaced_distinctly_from_paper_attestation():
    """A canary authority's digest namespace is structurally distinct from
    the P2 paper-eligibility attestation's -- the two can never collide."""
    assert CANARY_PAYLOAD_PREFIX != ATTESTATION_PAYLOAD_PREFIX


# ---------------------------------------------------------------------------
# Offline signing
# ---------------------------------------------------------------------------
def test_sign_canary_payload_rejects_public_key_id_mismatch():
    signer = _keypair()
    payload = build_canary_payload(
        strategy_id=STRATEGY, account_id=ACCOUNT, artifact_digest="ad", allowlist_digest="al",
        ruleset_digest="rd", max_gross_allocation=0.05, permitted_instruments=("AAPL",),
        paper_evidence_digest="ped", issued_at=NOW, expires_at=NOW + dt.timedelta(days=7),
        operator="operator:alice", reason="ready", public_key_id="not-the-signer-key",
    )
    with pytest.raises(ValueError, match="public_key_id"):
        sign_canary_payload(signer, payload)


def test_sign_canary_payload_produces_verifiable_attestation():
    signer = _keypair()
    payload = build_canary_payload(
        strategy_id=STRATEGY, account_id=ACCOUNT, artifact_digest="ad", allowlist_digest="al",
        ruleset_digest="rd", max_gross_allocation=0.05, permitted_instruments=("AAPL",),
        paper_evidence_digest="ped", issued_at=NOW, expires_at=NOW + dt.timedelta(days=7),
        operator="operator:alice", reason="ready", public_key_id=signer.public_key_id,
    )
    attestation = sign_canary_payload(signer, payload)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    verified = verifier.verify(
        attestation, _expected_bindings(artifact_digest="ad", allowlist_digest="al", ruleset_digest="rd"),
        now=NOW,
    )
    assert verified.strategy_id == STRATEGY
    assert verified.payload_digest == canary_payload_digest(payload)


# ---------------------------------------------------------------------------
# CanaryAuthorityVerifier -- fail closed
# ---------------------------------------------------------------------------
def test_verify_rejects_untrusted_key(tmp_path):
    machine, evidence, _, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)

    other_signer = _keypair()
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[other_signer.public_key])
    with pytest.raises(CanaryUnknownKey):
        verifier.verify(attestation, _expected_bindings(), now=NOW)


def test_verify_rejects_tampered_field(tmp_path):
    machine, evidence, _, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)

    import dataclasses
    tampered = dataclasses.replace(attestation, max_gross_allocation=0.06)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    with pytest.raises(CanaryBadSignature):
        verifier.verify(tampered, _expected_bindings(), now=NOW)


def test_verify_rejects_expired(tmp_path):
    machine, evidence, _, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(
        controller, signer, expires_at=NOW + dt.timedelta(hours=1),
    )
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    with pytest.raises(CanaryExpired):
        verifier.verify(attestation, _expected_bindings(), now=NOW + dt.timedelta(hours=2))


def test_verify_rejects_revoked(tmp_path):
    machine, evidence, _, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    digest = canary_payload_digest(attestation.unsigned_payload)

    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    with pytest.raises(CanaryRevoked):
        verifier.verify(attestation, _expected_bindings(), now=NOW, revoked_digests=frozenset([digest]))


@pytest.mark.parametrize("field,value", [
    ("account_id", "DU_WRONG_ACCOUNT"),
    ("artifact_digest", "wrong-artifact"),
    ("allowlist_digest", "wrong-allowlist"),
    ("ruleset_digest", "wrong-ruleset"),
])
def test_verify_rejects_binding_mismatch(tmp_path, field, value):
    machine, evidence, _, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)

    expected_kwargs = {
        "account_id": ACCOUNT, "artifact_digest": "artifact-digest-1",
        "allowlist_digest": "allow-digest-1", "ruleset_digest": "rule-digest-1",
    }
    expected_kwargs[field] = value
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    with pytest.raises(CanaryBindingMismatch):
        verifier.verify(attestation, ExpectedCanaryBindings(**expected_kwargs), now=NOW)


def _hand_built_signed(signer, **overrides) -> CanaryAttestation:
    """Bypasses ``build_canary_payload``'s validation entirely -- used only
    to prove ``CanaryAuthorityVerifier`` independently re-checks policy on a
    validly-signed-but-never-validated payload."""
    fields = {
        "strategy_id": STRATEGY, "account_id": ACCOUNT, "account_mode": CANARY_ACCOUNT_MODE,
        "eligibility_state": CANARY_ELIGIBLE, "artifact_digest": "artifact-digest-1",
        "allowlist_digest": "allow-digest-1", "ruleset_digest": "rule-digest-1",
        "max_gross_allocation": 0.05, "permitted_instruments": ("AAPL",),
        "paper_evidence_digest": "ped", "issued_at": NOW, "expires_at": NOW + dt.timedelta(days=7),
        "operator": "operator:mallory", "reason": "forged", "public_key_id": signer.public_key_id,
    }
    fields.update(overrides)
    return sign_canary_payload(signer, fields)


def test_verify_rejects_hand_built_two_instrument_payload(tmp_path):
    signer = _keypair()
    attestation = _hand_built_signed(signer, permitted_instruments=("AAPL", "MSFT"))
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    with pytest.raises(CanaryPolicyViolation):
        verifier.verify(attestation, _expected_bindings(), now=NOW)


def test_verify_rejects_hand_built_over_allocation_payload(tmp_path):
    signer = _keypair()
    attestation = _hand_built_signed(signer, max_gross_allocation=0.5)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    with pytest.raises(CanaryPolicyViolation):
        verifier.verify(attestation, _expected_bindings(), now=NOW)


def test_verify_rejects_hand_built_paper_eligible_state(tmp_path):
    """CRITICAL: PAPER_ELIGIBLE must never authorize live trading, even via
    a hand-built, validly-signed canary payload."""
    signer = _keypair()
    attestation = _hand_built_signed(signer, eligibility_state="PAPER_ELIGIBLE")
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    with pytest.raises(CanaryPolicyViolation):
        verifier.verify(attestation, _expected_bindings(), now=NOW)


def test_verify_rejects_hand_built_paper_account_mode(tmp_path):
    signer = _keypair()
    attestation = _hand_built_signed(signer, account_mode="paper")
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    with pytest.raises(CanaryPolicyViolation):
        verifier.verify(attestation, _expected_bindings(), now=NOW)


# ---------------------------------------------------------------------------
# Wire (de)serialization -- CLI/RPC safety
# ---------------------------------------------------------------------------
def test_wire_round_trip_preserves_every_field(tmp_path):
    machine, evidence, _, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)

    wire = canary_attestation_to_wire(attestation)
    restored = canary_attestation_from_wire(wire)
    assert restored == attestation


def test_wire_contains_no_private_key_material(tmp_path):
    """The wire projection is plain JSON-safe primitives -- no key object,
    no private bytes, ever."""
    machine, evidence, _, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    wire = canary_attestation_to_wire(attestation)

    import json
    # Must be JSON-serializable -- any private key object would raise here.
    json.dumps(wire)
    assert "private" not in json.dumps(wire).lower()


# ---------------------------------------------------------------------------
# PromotionController.prepare_canary
# ---------------------------------------------------------------------------
def test_prepare_canary_requires_paper_passed_stage(tmp_path):
    machine, evidence, _, *_ = _machine_and_evidence(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    controller = _controller(machine, evidence)
    with pytest.raises(PromotionPreparationError, match="PAPER_PASSED"):
        _unsigned_payload(controller)


def test_prepare_canary_succeeds_from_passed_paper_window(tmp_path):
    machine, evidence, _, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    payload = _unsigned_payload(controller)
    assert payload["strategy_id"] == STRATEGY
    assert payload["account_mode"] == "live"
    assert payload["eligibility_state"] == "CANARY_ELIGIBLE"
    assert payload["paper_evidence_digest"]


def test_prepare_canary_rejects_when_evidence_regressed_since_paper_passed(tmp_path):
    """Evidence can go stale/incident-bearing between when PAPER_PASSED was
    recorded and when an operator runs prepare_canary -- the stale STAGE row
    alone must never be trusted."""
    machine, evidence, _, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    evidence.append(EvidenceEvent(
        source_event_id="breaker-after-pass", strategy_id=STRATEGY, event_kind="breaker_trip",
        payload={"incident_id": "inc-1"}, source_timestamp=NOW,
    ))
    controller = _controller(machine, evidence)
    with pytest.raises(PromotionPreparationError, match="PaperGate"):
        _unsigned_payload(controller)


def test_prepare_canary_never_mutates_stage_or_evidence(tmp_path):
    """``prepare_canary`` uses ``rebuild_window`` (read-only) -- calling it
    repeatedly must never itself advance state."""
    machine, evidence, _, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    _unsigned_payload(controller)
    _unsigned_payload(controller)
    assert machine.current_stage(STRATEGY) == PAPER_PASSED


# ---------------------------------------------------------------------------
# CanaryActivationService.activate -- preflight + verification + transition
# ---------------------------------------------------------------------------
def test_activate_happy_path_from_paper_passed(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier)

    outcome = service.activate(_activate_cmd(attestation))
    assert outcome["stage"] == CANARY_ACTIVE
    assert machine.current_stage(STRATEGY) == CANARY_ACTIVE

    digest = outcome["authority_digest"]
    latest = authority_store.latest(digest)
    assert latest.event == "ACTIVATED"


def test_activate_requires_explicit_reason(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier)

    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(_activate_cmd(attestation, reason=""))
    assert exc_info.value.code == "REASON_REQUIRED"
    assert machine.current_stage(STRATEGY) == PAPER_PASSED


def test_activate_forbids_automatic_non_operator_source(tmp_path):
    """No automated pipeline may activate live canary trading -- only an
    explicit operator-sourced command."""
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier)

    for automatic_source in ("strategy_service", "dashboard", "system", "cron"):
        with pytest.raises(CommandValidationError) as exc_info:
            service.activate(_activate_cmd(attestation, source=automatic_source,
                                           command_id=f"auto-{automatic_source}"))
        assert exc_info.value.code == "AUTOMATIC_ACTIVATION_FORBIDDEN"
    assert machine.current_stage(STRATEGY) == PAPER_PASSED


def test_activate_requires_semantic_readiness(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier, ready=False)

    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(_activate_cmd(attestation))
    assert exc_info.value.code == "READINESS_NOT_MET"
    assert machine.current_stage(STRATEGY) == PAPER_PASSED


def test_activate_requires_broker_flat_and_reconciled(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier, flat=False)

    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(_activate_cmd(attestation))
    assert exc_info.value.code == "BROKER_NOT_FLAT"


def test_activate_requires_breaker_clear(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier, breaker_clear=False)

    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(_activate_cmd(attestation))
    assert exc_info.value.code == "BREAKER_NOT_CLEAR"


def test_activate_rejects_wrong_account_binding(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    # Signed for ACCOUNT, but the trader's own current bindings expect a
    # DIFFERENT live account -- must never activate.
    attestation = _signed_attestation(controller, signer, account_id="DU_SOME_OTHER_ACCOUNT")
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier)

    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(_activate_cmd(attestation))
    assert exc_info.value.code == "BINDING_MISMATCH"
    assert machine.current_stage(STRATEGY) == PAPER_PASSED


def test_activate_rejects_expired_attestation(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer, expires_at=NOW + dt.timedelta(minutes=1))
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(
        machine, evidence, authority_store, verifier, now=lambda: NOW + dt.timedelta(hours=1),
    )

    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(_activate_cmd(attestation))
    assert exc_info.value.code == "AUTHORITY_EXPIRED"


def test_activate_rejects_revoked_attestation(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    digest = canary_payload_digest(attestation.unsigned_payload)

    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier)

    # First activation succeeds and records ISSUED.
    service.activate(_activate_cmd(attestation, command_id="activate-first"))
    authority_store.record_revoked(digest, reason="incident review revoked authority")

    # The revocation check inside verify() runs BEFORE the ALREADY_ACTIVE
    # stage check, so a revoked authority is refused even while the
    # strategy is still CANARY_ACTIVE from the first (pre-revocation) call.
    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(_activate_cmd(attestation, command_id="activate-retry-revoked"))
    assert exc_info.value.code == "AUTHORITY_REVOKED"


def test_activate_rejects_writable_artifact_bindings_failure(tmp_path):
    """When the trader's own fresh artifact re-verification fails (e.g. a
    writable bundle mount rejected by ArtifactVerifier in live mode), the
    activation must fail cleanly -- never crash opaquely."""
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])

    def _raising_bindings():
        raise RuntimeError("bundle mount is writable in live mode")

    service = _service(machine, evidence, authority_store, verifier, expected_bindings=_raising_bindings)

    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(_activate_cmd(attestation))
    assert exc_info.value.code == "ARTIFACT_VERIFICATION_FAILED"
    assert machine.current_stage(STRATEGY) == PAPER_PASSED


def test_activate_rejects_changed_policy_digests(tmp_path):
    """The trader's OWN current allowlist/ruleset digest has moved on since
    the authority was signed -- binding mismatch, never silently honored."""
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])

    changed_bindings = _expected_bindings(allowlist_digest="a-DIFFERENT-allowlist")
    service = _service(
        machine, evidence, authority_store, verifier, expected_bindings=lambda: changed_bindings,
    )

    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(_activate_cmd(attestation))
    assert exc_info.value.code == "BINDING_MISMATCH"


def test_activate_rejects_second_strategy_hand_built_payload(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    signer = _keypair()
    attestation = _hand_built_signed(signer, permitted_instruments=("AAPL", "MSFT"))
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier)

    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(_activate_cmd(attestation))
    assert exc_info.value.code == "POLICY_VIOLATION"
    assert machine.current_stage(STRATEGY) == PAPER_PASSED


def test_activate_rejects_paper_eligible_hand_built_payload(tmp_path):
    """CRITICAL: PAPER_ELIGIBLE must never authorize live activation."""
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    signer = _keypair()
    attestation = _hand_built_signed(signer, eligibility_state="PAPER_ELIGIBLE")
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier)

    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(_activate_cmd(attestation))
    assert exc_info.value.code == "POLICY_VIOLATION"
    assert machine.current_stage(STRATEGY) == PAPER_PASSED


def test_activate_already_active_rejected(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier)

    service.activate(_activate_cmd(attestation, command_id="activate-1"))
    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(_activate_cmd(attestation, command_id="activate-2"))
    assert exc_info.value.code == "ALREADY_ACTIVE"


def test_activate_malformed_attestation_rejected(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    signer = _keypair()
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier)

    cmd = CommandRequest(
        command_id="bad-1", action="activate_live_canary", account_id=ACCOUNT,
        target_type="strategy", target_id=STRATEGY, expected_version=None,
        body={"attestation": {"strategy_id": STRATEGY}, "reason": "x"}, source="operator",
    )
    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(cmd)
    assert exc_info.value.code == "ATTESTATION_MALFORMED"


def test_activate_missing_attestation_rejected(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    signer = _keypair()
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier)

    cmd = CommandRequest(
        command_id="bad-2", action="activate_live_canary", account_id=ACCOUNT,
        target_type="strategy", target_id=STRATEGY, expected_version=None,
        body={"reason": "x"}, source="operator",
    )
    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(cmd)
    assert exc_info.value.code == "ATTESTATION_REQUIRED"


# ---------------------------------------------------------------------------
# CanaryActivationService.deactivate
# ---------------------------------------------------------------------------
def test_deactivate_happy_path(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier)
    service.activate(_activate_cmd(attestation))

    outcome = service.deactivate(_deactivate_cmd())
    assert outcome["stage"] == CANARY_SUSPENDED
    assert machine.current_stage(STRATEGY) == CANARY_SUSPENDED

    digest = outcome["authority_digest"]
    latest = authority_store.latest(digest)
    assert latest.event == "DEACTIVATED"


def test_deactivate_requires_explicit_reason(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier)
    service.activate(_activate_cmd(attestation))

    with pytest.raises(CommandValidationError) as exc_info:
        service.deactivate(_deactivate_cmd(reason=""))
    assert exc_info.value.code == "REASON_REQUIRED"
    assert machine.current_stage(STRATEGY) == CANARY_ACTIVE


def test_deactivate_requires_currently_active(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    signer = _keypair()
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    service = _service(machine, evidence, authority_store, verifier)

    with pytest.raises(CommandValidationError) as exc_info:
        service.deactivate(_deactivate_cmd())
    assert exc_info.value.code == "NOT_ACTIVE"


# ---------------------------------------------------------------------------
# Coordinator-level replay / conflict semantics
# ---------------------------------------------------------------------------
class _FakeNonceGate:
    def __init__(self):
        self._consumed: set[str] = set()

    def consume_in_tx(self, conn, nonce, request) -> bool:
        if not nonce or nonce in self._consumed:
            return False
        self._consumed.add(nonce)
        return True


class _FakeCommandAudit:
    def record_in_tx(self, conn, request, **_kwargs) -> None:
        pass


def _coordinator(tmp_path, machine, evidence, authority_store, verifier, *, name="coord.duckdb"):
    db, migrator, journal = _db(tmp_path, name)
    apply_command_ledger_migration(migrator)
    ledger = CommandLedger(journal)
    coordinator = TradingCommandCoordinator(
        journal=journal, ledger=ledger, audit=_FakeCommandAudit(),
        nonces=_FakeNonceGate(), now=lambda: NOW,
    )
    service = _service(machine, evidence, authority_store, verifier)
    coordinator.register_action("activate_live_canary", service.activate, requires_preflight=True)
    coordinator.register_action("deactivate_live_canary", service.deactivate, requires_preflight=False)
    return coordinator


def test_activate_replay_of_same_command_id_is_idempotent(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    coordinator = _coordinator(tmp_path, machine, evidence, authority_store, verifier)

    request = CommandRequest(
        command_id="activate-replay-1", action="activate_live_canary", account_id=ACCOUNT,
        target_type="strategy", target_id=STRATEGY, expected_version=None,
        body={"attestation": canary_attestation_to_wire(attestation), "reason": "go live"},
        source="operator", preflight_nonce="nonce-1",
    )
    first = coordinator.execute(request)
    assert first.state == "RESOLVED"
    assert machine.current_stage(STRATEGY) == CANARY_ACTIVE

    # Exact retry: same command_id + same body -- must replay the SAME
    # receipt, not attempt to activate an already-active strategy again.
    replay = coordinator.execute(request)
    assert replay == first
    assert machine.current_stage(STRATEGY) == CANARY_ACTIVE


def test_activate_conflicting_command_id_with_different_body_is_rejected(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    coordinator = _coordinator(tmp_path, machine, evidence, authority_store, verifier)

    first = coordinator.execute(CommandRequest(
        command_id="activate-conflict-1", action="activate_live_canary", account_id=ACCOUNT,
        target_type="strategy", target_id=STRATEGY, expected_version=None,
        body={"attestation": canary_attestation_to_wire(attestation), "reason": "go live"},
        source="operator", preflight_nonce="nonce-a",
    ))
    assert first.state == "RESOLVED"

    conflict = coordinator.execute(CommandRequest(
        command_id="activate-conflict-1", action="activate_live_canary", account_id=ACCOUNT,
        target_type="strategy", target_id=STRATEGY, expected_version=None,
        body={"attestation": canary_attestation_to_wire(attestation), "reason": "DIFFERENT reason"},
        source="operator", preflight_nonce="nonce-b",
    ))
    assert conflict.error_code == "COMMAND_CONFLICT"
    assert conflict.retryable is False


def test_deactivate_then_activate_round_trip_through_coordinator(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    coordinator = _coordinator(tmp_path, machine, evidence, authority_store, verifier)

    coordinator.execute(CommandRequest(
        command_id="rt-activate", action="activate_live_canary", account_id=ACCOUNT,
        target_type="strategy", target_id=STRATEGY, expected_version=None,
        body={"attestation": canary_attestation_to_wire(attestation), "reason": "go live"},
        source="operator", preflight_nonce="nonce-rt-1",
    ))
    assert machine.current_stage(STRATEGY) == CANARY_ACTIVE

    deactivate_receipt = coordinator.execute(CommandRequest(
        command_id="rt-deactivate", action="deactivate_live_canary", account_id=ACCOUNT,
        target_type="strategy", target_id=STRATEGY, expected_version=None,
        body={"strategy_id": STRATEGY, "reason": "pause for review"}, source="operator",
    ))
    assert deactivate_receipt.state == "RESOLVED"
    assert machine.current_stage(STRATEGY) == CANARY_SUSPENDED


# ---------------------------------------------------------------------------
# Migration 43 + LiveActivationAuthorityStore
# ---------------------------------------------------------------------------
def test_migration_43_creates_live_activation_authority_table(tmp_path):
    db, migrator, _ = _db(tmp_path, "mig43.duckdb")
    assert apply_live_activation_authority_migration(migrator) is True
    assert CANARY_AUTHORITY_MIGRATION_VERSIONS == (43,)
    assert apply_live_activation_authority_migration(migrator) is False

    cols = {row[1] for row in db.execute("PRAGMA table_info('live_activation_authority')", fetch="all")}
    assert {"authority_digest", "strategy_id", "event", "command_id"} <= cols


def test_authority_store_append_only_lifecycle(tmp_path):
    machine, evidence, authority_store, *_ = _machine_and_evidence(tmp_path)
    _promote_to_paper_passed(machine, evidence)
    controller = _controller(machine, evidence)
    signer = _keypair()
    attestation = _signed_attestation(controller, signer)
    verifier = CanaryAuthorityVerifier(trusted_public_keys=[signer.public_key])
    verified = verifier.verify(attestation, _expected_bindings(), now=NOW)

    digest = authority_store.record_issued(
        attestation, verified, operator="operator:alice", reason="ready",
    )
    # Idempotent: re-issuing the SAME (identical-digest) authority is a
    # safe no-op, not a duplicate row.
    assert authority_store.record_issued(
        attestation, verified, operator="operator:alice", reason="ready",
    ) == digest
    assert len(authority_store.history(digest)) == 1

    authority_store.record_activated(digest, command_id="cmd-1")
    authority_store.record_deactivated(digest, command_id="cmd-2", reason="pause")
    authority_store.record_revoked(digest, reason="incident")

    history = authority_store.history(digest)
    assert [e.event for e in history] == ["ISSUED", "ACTIVATED", "DEACTIVATED", "REVOKED"]
    assert authority_store.is_revoked(digest) is True
    assert digest in authority_store.revoked_digests()


def test_authority_store_operations_on_unknown_digest_raise(tmp_path):
    _, _, authority_store, *_ = _machine_and_evidence(tmp_path)
    with pytest.raises(ValueError):
        authority_store.record_activated("no-such-digest", command_id="cmd-1")
    with pytest.raises(ValueError):
        authority_store.record_deactivated("no-such-digest", command_id="cmd-1", reason="x")
    with pytest.raises(ValueError):
        authority_store.record_revoked("no-such-digest", reason="x")
