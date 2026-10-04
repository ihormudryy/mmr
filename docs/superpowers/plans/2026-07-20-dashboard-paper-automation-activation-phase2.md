# Dashboard Paper Automation Activation — Phase 2 Hot-Arm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One Scaling-tab Activate click prepares materials, late-registers trader `execute_automated_intent`, arms strategy IntentEmitter, verifies both sides, then persists YAML — no service restart for steady-state arm. Deactivate tears down memory first, then best-effort YAML.

**Architecture:** Extend `PaperAutomationActivationService` with injectable hot-arm ports (trader registrar + strategy arm/disarm client). Add `TypedRpcRegistry.unregister` and late-register helpers on the command stack. Add strategy-service RPCs `arm_paper_automation` / `disarm_paper_automation`. Same dashboard RPC names; success lifecycle becomes `armed` or `armed_unpersisted`.

**Tech Stack:** CPython 3.12, TypedRpcRegistry, CommandStack, StrategyRuntime, FastAPI command-center, pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-20-dashboard-paper-automation-activation-design.md` § Phase 2 (approved).
- Phase 1 gates unchanged: paper only, CA required, R1 no `auto_execute: propose`, one strategy bind, private keys FS-only.
- Activate phase order: `prepare_keys` → `export_artifact` → `trader_commit` → `strategy_commit` → `verify` → `persist`.
- On any failure before successful verify: compensate (memory off both sides) → `failed`.
- Persist fail after verify → leave memory armed → `armed_unpersisted`; retry Activate completes persist without re-keying.
- Startup still trusts YAML only; never invent enablement from `armed_unpersisted` across process death.
- Emit gate: no intents while partially committed; trader refuses mismatched strategy/artifact; emitter clears on disarm.
- Prefer registry `unregister`; if already registered at startup from YAML, replace/refuse-wrap on deactivate as needed.
- Do not implement optional force-delete keys/fixtures or “Restart services” button.
- Do not mix unrelated dashboard/allocation WIP.

## File map

| File | Responsibility |
|------|----------------|
| `trader/messaging/typed_rpc.py` | `unregister(role, method)` |
| `trader/automation/paper_hot_arm.py` | Ports + trader late-register/unregister helpers |
| `trader/automation/paper_activation.py` | Phase 2 activate/deactivate state machine + status |
| `trader/trading/command_stack.py` | Wire hot-arm ports into activation service |
| `trader/messaging/production_api.py` | Keep activate/deactivate handlers; support late register of execute path |
| `trader/strategy/strategy_runtime.py` | `arm_paper_automation` / `disarm_paper_automation` + typed handlers |
| `web/static/command_center.js` | Confirm copy for hot-arm (no restart required on success) |
| `tests/messaging/test_typed_rpc_registry.py` or extend existing | unregister |
| `tests/automation/test_paper_activation.py` | Phase 2 lifecycle + chaos injection hooks |
| `tests/automation/test_paper_hot_arm.py` | Port orchestration / compensate |
| `tests/test_strategy_runtime_*.py` or new | arm/disarm emitter |
| `docs/PAPER_AUTOMATION_SETUP.md` + design status | Phase 2 shipped note |

---

### Task 1: TypedRpcRegistry.unregister

**Files:**
- Modify: `trader/messaging/typed_rpc.py`
- Modify or create: registry tests under `tests/`

- [ ] **Step 1:** Failing test — register then unregister removes `contains`; resolve returns None; re-register succeeds.
- [ ] **Step 2:** Implement `unregister(self, socket_role: str, method: str) -> bool` (True if removed). Clear `_method_role` when no other role holds the method.
- [ ] **Step 3:** Tests pass. Commit `feat(rpc): allow TypedRpcRegistry unregister for hot-arm`.

---

### Task 2: Strategy arm / disarm surface

**Files:**
- Modify: `trader/strategy/strategy_runtime.py`
- Create/extend: `tests/test_strategy_paper_arm.py` (or nearest existing)

**Interfaces:**
- `StrategyRuntime.arm_paper_automation(*, strategy_name, artifact_bundle_path, public_key_ring_path, expected_artifact_id) -> dict`
  - Sets in-memory `automation_enabled=True`, paths, `automation_strategy_name`.
  - Clears prior emitter; verifies artifact for `strategy_name`; builds IntentEmitter via `_maybe_build_intent_emitter`.
  - Requires `_trader_command_client` present (else fail closed).
  - Refuse live / `automation_live_enabled`.
- `StrategyRuntime.disarm_paper_automation() -> dict`
  - Clears emitter, `_verified_artifact*`, automation enable flags / strategy name / expected id (paths may remain empty).
- Typed command handlers registered in `register_strategy_control_authority`.

- [ ] **Step 1:** Failing tests for arm builds emitter; disarm clears; refuse when client missing.
- [ ] **Step 2:** Implement + register RPCs.
- [ ] **Step 3:** Tests pass. Commit `feat(strategy): arm/disarm paper automation without restart`.

---

### Task 3: Hot-arm ports + Phase 2 activation service

**Files:**
- Create: `trader/automation/paper_hot_arm.py`
- Modify: `trader/automation/paper_activation.py`
- Modify: `tests/automation/test_paper_activation.py`
- Create: `tests/automation/test_paper_hot_arm.py`

**Interfaces:**
```python
class PaperHotArmPorts(Protocol):
    def trader_commit(self, *, strategy_name: str, artifact_id: str,
                      artifact_bundle_path: str, public_key_ring_path: str) -> None: ...
    def trader_compensate(self) -> None: ...
    def strategy_commit(self, *, strategy_name: str, artifact_id: str,
                        artifact_bundle_path: str, public_key_ring_path: str) -> None: ...
    def strategy_compensate(self) -> None: ...
    def verify_ready(self, *, strategy_name: str, artifact_id: str) -> None: ...
```

- `PaperAutomationActivationService(..., hot_arm: PaperHotArmPorts | None = None)`
  - When `hot_arm is None`: keep Phase 1 behaviour (`restart_required`) for unit tests that don't inject ports.
  - When ports present: Phase 2 algorithm; `phase` field updates during activate; status reflects memory lifecycle.
- Injectable `_fail_after: str | None` (tests only) to interrupt after named phase for chaos tests.
- `activate` success → `lifecycle=armed`, `restart_required=False` (or `armed_unpersisted` if persist fails).
- Idempotent: already `armed` for same strategy → return armed without re-keying.
- `armed_unpersisted` retry → skip prepare/commits when memory already matches; persist only.
- `deactivate` → strategy_compensate + trader_compensate first; then YAML `enabled: false`; lifecycle `disabled`, `restart_required=False`.

- [ ] **Step 1:** Failing tests for happy path, compensate on strategy_commit fail, persist fail → armed_unpersisted, retry persist, deactivate tears down memory.
- [ ] **Step 2:** Implement.
- [ ] **Step 3:** Tests pass. Commit `feat(automation): Phase 2 hot-arm activate/deactivate state machine`.

---

### Task 4: Wire production hot-arm ports

**Files:**
- Modify: `trader/trading/command_stack.py`
- Modify: `trader/messaging/production_api.py` (if needed for late-register helper)
- Extend: `tests/test_command_stack.py`

**Action:**
- Build `AutomatedIntentCommandService` from activation paths (reuse `_build_automated_intent_service` logic with explicit attrs or a shared builder).
- Late-register coordinator action + typed `execute_automated_intent` when not already registered; on compensate unregister or install refuse wrapper.
- Strategy port calls strategy typed command `arm_paper_automation` / `disarm_paper_automation` via existing strategy control client.
- `verify_ready`: trader registry contains execute method; strategy query or arm response confirms emitter for strategy+artifact.
- Status: when memory armed after startup from YAML (`automated_intent_service is not None`), surface `lifecycle=armed` not `restart_required`.

- [ ] **Step 1:** Test stack can late-register execute when starting with automation disabled.
- [ ] **Step 2:** Wire ports; fix status when already armed at boot.
- [ ] **Step 3:** Commit `feat(command-plane): wire paper automation hot-arm ports`.

---

### Task 5: Dashboard UI copy

**Files:**
- Modify: `web/static/command_center.js`
- Optionally: `web/templates/command_center.html` banner text (already has partial banner)

- [ ] Update Activate confirm: arms in-process; restart only if lifecycle stays `restart_required` (should not for Phase 2 success).
- [ ] Update Deactivate confirm: clears arm immediately; YAML updated best-effort.
- [ ] Message strings for `armed` / `armed_unpersisted` / `preparing` / `failed`.
- [ ] Commit `fix(dashboard): Phase 2 paper automation confirm and status copy`.

---

### Task 6: Docs + design status

**Files:**
- `docs/superpowers/specs/2026-07-20-dashboard-paper-automation-activation-design.md` — status line Phase 2 implemented
- `docs/PAPER_AUTOMATION_SETUP.md` — Activate no longer requires restart when hot-arm succeeds
- Ops runbook hybrid section if it still says restart-required only

- [ ] Commit `docs: paper automation Phase 2 hot-arm`.

---

## Verification

```bash
.venv/bin/pytest tests/automation/test_paper_activation.py tests/automation/test_paper_hot_arm.py \
  tests/automation/test_paper_materials.py -q --timeout=60
# plus new strategy arm tests and typed registry unregister tests
.venv/bin/pytest tests/test_command_stack.py -k paper -q --timeout=60
.venv/bin/pytest tests/test_command_routes.py -k paper_automation -q --timeout=60
```

Synthetic gates still green: `scripts/p1_release_gate.py --synthetic-only`, `scripts/p3_release_gate.py --synthetic-only` (if env allows).

## Success definition

- Activate with hot-arm ports → `armed` without restart; IntentEmitter + `execute_automated_intent` live.
- Persist failure → `armed_unpersisted`; retry finishes persist.
- Failure mid-commit → compensate; `failed`; no emit path.
- Deactivate clears memory even if YAML write fails.
- Startup with YAML enabled + good materials → `armed` (not stuck on `restart_required`).
