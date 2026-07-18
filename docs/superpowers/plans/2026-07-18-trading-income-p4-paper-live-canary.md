# P4 Paper and Live-Canary Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accumulate trustworthy paper evidence, promote through an authenticated distinct live-canary attestation, and operate the 6% canary with zero tolerance for capital-safety incidents.

**Architecture:** Trader-owned evidence counters consume P3 attribution/replay and broker truth. Paper and live stages are explicit durable state machines; elapsed time alone never promotes. An offline promotion controller prepares authority, but only a signed operator action activates live. Incidents atomically pause, cancel, reduce, reconcile, and suspend where required.

**Tech Stack:** P1-P3 services, journal migrations 40-49, offline Ed25519 signer, Docker Compose/IB paper and live profiles, pytest, fault injection.

## Global Constraints

- Software completion cannot mark paper or live gates passed. Required real sessions/trades must accumulate.
- `PAPER_ELIGIBLE` never authorizes live; live requires a new `CANARY_ELIGIBLE` signature bound to exact account mode, allowlist, risk policy, and 6% allocation.
- One safety incident cannot be averaged away by profit or sample size.
- Live activation and breaker reset are separate authenticated actions with separate deterministic IDs and audit entries.

---

### Task 1: Build durable evidence windows and stage state machine

**Files:**
- Create: `trader/promotion/__init__.py`
- Create: `trader/promotion/evidence_store.py`
- Create: `trader/promotion/stage.py`
- Modify: `trader/data/schema_migrations.py`
- Create: `tests/promotion/test_evidence_store.py`
- Create: `tests/promotion/test_stage_machine.py`

**Interfaces:** Produces `EvidenceStore.append`, `EvidenceStore.project`, and `PromotionStageMachine.transition`; consumes authoritative P3 attribution/replay/breaker events.

**Journal migrations 40-42:** append-only `promotion_evidence_events`, derived `promotion_evidence_windows`, and `strategy_promotion_state` with stages `PAPER_COLLECTING`, `PAPER_FAILED`, `PAPER_PASSED`, `CANARY_AUTHORIZED`, `CANARY_ACTIVE`, `CANARY_SUSPENDED`, `CANARY_PASSED`.

- [ ] Test idempotent evidence ingestion by source event ID, out-of-order events, corrections, restarts, session boundaries, and recomputation from append-only events.
- [ ] Test monotonic allowed transitions and forbid direct paper-to-live, automatic transition, or activation on expired/changed authority.
- [ ] Test code/config/allowlist/risk/data correction, 30-day evidence inactivity, breaker trip, cost breach, and drawdown expiry/suspension.
- [ ] Implement pure projections; mutation and corresponding domain event commit atomically.
- [ ] Run focused tests and commit `feat(promotion): add durable evidence stage machine`.

### Task 2: Implement paper/shadow operation and paper evidence gate

**Files:**
- Create: `trader/promotion/paper_gate.py`
- Create: `scripts/paper_evidence_report.py`
- Modify: `scripts/run_paper_soak.py`
- Create: `tests/promotion/test_paper_gate.py`

**Interfaces:** Produces `PaperGate.evaluate(EvidenceWindow) -> PromotionDecision` and a deterministic paper evidence JSON report.

- [ ] Add exact simultaneous floors: 30 calendar days, 20 completed sessions, 50 round trips, five instruments.
- [ ] Test no divergence, duplicate, unresolved ambiguity/alert, missed flat, replay mismatch, stressed-cost breach, negative expectancy, and concentration each independently blocks.
- [ ] Define correction impact: affected session/trade is removed and its session/trade counter resets; safety corrections reset the clean-session streak. Record old/new evidence projections.
- [ ] Add shadow mode that computes intents/decisions but never registers `execute_automated_intent`; compare shadow trace to paper trace on the same sealed input.
- [ ] Extend soak reports with breaker/readiness/reconciliation/protection/flat/replay metrics and signed configuration digest.
- [ ] Run focused tests and commit `feat(promotion): enforce accelerated paper evidence gate`.

### Task 3: Add live statistical, concentration, liquidity, and halt evidence

**Files:**
- Create: `trader/promotion/live_metrics.py`
- Create: `trader/promotion/liquidity_evidence.py`
- Create: `tests/promotion/test_live_metrics.py`
- Create: `tests/promotion/test_liquidity_evidence.py`

**Interfaces:** Produces `LiveMetrics.evaluate(window) -> MetricDecision` and `LiquidityEvidence.evaluate(conid, window) -> InstrumentEligibility`.

- [ ] Test net expectancy, daily Sharpe/Sortino, prediction envelope, average/tail slippage, drawdown, best-trade removal, 35% trade profit, 40% day profit, instrument/regime concentration.
- [ ] Treat Sharpe/Sortino as corroborative only; missing/negative safety evidence always wins.
- [ ] Test five consecutive below-floor sessions suspend an instrument; an unscheduled halt suspends immediately.
- [ ] Implement halt requalification requiring five complete qualifying sessions, current liquidity, halt-day replay, and operator-reviewed event; no fixed-day shortcut.
- [ ] Use authoritative P3 attribution only and report unresolved rows separately.
- [ ] Run focused tests and commit `feat(promotion): evaluate live and liquidity evidence`.

### Task 4: Build fault-injection and recovery certification

**Files:**
- Create: `scripts/automation_fault_drill.py`
- Create: `trader/testing/faults.py`
- Create: `tests/integration/test_automation_faults.py`
- Modify: `docker-compose.yml`

**Interfaces:** Produces a deterministic fault-drill report; consumes only disposable Compose volumes, explicit injection-point names, and P1-P3 observability.

- [ ] Create deterministic injection points after command claim, initial approval, dispatch revalidation, IB send, broker acknowledgement, partial fill, protection submit, journal event, attribution append, and replay seal.
- [ ] Exercise trader/strategy/dashboard stop, IB disconnect, message duplication/reordering, stale/crossed quote, rejected protection, partial fill, external order/position, disk error, and process restart.
- [ ] Assert one command/order identity, no unsafe retry, durable breaker, reconciliation continuation, verified flat behavior, and attribution/replay consistency.
- [ ] Add backup/restore and unexpected power-loss drills using copied disposable volumes; never run destructive drills against production data.
- [ ] Produce a signed JSON report containing git/config/container/artifact digests and pass/fail per invariant.
- [ ] Run integration tests and commit `test(operations): certify automation fault recovery`.

### Task 5: Implement signed canary promotion and activation

**Files:**
- Create: `trader/promotion/controller.py`
- Create: `trader/promotion/canary_attestation.py`
- Modify: `trader/automation/artifact_verifier.py`
- Modify: `trader/messaging/production_api.py`
- Modify: `trader/trading/command_stack.py`
- Modify: `trader/mmr_cli.py`
- Create: `tests/promotion/test_canary_activation.py`

**Interfaces:** Produces `PromotionController.prepare_canary`, offline canary signing, and authenticated typed `activate_live_canary`/`deactivate_live_canary` commands.

**Journal migration 43:** append-only `live_activation_authority` with exact account, artifact, attestation, risk-policy, allowlist, gross allocation, operator, issue/expiry/revocation, and activation command.

- [ ] Test promotion preparation consumes only a passed current paper window and produces a distinct unsigned canary payload.
- [ ] Test offline signing and trader verification against exact live account, `CANARY_ELIGIBLE`, maximum 6%, one strategy, public key ID, expiry, and unchanged digests.
- [ ] Add authenticated commands `activate_live_canary` and `deactivate_live_canary`; activation requires preflight, semantic readiness, flat/reconciled broker state, breaker clear, and explicit reason.
- [ ] Test replays/conflicts, wrong paper/live account, expired/revoked authority, writable artifact, changed policy, second strategy, and automatic activation attempts.
- [ ] Add CLI preparation/verification/activation flow that never sends private key material to trader RPC.
- [ ] Run focused tests and commit `feat(promotion): require signed live-canary activation`.

### Task 6: Enforce canary incident response and drawdown

**Files:**
- Create: `trader/promotion/canary_risk.py`
- Modify: `trader/trading/circuit_breaker.py`
- Modify: `trader/automation/session_risk.py`
- Modify: `trader/automation/attribution.py`
- Create: `tests/promotion/test_canary_risk.py`

**Interfaces:** Produces `CanaryRiskController.observe(broker_snapshot, attribution) -> CanaryRiskState`; consumes P1 breaker/liquidation and P3 attribution.

**Journal migration 44:** `canary_high_water_marks` and append-only `capital_safety_incidents`.

- [ ] Test high-water mark updates from broker net liquidation attributable to canary scope, 3% drawdown, 0.50% daily loss including commissions/unrealized P&L, and restart persistence.
- [ ] Test missing protection, duplicate submission, account mismatch, missed flat, and unexplained position each cause one incident and immediate global pause.
- [ ] Assert the atomic response orders state changes safely: persist/trip breaker first, reject new exposure, cancel entries, reduce, reconcile, suspend artifact when required.
- [ ] A drawdown/safety suspension requires a fresh promotion review; breaker reset alone cannot reactivate the artifact.
- [ ] Add next-session restriction for daily-loss reset and require current broker truth.
- [ ] Run focused tests and commit `feat(canary): enforce capital-safety incident response`.

### Task 7: Add pre-session/post-session operating controls

**Files:**
- Create: `trader/operations/session_checklist.py`
- Create: `scripts/session_open_check.py`
- Create: `scripts/session_close_check.py`
- Modify: `trader/trader_service.py`
- Create: `tests/operations/test_session_checklist.py`
- Modify: `docs/superpowers/rollout/trading-income-operations-runbook.md`

**Interfaces:** Produces `SessionChecklist.run_pre` and `run_post`, with durable `SessionChecklistResult`; consumes P1 readiness and P3 replay/attribution.

- [ ] Pre-session tests require exact account/mode/artifact/allocation/XNYS schedule, promoted broker generation, quote coverage, risk evidence, prior flat/reconciled session, eligibility, and clear breaker.
- [ ] Post-session tests require no broker positions/orders, sealed replay, deterministic replay pass, attributed costs/P&L, evidence update, and acknowledgement of divergence.
- [ ] Make results durable and idempotent by session/artifact/config digest. Missing check keeps automation paused.
- [ ] Wire pre-session completion into semantic automation readiness and post-session failure into breaker/promotion evidence.
- [ ] Document manual emergency pause, verified flatten, disconnect, crash/restart, backup restore, key rotation/revocation, and activation rollback.
- [ ] Run focused tests and commit `feat(operations): enforce daily automation checklists`.

### Task 8: Accumulate and adjudicate paper evidence

**Files:**
- Create/append runtime evidence under configured reports directory (no repository data commit)
- Create: `docs/superpowers/rollout/trading-income-paper-log.md`

**Interfaces:** Consumes real paper-session evidence; produces a signed review and sanitized evidence-report digests. No software API is introduced.

- [ ] Deploy P3 in paper mode with exact signed `PAPER_ELIGIBLE` bundle and one-strategy config.
- [ ] Run every pre/post-session checklist, seal/replay each day, and execute scheduled failure drills in the paper account.
- [ ] Accumulate at least 30 calendar days, 20 sessions, 50 round trips, and five instruments. Failed gates extend the stage; never edit counters manually.
- [ ] Generate `paper_evidence_report.py`; independently review command duplicates, ambiguities, divergences, flat deadlines, costs, expectancy, concentration, and alerts.
- [ ] Sign the qualitative paper-to-canary review and prepare (but do not activate) a canary authority.
- [ ] Commit only the sanitized rollout log/report digests, never account IDs, order IDs, market-data payloads, or credentials.

### Task 9: Activate and adjudicate the live canary

**Files:**
- Create: `docs/superpowers/rollout/trading-income-canary-log.md`

**Interfaces:** Consumes signed canary authority and real live broker evidence; produces `CANARY_PASSED` or a durable suspended/collecting state plus sanitized report digests.

- [ ] Confirm production broker permissions, market-data entitlements, emergency contact path, backups, and rollback; begin at the minimum order size inside all limits.
- [ ] Activate exact 6% maximum gross authority with at most 3 positions, 5% per position, 0.20% per-trade risk, and 0.50% daily loss.
- [ ] Accumulate at least 30 live sessions, 75 round trips, and five instruments. Insufficient evidence extends canary.
- [ ] Require zero capital-safety incidents, complete resolution/flat/replay, positive after-cost expectancy, prediction-envelope fit, cost limits, <3% drawdown, and all concentration/anti-luck gates.
- [ ] Have an independent operator sign historical/paper/live deviation review. Economic or safety failure suspends; it never triggers looser risk.
- [ ] Commit only sanitized signed report digests and the decision, not sensitive broker evidence.

## P4 exit criteria

- Paper evidence satisfies every simultaneous floor and safety gate.
- Live authority is distinct, signed, exact, and revocable.
- Canary incidents atomically stop risk and cannot be averaged away.
- Daily operations and recovery drills are durable and replayable.
- The first live canary earns `CANARY_PASSED` or remains safely suspended/collecting; no schedule forces promotion.
