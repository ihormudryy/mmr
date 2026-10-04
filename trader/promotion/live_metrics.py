"""P4 Task 3 -- live statistical + concentration evidence gate.

``LiveMetrics.evaluate`` is a pure function of an ``EvidenceWindow``
(``trader.promotion.evidence_store``), mirroring ``PaperGate.evaluate`` from
Task 2: it never touches a database or the clock -- the caller (a promotion
report, an operator workflow, or the future promotion controller) hands it a
freshly *projected* window (``EvidenceStore.project``).

Where ``PaperGate`` answers "has enough clean paper evidence accumulated?",
``LiveMetrics`` answers a stricter question layered on top: "does the
statistical shape of that evidence actually look like a real, non-lucky,
non-concentrated edge?" It consumes the SAME ``round_trip_records`` the
paper gate does, but expects each record to additionally carry the fields
listed below, sourced from authoritative P3 attribution/replay -- never
computed here:

* ``resolved`` (bool) -- only records with ``resolved is True`` (an
  authoritative, fully-resolved P3 ``TradeAttribution``) feed any metric
  below. Every other record is excluded and returned verbatim in
  ``MetricDecision.unresolved_round_trips`` -- never dropped silently, never
  averaged in as if its P&L were zero (mirrors
  ``trader.automation.attribution.PromotionAttributionReport``'s
  resolved/unresolved split).
* ``pnl_after_cost`` -- net P&L for the round trip, after costs (same field
  ``PaperGate`` uses).
* ``session_id`` -- the trading day/session the round trip belongs to (for
  day-profit concentration and daily Sharpe/Sortino).
* ``instrument_id`` -- for instrument-profit concentration.
* ``regime_id`` -- the market-regime tag attributed to the round trip, for
  regime-profit concentration.
* ``slippage_bps`` -- execution slippage in basis points, for average/tail
  slippage.
* ``within_prediction_envelope`` (bool) -- whether the realized outcome fell
  inside the strategy's/backtest's predicted range, for prediction-envelope
  fit.

Fail-closed contract ("missing or negative safety evidence always wins"):
every metric above except Sharpe/Sortino is SAFETY evidence -- if it is
negative/breached OR if the data needed to compute it is missing (a
resolved record lacking a required field, or zero resolved records at all),
that is a blocker, exactly on par with a computed breach. Nothing is ever
skipped-and-ignored; an incomplete-evidence blocker names exactly which
field was missing. Daily Sharpe and Sortino are the one exception: they are
CORROBORATIVE ONLY -- reported in ``metrics`` for context, but never
contribute a blocker, however negative or however undefined (``None``) they
are.

Window-level safety signals from Task 1 (breaker trip, cost breach,
drawdown-breach event, staleness) carry through as blockers too, exactly
like ``PaperGate`` -- one incident is never averaged away by profit or
sample size.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence

from trader.promotion.evidence_store import EvidenceWindow
from trader.promotion.paper_gate import (
    BLOCK_BREAKER_TRIP,
    BLOCK_DRAWDOWN_BREACH,
    BLOCK_STALE_EVIDENCE,
    BLOCK_STRESSED_COST_BREACH,
)

__all__ = [
    "MAX_TRADE_PROFIT_CONCENTRATION", "MAX_DAY_PROFIT_CONCENTRATION",
    "MAX_INSTRUMENT_PROFIT_CONCENTRATION", "MAX_REGIME_PROFIT_CONCENTRATION",
    "MAX_PREDICTION_ENVELOPE_MISS_RATE", "MAX_AVERAGE_SLIPPAGE_BPS",
    "MAX_TAIL_SLIPPAGE_BPS", "MAX_DRAWDOWN_PCT",
    "BLOCK_MISSING_EVIDENCE", "BLOCK_MISSING_NET_EXPECTANCY_EVIDENCE",
    "BLOCK_NEGATIVE_NET_EXPECTANCY", "BLOCK_PREDICTION_ENVELOPE",
    "BLOCK_INCOMPLETE_SLIPPAGE_EVIDENCE", "BLOCK_AVERAGE_SLIPPAGE", "BLOCK_TAIL_SLIPPAGE",
    "BLOCK_DRAWDOWN", "BLOCK_BEST_TRADE_RELIANCE",
    "BLOCK_TRADE_PROFIT_CONCENTRATION", "BLOCK_DAY_PROFIT_CONCENTRATION",
    "BLOCK_INSTRUMENT_PROFIT_CONCENTRATION", "BLOCK_REGIME_PROFIT_CONCENTRATION",
    "BLOCK_INCOMPLETE_SESSION_EVIDENCE", "BLOCK_INCOMPLETE_INSTRUMENT_EVIDENCE",
    "BLOCK_INCOMPLETE_REGIME_EVIDENCE",
    "BLOCK_BREAKER_TRIP", "BLOCK_STRESSED_COST_BREACH", "BLOCK_DRAWDOWN_BREACH", "BLOCK_STALE_EVIDENCE",
    "MetricDecision", "LiveMetrics",
]

# Concentration floors ("anti-luck" gates). 35%/40% are exact per plan; the
# instrument/regime floors reuse the same 40% figure ``PaperGate`` already
# uses for round-trip-count instrument concentration, applied here to
# PROFIT share instead of trip count.
MAX_TRADE_PROFIT_CONCENTRATION = 0.35
MAX_DAY_PROFIT_CONCENTRATION = 0.40
MAX_INSTRUMENT_PROFIT_CONCENTRATION = 0.40
MAX_REGIME_PROFIT_CONCENTRATION = 0.40

# No more than this fraction of resolved round trips may fall outside their
# predicted envelope (a record missing the field entirely counts as a miss).
MAX_PREDICTION_ENVELOPE_MISS_RATE = 0.20

# Slippage floors, in basis points. "Average" is the mean across every
# resolved round trip; "tail" is the single worst observed value -- a lone
# bad fill can breach the tail floor while the mean stays comfortably clear.
MAX_AVERAGE_SLIPPAGE_BPS = 15.0
MAX_TAIL_SLIPPAGE_BPS = 50.0

# Maximum peak-to-trough drawdown, as a fraction of the running peak of
# cumulative resolved P&L.
MAX_DRAWDOWN_PCT = 0.20

BLOCK_MISSING_EVIDENCE = "missing_evidence"
BLOCK_MISSING_NET_EXPECTANCY_EVIDENCE = "missing_net_expectancy_evidence"
BLOCK_NEGATIVE_NET_EXPECTANCY = "negative_net_expectancy"
BLOCK_PREDICTION_ENVELOPE = "prediction_envelope_breach"
BLOCK_INCOMPLETE_SLIPPAGE_EVIDENCE = "incomplete_slippage_evidence"
BLOCK_AVERAGE_SLIPPAGE = "average_slippage_breach"
BLOCK_TAIL_SLIPPAGE = "tail_slippage_breach"
BLOCK_DRAWDOWN = "drawdown_breach"
BLOCK_BEST_TRADE_RELIANCE = "best_trade_reliance"
BLOCK_TRADE_PROFIT_CONCENTRATION = "trade_profit_concentration"
BLOCK_DAY_PROFIT_CONCENTRATION = "day_profit_concentration"
BLOCK_INSTRUMENT_PROFIT_CONCENTRATION = "instrument_profit_concentration"
BLOCK_REGIME_PROFIT_CONCENTRATION = "regime_profit_concentration"
BLOCK_INCOMPLETE_SESSION_EVIDENCE = "incomplete_session_evidence"
BLOCK_INCOMPLETE_INSTRUMENT_EVIDENCE = "incomplete_instrument_evidence"
BLOCK_INCOMPLETE_REGIME_EVIDENCE = "incomplete_regime_evidence"


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


def _profit_concentration(pnls_by_key: Mapping[str, Decimal]) -> Optional[float]:
    """Largest fraction of total WINNING profit attributable to a single
    key (trade/day/instrument/regime). ``None`` when there is no positive
    profit at all -- there is nothing to concentrate (a strategy with no
    winners already fails on net expectancy, not concentration)."""
    positive = {k: v for k, v in pnls_by_key.items() if v > 0}
    total = sum(positive.values(), Decimal("0"))
    if total <= 0:
        return None
    return float(max(positive.values()) / total)


def _group_pnl_sum(records: Sequence[Mapping[str, Any]], field: str) -> dict[str, Decimal]:
    sums: dict[str, Decimal] = {}
    for record in records:
        key = str(record[field])
        value = _decimal(record.get("pnl_after_cost")) or Decimal("0")
        sums[key] = sums.get(key, Decimal("0")) + value
    return sums


def _sharpe_and_sortino(daily_values: Sequence[float]) -> tuple[Optional[float], Optional[float]]:
    """Daily Sharpe (mean/stdev) and Sortino (mean/downside-deviation,
    target 0) over a per-day P&L series. ``None`` for either when there
    are fewer than 2 days (no variance to measure) or the relevant
    denominator is exactly zero -- corroborative-only, so ``None`` simply
    means "no signal", never a blocker."""
    n = len(daily_values)
    if n < 2:
        return None, None
    mean = statistics.mean(daily_values)
    stdev = statistics.pstdev(daily_values)
    sharpe = mean / stdev if stdev > 0 else None

    downside_sq_sum = sum(min(v, 0.0) ** 2 for v in daily_values)
    downside_dev = math.sqrt(downside_sq_sum / n)
    sortino = mean / downside_dev if downside_dev > 0 else None
    return sharpe, sortino


def _max_drawdown_pct(ordered_pnls: Sequence[float]) -> Optional[float]:
    """Largest peak-to-trough fractional drawdown of the cumulative P&L
    curve built by walking ``ordered_pnls`` in the order given (the window
    is assumed chronological, matching real ``EvidenceWindow`` projection
    order). ``None`` when the running peak never went positive -- there is
    no meaningful equity base to express a percentage drawdown against."""
    peak: Optional[float] = None
    cumulative = 0.0
    max_dd = 0.0
    saw_positive_peak = False
    for pnl in ordered_pnls:
        cumulative += pnl
        if peak is None or cumulative > peak:
            peak = cumulative
        if peak is not None and peak > 0:
            saw_positive_peak = True
            drawdown = (peak - cumulative) / peak
            if drawdown > max_dd:
                max_dd = drawdown
    return max_dd if saw_positive_peak else None


@dataclass(frozen=True)
class MetricDecision:
    """Result of ``LiveMetrics.evaluate`` -- a pure snapshot, never a
    mutation. ``passed`` is true iff ``blockers`` is empty. ``metrics``
    always carries every computed value (``None`` where not computable) so
    a caller/report can show the full picture even on a failing decision.
    """

    strategy_id: str
    as_of: dt.datetime
    passed: bool
    blockers: tuple[str, ...]
    metrics: dict[str, Any]
    unresolved_round_trips: tuple[dict[str, Any], ...]
    window_reset_at: Optional[dt.datetime]

    def to_payload(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "as_of": _as_utc(self.as_of).isoformat(),
            "passed": self.passed,
            "blockers": list(self.blockers),
            "metrics": dict(self.metrics),
            "unresolved_round_trips": list(self.unresolved_round_trips),
            "window_reset_at": (
                _as_utc(self.window_reset_at).isoformat() if self.window_reset_at is not None else None
            ),
        }


class LiveMetrics:
    """Evaluates one strategy's ``EvidenceWindow`` against the live
    statistical/concentration safety gates. Stateless and side-effect
    free -- safe to call as often as evidence changes."""

    def __init__(
        self,
        *,
        max_trade_profit_concentration: float = MAX_TRADE_PROFIT_CONCENTRATION,
        max_day_profit_concentration: float = MAX_DAY_PROFIT_CONCENTRATION,
        max_instrument_profit_concentration: float = MAX_INSTRUMENT_PROFIT_CONCENTRATION,
        max_regime_profit_concentration: float = MAX_REGIME_PROFIT_CONCENTRATION,
        max_prediction_envelope_miss_rate: float = MAX_PREDICTION_ENVELOPE_MISS_RATE,
        max_average_slippage_bps: float = MAX_AVERAGE_SLIPPAGE_BPS,
        max_tail_slippage_bps: float = MAX_TAIL_SLIPPAGE_BPS,
        max_drawdown_pct: float = MAX_DRAWDOWN_PCT,
    ):
        self._max_trade_profit_concentration = max_trade_profit_concentration
        self._max_day_profit_concentration = max_day_profit_concentration
        self._max_instrument_profit_concentration = max_instrument_profit_concentration
        self._max_regime_profit_concentration = max_regime_profit_concentration
        self._max_prediction_envelope_miss_rate = max_prediction_envelope_miss_rate
        self._max_average_slippage_bps = max_average_slippage_bps
        self._max_tail_slippage_bps = max_tail_slippage_bps
        self._max_drawdown_pct = max_drawdown_pct

    def evaluate(self, window: EvidenceWindow) -> MetricDecision:
        blockers: list[str] = []
        if window.stale:
            blockers.append(BLOCK_STALE_EVIDENCE)
        if window.breaker_trips:
            blockers.append(BLOCK_BREAKER_TRIP)
        if window.cost_breaches:
            blockers.append(BLOCK_STRESSED_COST_BREACH)
        if window.drawdown_breaches:
            blockers.append(BLOCK_DRAWDOWN_BREACH)

        resolved = [r for r in window.round_trip_records if r.get("resolved") is True]
        unresolved = tuple(r for r in window.round_trip_records if r.get("resolved") is not True)

        metrics: dict[str, Any] = {
            "net_expectancy": None,
            "daily_sharpe": None,
            "daily_sortino": None,
            "prediction_envelope_miss_rate": None,
            "average_slippage_bps": None,
            "tail_slippage_bps": None,
            "max_drawdown_pct": None,
            "best_trade_removed_expectancy": None,
            "max_trade_profit_concentration": None,
            "max_day_profit_concentration": None,
            "max_instrument_profit_concentration": None,
            "max_regime_profit_concentration": None,
            "resolved_round_trip_count": len(resolved),
            "unresolved_round_trip_count": len(unresolved),
        }

        if not resolved:
            blockers.append(BLOCK_MISSING_EVIDENCE)
            return self._decision(window, blockers, metrics, unresolved)

        self._evaluate_pnl_dependent_metrics(resolved, blockers, metrics)
        self._evaluate_prediction_envelope(resolved, blockers, metrics)
        self._evaluate_slippage(resolved, blockers, metrics)

        return self._decision(window, blockers, metrics, unresolved)

    # -- pnl-dependent metrics: expectancy, best-trade removal, every
    #    concentration axis, drawdown, and (as a side effect of grouping by
    #    session) the corroborative Sharpe/Sortino pair. -------------------
    def _evaluate_pnl_dependent_metrics(
        self,
        resolved: Sequence[Mapping[str, Any]],
        blockers: list[str],
        metrics: dict[str, Any],
    ) -> None:
        pnl_values: list[Decimal] = []
        missing_pnl = False
        for record in resolved:
            value = _decimal(record.get("pnl_after_cost"))
            if value is None:
                missing_pnl = True
            else:
                pnl_values.append(value)

        if missing_pnl or not pnl_values:
            blockers.append(BLOCK_MISSING_NET_EXPECTANCY_EVIDENCE)
            return

        net_expectancy = float(sum(pnl_values, Decimal("0")) / Decimal(len(pnl_values)))
        metrics["net_expectancy"] = net_expectancy
        if net_expectancy <= 0:
            blockers.append(BLOCK_NEGATIVE_NET_EXPECTANCY)

        self._evaluate_best_trade_removal(pnl_values, blockers, metrics)
        self._evaluate_trade_profit_concentration(pnl_values, blockers, metrics)
        self._evaluate_day_profit_concentration_and_sharpe(resolved, blockers, metrics)
        self._evaluate_instrument_profit_concentration(resolved, blockers, metrics)
        self._evaluate_regime_profit_concentration(resolved, blockers, metrics)

        drawdown_pct = _max_drawdown_pct([float(v) for v in pnl_values])
        metrics["max_drawdown_pct"] = drawdown_pct
        if drawdown_pct is not None and drawdown_pct > self._max_drawdown_pct:
            blockers.append(BLOCK_DRAWDOWN)

    @staticmethod
    def _evaluate_best_trade_removal(
        pnl_values: Sequence[Decimal], blockers: list[str], metrics: dict[str, Any],
    ) -> None:
        if len(pnl_values) < 2:
            blockers.append(BLOCK_BEST_TRADE_RELIANCE)
            return
        best = max(pnl_values)
        remaining = list(pnl_values)
        remaining.remove(best)
        residual_mean = float(sum(remaining, Decimal("0")) / Decimal(len(remaining)))
        metrics["best_trade_removed_expectancy"] = residual_mean
        if residual_mean <= 0:
            blockers.append(BLOCK_BEST_TRADE_RELIANCE)

    def _evaluate_trade_profit_concentration(
        self, pnl_values: Sequence[Decimal], blockers: list[str], metrics: dict[str, Any],
    ) -> None:
        per_trade = {str(idx): value for idx, value in enumerate(pnl_values)}
        share = _profit_concentration(per_trade)
        metrics["max_trade_profit_concentration"] = share
        if share is not None and share > self._max_trade_profit_concentration:
            blockers.append(BLOCK_TRADE_PROFIT_CONCENTRATION)

    def _evaluate_day_profit_concentration_and_sharpe(
        self, resolved: Sequence[Mapping[str, Any]], blockers: list[str], metrics: dict[str, Any],
    ) -> None:
        if any(record.get("session_id") is None for record in resolved):
            blockers.append(BLOCK_INCOMPLETE_SESSION_EVIDENCE)
            return
        day_pnls = _group_pnl_sum(resolved, "session_id")
        share = _profit_concentration(day_pnls)
        metrics["max_day_profit_concentration"] = share
        if share is not None and share > self._max_day_profit_concentration:
            blockers.append(BLOCK_DAY_PROFIT_CONCENTRATION)

        sharpe, sortino = _sharpe_and_sortino([float(v) for v in day_pnls.values()])
        metrics["daily_sharpe"] = sharpe
        metrics["daily_sortino"] = sortino

    def _evaluate_instrument_profit_concentration(
        self, resolved: Sequence[Mapping[str, Any]], blockers: list[str], metrics: dict[str, Any],
    ) -> None:
        if any(record.get("instrument_id") is None for record in resolved):
            blockers.append(BLOCK_INCOMPLETE_INSTRUMENT_EVIDENCE)
            return
        instrument_pnls = _group_pnl_sum(resolved, "instrument_id")
        share = _profit_concentration(instrument_pnls)
        metrics["max_instrument_profit_concentration"] = share
        if share is not None and share > self._max_instrument_profit_concentration:
            blockers.append(BLOCK_INSTRUMENT_PROFIT_CONCENTRATION)

    def _evaluate_regime_profit_concentration(
        self, resolved: Sequence[Mapping[str, Any]], blockers: list[str], metrics: dict[str, Any],
    ) -> None:
        if any(record.get("regime_id") is None for record in resolved):
            blockers.append(BLOCK_INCOMPLETE_REGIME_EVIDENCE)
            return
        regime_pnls = _group_pnl_sum(resolved, "regime_id")
        share = _profit_concentration(regime_pnls)
        metrics["max_regime_profit_concentration"] = share
        if share is not None and share > self._max_regime_profit_concentration:
            blockers.append(BLOCK_REGIME_PROFIT_CONCENTRATION)

    # -- pnl-independent metrics: prediction envelope, slippage -----------
    def _evaluate_prediction_envelope(
        self, resolved: Sequence[Mapping[str, Any]], blockers: list[str], metrics: dict[str, Any],
    ) -> None:
        misses = sum(1 for r in resolved if r.get("within_prediction_envelope") is not True)
        miss_rate = misses / len(resolved)
        metrics["prediction_envelope_miss_rate"] = miss_rate
        if miss_rate > self._max_prediction_envelope_miss_rate:
            blockers.append(BLOCK_PREDICTION_ENVELOPE)

    def _evaluate_slippage(
        self, resolved: Sequence[Mapping[str, Any]], blockers: list[str], metrics: dict[str, Any],
    ) -> None:
        if any(r.get("slippage_bps") is None for r in resolved):
            blockers.append(BLOCK_INCOMPLETE_SLIPPAGE_EVIDENCE)
            return
        slippages = [float(r["slippage_bps"]) for r in resolved]
        average = sum(slippages) / len(slippages)
        tail = max(slippages)
        metrics["average_slippage_bps"] = average
        metrics["tail_slippage_bps"] = tail
        if average > self._max_average_slippage_bps:
            blockers.append(BLOCK_AVERAGE_SLIPPAGE)
        if tail > self._max_tail_slippage_bps:
            blockers.append(BLOCK_TAIL_SLIPPAGE)

    @staticmethod
    def _decision(
        window: EvidenceWindow,
        blockers: list[str],
        metrics: dict[str, Any],
        unresolved: tuple[dict[str, Any], ...],
    ) -> MetricDecision:
        return MetricDecision(
            strategy_id=window.strategy_id,
            as_of=window.as_of,
            passed=not blockers,
            blockers=tuple(blockers),
            metrics=metrics,
            unresolved_round_trips=unresolved,
            window_reset_at=window.window_reset_at,
        )
