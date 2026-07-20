"""Trader-owned session / portfolio risk controller for automated intents.

Hard limits (foundation design §9.1) always win over request fields and
artifact ceilings are applied via most-restrictive-wins.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Optional, Protocol, Tuple

from trader.automation.artifact_verifier import VerifiedArtifact
from trader.automation.calendar_policy import SessionSchedule, XNYSCalendarPolicy
from trader.automation.liquidity_policy import (
    LiquidityEvidence,
    LiquidityPolicy,
)
from trader.automation.models import ExecutionIntent
from trader.promotion.allocation_policy import (
    AllocationPolicy,
    AuthoritySource,
    STEADY_MAX_GROSS_FRACTION,
)
from trader.trading.approval_context import ApprovalContext
from trader.trading.circuit_breaker import BreakerSignal

# Hard trader-owned ceilings — never loosened by request/artifact fields.
MAX_POSITIONS = 3
MAX_POSITION_FRACTION = 0.05
MAX_GROSS_FRACTION = STEADY_MAX_GROSS_FRACTION
MAX_TRADE_RISK_FRACTION = 0.002  # 0.20%
MAX_DAILY_LOSS_FRACTION = 0.005  # 0.50%
MAX_DRAWDOWN_FRACTION = 0.03  # 3%


@dataclass(frozen=True)
class AllocationCeiling:
    """Signed gross allocation authority input (may only tighten the steady cap)."""

    max_gross_fraction: float
    authority_digest: Optional[str] = None

    def __post_init__(self):
        value = float(self.max_gross_fraction)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("max_gross_fraction must be finite and positive")


@dataclass(frozen=True)
class AutomationSessionState:
    high_water_mark: float
    expected_account_id: str
    liquidity: Optional[LiquidityEvidence] = None
    opening_stabilization: dt.timedelta = dt.timedelta(minutes=5)


@dataclass(frozen=True)
class AutomatedRiskDecision:
    approved: bool
    reason_codes: Tuple[str, ...]
    approved_quantity: Optional[Decimal] = None
    equity_risk_fraction: Optional[float] = None
    breaker_signals: Tuple[BreakerSignal, ...] = ()
    calendar_version: Optional[str] = None
    schedule: Optional[SessionSchedule] = None
    effective_gross_ceiling: Optional[float] = None
    authority_digest: Optional[str] = None
    allocation_limit_candidates: Tuple[tuple[str, float], ...] = ()


class BreakerPort(Protocol):
    def record(self, signal: BreakerSignal) -> Any: ...


@dataclass(frozen=True)
class _SyntheticAllocationAuthority:
    account_id: str
    account_mode: str
    artifact_digest: str
    max_gross_allocation: float
    authority_digest: Optional[str] = None
    stage: str = "CANARY"
    expires_at: dt.datetime = dt.datetime(2099, 1, 1, tzinfo=dt.timezone.utc)

    @property
    def payload_digest(self) -> Optional[str]:
        return self.authority_digest


def _finite(value: float) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _spread_bps_from_quote(bid: Optional[float], ask: Optional[float], price: float) -> Optional[float]:
    if bid is None or ask is None or price <= 0:
        return None
    if not (_finite(bid) and _finite(ask) and _finite(price)):
        return None
    if ask < bid:
        return None
    return ((ask - bid) / price) * 10_000.0


def _liquidity_from_context(
    session: AutomationSessionState,
    approval: ApprovalContext,
) -> Optional[LiquidityEvidence]:
    """Prefer explicit session liquidity; fill feed/session/spread from quote."""
    if session.liquidity is not None:
        evidence = session.liquidity
        if approval.market is None:
            return evidence
        quote = approval.market.quote
        spread = evidence.spread_bps
        computed = _spread_bps_from_quote(quote.bid, quote.ask, quote.price)
        if computed is not None:
            # Most restrictive (wider) spread wins when both present
            spread = max(spread, computed)
        return LiquidityEvidence(
            price=float(quote.price),
            median_dollar_volume_20d=evidence.median_dollar_volume_20d,
            adv_shares_20d=evidence.adv_shares_20d,
            spread_bps=spread,
            top_of_book_depth=evidence.top_of_book_depth,
            feed_type=quote.feed_type or evidence.feed_type,
            session_state=quote.session_state or evidence.session_state,
            halt_requalifying=evidence.halt_requalifying,
            sliced_execution_approved=evidence.sliced_execution_approved,
        )
    return None


class SessionRiskController:
    """Evaluate an intent against trader-owned session, liquidity, and risk policy."""

    def __init__(
        self,
        *,
        calendar: Optional[XNYSCalendarPolicy] = None,
        liquidity_policy: Optional[LiquidityPolicy] = None,
        breaker: Optional[BreakerPort] = None,
        allocation_policy: Optional[AllocationPolicy] = None,
        now: Optional[Callable[[], dt.datetime]] = None,
    ):
        self._calendar = calendar or XNYSCalendarPolicy()
        self._liquidity = liquidity_policy or LiquidityPolicy()
        self._breaker = breaker
        self._allocation_policy = allocation_policy or AllocationPolicy(now=now)
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def evaluate(
        self,
        intent: ExecutionIntent,
        artifact: VerifiedArtifact,
        approval_context: ApprovalContext,
        session_state: AutomationSessionState,
        allocation: AllocationCeiling,
        authority: AuthoritySource = None,
    ) -> AutomatedRiskDecision:
        now = self._now()
        reasons: list[str] = []
        signals: list[BreakerSignal] = []

        schedule = self._calendar.resolve(
            now, opening_stabilization=session_state.opening_stabilization,
        )
        calendar_version = self._calendar.calendar_version()

        broker = approval_context.broker
        equity = float(broker.net_liquidation)

        # --- Account fence -------------------------------------------------
        if broker.account_id != session_state.expected_account_id:
            reasons.append("ACCOUNT_MISMATCH")
            signals.append(BreakerSignal(
                "ACCOUNT_MISMATCH", now, detail=broker.account_id,
                key=broker.account_id,
            ))

        # --- Artifact / allowlist ------------------------------------------
        if intent.artifact_id != artifact.artifact_id:
            reasons.append("ARTIFACT_MISMATCH")

        allowlist = {str(item) for item in artifact.allowlist}
        if str(intent.conid) not in allowlist:
            reasons.append("CONID_NOT_PERMITTED")

        # --- Long-only -----------------------------------------------------
        held = broker.reducible_quantity(intent.conid)
        if intent.side == "SELL" and held <= 0:
            reasons.append("LONG_ONLY")
        elif intent.side == "SELL" and intent.requested_quantity is not None:
            if float(intent.requested_quantity) > held:
                reasons.append("LONG_ONLY")
        elif intent.side not in ("BUY", "SELL"):
            reasons.append("LONG_ONLY")

        # New exposure only proceeds through the remaining entry gates.
        is_entry = intent.side == "BUY"

        # --- Daily loss / drawdown -----------------------------------------
        if _finite(equity) and equity > 0:
            daily_loss_frac = max(0.0, -float(broker.daily_pnl)) / equity
            if daily_loss_frac >= MAX_DAILY_LOSS_FRACTION:
                reasons.append("DAILY_LOSS")
                signals.append(BreakerSignal(
                    "DAILY_LOSS_BREACH", now,
                    detail=f"daily_pnl={broker.daily_pnl}",
                ))

            hwm = float(session_state.high_water_mark)
            if _finite(hwm) and hwm > 0:
                drawdown = max(0.0, (hwm - equity) / hwm)
                if drawdown >= MAX_DRAWDOWN_FRACTION:
                    reasons.append("DRAWDOWN")
                    signals.append(BreakerSignal(
                        "DRAWDOWN_BREACH", now,
                        detail=f"drawdown={drawdown:.6f}",
                    ))
        else:
            reasons.append("EQUITY_INVALID")

        # --- Session calendar (entries only) -------------------------------
        if is_entry:
            if schedule is None or not self._calendar.allows_new_entry(now, schedule):
                # Distinguish cutoff vs closed/stabilization for audit clarity
                if schedule is not None and now >= schedule.entry_cutoff_utc:
                    reasons.append("ENTRY_CUTOFF")
                elif schedule is not None and now < schedule.opening_stabilization_end_utc:
                    reasons.append("OPENING_STABILIZATION")
                else:
                    reasons.append("SESSION_CLOSED")

        qty = intent.requested_quantity
        if qty is None:
            reasons.append("QUANTITY_REQUIRED")
            qty = Decimal("0")

        entry_price = None
        if approval_context.market is not None:
            entry_price = float(approval_context.market.quote.price)

        stop = float(intent.stop_policy.stop_price)

        # --- Stop validity (long entries) ----------------------------------
        if is_entry and entry_price is not None:
            if not (_finite(stop) and stop > 0 and stop < entry_price):
                reasons.append("STOP_INVALID")

        # --- Position count ------------------------------------------------
        if is_entry:
            open_conids = {
                row.conid for row in broker.positions
                if row.quantity and float(row.quantity) != 0 and not row.deleted
            }
            if intent.conid not in open_conids and len(open_conids) >= MAX_POSITIONS:
                reasons.append("MAX_POSITIONS")

        # --- Gross exposure via signed allocation policy (P5 Task 2) ---------

        resolved_authority = authority
        if resolved_authority is None:
            resolved_authority = _SyntheticAllocationAuthority(
                account_id=broker.account_id,
                account_mode=broker.account_mode,
                artifact_digest=artifact.artifact_id,
                max_gross_allocation=float(allocation.max_gross_fraction),
                authority_digest=allocation.authority_digest,
            )

        alloc_decision = self._allocation_policy.evaluate(
            intent,
            broker,
            resolved_authority,
            artifact=artifact,
            entry_price=entry_price,
            quote_prices={intent.conid: entry_price} if entry_price is not None else None,
        )
        reasons.extend(alloc_decision.reason_codes)
        effective_gross = alloc_decision.effective_gross_ceiling
        authority_digest = alloc_decision.authority_digest
        limit_candidates = tuple(
            (c.name, c.max_gross_fraction) for c in alloc_decision.limit_candidates
        )

        if is_entry and entry_price is not None and equity > 0 and qty > 0:
            existing_position_value = abs(float(broker.position_value(intent.conid)))
            order_notional = float(qty) * entry_price
            post_position_value = existing_position_value + order_notional
            if post_position_value / equity > MAX_POSITION_FRACTION:
                reasons.append("POSITION_PCT")

            # Trade risk from broker-native stop distance (hard 0.20%; intent
            # risk_fraction may only tighten, never loosen).
            stop_distance = entry_price - stop if stop < entry_price else float("nan")
            if math.isfinite(stop_distance) and stop_distance > 0:
                trade_risk = (stop_distance * float(qty)) / equity
                intent_cap = float(intent.risk_fraction)
                effective_risk_cap = MAX_TRADE_RISK_FRACTION
                if 0 < intent_cap < MAX_TRADE_RISK_FRACTION:
                    effective_risk_cap = intent_cap
                if trade_risk > effective_risk_cap + 1e-15:
                    reasons.append("TRADE_RISK")
            equity_risk_fraction = (
                (stop_distance * float(qty)) / equity
                if math.isfinite(stop_distance) and stop_distance > 0
                else None
            )
        else:
            equity_risk_fraction = None

        # --- Liquidity -----------------------------------------------------
        if is_entry:
            evidence = _liquidity_from_context(session_state, approval_context)
            if evidence is None:
                reasons.append("LIQUIDITY_EVIDENCE_MISSING")
            else:
                liq = self._liquidity.evaluate(qty, evidence, now=now)
                if not liq.approved:
                    reasons.extend(liq.reason_codes)
                signals.extend(liq.breaker_signals)

        # Deduplicate reason codes while preserving order
        seen: set[str] = set()
        unique_reasons: list[str] = []
        for code in reasons:
            if code not in seen:
                seen.add(code)
                unique_reasons.append(code)

        unique_signals: list[BreakerSignal] = []
        signal_keys: set[tuple] = set()
        for signal in signals:
            key = (signal.kind, signal.detail, signal.key)
            if key not in signal_keys:
                signal_keys.add(key)
                unique_signals.append(signal)

        for signal in unique_signals:
            if self._breaker is not None:
                self._breaker.record(signal)

        approved = not unique_reasons
        return AutomatedRiskDecision(
            approved=approved,
            reason_codes=tuple(unique_reasons),
            approved_quantity=qty if approved else None,
            equity_risk_fraction=equity_risk_fraction if approved else equity_risk_fraction,
            breaker_signals=tuple(unique_signals),
            calendar_version=calendar_version,
            schedule=schedule,
            effective_gross_ceiling=effective_gross,
            authority_digest=authority_digest,
            allocation_limit_candidates=limit_candidates,
        )
