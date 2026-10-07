"""Pure scoreboard metrics (Plan 5 ruling 15). ``None`` means unknown, never 0."""
from __future__ import annotations

import math
import statistics
from typing import Any, Mapping, Optional, Sequence

SMALL_SAMPLE_SESSIONS = 60
TRADING_DAYS = 252
EOD_DRAWDOWN_LABEL = "drawdown from end-of-day equity; intraday lows may be missed"
SPY_LABEL = "SPY price only; dividends excluded"
SPLIT_KEYS = ("strategy_version", "decider", "style")
UNATTRIBUTED = "unattributed"


def session_returns(rows: Sequence[Mapping[str, Any]]) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    for row in rows:
        start, end = row.get("start_nlv_usd"), row.get("end_nlv_usd")
        out.append(None if start is None or end is None or start <= 0 else end / start - 1)
    return out


def daily_sharpe(returns: Sequence[float]) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Mean / sample stdev of per-session returns, plain and x sqrt(252); unknown below 2 or at zero stdev."""
    warning = "SMALL_SAMPLE" if len(returns) < SMALL_SAMPLE_SESSIONS else None
    if len(returns) < 2:
        return None, None, warning
    deviation = statistics.stdev(returns)
    if not math.isfinite(deviation) or deviation <= 1e-15:
        return None, None, warning
    daily = statistics.fmean(returns) / deviation
    return daily, daily * math.sqrt(TRADING_DAYS), warning


def eod_drawdown_pct(values: Sequence[float]) -> Optional[float]:
    """The largest fall from a running peak of end-of-day values, in percent."""
    if len(values) < 2:
        return None
    peak, worst = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100)
    return worst


def trip_metrics(trips: Sequence[Mapping[str, Any]], *, start_nlv_usd: Optional[float]) -> dict:
    closed = [t for t in trips if t["status"] == "CLOSED"]
    known = [t["net_pnl_usd"] for t in closed if t["net_pnl_usd"] is not None]
    unresolved = len(closed) - len(known)
    wins = [net for net in known if net > 0]
    losses = [net for net in known if net < 0]
    fees_complete = all(t["fees_usd"] is not None for t in trips)
    notional = sum(t["notional_traded_usd"] for t in trips)
    return {
        "closed": len(closed),
        "open": len(trips) - len(closed),
        "unresolved_fee_trips": unresolved,
        "net_pnl_usd": sum(known) if unresolved == 0 else None,
        "net_pnl_complete": unresolved == 0,
        "fees_usd": sum(t["fees_usd"] for t in trips) if fees_complete else None,
        "fees_complete": fees_complete,
        "win_rate": len(wins) / len(known) if known else None,
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "turnover": None if start_nlv_usd is None or start_nlv_usd <= 0 else notional / start_nlv_usd,
    }


def group_trip_metrics(trips: Sequence[Mapping[str, Any]], key: str, *,
                       start_nlv_usd: Optional[float]) -> dict[str, dict]:
    if key not in SPLIT_KEYS:
        raise ValueError(f"split key must be one of {SPLIT_KEYS}, got {key!r}")
    groups: dict[str, list] = {}
    for trip in trips:
        groups.setdefault(trip.get(key) or UNATTRIBUTED, []).append(trip)
    return {name: trip_metrics(group, start_nlv_usd=start_nlv_usd) for name, group in sorted(groups.items())}
