"""P4 Task 2 -- the accelerated paper evidence gate + paper/shadow operation.

Contract:
* ``PaperGate.evaluate(EvidenceWindow) -> PromotionDecision`` is a pure
  function of the window: exact simultaneous floors are 30 calendar days,
  20 completed sessions, 50 round trips, and five instruments -- ALL must be
  met simultaneously for ``passed`` to be true.
* Each of divergence, duplicate, unresolved ambiguity/alert, missed flat,
  replay mismatch, stressed-cost breach, negative expectancy, and
  concentration independently blocks a pass, regardless of whether the
  floors are otherwise met.
* A correction's impact removes the affected session/trade from the counted
  window (already true of ``EvidenceWindow.window_reset_at``); a *safety*
  correction (scope in {"risk", "data"}) additionally resets the clean
  session streak to zero. ``correction_impact`` records the old/new
  projections for audit.
* Shadow mode computes intents/decisions but never registers
  ``execute_automated_intent`` -- even if the underlying compute pipeline
  tries to, the shadow harness raises. Shadow and paper traces over the
  same sealed input are compared and any mismatch is reported structurally.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from trader.promotion.evidence_store import EvidenceWindow

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
STRATEGY = "orb_breakout"


def _dates(n: int, start: dt.date = dt.date(2026, 5, 1)) -> list[str]:
    return [(start + dt.timedelta(days=i)).isoformat() for i in range(n)]


def _instruments(n: int) -> tuple[str, ...]:
    return tuple(str(1000 + i) for i in range(n))


def _round_trip_records(n: int, *, instruments: tuple[str, ...], pnl: float = 10.0) -> tuple[dict, ...]:
    records = []
    for i in range(n):
        instrument = instruments[i % len(instruments)]
        records.append({
            "round_trip_id": f"rt-{i}",
            "instrument_id": instrument,
            "pnl_after_cost": pnl,
        })
    return tuple(records)


def _window(
    *,
    calendar_days: int = 30,
    sessions: int = 20,
    round_trips: int = 50,
    instruments: int = 5,
    as_of: dt.datetime = NOW,
    window_reset_at: dt.datetime | None = None,
    stale: bool = False,
    breaker_trips: tuple = (),
    cost_breaches: tuple = (),
    drawdown_breaches: tuple = (),
    divergences: tuple = (),
    duplicates: tuple = (),
    unresolved_alerts: tuple = (),
    missed_flats: tuple = (),
    replay_mismatches: tuple = (),
    round_trip_records: tuple | None = None,
    corrections: tuple = (),
) -> EvidenceWindow:
    instrument_ids = _instruments(instruments)
    if round_trip_records is None:
        round_trip_records = _round_trip_records(round_trips, instruments=instrument_ids)
    return EvidenceWindow(
        strategy_id=STRATEGY,
        as_of=as_of,
        window_reset_at=window_reset_at,
        first_event_at=NOW - dt.timedelta(days=calendar_days),
        last_event_at=NOW,
        calendar_days=tuple(_dates(calendar_days)),
        session_ids=tuple(f"s{i}" for i in range(sessions)),
        round_trip_ids=tuple(f"rt-{i}" for i in range(round_trips)),
        instrument_ids=instrument_ids,
        corrections=corrections,
        breaker_trips=breaker_trips,
        cost_breaches=cost_breaches,
        drawdown_breaches=drawdown_breaches,
        stale=stale,
        event_count=calendar_days + sessions + round_trips,
        divergences=divergences,
        duplicates=duplicates,
        unresolved_alerts=unresolved_alerts,
        missed_flats=missed_flats,
        replay_mismatches=replay_mismatches,
        round_trip_records=round_trip_records,
    )


# ---------------------------------------------------------------------------
# Floors: exact simultaneous thresholds
# ---------------------------------------------------------------------------

def test_floors_constants_are_exact():
    from trader.promotion.paper_gate import (
        FLOOR_CALENDAR_DAYS,
        FLOOR_INSTRUMENTS,
        FLOOR_ROUND_TRIPS,
        FLOOR_SESSIONS,
    )

    assert FLOOR_CALENDAR_DAYS == 30
    assert FLOOR_SESSIONS == 20
    assert FLOOR_ROUND_TRIPS == 50
    assert FLOOR_INSTRUMENTS == 5


def test_all_floors_met_with_clean_evidence_passes():
    from trader.promotion.paper_gate import PaperGate

    decision = PaperGate().evaluate(_window())
    assert decision.passed is True
    assert decision.floors_met is True
    assert decision.blockers == ()


@pytest.mark.parametrize("field,value", [
    ("calendar_days", 29),
    ("sessions", 19),
    ("round_trips", 49),
    ("instruments", 4),
])
def test_missing_any_single_floor_blocks_pass(field, value):
    from trader.promotion.paper_gate import PaperGate

    window = _window(**{field: value})
    decision = PaperGate().evaluate(window)
    assert decision.passed is False
    assert decision.floors_met is False


def test_floors_must_be_met_simultaneously_not_independently():
    """29 days but every other floor generously exceeded still fails --
    floors are a simultaneous AND, never independently satisfiable."""
    from trader.promotion.paper_gate import PaperGate

    window = _window(calendar_days=29, sessions=40, round_trips=200, instruments=10)
    decision = PaperGate().evaluate(window)
    assert decision.passed is False
    day_floor = next(f for f in decision.floors if f.name == "calendar_days")
    assert day_floor.met is False


def test_stale_window_blocks_pass_even_with_floors_met():
    from trader.promotion.paper_gate import PaperGate

    decision = PaperGate().evaluate(_window(stale=True))
    assert decision.passed is False
    assert "stale_evidence" in decision.blockers


def test_calendar_days_floor_is_elapsed_span_not_distinct_session_date_count():
    """CRITICAL: a normal ~30-calendar-day paper soak with only ~20 trading
    sessions (weekends/holidays skipped) must be able to meet the 30-day
    floor -- the floor is the elapsed SPAN between the earliest and latest
    in-window session date, not a COUNT of distinct session dates."""
    from trader.promotion.paper_gate import PaperGate

    # 20 session dates spanning exactly 30 elapsed calendar days (gaps for
    # weekends), i.e. only 20 distinct dates -- NOT 30 distinct dates.
    offsets = (0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 18, 19, 20, 21, 29)
    start = dt.date(2026, 5, 1)
    calendar_dates = tuple((start + dt.timedelta(days=o)).isoformat() for o in offsets)

    window = replace(_window(), calendar_days=calendar_dates)
    assert len(window.calendar_days) == 20  # distinct dates: only 20
    assert window.elapsed_calendar_days == 30  # elapsed span: 30

    decision = PaperGate().evaluate(window)
    day_floor = next(f for f in decision.floors if f.name == "calendar_days")
    assert day_floor.observed == 30
    assert day_floor.met is True
    assert decision.passed is True


def test_calendar_days_floor_still_fails_when_elapsed_span_is_short():
    from trader.promotion.paper_gate import PaperGate

    # 20 sessions crammed into only 25 elapsed calendar days.
    calendar_dates = tuple(
        (dt.date(2026, 5, 1) + dt.timedelta(days=i)).isoformat() for i in range(20)
    )
    window = replace(_window(), calendar_days=calendar_dates)
    assert window.elapsed_calendar_days == 20

    decision = PaperGate().evaluate(window)
    day_floor = next(f for f in decision.floors if f.name == "calendar_days")
    assert day_floor.met is False
    assert decision.passed is False


# ---------------------------------------------------------------------------
# CRITICAL: fail closed on missing economic/diversification evidence -- a
# round trip that lacks pnl_after_cost/instrument_id must never be silently
# treated as "nothing to compute, so skip the check".
# ---------------------------------------------------------------------------

def test_missing_pnl_after_cost_on_all_round_trips_blocks():
    from trader.promotion.paper_gate import BLOCK_MISSING_ECONOMIC_EVIDENCE, PaperGate

    instruments = _instruments(5)
    records = tuple(
        {"round_trip_id": f"rt-{i}", "instrument_id": instruments[i % 5]}
        for i in range(50)
    )
    window = _window(round_trip_records=records)
    decision = PaperGate().evaluate(window)
    assert decision.passed is False
    assert BLOCK_MISSING_ECONOMIC_EVIDENCE in decision.blockers
    assert decision.metrics["net_expectancy"] is None


def test_unparseable_pnl_after_cost_on_all_round_trips_blocks():
    from trader.promotion.paper_gate import BLOCK_MISSING_ECONOMIC_EVIDENCE, PaperGate

    instruments = _instruments(5)
    records = tuple(
        {"round_trip_id": f"rt-{i}", "instrument_id": instruments[i % 5], "pnl_after_cost": "n/a"}
        for i in range(50)
    )
    window = _window(round_trip_records=records)
    decision = PaperGate().evaluate(window)
    assert decision.passed is False
    assert BLOCK_MISSING_ECONOMIC_EVIDENCE in decision.blockers


def test_missing_instrument_ids_on_all_round_trips_blocks():
    from trader.promotion.paper_gate import BLOCK_MISSING_INSTRUMENT_EVIDENCE, PaperGate

    records = tuple({"round_trip_id": f"rt-{i}", "pnl_after_cost": 10.0} for i in range(50))
    window = _window(round_trip_records=records)
    decision = PaperGate().evaluate(window)
    assert decision.passed is False
    assert BLOCK_MISSING_INSTRUMENT_EVIDENCE in decision.blockers
    assert decision.metrics["max_instrument_concentration"] is None


def test_50_round_trips_without_economic_or_diversification_evidence_never_passes():
    """The exact CRITICAL scenario from the review: 50 round trips exist
    (the floor is met) but NONE carry usable economic/diversification
    evidence -- must never return passed=True."""
    from trader.promotion.paper_gate import PaperGate

    records = tuple({"round_trip_id": f"rt-{i}"} for i in range(50))
    window = _window(round_trip_records=records)
    decision = PaperGate().evaluate(window)
    assert decision.passed is False


def test_partial_missing_pnl_still_excluded_not_zeroed_when_some_records_have_it():
    """Not every round trip missing pnl_after_cost blocks -- only a TOTAL
    absence does. Partial coverage still uses the documented "exclude, don't
    zero" averaging (existing behavior, unaffected by the fail-closed fix)."""
    from trader.promotion.paper_gate import BLOCK_MISSING_ECONOMIC_EVIDENCE, PaperGate

    instruments = _instruments(5)
    records = _round_trip_records(49, instruments=instruments, pnl=10.0) + (
        {"round_trip_id": "rt-49", "instrument_id": instruments[0]},
    )
    window = _window(round_trip_records=records)
    decision = PaperGate().evaluate(window)
    assert BLOCK_MISSING_ECONOMIC_EVIDENCE not in decision.blockers
    assert decision.metrics["net_expectancy"] == 10.0


# ---------------------------------------------------------------------------
# Each of the eight blockers independently blocks -- floors otherwise met
# ---------------------------------------------------------------------------

def test_divergence_independently_blocks():
    from trader.promotion.paper_gate import BLOCK_DIVERGENCE, PaperGate

    window = _window(divergences=({"trade_id": "t1", "detail": "signal mismatch"},))
    decision = PaperGate().evaluate(window)
    assert decision.passed is False
    assert BLOCK_DIVERGENCE in decision.blockers


def test_duplicate_independently_blocks():
    from trader.promotion.paper_gate import BLOCK_DUPLICATE, PaperGate

    window = _window(duplicates=({"command_id": "cmd-1"},))
    decision = PaperGate().evaluate(window)
    assert decision.passed is False
    assert BLOCK_DUPLICATE in decision.blockers


def test_unresolved_alert_independently_blocks():
    from trader.promotion.paper_gate import BLOCK_UNRESOLVED_ALERT, PaperGate

    window = _window(unresolved_alerts=({"alert_id": "a1", "alert_type": "ambiguity"},))
    decision = PaperGate().evaluate(window)
    assert decision.passed is False
    assert BLOCK_UNRESOLVED_ALERT in decision.blockers


def test_missed_flat_independently_blocks():
    from trader.promotion.paper_gate import BLOCK_MISSED_FLAT, PaperGate

    window = _window(missed_flats=({"session_id": "s0"},))
    decision = PaperGate().evaluate(window)
    assert decision.passed is False
    assert BLOCK_MISSED_FLAT in decision.blockers


def test_replay_mismatch_independently_blocks():
    from trader.promotion.paper_gate import BLOCK_REPLAY_MISMATCH, PaperGate

    window = _window(replay_mismatches=({"session_id": "s0"},))
    decision = PaperGate().evaluate(window)
    assert decision.passed is False
    assert BLOCK_REPLAY_MISMATCH in decision.blockers


def test_stressed_cost_breach_independently_blocks():
    from trader.promotion.paper_gate import BLOCK_STRESSED_COST_BREACH, PaperGate

    window = _window(cost_breaches=({"metric": "stressed_cost_bps"},))
    decision = PaperGate().evaluate(window)
    assert decision.passed is False
    assert BLOCK_STRESSED_COST_BREACH in decision.blockers


def test_negative_expectancy_independently_blocks():
    from trader.promotion.paper_gate import BLOCK_NEGATIVE_EXPECTANCY, PaperGate

    instruments = _instruments(5)
    records = _round_trip_records(50, instruments=instruments, pnl=-5.0)
    window = _window(round_trip_records=records)
    decision = PaperGate().evaluate(window)
    assert decision.passed is False
    assert BLOCK_NEGATIVE_EXPECTANCY in decision.blockers
    assert decision.metrics["net_expectancy"] < 0


def test_zero_expectancy_is_not_positive_and_blocks():
    from trader.promotion.paper_gate import BLOCK_NEGATIVE_EXPECTANCY, PaperGate

    instruments = _instruments(5)
    records = _round_trip_records(50, instruments=instruments, pnl=0.0)
    window = _window(round_trip_records=records)
    decision = PaperGate().evaluate(window)
    assert BLOCK_NEGATIVE_EXPECTANCY in decision.blockers


def test_concentration_independently_blocks():
    from trader.promotion.paper_gate import BLOCK_CONCENTRATION, PaperGate

    instruments = _instruments(5)
    # 45 of 50 round trips concentrated in a single instrument.
    records = [
        {"round_trip_id": f"rt-{i}", "instrument_id": instruments[0], "pnl_after_cost": 10.0}
        for i in range(45)
    ] + [
        {"round_trip_id": f"rt-{45 + i}", "instrument_id": instruments[i + 1], "pnl_after_cost": 10.0}
        for i in range(4)
    ]
    window = _window(round_trip_records=tuple(records))
    decision = PaperGate().evaluate(window)
    assert decision.passed is False
    assert BLOCK_CONCENTRATION in decision.blockers
    assert decision.metrics["max_instrument_concentration"] > 0.8


def test_breaker_trip_and_drawdown_breach_also_block():
    from trader.promotion.paper_gate import BLOCK_BREAKER_TRIP, BLOCK_DRAWDOWN_BREACH, PaperGate

    breaker_decision = PaperGate().evaluate(_window(breaker_trips=({"incident_id": "inc-1"},)))
    assert BLOCK_BREAKER_TRIP in breaker_decision.blockers

    drawdown_decision = PaperGate().evaluate(_window(drawdown_breaches=({"drawdown_pct": 5.0},)))
    assert BLOCK_DRAWDOWN_BREACH in drawdown_decision.blockers


def test_blockers_are_additive_not_mutually_exclusive():
    from trader.promotion.paper_gate import BLOCK_DIVERGENCE, BLOCK_MISSED_FLAT, PaperGate

    window = _window(
        divergences=({"trade_id": "t1"},),
        missed_flats=({"session_id": "s0"},),
    )
    decision = PaperGate().evaluate(window)
    assert BLOCK_DIVERGENCE in decision.blockers
    assert BLOCK_MISSED_FLAT in decision.blockers


def test_resolved_alert_does_not_block():
    """An alert with a distinct alert_id that IS marked resolved never makes
    it into ``EvidenceWindow.unresolved_alerts`` in the first place (Task 1
    projection semantics) -- confirm PaperGate reflects that cleanly."""
    from trader.promotion.paper_gate import BLOCK_UNRESOLVED_ALERT, PaperGate

    window = _window(unresolved_alerts=())
    decision = PaperGate().evaluate(window)
    assert BLOCK_UNRESOLVED_ALERT not in decision.blockers


# ---------------------------------------------------------------------------
# Recommended stage + decision payload
# ---------------------------------------------------------------------------

def test_floors_not_yet_met_with_no_blockers_recommends_collecting():
    from trader.promotion.paper_gate import PAPER_COLLECTING, PaperGate

    decision = PaperGate().evaluate(_window(sessions=5))
    assert decision.passed is False
    assert decision.recommended_stage == PAPER_COLLECTING


def test_blocker_present_recommends_failed_even_if_floors_met():
    from trader.promotion.paper_gate import PAPER_FAILED, PaperGate

    decision = PaperGate().evaluate(_window(cost_breaches=({"metric": "x"},)))
    assert decision.recommended_stage == PAPER_FAILED


def test_all_floors_met_no_blockers_recommends_passed():
    from trader.promotion.paper_gate import PAPER_PASSED, PaperGate

    decision = PaperGate().evaluate(_window())
    assert decision.recommended_stage == PAPER_PASSED


def test_decision_to_payload_is_json_serializable_and_deterministic():
    import json

    from trader.promotion.paper_gate import PaperGate

    decision = PaperGate().evaluate(_window())
    payload_a = decision.to_payload()
    payload_b = PaperGate().evaluate(_window()).to_payload()
    assert json.dumps(payload_a, sort_keys=True) == json.dumps(payload_b, sort_keys=True)
    assert payload_a["passed"] is True


# ---------------------------------------------------------------------------
# Correction impact: removal, counter reset, clean-session-streak reset
# ---------------------------------------------------------------------------

def test_clean_session_streak_counts_back_from_most_recent_clean_session():
    from trader.promotion.paper_gate import clean_session_streak

    window = _window(sessions=10, breaker_trips=())
    assert clean_session_streak(window) == 10


def test_clean_session_streak_is_zero_when_incident_names_no_session():
    """An incident payload with no ``session_id`` conservatively taints every
    session -- omission is never treated as 'safe'."""
    from trader.promotion.paper_gate import clean_session_streak

    window = _window(sessions=10, breaker_trips=({"incident_id": "inc-1"},))
    assert clean_session_streak(window) == 0


def test_clean_session_streak_stops_at_tainted_session():
    from trader.promotion.paper_gate import clean_session_streak

    window = _window(sessions=10, missed_flats=({"session_id": "s4"},))
    # sessions s0..s9 sorted; only sessions AFTER (alphabetically greater
    # than) the tainted one remain in the trailing streak: s5..s9 = 5.
    assert clean_session_streak(window) == 5


def test_correction_impact_reports_removed_sessions_and_round_trips():
    from trader.promotion.paper_gate import correction_impact

    old_window = _window(sessions=20, round_trips=50, instruments=5,
                         corrections=())
    new_window = _window(
        sessions=15, round_trips=40, instruments=5,
        window_reset_at=NOW,
        corrections=({"scope": "config", "description": "bad allowlist entry"},),
    )
    impact = correction_impact(old_window, new_window)
    assert set(impact["removed_sessions"]) == {"s15", "s16", "s17", "s18", "s19"}
    assert len(impact["removed_round_trips"]) == 10
    assert impact["old_projection"] == old_window.to_payload()
    assert impact["new_projection"] == new_window.to_payload()


def test_non_safety_correction_does_not_force_streak_reset():
    from trader.promotion.paper_gate import correction_impact

    old_window = _window(sessions=10)
    new_window = _window(
        sessions=8, window_reset_at=NOW,
        corrections=({"scope": "config", "description": "config bump"},),
    )
    impact = correction_impact(old_window, new_window)
    assert impact["clean_session_streak_after"] == 8


@pytest.mark.parametrize("scope", ["risk", "data"])
def test_safety_correction_resets_clean_session_streak_to_zero(scope):
    from trader.promotion.paper_gate import correction_impact

    old_window = _window(sessions=10)
    new_window = _window(
        sessions=8, window_reset_at=NOW,
        corrections=({"scope": scope, "description": "risk policy fix"},),
    )
    impact = correction_impact(old_window, new_window)
    assert impact["clean_session_streak_before"] == 10
    assert impact["clean_session_streak_after"] == 0


def test_correction_impact_with_no_corrections_on_new_window_is_a_noop_diff():
    from trader.promotion.paper_gate import correction_impact

    window = _window(sessions=10)
    impact = correction_impact(window, window)
    assert impact["removed_sessions"] == []
    assert impact["removed_round_trips"] == []
    assert impact["clean_session_streak_before"] == impact["clean_session_streak_after"]


# ---------------------------------------------------------------------------
# Shadow mode: compute intents/decisions, never register execute; compare
# ---------------------------------------------------------------------------

def _make_compute(intents, decisions, *, register_ids=()):
    """A fake sealed-input compute pipeline: always produces the same
    intents/decisions, and (whenever a strategy would decide to trade)
    always tries to register ``register_ids`` via whatever
    ``execute_automated_intent`` callable it is handed -- exactly like the
    real automated pipeline, where deciding and submitting are the same
    code path. This is what makes the shadow guard load-bearing: it is the
    ONLY thing standing between "would have traded" and an actual broker
    call, for the exact same compute logic paper/live would run."""

    def compute(sealed_input, execute_automated_intent):
        for intent_id in register_ids:
            execute_automated_intent(intent_id=intent_id, sealed_input=sealed_input)
        from trader.promotion.paper_gate import ShadowTrace
        return ShadowTrace(mode="unset", intents=tuple(intents), decisions=tuple(decisions),
                           registered_intents=(), intercepted_registrations=())

    return compute


def test_shadow_run_never_calls_the_real_execute_even_when_compute_tries_to():
    """The SAME compute pipeline that would register in paper/live mode is
    run under shadow with an interceptor swapped in: the attempt is
    captured, never reaches anything real, and never raises -- a strategy
    that decides to trade must not crash a shadow comparison."""
    from trader.promotion.paper_gate import ShadowRunner

    real_calls = []

    def real_execute(**kwargs):
        real_calls.append(kwargs)
        return {"ok": True}

    compute = _make_compute(
        intents=({"intent_id": "i1"},), decisions=({"approved": True},),
        register_ids=("i1",),
    )
    runner = ShadowRunner(compute)
    # Note: real_execute is never even passed to run_shadow -- there is no
    # parameter through which it could leak in.
    trace = runner.run_shadow({"session": "s1"})
    assert trace.mode == "shadow"
    assert trace.registered_intents == ()
    assert trace.intercepted_registrations == ("i1",)
    assert real_calls == []


def test_shadow_run_with_no_registration_attempt_intercepts_nothing():
    from trader.promotion.paper_gate import ShadowRunner

    compute = _make_compute(intents=({"intent_id": "i1"},), decisions=({"approved": True},))
    runner = ShadowRunner(compute)
    trace = runner.run_shadow({"session": "s1"})
    assert trace.mode == "shadow"
    assert trace.registered_intents == ()
    assert trace.intercepted_registrations == ()
    assert trace.intents == ({"intent_id": "i1"},)


def test_paper_run_tracks_registered_intents():
    from trader.promotion.paper_gate import ShadowRunner

    compute = _make_compute(
        intents=({"intent_id": "i1"},), decisions=({"approved": True},),
        register_ids=("i1",),
    )
    runner = ShadowRunner(compute)
    calls = []

    def execute_automated_intent(*, intent_id, sealed_input):
        calls.append(intent_id)
        return {"ok": True}

    trace = runner.run_paper({"session": "s1"}, execute_automated_intent)
    assert trace.mode == "paper"
    assert trace.registered_intents == ("i1",)
    assert trace.intercepted_registrations == ()
    assert calls == ["i1"]


def test_compare_matches_when_shadow_would_register_exactly_what_paper_registers():
    from trader.promotion.paper_gate import ShadowRunner

    compute = _make_compute(
        intents=({"intent_id": "i1"},), decisions=({"approved": True},),
        register_ids=("i1",),
    )
    runner = ShadowRunner(compute)
    result = runner.compare({"session": "s1"}, lambda **kw: {"ok": True})
    assert result.matched is True
    assert result.divergences == ()
    assert result.shadow_trace.registered_intents == ()
    assert result.shadow_trace.intercepted_registrations == ("i1",)
    assert result.paper_trace.registered_intents == ("i1",)


def test_compare_reports_divergence_when_intents_or_decisions_disagree():
    from trader.promotion.paper_gate import ShadowRunner

    calls = {"n": 0}

    def compute(sealed_input, execute_automated_intent):
        from trader.promotion.paper_gate import ShadowTrace
        # Simulate non-determinism: the second call (paper) sees a different
        # decision than the first (shadow) for the same sealed input.
        calls["n"] += 1
        approved = calls["n"] == 1
        return ShadowTrace(mode="unset", intents=({"intent_id": "i1"},),
                           decisions=({"approved": approved},), registered_intents=(),
                           intercepted_registrations=())

    runner = ShadowRunner(compute)
    result = runner.compare({"session": "s1"}, lambda **kw: {"ok": True})
    assert result.matched is False
    assert len(result.divergences) >= 1


def test_shadow_run_raises_if_compute_reports_a_real_registration():
    """IMPORTANT (defense in depth): ``registered_intents`` is documented to
    be populated ONLY by ``run_paper``'s real tracking wrapper -- if
    ``compute`` returns a trace with a non-empty ``registered_intents``
    while running under ``run_shadow`` (i.e. it never went through the
    interceptor at all, e.g. because the pipeline held a separate reference
    to a real executor), that is proof of an escape. ``run_shadow`` must
    raise loudly instead of silently coercing it back to ``()``."""
    from trader.promotion.paper_gate import ShadowEscapeError, ShadowRunner, ShadowTrace

    def compute(sealed_input, execute_automated_intent):
        # Never calls the injected interceptor at all -- simulates a
        # pipeline that registered via some other, real code path and
        # reports the result on the trace directly.
        return ShadowTrace(
            mode="unset", intents=({"intent_id": "i1"},), decisions=({"approved": True},),
            registered_intents=("i1",), intercepted_registrations=(),
        )

    runner = ShadowRunner(compute)
    with pytest.raises(ShadowEscapeError) as exc_info:
        runner.run_shadow({"session": "s1"})
    assert exc_info.value.escaped_intent_ids == ("i1",)


def test_compare_reports_divergence_when_shadow_would_register_but_paper_does_not():
    """A pure decision divergence beyond intents/decisions themselves: what
    shadow WOULD have registered must match what paper ACTUALLY registered
    -- if they diverge, that is itself reported, even when the raw
    intents/decisions sequences happen to look identical."""
    from trader.promotion.paper_gate import ShadowRunner

    calls = {"n": 0}

    def compute(sealed_input, execute_automated_intent):
        from trader.promotion.paper_gate import ShadowTrace
        calls["n"] += 1
        if calls["n"] == 1:  # shadow half: attempts to register
            execute_automated_intent(intent_id="i1", sealed_input=sealed_input)
        # paper half (n == 2): deliberately does NOT register -- e.g. a
        # risk-gate rejection that only fires on the real path.
        return ShadowTrace(mode="unset", intents=({"intent_id": "i1"},),
                           decisions=({"approved": True},), registered_intents=(),
                           intercepted_registrations=())

    runner = ShadowRunner(compute)
    result = runner.compare({"session": "s1"}, lambda **kw: {"ok": True})
    assert result.matched is False
    assert any(d["kind"] == "registration" for d in result.divergences)
