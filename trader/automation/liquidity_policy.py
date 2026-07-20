"""Trader-owned instrument liquidity gates for automated entries."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Tuple

from trader.trading.circuit_breaker import BreakerSignal

# Hard floors / ceilings from the trading-income foundation design §9.2
MIN_PRICE = 5.0
MIN_MEDIAN_DOLLAR_VOLUME = 50_000_000.0
MAX_SPREAD_BPS = 15.0
MAX_ADV_FRACTION = 0.0025  # 0.25% of 20-day ADV
PERMITTED_FEEDS = frozenset({"live"})


@dataclass(frozen=True)
class LiquidityEvidence:
    price: float
    median_dollar_volume_20d: float
    adv_shares_20d: float
    spread_bps: float
    top_of_book_depth: float
    feed_type: str
    session_state: str
    halt_requalifying: bool = False
    sliced_execution_approved: bool = False


@dataclass(frozen=True)
class LiquidityDecision:
    approved: bool
    reason_codes: Tuple[str, ...]
    breaker_signals: Tuple[BreakerSignal, ...] = ()


def _finite_positive(value: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


class LiquidityPolicy:
    """Pure evaluator — no I/O. Caller supplies evidence and an injected clock."""

    def evaluate(
        self,
        quantity: Decimal,
        evidence: LiquidityEvidence,
        *,
        now: dt.datetime,
    ) -> LiquidityDecision:
        reasons: list[str] = []
        signals: list[BreakerSignal] = []

        if not (
            _finite_positive(evidence.price)
            and _finite_positive(evidence.median_dollar_volume_20d)
            and _finite_positive(evidence.adv_shares_20d)
            and math.isfinite(float(evidence.spread_bps))
            and float(evidence.spread_bps) >= 0
            and math.isfinite(float(evidence.top_of_book_depth))
            and float(evidence.top_of_book_depth) >= 0
        ):
            return LiquidityDecision(
                approved=False,
                reason_codes=("LIQUIDITY_EVIDENCE_INVALID",),
            )

        qty = float(quantity)
        if not math.isfinite(qty) or qty <= 0:
            return LiquidityDecision(
                approved=False,
                reason_codes=("LIQUIDITY_EVIDENCE_INVALID",),
            )

        if evidence.price < MIN_PRICE:
            reasons.append("PRICE_FLOOR")

        if evidence.median_dollar_volume_20d < MIN_MEDIAN_DOLLAR_VOLUME:
            reasons.append("MEDIAN_DOLLAR_VOLUME")

        if evidence.spread_bps > MAX_SPREAD_BPS:
            reasons.append("SPREAD_BPS")

        adv_cap = evidence.adv_shares_20d * MAX_ADV_FRACTION
        if qty > adv_cap:
            reasons.append("ADV_CAP")

        if evidence.feed_type not in PERMITTED_FEEDS:
            reasons.append("FEED_NOT_LIVE")
            signals.append(BreakerSignal("QUOTE_FAILURE", now, detail=evidence.feed_type))

        if evidence.session_state == "halted":
            reasons.append("INSTRUMENT_HALT")
            signals.append(BreakerSignal("INSTRUMENT_HALT", now, detail="halted"))

        if evidence.halt_requalifying:
            reasons.append("HALT_REQUALIFICATION")

        if (
            qty > evidence.top_of_book_depth
            and not evidence.sliced_execution_approved
        ):
            reasons.append("DEPTH_EXCEEDED")

        return LiquidityDecision(
            approved=not reasons,
            reason_codes=tuple(reasons),
            breaker_signals=tuple(signals),
        )
