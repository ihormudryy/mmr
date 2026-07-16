# Realtime Trading Command Center Implementation Plan Suite

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved realtime trading command center in independently reviewable workstreams without weakening the current trading safety boundary.

**Architecture:** Ship the existing semantic corrections first, harden the runtime boundary, then build a trader-owned durable journal and command authority. Add the read-only command center over fenced snapshots and SSE before enabling authenticated paper and live commands; preserve the legacy surface until the compatibility and soak gates pass.

**Tech Stack:** CPython 3.12.13, DuckDB, FastAPI, Uvicorn, sse-starlette, canonical JSON over ZeroMQ, HMAC-SHA256, Jinja2, browser JavaScript, pytest, pytest-asyncio, Playwright, Docker Compose.

## Global Constraints

- The source specification is `docs/superpowers/specs/2026-07-15-realtime-trading-command-center-design.md` at or after commit `740a242`.
- Production uses exactly CPython `3.12.13`; `.python-version`, the Docker base image, developer setup, and CI must agree.
- `trader_service` is the sole production writer for proposal, command, pause, order, fill, risk, reconciliation, and domain-journal state.
- Browser code never opens ZeroMQ, DuckDB, IB, or serialized Python objects.
- Production RPC is allowlisted canonical JSON signed with HMAC-SHA256; arbitrary `dill` is unavailable on production sockets.
- Every durable entity mutation and its journal event commit in one explicit DuckDB transaction.
- HTTP `202 Accepted` means received only; browser success comes from a correlated authoritative event.
- One Uvicorn worker owns in-memory dashboard state; graceful connection drain is bounded to five seconds.
- Quote rendering defaults to 4 Hz and stays within 2-5 Hz; quotes are conflated and absent from the durable journal, domain FIFO, and replay ring.
- Each SSE client has a 1,000-event domain FIFO and a separate latest-quote map. Replay retains at most 10,000 domain/control events or five minutes.
- Durable journal, command ledger, and audit retention is 30 days. Dashboard terminal collections retain 500 rows for 24 hours and clean up every 60 seconds.
- Live exposure-increasing approval requires a live executable-side quote no older than five seconds and defaults to a 50-basis-point drift guard.
- `DASHBOARD_COMMANDS_ENABLED` and `DASHBOARD_LIVE_COMMANDS_ENABLED` default to false; live commands also require an exact `DASHBOARD_LIVE_ACCOUNT_ID` and maximum order notional.
- `[M2]` charts, history, redesigned watchlists, position-group editing, proposal-history browsing, and strategy undeploy are outside this suite.

---

## Delivery graph

```text
[S0] safety corrections ───────────────────────────────────────────────┐
                                                                      │
[G0] runtime/security foundation ───────────────┐                     │
                                                v                     v
[M1-F1] transactions, journal, snapshot/feed ─────> [M1-F2] broker producers
                    │                                   │
                    └──────────────> [M1-F3] command authority
                                      │                 │
                                      └────────┬────────┘
                                               v
                                  [M1-R] read-only command center
                                               │
                                               v
                                     [M1-C] command surfaces
                                               │
                                               v
                                  [COMPAT] parity, soak, retirement
```

`[S0]` is cherry-pickable and may ship immediately. `[G0]`, `[M1-F2]`, and `[M1-F3]` can be developed in parallel after the interfaces in `[M1-F1]` are fixed, but hardened production deployment waits for `[G0]`. `[M1-R]` may use fakes while foundation work is underway, then must pass against fenced real producers before `[M1-C]` is enabled.

## Plan files

1. `[S0]` — [Semantic safety corrections](2026-07-15-command-center-s0-safety.md)
2. `[G0]` — [Runtime and RPC security](2026-07-15-command-center-g0-platform-security.md)
3. `[M1-F1]` — [Transactional journal, snapshot, and feed](2026-07-15-command-center-m1f-event-foundation.md)
4. `[M1-F2]` — [Broker producers, correlation, and quote coverage](2026-07-15-command-center-m1f-broker-producers.md)
5. `[M1-F3]` — [Proposal, command, pause, and strategy authority](2026-07-15-command-center-m1f-command-authority.md)
6. `[M1-R]` — [Read-only realtime command center](2026-07-15-command-center-m1r-realtime-ui.md)
7. `[M1-C]` — [Authenticated command surfaces](2026-07-15-command-center-m1c-commands.md)
8. `[COMPAT]` — [Parity, soak, rollback, and legacy retirement](2026-07-15-command-center-compat-rollout.md)

## Cross-plan interface freeze

The following names are shared contracts. A change requires updating every consuming plan before implementation continues:

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

Operation = Literal["upsert", "delete"]

@dataclass(frozen=True)
class DomainEvent:
    event_id: str
    source_cursor: int
    entity_revision: int
    event_type: str
    entity_type: str
    entity_id: str
    operation: Operation
    account_id: str | None
    source: str
    source_timestamp: datetime
    correlation_id: str | None
    payload: dict[str, Any] | None  # None for delete tombstones (journal stores NULL, read reconstructs None)

@dataclass(frozen=True)
class SnapshotWithCursor:
    source_cursor: int
    broker_generation: int
    entities: dict[str, list[dict[str, Any]]]

@dataclass(frozen=True)
class CommandReceipt:
    command_id: str
    correlation_id: str
    state: str
    outcome: dict[str, Any] | None
    error_code: str | None
    retryable: bool
```

Typed service methods are named `snapshot_with_cursor`, `read_domain_events`, `get_quotes_snapshot`, `create_proposal`, `approve_proposal`, `reject_proposal`, `set_trading_pause`, `enable_strategy`, `disable_strategy`, `update_strategy_params`, `cancel_order`, `cancel_orders`, and `get_command`. Browser HTTP routes call those methods only through `DashboardCommandGateway` or `DashboardEventBridge`.

Additional frozen conventions:

- Typed socket ports: trader_service query `42101`, command `42102`, long-poll
  feed `42103`; strategy_service command `42104`, query `42105` (private Compose
  network only, never host-published).
- `schema_migrations` version ranges (trader DB): `[M1-F1]` owns 1-9, `[M1-F2]`
  owns 10-19, `[M1-F3]` owns 20-29. The strategy-service DuckDB has its own
  independent `schema_migrations` numbering starting at 1.
- `DomainJournal.mutate(conn, mutation, write_materialized)` invokes
  `write_materialized(conn, entity_revision: int) -> None` inside the same
  transaction.
- Order correlation: `encode_order_ref(order_group_id)` / `decode_order_ref(order_ref)`
  in `trader/trading/order_correlation.py` with prefix `mmr:`; the command
  coordinator writes the encoded order group into IB `orderRef` at dispatch.
- Risk decisions: the coordinator records one write-once
  `RiskProducer.publish_decision(command_id, payload, correlation_id=None)`
  per command.
- Quote-coverage owner kinds: `"position"`, `"order"`, `"proposal"`, `"strategy"`
  (`QuoteSubscriptionManager.set_owner_refs(owner_kind, contracts, delayed=False)`).
- Journal DB topology: the `domain_event_journal`, `domain_snapshot_checkpoints`,
  and ALL materialized-state tables (F1's + F2's broker tables) live in a dedicated
  `trader_service`-owned DuckDB file (`journal_duckdb_path`, separate from
  `mmr.duckdb`, mirroring the existing `history_duckdb_path` split), accessed in-process
  through ONE shared `duckdb.connect()` instance; writers and the long-poll reader use
  `.cursor()` off that instance and the reader does NOT go through `execute_atomic`/the
  per-db lock. No other process opens this file. This is why `snapshot_with_cursor`
  can read the journal cursor and every materialized adapter in one fenced transaction.
- Quote snapshot ownership: `get_quotes_snapshot() -> {"quotes": {instrument_id: quote_row}}`
  is produced by `[M1-F2]` (it owns the quote plane; quotes are absent from the journal),
  registered on the `query` socket. `[M1-F1]` does NOT register it. `[M1-R]` may use a
  fake for it during foundation-era development.
- Snapshot readiness gate: `snapshot_with_cursor` returns `broker_generation` and
  raises/returns `SNAPSHOT_NOT_READY` only once a complete broker generation exists.
  `[M1-F1]` defines `SnapshotWithCursor`/`SnapshotNotReady` and returns
  `broker_generation=0` with the gate DORMANT; `[M1-F2]` promotes generations and
  ACTIVATES the gate. `[M1-R]` develops against fakes and meets the active gate at the
  release-gate integration run.

## Release gates

- [ ] Merge and verify `[S0]` independently.
- [ ] Finish `[G0]` code and security tests before publishing any production command capability.
- [ ] Complete proposal migration and every producer contract before treating the read model as authoritative.
- [ ] Run `[M1-R]` read-only in paper mode and pass the eight-hour soak.
- [ ] Enable `[M1-C]` paper commands with overlapping legacy mutations disabled.
- [ ] Run both interfaces read-only for one complete live market session.
- [ ] Enable live commands only for the pinned account after the maximum-notional gate is configured.
- [ ] Retire the legacy dashboard only after the recorded compatibility checklist and rollback drill pass.
