"""P5 Task 5 — live execution capacity monitoring.

``CapacityMonitor.evaluate`` is a pure function of an attribution window and a
proposed per-instrument allocation. It compares observed execution quality
(participation, fills, slippage, spread) against capacity limits and rejects
scaling when projected quantity exceeds 0.25% ADV. Missing depth or sparse
evidence fails closed — never extrapolated beyond observed liquidity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

MAX_ADV_PARTICIPATION = 0.0025  # 0.25% of average daily volume
MIN_RECORDS_FOR_CAPACITY = 5

BLOCK_SPARSE_EVIDENCE = "sparse_capacity_evidence"
BLOCK_MISSING_DEPTH = "missing_depth_evidence"
BLOCK_ADV_PARTICIPATION = "adv_participation_breach"
BLOCK_LOW_FILL_PROBABILITY = "low_fill_probability"
BLOCK_HIGH_PARTIAL_FILL_RATE = "high_partial_fill_rate"
BLOCK_HIGH_SLIPPAGE = "high_slippage"
BLOCK_WIDE_SPREAD = "wide_spread"

MIN_FILL_PROBABILITY = 0.85
MAX_PARTIAL_FILL_RATE = 0.25
MAX_AVERAGE_SLIPPAGE_BPS = 20.0
MAX_AVERAGE_SPREAD_BPS = 20.0

__all__ = [
    "MAX_ADV_PARTICIPATION",
    "MIN_RECORDS_FOR_CAPACITY",
    "BLOCK_SPARSE_EVIDENCE",
    "BLOCK_MISSING_DEPTH",
    "BLOCK_ADV_PARTICIPATION",
    "BLOCK_LOW_FILL_PROBABILITY",
    "BLOCK_HIGH_PARTIAL_FILL_RATE",
    "BLOCK_HIGH_SLIPPAGE",
    "BLOCK_WIDE_SPREAD",
    "CapacityDecision",
    "CapacityMonitor",
]


def _float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_records(attribution_window: Any) -> tuple[dict[str, Any], ...]:
    if hasattr(attribution_window, "records"):
        raw = attribution_window.records
    elif isinstance(attribution_window, Mapping):
        raw = attribution_window.get("records") or ()
    else:
        raw = attribution_window
    return tuple(dict(r) if isinstance(r, Mapping) else r for r in raw)


def _window_sparse(attribution_window: Any, records: Sequence[Mapping[str, Any]]) -> bool:
    if hasattr(attribution_window, "sparse"):
        return bool(attribution_window.sparse)
    if isinstance(attribution_window, Mapping):
        return bool(attribution_window.get("sparse"))
    return len(records) < MIN_RECORDS_FOR_CAPACITY


@dataclass(frozen=True)
class CapacityDecision:
    passed: bool
    blockers: tuple[str, ...]
    metrics: Mapping[str, Any]
    projected_adv_participation: Optional[float] = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blockers": list(self.blockers),
            "metrics": dict(self.metrics),
            "projected_adv_participation": self.projected_adv_participation,
        }


class CapacityMonitor:
    """Evaluates execution capacity from attribution evidence and a proposed
    allocation. Stateless and side-effect free."""

    def __init__(
        self,
        *,
        max_adv_participation: float = MAX_ADV_PARTICIPATION,
        min_records: int = MIN_RECORDS_FOR_CAPACITY,
        min_fill_probability: float = MIN_FILL_PROBABILITY,
        max_partial_fill_rate: float = MAX_PARTIAL_FILL_RATE,
        max_average_slippage_bps: float = MAX_AVERAGE_SLIPPAGE_BPS,
        max_average_spread_bps: float = MAX_AVERAGE_SPREAD_BPS,
    ):
        self._max_adv_participation = max_adv_participation
        self._min_records = min_records
        self._min_fill_probability = min_fill_probability
        self._max_partial_fill_rate = max_partial_fill_rate
        self._max_average_slippage_bps = max_average_slippage_bps
        self._max_average_spread_bps = max_average_spread_bps

    def evaluate(
        self,
        attribution_window: Any,
        proposed_allocation: Mapping[str, Any],
        *,
        adv_by_instrument: Optional[Mapping[str, float]] = None,
    ) -> CapacityDecision:
        records = _normalize_records(attribution_window)
        blockers: list[str] = []

        metrics: dict[str, Any] = {
            "participation_rate": None,
            "fill_probability": None,
            "slippage_bps": None,
            "spread_bps": None,
            "partial_fill_rate": None,
            "record_count": len(records),
        }

        if _window_sparse(attribution_window, records) or len(records) < self._min_records:
            blockers.append(BLOCK_SPARSE_EVIDENCE)
            return CapacityDecision(passed=False, blockers=tuple(blockers), metrics=metrics)

        if any(r.get("depth_available") is not True for r in records):
            blockers.append(BLOCK_MISSING_DEPTH)

        fill_probs: list[float] = []
        partials: list[bool] = []
        slippages: list[float] = []
        spreads: list[float] = []
        participation_rates: list[float] = []

        for record in records:
            requested = _float(record.get("requested_quantity"))
            filled = _float(record.get("filled_quantity"))
            if requested is not None and requested > 0 and filled is not None:
                fill_probs.append(min(1.0, filled / requested))
                partials.append(filled < requested)
            elif record.get("filled") is False:
                fill_probs.append(0.0)
                partials.append(False)

            slip = _float(record.get("slippage_bps"))
            if slip is not None:
                slippages.append(slip)
            spread = _float(record.get("spread_bps"))
            if spread is not None:
                spreads.append(spread)

            part = _float(record.get("participation_rate"))
            if part is not None:
                participation_rates.append(part)

        if fill_probs:
            fill_probability = sum(fill_probs) / len(fill_probs)
            partial_fill_rate = sum(1 for p in partials if p) / len(partials) if partials else None
            metrics["fill_probability"] = fill_probability
            metrics["partial_fill_rate"] = partial_fill_rate
            if fill_probability < self._min_fill_probability:
                blockers.append(BLOCK_LOW_FILL_PROBABILITY)
            if partial_fill_rate is not None and partial_fill_rate > self._max_partial_fill_rate:
                blockers.append(BLOCK_HIGH_PARTIAL_FILL_RATE)

        if slippages:
            avg_slippage = sum(slippages) / len(slippages)
            metrics["slippage_bps"] = avg_slippage
            if avg_slippage > self._max_average_slippage_bps:
                blockers.append(BLOCK_HIGH_SLIPPAGE)

        if spreads:
            avg_spread = sum(spreads) / len(spreads)
            metrics["spread_bps"] = avg_spread
            if avg_spread > self._max_average_spread_bps:
                blockers.append(BLOCK_WIDE_SPREAD)

        if participation_rates:
            metrics["participation_rate"] = sum(participation_rates) / len(participation_rates)

        projected_adv = self._max_projected_adv_participation(
            proposed_allocation, adv_by_instrument, records,
        )
        if projected_adv is not None and projected_adv > self._max_adv_participation:
            blockers.append(BLOCK_ADV_PARTICIPATION)

        blockers = list(dict.fromkeys(blockers))
        return CapacityDecision(
            passed=not blockers,
            blockers=tuple(blockers),
            metrics=metrics,
            projected_adv_participation=projected_adv,
        )

    def _max_projected_adv_participation(
        self,
        proposed_allocation: Mapping[str, Any],
        adv_by_instrument: Optional[Mapping[str, float]],
        records: Sequence[Mapping[str, Any]],
    ) -> Optional[float]:
        adv_map: dict[str, float] = {}
        if adv_by_instrument:
            adv_map.update({str(k): float(v) for k, v in adv_by_instrument.items()})
        for record in records:
            instrument = record.get("instrument_id")
            adv = _float(record.get("adv"))
            if instrument is not None and adv is not None and adv > 0:
                adv_map.setdefault(str(instrument), adv)

        if not adv_map:
            return None

        max_participation: Optional[float] = None
        for instrument, quantity in proposed_allocation.items():
            qty = _float(quantity)
            adv = adv_map.get(str(instrument))
            if qty is None or adv is None or adv <= 0:
                continue
            participation = qty / adv
            if max_participation is None or participation > max_participation:
                max_participation = participation
        return max_participation
