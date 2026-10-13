"""How approving a pending proposal moves the book (AUDIT_ROADMAP H2), for the command-center proposal card.

The trader computes it with the dashboard risk projection, from the same broker snapshot, so it follows the book
(#169). Weights follow PortfolioRiskAnalyzer: a symbol's weight is its absolute market value over net liquidation,
and a group counts its member symbols' absolute market values.

Owner ruling (2026-10-13): every number is in one currency. An impact is computed only when the account's net
liquidation is in USD, the instrument is known to be USD and every position it counts is USD. Otherwise the row
names why it has none; the card never shows an approximate number.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

from trader.trading.portfolio_risk import budget_pct

IMPACT_CURRENCY = "USD"


class ImpactUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Book:
    positions: Sequence[Any]          # BrokerPositionRow-like: conid, symbol, currency, market_value
    net_liquidation: float
    groups: Sequence[Any]             # PositionGroup-like: name, members, max_allocation_pct


def proposal_impacts(
    proposals: Iterable[Any],
    *,
    positions: Sequence[Any],
    net_liquidation: float,
    base_currency: Optional[str],
    groups: Sequence[Any],
    instrument_currency: Callable[[int], Optional[str]],
) -> list[dict]:
    """One row per proposal: its impact, or ``{"proposal_id", "unavailable": reason}``."""
    book = _Book(tuple(positions), float(net_liquidation), tuple(groups))
    rows = []
    for proposal in proposals:
        try:
            _require_usd_account(base_currency, book.net_liquidation)
            rows.append(_impact(proposal, book, instrument_currency))
        except ImpactUnavailable as unavailable:
            rows.append({"proposal_id": proposal.id, "unavailable": unavailable.reason})
    return rows


def _require_usd_account(base_currency: Optional[str], net_liquidation: float) -> None:
    if base_currency is None:
        raise ImpactUnavailable("ACCOUNT_CURRENCY_UNKNOWN")
    if base_currency != IMPACT_CURRENCY:
        raise ImpactUnavailable("ACCOUNT_NOT_USD")
    if not math.isfinite(net_liquidation) or net_liquidation <= 0:
        raise ImpactUnavailable("NO_NET_LIQUIDATION")


def _impact(proposal: Any, book: _Book, instrument_currency: Callable[[int], Optional[str]]) -> dict:
    if (proposal.sec_type or "STK") != "STK":
        raise ImpactUnavailable("NOT_A_STOCK")             # an option or future needs its multiplier
    if proposal.conid is None:
        raise ImpactUnavailable("NO_CONID")
    held = [row for row in book.positions if row.conid == proposal.conid]
    if any(row.symbol != proposal.symbol for row in held):
        raise ImpactUnavailable("SYMBOL_MISMATCH")         # groups count by symbol, positions match by conid
    _require_usd_instrument(proposal.conid, held, instrument_currency)
    change = _signed_fill_value(proposal)
    before = sum(float(row.market_value or 0.0) for row in held)
    after = before + change
    return {
        "proposal_id": proposal.id,
        "currency": IMPACT_CURRENCY,
        "fill_value": round(abs(change), 2),
        "symbol_before_pct": _pct(abs(before), book),
        "symbol_after_pct": _pct(abs(after), book),
        "groups": [_group_impact(group, proposal, before, after, book) for group in book.groups
                   if proposal.symbol in group.members],
        "gross_delta": round(abs(after) - abs(before), 2),
    }


def _require_usd_instrument(conid: int, held: Sequence[Any], instrument_currency: Callable[[int], Optional[str]]
                            ) -> None:
    currencies = {row.currency for row in held} or {instrument_currency(conid)}
    if None in currencies or "" in currencies:
        raise ImpactUnavailable("INSTRUMENT_CURRENCY_UNKNOWN")
    if currencies != {IMPACT_CURRENCY}:
        raise ImpactUnavailable("INSTRUMENT_NOT_USD")


def _signed_fill_value(proposal: Any) -> float:
    if proposal.quantity is not None and proposal.reference_price is not None:
        value = float(proposal.quantity) * float(proposal.reference_price)
    elif proposal.quantity is None and proposal.amount is not None:
        value = float(proposal.amount)
    else:
        raise ImpactUnavailable("NO_PRICE")
    if not math.isfinite(value) or value <= 0:
        raise ImpactUnavailable("NO_PRICE")
    if proposal.action == "BUY":
        return value
    if proposal.action == "SELL":
        return -value
    raise ImpactUnavailable("UNKNOWN_SIDE")


def _group_impact(group: Any, proposal: Any, before: float, after: float, book: _Book) -> dict:
    members = [row for row in book.positions if row.symbol in group.members]
    if any(row.currency != IMPACT_CURRENCY for row in members):
        raise ImpactUnavailable("MIXED_CURRENCY")
    others = sum(abs(float(row.market_value or 0.0)) for row in members if row.conid != proposal.conid)
    return {
        "name": group.name,
        "before_pct": _pct(others + abs(before), book),
        "after_pct": _pct(others + abs(after), book),
        "budget_pct": budget_pct(group.max_allocation_pct),
    }


def _pct(value: float, book: _Book) -> float:
    return round(value / book.net_liquidation * 100, 2)
