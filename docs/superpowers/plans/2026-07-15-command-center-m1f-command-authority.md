# M1-F3 Proposal, Command, Pause, and Strategy Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `trader_service` the sole durable authority for proposal, command, pause, and forwarded strategy mutations: a migrated guarded proposal schema, an idempotent command ledger with a durable order saga, a per-account pause gate, strategy control revisions with a receipt ledger, and thin typed SDK/CLI/SignalProposer clients.

**Architecture:** Relocate proposal ownership from SDK orchestration into an in-process `ProposalCommandService` (create/reject/expiry/validation) with `ProposalRepository` as its persistence adapter. Every mutation and its `proposal.updated` / `command.updated` / `trading_control.updated` journal event commit in one explicit DuckDB transaction. A `TradingCommandCoordinator` is the only production mutation boundary: it claims the `command_ledger` row and audit record before any validation or side effect, binds `command_id` to IB `orderRef`, and reconciles ambiguous outcomes on a fixed schedule instead of timing out to failure. Strategy mutations are forwarded with the root command ID into a strategy-service receipt ledger so a coordinator retry can never repeat a mutation.

**Tech Stack:** CPython 3.12.13, DuckDB, dataclasses, Pydantic, typed canonical-JSON RPC over ZeroMQ (`[G0]`), `DomainJournal`/`SchemaMigrator`/`DuckDBConnection.transaction` (`[M1-F1]`), pytest.

## Global Constraints

- Spec sections §4.4, §5.4–§5.6, and §9.1–§9.7 of `docs/superpowers/specs/2026-07-15-realtime-trading-command-center-design.md` govern this plan. The cross-plan interface freeze in `2026-07-15-command-center-plan-index.md` is binding: `create_proposal`, `approve_proposal`, `reject_proposal`, `set_trading_pause`, `enable_strategy`, `disable_strategy`, `update_strategy_params`, `cancel_order`, `cancel_orders`, `get_command`, and `CommandReceipt` keep exactly those names.
- This plan consumes and does not redefine: `[M1-F1]` `DuckDBConnection.transaction(fn)`, `DomainJournal.mutate(conn, mutation, write_materialized)`, `SchemaMigrator.apply(version, name, statements)`, and the frozen domain-event dataclasses; `[G0]` `TypedRpcRegistry.register(socket_role, method, request_model, response_model, handler)` with roles `query`/`command`/`feed`, `HmacServiceAuthenticator`, and `build_production_registry`.
- This plan SUPERSEDES `[S0]`'s `ProposalStore.claim_for_approval` and `ProposalStore.expire_stale_pending` with trader-owned components while preserving their behavior: a missing expiry remains valid for legacy manual proposals; a present naive/invalid expiry fails closed as `EXPIRED`; approval never dispatches without one atomic claim proving the row is still `PENDING` and unexpired.
- `trader_service` is the sole production writer of `trade_proposals`, `command_ledger`, `command_audit`, and `trading_control_state`. Every durable mutation and its journal event commit in one explicit transaction (see the pre-flight resolution below for the exact mechanism).

> **Pre-flight resolution (mutate() usage + journal-file topology — binding, from M1-F1 T3 verification).** The landed `[M1-F1]` journal lives in a dedicated `journal_duckdb_path` file, and `DomainJournal.mutate(conn, mutation, write_materialized)` SELF-MANAGES its `BEGIN/COMMIT` on a `journal.connect()` connection — so it must NOT be wrapped in `DuckDBConnection.transaction` (that double-`BEGIN`s and DuckDB rejects nested `BEGIN`). Consequence for this plan: the domain-journaled command-authority materialized tables (`trade_proposals`, `command_ledger`, `command_audit`, `trading_control_state`) MOVE INTO the journal file so a mutation and its journal event share one transaction — this is consistent with this plan already superseding `[S0]`'s `mmr.duckdb` `ProposalStore` and with `[G0]`'s RPC-only read path (the CLI/dashboard read these via typed RPC/snapshot, never direct DuckDB, so trader-exclusive ownership is fine). Rewrite every `self._db.transaction(_tx)` / `self._db.transaction(_claim)` that calls `journal.mutate` to instead run on `journal.connect()` via `mutate()` directly, with the repository's own materialized write (and the documented no-op `write_materialized` for tables the repo mutates itself) inside `mutate()`'s transaction. Non-journaled bookkeeping that does NOT call `mutate()` (e.g. a pure `command_ledger` claim with no journal event) may still use its own single transaction on the journal connection. Multi-step sagas emit one `mutate()` per transition (each self-committing); they never wrap several `mutate()` calls in one outer transaction.

> **Pre-flight resolutions 2 (binding, from the M1-F3 understand-phase — these OVERRIDE any conflicting task-body code sample).**
> - **B1 (samples are illustrative, this rule governs):** wherever a task's code sample below shows `self._db.transaction(lambda conn: … journal.mutate(conn, …) …)`, that pattern is KNOWN-INCORRECT (double-`BEGIN`) and is superseded by the pre-flight resolution above — call `mutate()` directly on `journal.connect()`, one per transition, no `DuckDBConnection.transaction` wrapper. Do not copy the wrapper from the samples.
> - **B2 (Task 1 is a CROSS-FILE relocation, not in-place):** the four authority tables live in the journal file, so Task 1's migration must MOVE existing rows across files, not migrate `trade_proposals` in place in `mmr.duckdb`. Implement it as: create the tables in the journal DB (migration v20 on the journal `SchemaMigrator`), then `ATTACH '<mmr.duckdb>' AS legacy; INSERT INTO trade_proposals SELECT … FROM legacy.trade_proposals; DETACH legacy;` (migrated rows: `live_approval_eligible=false`, `revision=1`, guard/account/mode/reference columns NOT backfilled). Retarget all Task 1 fixtures/tests to the journal `DuckDBConnection`. The `[S0]` `mmr.duckdb` `trade_proposals` is frozen (no further writes) after cutover — add `test_legacy_writers_are_frozen_after_cutover`.
> - **B3 (`state_revision` rides in payload, never `entity_revision`):** `mutate()` computes the journal `entity_revision` itself (`current+1`) and `DomainMutation` has no `entity_revision` field; the strategy-service `state_revision` counter is independent. Carry `state_revision` in the event `payload` and assert on the payload (Tasks 7/9) — never claim the frozen contract lets a caller supply `entity_revision`.
> - **Colon-free command IDs (I3):** `encode_order_ref` builds `mmr:og-{command_id}`, so `command_id` MUST be colon-free — use `sdk-<uuid>` / `strategy-<uuid>`, and cancel child IDs `{root}__ord-<order_entity_id-sanitized>`. Add a `decode(encode("og-sdk-<uuid>"))` round-trip test.
> - **Import correction:** import `BrokerOrderRow` and the broker store from `trader.data.broker_state` (landed location), NOT the plan's stated `trader/trading/broker_state.py`.
> - **F2 dependency gating:** `encode_order_ref`/`decode_order_ref` (`trader/trading/order_correlation.py`), `RiskProducer.publish_decision`, `QuoteSubscriptionManager.set_owner_refs`, and the `OrderStateView` read protocol (with `BrokerOrderRow.leg_role`/`is_terminal`/`quantity`) are `[M1-F2]` deliverables that DO NOT EXIST YET. Tasks 5 & 6 dispatch/binding are HARD-BLOCKED until F2 lands them — build Phase A (Tasks 1–4) now; stub the single `set_owner_refs("proposal"/"strategy", …)` wiring line in Tasks 2/7; defer Tasks 5/6 and Task 5's `trading_runtime.py` edit until F2 is merged. Never invent a substitute `orderRef` encoding.
- Command-ledger rules: the exact-retry lookup by `command_id` + canonical request hash precedes preflight-nonce validation; reuse of a `command_id` with a different payload is a conflict; the ledger row and mandatory audit record are inserted before validation or side effects, and failure to persist either fails the command closed. Consuming a preflight nonce and claiming the command occur in the same transaction (nonce *issuing* is `[M1-C]`; this plan implements the coordinator-side verification hook).
- Order-command saga states are exactly `RECEIVED`, `VALIDATED`, `SUBMITTING`, `SUBMITTED`, `REJECTED`, `OUTCOME_UNKNOWN`, `RESOLVED`. `command_id` is bound into IB `orderRef` through `[M1-F2]`'s `encode_order_ref(order_group_id)` (`trader/trading/order_correlation.py`, `mmr:` prefix) with `order_group_id = f"og-{command_id}"` — this plan never invents a different `orderRef` encoding. `OUTCOME_UNKNOWN` is reconciled immediately, every 5 s for the first minute, every 30 s for the next 14 minutes, then raises a critical alert; it is never converted to failure by timeout alone. Ledger and audit retention is 30 days; unresolved commands are never purged.
- Proposal expiry is owned by the trader process: one sweep at startup and every 30 seconds, without a query limit; approval independently rechecks expiry inside the claiming transaction.
- Live exposure-increasing approval requires a live, non-delayed, non-frozen feed and an executable-side quote (ask for BUY, bid for SELL) no older than five seconds, plus the proposal's recorded drift guard (default 50 bps). Position-reducing exits are exempt from feed/session/drift guards but capped at the verified reducible quantity.
- Pause rows are absolute sets, never toggles, seeded idempotently at revision 1 by command ID `system:bootstrap` with reason `account initialization` (live paused, paper unpaused). A missing or unreadable row fails closed for exposure-increasing actions. Pause mutation serializes against final order dispatch through the shared claiming transaction.
- No production command schema carries a caller-controlled risk bypass (`extra="forbid"` on every request model); risk-limit administration is not registered on the coordinator's command socket.
- Trader-database `schema_migrations` version ranges are partitioned across plans: `[M1-F1]` owns 1–9, `[M1-F2]` owns 10–19, and this plan owns 20–29. Migrations here use versions 20–22; strategy-service revision state lives in the strategy service's own DuckDB file with its own migrator sequence starting at 1.
- Every coordinator risk decision is recorded exactly once through `[M1-F2]`'s write-once `RiskProducer.publish_decision(command_id, payload, correlation_id=None)` (risk ID namespace `decision:<command_id>`). Quote coverage for pending proposals and running strategies is maintained through `[M1-F2]`'s `QuoteSubscriptionManager.set_owner_refs` (`trader/trading/quote_coverage.py`) with owner kinds `"proposal"` and `"strategy"`.
- Strategy-service typed sockets bind on the private Compose network only: command role on port `42104`, query role on port `42105` (`strategy_typed_command_port` / `strategy_typed_query_port`).

---

### Task 1: Proposal schema migration and exclusive versioned cutover

**Files:**
- Create: `trader/data/proposal_repository.py` (migration + record types only in this task)
- Modify: `trader/data/proposal_store.py`
- Create: `tests/test_proposal_migration.py`
- Modify: `tests/test_proposal_store.py`

**Interfaces:**
- Consumes: `SchemaMigrator.apply(version: int, name: str, statements: Sequence[str])` from `[M1-F1]`.
- Produces: `apply_proposal_authority_migration(migrator: SchemaMigrator) -> None` (trader-DB migration version 20, name `m1f3_proposal_command_authority` — `[M1-F3]` owns versions 20–29).
- Produces: `ProposalRecord` frozen dataclass mirroring the migrated column list, and module constant `PROPOSAL_COLUMNS` (the explicit stable column list).
- Produces: `ProposalStoreFrozen(RuntimeError)` — every legacy `ProposalStore` write raises it once the cutover is applied to that database file; reads remain allowed for the read-only window.

- [ ] **Step 1: Write failing migration tests**

```python
import datetime as dt
import json

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.proposal_store import ProposalStore, ProposalStoreFrozen
from trader.data.schema_migrations import SchemaMigrator
from trader.data.proposal_repository import (
    PROPOSAL_COLUMNS,
    apply_proposal_authority_migration,
)
from trader.trading.proposal import TradeProposal


@pytest.fixture
def legacy_db(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "trader.duckdb"))
    store = ProposalStore(str(tmp_path / "trader.duckdb"))
    return db, store


def test_migration_copies_parsable_metadata_exactly(legacy_db):
    db, store = legacy_db
    pid = store.add(TradeProposal(
        symbol="AAPL", action="BUY", amount=5000.0, source="strategy:orb",
        metadata={"conid": 265598, "expires_at": "2026-07-15T10:00:00+00:00"},
    ))
    apply_proposal_authority_migration(SchemaMigrator(db))
    row = db.execute(
        "SELECT conid, expires_at, live_approval_eligible, revision, metadata "
        "FROM trade_proposals WHERE id = ?", [pid], fetch="one")
    assert row[0] == 265598
    assert row[1] == dt.datetime(2026, 7, 15, 10, 0, tzinfo=dt.timezone.utc)
    assert row[2] is False            # legacy rows never gain live guards
    assert row[3] == 1                # every migrated row starts at revision 1
    assert json.loads(row[4])["conid"] == 265598   # original metadata preserved


def test_migration_leaves_invalid_values_null_and_live_ineligible(legacy_db):
    db, store = legacy_db
    pid = store.add(TradeProposal(
        symbol="AAPL", action="BUY", source="strategy:orb",
        metadata={"conid": "not-a-number", "expires_at": "2026-07-15T10:30:00"},
    ))  # naive timestamp and non-integer conid
    apply_proposal_authority_migration(SchemaMigrator(db))
    row = db.execute(
        "SELECT conid, expires_at, account_id, account_mode, reference_price, "
        "live_approval_eligible FROM trade_proposals WHERE id = ?",
        [pid], fetch="one")
    assert row[0] is None and row[1] is None
    assert row[2] is None and row[3] is None and row[4] is None  # never inferred
    assert row[5] is False


def test_migration_is_idempotent(legacy_db):
    db, store = legacy_db
    store.add(TradeProposal(symbol="AAPL", action="BUY"))
    apply_proposal_authority_migration(SchemaMigrator(db))
    apply_proposal_authority_migration(SchemaMigrator(db))   # must be a no-op
    count = db.execute("SELECT COUNT(*) FROM trade_proposals", fetch="one")
    assert count[0] == 1


def test_legacy_writers_are_frozen_after_cutover(legacy_db):
    db, store = legacy_db
    pid = store.add(TradeProposal(symbol="AAPL", action="BUY"))
    apply_proposal_authority_migration(SchemaMigrator(db))
    with pytest.raises(ProposalStoreFrozen):
        store.add(TradeProposal(symbol="MSFT", action="BUY"))
    with pytest.raises(ProposalStoreFrozen):
        store.update_status(pid, "REJECTED")
    with pytest.raises(ProposalStoreFrozen):
        store.try_transition(pid, "PENDING", "REJECTED")
    # Reads stay available during the read-only window.
    assert store.get(pid).symbol == "AAPL"


def test_explicit_column_list_survives_added_columns(legacy_db):
    db, store = legacy_db
    pid = store.add(TradeProposal(
        symbol="BHP", action="BUY", exchange="ASX", currency="AUD", group="mining"))
    apply_proposal_authority_migration(SchemaMigrator(db))
    restored = store.get(pid)
    assert (restored.exchange, restored.currency, restored.group) == ("ASX", "AUD", "mining")
    assert len(PROPOSAL_COLUMNS) == 29
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run --frozen pytest tests/test_proposal_migration.py -q`

Expected: FAIL because `proposal_repository.py`, `ProposalStoreFrozen`, and the migration do not exist.

- [ ] **Step 3: Replace positional `SELECT *` decoding with the explicit stable column list**

In `trader/data/proposal_store.py`, before any column is added, define the legacy list and use it in `get()` and `query()`:

```python
_LEGACY_COLUMNS = (
    "id, symbol, action, quantity, amount, execution, reasoning, confidence, "
    "thesis, source, metadata, status, created_at, updated_at, order_ids, "
    "rejection_reason, sec_type"
)
```

```python
rows = self.db.execute(
    f"SELECT {_LEGACY_COLUMNS} FROM trade_proposals WHERE id = ?", [id], fetch='all',
)
```

`_rows_to_proposals` keeps its positional decoding — the positions are now pinned by `_LEGACY_COLUMNS`, not by physical table order, so the migration's added columns cannot shift them.

- [ ] **Step 4: Implement the migration and the write freeze**

In `trader/data/proposal_repository.py`:

```python
PROPOSAL_AUTHORITY_MIGRATION_VERSION = 20   # [M1-F3] owns 20-29 (1-9 = [M1-F1], 10-19 = [M1-F2])

PROPOSAL_COLUMNS: tuple[str, ...] = (
    "id", "symbol", "action", "quantity", "amount", "execution", "reasoning",
    "confidence", "thesis", "source", "metadata", "status", "created_at",
    "updated_at", "order_ids", "rejection_reason", "sec_type",
    "account_id", "account_mode", "conid", "reference_price",
    "reference_timestamp", "reference_quote_side", "reference_feed_type",
    "max_price_drift_bps", "expires_at", "live_approval_eligible", "revision",
    "order_group_id",
)

_EXPIRY_JSON = "json_extract_string(metadata, '$.expires_at')"
_AWARE_EXPIRY = (
    f"regexp_matches({_EXPIRY_JSON}, '(Z|[+-][0-9]{{2}}:[0-9]{{2}})$') "
    f"AND TRY_CAST({_EXPIRY_JSON} AS TIMESTAMPTZ) IS NOT NULL"
)

_MIGRATION_STATEMENTS = [
    """
    CREATE TABLE trade_proposals_v2 (
        id INTEGER PRIMARY KEY,
        symbol VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        quantity DOUBLE,
        amount DOUBLE,
        execution VARCHAR DEFAULT '{}',
        reasoning VARCHAR DEFAULT '',
        confidence DOUBLE DEFAULT 0.0,
        thesis VARCHAR DEFAULT '',
        source VARCHAR DEFAULT 'manual',
        metadata VARCHAR DEFAULT '{}',
        status VARCHAR DEFAULT 'PENDING',
        created_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        order_ids VARCHAR DEFAULT '[]',
        rejection_reason VARCHAR DEFAULT '',
        sec_type VARCHAR DEFAULT 'STK',
        account_id VARCHAR,
        account_mode VARCHAR,
        conid INTEGER,
        reference_price DOUBLE,
        reference_timestamp TIMESTAMPTZ,
        reference_quote_side VARCHAR,
        reference_feed_type VARCHAR,
        max_price_drift_bps DOUBLE,
        expires_at TIMESTAMPTZ,
        live_approval_eligible BOOLEAN NOT NULL DEFAULT false,
        revision BIGINT NOT NULL DEFAULT 1,
        order_group_id VARCHAR
    )
    """,
    f"""
    INSERT INTO trade_proposals_v2 (
        id, symbol, action, quantity, amount, execution, reasoning, confidence,
        thesis, source, metadata, status, created_at, updated_at, order_ids,
        rejection_reason, sec_type, conid, expires_at, live_approval_eligible,
        revision
    )
    SELECT
        id, symbol, action, quantity, amount, execution, reasoning, confidence,
        thesis, source, metadata, status, created_at, updated_at, order_ids,
        rejection_reason, sec_type,
        TRY_CAST(json_extract_string(metadata, '$.conid') AS INTEGER),
        CASE WHEN {_AWARE_EXPIRY}
             THEN TRY_CAST({_EXPIRY_JSON} AS TIMESTAMPTZ)
             ELSE NULL END,
        false,
        1
    FROM trade_proposals
    """,
    "DROP TABLE trade_proposals",
    "ALTER TABLE trade_proposals_v2 RENAME TO trade_proposals",
]


def apply_proposal_authority_migration(migrator) -> None:
    """Exclusive versioned cutover (spec §5.5). Idempotent: SchemaMigrator
    records version 20 in schema_migrations and skips it on re-run. Account,
    mode, reference price/side/feed, and drift guard are never backfilled —
    only parsable metadata conid and timezone-aware expires_at are copied.
    Original metadata is preserved verbatim for audit."""
    migrator.apply(
        version=PROPOSAL_AUTHORITY_MIGRATION_VERSION,
        name="m1f3_proposal_command_authority",
        statements=_MIGRATION_STATEMENTS,
    )
```

In `trader/data/proposal_store.py`, add the freeze. `__init__` sets `self._frozen = None`; every write entry point (`add`, `update_metadata`, `update_status`, `try_transition`, `delete`, `claim_for_approval`, `expire_stale_pending`) calls `self._assert_writable()` first:

```python
class ProposalStoreFrozen(RuntimeError):
    """Legacy trade_proposals writes are disabled after the [M1-F3] cutover."""


def _cutover_applied(self) -> bool:
    if self._frozen is None:
        row = self.db.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = 'trade_proposals' AND column_name = 'revision'",
            fetch='one',
        )
        self._frozen = bool(row and row[0])
    return self._frozen


def _assert_writable(self) -> None:
    if self._cutover_applied():
        raise ProposalStoreFrozen(
            'trade_proposals is owned by trader_service after the [M1-F3] '
            'cutover — use the typed create_proposal/reject_proposal/'
            'approve_proposal APIs'
        )
```

Old binaries never regain write access after cutover because they cannot maintain `revision` or journal atomicity; operationally the cutover runs with every non-trader writer stopped (`docker.sh -d` for strategy/dashboard, no CLI sessions), and the freeze makes any straggler running *this* code fail loudly instead of silently corrupting revisions. Rollback before any post-cutover mutation is a backup restore; after that it is forward-compatible service rollback only.

- [ ] **Step 5: Run migration and store tests**

Run: `uv run --frozen pytest tests/test_proposal_migration.py tests/test_proposal_store.py -q`

Expected: PASS (existing `ProposalStore` tests still pass because fresh test databases have no migration applied).

- [ ] **Step 6: Commit**

```bash
git add trader/data/proposal_repository.py trader/data/proposal_store.py tests/test_proposal_migration.py tests/test_proposal_store.py
git commit -m "feat(m1-f3): migrate proposals to explicit guarded columns"
```

### Task 2: ProposalRepository and ProposalCommandService

**Files:**
- Modify: `trader/data/proposal_repository.py`
- Create: `trader/trading/proposal_command_service.py`
- Create: `tests/test_proposal_command_service.py`
- Modify: `trader/trader_service.py` (start the expiry task)

**Interfaces:**
- Consumes: `DuckDBConnection.transaction(fn)`, `DomainJournal.mutate(conn, mutation, write_materialized)`, `DomainMutation` from `[M1-F1]`; `QuoteSubscriptionManager.set_owner_refs(owner_kind: str, conids: set[int])` from `[M1-F2]` (`trader/trading/quote_coverage.py`); `RiskGate.check_instrument`, `PositionSizer.compute`, `PositionGroupStore.add_member` from the existing codebase.
- Produces: `ProposalRepository` with `get(id) -> ProposalRecord | None`, `list(status, limit) -> list[ProposalRecord]`, and in-transaction methods `insert_pending_in_tx(conn, draft: ProposalDraft) -> ProposalRecord`, `claim_for_approval_in_tx(conn, id, expected_revision, account_id, now) -> ApprovalClaimOutcome`, `reject_in_tx(conn, id, reason, now) -> ProposalRecord | None`, `expire_stale_pending_in_tx(conn, now) -> list[ProposalRecord]`, `pending_duplicate_in_tx(conn, source, conid, action) -> bool`, `link_order_group_in_tx(conn, id, order_group_id)`, `mark_order_submitted_in_tx(conn, id, order_ids)`, `mark_failed_in_tx(conn, id, reason)`, `journal_in_tx(conn, record, correlation_id)`.
- Produces: `ApprovalClaim(str, Enum)` with `CLAIMED`, `EXPIRED`, `REVISION_MISMATCH`, `NOT_PENDING`, `NOT_FOUND`, `WRONG_ACCOUNT`; `ApprovalClaimOutcome(result: ApprovalClaim, record: ProposalRecord | None)`.
- Produces: `ProposalCommandService` with `create_proposal(request, source, correlation_id) -> ProposalRecord`, `reject_proposal(id, reason, correlation_id) -> ProposalRecord`, `expire_stale(now) -> list[int]`, `run_expiry_loop()`, and `ProposalCreationRefused(code, message)`.
- Produces port protocols implemented by `[M1-F2]` in production and fakes in tests: `QuoteAuthority.executable_quote(conid: int, side: Literal["bid","ask"]) -> ExecutableQuote | None`, `AccountStatePort.portfolio_state() -> PortfolioState`, and `ExecutableQuote(conid, side, price, market_timestamp, feed_type, session_state)`.

- [ ] **Step 1: Write failing creation, guard-shape, and expiry tests**

```python
UTC = dt.timezone.utc


@pytest.fixture
def authority(tmp_path):
    """Migrated trader DB + service wired with fakes."""
    path = str(tmp_path / "trader.duckdb")
    db = DuckDBConnection.get_instance(path)
    ProposalStore(path)                                   # creates legacy table
    apply_proposal_authority_migration(SchemaMigrator(db))
    journal = DomainJournal(db)
    repo = ProposalRepository(db, journal)
    quotes = FakeQuoteAuthority({265598: ExecutableQuote(
        conid=265598, side="ask", price=210.0,
        market_timestamp=dt.datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
        feed_type="live", session_state="continuous")})
    service = ProposalCommandService(
        db=db, repository=repo, journal=journal,
        risk_gate=FakeRiskGate(), quotes=quotes,
        account_state=FakeAccountState(net_liquidation=100_000.0),
        controls=AlwaysUnpausedControls(), group_store=FakeGroupStore(),
        universe=FakeUniverse({265598: _secdef("AAPL")}),
        account_id="DU111111", account_mode="paper",
        now=lambda: dt.datetime(2026, 7, 15, 14, 0, 1, tzinfo=UTC),
    )
    return SimpleNamespace(db=db, repo=repo, service=service, quotes=quotes, journal=journal)


def test_create_proposal_is_guard_complete_at_creation(authority):
    record = authority.service.create_proposal(
        _create_request(conid=265598, action="BUY", confidence=0.7, group="tech"),
        source="dashboard", correlation_id="cmd-1")
    assert record.status == "PENDING" and record.revision == 1
    assert record.account_id == "DU111111" and record.account_mode == "paper"
    assert record.conid == 265598
    assert record.reference_price == 210.0
    assert record.reference_quote_side == "ask" and record.reference_feed_type == "live"
    assert record.max_price_drift_bps == 50.0            # spec default
    assert record.expires_at is not None and record.expires_at.tzinfo is not None
    assert record.live_approval_eligible is True
    assert record.amount is not None and record.amount > 0     # auto-sized
    events = authority.journal.read_after(0, 100)
    assert [e.event_type for e in events] == ["proposal.updated"]
    assert events[0].correlation_id == "cmd-1"


def test_missing_quote_refuses_and_inserts_nothing(authority):
    authority.quotes.clear()
    with pytest.raises(ProposalCreationRefused) as exc:
        authority.service.create_proposal(
            _create_request(conid=265598, action="BUY"),
            source="dashboard", correlation_id="cmd-2")
    assert exc.value.code == "QUOTE_UNAVAILABLE"
    assert authority.repo.list(status="PENDING", limit=10) == []
    assert authority.journal.read_after(0, 100) == []     # no insert-then-enrich


def test_trading_filter_rejection_refuses_creation(authority):
    authority.service._risk_gate.deny_instrument("AAPL", "denylisted")
    with pytest.raises(ProposalCreationRefused) as exc:
        authority.service.create_proposal(
            _create_request(conid=265598, action="BUY"),
            source="dashboard", correlation_id="cmd-3")
    assert exc.value.code == "TRADING_FILTER_REJECTED"


def test_strategy_duplicate_pending_is_refused(authority):
    request = _create_request(conid=265598, action="BUY")
    authority.service.create_proposal(request, source="strategy:orb", correlation_id="c1")
    with pytest.raises(ProposalCreationRefused) as exc:
        authority.service.create_proposal(request, source="strategy:orb", correlation_id="c2")
    assert exc.value.code == "DUPLICATE_PENDING"


def test_reject_is_idempotent_pending_to_rejected_cas(authority):
    record = authority.service.create_proposal(
        _create_request(conid=265598, action="BUY"), source="dashboard", correlation_id="c1")
    first = authority.service.reject_proposal(record.id, "changed thesis", "c2")
    again = authority.service.reject_proposal(record.id, "changed thesis", "c3")
    assert first.status == "REJECTED" and again.status == "REJECTED"
    assert again.revision == first.revision               # no second mutation


def test_expiry_sweep_expires_all_stale_rows_and_journals(authority):
    record = authority.service.create_proposal(
        _create_request(conid=265598, action="BUY"), source="strategy:orb", correlation_id="c1")
    later = record.expires_at + dt.timedelta(seconds=1)
    expired = authority.service.expire_stale(later)
    assert expired == [record.id]
    stored = authority.repo.get(record.id)
    assert stored.status == "EXPIRED" and stored.revision == record.revision + 1
    kinds = [e.event_type for e in authority.journal.read_after(0, 100)]
    assert kinds.count("proposal.updated") == 2           # create + expire


def test_legacy_row_with_unparsable_expiry_fails_closed(authority):
    # Simulate a migrated row: metadata carries expires_at but the column is NULL.
    authority.db.execute(
        "INSERT INTO trade_proposals (id, symbol, action, metadata, status, "
        "created_at, updated_at, live_approval_eligible, revision) "
        "VALUES (999, 'AAPL', 'BUY', ?, 'PENDING', now(), now(), false, 1)",
        [json.dumps({"expires_at": "2026-07-15T10:30:00"})], fetch="none")
    assert 999 in authority.service.expire_stale(dt.datetime(2026, 7, 15, 14, 0, tzinfo=UTC))
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run --frozen pytest tests/test_proposal_command_service.py -q`

Expected: FAIL because `ProposalRepository` methods and `ProposalCommandService` do not exist.

- [ ] **Step 3: Implement the repository**

In `trader/data/proposal_repository.py`, add `ProposalDraft` (creation fields), `ProposalRecord` (all 29 columns, typed), and the SQL. The two load-bearing statements:

```python
_UNEXPIRED = (
    "((expires_at IS NULL AND json_extract_string(metadata, '$.expires_at') IS NULL) "
    "OR expires_at > ?)"
)

_CLAIM_SQL = f"""
    UPDATE trade_proposals
       SET status = CASE WHEN {_UNEXPIRED} THEN 'APPROVED' ELSE 'EXPIRED' END,
           revision = revision + 1,
           updated_at = ?
     WHERE id = ? AND status = 'PENDING' AND revision = ?
       AND (account_id IS NULL OR account_id = ?)
 RETURNING {', '.join(PROPOSAL_COLUMNS)}
"""

_EXPIRE_SQL = f"""
    UPDATE trade_proposals
       SET status = 'EXPIRED', revision = revision + 1, updated_at = ?
     WHERE status = 'PENDING'
       AND ((expires_at IS NOT NULL AND expires_at <= ?)
            OR (expires_at IS NULL
                AND json_extract_string(metadata, '$.expires_at') IS NOT NULL))
 RETURNING {', '.join(PROPOSAL_COLUMNS)}
"""
```

`_EXPIRE_SQL` preserves `[S0]` semantics exactly: a row with no expiry anywhere stays valid; an elapsed aware expiry expires; a row whose metadata carries an expiry that could not be copied (naive/invalid, column NULL after migration) fails closed. There is no query limit.

`claim_for_approval_in_tx` runs `_CLAIM_SQL`; a returned row with status `APPROVED` is `CLAIMED`, with `EXPIRED` is `EXPIRED`. On no row it distinguishes `NOT_FOUND` / `NOT_PENDING` / `WRONG_ACCOUNT` / `REVISION_MISMATCH` with one follow-up `SELECT status, revision, account_id`. Every mutating `_in_tx` method ends by returning the complete `ProposalRecord` decoded from `RETURNING`.

`journal_in_tx` appends the `proposal.updated` event for a row the repository has already materialized in the same transaction:

```python
def journal_in_tx(self, conn, record: ProposalRecord, correlation_id: str | None) -> None:
    """trade_proposals IS the materialized store for proposals, and this
    repository mutates it directly, so write_materialized is a documented
    no-op. UNIQUE(entity_type, entity_id, entity_revision) in the journal
    enforces exactly-once per revision."""
    self._journal.mutate(
        conn,
        DomainMutation(
            event_type="proposal.updated",
            entity_type="proposal",
            entity_id=str(record.id),
            operation="upsert",
            account_id=record.account_id,
            source="trader_service",
            source_timestamp=_utc(record.updated_at),
            correlation_id=correlation_id,
            payload=record.to_payload(),      # full summary, status, guards, revision, order-group link
        ),
        write_materialized=lambda _conn: None,
    )
```

- [ ] **Step 4: Implement the command service**

In `trader/trading/proposal_command_service.py`:

```python
class ProposalCreationRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ProposalCommandService:
    DEFAULT_DRIFT_BPS = 50.0

    def create_proposal(self, request, *, source: str, correlation_id: str) -> ProposalRecord:
        secdef = self._resolve_conid(request.conid)          # exact local lookup or refuse
        check = self._risk_gate.check_instrument(
            symbol=secdef.symbol, exchange=secdef.primaryExchange or '',
            sec_type=getattr(secdef, 'secType', '') or 'STK')
        if not check.approved:
            raise ProposalCreationRefused("TRADING_FILTER_REJECTED", check.reason)
        if source.startswith('strategy:') and self._account_mode == 'live' \
                and not self._strategy_live_propose_enabled:
            raise ProposalCreationRefused(
                "STRATEGY_LIVE_PROPOSE_DISABLED",
                "STRATEGY_LIVE_PROPOSE_ENABLED is false (spec §9.2)")

        if self._direction(request) is RiskDirection.INCREASING:
            self._controls.require_unpaused(self._account_id)   # Task 4 wires the real gate

        side = 'ask' if request.action == 'BUY' else 'bid'
        quote = self._quotes.executable_quote(request.conid, side=side)
        if quote is None:
            raise ProposalCreationRefused(
                "QUOTE_UNAVAILABLE",
                f"no fresh executable {side} quote for conId {request.conid}; "
                f"refusing to insert an incompletely guarded PENDING row")

        quantity, amount, sizing_meta = self._size(request, quote.price)
        now = self._now()
        draft = ProposalDraft(
            symbol=secdef.symbol, action=request.action,
            quantity=quantity, amount=amount,
            execution=request.execution or {}, reasoning=request.reasoning,
            confidence=float(request.confidence), thesis=request.thesis or '',
            source=source, metadata=sizing_meta,
            sec_type=getattr(secdef, 'secType', '') or 'STK',
            exchange=request.exchange or '', currency=request.currency or '',
            group=request.group or '',
            account_id=self._account_id, account_mode=self._account_mode,
            conid=request.conid,
            reference_price=quote.price,
            reference_timestamp=quote.market_timestamp,
            reference_quote_side=quote.side,
            reference_feed_type=quote.feed_type,
            max_price_drift_bps=request.max_price_drift_bps or self.DEFAULT_DRIFT_BPS,
            expires_at=now + dt.timedelta(minutes=self._ttl_minutes),
            live_approval_eligible=True,
        )

        def _tx(conn):
            if source.startswith('strategy:') and self._repo.pending_duplicate_in_tx(
                    conn, source, request.conid, request.action):
                raise ProposalCreationRefused(
                    "DUPLICATE_PENDING",
                    f"a PENDING {request.action} for conId {request.conid} "
                    f"from {source} already exists")
            record = self._repo.insert_pending_in_tx(conn, draft)
            self._repo.journal_in_tx(conn, record, correlation_id)
            return record

        record = self._db.transaction(_tx)
        self._register_group(request.group, record)          # CLI parity: warn, never fail
        return record
```

Sizing mirrors the CLI path: when neither quantity nor amount is supplied, `PositionSizer(self._sizing_config).compute(confidence=..., portfolio_state=self._account_state.portfolio_state(), price=quote.price, volatility=...)`; a zero-amount sizing result raises `ProposalCreationRefused("SIZING_BLOCKED", ...)`. `reject_proposal` is one transaction: `reject_in_tx` (CAS `WHERE status = 'PENDING'`; a row already `REJECTED` returns idempotent success without a new revision, any other status raises `ProposalCreationRefused("NOT_PENDING", ...)`) followed by `journal_in_tx` when a mutation happened.

The trader-owned expiry task (spec §5.5 — one periodic task, in-process):

```python
def expire_stale(self, now: dt.datetime) -> list[int]:
    def _sweep(conn):
        rows = self._repo.expire_stale_pending_in_tx(conn, now)
        for row in rows:
            self._repo.journal_in_tx(conn, row, correlation_id=None)
        return [row.id for row in rows]
    return self._db.transaction(_sweep)


async def run_expiry_loop(self, interval_seconds: float = 30.0) -> None:
    self.expire_stale(self._now())          # once at startup, before readiness
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            self.expire_stale(self._now())
        except Exception:
            logging.exception('proposal expiry sweep failed — retrying next tick')
```

`trader_service.py` creates the service after the migration runs and schedules `run_expiry_loop()` on the main loop. After every transaction that changes the set of `PENDING` proposals (create, reject, expiry, and — in Task 5 — approval), the service recomputes the pending-proposal conid set and calls `[M1-F2]`'s `QuoteSubscriptionManager.set_owner_refs("proposal", conids)`, so a pending proposal keeps its instrument's quote subscription alive without a separate armed strategy. The strategy-service sweep from `[S0]` Task 2 is deleted in Task 8 when `SignalProposer` stops owning proposals.

- [ ] **Step 5: Run the service tests**

Run: `uv run --frozen pytest tests/test_proposal_command_service.py tests/test_proposal_migration.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add trader/data/proposal_repository.py trader/trading/proposal_command_service.py trader/trader_service.py tests/test_proposal_command_service.py
git commit -m "feat(m1-f3): own proposal create reject expiry in trader service"
```

### Task 3: Command ledger, audit, and TradingCommandCoordinator boundary

**Files:**
- Create: `trader/trading/command_coordinator.py`
- Modify: `trader/data/proposal_repository.py` (nothing structural — export `ProposalRecord` for receipts)
- Modify: `trader/messaging/production_api.py`
- Create: `tests/test_command_coordinator.py`

**Interfaces:**
- Consumes: `TypedRpcRegistry.register(socket_role, method, request_model, response_model, handler)` and `canonical_json` from `[G0]`; `CommandReceipt` from the interface freeze; `DuckDBConnection.transaction` and `DomainJournal.mutate` from `[M1-F1]`.
- Produces: trader-DB migration version 21 (`m1f3_command_ledger_audit`) creating `command_ledger` and `command_audit`.
- Produces: `RiskDirection(str, Enum)` with `INCREASING` and `REDUCING` — always coordinator-computed, never caller-supplied.
- Produces: `CommandRequest(command_id, action, account_id, target_type, target_id, expected_version, body, source, preflight_nonce)` frozen dataclass and `canonical_request_hash(request) -> str`.
- Produces: `CommandLedger` with `claim_or_replay_in_tx(conn, request) -> LedgerClaim`, `transition_in_tx(conn, command_id, from_state, to_state, outcome=None, error_code=None)`, `get(command_id) -> LedgerRow | None`, `unresolved_for_target(target_type, target_id) -> list[LedgerRow]`, `purge_expired(now) -> int`; `LedgerClaim(kind: Literal["new","replay","conflict"], receipt: CommandReceipt | None)`.
- Produces: `TradingCommandCoordinator.execute(request: CommandRequest) -> CommandReceipt`, per-action handler registration `coordinator.register_action(action, handler, requires_preflight: bool)`, and `PreflightNonceGate` protocol with `consume_in_tx(conn, nonce, request: CommandRequest) -> bool` (issuing is `[M1-C]`; production wiring passes `[M1-C]`'s implementation, tests use fakes).
- Produces typed methods on `build_production_registry`: command `create_proposal`, `reject_proposal`; query `get_command`, `get_proposal`, `list_proposals`, all `extra="forbid"`.

- [ ] **Step 1: Write failing ledger and coordinator tests**

```python
def _request(command_id="cmd-1", action="reject_proposal", body=None, nonce=None):
    return CommandRequest(
        command_id=command_id, action=action, account_id="DU111111",
        target_type="proposal", target_id="7",
        expected_version=None, body=body or {"proposal_id": 7, "reason": "x"},
        source="dashboard", preflight_nonce=nonce)


def test_exact_retry_returns_recorded_state_before_nonce_validation(coordinator):
    coordinator.register_action("noop", lambda cmd: {"ok": True}, requires_preflight=True)
    request = _request(action="noop", nonce="nonce-1")
    first = coordinator.execute(request)
    assert first.state == "RESOLVED"
    # The nonce is consumed. An exact retry must return the recorded outcome
    # WITHOUT re-validating the nonce (spec §9.1).
    coordinator._nonces.fail_all()
    retry = coordinator.execute(request)
    assert retry.state == "RESOLVED" and retry.outcome == first.outcome


def test_same_command_id_with_different_payload_is_a_conflict(coordinator):
    coordinator.register_action("noop", lambda cmd: {"ok": True}, requires_preflight=False)
    coordinator.execute(_request(action="noop", body={"proposal_id": 7, "reason": "x"}))
    conflict = coordinator.execute(_request(action="noop", body={"proposal_id": 8, "reason": "x"}))
    assert conflict.error_code == "COMMAND_CONFLICT" and conflict.retryable is False


def test_ledger_row_exists_before_validation_runs(coordinator, ledger):
    seen = {}

    def handler(cmd):
        seen["row"] = ledger.get(cmd.command_id)
        raise CommandValidationError("RISK_REJECTED", "concentration too high")

    coordinator.register_action("noop", handler, requires_preflight=False)
    receipt = coordinator.execute(_request(action="noop"))
    assert seen["row"].state == "RECEIVED"        # insert precedes validation
    assert receipt.state == "REJECTED" and receipt.error_code == "RISK_REJECTED"


def test_audit_write_failure_fails_closed(coordinator):
    dispatched = []
    coordinator.register_action("noop", lambda cmd: dispatched.append(cmd) or {}, requires_preflight=False)
    coordinator._audit.fail_next()
    receipt = coordinator.execute(_request(action="noop"))
    assert receipt.error_code == "AUDIT_UNAVAILABLE" and receipt.state == "REJECTED"
    assert dispatched == []                        # nothing reached the handler


def test_command_transitions_journal_command_updated(coordinator, journal):
    coordinator.register_action("noop", lambda cmd: {"ok": True}, requires_preflight=False)
    coordinator.execute(_request(action="noop"))
    kinds = [e.event_type for e in journal.read_after(0, 100)]
    assert kinds == ["command.updated", "command.updated"]   # RECEIVED, RESOLVED


def test_no_risk_limit_admin_or_bypass_on_the_command_socket(production_registry):
    for method in ("set_risk_limits", "place_order_simple", "place_expressive_order"):
        assert not production_registry.contains("command", method)
    with pytest.raises(ValidationError):
        RejectProposalRequest(command_id="c", proposal_id=7, reason="x", skip_risk_gate=True)


def test_retention_purges_terminal_but_never_unknown(ledger, now):
    ledger.insert_for_test("old-resolved", state="RESOLVED", updated_at=now - dt.timedelta(days=31))
    ledger.insert_for_test("old-unknown", state="OUTCOME_UNKNOWN", updated_at=now - dt.timedelta(days=31))
    assert ledger.purge_expired(now) == 1
    assert ledger.get("old-resolved") is None
    assert ledger.get("old-unknown") is not None
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run --frozen pytest tests/test_command_coordinator.py -q`

Expected: FAIL because the ledger, coordinator, and request models do not exist.

- [ ] **Step 3: Create the ledger and audit schema (migration 21)**

```sql
CREATE TABLE IF NOT EXISTS command_ledger (
    command_id VARCHAR PRIMARY KEY,
    request_hash VARCHAR NOT NULL,
    account_id VARCHAR,
    action VARCHAR NOT NULL,
    target_type VARCHAR NOT NULL,
    target_id VARCHAR NOT NULL,
    expected_version BIGINT,
    state VARCHAR NOT NULL CHECK (state IN (
        'RECEIVED', 'VALIDATED', 'SUBMITTING', 'SUBMITTED',
        'REJECTED', 'OUTCOME_UNKNOWN', 'RESOLVED')),
    outcome JSON,
    error_code VARCHAR,
    source VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS command_audit (
    audit_id BIGINT PRIMARY KEY DEFAULT nextval('command_audit_seq'),
    command_id VARCHAR NOT NULL,
    correlation_id VARCHAR NOT NULL,
    action VARCHAR NOT NULL,
    target_type VARCHAR NOT NULL,
    target_id VARCHAR NOT NULL,
    expected_version BIGINT,
    redacted_inputs JSON NOT NULL,
    validation_result VARCHAR,
    acknowledgement VARCHAR,
    outcome VARCHAR,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE SEQUENCE IF NOT EXISTS command_audit_seq START 1;
```

(The sequence statement is ordered before the table in the actual migration list.)

- [ ] **Step 4: Implement hash, ledger, and coordinator**

```python
def canonical_request_hash(request: CommandRequest) -> str:
    return hashlib.sha256(canonical_json({
        "action": request.action,
        "account_id": request.account_id,
        "target_type": request.target_type,
        "target_id": request.target_id,
        "expected_version": request.expected_version,
        "body": request.body,
    })).hexdigest()
```

`CommandLedger.claim_or_replay_in_tx` selects the row by `command_id`. Missing → `LedgerClaim("new", None)`. Present with equal `request_hash` → `LedgerClaim("replay", receipt_from_row)`. Present with a different hash → `LedgerClaim("conflict", conflict_receipt)`. `transition_in_tx` is a guarded CAS (`WHERE command_id = ? AND state = ?`) and raises `IllegalCommandTransition` on a miss.

The coordinator entry point:

```python
def execute(self, request: CommandRequest) -> CommandReceipt:
    handler, requires_preflight = self._actions[request.action]

    def _claim(conn):
        claim = self._ledger.claim_or_replay_in_tx(conn, request)   # BEFORE nonce validation
        if claim.kind != "new":
            return claim
        if requires_preflight and not self._nonces.consume_in_tx(conn, request.preflight_nonce, request):
            raise CommandValidationError("PREFLIGHT_REQUIRED",
                                         "missing, expired, or already-consumed preflight nonce")
        self._ledger.insert_received_in_tx(conn, request)           # claim + nonce: one transaction
        self._audit.record_in_tx(conn, request)                     # raising here rolls back everything
        self._journal_command_in_tx(conn, request, "RECEIVED")
        return claim

    try:
        claim = self._db.transaction(_claim)
    except CommandValidationError as ex:
        return self._reject_unclaimed(request, ex)     # nonce failure: no ledger row was kept
    except Exception as ex:
        return CommandReceipt(request.command_id, request.command_id, "REJECTED",
                              None, "AUDIT_UNAVAILABLE", False)     # fail closed
    if claim.kind != "new":
        return claim.receipt

    try:
        outcome = handler(request)                     # validation + side effects per action
    except CommandValidationError as ex:
        self._transition(request, "RECEIVED", "REJECTED", error_code=ex.code)
        return self._receipt(request.command_id)
    self._transition(request, self._ledger.get(request.command_id).state, "RESOLVED", outcome=outcome)
    return self._receipt(request.command_id)
```

Handlers that own multi-step sagas (approval in Task 5, cancel in Task 6, strategy forwarding in Task 7) drive their own `VALIDATED → SUBMITTING → SUBMITTED/OUTCOME_UNKNOWN → RESOLVED` transitions and return the final receipt themselves; the skeleton's terminal fallback covers single-step actions (`create_proposal`, `reject_proposal`, `set_trading_pause`). Every transition writes `command.updated` through `DomainJournal.mutate` in the same transaction, entity ID `command_id`, entity revision = transition ordinal. There is no `skip_risk_gate` parameter anywhere in `CommandRequest`, the request models, or the handler signatures; `RiskDirection` is computed inside handlers from broker-verified position state. Risk-limit administration is deliberately not an action.

Wire the typed surface in `trader/messaging/production_api.py`:

```python
def register_command_authority(registry, coordinator, proposal_service, repository):
    registry.register("command", "create_proposal", CreateProposalRequest,
                      CommandReceipt, coordinator.handle_create_proposal)
    registry.register("command", "reject_proposal", RejectProposalRequest,
                      CommandReceipt, coordinator.handle_reject_proposal)
    registry.register("query", "get_command", GetCommandRequest,
                      CommandReceipt, coordinator.handle_get_command)
    registry.register("query", "get_proposal", GetProposalRequest,
                      ProposalView, coordinator.handle_get_proposal)
    registry.register("query", "list_proposals", ListProposalsRequest,
                      ProposalListView, coordinator.handle_list_proposals)
```

All request models are Pydantic with `model_config = ConfigDict(extra="forbid")`. `handle_create_proposal` and `handle_reject_proposal` build a `CommandRequest` (`target_type="proposal"`, source from the authenticated service credential, account derived server-side) and call `execute`; the registered inner actions call `ProposalCommandService.create_proposal` / `reject_proposal` and translate `ProposalCreationRefused` into `CommandValidationError`.

- [ ] **Step 5: Run coordinator and production-registry tests**

Run: `uv run --frozen pytest tests/test_command_coordinator.py tests/test_production_rpc_security.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add trader/trading/command_coordinator.py trader/data/proposal_repository.py trader/messaging/production_api.py tests/test_command_coordinator.py
git commit -m "feat(m1-f3): add command ledger and coordinator boundary"
```

### Task 4: Durable per-account pause gate

**Files:**
- Create: `trader/trading/trading_control.py`
- Modify: `trader/trading/proposal_command_service.py` (replace the port fake with the real gate)
- Modify: `trader/trading/command_coordinator.py` (register `set_trading_pause`)
- Modify: `trader/messaging/production_api.py`
- Modify: `trader/trader_service.py` (seed before readiness)
- Create: `tests/test_trading_control.py`

**Interfaces:**
- Consumes: `DomainJournal.mutate`, `DuckDBConnection.transaction` from `[M1-F1]`; `CommandLedger`/`TradingCommandCoordinator` from Task 3.
- Produces: trader-DB migration version 22 (`m1f3_trading_control_state`) creating `trading_control_state` exactly as spec §9.4.
- Produces: `TradingControlState(account_id, new_exposure_paused, revision, updated_at, updated_by_command_id, updated_reason)` frozen dataclass.
- Produces: `TradingControlStore` with `seed_in_tx(conn, accounts: Sequence[tuple[str, str]], now) -> list[TradingControlState]`, `get(account_id) -> TradingControlState` (raises `PauseStateUnavailable` on a missing row), `require_unpaused(account_id)` / `require_unpaused_in_tx(conn, account_id)` (raise `TradingPausedError` or `PauseStateUnavailable` — fail closed), `set_pause_in_tx(conn, account_id, paused: bool, expected_revision: int | None, command_id: str, reason: str, now) -> TradingControlState`.
- Produces typed methods: command `set_trading_pause`, query `get_trading_control`.

- [ ] **Step 1: Write failing pause-gate tests**

```python
def test_table_shape_matches_spec(control_db):
    cols = control_db.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'trading_control_state' ORDER BY ordinal_position",
        fetch="all")
    assert [c[0] for c in cols] == [
        "account_id", "new_exposure_paused", "revision", "updated_at",
        "updated_by_command_id", "updated_reason"]
    assert all(c[1] == "NO" for c in cols[1:])     # every non-key column NOT NULL


def test_bootstrap_seeds_live_paused_paper_unpaused_idempotently(controls, db):
    def _seed(conn):
        return controls.seed_in_tx(conn, [("U1234567", "live"), ("DU111111", "paper")], NOW)
    db.transaction(_seed)
    db.transaction(_seed)                                    # idempotent re-run
    live, paper = controls.get("U1234567"), controls.get("DU111111")
    assert live.new_exposure_paused is True and paper.new_exposure_paused is False
    assert live.revision == 1 and paper.revision == 1
    assert live.updated_by_command_id == "system:bootstrap"
    assert live.updated_reason == "account initialization"


def test_pause_is_absolute_and_idempotent_from_a_stale_view(controls, db):
    state = _set(controls, db, paused=True, expected_revision=None, command_id="cmd-1")
    again = _set(controls, db, paused=True, expected_revision=None, command_id="cmd-2")
    assert state.new_exposure_paused is True and again.new_exposure_paused is True
    assert again.revision == state.revision      # no-op repeat mints no revision


def test_resume_requires_the_exact_current_revision(controls, db):
    paused = _set(controls, db, paused=True, expected_revision=None, command_id="cmd-1")
    with pytest.raises(PauseRevisionConflict):
        _set(controls, db, paused=False, expected_revision=paused.revision - 1, command_id="cmd-2")
    resumed = _set(controls, db, paused=False, expected_revision=paused.revision, command_id="cmd-3")
    assert resumed.new_exposure_paused is False and resumed.revision == paused.revision + 1


def test_missing_row_fails_closed_for_exposure_increasing_actions(controls):
    with pytest.raises(PauseStateUnavailable):
        controls.require_unpaused("U_NEVER_SEEDED")


def test_pause_commit_serializes_against_final_dispatch(authority_with_controls):
    """A pause that commits first must reject a later exposure-increasing
    dispatch: the SUBMITTING claim transaction re-checks the gate row."""
    a = authority_with_controls
    _set(a.controls, a.db, paused=True, expected_revision=None, command_id="pause-1")
    with pytest.raises(TradingPausedError):
        a.db.transaction(lambda conn: a.controls.require_unpaused_in_tx(conn, "DU111111"))


def test_paused_account_blocks_exposure_increasing_creation_only(authority_with_controls):
    a = authority_with_controls
    _set(a.controls, a.db, paused=True, expected_revision=None, command_id="pause-1")
    with pytest.raises(ProposalCreationRefused) as exc:
        a.service.create_proposal(_create_request(conid=265598, action="BUY"),
                                  source="dashboard", correlation_id="c1")
    assert exc.value.code == "TRADING_PAUSED"
    a.positions.set_held(265598, 100.0)          # verified reducible position
    close = a.service.create_proposal(
        _create_request(conid=265598, action="SELL", quantity=100.0),
        source="dashboard", correlation_id="c2")
    assert close.status == "PENDING"             # reducing close allowed while paused
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run --frozen pytest tests/test_trading_control.py -q`

Expected: FAIL because `trading_control.py` and migration 22 do not exist.

- [ ] **Step 3: Implement the store (migration 22 + absolute set)**

```sql
CREATE TABLE IF NOT EXISTS trading_control_state (
    account_id VARCHAR PRIMARY KEY,
    new_exposure_paused BOOLEAN NOT NULL,
    revision BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    updated_by_command_id VARCHAR NOT NULL,
    updated_reason VARCHAR NOT NULL
)
```

```python
def set_pause_in_tx(self, conn, account_id, paused, expected_revision,
                    command_id, reason, now) -> TradingControlState:
    current = self._get_in_tx(conn, account_id)
    if current is None:
        raise PauseStateUnavailable(account_id)          # fail closed, never auto-seed here
    if current.new_exposure_paused == paused:
        return current                                   # absolute set: idempotent no-op
    if not paused:                                       # resume is risk-increasing
        if expected_revision != current.revision:
            raise PauseRevisionConflict(account_id, current.revision)
    rows = conn.execute(
        """
        UPDATE trading_control_state
           SET new_exposure_paused = ?, revision = revision + 1,
               updated_at = ?, updated_by_command_id = ?, updated_reason = ?
         WHERE account_id = ? AND revision = ?
     RETURNING account_id, new_exposure_paused, revision, updated_at,
               updated_by_command_id, updated_reason
        """,
        [paused, now, command_id, reason, account_id, current.revision],
    ).fetchall()
    if not rows:
        raise PauseRevisionConflict(account_id, current.revision)   # concurrent writer won
    state = TradingControlState(*rows[0])
    self._journal_in_tx(conn, state, correlation_id=command_id)     # trading_control.updated
    return state
```

Pause (`paused=True`) passes `expected_revision=None` and always succeeds against the current row — immediate and idempotent even from a stale view. `seed_in_tx` inserts only missing accounts at revision 1 (`system:bootstrap`, `account initialization`, live→paused / paper→unpaused) and journals one `trading_control.updated` per inserted row; `trader_service` runs it inside one transaction before reporting ready. `require_unpaused_in_tx` re-reads the row in the caller's transaction — the approval saga (Task 5) calls it inside the same transaction that claims `SUBMITTING`, which serializes pause mutation against final order dispatch. `ProposalCommandService` replaces its Task 2 port with the real store; the exposure-increasing creation path calls `require_unpaused`, the position-reducing close path (SELL with quantity ≤ verified held) does not.

Register the command and query:

```python
registry.register("command", "set_trading_pause", SetTradingPauseRequest,
                  CommandReceipt, coordinator.handle_set_trading_pause)
registry.register("query", "get_trading_control", GetTradingControlRequest,
                  TradingControlView, coordinator.handle_get_trading_control)
```

`SetTradingPauseRequest(command_id, paused: bool, expected_version: int | None, reason: str)` — the account is always the coordinator's configured account, never request-supplied. The coordinator action runs `set_pause_in_tx` and its `command.updated` transition in one transaction. Resume (`paused=False`) is registered with `requires_preflight=True`; `[M1-C]` supplies the live nonce ceremony, and the paper path passes the Task 3 fake-free single-POST rule because the paper `PreflightNonceGate` accepts the documented `paper:<command_id>` self-nonce.

- [ ] **Step 4: Run pause and coordinator tests**

Run: `uv run --frozen pytest tests/test_trading_control.py tests/test_command_coordinator.py tests/test_proposal_command_service.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/trading_control.py trader/trading/proposal_command_service.py trader/trading/command_coordinator.py trader/messaging/production_api.py trader/trader_service.py tests/test_trading_control.py
git commit -m "feat(m1-f3): enforce durable per-account pause gate"
```

### Task 5: Approval command — guards, saga, and orderRef binding

**Files:**
- Modify: `trader/trading/command_coordinator.py`
- Create: `tests/test_approval_command.py`
- Modify: `trader/messaging/production_api.py`
- Modify: `trader/trading/trading_runtime.py` (implement `OrderDispatchPort` over `place_expressive_order` internals)

**Interfaces:**
- Consumes: `ProposalRepository.claim_for_approval_in_tx` (Task 2), `TradingControlStore.require_unpaused_in_tx` (Task 4), `CommandLedger` (Task 3), `RiskGate.evaluate`; from `[M1-F2]`: `encode_order_ref(order_group_id: str) -> str` (`trader/trading/order_correlation.py`, `mmr:` prefix — the only permitted `orderRef` encoding), `RiskProducer.publish_decision(command_id, payload, correlation_id=None)` (write-once per command), and broker order rows from the `broker_orders` store.
- Produces: `OrderDispatchPort` protocol — `submit(proposal: ProposalRecord, order_ref: str, order_group_id: str) -> SubmittedOrders`, `cancel(order_entity_id: str, order_ref: str) -> CancelAck`, `find_by_order_ref(account_id: str, order_ref: str) -> list[BrokerOrderRow]`, `enumeration_complete() -> bool` (fenced broker generation). Implemented by `trading_runtime`; `[M1-F2]` provides `BrokerOrderRow`.
- Produces: `PositionAuthority.reducible_quantity(account_id: str, conid: int) -> float` and `BrokerHealthPort.is_ready() -> bool` protocols.
- Produces: pure guard function `check_exposure_increasing_guards(record, quote, now, account_mode, outside_session_limit_enabled=False) -> CommandProblem | None` and `classify_risk_direction(action: str, held_quantity: float, order_quantity: float) -> RiskDirection`.
- Produces typed command `approve_proposal` with `ApproveProposalRequest(command_id, proposal_id: int, expected_version: int, preflight_nonce: str | None)`.

- [ ] **Step 1: Write failing approval tests**

```python
def test_happy_path_claims_dispatches_and_binds_order_ref(approval):
    record = approval.pending(conid=265598, action="BUY", reference_price=210.0)
    receipt = approval.execute_approve(record, command_id="cmd-1")
    assert receipt.state == "SUBMITTED"
    submitted = approval.orders.submissions[0]
    assert submitted.order_group_id == "og-cmd-1"
    assert submitted.order_ref == encode_order_ref("og-cmd-1")   # [M1-F2] helper: "mmr:og-cmd-1"
    stored = approval.repo.get(record.id)
    assert stored.status == "EXECUTED"                    # storage keeps legacy name ([S0] display maps it)
    assert stored.order_group_id == "og-cmd-1"
    assert stored.revision == record.revision + 2         # claim + submit-link


def test_expired_row_flips_to_expired_inside_the_claiming_transaction(approval):
    record = approval.pending(conid=265598, action="BUY",
                              expires_at=approval.now() - dt.timedelta(seconds=1))
    receipt = approval.execute_approve(record, command_id="cmd-1")
    assert receipt.state == "REJECTED" and receipt.error_code == "PROPOSAL_EXPIRED"
    assert approval.repo.get(record.id).status == "EXPIRED"
    assert approval.orders.submissions == []


def test_revision_mismatch_rejects_without_dispatch(approval):
    record = approval.pending(conid=265598, action="BUY")
    receipt = approval.execute_approve(record, command_id="cmd-1",
                                       expected_version=record.revision + 5)
    assert receipt.error_code == "REVISION_MISMATCH"
    assert approval.orders.submissions == []


def test_drift_beyond_recorded_guard_rejects(approval):
    record = approval.pending(conid=265598, action="BUY",
                              reference_price=210.0, max_price_drift_bps=50.0)
    approval.quotes.set(265598, ask=212.0)                # ~95 bps drift
    receipt = approval.execute_approve(record, command_id="cmd-1")
    assert receipt.error_code == "PRICE_DRIFT_EXCEEDED"


def test_live_mode_requires_fresh_live_executable_side_quote(approval_live):
    record = approval_live.pending(conid=265598, action="BUY")
    approval_live.quotes.set(265598, ask=210.0, feed_type="delayed")
    assert approval_live.execute_approve(record, "c1").error_code == "FEED_NOT_LIVE"
    approval_live.quotes.set(265598, ask=210.0, feed_type="live",
                             age_seconds=6.0)
    assert approval_live.execute_approve(record, "c2").error_code == "QUOTE_STALE"
    approval_live.quotes.set(265598, ask=210.0, feed_type="live",
                             session_state="closed")
    assert approval_live.execute_approve(record, "c3").error_code == "SESSION_INCOMPATIBLE"


def test_live_ineligible_row_cannot_be_approved_live(approval_live):
    record = approval_live.pending(conid=265598, action="BUY", live_approval_eligible=False)
    assert approval_live.execute_approve(record, "c1").error_code == "LIVE_INELIGIBLE"


def test_position_reducing_exit_is_exempt_but_quantity_capped(approval):
    approval.positions.set_held(265598, 100.0)
    approval.quotes.set(265598, bid=205.0, feed_type="delayed", age_seconds=600.0)
    close = approval.pending(conid=265598, action="SELL", quantity=100.0)
    assert approval.execute_approve(close, "c1").state == "SUBMITTED"   # stale feed tolerated
    oversized = approval.pending(conid=265598, action="SELL", quantity=150.0)
    receipt = approval.execute_approve(oversized, "c2")
    assert receipt.error_code == "REDUCIBLE_QUANTITY_EXCEEDED"


def test_broker_unhealthy_rejects_retryably(approval):
    approval.broker.ready = False
    record = approval.pending(conid=265598, action="BUY")
    receipt = approval.execute_approve(record, "c1")
    assert receipt.error_code == "BROKER_UNAVAILABLE" and receipt.retryable is True


def test_dispatch_timeout_becomes_outcome_unknown_never_failed(approval):
    record = approval.pending(conid=265598, action="BUY")
    approval.orders.raise_on_submit(TimeoutError("ib ack timeout"))
    receipt = approval.execute_approve(record, "cmd-1")
    assert receipt.state == "OUTCOME_UNKNOWN"
    assert approval.repo.get(record.id).status == "APPROVED"    # ambiguous: not FAILED
    assert approval.reconciler.scheduled == ["cmd-1"]
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run --frozen pytest tests/test_approval_command.py -q`

Expected: FAIL because the approval action, guards, and dispatch port do not exist.

- [ ] **Step 3: Implement the §9.2 validation list and pure guards**

```python
def classify_risk_direction(action: str, held_quantity: float, order_quantity: float) -> RiskDirection:
    if action == "SELL" and held_quantity > 0 and order_quantity <= held_quantity:
        return RiskDirection.REDUCING
    if action == "BUY" and held_quantity < 0 and order_quantity <= -held_quantity:
        return RiskDirection.REDUCING           # covering a short reduces risk
    return RiskDirection.INCREASING


def check_exposure_increasing_guards(record, quote, now, account_mode,
                                     outside_session_limit_enabled=False):
    expected_side = "ask" if record.action == "BUY" else "bid"
    if quote is None or quote.side != expected_side or not quote.price or quote.price <= 0:
        return CommandProblem("EXECUTABLE_QUOTE_MISSING", retryable=True)
    if account_mode == "live":
        if quote.feed_type != "live":
            return CommandProblem("FEED_NOT_LIVE", retryable=True)
        if quote.session_state != "continuous":
            order_type = (record.execution or {}).get("order_type", "MARKET")
            if order_type == "MARKET" or not outside_session_limit_enabled:
                return CommandProblem("SESSION_INCOMPATIBLE", retryable=True)
        age = (now - quote.market_timestamp).total_seconds()
        if age > 5.0:
            return CommandProblem("QUOTE_STALE", retryable=True)
        if age < -MAX_SOURCE_CLOCK_SKEW_SECONDS:
            return CommandProblem("SOURCE_CLOCK_SKEW", retryable=True)
    drift_bps = abs(quote.price - record.reference_price) / record.reference_price * 10_000.0
    if drift_bps > record.max_price_drift_bps:
        return CommandProblem("PRICE_DRIFT_EXCEEDED", retryable=False,
                              detail=f"{drift_bps:.1f} bps > {record.max_price_drift_bps:.1f} bps guard")
    return None
```

The approval action validates in order (spec §9.2): row exists and is `PENDING` with `expected_version`; account/mode pinning (`WRONG_ACCOUNT`, `LIVE_INELIGIBLE`); no unresolved command for the same proposal (`COMMAND_IN_FLIGHT`, §9.5); broker and trader health; `RiskGate.evaluate` (`RISK_REJECTED`); then the direction split — `classify_risk_direction` against `PositionAuthority.reducible_quantity`, exposure-increasing rows through `check_exposure_increasing_guards`, position-reducing rows through the reducible-quantity cap only (staleness is reported in the receipt outcome, not a rejection). Whatever the outcome, the risk decision is recorded exactly once via `[M1-F2]`'s write-once `RiskProducer.publish_decision(cmd.command_id, decision_payload, correlation_id=cmd.command_id)` — the `decision:<command_id>` risk entity — never re-published on retries (the ledger replay path returns before validation re-runs).

- [ ] **Step 4: Implement the order saga**

```python
def _approve(self, cmd: CommandRequest) -> CommandReceipt:
    record = self._repo.get(int(cmd.body["proposal_id"]))
    problem = self._validate_approval(record, cmd)
    if problem is not None:
        self._transition(cmd, "RECEIVED", "REJECTED", error_code=problem.code)
        return self._receipt(cmd.command_id)
    self._transition(cmd, "RECEIVED", "VALIDATED")

    order_group_id = f"og-{cmd.command_id}"
    direction = self._direction_for(record)

    def _claim(conn):
        # §9.4: the SUBMITTING claim re-checks the pause row in the SAME
        # transaction, serializing pause mutation against final dispatch.
        if direction is RiskDirection.INCREASING:
            self._controls.require_unpaused_in_tx(conn, record.account_id)
        outcome = self._repo.claim_for_approval_in_tx(
            conn, record.id, cmd.expected_version, self._account_id, self._now())
        if outcome.result is not ApprovalClaim.CLAIMED:
            raise ApprovalClaimFailed(outcome)          # rolls back the transition below
        self._repo.link_order_group_in_tx(conn, record.id, order_group_id)
        self._repo.journal_in_tx(conn, outcome.record, cmd.command_id)
        self._ledger.transition_in_tx(conn, cmd.command_id, "VALIDATED", "SUBMITTING")
        self._journal_command_in_tx(conn, cmd, "SUBMITTING")

    try:
        self._db.transaction(_claim)
    except TradingPausedError:
        self._transition(cmd, "VALIDATED", "REJECTED", error_code="TRADING_PAUSED")
        return self._receipt(cmd.command_id)
    except ApprovalClaimFailed as ex:
        self._transition(cmd, "VALIDATED", "REJECTED", error_code=ex.code())  # PROPOSAL_EXPIRED etc.
        return self._receipt(cmd.command_id)

    try:
        submitted = self._orders.submit(
            proposal=self._repo.get(record.id),
            # §9.1 via [M1-F2]'s order_correlation helper: orderRef "mmr:og-<command_id>"
            order_ref=encode_order_ref(order_group_id),
            order_group_id=order_group_id)
    except Exception as ex:                            # timeout, disconnect, crash-adjacent
        self._transition(cmd, "SUBMITTING", "OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS")
        self._reconciler.schedule(cmd.command_id, self._now())
        return self._receipt(cmd.command_id)

    def _finish(conn):
        row = self._repo.mark_order_submitted_in_tx(conn, record.id, submitted.order_ids)
        self._repo.journal_in_tx(conn, row, cmd.command_id)
        self._ledger.transition_in_tx(
            conn, cmd.command_id, "SUBMITTING", "SUBMITTED",
            outcome={"order_ids": submitted.order_ids, "order_group_id": order_group_id})
        self._journal_command_in_tx(conn, cmd, "SUBMITTED")

    self._db.transaction(_finish)
    return self._receipt(cmd.command_id)
```

An explicit broker rejection inside `submit` (a clean `SuccessFail.fail` before any live order) raises `BrokerRejectedError`; the handler catches it separately, marks the proposal `FAILED` (`mark_failed_in_tx` + journal) and the command `REJECTED`. Only ambiguity — timeout or lost acknowledgement — produces `OUTCOME_UNKNOWN`, and the proposal stays `APPROVED` for the Task 9 reconciler. `trading_runtime` implements `OrderDispatchPort.submit` by reusing the existing `place_expressive_order` bracket transactionality with the `encode_order_ref(order_group_id)` value stamped on every leg's `orderRef` — the same value `[M1-F2]`'s order tracker uses to correlate `broker_orders` rows back to the group. Register `approve_proposal` with `requires_preflight=True`; the paper self-nonce rule from Task 4 applies, and `[M1-C]` supplies live nonce issuing.

- [ ] **Step 5: Run approval, coordinator, and pause tests**

Run: `uv run --frozen pytest tests/test_approval_command.py tests/test_command_coordinator.py tests/test_trading_control.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add trader/trading/command_coordinator.py trader/trading/trading_runtime.py trader/messaging/production_api.py tests/test_approval_command.py
git commit -m "feat(m1-f3): approve proposals through the guarded saga"
```

### Task 6: Working-order cancel authority

**Files:**
- Modify: `trader/trading/command_coordinator.py`
- Modify: `trader/messaging/production_api.py`
- Create: `tests/test_cancel_command.py`

**Interfaces:**
- Consumes: `[M1-F2]`'s `broker_orders` materialized store via a read protocol `OrderStateView.get_order(order_entity_id: str) -> BrokerOrderRow | None`, where `BrokerOrderRow` carries at minimum `order_entity_id`, `account_id`, `order_group_id`, `leg_role` (`entry` / `take_profit` / `stop` / `trailing_stop` / `unknown`), `status`, `is_terminal`, `conid`, `quantity`; `OrderDispatchPort.cancel` from Task 5.
- Produces: `classify_cancel(order: BrokerOrderRow | None) -> RiskDirection` — entry legs are `REDUCING`; protective legs and anything unclassifiable are `INCREASING`.
- Produces typed commands `cancel_order` (`CancelOrderRequest(command_id, order_entity_id: str, preflight_nonce: str | None)`) and `cancel_orders` (`CancelOrdersRequest(command_id, order_entity_ids: list[str], preflight_nonce: str | None)`).

- [ ] **Step 1: Write failing classification and no-op tests**

```python
def test_entry_cancel_is_risk_reducing_and_immediate(cancel):
    cancel.orders_view.add(_order("ord-1", leg_role="entry", status="Submitted"))
    receipt = cancel.execute("cancel_order", {"order_entity_id": "ord-1"}, command_id="c1")
    assert receipt.state == "SUBMITTED"
    assert cancel.dispatch.cancelled == [("ord-1", "c1")]      # dispatch correlation carries the command_id
    assert classify_cancel(cancel.orders_view.get_order("ord-1")) is RiskDirection.REDUCING


def test_protective_leg_cancel_is_risk_increasing_and_names_the_position(cancel_live):
    cancel_live.orders_view.add(_order("ord-2", leg_role="stop", status="Submitted", conid=265598))
    without_nonce = cancel_live.execute(
        "cancel_order", {"order_entity_id": "ord-2"}, command_id="c1", nonce=None)
    assert without_nonce.error_code == "PREFLIGHT_REQUIRED"     # §9.1 ceremony for live
    with_nonce = cancel_live.execute(
        "cancel_order", {"order_entity_id": "ord-2"}, command_id="c1b", nonce="n-1")
    assert with_nonce.state == "SUBMITTED"
    assert with_nonce.outcome["unprotected_conid"] == 265598


def test_unclassifiable_order_is_treated_as_protective(cancel_live):
    cancel_live.orders_view.add(_order("ord-3", leg_role="unknown", status="Submitted"))
    receipt = cancel_live.execute(
        "cancel_order", {"order_entity_id": "ord-3"}, command_id="c1", nonce=None)
    assert receipt.error_code == "PREFLIGHT_REQUIRED"
    assert classify_cancel(cancel_live.orders_view.get_order("ord-3")) is RiskDirection.INCREASING


def test_cancelling_a_terminal_order_is_a_noop_reporting_state(cancel):
    cancel.orders_view.add(_order("ord-4", leg_role="entry", status="Filled", is_terminal=True))
    receipt = cancel.execute("cancel_order", {"order_entity_id": "ord-4"}, command_id="c1")
    assert receipt.state == "RESOLVED"
    assert receipt.outcome == {"noop": True, "authoritative_status": "Filled"}
    assert cancel.dispatch.cancelled == []


def test_cancel_all_expands_under_one_correlation_id(cancel):
    cancel.orders_view.add(_order("ord-5", leg_role="entry", status="Submitted"))
    cancel.orders_view.add(_order("ord-6", leg_role="entry", status="Submitted"))
    receipt = cancel.execute(
        "cancel_orders", {"order_entity_ids": ["ord-5", "ord-6"]}, command_id="root-1")
    assert receipt.correlation_id == "root-1"
    children = [cancel.ledger.get(c) for c in receipt.outcome["child_command_ids"]]
    assert {c.state for c in children} == {"SUBMITTED"}
    assert all(cancel.journal_correlations(c.command_id) == {"root-1"} for c in children)


def test_cancel_timeout_reconciles_like_other_order_commands(cancel):
    cancel.orders_view.add(_order("ord-7", leg_role="entry", status="Submitted"))
    cancel.dispatch.raise_on_cancel(TimeoutError("ack lost"))
    receipt = cancel.execute("cancel_order", {"order_entity_id": "ord-7"}, command_id="c1")
    assert receipt.state == "OUTCOME_UNKNOWN"
    assert cancel.reconciler.scheduled == ["c1"]
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run --frozen pytest tests/test_cancel_command.py -q`

Expected: FAIL because `classify_cancel` and the cancel actions do not exist.

- [ ] **Step 3: Implement classification and the cancel actions**

```python
_PROTECTIVE_ROLES = {"take_profit", "stop", "trailing_stop"}


def classify_cancel(order) -> RiskDirection:
    """§9.7: cancelling an entry removes pending exposure (risk-reducing);
    cancelling a protective leg strips protection from an open position
    (risk-increasing). Anything we cannot classify is treated as protective."""
    if order is None or order.leg_role == "entry":
        return RiskDirection.REDUCING if order is not None else RiskDirection.INCREASING
    if order.leg_role in _PROTECTIVE_ROLES:
        return RiskDirection.INCREASING
    return RiskDirection.INCREASING                     # unclassifiable → protective
```

The `cancel_order` action loads the row from `OrderStateView`. A missing row is unclassifiable (rejected `ORDER_NOT_FOUND` — never blind-cancelled). A terminal row short-circuits to `RESOLVED` with `{"noop": True, "authoritative_status": ...}` — the authoritative state, not an error. Otherwise the preflight requirement is *derived from the classification and account mode* rather than static registration: the coordinator's Task 3 nonce check is deferred for `cancel_order`, and the action calls `self._require_ceremony(cmd, classify_cancel(order))`, which enforces the nonce inside the same claiming transaction only when the direction is `INCREASING` (both modes use a single authenticated POST for `REDUCING`; live `INCREASING` needs the signed nonce, paper uses the paper self-nonce). Dispatch then follows the Task 5 saga (`VALIDATED → SUBMITTING → SUBMITTED`) with `cancel(order_entity_id, order_ref=cmd.command_id)` — the `order_ref` argument is audit correlation only; IB's cancel carries no new `orderRef`, and an ambiguous cancel is reconciled in Task 9 by re-reading the target order's authoritative status from `[M1-F2]`'s `broker_orders` store (whose rows already carry their group's `encode_order_ref` value), never by inventing a second encoding. A protective-leg cancel records `unprotected_conid` in the outcome so the confirmation can name the position left unprotected. Cancel classification also publishes its risk decision once via `RiskProducer.publish_decision(cmd.command_id, ...)`.

`cancel_orders` validates the list, then expands to one child `CommandRequest` per order with `command_id = f"{root}:{order_entity_id}"`, `correlation_id` fixed to the root command ID, executing each through the same single-order action; the root command resolves with `{"child_command_ids": [...], "classifications": {...}}` so one confirmation lists every affected order with its classification.

Register both on the command socket:

```python
registry.register("command", "cancel_order", CancelOrderRequest,
                  CommandReceipt, coordinator.handle_cancel_order)
registry.register("command", "cancel_orders", CancelOrdersRequest,
                  CommandReceipt, coordinator.handle_cancel_orders)
```

- [ ] **Step 4: Run cancel and coordinator tests**

Run: `uv run --frozen pytest tests/test_cancel_command.py tests/test_command_coordinator.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/command_coordinator.py trader/messaging/production_api.py tests/test_cancel_command.py
git commit -m "feat(m1-f3): classify and reconcile working-order cancels"
```

### Task 7: Strategy revisions, receipt ledger, and coordinator forwarding

**Files:**
- Create: `trader/strategy/strategy_revisions.py`
- Modify: `trader/strategy/strategy_runtime.py`
- Modify: `trader/strategy_service.py` (typed sockets 42104/42105, startup recovery)
- Modify: `trader/trading/command_coordinator.py` (forwarding actions)
- Modify: `trader/messaging/production_api.py`
- Modify: `config_defaults/trader.yaml` (`strategy_typed_command_port: 42104`, `strategy_typed_query_port: 42105`)
- Create: `tests/test_strategy_revisions.py`

**Interfaces:**
- Consumes: `TypedRpcRegistry`/`TypedRpcClient`/`HmacServiceAuthenticator` from `[G0]`; `CommandLedger` and the saga from Tasks 3/5.
- Produces: `StrategyCommandReceipt(command_id, strategy_name, action, state: Literal["COMMITTED","ROLLED_BACK"], control_revision, state_revision, error: str | None)` frozen dataclass.
- Produces: `StrategyRevisionStore` (strategy-service DuckDB, migration version 1 `m1f3_strategy_revisions`) with `control_revision(name) -> int`, `bump_state_revision_in_tx(conn, name, payload) -> int` (writes the acknowledgement-outbox row in the same transaction), `get_receipt(command_id) -> StrategyCommandReceipt | None`, `record_receipt_in_tx(...)`, `prepare_config_revision(name, expected_control_revision, prior, proposed, command_id) -> int`, `mark_committed(revision_id)`, `mark_rolled_back(revision_id, error)`, `unacknowledged_outbox(limit) -> list[OutboxRow]`, `mark_acknowledged(ack_id)`, `recover_on_startup() -> list[int]`.
- Produces: `StrategyRuntime.apply_control_command(command_id, strategy_name, action, expected_control_revision, params: dict | None) -> StrategyCommandReceipt` and `ControlRevisionConflict`.
- Produces: `StrategyControlPort` protocol on the trader side — `forward(request: CommandRequest) -> StrategyCommandReceipt` and `get_receipt(command_id) -> StrategyCommandReceipt | None` (typed client to strategy_service).
- Produces typed methods: trader command socket `enable_strategy`, `disable_strategy`, `update_strategy_params` (frozen names — coordinator-forwarded); strategy-service command socket registers the same three plus `record_state_acknowledged`; strategy-service query socket registers `get_strategy_receipt`.

- [ ] **Step 1: Write failing revision, receipt, and staged-YAML tests**

```python
def test_control_revision_cas_rejects_stale_expected_version(runtime):
    current = runtime._revisions.control_revision("smi_crossover")
    with pytest.raises(ControlRevisionConflict):
        runtime.apply_control_command(
            "cmd-1", "smi_crossover", "disable_strategy",
            expected_control_revision=current - 1, params=None)
    assert runtime.get_strategy("smi_crossover").state != StrategyState.DISABLED


def test_receipt_ledger_makes_forwarded_retries_idempotent(runtime):
    current = runtime._revisions.control_revision("smi_crossover")
    first = runtime.apply_control_command(
        "cmd-1", "smi_crossover", "disable_strategy",
        expected_control_revision=current, params=None)
    assert first.state == "COMMITTED"
    retry = runtime.apply_control_command(
        "cmd-1", "smi_crossover", "disable_strategy",
        expected_control_revision=current, params=None)   # stale revision on purpose
    assert retry == first                                 # recorded receipt, mutation NOT repeated
    assert runtime._revisions.control_revision("smi_crossover") == current + 1


def test_state_revision_and_outbox_commit_together(revisions, db):
    def _bump(conn):
        rev = revisions.bump_state_revision_in_tx(conn, "smi_crossover", {"state": "RUNNING"})
        raise RuntimeError("crash before commit")
    with pytest.raises(RuntimeError):
        db.transaction(_bump)
    assert revisions.unacknowledged_outbox(10) == []      # neither row survived


def test_param_update_stages_prepared_then_committed(runtime, config_path):
    current = runtime._revisions.control_revision("smi_crossover")
    receipt = runtime.apply_control_command(
        "cmd-1", "smi_crossover", "update_strategy_params",
        expected_control_revision=current, params={"EMA_PERIOD": 15})
    assert receipt.state == "COMMITTED"
    states = runtime._revisions.config_revision_states("smi_crossover")
    assert states[-1] == "COMMITTED"
    assert yaml.safe_load(config_path.read_text())["strategies"][0]["params"]["EMA_PERIOD"] == 15


def test_failed_swap_restores_prior_config_and_marks_rolled_back(runtime, config_path):
    prior = config_path.read_text()
    runtime.fail_next_reload()                            # instantiation of the replacement fails
    current = runtime._revisions.control_revision("smi_crossover")
    receipt = runtime.apply_control_command(
        "cmd-1", "smi_crossover", "update_strategy_params",
        expected_control_revision=current, params={"EMA_PERIOD": 15})
    assert receipt.state == "ROLLED_BACK" and receipt.error
    assert config_path.read_text() == prior               # prior configuration restored
    assert runtime._revisions.control_revision("smi_crossover") == current   # no revision minted


def test_restart_recovers_prepared_to_last_committed(revisions, config_path):
    rid = revisions.prepare_config_revision(
        "smi_crossover", expected_control_revision=3,
        prior={"params": {"EMA_PERIOD": 20}}, proposed={"params": {"EMA_PERIOD": 15}},
        command_id="cmd-crash")
    recovered = revisions.recover_on_startup()
    assert recovered == [rid]
    assert revisions.config_revision_state(rid) == "ROLLED_BACK"


def test_trader_journals_strategy_updated_only_after_acknowledgement(forwarding):
    receipt = forwarding.coordinator.execute(_strategy_request(
        "cmd-1", "enable_strategy", expected_version=4))
    assert receipt.state == "RESOLVED"
    kinds = [e.event_type for e in forwarding.journal.read_after(0, 100)]
    assert "strategy.updated" in kinds
    strategy_events = [e for e in forwarding.journal.read_after(0, 100)
                       if e.event_type == "strategy.updated"]
    assert strategy_events[0].entity_revision == receipt.outcome["state_revision"]
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run --frozen pytest tests/test_strategy_revisions.py -q`

Expected: FAIL because `strategy_revisions.py` and `apply_control_command` do not exist.

- [ ] **Step 3: Implement the strategy-service revision store (its own DB, migration 1)**

```sql
CREATE SEQUENCE IF NOT EXISTS strategy_ack_seq START 1;
CREATE SEQUENCE IF NOT EXISTS strategy_config_rev_seq START 1;
CREATE TABLE IF NOT EXISTS strategy_revisions (
    strategy_name VARCHAR PRIMARY KEY,
    state_revision BIGINT NOT NULL,
    control_revision BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_command_receipts (
    command_id VARCHAR PRIMARY KEY,
    strategy_name VARCHAR NOT NULL,
    action VARCHAR NOT NULL,
    state VARCHAR NOT NULL CHECK (state IN ('COMMITTED', 'ROLLED_BACK')),
    control_revision BIGINT NOT NULL,
    state_revision BIGINT NOT NULL,
    error VARCHAR,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_ack_outbox (
    ack_id BIGINT PRIMARY KEY DEFAULT nextval('strategy_ack_seq'),
    strategy_name VARCHAR NOT NULL,
    state_revision BIGINT NOT NULL,
    control_revision BIGINT NOT NULL,
    payload JSON NOT NULL,
    acknowledged BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_config_revisions (
    revision_id BIGINT PRIMARY KEY DEFAULT nextval('strategy_config_rev_seq'),
    strategy_name VARCHAR NOT NULL,
    expected_control_revision BIGINT NOT NULL,
    prior_config JSON NOT NULL,
    proposed_config JSON NOT NULL,
    state VARCHAR NOT NULL CHECK (state IN ('PREPARED', 'COMMITTED', 'ROLLED_BACK')),
    command_id VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ
);
```

- [ ] **Step 4: Implement `apply_control_command` and the staged YAML swap**

```python
def apply_control_command(self, command_id, strategy_name, action,
                          expected_control_revision, params):
    existing = self._revisions.get_receipt(command_id)
    if existing is not None:
        return existing                          # §9.1: a coordinator retry never repeats the mutation
    current = self._revisions.control_revision(strategy_name)
    if expected_control_revision != current:
        raise ControlRevisionConflict(strategy_name, current)

    if action == "update_strategy_params":
        prior_entry, proposed_entry = self._config_entries(strategy_name, params)
        revision_id = self._revisions.prepare_config_revision(
            strategy_name, current, prior_entry, proposed_entry, command_id)
        try:
            self._stage_yaml(proposed_entry)     # write .tmp — not yet renamed
            self._swap_runtime(strategy_name, proposed_entry)   # validate + instantiate replacement
            os.replace(self._staged_path(), self.strategy_config_file)
        except Exception as ex:
            self._restore_runtime(strategy_name, prior_entry)
            self._unstage_yaml()
            self._revisions.mark_rolled_back(revision_id, str(ex))
            return self._record(command_id, strategy_name, action,
                                "ROLLED_BACK", current, error=str(ex))
        self._revisions.mark_committed(revision_id)
    elif action == "enable_strategy":
        self.enable_strategy(strategy_name)
    elif action == "disable_strategy":
        self.disable_strategy(strategy_name)

    def _commit(conn):
        control = self._revisions.bump_control_revision_in_tx(conn, strategy_name)
        state = self._revisions.bump_state_revision_in_tx(
            conn, strategy_name, self._state_payload(strategy_name, control))
        return self._revisions.record_receipt_in_tx(
            conn, command_id, strategy_name, action, "COMMITTED", control, state)

    return self._db.transaction(_commit)
```

`_swap_runtime` reuses the existing hot-swap body of `update_strategy_params` (`strategy_runtime.py:304-386`) but validates and instantiates the replacement *before* the staged file is renamed; the legacy public `update_strategy_params(name, params)` RPC delegates to `apply_control_command` with a synthetic `command_id` only in offline/legacy mode and is not registered on any typed production socket. `recover_on_startup()` runs before the service reports ready: every `PREPARED` row has its `prior_config` restored into the YAML entry and is marked `ROLLED_BACK`; `COMMITTED` revisions are simply what `config_loader` now loads. An acknowledgement loop retries `unacknowledged_outbox` rows against the trader's typed `record_state_acknowledged` command until the trader confirms the journal write; the trader-side handler journals `strategy.updated` with `entity_revision = state_revision` (the frozen `[M1-F1]` contract) and returns the journaled revision, after which `mark_acknowledged` runs. The trader's periodic anti-entropy compare against the revisioned strategy snapshot repairs lost acknowledgements.

On the trader side, the coordinator registers `enable_strategy`, `disable_strategy`, and `update_strategy_params` as forwarded sagas: validate (strategy exists in the snapshot, exposure check for disable per §9.3), transition to `SUBMITTING`, call `StrategyControlPort.forward` carrying the root `command_id` and the payload `control_revision` as `expected_version`, then resolve from the returned `StrategyCommandReceipt` (`COMMITTED` → `RESOLVED` with the resulting revisions; `ROLLED_BACK` → `RESOLVED` with the safe error). A timeout leaves `OUTCOME_UNKNOWN` for Task 9's receipt reconciliation. Exposure-owning disable validates that a remaining owner for exits is identified (`EXPOSURE_OWNERSHIP_UNRESOLVED` otherwise); it never silently orphans a protective order. After each acknowledged strategy-state change (and on the periodic anti-entropy pass), the trader recomputes the conid set of *running* strategies and calls `[M1-F2]`'s `QuoteSubscriptionManager.set_owner_refs("strategy", conids)`, mirroring the `"proposal"` owner-kind wiring from Task 2.

- [ ] **Step 5: Run strategy-revision and reconcile regressions**

Run: `uv run --frozen pytest tests/test_strategy_revisions.py tests/test_strategy_runtime_reconcile.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add trader/strategy/strategy_revisions.py trader/strategy/strategy_runtime.py trader/strategy_service.py trader/trading/command_coordinator.py trader/messaging/production_api.py config_defaults/trader.yaml tests/test_strategy_revisions.py
git commit -m "feat(m1-f3): persist strategy revisions and command receipts"
```

### Task 8: SDK, CLI, and SignalProposer as thin typed adapters

**Files:**
- Modify: `trader/sdk.py:1174-1360,1387-1500,1498-1615` (propose, proposals, reject, approve)
- Modify: `trader/strategy/signal_proposer.py`
- Modify: `trader/strategy/strategy_runtime.py` (SignalProposer wiring; delete the `[S0]` reconcile expiry call)
- Modify: `trader/mmr_cli.py` (pass `--expected-version` through `approve`; no other surface change)
- Modify: `tests/test_sdk.py`
- Modify: `tests/test_signal_proposer.py`
- Modify: `tests/test_propose_approve_integration.py`

**Interfaces:**
- Consumes: `TypedRpcClient.call(method, body, response_model)` from `[G0]`; typed methods `create_proposal`, `approve_proposal`, `reject_proposal`, `get_proposal`, `list_proposals`, `get_command`, `get_trading_control` from Tasks 3–5.
- Produces: `MMRTraderSDK.approve(proposal_id: int, expected_version: int | None = None) -> SuccessFail`, `reject(proposal_id, reason) -> bool`, `propose(...) -> SuccessFail`, `proposals(...) -> pd.DataFrame` as typed adapters holding no proposal-write capability.
- Produces: `SignalProposer(command_client: TypedRpcClient, query_client: TypedRpcClient, paper_trading: bool, account_id: str, proposal_ttl_minutes: int = 30)` — creates proposals only through `create_proposal` and reads the pause gate through `get_trading_control`.
- Removes: every direct `ProposalStore` write from `trader/sdk.py`, `trader/strategy/signal_proposer.py`, and `web/app.py` (the legacy dashboard calls the SDK, so it inherits the typed path with no route changes).

- [ ] **Step 1: Write failing adapter tests**

```python
def test_sdk_approve_is_a_typed_command_with_no_store_write(mmr, typed):
    typed.queue_query("get_proposal", {"proposal_id": 7, "revision": 3, "status": "PENDING"})
    typed.queue_command("approve_proposal", CommandReceipt(
        command_id="sdk-x", correlation_id="sdk-x", state="SUBMITTED",
        outcome={"order_ids": [17], "order_group_id": "og-sdk-x"},
        error_code=None, retryable=False))
    result = mmr.approve(7)
    assert result.is_success() and result.obj == [17]
    call = typed.commands[0]
    assert call.method == "approve_proposal"
    assert call.body["proposal_id"] == 7 and call.body["expected_version"] == 3
    assert call.body["command_id"].startswith("sdk:")
    assert typed.store_writes == []                       # no ProposalStore mutation anywhere


def test_sdk_surfaces_outcome_unknown_without_marking_failed(mmr, typed):
    typed.queue_query("get_proposal", {"proposal_id": 7, "revision": 3, "status": "PENDING"})
    typed.queue_command("approve_proposal", CommandReceipt(
        command_id="sdk-x", correlation_id="sdk-x", state="OUTCOME_UNKNOWN",
        outcome=None, error_code="DISPATCH_AMBIGUOUS", retryable=False))
    result = mmr.approve(7)
    assert not result.is_success()
    assert "reconcil" in result.error.lower()             # loud ambiguity, never silent failure
    assert "do NOT re-approve" in result.error


def test_sdk_propose_and_reject_are_typed_calls(mmr, typed):
    typed.queue_command("create_proposal", CommandReceipt(
        "c1", "c1", "RESOLVED", {"proposal_id": 41, "revision": 1}, None, False))
    created = mmr.propose(symbol="AAPL", action="BUY", confidence=0.7, group="tech")
    assert created.is_success() and created.obj["proposal_id"] == 41
    typed.queue_command("reject_proposal", CommandReceipt(
        "c2", "c2", "RESOLVED", {"proposal_id": 41, "status": "REJECTED"}, None, False))
    assert mmr.reject(41, "changed thesis") is True
    assert [c.method for c in typed.commands] == ["create_proposal", "reject_proposal"]


def test_signal_proposer_creates_via_typed_api(proposer, typed):
    typed.queue_query("get_trading_control", {"new_exposure_paused": False, "revision": 1})
    typed.queue_command("create_proposal", CommandReceipt(
        "s1", "s1", "RESOLVED", {"proposal_id": 41, "revision": 1}, None, False))
    pid = proposer.on_signal("orb", _signal(conid=265598, action=Action.BUY,
                                            probability=0.8), _frame())
    assert pid == 41
    body = typed.commands[0].body
    assert body["conid"] == 265598 and body["source"] == "strategy:orb"
    assert body["command_id"].startswith("strategy:")


def test_signal_proposer_suppresses_entries_when_paused_stale_or_unavailable(proposer, typed):
    typed.queue_query("get_trading_control", {"new_exposure_paused": True, "revision": 2})
    assert proposer.on_signal("orb", _signal(action=Action.BUY), _frame()) is None
    typed.fail_next_query("get_trading_control", ConnectionError("trader down"))
    assert proposer.on_signal("orb", _signal(action=Action.BUY), _frame()) is None  # fail closed
    # Verified exit proposals remain allowed while paused (§9.4).
    typed.queue_query("get_trading_control", {"new_exposure_paused": True, "revision": 2})
    typed.queue_command("create_proposal", CommandReceipt(
        "s2", "s2", "RESOLVED", {"proposal_id": 42, "revision": 1}, None, False))
    assert proposer.on_signal("orb", _signal(action=Action.SELL), _frame()) == 42


def test_non_trader_processes_hold_no_proposal_write_capability():
    banned = ("proposal_store.add(", ".try_transition(", ".update_status(",
              "claim_for_approval(", "expire_stale_pending(")
    for path in ("trader/sdk.py", "trader/strategy/signal_proposer.py", "web/app.py"):
        source = Path(path).read_text()
        for token in banned:
            assert token not in source, f"{path} still writes proposals via {token}"
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run --frozen pytest tests/test_sdk.py tests/test_signal_proposer.py -q`

Expected: FAIL because the SDK still claims via `ProposalStore.try_transition` and `SignalProposer` still constructs `TradeProposal` rows directly.

- [ ] **Step 3: Replace SDK orchestration with typed calls**

`approve()` (replacing `trader/sdk.py:1498-1615` — resolve/snapshot/sizing/order placement all move server-side):

```python
def approve(self, proposal_id: int, expected_version: Optional[int] = None) -> SuccessFail:
    try:
        if expected_version is None:
            view = self._typed_query.call(
                "get_proposal", {"proposal_id": proposal_id}, ProposalView)
            expected_version = view.revision
        receipt = self._typed_command.call(
            "approve_proposal",
            {"command_id": f"sdk:{uuid.uuid4()}", "proposal_id": proposal_id,
             "expected_version": expected_version},
            CommandReceipt)
    except (TimeoutError, ConnectionError) as ex:
        return SuccessFail.fail(
            error=f"approve_proposal for #{proposal_id} did not complete: {ex}. "
                  f"Check `mmr proposals` / `mmr orders` before retrying.", exception=ex)
    if receipt.state == "SUBMITTED":
        return SuccessFail.success(obj=(receipt.outcome or {}).get("order_ids", []))
    if receipt.state == "OUTCOME_UNKNOWN":
        return SuccessFail.fail(error=(
            f"Proposal #{proposal_id}: outcome unknown — trader_service is "
            f"reconciling by orderRef (command {receipt.command_id}). "
            f"Do NOT re-approve; watch `mmr proposals` for the resolution."))
    return SuccessFail.fail(
        error=f"Proposal #{proposal_id} approve rejected: "
              f"{receipt.error_code or receipt.state}")
```

`reject()` sends `reject_proposal` and returns `receipt.state == "RESOLVED"`. `propose()` maps its keyword surface onto a `create_proposal` body (the server owns filter/sizing/quote/group registration; the SDK no longer touches `PositionGroupStore` for proposals) and preserves its `--json` response shape from the receipt outcome. `proposals()` reads `list_proposals` and keeps the `[S0]` contract: numeric `confidence`, `storage_status`, `display_status` via `proposal_display_status`, plus the new `revision`, `expires_at`, and `reference_price` columns. The `_utcnow` seam and `claim_for_approval` import from `[S0]` are deleted. `mmr_cli.py` gains `approve N [--expected-version V]`, defaulting to the fetched current revision.

- [ ] **Step 4: Rewrite SignalProposer as a typed client**

Sizing, dedup, expiry, resolution, and portfolio reads all belong to `ProposalCommandService` now, so `SignalProposer` shrinks to gate + translate:

```python
def on_signal(self, strategy_name: str, signal: Signal,
              frame: pd.DataFrame) -> Optional[int]:
    if not self._gate(strategy_name):
        return None
    conid = int(signal.conid or 0)
    if conid <= 0:
        logging.error('signal from %s has no conid stamped — cannot propose', strategy_name)
        return None
    action = {Action.BUY: 'BUY', Action.SELL: 'SELL'}.get(signal.action)
    if action is None:
        return None

    if action == 'BUY' and not self._entries_allowed():   # §9.4: paused/stale/unavailable → suppress
        return None

    body = {
        'command_id': f'strategy:{uuid.uuid4()}',
        'conid': conid,
        'action': action,
        'confidence': float(signal.probability),
        'reasoning': f'{action} signal from strategy {strategy_name} '
                     f'(probability {signal.probability:.2f}, risk {signal.risk:.2f})',
        'source': f'{self.SOURCE_PREFIX}{strategy_name}',
        'max_hold_bars': signal.max_hold_bars,
        'close_by_time': signal.close_by_time.isoformat() if signal.close_by_time else None,
    }
    try:
        receipt = self._command_client.call('create_proposal', body, CommandReceipt)
    except (TimeoutError, ConnectionError) as ex:
        logging.error('create_proposal RPC failed for %s conId %s: %s',
                      strategy_name, conid, ex)
        return None
    if receipt.error_code:
        logging.info('create_proposal refused for %s conId %s: %s',
                     strategy_name, conid, receipt.error_code)   # DUPLICATE_PENDING, SIZING_BLOCKED...
        return None
    return (receipt.outcome or {}).get('proposal_id')


def _entries_allowed(self) -> bool:
    try:
        control = self._query_client.call(
            'get_trading_control', {'account_id': self._account_id}, TradingControlView)
    except Exception as ex:
        logging.warning('pause gate unavailable — suppressing entry proposals: %s', ex)
        return False                                   # fail closed
    return not control.new_exposure_paused
```

`check_exits` keeps its bar-driven trigger logic but reads executed bridge entries through `list_proposals` (`status="EXECUTED"`, `source` prefix filter) and proposes the close through the same `create_proposal` body with `reasoning`/`entry_proposal_id` metadata; the server's `DUPLICATE_PENDING` dedup replaces `_pending_exists`. `_expire_stale`, `_portfolio_state`, `_held_position`, `_add_proposal`, and the `ProposalStore` constructor argument are deleted; `strategy_runtime.py` builds `SignalProposer` with the typed command/query clients and removes the `[S0]` `signal_proposer.expire_stale()` reconcile call (trader-owned since Task 2). `tests/test_propose_approve_integration.py` is rewritten to drive an in-process coordinator + repository (Tasks 2–5 fixtures) through the SDK adapter, preserving its end-to-end propose → approve → order-IDs and failure-path assertions.

- [ ] **Step 5: Run the adapter regression set**

Run: `uv run --frozen pytest tests/test_sdk.py tests/test_signal_proposer.py tests/test_propose_approve_integration.py tests/test_web_dashboard.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add trader/sdk.py trader/strategy/signal_proposer.py trader/strategy/strategy_runtime.py trader/mmr_cli.py web/app.py tests/test_sdk.py tests/test_signal_proposer.py tests/test_propose_approve_integration.py
git commit -m "feat(m1-f3): route sdk cli and signals through typed authority"
```

### Task 9: OUTCOME_UNKNOWN reconciliation and the M1-F3 integration gate

**Files:**
- Modify: `trader/trading/command_coordinator.py` (add `OutcomeReconciler`)
- Modify: `trader/trader_service.py` (reconciler loop, startup rescan, daily ledger/audit retention job)
- Create: `tests/integration/test_command_authority.py`
- Modify: `tests/test_command_coordinator.py` (schedule unit tests)

**Interfaces:**
- Consumes: `OrderDispatchPort.find_by_order_ref` / `enumeration_complete` (Task 5), `StrategyControlPort.get_receipt` (Task 7), `CommandLedger` (Task 3), `encode_order_ref` from `[M1-F2]`.
- Produces: `RECONCILE_DELAYS: tuple[float, ...] = (0.0,) + (5.0,) * 12 + (30.0,) * 28` and `CRITICAL_AFTER_SECONDS = 900.0` (spec §9.5 exactly: immediate, 5 s × first minute, 30 s × next 14 minutes).
- Produces: `OutcomeReconciler` with `schedule(command_id: str, now: datetime)`, `run_due(now) -> list[ReconcileResult]`, `reconcile_once(command_id, now) -> ReconcileResult`, `rescan_on_startup() -> list[str]`; `ReconcileResult(command_id, resolved: bool, critical: bool)`.
- Produces: `CriticalAlertPort.raise_alert(command_id: str, detail: str)` protocol (consumed by `[M1-R]` health rendering).

- [ ] **Step 1: Write failing schedule and reconciliation tests**

```python
def test_schedule_is_immediate_then_5s_then_30s_for_15_minutes():
    assert RECONCILE_DELAYS[0] == 0.0
    assert RECONCILE_DELAYS[1:13] == (5.0,) * 12        # every 5 s for the first minute
    assert RECONCILE_DELAYS[13:] == (30.0,) * 28        # every 30 s for the next 14 minutes
    assert sum(RECONCILE_DELAYS) == 900.0               # critical alert boundary
    assert CRITICAL_AFTER_SECONDS == 900.0


def test_order_command_resolves_by_encoded_order_ref(recon):
    recon.mark_unknown("cmd-1", target_type="order", order_group_id="og-cmd-1", proposal_id=7)
    recon.orders.add_broker_order(
        order_ref=encode_order_ref("og-cmd-1"), status="Submitted", order_ids=[17])
    result = recon.reconciler.reconcile_once("cmd-1", recon.now())
    assert result.resolved is True
    row = recon.ledger.get("cmd-1")
    assert row.state == "RESOLVED" and row.outcome["order_ids"] == [17]
    assert recon.repo.get(7).status == "EXECUTED"       # submission evidence recorded
    kinds = [e.event_type for e in recon.journal.read_after(recon.cursor, 100)]
    assert "command.updated" in kinds and "proposal.updated" in kinds


def test_definitive_absence_requires_a_complete_enumeration(recon):
    recon.mark_unknown("cmd-1", target_type="order", order_group_id="og-cmd-1", proposal_id=7)
    recon.orders.enumeration_ok = False                 # no fenced generation yet
    assert recon.reconciler.reconcile_once("cmd-1", recon.now()).resolved is False
    assert recon.ledger.get("cmd-1").state == "OUTCOME_UNKNOWN"
    recon.orders.enumeration_ok = True                  # fenced view proves absence
    result = recon.reconciler.reconcile_once("cmd-1", recon.now())
    assert result.resolved is True
    assert recon.ledger.get("cmd-1").outcome == {"submitted": False}
    assert recon.repo.get(7).status == "FAILED"         # proven never-submitted: clean failure
    assert recon.orders.submissions == []               # never blindly resubmitted


def test_strategy_receipts_reconcile_by_root_command_id(recon):
    recon.mark_unknown("cmd-9", target_type="strategy", target_id="smi_crossover")
    recon.strategy.receipts["cmd-9"] = StrategyCommandReceipt(
        "cmd-9", "smi_crossover", "enable_strategy", "COMMITTED",
        control_revision=5, state_revision=12, error=None)
    assert recon.reconciler.reconcile_once("cmd-9", recon.now()).resolved is True
    assert recon.ledger.get("cmd-9").outcome["control_revision"] == 5
    # A missing receipt stays unknown and the mutation is never repeated.
    recon.mark_unknown("cmd-10", target_type="strategy", target_id="smi_crossover")
    assert recon.reconciler.reconcile_once("cmd-10", recon.now()).resolved is False
    assert recon.strategy.forward_calls == []


def test_unresolved_after_15_minutes_is_critical_never_failed(recon):
    recon.mark_unknown("cmd-1", target_type="order", order_group_id="og-cmd-1", proposal_id=7)
    late = recon.now() + dt.timedelta(seconds=901)
    results = recon.reconciler.run_due(late)
    assert results[0].critical is True
    assert recon.alerts.raised == ["cmd-1"]
    assert recon.ledger.get("cmd-1").state == "OUTCOME_UNKNOWN"   # never timeout-to-failure


def test_unknown_command_blocks_other_commands_for_the_same_proposal(gate):
    record = gate.pending(conid=265598, action="BUY")
    gate.orders.raise_on_submit(TimeoutError("ack lost"))
    unknown = gate.execute_approve(record, command_id="cmd-1")
    assert unknown.state == "OUTCOME_UNKNOWN"
    blocked = gate.execute_reject(record, command_id="cmd-2")
    assert blocked.error_code == "COMMAND_IN_FLIGHT"              # §9.5


def test_startup_rescan_requeues_inflight_commands(recon):
    recon.mark_unknown("cmd-1", target_type="order", order_group_id="og-cmd-1", proposal_id=7)
    recon.ledger.insert_for_test("cmd-2", state="SUBMITTING",
                                 updated_at=recon.now() - dt.timedelta(minutes=2))
    requeued = recon.reconciler.rescan_on_startup()
    assert set(requeued) == {"cmd-1", "cmd-2"}          # crash between claim and ack is covered
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run --frozen pytest tests/test_command_coordinator.py tests/integration/test_command_authority.py -q`

Expected: FAIL because `OutcomeReconciler` and the schedule constants do not exist.

- [ ] **Step 3: Implement the reconciler**

```python
RECONCILE_DELAYS: tuple[float, ...] = (0.0,) + (5.0,) * 12 + (30.0,) * 28   # §9.5
CRITICAL_AFTER_SECONDS = 900.0


class OutcomeReconciler:
    def reconcile_once(self, command_id: str, now: dt.datetime) -> ReconcileResult:
        row = self._ledger.get(command_id)
        if row is None or row.state not in ("SUBMITTING", "OUTCOME_UNKNOWN"):
            self._plans.pop(command_id, None)
            return ReconcileResult(command_id, resolved=True, critical=False)

        if row.target_type == "order":
            order_ref = encode_order_ref(f"og-{command_id}")
            found = self._orders.find_by_order_ref(row.account_id, order_ref)
            if found:
                self._resolve_order(row, found)                 # RESOLVED + proposal evidence
                return ReconcileResult(command_id, True, False)
            if self._orders.enumeration_complete():
                self._resolve_never_submitted(row)              # proven absence, never resubmit
                return ReconcileResult(command_id, True, False)
        elif row.target_type == "strategy":
            receipt = self._strategy.get_receipt(command_id)    # root command_id lookup
            if receipt is not None and receipt.state in ("COMMITTED", "ROLLED_BACK"):
                self._resolve_strategy(row, receipt)            # journals strategy + command outcome
                return ReconcileResult(command_id, True, False)

        plan = self._plans[command_id]
        if (now - plan.started).total_seconds() >= CRITICAL_AFTER_SECONDS and not plan.alerted:
            plan.alerted = True
            self._alerts.raise_alert(
                command_id, f"{row.action} unresolved after 15 minutes — "
                            f"operator reconciliation required")
        return ReconcileResult(command_id, False, plan.alerted)
```

`schedule` records the start time and attempt index; `run_due(now)` walks `RECONCILE_DELAYS` cumulatively so attempts fire at 0 s, 5–60 s, then 90–900 s. After the delay table is exhausted the plan stays registered at the periodic 30-second session-reconciliation cadence — the command is *never* converted to failure by time alone and continues to block same-target commands via `unresolved_for_target` (the `COMMAND_IN_FLIGHT` validation from Task 5). `_resolve_order` transitions `OUTCOME_UNKNOWN → RESOLVED` with the found broker aliases and marks the proposal `EXECUTED` (submission evidence) in the same transaction; `_resolve_never_submitted` requires `enumeration_complete()` — a fenced broker generation per `[M1-F2]` — before recording `{"submitted": False}` and failing the proposal cleanly. `_resolve_strategy` journals the acknowledged `strategy.updated` (Task 7 contract) and the command outcome together. `rescan_on_startup` selects every `SUBMITTING` and `OUTCOME_UNKNOWN` ledger row and schedules it immediately (coordinator crash recovery, spec §9.5). `trader_service.py` runs `rescan_on_startup()` before readiness, drives `run_due` from a 5-second asyncio task, and adds a daily `purge_expired` retention job for the ledger and audit tables.

- [ ] **Step 4: Write the integration gate**

`tests/integration/test_command_authority.py` wires a real migrated DuckDB, `DomainJournal`, `ProposalRepository`, `ProposalCommandService`, `TradingControlStore`, `CommandLedger`, `TradingCommandCoordinator`, and `OutcomeReconciler` with fake quote/position/broker/strategy ports, and proves end-to-end:

```python
def test_full_loop_create_approve_submit_resolve(stack):
    created = stack.coordinator.execute(_create_request("c-1", conid=265598, action="BUY"))
    pid = created.outcome["proposal_id"]
    approved = stack.coordinator.execute(_approve_request("c-2", pid, expected_version=1))
    assert approved.state == "SUBMITTED"
    events = [e.event_type for e in stack.journal.read_after(0, 1000)]
    assert events.count("proposal.updated") >= 3        # create, claim, submit-link
    assert events.count("command.updated") >= 5         # two command lifecycles
    assert stack.get_command("c-2").state == "SUBMITTED"


def test_crash_between_claim_and_dispatch_reconciles_after_restart(stack):
    pid = stack.create_pending(conid=265598, action="BUY")
    stack.orders.raise_on_submit(SimulatedCrash("killed before IB ack"))
    with pytest.raises(SimulatedCrash):
        stack.coordinator.execute(_approve_request("c-9", pid, expected_version=1))
    restarted = stack.restart()                          # new coordinator over the same DB
    assert restarted.reconciler.rescan_on_startup() == ["c-9"]
    restarted.orders.add_broker_order(
        order_ref=encode_order_ref("og-c-9"), status="Submitted", order_ids=[31])
    assert restarted.reconciler.reconcile_once("c-9", restarted.now()).resolved is True
    assert restarted.repo.get(pid).status == "EXECUTED"  # exactly one order, no resubmission
```

The gate also re-runs the Task 4 pause-serialization scenario through the full coordinator, replays a duplicate `create_proposal` command to prove one proposal row, and asserts the journal cursor stream is gap-free across the crash (`[M1-F1]` invariant held under this plan's writers).

- [ ] **Step 5: Run the complete M1-F3 gate**

Run: `uv run --frozen pytest tests/test_proposal_migration.py tests/test_proposal_command_service.py tests/test_command_coordinator.py tests/test_trading_control.py tests/test_approval_command.py tests/test_cancel_command.py tests/test_strategy_revisions.py tests/test_sdk.py tests/test_signal_proposer.py tests/test_propose_approve_integration.py tests/integration/test_command_authority.py -q`

Expected: PASS.

Run: `uv run --frozen pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py`

Expected: PASS — no regressions outside this plan's files.

- [ ] **Step 6: Commit**

```bash
git add trader/trading/command_coordinator.py trader/trader_service.py tests/integration/test_command_authority.py tests/test_command_coordinator.py
git commit -m "test(m1-f3): gate command authority and unknown-outcome reconciliation"
```
