"""The live OCA shrink proof (Plan 6 ruling 13, spec 5.1).

After the trader's probe set S's target to the bid with a display size of 1, the
harness reads the broker-order evidence every 0.5 s for at most 60 s and judges
each reading on its own: one promoted generation, its recorded status events
and the position. Only recorded events count; a transition that was not
recorded (a cursor gap, a generation change) is unobservable, so UNPROVEN.

- PROVEN: a target event E with 0 < filled < quantity and a later stop event F
  (same generation, F.cursor > E.cursor) whose remaining is quantity - E.filled,
  both legs in one non-empty OCA group of type 2, and the position matching
  the latest recorded target fill.
- FAILED (OCA_SIBLING_NOT_SHRUNK): a partial fill is recorded, the position
  confirms it, and the stop still does not carry the residual 5 s later (or at
  the end of the window).
- UNPROVEN (OCA_SHRINK_UNPROVEN): the target filled whole in one event, or
  nothing partial was recorded within 60 s.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional

from trader.acceptance.order_status import TERMINAL_STATUSES

WINDOW_SECONDS = 60.0
POLL_SECONDS = 0.5
FAIL_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class Verdict:
    result: str                     # PROVEN | FAILED | UNPROVEN | PENDING
    code: Optional[str]
    pair: Optional[dict] = None


def _legs(reading: dict, legs: dict) -> tuple[Optional[dict], Optional[dict]]:
    rows = {row.get("order_entity_id"): row for row in reading.get("orders") or []}
    return rows.get(legs["take_profit"]), rows.get(legs["stop"])


def judge(reading: dict, legs: dict, quantity: float, position: Optional[float]) -> Verdict:
    """One reading: PROVEN, FAILED (OCA type lost), or PENDING. Time-based outcomes are the caller's."""
    target, stop = _legs(reading, legs)
    if target is None or stop is None:
        return Verdict("PENDING", None)
    for row in (target, stop):
        if row.get("oca_type") != 2 or not row.get("oca_group"):
            return Verdict("FAILED", "OCA_TYPE_NOT_2")
    if target.get("oca_group") != stop.get("oca_group") or reading.get("events_gapless") is not True:
        return Verdict("PENDING", None)
    target_events = sorted(target.get("status_events") or [], key=lambda e: e["cursor"])
    stop_events = sorted(stop.get("status_events") or [], key=lambda e: e["cursor"])
    if not target_events:
        return Verdict("PENDING", None)
    latest_fill = max(e["filled_quantity"] for e in target_events)
    for event in target_events:
        if not 0 < event["filled_quantity"] < quantity or event.get("oca_type") not in (2, None):
            continue
        residual = quantity - event["filled_quantity"]
        shrunk = next((f for f in stop_events
                       if f["cursor"] > event["cursor"] and f["remaining_quantity"] == residual
                       and f.get("oca_type") in (2, None)), None)
        if shrunk is not None and position == quantity - latest_fill:
            return Verdict("PROVEN", None, {"generation_id": reading.get("generation_id"),
                                            "target_event": event, "stop_event": shrunk, "position": position})
    return Verdict("PENDING", None)


def partial_recorded(reading: dict, legs: dict, quantity: float) -> Optional[float]:
    """The residual of the latest recorded partial target fill, or None."""
    target, _ = _legs(reading, legs)
    fills = [e["filled_quantity"] for e in (target or {}).get("status_events") or []
             if 0 < e["filled_quantity"] < quantity]
    return quantity - max(fills) if fills else None


def stop_remaining(reading: dict, legs: dict) -> Optional[float]:
    _, stop = _legs(reading, legs)
    if stop is None:
        return None
    events = sorted(stop.get("status_events") or [], key=lambda e: e["cursor"])
    return events[-1]["remaining_quantity"] if events else stop.get("remaining_quantity")


def whole_fill_without_partial(reading: dict, legs: dict, quantity: float) -> bool:
    target, stop = _legs(reading, legs)
    if target is None:
        return False
    events = target.get("status_events") or []
    partial = any(0 < e["filled_quantity"] < quantity for e in events)
    filled = target.get("status") == "Filled" or any(e["filled_quantity"] >= quantity for e in events)
    stop_done = stop is None or stop.get("status") in TERMINAL_STATUSES
    return filled and not partial and stop_done


def reading_digest(reading: dict) -> str:
    return hashlib.sha256(json.dumps(reading, sort_keys=True, default=str).encode()).hexdigest()


def run_shrink_proof(port: Any, settings: Any, journal: Any, *, legs: dict, conid: int,
                     window: float = WINDOW_SECONDS, poll: float = POLL_SECONDS):
    """Poll the evidence and decide. Returns the ``shrink_proof`` StepResult."""
    from trader.acceptance.scenario import StepResult, held_quantity

    quantity = float(settings.quantity_s)
    started = port.now()
    seen: set[str] = set()
    readings: list[dict] = []
    unshrunk_since = None
    while True:
        reading = port.evidence(conid)
        if reading.get("capture_error"):
            verdict = Verdict("PENDING", None)       # a staging generation is ignored, not counted
            position = None
        else:
            position = held_quantity(port, conid)
            digest = reading_digest(reading)
            if digest not in seen:
                seen.add(digest)
                readings.append(reading)
                journal.append("reading", {"step": "shrink_proof", "generation_id": reading.get("generation_id"),
                                           "position": position, "evidence": reading})
            verdict = judge(reading, legs, quantity, position)
        elapsed = (port.now() - started).total_seconds()
        if verdict.result == "PROVEN":
            return StepResult("shrink_proof", True, None, {"oca_shrink": "PROVEN", "pair": verdict.pair,
                                                           "readings": len(readings)})
        if verdict.result == "FAILED":
            return StepResult("shrink_proof", False, verdict.code, {"oca_shrink": "FAILED", "readings": len(readings)})
        if not reading.get("capture_error"):
            residual = partial_recorded(reading, legs, quantity)
            if residual is not None and position == residual and stop_remaining(reading, legs) != residual:
                unshrunk_since = unshrunk_since or port.now()
                if (port.now() - unshrunk_since).total_seconds() >= FAIL_GRACE_SECONDS or elapsed >= window:
                    return StepResult("shrink_proof", False, "OCA_SIBLING_NOT_SHRUNK",
                                      {"oca_shrink": "FAILED", "residual": residual,
                                       "stop_remaining": stop_remaining(reading, legs), "readings": len(readings)})
            else:
                unshrunk_since = None
            if whole_fill_without_partial(reading, legs, quantity):
                return StepResult("shrink_proof", False, "OCA_SHRINK_UNPROVEN",
                                  {"oca_shrink": "UNPROVEN", "why": "whole fill in one event",
                                   "readings": len(readings)})
        if elapsed >= window:
            return StepResult("shrink_proof", False, "OCA_SHRINK_UNPROVEN",
                              {"oca_shrink": "UNPROVEN", "why": "no recorded partial fill and shrink in the window",
                               "readings": len(readings)})
        port.sleep(poll)
