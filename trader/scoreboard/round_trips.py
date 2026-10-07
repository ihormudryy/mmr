"""Round trips rebuilt from broker fills (Plan 5 ruling 7).

Each conid is walked in ``(fill_time, exec_id)`` order with an average-cost
position. A trip opens when the position leaves zero and closes when it
returns to zero. A fill that crosses zero is split into a closing piece and an
opening piece; its commission is split pro rata by quantity.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence

from trader.scoreboard.ports import AttributionLinks, session_date_et

ZERO = Decimal(0)
SIDES = ("BUY", "SELL")


class ProjectionError(ValueError):
    """A fill that cannot be projected; never skipped silently."""


@dataclass(frozen=True)
class FillFact:
    exec_id: str
    conid: int
    side: str
    quantity: Decimal
    price: Decimal
    commission: Optional[Decimal]   # None: unknown, or not in USD
    fill_time: dt.datetime
    order_ref: Optional[str] = None
    symbol: Optional[str] = None


@dataclass(frozen=True)
class Piece:
    exec_id: str
    conid: int
    session_date: dt.date
    realized: Decimal
    fee: Optional[Decimal]
    closes_trip: bool


@dataclass(frozen=True)
class RoundTrip:
    round_trip_id: str
    conid: int
    symbol: Optional[str]
    direction: str
    status: str
    opened_at: dt.datetime
    closed_at: Optional[dt.datetime]
    opened_session: dt.date
    closed_session: Optional[dt.date]
    entry_qty: float
    exit_qty: float
    entry_avg: Optional[float]
    exit_avg: Optional[float]
    gross_pnl_usd: float
    fees_usd: Optional[float]
    net_pnl_usd: Optional[float]
    fees_complete: bool
    notional_traded_usd: float
    strategy_version: Optional[str]
    decider: Optional[str]
    policy_revision: Optional[str]
    style: Optional[str]
    decision_id: Optional[str]
    links_digest: Optional[str]
    exec_ids: tuple[str, ...]
    fills_digest: str

    def as_row(self, experiment_id: str, account_id: str) -> dict:
        row = {name: getattr(self, name) for name in self.__dataclass_fields__}
        row["exec_ids"] = json.dumps(list(self.exec_ids))
        return {"experiment_id": experiment_id, "account_id": account_id, **row}


@dataclass(frozen=True)
class Projection:
    trips: tuple[RoundTrip, ...]
    pieces: tuple[Piece, ...]


def fills_digest(fills: Sequence[FillFact]) -> str:
    """Over the immutable fill fields; the commission is left out (it may arrive late, ruling 10)."""
    items = sorted((f.exec_id, f.side, str(f.quantity), str(f.price),
                    f.fill_time.astimezone(dt.timezone.utc).isoformat()) for f in fills)
    return hashlib.sha256(json.dumps(items, separators=(",", ":")).encode()).hexdigest()


def _validate(fill: FillFact) -> None:
    if fill.side not in SIDES:
        raise ProjectionError(f"fill {fill.exec_id}: side must be BUY or SELL, got {fill.side!r}")
    if not isinstance(fill.quantity, Decimal) or not fill.quantity.is_finite() or fill.quantity <= 0:
        raise ProjectionError(f"fill {fill.exec_id}: quantity must be positive, got {fill.quantity!r}")
    if not isinstance(fill.price, Decimal) or not fill.price.is_finite() or fill.price <= 0:
        raise ProjectionError(f"fill {fill.exec_id}: price must be positive, got {fill.price!r}")
    if fill.commission is not None and not fill.commission.is_finite():
        raise ProjectionError(f"fill {fill.exec_id}: commission must be finite")
    if not isinstance(fill.fill_time, dt.datetime) or fill.fill_time.tzinfo is None:
        raise ProjectionError(f"fill {fill.exec_id}: fill_time must be timezone-aware")


@dataclass
class _TripBuilder:
    first: FillFact
    direction: str
    account_id: str
    entry_qty: Decimal = ZERO
    entry_cost: Decimal = ZERO
    exit_qty: Decimal = ZERO
    exit_value: Decimal = ZERO
    gross: Decimal = ZERO
    fees: list = field(default_factory=list)
    fills: dict = field(default_factory=dict)
    last: Optional[FillFact] = None

    def add_entry(self, fill: FillFact, size: Decimal, fee: Optional[Decimal]) -> None:
        self.entry_qty += size
        self.entry_cost += size * fill.price
        self._touch(fill, fee)

    def add_exit(self, fill: FillFact, size: Decimal, realized: Decimal, fee: Optional[Decimal]) -> None:
        self.exit_qty += size
        self.exit_value += size * fill.price
        self.gross += realized
        self._touch(fill, fee)

    def _touch(self, fill: FillFact, fee: Optional[Decimal]) -> None:
        self.fees.append(fee)
        self.fills.setdefault(fill.exec_id, fill)
        self.last = fill

    def build(self, status: str, links_for: Callable[[str], Optional[AttributionLinks]]) -> RoundTrip:
        links = links_for(self.first.order_ref) if self.first.order_ref else None
        complete = all(fee is not None for fee in self.fees)
        fees = sum(self.fees, ZERO) if complete else None
        closed = status == "CLOSED"
        net = self.gross - fees if closed and fees is not None else None
        seed = f"{self.account_id}|{self.first.conid}|{self.first.exec_id}"
        return RoundTrip(
            round_trip_id=hashlib.sha256(seed.encode()).hexdigest()[:32],
            conid=self.first.conid,
            symbol=next((f.symbol for f in self.fills.values() if f.symbol), None),
            direction=self.direction,
            status=status,
            opened_at=self.first.fill_time,
            closed_at=self.last.fill_time if closed else None,
            opened_session=session_date_et(self.first.fill_time),
            closed_session=session_date_et(self.last.fill_time) if closed else None,
            entry_qty=float(self.entry_qty),
            exit_qty=float(self.exit_qty),
            entry_avg=float(self.entry_cost / self.entry_qty),
            exit_avg=float(self.exit_value / self.exit_qty) if self.exit_qty else None,
            gross_pnl_usd=float(self.gross),
            fees_usd=None if fees is None else float(fees),
            net_pnl_usd=None if net is None else float(net),
            fees_complete=complete,
            notional_traded_usd=float(self.entry_cost + self.exit_value),
            strategy_version=None if links is None else links.strategy_version,
            decider=None if links is None else links.decider,
            policy_revision=None if links is None else links.policy_revision,
            style=None if links is None else links.style,
            decision_id=None if links is None else links.decision_id,
            links_digest=None if links is None else links.digest,
            exec_ids=tuple(self.fills),
            fills_digest=fills_digest(list(self.fills.values())),
        )


def project_round_trips(fills: Sequence[FillFact], *,
                        links_for: Callable[[str], Optional[AttributionLinks]],
                        account_id: str = "") -> Projection:
    by_conid: dict[int, list[FillFact]] = defaultdict(list)
    seen: set[str] = set()
    for fill in fills:
        _validate(fill)
        if fill.exec_id in seen:
            raise ProjectionError(f"exec_id {fill.exec_id} appears twice")
        seen.add(fill.exec_id)
        by_conid[fill.conid].append(fill)
    trips: list[RoundTrip] = []
    pieces: list[Piece] = []
    for conid in sorted(by_conid):
        position = average = ZERO
        trip: Optional[_TripBuilder] = None
        for fill in sorted(by_conid[conid], key=lambda f: (f.fill_time, f.exec_id)):
            left = fill.quantity if fill.side == "BUY" else -fill.quantity
            while left != 0:
                opening = position == 0 or (position > 0) == (left > 0)
                size = abs(left) if opening else min(abs(left), abs(position))
                fee = None if fill.commission is None else fill.commission * size / fill.quantity
                if opening:
                    trip = trip or _TripBuilder(fill, "LONG" if left > 0 else "SHORT", account_id)
                    average = (abs(position) * average + size * fill.price) / (abs(position) + size)
                    position += size if left > 0 else -size
                    realized = ZERO
                    trip.add_entry(fill, size, fee)
                else:
                    direction = 1 if position > 0 else -1
                    realized = (fill.price - average) * size * direction
                    position -= direction * size
                    trip.add_exit(fill, size, realized, fee)
                left += -size if left > 0 else size
                closes = not opening and position == 0
                pieces.append(Piece(fill.exec_id, conid, session_date_et(fill.fill_time), realized, fee, closes))
                if closes:
                    trips.append(trip.build("CLOSED", links_for))
                    trip, average = None, ZERO
        if trip is not None:
            trips.append(trip.build("OPEN", links_for))
    return Projection(tuple(trips), tuple(pieces))


def load_fill_facts(db: Any, account_id: str, since: dt.datetime) -> list[FillFact]:
    """Fills of the account from ``since``, with the order ref and symbol of their order.

    The commission is kept only when it is reported in USD (ruling 8).
    """
    rows = db.execute(
        "SELECT f.exec_id, f.conid, f.side, f.quantity, f.price, f.commission, f.commission_currency, "
        "f.fill_time, (SELECT MIN(a.alias_value) FROM broker_order_aliases a "
        "  WHERE a.order_entity_id = f.order_entity_id AND a.alias_type = 'order_ref'), o.symbol "
        "FROM broker_fills f LEFT JOIN broker_orders o ON o.order_entity_id = f.order_entity_id "
        "WHERE f.account_id = ? AND f.fill_time >= ? ORDER BY f.fill_time, f.exec_id",
        [account_id, since], fetch="all")
    facts = []
    for exec_id, conid, side, quantity, price, commission, currency, fill_time, ref, symbol in rows:
        usd_fee = None if commission is None or currency != "USD" else Decimal(str(commission))
        facts.append(FillFact(exec_id=exec_id, conid=int(conid), side=side, quantity=Decimal(str(quantity)),
                              price=Decimal(str(price)), commission=usd_fee, fill_time=fill_time,
                              order_ref=ref, symbol=symbol))
    return facts
