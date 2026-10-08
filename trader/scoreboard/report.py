"""The scoreboard report: one pure function over stored inputs (spec 5.2, Plan 5 Task 5).

JSON-safe. ``None`` is unknown and stays ``None``; counts may be 0. Money is
rounded only here, at the edge.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from trader.scoreboard.books import build_books, build_shadow_books, summarize_costs
from trader.scoreboard.metrics import (EOD_DRAWDOWN_LABEL, SPLIT_KEYS, SPY_LABEL, daily_sharpe, eod_drawdown_pct,
                                       group_trip_metrics, session_returns, trip_metrics)
from trader.scoreboard.ports import session_date_et

LABEL = "PAPER"
DISCLAIMER = "Paper trading. Nothing here is proof of live edge."


@dataclass(frozen=True)
class ReportInputs:
    experiment: Any
    rows: Sequence[Mapping[str, Any]]
    adjustments: Sequence[Mapping[str, Any]]
    trips: Sequence[Mapping[str, Any]]
    spy_closes: Mapping[dt.date, float]
    spy_version: Optional[int]
    spy_provider: str
    ai_costs: Sequence[Mapping[str, Any]]          # every ai_costs row: originals and corrections
    sim_decisions: Sequence[Mapping[str, Any]]
    sim_outcomes: Sequence[Mapping[str, Any]]
    incidents: Sequence[Mapping[str, Any]]
    warnings: Sequence[Mapping[str, Any]]
    outbox: Optional[Mapping[str, Any]]
    calendar: Any                 # previous_session(day) for the SPY base (ruling 14)
    shadow_rows: Sequence[Mapping[str, Any]] = ()   # SP2c Plan 3: forward replay rows, global (not per experiment)


def _round(value: Any) -> Any:
    return round(value, 6) if isinstance(value, float) else value


def _clean(value: Any) -> Any:
    """Dates to ISO text, floats rounded; the rest as is."""
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return _round(value)


def _pct(end: Optional[float], start: Optional[float]) -> Optional[float]:
    if end is None or start is None or start <= 0:
        return None
    return (end / start - 1) * 100


def _session_commissions(row: Mapping[str, Any], adjustments: Sequence[Mapping[str, Any]]) -> Optional[float]:
    """Stored per-fill commissions plus later adjustments; unknown while any fill's fee is still unknown."""
    stored = json.loads(row["commission_json"])
    adjusted: dict[str, float] = defaultdict(float)
    for adjustment in adjustments:
        if adjustment["session_date"] == row["session_date"]:
            adjusted[adjustment["exec_id"]] += adjustment["amount_usd"]
    if any(fee is None and exec_id not in adjusted for exec_id, fee in stored.items()):
        return None
    return sum(fee or 0.0 for fee in stored.values()) + sum(adjusted.values())


def _experiment_view(experiment: Any) -> Optional[dict]:
    if experiment is None:
        return None
    return {"id": experiment.experiment_id, "state": experiment.state, "started_at": experiment.started_at,
            "base_currency": experiment.base_currency}


def _start_usd(experiment: Any) -> Optional[float]:
    if experiment is None or experiment.start_net_liquidation is None or experiment.start_usd_per_base is None:
        return None
    return experiment.start_net_liquidation * experiment.start_usd_per_base


def _account(inputs: ReportInputs, rows: list) -> dict:
    start = _start_usd(inputs.experiment)
    known = [r for r in rows if r["end_nlv_usd"] is not None]
    end = known[-1]["end_nlv_usd"] if known else None
    returns = [r for r in session_returns(rows) if r is not None]
    sharpe, annualised, warning = daily_sharpe(returns)
    curve = ([start] if start is not None else []) + [r["end_nlv_usd"] for r in known]
    return {
        "sessions": len(rows), "start_nlv_usd": start, "end_nlv_usd": end,
        "end_date": known[-1]["session_date"] if known else None,
        "pnl_usd": None if start is None or end is None else end - start,
        "return_pct": _pct(end, start),
        "eod_drawdown_pct": eod_drawdown_pct(curve), "eod_drawdown_label": EOD_DRAWDOWN_LABEL,
        "sharpe_daily": sharpe, "sharpe_annualised": annualised, "sharpe_warning": warning,
        "unknown_nlv_sessions": len(rows) - len(known),
    }


def _spy(inputs: ReportInputs, end_date: Optional[dt.date]) -> dict:
    base_date = None
    if inputs.experiment is not None:
        base_date = inputs.calendar.previous_session(session_date_et(inputs.experiment.started_at))
    base = inputs.spy_closes.get(base_date) if base_date is not None else None
    last = inputs.spy_closes.get(end_date) if end_date is not None else None
    return {"return_pct": _pct(last, base), "base_date": base_date, "last_date": end_date,
            "version": inputs.spy_version, "provider": inputs.spy_provider, "label": SPY_LABEL}


def _benchmarks(inputs: ReportInputs, account: dict) -> dict:
    spy = _spy(inputs, account["end_date"])
    vs_spy = (None if account["return_pct"] is None or spy["return_pct"] is None
              else account["return_pct"] - spy["return_pct"])
    cost = summarize_costs(inputs.ai_costs)
    unavailable = cost["status"] == "NONE"
    pnl = account["pnl_usd"]
    return {"spy": spy, "vs_spy_pp": vs_spy, "books": build_books(inputs.sim_decisions, inputs.sim_outcomes),
            "ai_cost": cost, "ai_cost_usd": cost["total_usd"], "ai_calls": None if unavailable else cost["calls"],
            "ai_costs_status": "UNAVAILABLE" if unavailable else "AVAILABLE",
            "pnl_minus_ai_cost_usd": None if pnl is None or cost["total_usd"] is None else pnl - cost["total_usd"]}


def _session_view(row: Mapping[str, Any], adjustments: Sequence[Mapping[str, Any]]) -> dict:
    return {"date": row["session_date"], "end_state": row["session_end_state"],
            "start_nlv_usd": row["start_nlv_usd"], "end_nlv_usd": row["end_nlv_usd"],
            "return_pct": _pct(row["end_nlv_usd"], row["start_nlv_usd"]),
            "realized_pnl_usd": row["realized_pnl_usd"],
            "commissions_usd": _session_commissions(row, adjustments),
            "peak_gross_exposure_usd": row["peak_gross_exposure_usd"], "trade_count": row["trade_count"],
            "open_positions": row["open_positions"], "start_source": row["start_source"],
            "missing_sessions_before": row["missing_sessions_before"]}


def build_report(inputs: ReportInputs) -> dict:
    rows = sorted(inputs.rows, key=lambda r: r["session_date"])
    account = _account(inputs, rows)
    start = account["start_nlv_usd"]
    outbox = ({"enabled": False, "pending": None, "last_sent_at": None} if inputs.outbox is None
              else {"enabled": True, "pending": inputs.outbox["pending"],
                    "last_sent_at": inputs.outbox["last_sent_at"]})
    report = {
        "label": LABEL, "disclaimer": DISCLAIMER,
        "experiment": _experiment_view(inputs.experiment),
        "account": account,
        "benchmarks": _benchmarks(inputs, account),
        "shadow_books": build_shadow_books(inputs.shadow_rows),
        "trips": trip_metrics(inputs.trips, start_nlv_usd=start),
        "splits": {key: group_trip_metrics(inputs.trips, key, start_nlv_usd=start) for key in SPLIT_KEYS},
        "sessions": [_session_view(row, inputs.adjustments) for row in rows],
        "warnings": list(inputs.warnings),
        "incidents": [{"kind": i["kind"], "key": i["key"], "detail": i["detail"], "recorded_at": i["recorded_at"]}
                      for i in inputs.incidents],
        "outbox": outbox,
    }
    return _clean(report)
