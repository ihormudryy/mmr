"""Broker producers — normalize IB callbacks into revisioned domain events.

IB eventkit callbacks only normalize and enqueue observations.  A single writer
thread owns state application; each materialized write is performed inside the
transaction self-managed by ``DomainJournal.mutate``.
"""
from __future__ import annotations

import datetime as dt
import queue
import threading
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from trader.data.broker_state import (
    BrokerAccountRow,
    BrokerOrderRow,
    BrokerPositionRow,
    BrokerStateStore,
)
from trader.domain.events import DomainMutation
from trader.domain.identity import position_entity_id
from trader.trading.order_correlation import (
    OrderCorrelator,
    OrderObservation,
    classify_leg,
    decode_order_ref,
    normalize_open_order,
)


_UNSET_DOUBLE = 1.7976931348623157e308
_ACCOUNT_TAG_COLUMNS = {
    "NetLiquidation": "net_liquidation",
    "TotalCashValue": "total_cash",
    "BuyingPower": "buying_power",
    "AvailableFunds": "available_funds",
    "MaintMarginReq": "maintenance_margin",
}


def _none_if_unset(value: Any) -> Optional[float]:
    if value is None:
        return None
    numeric = float(value)
    if numeric == 0.0 or numeric >= _UNSET_DOUBLE:
        return None
    return numeric


@dataclass(frozen=True)
class AccountValueObservation:
    account_id: str
    tag: str
    currency: str
    value: str
    source_timestamp: dt.datetime


@dataclass(frozen=True)
class PositionObservation:
    account_id: str
    conid: int
    symbol: str
    sec_type: str
    exchange: Optional[str]
    currency: str
    quantity: float
    average_cost: Optional[float]
    market_price: Optional[float]
    market_value: Optional[float]
    unrealized_pnl: Optional[float]
    realized_pnl: Optional[float]
    daily_pnl: Optional[float]
    source_timestamp: dt.datetime


def normalize_account_value(av: Any, now: dt.datetime) -> AccountValueObservation:
    return AccountValueObservation(
        account_id=av.account,
        tag=av.tag,
        currency=av.currency or "",
        value=str(av.value),
        source_timestamp=now,
    )


def normalize_position(pos: Any, now: dt.datetime) -> PositionObservation:
    contract = pos.contract
    return PositionObservation(
        account_id=pos.account,
        conid=int(contract.conId),
        symbol=contract.symbol or "",
        sec_type=contract.secType or "",
        exchange=contract.exchange or None,
        currency=contract.currency or "",
        quantity=float(pos.position),
        average_cost=float(pos.avgCost) if pos.avgCost else None,
        market_price=None,
        market_value=None,
        unrealized_pnl=None,
        realized_pnl=None,
        daily_pnl=None,
        source_timestamp=now,
    )


def normalize_portfolio_item(item: Any, now: dt.datetime) -> PositionObservation:
    contract = item.contract
    return PositionObservation(
        account_id=item.account,
        conid=int(contract.conId),
        symbol=contract.symbol or "",
        sec_type=contract.secType or "",
        exchange=contract.exchange or None,
        currency=contract.currency or "",
        quantity=float(item.position),
        average_cost=float(item.averageCost) if item.averageCost else None,
        market_price=_none_if_unset(item.marketPrice),
        market_value=_none_if_unset(item.marketValue),
        unrealized_pnl=float(item.unrealizedPNL) if item.unrealizedPNL is not None else None,
        realized_pnl=float(item.realizedPNL) if item.realizedPNL is not None else None,
        daily_pnl=None,
        source_timestamp=now,
    )


def merge_account_value(
    current: Optional[BrokerAccountRow], obs: AccountValueObservation, account_mode: str
) -> BrokerAccountRow:
    balances = dict(current.balances) if current else {}
    balances[f"{obs.tag}:{obs.currency}"] = obs.value
    fields = {
        "net_liquidation": current.net_liquidation if current else None,
        "total_cash": current.total_cash if current else None,
        "buying_power": current.buying_power if current else None,
        "available_funds": current.available_funds if current else None,
        "maintenance_margin": current.maintenance_margin if current else None,
    }
    column = _ACCOUNT_TAG_COLUMNS.get(obs.tag)
    if column and obs.currency in ("", "BASE", "USD", "AUD"):
        try:
            fields[column] = float(obs.value)
        except ValueError:
            pass
    return BrokerAccountRow(
        account_id=obs.account_id,
        account_mode=account_mode,
        balances=balances,
        revision=current.revision if current else 0,
        source_timestamp=obs.source_timestamp,
        **fields,
    )


def merge_position(
    current: Optional[BrokerPositionRow], obs: PositionObservation
) -> BrokerPositionRow:
    def keep(observed: Optional[float | str], existing: Optional[float | str]) -> Optional[float | str]:
        return observed if observed is not None else existing

    prior = current if current is not None and not current.deleted else None
    return BrokerPositionRow(
        account_id=obs.account_id,
        conid=obs.conid,
        symbol=obs.symbol,
        sec_type=obs.sec_type,
        exchange=keep(obs.exchange, prior.exchange if prior else None),
        currency=obs.currency,
        quantity=obs.quantity,
        average_cost=keep(obs.average_cost, prior.average_cost if prior else None),
        market_price=keep(obs.market_price, prior.market_price if prior else None),
        market_value=keep(obs.market_value, prior.market_value if prior else None),
        unrealized_pnl=keep(obs.unrealized_pnl, prior.unrealized_pnl if prior else None),
        realized_pnl=keep(obs.realized_pnl, prior.realized_pnl if prior else None),
        daily_pnl=keep(obs.daily_pnl, prior.daily_pnl if prior else None),
        deleted=False,
        revision=current.revision if current else 0,
        source_timestamp=obs.source_timestamp,
    )


class BrokerIngest:
    def __init__(
        self,
        db: Any,
        journal: Any,
        store: BrokerStateStore,
        account_id: str,
        account_mode: str,
        session_epoch: Optional[str] = None,
        clock: Optional[Callable[[], dt.datetime]] = None,
    ):
        self.db = db
        self.journal = journal
        self.store = store
        self.account_id = account_id
        self.account_mode = account_mode
        self.session_epoch = session_epoch or uuid.uuid4().hex
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._queue: queue.Queue[AccountValueObservation | PositionObservation | OrderObservation] = queue.Queue()
        self._ingest_seq = 0
        self._generation: Optional[int] = None
        self._apply_lock = threading.Lock()
        self._stop = threading.Event()
        self._writer: Optional[threading.Thread] = None
        self.correlator = OrderCorrelator(store, self.session_epoch)

    def on_account_value(self, account_value: Any) -> None:
        observation = normalize_account_value(account_value, self.clock())
        if observation.account_id == self.account_id:
            self._queue.put(observation)

    def on_position(self, position: Any) -> None:
        observation = normalize_position(position, self.clock())
        if observation.account_id == self.account_id:
            self._queue.put(observation)

    def on_portfolio_item(self, item: Any) -> None:
        observation = normalize_portfolio_item(item, self.clock())
        if observation.account_id == self.account_id:
            self._queue.put(observation)

    def on_open_order(self, trade: Any) -> None:
        observation = normalize_open_order(trade, self.clock())
        if observation.account_id == self.account_id:
            self._queue.put(observation)

    def on_order_status(self, trade: Any) -> None:
        observation = normalize_open_order(trade, self.clock())
        if observation.account_id == self.account_id:
            self._queue.put(observation)

    def start(self) -> None:
        if self._writer is not None and self._writer.is_alive():
            return
        self._stop.clear()
        self._writer = threading.Thread(target=self._drain_loop, name="broker-ingest", daemon=True)
        self._writer.start()

    def stop(self) -> None:
        self._stop.set()
        if self._writer is not None:
            self._writer.join(timeout=5)
            self._writer = None

    def _drain_loop(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            batch = [first]
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._apply_batch(batch)
            except Exception:
                # Keep a poison record from permanently stopping broker ingestion.
                import logging

                logging.getLogger(__name__).exception("broker ingest batch failed")

    def drain_once(self) -> int:
        batch: list[AccountValueObservation | PositionObservation | OrderObservation] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._apply_batch(batch)
        return len(batch)

    def _apply_batch(
        self, batch: list[AccountValueObservation | PositionObservation | OrderObservation]
    ) -> None:
        with self._apply_lock:
            for record in batch:
                self._ingest_seq += 1
                if self._generation is not None:
                    self._stage(self._ingest_seq, record)
                else:
                    self._apply_record(self.journal.connect(), record)

    def _apply_record(
        self, conn: Any, record: AccountValueObservation | PositionObservation | OrderObservation
    ) -> None:
        if isinstance(record, AccountValueObservation):
            self._apply_account_value(conn, record)
        elif isinstance(record, PositionObservation):
            self._apply_position(conn, record)
        elif isinstance(record, OrderObservation):
            self._apply_order(conn, record)
        else:
            raise TypeError(f"unknown ingest record: {type(record)!r}")

    def _apply_account_value(self, conn: Any, obs: AccountValueObservation) -> None:
        current = self.store.get_account_in_tx(conn, obs.account_id)
        merged = merge_account_value(current, obs, self.account_mode)
        if current is not None and merged.same_fields(current):
            return
        mutation = DomainMutation(
            event_type="account.updated",
            entity_type="account",
            entity_id=obs.account_id,
            operation="upsert",
            account_id=obs.account_id,
            source="trader_service",
            source_timestamp=obs.source_timestamp,
            correlation_id=None,
            payload=merged.to_payload(),
        )

        def write(write_conn: Any, revision: int) -> None:
            self.store.upsert_account_in_tx(write_conn, replace(merged, revision=revision))

        self.journal.mutate(conn, mutation, write)

    def _apply_order(self, conn: Any, obs: OrderObservation) -> tuple[str, Any | None]:
        entity_id = self.correlator.resolve_in_tx(conn, obs)
        current = self.store.get_order_in_tx(conn, entity_id)
        group_id = decode_order_ref(obs.order_ref)
        merged = BrokerOrderRow(
            order_entity_id=entity_id,
            account_id=obs.account_id,
            conid=obs.conid,
            symbol=obs.symbol,
            order_group_id=group_id or (current.order_group_id if current else None),
            leg=(
                current.leg
                if current and current.leg
                else (
                    classify_leg(obs.order_type, obs.parent_id, obs.client_order_id)
                    if group_id
                    else None
                )
            ),
            is_external=(group_id is None) if current is None else current.is_external,
            action=obs.action,
            order_type=obs.order_type,
            total_quantity=obs.total_quantity,
            filled_quantity=obs.filled_quantity,
            avg_fill_price=(
                obs.avg_fill_price
                if obs.avg_fill_price is not None
                else (current.avg_fill_price if current else None)
            ),
            limit_price=(
                obs.limit_price if obs.limit_price is not None else (current.limit_price if current else None)
            ),
            stop_price=(
                obs.stop_price if obs.stop_price is not None else (current.stop_price if current else None)
            ),
            tif=obs.tif or (current.tif if current else None),
            status=obs.status,
            deleted=False,
            revision=current.revision if current else 0,
            source_timestamp=obs.source_timestamp,
        )
        if current is not None and not current.deleted and merged.same_fields(current):
            self.correlator.bind_aliases_in_tx(conn, entity_id, obs)
            return entity_id, None
        mutation = DomainMutation(
            event_type="order.updated",
            entity_type="order",
            entity_id=entity_id,
            operation="upsert",
            account_id=obs.account_id,
            source="trader_service",
            source_timestamp=obs.source_timestamp,
            correlation_id=None,
            payload=merged.to_payload(),
        )

        def write(write_conn: Any, revision: int) -> None:
            self.correlator.bind_aliases_in_tx(write_conn, entity_id, obs)
            self.store.upsert_order_in_tx(write_conn, replace(merged, revision=revision))

        event = self.journal.mutate(conn, mutation, write)
        self._resolve_unbound_fills_in_tx(conn, obs, entity_id)
        return entity_id, event

    def _resolve_unbound_fills_in_tx(
        self, conn: Any, obs: OrderObservation, entity_id: str
    ) -> None:
        return None

    def _apply_position(self, conn: Any, obs: PositionObservation) -> None:
        current = self.store.get_position_in_tx(conn, obs.account_id, obs.conid)
        entity_id = position_entity_id(obs.account_id, obs.conid)
        if obs.quantity == 0.0:
            if current is None or current.deleted:
                return
            mutation = DomainMutation(
                event_type="position.updated",
                entity_type="position",
                entity_id=entity_id,
                operation="delete",
                account_id=obs.account_id,
                source="trader_service",
                source_timestamp=obs.source_timestamp,
                correlation_id=None,
                payload=None,
            )

            def write_tombstone(write_conn: Any, revision: int) -> None:
                self.store.tombstone_position_in_tx(
                    write_conn, obs.account_id, obs.conid, revision, obs.source_timestamp
                )

            self.journal.mutate(conn, mutation, write_tombstone)
            return

        merged = merge_position(current, obs)
        if current is not None and not current.deleted and merged.same_fields(current):
            return
        mutation = DomainMutation(
            event_type="position.updated",
            entity_type="position",
            entity_id=entity_id,
            operation="upsert",
            account_id=obs.account_id,
            source="trader_service",
            source_timestamp=obs.source_timestamp,
            correlation_id=None,
            payload=merged.to_payload(),
        )

        def write(write_conn: Any, revision: int) -> None:
            self.store.upsert_position_in_tx(write_conn, replace(merged, revision=revision))

        self.journal.mutate(conn, mutation, write)

    def _stage(
        self, ingest_seq: int, record: AccountValueObservation | PositionObservation | OrderObservation
    ) -> None:
        raise NotImplementedError("broker-sync staging lands in Task 5")
