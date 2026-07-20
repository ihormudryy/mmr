"""P4 Task 2 -- the accelerated paper evidence gate + paper/shadow operation.

``PaperGate.evaluate`` is a pure function of an ``EvidenceWindow``
(``trader.promotion.evidence_store``) that decides whether a strategy's
paper evidence is trustworthy enough to recommend ``PAPER_PASSED``. It never
touches a database or the clock itself -- the caller (a report script, an
operator workflow, or a future scheduler) is responsible for producing a
freshly *projected* window (``EvidenceStore.project``) and handing it here;
that keeps the same "no forged/stale window" discipline
``PromotionStageMachine`` enforces in Task 1, at the layer that actually
knows the paper-specific floors and safety signals.

Two independent requirements gate a pass:

1. **Floors** -- exactly 30 calendar days, 20 completed sessions, 50 round
   trips, and five distinct instruments, ALL met simultaneously. Meeting
   four of the four floors generously is not "close enough" if the fifth
   isn't met -- see ``test_floors_must_be_met_simultaneously_not_independently``.
2. **Blockers** -- divergence, duplicate, unresolved ambiguity/alert, missed
   flat, replay mismatch, a stressed-cost breach, negative expectancy, and
   instrument concentration EACH independently block a pass, regardless of
   the floors. Breaker trips, drawdown breaches, and staleness (Task 1's
   own generic safety signals) block too, for the same reason
   ``PromotionStageMachine._require_clean_evidence`` treats them as
   disqualifying: one incident is never averaged away by volume.

``correction_impact`` and ``clean_session_streak`` implement the plan's
correction-impact rule: a correction already truncates the counted window
(Task 1's ``window_reset_at``); a *safety* correction (scope ``risk`` or
``data``) additionally resets the clean-session streak to zero, and the
old/new projections are recorded verbatim for audit.

``ShadowRunner`` implements shadow/paper operation: the SAME compute
pipeline is run twice over one sealed input, once with a guard that raises
if it is ever asked to register ``execute_automated_intent`` (shadow), and
once with the real callback wired through (paper). ``compare`` runs both
and reports any divergence between the two traces -- proving a strategy's
decisions are stable before its output is ever allowed to touch a broker.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Optional, Sequence

from trader.promotion.evidence_store import EvidenceWindow
from trader.promotion.stage import PAPER_COLLECTING, PAPER_FAILED, PAPER_PASSED

__all__ = [
    "FLOOR_CALENDAR_DAYS", "FLOOR_SESSIONS", "FLOOR_ROUND_TRIPS", "FLOOR_INSTRUMENTS",
    "BLOCK_DIVERGENCE", "BLOCK_DUPLICATE", "BLOCK_UNRESOLVED_ALERT", "BLOCK_MISSED_FLAT",
    "BLOCK_REPLAY_MISMATCH", "BLOCK_STRESSED_COST_BREACH", "BLOCK_NEGATIVE_EXPECTANCY",
    "BLOCK_CONCENTRATION", "BLOCK_BREAKER_TRIP", "BLOCK_DRAWDOWN_BREACH", "BLOCK_STALE_EVIDENCE",
    "MAX_INSTRUMENT_CONCENTRATION",
    "PAPER_COLLECTING", "PAPER_FAILED", "PAPER_PASSED",
    "FloorStatus", "PromotionDecision", "PaperGate",
    "SAFETY_CORRECTION_SCOPES", "is_safety_correction", "clean_session_streak", "correction_impact",
    "ShadowTrace", "ShadowComparisonResult", "ShadowRunner",
]

# Exact simultaneous floors (plan Task 2 / brief): 30 calendar days, 20
# completed sessions, 50 round trips, five instruments. All four must hold
# at once -- see ``PaperGate.evaluate``.
FLOOR_CALENDAR_DAYS = 30
FLOOR_SESSIONS = 20
FLOOR_ROUND_TRIPS = 50
FLOOR_INSTRUMENTS = 5

# Each of these blocks a pass independently, regardless of every other
# blocker or of whether the floors are otherwise met.
BLOCK_DIVERGENCE = "divergence"
BLOCK_DUPLICATE = "duplicate"
BLOCK_UNRESOLVED_ALERT = "unresolved_alert"
BLOCK_MISSED_FLAT = "missed_flat"
BLOCK_REPLAY_MISMATCH = "replay_mismatch"
BLOCK_STRESSED_COST_BREACH = "stressed_cost_breach"
BLOCK_NEGATIVE_EXPECTANCY = "negative_expectancy"
BLOCK_CONCENTRATION = "concentration"
# Task 1's own generic safety signals -- carried forward here so a caller
# relying on PaperGate alone (rather than also re-deriving from the window)
# gets the same fail-closed coverage ``PromotionStageMachine`` has.
BLOCK_BREAKER_TRIP = "breaker_trip"
BLOCK_DRAWDOWN_BREACH = "drawdown_breach"
BLOCK_STALE_EVIDENCE = "stale_evidence"

# No single instrument may account for more than this fraction of in-window
# round trips -- otherwise the "five instruments" floor is technically met
# but the evidence is not meaningfully diversified.
MAX_INSTRUMENT_CONCENTRATION = 0.40


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@dataclass(frozen=True)
class FloorStatus:
    """One simultaneous-floor check: name, the required threshold, what was
    actually observed on the window, and whether it was met."""

    name: str
    required: float
    observed: float
    met: bool

    def to_payload(self) -> dict[str, Any]:
        return {"name": self.name, "required": self.required, "observed": self.observed, "met": self.met}


@dataclass(frozen=True)
class PromotionDecision:
    """The result of ``PaperGate.evaluate`` -- a pure snapshot, never a
    mutation. ``passed`` is true iff every floor is met AND ``blockers`` is
    empty. ``recommended_stage`` maps that onto the Task 1 stage vocabulary
    (``PAPER_PASSED``/``PAPER_FAILED``/``PAPER_COLLECTING``) for a caller
    that wants to feed straight into ``PromotionStageMachine.transition`` --
    the machine still independently re-verifies via its own
    ``evidence_store`` projection, so this is advisory, not authoritative."""

    strategy_id: str
    as_of: dt.datetime
    passed: bool
    floors_met: bool
    recommended_stage: str
    floors: tuple[FloorStatus, ...]
    blockers: tuple[str, ...]
    metrics: dict[str, Any]
    window_reset_at: Optional[dt.datetime]

    def to_payload(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "as_of": _as_utc(self.as_of).isoformat(),
            "passed": self.passed,
            "floors_met": self.floors_met,
            "recommended_stage": self.recommended_stage,
            "floors": [f.to_payload() for f in self.floors],
            "blockers": list(self.blockers),
            "metrics": dict(self.metrics),
            "window_reset_at": (
                _as_utc(self.window_reset_at).isoformat() if self.window_reset_at is not None else None
            ),
        }


def _instrument_key(record: Mapping[str, Any]) -> Optional[str]:
    value = record.get("instrument_id")
    if value is None:
        value = record.get("conid")
    return None if value is None else str(value)


def _expectancy_and_concentration(
    records: Sequence[Mapping[str, Any]],
    *,
    max_concentration: float,
) -> tuple[Optional[float], Optional[float], tuple[str, ...]]:
    """Pure helper: (net_expectancy, max_instrument_concentration, blockers).

    ``net_expectancy`` is the mean ``pnl_after_cost`` across every record
    that carries that field (records without it are excluded from the
    average, never treated as zero -- see
    ``trader.automation.attribution``'s "never assume zero P&L" precedent).
    ``max_instrument_concentration`` is the largest fraction of records
    attributed to a single instrument. Either metric is ``None`` (and never
    blocks) when there is nothing to compute it from.
    """
    pnls = [d for r in records if (d := _decimal(r.get("pnl_after_cost"))) is not None]
    net_expectancy: Optional[float] = None
    blockers: list[str] = []
    if pnls:
        net_expectancy = float(sum(pnls, Decimal("0")) / Decimal(len(pnls)))
        if net_expectancy <= 0:
            blockers.append(BLOCK_NEGATIVE_EXPECTANCY)

    max_concentration_observed: Optional[float] = None
    counts: dict[str, int] = {}
    for record in records:
        key = _instrument_key(record)
        if key is None:
            continue
        counts[key] = counts.get(key, 0) + 1
    if counts:
        total = sum(counts.values())
        max_concentration_observed = max(counts.values()) / total
        if max_concentration_observed > max_concentration:
            blockers.append(BLOCK_CONCENTRATION)

    return net_expectancy, max_concentration_observed, tuple(blockers)


class PaperGate:
    """Evaluates one strategy's ``EvidenceWindow`` against the paper floors
    and safety blockers. Stateless and side-effect free -- safe to call as
    often as evidence changes; the caller decides what to do with the
    resulting ``PromotionDecision`` (e.g. attempt
    ``PromotionStageMachine.transition(..., PAPER_PASSED, ...)``, or persist
    it into a report)."""

    def __init__(self, *, max_instrument_concentration: float = MAX_INSTRUMENT_CONCENTRATION):
        self._max_instrument_concentration = max_instrument_concentration

    def evaluate(self, window: EvidenceWindow) -> PromotionDecision:
        floors = (
            FloorStatus("calendar_days", FLOOR_CALENDAR_DAYS, window.calendar_day_count,
                       window.calendar_day_count >= FLOOR_CALENDAR_DAYS),
            FloorStatus("sessions", FLOOR_SESSIONS, window.session_count,
                       window.session_count >= FLOOR_SESSIONS),
            FloorStatus("round_trips", FLOOR_ROUND_TRIPS, window.round_trip_count,
                       window.round_trip_count >= FLOOR_ROUND_TRIPS),
            FloorStatus("instruments", FLOOR_INSTRUMENTS, window.instrument_count,
                       window.instrument_count >= FLOOR_INSTRUMENTS),
        )
        floors_met = all(f.met for f in floors)

        blockers: list[str] = []
        if window.stale:
            blockers.append(BLOCK_STALE_EVIDENCE)
        if window.breaker_trips:
            blockers.append(BLOCK_BREAKER_TRIP)
        if window.drawdown_breaches:
            blockers.append(BLOCK_DRAWDOWN_BREACH)
        if window.cost_breaches:
            blockers.append(BLOCK_STRESSED_COST_BREACH)
        if window.divergences:
            blockers.append(BLOCK_DIVERGENCE)
        if window.duplicates:
            blockers.append(BLOCK_DUPLICATE)
        if window.unresolved_alerts:
            blockers.append(BLOCK_UNRESOLVED_ALERT)
        if window.missed_flats:
            blockers.append(BLOCK_MISSED_FLAT)
        if window.replay_mismatches:
            blockers.append(BLOCK_REPLAY_MISMATCH)

        net_expectancy, max_concentration, econ_blockers = _expectancy_and_concentration(
            window.round_trip_records, max_concentration=self._max_instrument_concentration,
        )
        blockers.extend(econ_blockers)

        passed = floors_met and not blockers
        if blockers:
            recommended_stage = PAPER_FAILED
        elif floors_met:
            recommended_stage = PAPER_PASSED
        else:
            recommended_stage = PAPER_COLLECTING

        metrics = {
            "net_expectancy": net_expectancy,
            "max_instrument_concentration": max_concentration,
            "round_trip_count_with_pnl": sum(
                1 for r in window.round_trip_records if _decimal(r.get("pnl_after_cost")) is not None
            ),
        }

        return PromotionDecision(
            strategy_id=window.strategy_id,
            as_of=window.as_of,
            passed=passed,
            floors_met=floors_met,
            recommended_stage=recommended_stage,
            floors=floors,
            blockers=tuple(blockers),
            metrics=metrics,
            window_reset_at=window.window_reset_at,
        )


# ---------------------------------------------------------------------------
# Correction impact: removal + counter reset (already true of window_reset_at)
# plus the clean-session-streak rule this task adds on top.
# ---------------------------------------------------------------------------

# Corrections whose scope is safety-relevant (risk policy or data integrity)
# reset the clean-session streak to zero on top of truncating the counted
# window -- a code/config/allowlist correction truncates the window too, but
# does not by itself imply a safety incident occurred.
SAFETY_CORRECTION_SCOPES = frozenset({"risk", "data"})


def is_safety_correction(correction: Mapping[str, Any]) -> bool:
    return str(correction.get("scope")) in SAFETY_CORRECTION_SCOPES


def clean_session_streak(window: EvidenceWindow) -> int:
    """Count of the most-recent consecutive (alphabetically-last) sessions in
    ``window.session_ids`` with no safety incident attributed to them.

    An incident is attributed to a session via its payload's
    ``session_id`` field. An incident that does NOT name a ``session_id``
    conservatively taints every session in the window (omission is never
    treated as "safe") -- callers that want a session-scoped incident to be
    excludable from the streak must record which session it occurred in.
    """
    incident_lists: tuple[Sequence[Mapping[str, Any]], ...] = (
        window.breaker_trips, window.cost_breaches, window.drawdown_breaches,
        window.divergences, window.duplicates, window.unresolved_alerts,
        window.missed_flats, window.replay_mismatches,
    )
    tainted_sessions: set[str] = set()
    for incidents in incident_lists:
        for incident in incidents:
            session_id = incident.get("session_id")
            if session_id:
                tainted_sessions.add(str(session_id))
            else:
                return 0

    streak = 0
    for session_id in reversed(window.session_ids):
        if session_id in tainted_sessions:
            break
        streak += 1
    return streak


def correction_impact(old_window: EvidenceWindow, new_window: EvidenceWindow) -> dict[str, Any]:
    """Pure diff between an "old" and "new" (post-correction) projection.

    Both windows are supplied by the caller -- e.g. a store-backed
    projection taken before appending a correction, and a fresh
    ``EvidenceStore.project`` taken after. ``removed_sessions``/
    ``removed_round_trips``/``removed_instruments`` are exactly the ids that
    were in ``old_window`` but dropped out of ``new_window`` (the concrete
    "affected session/trade removed and counters reset" the plan asks for --
    the reset itself is Task 1's ``window_reset_at`` truncation; this
    function reports its effect). If ``new_window``'s latest correction is a
    *safety* correction (scope risk/data), ``clean_session_streak_after`` is
    forced to zero regardless of what the fresh projection would otherwise
    compute -- a safety correction is never averaged away by an otherwise
    clean trailing streak.
    """
    removed_sessions = sorted(set(old_window.session_ids) - set(new_window.session_ids))
    removed_round_trips = sorted(set(old_window.round_trip_ids) - set(new_window.round_trip_ids))
    removed_instruments = sorted(set(old_window.instrument_ids) - set(new_window.instrument_ids))

    latest_correction = new_window.corrections[-1] if new_window.corrections else None
    safety = latest_correction is not None and is_safety_correction(latest_correction)

    streak_before = clean_session_streak(old_window)
    streak_after = 0 if safety else clean_session_streak(new_window)

    return {
        "strategy_id": new_window.strategy_id,
        "is_safety_correction": safety,
        "latest_correction": dict(latest_correction) if latest_correction is not None else None,
        "removed_sessions": removed_sessions,
        "removed_round_trips": removed_round_trips,
        "removed_instruments": removed_instruments,
        "clean_session_streak_before": streak_before,
        "clean_session_streak_after": streak_after,
        "old_projection": old_window.to_payload(),
        "new_projection": new_window.to_payload(),
    }


# ---------------------------------------------------------------------------
# Shadow / paper operation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShadowTrace:
    """One run's computed intents/decisions.

    ``registered_intents`` is the intent ids that were ACTUALLY registered
    via a real ``execute_automated_intent`` call (only ever non-empty for a
    ``"paper"`` trace). ``intercepted_registrations`` is the intent ids the
    SAME compute logic attempted to register while running under the shadow
    interceptor (only ever non-empty for a ``"shadow"`` trace) -- captured,
    never forwarded to anything real. ``mode`` is stamped by ``ShadowRunner``
    (``"shadow"`` or ``"paper"``), overriding whatever the ``compute``
    callable itself set.
    """

    mode: str
    intents: tuple[dict[str, Any], ...]
    decisions: tuple[dict[str, Any], ...]
    registered_intents: tuple[str, ...]
    intercepted_registrations: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "intents": list(self.intents),
            "decisions": list(self.decisions),
            "registered_intents": list(self.registered_intents),
            "intercepted_registrations": list(self.intercepted_registrations),
        }


@dataclass(frozen=True)
class ShadowComparisonResult:
    """The result of comparing a shadow trace to a paper trace over the
    same sealed input. ``divergences`` mirrors
    ``trader.automation.replay.Divergence`` shape (kind/path/expected/actual
    dicts) for consistency with the other P3/P4 replay-style comparisons."""

    matched: bool
    divergences: tuple[dict[str, Any], ...]
    shadow_trace: ShadowTrace
    paper_trace: ShadowTrace

    def to_payload(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "divergences": list(self.divergences),
            "shadow_trace": self.shadow_trace.to_payload(),
            "paper_trace": self.paper_trace.to_payload(),
        }


def _diff_sequences(kind: str, expected: Sequence[Mapping[str, Any]],
                    actual: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    divergences: list[dict[str, Any]] = []
    if len(expected) != len(actual):
        divergences.append({
            "kind": kind, "path": f"{kind}.length",
            "expected": len(expected), "actual": len(actual),
        })
        return divergences
    for idx, (left, right) in enumerate(zip(expected, actual)):
        if dict(left) != dict(right):
            divergences.append({
                "kind": kind, "path": f"{kind}[{idx}]",
                "expected": dict(left), "actual": dict(right),
            })
    return divergences


def _diff_registrations(shadow_would_register: Sequence[str],
                        paper_registered: Sequence[str]) -> list[dict[str, Any]]:
    expected = tuple(sorted(shadow_would_register))
    actual = tuple(sorted(paper_registered))
    if expected == actual:
        return []
    return [{
        "kind": "registration", "path": "registered_intents",
        "expected": list(expected), "actual": list(actual),
    }]


class ShadowRunner:
    """Runs one ``compute`` pipeline in shadow mode and/or paper mode over
    the same sealed input, and can compare the two traces.

    ``compute`` has the signature
    ``compute(sealed_input, execute_automated_intent) -> ShadowTrace`` --
    the SAME callable is used for both modes (it computes decisions and, for
    any it approves, calls through the injected execute callback -- exactly
    mirroring the real automated pipeline, where deciding and submitting
    share one code path); only the injected callback differs.

    In shadow mode the callback is always an in-memory interceptor that
    records the attempted call and returns a synthetic result WITHOUT ever
    invoking anything real -- there is no code path by which the caller's
    real ``execute_automated_intent`` (e.g. the one passed to ``compare``)
    can be reached from ``run_shadow``, so a strategy that decides to trade
    can never place a shadow order, and a full session's shadow run always
    completes (it never raises just because the strategy wanted to trade).
    In paper mode the callback is a tracking wrapper around whatever real
    ``execute_automated_intent`` the caller supplies.
    """

    def __init__(self, compute: Callable[[Any, Callable[..., Any]], ShadowTrace]):
        self._compute = compute

    def run_shadow(self, sealed_input: Any) -> ShadowTrace:
        intercepted: list[str] = []

        def _intercepting_execute(*args: Any, **kwargs: Any) -> Any:
            intent_id = kwargs.get("intent_id")
            if intent_id is None and args:
                intent_id = args[0]
            intercepted.append(str(intent_id))
            return {"shadow_intercepted": True, "intent_id": intent_id}

        trace = self._compute(sealed_input, _intercepting_execute)
        return replace(
            trace, mode="shadow", registered_intents=(),
            intercepted_registrations=tuple(intercepted),
        )

    def run_paper(self, sealed_input: Any, execute_automated_intent: Callable[..., Any]) -> ShadowTrace:
        registered: list[str] = []

        def _tracking_execute(*args: Any, **kwargs: Any) -> Any:
            result = execute_automated_intent(*args, **kwargs)
            intent_id = kwargs.get("intent_id")
            if intent_id is None and args:
                intent_id = args[0]
            registered.append(str(intent_id))
            return result

        trace = self._compute(sealed_input, _tracking_execute)
        return replace(
            trace, mode="paper", registered_intents=tuple(registered),
            intercepted_registrations=(),
        )

    def compare(
        self, sealed_input: Any, execute_automated_intent: Callable[..., Any],
    ) -> ShadowComparisonResult:
        shadow_trace = self.run_shadow(sealed_input)
        paper_trace = self.run_paper(sealed_input, execute_automated_intent)

        divergences = _diff_sequences("intent", shadow_trace.intents, paper_trace.intents)
        divergences += _diff_sequences("decision", shadow_trace.decisions, paper_trace.decisions)
        divergences += _diff_registrations(
            shadow_trace.intercepted_registrations, paper_trace.registered_intents,
        )

        return ShadowComparisonResult(
            matched=not divergences,
            divergences=tuple(divergences),
            shadow_trace=shadow_trace,
            paper_trace=paper_trace,
        )
