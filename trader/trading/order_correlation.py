"""Order identity and immutable alias correlation. [M1-F2]

An entity id is minted once.  Later IB identifiers are aliases only: they can
improve correlation but can never re-key an order or create another revision
stream for it.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional

from trader.data.broker_state import BrokerStateStore
from trader.domain.identity import external_order_entity_id, order_group_leg_entity_id


ORDER_REF_PREFIX = "mmr:"
_STOP_TYPES = {"STP", "STP LMT", "TRAIL", "TRAIL LIMIT"}
_UNSET_DOUBLE = 1.7976931348623157e308


def encode_order_ref(order_group_id: str) -> str:
    return f"{ORDER_REF_PREFIX}{order_group_id}"


def decode_order_ref(order_ref: Optional[str]) -> Optional[str]:
    if order_ref and order_ref.startswith(ORDER_REF_PREFIX):
        return order_ref[len(ORDER_REF_PREFIX):] or None
    return None


def classify_leg(order_type: str, parent_id: int, client_order_id: int) -> str:
    if not parent_id:
        return "entry"
    if order_type in _STOP_TYPES:
        return "stop"
    if order_type == "LMT":
        return "take_profit"
    return f"child-{client_order_id}"


def _price_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    numeric = float(value)
    return None if numeric == 0.0 or numeric >= _UNSET_DOUBLE else numeric


@dataclass(frozen=True)
class OrderObservation:
    account_id: str
    client_order_id: int
    perm_id: int
    parent_id: int
    order_ref: Optional[str]
    conid: int
    symbol: str
    action: str
    order_type: str
    total_quantity: float
    limit_price: Optional[float]
    stop_price: Optional[float]
    tif: Optional[str]
    status: str
    filled_quantity: float
    avg_fill_price: Optional[float]
    source_timestamp: dt.datetime


def normalize_open_order(trade: Any, now: dt.datetime) -> OrderObservation:
    order, status, contract = trade.order, trade.orderStatus, trade.contract
    return OrderObservation(
        account_id=order.account or "",
        client_order_id=int(order.orderId or 0),
        perm_id=int(order.permId or 0),
        parent_id=int(order.parentId or 0),
        order_ref=order.orderRef or None,
        conid=int(contract.conId),
        symbol=contract.symbol or "",
        action=order.action or "",
        order_type=order.orderType or "",
        total_quantity=float(order.totalQuantity or 0.0),
        limit_price=_price_or_none(order.lmtPrice),
        stop_price=_price_or_none(order.auxPrice),
        tif=order.tif or None,
        status=str(status.status or ""),
        filled_quantity=float(status.filled or 0.0),
        avg_fill_price=_price_or_none(status.avgFillPrice),
        source_timestamp=now,
    )


class OrderCorrelator:
    def __init__(self, store: BrokerStateStore, session_epoch: str):
        self.store = store
        self.session_epoch = session_epoch

    def resolve_in_tx(self, conn: Any, obs: OrderObservation) -> str:
        """Resolve an existing identity or mint one; this method does not write.

        Alias writes happen inside the caller's order mutation callback so an
        alias can never survive a failed order/journal transaction.
        """
        if obs.perm_id:
            entity = self.store.find_order_by_alias_in_tx(
                conn, "perm_id", str(obs.perm_id), obs.account_id, ""
            )
            if entity:
                return entity
        if obs.client_order_id:
            entity = self.store.find_order_by_alias_in_tx(
                conn,
                "client_order_id",
                str(obs.client_order_id),
                obs.account_id,
                self.session_epoch,
            )
            if entity:
                return entity
        group_id = decode_order_ref(obs.order_ref)
        if group_id is not None:
            return order_group_leg_entity_id(
                group_id, classify_leg(obs.order_type, obs.parent_id, obs.client_order_id)
            )
        return external_order_entity_id()

    def bind_aliases_in_tx(self, conn: Any, entity_id: str, obs: OrderObservation) -> None:
        bound_at = obs.source_timestamp
        if obs.perm_id:
            self.store.bind_alias_in_tx(
                conn, "perm_id", str(obs.perm_id), obs.account_id, "", entity_id, bound_at
            )
        if obs.client_order_id:
            self.store.bind_alias_in_tx(
                conn,
                "client_order_id",
                str(obs.client_order_id),
                obs.account_id,
                self.session_epoch,
                entity_id,
                bound_at,
            )
        if obs.parent_id:
            self.store.bind_alias_in_tx(
                conn,
                "parent_id",
                str(obs.parent_id),
                obs.account_id,
                self.session_epoch,
                entity_id,
                bound_at,
            )
        if obs.order_ref:
            self.store.bind_alias_in_tx(
                conn, "order_ref", obs.order_ref, obs.account_id, "", entity_id, bound_at
            )
