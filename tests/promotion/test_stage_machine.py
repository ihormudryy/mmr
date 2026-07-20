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
  (PAPER_PASSED/CANARY_AUTHORIZED/CANARY_PASSED) MANDATE a caller-supplied
  ``evidence_store`` -- the machine recomputes
  ``evidence_store.project(strategy_id, as_of=now)`` itself and evaluates
  THAT for cleanliness. A caller-constructed ``EvidenceWindow`` (however
  clean it claims to be) is NEVER accepted as sufficient proof by itself --
  omitting the store, or supplying an ``evidence_window`` that disagrees
  with the store's own projection, all fail closed. Software completion
  alone can never mark a paper or live gate passed.
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
    from trader.promotion.evidence_store import EvidenceStore, apply_evidence_migrations
    from trader.promotion.stage import PromotionStageMachine, apply_stage_migration

    db, migrator, journal = _db(tmp_path)
    apply_stage_migration(migrator)
    apply_evidence_migrations(migrator)
    now_fn = overrides.pop("now", lambda: NOW)
    machine = PromotionStageMachine(journal=journal, db=db, now=now_fn, **overrides)
    evidence = EvidenceStore(journal=journal, db=db, now=now_fn)
    return machine, evidence, journal, db, migrator


# 20 session dates spanning exactly 30 ELAPSED calendar days (gaps for
# weekends/holidays) -- proves the calendar-days floor is span-based, not a
# count of distinct session dates (P4 Task 2 review fix).
_PAPER_GATE_SESSION_OFFSETS = (0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 18, 19, 20, 21, 29)


def _seed_clean_evidence(evidence, strategy_id=STRATEGY, ts=NOW, suffix=""):
    """Append enough session + round-trip evidence for a fresh projection to
    satisfy PaperGate's simultaneous floors (30 elapsed calendar days, 20
    sessions, 50 round trips, 5 instruments), be economically/
    diversification-evidenced (every round trip carries pnl_after_cost +
    instrument_id), and be genuinely clean (no safety incidents, not stale
    as of ``ts``) -- since ``PromotionStageMachine`` now requires
    ``PaperGate.evaluate(...).passed`` for PAPER_PASSED, "clean" alone
    (Task 1's stale/breaker/cost/drawdown-only definition) is no longer
    sufficient evidence for the many tests below that expect PAPER_PASSED
    to succeed.
    """
    from trader.promotion.evidence_store import EvidenceEvent

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


def _seed_single_session_only(evidence, strategy_id=STRATEGY, ts=NOW, suffix=""):
    """The OLD ``_seed_clean_evidence`` behavior: exactly one session event.
    ``is_clean`` (not stale, no breaker/cost/drawdown) but far short of
    every PaperGate floor -- used to prove Task 1's generic cleanliness
    alone is no longer sufficient for PAPER_PASSED."""
    from trader.promotion.evidence_store import EvidenceEvent

    evidence.append(EvidenceEvent(
        source_event_id=f"{strategy_id}-sess-{suffix or ts.isoformat()}",
        strategy_id=strategy_id, event_kind="session",
        payload={"session_id": f"s{suffix or '1'}"}, source_timestamp=ts,
    ))


def _clean_window(strategy_id=STRATEGY, as_of=NOW):
    """A free-standing, forgeable ``EvidenceWindow`` -- NOT backed by any
    store. Used only to prove such an object can never authorize a gate on
    its own."""
    from trader.promotion.evidence_store import EvidenceWindow

    return EvidenceWindow(
        strategy_id=strategy_id, as_of=as_of, window_reset_at=None,
        first_event_at=NOW, last_event_at=NOW, calendar_days=("2026-07-18",),
        session_ids=("s1",), round_trip_ids=("rt-1",), instrument_ids=("1",),
        corrections=(), breaker_trips=(), cost_breaches=(), drawdown_breaches=(),
        stale=False, event_count=2,
    )


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

    machine, evidence, *_ = _machine(tmp_path)
    _seed_clean_evidence(evidence)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    record = machine.transition(
        STRATEGY, PAPER_PASSED, reason="all paper floors met", actor="paper_gate",
        evidence_store=evidence,
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

    machine, evidence, *_ = _machine(tmp_path)
    _seed_clean_evidence(evidence)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    machine.transition(
        STRATEGY, PAPER_PASSED, reason="floors met", actor="paper_gate",
        evidence_store=evidence,
    )
    with pytest.raises(IllegalStageTransition):
        machine.transition(
            STRATEGY, CANARY_ACTIVE, reason="skip authorization", actor="operator:bob",
            authority_ref="auth-1", authority_expiry=NOW + dt.timedelta(days=1),
        )


def test_forbids_canary_suspended_directly_back_to_canary_active(tmp_path):
    from trader.promotion.stage import CANARY_ACTIVE, IllegalStageTransition

    machine, evidence, *_ = _machine(tmp_path)
    _authorize_and_activate(machine, evidence)
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
    from trader.promotion.evidence_store import EvidenceEvent
    from trader.promotion.stage import PAPER_COLLECTING

    machine, evidence, *_ = _machine(tmp_path)

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

def _authorize(machine, evidence, *, authority_ref="auth-1", expiry=None):
    from trader.promotion.stage import CANARY_AUTHORIZED, PAPER_COLLECTING, PAPER_PASSED

    _seed_clean_evidence(evidence)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    machine.transition(
        STRATEGY, PAPER_PASSED, reason="floors met", actor="paper_gate",
        evidence_store=evidence,
    )
    return machine.transition(
        STRATEGY, CANARY_AUTHORIZED, reason="promotion controller prepared canary",
        actor="promotion_controller",
        authority_ref=authority_ref,
        authority_expiry=expiry or (NOW + dt.timedelta(days=7)),
        evidence_store=evidence,
    )


def _authorize_and_activate(machine, evidence, *, authority_ref="auth-1", expiry=None):
    from trader.promotion.stage import CANARY_ACTIVE

    expiry = expiry or (NOW + dt.timedelta(days=7))
    _authorize(machine, evidence, authority_ref=authority_ref, expiry=expiry)
    return machine.transition(
        STRATEGY, CANARY_ACTIVE, reason="operator activated canary", actor="operator:alice",
        authority_ref=authority_ref, authority_expiry=expiry,
    )


def test_canary_authorized_requires_authority_ref_and_expiry(tmp_path):
    from trader.promotion.stage import AuthorityRequiredError, CANARY_AUTHORIZED, PAPER_COLLECTING, PAPER_PASSED

    machine, evidence, *_ = _machine(tmp_path)
    _seed_clean_evidence(evidence)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    machine.transition(
        STRATEGY, PAPER_PASSED, reason="floors met", actor="paper_gate",
        evidence_store=evidence,
    )
    with pytest.raises(AuthorityRequiredError):
        machine.transition(
            STRATEGY, CANARY_AUTHORIZED, reason="missing authority", actor="promotion_controller",
            evidence_store=evidence,
        )


def test_activation_succeeds_with_matching_unexpired_authority(tmp_path):
    from trader.promotion.stage import CANARY_ACTIVE

    machine, evidence, *_ = _machine(tmp_path)
    record = _authorize_and_activate(machine, evidence)
    assert record.stage == CANARY_ACTIVE
    assert record.authority_ref == "auth-1"


def test_activation_forbidden_without_authority_supplied(tmp_path):
    from trader.promotion.stage import AuthorityRequiredError, CANARY_ACTIVE

    machine, evidence, *_ = _machine(tmp_path)
    _authorize(machine, evidence)
    with pytest.raises(AuthorityRequiredError):
        machine.transition(STRATEGY, CANARY_ACTIVE, reason="activate", actor="operator:alice")


def test_activation_forbidden_on_changed_authority(tmp_path):
    from trader.promotion.stage import AuthorityMismatchError, CANARY_ACTIVE

    machine, evidence, *_ = _machine(tmp_path)
    _authorize(machine, evidence, authority_ref="auth-1")
    with pytest.raises(AuthorityMismatchError):
        machine.transition(
            STRATEGY, CANARY_ACTIVE, reason="activate", actor="operator:alice",
            authority_ref="auth-DIFFERENT", authority_expiry=NOW + dt.timedelta(days=7),
        )


def test_activation_forbidden_on_changed_expiry(tmp_path):
    from trader.promotion.stage import AuthorityMismatchError, CANARY_ACTIVE

    machine, evidence, *_ = _machine(tmp_path)
    original_expiry = NOW + dt.timedelta(days=7)
    _authorize(machine, evidence, authority_ref="auth-1", expiry=original_expiry)
    with pytest.raises(AuthorityMismatchError):
        machine.transition(
            STRATEGY, CANARY_ACTIVE, reason="activate", actor="operator:alice",
            authority_ref="auth-1", authority_expiry=original_expiry + dt.timedelta(days=30),
        )


def test_activation_forbidden_on_expired_authority(tmp_path):
    from trader.promotion.stage import AuthorityExpiredError, CANARY_ACTIVE

    machine, evidence, *_ = _machine(tmp_path)
    expiry = NOW + dt.timedelta(hours=1)
    _authorize(machine, evidence, authority_ref="auth-1", expiry=expiry)

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

    machine, evidence, *_ = _machine(tmp_path)
    _authorize_and_activate(machine, evidence, authority_ref="auth-1")
    machine.transition(STRATEGY, CANARY_SUSPENDED, reason="drawdown breach", actor="canary_risk")
    assert machine.current_stage(STRATEGY) == CANARY_SUSPENDED

    reauthorized = machine.transition(
        STRATEGY, CANARY_AUTHORIZED, reason="fresh promotion review completed",
        actor="promotion_controller",
        authority_ref="auth-2", authority_expiry=NOW + dt.timedelta(days=7),
        evidence_store=evidence,
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

    machine, evidence, *_ = _machine(tmp_path)
    _authorize_and_activate(machine, evidence)
    record = machine.transition(
        STRATEGY, CANARY_PASSED, reason="live floors met, zero incidents", actor="live_metrics",
        evidence_store=evidence,
    )
    assert record.stage == CANARY_PASSED


# ---------------------------------------------------------------------------
# Correction / inactivity / breaker / cost / drawdown block "clean" stages
# ---------------------------------------------------------------------------

def test_stale_evidence_blocks_paper_passed(tmp_path):
    from trader.promotion.stage import EvidenceNotCleanError, PAPER_COLLECTING, PAPER_PASSED

    machine, evidence, *_ = _machine(tmp_path)
    # Only ancient evidence (>30 days before `now`) -- the fresh projection
    # will be stale even though it's computed live from the real store.
    _seed_clean_evidence(evidence, ts=NOW - dt.timedelta(days=40))
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, PAPER_PASSED, reason="floors met but evidence is old", actor="paper_gate",
            evidence_store=evidence,
        )
    assert "stale_evidence" in exc_info.value.reasons


def test_breaker_trip_blocks_paper_passed(tmp_path):
    from trader.promotion.evidence_store import EvidenceEvent
    from trader.promotion.stage import EvidenceNotCleanError, PAPER_COLLECTING, PAPER_PASSED

    machine, evidence, *_ = _machine(tmp_path)
    _seed_clean_evidence(evidence)
    evidence.append(EvidenceEvent(
        source_event_id="breaker-1", strategy_id=STRATEGY, event_kind="breaker_trip",
        payload={"incident_id": "inc-1"}, source_timestamp=NOW,
    ))
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, PAPER_PASSED, reason="floors met but breaker tripped", actor="paper_gate",
            evidence_store=evidence,
        )
    assert "breaker_trip" in exc_info.value.reasons


def test_cost_breach_blocks_canary_authorized(tmp_path):
    from trader.promotion.evidence_store import EvidenceEvent
    from trader.promotion.stage import CANARY_AUTHORIZED, EvidenceNotCleanError, PAPER_COLLECTING, PAPER_PASSED

    machine, evidence, *_ = _machine(tmp_path)
    _seed_clean_evidence(evidence)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    machine.transition(
        STRATEGY, PAPER_PASSED, reason="floors met", actor="paper_gate",
        evidence_store=evidence,
    )
    evidence.append(EvidenceEvent(
        source_event_id="cost-1", strategy_id=STRATEGY, event_kind="cost_breach",
        payload={"metric": "stressed_cost_bps"}, source_timestamp=NOW,
    ))
    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, CANARY_AUTHORIZED, reason="prepare canary despite cost breach",
            actor="promotion_controller",
            authority_ref="auth-1", authority_expiry=NOW + dt.timedelta(days=7),
            evidence_store=evidence,
        )
    assert "cost_breach" in exc_info.value.reasons


def test_drawdown_breach_blocks_canary_passed(tmp_path):
    from trader.promotion.evidence_store import EvidenceEvent
    from trader.promotion.stage import CANARY_PASSED, EvidenceNotCleanError

    machine, evidence, *_ = _machine(tmp_path)
    _authorize_and_activate(machine, evidence)
    evidence.append(EvidenceEvent(
        source_event_id="dd-1", strategy_id=STRATEGY, event_kind="drawdown_breach",
        payload={"drawdown_pct": 5.0}, source_timestamp=NOW,
    ))
    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, CANARY_PASSED, reason="try to pass despite drawdown", actor="live_metrics",
            evidence_store=evidence,
        )
    assert "drawdown_breach" in exc_info.value.reasons


# ---------------------------------------------------------------------------
# CRITICAL: a caller-constructed EvidenceWindow is never sufficient proof by
# itself -- an EvidenceStore is mandatory and the machine projects from it.
# ---------------------------------------------------------------------------

def test_evidence_store_is_mandatory_bare_call_blocks_paper_passed(tmp_path):
    """CRITICAL fail-closed contract: software completion alone (a bare
    reason/actor with no evidence at all) can never mark PAPER_PASSED.
    Omission is treated as a rejection, never trusted."""
    from trader.promotion.stage import EvidenceNotCleanError, PAPER_COLLECTING, PAPER_PASSED

    machine, *_ = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, PAPER_PASSED, reason="caller says floors met, trust me", actor="paper_gate",
        )
    assert "missing_evidence_store" in exc_info.value.reasons
    assert machine.current_stage(STRATEGY) == PAPER_COLLECTING


def test_forged_evidence_window_without_a_store_is_rejected(tmp_path):
    """CRITICAL: the exact bypass this fix closes. Constructing a
    free-standing, perfectly clean ``EvidenceWindow`` and handing it to
    ``transition()`` -- with NO backing ``evidence_store`` -- must NOT be
    enough to mark PAPER_PASSED. A forged clean window is worthless without
    a store to verify it against."""
    from trader.promotion.stage import EvidenceNotCleanError, PAPER_COLLECTING, PAPER_PASSED

    machine, *_ = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    forged = _clean_window()
    assert forged.is_clean  # the forged window claims to be spotless

    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, PAPER_PASSED, reason="forged clean window, no store", actor="attacker",
            evidence_window=forged,
        )
    assert "missing_evidence_store" in exc_info.value.reasons
    assert machine.current_stage(STRATEGY) == PAPER_COLLECTING


def test_forged_evidence_window_without_matching_store_projection_is_rejected(tmp_path):
    """CRITICAL: even when a real ``evidence_store`` IS supplied, a
    caller-supplied ``evidence_window`` that does NOT match what the store
    actually projects (e.g. it claims clean evidence the store has never
    recorded) must be rejected -- the store's own projection is what is
    evaluated, and a forged window is treated as untrustworthy on sight."""
    from trader.promotion.stage import EvidenceNotCleanError, PAPER_COLLECTING, PAPER_PASSED

    machine, evidence, *_ = _machine(tmp_path)
    # The real store has NO evidence at all for this strategy.
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    forged = _clean_window()

    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, PAPER_PASSED, reason="forged window, real store disagrees", actor="attacker",
            evidence_store=evidence, evidence_window=forged,
        )
    assert "evidence_window_mismatch" in exc_info.value.reasons
    # And the real (empty) projection is independently unclean too.
    assert "stale_evidence" in exc_info.value.reasons
    assert machine.current_stage(STRATEGY) == PAPER_COLLECTING


def test_evidence_window_for_wrong_strategy_is_rejected_as_mismatch(tmp_path):
    """A window that's internally clean but bound to a DIFFERENT strategy_id
    must never be honored -- even with a real store present, it disagrees
    with the store's own (this-strategy) projection and is rejected."""
    from trader.promotion.stage import EvidenceNotCleanError, PAPER_COLLECTING, PAPER_PASSED

    machine, evidence, *_ = _machine(tmp_path)
    _seed_clean_evidence(evidence)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    wrong_strategy_window = _clean_window(strategy_id="some_other_strategy")

    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, PAPER_PASSED, reason="floors met (wrong strategy's evidence)", actor="paper_gate",
            evidence_store=evidence, evidence_window=wrong_strategy_window,
        )
    assert "evidence_window_mismatch" in exc_info.value.reasons


def test_freshly_projected_clean_window_at_transition_time_passes(tmp_path):
    """The positive case: a real, store-backed, clean projection is
    accepted -- with or without also passing the matching window object."""
    from trader.promotion.stage import PAPER_COLLECTING, PAPER_PASSED

    machine, evidence, *_ = _machine(tmp_path)
    _seed_clean_evidence(evidence)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    record = machine.transition(
        STRATEGY, PAPER_PASSED, reason="floors met", actor="paper_gate",
        evidence_store=evidence,
    )
    assert record.stage == PAPER_PASSED


# ---------------------------------------------------------------------------
# CRITICAL: PAPER_PASSED requires PaperGate.evaluate(...).passed, not just
# Task 1's generic is_clean -- a store-backed window that fails floors or
# blockers must never become PAPER_PASSED.
# ---------------------------------------------------------------------------

def test_paper_gate_floors_not_met_blocks_paper_passed_even_though_is_clean(tmp_path):
    """A window can be ``is_clean`` (not stale, no breaker/cost/drawdown)
    while still nowhere near PaperGate's floors (1 session, 0 round trips,
    0 instruments). Task 1's generic cleanliness alone must NOT be enough
    to reach PAPER_PASSED anymore -- PaperGate must also pass."""
    from trader.promotion.stage import EvidenceNotCleanError, PAPER_COLLECTING, PAPER_PASSED

    machine, evidence, *_ = _machine(tmp_path)
    _seed_single_session_only(evidence)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")

    real_window = evidence.project(STRATEGY, as_of=NOW)
    assert real_window.is_clean is True  # Task 1 alone would have allowed this

    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, PAPER_PASSED, reason="claims floors met but they aren't",
            actor="paper_gate", evidence_store=evidence,
        )
    assert any(reason.startswith("floor_") for reason in exc_info.value.reasons)
    assert machine.current_stage(STRATEGY) == PAPER_COLLECTING


def test_paper_gate_blocker_blocks_paper_passed_even_with_floors_met(tmp_path):
    """Floors fully met (via ``_seed_clean_evidence``) but a Task 2 blocker
    (a missed flat) is present on the same store-backed projection --
    PAPER_PASSED must still be refused."""
    from trader.promotion.evidence_store import EvidenceEvent
    from trader.promotion.stage import EvidenceNotCleanError, PAPER_COLLECTING, PAPER_PASSED

    machine, evidence, *_ = _machine(tmp_path)
    _seed_clean_evidence(evidence)
    evidence.append(EvidenceEvent(
        source_event_id="missed-flat-1", strategy_id=STRATEGY, event_kind="missed_flat",
        payload={"session_id": "s100"}, source_timestamp=NOW,
    ))
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")

    real_window = evidence.project(STRATEGY, as_of=NOW)
    from trader.promotion.paper_gate import PaperGate
    decision = PaperGate().evaluate(real_window)
    assert decision.floors_met is True  # floors alone are satisfied

    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, PAPER_PASSED, reason="floors met but missed a flat", actor="paper_gate",
            evidence_store=evidence,
        )
    assert "missed_flat" in exc_info.value.reasons
    assert machine.current_stage(STRATEGY) == PAPER_COLLECTING


def test_paper_gate_missing_economic_evidence_blocks_paper_passed(tmp_path):
    """50 round trips (floor met) but none carry a parseable
    ``pnl_after_cost`` -- must never let PAPER_PASSED through."""
    from trader.promotion.evidence_store import EvidenceEvent
    from trader.promotion.stage import EvidenceNotCleanError, PAPER_COLLECTING, PAPER_PASSED

    machine, evidence, *_ = _machine(tmp_path)
    tag = "econ"
    start = NOW - dt.timedelta(days=29)
    for i, offset in enumerate(_PAPER_GATE_SESSION_OFFSETS):
        evidence.append(EvidenceEvent(
            source_event_id=f"{STRATEGY}-sess-{tag}-{i}", strategy_id=STRATEGY,
            event_kind="session", payload={"session_id": f"s{tag}{i:02d}"},
            source_timestamp=start + dt.timedelta(days=offset),
        ))
    for i in range(50):
        evidence.append(EvidenceEvent(
            source_event_id=f"{STRATEGY}-rt-{tag}-{i}", strategy_id=STRATEGY,
            event_kind="round_trip",
            # instrument_id present (diversification floor met) but no
            # pnl_after_cost at all -- economic evidence missing entirely.
            payload={"round_trip_id": f"rt-{tag}-{i}", "instrument_id": str(1000 + (i % 5))},
            source_timestamp=NOW,
        ))
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")

    with pytest.raises(EvidenceNotCleanError) as exc_info:
        machine.transition(
            STRATEGY, PAPER_PASSED, reason="50 round trips, no economic evidence",
            actor="paper_gate", evidence_store=evidence,
        )
    assert "missing_economic_evidence" in exc_info.value.reasons
    assert machine.current_stage(STRATEGY) == PAPER_COLLECTING


def test_matching_evidence_window_alongside_store_passes(tmp_path):
    """Supplying an ``evidence_window=`` that DOES match the store's own
    projection (e.g. the caller already called ``project()`` and wants to
    pass the result through for its own bookkeeping) is accepted -- the
    optional integrity check is satisfied, not merely bypassed."""
    from trader.promotion.stage import PAPER_COLLECTING, PAPER_PASSED

    machine, evidence, *_ = _machine(tmp_path)
    _seed_clean_evidence(evidence)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")
    real_window = evidence.project(STRATEGY, as_of=NOW)
    record = machine.transition(
        STRATEGY, PAPER_PASSED, reason="floors met", actor="paper_gate",
        evidence_store=evidence, evidence_window=real_window,
    )
    assert record.stage == PAPER_PASSED


# ---------------------------------------------------------------------------
# Atomicity: mutation + domain event commit together
# ---------------------------------------------------------------------------

def test_transition_commits_domain_event_atomically(tmp_path):
    from trader.promotion.stage import PAPER_COLLECTING

    machine, evidence, journal, db, _ = _machine(tmp_path)
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

    machine, evidence, journal, db, migrator = _machine(tmp_path)
    machine.transition(STRATEGY, PAPER_COLLECTING, reason="bootstrap", actor="system")

    # Fresh machine instance (process restart), same db/journal.
    restarted = PromotionStageMachine(journal=journal, db=db, now=lambda: NOW)
    assert restarted.current_stage(STRATEGY) == PAPER_COLLECTING
