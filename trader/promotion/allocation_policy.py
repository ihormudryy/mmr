"""P5 Task 2 — enforce signed gross-allocation ladder at command time."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple, Union

from trader.automation.artifact_verifier import VerifiedArtifact
from trader.automation.models import ExecutionIntent
from trader.data.allocation_authority_store import AllocationAuthorityRecord
from trader.data.broker_state import BrokerOrderRow, BrokerRiskSnapshot
from trader.promotion.allocation_attestation import (
    STAGE_STEADY,
    STAGE_MAX_CEILING,
    VerifiedAllocationAuthority,
)

# Trader-owned hard ceilings — never loosened by request/artifact/signed authority.
STEADY_MAX_GROSS_FRACTION = float(STAGE_MAX_CEILING[STAGE_STEADY])  # 15%
MAX_POSITIONS = 3
MAX_POSITION_FRACTION = 0.05
MAX_TRADE_RISK_FRACTION = 0.002
MAX_DAILY_LOSS_FRACTION = 0.005

AuthoritySource = Union[VerifiedAllocationAuthority, AllocationAuthorityRecord, None]


@dataclass(frozen=True)
class AllocationLimitCandidate:
    name: str
    max_gross_fraction: float


@dataclass(frozen=True)
class AllocationDecision:
    approved: bool
    reason_codes: Tuple[str, ...]
    effective_gross_ceiling: float
    authority_digest: Optional[str]
    limit_candidates: Tuple[AllocationLimitCandidate, ...]
    current_gross_fraction: float
    projected_gross_fraction: Optional[float]
    broker_generation_id: int
    broker_source_cursor: int


def _finite(value: float) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _authority_fields(authority: AuthoritySource) -> Optional[dict[str, Any]]:
    if authority is None:
        return None
    if isinstance(authority, VerifiedAllocationAuthority):
        return {
            "authority_digest": authority.payload_digest,
            "account_id": authority.account_id,
            "account_mode": authority.account_mode,
            "artifact_digest": authority.artifact_digest,
            "max_gross_allocation": float(authority.max_gross_allocation),
            "expires_at": authority.expires_at,
            "stage": authority.stage,
        }
    return {
        "authority_digest": authority.authority_digest,
        "account_id": authority.account_id,
        "account_mode": authority.account_mode,
        "artifact_digest": authority.artifact_digest,
        "max_gross_allocation": float(authority.max_gross_allocation),
        "expires_at": authority.expires_at,
        "stage": authority.stage,
    }


def resolve_effective_gross_ceiling(
    *,
    artifact_max_gross: float,
    authority: AuthoritySource,
    account_mode: str,
    account_id: str,
    artifact_digest: str,
    now: dt.datetime,
) -> tuple[float, Tuple[AllocationLimitCandidate, ...], Optional[str], Tuple[str, ...]]:
    """Most-restrictive-wins gross ceiling + audit candidates."""
    reasons: list[str] = []
    candidates: list[AllocationLimitCandidate] = [
        AllocationLimitCandidate("trader_steady_cap", STEADY_MAX_GROSS_FRACTION),
        AllocationLimitCandidate("artifact", float(artifact_max_gross)),
    ]

    auth = _authority_fields(authority)
    authority_digest: Optional[str] = None

    if account_mode == "live":
        if auth is None:
            reasons.append("ALLOCATION_AUTHORITY_ABSENT")
        else:
            authority_digest = auth["authority_digest"]
            candidates.append(
                AllocationLimitCandidate("signed_authority", float(auth["max_gross_allocation"]))
            )
            candidates.append(
                AllocationLimitCandidate(
                    f"stage_{auth['stage'].lower()}",
                    float(STAGE_MAX_CEILING.get(auth["stage"], auth["max_gross_allocation"])),
                )
            )
            if auth["account_id"] != account_id:
                reasons.append("ALLOCATION_AUTHORITY_MISMATCH")
            if auth["artifact_digest"] != artifact_digest:
                reasons.append("ALLOCATION_AUTHORITY_MISMATCH")
            if _as_utc(auth["expires_at"]) <= _as_utc(now):
                reasons.append("ALLOCATION_AUTHORITY_EXPIRED")
    elif auth is not None:
        authority_digest = auth["authority_digest"]
        candidates.append(
            AllocationLimitCandidate("signed_authority", float(auth["max_gross_allocation"]))
        )

    effective = min(c.max_gross_fraction for c in candidates)
    if effective <= 0 or not _finite(effective):
        reasons.append("GROSS_CEILING_INVALID")
        effective = 0.0

    return effective, tuple(candidates), authority_digest, tuple(reasons)


def _order_reference_price(
    order: BrokerOrderRow,
    *,
    quote_prices: Mapping[int, float],
) -> Optional[float]:
    for candidate in (order.limit_price, order.stop_price):
        if candidate is not None and _finite(float(candidate)) and float(candidate) > 0:
            return float(candidate)
    return quote_prices.get(order.conid)


def compute_position_gross_notional(broker: BrokerRiskSnapshot) -> float:
    return sum(
        abs(float(row.market_value or 0.0))
        for row in broker.positions
        if not row.deleted
    )


def compute_working_entry_notional(
    broker: BrokerRiskSnapshot,
    *,
    quote_prices: Optional[Mapping[int, float]] = None,
    exclude_order_entity_id: Optional[str] = None,
) -> tuple[float, Tuple[str, ...]]:
    """Worst-case notional for unfilled BUY working orders (pending gross)."""
    prices = quote_prices or {}
    reasons: list[str] = []
    total = 0.0
    for order in broker.working_orders:
        if order.deleted:
            continue
        if exclude_order_entity_id is not None and order.order_entity_id == exclude_order_entity_id:
            continue
        remaining = max(0.0, float(order.total_quantity) - float(order.filled_quantity))
        if remaining <= 0:
            continue
        if str(order.action).upper() != "BUY":
            continue
        price = _order_reference_price(order, quote_prices=prices)
        if price is None:
            reasons.append("WORKING_ORDER_PRICE_UNKNOWN")
            continue
        total += remaining * price
    return total, tuple(reasons)


def compute_gross_notional(
    broker: BrokerRiskSnapshot,
    *,
    quote_prices: Optional[Mapping[int, float]] = None,
    exclude_order_entity_id: Optional[str] = None,
) -> tuple[float, Tuple[str, ...]]:
    positions = compute_position_gross_notional(broker)
    pending, reasons = compute_working_entry_notional(
        broker,
        quote_prices=quote_prices,
        exclude_order_entity_id=exclude_order_entity_id,
    )
    return positions + pending, reasons


class AllocationPolicy:
    """Evaluate gross exposure against signed allocation authority."""

    def __init__(self, *, now: Optional[Callable[[], dt.datetime]] = None):
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def evaluate(
        self,
        intent: ExecutionIntent,
        broker: BrokerRiskSnapshot,
        authority: AuthoritySource,
        *,
        artifact: VerifiedArtifact,
        entry_price: Optional[float] = None,
        quote_prices: Optional[Mapping[int, float]] = None,
        exclude_working_order_id: Optional[str] = None,
    ) -> AllocationDecision:
        now = self._now()
        is_entry = intent.side == "BUY"
        equity = float(broker.net_liquidation)
        reasons: list[str] = []

        effective, candidates, authority_digest, auth_reasons = resolve_effective_gross_ceiling(
            artifact_max_gross=float(artifact.max_gross_allocation),
            authority=authority,
            account_mode=broker.account_mode,
            account_id=broker.account_id,
            artifact_digest=artifact.artifact_id,
            now=now,
        )
        reasons.extend(auth_reasons)

        if not _finite(equity) or equity <= 0:
            reasons.append("EQUITY_INVALID")
            return AllocationDecision(
                approved=False,
                reason_codes=tuple(dict.fromkeys(reasons)),
                effective_gross_ceiling=effective,
                authority_digest=authority_digest,
                limit_candidates=candidates,
                current_gross_fraction=0.0,
                projected_gross_fraction=None,
                broker_generation_id=broker.generation_id,
                broker_source_cursor=broker.source_cursor,
            )

        prices = dict(quote_prices or ())
        if entry_price is not None and _finite(entry_price):
            prices[intent.conid] = float(entry_price)

        current_notional, pending_reasons = compute_gross_notional(
            broker,
            quote_prices=prices,
            exclude_order_entity_id=exclude_working_order_id,
        )
        reasons.extend(pending_reasons)
        current_frac = current_notional / equity

        projected_frac: Optional[float] = None
        if is_entry:
            if entry_price is None or not _finite(entry_price) or float(entry_price) <= 0:
                reasons.append("ENTRY_PRICE_REQUIRED")
            else:
                qty = float(intent.requested_quantity or 0)
                if qty <= 0:
                    reasons.append("QUANTITY_REQUIRED")
                else:
                    incremental = qty * float(entry_price)
                    projected_notional = current_notional + incremental
                    projected_frac = projected_notional / equity
                    if projected_frac > effective + 1e-15:
                        reasons.append("GROSS_EXPOSURE")

        unique = tuple(dict.fromkeys(reasons))
        return AllocationDecision(
            approved=not unique,
            reason_codes=unique,
            effective_gross_ceiling=effective,
            authority_digest=authority_digest,
            limit_candidates=candidates,
            current_gross_fraction=current_frac,
            projected_gross_fraction=projected_frac,
            broker_generation_id=broker.generation_id,
            broker_source_cursor=broker.source_cursor,
        )

    def revalidate_dispatch(
        self,
        *,
        broker: BrokerRiskSnapshot,
        approved_broker: BrokerRiskSnapshot,
        conid: int,
        side: str,
        quantity: float,
        entry_price: float,
        authority: AuthoritySource,
        artifact_max_gross: float,
        artifact_digest: str,
        authority_digest: Optional[str],
        effective_gross_ceiling: float,
    ) -> AllocationDecision:
        """Immediate pre-dispatch gross re-check against a fresh broker snapshot."""
        reasons: list[str] = []
        if broker.generation_id < approved_broker.generation_id:
            reasons.append("BROKER_GENERATION_STALE")
        if broker.source_cursor < approved_broker.source_cursor:
            reasons.append("BROKER_CURSOR_STALE")

        now = self._now()
        effective, candidates, resolved_digest, auth_reasons = resolve_effective_gross_ceiling(
            artifact_max_gross=float(artifact_max_gross),
            authority=authority,
            account_mode=broker.account_mode,
            account_id=broker.account_id,
            artifact_digest=artifact_digest,
            now=now,
        )
        reasons.extend(auth_reasons)
        if authority_digest is not None and resolved_digest != authority_digest:
            reasons.append("ALLOCATION_AUTHORITY_CHANGED")
        if effective > effective_gross_ceiling + 1e-15:
            reasons.append("ALLOCATION_CEILING_TIGHTENED")
        elif effective + 1e-15 < effective_gross_ceiling:
            # Signed authority may only tighten between approval and dispatch.
            effective = effective_gross_ceiling

        equity = float(broker.net_liquidation)
        if not _finite(equity) or equity <= 0:
            reasons.append("EQUITY_INVALID")
            return AllocationDecision(
                approved=False,
                reason_codes=tuple(dict.fromkeys(reasons)),
                effective_gross_ceiling=effective,
                authority_digest=resolved_digest or authority_digest,
                limit_candidates=candidates,
                current_gross_fraction=0.0,
                projected_gross_fraction=None,
                broker_generation_id=broker.generation_id,
                broker_source_cursor=broker.source_cursor,
            )

        prices = {conid: float(entry_price)}
        current_notional, pending_reasons = compute_gross_notional(broker, quote_prices=prices)
        reasons.extend(pending_reasons)
        current_frac = current_notional / equity

        projected_frac: Optional[float] = None
        if str(side).upper() == "BUY":
            incremental = abs(float(quantity)) * float(entry_price)
            projected_notional = current_notional + incremental
            projected_frac = projected_notional / equity
            if projected_frac > effective + 1e-15:
                reasons.append("GROSS_EXPOSURE")

        unique = tuple(dict.fromkeys(reasons))
        return AllocationDecision(
            approved=not unique,
            reason_codes=unique,
            effective_gross_ceiling=effective,
            authority_digest=resolved_digest or authority_digest,
            limit_candidates=candidates,
            current_gross_fraction=current_frac,
            projected_gross_fraction=projected_frac,
            broker_generation_id=broker.generation_id,
            broker_source_cursor=broker.source_cursor,
        )
