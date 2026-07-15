# Realtime Trading Command Center

**Date:** 2026-07-15
**Status:** Design approved; written specification pending user review
**Baseline:** Repository audit at `7ad150f`; unrelated active worktree changes are outside this specification

## 1. Purpose

Replace the current periodically refreshed dashboard with a realtime, single-user
trading command center. The trading services remain authoritative. The dashboard
provides a coherent operational view and safe controls, including proposal
approval and strategy controls in both paper and live modes.

This is the first milestone. Charts and watchlists are a separate second
milestone and must not delay the command center.

## 2. Goals

- Show account, position, proposal, order, fill, strategy, risk, reconciliation,
  and dependency state with low latency.
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
- Charts, historical quote exploration, and watchlists in milestone 1.
- Mobile-first layout or a general-purpose trading workstation framework.
- Renaming every legacy proposal status in storage as part of this milestone.

## 4. Decisions

### 4.1 Realtime transport

Use an initial JSON snapshot followed by Server-Sent Events (SSE). This is a
better fit than faster full-page polling because updates are incremental and
freshness is explicit. It is simpler than a WebSocket protocol because browser
traffic is predominantly server-to-client; mutating commands remain ordinary
authenticated HTTP requests.

If SSE is unavailable, the browser falls back to snapshot polling. This fallback
is a degraded mode, not an alternative normal mode.

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
input to a bounded, conflated quote queue. Domain updates are consumed by durable
journal cursor. One reducer task owns state mutation. Snapshot readers obtain an
immutable copy, so templates and API handlers never observe a partially applied
event.

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

Milestone 1 adds a trader-service-owned, append-only `DomainEventJournal` and a
fenced `snapshot_with_cursor` API. Account, position, proposal, command, order,
fill, risk, and reconciliation mutations append their domain event in the same
durable transaction as their local state change. One read transaction returns
the baseline and its maximum included journal cursor. The journal assigns a
stable event ID, source cursor, entity revision, and tombstone for deletions.
The trader service is the only writer to this journal and exposes it through
typed APIs; the dashboard never opens its DuckDB file.

Strategy state exposes its own persistent monotonic revision. The trader service
records acknowledged strategy changes in the journal and periodically compares
them with a revisioned strategy snapshot so a lost acknowledgement is repaired.
IB order and execution callbacks are persisted and journaled before they are
exposed to dashboard consumers. A lightweight live notification may wake
consumers, but consumers always read by journal cursor; notification loss cannot
lose state.

This source contract is a prerequisite for claiming a coherent realtime view.
A source without a revisioned snapshot or replayable journal is marked
degraded and cannot enable dependent live commands.

### 5.5 Authoritative command boundary

All order-producing, risk-limit-changing, and strategy-mutating production paths
pass through a `TradingCommandCoordinator` in `trader_service`. Dashboard, CLI,
and SDK callers
may provide different user experiences, but none can bypass the same command
ledger, risk checks, pause gate, and idempotency rules. Caller-controlled risk
bypass flags and unrestricted direct-order RPC methods are not available on the
production command capability. Risk-limit administration is outside the
dashboard and uses a distinct, narrower admin capability.

Production service RPC uses JSON validated against allowlisted typed schemas
plus an HMAC service credential, timestamp, and replay nonce. No process
reachable from another container accepts arbitrary `dill`, even for read calls;
legacy Python-object transport is limited to offline/test processes isolated
from broker credentials. Only the private service network can reach RPC ports,
and containers run as unprivileged users.

## 6. Event Contract

Every browser-visible event is JSON with this envelope:

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
- `source_cursor` is the durable journal position; it is nullable for quotes.
- `entity_revision` is required for durable domain entities, rejects
  regressions, and makes snapshot fencing possible. Quotes use monotonic market
  time/tick identity and may leave it null.
- `correlation_id` connects a command to all resulting events. It is nullable
  for external market events.
- `account_id` is required for account-bound entities and commands.
- Timestamps are UTC. The UI displays source age and transport lag separately.
  Command freshness uses source/market time, feed quality, and exchange session;
  receive time alone can never make an old buffered quote fresh.
- Domain payloads are typed JSON. Internal `dill` or arbitrary Python objects
  never cross the web boundary.

Initial event types are `account.updated`, `quote.updated`, `position.updated`,
`proposal.updated`, `command.updated`, `order.updated`, `fill.received`,
`strategy.updated`, `risk.updated`, `reconciliation.updated`, and
`service.health`.

Entity keys are canonical, never bare display symbols. Instruments use IB
`conId` plus contract metadata; positions use account plus `conId`; orders use
account plus IB `permId` when available, with an alias from the temporary
client/order ID; fills use account plus IB execution ID. Proposal-to-order joins
use an explicit order-group relation.

Account, position, proposal, command, order, fill, risk, strategy, and
reconciliation events are appended to the durable journal before publication.
Transient service health is derived from heartbeats and revisioned source
snapshots. Quotes are coalesced per canonical
instrument at a configurable 2-5 Hz, with 4 Hz as the default. A conflated quote
map replaces older queued quotes for the same instrument. Domain events are
re-read from their journal cursor rather than held indefinitely in an in-memory
queue.

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

On startup or domain-source reconnection the bridge calls
`snapshot_with_cursor`, installs that revisioned baseline, and tails the journal
strictly after its returned cursor. Tombstones remove entities. Events at or
below an entity revision are idempotently ignored. Quotes use a separate latest
snapshot followed by conflated updates; a quote gap affects freshness but cannot
erase a durable order, fill, proposal, or command outcome. If a source cannot
provide its fence, the affected panel remains degraded instead of guessing.

Each SSE client has a queue capped at 1,000 events. A slow browser or background
tab is never allowed to block source consumption. On client overflow, the server
clears that client queue, emits `resync_required` when possible, and closes the
stream; reconnect then obtains a fresh snapshot. Multiple tabs therefore remain
bounded independently.

State bounds are:

- Latest quote only for each active canonical instrument. An instrument is
  active while referenced by an open position/order, pending proposal, or
  running strategy and expires from the quote cache ten minutes after its last
  reference disappears.
- All currently active positions, proposals, and orders.
- At most 500 terminal proposals, 500 terminal orders, and 500 fills, each with
  a 24-hour TTL.
- A replay ring capped at 10,000 events or five minutes, whichever is reached
  first.
- Cleanup every 60 seconds.

The durable domain journal retains 30 days plus the newest complete snapshot
checkpoint. Compaction never removes an event required by an active consumer
cursor. A bridge returning after retention has elapsed must take a full fenced
snapshot rather than request old deltas.

Historical chart data does not use this ring and belongs to milestone 2.

## 8. Command Center

### 8.1 Status and account summary

The top bar shows a prominent `LIVE` or `PAPER` badge, the exact IB account, IB,
ticker, trader, strategy, event-bridge, and browser-stream health, and the latest authoritative
event time. Every panel and row still shows its own data age; a recent quote
cannot make stale account or strategy state appear fresh. Summary cards show net liquidation, daily P&L, exposure, buying
power, and margin cushion. Net liquidation comes from account values even when
the account has no positions; a flat cash account must not display zero by
construction.

### 8.2 Positions

Positions are the largest panel. Each row shows quantity, average and current
price, instrument and account currency, base-currency conversion, unrealized
and daily P&L, exposure, protection state, and freshness.
Selecting a position opens a drawer with related orders, fills, proposals, risk
information, and reconciliation findings.

### 8.3 Action queue

The persistent right rail contains pending proposals and urgent warnings.
Proposal cards show side, instrument, quantity and notional, rationale, source,
typed numeric confidence, reference-price age, current price drift, risk result,
and expiry. Approve and reject are available in paper and live modes subject to
Section 9.

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

Strategy rows derive enabled state from dispatchable runtime states, not merely
from an installed configuration. They show latest activity, data freshness, and
errors.
Their drawers provide enable/disable and schema-driven parameter controls. Risk
and reconciliation failures are explicit alerts; an RPC or validation failure
can never render as a green "no warnings" result.

## 9. Live Commands and Safety

Supported milestone-1 commands are proposal approve/reject, strategy
enable/disable, atomic strategy-parameter update, and global **Pause new
trading**.

### 9.1 Common lifecycle

Every live proposal approval and every potentially risk-increasing command uses
two stages:

1. Preflight returns the exact action summary and a signed, 30-second nonce bound
   to the action, parameters, entity version, exact IB account ID, account mode,
   and user session.
2. Confirmation submits that nonce with a client-generated `command_id` and the
   expected entity version.

The authoritative service revalidates at confirmation time. A successful POST
returns `202 Accepted`, meaning received, not completed. The UI shows **Pending
confirmation** until a correlated domain event establishes the result. Timeout
or lost acknowledgement becomes **Outcome unknown - reconciling**, followed by
an authoritative refresh.

Before validation or side effects, the coordinator inserts `command_id`, a
canonical request hash, account, action, target, expected version, and state into
a durable command ledger. An exact retry returns the recorded state or outcome;
this lookup precedes nonce validation so the consumed nonce does not break safe
retries. Reuse of an ID with different input returns a conflict. Ledger and audit
retention is 30 days.

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

### 9.2 Proposal approval

Immediately before approval, the service reloads and validates:

- The proposal exists, is `PENDING`, and matches `expected_version`.
- Its expiry has not passed.
- Market/account data required by that action is fresh, subject to the
  risk-reducing exception below.
- The current risk policy permits the action.
- Broker and trader connections are healthy.
- The order type is compatible with the current market session.
- Current price drift does not exceed the proposal's recorded guard.

Proposals record `reference_price`, `reference_timestamp`, and
`max_price_drift_bps`. The system default is 50 basis points and can be made
stricter per strategy. Existing pending proposals lacking these fields are
quarantined from live approval and expired or recreated before live commands are
enabled; values are never silently backfilled from a later quote.

During a continuous market session, live exposure-increasing approval requires
a live, non-delayed, non-frozen feed and an executable-side quote no older than
five seconds: ask for a buy and bid for a sell. Drift is the absolute difference
between that executable-side price and the recorded reference price. Feed
regression, halt/unknown session state, missing side, or excessive source clock
skew rejects the command. Outside the continuous session, market orders are
rejected; explicitly enabled limit-order workflows require their own current
price guard.

A position-reducing exit instead requires a current broker position and quantity
check. Quote staleness is shown prominently but does not by itself disable an
otherwise valid protective or closing order; the live confirmation must state
that the executable price is unknown or stale. The order must not exceed the
verified reducible quantity.

Expiry is authoritative at the proposal service: a periodic sweep transitions
stale pending proposals, and the approval path atomically changes an expired
`PENDING` proposal to `EXPIRED`. Expiry must not depend on another strategy
signal arriving.

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
is immediate. If the strategy may own an exit or protective action, disable
requires a preflight that identifies the exposure and confirms which remaining
service owns its exit management; the system never silently orphans it.

Enabling a strategy and every live parameter mutation require a before/after
preflight. Parameters have a versioned explicit schema, allowed types, finite
ranges, cross-field validation, and risk-direction metadata. Any parameter whose
risk effect is unspecified is treated as risk-increasing. AST-discovered names
may inform the UI but cannot authorize a live change.

The strategy service validates and instantiates a replacement before changing
the active instance. It writes a durable revision record containing the prior
and proposed configuration in `PREPARED` state using a compare-and-set expected
revision, stages the YAML file, switches
the runtime, atomically renames the staged file, and marks the revision
`COMMITTED`. Failure restores the prior runtime/configuration and marks
`ROLLED_BACK`. On restart, `PREPARED` recovers to the last committed revision,
while a committed revision is loaded before the service reports ready. A
strategy event is journaled only after the runtime reports the committed
revision active.

### 9.4 Pause semantics

The UI label **Pause new trading** controls a durable, trader-service-owned
`new_exposure_paused` gate. It blocks every exposure-increasing order regardless
of whether it came from the dashboard, CLI, SDK, or a strategy. If the gate state
cannot be read, exposure-increasing actions fail closed.

The gate still permits position-reducing exits, protective orders, risk-reducing
cancel/replace, and proposal rejection. Strategy entry proposals are suppressed
when the strategy service observes the gate, but trader-side enforcement remains
authoritative if that propagation is delayed. Pending entry proposals remain
visible but cannot be approved. Existing working orders are not cancelled and
positions are not flattened; the UI warns about any still-working
exposure-increasing orders.

### 9.5 Ambiguous-outcome reconciliation

An `OUTCOME_UNKNOWN` command triggers an immediate IB lookup by account and
`orderRef`, every five seconds for the first minute, and every 30 seconds for the
next 14 minutes. A periodic 30-second session reconciliation also covers process
restart and missed callbacks. A `command.updated` event in `RESOLVED` state plus
the corresponding order or fill event ends the unknown state. An unresolved
command after 15 minutes remains a critical alert, blocks another command for
the same proposal, and requires operator reconciliation; it is never converted
to failure by timeout alone.

## 10. Authentication, CSRF, and Audit

The service binds to loopback by default. The operator supplies a high-entropy
`DASHBOARD_TOKEN` and an independent session-signing secret through the deployment
secret mechanism. `POST /session` compares the token in constant time, is rate
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
service, strategy service, proposal store, and reconciliation.

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
- Graceful shutdown rejects new commands, closes SSE clients, and then releases
  source and command connections.

The web runs in its own non-root Compose service/container with a healthcheck,
restart policy, private service network, read-only application filesystem,
ephemeral `/tmp`, and explicit CPU/memory limits. Only the services that own
DuckDB, audit, or configuration state receive narrowly scoped writable volumes.
Trader, strategy, and scheduled-job
processes are also independently supervised. This replaces the current
unsupervised shell fan-out: one child process failing must not be silently
ignored, and required scheduled reconciliation/maintenance cannot be omitted.
A dashboard restart never restarts or interrupts the trading services.

Independent supervision, non-root execution, typed authenticated command RPC,
and removal of production risk-bypass capabilities are rollout gate 0, before
the dashboard is trusted for live monitoring or control.

## 13. Testing

### 13.1 Unit tests

- Event envelope validation, normalization, ordering, and deduplication.
- Canonical instrument, position, order-group, fill, and proposal identities.
- State reducers, entity revisions, tombstones, retention, cleanup, and quote
  coalescing.
- Journal cursor, fenced snapshot, replay cursor, and stream-change behavior.
- Proposal expiry, price drift, freshness, risk, and compare-and-set validation.
- Command-ledger idempotency, same-ID/different-payload conflict, saga crash
  points, one-time preflight expiry, pause semantics, session authentication,
  CSRF, and origin enforcement.
- Strategy-schema validation, staged activation, failed-swap rollback, and
  restart convergence.

### 13.2 Integration tests

Fake quote publishers, durable-journal adapters, and command services cover
fenced snapshots, disconnect/reconnect, `Last-Event-ID` replay, cursor expiry,
queue overflow, critical-event recovery, separate command connections,
ambiguous timeouts, broker lookup by `orderRef`, and reconciliation. Broker
fixtures cover
parent/child orders, partial fills, cancel-after-partial, execution-ID dedup,
late commission reports, and restart recovery.

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

The repository gains a documented, reproducible test dependency set because the
audited local environment does not currently include `pytest` and its test
extras.

### 13.3 Performance and soak targets

- With 100 active instruments at four quote updates per second, 20 domain events
  per second, and three browser tabs, critical local domain events reach a
  foreground browser within 500 ms at p95.
- Per-instrument quote rendering never exceeds the configured 2-5 Hz rate.
- An eight-hour representative paper session remains within all configured
  state, queue, and replay bounds; from the end of the first warm-up hour to the
  end of the run, process RSS may not grow by more than 20%, average dashboard
  CPU remains below one core, and
  there are no unhandled errors or unresolved test commands.
- Every simulated stream gap and service restart ends in either a coherent live
  state or an explicit degraded state; never a silently inconsistent state.

## 14. Rollout

0. Separate and supervise processes, run them non-root, secure the typed internal
   command capability, remove production bypass paths, and correct approval,
   risk, flat-account, confidence, strategy-state, expiry, and order/fill
   semantics.
1. Add the command coordinator, durable journal, entity revisions, order/fill
   persistence, and fenced snapshot API.
2. Deploy the read-only command center with snapshot plus SSE.
3. Enable authenticated proposal and strategy commands in paper mode.
4. Pass CI, process-kill tests, and the eight-hour paper soak with simulated
   dependency failures.
5. Run the command center read-only against the live account alongside the
   existing interface.
6. Enable live commands explicitly for the exact account only after configuring
   a mandatory maximum order notional and all existing broker/risk limits. There
   is no permissive fallback for a missing live limit.
7. Begin milestone 2 for charts and watchlists.

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
- Proposal approval and strategy controls work in paper and live modes under the
  defined gates.
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
- Dashboard memory and UI update rates remain bounded during the eight-hour
  soak.
- Dashboard failure does not stop trading, and trading-service failure cannot
  leave a false healthy indicator.
- Exposure-reducing exits remain possible while new exposure is paused, and a
  missing pause state fails closed for exposure-increasing actions.
