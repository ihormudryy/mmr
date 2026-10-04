"""P5 Task 3 — deliberate allocation scaling evidence gate."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional, Tuple

from trader.promotion.allocation_attestation import STAGE_CANARY, STAGE_SCALE_1, STAGE_SCALE_2, STAGE_STEADY
from trader.promotion.evidence_store import EvidenceWindow
from trader.promotion.live_metrics import LiveMetrics, MetricDecision
from trader.promotion.stage import CANARY_PASSED

FLOOR_SESSIONS_AFTER_SCALE_1 = 20
FLOOR_ROUND_TRIPS_AFTER_SCALE_1 = 50

BLOCK_PROMOTION_STAGE = "promotion_stage_not_ready"
BLOCK_ALLOCATION_STAGE = "allocation_stage_mismatch"
BLOCK_CAPACITY_REVIEW = "capacity_review_required"
BLOCK_INCREMENTAL_FLOORS = "incremental_floors_not_met"
BLOCK_LIVE_METRICS = "live_metrics_blocked"
BLOCK_EVIDENCE_TRUNCATED_EMPTY = "no_evidence_since_authority"


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _parse_ts(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return _as_utc(value)
    return _as_utc(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _record_at_or_after(record: Mapping[str, Any], since: dt.datetime) -> bool:
    for key in ("source_timestamp", "closed_at", "recorded_at"):
        raw = record.get(key)
        if raw is None:
            continue
        try:
            return _parse_ts(raw) >= since
        except (TypeError, ValueError):
            continue
    return False


def truncate_window_since(window: EvidenceWindow, since: dt.datetime) -> EvidenceWindow:
    boundary = _as_utc(since)
    filtered_records = tuple(r for r in window.round_trip_records if _record_at_or_after(r, boundary))
    filtered_sessions = tuple(sorted({
        str(r["session_id"]) for r in filtered_records if r.get("session_id")
    }))
    filtered_round_trips = tuple(sorted({
        str(r.get("round_trip_id") or r.get("trade_id"))
        for r in filtered_records
        if r.get("round_trip_id") or r.get("trade_id")
    }))
    filtered_calendar = tuple(d for d in window.calendar_days if d >= boundary.date().isoformat())
    return replace(
        window,
        calendar_days=filtered_calendar,
        session_ids=filtered_sessions,
        round_trip_ids=filtered_round_trips,
        round_trip_records=filtered_records,
    )


def scaling_evidence_digest(window: EvidenceWindow) -> str:
    from trader.research.canonical import sha256_digest
    return sha256_digest("p5_scaling_evidence_window", window.to_payload())


@dataclass(frozen=True)
class ScalingDecision:
    passed: bool
    target_stage: str
    blockers: Tuple[str, ...]
    metrics: Mapping[str, Any]
    evidence_digest: str
    sessions_since_authority: int
    round_trips_since_authority: int
    live_metrics: Optional[MetricDecision] = None


class ScalingGate:
    def __init__(self, *, live_metrics: Optional[LiveMetrics] = None):
        self._live_metrics = live_metrics or LiveMetrics()

    def evaluate(
        self,
        *,
        promotion_stage: str,
        current_allocation_stage: Optional[str],
        window: EvidenceWindow,
        target_stage: str,
        authority_started_at: Optional[dt.datetime] = None,
        capacity_review_passed: bool = False,
    ) -> ScalingDecision:
        blockers: list[str] = []
        eval_window = window
        sessions_since = window.session_count
        round_trips_since = window.round_trip_count

        if target_stage == STAGE_SCALE_1:
            if promotion_stage != CANARY_PASSED:
                blockers.append(BLOCK_PROMOTION_STAGE)
            if current_allocation_stage not in (None, STAGE_CANARY):
                blockers.append(BLOCK_ALLOCATION_STAGE)
        elif target_stage == STAGE_SCALE_2:
            if current_allocation_stage != STAGE_SCALE_1:
                blockers.append(BLOCK_ALLOCATION_STAGE)
            elif authority_started_at is None:
                blockers.append(BLOCK_INCREMENTAL_FLOORS)
            else:
                eval_window = truncate_window_since(window, authority_started_at)
                sessions_since = eval_window.session_count
                round_trips_since = eval_window.round_trip_count
                if sessions_since < FLOOR_SESSIONS_AFTER_SCALE_1 or round_trips_since < FLOOR_ROUND_TRIPS_AFTER_SCALE_1:
                    blockers.append(BLOCK_INCREMENTAL_FLOORS)
                if not eval_window.round_trip_records:
                    blockers.append(BLOCK_EVIDENCE_TRUNCATED_EMPTY)
        elif target_stage == STAGE_STEADY:
            if current_allocation_stage != STAGE_SCALE_2:
                blockers.append(BLOCK_ALLOCATION_STAGE)
            if not capacity_review_passed:
                blockers.append(BLOCK_CAPACITY_REVIEW)
            if authority_started_at is not None:
                eval_window = truncate_window_since(window, authority_started_at)
                sessions_since = eval_window.session_count
                round_trips_since = eval_window.round_trip_count
        else:
            blockers.append(BLOCK_ALLOCATION_STAGE)

        metrics_decision = self._live_metrics.evaluate(eval_window)
        if not metrics_decision.passed:
            blockers.append(BLOCK_LIVE_METRICS)
            blockers.extend(metrics_decision.blockers)

        blockers = tuple(dict.fromkeys(blockers))
        return ScalingDecision(
            passed=not blockers,
            target_stage=target_stage,
            blockers=blockers,
            metrics={
                "sessions_since_authority": sessions_since,
                "round_trips_since_authority": round_trips_since,
                **dict(metrics_decision.metrics),
            },
            evidence_digest=scaling_evidence_digest(eval_window),
            sessions_since_authority=sessions_since,
            round_trips_since_authority=round_trips_since,
            live_metrics=metrics_decision,
        )
