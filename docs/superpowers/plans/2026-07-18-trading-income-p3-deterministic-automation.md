# P3 Deterministic Automated Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run exactly one signed, eligible, deterministic strategy through the existing command coordinator with hard session/liquidity risk, broker-native protection, daily flattening, complete attribution, and forensic replay.

**Architecture:** `strategy_service` loads a read-only verified artifact and emits canonical `ExecutionIntent` messages. `trader_service` alone verifies authority, claims the deterministic command ID, captures P1 evidence, applies session risk, dispatches a protected order saga, reconciles broker truth, attributes results, and seals daily replay. There is no direct strategy-to-IB path.

**Tech Stack:** P1 command stack, P2 artifacts/attestations, typed RPC, DuckDB journal migrations 30-39, `exchange_calendars`, IBKR bracket/OCA orders, pytest/Hypothesis.

## Global Constraints

- P1 release gate is merged and enabled in an IB paper profile.
- P2 canonicalization, artifact bundle, ruleset, and attestation verification contracts are frozen.
- Automation feature flag defaults false; live automation additionally defaults false.

---

### Task 1: Define canonical execution intents and deterministic IDs

**Files:**
- Create: `trader/automation/__init__.py`
- Create: `trader/automation/models.py`
- Create: `trader/automation/intent_ids.py`
- Create: `tests/automation/test_execution_intent.py`

**Interfaces:** Produces the frozen `ExecutionIntent` contract from the program index plus `derive_intent_id(fields) -> str` and `derive_command_id(intent_id) -> str`.

- [x] Add strict schema tests for all frozen index fields, timezone-aware timestamps, positive conid, BUY/SELL only, risk fraction bounds, stop distance, account mode, and completed-bar ordering.
- [x] Add golden ID tests and Hypothesis tests proving identical stable inputs create identical IDs; any authority-relevant change changes both intent and command ID.
- [x] Reject caller-supplied IDs that do not equal recomputation. IDs are colon-free and round-trip through existing `encode_order_ref`.
- [x] Implement frozen policies (`EntryPolicy`, `StopPolicy`, `TargetPolicy`, `TimeExitPolicy`) with no free-form executable strings.
- [x] Use P2 canonical bytes and SHA-256 prefixes `intent-`/`auto-`; do not use Python `hash()` or random UUIDs.
- [x] Run focused tests and commit `feat(automation): define deterministic execution intents`.

### Task 2: Verify artifacts at both load and command boundaries

**Files:**
- Create: `trader/automation/artifact_verifier.py`
- Modify: `trader/strategy/strategy_runtime.py`
- Modify: `trader/config.py`
- Modify: `config_defaults/trader.yaml`
- Create: `tests/automation/test_artifact_verifier.py`

**Interfaces:** `ArtifactVerifier.verify(bundle_path, expected_mode, now) -> VerifiedArtifact`; result contains immutable allowlist, parameters, digests, allocation, expiry, and public-key ID.

- [x] Test checksum/signature/expiry/revocation/mode/allowlist/ruleset/artifact mismatch and changed files after load.
- [x] Test `CANDIDATE`, `SUSPENDED`, and `RETIRED` cannot run; `PAPER_ELIGIBLE` cannot run live; `CANARY_ELIGIBLE` is required live.
- [x] Add config paths for read-only artifact bundle, public verification key ring, and expected artifact ID; refuse writable bundle mounts in live mode.
- [x] Verify once at strategy load and again from immutable bundle evidence at trader command validation. Never trust a strategy-service “verified” boolean.
- [x] Persist safe verification reason codes, not artifact source contents or keys.
- [x] Run focused tests and commit `feat(automation): verify signed strategy authority`.

### Task 3: Register automated intent as a coordinator action

**Files:**
- Modify: `trader/domain/commands.py`
- Modify: `trader/messaging/production_api.py`
- Modify: `trader/trading/command_policy.py`
- Modify: `trader/trading/command_stack.py`
- Modify: `trader/trading/command_coordinator.py`
- Create: `tests/automation/test_automated_command_boundary.py`

**Interfaces:** Consumes `ExecutionIntent`, `ArtifactVerifier`, P1 `TradingCommandCoordinator`, and typed authenticated principal context; produces the `execute_automated_intent` command action returning frozen `CommandReceipt`.

**Action:** `execute_automated_intent`; only the authenticated strategy-service principal may call it. Browser/CLI principals are forbidden.

- [ ] Test principal allowlisting, canonical request hash replay, changed payload conflict, command claimed/audited before validation, and colon-free order-group correlation.
- [ ] Test duplicate delivery during in-flight, after reject, after submit, after `OUTCOME_UNKNOWN`, and after resolution never dispatches twice.
- [ ] Add a typed request model carrying the full intent and artifact bundle digest; account ID is derived from the trader, never request body.
- [ ] Register the action on the existing coordinator. Do not create another ledger/coordinator or call `TradingRuntimeOrderDispatch` directly from a route/runtime.
- [ ] Record artifact, session, signal, intent, attestation, policy, and evidence IDs in command audit before dispatch.
- [ ] Run focused tests and commit `feat(automation): route intents through command authority`.

### Task 4: Implement trader-owned session, liquidity, and risk policy

**Files:**
- Create: `trader/automation/session_risk.py`
- Create: `trader/automation/calendar_policy.py`
- Create: `trader/automation/liquidity_policy.py`
- Modify: `trader/trading/command_stack.py`
- Create: `tests/automation/test_session_risk.py`
- Create: `tests/automation/test_calendar_policy.py`
- Create: `tests/automation/test_liquidity_policy.py`

**Interfaces:** `SessionRiskController.evaluate(intent, artifact, approval_context, session_state, allocation) -> AutomatedRiskDecision`.

- [ ] Add rule tests: permitted strategy/conid, long-only, at most 3 positions, 5% position, 6% initial gross, 0.20% equity risk, 0.50% daily loss, 3% drawdown, stop validity, and most-restrictive-wins.
- [ ] Add liquidity tests: price >=5, 20-day median dollar volume >=50m, spread <=15bps, quantity <=0.25% ADV, permitted live feed, halt/requalification, depth/slicing policy.
- [ ] Add XNYS tests across DST, holidays, early closes, opening stabilization, 15:30/15:35/15:45/15:55 relative offsets, and calendar package version recording.
- [ ] Add monotonic property tests: lowering allocation/equity/ADV/depth or raising risk/spread/order size cannot convert rejection to approval unless the changed dimension is irrelevant.
- [ ] Use only trader-owned policy/config and signed artifact ceilings. Ignore/reject request fields that would weaken hard limits.
- [ ] Feed P1 breaker signals for repeated quote failures, account mismatch, loss, and halt.
- [ ] Run focused tests and commit `feat(automation): enforce session and liquidity risk`.

### Task 5: Build the protective entry saga

**Files:**
- Create: `trader/automation/protective_order_saga.py`
- Modify: `trader/trading/executioner.py`
- Modify: `trader/trading/trading_runtime.py`
- Modify: `trader/trading/command_coordinator.py`
- Create: `tests/automation/test_protective_order_saga.py`
- Modify: `tests/test_executioner.py`

**Interfaces:** Consumes approved intent, dispatch permit, existing order correlation, and broker events; produces `ProtectiveOrderSaga.start/resume/on_broker_event` and durable saga state.

**Journal migration 30:** `automated_order_sagas` keyed by command/order group with states `VALIDATED`, `SUBMITTING`, `ENTRY_WORKING`, `PARTIALLY_FILLED`, `PROTECTED`, `EXITING`, `CLOSED`, `OUTCOME_UNKNOWN`, `SAFETY_FAILED`.

- [ ] Test broker-native bracket/OCA construction, deterministic order refs, transmit ordering, parent/child quantity, stop side/price, and no unrestricted market fallback.
- [ ] Test parent rejection, child rejection, missing protection, partial parent fill, protection quantity adjustment, stop fill, target fill, cancel race, disconnect, duplicate broker events, and restart.
- [ ] Assert an entry fill without confirmed working protection immediately trips P1 breaker and starts verified liquidation.
- [ ] Implement saga transitions transactionally with domain events and command correlation. Broker events, not submit returns, advance working/filled states.
- [ ] Re-run P1 dispatch guard immediately before the first IB side effect.
- [ ] Run focused tests and commit `feat(automation): add broker-protective order saga`.

### Task 6: Add session deadlines and deterministic time exits

**Files:**
- Create: `trader/automation/session_controller.py`
- Modify: `trader/trader_service.py`
- Modify: `trader/trading/command_stack.py`
- Create: `tests/automation/test_session_controller.py`

**Interfaces:** Produces `SessionController.recover(now)`, `on_bar(now)`, and `run_due(now)`; consumes XNYS schedule, command coordinator, cancel service, liquidation service, and broker snapshots.

**Journal migration 31:** `automation_session_state` stores schedule/version, state, deadlines, entry cutoff reached, flatten command ID, flat generation, and incident.

- [ ] Test completed-bar time exits, max-hold-bars, artifact close-by time, no entries after cutoff, cancel entries at cancel deadline, flatten at flatten deadline, and broker-confirmed flat deadline.
- [ ] Test restart at every state/deadline, clock jumps, DST, half-day, delayed task scheduling, partial fills during cancel, and an external position.
- [ ] Implement a trader-owned scheduler driven by absolute UTC deadlines resolved from XNYS. Strategy timers are advisory only.
- [ ] Use deterministic root/child command IDs; reuse P1 cancel/liquidation. A missed flat deadline trips breaker and never self-resets.
- [ ] Start session recovery before semantic readiness.
- [ ] Run focused tests and commit `feat(automation): enforce exchange-aware session flattening`.

### Task 7: Implement the authoritative attribution ledger

**Files:**
- Create: `trader/automation/attribution.py`
- Create: `trader/data/attribution_store.py`
- Modify: `trader/trading/broker_ingest.py`
- Create: `tests/automation/test_attribution_ledger.py`

**Interfaces:** Produces `AttributionLedger.append(evidence_event)` and `rebuild_trade(trade_id) -> TradeAttribution`; consumes P3 command/broker/policy evidence and never dashboard state.

**Journal migrations 32-34:** append-only `automation_decisions`, `trade_attribution`, `execution_cost_attribution`, and `operator_action_refs`; deterministic unique keys prevent duplicate broker-event accounting.

- [ ] Test joins across artifact/dataset/signal/intent/context/policy/command/order/fill/commission/position and rejection/breaker/operator actions.
- [ ] Test out-of-order fills/commissions, corrections, partial fills, duplicate exec IDs, multiple exits, MFE/MAE sampling, gross/net P&L, spread/slippage/latency, and crash between event and derived aggregation.
- [ ] Store raw append-only evidence and rebuild derived trade rows deterministically; never overwrite raw broker evidence.
- [ ] Ensure promotion queries exclude unresolved trades and report them explicitly rather than assuming zero P&L/cost.
- [ ] Emit domain events for dashboard observability without making dashboard state authoritative.
- [ ] Run focused tests and commit `feat(automation): record end-to-end trade attribution`.

### Task 8: Seal and replay a complete trading day

**Files:**
- Create: `trader/automation/replay.py`
- Create: `trader/automation/replay_bundle.py`
- Create: `scripts/replay_trading_day.py`
- Create: `tests/automation/test_forensic_replay.py`

**Interfaces:** Produces `ReplayBundle.seal(session_id) -> BundleDigest` and `TradingDayReplay.run(bundle) -> ReplayResult`; consumes immutable P2/P3 evidence.

- [ ] Define bundle entries for artifact/attestation, bars, quote evidence, broker snapshots, policies, intents, decisions, commands, broker events, attribution, breaker/reconciliation/operator actions, and XNYS resolved schedule/version.
- [ ] Test atomic seal (`tmp` + fsync + rename), checksums, path traversal, missing evidence, unresolved commands, corruption, and repeatable bundle digest.
- [ ] Implement pure replay that recomputes signals, intent IDs, sizing, and policy decisions, while replaying recorded broker events rather than simulating broker behavior.
- [ ] Compare exact decision traces and produce a structured divergence report; any divergence blocks evidence credit and trips/suspends via P4.
- [ ] Run one golden accepted day and adversarial rejected/partial-fill/restart days.
- [ ] Commit `feat(automation): add sealed forensic day replay`.

### Task 9: Wire one strategy and pass the full paper vertical slice

**Files:**
- Modify: `trader/strategy/strategy_runtime.py`
- Modify: `trader/messaging/strategy_service_api.py`
- Modify: `docker-compose.yml`
- Modify: `config_defaults/trader.yaml`
- Create: `tests/integration/test_automated_vertical_slice.py`
- Create: `scripts/automation_paper_drill.py`
- Modify: `docs/superpowers/rollout/trading-income-operations-runbook.md`

**Interfaces:** Consumes the complete P3 command, strategy, protection, session, attribution, and replay contracts; produces the first one-strategy paper release report.

- [ ] Add `automation.enabled`, `automation.live_enabled`, exact artifact ID, bundle path, public key ring, and one-strategy name. All default false/empty.
- [ ] Emit intents only after completed session-valid bars. The strategy runtime must not construct IB orders, use legacy RPC, or mutate the journal.
- [ ] Test full typed transport: duplicate bar -> identical intent -> one coordinator command -> protected paper order -> broker events -> attribution -> time exit/flatten -> sealed replay.
- [ ] Inject stale quote, rejected stop, ambiguous submission, disconnect, duplicate event, crash/restart, and missed deadline; assert breaker and no duplicate exposure.
- [ ] Run focused integration tests, canonical full suite, Compose configuration, and a two-minute synthetic drill.
- [ ] Run one complete IB paper session with automation allocation set to the minimum safe test size; record bundle/report/config/commit digests. This is the P3 release gate, not evidence toward P4 unless its data is fully qualified.
- [ ] Commit `feat(automation): complete one-strategy paper vertical slice`.

## P3 exit criteria

- One signed eligible artifact is the only automated strategy.
- Every repeated signal produces the same command and no duplicate order.
- All hard risk/session/liquidity rules live in `trader_service`.
- Entry protection and daily flatten are broker-confirmed state machines.
- Every decision is attributable and every day seals/replays exactly.
- Live automation remains disabled pending P4 promotion.
