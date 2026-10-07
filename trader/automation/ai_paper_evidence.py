"""Evidence and sizing for ai_paper entries, and the AI dispatch gate (Plan 3 Task 6, R11, R12, R25).

``AiPaperEvidence.prepare_entry`` sizes an entry from the effective limits,
then captures the approval with that quantity and re-checks the size on the
captured snapshot. Every missing or invalid input refuses with its own code.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Callable, Optional

from trader.automation.ai_paper_filter import AiEntryFilter
from trader.automation.ai_paper_sizing import (
    max_entry_quantity, pending_entry_refusal, sizing_inputs,
)
from trader.automation.liquidity_policy import LiquidityPolicy
from trader.automation.models import EntryPolicy
from trader.automation.production_evidence import (
    liquidity_from_history, liquidity_from_sessions, validate_approval,
)
from trader.automation.protective_order_saga import compute_entry_limit
from trader.automation.risk_limits import RiskLimits
from trader.automation.session_risk import AllocationCeiling, AutomationSessionState
from trader.promotion.canary_risk import CanaryRiskStore
from trader.research.market_context import LIVE_NOTIONAL_TOLERANCE
from trader.trading.approval_context import (
    ApprovalContext, ApprovalContextError, EntryLimitsEvidence, capture_approval_context,
)
from trader.trading.quote_feeds import LIVE_ONLY_FEEDS

AI_PAPER_ACTION = "submit_ai_paper_decision"
# R18: the trader owns the order type. A DAY marketable limit at most 10 bps through the ask.
AI_ENTRY_POLICY = EntryPolicy("MARKETABLE_LIMIT", Decimal("10"), "DAY")
_MARGIN_KEYS = ("initMarginAfter", "equityWithLoanAfter")


@dataclass(frozen=True)
class PreparedEntry:
    quantity: int
    approval: ApprovalContext
    session_state: AutomationSessionState
    allocation: AllocationCeiling
    margin_checked: bool


def planned_entry_limit(ask: float, bid: float, offset_bps: Decimal) -> float:
    """The limit the protective saga will send for a BUY on this quote."""
    policy = SimpleNamespace(entry_policy=replace(AI_ENTRY_POLICY, limit_offset_bps=offset_bps), side="BUY")
    return float(compute_entry_limit(policy, ask=Decimal(str(ask)), bid=Decimal(str(bid))))


def _refuse(code: str, message: str) -> ApprovalContextError:
    return ApprovalContextError(code, message)


def _positive(value: Any, code: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise _refuse(code, "required numeric evidence is invalid") from None
    if not math.isfinite(number) or number <= 0:
        raise _refuse(code, "required numeric evidence is invalid")
    return number


def check_margin(approval: ApprovalContext) -> None:
    """R11: an ai_paper entry needs a valid what-if; a missing or invalid one refuses."""
    if approval.what_if is None:
        raise _refuse("MARGIN_UNAVAILABLE", "margin what-if is required for an ai_paper entry")
    try:
        values = [float(approval.what_if.response[key]) for key in _MARGIN_KEYS]
    except (KeyError, TypeError, ValueError):
        raise _refuse("MARGIN_INVALID", "margin what-if is incomplete") from None
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise _refuse("MARGIN_INVALID", "margin what-if is not finite and non-negative")


class AiPaperEvidence:
    def __init__(self, *, broker: Any, quotes: Any, margin: Any, history: Any, journal: Any,
                 account_id: str, account_mode: str, now: Callable[[], dt.datetime], max_drift_bps: float,
                 entry_offset_bps: Decimal, entry_filter: AiEntryFilter,
                 liquidity_policy: Optional[LiquidityPolicy] = None,
                 accepted_feeds: frozenset[str] = LIVE_ONLY_FEEDS):
        self._broker = broker
        self._quotes = quotes
        self._margin = margin
        self._history = history
        self._journal = journal
        self._account_id = account_id
        self._account_mode = account_mode
        self._now = now
        self._max_drift_bps = max_drift_bps
        self._offset_bps = entry_offset_bps
        self._filter = entry_filter
        self._accepted_feeds = frozenset(accepted_feeds)
        self._liquidity_policy = liquidity_policy or LiquidityPolicy(accepted_feeds=self._accepted_feeds)

    def prepare_entry(self, *, conid: int, stop_price: float, requested_quantity: Optional[int],
                      limits: RiskLimits, session: Any, notional: float, experiment_id: str,
                      volume: Any = None, scope_evidence: Any = None) -> PreparedEntry:
        """``volume`` and ``scope_evidence`` come from a discretionary admission (SP2 Plan 3)."""
        self._check_binding()
        snapshot = self._capture()
        refusal = pending_entry_refusal(snapshot, conid, limits)
        if refusal:
            raise _refuse(refusal, "pending entries or position slots are full")
        quote = self._quote(conid)
        liquidity = (liquidity_from_history(self._history, conid, quote, self._now()) if volume is None
                     else liquidity_from_sessions(volume, quote))
        liquidity_max = self._liquidity_policy.max_quantity(liquidity)
        price = self._entry_price(quote)
        if not stop_price < price:
            # Otherwise the sizing would only say "less than one share".
            raise _refuse("STOP_INVALID", "the stop must be below the entry price")
        self._check_filter(conid, price)
        notional_cap = _positive(notional, "NOTIONAL_INVALID") * (1.0 + LIVE_NOTIONAL_TOLERANCE)
        sized = max_entry_quantity(limits, sizing_inputs(
            snapshot, conid=conid, price=price, stop_price=stop_price,
            liquidity_max_shares=liquidity_max, notional_cap=notional_cap))
        quantity = self._choose_quantity(requested_quantity, sized, price, notional_cap)

        approval = self._capture_approval(conid, quantity)
        order = SimpleNamespace(conid=conid, side="BUY", requested_quantity=quantity)
        self._validate(order, approval)
        self._recheck_on_approval(approval, conid, quantity, stop_price, limits, liquidity_max, notional_cap)
        check_margin(approval)
        hwm = self._update_high_water_mark(experiment_id, approval)
        # Persistence and history I/O can use up the quote's freshness window.
        self._validate(order, approval)

        evidence = EntryLimitsEvidence(
            limits=limits, stop_price=float(stop_price), liquidity_max_shares=liquidity_max,
            notional_cap=notional_cap, daily_loss_anchor=float(session.anchor), high_water_mark=hwm)
        return PreparedEntry(
            quantity=quantity,
            approval=replace(approval, risk_direction="INCREASING", entry_limits=evidence,
                             discretionary_scope=scope_evidence),
            session_state=AutomationSessionState(
                high_water_mark=hwm, expected_account_id=self._account_id, limits=limits,
                liquidity=liquidity, daily_loss_anchor=float(session.anchor)),
            allocation=AllocationCeiling(max_gross_fraction=limits.gross_fraction),
            margin_checked=True,
        )

    # -- steps ---------------------------------------------------------------

    def _validate(self, order: Any, approval: ApprovalContext) -> None:
        validate_approval(order, approval, account_id=self._account_id, now=self._now(),
                          accepted_feeds=self._accepted_feeds)

    def _check_binding(self) -> None:
        if (self._account_mode != "paper" or not isinstance(self._account_id, str)
                or not self._account_id.startswith("DU")):
            raise _refuse("PAPER_ONLY", "ai_paper requires a paper account")

    def _capture(self) -> Any:
        try:
            snapshot = self._broker.capture(self._account_id)
        except Exception:
            # Provider text is opaque and may carry credentials.
            raise _refuse("EVIDENCE_UNAVAILABLE", "broker snapshot capture failed") from None
        if snapshot.account_id != self._account_id:
            raise _refuse("ACCOUNT_MISMATCH", "broker account does not match")
        if snapshot.account_mode != "paper":
            raise _refuse("PAPER_ONLY", "broker is not in paper mode")
        if (type(snapshot.generation_id) is not int or snapshot.generation_id <= 0
                or type(snapshot.source_cursor) is not int or snapshot.source_cursor < 0):
            raise _refuse("BROKER_FENCE_INVALID", "complete broker fence is required")
        _positive(snapshot.net_liquidation, "BROKER_EVIDENCE_INVALID")
        return snapshot

    def _quote(self, conid: int) -> Any:
        try:
            quote = self._quotes.executable_quote(conid, side="BUY")
        except Exception:
            quote = None
        if quote is None:
            raise _refuse("EVIDENCE_UNAVAILABLE", "no executable quote")
        return quote

    def _entry_price(self, quote: Any) -> float:
        ask = _positive(quote.ask, "QUOTE_INVALID")
        bid = _positive(quote.bid, "QUOTE_INVALID")
        return planned_entry_limit(ask, bid, self._offset_bps)

    def _check_filter(self, conid: int, price: float) -> None:
        refusal = self._filter.refusal(conid, price)
        if refusal:
            raise _refuse(refusal, self._filter.last_reason or "trading filter refused the entry")

    @staticmethod
    def _choose_quantity(requested: Optional[int], sized: int, price: float, notional_cap: float) -> int:
        if requested is not None:
            if type(requested) is not int or requested < 1:
                raise _refuse("QUANTITY_INVALID", "quantity must be an integer >= 1")
            # R12: checked before the maximum, so the code matches the old path.
            if requested * price > notional_cap:
                raise _refuse("ORDER_EXCEEDS_ATTESTED_NOTIONAL", "quantity exceeds the attested notional")
            if requested > sized:
                raise _refuse("QUANTITY_ABOVE_MAXIMUM", f"quantity {requested} is above the maximum {sized}")
        quantity = sized if requested is None else requested
        if quantity < 1:
            raise _refuse("QUANTITY_BELOW_ONE_SHARE", "the limits leave less than one share")
        return quantity

    def _capture_approval(self, conid: int, quantity: int) -> ApprovalContext:
        try:
            return capture_approval_context(
                account_id=self._account_id, conid=conid, side="BUY", quantity=float(quantity),
                quotes=self._quotes, broker=self._broker, margin=self._margin, now=self._now(),
                max_drift_bps=self._max_drift_bps)
        except Exception:
            raise _refuse("EVIDENCE_UNAVAILABLE", "approval capture failed") from None

    def _recheck_on_approval(self, approval, conid, quantity, stop_price, limits, liquidity_max, notional_cap):
        """The broker may have moved since sizing: the captured snapshot decides (review focus 2)."""
        refusal = pending_entry_refusal(approval.broker, conid, limits)
        if refusal:
            raise _refuse(refusal, "pending entries or position slots are full")
        price = self._entry_price(approval.quote)
        captured_max = max_entry_quantity(limits, sizing_inputs(
            approval.broker, conid=conid, price=price, stop_price=stop_price,
            liquidity_max_shares=liquidity_max, notional_cap=notional_cap))
        if quantity > captured_max:
            raise _refuse("QUANTITY_ABOVE_MAXIMUM", f"quantity {quantity} is above the captured maximum {captured_max}")

    def _update_high_water_mark(self, experiment_id: str, approval: ApprovalContext) -> float:
        store = CanaryRiskStore(self._journal, f"ai_paper:{experiment_id}", self._account_id)
        try:
            return store.update_high_water_mark(float(approval.broker.net_liquidation), self._now())
        except Exception:
            raise _refuse("HWM_UNAVAILABLE", "durable high-water-mark update failed") from None


def _expiry(body: Any) -> Optional[dt.datetime]:
    try:
        value = body["expires_at"]
        parsed = dt.datetime.fromisoformat(value) if isinstance(value, str) else None
    except Exception:
        return None
    if parsed is None or parsed.utcoffset() is None:
        return None
    return parsed


def ai_entry_gate(*, entry_filter: AiEntryFilter, offset_bps: Decimal = AI_ENTRY_POLICY.limit_offset_bps,
                  ) -> Callable[[Any, ApprovalContext, Any, dt.datetime], Optional[str]]:
    """R25: expiry, the trading filter and the limit through the fresh ask, at final dispatch."""

    def gate(request: Any, approval: ApprovalContext, quote: Any, now: dt.datetime) -> Optional[str]:
        if getattr(request, "action", None) != AI_PAPER_ACTION:
            return None
        expires_at = _expiry(request.body)
        if expires_at is None:
            return "DECISION_INVALID"
        if now >= expires_at:
            return "DECISION_EXPIRED"
        planned = planned_entry_limit(float(approval.quote.ask), float(approval.quote.bid), offset_bps)
        refusal = entry_filter.refusal(approval.conid, planned)
        if refusal:
            return refusal
        fresh_ask = float(quote.ask if quote.ask is not None else quote.price)
        fresh_bid = float(quote.bid if quote.bid is not None else fresh_ask)
        if planned > planned_entry_limit(fresh_ask, fresh_bid, offset_bps):
            return "ENTRY_LIMIT_THROUGH_QUOTE"
        return None

    return gate
