"""P5 — breaker trip automatically suspends active allocation authority."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519

from trader.data.allocation_authority_store import (
    AllocationAuthorityStore,
    apply_allocation_authority_migrations,
)
from trader.data.circuit_breaker_store import CircuitBreakerStore, apply_circuit_breaker_migration
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.promotion.allocation_attestation import (
    STAGE_SCALE_2,
    AllocationAttestationVerifier,
    ExpectedAllocationBindings,
    build_allocation_payload,
    sign_allocation_payload,
)
from trader.promotion.degradation_reaction import react_to_breaker_trip
from trader.research.signing import AttestationSigner
from trader.trading.circuit_breaker import BreakerSignal, CircuitBreaker

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
ACCOUNT = "DU9000001"
ARTIFACT = "artifact-digest-1"
ALLOW = "allow-digest-1"
RULES = "rule-digest-1"
EVIDENCE = "evidence-digest-1"
STRATEGY = "orb_breakout"


def _db(tmp_path: Path):
    db = DuckDBConnection.get_instance(str(tmp_path / "degrade_wire.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_allocation_authority_migrations(migrator)
    apply_circuit_breaker_migration(migrator)
    return db, journal


def _activate(store: AllocationAuthorityStore) -> str:
    signer = AttestationSigner(ed25519.Ed25519PrivateKey.generate())
    unsigned = build_allocation_payload(
        strategy_id=STRATEGY,
        account_id=ACCOUNT,
        account_mode="live",
        stage=STAGE_SCALE_2,
        artifact_digest=ARTIFACT,
        allowlist_digest=ALLOW,
        ruleset_digest=RULES,
        max_gross_allocation=0.10,
        evidence_digest=EVIDENCE,
        issued_at=NOW,
        expires_at=NOW + dt.timedelta(days=30),
        operator="operator:alice",
        reason="scale",
        public_key_id=signer.public_key_id,
    )
    attestation = sign_allocation_payload(signer, unsigned)
    verified = AllocationAttestationVerifier(
        trusted_public_keys={signer.public_key_id: signer.public_key},
    ).verify(
        attestation,
        expected=ExpectedAllocationBindings(
            account_id=ACCOUNT,
            account_mode="live",
            artifact_digest=ARTIFACT,
            allowlist_digest=ALLOW,
            ruleset_digest=RULES,
            strategy_id=STRATEGY,
        ),
        now=NOW,
    )
    digest = store.record_issued(attestation, verified, operator="op", reason="issued")
    store.record_activated(digest, command_id="cmd-1")
    return digest


def test_react_to_breaker_trip_suspends_active_authority(tmp_path):
    db, journal = _db(tmp_path)
    store = AllocationAuthorityStore(journal=journal, db=db, now=lambda: NOW)
    _activate(store)

    class _State:
        reason_code = "DAILY_LOSS_BREACH"
        state = "TRIPPED"

    record = react_to_breaker_trip(
        store, account_id=ACCOUNT, breaker_state=_State(), now=NOW,
    )
    assert record is not None
    assert record.max_gross_allocation == 0.0
    assert store.active_for_account(ACCOUNT, now=NOW) is None


def test_circuit_breaker_on_trip_wires_allocation_suspend(tmp_path):
    db, journal = _db(tmp_path)
    alloc = AllocationAuthorityStore(journal=journal, db=db, now=lambda: NOW)
    _activate(alloc)
    breaker_store = CircuitBreakerStore(journal, ACCOUNT)
    breaker_store.seed(NOW)
    clock = [NOW]
    breaker = CircuitBreaker(
        breaker_store,
        now=lambda: clock[0],
        reset_ready=lambda: True,
        reconciliation_complete=lambda: True,
        session_key=lambda value: value.date().isoformat(),
        on_trip=lambda state: react_to_breaker_trip(
            alloc, account_id=ACCOUNT, breaker_state=state, now=clock[0],
        ),
    )
    state = breaker.record(BreakerSignal("DAILY_LOSS_BREACH", NOW, "loss"))
    assert state.state == "TRIPPED"
    assert alloc.active_for_account(ACCOUNT, now=NOW) is None
