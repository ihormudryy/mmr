"""Immediate pre-dispatch revalidation for approved trading commands."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Optional

from trader.data.broker_state import BrokerRiskSnapshotError
from trader.trading.command_policy import CommandAuthorityPolicy
from trader.trading.trading_control import PauseStateUnavailable, TradingPausedError


MAX_QUOTE_AGE_SECONDS = 5.0
MAX_SOURCE_CLOCK_SKEW_SECONDS = 30.0


class DispatchGuardError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class DispatchPermit:
    generation_id: int
    source_cursor: int
    quote_timestamp: Optional[dt.datetime]
    what_if_timestamp: Optional[dt.datetime]
    warnings: tuple[str, ...] = ()


def _direction(value: object) -> str:
    return str(getattr(value, "value", value))


def _working_order_fingerprint(snapshot) -> tuple:
    return tuple(
        (row.order_entity_id, row.status, row.total_quantity, row.filled_quantity)
        for row in snapshot.working_orders
    )


class DispatchGuard:
    def __init__(
        self, *, broker, quotes, margin, controls, risk_gate,
        policy: CommandAuthorityPolicy, account_id: str, account_mode: str,
    ):
        self._broker = broker
        self._quotes = quotes
        self._margin = margin
        self._controls = controls
        self._risk_gate = risk_gate
        self._policy = policy
        self._account_id = account_id
        self._account_mode = account_mode

    def revalidate(self, approved, request, now: dt.datetime) -> DispatchPermit:
        if request.account_id != self._account_id:
            raise DispatchGuardError("ACCOUNT_MISMATCH", "command account is not pinned account")
        try:
            current = self._broker.capture(self._account_id)
        except BrokerRiskSnapshotError as exc:
            raise DispatchGuardError(exc.code, exc.message, retryable=True) from exc
        except Exception as exc:
            raise DispatchGuardError(
                "BROKER_UNAVAILABLE", "broker snapshot unavailable", retryable=True
            ) from exc

        if current.account_id != self._account_id:
            raise DispatchGuardError("ACCOUNT_MISMATCH", "broker snapshot account changed")
        if current.account_mode != self._account_mode:
            raise DispatchGuardError("ACCOUNT_MODE_MISMATCH", "broker account mode changed")
        if (
            current.generation_id < approved.broker.generation_id
            or current.source_cursor < approved.broker.source_cursor
        ):
            raise DispatchGuardError("GENERATION_REGRESSION", "broker fence moved backwards")

        initial_held = approved.broker.reducible_quantity(approved.conid)
        current_held = current.reducible_quantity(approved.conid)
        if (
            current_held != initial_held
            or _working_order_fingerprint(current)
            != _working_order_fingerprint(approved.broker)
        ):
            raise DispatchGuardError(
                "BROKER_STATE_CHANGED", "target position or working orders changed"
            )

        if _direction(approved.risk_direction) == "REDUCING":
            quantity = abs(float(approved.quantity))
            reducing = (
                approved.side == "SELL" and current_held > 0 and quantity <= current_held
            ) or (
                approved.side == "BUY" and current_held < 0 and quantity <= -current_held
            )
            if not reducing:
                raise DispatchGuardError(
                    "REDUCTION_NOT_MONOTONIC", "order could increase or flip exposure"
                )
            return DispatchPermit(
                current.generation_id, current.source_cursor, None, None
            )

        if self._account_mode == "live":
            if not self._policy.live_enabled:
                raise DispatchGuardError("LIVE_TRADING_DISABLED", "live authority is disabled")
            if self._policy.live_account_id != self._account_id:
                raise DispatchGuardError("WRONG_LIVE_ACCOUNT", "live account policy mismatch")
        side = "ask" if approved.side == "BUY" else "bid"
        quote = self._quotes.executable_quote(approved.conid, side=side)
        if quote is None:
            raise DispatchGuardError(
                "EXECUTABLE_QUOTE_MISSING", "no executable quote", retryable=True
            )
        try:
            price = float(quote.price)
        except (TypeError, ValueError) as exc:
            raise DispatchGuardError("EXECUTABLE_QUOTE_INVALID", "quote is not numeric") from exc
        if not math.isfinite(price) or price <= 0 or quote.conid != approved.conid:
            raise DispatchGuardError("EXECUTABLE_QUOTE_INVALID", "quote is not finite/positive")
        if quote.side != side:
            raise DispatchGuardError("EXECUTABLE_QUOTE_INVALID", "wrong executable side")
        if quote.bid is not None and quote.ask is not None:
            if (
                not math.isfinite(float(quote.bid))
                or not math.isfinite(float(quote.ask))
                or float(quote.bid) <= 0
                or float(quote.ask) <= 0
            ):
                raise DispatchGuardError("EXECUTABLE_QUOTE_INVALID", "non-finite market")
            if float(quote.bid) > float(quote.ask):
                raise DispatchGuardError("CROSSED_MARKET", "bid exceeds ask", retryable=True)

        if self._account_mode == "live":
            if quote.feed_type != "live":
                raise DispatchGuardError("FEED_NOT_LIVE", "live feed required", retryable=True)
            if quote.session_state != "continuous":
                raise DispatchGuardError(
                    "SESSION_INCOMPATIBLE", "market is not continuous", retryable=True
                )
            if quote.market_timestamp.tzinfo is None:
                raise DispatchGuardError("EXECUTABLE_QUOTE_INVALID", "quote timestamp is naive")
            age = (now - quote.market_timestamp).total_seconds()
            if age > MAX_QUOTE_AGE_SECONDS:
                raise DispatchGuardError("QUOTE_STALE", "quote is stale", retryable=True)
            if age < -MAX_SOURCE_CLOCK_SKEW_SECONDS:
                raise DispatchGuardError("SOURCE_CLOCK_SKEW", "quote clock is in the future")

        reference = float(approved.reference_price)
        if not math.isfinite(reference) or reference <= 0:
            raise DispatchGuardError("REFERENCE_PRICE_INVALID", "reference price is invalid")
        limit = float(self._policy.max_drift_bps)
        if self._account_mode != "live":
            requested_limit = float(approved.max_drift_bps)
            if not math.isfinite(requested_limit) or requested_limit <= 0:
                raise DispatchGuardError("PRICE_DRIFT_INVALID", "price drift limit is invalid")
            limit = min(limit, requested_limit)
        drift = abs(price - reference) / reference * 10_000.0
        if not math.isfinite(drift) or drift > limit:
            raise DispatchGuardError("PRICE_DRIFT_EXCEEDED", "price drift exceeds policy")

        quantity = abs(float(approved.quantity))
        notional = quantity * price
        ceiling = self._policy.max_order_notional
        if quantity <= 0 or not math.isfinite(notional) or (
            ceiling is not None and notional > float(ceiling)
        ):
            raise DispatchGuardError("ORDER_NOTIONAL_LIMIT", "order notional exceeds policy")

        try:
            self._controls.require_unpaused(self._account_id)
        except (TradingPausedError, PauseStateUnavailable, RuntimeError) as exc:
            raise DispatchGuardError("TRADING_PAUSED", "new exposure is paused", retryable=True) from exc

        warnings: tuple[str, ...] = ()
        try:
            margin = self._margin.what_if_margin(
                approved.conid, approved.side, approved.quantity
            )
        except Exception:
            margin = None
        if self._account_mode == "live" and margin is None:
            raise DispatchGuardError(
                "WHAT_IF_UNAVAILABLE", "live margin what-if is required", retryable=True
            )
        if margin is not None:
            try:
                required_margin = {
                    key: float(margin[key])
                    for key in ("initMarginAfter", "equityWithLoanAfter")
                }
                if any(
                    not math.isfinite(value) or value < 0
                    for value in required_margin.values()
                ):
                    raise ValueError("non-finite margin")
            except (KeyError, TypeError, ValueError):
                if self._account_mode == "live":
                    raise DispatchGuardError(
                        "WHAT_IF_INVALID", "live margin what-if is invalid"
                    )
                margin = None
        if self._account_mode != "live" and margin is None:
            warnings = ("WHAT_IF_UNAVAILABLE_PAPER",)
        if margin is not None:
            leverage = self._risk_gate.check_leverage(margin, current.net_liquidation)
            if not leverage.approved:
                raise DispatchGuardError("LEVERAGE_REJECTED", str(leverage.reason))

        return DispatchPermit(
            generation_id=current.generation_id,
            source_cursor=current.source_cursor,
            quote_timestamp=quote.market_timestamp,
            what_if_timestamp=(now if margin is not None else None),
            warnings=warnings,
        )
