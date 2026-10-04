"""P4 Task 3 -- per-instrument liquidity + halt evidence gate.

Contract:
* ``LiquidityEvidence.evaluate(conid, LiquidityWindow) -> InstrumentEligibility``
  is a pure function -- no clock, no database.
* Five CONSECUTIVE below-liquidity-floor sessions suspend the instrument;
  recovering (a session that meets the floor again) resets the streak and
  restores eligibility immediately -- no heavier requalification is needed
  for a plain liquidity dip.
* An UNSCHEDULED halt suspends the instrument immediately, even on the very
  first session evaluated and even if every liquidity reading is fine. A
  SCHEDULED halt does not trigger this immediate-suspension path.
* Halt requalification requires ALL of: five COMPLETE qualifying sessions
  after the halt (non-halted, meets-floor, contiguous -- any disqualifying
  session in between resets the count), CURRENT liquidity (the latest
  session meets the floor), a passed halt-day replay, and an
  operator-reviewed event. There is deliberately no calendar-day shortcut:
  a huge gap in ``as_of`` timestamps with too few actual qualifying
  sessions never requalifies the instrument.
"""
from __future__ import annotations

import datetime as dt

import pytest

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
CONID = "265598"


def _session(
    idx: int,
    *,
    meets_floor: bool = True,
    halted: bool = False,
    halt_scheduled: bool = False,
    halt_operator_reviewed: bool = False,
    halt_replay_passed: bool | None = None,
    day: int = 0,
):
    from trader.promotion.liquidity_evidence import LiquiditySession

    return LiquiditySession(
        session_id=f"s{idx}",
        as_of=NOW + dt.timedelta(days=day if day else idx),
        meets_floor=meets_floor,
        halted=halted,
        halt_scheduled=halt_scheduled,
        halt_operator_reviewed=halt_operator_reviewed,
        halt_replay_passed=halt_replay_passed,
    )


def _window(sessions, conid: str = CONID):
    from trader.promotion.liquidity_evidence import LiquidityWindow

    return LiquidityWindow(conid=conid, as_of=NOW, sessions=tuple(sessions))


# ---------------------------------------------------------------------------
# Baseline / simple liquidity floor streak
# ---------------------------------------------------------------------------

def test_all_sessions_meet_floor_is_eligible():
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    sessions = [_session(i) for i in range(10)]
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is True
    assert result.suspended is False
    assert result.reasons == ()


def test_five_consecutive_below_floor_sessions_suspends():
    from trader.promotion.liquidity_evidence import REASON_FIVE_CONSECUTIVE_BELOW_FLOOR, LiquidityEvidence

    sessions = [_session(i, meets_floor=False) for i in range(5)]
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is False
    assert result.suspended is True
    assert REASON_FIVE_CONSECUTIVE_BELOW_FLOOR in result.reasons
    assert result.consecutive_below_floor_sessions == 5


def test_four_consecutive_below_floor_sessions_does_not_suspend():
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    sessions = [_session(i, meets_floor=False) for i in range(4)]
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is True
    assert result.suspended is False
    assert result.consecutive_below_floor_sessions == 4


def test_below_floor_streak_resets_on_a_single_recovering_session():
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    sessions = [_session(i, meets_floor=False) for i in range(4)] + [_session(4, meets_floor=True)]
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is True
    assert result.consecutive_below_floor_sessions == 0


def test_five_below_floor_then_recovery_restores_eligibility_immediately():
    """A plain liquidity dip is not a halt -- one clean session is enough
    to restore eligibility, no five-session requalification needed."""
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    sessions = [_session(i, meets_floor=False) for i in range(6)] + [_session(6, meets_floor=True)]
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is True
    assert result.suspended is False


def test_non_consecutive_below_floor_sessions_never_reach_five():
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    # below, below, ok, below, below, ok, below -- longest run is 2.
    pattern = [False, False, True, False, False, True, False]
    sessions = [_session(i, meets_floor=ok) for i, ok in enumerate(pattern)]
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is True
    assert result.consecutive_below_floor_sessions == 1


# ---------------------------------------------------------------------------
# Halt: unscheduled suspends immediately; scheduled does not
# ---------------------------------------------------------------------------

def test_unscheduled_halt_suspends_immediately_even_on_first_session():
    from trader.promotion.liquidity_evidence import REASON_UNSCHEDULED_HALT, LiquidityEvidence

    sessions = [_session(0, meets_floor=True, halted=True, halt_scheduled=False)]
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is False
    assert result.suspended is True
    assert REASON_UNSCHEDULED_HALT in result.reasons


def test_unscheduled_halt_suspends_even_with_otherwise_perfect_liquidity():
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    sessions = [_session(i) for i in range(10)] + [
        _session(10, meets_floor=True, halted=True, halt_scheduled=False),
    ]
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is False
    assert result.suspended is True


def test_scheduled_halt_does_not_immediately_suspend():
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    sessions = [_session(i) for i in range(5)] + [
        _session(5, meets_floor=True, halted=True, halt_scheduled=True),
    ] + [_session(i) for i in range(6, 10)]
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is True
    assert result.suspended is False


def test_halt_with_unspecified_scheduling_defaults_to_unscheduled():
    """Missing safety evidence always wins: a halt session that doesn't say
    whether it was scheduled is treated as unscheduled (the worse case),
    never assumed benign."""
    from trader.promotion.liquidity_evidence import LiquiditySession, LiquidityEvidence

    session = LiquiditySession(session_id="s0", as_of=NOW, meets_floor=True, halted=True)
    result = LiquidityEvidence().evaluate(CONID, _window([session]))
    assert result.suspended is True


# ---------------------------------------------------------------------------
# Halt requalification
# ---------------------------------------------------------------------------

def _halted_then(qualifying_after: int, **halt_kwargs):
    """One halt session followed by ``qualifying_after`` clean sessions."""
    halt = _session(0, meets_floor=False, halted=True, halt_scheduled=False, **halt_kwargs)
    after = [_session(i, meets_floor=True) for i in range(1, 1 + qualifying_after)]
    return [halt] + after


def test_halt_requalification_succeeds_with_all_requirements_met():
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    sessions = _halted_then(5, halt_operator_reviewed=True, halt_replay_passed=True)
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is True
    assert result.suspended is False
    assert result.requalification is not None
    assert result.requalification.met is True


def test_halt_requalification_fails_with_insufficient_qualifying_sessions():
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    sessions = _halted_then(4, halt_operator_reviewed=True, halt_replay_passed=True)
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is False
    assert result.suspended is True
    assert "insufficient_qualifying_sessions" in result.requalification.unmet_requirements


def test_halt_requalification_fails_missing_operator_review():
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    sessions = _halted_then(5, halt_operator_reviewed=False, halt_replay_passed=True)
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is False
    assert "operator_review_missing" in result.requalification.unmet_requirements


def test_halt_requalification_fails_missing_halt_day_replay():
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    sessions = _halted_then(5, halt_operator_reviewed=True, halt_replay_passed=False)
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is False
    assert "halt_day_replay_not_passed" in result.requalification.unmet_requirements


def test_halt_requalification_fails_missing_halt_day_replay_when_unset():
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    sessions = _halted_then(5, halt_operator_reviewed=True, halt_replay_passed=None)
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is False
    assert "halt_day_replay_not_passed" in result.requalification.unmet_requirements


def test_halt_requalification_fails_when_current_session_below_floor():
    """Five qualifying sessions happened, but liquidity has since dipped
    again -- CURRENT liquidity must also hold, not just the historical
    streak."""
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    sessions = _halted_then(5, halt_operator_reviewed=True, halt_replay_passed=True)
    sessions.append(_session(6, meets_floor=False))
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is False
    assert "current_liquidity_not_met" in result.requalification.unmet_requirements


def test_halt_requalification_disqualifying_session_resets_the_streak():
    """A below-floor session in the middle of the post-halt run breaks the
    contiguous qualifying streak -- it must restart from there, not simply
    subtract one from the total count."""
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    halt = _session(0, meets_floor=False, halted=True, halt_scheduled=False,
                     halt_operator_reviewed=True, halt_replay_passed=True)
    sessions = [halt]
    sessions += [_session(i, meets_floor=True) for i in range(1, 4)]  # 3 qualifying
    sessions += [_session(4, meets_floor=False)]  # disqualifying -- resets streak
    sessions += [_session(i, meets_floor=True) for i in range(5, 9)]  # only 4 after reset
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is False
    assert "insufficient_qualifying_sessions" in result.requalification.unmet_requirements


def test_halt_requalification_no_fixed_day_shortcut():
    """A huge elapsed-time gap between sessions must never substitute for
    actually having five complete qualifying SESSIONS."""
    from trader.promotion.liquidity_evidence import LiquidityEvidence, LiquiditySession

    halt = LiquiditySession(
        session_id="s0", as_of=NOW, meets_floor=False, halted=True, halt_scheduled=False,
        halt_operator_reviewed=True, halt_replay_passed=True,
    )
    # Only 2 qualifying sessions, but 400 days after the halt -- far more
    # than enough calendar time, nowhere near enough actual sessions.
    late = LiquiditySession(
        session_id="s1", as_of=NOW + dt.timedelta(days=400), meets_floor=True,
    )
    later = LiquiditySession(
        session_id="s2", as_of=NOW + dt.timedelta(days=401), meets_floor=True,
    )
    result = LiquidityEvidence().evaluate(CONID, _window([halt, late, later]))
    assert result.eligible is False
    assert "insufficient_qualifying_sessions" in result.requalification.unmet_requirements


def test_halt_requalification_new_halt_after_requalifying_resets_again():
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    sessions = _halted_then(5, halt_operator_reviewed=True, halt_replay_passed=True)
    sessions.append(_session(6, meets_floor=True, halted=True, halt_scheduled=False))
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    assert result.eligible is False
    assert result.suspended is True


# ---------------------------------------------------------------------------
# Validation / edge cases
# ---------------------------------------------------------------------------

def test_conid_mismatch_raises():
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    with pytest.raises(ValueError):
        LiquidityEvidence().evaluate("999999", _window([_session(0)], conid=CONID))


def test_empty_window_is_not_eligible():
    from trader.promotion.liquidity_evidence import REASON_NO_EVIDENCE, LiquidityEvidence

    result = LiquidityEvidence().evaluate(CONID, _window([]))
    assert result.eligible is False
    assert result.suspended is True
    assert REASON_NO_EVIDENCE in result.reasons


def test_conid_accepts_int_and_str_equivalently():
    from trader.promotion.liquidity_evidence import LiquidityEvidence, LiquidityWindow

    window = LiquidityWindow(conid=265598, as_of=NOW, sessions=(_session(0),))
    result = LiquidityEvidence().evaluate("265598", window)
    assert result.eligible is True


# ---------------------------------------------------------------------------
# Payload serialization
# ---------------------------------------------------------------------------

def test_to_payload_is_json_serializable():
    import json

    from trader.promotion.liquidity_evidence import LiquidityEvidence

    sessions = _halted_then(5, halt_operator_reviewed=True, halt_replay_passed=True)
    result = LiquidityEvidence().evaluate(CONID, _window(sessions))
    payload = result.to_payload()
    assert json.dumps(payload, sort_keys=True)
    assert payload["eligible"] is True


def test_to_payload_without_halt_has_null_requalification():
    from trader.promotion.liquidity_evidence import LiquidityEvidence

    result = LiquidityEvidence().evaluate(CONID, _window([_session(0)]))
    payload = result.to_payload()
    assert payload["requalification"] is None
    assert payload["active_halt"] is None
