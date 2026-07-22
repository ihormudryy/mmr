"""P4 Task 3 -- per-instrument liquidity and halt evidence gate.

``LiquidityEvidence.evaluate(conid, LiquidityWindow) -> InstrumentEligibility``
is a pure function -- no clock, no database, no dependency on
``EvidenceStore`` (liquidity/halt readings are not P3 trade attribution;
they are per-session, per-instrument market-structure facts a caller
assembles from broker/market-data snapshots and hands in as a
``LiquidityWindow``, chronologically ordered oldest-first).

Two independent suspension triggers, mirroring the plan exactly:

1. **Five consecutive below-liquidity-floor sessions** suspend the
   instrument. Recovering is simple and immediate: the very next session
   that meets the floor again resets the streak to zero and restores
   eligibility -- a plain liquidity dip is not treated as a safety incident
   requiring a heavier review.
2. **An unscheduled halt suspends the instrument immediately** -- even on
   the very first session ever evaluated, even if every liquidity reading
   is otherwise perfect. A *scheduled* halt (a known, expected market-
   structure event) does not trigger this path. Per "missing safety
   evidence always wins", a halt session that does not say whether it was
   scheduled defaults to unscheduled (the worse case) via
   ``LiquiditySession.halt_scheduled``'s ``False`` default.

Halt requalification is deliberately heavier than a simple liquidity-floor
recovery, and has NO fixed-calendar-day shortcut -- only counting real,
complete, contiguous qualifying SESSIONS satisfies it, regardless of how
much wall-clock time elapses. All four of the following must hold
simultaneously against the LATEST unscheduled halt in the window:

* Five complete qualifying sessions after the halt -- non-halted, meeting
  the floor, and CONTIGUOUS (any disqualifying session in between resets
  the count; it is the trailing streak since the last disqualifying event,
  not a raw total).
* Current liquidity -- the single latest session in the window must itself
  meet the floor and not be halted.
* A passed halt-day replay (``LiquiditySession.halt_replay_passed is True``
  on the halt session itself -- ``None``/``False`` both fail closed).
* An operator-reviewed event (``LiquiditySession.halt_operator_reviewed``
  on the halt session).

Any one requirement missing keeps the instrument suspended; the specific
gaps are reported in ``HaltRequalificationStatus.unmet_requirements`` for
an operator to act on.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional, Sequence

__all__ = [
    "BELOW_FLOOR_SUSPENSION_THRESHOLD", "HALT_REQUALIFICATION_SESSIONS",
    "REASON_UNSCHEDULED_HALT", "REASON_FIVE_CONSECUTIVE_BELOW_FLOOR", "REASON_NO_EVIDENCE",
    "LiquiditySession", "LiquidityWindow", "HaltRequalificationStatus", "InstrumentEligibility",
    "LiquidityEvidence",
]

# Five consecutive below-floor sessions suspend an instrument (plan Task 3).
BELOW_FLOOR_SUSPENSION_THRESHOLD = 5
# Five complete qualifying sessions are required to requalify after a halt.
HALT_REQUALIFICATION_SESSIONS = 5

REASON_UNSCHEDULED_HALT = "unscheduled_halt"
REASON_FIVE_CONSECUTIVE_BELOW_FLOOR = "five_consecutive_below_floor_sessions"
REASON_NO_EVIDENCE = "no_liquidity_evidence"


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


@dataclass(frozen=True)
class LiquiditySession:
    """One instrument's liquidity/halt snapshot for a single completed
    trading session. ``halt_scheduled`` defaults to ``False`` (i.e. an
    unspecified halt is treated as unscheduled -- the worse case, never
    assumed benign). ``halt_replay_passed``/``halt_operator_reviewed`` are
    only meaningful when ``halted`` is true and are read only off the
    LATEST relevant halt session during requalification."""

    session_id: str
    as_of: dt.datetime
    meets_floor: bool
    halted: bool = False
    halt_scheduled: bool = False
    halt_operator_reviewed: bool = False
    halt_replay_passed: Optional[bool] = None
    metrics: Optional[dict[str, Any]] = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "as_of": _as_utc(self.as_of).isoformat(),
            "meets_floor": self.meets_floor,
            "halted": self.halted,
            "halt_scheduled": self.halt_scheduled,
            "halt_operator_reviewed": self.halt_operator_reviewed,
            "halt_replay_passed": self.halt_replay_passed,
            "metrics": dict(self.metrics) if self.metrics is not None else None,
        }


@dataclass(frozen=True)
class LiquidityWindow:
    """Chronologically ordered (oldest first) liquidity sessions for one
    instrument. Caller-assembled -- no store binding of its own."""

    conid: Any
    as_of: dt.datetime
    sessions: tuple[LiquiditySession, ...]


@dataclass(frozen=True)
class HaltRequalificationStatus:
    """Status of the four halt-requalification requirements against the
    LATEST unscheduled halt session found in the window."""

    halt_session_id: str
    qualifying_sessions_count: int
    qualifying_sessions_required: int
    current_liquidity_met: bool
    halt_day_replay_passed: bool
    operator_reviewed: bool
    met: bool
    unmet_requirements: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "halt_session_id": self.halt_session_id,
            "qualifying_sessions_count": self.qualifying_sessions_count,
            "qualifying_sessions_required": self.qualifying_sessions_required,
            "current_liquidity_met": self.current_liquidity_met,
            "halt_day_replay_passed": self.halt_day_replay_passed,
            "operator_reviewed": self.operator_reviewed,
            "met": self.met,
            "unmet_requirements": list(self.unmet_requirements),
        }


@dataclass(frozen=True)
class InstrumentEligibility:
    """Result of ``LiquidityEvidence.evaluate`` -- a pure snapshot."""

    conid: str
    as_of: dt.datetime
    eligible: bool
    suspended: bool
    reasons: tuple[str, ...]
    consecutive_below_floor_sessions: int
    active_halt: Optional[dict[str, Any]]
    requalification: Optional[HaltRequalificationStatus]

    def to_payload(self) -> dict[str, Any]:
        return {
            "conid": self.conid,
            "as_of": _as_utc(self.as_of).isoformat(),
            "eligible": self.eligible,
            "suspended": self.suspended,
            "reasons": list(self.reasons),
            "consecutive_below_floor_sessions": self.consecutive_below_floor_sessions,
            "active_halt": dict(self.active_halt) if self.active_halt is not None else None,
            "requalification": (
                self.requalification.to_payload() if self.requalification is not None else None
            ),
        }


def _trailing_below_floor_streak(sessions: Sequence[LiquiditySession]) -> int:
    """Count of the most-recent consecutive sessions failing the liquidity
    floor. A scheduled halt is neutral -- it neither extends nor breaks the
    streak (it is an expected pause, not a liquidity signal either way)."""
    streak = 0
    for session in reversed(sessions):
        if session.halted and session.halt_scheduled:
            continue
        if not session.meets_floor:
            streak += 1
        else:
            break
    return streak


def _trailing_qualifying_streak(sessions: Sequence[LiquiditySession]) -> int:
    """Count of the most-recent consecutive sessions that are non-halted
    AND meet the liquidity floor -- the contiguous "clean since the last
    disqualifying event" streak used for halt requalification."""
    streak = 0
    for session in reversed(sessions):
        if session.halted or not session.meets_floor:
            break
        streak += 1
    return streak


def _last_unscheduled_halt_index(sessions: Sequence[LiquiditySession]) -> Optional[int]:
    index: Optional[int] = None
    for i, session in enumerate(sessions):
        if session.halted and not session.halt_scheduled:
            index = i
    return index


class LiquidityEvidence:
    """Evaluates one instrument's ``LiquidityWindow`` against the
    below-floor-streak and halt-requalification suspension rules.
    Stateless and side-effect free."""

    def __init__(
        self,
        *,
        below_floor_suspension_threshold: int = BELOW_FLOOR_SUSPENSION_THRESHOLD,
        halt_requalification_sessions: int = HALT_REQUALIFICATION_SESSIONS,
    ):
        self._below_floor_suspension_threshold = below_floor_suspension_threshold
        self._halt_requalification_sessions = halt_requalification_sessions

    def evaluate(self, conid: Any, window: LiquidityWindow) -> InstrumentEligibility:
        if str(window.conid) != str(conid):
            raise ValueError(
                f"conid mismatch: requested {conid!r}, window is for {window.conid!r}"
            )

        sessions = window.sessions
        if not sessions:
            return InstrumentEligibility(
                conid=str(conid),
                as_of=window.as_of,
                eligible=False,
                suspended=True,
                reasons=(REASON_NO_EVIDENCE,),
                consecutive_below_floor_sessions=0,
                active_halt=None,
                requalification=None,
            )

        below_floor_streak = _trailing_below_floor_streak(sessions)
        halt_index = _last_unscheduled_halt_index(sessions)

        if halt_index is not None:
            return self._evaluate_halt_requalification(
                conid, window.as_of, sessions, halt_index, below_floor_streak,
            )

        suspended = below_floor_streak >= self._below_floor_suspension_threshold
        return InstrumentEligibility(
            conid=str(conid),
            as_of=window.as_of,
            eligible=not suspended,
            suspended=suspended,
            reasons=(REASON_FIVE_CONSECUTIVE_BELOW_FLOOR,) if suspended else (),
            consecutive_below_floor_sessions=below_floor_streak,
            active_halt=None,
            requalification=None,
        )

    def _evaluate_halt_requalification(
        self,
        conid: Any,
        as_of: dt.datetime,
        sessions: Sequence[LiquiditySession],
        halt_index: int,
        below_floor_streak: int,
    ) -> InstrumentEligibility:
        halt = sessions[halt_index]
        after = sessions[halt_index + 1:]
        qualifying_streak = _trailing_qualifying_streak(after)
        latest = sessions[-1]
        current_liquidity_met = bool(latest.meets_floor) and not latest.halted
        halt_day_replay_passed = halt.halt_replay_passed is True
        operator_reviewed = bool(halt.halt_operator_reviewed)

        unmet: list[str] = []
        if qualifying_streak < self._halt_requalification_sessions:
            unmet.append("insufficient_qualifying_sessions")
        if not current_liquidity_met:
            unmet.append("current_liquidity_not_met")
        if not halt_day_replay_passed:
            unmet.append("halt_day_replay_not_passed")
        if not operator_reviewed:
            unmet.append("operator_review_missing")

        met = not unmet
        requalification = HaltRequalificationStatus(
            halt_session_id=halt.session_id,
            qualifying_sessions_count=qualifying_streak,
            qualifying_sessions_required=self._halt_requalification_sessions,
            current_liquidity_met=current_liquidity_met,
            halt_day_replay_passed=halt_day_replay_passed,
            operator_reviewed=operator_reviewed,
            met=met,
            unmet_requirements=tuple(unmet),
        )

        return InstrumentEligibility(
            conid=str(conid),
            as_of=as_of,
            eligible=met,
            suspended=not met,
            reasons=() if met else (REASON_UNSCHEDULED_HALT,),
            consecutive_below_floor_sessions=below_floor_streak,
            active_halt=halt.to_payload(),
            requalification=requalification,
        )
