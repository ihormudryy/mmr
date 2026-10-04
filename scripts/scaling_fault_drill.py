#!/usr/bin/env python3
"""P5 Task 9 — scaling fault drill (offline synthetic checks)."""
from __future__ import annotations

import argparse
import json
import sys

from trader.promotion.degradation_monitor import DegradationAction, DegradationMonitor
from trader.promotion.evidence_store import EvidenceWindow
from trader.promotion.portfolio_risk_budget import PortfolioRiskBudget


def _empty_window(strategy_id: str) -> EvidenceWindow:
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    return EvidenceWindow(
        strategy_id=strategy_id,
        as_of=now,
        window_reset_at=None,
        first_event_at=now,
        last_event_at=now,
        calendar_days=(),
        session_ids=(),
        round_trip_ids=(),
        instrument_ids=(),
        corrections=(),
        breaker_trips=(),
        cost_breaches=(),
        drawdown_breaches=(),
        stale=False,
        event_count=0,
        round_trip_records=(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline scaling fault drills")
    parser.add_argument("--strategy-id", default="orb_breakout")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    window = _empty_window(args.strategy_id)
    window_with_breaker = EvidenceWindow(
        **{**window.__dict__, "breaker_trips": ({"incident_id": "b1"},)}
    )
    degrade = DegradationMonitor().evaluate(window_with_breaker, "SCALE_2")
    risk = PortfolioRiskBudget().evaluate(
        intents=[{"proposed_gross": 0.10, "projected_daily_loss": 0.006}],
        broker_snapshot={"positions": [], "gross_exposure": 0.0, "daily_loss_pct": 0.0},
        authorities=[{"max_gross_allocation": 0.06}],
    )

    report = {
        "degradation": degrade.to_payload(),
        "portfolio_risk": risk.to_payload(),
        "passed": degrade.action == DegradationAction.SUSPEND and not risk.passed,
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("Scaling fault drill:")
        print(f"  degradation action: {degrade.action.value}")
        print(f"  portfolio risk passed: {risk.passed}")
        print(f"  overall fail-closed: {report['passed']}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
