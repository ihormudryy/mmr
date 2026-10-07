"""Baseline decisions for the trader to simulate (SP2 spec 7; index baseline rulings; Plan 2 Rulings 9, 18, 19, 21).

Deterministic code only. The trader sizes follow-signal and fixed-rule; missing evidence is sent incomplete.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Mapping, Optional, Sequence

from trader.ai.discovery_client import EligibleCandidate
from trader.ai.engine import OwnedPosition, SignalOpportunity, SimulatedBaseline
from trader.ai.evidence import PricedEntry

FEED_NOT_ACCEPTED, QUOTE_UNAVAILABLE = "feed_not_accepted", "quote_unavailable"
QUOTE_NOT_EXECUTABLE, RANKING_UNAVAILABLE = "quote_not_executable", "ranking_unavailable"
NOT_EXECUTABLE_CODES = frozenset({"QUOTE_STALE", "QUOTE_INVALID", "QUOTE_NOT_CONTINUOUS", "BRACKET_INVALID"})


def _sized_by_trader(baseline_id: str, cohort: str, opportunity_id: str, priced: PricedEntry,
                     deployment_digest: str) -> SimulatedBaseline:
    return SimulatedBaseline(baseline_id, cohort, opportunity_id, priced.read_at, conid=priced.conid, side="BUY",
                             reference_price=priced.reference_price, stop_price=priced.stop_price,
                             target_price=priced.target_price, deployment_digest=deployment_digest)


def follow_signal(opportunity: SignalOpportunity, priced: PricedEntry, deployment_digest: str) -> SimulatedBaseline:
    """The strategy's BUY at the fresh ask with the strategy bracket, whatever Jev ruled; the trader sizes it."""
    return _sized_by_trader("follow_signal.v1", "strategy_signal", opportunity.opportunity_id, priced,
                            deployment_digest)


def no_trade(cycle_id: str, decided_at: dt.datetime) -> SimulatedBaseline:
    return SimulatedBaseline("no_trade.v1", "self_found", cycle_id, decided_at)


def pick_fixed_rule(eligible: Sequence[EligibleCandidate]) -> Optional[EligibleCandidate]:
    """Highest change_pct; ties by higher 20-session median dollar volume, then symbol."""
    ranked = [c for c in eligible if c.change_pct is not None and math.isfinite(c.change_pct)]
    if not ranked:
        return None
    return min(ranked, key=lambda c: (-c.change_pct, -(c.median_dollar_volume or 0.0), c.symbol))


def fixed_rule(cycle_id: str, priced: PricedEntry, deployment_digest: str) -> SimulatedBaseline:
    return _sized_by_trader("fixed_rule.v1", "self_found", cycle_id, priced, deployment_digest)


def incomplete_reason_for(code: str) -> str:
    """Ruling 11: which Plan 2 reason a failed quote read gives."""
    if code == "QUOTE_FEED_NOT_ACCEPTED":
        return FEED_NOT_ACCEPTED
    return QUOTE_NOT_EXECUTABLE if code in NOT_EXECUTABLE_CODES else QUOTE_UNAVAILABLE


def incomplete(baseline_id: str, cohort: str, opportunity_id: str, decided_at: dt.datetime, *, conid: Optional[int],
               reason: str, deployment_digest: Optional[str] = None) -> SimulatedBaseline:
    """A baseline whose evidence is missing: sent so the book shows the hole, with no side, size or price."""
    return SimulatedBaseline(baseline_id, cohort, opportunity_id, decided_at, conid=conid,
                             deployment_digest=deployment_digest, incomplete_reason=reason)


def matched_entry(position: OwnedPosition, entry_body: Optional[Mapping[str, Any]], *,
                  close_decision_id: str, closed_quantity: int) -> Optional[SimulatedBaseline]:
    """One record per model close: the shares it asked to remove, at the entry price, held with only the
    original stop and target (Plan 2 Ruling 21; the trader clips so a trip never counts more than its entry).

    None when no counterfactual exists (no ENTER decision, entry price or entry body of this experiment)."""
    if entry_body is None or position.decision_id is None or position.entry_price is None:
        return None
    if type(closed_quantity) is not int or closed_quantity < 1:
        return None
    stop, target = entry_body.get("stop_price"), entry_body.get("target_price")
    if type(stop) is not float or type(target) is not float or not stop < position.entry_price < target:
        return None
    try:
        return SimulatedBaseline("matched_entry_bracket_exit.v1", "model_close", close_decision_id,
                                 position.opened_at, conid=position.conid, side="BUY",
                                 quantity=closed_quantity, reference_price=float(position.entry_price),
                                 stop_price=stop, target_price=target, linked_decision_id=position.decision_id,
                                 linked_round_trip_id=position.round_trip_id)
    except ValueError:
        return None                                # e.g. a round_trip_id the opportunity pattern refuses
