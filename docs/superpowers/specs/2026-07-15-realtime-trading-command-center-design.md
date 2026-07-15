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
IB / trader_service / strategy_service / proposal store / reconciliation
                                  |
                                  v
                       DashboardEventBridge
                                  |
                                  v
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
                 DashboardCommandGateway (separate SDK client)
                                    |
                                    v
                        authoritative trading services
```

### 5.1 `DashboardEventBridge`

The bridge is the dashboard's only subscriber to ticker and domain streams. It
normalizes quotes, account changes, positions, proposal transitions, order and
fill changes, strategy status, risk state, reconciliation results, and service
health.

Blocking ZMQ reads run outside the ASGI event loop and hand normalized input to
a bounded queue. One reducer task owns state mutation. Snapshot readers obtain
an immutable copy, so templates and API handlers never observe a partially
applied event.

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

## 6. Event Contract

Every browser-visible event is JSON with this envelope:

```json
{
  "schema_version": 1,
  "stream_id": "01J...",
  "sequence": 1842,
  "event_id": "01J...",
  "event_type": "proposal.updated",
  "entity_type": "proposal",
  "entity_id": "438",
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
- `event_id` permits deduplication where a source redelivers data.
- `correlation_id` connects a command to all resulting events. It is nullable
  for external market events.
- Timestamps are UTC. Source time supports market diagnostics; receive time
  drives UI freshness.
- Domain payloads are typed JSON. Internal `dill` or arbitrary Python objects
  never cross the web boundary.

Initial event types are `account.updated`, `quote.updated`, `position.updated`,
`proposal.updated`, `order.updated`, `fill.received`, `strategy.updated`,
`risk.updated`, `reconciliation.updated`, and `service.health`.

Proposal, order, fill, risk, strategy, and health events are emitted immediately.
Quotes are coalesced per symbol at a configurable 2-5 Hz, with 4 Hz as the
default. Backpressure replaces an older queued quote for the same symbol but
never silently drops a critical domain event.

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

On startup or source reconnection the bridge subscribes and buffers, loads an
authoritative baseline, applies buffered events newer than each entity's
baseline version, and only then reports itself live. If event ordering cannot be
established, it remains degraded and retries the synchronization instead of
guessing.

State bounds are:

- Latest quote only for each active symbol.
- All currently active positions, proposals, and orders.
- At most 500 terminal proposals, 500 terminal orders, and 500 fills, each with
  a 24-hour TTL.
- A replay ring capped at 10,000 events or five minutes, whichever is reached
  first.
- Cleanup every 60 seconds.

Historical chart data does not use this ring and belongs to milestone 2.

## 8. Command Center

### 8.1 Status and account summary

The top bar shows a prominent `LIVE` or `PAPER` badge, IB, ticker, trader,
strategy, event-bridge, and browser-stream health, and the latest authoritative
event time. Summary cards show net liquidation, daily P&L, exposure, buying
power, and margin cushion.

### 8.2 Positions

Positions are the largest panel. Each row shows quantity, average and current
price, unrealized and daily P&L, exposure, protection state, and freshness.
Selecting a position opens a drawer with related orders, fills, proposals, risk
information, and reconciliation findings.

### 8.3 Action queue

The persistent right rail contains pending proposals and urgent warnings.
Proposal cards show side, symbol, quantity and notional, rationale, source,
reference-price age, current price drift, risk result, and expiry. Approve and
reject are available in paper and live modes subject to Section 9.

### 8.4 Orders, executions, strategy, and risk

Orders distinguish queued, submitted, working, partially filled, filled,
cancelled, and rejected. The normalized dashboard contract maps the legacy
proposal status `EXECUTED` to `ORDER_SUBMITTED`; it never presents submission as
a fill. Actual order and fill events determine execution progress.

Strategy rows show enabled state, latest activity, data freshness, and errors.
Their drawers provide enable/disable and schema-driven parameter controls. Risk
and reconciliation failures are explicit alerts; an RPC or validation failure
can never render as a green "no warnings" result.

## 9. Live Commands and Safety

Supported milestone-1 commands are proposal approve/reject, strategy
enable/disable, atomic strategy-parameter update, and global **Pause new
trading**.

### 9.1 Common lifecycle

Risk-increasing commands use two stages:

1. Preflight returns the exact action summary and a signed, 30-second nonce bound
   to the action, parameters, entity version, account mode, and user session.
2. Confirmation submits that nonce with a client-generated `command_id` and the
   expected entity version.

The authoritative service revalidates at confirmation time. A successful POST
returns `202 Accepted`, meaning received, not completed. The UI shows **Pending
confirmation** until a correlated domain event establishes the result. Timeout
or lost acknowledgement becomes **Outcome unknown - reconciling**, followed by
an authoritative refresh.

`command_id` is stored and enforced at the service boundary so browser retries
cannot create duplicate orders or configuration mutations. Compare-and-set
entity versions prevent stale approvals and lost strategy updates. The root
command ID is also the default correlation ID for resulting events.

### 9.2 Proposal approval

Immediately before approval, the service reloads and validates:

- The proposal exists, is `PENDING`, and matches `expected_version`.
- Its expiry has not passed.
- Market/account data required by risk checks is fresh.
- The current risk policy permits the action.
- Broker and trader connections are healthy.
- The order type is compatible with the current market session.
- Current price drift does not exceed the proposal's recorded guard.

Proposals record `reference_price`, `reference_timestamp`, and
`max_price_drift_bps`. The system default is 50 basis points and can be made
stricter per strategy. Live approval rejects proposals lacking these fields.
Required quote freshness defaults to five seconds during an active market
session.

Expiry is authoritative at the proposal service: a periodic sweep transitions
stale pending proposals, and the approval path atomically changes an expired
`PENDING` proposal to `EXPIRED`. Expiry must not depend on another strategy
signal arriving.

The live confirmation drawer repeats side, symbol, quantity, notional, order
type, latest price, drift, warnings, and account mode. Reject is an immediate,
idempotent risk-reducing action.

### 9.3 Strategy control

Disabling a strategy is immediate when the strategy service is reachable.
Enabling a strategy or increasing a risk-related parameter requires a preflight
and before/after confirmation. Parameters have an explicit schema, ranges, and
types. The service validates the complete new configuration and applies it
atomically under an expected version; failure preserves the prior configuration.

**Pause new trading** disables new signal-to-proposal actions and new approvals.
It does not cancel working orders, disable market-data collection, or flatten
positions. Those materially different actions require separate future designs.

## 10. Authentication, CSRF, and Audit

The service binds to loopback by default. The operator supplies a high-entropy
`DASHBOARD_TOKEN` through the environment and exchanges it for a signed
`HttpOnly`, `SameSite=Strict` session cookie. The credential is never placed in
an SSE URL. Cookies use `Secure` when HTTPS is configured.

Mutating requests require a session-bound CSRF token and strict origin/host
validation. If `DASHBOARD_LIVE_COMMANDS_ENABLED=true` without authentication
configured, application startup fails. Remote exposure is outside this
single-user design; if enabled later it requires TLS and a separate security
review.

Every command creates a persistent audit record containing timestamp, command
and correlation IDs, action, target, expected version, redacted inputs,
validation result, service acknowledgement, and eventual authoritative outcome.
Secrets and raw session tokens are never logged.

## 11. Failure Handling

The bridge has `starting`, `synchronizing`, `live`, `degraded`, and
`disconnected` states. Health is tracked separately for IB, ticker feed, trader
service, strategy service, proposal store, and reconciliation.

On source failure, the dashboard keeps the last known state visible but marks
affected fields stale with their age. The bridge reconnects with exponential
backoff and jitter, resubscribes, rebuilds its baseline, and changes
`stream_id`. Queue overflow marks the bridge degraded and forces resynchronization.

If SSE is disconnected for 15 seconds, the browser starts five-second snapshot
polling and displays a persistent degraded-connectivity banner. It returns to
SSE only after a coherent snapshot.

Command availability is dependency-aware:

- Approval, strategy enable, and risk-increasing changes require the relevant
  services plus fresh market/account state.
- Strategy disable and **Pause new trading** remain available whenever the
  strategy service is reachable.
- Commands are never queued for delayed execution while a dependency is down.
- Error responses include a stable code, safe message, retryability, and
  correlation ID.

## 12. Operations

- `/healthz` reports only web-process liveness.
- `/readyz` succeeds after a coherent snapshot exists and the bridge is
  initialized.
- `/api/health` reports every dependency's state, freshness, reconnect count,
  and last safe error.
- Structured logs carry stream, sequence, event, command, and correlation IDs.
- Graceful shutdown rejects new commands, closes SSE clients, and then releases
  source and command connections.

The web, trader, and strategy processes must be supervised independently, with
restart policies and dependency health visible in the command center. This
replaces the current unsupervised shell fan-out: one child process failing must
not be silently ignored. A dashboard restart never restarts or interrupts the
trading services.

## 13. Testing

### 13.1 Unit tests

- Event envelope validation, normalization, ordering, and deduplication.
- State reducers, entity versions, retention, cleanup, and quote coalescing.
- Replay cursor and stream-change behavior.
- Proposal expiry, price drift, freshness, risk, and compare-and-set validation.
- Command idempotency, signed preflight expiry, session authentication, CSRF,
  and origin enforcement.

### 13.2 Integration tests

Fake ZMQ publishers and command services cover startup buffering, coherent
snapshots, disconnect/reconnect, `Last-Event-ID` replay, cursor expiry, queue
overflow, critical-event preservation, separate command connections, ambiguous
timeouts, and reconciliation.

Regression tests prove that:

- SDK failure cannot display as approval success.
- Risk-check failure cannot display as a green pass.
- Expired or over-drift proposals cannot be approved.
- Retried requests do not duplicate orders.
- Legacy `EXECUTED` is displayed as order-submitted until fill evidence exists.

Browser tests cover layout, freshness, degraded banners, confirmation drawers,
live warnings, authoritative pending states, and polling fallback.

The repository gains a documented, reproducible test dependency set because the
audited local environment does not currently include `pytest` and its test
extras.

### 13.3 Performance and soak targets

- Critical local domain events reach the browser within 500 ms at p95.
- Per-symbol quote rendering never exceeds the configured 2-5 Hz rate.
- An eight-hour representative paper session remains within all configured
  state, queue, and replay bounds.
- Every simulated stream gap and service restart ends in either a coherent live
  state or an explicit degraded state; never a silently inconsistent state.

## 14. Rollout

1. Correct the approval-result, risk-presentation, expiry, and order/fill status
   semantics protected by regression tests.
2. Deploy the read-only command center with snapshot plus SSE.
3. Enable proposal and strategy commands in paper mode.
4. Complete an eight-hour paper soak including simulated dependency failures.
5. Run the new command center read-only against the live account alongside the
   existing interface.
6. Enable live commands explicitly with authentication, preflight, idempotency,
   and conservative broker/risk limits.
7. Begin milestone 2 for charts and watchlists.

Live commands can be disabled independently without stopping realtime
monitoring or the trading services.

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
- Stale or unavailable dependencies are visible and disable only the unsafe
  commands that require them.
- SSE gaps and bridge restarts recover through bounded replay or an explicit
  snapshot resynchronization.
- Dashboard memory and UI update rates remain bounded during the eight-hour
  soak.
- Dashboard failure does not stop trading, and trading-service failure cannot
  leave a false healthy indicator.

