# Hybrid Paper-Auto / Live-Propose Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Paper runs one signed strategy unattended; live queues proposals for human approve; automation never places live orders.

**Architecture:** Fail-closed mode matrix from `docs/superpowers/specs/2026-07-20-hybrid-paper-auto-live-propose-design.md`. Dispatch exclusivity in strategy_runtime; live propose gated by command-authority live policy; nested automation config loaded; `AutomatedIntentCommandService` wired into production command stack when paper automation is enabled.

**Tech Stack:** CPython 3.12, existing typed RPC / CommandStack, SignalProposer, IntentEmitter, pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-20-hybrid-paper-auto-live-propose-design.md` (approved).
- `automation_live_enabled` remains false; refuse startup if true.
- Exactly one `automation_strategy_name` when automation enabled.
- Same signal never both proposes and emits an automated intent.
- Live propose requires `command_authority.enabled` and `live_enabled` with pinned account.

---

### Task 1: Dispatch exclusivity (propose XOR intent)

**Files:**
- Modify: `trader/strategy/strategy_runtime.py` (`_dispatch_signal`)
- Create/Modify: `tests/test_signal_dispatch_exclusivity.py` (or extend existing strategy_runtime tests)

- [ ] Write failing test: when IntentEmitter armed for strategy, SignalProposer is not called.
- [ ] Write failing test: when emitter not armed and `auto_execute=propose`, proposer is called.
- [ ] Implement exclusivity in `_dispatch_signal` (R1).
- [ ] Run focused tests; commit `fix(strategy): exclusive propose vs automated intent dispatch`.

### Task 2: Live-capable SignalProposer gate (R2)

**Files:**
- Modify: `trader/strategy/signal_proposer.py`
- Modify: `trader/strategy/strategy_runtime.py` (pass live-authority flag)
- Modify/Create: `tests/test_signal_proposer.py`

- [ ] Write failing tests: live + live_authority → propose; live without → skip+warn; paper → propose.
- [ ] Replace paper-only `_gate` with R2.
- [ ] Run focused tests; commit `feat(propose): allow live auto_execute propose under live command authority`.

### Task 3: Nested automation config loading (R4)

**Files:**
- Modify: `trader/config.py` (`MMRConfig.from_yaml`)
- Modify: `tests/test_config.py` / `tests/test_research_config.py` as appropriate

- [ ] Write failing test: nested `automation:` populates `AutomationConfig`.
- [ ] Write test: flat `automation_*` still wins / merges correctly with env override.
- [ ] Implement nested merge; refuse `automation_live_enabled=true` at validate/startup if not already.
- [ ] Run focused tests; commit `fix(config): load nested automation YAML block`.

### Task 4: Wire AutomatedIntentCommandService into production stack

**Files:**
- Modify: `trader/trading/command_stack.py`
- Modify: `trader/trading/trading_runtime.py` / `trader/trader_service.py` as needed
- Modify: `trader/messaging/production_api.py` (already supports `automated_intent_service=`)
- Create/Modify: `tests/test_command_stack.py` or integration test

- [ ] Write failing test: paper automation enabled → registry resolves `execute_automated_intent` on command role.
- [ ] Write failing test: automation disabled → method not registered.
- [ ] Construct service + pass into `register_command_authority` when enabled.
- [ ] Run focused tests; commit `feat(command-plane): register automated intent on paper command stack`.

### Task 5: Activation helpers — keys, fixture artifact, user-config template

**Files:**
- Create: `scripts/bootstrap_paper_automation.py` (keys + optional fixture bundle export from test factory)
- Modify: `docs/superpowers/rollout/trading-income-operations-runbook.md` (hybrid section)
- Do **not** commit private keys; write to `~/.config/mmr/` / `~/.local/share/mmr/` only

- [ ] Script: generate Ed25519 keypair (0o600 private), public PEM into keys dir, export one PAPER_ELIGIBLE bundle via `build_populated_db`-style path or research DB if present.
- [ ] Print exact flat/nested trader.yaml snippet + strategy YAML changes for one strategy.
- [ ] Document P1 then P3 soak commands.
- [ ] Commit `feat(ops): bootstrap paper automation artifact and hybrid runbook`.

### Task 6: Synthetic gates + optional IB soak attempt

**Files:** none required beyond prior

- [ ] Run `p1_release_gate.py --synthetic-only` and `p3_release_gate.py --synthetic-only`.
- [ ] If Docker+IB available during RTH, run `--ib-paper` phases; otherwise leave operator checklist.
- [ ] Commit any gate/script fixes discovered.

## Exit criteria

- Paper: one strategy can emit intents into a wired coordinator path (synthetic proven; IB soak when available).
- Live: propose works under live authority; automation cannot submit.
- Config nested + flat both work; exclusivity tests green.
