# Command Plane Activation (M1-C / M1-F3 integration gate)

**Date:** 2026-07-18
**Status:** draft, rev 2 — incorporates the runtime-integration review (six
blocking corrections verified against the code). This is the gate that must
pass before `DASHBOARD_COMMANDS_ENABLED=true` is set against a live trader.

Closes command-plane findings #1–#5 from the 2026-07-18 review. All are
**latent** (the authority is unwired and commands are off), so these are the
safety + integration criteria for turning the command plane on, not
live-exploitable bugs today. Read-path findings #6–#9 are fixed (commit
`1532606`) and out of scope — except one coupling noted in **C3**.

## Problem

The command router, gateway, coordinator, and services exist, but the running
trader **command socket (42102) registers only `record_state_acknowledged`**.
Every user-facing method (`preflight_command`, `create_proposal`,
`approve_proposal`/`reject_proposal`, `set_trading_pause`, strategy controls) is
absent, so enabling `DASHBOARD_COMMANDS_ENABLED` exposes the UI and disables
legacy mutations but the replacements can't execute (`METHOD_NOT_ALLOWED`).
`trading_runtime.py:436-440` and `trader_service.py:139-141` state this is a
deliberate, deferred integration gate.

## Current state (verified against code)

- Command server gets a **separate** registry with only
  `register_strategy_state_ingest` (`trading_runtime.py:441-448`); query/feed
  get `production_registry`. `register_command_authority` is gated off in
  `build_production_registry` (`trading_runtime.py:411-416`) and, even if run,
  registers onto the query/feed registry — not the command socket.
- The authority stack is real but constructed **only in tests** with fake ports
  (`tests/integration/test_command_authority.py::_Stack`): `FakeNonceGate`,
  `FakeOrders`, `FakeStrategyPort`, `SimpleNamespace` risk/quotes/broker/positions.
- The production dispatch adapter is partial: `TradingRuntimeOrderDispatch.cancel()`
  raises `NotImplementedError` (`trading_runtime.py:2186`) and
  `find_by_order_ref()` returns `[]` (`trading_runtime.py:2190`).
- Approval risk today runs with **default zero** inputs
  (`command_coordinator.py:1483`); the dispatch path re-checks with real values
  but **degrades failed reads to 0 and proceeds** (daily P&L,
  `trading_runtime.py:1323`; leverage skipped if the what-if fails, `:1280`).
- `get_command` is on the **query** role (`production_api.py:862`) but the
  dashboard gateway's `_call` uses a **command**-socket client
  (`gateway.py:77,100,237`).
- `POST /api/preflight` **403s unless `live_commands_enabled`**
  (`routes_commands.py:712`), yet the coordinator's approve requires a nonce
  (`requires_preflight=True`).
- Compose passes **no** `DASHBOARD_LIVE_*` / command-policy env to the trader
  container (`docker-compose.yml`; only a "commands disabled" comment at :342).

## Decision

Wire the full authority into the live trader and register it on the command
socket, with **server-authoritative** enforcement (never trusting the browser,
the request shape, or caller-supplied limits) and **fail-closed** everywhere.
Paper-first, soak-gated. Full-auto stays a hard trader-side refusal.

---

## Blocking runtime constraints (what fake-port tests hide)

These are hard preconditions folded into the phases below. Each has an
acceptance test.

### C1 — Command handlers must not block the trader event loop

`TypedRpcServer` calls the handler **synchronously on its asyncio loop**
(`typed_rpc.py:899`; it only `await`s if the handler returns an awaitable).
`TradingRuntimeOrderDispatch.submit()` schedules `place_expressive_order` back
onto that same loop and **blocks** on `future.result()`
(`trading_runtime.py:2159-2167`). A sync command handler that reaches dispatch
therefore deadlocks the loop — the order coroutine can't run → timeout →
`OUTCOME_UNKNOWN` → wedged command path.

**Required:** every blocking coordinator handler is `async` and offloads the
blocking work — `await asyncio.to_thread(coordinator.execute, request)` — so the
trader loop stays free to run the dispatched order coroutine. Same rule for any
preflight/approval handler making blocking DuckDB, broker, or strategy-service
calls. (Mirrors the read-path `read_domain_events` fix already in the codebase.)

### C2 — Policy + limits are trader-owned config, not dashboard settings

`DASHBOARD_LIVE_ACCOUNT_ID`, `DASHBOARD_LIVE_MAX_ORDER_NOTIONAL`, and
`DASHBOARD_LIVE_COMMANDS_ENABLED` are dashboard-layer settings and are **not
passed to the trader**. Enforcement at the trader requires a **trader-owned**
policy, e.g. in `trader.yaml`:

```yaml
command_authority:
  enabled: false          # register the authority at all
  live_enabled: false     # allow risk-increasing commands on a live account
  live_account_id: U...    # must equal the trader's configured IB account
  max_order_notional: 25000
  max_drift_bps: 50
```

The dashboard `DASHBOARD_COMMANDS_ENABLED` flag stays a **separate UI kill
switch**, never the enforcement source. Trader startup **rejects contradictory
config** (e.g. `live_enabled: true` with `live_account_id` != the trader's
account, or `enabled: true` on a live account without an explicit live opt-in).
Compose must plumb this policy into the trader container.

### C3 — One registry; `get_command`/capabilities reachable by the gateway's client

Register the authority on **one shared `production_registry`** served by the
query, command, and feed servers (role-based resolution already prevents
cross-role access — the feed server exposes only feed methods, etc.). This fixes
the `get_command`-on-query vs command-client mismatch in one move, and removes
the current command-registry-of-one (`record_state_acknowledged`).

> Coupling: this same mismatch breaks the read-path **#9** ledger reconcile
> already shipped — `/api/commands/{id}` → `gateway.get_command()` → command
> client → `get_command` (query) = `METHOD_NOT_ALLOWED`. Unifying the registry
> repairs both; until then #9's ledger fallback is inert whenever the SSE path
> is also down. (Alternative: give the gateway a dedicated query client for
> `get_command`/`get_command_capabilities` — but the shared registry is simpler
> and matches the existing model.)

### C4 — Capability manifest derived from real, constructed ports

Phase 0 must **enumerate** the production adapters and their readiness
contracts, and **register only commands whose ports are real**:

| Port | Status today | Gate |
|------|--------------|------|
| OrderDispatchPort.submit | real (`trading_runtime.py:2159`) | ok for approve/create |
| OrderDispatchPort.cancel | **stub** (`:2186` NotImplementedError) | **do not advertise cancel** |
| OrderStateView.find_by_order_ref | **stub** (`:2190` returns []) | **do not advertise approval reconciliation** |
| QuoteAuthority (executable_quote) | needs prod adapter (pubsub/snapshot) | required for risk/notional |
| PositionAuthority (reducible qty, position value) | needs prod adapter | required for reduce/concentration |
| BrokerHealthPort (is_ready, net_liq, daily P&L, open orders, what-if margin) | partial | required, fail-closed (C-risk) |
| StrategyControlPort (enable/disable/update) | typed client → strategy_service | required for strategy commands |
| CriticalAlertPort | needs prod adapter | required for saga alerts |

`get_command_capabilities` returns the manifest **derived from the actually
constructed dependencies**, not a static desired list. Cancel + approval
reconciliation stay unregistered (and absent from the manifest) until their
adapters are real and tested.

### C5 — Preflight nonce: full request binding + explicit paper contract

The nonce must bind **all** of: `command_id`, authoritative account + mode,
session fingerprint, action, target, `expected_version`, **canonicalized
command body/params**, expiry, and one-time-consumed state — so a nonce can't be
replayed for a materially different request against the same target. The
coordinator consumes it atomically in the RECEIVED transaction.

**Paper contract (decision required, recommend option A):**
- **A (recommended):** server-issued preflight for **every executing approval**,
  paper included, with a lighter/shorter paper confirmation UI. Requires
  changing `/api/preflight` (`routes_commands.py:712`), which currently 403s
  unless `live_commands_enabled`, to issue paper nonces too.
- B: a separately-defined, unforgeable paper confirmation mechanism distinct
  from the live nonce.

Either way, the current state — paper UI approves with no nonce while the
coordinator requires one — must be resolved explicitly, not left implicit.

### C6 — Command-authority readiness is trader-owned

`/readyz` is the **dashboard's** LB-drain flag; the trader doesn't own it. Define:
- Trader **fails startup** when `command_authority.enabled` but the served
  registry is incomplete (a declared command's port is missing/stub).
- Trader exposes a typed `get_command_authority_health` query (registered,
  ready-commands list, missing ports).
- Dashboard `/readyz` incorporates that health **only when the command UI is
  enabled**.
- Compose healthchecks test **typed command capability**, not just an open query
  TCP socket — so a trader can't read "healthy" while mutating commands are
  unavailable.

---

## Phased design

### Phase 1 — Trader-owned policy + capability matrix (C2, C4)
Define the `command_authority` config, the exact command→required-port matrix,
and the startup config-validation (reject contradictions).

### Phase 2 — Real adapter ports (C4)
Build/verify every production port. Do **not** register commands whose ports are
stubs (cancel, approval reconciliation) until real + tested.

### Phase 3 — Non-blocking handlers + unified registry (C1, C3)
Make every blocking command handler `async` + `asyncio.to_thread`. Register the
authority on one shared `production_registry` served by all three roles; fix the
gateway `get_command` path (repairs #9 too).

### Phase 4 — Real preflight nonce (C5)
Implement issuance + atomic one-time consumption with full request binding.
Resolve the paper contract (option A).

### Phase 5 — Approval-context risk + dispatch revalidation (#2)
- Build a **single immutable `ApprovalContext`** from ONE broker-state
  generation / quote timestamp (account, positions, open orders, net-liq, daily
  P&L, margin what-if, executable quote) — no reading inputs at arbitrary times
  across a multi-second flow.
- Enforce at approval **and** re-validate immediately before dispatch: account
  assertion (== trader account, and == `live_account_id` in live mode →
  `WRONG_LIVE_ACCOUNT`); notional cap (non-finite or `> max_order_notional` →
  `LIVE_NOTIONAL_LIMIT`); real `RiskGate.evaluate` with real inputs; leverage
  check. **Fail closed in live mode** on any failed broker/what-if/account read —
  no degrade-to-zero (`trading_runtime.py:1323`). Explicit caller
  `quantity`/`amount` gets the same ceilings (it bypasses the sizer today,
  `proposal_command_service.py:302-317`).
- Add the account + notional assertion **inside `TradingRuntimeOrderDispatch`**,
  not only in `ApprovalCommandService`, so no other execution path bypasses
  policy.
- Drift: reject non-finite at the model; `effective = min(requested or default,
  max_drift_bps)`; in live mode ignore caller drift and use server policy.

### Phase 6 — Split pause/resume (#4)
`pause_new_trading` (no preflight, no stale-version block, idempotent) and
`resume_new_trading` (exact version + live preflight). Pause is risk-reducing and
must never return `PREFLIGHT_REQUIRED`.

### Phase 7 — Trader-side live gate (#5)
Determine live/paper from the **trader's authoritative account**, not request
shape. Reject every risk-increasing action (create/approve proposal,
enable/update strategy) when the account is live and `live_enabled` is false —
independent of whether a nonce was sent. Bind strategy enable/param-update to
real trader-side preflight in live mode.

## Acceptance criteria

| Ref | Passes when |
|-----|-------------|
| C1 | A real `approve_proposal` reaches `place_expressive_order` **without blocking the trader loop** (timed test: a concurrent query still responds during dispatch). |
| C2 | Trader startup rejects contradictory `command_authority` config; enforcement reads trader policy, not dashboard env. |
| C3 | `gateway.get_command()` succeeds against the running socket (no `METHOD_NOT_ALLOWED`); #9 ledger reconcile resolves a command with SSE down. |
| C4 | `get_command_capabilities` lists only commands with real ports; cancel + approval reconciliation are absent while their adapters are stubs; startup fails closed if a declared command's port is missing. |
| C5 | A nonce bound to one body can't approve a mutated body/target; paper approval path is server-issued-preflight (or the defined unforgeable paper mechanism). |
| C6 | Compose healthcheck fails when command authority is enabled-but-incomplete; dashboard readiness reflects command health only when command UI is on. |
| #2 | Oversized-explicit-amount, wrong-account, concentration/drawdown, and failed-read (live) approvals are all rejected at the trader boundary from one `ApprovalContext`; leverage checked + fail-closed. |
| #3 | Over-cap/non-finite drift clamped/ignored server-side. |
| #4 | No-nonce "pause new trading" pauses (never `PREFLIGHT_REQUIRED`). |
| #5 | No-nonce request can't enable/update strategy or create/approve on a live account when `live_enabled=false`. |

## Sequencing

1. Trader-owned command policy + exact command→port capability matrix.
2. Build/verify all real adapter ports; do not register unsupported commands.
3. Make typed command handlers non-blocking to the trader loop.
4. Unify registry/query wiring; fix dashboard command reconciliation (+ #9).
5. Real nonce issuance/atomic consumption with full request binding.
6. `ApprovalContext` risk enforcement + dispatch-time revalidation.
7. Split pause/resume.
8. Paper-only end-to-end tests + soak (`scripts/run_paper_soak.py`).
9. Only then permit `DASHBOARD_COMMANDS_ENABLED=true` in **paper** mode.

## Not in scope

- Read-path freshness #6–#9 (landed `1532606`).
- Per-service credentials / CurveZMQ / least-privilege (#10) — separate track.
  Note explicitly: **once this lands, every holder of the shared service HMAC
  key can call the command socket directly**, bypassing browser sessions/CSRF/
  dashboard flags. That is an accepted, documented residual until the #10 track.
- Full-auto (`auto_execute: true`) — a hard **trader-side config invariant**
  (refused at load), not merely a dashboard limitation.

## Risks / open questions

- The real preflight nonce gate is net-new — the largest unknown; gates the
  whole ceremony.
- Approval-context reads (executable quote + broker state) must be a consistent
  single generation and must not block the trader loop (C1 applies here too).
- This flips the trader from read-only + strategy-state-ack to accepting
  mutating commands — the most dangerous change in the system. Every phase is
  paper-gated and fail-closed by construction.
