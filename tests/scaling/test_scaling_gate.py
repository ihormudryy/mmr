"""P5 Task 3 — scaling gate evidence decisions."""
from __future__ import annotations

import datetime as dt

import pytest

from trader.promotion.allocation_attestation import STAGE_SCALE_1, STAGE_SCALE_2, STAGE_STEADY
from trader.promotion.evidence_store import EvidenceWindow
from trader.promotion.scaling_gate import (
    BLOCK_CAPACITY_REVIEW,
    BLOCK_INCREMENTAL_FLOORS,
    BLOCK_PROMOTION_STAGE,
    FLOOR_ROUND_TRIPS_AFTER_SCALE_1,
    FLOOR_SESSIONS_AFTER_SCALE_1,
    ScalingGate,
    truncate_window_since,
)
from trader.promotion.stage import CANARY_PASSED, PAPER_PASSED

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
STRATEGY = "orb_breakout"
AUTH_START = NOW - dt.timedelta(days=25)


def _rt(idx: int, *, session: str, ts: dt.datetime, pnl: float = 10.0) -> dict:
    return {
        "round_trip_id": f"rt-{idx}",
        "session_id": session,
        "instrument_id": str(1000 + (idx % 5)),
        "regime_id": "trend",
        "pnl_after_cost": pnl,
        "resolved": True,
        "slippage_bps": 2.0,
        "within_prediction_envelope": True,
        "source_timestamp": ts.isoformat().replace("+00:00", "Z"),
    }


def _window(*, records, sessions=None, promotion_as_of=NOW) -> EvidenceWindow:
    sessions = sessions or sorted({r["session_id"] for r in records})
    return EvidenceWindow(
        strategy_id=STRATEGY,
        as_of=promotion_as_of,
        window_reset_at=None,
        first_event_at=AUTH_START,
        last_event_at=NOW,
        calendar_days=tuple(sorted({ts[:10] for r in records for ts in [r["source_timestamp"]]})),
        session_ids=tuple(sessions),
        round_trip_ids=tuple(r["round_trip_id"] for r in records),
        instrument_ids=tuple(sorted({r["instrument_id"] for r in records})),
        corrections=(),
        breaker_trips=(),
        cost_breaches=(),
        drawdown_breaches=(),
        stale=False,
        event_count=len(records),
        round_trip_records=tuple(records),
    )


def _records_since(start: dt.datetime, n_sessions: int, n_trips: int):
    records = []
    idx = 0
    instruments = [str(1000 + i) for i in range(5)]
    regimes = ["trend", "range", "volatile", "squeeze", "reversal"]
    for s in range(n_sessions):
        session_ts = start + dt.timedelta(days=s)
        sid = f"s{s:02d}"
        per = max(1, n_trips // n_sessions)
        for j in range(per):
            k = idx % 5
            records.append({
                "round_trip_id": f"rt-{idx}",
                "session_id": sid,
                "instrument_id": instruments[k],
                "regime_id": regimes[k],
                "pnl_after_cost": 9.0 + 0.5 * k,
                "resolved": True,
                "slippage_bps": 2.0,
                "within_prediction_envelope": True,
                "source_timestamp": (session_ts + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            })
            idx += 1
    while len(records) < n_trips:
        k = idx % 5
        records.append({
            "round_trip_id": f"rt-{idx}",
            "session_id": f"s{n_sessions-1:02d}",
            "instrument_id": instruments[k],
            "regime_id": regimes[k],
            "pnl_after_cost": 9.0 + 0.5 * k,
            "resolved": True,
            "slippage_bps": 2.0,
            "within_prediction_envelope": True,
            "source_timestamp": (start + dt.timedelta(days=n_sessions)).isoformat().replace("+00:00", "Z"),
        })
        idx += 1
    return records


def test_scale_1_requires_canary_passed_promotion_stage():
    window = _window(records=_records_since(NOW - dt.timedelta(days=40), 20, 50))
    decision = ScalingGate().evaluate(
        promotion_stage=PAPER_PASSED,
        current_allocation_stage="CANARY",
        window=window,
        target_stage=STAGE_SCALE_1,
    )
    assert decision.passed is False
    assert BLOCK_PROMOTION_STAGE in decision.blockers


def test_scale_1_passes_with_canary_passed_and_clean_metrics():
    window = _window(records=_records_since(NOW - dt.timedelta(days=40), 20, 50))
    decision = ScalingGate().evaluate(
        promotion_stage=CANARY_PASSED,
        current_allocation_stage="CANARY",
        window=window,
        target_stage=STAGE_SCALE_1,
    )
    assert decision.passed is True


def test_scale_2_requires_incremental_floors_after_authority_start():
    authority_start = NOW - dt.timedelta(days=30)
    all_records = _records_since(authority_start, 25, 55)
    window = _window(records=all_records)
    decision = ScalingGate().evaluate(
        promotion_stage=CANARY_PASSED,
        current_allocation_stage=STAGE_SCALE_1,
        window=window,
        target_stage=STAGE_SCALE_2,
        authority_started_at=authority_start,
    )
    assert decision.passed is True
    assert decision.sessions_since_authority >= FLOOR_SESSIONS_AFTER_SCALE_1
    assert decision.round_trips_since_authority >= FLOOR_ROUND_TRIPS_AFTER_SCALE_1


def test_evidence_before_authority_start_not_double_counted():
    old_records = _records_since(NOW - dt.timedelta(days=90), 20, 50)
    new_records = _records_since(AUTH_START, 5, 10)
    window = _window(records=old_records + new_records)
    decision = ScalingGate().evaluate(
        promotion_stage=CANARY_PASSED,
        current_allocation_stage=STAGE_SCALE_1,
        window=window,
        target_stage=STAGE_SCALE_2,
        authority_started_at=AUTH_START,
    )
    assert decision.passed is False
    assert BLOCK_INCREMENTAL_FLOORS in decision.blockers
    assert decision.round_trips_since_authority == len(truncate_window_since(window, AUTH_START).round_trip_records)


def test_steady_requires_capacity_review():
    window = _window(records=_records_since(NOW - dt.timedelta(days=40), 20, 50))
    decision = ScalingGate().evaluate(
        promotion_stage=CANARY_PASSED,
        current_allocation_stage=STAGE_SCALE_2,
        window=window,
        target_stage=STAGE_STEADY,
        capacity_review_passed=False,
    )
    assert decision.passed is False
    assert BLOCK_CAPACITY_REVIEW in decision.blockers


def test_steady_passes_with_capacity_review():
    window = _window(records=_records_since(NOW - dt.timedelta(days=40), 20, 50))
    decision = ScalingGate().evaluate(
        promotion_stage=CANARY_PASSED,
        current_allocation_stage=STAGE_SCALE_2,
        window=window,
        target_stage=STAGE_STEADY,
        capacity_review_passed=True,
    )
    assert decision.passed is True
