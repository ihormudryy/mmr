# Realtime Trading Command Center

**Date:** 2026-07-15
**Status:** Revised after architecture review; pending user re-review
**Baseline:** Repository audit through `89b97a8`; unrelated active worktree changes are outside this specification

## 1. Purpose

Replace the current periodically refreshed dashboard with a realtime, single-user
trading command center. The trading services remain authoritative. The dashboard
provides a coherent operational view and safe controls, including proposal
creation, proposal approval, and strategy controls in both paper and live modes.
With proposal origination included, the dashboard covers the complete
propose → review → approve → monitor loop — the one workflow it is built
around — so routine trading operation does not require the CLI. Section 8.5
records the disposition of every CLI capability.

This is the first milestone. New charting and historical exploration remain a
separate second milestone. Watchlist management and deploy-from-disk already
exist in the legacy dashboard; rebuilding them must not delay milestone 1, but
they remain compatibility requirements before that dashboard can be retired.

## 2. Goals

- Show account, position, proposal, order, fill, strategy, risk, reconciliation,
  and dependency state with low latency.
- Originate trade proposals from the dashboard through the same trading-filter,
  position-sizing, and risk pipeline as the CLI `propose` command.
- Recover coherently from browser, bridge, and source-service disconnections.
- Make stale, degraded, submitted, partially filled, filled, and failed states
  unambiguous.
- Permit live proposal approval and live strategy control behind explicit safety
  and authentication gates.
- Enforce those gates at the trader-service boundary so another local RPC client
  cannot bypass them.
- Keep browser code outside the ZMQ, DuckDB, and Python-object trust boundaries.
- Bound memory and browser update rates for an all-day local session.
- Preserve trading operation when the dashboard fails.

## 3. Non-goals

- Multi-user accounts, RBAC, OAuth, or remote collaboration.
- Direct browser access to ZMQ, DuckDB, IB, or serialized Python objects.
- Full automatic signal execution without human approval.
- New charts, historical quote exploration, or a redesigned watchlist workflow
  in milestone 1. Existing watchlist and deployment capabilities remain
  available through the legacy dashboard until the parity gate in Section 14.1.
- Mobile-first layout or a general-purpose trading workstation framework.
- Renaming every legacy proposal status in storage as part of this milestone.

### 3.1 Delivery tags and component ownership

Every implementation issue, pull request, schema migration, and acceptance test
carries one delivery tag and one owning component. The tags are:

- `[S0]`: independent semantic corrections that can ship before realtime work.
- `[G0]`: production supervision and internal-RPC security gates.
- `[M1-F]`: source journal, entity stores, command coordinator, and migrations.
- `[M1-R]`: realtime read-only dashboard, snapshot, and SSE delivery.
- `[M1-C]`: authenticated paper and live command controls.
- `[COMPAT]`: an existing legacy-dashboard capability retained during migration.
- `[M2]`: charts, history, and redesigned watchlist experiences.

Each issue has exactly one accountable owner: `trader_service`,
`strategy_service`, `sdk/cli`, `dashboard`, `store-schema`, or
`operations/test`. The rollout matrix lists that accountable owner and names all
affected components separately. Lower delivery tags may not depend on a higher
milestone. Existing watchlist and deploy-from-disk behavior is `[COMPAT]`;
redesigning it is `[M2]`.

This is an umbrella architecture specification, not authorization for one
monolithic implementation plan. Planning is split at least into `[S0]`, `[G0]`,
`[M1-F]` source/store/command foundation, `[M1-R]` read-only delivery, and
`[M1-C]` commands. `[M1-F]` may be decomposed further into transactional stores,
IB producers and correlation, proposal/coordinator ownership, and strategy
revision integration. Each plan has its own tests and review gate.

### 3.2 Independent semantic corrections (`[S0]`)

Approval-result truthfulness, degraded-risk rendering, flat-account net
liquidation, numeric confidence, dispatchable strategy state, authoritative
proposal expiry, and `ORDER_SUBMITTED` versus fill semantics ship as an
independent, cherry-pickable workstream. These corrections require no journal,
SSE, session, or command-center UI.

Fixes belong in the current authoritative SDK, service, and store contracts,
with regression coverage for the legacy dashboard and a transport-independent
domain-value contract. Journal/event-adapter coverage belongs to `[M1-F]`. In
the transitional implementation, the existing strategy-service
reconciliation loop performs the periodic proposal sweep and `ProposalStore`
provides one atomic approve-if-pending-and-unexpired operation. Section 5.5 then
moves both responsibilities to trader service without changing their behavior.
`[S0]` may proceed in parallel with `[G0]` and milestone 1, but must pass before
the read-only command center is treated as authoritative or any new mutation
surface is enabled.

## 4. Decisions

### 4.1 Realtime transport

Use an initial JSON snapshot followed by Server-Sent Events (SSE). This is a
better fit than faster full-page polling because updates are incremental and
freshness is explicit. It is simpler than a WebSocket protocol because browser
traffic is predominantly server-to-client; mutating commands remain ordinary
authenticated HTTP requests.

If SSE is unavailable, the browser falls back to snapshot polling. This fallback
is a degraded mode, not an alternative normal mode.

Domain delivery to the dashboard bridge uses a dedicated typed long-poll API
over the durable journal: `read_domain_events(after_cursor, limit, wait_ms)`.
The request waits off the IB event loop and returns immediately when a committed
journal row advances the cursor. A ten-second empty response is only a
heartbeat. This is neither snapshot polling nor poll-and-diff, and it does not
continuously recompute account or broker state. At most one outstanding
long-poll exists per bridge, with a separate connection from commands and
snapshot loading. Existing PubSub and MessageBus are not the domain-event
transport; their lossy payloads are never authoritative.

### 4.2 Command-center layout

Use the approved **Layout A: operations-first grid**:

1. Persistent environment and dependency status bar.
2. Account summary metrics.
3. Positions as the dominant workspace.
4. Persistent proposal and urgent-action rail.
5. Working orders, recent executions, strategy health, and risk/reconciliation
   state below the primary workspace.

Details open in drawers so the operator does not lose command-center context.

### 4.3 Deployment model

Run one Uvicorn worker. `DashboardState` is deliberately in-process and is not
shared across workers. The dashboard is a separate supervised process from the
trader and strategy services.

### 4.4 Proposal and command ownership

Milestone 1 deliberately relocates proposal approval from SDK orchestration to
`trader_service`; this is part of `[M1-F]`, not dashboard-only work. A
trader-owned `ProposalCommandService`, `ProposalRepository`,
`TradingCommandCoordinator`, command ledger, audit log, and pause gate are the
only production mutation boundary. The SDK, CLI, dashboard, and strategy service
become typed clients and do not open or write proposal tables directly.

Keeping SDK-side approval was considered as a smaller alternative. It preserves
the current status-only DuckDB compare-and-swap but cannot make proposal state,
command idempotency, audit, and broker reconciliation one authoritative durable
saga. The milestone-1 service makes every local claim, ledger, audit, and journal
transition atomic, then reconciles the necessarily non-transactional IB step.
SDK-side approval is therefore limited to the `[S0]` compatibility patch and is
not the milestone-1 target architecture.

## 5. Architecture

```text
IB / strategy_service / proposal, order, fill, risk, reconciliation state
                                  |
                                  v
              trader_service snapshot + durable domain-event journal
                         /                         \
                        v                           v
           DashboardEventBridge              Quote subscriber
                         \                         /
                          v                       v
                    bounded in-memory DashboardState
                            /                    \
                           v                      v
                 GET /api/snapshot       GET /api/events (SSE)
                            \                    /
                             v                  v
                                    Browser
                                       |
                                       v
                            authenticated POST commands
                                       |
                                       v
                    DashboardCommandGateway (separate client)
                                       |
                                       v
                    trader_service TradingCommandCoordinator
                                       |
                                       v
                      strategy_service / IB side effects
```

### 5.1 `DashboardEventBridge`

The bridge is the dashboard's only consumer of ticker and domain updates. It
normalizes quotes, account changes, positions, proposal transitions, order and
fill changes, strategy status, risk state, reconciliation results, and service
health. Quotes are ephemeral and latest-value oriented. Critical domain changes
come from the durable journal described in Section 5.4, not from a claim that
the current PUB/SUB transport is lossless.

Blocking quote-stream reads run outside the ASGI event loop and hand normalized
input to a bounded, conflated quote queue. Domain updates arrive through the
long-poll journal API and are consumed strictly by durable cursor. One reducer
task owns state mutation. Snapshot readers obtain an immutable copy, so
templates and API handlers never observe a partially applied event.

FastAPI lifespan owns exactly one bridge, reducer, and fan-out registry.
`GET /api/events` is an `async def` endpoint implemented with
`sse-starlette.EventSourceResponse`; `sse-starlette` is a direct locked runtime
dependency. Blocking SDK and RPC work runs in dedicated threads, never on the
ASGI loop. Producer threads cross into the loop only through
`loop.call_soon_threadsafe`; only loop-owned code mutates `asyncio` queues. The
event generator unregisters its client in `finally` on disconnect or
cancellation. Uvicorn owns connection shutdown: it stops accepting requests and
uses an explicit five-second graceful-shutdown timeout, after which it cancels
any remaining `EventSourceResponse` generators and triggers their `finally`
cleanup. Starlette lifespan teardown begins only after those connections drain;
it then verifies the client registry is empty, stops source producers, cancels
reducer/fan-out tasks, and closes source and command clients. Lifespan teardown
never waits on an active SSE response. One Uvicorn worker and the shutdown
timeout are enforced by deployment and checked at startup.

### 5.2 `DashboardState`

The state is a UI-oriented read model, not a second source of truth. It contains
the latest account, position, quote, strategy, risk, reconciliation, and
dependency state; active proposals and orders; bounded recent terminal records;
and a short event-replay ring.

### 5.3 `DashboardCommandGateway`

Commands use an SDK/RPC connection independent from the event subscriber and
snapshot loading. Its own timeout and serialization lock prevent slow reads or
reconnection work from delaying urgent actions. Domain validation and
idempotency are enforced again by the authoritative target service, not only by
the web process.

### 5.4 Source event foundation

Milestone 1 adds trader-service-owned, revisioned materialized tables, an
append-only `DomainEventJournal`, and a fenced `snapshot_with_cursor` API. One
read transaction returns the baseline and its maximum included journal cursor.
The journal assigns a stable event ID, source cursor, entity revision, and
tombstone for deletions. The trader service exposes snapshot, long-poll cursor,
and command APIs through typed JSON; the dashboard never opens its DuckDB file.

Every local state mutation and its journal row commit through one explicit
database transaction. The existing process lock around `execute_atomic` is not
sufficient evidence of transactional atomicity; `[M1-F]` adds an explicit
transaction/unit-of-work API and crash-point tests. IB callbacks are persisted
before their events become visible. A cursor reader is served from a dedicated
thread/connection and is signalled after commit, so a waiting read does not
block the IB loop or command connection.

The producer contract is:

| Event | Authoritative owner | Persistence and emission point | Transport | Minimum payload |
|---|---|---|---|---|
| `account.updated` | `trader_service` | Normalize IB account-value/account-summary callbacks; persist and journal only changed account fields. | Journal long-poll. | Account, mode, currency-tagged balances, buying power, margin, source time. |
| `position.updated` | `trader_service` | Merge IB position, portfolio, and P&L callbacks; persist each normalized change and emit a tombstone when quantity reaches zero. | Journal long-poll. | Account + `conId`, contract metadata, quantity, cost, market value, P&L, currencies. |
| `proposal.updated` | `trader_service` `ProposalRepository` | Every create, metadata change, expiry, reject, approval claim, submission link, and terminal transition commits with its event. | Journal long-poll. | Full proposal summary, status, guards, revision, order-group link. |
| `command.updated` | `trader_service` `TradingCommandCoordinator` | Every command-ledger saga transition commits with its event. | Journal long-poll. | Command/correlation IDs, action, target, state, safe error, broker aliases. |
| `trading_control.updated` | `trader_service` `TradingCommandCoordinator` | Seed, pause, and resume commit the per-account control row with its journal event. | Journal long-poll. | Account, paused boolean, revision, updated time, changing command ID and reason. |
| `order.updated` | `trader_service` | Persist normalized IB open-order and order-status callbacks through the order tracker, then journal in the same transaction. | Journal long-poll. | Immutable MMR order entity ID, account, order group/leg, client order ID, `permId`, `parentId`, `orderRef`, quantities, prices, status. |
| `fill.received` / `fill.updated` | `trader_service` | Persist `execDetails` once by account + execution ID; a later commission report revises the same fill and emits `fill.updated`. | Journal long-poll. | Execution ID, immutable order entity ID when resolved, order aliases, `permId`, `conId`, side, quantity, price, time, commission. |
| `strategy.updated` | `strategy_service`, acknowledged by `trader_service` | Emit after a control revision is committed and active or command-center state changes. Dependency liveness belongs to `service.health`. Trader journals the acknowledged state revision and repairs missed acknowledgements from the revisioned strategy snapshot. | Typed strategy acknowledgement, then journal long-poll. | Strategy ID, state revision, control revision, runtime state, enabled state, parameter/config digest, activity/error time. |
| `risk.updated` | `trader_service` | Persist policy revisions and per-command decisions; debounce an account projection by at most 100 ms after relevant account, position, proposal, or order changes and journal only a changed projection. Policy, projection, and decision are separate entities. | Journal long-poll. | Risk kind and namespaced ID, its own revision, result state, limits used, warnings or safe failure. |
| `reconciliation.updated` | `trader_service` | Persist and journal completion of startup, scheduled, command-triggered, or operator-triggered broker reconciliation. | Journal long-poll. | Run ID, source cursor, discrepancies, resolutions, started/completed time. |
| `quote.updated` | `trader_service` quote publisher | Existing ticker PubSub remains an ephemeral, conflated plane and is never written to the domain journal. | Ticker PubSub, then SSE quote batch. | Canonical instrument, bid/ask/last, market time, feed type and quality. |
| `service.health` | `DashboardEventBridge` | Derive from heartbeats, long-poll state, and revisioned snapshots; it is ephemeral. | In-process reducer, then SSE. | Dependency, state, last success/error, source age, transport lag. |

The current transports are not described as if these producers already exist:
PubSub currently carries tickers and heterogeneous dataclasses, while
MessageBus carries the signal topic. `[M1-F]` creates the callbacks, stores,
typed feed, and payloads above. Snapshot diffing at five-to-thirty-second
intervals remains anti-entropy only and cannot satisfy the 500 ms target.

IB connect and reconnect uses an explicit broker-snapshot completeness barrier.
Before requesting state, trader service registers live callbacks and opens a new
staging generation. It requests account values, positions, open and completed
orders, and executions, and waits for each IB end marker or awaited request
completion. All snapshot callbacks and interleaved live deltas receive a local
ingest sequence and stage by canonical key. After every required source is
complete, one transaction promotes the generation, applies later staged deltas,
tombstones rows absent from complete enumerable sets, journals the resulting
changes, and records the coherent generation and cursor. A timeout or disconnect
abandons the generation, leaves the prior view visible but stale, and prevents
readiness and dependent live commands. A partial callback set is never promoted
or advertised as a fenced snapshot.

Absence tombstones apply only to APIs that enumerate a complete set for the
configured account, such as current positions and open orders. Completed-order
and execution queries merge and deduplicate within their declared broker time
window; they never tombstone durable history merely because it fell outside a
query window. Journal retention governs that history.

Strategy state keeps two persistent counters and an acknowledgement outbox at
`strategy_service`. `state_revision` increments for every browser-visible
strategy-state change and becomes the event `entity_revision`.
`control_revision` increments only for enable/disable or committed parameter and
configuration mutations and is the strategy command compare-and-set value.
Committing either revision and its outbox row is one local transaction. The
service retries acknowledgement until trader service records it in the journal,
and trader service periodically compares against the revisioned strategy
snapshot so a lost acknowledgement is repaired. Dependency liveness is emitted
as `service.health`, not by consuming control revisions. This source contract is
a prerequisite for claiming a coherent realtime view. A source without a
revisioned snapshot or replayable journal is marked degraded and cannot enable
dependent live commands.

### 5.5 Proposal repository and schema migration

`trader_service` is the sole production owner of proposal persistence. An
in-process `ProposalCommandService` owns create, reject, expiry, and validation;
`ProposalRepository` is its persistence adapter, not a separately deployed
service. Strategy signal-to-proposal code calls a typed `create_proposal` API;
SDK and CLI proposal methods call typed query/reject/approve APIs. Direct
`ProposalStore` access from those processes is removed after a compatibility
migration. The trader process runs a single periodic expiry task every 30
seconds and once at startup. Approval also evaluates expiry inside the same
transaction that claims the proposal, so scheduler delay can never make an
expired proposal approvable.

The proposal table gains explicit `account_id`, `account_mode`, canonical
`conid`, `reference_price`, `reference_timestamp`, `reference_quote_side`,
`reference_feed_type`, `max_price_drift_bps`, `expires_at`,
`live_approval_eligible BOOLEAN NOT NULL`, `revision BIGINT NOT NULL`, and
`order_group_id` columns. Every mutation increments `revision`; migration
initializes existing rows at revision 1. The migration replaces positional
`SELECT *` decoding with an explicit stable column list before adding fields.
Existing pending rows without the required live guards are marked ineligible for
live approval and must expire or be recreated. Metadata may retain descriptive
strategy fields, but fields used for identity, compare-and-set, price drift, or
expiry are not hidden in JSON.

Cutover is exclusive and versioned. `[M1-F]` first disables every direct legacy
writer and puts proposal mutations in a maintenance/read-only window. The trader
process then runs one idempotent migration recorded in `schema_migrations`, adds
and validates explicit columns, copies rows using named columns, and only then
enables typed proposal RPC. Parsable legacy metadata `conid` and timezone-aware
`expires_at` values may be copied exactly; invalid, ambiguous, or naive values
remain null and make the row live-ineligible. Account, mode, price, quote side,
feed type, and drift guard are never inferred from current configuration or later
market data. Original metadata is preserved for audit.

Old binaries never regain write access after cutover because they cannot
maintain revisions or journal atomicity. Rollback disables new commands and
returns both dashboards to read-only on the migrated schema. Restoring the
pre-migration backup is permitted only before any post-cutover mutation; after
that point, rollback is forward-compatible service rollback or a separately
tested down-migration, never an old writer against new data.

Existing proposal `order_ids` plus broker execution `orderId` observations are
partial correlation seeds, not a durable fill history. `[M1-F]` creates durable
order and fill stores and may backfill them only through an explicit broker
reconciliation/import with provenance. It hardens the link by assigning an
immutable correlation/order-group ID, placing the correlation in IB `orderRef`,
and binding temporary client order IDs, permanent `permId` values, parent/child
IDs, and execution IDs as they arrive. Symbol/action heuristics are never an
authoritative join.

### 5.6 Authoritative command boundary

All order-producing, risk-limit-changing, and strategy-mutating production paths
pass through a `TradingCommandCoordinator` in `trader_service`. Dashboard, CLI,
and SDK callers
may provide different user experiences, but none can bypass the same command
ledger, risk checks, pause gate, and idempotency rules. Caller-controlled risk
bypass flags and unrestricted direct-order RPC methods are not available on the
production command capability. Risk-limit administration is outside the
dashboard and uses a distinct, narrower admin capability.

Production service RPC is canonical JSON over ZMQ request/reply on dedicated
command, query, and long-poll feed sockets. Each request is validated against an
allowlisted typed schema and signed with an HMAC service credential over method,
request ID, timestamp, replay nonce, and canonical body. No production socket
accepts arbitrary `dill`, even for read calls; legacy Python-object transport is
limited to offline/test processes isolated from broker credentials.

The container sockets bind on the private Compose network. Compose publishes
only the typed query/command gateway on host `127.0.0.1` for SDK/CLI use; the
long-poll feed remains private, and existing raw ZMQ ports are not host-published
in the production profile. Host SDK/CLI uses the same HMAC-authenticated typed
schemas and receives no capability that bypasses the coordinator. Containers run
as unprivileged users.

## 6. Event Contract

Every sequenced browser-visible event is JSON with this envelope:

```json
{
  "schema_version": 1,
  "stream_id": "01J...",
  "sequence": 1842,
  "event_id": "01J...",
  "source_cursor": 88741,
  "entity_revision": 19,
  "event_type": "proposal.updated",
  "entity_type": "proposal",
  "entity_id": "438",
  "operation": "upsert",
  "account_id": "U1234567",
  "source": "trader_service",
  "source_timestamp": "2026-07-15T13:42:17.201Z",
  "received_timestamp": "2026-07-15T13:42:17.219Z",
  "correlation_id": "01J...",
  "payload": {}
}
```

- `schema_version` versions the public JSON shape.
- `stream_id` identifies one bridge lifetime.
- `sequence` increases monotonically within that stream.
- `event_id` is stable in the journal for domain events and permits
  deduplication where a source redelivers data. The bridge assigns ephemeral IDs
  to quote and health updates.
- `source_cursor` is the durable journal position; it is nullable for ephemeral
  quote, health, and stream-control events.
- `entity_revision` is required for durable domain entities, rejects
  regressions, and makes snapshot fencing possible. Quotes use monotonic market
  time/tick identity and may leave it null.
- `operation` is `upsert` or `delete`. `delete` is the explicit tombstone for
  the entity key and carries only identity, revision, source, and correlation
  fields needed to remove it safely.
- Every durable `upsert` carries the complete normalized entity row at that
  revision after the producer merges any partial source callback in the same
  transaction. Reducers replace by entity key; there is no implicit JSON merge
  or ambiguous partial patch.
- `correlation_id` connects a command to all resulting events. It is nullable
  for external market events.
- `account_id` is required for account-bound entities and commands.
- Timestamps are UTC. The UI displays source age and transport lag separately.
  Command freshness uses source/market time, feed quality, and exchange session;
  receive time alone can never make an old buffered quote fresh.
- Domain payloads are typed JSON. Internal `dill` or arbitrary Python objects
  never cross the web boundary.

Initial event types are `account.updated`, `quote.updated`, `position.updated`,
`proposal.updated`, `command.updated`, `trading_control.updated`,
`order.updated`, `fill.received`, `fill.updated`, `strategy.updated`,
`risk.updated`, `reconciliation.updated`, and `service.health`. Stream-control
types are `quotes.snapshot` and `resync_required`; neither is a durable domain
entity. `quotes.snapshot` is a client-local control frame without an SSE `id`;
it cannot advance `Last-Event-ID` for other clients.

Entity keys are canonical, never bare display symbols. Instruments use IB
`conId` plus contract metadata; positions use account plus `conId`; orders use
an immutable MMR `order_entity_id` assigned and persisted at first observation.
For MMR-created orders it derives from order group plus leg identity; external
broker orders receive a persisted local ID. Client order ID, `permId`,
`parentId`, and `orderRef` are aliases and relationships, so a late `permId`
never rekeys an entity or starts a second revision stream. Fills use account plus
IB execution ID and may arrive before their order alias is resolved; later
resolution updates the same fill revision. Proposal-to-order joins use the
explicit order-group relation.

Risk IDs are namespaced: `policy:<policy_id>`,
`projection:<account_id>`, and `decision:<command_id>`. Each namespace has its
own monotonic revision stream; a policy edit never advances an account
projection or rewrites a historical command decision.

Account, position, proposal, command, trading-control, order, fill, risk,
strategy, and reconciliation events are appended to the durable journal before
publication. Transient service health is derived from heartbeats and revisioned
source snapshots. Quotes are coalesced per canonical instrument at a
configurable 2-5 Hz, with 4 Hz as the default. A conflated quote map replaces
older queued quotes for the same instrument. Domain events are re-read from
their journal cursor rather than held indefinitely in an in-memory queue.

### 6.1 Revision authority and command concurrency

`entity_revision` orders read-model updates; `expected_version` is accepted only
where the named service can perform an authoritative compare-and-set. The terms
are not interchangeable for broker-sourced state.

| Entity | Revision authority | Is `expected_version` a command guard? |
|---|---|---|
| Proposal | `trader_service` proposal row; incremented on every mutation. | Yes for approval and risk-increasing changes. Reject uses an idempotent `PENDING`-to-`REJECTED` status CAS so a stale view cannot block a risk-reducing action. |
| Strategy | Persistent `strategy_service` `state_revision` orders events; a separate `control_revision` changes only for enable/disable/configuration commits. Both are acknowledged by trader service. | Yes. Strategy commands compare-and-set `control_revision`, which is exposed in the event payload; runtime activity cannot create a false command conflict. |
| Pause gate | Singleton trader-service gate row with persistent revision. | Yes for resume; pause itself is idempotent and risk-reducing. |
| Command | Trader-service ledger state revision; `command_id` is the immutable idempotency key. | No client CAS; retries match the canonical request hash. |
| Account, position, order, and fill | Trader ingestion revision orders persisted observations of IB callbacks. | No. Broker truth has no MMR compare-and-set. Commands re-read broker state, identity, quantity/status, and freshness immediately before acting. |
| Risk and reconciliation | Separate trader-service revisions for risk policy, per-account projection, per-command decision, and each reconciliation run. | Display ordering only; a live preflight binds the exact risk-policy revision it evaluated. |
| Quote | Exchange/IB market timestamp and feed identity. | No; price-drift, feed-quality, session, and freshness guards apply. |

For IB-sourced entities, a newer MMR revision prevents stale UI regression but
does not prove the broker has not changed again. No API advertises
`expected_version` as protection where the service lacks that authority.

## 7. Snapshot, Replay, and Retention

`GET /api/snapshot` returns `schema_version`, `stream_id`, `sequence`,
`generated_at`, and the complete bounded read model. The browser then opens
`GET /api/events?after=<stream_id>:<sequence>`. Events that arrived while the
snapshot was loading are replayed from the ring.

SSE event IDs use `<stream_id>:<sequence>`. Reconnects honor `Last-Event-ID`.
If the stream changed or the cursor aged out, the server emits
`resync_required`; the browser discards incremental assumptions and fetches a
new snapshot. SSE comments provide heartbeats every ten seconds without
advancing the domain sequence.

The replay ring contains durable domain events and stream-control events only;
individual `quote.updated` events never enter it. Opening an SSE connection is
an atomic reducer-loop handshake: register the client's live queue and capture
the domain replay cutover and current quote map. The server replays retained
sequenced domain events after the requested sequence through the cutover, emits
the current quote baseline as an unsequenced client-local `quotes.snapshot`
frame outside the replay ring, and then drains queued live events in sequence
order. Missing quote history is therefore latest-value-wins without advancing
another client's sequence, violating monotonic SSE IDs, or consuming the
intended domain replay depth.

On startup or domain-source reconnection the bridge calls
`snapshot_with_cursor` only after trader service reports a complete broker-sync
generation. It installs that revisioned baseline and tails the journal strictly
after its returned cursor. Tombstones remove entities. Events at or below an
entity revision are idempotently ignored. Quotes use a separate latest snapshot
followed by conflated updates; a quote gap affects freshness but cannot erase a
durable order, fill, proposal, or command outcome. If a source cannot provide
its completeness barrier and fence, the affected panel remains degraded instead
of guessing.

Each SSE client has a FIFO capped at 1,000 domain/control events plus a
latest-value quote map keyed by canonical instrument. Quote updates replace an
older unsent quote and never consume FIFO capacity. A slow browser or background
tab is never allowed to block source consumption. On domain FIFO overflow, the
server clears that client state, emits `resync_required` when possible, and
closes the stream; reconnect then obtains a fresh snapshot. Multiple tabs
therefore remain bounded independently.

State bounds are:

- Latest quote only for each active canonical instrument. A trader-owned,
  reference-counted quote-subscription manager acquires the union of open
  positions, working orders, pending proposals, and running strategies; a
  position is covered even when no strategy is armed. It releases a subscription
  ten minutes after the final reference disappears. Implementing both acquire
  and release is part of `[M1-F]`; the current one-way `publish_contract` behavior
  is not sufficient.
- All currently active positions, proposals, and orders.
- At most 500 terminal proposals, 500 terminal orders, and 500 fills, each with
  a 24-hour TTL.
- A quote-free replay ring capped at 10,000 domain/control events or five
  minutes, whichever is reached first. At the 20-domain-events/second soak load,
  five minutes consumes 6,000 entries.
- Cleanup every 60 seconds.

The durable domain journal retains 30 days plus the newest complete snapshot
checkpoint. Compaction never removes an event required by an active consumer
cursor. A bridge returning after retention has elapsed must take a full fenced
snapshot rather than request old deltas.

Historical chart data does not use this ring and belongs to milestone 2.

## 8. Command Center

### 8.1 Status and account summary

The top bar shows a prominent `LIVE` or `PAPER` badge, the exact IB account, IB,
ticker, trader, strategy, event-bridge, and browser-stream health, and the latest
authoritative event time. Every panel and row still shows its own data age; a
recent quote cannot make stale account or strategy state appear fresh. Summary
cards show net liquidation, daily P&L, exposure, buying power, and margin
cushion. Net liquidation comes from account values even when the account has no
positions; a flat cash account must not display zero by construction.

### 8.2 Positions

Positions are the largest panel. Each row shows quantity, average and current
price, instrument and account currency, base-currency conversion, unrealized
and daily P&L, exposure, protection state, and freshness.
Selecting a position opens a drawer with related orders, fills, proposals, risk
information, and reconciliation findings.

The drawer's **Close position** action never places an order directly. It
creates a pre-filled position-reducing proposal (opposite side, verified
reducible quantity, market order by default) that enters the ordinary action
queue and follows the position-reducing approval rules of Section 9.2.

### 8.3 Action queue

The persistent right rail contains pending proposals and urgent warnings.
Proposal cards show side, instrument, quantity and notional, rationale, source,
typed numeric confidence, reference-price age, current price drift, risk result,
and expiry. Approve and reject are available in paper and live modes subject to
Section 9. A proposal's drawer shows the complete position-sizing reasoning
chain (the CLI's `proposals show`), not only a one-line preview.

A **New proposal** action opens a drawer with the same expressiveness as the
CLI `propose` command: instrument, side, order type, quantity or notional
amount, bracket / stop-loss / trailing-stop exits, time in force, confidence,
group tag, and reasoning. Leaving quantity and amount empty invokes the same
automatic position sizing as the CLI. Creation follows Section 9.6.

### 8.4 Orders, executions, strategy, and risk

Orders distinguish queued, submitted, working, partially filled, filled,
cancelled, cancelled-after-partial, and rejected. One proposal can own an order
group containing parent, profit-taker, stop, and replacement orders. IB
`orderStatus` updates drive working state; `execDetails` events deduplicate fills
by execution ID; commission reports enrich fills when they arrive. Group status
aggregates children without hiding per-leg state, filled quantity, average fill
price, or cancelled remainder. Startup reconciliation queries open orders,
completed orders, and executions.

The normalized dashboard contract maps the legacy proposal status `EXECUTED` to
`ORDER_SUBMITTED`; it never presents submission as a fill. Actual order and fill
events determine execution progress.

Each working order exposes **Cancel**, subject to the risk classification in
Section 9.7. **Cancel all** requires one confirmation listing every affected
order with its classification.

Strategy rows derive enabled state from dispatchable runtime states, not merely
from an installed configuration. They show latest activity, data freshness, and
errors.
Their drawers provide enable/disable and schema-driven parameter controls. Risk
and reconciliation failures are explicit alerts; an RPC or validation failure
can never render as a green "no warnings" result.

### 8.5 CLI capability coverage

The command center is an operations console, not a replacement terminal. Every
CLI capability has an explicit disposition and delivery tag so parity gaps are
chosen rather than discovered:

- `[M1-C]` — proposal creation (`propose`), review detail (`proposals show`),
  approval and rejection (`approve`, `reject`); position close via a
  pre-filled reducing proposal (`close`); working-order cancel (`cancel`,
  `cancel-all`).
- `[M1-R]` — account, portfolio, order, execution, risk, and session state
  (`portfolio`, `account`, `status`, `orders`, `trades`, `portfolio-risk`,
  `session`) as read panels.
- `[COMPAT]` — watchlist CRUD and CSV import, strategy discovery and
  deploy-from-disk, parameter editing, and reasoning views, retained per
  Section 14.1.
- `[M2]` — charts and history; position-group management (`group ...`);
  proposal history browsing and filtering; `strategies undeploy`; read-only
  data freshness (`data status`) surfaced beside strategy health.
- **Deliberately excluded** — direct order entry (`buy`, `sell`),
  `resize-positions`, and standalone protective-order placement. Every
  dashboard-originated order passes through the proposal pipeline and the
  Section 5.6 coordinator; order placement that bypasses them is materially
  different and requires a separate future design.
- **Remains CLI** — research and development workflows: scanners (`ideas`,
  `scan`, `movers`), per-symbol research (`news`, `ratios`, `depth`,
  `snapshot`), options and forex tooling, backtesting (`backtest`, `bt-sweep`,
  `sweep`, `backtests`), universe bulk operations, and data download/refresh
  management.

## 9. Live Commands and Safety

Supported milestone-1 commands are proposal create/approve/reject, position
close via a pre-filled reducing proposal, working-order cancel, strategy
enable/disable, atomic strategy-parameter update, and account-scoped **Pause new
trading** for the exact configured account.

### 9.1 Common lifecycle

Command ceremony is proportional to the authoritative target account. Paper
proposal approval and paper-only strategy controls use one authenticated POST
carrying `command_id` and `expected_version`. They retain server revalidation,
ledger idempotency, audit, `202 Accepted`, and event-confirmed outcomes, but do
not require a signed nonce or a second confirmation. A strategy configuration
shared with an enabled live strategy is treated as live even if opened from a
paper page. Mode and account are derived server-side and cannot be selected by
the request.

Every live proposal approval and every command capable of increasing live risk
uses two stages. The command issuer creates one application `command_id` before
preflight and reuses it for confirmation and every retry:

1. Preflight returns the exact action summary and a signed, 30-second nonce bound
   to the command ID, action, parameters, `expected_version` (the authoritative
   command revision), exact IB account ID, account mode, and user session.
2. Confirmation submits that nonce, the same command ID, and the same
   `expected_version`.

Proposal rejection, **Pause new trading**, entry-order cancel (Section 9.7), and
strategy disable with verified zero exposure are immediate idempotent actions in
both modes. Exposure-owning
disable follows Section 9.3. Resuming new trading is risk-increasing and follows
the live preflight ceremony for a live account.

The authoritative service revalidates at confirmation time. A successful POST
returns `202 Accepted`, meaning received, not completed. The UI shows **Pending
confirmation** until a correlated domain event establishes the result. Timeout
or lost acknowledgement becomes **Outcome unknown - reconciling**, followed by
an authoritative refresh.

`command_id` is distinct from the transport request ID. Before validation or
side effects, the coordinator inserts it with a
canonical request hash, account, action, target, expected version, and state into
a `command_ledger` table in the trader database; only the coordinator writes it.
An exact retry returns the recorded state or outcome; this lookup precedes nonce
validation so the consumed nonce does not break safe retries. Reuse of an ID
with different input returns a conflict. Ledger and audit retention is 30 days.

Order commands are durable sagas with `RECEIVED`, `VALIDATED`, `SUBMITTING`,
`SUBMITTED`, `REJECTED`, `OUTCOME_UNKNOWN`, and `RESOLVED` states. The coordinator
binds `command_id` to IB `orderRef`. It persists the claim before calling IB and
persists broker identifiers and journal events after acknowledgement. A crash or
timeout after dispatch is reconciled by `orderRef`; it is never blindly
resubmitted. If the ledger or mandatory audit write fails before dispatch, the
command fails closed.

Compare-and-set entity revisions prevent stale approvals and lost strategy
updates. The root command ID is also the default correlation ID for resulting
events. The signed preflight nonce is single-use; consuming it and claiming the
command occur in the same transaction.

`expected_version` means the target's authoritative command revision and is used
only for the authorities listed in Section 6.1. For proposals and pause state it
equals `entity_revision`; for strategy commands it equals the payload's
`control_revision`, while `entity_revision` continues to order state events.
Proposal and pause revisions are minted by `trader_service`; strategy revisions
are minted and enforced by `strategy_service`. An intermediary never invents a
version from a timestamp or cached object. Forwarded strategy mutations carry
the root command ID into a strategy-service receipt ledger so a coordinator
retry cannot repeat the target mutation.

### 9.2 Proposal approval

Immediately before approval, the service reloads and validates:

- The proposal exists, is `PENDING`, and matches `expected_version`.
- Its expiry has not passed.
- Market/account data required by that action is fresh, subject to the
  risk-reducing exception below.
- The current risk policy permits the action.
- Broker and trader connections are healthy.
- For exposure-increasing approval, the order type is compatible with the
  current market session and current price drift does not exceed the proposal's
  recorded guard.

Proposals record `reference_price`, `reference_timestamp`, and
`max_price_drift_bps`. The system default is 50 basis points and can be made
stricter per strategy. Existing pending proposals lacking these fields are
marked `live_approval_eligible=false`; they may remain paper-only under their
recorded paper account/mode, but are expired or recreated before live commands
are enabled. They cannot be reassigned or promoted to a live account, and values
are never silently backfilled from a later quote.

A proposal becomes visible as `PENDING` only after account, account mode,
canonical `conId`, UTC expiry, reference price and timestamp, reference quote
side and feed type, drift guard, and initial revision are durably present. A
producer that cannot gather them fails creation; it never inserts an
incompletely guarded pending row and enriches it later. This shape is required in
both modes; a paper proposal may record a delayed feed, but only a live proposal
must pass the live-feed rule below.

During a continuous market session, live exposure-increasing approval requires
a live, non-delayed, non-frozen feed and an executable-side quote no older than
five seconds: ask for a buy and bid for a sell. Drift is the absolute difference
between that executable-side price and the recorded reference price. Feed
regression, halt/unknown session state, missing side, or excessive source clock
skew rejects the command. Outside the continuous session, market orders are
rejected; explicitly enabled limit-order workflows require their own current
price guard.

A position-reducing exit instead requires a current broker position and quantity
check. It is exempt from the exposure-increasing feed, session, and drift guards.
Quote staleness is shown prominently but does not by itself disable an otherwise
valid protective or closing order; the live confirmation must state that the
executable price is unknown or stale. The order must not exceed the verified
reducible quantity.

Expiry is owned by the in-process trader-service proposal component. Its startup
and periodic SQL sweep expires every stale pending row without a query limit.
Approval independently rechecks expiry and performs its final
`PENDING`/revision/account compare-and-set before dispatch, atomically changing
an expired row to `EXPIRED`. Expiry never depends on another strategy signal or
on the sweep running on time.

Live approval applies to proposals for the configured live account regardless
of origin. Strategy `auto_execute: propose` may create such proposals only when a
separate `STRATEGY_LIVE_PROPOSE_ENABLED` flag is true; it defaults false and must
populate account, reference-price, freshness, revision, and expiry fields. It
still creates only `PENDING` proposals and can never submit an order without the
approval path above. Full auto-execution remains unsupported.

The live confirmation drawer repeats side, instrument, quantity, notional, order
type, latest price, drift, warnings, and account mode. Reject is an immediate,
idempotent risk-reducing action.

### 9.3 Strategy control

Disabling a strategy with no open position, working order, or in-flight proposal
is immediate. If the strategy may own an exit or protective action, paper disable
performs the ownership/exit-manager validation in its single authenticated POST;
live disable uses a signed preflight. Both identify the exposure and confirm
which remaining service owns its exit management; the system never silently
orphans it.

Paper enable and parameter commands use Section 9.1's single authenticated POST,
but still validate the complete before/after state and compare-and-set
`control_revision`. Live enable and every live parameter mutation require the
signed two-stage before/after preflight. Parameters have a versioned explicit
schema, allowed types, finite ranges, cross-field validation, and risk-direction
metadata. Any parameter whose risk effect is unspecified is treated as
risk-increasing. AST-discovered names may inform the UI but cannot authorize a
live change.

The strategy service validates and instantiates a replacement before changing
the active instance. It writes a durable revision record containing the prior
and proposed configuration in `PREPARED` state using a compare-and-set expected
`control_revision`, stages the YAML file, switches
the runtime, atomically renames the staged file, and marks the revision
`COMMITTED`. Failure restores the prior runtime/configuration and marks
`ROLLED_BACK`. On restart, `PREPARED` recovers to the last committed revision,
while a committed revision is loaded before the service reports ready. A
strategy event is journaled only after the runtime reports the committed
revision active.

### 9.4 Pause semantics

The UI label **Pause new trading** controls a per-account, durable,
trader-service-owned `new_exposure_paused` entity with a monotonic revision,
updated time, and changing command ID. Commands set an absolute boolean; they
never toggle. Before readiness, an idempotent startup/migration step seeds every
configured account at revision 1 with reserved command ID `system:bootstrap` and
reason `account initialization`: live accounts start paused and paper accounts
start unpaused. If the row is absent or cannot be read, exposure-increasing
actions fail closed.

It is stored in the trader database as a singleton per account:

```text
trading_control_state(
  account_id PRIMARY KEY,
  new_exposure_paused BOOLEAN NOT NULL,
  revision BIGINT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  updated_by_command_id VARCHAR NOT NULL,
  updated_reason VARCHAR NOT NULL
)
```

Setting `paused=true` is immediate and idempotent even from a stale view.
Setting `paused=false` requires preflight and the exact current revision because
it increases risk. In paper mode, resume uses one authenticated POST with
`command_id` and the current revision; live resume uses the signed two-stage
preflight. The coordinator serializes pause mutation against final order
dispatch: a pause that commits first rejects a later exposure-increasing
dispatch. The low-level production order capability checks the same gate, so a
generic RPC caller cannot bypass it.

The gate still permits position-reducing exits, protective orders, risk-reducing
cancel/replace, and proposal rejection. `SignalProposer` reads the revisioned
state through typed trader RPC and suppresses exposure-increasing proposals when
paused, stale, or unavailable; verified exit proposals remain allowed. The
trader-side create-proposal and approval paths enforce the gate again if
propagation is delayed. Pending entry proposals remain visible but cannot be
approved. Existing working orders are not cancelled and positions are not
flattened; the UI warns about any still-working exposure-increasing orders.

### 9.5 Ambiguous-outcome reconciliation

An `OUTCOME_UNKNOWN` command triggers an immediate IB lookup by account and
`orderRef`, every five seconds for the first minute, and every 30 seconds for the
next 14 minutes. A periodic 30-second session reconciliation also covers process
restart and missed callbacks. A `command.updated` event in `RESOLVED` state plus
the corresponding order or fill event ends the unknown state. An unresolved
command after 15 minutes remains a critical alert, blocks another command for
the same proposal, and requires operator reconciliation; it is never converted
to failure by timeout alone.

A forwarded strategy command uses the same rule against the strategy-service
receipt ledger rather than IB. On timeout and coordinator startup, trader service
queries by root `command_id`. A `COMMITTED` or `ROLLED_BACK` receipt, including
the resulting control/state revisions, resolves and journals both the strategy
and command outcome. A missing or indeterminate receipt remains
`OUTCOME_UNKNOWN`; the coordinator does not repeat the mutation until the target
service proves whether the original command committed. Receipt reconciliation
is included in the initial immediate/5-second/30-second schedule and the
15-minute critical-alert rule.

### 9.6 Proposal creation and position close

Creating a proposal stages intent; it places no order. It therefore uses
Section 9.1's single authenticated POST in both modes — carrying a
client-generated `command_id` so a double-submit, retry, or reconnect cannot
create duplicate proposals — rather than the two-stage preflight. The
`TradingCommandCoordinator` routes creation to the same typed `create_proposal`
API that strategy signal bridging uses (Section 5.5), with the CLI path's
trading-filter check, automatic position sizing (confidence, risk level, ATR
volatility, liquidity) when no quantity or amount is supplied, and group
registration. The source is recorded as `dashboard`.

Dashboard-created proposals satisfy the guard-complete `PENDING` shape of
Section 9.2 at creation: account, account mode, canonical `conId`, UTC expiry,
reference price/timestamp/quote side/feed type, drift guard, and initial
revision come from the trader's own quote and account state, never from
browser-supplied values. If the trader lacks a sufficiently fresh quote for the
instrument, creation is refused with an explicit error; a pending row is never
inserted and enriched later. The Section 9.4 pause gate blocks
exposure-increasing creation; verified position-reducing close proposals remain
allowed while paused.

The **Close position** action (Section 8.2) creates a reducing proposal through
this same path, pre-filled from the broker-verified position. Its quantity may
not exceed the verified reducible quantity, and that limit is re-checked at
approval per Section 9.2.

### 9.7 Working-order cancel

Cancel is classified by its risk direction, not treated as uniformly safe.
Cancelling a working exposure-increasing entry order is risk-reducing:
immediate, idempotent, and available in both modes with a single authenticated
POST. Cancelling a protective order (a stop, trailing stop, or take-profit leg
attached to an open position) removes protection and is risk-increasing: it
requires the Section 9.1 ceremony for the account mode, and the confirmation
names the position left unprotected. Order-group membership (Section 8.4)
determines the classification; an order whose classification cannot be
established is treated as protective.

Cancel commands flow through the coordinator's command ledger and are
reconciled like other order commands (Section 9.5). Cancelling an order that
reached a terminal state in the meantime is a no-op that reports the
authoritative state. **Cancel all** expands to per-order commands under one
correlation ID and one confirmation that lists every affected order with its
classification.

## 10. Authentication, CSRF, and Audit

In direct host mode the dashboard binds to loopback. In Compose it binds to
`0.0.0.0` inside the container, while Compose publishes the HTTP port only on
host `127.0.0.1`; it is not published on LAN interfaces. Canonical deployment inputs
are `DASHBOARD_TOKEN_FILE` and `DASHBOARD_SESSION_SECRET_FILE`; direct
`DASHBOARD_TOKEN` and `DASHBOARD_SESSION_SECRET` values are local-development
fallbacks. `POST /session` compares the login token in constant time, is rate
limited to five failed attempts per minute, and exchanges it for a signed
`HttpOnly`, `SameSite=Strict` session cookie with a 12-hour absolute lifetime.
Logout clears it. Sessions carry a server epoch; rotating either secret and
restarting changes that epoch and invalidates existing sessions. The credential
is never accepted in a URL or written to logs. Cookies
use `Secure` when HTTPS is configured.

All dashboard pages and APIs containing account data require the session except
the login flow and minimal boolean liveness/readiness endpoints. Every mutation
requires authentication in paper and live modes, a session-bound CSRF token,
and strict origin/host validation. Startup fails if either the login token or
session-signing secret is absent.

For one release, the existing `MMR_WEB_TOKEN` name is accepted only as a
deprecated configuration alias when no canonical token is set and live commands
are disabled. It never restores `?token=` or `X-MMR-Token` authentication.
Setting legacy and canonical names together fails startup. Migration logs a
deprecation warning without values, rotates the old credential before live
commands are enabled because it may exist in URL history or logs, and removes
the alias when the legacy dashboard is retired. Compose explicitly forwards the
canonical secret-file paths.

`DASHBOARD_COMMANDS_ENABLED` and `DASHBOARD_LIVE_COMMANDS_ENABLED` both default
to false. Live commands additionally require an exact
`DASHBOARD_LIVE_ACCOUNT_ID`; a mode label or wildcard account is insufficient.

Responses set a restrictive Content Security Policy, `frame-ancestors 'none'`,
`Referrer-Policy: no-referrer`, and `X-Content-Type-Options: nosniff`. Remote
exposure is outside this single-user design; if enabled later it requires TLS
and a separate security review.

Every command creates a persistent audit record containing timestamp, command
and correlation IDs, action, target, expected version, redacted inputs,
validation result, service acknowledgement, and eventual authoritative outcome.
Secrets and raw session tokens are never logged. Failure to persist the initial
audit/ledger claim blocks dispatch. Failure after a possible broker side effect
sets `OUTCOME_UNKNOWN`, degrades command health, and starts reconciliation.

The Compose configuration explicitly forwards secret-file paths and non-secret
settings for session lifetime, command enablement, price drift, and freshness;
it never bakes or prints secret values. The internal command capability has a
separate rotating service credential from the browser token.

## 11. Failure Handling

The bridge has `starting`, `synchronizing`, `live`, `degraded`, and
`disconnected` states. Health is tracked separately for IB, ticker feed, trader
service, strategy service, the proposal repository/durable store, and
reconciliation.

On source failure, the dashboard keeps the last known state visible but marks
affected fields stale with their age. The bridge reconnects with exponential
backoff and jitter, rebuilds its fenced baseline, and changes `stream_id`.
Journal unavailability degrades the bridge; quote pressure is handled by
conflation; per-client SSE overflow affects only that client and forces its
resynchronization.

If SSE is disconnected for 15 seconds, the browser starts five-second snapshot
polling and displays a persistent degraded-connectivity banner. It returns to
SSE only after a coherent snapshot. While the realtime stream is degraded, the
client disables exposure-increasing controls; server validation remains final.

Command availability is dependency-aware:

- Approval, strategy enable, and risk-increasing changes require the relevant
  services plus fresh market/account state.
- Proposal creation requires the trader command coordinator, its durable store,
  and a fresh quote for the target instrument.
- Entry-order cancel requires the trader service and its broker path; it does
  not require fresh market data.
- **Pause new trading** remains available whenever the trader command
  coordinator and its durable store are reachable. Risk-reducing exits also
  require their broker path. Strategy disable additionally requires
  strategy-service health and the exposure preflight described above.
- Commands are never queued for delayed execution while a dependency is down.
- Error responses include a stable code, safe message, retryability, and
  correlation ID.

## 12. Operations

- `/healthz` reports only web-process liveness and exposes no dependency detail.
- `/readyz` succeeds after a coherent snapshot exists and the bridge is
  initialized; it exposes only a boolean status to unauthenticated callers.
- Authenticated `/api/health` reports every dependency's state, source-data age,
  transport lag, feed type, reconnect count, and last safe error.
- Structured logs carry stream, sequence, event, command, and correlation IDs.
- Graceful shutdown follows Section 5.1's bounded order: Uvicorn stops new
  requests and drains or cancels SSE generators within five seconds; only then
  does lifespan teardown stop producers, cancel reducers, and release source and
  command connections.

Production has exactly one supervisor for each process. `[G0]` splits the
existing Compose deployment so Compose owns separate non-root `data`, `trader`,
`strategy`, `dashboard`, and scheduler services with health checks, restart
policies, a private service network, read-only application filesystems,
ephemeral `/tmp`, and explicit resource limits. Only services that own DuckDB,
audit, or configuration state receive narrowly scoped writable volumes. This
reuses the repository's Compose and pycron mechanisms; it does not introduce
another supervisor framework.

Pycron owns scheduled one-shot reconciliation, refresh, maintenance, and backup
jobs only; it does not also launch the long-lived services owned by Compose.
`[G0]` keeps `start_mmr.sh` as a local-development launcher but changes it to
exit non-zero when any mandatory child dies; today it only exits after nearly
all children have died and does not restart them. No process appears under both
Compose and pycron supervision. The migration updates the existing pycron
templates, because the current Docker entrypoint runs `start_mmr.sh` and
therefore omits configured scheduled jobs despite pycron already having
restart-capable service entries.
Authenticated `/api/health` exposes every required process plus each scheduled
job's last success and failure state. A dashboard restart never restarts or
interrupts trading services.

Independent supervision, non-root execution, typed authenticated command RPC,
and removal of production risk-bypass capabilities are the `[G0]` gate before
the dashboard is trusted for live monitoring or control.

## 13. Testing

### 13.1 Unit tests

- Event envelope validation, normalization, ordering, and deduplication.
- Canonical instrument, position, order-group, fill, and proposal identities.
- State reducers, entity revisions, tombstones, retention, cleanup, and quote
  coalescing.
- Journal cursor, fenced snapshot, replay cursor, and stream-change behavior.
- Proposal expiry, price drift, freshness, risk, and compare-and-set validation.
- Proposal creation: guard-complete `PENDING` shape, sizing parity with the CLI
  path, duplicate-submit idempotency, trading-filter rejection, missing-quote
  refusal, pause-gate enforcement, and close-proposal quantity limits.
- Order-cancel risk classification (entry versus protective) and idempotency
  against already-terminal orders.
- Command-ledger idempotency, same-ID/different-payload conflict, saga crash
  points, one-time preflight expiry, pause semantics, session authentication,
  CSRF, and origin enforcement.
- Strategy-schema validation, staged activation, failed-swap rollback, and
  restart convergence.

### 13.2 Integration tests

Fake quote publishers, durable-journal adapters, and command services cover
fenced snapshots, disconnect/reconnect, `Last-Event-ID` replay, cursor expiry,
queue overflow, critical-event recovery, separate command connections,
disconnect/cancellation cleanup, bounded lifespan shutdown, ambiguous timeouts,
broker lookup by `orderRef`, and reconciliation. Broker
fixtures cover
parent/child orders, partial fills, cancel-after-partial, execution-ID dedup,
late commission reports, and restart recovery.

Producer contract tests drive every emission point in Section 5.4 and verify the
materialized row, exactly-once journal identity, entity revision, tombstone or
payload, fenced snapshot, and long-poll wake-up. Crash injection at every
transaction boundary proves that local state never commits without its journal
row and that a retry does not duplicate an event or fill. Quote tests cover
position-only subscription, reference-counted acquire/release, reconnect
baseline, and absence from both replay and domain FIFO.

Order-correlation tests cover fill-before-order, late `permId`, client-order-ID
reuse after restart, bracket legs, external orders, and process restart. Each
case must retain one immutable order entity and one revision stream while adding
aliases or resolving a previously unbound fill.

Broker-generation tests interleave live callbacks with snapshot callbacks,
withhold each completion marker in turn, disconnect mid-generation, and remove
entities between generations. No incomplete generation may advance readiness,
emit absence tombstones, or enable a dependent command; a complete generation
must promote once with the correct cursor and tombstones.

Regression tests prove that:

- SDK failure cannot display as approval success.
- Risk-check failure cannot display as a green pass.
- Expired or over-drift proposals cannot be approved.
- Retried requests do not duplicate orders.
- Legacy `EXECUTED` is displayed as order-submitted until fill evidence exists.
- A cash-only account retains its authoritative net liquidation value.
- Proposal confidence remains numeric and strategy enabled state reflects
  dispatchable runtime state.

Browser tests cover layout, currency labels, freshness and transport-lag states,
degraded banners, confirmation drawers, live warnings, authoritative pending
states, non-color-only alerts, keyboard/focus behavior, and polling fallback.

Negative security tests attempt unauthenticated direct RPC orders, production
risk-bypass flags, malformed typed messages, arbitrary serialized objects,
cross-account and replayed nonces, command-ID payload substitution, audit-store
failure, CSRF, hostile origins, and secret leakage.

Full-stack paper/Compose tests kill and restart the web, trader, strategy, and
event consumers at every command-saga boundary. They also exercise multiple slow
browser tabs, journal/database unavailability, broker disconnect, and host or
container restart. CI must pass unit, integration, browser, security, and
full-stack smoke gates before live-command artifacts are produced.

The existing `test` optional-dependency group is the supported host entry point;
it is not recreated. `[G0]` commits an exact CPython 3.12 patch version in
`.python-version`; `uv.lock` pins packages, not the interpreter. The canonical
developer and CI setup is:

```bash
uv sync --python "$(cat .python-version)" --frozen --extra test
uv run --frozen pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py
```

CI asserts the running interpreter exactly matches `.python-version`.

The quarantined async test is a separate required job using
`uv run --frozen pytest tests/test_ibrx_async.py --timeout=30 -q`; it does not
disappear from CI. Browser jobs first run
`uv sync --python "$(cat .python-version)" --frozen --extra test --extra browser-test`
and use a pinned Playwright/Chromium image.
Full-stack tests use the same Compose artifacts in paper/fake-broker mode with no
live credentials. Global Python packages and runtime-only `requirements.txt` are
not accepted as test-environment definitions.

### 13.3 Performance and soak targets

- With 100 active instruments at four quote updates per second, 20 domain events
  per second, and three browser tabs, critical local domain events reach a
  foreground browser within 500 ms at p95, measured from producer callback
  receipt through durable commit, long-poll wake-up, SSE delivery, and browser
  reducer application. For derived risk projections, measurement starts at the
  underlying committed mutation and includes the bounded projection debounce.
- Per-instrument quote rendering never exceeds the configured 2-5 Hz rate.
- An eight-hour representative paper session remains within all configured
  state, queue, and replay bounds; from the end of the first warm-up hour to the
  end of the run, process RSS may not grow by more than 20%, average dashboard
  CPU remains below one core, and
  there are no unhandled errors or unresolved test commands.
- Every simulated stream gap and service restart ends in either a coherent live
  state or an explicit degraded state; never a silently inconsistent state.

## 14. Rollout

The delivery workstreams expose service-side effort before scheduling starts:

| Tag | Primary owner | Changed components and deliverables | Dependency |
|---|---|---|---|
| `[S0]` | `sdk/cli` | Current `ProposalStore`, SDK approval, strategy reconciliation sweep, legacy web rendering, and focused regressions. No SSE work. | None; ships independently. |
| `[G0]` | `operations/test` | Compose/pycron ownership, non-root services, health, typed authenticated internal RPC, network isolation, and closure of production bypass capabilities. | Required before production trust. |
| `[M1-F]` | `trader_service` | Explicit DB transactions; proposal/entity schema migrations; producer callbacks; journal, materialized stores, long-poll feed, fenced snapshot, command ledger/coordinator, audit, pause row, order/fill persistence and correlation, quote coverage. | `[S0]` behavior retained; `[G0]` before production deployment. |
| `[M1-F]` | `strategy_service` | Typed proposal creation, persistent strategy revisions/receipts, pause observation, committed-state acknowledgement, and anti-entropy snapshot. | Trader typed APIs. |
| `[M1-F]` | SDK/CLI | Thin typed proposal/command/query adapters; removal of direct proposal-table writes and unrestricted production order calls. | Trader coordinator. |
| `[M1-R]` | Dashboard | Authenticated snapshot, long-poll bridge, reducer, quote conflation, quote-free replay, async SSE, operations-first UI, degraded polling fallback, and browser tests. | Fenced source foundation. |
| `[M1-C]` | `trader_service` | Dashboard and strategy integration; proposal creation, position-close, and order-cancel commands; paper commands first, then live preflight, account pinning, drift/freshness gates, resume ceremony, event-confirmed outcomes, and full audit/reconciliation. | `[M1-F]`, `[M1-R]`, soak gates. |
| `[COMPAT]` | Dashboard | Legacy reasoning views, parameter editor, watchlists/CSV, strategy discovery/deploy, side-by-side route, and migration of retained strategy mutations to the common authenticated coordinator. | Retained until Section 14.1 passes. |

Rollout order is:

0. Ship `[S0]` as an independent small PR while `[G0]` and `[M1-F]` planning
   continues.
1. Complete `[G0]`: assign each long-lived process to Compose, scheduled jobs to
   pycron, run them non-root, secure typed internal capabilities, and remove
   production bypass paths.
2. Complete `[M1-F]`: migrate proposal ownership, add every producer in Section
   5.4, command coordinator, journal, entity revisions, pause state, order/fill
   persistence, quote coverage, and fenced snapshot/long-poll APIs.
3. Deploy `[M1-R]` read-only in paper mode with snapshot plus SSE.
4. Enable `[M1-C]` authenticated proposal creation, approval, position-close,
   order-cancel, and strategy commands in paper mode; disable overlapping legacy
   proposal and strategy-control routes so each command type has one acting
   surface.
5. Pass CI, process-kill tests, and the eight-hour paper soak with simulated
   dependency failures.
6. Run both interfaces read-only for at least one complete live market session
   and complete the parity/divergence checklist in Section 14.1.
7. Enable live commands explicitly for the exact account only after configuring
   a mandatory maximum order notional and all existing broker/risk limits. There
   is no permissive fallback for a missing live limit.
8. Retire the legacy dashboard only after Section 14.1 passes, then begin `[M2]`
   charts, history, and any watchlist redesign.

### 14.1 Side-by-side verification and legacy retirement

The legacy dashboard remains at its existing route while the command center is
introduced at a distinct route or port. Both are read-only during live
comparison. Enabling command-center paper mutations disables the overlapping
legacy approve/reject, enable/disable, and parameter routes. Non-overlapping
`[COMPAT]` mutations may remain temporarily: watchlist CRUD keeps authenticated
session/CSRF and store validation, while strategy deploy-from-disk must use the
same typed coordinator, idempotency, and audit boundary as other strategy
mutations. No command type is exposed on both surfaces at once.

Retirement requires a recorded checklist:

- The eight-hour paper soak and at least one complete live read-only market
  session show no unexplained divergence.
- Account/mode, cash and net liquidation, positions, proposals, strategy state
  and parameters, risk/limits, orders, and fills reconcile to the same
  authoritative sources.
- Proposal reasoning/rationale detail, sanitized Markdown popups, approve and
  reject, strategy enable/disable, schema-driven parameter editing, strategy
  discovery and deploy-from-disk, watchlist CRUD, and CSV import are migrated or
  have an explicit retained owner.
- Rollback to the legacy read-only view is exercised.
- Health checks, runbooks, bookmarks, and operator sign-off are updated.

Watchlists and deploy-from-disk already exist, so calling them milestone 2 does
not permit their accidental removal. The legacy surface stays available until
those `[COMPAT]` capabilities are preserved elsewhere or their `[M2]`
replacement is complete.

Live commands can be disabled independently without stopping realtime
monitoring or the trading services.

The single operator records the go/no-go checklist. Any duplicate command,
unresolved command older than 15 minutes, source-coherence failure, audit write
failure, bypass-capability exposure, or violated soak threshold is an automatic
no-go or rollback trigger. Rollback disables the live-command feature flag and
leaves the dashboard read-only while trading services continue under their
existing controls.

## 15. Acceptance Criteria

- A single local operator can monitor and control the system from Layout A
  without full-page refreshes.
- The operator can complete the entire trading loop — create a proposal, review
  its sizing and risk, approve it, and watch the order work and fill — without
  the CLI.
- Every event in Section 5.4 has a tested producer, durable transaction where
  required, fenced snapshot representation, and typed long-poll delivery; the
  read-only dashboard does not depend on snapshot poll-and-diff for normal
  operation.
- Proposal approval and strategy controls work in paper and live modes under the
  defined gates.
- Trader service is the sole proposal/command/pause mutation authority; SDK,
  CLI, dashboard, and strategy processes cannot write proposal tables directly.
- The browser never infers success from an HTTP acknowledgement.
- Submitted, working, partially filled, filled, and reconciled states are
  distinguishable.
- No retry, double-click, reconnect, or ambiguous timeout creates a duplicate
  order or strategy mutation.
- CLI, SDK, and internal-network callers cannot bypass the same production risk,
  pause, idempotency, and audit boundary.
- Stale or unavailable dependencies are visible and disable only the unsafe
  commands that require them.
- SSE gaps and bridge restarts recover through bounded replay or an explicit
  snapshot resynchronization.
- Open positions receive quotes even without an armed strategy. Quote traffic is
  conflated and excluded from the replay ring and domain FIFO.
- Dashboard memory and UI update rates remain bounded during the eight-hour
  soak.
- Dashboard failure does not stop trading, and trading-service failure cannot
  leave a false healthy indicator.
- Exposure-reducing exits remain possible while new exposure is paused, and a
  missing pause state fails closed for exposure-increasing actions.
- Pause state survives trader and strategy restarts; pausing is immediate and
  idempotent, while resuming a live account requires the current pause revision
  and a risk-increasing preflight.
- No broker-sourced entity claims compare-and-set protection it does not have;
  price, freshness, identity, quantity, and broker-state revalidation protect
  those commands instead.
- The legacy dashboard is not retired until the side-by-side parity checklist,
  including existing watchlist, deployment, parameter, and reasoning features,
  is recorded as passed.
