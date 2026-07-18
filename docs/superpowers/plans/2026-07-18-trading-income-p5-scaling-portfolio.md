# P5 Controlled Scaling and Portfolio Reuse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scale only through signed 6% -> 9% -> 13.5% -> 15% gross authority, reduce allocation immediately when evidence degrades, and admit a second strategy only after portfolio covariance/factor and combined-risk gates pass.

**Architecture:** An offline allocation controller evaluates P4 evidence and prepares immutable authority. Trader-owned allocation policy verifies and enforces the signed ceiling on every intent. Capacity/degradation monitors may suspend or reduce authority but never increase it. Portfolio admission is deterministic and preserves the account-level 0.50% daily-loss ceiling.

**Tech Stack:** P1-P4 authority/attribution, journal migrations 50-59, P2 Ed25519 contracts, NumPy/SciPy, pytest/Hypothesis.

## Global Constraints

- Scaling is not scheduled and never automatic.
- All percentages are gross account exposure. The most restrictive of account, strategy, position, trade-risk, liquidity, depth, and signed allocation limits wins.
- A system component may automatically reduce/suspend allocation. Only authenticated signed operator authority may increase/re-enable it.
- A second strategy is out of scope until the first has successfully completed Scale 2 and the second independently completes research, paper, and canary.

---

### Task 1: Add immutable allocation attestations and authority store

**Files:**
- Create: `trader/promotion/allocation_attestation.py`
- Create: `trader/data/allocation_authority_store.py`
- Modify: `trader/automation/artifact_verifier.py`
- Modify: `trader/data/schema_migrations.py`
- Create: `tests/scaling/test_allocation_attestation.py`

**Interfaces:** Produces offline `AllocationAttestationSigner`, production `AllocationAttestationVerifier`, and append-only `AllocationAuthorityStore.active_for(account_id, artifact_id)`.

**Journal migrations 50-51:** append-only `allocation_authorities` and `allocation_authority_events` with artifact/account/stage/gross ceiling/evidence/operator/signature/issue/expiry/revocation/supersession.

- [ ] Test canonical sign/verify and tampering of account, artifact, mode, stage, ceiling, evidence, dates, key ID, risk policy, or allowlist.
- [ ] Test one active authority per account/artifact, explicit supersession, revocation, expiry, restart persistence, and stale replay.
- [ ] Enforce exact allowed stage ceilings: Canary 6%, Scale 1 9%, Scale 2 13.5%, Steady 15%; lower signed ceilings are allowed, higher values reject.
- [ ] Ensure production stores public authority only and cannot sign.
- [ ] Run focused tests and commit `feat(scaling): add signed allocation authority`.

### Task 2: Enforce the allocation ladder at command time

**Files:**
- Create: `trader/promotion/allocation_policy.py`
- Modify: `trader/automation/session_risk.py`
- Modify: `trader/trading/dispatch_guard.py`
- Modify: `trader/trading/command_stack.py`
- Create: `tests/scaling/test_allocation_policy.py`

**Interfaces:** Produces `AllocationPolicy.evaluate(intent, broker_snapshot, authority) -> AllocationDecision`; consumed by P3 initial and dispatch risk checks.

- [ ] Add tests for current gross exposure plus proposed worst-case exposure, pending entry orders, partial fills, market gaps, overlapping symbols, and stale broker generations.
- [ ] Add property tests proving a lower signed ceiling cannot permit an order rejected by a higher ceiling for any otherwise identical state.
- [ ] Enforce at initial risk decision and immediate dispatch revalidation; include working orders to avoid over-allocation races.
- [ ] Reject authority mismatch/absence/expiry. Persist authority ID and every limit candidate in risk decision evidence.
- [ ] Preserve the 5% position, 3-position, 0.20% trade, and 0.50% daily limits at every stage.
- [ ] Run focused tests and commit `feat(scaling): enforce signed gross allocation ladder`.

### Task 3: Implement Scale 1/Scale 2/steady evidence decisions

**Files:**
- Create: `trader/promotion/scaling_gate.py`
- Modify: `trader/promotion/controller.py`
- Modify: `trader/mmr_cli.py`
- Create: `tests/scaling/test_scaling_gate.py`

**Interfaces:** Produces `ScalingGate.evaluate(current_stage, evidence) -> ScalingDecision` and `PromotionController.prepare_allocation`.

- [ ] Require P4 canary gate before Scale 1. Require at least 20 additional sessions and 50 additional round trips after Scale 1 authority start before Scale 2.
- [ ] Re-evaluate after-cost expectancy, prediction envelope, cost/slippage, drawdown, concentration, replay, reconciliation, incidents, and capacity at each stage.
- [ ] Test evidence before the current authority start cannot be double-counted toward the next stage.
- [ ] Implement preparation of unsigned allocation payload and offline sign/verify/activate commands. Activation requires current flat/reconciled/readiness state and a deterministic authenticated command.
- [ ] Steady-state authority requires an explicit observed capacity review and remains capped at 15%; there is no implicit Scale 3.
- [ ] Run focused tests and commit `feat(scaling): gate deliberate allocation increases`.

### Task 4: Add automatic degradation and allocation reduction

**Files:**
- Create: `trader/promotion/degradation_monitor.py`
- Modify: `trader/trading/circuit_breaker.py`
- Modify: `trader/data/allocation_authority_store.py`
- Create: `tests/scaling/test_degradation_monitor.py`

**Interfaces:** Produces `DegradationMonitor.evaluate(evidence) -> DegradationDecision` and restrictive `AllocationAuthorityStore.apply_override`.

- [ ] Test live costs above envelope, falling/negative expectancy, liquidity loss, drawdown, evidence inactivity, replay divergence, breaker trip, and capacity breach.
- [ ] Define deterministic actions: `WARN`, `REDUCE_TO_PREVIOUS_STAGE`, `SUSPEND`, `RETIRE`; safety/reconciliation always choose at least `SUSPEND`.
- [ ] Automatically write a restrictive override authority/event effective immediately. It may reduce only; any attempt to increase fails.
- [ ] Ensure existing positions are not expanded; policy may allow broker-verified reduction and scheduled flatten.
- [ ] Reinstatement requires a new signed operator authority and current evidence; restart cannot restore superseded allocation.
- [ ] Run focused tests and commit `feat(scaling): reduce allocation on evidence degradation`.

### Task 5: Build capacity and execution-quality monitoring

**Files:**
- Create: `trader/promotion/capacity.py`
- Modify: `trader/automation/attribution.py`
- Create: `tests/scaling/test_capacity.py`

**Interfaces:** Produces `CapacityMonitor.evaluate(attribution_window, proposed_allocation) -> CapacityDecision`; consumed by scaling and degradation gates.

- [ ] Attribute participation rate, top-of-book coverage, fill probability, partial-fill/cancel rate, spread paid, slippage distribution, latency, and market impact by instrument/time/regime/allocation stage.
- [ ] Test sparse/missing depth, censored unfilled orders, outliers, auction periods, and changed feed entitlement.
- [ ] Compare observed metrics to signed capacity envelope; do not extrapolate fills beyond observed liquidity.
- [ ] Reject scaling when projected quantity exceeds 0.25% ADV or approved depth/slicing policy, even when gross budget permits it.
- [ ] Feed breaches into degradation monitor and scaling reports.
- [ ] Run focused tests and commit `feat(scaling): measure live execution capacity`.

### Task 6: Add second-strategy portfolio admission analysis

**Files:**
- Create: `trader/promotion/portfolio_admission.py`
- Create: `trader/promotion/factor_exposure.py`
- Create: `tests/scaling/test_portfolio_admission.py`

**Interfaces:** `PortfolioAdmissionDecision(passed, covariance_window, stress_windows, factor_exposures, combined_loss, failures, evidence_refs)`.

- [ ] Require first strategy at successful Scale 2 and second strategy's own current research/paper/canary authority.
- [ ] Test aligned daily/intraday return covariance, overlapping positions, shared symbol/sector/market/volatility factors, regime covariance, drawdown overlap, and tail/stress dependence.
- [ ] Reject insufficient overlap/sample history, non-finite/singular inputs without a declared robust method, and material shared-factor concentration.
- [ ] Compute combined stressed daily loss under signed allocations and require it remain within the unchanged 0.50% account daily-loss limit.
- [ ] Record methodology/version/digest and exact evidence window. No dashboard statistic may substitute.
- [ ] Run focused tests and commit `feat(portfolio): gate second-strategy admission`.

### Task 7: Generalize account risk without weakening single-strategy safety

**Files:**
- Create: `trader/promotion/portfolio_risk_budget.py`
- Modify: `trader/automation/session_risk.py`
- Modify: `trader/automation/session_controller.py`
- Modify: `trader/trading/circuit_breaker.py`
- Create: `tests/scaling/test_portfolio_risk_budget.py`

**Interfaces:** Produces `PortfolioRiskBudget.evaluate(intents, broker_snapshot, authorities) -> PortfolioRiskDecision`; consumed by the single coordinator serialization point.

**Journal migration 52:** signed `portfolio_risk_authority` and append-only utilization events.

- [ ] Test combined gross, position count, correlated sector/factor exposure, working-order exposure, daily loss, and simultaneous flatten.
- [ ] Preserve global breaker and 0.50% daily-loss ceiling; a strategy-local profit cannot offset another's safety incident.
- [ ] Test deterministic allocation of remaining risk capacity and simultaneous-intent race serialization through the one coordinator.
- [ ] Keep one broker-verified account flatten authority capable of reducing both strategies and resolving external positions.
- [ ] Require a signed portfolio authority before enabling strategy two; absence leaves strategy one behavior unchanged.
- [ ] Run focused tests and commit `feat(portfolio): enforce combined account risk budget`.

### Task 8: Expose safe scaling observability and operational rollback

**Files:**
- Modify: `web/command_center/state.py`
- Modify: `web/command_center/routes_read.py`
- Modify: `web/templates/command_center.html`
- Modify: `web/static/command_center.js`
- Create: `tests/scaling/test_scaling_dashboard.py`
- Modify: `docs/superpowers/rollout/trading-income-operations-runbook.md`

**Interfaces:** Consumes domain snapshot/events for allocation/promotion entities and existing authenticated command routes; produces read-only operator observability.

- [ ] Render authoritative current stage, signed ceiling, utilization, expiry, evidence progress, capacity/degradation, and portfolio admission state from domain events/snapshot.
- [ ] Clearly distinguish “eligible”, “authorized”, “active”, “suspended”, and “passed”; never color missing evidence green.
- [ ] UI may prepare/view status but cannot locally calculate or override allocation. Activation uses existing authenticated command flow and correlated authoritative outcome.
- [ ] Add mobile/accessibility and stale-state tests; redact account/order identifiers and signed payload internals from ordinary responses.
- [ ] Document immediate scale-down, suspension, authority revocation, key rotation, backup/restore, and return-to-paper procedures.
- [ ] Run focused tests and commit `feat(cockpit): show authoritative scaling evidence`.

### Task 9: Pass scaling and portfolio recovery gates

**Files:**
- Create: `tests/integration/test_scaling_vertical_slice.py`
- Create: `scripts/scaling_fault_drill.py`
- Create: `docs/superpowers/rollout/trading-income-scaling-log.md`

**Interfaces:** Produces a signed scaling fault/release report and sanitized evidence digests; consumes the complete P5 authority and portfolio stack.

- [ ] Test signed Canary -> Scale 1 -> Scale 2 transitions with synthetic evidence, then prove real activation waits on actual evidence IDs.
- [ ] Inject capacity degradation, cost breach, allocation replay, simultaneous intents, service restart, key revocation, and account loss; assert fail-closed reduction/suspension.
- [ ] Test a rejected second strategy for insufficient covariance and an accepted fixture whose combined stressed loss remains within 0.50%.
- [ ] Run the canonical full suite, Compose checks, and a complete paper-session multi-strategy shadow drill before any second live strategy.
- [ ] Accumulate actual stage evidence and obtain signed independent review before each allocation increase; record sanitized report/authority digests only.
- [ ] Commit `test(scaling): add allocation and portfolio release gates`.

## P5 exit criteria

- Every capital increase is signed, exact, expiring, revocable, and manually activated.
- Trader policy enforces the allocation at approval and dispatch, including pending orders.
- Degradation reduces/suspends automatically but never increases authority.
- Capacity is based on realized execution evidence, not backtest optimism.
- A second strategy cannot run until independent eligibility and combined portfolio risk pass without raising the 0.50% account daily-loss ceiling.
