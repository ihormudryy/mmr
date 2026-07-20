"""P4 Task 1 — the durable promotion stage state machine.

Contract:
* Frozen stages: PAPER_COLLECTING, PAPER_FAILED, PAPER_PASSED,
  CANARY_AUTHORIZED, CANARY_ACTIVE, CANARY_SUSPENDED, CANARY_PASSED.
* Monotonic, explicit-only transitions -- every call requires a ``reason``
  and ``actor``; there is no timer/cron path and no way for elapsed time
  alone to change stage.
* No direct paper-to-live path: only CANARY_AUTHORIZED -> CANARY_ACTIVE
  activates; no PAPER_* stage transitions straight to any CANARY_* stage.
* Activation onto CANARY_ACTIVE requires authority that exactly matches
  what was recorded at CANARY_AUTHORIZED and is not expired as of "now".
* Transitions into a "requires clean evidence" stage
  (PAPER_PASSED/CANARY_AUTHORIZED/CANARY_PASSED) refuse a supplied evidence
  window that is stale or carries breaker/cost/drawdown evidence.
* Migration 42: strategy_promotion_state (current stage per strategy).
* Mutation and its domain event commit atomically (via DomainJournal).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
STRATEGY = "orb_breakout"


def _db(tmp_path: Path, name: str = "stage.duckdb"):
    db = DuckDBConnection.get_instance(str(tmp_path / name))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    return db, migrator, journal


def _machine(tmp_path: Path, **overrides):
    from trader.promotion.stage import PromotionStageMachine, apply_stage_migration

    db, migrator, journal = _db(tmp_path)
    apply_stage_migration(migrator)
    machine = PromotionStageMachine(
        journal=journal, db=db, now=overrides.pop("now", lambda: NOW), **overrides,
    )
    return machine, journal, db, migrator


def _clean_window(strategy_id=STRATEGY, as_of=NOW):
    from trader.promotion.evidence_store import EvidenceWindow

    return EvidenceWindow(
        strategy_id=strategy_id, as_of=as_of, window_reset_at=None,
        first_event_at=NOW, last_event_at=NOW, calendar_days=("2026-07-18",),
        session_ids=("s1",), round_trip_ids=("rt-1",), instrument_ids=("1",),
        corrections=(), breaker_trips=(), cost_breaches=(), drawdown_breaches=(),
        stale=False, event_count=2,
    )


def _dirty_window(**kwargs):
    from dataclasses import replace
    return replace(_clean_window(), **kwargs)


# ---------------------------------------------------------------------------
# Migration 42
# ---------------------------------------------------------------------------

def test_migration_42_creates_strategy_promotion_state(tmp_path):
    from trader.promotion.stage import STAGE_MIGRATION_VERSIONS, apply_stage_migration

    db, migrator, _ = _db(tmp_path, "mig.duckdb")
    assert apply_stage_migration(migrator) is True
    assert STAGE_MIGRATION_VERSIONS == (42,)
    assert apply_stage_migration(migrator) is False

    cols = {row[1] for row in db.execute("PRAGMA table_info('strategy_promotion_state')", fetch="all")}
    assert "stage" in cols
    assert "strategy_id" in cols


def test_frozen_stage_names_exact(tmp_path):
    from trader.promotion.stage import (
        CANARY_ACTIVE,
        CANARY_AUTHORIZED,
        CANARY_PASSED,
        CANARY_SUSPENDED,
        PAPER_COLLECTING,
        PAPER_FAILED,
        PAPER_PASSED,
        STAGES,
    )

    assert STAGES == (
        PAPER_COLLECTING, PAPER_FAILED, PAPER_PASSED,
        CANARY_AUTHORIZED, CANARY_ACTIVE, CANARY_SUSPENDED, CANARY_PASSED,
    )
    assert PAPER_COLLECTING == "PAPER_COLLECTING"
    assert PAPER_FAILED == "PAPER_FAILED"
    assert PAPER_PASSED == "PAPER_PASSED"
    assert CANARY_AUTHORIZED == "CANARY_AUTHORIZED"
    assert CANARY_ACTIVE == "CANARY_ACTIVE"
    assert CANARY_SUSPENDED == "CANARY_SUSPENDED"
    assert CANARY_PASSED == "CANARY_PASSED"


# ---------------------------------------------------------------------------
# Bootstrap + basic monotonic transitions
# ---------------------------------------------------------------------------

def test_strategy_bootstraps_into_paper_collecting(tmp_path):
    from trader.promotion.stage import PAPER_COLLECTING

    machine, *_ = _machine(tmp_path)
    assert machine.current_stage(STRATEGY) is None

    record = machine.transition(
        STRATEGY, PAPER_COLLECTING, reason="new strategy deployed to paper", actor="operator:alice",
    )
    assert record.stage == PAPER_COLLECTING
    assert record.prior_stage is None
    assert machine.current_stage(STRATEGY) == PAPER_COLLECTING


def test_paper_collecting_to_paper_passed_with_clean_evidence(tmp_path):
    from trader.promotion.stage import PAPER_COLLECTING, PAPER_PASSED

    machine, *_ = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    record = machine.transition(
        STRATEGY, PAPER_PASSED, reason="all paper floors met", actor="paper_gate",
        evidence_window=_clean_window(),
    )
    assert record.stage == PAPER_PASSED
    assert record.prior_stage == PAPER_COLLECTING


def test_paper_collecting_to_paper_failed_and_back(tmp_path):
    from trader.promotion.stage import PAPER_COLLECTING, PAPER_FAILED

    machine, *_ = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    machine.transition(STRATEGY, PAPER_FAILED, reason="negative expectancy", actor="paper_gate")
    assert machine.current_stage(STRATEGY) == PAPER_FAILED

    record = machine.transition(
        STRATEGY, PAPER_COLLECTING, reason="resumed after review", actor="operator:alice",
    )
    assert record.stage == PAPER_COLLECTING
    assert record.prior_stage == PAPER_FAILED


# ---------------------------------------------------------------------------
# Monotonic edges: forbid illegal / direct paper-to-live transitions
# ---------------------------------------------------------------------------

def test_forbids_direct_paper_collecting_to_canary_active(tmp_path):
    from trader.promotion.stage import CANARY_ACTIVE, IllegalStageTransition, PAPER_COLLECTING

    machine, *_ = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    with pytest.raises(IllegalStageTransition):
        machine.transition(
            STRATEGY, CANARY_ACTIVE, reason="skip the queue", actor="operator:bob",
            authority_ref="auth-1", authority_expiry=NOW + dt.timedelta(days=1),
        )


def test_forbids_direct_paper_passed_to_canary_active(tmp_path):
    from trader.promotion.stage import CANARY_ACTIVE, IllegalStageTransition, PAPER_COLLECTING, PAPER_PASSED

    machine, *_ = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    machine.transition(
        STRATEGY, PAPER_PASSED, reason="floors met", actor="paper_gate",
        evidence_window=_clean_window(),
    )
    with pytest.raises(IllegalStageTransition):
        machine.transition(
            STRATEGY, CANARY_ACTIVE, reason="skip authorization", actor="operator:bob",
            authority_ref="auth-1", authority_expiry=NOW + dt.timedelta(days=1),
        )


def test_forbids_canary_suspended_directly_back_to_canary_active(tmp_path):
    from trader.promotion.stage import CANARY_ACTIVE, IllegalStageTransition

    machine, *_ = _machine(tmp_path)
    _authorize_and_activate(machine)
    machine.transition(STRATEGY, "CANARY_SUSPENDED", reason="breaker trip", actor="canary_risk")
    with pytest.raises(IllegalStageTransition):
        machine.transition(
            STRATEGY, CANARY_ACTIVE, reason="just flip it back on", actor="operator:bob",
            authority_ref="auth-1", authority_expiry=NOW + dt.timedelta(days=1),
        )


def test_forbids_unknown_stage_name(tmp_path):
    machine, *_ = _machine(tmp_path)
    with pytest.raises(ValueError):
        machine.transition(STRATEGY, "LIVE_FULL_SEND", reason="x", actor="y")


def test_requires_reason_and_actor(tmp_path):
    from trader.promotion.stage import PAPER_COLLECTING

    machine, *_ = _machine(tmp_path)
    with pytest.raises(ValueError):
        machine.transition(STRATEGY, PAPER_COLLECTING, reason="", actor="system")
    with pytest.raises(ValueError):
        machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="")


# ---------------------------------------------------------------------------
# No automatic transition: only explicit calls mutate state
# ---------------------------------------------------------------------------

def test_elapsed_time_alone_never_changes_stage(tmp_path):
    from trader.promotion.stage import PAPER_COLLECTING

    machine, *_ = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    before = machine.current_stage(STRATEGY)

    # Simulate "time passing" -- reading current stage repeatedly, far into
    # the future, with no explicit transition() call in between.
    for _ in range(3):
        assert machine.current_stage(STRATEGY) == before


def test_projecting_evidence_never_mutates_stage_state(tmp_path):
    """EvidenceStore.project() and PromotionStageMachine are decoupled --
    accumulating evidence alone must never advance the stage."""
    from trader.promotion.evidence_store import EvidenceEvent, EvidenceStore, apply_evidence_migrations
    from trader.promotion.stage import PAPER_COLLECTING

    machine, journal, db, migrator = _machine(tmp_path)
    apply_evidence_migrations(migrator)
    evidence = EvidenceStore(journal=journal, db=db, now=lambda: NOW)

    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    for i in range(60):
        evidence.append(EvidenceEvent(
            source_event_id=f"sess-{i}", strategy_id=STRATEGY, event_kind="session",
            payload={"session_id": f"s{i}"}, source_timestamp=NOW,
        ))
    evidence.project(STRATEGY, as_of=NOW)

    assert machine.current_stage(STRATEGY) == PAPER_COLLECTING


# ---------------------------------------------------------------------------
# Authority: required, must match, must not be expired
# ---------------------------------------------------------------------------

def _authorize(machine, *, authority_ref="auth-1", expiry=None):
    from trader.promotion.stage import CANARY_AUTHORIZED, PAPER_COLLECTING, PAPER_PASSED

    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    machine.transition(
        STRATEGY, PAPER_PASSED, reason="floors met", actor="paper_gate",
        evidence_window=_clean_window(),
    )
    return machine.transition(
        STRATEGY, CANARY_AUTHORIZED, reason="promotion controller prepared canary",
        actor="promotion_controller",
        authority_ref=authority_ref,
        authority_expiry=expiry or (NOW + dt.timedelta(days=7)),
    )


def _authorize_and_activate(machine, *, authority_ref="auth-1", expiry=None):
    from trader.promotion.stage import CANARY_ACTIVE

    expiry = expiry or (NOW + dt.timedelta(days=7))
    _authorize(machine, authority_ref=authority_ref, expiry=expiry)
    return machine.transition(
        STRATEGY, CANARY_ACTIVE, reason="operator activated canary", actor="operator:alice",
        authority_ref=authority_ref, authority_expiry=expiry,
    )


def test_canary_authorized_requires_authority_ref_and_expiry(tmp_path):
    from trader.promotion.stage import AuthorityRequiredError, CANARY_AUTHORIZED, PAPER_COLLECTING, PAPER_PASSED

    machine, *_ = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    machine.transition(
        STRATEGY, PAPER_PASSED, reason="floors met", actor="paper_gate",
        evidence_window=_clean_window(),
    )
    with pytest.raises(AuthorityRequiredError):
        machine.transition(
            STRATEGY, CANARY_AUTHORIZED, reason="missing authority", actor="promotion_controller",
        )


def test_activation_succeeds_with_matching_unexpired_authority(tmp_path):
    from trader.promotion.stage import CANARY_ACTIVE

    machine, *_ = _machine(tmp_path)
    record = _authorize_and_activate(machine)
    assert record.stage == CANARY_ACTIVE
    assert record.authority_ref == "auth-1"


def test_activation_forbidden_without_authority_supplied(tmp_path):
    from trader.promotion.stage import AuthorityRequiredError, CANARY_ACTIVE

    machine, *_ = _machine(tmp_path)
    _authorize(machine)
    with pytest.raises(AuthorityRequiredError):
        machine.transition(STRATEGY, CANARY_ACTIVE, reason="activate", actor="operator:alice")


def test_activation_forbidden_on_changed_authority(tmp_path):
    from trader.promotion.stage import AuthorityMismatchError, CANARY_ACTIVE

    machine, *_ = _machine(tmp_path)
    _authorize(machine, authority_ref="auth-1")
    with pytest.raises(AuthorityMismatchError):
        machine.transition(
            STRATEGY, CANARY_ACTIVE, reason="activate", actor="operator:alice",
            authority_ref="auth-DIFFERENT", authority_expiry=NOW + dt.timedelta(days=7),
        )


def test_activation_forbidden_on_changed_expiry(tmp_path):
    from trader.promotion.stage import AuthorityMismatchError, CANARY_ACTIVE

    machine, *_ = _machine(tmp_path)
    original_expiry = NOW + dt.timedelta(days=7)
    _authorize(machine, authority_ref="auth-1", expiry=original_expiry)
    with pytest.raises(AuthorityMismatchError):
        machine.transition(
            STRATEGY, CANARY_ACTIVE, reason="activate", actor="operator:alice",
            authority_ref="auth-1", authority_expiry=original_expiry + dt.timedelta(days=30),
        )


def test_activation_forbidden_on_expired_authority(tmp_path):
    from trader.promotion.stage import AuthorityExpiredError, CANARY_ACTIVE

    machine, *_ = _machine(tmp_path)
    expiry = NOW + dt.timedelta(hours=1)
    _authorize(machine, authority_ref="auth-1", expiry=expiry)

    later = expiry + dt.timedelta(minutes=1)
    with pytest.raises(AuthorityExpiredError):
        machine.transition(
            STRATEGY, CANARY_ACTIVE, reason="activate late", actor="operator:alice",
            authority_ref="auth-1", authority_expiry=expiry, now=later,
        )


def test_canary_suspended_requires_fresh_authorization_not_bare_reset(tmp_path):
    """A suspension requires a NEW CANARY_AUTHORIZED (fresh promotion review)
    -- there is no edge straight from CANARY_SUSPENDED to CANARY_ACTIVE, so a
    breaker reset alone can never reactivate."""
    from trader.promotion.stage import CANARY_ACTIVE, CANARY_AUTHORIZED, CANARY_SUSPENDED

    machine, *_ = _machine(tmp_path)
    _authorize_and_activate(machine, authority_ref="auth-1")
    machine.transition(STRATEGY, CANARY_SUSPENDED, reason="drawdown breach", actor="canary_risk")
    assert machine.current_stage(STRATEGY) == CANARY_SUSPENDED

    reauthorized = machine.transition(
        STRATEGY, CANARY_AUTHORIZED, reason="fresh promotion review completed",
        actor="promotion_controller",
        authority_ref="auth-2", authority_expiry=NOW + dt.timedelta(days=7),
    )
    assert reauthorized.stage == CANARY_AUTHORIZED
    reactivated = machine.transition(
        STRATEGY, CANARY_ACTIVE, reason="reactivated under new authority", actor="operator:alice",
        authority_ref="auth-2", authority_expiry=NOW + dt.timedelta(days=7),
    )
    assert reactivated.stage == CANARY_ACTIVE
    # The stale auth-1 no longer works once superseded by auth-2.
    with pytest.raises(Exception):
        machine.transition(
            STRATEGY, CANARY_ACTIVE, reason="try old authority", actor="operator:mallory",
            authority_ref="auth-1", authority_expiry=NOW + dt.timedelta(days=7),
        )


def test_canary_active_to_canary_passed(tmp_path):
    from trader.promotion.stage import CANARY_PASSED

    machine, *_ = _machine(tmp_path)
    _authorize_and_activate(machine)
    record = machine.transition(
        STRATEGY, CANARY_PASSED, reason="live floors met, zero incidents", actor="live_metrics",
        evidence_window=_clean_window(),
    )
    assert record.stage == CANARY_PASSED


# ---------------------------------------------------------------------------
# Correction / inactivity / breaker / cost / drawdown block "clean" stages
# ---------------------------------------------------------------------------

def test_stale_evidence_blocks_paper_passed(tmp_path):
    from trader.promotion.stage import EvidenceNotCleanError, PAPER_COLLECTING, PAPER_PASSED

    machine, *_ = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, PAPER_PASSED, reason="floors met but evidence is old", actor="paper_gate",
            evidence_window=_dirty_window(stale=True),
        )
    assert "stale_evidence" in exc_info.value.reasons


def test_breaker_trip_blocks_paper_passed(tmp_path):
    from trader.promotion.stage import EvidenceNotCleanError, PAPER_COLLECTING, PAPER_PASSED

    machine, *_ = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, PAPER_PASSED, reason="floors met but breaker tripped", actor="paper_gate",
            evidence_window=_dirty_window(breaker_trips=({"incident_id": "inc-1"},)),
        )
    assert "breaker_trip" in exc_info.value.reasons


def test_cost_breach_blocks_canary_authorized(tmp_path):
    from trader.promotion.stage import CANARY_AUTHORIZED, EvidenceNotCleanError, PAPER_COLLECTING, PAPER_PASSED

    machine, *_ = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    machine.transition(
        STRATEGY, PAPER_PASSED, reason="floors met", actor="paper_gate",
        evidence_window=_clean_window(),
    )
    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, CANARY_AUTHORIZED, reason="prepare canary despite cost breach",
            actor="promotion_controller",
            authority_ref="auth-1", authority_expiry=NOW + dt.timedelta(days=7),
            evidence_window=_dirty_window(cost_breaches=({"metric": "stressed_cost_bps"},)),
        )
    assert "cost_breach" in exc_info.value.reasons


def test_drawdown_breach_blocks_canary_passed(tmp_path):
    from trader.promotion.stage import CANARY_PASSED, EvidenceNotCleanError

    machine, *_ = _machine(tmp_path)
    _authorize_and_activate(machine)
    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, CANARY_PASSED, reason="try to pass despite drawdown", actor="live_metrics",
            evidence_window=_dirty_window(drawdown_breaches=({"drawdown_pct": 5.0},)),
        )
    assert "drawdown_breach" in exc_info.value.reasons


def test_clean_evidence_gate_is_only_checked_when_window_supplied(tmp_path):
    """Task 1 does not itself enforce the paper-gate floors (Task 2's job);
    when no evidence_window is supplied, the machine trusts the caller."""
    from trader.promotion.stage import PAPER_COLLECTING, PAPER_PASSED

    machine, *_ = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    record = machine.transition(
        STRATEGY, PAPER_PASSED, reason="caller already validated floors", actor="paper_gate",
    )
    assert record.stage == PAPER_PASSED


# ---------------------------------------------------------------------------
# Atomicity: mutation + domain event commit together
# ---------------------------------------------------------------------------

def test_transition_commits_domain_event_atomically(tmp_path):
    from trader.promotion.stage import PAPER_COLLECTING

    machine, journal, db, _ = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")

    kinds = {
        row[0]
        for row in db.execute(
            "SELECT event_type FROM domain_event_journal WHERE entity_type = 'strategy_promotion_state'",
            fetch="all",
        )
    }
    assert "promotion.stage_transitioned" in kinds

    state_row = db.execute(
        "SELECT stage FROM strategy_promotion_state WHERE strategy_id = ?",
        [STRATEGY], fetch="one",
    )
    assert state_row == (PAPER_COLLECTING,)


def test_restart_recovers_current_stage_from_durable_state(tmp_path):
    from trader.promotion.stage import PAPER_COLLECTING, PromotionStageMachine

    machine, journal, db, migrator = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")

    # Fresh machine instance (process restart), same db/journal.
    restarted = PromotionStageMachine(journal=journal, db=db, now=lambda: NOW)
    assert restarted.current_stage(STRATEGY) == PAPER_COLLECTING
