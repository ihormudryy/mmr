"""Broker materialized state — trader-owned revisioned tables. [M1-F2]

Write methods ending in ``_in_tx`` take the open DuckDB connection supplied by
``DuckDBConnection.transaction`` or ``DomainJournal.mutate``.  They never open
their own connection, which keeps a materialized mutation and its journal row
within the same transaction.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, Callable, Optional


BROKER_STATE_MIGRATION_VERSION = 10

_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS broker_account_state (
        account_id VARCHAR PRIMARY KEY,
        account_mode VARCHAR NOT NULL CHECK (account_mode IN ('paper', 'live')),
        net_liquidation DOUBLE,
        total_cash DOUBLE,
        buying_power DOUBLE,
        available_funds DOUBLE,
        maintenance_margin DOUBLE,
        balances JSON NOT NULL,
        revision BIGINT NOT NULL,
        source_timestamp TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS broker_positions (
        account_id VARCHAR NOT NULL,
        conid BIGINT NOT NULL,
        symbol VARCHAR NOT NULL,
        sec_type VARCHAR NOT NULL,
        exchange VARCHAR,
        currency VARCHAR NOT NULL,
        quantity DOUBLE NOT NULL,
        average_cost DOUBLE,
        market_price DOUBLE,
        market_value DOUBLE,
        unrealized_pnl DOUBLE,
        realized_pnl DOUBLE,
        daily_pnl DOUBLE,
        deleted BOOLEAN NOT NULL DEFAULT FALSE,
        revision BIGINT NOT NULL,
        source_timestamp TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE (account_id, conid)
    )""",
    """CREATE TABLE IF NOT EXISTS broker_orders (
        order_entity_id VARCHAR PRIMARY KEY,
        account_id VARCHAR NOT NULL,
        conid BIGINT NOT NULL,
        symbol VARCHAR NOT NULL,
        order_group_id VARCHAR,
        leg VARCHAR,
        is_external BOOLEAN NOT NULL DEFAULT FALSE,
        action VARCHAR NOT NULL,
        order_type VARCHAR NOT NULL,
        total_quantity DOUBLE NOT NULL,
        filled_quantity DOUBLE NOT NULL DEFAULT 0,
        avg_fill_price DOUBLE,
        limit_price DOUBLE,
        stop_price DOUBLE,
        tif VARCHAR,
        status VARCHAR NOT NULL,
        deleted BOOLEAN NOT NULL DEFAULT FALSE,
        revision BIGINT NOT NULL,
        source_timestamp TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS broker_order_aliases (
        alias_type VARCHAR NOT NULL CHECK (alias_type IN
            ('client_order_id', 'perm_id', 'parent_id', 'order_ref')),
        alias_value VARCHAR NOT NULL,
        account_id VARCHAR NOT NULL,
        session_epoch VARCHAR NOT NULL DEFAULT '',
        order_entity_id VARCHAR NOT NULL,
        bound_at TIMESTAMPTZ NOT NULL,
        UNIQUE (alias_type, alias_value, account_id, session_epoch)
    )""",
    """CREATE TABLE IF NOT EXISTS broker_fills (
        account_id VARCHAR NOT NULL,
        exec_id VARCHAR NOT NULL,
        order_entity_id VARCHAR,
        perm_id BIGINT,
        client_order_id BIGINT,
        session_epoch VARCHAR NOT NULL DEFAULT '',
        conid BIGINT NOT NULL,
        side VARCHAR NOT NULL,
        quantity DOUBLE NOT NULL,
        price DOUBLE NOT NULL,
        commission DOUBLE,
        commission_currency VARCHAR,
        realized_pnl DOUBLE,
        fill_time TIMESTAMPTZ NOT NULL,
        revision BIGINT NOT NULL,
        source_timestamp TIMESTAMPTZ NOT NULL,
        UNIQUE (account_id, exec_id)
    )""",
    "CREATE SEQUENCE IF NOT EXISTS broker_sync_generation_seq START 1",
    """CREATE TABLE IF NOT EXISTS broker_sync_generations (
        generation_id BIGINT PRIMARY KEY DEFAULT nextval('broker_sync_generation_seq'),
        status VARCHAR NOT NULL CHECK (status IN ('staging', 'promoted', 'abandoned')),
        sources_required JSON NOT NULL,
        sources_complete JSON NOT NULL,
        promoted_cursor BIGINT,
        abandon_reason VARCHAR,
        started_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS broker_sync_staging (
        generation_id BIGINT NOT NULL,
        ingest_seq BIGINT NOT NULL,
        source VARCHAR NOT NULL,
        entity_type VARCHAR NOT NULL,
        canonical_key VARCHAR NOT NULL,
        record_kind VARCHAR NOT NULL,
        record_json JSON NOT NULL,
        UNIQUE (generation_id, ingest_seq)
    )""",
]

_EXCLUDED_FROM_COMPARISON = ("revision", "source_timestamp")
_WORKING_ORDER_STATUSES = (
    "PendingSubmit",
    "ApiPending",
    "PreSubmitted",
    "Submitted",
    "PendingCancel",
)


def _same_fields(a: Any, b: Any) -> bool:
    left, right = dataclasses.asdict(a), dataclasses.asdict(b)
    for key in _EXCLUDED_FROM_COMPARISON:
        left.pop(key, None)
        right.pop(key, None)
    return left == right


def _json_timestamp(value: Optional[dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("broker timestamp must be timezone-aware")
    return value.astimezone(dt.timezone.utc).isoformat()


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass(frozen=True)
class BrokerAccountRow:
    account_id: str
    account_mode: str
    net_liquidation: Optional[float]
    total_cash: Optional[float]
    buying_power: Optional[float]
    available_funds: Optional[float]
    maintenance_margin: Optional[float]
    balances: dict[str, str]
    revision: int
    source_timestamp: dt.datetime

    def same_fields(self, other: "BrokerAccountRow") -> bool:
        return _same_fields(self, other)

    def to_payload(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["source_timestamp"] = _json_timestamp(self.source_timestamp)
        payload["entity_id"] = self.account_id
        payload["entity_revision"] = self.revision
        return payload


@dataclass(frozen=True)
class BrokerPositionRow:
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
    deleted: bool
    revision: int
    source_timestamp: dt.datetime

    def same_fields(self, other: "BrokerPositionRow") -> bool:
        return _same_fields(self, other)

    def to_payload(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["source_timestamp"] = _json_timestamp(self.source_timestamp)
        payload["entity_id"] = f"{self.account_id}:{self.conid}"
        payload["entity_revision"] = self.revision
        return payload


@dataclass(frozen=True)
class BrokerOrderRow:
    order_entity_id: str
    account_id: str
    conid: int
    symbol: str
    order_group_id: Optional[str]
    leg: Optional[str]
    is_external: bool
    action: str
    order_type: str
    total_quantity: float
    filled_quantity: float
    avg_fill_price: Optional[float]
    limit_price: Optional[float]
    stop_price: Optional[float]
    tif: Optional[str]
    status: str
    deleted: bool
    revision: int
    source_timestamp: dt.datetime

    def same_fields(self, other: "BrokerOrderRow") -> bool:
        return _same_fields(self, other)

    def to_payload(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["source_timestamp"] = _json_timestamp(self.source_timestamp)
        payload["entity_id"] = self.order_entity_id
        payload["entity_revision"] = self.revision
        return payload


class BrokerRiskSnapshotError(RuntimeError):
    """A fenced broker snapshot cannot be trusted for a risk decision."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class BrokerRiskSnapshot:
    """Immutable account/position/order evidence from one DuckDB snapshot.

    ``generation_id`` identifies the most recent complete broker enumeration.
    ``source_cursor`` is the latest journal cursor visible in the same read
    transaction, so it also fences broker deltas committed after promotion.
    Quotes and what-if responses deliberately do not belong to this object.
    """

    generation_id: int
    source_cursor: int
    promoted_at: dt.datetime
    account_id: str
    account_mode: str
    net_liquidation: float
    daily_pnl: float
    positions: tuple[BrokerPositionRow, ...]
    working_orders: tuple[BrokerOrderRow, ...]

    def reducible_quantity(self, conid: int) -> float:
        return sum(row.quantity for row in self.positions if row.conid == conid)

    def position_value(self, conid: int) -> float:
        return sum(
            abs(float(row.market_value or 0.0))
            for row in self.positions
            if row.conid == conid
        )

    @property
    def open_order_count(self) -> int:
        return len(self.working_orders)


@dataclass(frozen=True)
class BrokerFillRow:
    account_id: str
    exec_id: str
    order_entity_id: Optional[str]
    perm_id: Optional[int]
    client_order_id: Optional[int]
    session_epoch: str
    conid: int
    side: str
    quantity: float
    price: float
    commission: Optional[float]
    commission_currency: Optional[str]
    realized_pnl: Optional[float]
    fill_time: dt.datetime
    revision: int
    source_timestamp: dt.datetime

    def same_fields(self, other: "BrokerFillRow") -> bool:
        return _same_fields(self, other)

    def to_payload(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["fill_time"] = _json_timestamp(self.fill_time)
        payload["source_timestamp"] = _json_timestamp(self.source_timestamp)
        payload["entity_id"] = f"{self.account_id}:{self.exec_id}"
        payload["entity_revision"] = self.revision
        return payload


class BrokerStateStore:
    def __init__(self, db: Any):
        self.db = db

    def migrate(self, migrator: Any) -> None:
        migrator.apply(BROKER_STATE_MIGRATION_VERSION, "broker_state_tables", _STATEMENTS)

    @staticmethod
    def _upsert(conn: Any, table: str, key: dict[str, Any], values: dict[str, Any]) -> None:
        where = " AND ".join(f"{column} = ?" for column in key)
        conn.execute(f"DELETE FROM {table} WHERE {where}", list(key.values()))
        columns = ", ".join(values)
        markers = ", ".join("?" for _ in values)
        conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({markers})", list(values.values()))

    @staticmethod
    def _account_from_row(row: tuple[Any, ...]) -> BrokerAccountRow:
        return BrokerAccountRow(
            account_id=row[0], account_mode=row[1], net_liquidation=row[2],
            total_cash=row[3], buying_power=row[4], available_funds=row[5],
            maintenance_margin=row[6], balances=_json_value(row[7]), revision=row[8],
            source_timestamp=row[9],
        )

    @staticmethod
    def _position_from_row(row: tuple[Any, ...]) -> BrokerPositionRow:
        return BrokerPositionRow(*row)

    @staticmethod
    def _order_from_row(row: tuple[Any, ...]) -> BrokerOrderRow:
        return BrokerOrderRow(*row)

    @staticmethod
    def _fill_from_row(row: tuple[Any, ...]) -> BrokerFillRow:
        return BrokerFillRow(*row)

    def upsert_account_in_tx(self, conn: Any, row: BrokerAccountRow) -> None:
        self._upsert(conn, "broker_account_state", {"account_id": row.account_id}, {
            "account_id": row.account_id, "account_mode": row.account_mode,
            "net_liquidation": row.net_liquidation, "total_cash": row.total_cash,
            "buying_power": row.buying_power, "available_funds": row.available_funds,
            "maintenance_margin": row.maintenance_margin, "balances": json.dumps(row.balances),
            "revision": row.revision, "source_timestamp": row.source_timestamp, "updated_at": _now(),
        })

    def get_account_in_tx(self, conn: Any, account_id: str) -> Optional[BrokerAccountRow]:
        row = conn.execute(
            "SELECT account_id, account_mode, net_liquidation, total_cash, buying_power, "
            "available_funds, maintenance_margin, balances, revision, source_timestamp "
            "FROM broker_account_state WHERE account_id = ?", [account_id]
        ).fetchone()
        return None if row is None else self._account_from_row(row)

    def select_accounts_in_tx(self, conn: Any) -> list[BrokerAccountRow]:
        rows = conn.execute(
            "SELECT account_id, account_mode, net_liquidation, total_cash, buying_power, "
            "available_funds, maintenance_margin, balances, revision, source_timestamp "
            "FROM broker_account_state ORDER BY account_id"
        ).fetchall()
        return [self._account_from_row(row) for row in rows]

    def upsert_position_in_tx(self, conn: Any, row: BrokerPositionRow) -> None:
        self._upsert(conn, "broker_positions", {"account_id": row.account_id, "conid": row.conid}, {
            "account_id": row.account_id, "conid": row.conid, "symbol": row.symbol,
            "sec_type": row.sec_type, "exchange": row.exchange, "currency": row.currency,
            "quantity": row.quantity, "average_cost": row.average_cost,
            "market_price": row.market_price, "market_value": row.market_value,
            "unrealized_pnl": row.unrealized_pnl, "realized_pnl": row.realized_pnl,
            "daily_pnl": row.daily_pnl, "deleted": row.deleted, "revision": row.revision,
            "source_timestamp": row.source_timestamp, "updated_at": _now(),
        })

    def get_position_in_tx(self, conn: Any, account_id: str, conid: int) -> Optional[BrokerPositionRow]:
        row = conn.execute(
            "SELECT account_id, conid, symbol, sec_type, exchange, currency, quantity, average_cost, "
            "market_price, market_value, unrealized_pnl, realized_pnl, daily_pnl, deleted, revision, "
            "source_timestamp FROM broker_positions WHERE account_id = ? AND conid = ?",
            [account_id, conid],
        ).fetchone()
        return None if row is None else self._position_from_row(row)

    def tombstone_position_in_tx(
        self, conn: Any, account_id: str, conid: int, revision: int, source_timestamp: dt.datetime
    ) -> None:
        row = self.get_position_in_tx(conn, account_id, conid)
        if row is not None:
            self.upsert_position_in_tx(
                conn, replace(row, quantity=0.0, deleted=True, revision=revision,
                              source_timestamp=source_timestamp)
            )

    def select_active_positions_in_tx(self, conn: Any) -> list[BrokerPositionRow]:
        rows = conn.execute(
            "SELECT account_id, conid, symbol, sec_type, exchange, currency, quantity, average_cost, "
            "market_price, market_value, unrealized_pnl, realized_pnl, daily_pnl, deleted, revision, "
            "source_timestamp FROM broker_positions WHERE NOT deleted ORDER BY account_id, conid"
        ).fetchall()
        return [self._position_from_row(row) for row in rows]

    def upsert_order_in_tx(self, conn: Any, row: BrokerOrderRow) -> None:
        self._upsert(conn, "broker_orders", {"order_entity_id": row.order_entity_id}, {
            "order_entity_id": row.order_entity_id, "account_id": row.account_id, "conid": row.conid,
            "symbol": row.symbol, "order_group_id": row.order_group_id, "leg": row.leg,
            "is_external": row.is_external, "action": row.action, "order_type": row.order_type,
            "total_quantity": row.total_quantity, "filled_quantity": row.filled_quantity,
            "avg_fill_price": row.avg_fill_price, "limit_price": row.limit_price,
            "stop_price": row.stop_price, "tif": row.tif, "status": row.status,
            "deleted": row.deleted, "revision": row.revision,
            "source_timestamp": row.source_timestamp, "updated_at": _now(),
        })

    def get_order_in_tx(self, conn: Any, order_entity_id: str) -> Optional[BrokerOrderRow]:
        row = conn.execute(
            "SELECT order_entity_id, account_id, conid, symbol, order_group_id, leg, is_external, action, "
            "order_type, total_quantity, filled_quantity, avg_fill_price, limit_price, stop_price, tif, "
            "status, deleted, revision, source_timestamp FROM broker_orders WHERE order_entity_id = ?",
            [order_entity_id],
        ).fetchone()
        return None if row is None else self._order_from_row(row)

    def tombstone_order_in_tx(
        self, conn: Any, order_entity_id: str, revision: int, source_timestamp: dt.datetime
    ) -> None:
        row = self.get_order_in_tx(conn, order_entity_id)
        if row is not None:
            self.upsert_order_in_tx(
                conn, replace(row, deleted=True, revision=revision, source_timestamp=source_timestamp)
            )

    def select_active_orders_in_tx(self, conn: Any) -> list[BrokerOrderRow]:
        rows = conn.execute(
            "SELECT order_entity_id, account_id, conid, symbol, order_group_id, leg, is_external, action, "
            "order_type, total_quantity, filled_quantity, avg_fill_price, limit_price, stop_price, tif, "
            "status, deleted, revision, source_timestamp FROM broker_orders WHERE NOT deleted "
            "ORDER BY order_entity_id"
        ).fetchall()
        return [self._order_from_row(row) for row in rows]

    def select_working_orders_in_tx(self, conn: Any) -> list[BrokerOrderRow]:
        markers = ", ".join("?" for _ in _WORKING_ORDER_STATUSES)
        rows = conn.execute(
            "SELECT order_entity_id, account_id, conid, symbol, order_group_id, leg, is_external, action, "
            "order_type, total_quantity, filled_quantity, avg_fill_price, limit_price, stop_price, tif, "
            f"status, deleted, revision, source_timestamp FROM broker_orders WHERE NOT deleted AND status IN ({markers}) "
            "ORDER BY order_entity_id",
            list(_WORKING_ORDER_STATUSES),
        ).fetchall()
        return [self._order_from_row(row) for row in rows]

    def bind_alias_in_tx(
        self, conn: Any, alias_type: str, alias_value: str, account_id: str,
        session_epoch: str, order_entity_id: str, bound_at: dt.datetime,
    ) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO broker_order_aliases "
            "(alias_type, alias_value, account_id, session_epoch, order_entity_id, bound_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [alias_type, alias_value, account_id, session_epoch, order_entity_id, bound_at],
        )

    def find_order_by_alias_in_tx(
        self, conn: Any, alias_type: str, alias_value: str, account_id: str, session_epoch: str
    ) -> Optional[str]:
        row = conn.execute(
            "SELECT order_entity_id FROM broker_order_aliases WHERE alias_type = ? AND alias_value = ? "
            "AND account_id = ? AND session_epoch = ?",
            [alias_type, alias_value, account_id, session_epoch],
        ).fetchone()
        return None if row is None else row[0]

    def find_perm_id_for_order_in_tx(
        self, conn: Any, order_entity_id: str
    ) -> Optional[int]:
        """Reverse alias lookup: the STABLE IB ``perm_id`` bound to this order
        entity, or None if none has been observed yet. perm_id aliases are
        session-independent (bound with ``session_epoch=''``), so a working
        order has at most one. Used by the cancel adapter to re-validate against
        the LIVE session by perm_id rather than trusting a session-scoped
        ``orderId`` (which can go stale / be reused across a reconnect)."""
        row = conn.execute(
            "SELECT alias_value FROM broker_order_aliases "
            "WHERE order_entity_id = ? AND alias_type = 'perm_id' LIMIT 1",
            [order_entity_id],
        ).fetchone()
        return None if row is None else int(row[0])

    def upsert_fill_in_tx(self, conn: Any, row: BrokerFillRow) -> None:
        self._upsert(conn, "broker_fills", {"account_id": row.account_id, "exec_id": row.exec_id}, {
            "account_id": row.account_id, "exec_id": row.exec_id,
            "order_entity_id": row.order_entity_id, "perm_id": row.perm_id,
            "client_order_id": row.client_order_id, "session_epoch": row.session_epoch,
            "conid": row.conid, "side": row.side, "quantity": row.quantity, "price": row.price,
            "commission": row.commission, "commission_currency": row.commission_currency,
            "realized_pnl": row.realized_pnl, "fill_time": row.fill_time,
            "revision": row.revision, "source_timestamp": row.source_timestamp,
        })

    def get_fill_in_tx(self, conn: Any, account_id: str, exec_id: str) -> Optional[BrokerFillRow]:
        row = conn.execute(
            "SELECT account_id, exec_id, order_entity_id, perm_id, client_order_id, session_epoch, conid, "
            "side, quantity, price, commission, commission_currency, realized_pnl, fill_time, revision, "
            "source_timestamp FROM broker_fills WHERE account_id = ? AND exec_id = ?",
            [account_id, exec_id],
        ).fetchone()
        return None if row is None else self._fill_from_row(row)

    def select_fills_in_tx(self, conn: Any) -> list[BrokerFillRow]:
        rows = conn.execute(
            "SELECT account_id, exec_id, order_entity_id, perm_id, client_order_id, session_epoch, conid, "
            "side, quantity, price, commission, commission_currency, realized_pnl, fill_time, revision, "
            "source_timestamp FROM broker_fills ORDER BY fill_time, account_id, exec_id"
        ).fetchall()
        return [self._fill_from_row(row) for row in rows]

    def unbound_fills_in_tx(self, conn: Any, account_id: str) -> list[BrokerFillRow]:
        rows = conn.execute(
            "SELECT account_id, exec_id, order_entity_id, perm_id, client_order_id, session_epoch, conid, "
            "side, quantity, price, commission, commission_currency, realized_pnl, fill_time, revision, "
            "source_timestamp FROM broker_fills WHERE account_id = ? AND order_entity_id IS NULL "
            "ORDER BY fill_time, exec_id",
            [account_id],
        ).fetchall()
        return [self._fill_from_row(row) for row in rows]

    def open_generation_in_tx(
        self, conn: Any, sources: tuple[str, ...], started_at: dt.datetime
    ) -> int:
        return conn.execute(
            "INSERT INTO broker_sync_generations "
            "(status, sources_required, sources_complete, started_at) VALUES ('staging', ?, '[]', ?) "
            "RETURNING generation_id",
            [json.dumps(list(sources)), started_at],
        ).fetchone()[0]

    def mark_source_complete_in_tx(self, conn: Any, generation_id: int, source: str) -> None:
        row = conn.execute(
            "SELECT sources_complete FROM broker_sync_generations WHERE generation_id = ?", [generation_id]
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown broker generation {generation_id}")
        complete = set(_json_value(row[0]))
        complete.add(source)
        conn.execute(
            "UPDATE broker_sync_generations SET sources_complete = ? WHERE generation_id = ?",
            [json.dumps(sorted(complete)), generation_id],
        )

    def mark_generation_promoted_in_tx(
        self, conn: Any, generation_id: int, cursor: int, completed_at: dt.datetime
    ) -> None:
        conn.execute(
            "UPDATE broker_sync_generations SET status = 'promoted', promoted_cursor = ?, completed_at = ?, "
            "abandon_reason = NULL WHERE generation_id = ?",
            [cursor, completed_at, generation_id],
        )

    def mark_generation_abandoned_in_tx(
        self, conn: Any, generation_id: int, reason: str, completed_at: dt.datetime
    ) -> None:
        conn.execute(
            "UPDATE broker_sync_generations SET status = 'abandoned', abandon_reason = ?, completed_at = ? "
            "WHERE generation_id = ?",
            [reason, completed_at, generation_id],
        )

    def stage_in_tx(
        self, conn: Any, generation_id: int, ingest_seq: int, source: str, entity_type: str,
        canonical_key: str, record_json: str, record_kind: str = "upsert",
    ) -> None:
        conn.execute(
            "INSERT INTO broker_sync_staging "
            "(generation_id, ingest_seq, source, entity_type, canonical_key, record_kind, record_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [generation_id, ingest_seq, source, entity_type, canonical_key, record_kind, record_json],
        )

    def staged_rows_in_tx(self, conn: Any, generation_id: int) -> list[SimpleNamespace]:
        rows = conn.execute(
            "SELECT ingest_seq, source, entity_type, canonical_key, record_kind, record_json "
            "FROM broker_sync_staging WHERE generation_id = ? ORDER BY ingest_seq",
            [generation_id],
        ).fetchall()
        return [
            SimpleNamespace(
                ingest_seq=row[0], source=row[1], entity_type=row[2], canonical_key=row[3],
                record_kind=row[4], record_json=row[5],
            )
            for row in rows
        ]

    def purge_staging_in_tx(self, conn: Any, generation_id: int) -> None:
        conn.execute("DELETE FROM broker_sync_staging WHERE generation_id = ?", [generation_id])

    def latest_promoted_generation_in_tx(self, conn: Any) -> Optional[int]:
        row = conn.execute(
            "SELECT MAX(generation_id) FROM broker_sync_generations WHERE status = 'promoted'"
        ).fetchone()
        return None if row is None or row[0] is None else row[0]

    def capture_risk_snapshot_in_tx(
        self, conn: Any, account_id: str
    ) -> BrokerRiskSnapshot:
        """Read all broker risk inputs under one caller-owned transaction.

        A promoted generation is a completeness barrier, not a claim that
        independently timestamped market data shares its clock. Materialized
        broker deltas newer than that promotion are included and fenced by the
        journal ``source_cursor`` visible in this same DuckDB snapshot.
        """
        promoted = conn.execute(
            "SELECT generation_id, promoted_cursor, completed_at "
            "FROM broker_sync_generations WHERE status = 'promoted' "
            "ORDER BY generation_id DESC LIMIT 1"
        ).fetchone()
        if promoted is None:
            raise BrokerRiskSnapshotError(
                "NO_PROMOTED_GENERATION", "no complete broker generation is available"
            )
        generation_id, promoted_cursor, promoted_at = promoted
        if promoted_cursor is None or int(promoted_cursor) < 0 or promoted_at is None:
            raise BrokerRiskSnapshotError(
                "INVALID_GENERATION_CURSOR", "promoted generation has invalid provenance"
            )

        staging = conn.execute(
            "SELECT generation_id FROM broker_sync_generations "
            "WHERE status = 'staging' AND generation_id > ? "
            "ORDER BY generation_id DESC LIMIT 1",
            [generation_id],
        ).fetchone()
        if staging is not None:
            raise BrokerRiskSnapshotError(
                "GENERATION_STAGING",
                f"newer broker generation {staging[0]} is still staging",
            )

        accounts = self.select_accounts_in_tx(conn)
        account = next((row for row in accounts if row.account_id == account_id), None)
        if account is None:
            if accounts:
                raise BrokerRiskSnapshotError(
                    "ACCOUNT_MISMATCH", f"broker state does not contain account {account_id!r}"
                )
            raise BrokerRiskSnapshotError(
                "ACCOUNT_STATE_UNAVAILABLE", "promoted generation contains no account state"
            )
        try:
            net_liquidation = float(account.net_liquidation)
        except (TypeError, ValueError) as exc:
            raise BrokerRiskSnapshotError(
                "INVALID_NET_LIQUIDATION", "net liquidation is unavailable"
            ) from exc
        if not math.isfinite(net_liquidation) or net_liquidation <= 0:
            raise BrokerRiskSnapshotError(
                "INVALID_NET_LIQUIDATION", "net liquidation must be finite and positive"
            )

        positions = tuple(
            row for row in self.select_active_positions_in_tx(conn)
            if row.account_id == account_id
        )
        for row in positions:
            if not math.isfinite(float(row.quantity)):
                raise BrokerRiskSnapshotError(
                    "INVALID_POSITION", f"position {row.conid} has non-finite quantity"
                )
            if row.quantity and (
                row.market_value is None or not math.isfinite(float(row.market_value))
            ):
                raise BrokerRiskSnapshotError(
                    "INVALID_POSITION", f"position {row.conid} has no finite market value"
                )

        daily_values = [
            value for key, value in account.balances.items()
            if key.split(":", 1)[0] == "DailyPnL"
        ]
        if daily_values:
            if len(daily_values) != 1:
                raise BrokerRiskSnapshotError(
                    "INVALID_DAILY_PNL", "daily P&L is ambiguous across currencies"
                )
            try:
                daily_pnl = float(daily_values[0])
            except (TypeError, ValueError) as exc:
                raise BrokerRiskSnapshotError(
                    "INVALID_DAILY_PNL", "daily P&L is not numeric"
                ) from exc
        elif positions:
            if any(row.daily_pnl is None for row in positions):
                raise BrokerRiskSnapshotError(
                    "DAILY_PNL_UNAVAILABLE", "daily P&L is missing for an active position"
                )
            daily_pnl = sum(float(row.daily_pnl) for row in positions)
        else:
            daily_pnl = 0.0
        if not math.isfinite(daily_pnl):
            raise BrokerRiskSnapshotError(
                "INVALID_DAILY_PNL", "daily P&L must be finite"
            )

        working_orders = tuple(
            row for row in self.select_working_orders_in_tx(conn)
            if row.account_id == account_id
        )
        for row in working_orders:
            values = (row.total_quantity, row.filled_quantity)
            if any(not math.isfinite(float(value)) for value in values):
                raise BrokerRiskSnapshotError(
                    "INVALID_WORKING_ORDER",
                    f"working order {row.order_entity_id!r} has non-finite quantity",
                )

        cursor_row = conn.execute(
            "SELECT COALESCE(MAX(source_cursor), 0) FROM domain_event_journal"
        ).fetchone()
        source_cursor = int(cursor_row[0]) if cursor_row is not None else 0
        if source_cursor < int(promoted_cursor):
            raise BrokerRiskSnapshotError(
                "INVALID_GENERATION_CURSOR",
                "promoted cursor is ahead of the visible event journal",
            )

        return BrokerRiskSnapshot(
            generation_id=int(generation_id),
            source_cursor=source_cursor,
            promoted_at=promoted_at,
            account_id=account.account_id,
            account_mode=account.account_mode,
            net_liquidation=net_liquidation,
            daily_pnl=daily_pnl,
            positions=positions,
            working_orders=working_orders,
        )


@dataclass(frozen=True)
class _TableAdapter:
    entity_type: str
    store: BrokerStateStore
    select: Callable[[Any], list[dict[str, Any]]]

    def select_active(self, conn: Any) -> list[dict[str, Any]]:
        return self.select(conn)

    def checkpoint(self, conn: Any) -> list[dict[str, Any]]:
        return self.select(conn)


def broker_materialized_adapters(store: BrokerStateStore) -> list[_TableAdapter]:
    return [
        _TableAdapter("account", store, lambda conn: [row.to_payload() for row in store.select_accounts_in_tx(conn)]),
        _TableAdapter("position", store, lambda conn: [row.to_payload() for row in store.select_active_positions_in_tx(conn)]),
        _TableAdapter("order", store, lambda conn: [row.to_payload() for row in store.select_active_orders_in_tx(conn)]),
        _TableAdapter("fill", store, lambda conn: [row.to_payload() for row in store.select_fills_in_tx(conn)]),
    ]
