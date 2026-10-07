"""Immediate pre-dispatch revalidation for approved trading commands."""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trader.automation.risk_limits import PAPER_LIMITS, RiskLimits
from trader.data.broker_state import BrokerRiskSnapshotError
from trader.promotion.allocation_policy import AllocationPolicy
from trader.trading.command_policy import CommandAuthorityPolicy
from trader.trading.quote_feeds import LIVE_ONLY_FEEDS, require_live_feed_on_live_account
from trader.trading.trading_control import PauseStateUnavailable, TradingPausedError


MAX_QUOTE_AGE_SECONDS = 5.0
MAX_SOURCE_CLOCK_SKEW_SECONDS = 30.0

# Automated entries need executable live evidence at dispatch; manual paper proposals do not.
AUTOMATED_ENTRY_ACTIONS = frozenset({"execute_automated_intent", "submit_ai_paper_decision"})


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


@dataclass(frozen=True)
class _ApprovedCeilingAuthority:
    account_id: str
    account_mode: str
    artifact_digest: str
    max_gross_allocation: float
    authority_digest: Optional[str] = None
    stage: str = "CANARY"
    expires_at: dt.datetime = dt.datetime(2099, 1, 1, tzinfo=dt.timezone.utc)


def _direction(value: object) -> str:
    return str(getattr(value, "value", value))


def _working_order_fingerprint(snapshot) -> tuple:
    return tuple(
        (row.order_entity_id, row.status, row.total_quantity, row.filled_quantity)
        for row in snapshot.working_orders
    )


def _broker_counted_entry_quantity(entry, snapshot) -> float:
    """Unfilled shares of this entry that the snapshot's gross already counts.

    Only the BUY entry order itself counts: a working SELL stop of the same
    group adds nothing to gross. Mirrors ``compute_working_entry_notional``.
    """
    return sum(
        max(0.0, float(row.total_quantity) - float(row.filled_quantity))
        for row in snapshot.working_orders
        if not row.deleted
        and row.order_group_id == entry.order_group_id
        and row.leg == "entry"
        and str(row.action).upper() == "BUY"
    )


def _enumerated_after_fill(entry, snapshot) -> bool:
    started_at = snapshot.generation_started_at
    return (
        started_at is not None and entry.filled_at is not None and started_at > entry.filled_at
    )


def _fills_in_snapshot(entries, snapshot) -> set[str]:
    """Order groups whose filled shares the snapshot provably counts.

    A full enumeration that began after the fill counts whatever happened to
    the shares. Otherwise the position quantity must have grown by the fill
    from the quantity at send time. Each share of growth proves one fill
    only: entries on one conid claim it in order of their baseline, and a
    share already claimed is assumed not to be in a later baseline. That can
    over-reserve, never under-reserve. A timestamp alone proves nothing: a
    PnL-only update refreshes the position row without a new quantity.
    """
    proven: set[str] = set()
    claimed: dict[int, float] = defaultdict(float)
    filled = [entry for entry in entries if entry.filled_quantity > 0]
    for entry in filled:
        if _enumerated_after_fill(entry, snapshot):
            proven.add(entry.order_group_id)
            claimed[entry.conid] += entry.filled_quantity
    by_baseline = sorted(
        (
            entry for entry in filled
            if entry.order_group_id not in proven and entry.baseline_position is not None
        ),
        key=lambda entry: entry.baseline_position,
    )
    for entry in by_baseline:
        growth = (
            snapshot.reducible_quantity(entry.conid)
            - entry.baseline_position
            - claimed[entry.conid]
        )
        if growth >= entry.filled_quantity:
            proven.add(entry.order_group_id)
            claimed[entry.conid] += entry.filled_quantity
    return proven


def _unseen_in_flight_notional(evidence, snapshot) -> float:
    """Notional of in-flight entries that the broker snapshot does not count yet."""
    entries = evidence.in_flight_entries
    proven_fills = _fills_in_snapshot(entries, snapshot)
    total = 0.0
    for entry in entries:
        unseen = max(0.0, entry.unfilled_quantity - _broker_counted_entry_quantity(entry, snapshot))
        if entry.order_group_id not in proven_fills:
            unseen += entry.filled_quantity
        total += unseen * entry.limit_price
    return total


class DispatchGuard:
    def __init__(
        self, *, broker, quotes, margin, controls, risk_gate,
        policy: CommandAuthorityPolicy, account_id: str, account_mode: str,
        allocation_policy: Any = None,
        allocation_authority_lookup: Any = None,
        current_limits: Callable[[Any], RiskLimits] = lambda request: PAPER_LIMITS,
        ai_entry_gate: Callable[[Any, Any, Any, dt.datetime], Optional[str]] = lambda *args: None,
        strict_margin_actions: frozenset[str] = frozenset(),
        experiment_gate: Callable[[Any], Optional[str]] = lambda request: None,
        accepted_feeds: frozenset[str] = LIVE_ONLY_FEEDS,
    ):
        require_live_feed_on_live_account(account_mode, accepted_feeds)
        self._broker = broker
        self._quotes = quotes
        self._margin = margin
        self._controls = controls
        self._risk_gate = risk_gate
        self._policy = policy
        self._account_id = account_id
        self._account_mode = account_mode
        self._allocation_policy = allocation_policy
        self._allocation_authority_lookup = allocation_authority_lookup
        self._current_limits = current_limits
        self._ai_entry_gate = ai_entry_gate
        self._strict_margin_actions = frozenset(strict_margin_actions)
        # SP1 Plan 4 K20: an ai_paper entry admitted before a kill is refused here after it.
        self._experiment_gate = experiment_gate
        self._accepted_feeds = frozenset(accepted_feeds)

    def _limits_for(self, request) -> RiskLimits:
        try:
            return self._current_limits(request)
        except Exception as exc:
            raise DispatchGuardError("LIMITS_UNAVAILABLE", "current risk limits unavailable") from exc

    def _recheck_allocation(self, approved, request, current, price: float, automated: bool) -> None:
        evidence = getattr(approved, "allocation", None)
        if evidence is None:
            # Only automated entries carry allocation evidence. Manual paper
            # proposals have no allocation ceiling and are not re-checked.
            if automated:
                raise DispatchGuardError(
                    "ALLOCATION_EVIDENCE_MISSING", "allocation evidence is required for entries"
                )
            return
        limits_now = self._limits_for(request)
        try:
            in_flight_notional = _unseen_in_flight_notional(evidence, current)
            authority = self._active_authority(current.account_id, evidence)
            decision = self._allocation_policy.revalidate_dispatch(
                broker=current,
                approved_broker=approved.broker,
                conid=approved.conid,
                side=approved.side,
                quantity=abs(float(approved.quantity)),
                entry_price=max(price, float(evidence.entry_limit_price or 0.0)),
                authority=authority,
                artifact_max_gross=evidence.artifact_max_gross,
                artifact_digest=evidence.artifact_digest,
                authority_digest=evidence.authority_digest,
                effective_gross_ceiling=evidence.effective_gross_ceiling,
                in_flight_notional=in_flight_notional,
                risk_limits_gross=limits_now.gross_fraction,
            )
        except Exception as exc:
            raise DispatchGuardError(
                "ALLOCATION_EVIDENCE_UNAVAILABLE", "allocation re-check could not be evaluated"
            ) from exc
        if not decision.approved:
            code = decision.reason_codes[0] if decision.reason_codes else "GROSS_EXPOSURE"
            raise DispatchGuardError(code, "allocation policy rejected at dispatch")

    def _recheck_entry_limits(self, approved, request, current, price: float) -> None:
        """An ai_paper entry always re-checks loss and drawdown on the current broker P&L.

        Sizing and slot limits are re-checked only when a field got tighter since approval.
        """
        evidence = getattr(approved, "entry_limits", None)
        if evidence is None:
            return
        tight = evidence.limits.tighter(self._limits_for(request))
        from trader.automation.ai_paper_sizing import entry_limit_violations, loss_limit_breaches
        try:
            loss_breaches = loss_limit_breaches(tight, broker=current, evidence=evidence)
        except Exception as exc:
            raise DispatchGuardError("LIMITS_UNAVAILABLE", "loss limits re-check failed") from exc
        if loss_breaches:
            raise DispatchGuardError(loss_breaches[0], "loss limit breached at dispatch")
        if tight == evidence.limits:
            return
        try:
            violations = entry_limit_violations(
                tight, broker=current, conid=approved.conid, quantity=abs(float(approved.quantity)),
                price=price, evidence=evidence)
        except Exception as exc:
            raise DispatchGuardError("LIMITS_UNAVAILABLE", "entry limits re-check failed") from exc
        if violations:
            raise DispatchGuardError(violations[0], "limits tightened after approval")

    def _run_ai_entry_gate(self, request, approved, quote, now: dt.datetime) -> None:
        try:
            code = self._ai_entry_gate(request, approved, quote, now)
        except Exception as exc:
            raise DispatchGuardError("AI_ENTRY_GATE_UNAVAILABLE", "ai entry gate failed") from exc
        if code:
            raise DispatchGuardError(code, "ai_paper entry refused at dispatch")

    def _active_authority(self, account_id: str, evidence):
        authority = None
        if self._allocation_authority_lookup is not None:
            authority = self._allocation_authority_lookup(account_id, evidence.artifact_digest)
        if authority is None and self._account_mode != "live":
            # Paper has no signed authority: the trader-owned ceiling frozen at
            # approval is the baseline. A signed authority found now can only lower it.
            authority = _ApprovedCeilingAuthority(
                account_id=account_id,
                account_mode=self._account_mode,
                artifact_digest=evidence.artifact_digest,
                max_gross_allocation=float(evidence.effective_gross_ceiling),
            )
        return authority

    def revalidate(self, approved, request, now: dt.datetime) -> DispatchPermit:
        if request.account_id != self._account_id:
            raise DispatchGuardError("ACCOUNT_MISMATCH", "command account is not pinned account")
        try:
            experiment_code = self._experiment_gate(request)
        except Exception as exc:
            raise DispatchGuardError(
                "EXPERIMENT_STATE_UNAVAILABLE", "the experiment state is unreadable", retryable=True
            ) from exc
        if experiment_code:
            raise DispatchGuardError(experiment_code, "the experiment does not allow new exposure")
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

        # Automated paper entries require executable evidence too; the manual
        # paper proposal path may still use its documented delayed reference.
        automated = getattr(request, "action", None) in AUTOMATED_ENTRY_ACTIONS
        if self._account_mode == "live" or automated:
            if quote.feed_type not in self._accepted_feeds:
                raise DispatchGuardError("FEED_NOT_LIVE", "accepted feed required", retryable=True)
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
        # An ai_paper entry refuses a missing or invalid what-if; the old paper path only warns (R11).
        strict_margin = getattr(request, "action", None) in self._strict_margin_actions
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
        if strict_margin and margin is None:
            raise DispatchGuardError(
                "MARGIN_UNAVAILABLE", "margin what-if is required", retryable=True
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
                if strict_margin:
                    raise DispatchGuardError("MARGIN_INVALID", "margin what-if is invalid")
                margin = None
        if self._account_mode != "live" and margin is None:
            warnings = ("WHAT_IF_UNAVAILABLE_PAPER",)
        if margin is not None:
            leverage = self._risk_gate.check_leverage(margin, current.net_liquidation)
            if not leverage.approved:
                raise DispatchGuardError("LEVERAGE_REJECTED", str(leverage.reason))

        if (
            self._allocation_policy is not None
            and _direction(approved.risk_direction) != "REDUCING"
        ):
            self._recheck_allocation(approved, request, current, price, automated)
        # Reductions returned above, so both run for every entry.
        self._recheck_entry_limits(approved, request, current, price)
        self._run_ai_entry_gate(request, approved, quote, now)

        return DispatchPermit(
            generation_id=current.generation_id,
            source_cursor=current.source_cursor,
            quote_timestamp=quote.market_timestamp,
            what_if_timestamp=(now if margin is not None else None),
            warnings=warnings,
        )
