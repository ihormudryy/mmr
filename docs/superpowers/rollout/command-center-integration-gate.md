# Command Center — web↔trader integration-gate reconciliation

The M1-C command surfaces and the M1-R dashboard were built + unit-tested against
**fakes** for the typed-RPC transport (per plan). F3 T9's `tests/integration/test_command_authority.py`
proves the **trader-internal** command authority (coordinator → saga → reconciler → broker
store), but nothing yet exercises the **web↔trader wire seam** (the M1-C `DashboardCommandGateway`
and the M1-R `DashboardEventBridge` against the real fenced producers/coordinator). Every item
below lives in that seam and is **cross-lane**: it needs a coordinated web-side + trader-side
change plus one new web↔trader integration test. This doc is the joint contract for the
convergence run.

Status legend: WEB = this session's lane (M1-C/M1-R/COMPAT); TRADER = Worker-B (F3/SSE-repair).
Verified against HEAD `da2cf88` (full suite: 2228 passed; the only failures are 2 pre-existing
`test_data_access` tz cases + Worker-B's in-flight `test_runtime_dependencies`).

---

## 1. CRITICAL-1 — bridge ↔ producer wire contract (dataclass vs dict)
**Symptom (real path):** `DashboardEventBridge` calls `TypedRpcClient.call(method, body,
response_model=SnapshotWithCursor / ReadDomainEventsResult)`. `TypedRpcClient.call` does
`response_model.model_validate(...)` for a non-`dict` model, but those are plain frozen
`@dataclass`es (no `model_validate`) → `AttributeError`. Even bypassing that, the server registers
`snapshot_with_cursor`/`read_domain_events` with `response_model=dict` and returns raw wire dicts,
and there is **no `domain_event_from_wire`** — so `baseline.source_cursor`, `result.events`,
`event.*`, `state.apply(event)` all fail against dicts. Net: on the real path the bridge degrades
forever, `has_baseline` never becomes true, `/api/snapshot` 503s forever — dashboard non-functional.
Masked because every test injects fakes returning dataclasses.
- **TRADER (Worker-B, "SSE-repair" lane — owns this):** add `domain_event_from_wire(dict) ->
  DomainEvent` and a `SnapshotWithCursor.from_wire(dict)` (inverse of `domain_event_to_wire` /
  the snapshot wire serializer); confirm the `/api/snapshot` + `/api/events` wire dict shapes.
- **WEB:** `DashboardEventBridge` calls with `response_model=dict` and reconstructs via the new
  `from_wire` helpers (or the transport returns dataclasses and the bridge stays as-is — pick ONE).
- **ACCEPTANCE:** an integration test that stands up the real snapshot/feed producers + the real
  bridge and asserts a baseline installs (`has_baseline` true) and a `position.updated` event flows
  producer → bridge → `DashboardState` → SSE without degrading.
- Design already sketched by Worker-B in `docs/superpowers/plans/2026-07-17-dashboard-sse-runtime-dependency.md`.

## 2. `preflight_command` — the live-nonce mint does not exist
**Symptom:** M1-C's live ceremony (`gateway.preflight` → coordinator `preflight_command` →
signed nonce + summary) is fake-tested; the real `preflight_command` typed method is registered
NOWHERE in `trader/messaging/production_api.py`. Live approve/cancel cannot complete end-to-end.
- **TRADER (Worker-B):** implement + register `preflight_command(body) -> dict` on the coordinator
  with the pinned body `{command_id, action, params, expected_version, session_fingerprint}` →
  `{command_id, nonce, expires_at, summary}` (summary = side/instrument/quantity/notional/
  order_type/latest_price/drift_bps/warnings/account_id/account_mode). The nonce is 30s, single-use,
  and bound to command_id/action/params/expected_version/account/mode/session.
- **WEB:** already consumes it via `gateway.preflight` (contract pinned in M1-C T2) — no change
  expected once the server method matches the pinned shape.
- **ACCEPTANCE:** integration test: preflight → confirm → approve with the returned nonce succeeds;
  a tampered/expired/reused nonce is rejected server-side.

## 3. `session_fingerprint` — web forwards a field the F3 models forbid
**Symptom:** M1-C `routes_commands.py` forwards `session_fingerprint` on approve/cancel/preflight
bodies, but `ApproveProposalRequest`/`CancelOrderRequest`/`CancelOrdersRequest` are
`extra="forbid"` with no such field → real approve/cancel would 422 before nonce processing.
**This is coupled to item 2** (the nonce-verification design determines whether the coordinator
needs the raw fingerprint at approve, or the signed nonce alone carries the session binding).
- **DECISION (make at convergence, driven by item 2's design):**
  - If the signed nonce self-carries the session binding (standard) → **WEB stops forwarding
    `session_fingerprint` on approve/cancel** (send only F3-declared fields); the fingerprint is
    used ONLY in the `preflight_command` body (item 2).
  - If the coordinator must re-check the raw fingerprint at verify → **TRADER declares
    `session_fingerprint` on the approve/cancel models**; WEB keeps forwarding it.
- **WEB default (safe, reversible):** conform to the current contract — drop `session_fingerprint`
  from the approve/cancel forwarded bodies (one-line each in `routes_commands.py`), keep it in the
  preflight body. Re-add only if item 2's design requires it.
- **ACCEPTANCE:** the item-2 integration test also asserts approve/cancel don't 422 on the real wire.

## 4. Strategy-control ceremony has no server nonce backing
**Symptom:** `enable_strategy`/`disable_strategy`/`update_strategy_params` are registered
`requires_preflight=False`; M1-C runs a client-side live ceremony for enable/params but the nonce
is dropped before forwarding (F3 models don't declare it), so it's confirmation-UX-only, not
server-verified. A client suppressing the nonce still gets a server-accepted enable.
- **DECISION (spec §9.3 says re-enable/reparameterize is risk-increasing):** either **TRADER**
  registers those actions `requires_preflight=True` (server-verified ceremony), OR the design
  confirms strategy control is confirmation-UX-only (enable only lets a strategy *propose*;
  execution still needs a nonce-gated approve — the current mitigation). Record the accepted choice.
- **WEB:** if server preflight is added, forward the nonce (like `set_trading_pause` already does).

## 5. `/strategies/deploy` single-authority leak (COMPAT T2)
**Symptom:** `web/app.py`'s `deploy_strategy` reaches `enable_strategy`/`reload_strategies` via the
unguarded legacy SDK RPC — NOT in `LEGACY_TRADING_MUTATION_PATHS`, so it's a second strategy-enable
authority even when `DASHBOARD_COMMANDS_ENABLED`. Bounded (enable→propose→nonce-gated approve), but
the "single mutation authority" invariant leaks for enablement.
- **TRADER (Worker-B, COMPAT T2):** migrate deploy-from-disk through the coordinator
  (`deploy_strategy` typed command / `StrategyDeployService`).
- **WEB (COMPAT T2/T6):** once migrated, route the `/manage` deploy form through the gateway and
  add the legacy `/strategies/deploy` route to the 409-gate (or retire it at T6).

## 6. Two trader-side soak metrics still fail-closed
**Symptom:** `run_paper_soak.py` now has live exporters for `max_replay_ring_events`/
`max_client_fifo_depth`/`max_terminal_rows` (via `/api/cc-health`), but `unhandled_errors` and
`unresolved_commands` have no exporter → they stay `observed=None`/fail-closed, so a real 8h soak
can't pass those two thresholds.
- **TRADER (Worker-B):** expose an unhandled-error counter and the command-ledger
  `unresolved_for_target` count via a query RPC (or on a health endpoint) the soak runner can read.
- **WEB:** extend `run_paper_soak.py`'s sampler to fold those two into the maxima (mirrors the
  cc-health sampling already added) once an exporter exists.
- Also: the `strategy_outage` soak scenario has no live health signal (falls back to the
  post-recovery parity check) — a strategy-service health field would make it a real gate.

## 7. F3-internal dispatch-wiring blockers (TRADER-only; confirm cleared at T9)
Not web↔trader seams, but they gate arming real order dispatch and were catalogued during the F3
T5 re-verification (before the lane split). Worker-B's T9 (`3f3994f`) + the reconciler fix
(`7c9af4d`) should have addressed these — confirm before enabling live:
- **DP1:** the sync→async dispatch bridge (`run_coroutine_threadsafe(...).result(timeout=30)`) must
  not run on the same asyncio loop that serves the command RPC (deadlock → OUTCOME_UNKNOWN → order
  placed post-return). Fix: run handlers off-loop (executor / dedicated thread) or await directly;
  cancel the coroutine on timeout.
- **RJ2:** any wedge into OUTCOME_UNKNOWN must schedule reconciliation (coordinator needs a
  reconciler ref); reconciler must handle `target_type=="proposal"`. (`7c9af4d` made the reconciler
  command-type-aware — verify it covers the pre-dispatch wedge path.)
- **SA1:** `_evaluate_risk` must pass real portfolio state (NetLiq/open-orders/daily-pnl/
  position-value) — else the approval risk gate can never reject.
- **SA7:** the dispatch adapter must honor proposal exchange/currency (not hardcode SMART/USD) —
  needs exchange/currency on the proposal record or conId-based resolution parity with legacy sdk.

---

## Convergence run — order of operations
1. Worker-B lands item 1 (SSE wire) + item 2 (`preflight_command`); confirm item 7 (DP1/RJ2/SA1/SA7).
2. Make the item-3 and item-4 decisions (driven by item 2's nonce design); apply the web + trader
   halves.
3. Add ONE web↔trader integration test covering: bridge baseline+event flow (item 1), preflight→
   approve with a real nonce (items 2/3), and a strategy enable ceremony (item 4).
4. Wire the item-6 trader-side soak exporters; run the release-gate 8h soak (COMPAT T5 §A).
5. COMPAT T2 (deploy via coordinator, item 5), then the operator-gated COMPAT T6 retirement once
   the T5 checklist (soak + live read-only session + rollback drill + credential rotation) passes.

Durable per-lane detail: `.superpowers/sdd/{m1c,m1r,m1f3,compat}-progress.md` (the m1f3 ledger has
the consolidated INTEGRATION-GATE section this doc formalizes for both workers).
