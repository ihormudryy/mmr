"""P5 Task 1 — signed allocation authority attestations and store."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from trader.data.allocation_authority_store import (
    ALLOCATION_AUTHORITY_MIGRATION_VERSIONS,
    AllocationAuthorityStore,
    apply_allocation_authority_migrations,
)
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.promotion.allocation_attestation import (
    ALLOCATION_PAYLOAD_PREFIX,
    STAGE_CANARY,
    STAGE_MAX_CEILING,
    STAGE_SCALE_1,
    STAGE_SCALE_2,
    STAGE_STEADY,
    AllocationAttestation,
    AllocationAttestationSigner,
    AllocationAttestationVerifier,
    AllocationBadSignature,
    AllocationBindingMismatch,
    AllocationCeilingExceeded,
    AllocationExpired,
    AllocationRevoked,
    AllocationUnknownKey,
    ExpectedAllocationBindings,
    build_allocation_payload,
    allocation_attestation_from_wire,
    allocation_attestation_to_wire,
    allocation_payload_digest,
    sign_allocation_payload,
)
from trader.research.signing import AttestationSigner

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
STRATEGY = "orb_breakout"
ACCOUNT = "DU9000001"
ARTIFACT = "artifact-digest-1"
ALLOW = "allow-digest-1"
RULES = "rule-digest-1"
EVIDENCE = "evidence-digest-1"


def _db(tmp_path: Path, name: str = "alloc.duckdb"):
    db = DuckDBConnection.get_instance(str(tmp_path / name))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    return db, migrator, journal


def _store(tmp_path: Path, *, now=lambda: NOW):
    db, migrator, journal = _db(tmp_path)
    apply_allocation_authority_migrations(migrator)
    return AllocationAuthorityStore(journal=journal, db=db, now=now), db, migrator


def _keypair() -> AttestationSigner:
    return AttestationSigner(ed25519.Ed25519PrivateKey.generate())


def _unsigned(
    *,
    stage=STAGE_CANARY,
    max_gross_allocation=0.05,
    account_mode="live",
    public_key_id="ed25519-fake",
    issued_at=None,
    expires_at=None,
):
    issued = issued_at or NOW
    return build_allocation_payload(
        strategy_id=STRATEGY,
        account_id=ACCOUNT,
        account_mode=account_mode,
        stage=stage,
        artifact_digest=ARTIFACT,
        allowlist_digest=ALLOW,
        ruleset_digest=RULES,
        max_gross_allocation=max_gross_allocation,
        evidence_digest=EVIDENCE,
        issued_at=issued,
        expires_at=expires_at or (issued + dt.timedelta(days=30)),
        operator="operator:alice",
        reason="scale authority",
        public_key_id=public_key_id,
    )


def _signed(signer: AttestationSigner, **kwargs) -> AllocationAttestation:
    unsigned = _unsigned(public_key_id=signer.public_key_id, **kwargs)
    return sign_allocation_payload(signer, unsigned)


def _bindings(**kwargs) -> ExpectedAllocationBindings:
    return ExpectedAllocationBindings(
        account_id=ACCOUNT,
        account_mode=kwargs.get("account_mode", "live"),
        artifact_digest=ARTIFACT,
        allowlist_digest=ALLOW,
        ruleset_digest=RULES,
        strategy_id=STRATEGY,
    )


def _verify(signer: AttestationSigner, attestation, *, now=NOW, bindings=None):
    verifier = AllocationAttestationVerifier(trusted_public_keys={signer.public_key_id: signer.public_key})
    return verifier.verify(attestation, expected=bindings or _bindings(), now=now)


# ---------------------------------------------------------------------------
# Stage ceiling enforcement
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("stage", "ceiling", "signed"),
    [
        (STAGE_CANARY, 0.06, 0.06),
        (STAGE_SCALE_1, 0.09, 0.08),
        (STAGE_SCALE_2, 0.135, 0.10),
        (STAGE_STEADY, 0.15, 0.12),
    ],
)
def test_stage_ceilings_allow_at_or_below_cap(stage, ceiling, signed):
    payload = build_allocation_payload(
        strategy_id=STRATEGY,
        account_id=ACCOUNT,
        account_mode="live",
        stage=stage,
        artifact_digest=ARTIFACT,
        allowlist_digest=ALLOW,
        ruleset_digest=RULES,
        max_gross_allocation=signed,
        evidence_digest=EVIDENCE,
        issued_at=NOW,
        expires_at=NOW + dt.timedelta(days=7),
        operator="operator:alice",
        reason="ok",
        public_key_id="ed25519-fake",
    )
    assert payload["stage"] == stage
    assert payload["max_gross_allocation"] == signed
    assert Decimal(str(signed)) <= STAGE_MAX_CEILING[stage]


@pytest.mark.parametrize(
    ("stage", "over"),
    [
        (STAGE_CANARY, 0.061),
        (STAGE_SCALE_1, 0.091),
        (STAGE_SCALE_2, 0.136),
        (STAGE_STEADY, 0.151),
    ],
)
def test_stage_ceilings_reject_above_cap(stage, over):
    with pytest.raises(AllocationCeilingExceeded):
        build_allocation_payload(
            strategy_id=STRATEGY,
            account_id=ACCOUNT,
            account_mode="live",
            stage=stage,
            artifact_digest=ARTIFACT,
            allowlist_digest=ALLOW,
            ruleset_digest=RULES,
            max_gross_allocation=over,
            evidence_digest=EVIDENCE,
            issued_at=NOW,
            expires_at=NOW + dt.timedelta(days=7),
            operator="operator:alice",
            reason="too high",
            public_key_id="ed25519-fake",
        )


# ---------------------------------------------------------------------------
# Sign / verify round trip
# ---------------------------------------------------------------------------
def test_sign_verify_round_trip():
    signer = _keypair()
    attestation = _signed(signer)
    verified = _verify(signer, attestation)
    assert verified.stage == STAGE_CANARY
    assert verified.max_gross_allocation == 0.05
    assert verified.payload_digest == allocation_payload_digest(attestation)


def test_payload_uses_allocation_namespace():
    from trader.promotion.allocation_attestation import allocation_unsigned_payload
    from trader.research.canonical import sha256_digest

    unsigned = _unsigned()
    digest = allocation_payload_digest(unsigned)
    expected = sha256_digest(ALLOCATION_PAYLOAD_PREFIX, allocation_unsigned_payload(unsigned))
    assert digest == expected
    assert digest != sha256_digest("canary_activation_authority", allocation_unsigned_payload(unsigned))


def test_offline_signer_wrapper_never_exposes_private_material():
    signer = _keypair()
    offline = AllocationAttestationSigner(signer)
    attestation = offline.sign(_unsigned(public_key_id=signer.public_key_id))
    assert attestation.public_key_id == offline.public_key_id
    assert "PRIVATE" not in repr(offline)


# ---------------------------------------------------------------------------
# Tampering / binding failures
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "DU9999999"),
        ("account_mode", "paper"),
        ("stage", STAGE_SCALE_1),
        ("artifact_digest", "tampered-artifact"),
        ("allowlist_digest", "tampered-allow"),
        ("ruleset_digest", "tampered-rules"),
        ("max_gross_allocation", 0.04),
        ("evidence_digest", "tampered-evidence"),
        ("public_key_id", "ed25519-other"),
    ],
)
def test_tampered_field_rejects(field, value):
    signer = _keypair()
    attestation = _signed(signer)
    wire = allocation_attestation_to_wire(attestation)
    wire[field] = value
    tampered = allocation_attestation_from_wire(wire)
    with pytest.raises((AllocationBadSignature, AllocationBindingMismatch, AllocationUnknownKey)):
        _verify(signer, tampered)


def test_expired_attestation_rejects():
    signer = _keypair()
    attestation = _signed(
        signer,
        issued_at=NOW - dt.timedelta(days=2),
        expires_at=NOW - dt.timedelta(seconds=1),
    )
    with pytest.raises(AllocationExpired):
        _verify(signer, attestation, now=NOW)


def test_untrusted_key_rejects():
    signer = _keypair()
    attestation = _signed(signer)
    verifier = AllocationAttestationVerifier(trusted_public_keys={})
    with pytest.raises(AllocationUnknownKey):
        verifier.verify(attestation, expected=_bindings(), now=NOW)


def test_revoked_key_rejects():
    signer = _keypair()
    attestation = _signed(signer)
    verifier = AllocationAttestationVerifier(
        trusted_public_keys={signer.public_key_id: signer.public_key},
        revoked_key_ids=frozenset({signer.public_key_id}),
    )
    with pytest.raises(AllocationRevoked):
        verifier.verify(attestation, expected=_bindings(), now=NOW)


def test_binding_mismatch_rejects():
    signer = _keypair()
    attestation = _signed(signer)
    with pytest.raises(AllocationBindingMismatch):
        _verify(
            signer,
            attestation,
            bindings=ExpectedAllocationBindings(
                account_id="OTHER",
                account_mode="live",
                artifact_digest=ARTIFACT,
                allowlist_digest=ALLOW,
                ruleset_digest=RULES,
                strategy_id=STRATEGY,
            ),
        )


def test_signature_tamper_rejects():
    signer = _keypair()
    attestation = _signed(signer)
    wire = allocation_attestation_to_wire(attestation)
    wire["signature"] = wire["signature"][:-4] + "XXXX"
    tampered = allocation_attestation_from_wire(wire)
    with pytest.raises(AllocationBadSignature):
        _verify(signer, tampered)


# ---------------------------------------------------------------------------
# Migrations + store lifecycle
# ---------------------------------------------------------------------------
def test_migrations_50_51_idempotent(tmp_path):
    db, migrator, _ = _db(tmp_path, "mig.duckdb")
    assert apply_allocation_authority_migrations(migrator) is True
    assert ALLOCATION_AUTHORITY_MIGRATION_VERSIONS == (50, 51)
    assert apply_allocation_authority_migrations(migrator) is False

    auth_cols = {row[1] for row in db.execute("PRAGMA table_info('allocation_authorities')", fetch="all")}
    event_cols = {row[1] for row in db.execute("PRAGMA table_info('allocation_authority_events')", fetch="all")}
    assert {"authority_digest", "stage", "event", "superseded_by_digest"} <= auth_cols
    assert {"authority_digest", "event", "account_id", "artifact_digest"} <= event_cols


def test_store_append_only_lifecycle(tmp_path):
    store, *_ = _store(tmp_path)
    signer = _keypair()
    attestation = _signed(signer)
    verified = _verify(signer, attestation)

    digest = store.record_issued(
        attestation, verified, operator="operator:alice", reason="ready",
    )
    assert store.record_issued(
        attestation, verified, operator="operator:alice", reason="ready",
    ) == digest
    assert len(store.history(digest)) == 1

    store.record_activated(digest, command_id="cmd-1")
    store.record_deactivated(digest, command_id="cmd-2", reason="pause")
    store.record_revoked(digest, reason="incident")

    history = store.history(digest)
    assert [e.event for e in history] == ["ISSUED", "ACTIVATED", "DEACTIVATED", "REVOKED"]
    assert store.is_revoked(digest) is True
    assert digest in store.revoked_digests()


def test_active_for_one_authority_per_account_artifact(tmp_path):
    store, *_ = _store(tmp_path)
    signer = _keypair()

    first = _signed(signer, max_gross_allocation=0.04)
    first_verified = _verify(signer, first)
    first_digest = store.record_issued(first, first_verified, operator="op", reason="v1")
    store.record_activated(first_digest, command_id="cmd-1")

    second = _signed(signer, max_gross_allocation=0.05, expires_at=NOW + dt.timedelta(days=60))
    second_verified = _verify(signer, second)
    second_digest = store.record_issued(second, second_verified, operator="op", reason="v2")
    store.record_superseded(first_digest, superseded_by_digest=second_digest, reason="upgrade")
    store.record_activated(second_digest, command_id="cmd-2")

    active = store.active_for(ACCOUNT, ARTIFACT, now=NOW)
    assert active is not None
    assert active.authority_digest == second_digest
    assert store.is_superseded(first_digest) is True
    assert store.active_for(ACCOUNT, ARTIFACT, now=NOW + dt.timedelta(days=90)) is None


def test_stale_replay_after_restart_persists(tmp_path):
    store, db, migrator = _store(tmp_path, now=lambda: NOW)
    signer = _keypair()
    attestation = _signed(signer)
    verified = _verify(signer, attestation)
    digest = store.record_issued(attestation, verified, operator="op", reason="persist")
    store.record_activated(digest, command_id="cmd-1")

    db2 = DuckDBConnection.get_instance(str(tmp_path / "alloc.duckdb"))
    journal2 = DomainJournal(db2)
    store2 = AllocationAuthorityStore(journal=journal2, db=db2, now=lambda: NOW)
    active = store2.active_for(ACCOUNT, ARTIFACT, now=NOW)
    assert active is not None
    assert active.authority_digest == digest


def test_store_unknown_digest_operations_raise(tmp_path):
    store, *_ = _store(tmp_path)
    with pytest.raises(ValueError):
        store.record_activated("missing", command_id="cmd-1")
    with pytest.raises(ValueError):
        store.record_deactivated("missing", command_id="cmd-1", reason="x")
    with pytest.raises(ValueError):
        store.record_revoked("missing", reason="x")
    with pytest.raises(ValueError):
        store.record_superseded("missing", superseded_by_digest="other", reason="x")
