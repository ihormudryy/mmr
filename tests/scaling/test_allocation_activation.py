"""P5 Task 3 — signed allocation authority activation wiring."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from trader.data.allocation_authority_store import AllocationAuthorityStore, apply_allocation_authority_migrations
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_coordinator import CommandRequest, CommandValidationError
from trader.promotion.allocation_attestation import (
    STAGE_SCALE_1,
    AllocationAttestationVerifier,
    ExpectedAllocationBindings,
    allocation_attestation_to_wire,
    sign_allocation_payload,
    build_allocation_payload,
)
from trader.promotion.controller import AllocationActivationService
from trader.research.signing import AttestationSigner

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
STRATEGY = "orb_breakout"
ACCOUNT = "DU9000001"
ARTIFACT = "artifact-digest-1"
ALLOW = "allow-digest-1"
RULES = "rule-digest-1"
EVIDENCE = "evidence-digest-1"


def _db(tmp_path: Path):
    db = DuckDBConnection.get_instance(str(tmp_path / "alloc_activate.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_allocation_authority_migrations(migrator)
    return db, journal


def _store(tmp_path: Path) -> AllocationAuthorityStore:
    db, journal = _db(tmp_path)
    return AllocationAuthorityStore(journal=journal, db=db, now=lambda: NOW)


def _keypair() -> AttestationSigner:
    return AttestationSigner(ed25519.Ed25519PrivateKey.generate())


def _bindings() -> ExpectedAllocationBindings:
    return ExpectedAllocationBindings(
        account_id=ACCOUNT,
        account_mode="live",
        artifact_digest=ARTIFACT,
        allowlist_digest=ALLOW,
        ruleset_digest=RULES,
        strategy_id=STRATEGY,
    )


def _signed(signer: AttestationSigner):
    unsigned = build_allocation_payload(
        strategy_id=STRATEGY,
        account_id=ACCOUNT,
        account_mode="live",
        stage=STAGE_SCALE_1,
        artifact_digest=ARTIFACT,
        allowlist_digest=ALLOW,
        ruleset_digest=RULES,
        max_gross_allocation=0.08,
        evidence_digest=EVIDENCE,
        issued_at=NOW,
        expires_at=NOW + dt.timedelta(days=30),
        operator="operator:alice",
        reason="scale 1",
        public_key_id=signer.public_key_id,
    )
    return sign_allocation_payload(signer, unsigned)


def _service(
    tmp_path: Path,
    signer: AttestationSigner,
    *,
    readiness=True,
    flat=True,
    breaker=True,
) -> AllocationActivationService:
    store = _store(tmp_path)
    verifier = AllocationAttestationVerifier(
        trusted_public_keys={signer.public_key_id: signer.public_key},
    )
    return AllocationActivationService(
        authority_store=store,
        verifier=verifier,
        expected_bindings=_bindings,
        semantic_readiness_ready=lambda: readiness,
        broker_flat_reconciled=lambda: flat,
        breaker_clear=lambda: breaker,
        now=lambda: NOW,
    )


def _activate_cmd(attestation, *, command_id="alloc-activate-1", source="operator"):
    return CommandRequest(
        command_id=command_id,
        action="activate_allocation",
        account_id=ACCOUNT,
        target_type="allocation_authority",
        target_id=STRATEGY,
        expected_version=None,
        body={"attestation": allocation_attestation_to_wire(attestation), "reason": "operator approved"},
        source=source,
    )


def test_activate_happy_path(tmp_path):
    signer = _keypair()
    service = _service(tmp_path, signer)
    outcome = service.activate(_activate_cmd(_signed(signer)))
    assert outcome["strategy_id"] == STRATEGY
    assert outcome["stage"] == STAGE_SCALE_1
    assert outcome["authority_digest"]


def test_activate_rejects_non_operator_source(tmp_path):
    signer = _keypair()
    service = _service(tmp_path, signer)
    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(_activate_cmd(_signed(signer), source="dashboard"))
    assert exc_info.value.code == "AUTOMATIC_ACTIVATION_FORBIDDEN"


def test_activate_rejects_when_readiness_not_met(tmp_path):
    signer = _keypair()
    service = _service(tmp_path, signer, readiness=False)
    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(_activate_cmd(_signed(signer)))
    assert exc_info.value.code == "READINESS_NOT_MET"


def test_activate_rejects_malformed_attestation(tmp_path):
    signer = _keypair()
    service = _service(tmp_path, signer)
    cmd = CommandRequest(
        command_id="bad-1",
        action="activate_allocation",
        account_id=ACCOUNT,
        target_type="allocation_authority",
        target_id=STRATEGY,
        expected_version=None,
        body={"attestation": {"strategy_id": STRATEGY}, "reason": "x"},
        source="operator",
    )
    with pytest.raises(CommandValidationError) as exc_info:
        service.activate(cmd)
    assert exc_info.value.code == "ATTESTATION_MALFORMED"


def _suspend_cmd(*, command_id="alloc-suspend-1", source="operator", account_id=ACCOUNT):
    return CommandRequest(
        command_id=command_id,
        action="suspend_allocation",
        account_id=account_id,
        target_type="allocation_authority",
        target_id=account_id,
        expected_version=None,
        body={"reason": "operator suspend"},
        source=source,
    )


def test_suspend_happy_path(tmp_path):
    signer = _keypair()
    service = _service(tmp_path, signer)
    activated = service.activate(_activate_cmd(_signed(signer)))
    outcome = service.suspend(_suspend_cmd())
    assert outcome["strategy_id"] == STRATEGY
    assert outcome["authority_digest"] == activated["authority_digest"]
    assert outcome["event"] == "DEACTIVATED"
    assert service._authority_store.active_for_account(ACCOUNT, now=NOW) is None


def test_suspend_rejects_when_not_active(tmp_path):
    signer = _keypair()
    service = _service(tmp_path, signer)
    with pytest.raises(CommandValidationError) as exc_info:
        service.suspend(_suspend_cmd())
    assert exc_info.value.code == "NOT_ACTIVE"


def test_suspend_rejects_non_operator_source(tmp_path):
    signer = _keypair()
    service = _service(tmp_path, signer)
    service.activate(_activate_cmd(_signed(signer)))
    with pytest.raises(CommandValidationError) as exc_info:
        service.suspend(_suspend_cmd(source="dashboard"))
    assert exc_info.value.code == "AUTOMATIC_SUSPEND_FORBIDDEN"
