"""Broker producers — normalize IB callbacks into revisioned domain events.

IB eventkit callbacks only normalize and enqueue observations.  A single writer
thread owns state application; each materialized write is performed inside the
transaction self-managed by ``DomainJournal.mutate``.
"""
from __future__ import annotations

import datetime as dt
import dataclasses
import json
import logging
import queue
import threading
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from trader.data.broker_state import (
    BrokerAccountRow,
    BrokerFillRow,
    BrokerOrderRow,
    BrokerPositionRow,
    BrokerStateStore,
)
from trader.domain.events import DomainMutation
from trader.domain.identity import fill_entity_id, position_entity_id
from trader.trading.order_correlation import (
    OrderCorrelator,
    OrderObservation,
    classify_leg,
    decode_order_ref,
    normalize_open_order,
)


BROKER_SYNC_SOURCES = ("account", "positions", "open_orders", "completed_orders", "executions")


class GenerationIncomplete(RuntimeError):
    """A broker snapshot cannot promote until every required source ends."""


class NoActiveGeneration(RuntimeError):
    """A completion marker was received without an active broker snapshot."""


@dataclass
class _Generation:
    generation_id: int
    required: tuple[str, ...]
    complete: set[str] = dataclasses.field(default_factory=set)


def _encode_observation(record: Any) -> str:
    def default(value: Any) -> Any:
        if isinstance(value, dt.datetime):
            return {"__datetime__": value.isoformat()}
        raise TypeError(f"cannot encode {type(value)!r}")
    return json.dumps(dataclasses.asdict(record), default=default, sort_keys=True)


def _canonical_key(record: Any) -> str:
    if isinstance(record, AccountValueObservation):
        return f"account:{record.account_id}:{record.tag}:{record.currency}"
    if isinstance(record, PositionObservation):
        return f"position:{record.account_id}:{record.conid}"
    if isinstance(record, OrderObservation):
        return f"order:{record.account_id}:{record.perm_id or record.client_order_id}"
    if isinstance(record, FillObservation):
        return f"fill:{record.account_id}:{record.exec_id}"
    if isinstance(record, CommissionObservation):
        return f"commission:{record.fill.account_id}:{record.fill.exec_id}"
    raise TypeError(f"unsupported broker observation {type(record)!r}")


def _decode_observation(kind: str, record_json: Any) -> Any:
    payload = json.loads(record_json) if isinstance(record_json, str) else record_json

    def revive(value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"__datetime__"}:
                return dt.datetime.fromisoformat(value["__datetime__"])
            return {key: revive(item) for key, item in value.items()}
        if isinstance(value, list):
            return [revive(item) for item in value]
        return value

    revived = revive(payload)
    record_types = {
        "AccountValueObservation": AccountValueObservation,
        "PositionObservation": PositionObservation,
        "OrderObservation": OrderObservation,
        "FillObservation": FillObservation,
    }
    if kind == "CommissionObservation":
        revived["fill"] = FillObservation(**revived["fill"])
        return CommissionObservation(**revived)
    try:
        return record_types[kind](**revived)
    except KeyError as exc:
        raise ValueError(f"unknown staged broker observation kind {kind!r}") from exc


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


@dataclass(frozen=True)
class FillObservation:
    account_id: str
    exec_id: str
    perm_id: int
    client_order_id: int
    conid: int
    side: str
    quantity: float
    price: float
    fill_time: dt.datetime
    source_timestamp: dt.datetime


@dataclass(frozen=True)
class CommissionObservation:
    fill: FillObservation
    commission: float
    currency: str
    realized_pnl: Optional[float]


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


def normalize_execution(fill_obj: Any, now: dt.datetime) -> FillObservation:
    execution, contract = fill_obj.execution, fill_obj.contract
    reported_time = getattr(execution, "time", None)
    fill_time = reported_time if getattr(reported_time, "tzinfo", None) else now
    return FillObservation(
        account_id=execution.acctNumber,
        exec_id=execution.execId,
        perm_id=int(execution.permId or 0),
        client_order_id=int(execution.orderId or 0),
        conid=int(contract.conId),
        side="BUY" if execution.side == "BOT" else "SELL",
        quantity=float(execution.shares),
        price=float(execution.price),
        fill_time=fill_time,
        source_timestamp=now,
    )


def normalize_commission(fill_obj: Any, report: Any, now: dt.datetime) -> CommissionObservation:
    return CommissionObservation(
        fill=normalize_execution(fill_obj, now),
        commission=float(report.commission),
        currency=report.currency or "",
        realized_pnl=_none_if_unset(getattr(report, "realizedPNL", None)),
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
        self._queue: queue.Queue[
            AccountValueObservation | PositionObservation | OrderObservation | FillObservation | CommissionObservation
        ] = queue.Queue()
        self._ingest_seq = 0
        self._generation: Optional[_Generation] = None
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

    def on_exec_details(self, _trade: Any, fill: Any) -> None:
        observation = normalize_execution(fill, self.clock())
        if observation.account_id == self.account_id:
            self._queue.put(observation)

    def on_commission_report(self, _trade: Any, fill: Any, report: Any) -> None:
        observation = normalize_commission(fill, report, self.clock())
        if observation.fill.account_id == self.account_id:
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

    def begin_generation(self, required: tuple[str, ...] = BROKER_SYNC_SOURCES) -> int:
        """Start staging one broker snapshot generation.

        Live broker rows remain untouched until a later, complete generation
        is promoted.  Reject overlapping generations: there is only one IB
        snapshot stream per ingest instance and mixing their callbacks would
        make absence tombstones unsafe.
        """
        with self._apply_lock:
            if self._generation is not None:
                raise RuntimeError("broker snapshot generation already active")
            if not required or len(set(required)) != len(required):
                raise ValueError("required broker sources must be unique and non-empty")
            generation_id = self.db.transaction(
                lambda conn: self.store.open_generation_in_tx(conn, required, self.clock())
            )
            self._generation = _Generation(generation_id=generation_id, required=required)
            return generation_id

    @property
    def is_ready(self) -> bool:
        """Whether a coherent broker generation has been promoted."""
        with self._apply_lock:
            if self._generation is not None:
                return False
        return self.db.transaction(self.store.latest_promoted_generation_in_tx) is not None

    def mark_source_complete(self, source: str) -> None:
        with self._apply_lock:
            generation = self._require_generation()
            if source not in generation.required:
                raise ValueError(f"source {source!r} is not required by this broker generation")
            self.db.transaction(
                lambda conn: self.store.mark_source_complete_in_tx(
                    conn, generation.generation_id, source
                )
            )
            generation.complete.add(source)

    def promote_generation(self) -> int:
        """Apply a complete staged generation in one journal transaction."""
        with self._apply_lock:
            generation = self._require_generation()
            missing = sorted(set(generation.required) - generation.complete)
            if missing:
                raise GenerationIncomplete(
                    f"broker generation {generation.generation_id} is incomplete; missing: {', '.join(missing)}"
                )
            cursor = self.journal.mutate_batch_work(
                self.journal.connect(),
                lambda conn, append: self._promote_in_journal_transaction(conn, generation, append),
            )
            self._generation = None
            return cursor

    def abandon_generation(self, reason: str) -> None:
        with self._apply_lock:
            generation = self._generation
            if generation is None:
                return
            self.db.transaction(
                lambda conn: (
                    self.store.mark_generation_abandoned_in_tx(
                        conn, generation.generation_id, reason, self.clock()
                    ),
                    self.store.purge_staging_in_tx(conn, generation.generation_id),
                )
            )
            self._generation = None

    def _require_generation(self) -> _Generation:
        if self._generation is None:
            raise NoActiveGeneration("no broker snapshot generation is active")
        return self._generation

    async def run_broker_sync(self, client: Any, timeout_seconds: float = 45.0) -> bool:
        """Stage and promote one complete IB broker snapshot.

        Registered callbacks continue to feed the same staging queue while
        these requests run.  Ingest sequence therefore preserves the broker's
        observed ordering when a live delta interleaves the initial snapshot.
        """
        import asyncio

        self.begin_generation()
        try:
            async with asyncio.timeout(timeout_seconds):
                # ib_async's connectAsync(account=...) already issues
                # reqAccountUpdatesAsync (with its own timeout) during connect,
                # so the account-updates subscription is active and its values
                # are already cached by the time this runs. Re-issuing
                # reqAccountUpdatesAsync here re-subscribes an already-subscribed
                # connection: IB sends no fresh accountDownloadEnd, so the await
                # hangs until the outer timeout, the sync abandons, and NO broker
                # generation ever promotes -> the command-center snapshot stays
                # SNAPSHOT_NOT_READY forever (dashboard 503). ibreactive.connect*
                # documents this same "reqAccountUpdates deadlocks post-connect"
                # trap. Read the already-cached values instead, mirroring the
                # reqPositionsAsync/reqAllOpenOrdersAsync pattern below.
                account_values = client.ib.accountValues(self.account_id)
                if not account_values:
                    # Fail loudly (project principle): an empty cache means the
                    # connect-time reqAccountUpdatesAsync likely timed out on a
                    # busy gateway. We still promote (positions/orders are
                    # complete), but the fenced snapshot will carry no balances
                    # until the next sync -- surface it rather than silently
                    # shipping a "ready" snapshot with missing account rows.
                    logging.getLogger(__name__).warning(
                        "broker sync: no cached account values for %s; promoting "
                        "with empty balances (reqAccountUpdatesAsync at connect "
                        "likely timed out)", self.account_id)
                for account_value in account_values:
                    self.on_account_value(account_value)
                self.mark_source_complete("account")
                for position in await client.ib.reqPositionsAsync():
                    self.on_position(position)
                self.mark_source_complete("positions")
                for trade in await client.ib.reqAllOpenOrdersAsync():
                    self.on_open_order(trade)
                self.mark_source_complete("open_orders")
                for trade in await client.ib.reqCompletedOrdersAsync(apiOnly=True):
                    self.on_open_order(trade)
                self.mark_source_complete("completed_orders")
                for fill in await client.ib.reqExecutionsAsync():
                    self.on_exec_details(None, fill)
                self.mark_source_complete("executions")
            await asyncio.to_thread(self._promote_after_drain)
            return True
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            await asyncio.to_thread(self._abandon_if_active, f"broker sync failed: {exc}")
            return False

    def _promote_after_drain(self) -> None:
        self.drain_once()
        self.promote_generation()

    def _abandon_if_active(self, reason: str) -> None:
        with self._apply_lock:
            active = self._generation is not None
        if active:
            self.abandon_generation(reason)

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
        batch: list[
            AccountValueObservation | PositionObservation | OrderObservation | FillObservation | CommissionObservation
        ] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._apply_batch(batch)
        return len(batch)

    def _apply_batch(
        self, batch: list[
            AccountValueObservation | PositionObservation | OrderObservation | FillObservation | CommissionObservation
        ]
    ) -> None:
        with self._apply_lock:
            for record in batch:
                self._ingest_seq += 1
                if self._generation is not None:
                    self._stage(self._ingest_seq, record)
                else:
                    conn = self.journal.connect()
                    self._apply_record(
                        conn,
                        record,
                        lambda mutation, write: self.journal.mutate(conn, mutation, write),
                    )

    def _apply_record(
        self,
        conn: Any,
        record: AccountValueObservation | PositionObservation | OrderObservation | FillObservation | CommissionObservation,
        emit: Callable[[DomainMutation, Callable[[Any, int], None]], Any],
    ) -> None:
        if isinstance(record, AccountValueObservation):
            self._apply_account_value(conn, record, emit)
        elif isinstance(record, PositionObservation):
            self._apply_position(conn, record, emit)
        elif isinstance(record, OrderObservation):
            self._apply_order(conn, record, emit)
        elif isinstance(record, FillObservation):
            self._apply_fill(conn, record, emit)
        elif isinstance(record, CommissionObservation):
            self._apply_commission(conn, record, emit)
        else:
            raise TypeError(f"unknown ingest record: {type(record)!r}")

    def _apply_account_value(
        self, conn: Any, obs: AccountValueObservation, emit: Callable[[DomainMutation, Callable[[Any, int], None]], Any]
    ) -> None:
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

        emit(mutation, write)

    def _apply_order(
        self, conn: Any, obs: OrderObservation, emit: Callable[[DomainMutation, Callable[[Any, int], None]], Any]
    ) -> tuple[str, Any | None]:
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
            self._resolve_unbound_fills_in_tx(conn, obs, entity_id, emit)
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

        event = emit(mutation, write)
        self._resolve_unbound_fills_in_tx(conn, obs, entity_id, emit)
        return entity_id, event

    def _resolve_unbound_fills_in_tx(
        self,
        conn: Any,
        obs: OrderObservation,
        entity_id: str,
        emit: Callable[[DomainMutation, Callable[[Any, int], None]], Any],
    ) -> None:
        for fill in self.store.unbound_fills_in_tx(conn, obs.account_id):
            matches = (
                (obs.perm_id and fill.perm_id == obs.perm_id)
                or (
                    obs.client_order_id
                    and fill.client_order_id == obs.client_order_id
                    and fill.session_epoch == self.session_epoch
                )
            )
            if not matches:
                continue
            bound = replace(fill, order_entity_id=entity_id, source_timestamp=obs.source_timestamp)
            mutation = DomainMutation(
                event_type="fill.updated",
                entity_type="fill",
                entity_id=fill_entity_id(fill.account_id, fill.exec_id),
                operation="upsert",
                account_id=fill.account_id,
                source="trader_service",
                source_timestamp=obs.source_timestamp,
                correlation_id=None,
                payload=bound.to_payload(),
            )

            def write(write_conn: Any, revision: int, row: BrokerFillRow = bound) -> None:
                self.store.upsert_fill_in_tx(write_conn, replace(row, revision=revision))

            emit(mutation, write)

    def _fill_row(self, conn: Any, obs: FillObservation) -> BrokerFillRow:
        return BrokerFillRow(
            account_id=obs.account_id,
            exec_id=obs.exec_id,
            order_entity_id=self._resolve_fill_order_in_tx(conn, obs),
            perm_id=obs.perm_id or None,
            client_order_id=obs.client_order_id or None,
            session_epoch=self.session_epoch,
            conid=obs.conid,
            side=obs.side,
            quantity=obs.quantity,
            price=obs.price,
            commission=None,
            commission_currency=None,
            realized_pnl=None,
            fill_time=obs.fill_time,
            revision=0,
            source_timestamp=obs.source_timestamp,
        )

    def _resolve_fill_order_in_tx(self, conn: Any, obs: FillObservation) -> Optional[str]:
        if obs.perm_id:
            entity = self.store.find_order_by_alias_in_tx(
                conn, "perm_id", str(obs.perm_id), obs.account_id, ""
            )
            if entity:
                return entity
        if obs.client_order_id:
            return self.store.find_order_by_alias_in_tx(
                conn,
                "client_order_id",
                str(obs.client_order_id),
                obs.account_id,
                self.session_epoch,
            )
        return None

    def _apply_fill(
        self, conn: Any, obs: FillObservation, emit: Callable[[DomainMutation, Callable[[Any, int], None]], Any]
    ) -> Any | None:
        if self.store.get_fill_in_tx(conn, obs.account_id, obs.exec_id) is not None:
            return None
        row = self._fill_row(conn, obs)
        mutation = DomainMutation(
            event_type="fill.received",
            entity_type="fill",
            entity_id=fill_entity_id(obs.account_id, obs.exec_id),
            operation="upsert",
            account_id=obs.account_id,
            source="trader_service",
            source_timestamp=obs.source_timestamp,
            correlation_id=None,
            payload=row.to_payload(),
        )

        def write(write_conn: Any, revision: int) -> None:
            self.store.upsert_fill_in_tx(write_conn, replace(row, revision=revision))

        return emit(mutation, write)

    def _apply_commission(
        self, conn: Any, obs: CommissionObservation, emit: Callable[[DomainMutation, Callable[[Any, int], None]], Any]
    ) -> Any | None:
        self._apply_fill(conn, obs.fill, emit)
        current = self.store.get_fill_in_tx(conn, obs.fill.account_id, obs.fill.exec_id)
        if current is None:
            raise RuntimeError("fill was not persisted before commission update")
        revised = replace(
            current,
            commission=obs.commission,
            commission_currency=obs.currency,
            realized_pnl=obs.realized_pnl,
            source_timestamp=obs.fill.source_timestamp,
        )
        if revised.same_fields(current):
            return None
        mutation = DomainMutation(
            event_type="fill.updated",
            entity_type="fill",
            entity_id=fill_entity_id(obs.fill.account_id, obs.fill.exec_id),
            operation="upsert",
            account_id=obs.fill.account_id,
            source="trader_service",
            source_timestamp=obs.fill.source_timestamp,
            correlation_id=None,
            payload=revised.to_payload(),
        )

        def write(write_conn: Any, revision: int) -> None:
            self.store.upsert_fill_in_tx(write_conn, replace(revised, revision=revision))

        return emit(mutation, write)

    def _apply_position(
        self, conn: Any, obs: PositionObservation, emit: Callable[[DomainMutation, Callable[[Any, int], None]], Any]
    ) -> None:
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

            emit(mutation, write_tombstone)
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

        emit(mutation, write)

    def _tombstone_order(
        self,
        conn: Any,
        row: BrokerOrderRow,
        now: dt.datetime,
        emit: Callable[[DomainMutation, Callable[[Any, int], None]], Any],
    ) -> None:
        mutation = DomainMutation(
            event_type="order.updated",
            entity_type="order",
            entity_id=row.order_entity_id,
            operation="delete",
            account_id=row.account_id,
            source="trader_service",
            source_timestamp=now,
            correlation_id=None,
            payload=None,
        )

        def write(write_conn: Any, revision: int) -> None:
            self.store.tombstone_order_in_tx(
                write_conn, row.order_entity_id, revision, now
            )

        emit(mutation, write)

    def _promote_in_journal_transaction(
        self,
        conn: Any,
        generation: _Generation,
        append: Callable[[DomainMutation, Callable[[Any, int], None], Optional[str]], Any],
    ) -> int:
        """Replay staging and the enumerable-set absence checks atomically."""
        observed_positions: set[tuple[str, int]] = set()
        observed_orders: set[str] = set()
        for staged in self.store.staged_rows_in_tx(conn, generation.generation_id):
            record = _decode_observation(staged.record_kind, staged.record_json)
            if isinstance(record, PositionObservation):
                observed_positions.add((record.account_id, record.conid))
                self._apply_position(conn, record, append)
            elif isinstance(record, OrderObservation):
                entity_id, _event = self._apply_order(conn, record, append)
                observed_orders.add(entity_id)
            else:
                self._apply_record(conn, record, append)

        # Positions and currently working orders are enumerable broker sets.
        # Absence is therefore a delete only after every snapshot source has
        # completed and only for rows belonging to this ingest account.
        now = self.clock()
        for row in self.store.select_active_positions_in_tx(conn):
            if row.account_id == self.account_id and (row.account_id, row.conid) not in observed_positions:
                self._apply_position(
                    conn,
                    PositionObservation(
                        account_id=row.account_id,
                        conid=row.conid,
                        symbol=row.symbol,
                        sec_type=row.sec_type,
                        exchange=row.exchange,
                        currency=row.currency,
                        quantity=0.0,
                        average_cost=None,
                        market_price=None,
                        market_value=None,
                        unrealized_pnl=None,
                        realized_pnl=None,
                        daily_pnl=None,
                        source_timestamp=now,
                    ),
                    append,
                )
        for row in self.store.select_working_orders_in_tx(conn):
            if row.account_id == self.account_id and row.order_entity_id not in observed_orders:
                self._tombstone_order(conn, row, now, append)

        cursor_row = conn.execute(
            "SELECT COALESCE(MAX(source_cursor), 0) FROM domain_event_journal"
        ).fetchone()
        cursor = int(cursor_row[0]) if cursor_row is not None else 0
        self.store.mark_generation_promoted_in_tx(
            conn, generation.generation_id, cursor, now
        )
        self.store.purge_staging_in_tx(conn, generation.generation_id)
        return cursor

    def _stage(
        self, ingest_seq: int, record: AccountValueObservation | PositionObservation | OrderObservation | FillObservation | CommissionObservation
    ) -> None:
        generation = self._require_generation()
        canonical_key = _canonical_key(record)
        self.db.transaction(
            lambda conn: self.store.stage_in_tx(
                conn,
                generation.generation_id,
                ingest_seq,
                "snapshot",
                canonical_key.split(":", 1)[0],
                canonical_key,
                _encode_observation(record),
                type(record).__name__,
            )
        )
