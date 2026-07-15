# M1-F1 Transactional Journal, Snapshot, and Feed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the trader-owned transaction, durable domain journal, fenced snapshot, and typed long-poll feed that every realtime producer and consumer uses.

**Architecture:** Add an explicit DuckDB unit of work, immutable domain-event contracts, and per-entity materialized adapters. A mutation writes its complete materialized row and journal entry in one transaction; a dedicated reader connection serves fenced snapshots and cursor long-polls without blocking the IB or command loops.

**Tech Stack:** CPython 3.12.13, DuckDB, dataclasses, JSON, threading conditions, typed JSON RPC, pytest.

## Global Constraints

- Durable upserts carry the complete normalized entity row; reducers never merge partial JSON patches.
- Deletes are explicit tombstones with a higher entity revision.
- Cursor order is global and monotonic; entity revisions are monotonic only within one entity key.
- A snapshot and its maximum included cursor come from one read transaction.
- Long-poll waits off the IB loop, returns at most `limit` rows, and emits an empty heartbeat after 10 seconds.
- Journal retention is 30 days plus the newest complete checkpoint; active cursors prevent required compaction.
- The existing `EventStore` remains for legacy trading-history compatibility and is not renamed to the domain journal.

---

### Task 1: Explicit DuckDB transaction API

**Files:**
- Modify: `trader/data/duckdb_store.py:20-120`
- Modify: `tests/test_duckdb_store.py`

**Interfaces:**
- Produces: `DuckDBConnection.transaction(fn: Callable[[DuckDBPyConnection], T]) -> T`.
- Guarantees: `BEGIN TRANSACTION`, one `COMMIT`, and `ROLLBACK` on every exception.

- [ ] **Step 1: Write commit and crash rollback tests**

```python
def test_transaction_rolls_back_every_statement(tmp_duckdb_path):
    db = DuckDBConnection.get_instance(tmp_duckdb_path)
    db.execute("CREATE TABLE tx_test(id INTEGER PRIMARY KEY)")

    def write_then_fail(conn):
        conn.execute("INSERT INTO tx_test VALUES (1)")
        raise RuntimeError("crash point")

    with pytest.raises(RuntimeError, match="crash point"):
        db.transaction(write_then_fail)
    assert db.execute("SELECT * FROM tx_test", fetch="all") == []
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_duckdb_store.py -q`

Expected: FAIL because `transaction()` does not exist.

- [ ] **Step 3: Implement the transaction wrapper**

```python
def transaction(self, fn):
    def _transaction(conn):
        conn.execute("BEGIN TRANSACTION")
        try:
            result = fn(conn)
            conn.execute("COMMIT")
            return result
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    return self.execute_atomic(_transaction)
```

Document that repository methods ending in `_in_tx` accept the supplied connection and must not open another connection.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --frozen pytest tests/test_duckdb_store.py -q`

Expected: PASS.

```bash
git add trader/data/duckdb_store.py tests/test_duckdb_store.py
git commit -m "feat(m1-f): add explicit DuckDB unit of work"
```

### Task 2: Domain contracts and canonical identities

**Files:**
- Create: `trader/domain/__init__.py`
- Create: `trader/domain/events.py`
- Create: `trader/domain/identity.py`
- Create: `tests/test_domain_events.py`
- Create: `tests/test_domain_identity.py`

**Interfaces:**
- Produces: `EntityKey`, `DomainMutation`, `DomainEvent`, `SnapshotWithCursor`, and `ReadDomainEventsResult`.
- Produces identity helpers for instruments, positions, proposals, strategies, risk namespaces, commands, and reconciliation runs.

- [ ] **Step 1: Write serialization and identity tests**

```python
def test_position_key_includes_account_and_conid():
    assert position_entity_id("DU123", 265598) == "DU123:265598"


def test_domain_upsert_requires_complete_payload():
    with pytest.raises(ValueError, match="payload"):
        DomainMutation(
            event_type="position.updated",
            entity_type="position",
            entity_id="DU123:265598",
            operation="upsert",
            account_id="DU123",
            source="trader_service",
            source_timestamp=UTC_NOW,
            correlation_id=None,
            payload=None,
        )
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_domain_events.py tests/test_domain_identity.py -q`

Expected: FAIL because the domain package does not exist.

- [ ] **Step 3: Implement immutable contracts**

```python
@dataclass(frozen=True)
class EntityKey:
    entity_type: str
    entity_id: str
    account_id: str | None


@dataclass(frozen=True)
class DomainMutation:
    event_type: str
    entity_type: str
    entity_id: str
    operation: Literal["upsert", "delete"]
    account_id: str | None
    source: str
    source_timestamp: datetime
    correlation_id: str | None
    payload: dict[str, JSONValue] | None

    def __post_init__(self):
        if self.source_timestamp.tzinfo is None:
            raise ValueError("source_timestamp must be timezone-aware UTC")
        if self.operation == "upsert" and self.payload is None:
            raise ValueError("upsert requires a complete payload")
        if self.operation == "delete" and self.payload is not None:
            raise ValueError("delete tombstone must not carry a payload")
```

Use canonical IDs from the spec: `account:conId` for positions, proposal integer string for proposals, immutable local order ID for orders, `account:execId` for fills, and `policy:`, `projection:`, and `decision:` prefixes for risk.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --frozen pytest tests/test_domain_events.py tests/test_domain_identity.py -q`

Expected: PASS.

```bash
git add trader/domain tests/test_domain_events.py tests/test_domain_identity.py
git commit -m "feat(m1-f): define domain events and canonical keys"
```

### Task 3: Schema migrations and atomic journal append

**Files:**
- Create: `trader/data/schema_migrations.py`
- Create: `trader/data/domain_journal.py`
- Create: `tests/test_schema_migrations.py`
- Create: `tests/test_domain_journal.py`

**Interfaces:**
- Produces: `SchemaMigrator.apply(version: int, name: str, statements: Sequence[str])`.
- Produces: `DomainJournal.mutate(conn, mutation, write_materialized) -> DomainEvent`.
- The `write_materialized` callback signature is `write_materialized(conn, entity_revision: int) -> None`; it runs inside the same transaction as the journal insert.
- Migration version allocation: this plan owns versions 1-9; `[M1-F2]` owns 10-19; `[M1-F3]` owns 20-29.
- Produces tables `schema_migrations`, `domain_event_journal`, and `domain_snapshot_checkpoints`.

- [ ] **Step 1: Write migration and crash-point tests**

```python
def test_materialized_write_and_event_are_atomic(domain_store):
    with pytest.raises(RuntimeError, match="after materialized"):
        domain_store.apply(
            POSITION_MUTATION,
            crash_after="materialized",
        )
    assert domain_store.get_entity("position", "DU123:265598") is None
    assert domain_store.journal.read_after(0, 100) == []


def test_same_event_id_is_idempotent(domain_store):
    first = domain_store.apply(POSITION_MUTATION, event_id="event-1")
    second = domain_store.apply(POSITION_MUTATION, event_id="event-1")
    assert first.source_cursor == second.source_cursor
    assert len(domain_store.journal.read_after(0, 100)) == 1
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_schema_migrations.py tests/test_domain_journal.py -q`

Expected: FAIL because the migration and journal repositories do not exist.

- [ ] **Step 3: Create the durable journal schema**

Use named columns and uniqueness constraints:

```sql
CREATE SEQUENCE IF NOT EXISTS domain_event_cursor_seq START 1;
CREATE TABLE IF NOT EXISTS domain_event_journal (
    source_cursor BIGINT PRIMARY KEY DEFAULT nextval('domain_event_cursor_seq'),
    event_id VARCHAR NOT NULL UNIQUE,
    entity_revision BIGINT NOT NULL,
    event_type VARCHAR NOT NULL,
    entity_type VARCHAR NOT NULL,
    entity_id VARCHAR NOT NULL,
    operation VARCHAR NOT NULL CHECK (operation IN ('upsert', 'delete')),
    account_id VARCHAR,
    source VARCHAR NOT NULL,
    source_timestamp TIMESTAMPTZ NOT NULL,
    received_timestamp TIMESTAMPTZ NOT NULL,
    correlation_id VARCHAR,
    payload JSON,
    UNIQUE(entity_type, entity_id, entity_revision)
);
```

`mutate()` reads the current materialized revision inside the transaction, requires the next revision, invokes the typed materialized callback, inserts the event, and returns the inserted row. A retry with the same event ID returns the existing event only when every canonical field matches; otherwise it raises `EventIdentityConflict`.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --frozen pytest tests/test_schema_migrations.py tests/test_domain_journal.py -q`

Expected: PASS.

```bash
git add trader/data/schema_migrations.py trader/data/domain_journal.py tests/test_schema_migrations.py tests/test_domain_journal.py
git commit -m "feat(m1-f): persist atomic materialized events"
```

### Task 4: Fenced materialized snapshot

**Files:**
- Create: `trader/data/materialized_state.py`
- Create: `trader/domain/snapshot_service.py`
- Create: `tests/test_domain_snapshot.py`

**Interfaces:**
- Produces: `MaterializedAdapter` protocol with `entity_type`, `select_active(conn)`, and `checkpoint(conn)`.
- Produces: `DomainSnapshotService.snapshot_with_cursor() -> SnapshotWithCursor`.

- [ ] **Step 1: Write an interleaving fence test**

```python
def test_snapshot_cursor_covers_exact_returned_revisions(snapshot_service, writer):
    writer.position(quantity=10, event_id="p1")
    snapshot = snapshot_service.snapshot_with_cursor(on_read_started=lambda: writer.position(quantity=20, event_id="p2"))
    position = snapshot.entities["position"][0]
    assert position["quantity"] == 10
    assert snapshot.source_cursor == 1
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_domain_snapshot.py -q`

Expected: FAIL because no fenced snapshot service exists.

- [ ] **Step 3: Implement one-read-transaction fencing**

Open a dedicated read connection, `BEGIN TRANSACTION`, read the newest complete broker generation, read every registered materialized adapter, then read `MAX(source_cursor)` before `COMMIT`. Return JSON-native named fields only. If no complete broker generation exists, raise `SnapshotNotReady` instead of returning a partial view.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --frozen pytest tests/test_domain_snapshot.py -q`

Expected: PASS.

```bash
git add trader/data/materialized_state.py trader/domain/snapshot_service.py tests/test_domain_snapshot.py
git commit -m "feat(m1-f): return fenced domain snapshots"
```

### Task 5: Cursor long-poll reader

**Files:**
- Create: `trader/domain/feed_service.py`
- Create: `tests/test_domain_feed.py`
- Modify: `trader/messaging/production_api.py`

**Interfaces:**
- Produces: `DomainFeedService.read_domain_events(after_cursor: int, limit: int, wait_ms: int) -> ReadDomainEventsResult`.
- Registers typed feed method `read_domain_events` and typed query method `snapshot_with_cursor`.

- [ ] **Step 1: Write wake-up, heartbeat, and cursor-expiry tests**

```python
def test_long_poll_wakes_after_commit(feed_service, writer):
    future = executor.submit(feed_service.read_domain_events, 0, 100, 10_000)
    writer.account(net_liquidation=50_000, event_id="a1")
    result = future.result(timeout=1)
    assert [event.event_id for event in result.events] == ["a1"]


def test_cursor_before_retention_requires_snapshot(feed_service):
    with pytest.raises(CursorExpired):
        feed_service.read_domain_events(7, 100, 0)
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_domain_feed.py -q`

Expected: FAIL because the feed service does not exist.

- [ ] **Step 3: Implement commit signalling and dedicated reads**

Use a `threading.Condition` owned by `DomainJournal`. Signal it only after the transaction commits. The reader queries `source_cursor > after_cursor ORDER BY source_cursor LIMIT limit`; if empty, waits for the remaining timeout and retries. Clamp `limit` to `1..1000` and `wait_ms` to `0..10000`. Return the newest cursor even for an empty heartbeat.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --frozen pytest tests/test_domain_feed.py tests/test_typed_rpc_transport.py -q`

Expected: PASS.

```bash
git add trader/domain/feed_service.py trader/messaging/production_api.py tests/test_domain_feed.py
git commit -m "feat(m1-f): expose fenced snapshot and long-poll feed"
```

### Task 6: Retention, checkpoints, and foundation integration gate

**Files:**
- Modify: `trader/data/domain_journal.py`
- Modify: `trader/domain/snapshot_service.py`
- Create: `tests/test_domain_retention.py`
- Create: `tests/integration/test_domain_foundation.py`

**Interfaces:**
- Produces: `DomainJournal.compact(now, active_cursors) -> CompactionResult`.
- Produces a checkpoint record only after a complete broker generation is fenced.

- [ ] **Step 1: Write retention tests**

```python
def test_compaction_preserves_active_cursor_and_latest_checkpoint(journal):
    result = journal.compact(
        now=dt.datetime(2026, 8, 15, tzinfo=dt.timezone.utc),
        active_cursors={"dashboard": 41},
    )
    assert result.oldest_retained_cursor <= 41
    assert journal.latest_checkpoint() is not None
```

- [ ] **Step 2: Implement bounded compaction**

Delete only rows older than 30 days, below every active cursor, and not required to reconstruct the newest complete checkpoint. Record `oldest_retained_cursor`, `newest_cursor`, deletion count, and completion time. If checkpoint creation or compaction fails, retain data and expose degraded maintenance health.

- [ ] **Step 3: Run the foundation gate**

Run: `uv run --frozen pytest tests/test_duckdb_store.py tests/test_domain_events.py tests/test_domain_identity.py tests/test_schema_migrations.py tests/test_domain_journal.py tests/test_domain_snapshot.py tests/test_domain_feed.py tests/test_domain_retention.py tests/integration/test_domain_foundation.py -q`

Expected: PASS, including crash injection at materialized-write, journal-insert, commit, and post-commit-signal boundaries.

- [ ] **Step 4: Commit**

```bash
git add trader/data/domain_journal.py trader/domain/snapshot_service.py tests/test_domain_retention.py tests/integration/test_domain_foundation.py
git commit -m "test(m1-f): gate journal snapshot and feed coherence"
```
