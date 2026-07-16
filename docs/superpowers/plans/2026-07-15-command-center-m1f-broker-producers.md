# M1-F2 Broker Producers, Correlation, and Quote Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the trader service durable, revisioned broker truth — account, position, order, fill, risk, and reconciliation producers with immutable order identity, an IB connect/reconnect completeness barrier, and reference-counted quote coverage.

**Architecture:** Normalize every IB callback into an immutable observation on the IB thread, apply it on one dedicated writer thread through the `[M1-F1]` `DuckDBConnection.transaction` + `DomainJournal.mutate` unit of work, and journal only changed entities. Order identity is minted once at first observation from the `orderRef`-carried order-group correlation; `permId`, client order IDs, and parent IDs are aliases that never rekey. Connect-time broker snapshots stage into a generation that promotes in one transaction only after every IB end marker; quotes stay on the existing ticker PubSub behind a new acquire/release subscription manager.

**Tech Stack:** CPython 3.12.13, DuckDB, dataclasses, threading, ib_async event callbacks, JSON, pytest.

## Global Constraints

- This plan implements spec sections §5.4 (producer contract + broker-snapshot completeness barrier), §5.5 final paragraph and §6 (order/fill identity and aliases), §7 state-bounds bullet 1 (quote-subscription manager), and §13.2 (producer, correlation, and generation tests) of `docs/superpowers/specs/2026-07-15-realtime-trading-command-center-design.md`.
- `[M1-F1]` interfaces are consumed as frozen and never redefined here: `DuckDBConnection.transaction(fn)`, `trader/domain/events.py` (`DomainMutation`, `DomainEvent`, `EntityKey`), `trader/domain/identity.py` helpers, `SchemaMigrator.apply(version, name, statements)`, `DomainJournal.mutate(conn, mutation, write_materialized) -> DomainEvent`, `DomainSnapshotService.snapshot_with_cursor()`, `DomainFeedService.read_domain_events(after_cursor, limit, wait_ms)`.
- `[M1-F1]` implementation assumptions used by this plan: the `write_materialized` callback signature is `write_materialized(conn, entity_revision: int) -> None` and must stamp the materialized row with exactly that revision; the `MaterializedAdapter` protocol is `entity_type`, `select_active(conn)`, `checkpoint(conn)`; `DomainJournal(db)` constructs from a `DuckDBConnection` and applies its own migrations via `migrate(migrator)`. If the landed `[M1-F1]` code differs, adapt these call sites — never the frozen names in the plan index.
- `schema_migrations` version numbers 10–19 are reserved for `[M1-F2]`; `[M1-F1]` owns 1–9 and `[M1-F3]` owns 20–29.
- IB eventkit callbacks only normalize and enqueue; DuckDB writes happen exclusively on the single `BrokerIngest` writer thread (or a test's explicit `drain_once()`).
- Changed-fields-only persistence: a normalized observation that leaves the merged materialized row identical produces no revision bump and no journal row.
- Every materialized mutation and its journal row commit atomically through a single `DomainJournal.mutate(conn, mutation, write_materialized)` call on a `journal.connect()` connection (see the pre-flight resolution below). The materialized-store write happens INSIDE the `write_materialized` callback (F1 runs it in `mutate()`'s own transaction). Producers must NOT wrap `mutate()` in `DuckDBConnection.transaction` — `mutate()` self-manages its `BEGIN/COMMIT`, and a wrapper double-`BEGIN`s (DuckDB rejects nested `BEGIN` → the producer crashes on its first write).

> **Pre-flight resolution (mutate() usage + journal-file topology — binding, from M1-F1 T3 verification).** The landed `[M1-F1]` journal lives in a dedicated `journal_duckdb_path` file with a self-managing `mutate()` transaction on `journal.connect()`. Therefore: (1) every domain-journaled materialized table this plan defines (broker positions/orders/fills) lives in that SAME journal file, so the materialized row and its journal event commit in one transaction; (2) replace every `self.db.transaction(lambda conn: ... journal.mutate(conn, ...) ...)` with `conn = self.journal.connect(); self.journal.mutate(conn, mutation, write_materialized)`, moving the `store.*_in_tx` materialized write inside `write_materialized`; (3) batch/generation atomicity: since each `mutate()` self-commits, drain-batch observations commit per-mutation and rely on `event_id` idempotent replay for crash recovery, while generation-promote bookkeeping (tombstones + `mark_generation_promoted_in_tx` + `purge_staging_in_tx`) runs as its OWN single transaction on the journal connection so a promote is all-or-nothing. Do not wrap multiple `mutate()` calls in one outer transaction.
- Absence tombstones apply only to complete enumerable sets — current positions and open orders. Completed-order and execution queries merge and deduplicate within their broker time window and never tombstone durable history.
- An order's `order_entity_id` is immutable from first observation; a late `permId` binds an alias and never rekeys or starts a second revision stream. Fills are keyed `account + execId` and may arrive before their order alias resolves; later resolution updates the same fill revision stream.
- Risk namespaces `policy:`, `projection:`, and `decision:` are separate entities with independent monotonic revision streams; the account projection is debounced by at most 100 ms.
- `quote.updated` stays on the existing ticker PubSub and is never written to the domain journal, replay ring, or domain FIFO.
- Quote subscriptions are released 600 seconds (10 minutes) after the last reference disappears, never immediately.
- No readiness (`SnapshotWithCursor.broker_generation`) until one complete broker-sync generation has promoted; a timeout or disconnect abandons the staging generation and leaves the prior view visible but stale.

---

### Task 1: Broker materialized stores and schema migration

**Files:**
- Create: `trader/data/broker_state.py`
- Create: `tests/test_broker_state.py`

**Interfaces:**
- Consumes: `DuckDBConnection` (`trader/data/duckdb_store.py`), `SchemaMigrator.apply(version, name, statements)` (`trader/data/schema_migrations.py`).
- Produces: tables `broker_account_state`, `broker_positions`, `broker_orders`, `broker_order_aliases`, `broker_fills`, `broker_sync_generations`, `broker_sync_staging` (migration version 10).
- Produces: `BrokerAccountRow`, `BrokerPositionRow`, `BrokerOrderRow`, `BrokerFillRow` frozen dataclasses with `to_payload()` and `same_fields(other)`.
- Produces: `BrokerStateStore` with `migrate(migrator)`, per-table `get_*_in_tx` / `upsert_*_in_tx` / `tombstone_*_in_tx` methods, alias binding/lookup, staging/generation methods, `latest_promoted_generation_in_tx(conn) -> int | None`, and `broker_materialized_adapters(store)` for the `[M1-F1]` snapshot registry.

- [ ] **Step 1: Write failing store tests**

Create `tests/test_broker_state.py`:

```python
import datetime as dt
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from trader.data.broker_state import (
    BrokerFillRow,
    BrokerPositionRow,
    BrokerStateStore,
)
from trader.data.duckdb_store import DuckDBConnection
from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator

UTC_NOW = dt.datetime(2026, 7, 15, 13, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def env(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "broker.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)  # [M1-F1] journal tables — reuse its canonical bootstrap
    store = BrokerStateStore(db)
    store.migrate(migrator)
    return SimpleNamespace(db=db, journal=journal, store=store)


def _position(quantity=10.0, revision=1, deleted=False):
    return BrokerPositionRow(
        account_id="DU123", conid=265598, symbol="AAPL", sec_type="STK",
        exchange="NASDAQ", currency="USD", quantity=quantity,
        average_cost=180.0, market_price=185.0, market_value=1850.0,
        unrealized_pnl=50.0, realized_pnl=0.0, daily_pnl=12.0,
        deleted=deleted, revision=revision, source_timestamp=UTC_NOW,
    )


def test_migration_is_idempotent_and_creates_all_tables(env):
    # A second migrate() must be a recorded no-op, not a failure.
    env.store.migrate(SchemaMigrator(env.db))
    tables = {
        row[0]
        for row in env.db.execute(
            "SELECT table_name FROM information_schema.tables", fetch="all"
        )
    }
    assert {
        "broker_account_state", "broker_positions", "broker_orders",
        "broker_order_aliases", "broker_fills",
        "broker_sync_generations", "broker_sync_staging",
    } <= tables


def test_position_upsert_replaces_by_account_and_conid(env):
    def _tx(conn):
        env.store.upsert_position_in_tx(conn, _position(quantity=10.0, revision=1))
        env.store.upsert_position_in_tx(conn, _position(quantity=25.0, revision=2))
        return env.store.get_position_in_tx(conn, "DU123", 265598)

    row = env.db.transaction(_tx)
    assert row.quantity == 25.0
    assert row.revision == 2
    count = env.db.execute("SELECT COUNT(*) FROM broker_positions", fetch="one")[0]
    assert count == 1


def test_fill_identity_is_unique_per_account_and_exec_id(env):
    fill = BrokerFillRow(
        account_id="DU123", exec_id="0001.abc", order_entity_id=None,
        perm_id=777, client_order_id=5, session_epoch="s1", conid=265598,
        side="BUY", quantity=10.0, price=185.0, commission=None,
        commission_currency=None, realized_pnl=None, fill_time=UTC_NOW,
        revision=1, source_timestamp=UTC_NOW,
    )

    def _tx(conn):
        env.store.upsert_fill_in_tx(conn, fill)
        env.store.upsert_fill_in_tx(conn, replace(fill, commission=1.5, revision=2))
        return env.store.get_fill_in_tx(conn, "DU123", "0001.abc")

    row = env.db.transaction(_tx)
    assert row.commission == 1.5 and row.revision == 2
    assert env.db.execute("SELECT COUNT(*) FROM broker_fills", fetch="one")[0] == 1


def test_alias_binding_is_idempotent_and_scoped(env):
    def _tx(conn):
        env.store.bind_alias_in_tx(conn, "perm_id", "777", "DU123", "", "grp1:entry", UTC_NOW)
        env.store.bind_alias_in_tx(conn, "perm_id", "777", "DU123", "", "grp1:entry", UTC_NOW)
        env.store.bind_alias_in_tx(conn, "client_order_id", "5", "DU123", "s1", "grp1:entry", UTC_NOW)
        env.store.bind_alias_in_tx(conn, "client_order_id", "5", "DU123", "s2", "ext:other", UTC_NOW)
        return (
            env.store.find_order_by_alias_in_tx(conn, "perm_id", "777", "DU123", ""),
            env.store.find_order_by_alias_in_tx(conn, "client_order_id", "5", "DU123", "s1"),
            env.store.find_order_by_alias_in_tx(conn, "client_order_id", "5", "DU123", "s2"),
        )

    by_perm, by_cid_s1, by_cid_s2 = env.db.transaction(_tx)
    assert by_perm == "grp1:entry"
    assert by_cid_s1 == "grp1:entry"
    assert by_cid_s2 == "ext:other"  # session-scoped: reuse after restart maps elsewhere


def test_tombstoned_position_is_excluded_from_active(env):
    def _tx(conn):
        env.store.upsert_position_in_tx(conn, _position(revision=1))
        env.store.tombstone_position_in_tx(conn, "DU123", 265598, revision=2, source_timestamp=UTC_NOW)
        return env.store.select_active_positions_in_tx(conn)

    assert env.db.transaction(_tx) == []


def test_generation_lifecycle_rows(env):
    def _open(conn):
        return env.store.open_generation_in_tx(conn, ("account", "positions"), UTC_NOW)

    generation_id = env.db.transaction(_open)

    def _rest(conn):
        env.store.stage_in_tx(conn, generation_id, 1, "positions", "position",
                              "position:DU123:265598", json.dumps({"k": "v"}))
        env.store.mark_source_complete_in_tx(conn, generation_id, "account")
        env.store.mark_generation_promoted_in_tx(conn, generation_id, cursor=17, completed_at=UTC_NOW)
        env.store.purge_staging_in_tx(conn, generation_id)
        return (
            env.store.latest_promoted_generation_in_tx(conn),
            env.store.staged_rows_in_tx(conn, generation_id),
        )

    latest, staged = env.db.transaction(_rest)
    assert latest == generation_id
    assert staged == []
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_broker_state.py -q`

Expected: FAIL because `trader/data/broker_state.py` does not exist.

- [ ] **Step 3: Implement the schema and store**

Create `trader/data/broker_state.py`. The migration (version 10) uses named columns, `revision BIGINT NOT NULL`, and uniqueness on every identity key:

```python
"""Broker materialized state — trader-owned revisioned tables. [M1-F2]

Write methods ending in ``_in_tx`` take the open DuckDBPyConnection supplied
by ``DuckDBConnection.transaction`` / ``DomainJournal.mutate`` and must not
open another connection.
"""
import dataclasses
import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, Optional

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
    """CREATE SEQUENCE IF NOT EXISTS broker_sync_generation_seq START 1""",
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


def _same_fields(a, b) -> bool:
    da, db_ = dataclasses.asdict(a), dataclasses.asdict(b)
    for key in _EXCLUDED_FROM_COMPARISON:
        da.pop(key, None)
        db_.pop(key, None)
    return da == db_


def _json_ts(value: Optional[dt.datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class BrokerAccountRow:
    account_id: str
    account_mode: str
    net_liquidation: Optional[float]
    total_cash: Optional[float]
    buying_power: Optional[float]
    available_funds: Optional[float]
    maintenance_margin: Optional[float]
    balances: dict          # "tag:currency" -> raw IB value string
    revision: int
    source_timestamp: dt.datetime

    def same_fields(self, other: 'BrokerAccountRow') -> bool:
        return _same_fields(self, other)

    def to_payload(self) -> dict:
        payload = dataclasses.asdict(self)
        payload["source_timestamp"] = _json_ts(self.source_timestamp)
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

    def same_fields(self, other: 'BrokerPositionRow') -> bool:
        return _same_fields(self, other)

    def to_payload(self) -> dict:
        payload = dataclasses.asdict(self)
        payload["source_timestamp"] = _json_ts(self.source_timestamp)
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

    def same_fields(self, other: 'BrokerOrderRow') -> bool:
        return _same_fields(self, other)

    def to_payload(self) -> dict:
        payload = dataclasses.asdict(self)
        payload["source_timestamp"] = _json_ts(self.source_timestamp)
        return payload


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

    def same_fields(self, other: 'BrokerFillRow') -> bool:
        return _same_fields(self, other)

    def to_payload(self) -> dict:
        payload = dataclasses.asdict(self)
        payload["fill_time"] = _json_ts(self.fill_time)
        payload["source_timestamp"] = _json_ts(self.source_timestamp)
        return payload
```

Store implementation (delete-then-insert keeps upserts obviously correct under DuckDB unique constraints):

```python
class BrokerStateStore:
    def __init__(self, db):
        self.db = db

    def migrate(self, migrator) -> None:
        migrator.apply(BROKER_STATE_MIGRATION_VERSION, "broker_state_tables", _STATEMENTS)

    # ---- generic upsert helper -----------------------------------------
    @staticmethod
    def _upsert(conn, table: str, key: dict, values: dict) -> None:
        where = " AND ".join(f"{c} = ?" for c in key)
        conn.execute(f"DELETE FROM {table} WHERE {where}", list(key.values()))
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))

    # ---- account --------------------------------------------------------
    def upsert_account_in_tx(self, conn, row: BrokerAccountRow) -> None:
        self._upsert(conn, "broker_account_state", {"account_id": row.account_id}, {
            "account_id": row.account_id, "account_mode": row.account_mode,
            "net_liquidation": row.net_liquidation, "total_cash": row.total_cash,
            "buying_power": row.buying_power, "available_funds": row.available_funds,
            "maintenance_margin": row.maintenance_margin,
            "balances": json.dumps(row.balances), "revision": row.revision,
            "source_timestamp": row.source_timestamp,
            "updated_at": dt.datetime.now(dt.timezone.utc),
        })

    def get_account_in_tx(self, conn, account_id: str) -> Optional[BrokerAccountRow]:
        row = conn.execute(
            "SELECT account_id, account_mode, net_liquidation, total_cash, buying_power, "
            "available_funds, maintenance_margin, balances, revision, source_timestamp "
            "FROM broker_account_state WHERE account_id = ?", [account_id]).fetchone()
        if row is None:
            return None
        return BrokerAccountRow(
            account_id=row[0], account_mode=row[1], net_liquidation=row[2],
            total_cash=row[3], buying_power=row[4], available_funds=row[5],
            maintenance_margin=row[6], balances=json.loads(row[7]),
            revision=row[8], source_timestamp=row[9],
        )
```

Implement the remaining methods with the same explicit-column pattern (no `SELECT *`):

- `upsert_position_in_tx(conn, row)` / `get_position_in_tx(conn, account_id, conid)` / `tombstone_position_in_tx(conn, account_id, conid, revision, source_timestamp)` (sets `deleted=TRUE`, `quantity=0`) / `select_active_positions_in_tx(conn)` (`WHERE NOT deleted`).
- `upsert_order_in_tx(conn, row)` / `get_order_in_tx(conn, order_entity_id)` / `tombstone_order_in_tx(conn, order_entity_id, revision, source_timestamp)` / `select_working_orders_in_tx(conn)` (`WHERE NOT deleted AND status IN ('PendingSubmit','ApiPending','PreSubmitted','Submitted','PendingCancel')`).
- `bind_alias_in_tx(conn, alias_type, alias_value, account_id, session_epoch, order_entity_id, bound_at)` using `INSERT OR IGNORE INTO broker_order_aliases ...` (idempotent) and `find_order_by_alias_in_tx(conn, alias_type, alias_value, account_id, session_epoch) -> str | None`.
- `upsert_fill_in_tx(conn, row)` / `get_fill_in_tx(conn, account_id, exec_id)` / `unbound_fills_in_tx(conn, account_id)` (`WHERE order_entity_id IS NULL`).
- Generation methods: `open_generation_in_tx(conn, sources, started_at) -> int` (INSERT with `status='staging'`, `sources_required=json.dumps(list(sources))`, `sources_complete='[]'`, `RETURNING generation_id`), `mark_source_complete_in_tx(conn, generation_id, source)` (read-modify-write `sources_complete`), `mark_generation_promoted_in_tx(conn, generation_id, cursor, completed_at)`, `mark_generation_abandoned_in_tx(conn, generation_id, reason, completed_at)`, `stage_in_tx(conn, generation_id, ingest_seq, source, entity_type, canonical_key, record_json, record_kind=...)`, `staged_rows_in_tx(conn, generation_id)` (ordered by `ingest_seq`, returns `list[SimpleNamespace(ingest_seq, source, entity_type, canonical_key, record_kind, record_json)]`), `purge_staging_in_tx(conn, generation_id)`, `latest_promoted_generation_in_tx(conn) -> int | None` (`SELECT MAX(generation_id) ... WHERE status = 'promoted'`).

Finally add the `[M1-F1]` snapshot adapters:

```python
@dataclass(frozen=True)
class _TableAdapter:
    entity_type: str
    store: 'BrokerStateStore'
    select: Any  # Callable[[conn], list[dict]]

    def select_active(self, conn) -> list:
        return self.select(conn)

    def checkpoint(self, conn) -> list:
        return self.select(conn)


def broker_materialized_adapters(store: BrokerStateStore) -> list:
    return [
        _TableAdapter("account", store,
                      lambda conn: [r.to_payload() for r in store.select_accounts_in_tx(conn)]),
        _TableAdapter("position", store,
                      lambda conn: [r.to_payload() for r in store.select_active_positions_in_tx(conn)]),
        _TableAdapter("order", store,
                      lambda conn: [r.to_payload() for r in store.select_active_orders_in_tx(conn)]),
        _TableAdapter("fill", store,
                      lambda conn: [r.to_payload() for r in store.select_fills_in_tx(conn)]),
    ]
```

(`select_accounts_in_tx`, `select_active_orders_in_tx` — `WHERE NOT deleted` — and `select_fills_in_tx` are trivial explicit-column selects added alongside.)

- [ ] **Step 4: Run tests and commit**

Run: `uv run --frozen pytest tests/test_broker_state.py -q`

Expected: PASS.

```bash
git add trader/data/broker_state.py tests/test_broker_state.py
git commit -m "feat(m1-f2): add broker materialized stores and migration"
```

### Task 2: Account and position producers off the IB thread

**Files:**
- Create: `trader/trading/broker_ingest.py`
- Create: `tests/test_broker_ingest.py`

**Interfaces:**
- Consumes: `DomainJournal.mutate(conn, mutation, write_materialized)`, `DomainMutation` (`trader/domain/events.py`), `position_entity_id(account, conid)` (`trader/domain/identity.py`), `BrokerStateStore` from Task 1.
- Produces: `AccountValueObservation`, `PositionObservation` frozen dataclasses with `normalize_account_value(av, now)`, `normalize_position(pos, now)`, `normalize_portfolio_item(item, now)` module functions.
- Produces: `BrokerIngest(db, journal, store, account_id, account_mode, session_epoch=None, clock=None)` with IB-thread-safe callbacks `on_account_value(av)`, `on_position(position)`, `on_portfolio_item(item)`, writer-thread lifecycle `start()` / `stop()`, and the synchronous test seam `drain_once() -> int`.
- Trader wiring is deliberately deferred to Task 5 (the generation must be open before IB state is requested).

- [ ] **Step 1: Write failing producer tests**

Create `tests/test_broker_ingest.py`:

```python
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerStateStore
from trader.data.duckdb_store import DuckDBConnection
from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.broker_ingest import BrokerIngest

UTC_NOW = dt.datetime(2026, 7, 15, 13, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def env(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "ingest.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    store = BrokerStateStore(db)
    store.migrate(migrator)
    ingest = BrokerIngest(
        db=db, journal=journal, store=store,
        account_id="DU123", account_mode="paper",
        session_epoch="s1", clock=lambda: UTC_NOW,
    )
    return SimpleNamespace(db=db, journal=journal, store=store, ingest=ingest)


def _events(db):
    rows = db.execute(
        "SELECT event_type, entity_type, entity_id, operation, entity_revision, payload "
        "FROM domain_event_journal ORDER BY source_cursor",
        fetch="all",
    )
    return [
        {"event_type": r[0], "entity_type": r[1], "entity_id": r[2],
         "operation": r[3], "entity_revision": r[4],
         "payload": json.loads(r[5]) if r[5] else None}
        for r in rows
    ]


def fake_account_value(tag="NetLiquidation", value="50000.0", currency="USD"):
    return SimpleNamespace(account="DU123", tag=tag, value=value,
                           currency=currency, modelCode="")


def fake_position(conid=265598, qty=10.0, avg=180.0):
    contract = SimpleNamespace(conId=conid, symbol="AAPL", secType="STK",
                               exchange="NASDAQ", currency="USD")
    return SimpleNamespace(account="DU123", contract=contract,
                           position=qty, avgCost=avg)


def fake_portfolio_item(conid=265598, qty=10.0):
    contract = SimpleNamespace(conId=conid, symbol="AAPL", secType="STK",
                               exchange="NASDAQ", currency="USD")
    return SimpleNamespace(account="DU123", contract=contract, position=qty,
                           marketPrice=185.0, marketValue=1850.0,
                           averageCost=180.0, unrealizedPNL=50.0,
                           realizedPNL=0.0)


def test_callbacks_only_enqueue_until_drained(env):
    env.ingest.on_position(fake_position())
    assert _events(env.db) == []  # nothing persisted on the IB callback thread
    assert env.ingest.drain_once() == 1
    assert len(_events(env.db)) == 1


def test_account_value_journals_only_changed_fields(env):
    env.ingest.on_account_value(fake_account_value())
    env.ingest.drain_once()
    events = _events(env.db)
    assert [e["event_type"] for e in events] == ["account.updated"]
    assert events[0]["entity_id"] == "DU123"
    assert events[0]["payload"]["net_liquidation"] == 50000.0
    assert events[0]["payload"]["account_mode"] == "paper"

    env.ingest.on_account_value(fake_account_value())  # identical value
    env.ingest.drain_once()
    assert len(_events(env.db)) == 1  # unchanged merged row: no revision, no event


def test_position_and_portfolio_callbacks_merge_into_one_entity(env):
    env.ingest.on_position(fake_position(qty=10.0))
    env.ingest.on_portfolio_item(fake_portfolio_item(qty=10.0))
    env.ingest.drain_once()
    events = _events(env.db)
    assert [e["entity_id"] for e in events] == ["DU123:265598", "DU123:265598"]
    assert [e["entity_revision"] for e in events] == [1, 2]
    # The portfolio callback merged market data on top of the bare position.
    assert events[-1]["payload"]["quantity"] == 10.0
    assert events[-1]["payload"]["market_value"] == 1850.0


def test_bare_position_does_not_erase_market_fields(env):
    env.ingest.on_portfolio_item(fake_portfolio_item(qty=10.0))
    env.ingest.drain_once()
    env.ingest.on_position(fake_position(qty=10.0))  # no market fields observed
    env.ingest.drain_once()
    assert len(_events(env.db)) == 1  # merged row unchanged: no new event


def test_zero_quantity_emits_tombstone_and_allows_resurrection(env):
    env.ingest.on_position(fake_position(qty=10.0))
    env.ingest.on_position(fake_position(qty=0.0))
    env.ingest.on_position(fake_position(qty=5.0))
    env.ingest.drain_once()
    events = _events(env.db)
    assert [(e["operation"], e["entity_revision"]) for e in events] == [
        ("upsert", 1), ("delete", 2), ("upsert", 3),
    ]
    assert events[1]["payload"] is None  # tombstone carries no payload


def test_zero_quantity_without_existing_row_is_ignored(env):
    env.ingest.on_position(fake_position(qty=0.0))
    env.ingest.drain_once()
    assert _events(env.db) == []
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_broker_ingest.py -q`

Expected: FAIL because `trader/trading/broker_ingest.py` does not exist.

- [ ] **Step 3: Implement normalization and the writer thread**

Create `trader/trading/broker_ingest.py`:

```python
"""Broker producers — normalize IB callbacks into revisioned domain events. [M1-F2]

IB eventkit callbacks run on the IB loop; they must never touch DuckDB.
Callbacks normalize into frozen observation dataclasses and enqueue; one
writer thread drains the queue and applies each observation through
``DuckDBConnection.transaction`` + ``DomainJournal.mutate``.
"""
import dataclasses
import datetime as dt
import queue
import threading
import uuid
from dataclasses import dataclass, replace
from typing import Callable, Optional

from trader.common.logging_helper import setup_logging
from trader.data.broker_state import (
    BrokerAccountRow, BrokerPositionRow, BrokerStateStore,
)
from trader.domain.events import DomainMutation
from trader.domain.identity import position_entity_id

logging = setup_logging(module_name='broker_ingest')

_UNSET_DOUBLE = 1.7976931348623157e+308


def _none_if_unset(value) -> Optional[float]:
    if value is None:
        return None
    v = float(value)
    if v == 0.0 or v >= _UNSET_DOUBLE:
        return None
    return v


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
    market_price: Optional[float]     # None == "not observed by this callback"
    market_value: Optional[float]
    unrealized_pnl: Optional[float]
    realized_pnl: Optional[float]
    daily_pnl: Optional[float]
    source_timestamp: dt.datetime


def normalize_account_value(av, now: dt.datetime) -> AccountValueObservation:
    return AccountValueObservation(
        account_id=av.account, tag=av.tag,
        currency=av.currency or "", value=str(av.value),
        source_timestamp=now,
    )


def normalize_position(pos, now: dt.datetime) -> PositionObservation:
    c = pos.contract
    return PositionObservation(
        account_id=pos.account, conid=int(c.conId), symbol=c.symbol or "",
        sec_type=c.secType or "", exchange=c.exchange or None,
        currency=c.currency or "", quantity=float(pos.position),
        average_cost=float(pos.avgCost) if pos.avgCost else None,
        market_price=None, market_value=None, unrealized_pnl=None,
        realized_pnl=None, daily_pnl=None, source_timestamp=now,
    )


def normalize_portfolio_item(item, now: dt.datetime) -> PositionObservation:
    c = item.contract
    return PositionObservation(
        account_id=item.account, conid=int(c.conId), symbol=c.symbol or "",
        sec_type=c.secType or "", exchange=c.exchange or None,
        currency=c.currency or "", quantity=float(item.position),
        average_cost=float(item.averageCost) if item.averageCost else None,
        market_price=_none_if_unset(item.marketPrice),
        market_value=_none_if_unset(item.marketValue),
        unrealized_pnl=float(item.unrealizedPNL) if item.unrealizedPNL is not None else None,
        realized_pnl=float(item.realizedPNL) if item.realizedPNL is not None else None,
        daily_pnl=None, source_timestamp=now,
    )


# Account tags mapped onto named columns when currency is base/empty.
_ACCOUNT_TAG_COLUMNS = {
    "NetLiquidation": "net_liquidation",
    "TotalCashValue": "total_cash",
    "BuyingPower": "buying_power",
    "AvailableFunds": "available_funds",
    "MaintMarginReq": "maintenance_margin",
}


def merge_account_value(current: Optional[BrokerAccountRow],
                        obs: AccountValueObservation,
                        account_mode: str) -> BrokerAccountRow:
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
            pass  # non-numeric tag value stays in balances only
    return BrokerAccountRow(
        account_id=obs.account_id, account_mode=account_mode,
        balances=balances, revision=current.revision if current else 0,
        source_timestamp=obs.source_timestamp, **fields,
    )


def merge_position(current: Optional[BrokerPositionRow],
                   obs: PositionObservation) -> BrokerPositionRow:
    def keep(observed, existing):
        return observed if observed is not None else existing

    prior = current if current is not None and not current.deleted else None
    return BrokerPositionRow(
        account_id=obs.account_id, conid=obs.conid, symbol=obs.symbol,
        sec_type=obs.sec_type,
        exchange=keep(obs.exchange, prior.exchange if prior else None),
        currency=obs.currency, quantity=obs.quantity,
        average_cost=keep(obs.average_cost, prior.average_cost if prior else None),
        market_price=keep(obs.market_price, prior.market_price if prior else None),
        market_value=keep(obs.market_value, prior.market_value if prior else None),
        unrealized_pnl=keep(obs.unrealized_pnl, prior.unrealized_pnl if prior else None),
        realized_pnl=keep(obs.realized_pnl, prior.realized_pnl if prior else None),
        daily_pnl=keep(obs.daily_pnl, prior.daily_pnl if prior else None),
        deleted=False, revision=current.revision if current else 0,
        source_timestamp=obs.source_timestamp,
    )


class BrokerIngest:
    def __init__(self, db, journal, store: BrokerStateStore,
                 account_id: str, account_mode: str,
                 session_epoch: Optional[str] = None,
                 clock: Optional[Callable[[], dt.datetime]] = None):
        self.db = db
        self.journal = journal
        self.store = store
        self.account_id = account_id
        self.account_mode = account_mode
        self.session_epoch = session_epoch or uuid.uuid4().hex
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._queue: queue.Queue = queue.Queue()
        self._ingest_seq = 0
        self._generation = None            # Task 5
        self._apply_lock = threading.Lock()
        self._stop = threading.Event()
        self._writer: Optional[threading.Thread] = None

    # ---- IB-thread-safe callbacks: normalize and enqueue only ----------
    def on_account_value(self, av) -> None:
        self._queue.put(normalize_account_value(av, self.clock()))

    def on_position(self, position) -> None:
        self._queue.put(normalize_position(position, self.clock()))

    def on_portfolio_item(self, item) -> None:
        self._queue.put(normalize_portfolio_item(item, self.clock()))

    # ---- writer thread ---------------------------------------------------
    def start(self) -> None:
        self._writer = threading.Thread(target=self._drain_loop,
                                        name="broker-ingest", daemon=True)
        self._writer.start()

    def stop(self) -> None:
        self._stop.set()
        if self._writer is not None:
            self._writer.join(timeout=5)

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
            except Exception as ex:  # a poison record must not kill the thread
                logging.error("broker ingest batch failed: %s", ex, exc_info=True)

    def drain_once(self) -> int:
        """Synchronously apply everything currently queued (test seam)."""
        batch = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._apply_batch(batch)
        return len(batch)

    def _apply_batch(self, batch: list) -> None:
        with self._apply_lock:
            def _tx(conn):
                for record in batch:
                    self._ingest_seq += 1
                    if self._generation is not None:
                        self._stage(conn, self._ingest_seq, record)  # Task 5
                        continue
                    self._apply_record(conn, record)
            self.db.transaction(_tx)

    def _apply_record(self, conn, record) -> None:
        if isinstance(record, AccountValueObservation):
            self._apply_account_value(conn, record)
        elif isinstance(record, PositionObservation):
            self._apply_position(conn, record)
        else:
            raise TypeError(f"unknown ingest record: {type(record)!r}")

    # ---- appliers ---------------------------------------------------------
    def _apply_account_value(self, conn, obs: AccountValueObservation):
        current = self.store.get_account_in_tx(conn, obs.account_id)
        merged = merge_account_value(current, obs, self.account_mode)
        if current is not None and merged.same_fields(current):
            return None
        mutation = DomainMutation(
            event_type="account.updated", entity_type="account",
            entity_id=obs.account_id, operation="upsert",
            account_id=obs.account_id, source="trader_service",
            source_timestamp=obs.source_timestamp, correlation_id=None,
            payload=merged.to_payload(),
        )

        def write(conn_, revision):
            self.store.upsert_account_in_tx(conn_, replace(merged, revision=revision))

        return self.journal.mutate(conn, mutation, write)

    def _apply_position(self, conn, obs: PositionObservation):
        current = self.store.get_position_in_tx(conn, obs.account_id, obs.conid)
        entity_id = position_entity_id(obs.account_id, obs.conid)
        if obs.quantity == 0.0:
            if current is None or current.deleted:
                return None  # nothing held, nothing to tombstone
            mutation = DomainMutation(
                event_type="position.updated", entity_type="position",
                entity_id=entity_id, operation="delete",
                account_id=obs.account_id, source="trader_service",
                source_timestamp=obs.source_timestamp, correlation_id=None,
                payload=None,
            )

            def write_tombstone(conn_, revision):
                self.store.tombstone_position_in_tx(
                    conn_, obs.account_id, obs.conid, revision, obs.source_timestamp)

            return self.journal.mutate(conn, mutation, write_tombstone)

        merged = merge_position(current, obs)
        if current is not None and not current.deleted and merged.same_fields(current):
            return None
        mutation = DomainMutation(
            event_type="position.updated", entity_type="position",
            entity_id=entity_id, operation="upsert",
            account_id=obs.account_id, source="trader_service",
            source_timestamp=obs.source_timestamp, correlation_id=None,
            payload=merged.to_payload(),
        )

        def write(conn_, revision):
            self.store.upsert_position_in_tx(conn_, replace(merged, revision=revision))

        return self.journal.mutate(conn, mutation, write)

    def _stage(self, conn, ingest_seq: int, record) -> None:
        raise NotImplementedError("broker-sync staging lands in Task 5")
```

- [ ] **Step 4: Run tests and commit**

Run: `uv run --frozen pytest tests/test_broker_ingest.py tests/test_broker_state.py -q`

Expected: PASS.

```bash
git add trader/trading/broker_ingest.py tests/test_broker_ingest.py
git commit -m "feat(m1-f2): produce account and position events off the IB thread"
```

### Task 3: Order producer and immutable identity correlation

**Files:**
- Create: `trader/trading/order_correlation.py`
- Modify: `trader/domain/identity.py` (add order/fill helpers if `[M1-F1]` did not already define equivalents — reuse the existing names if it did)
- Modify: `trader/trading/broker_ingest.py`
- Create: `tests/test_order_correlation.py`

**Interfaces:**
- Consumes: `BrokerStateStore` alias methods from Task 1, `DomainJournal.mutate`.
- Produces: `ORDER_REF_PREFIX = "mmr:"`, `encode_order_ref(order_group_id: str) -> str`, `decode_order_ref(order_ref: str | None) -> str | None`, `classify_leg(order_type: str, parent_id: int, client_order_id: int) -> str`.
- Produces: `OrderObservation` dataclass with `normalize_open_order(trade, now)`; `OrderCorrelator(store, session_epoch)` with `resolve_in_tx(conn, obs) -> str`.
- Produces identity helpers: `order_group_leg_entity_id(order_group_id, leg) -> str` (`f"{order_group_id}:{leg}"`), `external_order_entity_id() -> str` (`f"ext:{uuid4()}"`), `fill_entity_id(account_id, exec_id) -> str` (`f"{account_id}:{exec_id}"`).
- Produces: `BrokerIngest.on_open_order(trade)` and `on_order_status(trade)` callbacks plus `_apply_order(conn, obs) -> tuple[str, DomainEvent | None]`.
- Note: `[M1-F3]`'s `TradingCommandCoordinator` MUST write `encode_order_ref(order_group_id)` into IB `orderRef` when dispatching; that string is the only authoritative MMR-order join (spec §5.5).

- [ ] **Step 1: Write failing correlation tests**

Create `tests/test_order_correlation.py` (copy the `env` fixture and `_events` helper from `tests/test_broker_ingest.py` verbatim, importing `BrokerIngest` the same way):

```python
import datetime as dt
from types import SimpleNamespace

from trader.trading.order_correlation import (
    classify_leg, decode_order_ref, encode_order_ref,
)

UTC_NOW = dt.datetime(2026, 7, 15, 13, 0, tzinfo=dt.timezone.utc)


def fake_trade(client_order_id=5, perm_id=0, parent_id=0, order_ref="mmr:grp1",
               order_type="MKT", status="Submitted", filled=0.0, conid=265598,
               account="DU123", quantity=10.0):
    order = SimpleNamespace(
        orderId=client_order_id, permId=perm_id, parentId=parent_id,
        orderRef=order_ref, account=account, action="BUY",
        orderType=order_type, totalQuantity=quantity,
        lmtPrice=0.0, auxPrice=0.0, tif="DAY",
    )
    order_status = SimpleNamespace(status=status, filled=filled, avgFillPrice=0.0)
    contract = SimpleNamespace(conId=conid, symbol="AAPL", secType="STK",
                               exchange="NASDAQ", currency="USD")
    return SimpleNamespace(order=order, orderStatus=order_status, contract=contract)


def test_order_ref_round_trip():
    assert encode_order_ref("grp1") == "mmr:grp1"
    assert decode_order_ref("mmr:grp1") == "grp1"
    assert decode_order_ref("manual TWS ref") is None
    assert decode_order_ref(None) is None
    assert decode_order_ref("") is None


def test_classify_leg():
    assert classify_leg("MKT", 0, 5) == "entry"
    assert classify_leg("LMT", 5, 6) == "take_profit"
    assert classify_leg("STP", 5, 7) == "stop"
    assert classify_leg("TRAIL", 5, 7) == "stop"
    assert classify_leg("MOC", 5, 9) == "child-9"  # deterministic fallback


def test_first_observation_mints_immutable_group_leg_entity(env):
    env.ingest.on_open_order(fake_trade())
    env.ingest.drain_once()
    events = _events(env.db)
    assert events[0]["entity_type"] == "order"
    assert events[0]["entity_id"] == "grp1:entry"
    assert events[0]["event_type"] == "order.updated"
    assert events[0]["payload"]["status"] == "Submitted"


def test_late_perm_id_never_rekeys_or_forks_the_revision_stream(env):
    env.ingest.on_open_order(fake_trade(perm_id=0))
    env.ingest.on_order_status(fake_trade(perm_id=999888, status="Filled", filled=10.0))
    env.ingest.drain_once()
    events = _events(env.db)
    assert [e["entity_id"] for e in events] == ["grp1:entry", "grp1:entry"]
    assert [e["entity_revision"] for e in events] == [1, 2]
    # permId is now an alias for the same entity.
    alias = env.db.execute(
        "SELECT order_entity_id FROM broker_order_aliases "
        "WHERE alias_type = 'perm_id' AND alias_value = '999888'",
        fetch="one",
    )
    assert alias == ("grp1:entry",)


def test_bracket_legs_get_distinct_immutable_entities(env):
    env.ingest.on_open_order(fake_trade(client_order_id=5, order_type="MKT", parent_id=0))
    env.ingest.on_open_order(fake_trade(client_order_id=6, order_type="LMT", parent_id=5))
    env.ingest.on_open_order(fake_trade(client_order_id=7, order_type="STP", parent_id=5))
    env.ingest.drain_once()
    assert {e["entity_id"] for e in _events(env.db)} == {
        "grp1:entry", "grp1:take_profit", "grp1:stop",
    }


def test_external_order_gets_persisted_local_id(env):
    env.ingest.on_open_order(fake_trade(order_ref="manual TWS ref", perm_id=42))
    env.ingest.drain_once()
    events = _events(env.db)
    assert events[0]["entity_id"].startswith("ext:")
    assert events[0]["payload"]["is_external"] is True


def test_client_order_id_reuse_after_restart_creates_new_entity(env, tmp_path):
    env.ingest.on_open_order(fake_trade(client_order_id=5, order_ref="ref-a", perm_id=100))
    env.ingest.drain_once()
    # Simulate a trader restart: new ingest, new session epoch, same store.
    from trader.trading.broker_ingest import BrokerIngest
    restarted = BrokerIngest(
        db=env.db, journal=env.journal, store=env.store,
        account_id="DU123", account_mode="paper",
        session_epoch="s2", clock=lambda: UTC_NOW,
    )
    restarted.on_open_order(fake_trade(client_order_id=5, order_ref="ref-b", perm_id=200))
    restarted.drain_once()
    entities = {e["entity_id"] for e in _events(env.db)}
    assert len(entities) == 2  # same client id, different sessions, different orders


def test_restart_resolves_perm_id_to_same_entity(env):
    env.ingest.on_open_order(fake_trade(client_order_id=5, perm_id=100))
    env.ingest.drain_once()
    from trader.trading.broker_ingest import BrokerIngest
    restarted = BrokerIngest(
        db=env.db, journal=env.journal, store=env.store,
        account_id="DU123", account_mode="paper",
        session_epoch="s2", clock=lambda: UTC_NOW,
    )
    restarted.on_order_status(fake_trade(client_order_id=91, perm_id=100,
                                         status="Filled", filled=10.0))
    restarted.drain_once()
    entities = [e["entity_id"] for e in _events(env.db)]
    assert entities == ["grp1:entry", "grp1:entry"]  # one entity, one stream
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_order_correlation.py -q`

Expected: FAIL because `trader/trading/order_correlation.py` does not exist.

- [ ] **Step 3: Implement the correlator and order applier**

Create `trader/trading/order_correlation.py`:

```python
"""Order identity and alias correlation. [M1-F2]

An MMR ``order_entity_id`` is assigned at first observation and is immutable.
MMR-created orders derive it from the order-group correlation carried in IB
``orderRef`` plus leg identity; external orders receive a persisted local id.
Client order id, ``permId``, ``parentId``, and ``orderRef`` are aliases —
binding them later never rekeys an entity (spec §5.5/§6).
"""
import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Optional

from trader.data.broker_state import BrokerStateStore

ORDER_REF_PREFIX = "mmr:"

_STOP_TYPES = {"STP", "STP LMT", "TRAIL", "TRAIL LIMIT"}
_UNSET_DOUBLE = 1.7976931348623157e+308


def encode_order_ref(order_group_id: str) -> str:
    return f"{ORDER_REF_PREFIX}{order_group_id}"


def decode_order_ref(order_ref: Optional[str]) -> Optional[str]:
    if order_ref and order_ref.startswith(ORDER_REF_PREFIX):
        group = order_ref[len(ORDER_REF_PREFIX):]
        return group or None
    return None


def classify_leg(order_type: str, parent_id: int, client_order_id: int) -> str:
    if not parent_id:
        return "entry"
    if order_type in _STOP_TYPES:
        return "stop"
    if order_type == "LMT":
        return "take_profit"
    return f"child-{client_order_id}"  # deterministic, immutable fallback


def _price_or_none(value) -> Optional[float]:
    if value is None:
        return None
    v = float(value)
    if v == 0.0 or v >= _UNSET_DOUBLE:
        return None
    return v


@dataclass(frozen=True)
class OrderObservation:
    account_id: str
    client_order_id: int
    perm_id: int          # 0 == not yet assigned by IB
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


def normalize_open_order(trade, now: dt.datetime) -> OrderObservation:
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

    def resolve_in_tx(self, conn, obs: OrderObservation) -> str:
        # 1. permId is broker-stable across sessions — strongest alias.
        if obs.perm_id:
            entity = self.store.find_order_by_alias_in_tx(
                conn, "perm_id", str(obs.perm_id), obs.account_id, "")
            if entity:
                self._bind_aliases_in_tx(conn, entity, obs)
                return entity
        # 2. Client order id is only valid within this session epoch —
        #    IB reuses ids after every restart.
        if obs.client_order_id:
            entity = self.store.find_order_by_alias_in_tx(
                conn, "client_order_id", str(obs.client_order_id),
                obs.account_id, self.session_epoch)
            if entity:
                self._bind_aliases_in_tx(conn, entity, obs)
                return entity
        # 3. First observation: mint the immutable id.
        group_id = decode_order_ref(obs.order_ref)
        if group_id is not None:
            from trader.domain.identity import order_group_leg_entity_id
            entity = order_group_leg_entity_id(
                group_id, classify_leg(obs.order_type, obs.parent_id, obs.client_order_id))
        else:
            from trader.domain.identity import external_order_entity_id
            entity = external_order_entity_id()
        self._bind_aliases_in_tx(conn, entity, obs)
        return entity

    def _bind_aliases_in_tx(self, conn, entity: str, obs: OrderObservation) -> None:
        now = obs.source_timestamp
        if obs.perm_id:
            self.store.bind_alias_in_tx(conn, "perm_id", str(obs.perm_id),
                                        obs.account_id, "", entity, now)
        if obs.client_order_id:
            self.store.bind_alias_in_tx(conn, "client_order_id", str(obs.client_order_id),
                                        obs.account_id, self.session_epoch, entity, now)
        if obs.parent_id:
            self.store.bind_alias_in_tx(conn, "parent_id", str(obs.parent_id),
                                        obs.account_id, self.session_epoch, entity, now)
        if obs.order_ref:
            self.store.bind_alias_in_tx(conn, "order_ref", obs.order_ref,
                                        obs.account_id, "", entity, now)
```

Add to `trader/domain/identity.py` (only if `[M1-F1]` did not land equivalents; if it did, import and re-export the existing names instead of adding duplicates):

```python
def order_group_leg_entity_id(order_group_id: str, leg: str) -> str:
    return f"{order_group_id}:{leg}"


def external_order_entity_id() -> str:
    import uuid
    return f"ext:{uuid.uuid4()}"


def fill_entity_id(account_id: str, exec_id: str) -> str:
    return f"{account_id}:{exec_id}"
```

In `trader/trading/broker_ingest.py`, add the callbacks, the correlator, and the applier. `BrokerIngest.__init__` gains:

```python
from trader.trading.order_correlation import (
    OrderCorrelator, OrderObservation, normalize_open_order,
)
# in __init__:
self.correlator = OrderCorrelator(store, self.session_epoch)
```

Callbacks and applier (`_apply_record` gains an `OrderObservation` branch delegating to `_apply_order`):

```python
def on_open_order(self, trade) -> None:
    self._queue.put(normalize_open_order(trade, self.clock()))

def on_order_status(self, trade) -> None:
    self._queue.put(normalize_open_order(trade, self.clock()))

def _apply_order(self, conn, obs: OrderObservation):
    entity_id = self.correlator.resolve_in_tx(conn, obs)
    current = self.store.get_order_in_tx(conn, entity_id)
    group_id = decode_order_ref(obs.order_ref)
    merged = BrokerOrderRow(
        order_entity_id=entity_id, account_id=obs.account_id, conid=obs.conid,
        symbol=obs.symbol,
        order_group_id=group_id or (current.order_group_id if current else None),
        leg=(current.leg if current and current.leg else
             (classify_leg(obs.order_type, obs.parent_id, obs.client_order_id)
              if group_id else None)),
        is_external=group_id is None if current is None else current.is_external,
        action=obs.action, order_type=obs.order_type,
        total_quantity=obs.total_quantity, filled_quantity=obs.filled_quantity,
        avg_fill_price=obs.avg_fill_price if obs.avg_fill_price is not None
                       else (current.avg_fill_price if current else None),
        limit_price=obs.limit_price if obs.limit_price is not None
                    else (current.limit_price if current else None),
        stop_price=obs.stop_price if obs.stop_price is not None
                   else (current.stop_price if current else None),
        tif=obs.tif or (current.tif if current else None),
        status=obs.status, deleted=False,
        revision=current.revision if current else 0,
        source_timestamp=obs.source_timestamp,
    )
    if current is not None and not current.deleted and merged.same_fields(current):
        return entity_id, None
    mutation = DomainMutation(
        event_type="order.updated", entity_type="order", entity_id=entity_id,
        operation="upsert", account_id=obs.account_id, source="trader_service",
        source_timestamp=obs.source_timestamp, correlation_id=None,
        payload=merged.to_payload(),
    )

    def write(conn_, revision):
        self.store.upsert_order_in_tx(conn_, replace(merged, revision=revision))

    event = self.journal.mutate(conn, mutation, write)
    self._resolve_unbound_fills_in_tx(conn, obs, entity_id)  # no-op until Task 4
    return entity_id, event

def _resolve_unbound_fills_in_tx(self, conn, obs, entity_id: str) -> None:
    return None  # fill-before-order resolution lands in Task 4
```

Add the imports (`BrokerOrderRow`, `classify_leg`, `decode_order_ref`) at module top.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --frozen pytest tests/test_order_correlation.py tests/test_broker_ingest.py -q`

Expected: PASS.

```bash
git add trader/trading/order_correlation.py trader/domain/identity.py trader/trading/broker_ingest.py tests/test_order_correlation.py
git commit -m "feat(m1-f2): correlate orders with immutable entity identity"
```

### Task 4: Fill producer — execution dedup, commission revision, fill-before-order

**Files:**
- Modify: `trader/trading/broker_ingest.py`
- Create: `tests/test_fill_producer.py`

**Interfaces:**
- Consumes: `fill_entity_id(account_id, exec_id)`, `BrokerFillRow`, alias lookups from Tasks 1/3.
- Produces: `FillObservation`, `CommissionObservation` dataclasses with `normalize_execution(fill_obj, now)` and `normalize_commission(fill_obj, report, now)`.
- Produces: `BrokerIngest.on_exec_details(trade, fill)`, `on_commission_report(trade, fill, report)` callbacks; `_apply_fill` (journals `fill.received` exactly once per `account+execId`), `_apply_commission` (journals `fill.updated` on the same entity), and the real `_resolve_unbound_fills_in_tx` (order arrival binds earlier fills — `fill.updated` on the same revision stream).

- [ ] **Step 1: Write failing fill tests**

Create `tests/test_fill_producer.py` (copy the `env` fixture, `_events` helper, and `fake_trade` factory from the earlier test files verbatim):

```python
import datetime as dt
from types import SimpleNamespace

UTC_NOW = dt.datetime(2026, 7, 15, 13, 0, tzinfo=dt.timezone.utc)


def fake_fill(exec_id="0001.abc", perm_id=100, client_order_id=5,
              conid=265598, side="BOT", shares=10.0, price=185.0):
    execution = SimpleNamespace(
        execId=exec_id, acctNumber="DU123", permId=perm_id,
        orderId=client_order_id, side=side, shares=shares, price=price,
        time=UTC_NOW,
    )
    contract = SimpleNamespace(conId=conid, symbol="AAPL", secType="STK",
                               exchange="NASDAQ", currency="USD")
    return SimpleNamespace(execution=execution, contract=contract)


def fake_commission(commission=1.25, currency="USD", realized_pnl=None):
    return SimpleNamespace(commission=commission, currency=currency,
                           realizedPNL=realized_pnl if realized_pnl is not None
                           else 1.7976931348623157e+308)


def test_exec_details_persist_once_per_account_and_exec_id(env):
    env.ingest.on_exec_details(None, fake_fill())
    env.ingest.on_exec_details(None, fake_fill())  # IB redelivers at-least-once
    env.ingest.drain_once()
    events = _events(env.db)
    assert [e["event_type"] for e in events] == ["fill.received"]
    assert events[0]["entity_id"] == "DU123:0001.abc"
    assert events[0]["payload"]["side"] == "BUY"
    assert events[0]["payload"]["quantity"] == 10.0


def test_commission_report_revises_the_same_fill(env):
    env.ingest.on_exec_details(None, fake_fill())
    env.ingest.on_commission_report(None, fake_fill(), fake_commission(commission=1.25))
    env.ingest.drain_once()
    events = _events(env.db)
    assert [(e["event_type"], e["entity_revision"]) for e in events] == [
        ("fill.received", 1), ("fill.updated", 2),
    ]
    assert {e["entity_id"] for e in events} == {"DU123:0001.abc"}
    assert events[1]["payload"]["commission"] == 1.25


def test_duplicate_commission_report_is_a_noop(env):
    env.ingest.on_exec_details(None, fake_fill())
    env.ingest.on_commission_report(None, fake_fill(), fake_commission())
    env.ingest.on_commission_report(None, fake_fill(), fake_commission())
    env.ingest.drain_once()
    assert len(_events(env.db)) == 2  # received + one update


def test_commission_before_exec_details_creates_then_revises(env):
    env.ingest.on_commission_report(None, fake_fill(), fake_commission())
    env.ingest.drain_once()
    events = _events(env.db)
    assert [e["event_type"] for e in events] == ["fill.received", "fill.updated"]
    assert events[-1]["payload"]["commission"] == 1.25


def test_fill_before_order_resolves_on_order_arrival(env):
    env.ingest.on_exec_details(None, fake_fill(perm_id=100))
    env.ingest.drain_once()
    assert _events(env.db)[-1]["payload"]["order_entity_id"] is None

    env.ingest.on_open_order(fake_trade(client_order_id=5, perm_id=100))
    env.ingest.drain_once()
    events = _events(env.db)
    fill_events = [e for e in events if e["entity_type"] == "fill"]
    assert [(e["event_type"], e["entity_revision"]) for e in fill_events] == [
        ("fill.received", 1), ("fill.updated", 2),
    ]
    assert fill_events[-1]["payload"]["order_entity_id"] == "grp1:entry"
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_fill_producer.py -q`

Expected: FAIL because `on_exec_details` / `on_commission_report` do not exist on `BrokerIngest`.

- [ ] **Step 3: Implement fill normalization, dedup, and late binding**

In `trader/trading/broker_ingest.py` add:

```python
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


def normalize_execution(fill_obj, now: dt.datetime) -> FillObservation:
    ex, contract = fill_obj.execution, fill_obj.contract
    fill_time = ex.time if getattr(ex.time, 'tzinfo', None) else now
    return FillObservation(
        account_id=ex.acctNumber, exec_id=ex.execId,
        perm_id=int(ex.permId or 0), client_order_id=int(ex.orderId or 0),
        conid=int(contract.conId),
        side="BUY" if ex.side == "BOT" else "SELL",
        quantity=float(ex.shares), price=float(ex.price),
        fill_time=fill_time, source_timestamp=now,
    )


def normalize_commission(fill_obj, report, now: dt.datetime) -> CommissionObservation:
    return CommissionObservation(
        fill=normalize_execution(fill_obj, now),
        commission=float(report.commission),
        currency=report.currency or "",
        realized_pnl=_none_if_unset(getattr(report, "realizedPNL", None)),
    )
```

Callbacks and appliers on `BrokerIngest` (extend `_apply_record` dispatch with both new types):

```python
def on_exec_details(self, trade, fill) -> None:      # eventkit: (trade, fill)
    self._queue.put(normalize_execution(fill, self.clock()))

def on_commission_report(self, trade, fill, report) -> None:  # (trade, fill, report)
    self._queue.put(normalize_commission(fill, report, self.clock()))

def _fill_row(self, conn, obs: FillObservation) -> BrokerFillRow:
    return BrokerFillRow(
        account_id=obs.account_id, exec_id=obs.exec_id,
        order_entity_id=self._resolve_fill_order_in_tx(conn, obs),
        perm_id=obs.perm_id or None, client_order_id=obs.client_order_id or None,
        session_epoch=self.session_epoch, conid=obs.conid, side=obs.side,
        quantity=obs.quantity, price=obs.price, commission=None,
        commission_currency=None, realized_pnl=None, fill_time=obs.fill_time,
        revision=0, source_timestamp=obs.source_timestamp,
    )

def _resolve_fill_order_in_tx(self, conn, obs: FillObservation) -> Optional[str]:
    if obs.perm_id:
        entity = self.store.find_order_by_alias_in_tx(
            conn, "perm_id", str(obs.perm_id), obs.account_id, "")
        if entity:
            return entity
    if obs.client_order_id:
        return self.store.find_order_by_alias_in_tx(
            conn, "client_order_id", str(obs.client_order_id),
            obs.account_id, self.session_epoch)
    return None

def _apply_fill(self, conn, obs: FillObservation):
    from trader.domain.identity import fill_entity_id
    if self.store.get_fill_in_tx(conn, obs.account_id, obs.exec_id) is not None:
        return None  # execDetails is at-least-once; identity is account + execId
    row = self._fill_row(conn, obs)
    mutation = DomainMutation(
        event_type="fill.received", entity_type="fill",
        entity_id=fill_entity_id(obs.account_id, obs.exec_id),
        operation="upsert", account_id=obs.account_id, source="trader_service",
        source_timestamp=obs.source_timestamp, correlation_id=None,
        payload=row.to_payload(),
    )

    def write(conn_, revision):
        self.store.upsert_fill_in_tx(conn_, replace(row, revision=revision))

    return self.journal.mutate(conn, mutation, write)

def _apply_commission(self, conn, obs: CommissionObservation):
    from trader.domain.identity import fill_entity_id
    self._apply_fill(conn, obs.fill)  # creates fill.received if execDetails was missed
    current = self.store.get_fill_in_tx(conn, obs.fill.account_id, obs.fill.exec_id)
    revised = replace(current, commission=obs.commission,
                      commission_currency=obs.currency,
                      realized_pnl=obs.realized_pnl,
                      source_timestamp=obs.fill.source_timestamp)
    if revised.same_fields(current):
        return None  # duplicate commission report
    mutation = DomainMutation(
        event_type="fill.updated", entity_type="fill",
        entity_id=fill_entity_id(obs.fill.account_id, obs.fill.exec_id),
        operation="upsert", account_id=obs.fill.account_id,
        source="trader_service", source_timestamp=obs.fill.source_timestamp,
        correlation_id=None, payload=revised.to_payload(),
    )

    def write(conn_, revision):
        self.store.upsert_fill_in_tx(conn_, replace(revised, revision=revision))

    return self.journal.mutate(conn, mutation, write)
```

Replace the Task 3 stub so a newly observed order binds any earlier unbound fills — the same entity id and revision stream, one `fill.updated` each:

```python
def _resolve_unbound_fills_in_tx(self, conn, obs, entity_id: str) -> None:
    from trader.domain.identity import fill_entity_id
    for fill in self.store.unbound_fills_in_tx(conn, obs.account_id):
        matches = (
            (obs.perm_id and fill.perm_id == obs.perm_id)
            or (fill.client_order_id == obs.client_order_id
                and fill.session_epoch == self.session_epoch)
        )
        if not matches:
            continue
        bound = replace(fill, order_entity_id=entity_id,
                        source_timestamp=obs.source_timestamp)
        mutation = DomainMutation(
            event_type="fill.updated", entity_type="fill",
            entity_id=fill_entity_id(fill.account_id, fill.exec_id),
            operation="upsert", account_id=fill.account_id,
            source="trader_service", source_timestamp=obs.source_timestamp,
            correlation_id=None, payload=bound.to_payload(),
        )

        def write(conn_, revision, bound=bound):
            self.store.upsert_fill_in_tx(conn_, replace(bound, revision=revision))

        self.journal.mutate(conn, mutation, write)
```

- [ ] **Step 4: Run tests and commit**

Run: `uv run --frozen pytest tests/test_fill_producer.py tests/test_order_correlation.py tests/test_broker_ingest.py -q`

Expected: PASS.

```bash
git add trader/trading/broker_ingest.py tests/test_fill_producer.py
git commit -m "feat(m1-f2): dedupe and revise fills by execution id"
```

### Task 5: Broker snapshot completeness barrier

**Files:**
- Modify: `trader/trading/broker_ingest.py`
- Modify: `trader/domain/snapshot_service.py` (wire the generation reader)
- Modify: `trader/trading/trading_runtime.py` (`connect()` ~line 282, `setup_subscriptions()` ~line 406, `connected_event()` ~line 560, `disconnected_event()` ~line 640)
- Create: `tests/test_broker_generation.py`

**Interfaces:**
- Consumes: staging/generation store methods from Task 1, appliers from Tasks 2–4.
- Produces: `BROKER_SYNC_SOURCES = ("account", "positions", "open_orders", "completed_orders", "executions")`, exceptions `GenerationIncomplete` and `NoActiveGeneration`.
- Produces: `BrokerIngest.begin_generation(required=BROKER_SYNC_SOURCES) -> int`, `mark_source_complete(source)`, `promote_generation() -> int` (returns the promoted cursor), `abandon_generation(reason)`, `is_ready` property, and `async run_broker_sync(client, timeout_seconds=45.0) -> bool`.
- Produces: `BrokerStateStore.latest_promoted_generation_in_tx` wired into `DomainSnapshotService` so `SnapshotWithCursor.broker_generation` is real and `SnapshotNotReady` is raised until one generation promotes.

- [ ] **Step 1: Write failing generation tests**

Create `tests/test_broker_generation.py` (copy the `env` fixture, `_events`, `fake_position`, `fake_account_value`, and `fake_trade` helpers from the earlier test files verbatim):

```python
import pytest

from trader.trading.broker_ingest import BROKER_SYNC_SOURCES, GenerationIncomplete


def _complete_all(ingest, except_source=None):
    for source in BROKER_SYNC_SOURCES:
        if source != except_source:
            ingest.mark_source_complete(source)


def test_no_readiness_until_a_generation_promotes(env):
    assert env.ingest.is_ready is False
    env.ingest.begin_generation()
    env.ingest.on_position(fake_position(qty=10.0))
    env.ingest.drain_once()
    _complete_all(env.ingest)
    env.ingest.promote_generation()
    assert env.ingest.is_ready is True


@pytest.mark.parametrize("withheld", list(BROKER_SYNC_SOURCES))
def test_withholding_any_completion_marker_blocks_promotion(env, withheld):
    env.ingest.begin_generation()
    env.ingest.on_position(fake_position(qty=10.0))
    env.ingest.drain_once()
    _complete_all(env.ingest, except_source=withheld)
    with pytest.raises(GenerationIncomplete, match=withheld):
        env.ingest.promote_generation()
    assert _events(env.db) == []          # nothing journaled from staging
    assert env.ingest.is_ready is False   # no readiness from a partial set


def test_staged_records_do_not_touch_live_state(env):
    env.ingest.begin_generation()
    env.ingest.on_position(fake_position(qty=10.0))
    env.ingest.drain_once()
    assert _events(env.db) == []
    staged = env.db.execute("SELECT COUNT(*) FROM broker_sync_staging", fetch="one")
    assert staged == (1,)


def test_disconnect_mid_generation_abandons_and_keeps_prior_view(env):
    # Establish a live position from a previous session.
    env.ingest.on_position(fake_position(qty=10.0))
    env.ingest.drain_once()
    baseline = _events(env.db)

    env.ingest.begin_generation()
    env.ingest.on_position(fake_position(qty=99.0))  # staged, never promoted
    env.ingest.drain_once()
    env.ingest.abandon_generation("ib disconnected")

    assert _events(env.db) == baseline  # prior view intact, stale but visible
    assert env.db.execute("SELECT COUNT(*) FROM broker_sync_staging", fetch="one") == (0,)
    status = env.db.execute(
        "SELECT status, abandon_reason FROM broker_sync_generations "
        "ORDER BY generation_id DESC LIMIT 1", fetch="one")
    assert status == ("abandoned", "ib disconnected")
    assert env.ingest.is_ready is False


def test_entity_removed_between_generations_is_tombstoned_on_promotion(env):
    # Generation 1: two positions.
    env.ingest.begin_generation()
    env.ingest.on_position(fake_position(conid=265598, qty=10.0))
    env.ingest.on_position(fake_position(conid=272093, qty=5.0))
    env.ingest.drain_once()
    _complete_all(env.ingest)
    env.ingest.promote_generation()

    # Generation 2 (reconnect): the broker now enumerates only one.
    env.ingest.begin_generation()
    env.ingest.on_position(fake_position(conid=265598, qty=10.0))
    env.ingest.drain_once()
    _complete_all(env.ingest)
    env.ingest.promote_generation()

    tombstones = [e for e in _events(env.db) if e["operation"] == "delete"]
    assert [t["entity_id"] for t in tombstones] == ["DU123:272093"]


def test_interleaved_live_delta_wins_over_earlier_snapshot_row(env):
    env.ingest.begin_generation()
    env.ingest.on_position(fake_position(qty=10.0))   # snapshot enumeration
    env.ingest.on_position(fake_position(qty=25.0))   # live delta during sync
    env.ingest.drain_once()
    _complete_all(env.ingest)
    env.ingest.promote_generation()
    upserts = [e for e in _events(env.db) if e["entity_type"] == "position"]
    assert upserts[-1]["payload"]["quantity"] == 25.0  # ingest-seq order applied


def test_promotion_is_one_transaction_recording_the_cursor(env):
    env.ingest.begin_generation()
    env.ingest.on_account_value(fake_account_value())
    env.ingest.on_position(fake_position(qty=10.0))
    env.ingest.drain_once()
    _complete_all(env.ingest)
    cursor = env.ingest.promote_generation()
    max_cursor = env.db.execute(
        "SELECT MAX(source_cursor) FROM domain_event_journal", fetch="one")[0]
    assert cursor == max_cursor
    row = env.db.execute(
        "SELECT status, promoted_cursor FROM broker_sync_generations "
        "ORDER BY generation_id DESC LIMIT 1", fetch="one")
    assert row == ("promoted", cursor)
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_broker_generation.py -q`

Expected: FAIL because `begin_generation` / `promote_generation` / `abandon_generation` do not exist (`_stage` still raises `NotImplementedError`).

- [ ] **Step 3: Implement staging, promotion, and abandonment**

In `trader/trading/broker_ingest.py`:

```python
import json as _json

BROKER_SYNC_SOURCES = ("account", "positions", "open_orders",
                       "completed_orders", "executions")


class GenerationIncomplete(Exception):
    pass


class NoActiveGeneration(Exception):
    pass


@dataclass
class _Generation:
    generation_id: int
    required: tuple
    complete: set = dataclasses.field(default_factory=set)


_RECORD_KINDS = {}


def _register_kind(cls):
    _RECORD_KINDS[cls.__name__] = cls
    return cls


# decorate the observation dataclasses:
#   @_register_kind on AccountValueObservation, PositionObservation,
#   OrderObservation is registered explicitly (it lives in order_correlation):
#   _RECORD_KINDS['OrderObservation'] = OrderObservation
#   and FillObservation / CommissionObservation likewise.


def encode_observation(record) -> str:
    def _default(value):
        if isinstance(value, dt.datetime):
            return {"__dt__": value.isoformat()}
        return dataclasses.asdict(value)
    return _json.dumps(dataclasses.asdict(record), default=_default)


def _revive(payload: dict) -> dict:
    return {
        k: (dt.datetime.fromisoformat(v["__dt__"])
            if isinstance(v, dict) and "__dt__" in v else v)
        for k, v in payload.items()
    }


def decode_observation(kind: str, record_json: str):
    cls = _RECORD_KINDS[kind]
    payload = _revive(_json.loads(record_json))
    if cls is CommissionObservation:
        payload["fill"] = FillObservation(**_revive(payload["fill"]))
    return cls(**payload)


def _canonical_key(record) -> str:
    if isinstance(record, AccountValueObservation):
        return f"account:{record.account_id}:{record.tag}:{record.currency}"
    if isinstance(record, PositionObservation):
        return f"position:{record.account_id}:{record.conid}"
    if isinstance(record, OrderObservation):
        broker_key = record.perm_id or f"cid-{record.client_order_id}"
        return f"order:{record.account_id}:{broker_key}"
    if isinstance(record, FillObservation):
        return f"fill:{record.account_id}:{record.exec_id}"
    if isinstance(record, CommissionObservation):
        return f"commission:{record.fill.account_id}:{record.fill.exec_id}"
    raise TypeError(f"unknown ingest record: {type(record)!r}")


def _entity_type_of(record) -> str:
    return _canonical_key(record).split(":", 1)[0]
```

Methods on `BrokerIngest` (the `_stage` stub from Task 2 becomes real):

```python
def _stage(self, conn, ingest_seq: int, record) -> None:
    self.store.stage_in_tx(
        conn, self._generation.generation_id, ingest_seq,
        source="live", entity_type=_entity_type_of(record),
        canonical_key=_canonical_key(record),
        record_json=encode_observation(record),
        record_kind=type(record).__name__,
    )

def _require_generation(self) -> '_Generation':
    if self._generation is None:
        raise NoActiveGeneration("no broker-sync generation is staging")
    return self._generation

def begin_generation(self, required: tuple = BROKER_SYNC_SOURCES) -> int:
    with self._apply_lock:
        generation_id = self.db.transaction(
            lambda conn: self.store.open_generation_in_tx(conn, required, self.clock()))
        self._generation = _Generation(generation_id=generation_id, required=required)
        return generation_id

def mark_source_complete(self, source: str) -> None:
    gen = self._require_generation()
    if source not in gen.required:
        raise ValueError(f"unknown broker-sync source: {source}")
    gen.complete.add(source)
    self.db.transaction(
        lambda conn: self.store.mark_source_complete_in_tx(conn, gen.generation_id, source))

def promote_generation(self) -> int:
    with self._apply_lock:
        gen = self._require_generation()
        missing = [s for s in gen.required if s not in gen.complete]
        if missing:
            raise GenerationIncomplete(
                f"generation {gen.generation_id} missing sources: {', '.join(missing)}")

        def _tx(conn):
            staged = self.store.staged_rows_in_tx(conn, gen.generation_id)
            position_keys, order_entities = set(), set()
            for row in staged:  # strict ingest-seq order: interleaved deltas win
                record = decode_observation(row.record_kind, row.record_json)
                if isinstance(record, PositionObservation):
                    position_keys.add((record.account_id, record.conid))
                    self._apply_position(conn, record)
                elif isinstance(record, OrderObservation):
                    entity_id, _ = self._apply_order(conn, record)
                    order_entities.add(entity_id)
                elif isinstance(record, AccountValueObservation):
                    self._apply_account_value(conn, record)
                elif isinstance(record, FillObservation):
                    self._apply_fill(conn, record)      # merge + dedup, never tombstone
                elif isinstance(record, CommissionObservation):
                    self._apply_commission(conn, record)
            # Absence tombstones — ONLY the complete enumerable sets
            # (current positions and open orders, spec §5.4).
            now = self.clock()
            for row in self.store.select_active_positions_in_tx(conn):
                if (row.account_id, row.conid) not in position_keys:
                    self._apply_position(conn, PositionObservation(
                        account_id=row.account_id, conid=row.conid,
                        symbol=row.symbol, sec_type=row.sec_type,
                        exchange=row.exchange, currency=row.currency,
                        quantity=0.0, average_cost=None, market_price=None,
                        market_value=None, unrealized_pnl=None,
                        realized_pnl=None, daily_pnl=None,
                        source_timestamp=now))
            for row in self.store.select_working_orders_in_tx(conn):
                if row.order_entity_id not in order_entities:
                    self._tombstone_order(conn, row, now)
            cursor = conn.execute(
                "SELECT COALESCE(MAX(source_cursor), 0) FROM domain_event_journal"
            ).fetchone()[0]
            self.store.mark_generation_promoted_in_tx(conn, gen.generation_id, cursor, now)
            self.store.purge_staging_in_tx(conn, gen.generation_id)
            return int(cursor)

        cursor = self.db.transaction(_tx)
        self._generation = None
        logging.info("broker-sync generation %d promoted at cursor %d",
                     gen.generation_id, cursor)
        return cursor

def _tombstone_order(self, conn, row, now) -> None:
    mutation = DomainMutation(
        event_type="order.updated", entity_type="order",
        entity_id=row.order_entity_id, operation="delete",
        account_id=row.account_id, source="trader_service",
        source_timestamp=now, correlation_id=None, payload=None,
    )

    def write(conn_, revision):
        self.store.tombstone_order_in_tx(conn_, row.order_entity_id, revision, now)

    self.journal.mutate(conn, mutation, write)

def abandon_generation(self, reason: str) -> None:
    with self._apply_lock:
        gen = self._generation
        if gen is None:
            return  # idempotent — nothing staging
        def _tx(conn):
            self.store.mark_generation_abandoned_in_tx(conn, gen.generation_id, reason, self.clock())
            self.store.purge_staging_in_tx(conn, gen.generation_id)
        self.db.transaction(_tx)
        self._generation = None
        logging.warning("broker-sync generation %d abandoned: %s", gen.generation_id, reason)

@property
def is_ready(self) -> bool:
    if self._generation is not None:
        return False
    latest = self.db.transaction(self.store.latest_promoted_generation_in_tx)
    return latest is not None

async def run_broker_sync(self, client, timeout_seconds: float = 45.0) -> bool:
    """IB connect/reconnect completeness barrier (spec §5.4).

    Live callbacks are already registered before this runs, so snapshot
    callbacks and interleaved live deltas both stage with a local ingest
    sequence. Each awaited request resolves on its IB end marker —
    accountDownloadEnd, positionEnd, openOrderEnd, and the completed-order /
    execDetails end messages (ib_async awaits them internally).
    """
    import asyncio
    self.begin_generation()
    try:
        async with asyncio.timeout(timeout_seconds):
            await client.ib.reqAccountUpdatesAsync(self.account_id)
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
    except (asyncio.TimeoutError, ConnectionError, OSError) as ex:
        await asyncio.to_thread(self.abandon_generation, f"broker sync failed: {ex}")
        return False

def _promote_after_drain(self) -> None:
    self.drain_once()  # flush queued snapshot callbacks into staging first
    self.promote_generation()
```

Note the double-observation property: `reqPositionsAsync` both fires `positionEvent` (already wired) and returns the list; feeding both is safe because staged records replay through the same merge/changed-fields-only appliers, so the duplicate produces no second journal row.

In `trader/domain/snapshot_service.py`, replace the `[M1-F1]` generation placeholder with the real reader:

```python
# DomainSnapshotService gains:
def register_broker_generation_reader(self, reader) -> None:
    """reader(conn) -> int | None — BrokerStateStore.latest_promoted_generation_in_tx."""
    self._broker_generation_reader = reader

# inside the snapshot read transaction, before reading adapters:
generation = (self._broker_generation_reader(conn)
              if self._broker_generation_reader else None)
if generation is None:
    raise SnapshotNotReady("no complete broker-sync generation")
```

- [ ] **Step 4: Run generation tests**

Run: `uv run --frozen pytest tests/test_broker_generation.py tests/test_broker_ingest.py tests/test_fill_producer.py -q`

Expected: PASS.

- [ ] **Step 5: Wire the ingest pipeline into the trader runtime**

In `trader/trading/trading_runtime.py` `connect()` (next to the `EventStore` construction, ~line 282):

```python
# [M1-F2] broker producers: journal, store, and ingest pipeline.
from trader.data.broker_state import BrokerStateStore, broker_materialized_adapters
from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.broker_ingest import BrokerIngest

_db = DuckDBConnection.get_instance(self.duckdb_path)
_migrator = SchemaMigrator(_db)
self.domain_journal = DomainJournal(_db)
self.domain_journal.migrate(_migrator)
_broker_store = BrokerStateStore(_db)
_broker_store.migrate(_migrator)
self.broker_ingest = BrokerIngest(
    db=_db, journal=self.domain_journal, store=_broker_store,
    account_id=self.ib_account,
    account_mode='paper' if self.paper_trading else 'live',
)
self.broker_ingest.start()
```

In `setup_subscriptions()` (immediately after the order-tracker attachment, ~line 413), mirror the same disconnect-then-connect `keep_ref=True` pattern:

```python
if getattr(self, 'broker_ingest', None) is not None:
    for _ev, _handler in (
        (self.client.ib.accountValueEvent, self.broker_ingest.on_account_value),
        (self.client.ib.positionEvent, self.broker_ingest.on_position),
        (self.client.ib.updatePortfolioEvent, self.broker_ingest.on_portfolio_item),
        (self.client.ib.openOrderEvent, self.broker_ingest.on_open_order),
        (self.client.ib.orderStatusEvent, self.broker_ingest.on_order_status),
        (self.client.ib.execDetailsEvent, self.broker_ingest.on_exec_details),
        (self.client.ib.commissionReportEvent, self.broker_ingest.on_commission_report),
    ):
        try:
            _ev.disconnect(_handler)
        except Exception:
            pass
        _ev.connect(_handler, keep_ref=True)
    logging.info('broker ingest producers attached to IB events')
```

In `connected_event()` (after `self._republish_ticker_subscriptions()`):

```python
if getattr(self, 'broker_ingest', None) is not None:
    asyncio.get_running_loop().create_task(
        self.broker_ingest.run_broker_sync(self.client))
```

At the top of `disconnected_event()` (before the reconnect loop):

```python
if getattr(self, 'broker_ingest', None) is not None:
    try:
        await asyncio.to_thread(self.broker_ingest.abandon_generation, 'ib disconnected')
    except Exception as ex:
        logging.warning('abandoning broker-sync generation failed: %s', ex)
```

And wherever the trader constructs `DomainSnapshotService` (`[M1-F1]` wiring), register the reader and adapters:

```python
self.snapshot_service.register_broker_generation_reader(
    _broker_store.latest_promoted_generation_in_tx)
for adapter in broker_materialized_adapters(_broker_store):
    self.snapshot_service.register_adapter(adapter)
```

Run: `uv run --frozen pytest tests/test_trading_runtime.py tests/test_broker_generation.py -q`

Expected: PASS (existing trading-runtime tests unaffected; the wiring is guarded by `getattr`).

- [ ] **Step 6: Commit**

```bash
git add trader/trading/broker_ingest.py trader/domain/snapshot_service.py trader/trading/trading_runtime.py tests/test_broker_generation.py
git commit -m "feat(m1-f2): promote broker snapshots behind a completeness barrier"
```

### Task 6: Risk and reconciliation producers

**Files:**
- Create: `trader/trading/risk_producer.py`
- Modify: `trader/trading/trading_runtime.py` (`reconcile_with_broker()` ~line 1623)
- Create: `tests/test_risk_producer.py`

**Interfaces:**
- Consumes: `DomainJournal.mutate`, `SchemaMigrator.apply` (migration version 11: `risk_state`, `reconciliation_runs` tables), risk-namespace identity helpers from `trader/domain/identity.py` (`policy:<id>`, `projection:<account_id>`, `decision:<command_id>`, reconciliation-run IDs).
- Produces: `RiskProducer(db, journal, account_id, compute_projection, debounce_seconds=0.1, timer_factory=threading.Timer)` with `migrate(migrator)`, `publish_policy(policy_id, payload) -> DomainEvent | None`, `publish_decision(command_id, payload, correlation_id=None) -> DomainEvent | None` (write-once; conflicting rewrite raises `RiskDecisionImmutable`), `mark_projection_dirty()`, `flush_projection() -> DomainEvent | None`.
- Produces: `ReconciliationProducer(db, journal)` with `publish_run(run_id, trigger, source_cursor, discrepancies, resolutions, started_at, completed_at) -> DomainEvent`.
- Note for `[M1-F3]`: the command coordinator calls `publish_decision(command_id, ...)` for every preflight/approval risk evaluation; `BrokerIngest` and the proposal repository call `mark_projection_dirty()` after account, position, proposal, or order changes.

- [ ] **Step 1: Write failing risk-producer tests**

Create `tests/test_risk_producer.py`:

```python
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.risk_producer import (
    ReconciliationProducer, RiskDecisionImmutable, RiskProducer,
)

UTC_NOW = dt.datetime(2026, 7, 15, 13, 0, tzinfo=dt.timezone.utc)


class FakeTimer:
    """Captures the debounce schedule; fired manually by the test."""
    instances: list = []

    def __init__(self, interval, fn):
        self.interval, self.fn, self.cancelled = interval, fn, False
        FakeTimer.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def env(tmp_path):
    FakeTimer.instances = []
    db = DuckDBConnection.get_instance(str(tmp_path / "risk.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    projection = {"gross_exposure_pct": 42.0}
    producer = RiskProducer(
        db=db, journal=journal, account_id="DU123",
        compute_projection=lambda: dict(projection),
        timer_factory=FakeTimer, clock=lambda: UTC_NOW,
    )
    producer.migrate(migrator)
    return SimpleNamespace(db=db, journal=journal, producer=producer,
                           projection=projection)


def _events(db):
    rows = db.execute(
        "SELECT event_type, entity_id, entity_revision, payload "
        "FROM domain_event_journal ORDER BY source_cursor", fetch="all")
    return [{"event_type": r[0], "entity_id": r[1], "entity_revision": r[2],
             "payload": json.loads(r[3]) if r[3] else None} for r in rows]


def test_projection_is_debounced_to_at_most_100ms(env):
    env.producer.mark_projection_dirty()
    env.producer.mark_projection_dirty()  # coalesced into the pending timer
    assert len(FakeTimer.instances) == 1
    assert FakeTimer.instances[0].interval <= 0.1
    assert _events(env.db) == []           # nothing journaled before the timer fires
    FakeTimer.instances[0].fn()
    events = _events(env.db)
    assert [e["entity_id"] for e in events] == ["projection:DU123"]
    assert events[0]["event_type"] == "risk.updated"


def test_unchanged_projection_is_not_rejournaled(env):
    env.producer.mark_projection_dirty()
    FakeTimer.instances[-1].fn()
    env.producer.mark_projection_dirty()   # same computed projection
    FakeTimer.instances[-1].fn()
    assert len(_events(env.db)) == 1


def test_namespaces_have_independent_revision_streams(env):
    env.producer.publish_policy("default", {"max_position_pct": 10.0})
    env.producer.publish_decision("cmd-1", {"result": "pass"})
    env.producer.mark_projection_dirty()
    FakeTimer.instances[-1].fn()
    env.producer.publish_policy("default", {"max_position_pct": 12.0})
    events = _events(env.db)
    by_entity = {}
    for e in events:
        by_entity.setdefault(e["entity_id"], []).append(e["entity_revision"])
    assert by_entity == {
        "policy:default": [1, 2],       # policy edits advance only the policy
        "decision:cmd-1": [1],          # decisions are written once
        "projection:DU123": [1],        # projection untouched by the policy edit
    }


def test_decision_is_write_once(env):
    env.producer.publish_decision("cmd-1", {"result": "pass"})
    assert env.producer.publish_decision("cmd-1", {"result": "pass"}) is None  # idempotent
    with pytest.raises(RiskDecisionImmutable):
        env.producer.publish_decision("cmd-1", {"result": "fail"})


def test_reconciliation_run_is_journaled(env):
    recon = ReconciliationProducer(db=env.db, journal=env.journal)
    recon.migrate(SchemaMigrator(env.db))
    recon.publish_run(
        run_id="run-1", trigger="startup", source_cursor=17,
        discrepancies=[{"kind": "orphan_order", "order_id": 9}],
        resolutions=[], started_at=UTC_NOW, completed_at=UTC_NOW,
    )
    events = _events(env.db)
    assert events[-1]["event_type"] == "reconciliation.updated"
    assert events[-1]["payload"]["discrepancies"][0]["kind"] == "orphan_order"
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_risk_producer.py -q`

Expected: FAIL because `trader/trading/risk_producer.py` does not exist.

- [ ] **Step 3: Implement the producers**

Create `trader/trading/risk_producer.py`:

```python
"""Risk and reconciliation producers. [M1-F2]

Risk state is three separate entity namespaces (spec §6) with independent
monotonic revisions: ``policy:<policy_id>``, ``projection:<account_id>``, and
``decision:<command_id>``. A policy edit never advances an account projection
or rewrites a historical command decision. The account projection is
recomputed at most once per 100 ms debounce window and journaled only when
its computed payload changed.
"""
import datetime as dt
import json
import threading
from typing import Any, Callable, Optional

from trader.common.logging_helper import setup_logging
from trader.domain.events import DomainMutation

logging = setup_logging(module_name='risk_producer')

RISK_STATE_MIGRATION_VERSION = 11

_RISK_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS risk_state (
        risk_id VARCHAR PRIMARY KEY,
        kind VARCHAR NOT NULL CHECK (kind IN ('policy', 'projection', 'decision')),
        account_id VARCHAR,
        payload JSON NOT NULL,
        revision BIGINT NOT NULL,
        source_timestamp TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS reconciliation_runs (
        run_id VARCHAR PRIMARY KEY,
        trigger VARCHAR NOT NULL CHECK (trigger IN
            ('startup', 'scheduled', 'command', 'operator')),
        source_cursor BIGINT,
        discrepancies JSON NOT NULL,
        resolutions JSON NOT NULL,
        started_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ NOT NULL,
        revision BIGINT NOT NULL
    )""",
]


class RiskDecisionImmutable(Exception):
    """A per-command risk decision is historical fact; it is never rewritten."""


class RiskProducer:
    def __init__(self, db, journal, account_id: str,
                 compute_projection: Callable[[], dict],
                 debounce_seconds: float = 0.1,
                 timer_factory=threading.Timer,
                 clock: Optional[Callable[[], dt.datetime]] = None):
        assert debounce_seconds <= 0.1, "spec §5.4: projection debounce is at most 100 ms"
        self.db = db
        self.journal = journal
        self.account_id = account_id
        self.compute_projection = compute_projection
        self.debounce_seconds = debounce_seconds
        self.timer_factory = timer_factory
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._timer_lock = threading.Lock()
        self._pending_timer = None

    def migrate(self, migrator) -> None:
        migrator.apply(RISK_STATE_MIGRATION_VERSION, "risk_state_tables", _RISK_STATEMENTS)

    # ---- shared write path ------------------------------------------------
    def _get_in_tx(self, conn, risk_id: str):
        return conn.execute(
            "SELECT payload, revision FROM risk_state WHERE risk_id = ?",
            [risk_id]).fetchone()

    def _write_in_tx(self, conn, risk_id: str, kind: str, payload: dict,
                     correlation_id: Optional[str]):
        now = self.clock()
        mutation = DomainMutation(
            event_type="risk.updated", entity_type="risk", entity_id=risk_id,
            operation="upsert", account_id=self.account_id,
            source="trader_service", source_timestamp=now,
            correlation_id=correlation_id,
            payload={"kind": kind, "risk_id": risk_id, **payload},
        )

        def write(conn_, revision):
            conn_.execute("DELETE FROM risk_state WHERE risk_id = ?", [risk_id])
            conn_.execute(
                "INSERT INTO risk_state (risk_id, kind, account_id, payload, "
                "revision, source_timestamp, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [risk_id, kind, self.account_id, json.dumps(payload),
                 revision, now, now])

        return self.journal.mutate(conn, mutation, write)

    # ---- policy -------------------------------------------------------------
    def publish_policy(self, policy_id: str, payload: dict):
        risk_id = f"policy:{policy_id}"

        def _tx(conn):
            current = self._get_in_tx(conn, risk_id)
            if current is not None and json.loads(current[0]) == payload:
                return None
            return self._write_in_tx(conn, risk_id, "policy", payload, None)

        return self.db.transaction(_tx)

    # ---- decision (write-once) ----------------------------------------------
    def publish_decision(self, command_id: str, payload: dict,
                         correlation_id: Optional[str] = None):
        risk_id = f"decision:{command_id}"

        def _tx(conn):
            current = self._get_in_tx(conn, risk_id)
            if current is not None:
                if json.loads(current[0]) == payload:
                    return None  # idempotent retry
                raise RiskDecisionImmutable(
                    f"risk decision for command {command_id} already recorded")
            return self._write_in_tx(conn, risk_id, "decision", payload,
                                     correlation_id or command_id)

        return self.db.transaction(_tx)

    # ---- projection (debounced) -----------------------------------------------
    def mark_projection_dirty(self) -> None:
        with self._timer_lock:
            if self._pending_timer is not None:
                return  # a recompute is already scheduled within the window
            timer = self.timer_factory(self.debounce_seconds, self.flush_projection)
            self._pending_timer = timer
            timer.start()

    def flush_projection(self):
        with self._timer_lock:
            self._pending_timer = None
        try:
            payload = self.compute_projection()
        except Exception as ex:
            logging.warning("risk projection compute failed: %s", ex)
            return None
        risk_id = f"projection:{self.account_id}"

        def _tx(conn):
            current = self._get_in_tx(conn, risk_id)
            if current is not None and json.loads(current[0]) == payload:
                return None  # journal only a changed projection
            return self._write_in_tx(conn, risk_id, "projection", payload, None)

        return self.db.transaction(_tx)


class ReconciliationProducer:
    def __init__(self, db, journal,
                 clock: Optional[Callable[[], dt.datetime]] = None):
        self.db = db
        self.journal = journal
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    def migrate(self, migrator) -> None:
        migrator.apply(RISK_STATE_MIGRATION_VERSION, "risk_state_tables", _RISK_STATEMENTS)

    def publish_run(self, run_id: str, trigger: str, source_cursor: Optional[int],
                    discrepancies: list, resolutions: list,
                    started_at: dt.datetime, completed_at: dt.datetime):
        payload = {
            "run_id": run_id, "trigger": trigger, "source_cursor": source_cursor,
            "discrepancies": discrepancies, "resolutions": resolutions,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
        }
        mutation = DomainMutation(
            event_type="reconciliation.updated", entity_type="reconciliation",
            entity_id=run_id, operation="upsert", account_id=None,
            source="trader_service", source_timestamp=completed_at,
            correlation_id=None, payload=payload,
        )

        def _tx(conn):
            def write(conn_, revision):
                conn_.execute("DELETE FROM reconciliation_runs WHERE run_id = ?", [run_id])
                conn_.execute(
                    "INSERT INTO reconciliation_runs (run_id, trigger, source_cursor, "
                    "discrepancies, resolutions, started_at, completed_at, revision) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [run_id, trigger, source_cursor, json.dumps(discrepancies),
                     json.dumps(resolutions), started_at, completed_at, revision])
            return self.journal.mutate(conn, mutation, write)

        return self.db.transaction(_tx)
```

Wire the reconciliation producer at the end of `Trader.reconcile_with_broker()` (~line 1623), guarded so a journal failure never breaks the existing report-only path:

```python
# [M1-F2] journal the completed reconciliation run.
if getattr(self, 'domain_journal', None) is not None:
    try:
        from trader.trading.risk_producer import ReconciliationProducer
        producer = ReconciliationProducer(
            db=DuckDBConnection.get_instance(self.duckdb_path),
            journal=self.domain_journal)
        producer.publish_run(
            run_id=f"recon-{int(started_at.timestamp())}",
            trigger="startup" if startup else "scheduled",
            source_cursor=None,
            discrepancies=report.get("discrepancies", []),
            resolutions=report.get("resolutions", []),
            started_at=started_at,
            completed_at=dt.datetime.now(dt.timezone.utc),
        )
    except Exception as ex:
        logging.warning("journaling reconciliation run failed: %s", ex)
```

(Adapt the `report` / `started_at` variable names to the actual locals in `reconcile_with_broker`; the method already builds a divergence dict — journal exactly what it reports.) Also add `risk` and `reconciliation` materialized adapters next to `broker_materialized_adapters` in `trader/data/broker_state.py` style, selecting `risk_state` and `reconciliation_runs` rows as payload dicts, and register them with the snapshot service in the Task 5 wiring block.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --frozen pytest tests/test_risk_producer.py -q`

Expected: PASS.

```bash
git add trader/trading/risk_producer.py trader/trading/trading_runtime.py trader/data/broker_state.py tests/test_risk_producer.py
git commit -m "feat(m1-f2): journal risk and reconciliation state"
```

### Task 7: Reference-counted quote coverage and the [M1-F2] integration gate

**Files:**
- Create: `trader/trading/quote_coverage.py`
- Modify: `trader/trading/trading_runtime.py` (`__init__` ~line 155, `publish_contract()` ~line 831, add `unpublish_contract()`)
- Modify: `trader/trading/broker_ingest.py` (sync position/order refs after each applied batch)
- Create: `tests/test_quote_coverage.py`

**Interfaces:**
- Consumes: `Trader.publish_contract(contract, delayed)` (`trader/trading/trading_runtime.py:831`) and `IBAIORx.unsubscribe_contract(contract)` (`trader/listeners/ibreactive.py:555`).
- Produces: `QUOTE_RELEASE_LINGER_SECONDS = 600.0` and `QuoteSubscriptionManager(publish, unpublish, linger_seconds=QUOTE_RELEASE_LINGER_SECONDS, clock=time.monotonic)` with `acquire(owner: str, contract, delayed=False)`, `release(owner: str, conid: int)`, `set_owner_refs(owner_kind: str, contracts: Mapping[int, Contract], delayed=False)`, `sweep() -> list[int]`, `active_conids() -> set[int]`, `start(interval_seconds=60.0)` / `stop()`.
- Produces: `Trader.unpublish_contract(contract) -> None` — the missing release half of `publish_contract`.
- Frozen owner-kind strings for `set_owner_refs`: `"position"`, `"order"`, `"proposal"`, `"strategy"`. This plan wires `"position"` and `"order"` from `BrokerIngest`; `[M1-F3]` wires `"proposal"` (pending proposals) and `"strategy"` (running strategies) through the same method.
- Produces (pre-flight resolution RA-8 — quote-plane ownership moved here from `[M1-F1]`): `get_quotes_snapshot() -> {"quotes": {instrument_id: quote_row}}`, registered on the typed `query` socket (port `42101`), returning the trader's current conflated quote map for the covered instruments. `[M1-F1]` does NOT register it (quotes are absent from the journal); `[M1-R]` fakes it until this plan is fenced. Also activates the snapshot readiness gate: once a complete broker generation exists, `snapshot_with_cursor` stamps the real `broker_generation` and its `SNAPSHOT_NOT_READY` path becomes live (F1 left it dormant at `broker_generation=0`).

- [ ] **Step 1: Write failing quote-coverage tests**

Create `tests/test_quote_coverage.py`:

```python
from types import SimpleNamespace

import pytest

from trader.trading.quote_coverage import (
    QUOTE_RELEASE_LINGER_SECONDS, QuoteSubscriptionManager,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def rig():
    published, unpublished = [], []
    clock = FakeClock()
    manager = QuoteSubscriptionManager(
        publish=lambda contract, delayed: published.append((contract.conId, delayed)),
        unpublish=lambda contract: unpublished.append(contract.conId),
        clock=clock,
    )
    return SimpleNamespace(manager=manager, published=published,
                           unpublished=unpublished, clock=clock)


def _contract(conid=265598):
    return SimpleNamespace(conId=conid)


def test_two_owners_publish_once(rig):
    rig.manager.acquire("position:DU123:265598", _contract())
    rig.manager.acquire("strategy:smi", _contract())
    assert rig.published == [(265598, False)]


def test_position_without_strategy_keeps_the_quote(rig):
    rig.manager.acquire("position:DU123:265598", _contract())
    rig.manager.release("strategy:smi", 265598)  # releasing a non-holder is a no-op
    rig.clock.advance(QUOTE_RELEASE_LINGER_SECONDS + 1)
    assert rig.manager.sweep() == []
    assert rig.unpublished == []


def test_release_lingers_ten_minutes_before_unpublish(rig):
    rig.manager.acquire("position:DU123:265598", _contract())
    rig.manager.release("position:DU123:265598", 265598)
    rig.clock.advance(QUOTE_RELEASE_LINGER_SECONDS - 1)
    assert rig.manager.sweep() == []                # not yet: 10-minute linger
    rig.clock.advance(2)
    assert rig.manager.sweep() == [265598]
    assert rig.unpublished == [265598]


def test_reacquire_during_linger_cancels_the_release(rig):
    rig.manager.acquire("position:DU123:265598", _contract())
    rig.manager.release("position:DU123:265598", 265598)
    rig.clock.advance(300)
    rig.manager.acquire("order:grp1:entry", _contract())  # back in the union
    rig.clock.advance(QUOTE_RELEASE_LINGER_SECONDS)
    assert rig.manager.sweep() == []
    assert rig.published == [(265598, False)]  # still the original subscription
    assert rig.unpublished == []


def test_set_owner_refs_syncs_the_union_declaratively(rig):
    rig.manager.set_owner_refs("position", {1: _contract(1), 2: _contract(2)})
    assert [c for c, _ in rig.published] == [1, 2]
    rig.manager.set_owner_refs("position", {2: _contract(2)})  # position 1 closed
    rig.clock.advance(QUOTE_RELEASE_LINGER_SECONDS + 1)
    assert rig.manager.sweep() == [1]
    assert rig.manager.active_conids() == {2}


def test_owner_kinds_are_independent(rig):
    rig.manager.set_owner_refs("position", {1: _contract(1)})
    rig.manager.set_owner_refs("order", {1: _contract(1)})
    rig.manager.set_owner_refs("position", {})  # position gone, order still working
    rig.clock.advance(QUOTE_RELEASE_LINGER_SECONDS + 1)
    assert rig.manager.sweep() == []
    assert rig.manager.active_conids() == {1}
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_quote_coverage.py -q`

Expected: FAIL because `trader/trading/quote_coverage.py` does not exist.

- [ ] **Step 3: Implement the manager and the trader release path**

Create `trader/trading/quote_coverage.py`:

```python
"""Reference-counted quote subscription coverage. [M1-F2]

Spec §7 state bounds: the trader owns the union of quote references from open
positions, working orders, pending proposals, and running strategies — a
position is covered even when no strategy is armed. A subscription is
released ten minutes after the final reference disappears, wrapping the
previously one-way ``publish_contract``. Quotes stay on the ticker PubSub and
are never journaled.
"""
import threading
import time
from typing import Callable, Dict, Mapping, Optional, Set

from trader.common.logging_helper import setup_logging

logging = setup_logging(module_name='quote_coverage')

QUOTE_RELEASE_LINGER_SECONDS = 600.0  # ten minutes


class QuoteSubscriptionManager:
    def __init__(self,
                 publish: Callable,        # (contract, delayed) -> Any
                 unpublish: Callable,      # (contract) -> None
                 linger_seconds: float = QUOTE_RELEASE_LINGER_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self._publish = publish
        self._unpublish = unpublish
        self._linger_seconds = linger_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._refs: Dict[int, Set[str]] = {}         # conid -> owner keys
        self._contracts: Dict[int, object] = {}       # conid -> Contract
        self._linger_until: Dict[int, float] = {}     # conid -> release deadline
        self._sweeper: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def acquire(self, owner: str, contract, delayed: bool = False) -> None:
        conid = int(contract.conId)
        with self._lock:
            holders = self._refs.setdefault(conid, set())
            first = not holders and conid not in self._linger_until \
                and conid not in self._contracts
            holders.add(owner)
            self._linger_until.pop(conid, None)  # re-acquire cancels a pending release
            already_subscribed = conid in self._contracts
            self._contracts[conid] = contract
        if first or not already_subscribed:
            self._publish(contract, delayed)

    def release(self, owner: str, conid: int) -> None:
        with self._lock:
            holders = self._refs.get(conid)
            if not holders or owner not in holders:
                return  # releasing a non-holder is a no-op
            holders.discard(owner)
            if not holders:
                self._refs.pop(conid, None)
                self._linger_until[conid] = self._clock() + self._linger_seconds

    def set_owner_refs(self, owner_kind: str,
                       contracts: Mapping[int, object],
                       delayed: bool = False) -> None:
        """Declaratively sync one owner kind's reference set to `contracts`."""
        with self._lock:
            currently_held = {
                conid for conid, holders in self._refs.items()
                if any(h == owner_kind or h.startswith(f"{owner_kind}:") for h in holders)
            }
        for conid in currently_held - set(contracts):
            self.release(owner_kind, conid)
        for conid, contract in contracts.items():
            self.acquire(owner_kind, contract, delayed)

    def sweep(self) -> list:
        now = self._clock()
        released = []
        with self._lock:
            for conid, deadline in list(self._linger_until.items()):
                if deadline <= now and not self._refs.get(conid):
                    self._linger_until.pop(conid)
                    contract = self._contracts.pop(conid, None)
                    if contract is not None:
                        released.append((conid, contract))
        for conid, contract in released:
            try:
                self._unpublish(contract)
            except Exception as ex:
                logging.warning("unpublish for conId %s failed: %s", conid, ex)
        return [conid for conid, _ in released]

    def active_conids(self) -> set:
        with self._lock:
            return set(self._refs)

    def start(self, interval_seconds: float = 60.0) -> None:
        def _loop():
            while not self._stop.wait(interval_seconds):
                try:
                    self.sweep()
                except Exception as ex:
                    logging.warning("quote sweep failed: %s", ex)
        self._sweeper = threading.Thread(target=_loop, name="quote-coverage",
                                         daemon=True)
        self._sweeper.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sweeper is not None:
            self._sweeper.join(timeout=5)
```

In `trader/trading/trading_runtime.py`, add the release half next to `publish_contract` (~line 831):

```python
def unpublish_contract(self, contract: Contract) -> None:
    """Release half of publish_contract — stops the IB market-data line and
    forgets the reconnect-replay entry. [M1-F2]"""
    self.zmq_pubsub_published_contracts.pop(contract.conId, None)
    self.zmq_pubsub_contracts.pop(contract.conId, None)
    if self.zmq_pubsub_contract_filters.pop(contract.conId, None) is not None:
        try:
            self.client.unsubscribe_contract(contract)
        except Exception as ex:
            logging.warning('unsubscribe_contract failed for conId %s: %s',
                            contract.conId, ex)
```

and construct the manager in `__init__` (near the `zmq_pubsub_published_contracts` block, ~line 148):

```python
from trader.trading.quote_coverage import QuoteSubscriptionManager
self.quote_subscriptions = QuoteSubscriptionManager(
    publish=lambda contract, delayed: self.publish_contract(contract, delayed=delayed),
    unpublish=self.unpublish_contract,
)
```

Start its sweeper in `connect()` (`self.quote_subscriptions.start()`), and pass it to `BrokerIngest` in the Task 5 wiring block (`self.broker_ingest.quote_refs = self.quote_subscriptions`). In `trader/trading/broker_ingest.py`, sync the position/order union after every applied (non-staging) batch — at the end of `_apply_batch`, outside the transaction:

```python
# __init__ gains: self.quote_refs = None  # Optional[QuoteSubscriptionManager]

def _sync_quote_refs(self) -> None:
    if self.quote_refs is None:
        return
    def _read(conn):
        return (self.store.select_active_positions_in_tx(conn),
                self.store.select_working_orders_in_tx(conn))
    positions, orders = self.db.transaction(_read)
    from ib_async import Contract
    self.quote_refs.set_owner_refs("position", {
        p.conid: Contract(conId=p.conid, exchange=p.exchange or "SMART",
                          symbol=p.symbol, secType=p.sec_type, currency=p.currency)
        for p in positions})
    self.quote_refs.set_owner_refs("order", {
        o.conid: Contract(conId=o.conid, exchange="SMART", symbol=o.symbol)
        for o in orders})
```

Call `self._sync_quote_refs()` at the end of `_apply_batch` (after the transaction commits and only when `self._generation is None`) and at the end of `promote_generation` (after the promotion transaction commits).

- [ ] **Step 4: Run quote tests and commit the feature**

Run: `uv run --frozen pytest tests/test_quote_coverage.py tests/test_trading_runtime.py -q`

Expected: PASS.

```bash
git add trader/trading/quote_coverage.py trader/trading/trading_runtime.py trader/trading/broker_ingest.py tests/test_quote_coverage.py
git commit -m "feat(m1-f2): ref-count quote subscriptions with release linger"
```

- [ ] **Step 5: Run the [M1-F2] integration gate**

Run: `uv run --frozen pytest tests/test_broker_state.py tests/test_broker_ingest.py tests/test_order_correlation.py tests/test_fill_producer.py tests/test_broker_generation.py tests/test_risk_producer.py tests/test_quote_coverage.py -q`

Expected: PASS — every §5.4 broker producer has a contract test (materialized row + exactly-once journal identity + entity revision + tombstone-or-payload), the §13.2 order-correlation matrix passes (fill-before-order, late `permId`, client-ID reuse after restart, bracket legs, external orders, restart), and no incomplete generation advances readiness or emits absence tombstones.

Then run the canonical full suite to prove no regression:

Run: `uv run --frozen pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py`

Expected: PASS with no new failures or warnings caused by `[M1-F2]`.

- [ ] **Step 6: Commit the gate**

```bash
git add -A tests/
git commit -m "test(m1-f2): gate broker producers correlation and quote coverage"
```
