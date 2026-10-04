"""P5 Task 9 — scaling vertical slice integration tests."""
from __future__ import annotations

import datetime as dt

import numpy as np

from trader.promotion.allocation_attestation import STAGE_CANARY, STAGE_SCALE_1, STAGE_SCALE_2
from trader.promotion.capacity import CapacityMonitor
from trader.promotion.degradation_monitor import DegradationAction, DegradationMonitor
from trader.promotion.evidence_store import EvidenceWindow
from trader.promotion.portfolio_admission import PortfolioAdmissionGate
from trader.promotion.portfolio_risk_budget import PortfolioRiskBudget
from trader.promotion.scaling_gate import ScalingGate
from trader.promotion.stage import CANARY_PASSED

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 20, tzinfo=UTC)
STRATEGY = "orb_breakout"


def _records(n_sessions: int, n_trips: int):
    records = []
    idx = 0
    instruments = [str(1000 + i) for i in range(5)]
    regimes = ["trend", "range", "volatile", "squeeze", "reversal"]
    start = NOW - dt.timedelta(days=40)
    for s in range(n_sessions):
        session_ts = start + dt.timedelta(days=s)
        sid = f"s{s:02d}"
        per = max(1, n_trips // n_sessions)
        for _ in range(per):
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


def _window(records):
    return EvidenceWindow(
        strategy_id=STRATEGY,
        as_of=NOW,
        window_reset_at=None,
        first_event_at=NOW - dt.timedelta(days=40),
        last_event_at=NOW,
        calendar_days=tuple(),
        session_ids=tuple(sorted({r["session_id"] for r in records})),
        round_trip_ids=tuple(r["round_trip_id"] for r in records),
        instrument_ids=("265598",),
        corrections=(),
        breaker_trips=(),
        cost_breaches=(),
        drawdown_breaches=(),
        stale=False,
        event_count=len(records),
        round_trip_records=tuple(records),
    )


def test_canary_to_scale1_evidence_gate():
    records = _records(20, 50)
    window = _window(records)
    decision = ScalingGate().evaluate(
        promotion_stage=CANARY_PASSED,
        current_allocation_stage=STAGE_CANARY,
        window=window,
        target_stage=STAGE_SCALE_1,
    )
    assert decision.passed is True


def test_capacity_degradation_fail_closed():
    sparse = {"records": [{"depth_available": False}]}
    cap = CapacityMonitor().evaluate(sparse, {"265598": 1000.0})
    assert cap.passed is False

    degrade = DegradationMonitor().evaluate(
        _window(_records(5, 5)), STAGE_SCALE_2, capacity_breach_signals=({"metric": "participation_rate"},),
    )
    assert degrade.action == DegradationAction.WARN


def test_portfolio_admission_rejects_insufficient_covariance():
    cov = np.eye(2) * 0.0001
    decision = PortfolioAdmissionGate().evaluate(
        first_strategy_stage=STAGE_SCALE_2,
        second_strategy_stage=CANARY_PASSED,
        covariance_matrix=cov,
        factor_exposures={"s1": {"market": 0.5}, "s2": {"market": 0.4}},
        signed_allocations=[0.08, 0.04],
        covariance_window=5,
    )
    assert decision.passed is False


def test_portfolio_risk_preserves_daily_loss_ceiling():
    decision = PortfolioRiskBudget().evaluate(
        intents=[{"proposed_gross": 0.03, "projected_daily_loss": 0.003}],
        broker_snapshot={"positions": [], "gross_exposure": 0.03, "daily_loss_pct": 0.003},
        authorities=[{"max_gross_allocation": 0.15}],
    )
    assert decision.passed is False
