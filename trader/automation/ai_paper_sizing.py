"""ai_paper entry sizing and pending-entry counting (spec 5.4, Plan 3 Task 6, R11-R13).

Pure functions: the caller supplies the broker snapshot and prices.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from trader.automation.risk_limits import RiskLimits
from trader.promotion.allocation_policy import compute_gross_notional

# Legs that protect a position. Any other working order (an entry, an unknown
# leg, an external order) counts as a pending entry, so the limit cannot be dodged.
PROTECTIVE_LEGS = frozenset({"stop", "take_profit", "exit"})
LIMIT_TIGHTENED = "LIMIT_TIGHTENED_BEFORE_DISPATCH"
LOSS_STATE_UNKNOWN = "LOSS_STATE_UNKNOWN"
_FLOOR_TOLERANCE = 1e-9


@dataclass(frozen=True)
class SizingInputs:
    equity: float
    price: float
    stop_price: float
    existing_position_value: float
    current_gross_notional: float
    liquidity_max_shares: float
    notional_cap: float


def _shares(value: float) -> float:
    return value if math.isfinite(value) and value > 0 else 0.0


def max_entry_quantity(limits: RiskLimits, inputs: SizingInputs) -> int:
    """The minimum over every limit, rounded down to whole shares; 0 when any input is unusable."""
    equity, price, stop = inputs.equity, inputs.price, inputs.stop_price
    if not all(math.isfinite(v) and v > 0 for v in (equity, price, stop)) or stop >= price:
        return 0
    bounds = (
        limits.trade_risk_fraction * equity / (price - stop),
        (limits.position_fraction * equity - inputs.existing_position_value) / price,
        (limits.gross_fraction * equity - inputs.current_gross_notional) / price,
        inputs.liquidity_max_shares,
        inputs.notional_cap / price,
    )
    return int(math.floor(min(_shares(bound) for bound in bounds) + _FLOOR_TOLERANCE))


def is_pending_entry(order: Any) -> bool:
    remaining = float(order.total_quantity) - float(order.filled_quantity)
    return not order.deleted and remaining > 0 and order.leg not in PROTECTIVE_LEGS


def pending_entry_refusal(broker: Any, conid: int, limits: RiskLimits) -> Optional[str]:
    """MAX_PENDING_ENTRIES when one more entry would exceed the pending-order or position slots."""
    pending = [order for order in broker.working_orders if is_pending_entry(order)]
    held = {row.conid for row in broker.positions
            if row.quantity and float(row.quantity) != 0 and not row.deleted}
    busy = held | {order.conid for order in pending}
    if len(pending) + 1 > limits.max_pending_entry_orders:
        return "MAX_PENDING_ENTRIES"
    if conid not in busy and len(busy) + 1 > limits.max_positions:
        return "MAX_PENDING_ENTRIES"
    return None


def sizing_inputs(broker: Any, *, conid: int, price: float, stop_price: float,
                  liquidity_max_shares: float, notional_cap: float) -> SizingInputs:
    gross, unknown = compute_gross_notional(broker, quote_prices={conid: price})
    if unknown:
        # A working buy with no price would be left out of gross: never size on that.
        gross = math.inf
    return SizingInputs(
        equity=float(broker.net_liquidation), price=float(price), stop_price=float(stop_price),
        existing_position_value=float(broker.position_value(conid)), current_gross_notional=gross,
        liquidity_max_shares=float(liquidity_max_shares), notional_cap=float(notional_cap))


def entry_limit_violations(limits: RiskLimits, *, broker: Any, conid: int, quantity: float, price: float,
                           evidence: Any) -> tuple[str, ...]:
    """LIMIT_TIGHTENED_BEFORE_DISPATCH when the entry no longer fits ``limits`` on ``broker``."""
    inputs = sizing_inputs(broker, conid=conid, price=price, stop_price=evidence.stop_price,
                           liquidity_max_shares=evidence.liquidity_max_shares,
                           notional_cap=evidence.notional_cap)
    breaches = (
        quantity > max_entry_quantity(limits, inputs),
        pending_entry_refusal(broker, conid, limits) is not None,
        bool(loss_limit_breaches(limits, broker=broker, evidence=evidence)),
    )
    return (LIMIT_TIGHTENED,) if any(breaches) else ()


def loss_limit_breaches(limits: RiskLimits, *, broker: Any, evidence: Any) -> tuple[str, ...]:
    """DAILY_LOSS / DRAWDOWN on the current broker P&L; LOSS_STATE_UNKNOWN when it cannot be read."""
    values = (float(broker.daily_pnl), float(broker.net_liquidation),
              float(evidence.daily_loss_anchor), float(evidence.high_water_mark))
    if not all(math.isfinite(value) for value in values):
        return (LOSS_STATE_UNKNOWN,)
    daily_pnl, equity, anchor, hwm = values
    breaches = []
    if max(0.0, -daily_pnl) >= anchor * limits.daily_loss_fraction:
        breaches.append("DAILY_LOSS")
    if hwm > 0 and (hwm - equity) / hwm >= limits.drawdown_fraction:
        breaches.append("DRAWDOWN")
    return tuple(breaches)
