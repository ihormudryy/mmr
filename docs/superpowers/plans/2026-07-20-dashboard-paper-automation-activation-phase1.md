# Dashboard Paper Automation Activation — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** From the Scaling tab, Activate paper automation generates FS keys + fixture artifact, atomically persists YAML, and returns `restart_required` so a service restart arms P3 for one strategy.

**Architecture:** New trader-side `PaperAutomationActivationService` owns keygen, fixture export, and atomic YAML patches (Approach A). Dashboard posts through the existing command gateway. No in-process hot-arm in Phase 1. Phase 2 (hot-arm) is outlined only in the appendix — do not implement it in this plan.

**Tech Stack:** CPython 3.12, typed RPC / CommandStack, FastAPI command-center, vanilla JS Scaling tab, pytest, Ed25519 via `trader.research.signing`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-20-dashboard-paper-automation-activation-design.md` (approved).
- Hybrid rules: `docs/superpowers/specs/2026-07-20-hybrid-paper-auto-live-propose-design.md`.
- Private keys: filesystem PKCS8 PEM `0o600` only — never DuckDB, RPC body, logs, or browser.
- Paper account mode only; refuse live / `automation.live_enabled`.
- Refuse Activate unless `command_authority.enabled` is true.
- Exactly one `automation.strategy_name`; strategy must not have `auto_execute: propose` (R1).
- Persist last in Phase 1 means: materials + YAML written; **no** late-register / IntentEmitter build.
- Atomic YAML: temp file → fsync → `os.replace` → best-effort parent fsync; log redacted diff only.
- Do not mix unrelated dashboard/allocation WIP into these commits.

## File map

| File | Responsibility |
|------|----------------|
| `trader/automation/paper_materials.py` | Shared keygen/reuse, fixture bundle export, orphan cleanup |
| `trader/automation/paper_activation.py` | Phase 1 activate/deactivate + status + atomic YAML helpers |
| `trader/messaging/production_api.py` | Request models + register activate/deactivate/status |
| `trader/trading/command_stack.py` | Construct + attach `PaperAutomationActivationService` |
| `scripts/bootstrap_paper_automation.py` | Thin CLI wrapper over `paper_materials` |
| `web/command_center/routes_commands.py` | HTTP POST activate/deactivate |
| `web/command_center/routes_read.py` / snapshot | Expose `paper_automation` read model |
| `web/templates/command_center.html` | Scaling-tab Paper automation section |
| `web/static/command_center.js` | Activate/Deactivate handlers + render |
| `tests/automation/test_paper_materials.py` | Keys + fixture + orphan cleanup |
| `tests/automation/test_paper_activation.py` | Gates, activate/deactivate, atomic YAML |
| `tests/test_command_routes.py` | Dashboard HTTP + CSRF gates |
| `docs/superpowers/rollout/trading-income-operations-runbook.md` | Dashboard Activate note |

---

### Task 1: Shared paper materials (keys + fixture)

**Files:**
- Create: `trader/automation/paper_materials.py`
- Create: `tests/automation/test_paper_materials.py`
- Modify: `scripts/bootstrap_paper_automation.py`

**Interfaces:**
- Produces:
  - `ensure_signing_keypair(*, private_key_path: Path, public_key_path: Path, force: bool = False) -> tuple[AttestationSigner, bool]` — `(signer, reused_existing)`. Private `0o600`; public `0o644`. Reuse when both exist and load.
  - `export_fixture_paper_eligible_bundle(*, signer: AttestationSigner, artifacts_root: Path) -> str` — returns `artifact_id`; on failure after creating a new dir, best-effort `rmtree`.
  - `default_key_paths(config_dir: Path) -> tuple[Path, Path, Path]` → `(private_pem, verify_dir, public_pem)`

- [ ] **Step 1: Write failing tests** in `tests/automation/test_paper_materials.py`:
  - `test_ensure_signing_keypair_writes_0600_and_reuses`
  - `test_export_fixture_bundle_is_paper_eligible`
  Assert private mode `0o600`, reuse returns same `public_key_id`, bundle has `attestation.json` + `manifest.json`.

- [ ] **Step 2: Run** `uv run --frozen --extra test pytest tests/automation/test_paper_materials.py -q --timeout=60` — expect FAIL (module missing).

- [ ] **Step 3: Implement `trader/automation/paper_materials.py`** by extracting `_write_private_key`, `_write_public_key`, `_build_and_export`, `_evidence` from `scripts/bootstrap_paper_automation.py`. Refactor bootstrap to call the shared helpers and print snippets.

- [ ] **Step 4: Re-run tests — expect PASS.**

- [ ] **Step 5: Commit** `refactor(automation): shared paper keygen and fixture bundle export`

---

### Task 2: Atomic YAML helpers + Phase 1 activation service

**Files:**
- Create: `trader/automation/paper_activation.py`
- Create: `tests/automation/test_paper_activation.py`

**Interfaces:**
- Consumes: Task 1 helpers
- Produces:
  - `PaperAutomationActivationError` with `.code: str`
  - `PaperAutomationStatus` dataclass: `lifecycle`, `strategy_name`, `artifact_id`, path fields, `restart_required`, `armed_unpersisted` (False in Phase 1), `last_error`, `command_authority_ready`, `account_mode`, `last_activated_at`, `phase` (None)
  - `PaperAutomationActivationService(trader_yaml_path, strategy_yaml_path, config_dir, share_dir, account_mode, command_authority_enabled, now)`
    - `status() -> PaperAutomationStatus`
    - `activate(*, strategy_name: str, reason: str) -> dict` → `lifecycle=restart_required`
    - `deactivate(*, reason: str) -> dict` → `restart_required=true`
  - Error codes: `NOT_PAPER`, `COMMAND_AUTHORITY_REQUIRED`, `STRATEGY_NOT_FOUND`, `STRATEGY_HAS_PROPOSE`, `AUTOMATION_ALREADY_BOUND`, `LIVE_AUTOMATION_REFUSED`

- [ ] **Step 1: Write failing tests** covering: refuse without CA; refuse `auto_execute: propose`; activate writes trader+strategy YAML and returns `restart_required`; deactivate clears `enabled`; status shows `restart_required` / `degraded` when enabled but artifact missing.

- [ ] **Step 2: Run** `pytest tests/automation/test_paper_activation.py -q --timeout=60` — expect FAIL.

- [ ] **Step 3: Implement service** with atomic YAML writer:

```python
def _atomic_write_yaml(path: Path, data: dict) -> None:
    import os, yaml
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
```

Log redacted automation field diffs only. Strip `auto_execute: propose` on activate. Do **not** late-register or build IntentEmitter.

- [ ] **Step 4: Re-run** materials + activation tests — expect PASS.

- [ ] **Step 5: Commit** `feat(automation): Phase 1 paper Activate/Deactivate with atomic YAML`

---

### Task 3: Wire typed RPC on trader command stack

**Files:**
- Modify: `trader/messaging/production_api.py`
- Modify: `trader/trading/command_stack.py`
- Modify: `tests/test_command_stack.py`

**Interfaces:**
- `ActivatePaperAutomationRequest(command_id, strategy_name, reason, preflight_nonce: Optional[str]=None)`
- `DeactivatePaperAutomationRequest(command_id, reason)`
- Register: `activate_paper_automation` with `requires_preflight=True`; `deactivate_paper_automation` with `requires_preflight=False`; query `get_paper_automation_status`
- `CommandStack.paper_automation_service`
- Pass into `register_command_authority(..., paper_automation_service=...)`

- [ ] **Step 1: Failing test** — enabled stack registers activate/deactivate/status methods when service is constructed.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** service construction in `build_command_stack` (paths from `~/.config/mmr`, `strategy_config_file`, `~/.local/share/mmr`; `account_mode` from paper/live; `command_authority_enabled=policy.enabled`). Map `PaperAutomationActivationError.code` into RPC failure.

- [ ] **Step 4: Run** `pytest tests/test_command_stack.py tests/automation/ -q --timeout=60` — expect PASS.

- [ ] **Step 5: Commit** `feat(command-plane): register paper automation activate/deactivate RPC`

---

### Task 4: Dashboard HTTP routes + read model

**Files:**
- Modify: `web/command_center/routes_commands.py`
- Modify: `web/command_center/routes_read.py` (and snapshot enrichment as needed)
- Modify: `tests/test_command_routes.py`
- Modify: `tests/test_command_security_gate.py` if `_PREFLIGHT_ACTIONS` allowlist needs `activate_paper_automation`

**Interfaces:**
- `POST /api/commands/paper-automation/activate` → gateway `activate_paper_automation`
- `POST /api/commands/paper-automation/deactivate` → gateway `deactivate_paper_automation`
- Snapshot field `paper_automation` from query `get_paper_automation_status` (prefer trader RPC over web reading YAML)

- [ ] **Step 1: Failing tests** — CSRF; commands disabled 403; activate without nonce 428; mocked happy path.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** bodies, routes, preflight allowlist, snapshot inclusion.

- [ ] **Step 4: Run** `pytest tests/test_command_routes.py tests/test_command_security_gate.py -q --timeout=60` — expect PASS.

- [ ] **Step 5: Commit** `feat(dashboard): paper automation activate/deactivate API and snapshot`

---

### Task 5: Scaling-tab UI

**Files:**
- Modify: `web/templates/command_center.html`
- Modify: `web/static/command_center.js`
- Modify: `web/templates/_guide_tab.html`
- Modify: `docs/superpowers/rollout/trading-income-operations-runbook.md`

**Exact UI copy:**
- Section: `Paper automation`
- Banner `restart_required`: `Config written. Restart trader and strategy services to arm.`
- Banner `armed_unpersisted` (forward-compat): `Activation partially succeeded — click Activate again to complete.`
- Buttons: `Activate paper automation` / `Deactivate`

- [ ] **Step 1: HTML** below allocation controls — `#paper-auto-strategy` select, reason, buttons, status cards, banners.

- [ ] **Step 2: JS** — `renderPaperAutomation`, populate strategies from `store.view.strategies`, `ccActivatePaperAutomation` via `ccRunLiveCeremony('activate_paper_automation', ...)`, deactivate via confirm + `ccSubmitCommand`.

- [ ] **Step 3: Smoke** (if stack available): Scaling tab shows section; controls gated on `commands_enabled`.

- [ ] **Step 4: Commit** `feat(dashboard): Scaling tab Activate paper automation UI`

---

### Task 6: Verification + docs closeout

- [ ] **Step 1: Focused suite**

```bash
uv run --frozen --extra test pytest \
  tests/automation/test_paper_materials.py \
  tests/automation/test_paper_activation.py \
  tests/test_command_stack.py \
  tests/test_command_routes.py \
  -q --timeout=60
```

Expected: all passed.

- [ ] **Step 2: Synthetic gates**

```bash
uv run --frozen python3 scripts/p1_release_gate.py --synthetic-only
uv run --frozen python3 scripts/p3_release_gate.py --synthetic-only
```

Expected: exit 0; synthetic phases PASSED.

- [ ] **Step 3: Update spec status line** to `approved; Phase 1 implemented` and commit docs if needed: `docs(ops): Phase 1 dashboard paper automation Activate`

---

## Spec coverage (Phase 1)

| Spec requirement | Task |
|------------------|------|
| BE FS keygen, not DB | 1–2 |
| Idempotent key reuse | 1–2 |
| Fixture export + orphan cleanup | 1–2 |
| Atomic YAML + redacted diff | 2 |
| Refuse without CA / not paper / propose | 2–3 |
| One strategy bind | 2 |
| `restart_required`, no hot-arm | 2–3 |
| Scaling UI + banners | 5 |
| Preflight on activate | 3–4 |
| Deactivate best-effort persist | 2 |
| Startup trust YAML / degraded | 2 |
| Private key never in response | 2–4 |
| Phase 2 hot-arm | Appendix only |

---

## Appendix — Phase 2 (do not implement here)

Separate plan later: prepare→trader_commit→strategy_commit→verify→persist; compensate; emit gate; `armed_unpersisted` retry; chaos tests at each boundary; late-register `execute_automated_intent` + IntentEmitter without restart.

