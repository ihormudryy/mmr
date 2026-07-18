# P1 Safety and Command-Plane Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate the existing command authority in production with fenced broker evidence, non-blocking handlers, trader-owned mode/risk decisions, durable circuit breaking, verified liquidation, and semantic readiness.

**Architecture:** Build all command services inside `Trader.connect()` after the journal and broker adapters exist, and register queries, feed, strategy ingest, and authorized commands in one registry. Replace sequential pseudo-snapshots with one transactionally fenced materialized broker snapshot plus separately timestamped executable-market evidence. A durable automation breaker fails closed and a liquidation saga resolves only from broker state.

**Tech Stack:** DuckDB, existing `DomainJournal`, typed RPC, asyncio, IBKR adapters, `exchange_calendars`, pytest/Hypothesis, Docker Compose.

> **Audit status (2026-07-18):** Checked items are supported by the current codebase, committed history, and the focused P1 verification run (261 passing tests). Historical RED-step observations remain unchecked because they cannot be re-executed against the completed implementation. The real-Compose/IB-paper operational gate remains unchecked.

## Global Constraints

- This program completes `docs/superpowers/specs/2026-07-18-command-plane-activation-design.md`; do not duplicate its landed F3 components.
- Preserve the six-field `CommandReceipt` and current command-ledger replay semantics.
- `pause_trading` is risk-reducing and requires authentication plus command ID, but no live preflight nonce. `resume_trading` is risk-increasing and requires preflight, reconciliation, semantic readiness, and reason.
- No synchronous DuckDB, IB, or downstream RPC work may run on the typed server's asyncio loop.
- `command_authority.enabled` remains false by default. Enabling it without every required production adapter must fail startup.

---

### Task 1: Prove the current production gap and define one composition root

**Files:**
- Create: `trader/trading/command_stack.py`
- Modify: `trader/trading/trading_runtime.py`
- Modify: `trader/trader_service.py`
- Modify: `trader/messaging/production_api.py`
- Create: `tests/test_command_stack.py`
- Modify: `tests/test_production_rpc_security.py`

**Interfaces:** `build_command_stack(trader, policy, now) -> CommandStack`; frozen `CommandStack` holds coordinator, ledger, reconciler, services, and trading control. `build_production_registry(..., command_stack=stack)` remains the transport composition point and returns the one `TypedRpcRegistry`; keeping the authenticated transport outside the domain stack avoids coupling `command_stack.py` to socket construction.

- [x] Write a failing test that constructs a fully enabled fake trader and asserts `build_command_stack` refuses any missing required adapter with `CommandStackConfigurationError(code="MISSING_<PORT>")`.
- [x] Write a failing integration-style test asserting the same registry resolves `snapshot_with_cursor`, `read_domain_events`, `record_state_acknowledged`, `approve_proposal`, `cancel_order`, and the currently landed `set_trading_pause` on their correct socket roles. Task 5 replaces `set_trading_pause` with separately classified `pause_trading` and `resume_trading`; Task 1 must not implement that later behavior early.
- [ ] Run `uv run --frozen --extra test pytest tests/test_command_stack.py tests/test_production_rpc_security.py -q`; confirm failure because composition and split pause/resume do not exist.
- [x] Implement `CommandStack` and `build_command_stack` by composing the landed repositories/services. Do not recreate proposal, ledger, preflight, approval, cancel, risk, or strategy-control logic.
- [x] Change `build_production_registry` to accept `command_stack: CommandStack | None` and register strategy ingest on that same registry. Remove the separately constructed command-only registry from `Trader.connect()`.
- [x] When policy is disabled, preserve read/feed/state-ingest capabilities and omit market-impact commands. When enabled, build the stack or abort startup; never silently downgrade.
- [x] Until Task 4 lands maximum-notional and immediate dispatch revalidation, reject any enabled live command stack at startup with `LIVE_GUARDS_INCOMPLETE`. Task 4 removes this temporary gate only after its live policy tests pass; paper activation is the only Task 1 runtime posture.
- [x] Attach `command_ledger` and `command_reconciler` to `Trader` before `_maybe_start_command_reconciliation` runs.
- [x] Run the focused tests and commit: `feat(command-plane): activate one production command registry`.

### Task 2: Move every command handler off the event loop

**Files:**
- Modify: `trader/messaging/typed_rpc.py`
- Modify: `trader/messaging/production_api.py`
- Create: `tests/test_typed_rpc_async_handlers.py`
- Modify: `tests/test_command_stack.py`

**Interfaces:** `TypedRpcRegistration.execution: Literal["inline", "thread"]`; production commands use `thread`; cheap pure health serialization may remain `inline`.

- [x] Write a test with a blocking handler guarded by threading events and prove a second async heartbeat/query completes while the first handler is blocked.
- [x] Write cancellation/shutdown tests proving at most the configured five-second drain is awaited and no response is emitted after socket close.
- [ ] Run the focused tests and observe the blocking behavior.
- [x] Extend `TypedRpcRegistry.register(..., execution="inline")`; make `TypedRpcServer` invoke `thread` handlers through `asyncio.to_thread` while preserving schema validation and authenticated request context.
- [x] Mark every coordinator, broker snapshot, proposal, cancel, control, and strategy-forwarding registration `execution="thread"`.
- [x] Add a bounded per-server semaphore (`TYPED_RPC_MAX_IN_FLIGHT`, default 32) and return `SERVER_BUSY` without invoking a handler when saturated.
- [x] Run `uv run --frozen --extra test pytest tests/test_typed_rpc_async_handlers.py tests/test_typed_rpc.py tests/test_command_stack.py -q` and commit `fix(command-plane): isolate blocking command work`.

### Task 3: Replace sequential reads with a fenced broker risk snapshot

**Files:**
- Modify: `trader/data/broker_state.py`
- Modify: `trader/trading/approval_context.py`
- Modify: `trader/trading/command_ports.py`
- Modify: `trader/trading/approval_command_service.py`
- Create: `tests/test_broker_risk_snapshot.py`
- Modify: `tests/test_approval_context.py`

**Interfaces:**

```python
class BrokerRiskSnapshotAuthority(Protocol):
    def capture(self, account_id: str) -> BrokerRiskSnapshot: ...

class BrokerStateStore:
    def capture_risk_snapshot_in_tx(
        self, conn, account_id: str
    ) -> BrokerRiskSnapshot: ...
```

- [x] Add tests that insert two promoted broker generations and prove account, positions, and working orders all come from the latest single promoted generation/cursor.
- [x] Add tests for no promoted generation, account mismatch, mode mismatch, staging generation, generation regression, non-finite net liquidation/P&L, and a concurrent writer committing during the open read transaction.
- [x] Update approval-context tests to require `generation_id`, `source_cursor`, `promoted_at`, and authoritative `account_mode`; remove the “one call per live port means one generation” assertion.
- [ ] Run the focused tests and confirm they fail against the current sequential `BrokerAuthority` reads.
- [x] Implement the store read inside one DuckDB read transaction. Select the latest `status='promoted'` row, validate its cursor, and read all materialized account/position/order rows before commit.
- [x] Implement `TraderBrokerRiskSnapshotAuthority` over the trader-owned journal connection. Quotes and IB what-if remain independent evidence and must not be labeled with the broker generation.
- [x] Refactor `ApprovalContext` to contain `broker: BrokerRiskSnapshot`, `market: ExecutableMarketEvidence`, and optional timestamped `what_if`; retain compatibility properties only inside the module while callers migrate in this task.
- [x] Run `uv run --frozen --extra test pytest tests/test_broker_risk_snapshot.py tests/test_approval_context.py tests/test_command_ports.py tests/test_approval_command.py -q` and commit `fix(risk): capture fenced broker approval evidence`. (`test_approval_command.py` is the current test-module name.)

### Task 4: Enforce initial validation and immediate dispatch revalidation

**Files:**
- Modify: `trader/trading/approval_command_service.py`
- Modify: `trader/trading/command_coordinator.py`
- Modify: `trader/trading/command_policy.py`
- Create: `trader/trading/dispatch_guard.py`
- Create: `tests/test_dispatch_guard.py`
- Modify: `tests/test_approval_command_service.py`

**Interfaces:** `DispatchGuard.revalidate(approved: ApprovalContext, request: CommandRequest, now: datetime) -> DispatchPermit`; permit records refreshed quote/what-if timestamps and broker generation.

- [x] Add table-driven failures for stale/missing/crossed/non-finite quote, delayed live quote, halt, session closed, broker not ready, generation regression, account/mode mismatch, maximum notional, drift, pause, and missing live what-if.
- [x] Add a TOCTOU test: initial validation passes, quote becomes stale or position changes before dispatch, no call reaches `OrderDispatch`.
- [x] Add a monotonic reducing invariant property test: for any signed position/quantity, a reduction is permitted only when the result keeps the sign or reaches zero and strictly reduces absolute exposure.
- [ ] Run failing tests.
- [x] Implement explicit `RiskDecision` reason codes and persist both initial and dispatch-time decisions before side effects.
- [x] Re-capture broker snapshot and executable quote immediately before dispatch. Reject if generation regresses, the target position/order state materially changes, or freshness/policy fails.
- [x] Live mode fails closed when what-if is unavailable; paper behavior follows explicit config and records `WHAT_IF_UNAVAILABLE_PAPER`, never zero.
- [x] Run focused tests and commit `feat(risk): enforce pre-dispatch evidence revalidation`.

### Task 5: Split pause from resume and make mode classification trader-owned

**Files:**
- Modify: `trader/messaging/production_api.py`
- Modify: `trader/trading/command_coordinator.py`
- Modify: `trader/trading/command_policy.py`
- Modify: `trader/trading/trading_control.py`
- Modify: `web/command_center/commands.py`
- Modify: `web/command_center/routes_commands.py`
- Modify: `web/static/command_center.js`
- Create: `tests/test_pause_resume_authority.py`
- Modify: `tests/test_dashboard_commands.py`

**Interfaces:** Produces typed `pause_trading(command_id, reason)` and `resume_trading(command_id, expected_control_revision, reason, preflight_nonce)` actions plus the existing `get_trading_control` query; consumes pinned `trader.ib_account`/`trader.paper_trading` and `TradingControlStore`.

- [x] Write tests proving `pause_trading` accepts no caller-supplied `is_live`, requires a reason, does not require preflight, and is idempotent.
- [x] Write tests proving `resume_trading` derives live/paper from the pinned trader account, requires expected control revision, live preflight where applicable, current readiness, and complete reconciliation.
- [x] Test that a paper browser cannot label a live trader command as paper and that account ID in a request body is ignored/rejected.
- [x] Replace `set_trading_pause` with explicit typed actions. Preserve a read-only compatibility alias only if an existing caller needs it; it must not be registered on production command sockets.
- [x] Change the UI to use pause/resume endpoints and show authoritative correlated state, never HTTP 202 as completion.
- [x] Run `uv run --frozen --extra test pytest tests/test_pause_resume_authority.py tests/test_command_policy.py -q` and commit `fix(control): separate safe pause from guarded resume`. (Dashboard coverage is consolidated into `test_pause_resume_authority.py`.)

### Task 6: Add durable automation circuit breaker and semantic readiness

**Files:**
- Create: `trader/trading/circuit_breaker.py`
- Create: `trader/data/circuit_breaker_store.py`
- Modify: `trader/data/schema_migrations.py`
- Modify: `trader/trading/command_stack.py`
- Modify: `trader/messaging/trader_service_api.py`
- Modify: `docker-compose.yml`
- Create: `tests/test_circuit_breaker.py`
- Create: `tests/test_semantic_readiness.py`

**Interfaces:** Produces `CircuitBreaker.record(signal: BreakerSignal) -> BreakerState`, `CircuitBreaker.reset(command_id, reason, operator_authority) -> BreakerState`, and `SemanticReadiness.evaluate(now) -> ReadinessReport`.

**Migration 24:** `automation_circuit_breaker(account_id, state, reason_code, reason, revision, tripped_at, reset_at, reset_command_id)` plus append-only `automation_incidents` with unique deterministic incident ID.

- [x] Add state-machine tests for immediate triggers, rolling thresholds (3 quote failures/5m, 3 ambiguous outcomes/5m, 5 runtime exceptions/10m), disconnect grace, restart persistence, duplicate trigger idempotency, and next-session daily-loss reset restriction.
- [x] Add semantic readiness tests requiring: IB connected/account pinned, current promoted broker generation, journal writable, reconciliation backlog safe, pause/control readable, breaker clear, current XNYS session, command stack active, and quote readiness for configured instruments.
- [ ] Run tests and confirm missing implementation.
- [x] Implement `CircuitBreakerStore` mutations and journal events atomically. Use injected clocks; no wall-clock calls inside policy methods.
- [x] Implement `CircuitBreaker.record(signal)` and `reset(command_id, reason, operator_authority)`; reset fails unless semantic readiness and reconciliation pass.
- [x] Expose separate liveness and semantic readiness fields through `get_status`; make Compose healthcheck require liveness only, while automation requires semantic readiness.
- [x] Run focused tests and commit `feat(safety): add durable automation circuit breaker`.

### Task 7: Implement broker-verified liquidation saga

**Files:**
- Create: `trader/trading/liquidation_service.py`
- Modify: `trader/trading/command_coordinator.py`
- Modify: `trader/trading/command_stack.py`
- Modify: `trader/trader_service.py`
- Create: `tests/test_liquidation_service.py`
- Modify: `tests/integration/test_command_authority.py`

**Interfaces:** `LiquidationService.start(account_id, cause_command_id, deadline) -> LiquidationReceipt`; states `REQUESTED`, `CANCELLING_ENTRIES`, `REDUCING`, `VERIFYING`, `FLAT`, `OUTCOME_UNKNOWN`, `FAILED_SAFE`.

- [x] Test cancel-before-reduce ordering, partial fills, position changes during liquidation, repeated calls, disconnect, timeout, restart rescan, external working orders, and a would-flip quantity.
- [x] Assert `FLAT` requires a fresh promoted broker snapshot with zero positions and no working orders; an HTTP/RPC acknowledgement or submitted market order is insufficient.
- [x] Implement child command IDs deterministically under the root cause ID and use existing cancel/dispatch/reconciliation adapters.
- [x] Keep breaker tripped for all non-`FLAT` terminal/ambiguous results. Never fabricate zero positions.
- [x] Start rescan before readiness and continue until broker evidence resolves.
- [x] Run focused/integration tests and commit `feat(safety): add broker-verified liquidation saga`.

### Task 8: Production activation, failure drills, and release gate

**Files:**
- Modify: `config_defaults/trader.yaml`
- Modify: `docker-compose.yml`
- Modify: `scripts/run_paper_soak.py`
- Create: `scripts/command_plane_drill.py`
- Create: `tests/integration/test_command_plane_activation.py`
- Create: `docs/superpowers/rollout/trading-income-operations-runbook.md`

**Interfaces:** Consumes the complete P1 `CommandStack`; produces versioned drill JSON with `commit_digest`, `config_digest`, `scenario_results`, and `passed`, plus the signed operator release record.

- [ ] Add a real-Compose integration test/profile that enables paper command authority and proves proposal -> approve -> broker order event -> correlated terminal state without legacy RPC.
- [x] Add scripted drills for stale quote, trader delay, broker disconnect, ambiguous submit, partial fill, rejected protection hook, restart with unresolved command, pause/resume, and liquidation.
- [x] Assert every drill leaves one idempotent command history, durable audit, no duplicate order reference, and a coherent breaker/readiness state.
- [x] Update default config documentation with every activation prerequisite; keep commands disabled by default.
- [ ] Run focused unit/integration tests, the canonical full suite, `docker compose config --quiet`, and at least a two-minute synthetic soak.
- [ ] In IB paper during market hours, run one complete session soak and record the report path/commit/config digest. This manual gate may not be replaced by synthetic fixtures.
- [x] Commit `test(command-plane): add production activation and recovery gates`.

## P1 exit criteria

- The typed command socket has one authenticated registry and no optional missing production authority when enabled.
- Broker state is transactionally fenced; market evidence remains independently timestamped.
- Every exposure increase passes initial and immediate pre-dispatch checks.
- Pause is always available; resume/reset are risk-increasing authenticated commands.
- Circuit breaker and liquidation survive restarts and resolve only from broker truth.
- The event loop remains responsive under slow command work.
- Automation stays prohibited until the real IB paper session drill is signed off.
