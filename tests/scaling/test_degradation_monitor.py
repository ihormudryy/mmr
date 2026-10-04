"""P5 Task 4 — degradation monitor and restrictive allocation overrides."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from trader.data.allocation_authority_store import (
    ALLOCATION_AUTHORITY_MIGRATION_VERSIONS,
    EVENT_OVERRIDE,
    AllocationAuthorityStore,
    AllocationOverrideRejected,
    apply_allocation_authority_migrations,
)
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.promotion.allocation_attestation import (
    STAGE_CANARY,
    STAGE_SCALE_1,
    STAGE_SCALE_2,
    STAGE_STEADY,
    AllocationAttestationSigner,
    AllocationAttestationVerifier,
    ExpectedAllocationBindings,
    build_allocation_payload,
    sign_allocation_payload,
)
from trader.promotion.degradation_monitor import (
    TRIGGER_BREAKER_TRIP,
    TRIGGER_CAPACITY_BREACH,
    TRIGGER_COST_BREACH,
    TRIGGER_DRAWDOWN_BREACH,
    TRIGGER_NEGATIVE_EXPECTANCY,
    TRIGGER_REPLAY_MISMATCH,
    TRIGGER_STALE_EVIDENCE,
    DegradationAction,
    DegradationMonitor,
)
from trader.promotion.evidence_store import EvidenceWindow
from trader.research.signing import AttestationSigner

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
STRATEGY = "orb_breakout"
ACCOUNT = "DU9000001"
ARTIFACT = "artifact-digest-1"
ALLOW = "allow-digest-1"
RULES = "rule-digest-1"
EVIDENCE = "evidence-digest-1"


def _window(**kwargs) -> EvidenceWindow:
    defaults = dict(
        strategy_id=STRATEGY,
        as_of=NOW,
        window_reset_at=None,
        first_event_at=NOW - dt.timedelta(days=40),
        last_event_at=NOW,
        calendar_days=("2026-07-01", "2026-07-20"),
        session_ids=("s01", "s02"),
        round_trip_ids=("rt-1", "rt-2"),
        instrument_ids=("1001", "1002"),
        corrections=(),
        breaker_trips=(),
        cost_breaches=(),
        drawdown_breaches=(),
        divergences=(),
        duplicates=(),
        unresolved_alerts=(),
        missed_flats=(),
        replay_mismatches=(),
        stale=False,
        event_count=2,
        round_trip_records=(
            {
                "round_trip_id": "rt-1",
                "instrument_id": "1001",
                "pnl_after_cost": Decimal("5.0"),
            },
            {
                "round_trip_id": "rt-2",
                "instrument_id": "1002",
                "pnl_after_cost": Decimal("3.0"),
            },
        ),
    )
    defaults.update(kwargs)
    return EvidenceWindow(**defaults)


def _db(tmp_path: Path, name: str = "degradation.duckdb"):
    db = DuckDBConnection.get_instance(str(tmp_path / name))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    return db, migrator, journal


def _store(tmp_path: Path):
    db, migrator, journal = _db(tmp_path)
    apply_allocation_authority_migrations(migrator)
    return AllocationAuthorityStore(journal=journal, db=db, now=lambda: NOW), db


def _keypair() -> AttestationSigner:
    return AttestationSigner(ed25519.Ed25519PrivateKey.generate())


def _signed(signer: AttestationSigner, *, stage=STAGE_SCALE_2, max_gross=0.10):
    unsigned = build_allocation_payload(
        strategy_id=STRATEGY,
        account_id=ACCOUNT,
        account_mode="live",
        stage=stage,
        artifact_digest=ARTIFACT,
        allowlist_digest=ALLOW,
        ruleset_digest=RULES,
        max_gross_allocation=max_gross,
        evidence_digest=EVIDENCE,
        issued_at=NOW,
        expires_at=NOW + dt.timedelta(days=30),
        operator="operator:alice",
        reason="scale authority",
        public_key_id=signer.public_key_id,
    )
    return sign_allocation_payload(signer, unsigned)


def _verify(signer: AttestationSigner, attestation):
    verifier = AllocationAttestationVerifier(
        trusted_public_keys={signer.public_key_id: signer.public_key},
    )
    return verifier.verify(
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


def _activate(store: AllocationAuthorityStore, signer: AttestationSigner, **kwargs) -> str:
    attestation = _signed(signer, **kwargs)
    verified = _verify(signer, attestation)
    digest = store.record_issued(attestation, verified, operator="op", reason="issued")
    store.record_activated(digest, command_id="cmd-activate")
    return digest


@pytest.mark.parametrize(
    ("window_kwargs", "expected_trigger", "expected_action"),
    [
        ({"cost_breaches": ({"incident_id": "c1"},)}, TRIGGER_COST_BREACH, DegradationAction.REDUCE_TO_PREVIOUS_STAGE),
        (
            {
                "round_trip_records": (
                    {"round_trip_id": "rt-1", "pnl_after_cost": Decimal("-1.0")},
                    {"round_trip_id": "rt-2", "pnl_after_cost": Decimal("-2.0")},
                ),
            },
            TRIGGER_NEGATIVE_EXPECTANCY,
            DegradationAction.REDUCE_TO_PREVIOUS_STAGE,
        ),
        ({"drawdown_breaches": ({"incident_id": "d1"},)}, TRIGGER_DRAWDOWN_BREACH, DegradationAction.SUSPEND),
        ({"stale": True}, TRIGGER_STALE_EVIDENCE, DegradationAction.SUSPEND),
        ({"replay_mismatches": ({"incident_id": "r1"},)}, TRIGGER_REPLAY_MISMATCH, DegradationAction.SUSPEND),
        ({"breaker_trips": ({"incident_id": "b1"},)}, TRIGGER_BREAKER_TRIP, DegradationAction.SUSPEND),
    ],
)
def test_each_trigger_maps_to_deterministic_action(window_kwargs, expected_trigger, expected_action):
    decision = DegradationMonitor().evaluate(
        _window(**window_kwargs),
        STAGE_SCALE_2,
        current_max_gross=0.10,
    )
    assert expected_trigger in decision.triggers
    assert decision.action is expected_action


def test_capacity_breach_warns_without_override():
    decision = DegradationMonitor().evaluate(
        _window(),
        STAGE_SCALE_2,
        capacity_breach_signals=({"metric": "participation_rate", "observed": 0.3},),
    )
    assert decision.action is DegradationAction.WARN
    assert TRIGGER_CAPACITY_BREACH in decision.triggers
    assert decision.requires_override is False


def test_safety_trigger_wins_over_capacity_warn():
    decision = DegradationMonitor().evaluate(
        _window(breaker_trips=({"incident_id": "b1"},)),
        STAGE_SCALE_2,
        capacity_breach_signals=({"metric": "spread_paid"},),
    )
    assert decision.action is DegradationAction.SUSPEND
    assert TRIGGER_BREAKER_TRIP in decision.triggers


def test_reduce_recommends_previous_stage_and_ceiling():
    decision = DegradationMonitor().evaluate(
        _window(cost_breaches=({"incident_id": "c1"},)),
        STAGE_SCALE_2,
        current_max_gross=0.10,
    )
    assert decision.recommended_stage == STAGE_SCALE_1
    assert decision.recommended_max_gross == pytest.approx(0.09)
    assert decision.requires_override is True


def test_canary_economic_failure_retires():
    decision = DegradationMonitor().evaluate(
        _window(
            round_trip_records=(
                {"round_trip_id": "rt-1", "pnl_after_cost": Decimal("-1.0")},
            ),
        ),
        STAGE_CANARY,
        current_max_gross=0.05,
    )
    assert decision.action is DegradationAction.RETIRE
    assert decision.recommended_max_gross == 0.0


def test_clean_window_has_no_action():
    decision = DegradationMonitor().evaluate(_window(), STAGE_SCALE_2)
    assert decision.action is None
    assert decision.triggers == ()


def test_apply_override_reduces_stage_and_ceiling(tmp_path):
    store, _ = _store(tmp_path)
    signer = _keypair()
    _activate(store, signer, stage=STAGE_SCALE_2, max_gross=0.10)

    record = store.apply_override(
        ACCOUNT,
        ARTIFACT,
        new_stage=STAGE_SCALE_1,
        new_max_gross=0.08,
        reason="cost breach",
    )
    assert record.event == EVENT_OVERRIDE
    assert record.stage == STAGE_SCALE_1
    assert record.max_gross_allocation == pytest.approx(0.08)

    active = store.active_for(ACCOUNT, ARTIFACT, now=NOW)
    assert active is not None
    assert active.stage == STAGE_SCALE_1
    assert active.max_gross_allocation == pytest.approx(0.08)


@pytest.mark.parametrize(
    ("new_stage", "new_max_gross"),
    [
        (STAGE_STEADY, 0.10),
        (STAGE_SCALE_2, 0.11),
        (STAGE_SCALE_2, 0.10),
    ],
)
def test_apply_override_rejects_increases(tmp_path, new_stage, new_max_gross):
    store, _ = _store(tmp_path)
    signer = _keypair()
    _activate(store, signer, stage=STAGE_SCALE_2, max_gross=0.10)

    with pytest.raises(AllocationOverrideRejected):
        store.apply_override(
            ACCOUNT,
            ARTIFACT,
            new_stage=new_stage,
            new_max_gross=new_max_gross,
            reason="invalid increase",
        )


def test_suspend_override_zeroes_active_authority(tmp_path):
    store, _ = _store(tmp_path)
    signer = _keypair()
    _activate(store, signer, stage=STAGE_SCALE_2, max_gross=0.10)

    store.apply_override(
        ACCOUNT,
        ARTIFACT,
        new_stage=STAGE_SCALE_2,
        new_max_gross=0.0,
        reason="drawdown breach",
    )
    assert store.active_for(ACCOUNT, ARTIFACT, now=NOW) is None


def test_migrations_include_override_event(tmp_path):
    db, migrator, _ = _db(tmp_path)
    assert apply_allocation_authority_migrations(migrator) is True
    assert ALLOCATION_AUTHORITY_MIGRATION_VERSIONS == (50, 51, 52)
    assert apply_allocation_authority_migrations(migrator) is False

    db.execute(
        "INSERT INTO allocation_authorities "
        "(authority_digest, strategy_id, account_id, account_mode, stage, "
        "artifact_digest, allowlist_digest, ruleset_digest, max_gross_allocation, "
        "evidence_digest, public_key_id, operator, reason, issued_at, expires_at, "
        "event, recorded_at) VALUES "
        "('d', 's', 'a', 'live', 'CANARY', 'art', 'al', 'ru', 0.05, 'ev', 'k', "
        "'op', 'r', ?, ?, 'OVERRIDE', ?)",
        [NOW, NOW + dt.timedelta(days=1), NOW],
    )
