# Paper Docker E2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Opt-in `pytest -m paper_e2e` suite that exercises a real paper Compose stack (`./docker.sh -b -u`) via dashboard httpx + typed HMAC SDK — smoke (A), Portfolios/Strategies/Research/Scaling HTTP (B), and typed RPC (C) — skipping cleanly when the stack is down.

**Architecture:** Package `tests/paper_e2e/` with an **autouse** `paper_stack` fixture (healthz JSON → typed `get_status` → paper mode → IB upstream → capability probe via `METHOD_NOT_ALLOWED`). Dashboard mutations use session + `X-CSRF-Token` for `/api/commands/*`; Portfolios CRUD uses typed `42102` universe methods. Live-order and restart paths are env-gated with elevated timeouts and position/automation teardown.

**Tech Stack:** CPython 3.12, pytest + pytest-timeout, httpx, `trader.sdk.MMR` / `TypedRpcClient` + `TypedRpcRemoteError`, Docker Compose paper stack on `127.0.0.1:7424` / `42101` / `42102`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-23-paper-docker-e2e-design.md` (approved; METHOD_NOT_ALLOWED probe).
- Stack down → **skips only** (exit 0), never red.
- Unwired commands → skip on `METHOD_NOT_ALLOWED`; `VALIDATION_ERROR` / `PROPOSAL_NOT_FOUND` / `COMMAND_NOT_FOUND` mean **registered**.
- Portfolios mutations: typed RPC primary; optional one legacy form create with `form_csrf`.
- Research propose: only `POST /api/commands/proposals` (no `/api/research/*/propose`).
- Exact RPC ids: `activate_allocation` / `suspend_allocation`, `activate_paper_automation` / `deactivate_paper_automation`.
- Strategy path param: `{strategy_name}`.
- HMAC: `MMR_SERVICE_HMAC_KEY_FILE` / `~/.config/mmr/service_hmac.key` — not `MMR_HMAC_SECRET`.
- Temp names / teardown: this-run `e2e_{pid}_{ts}` prefix only.
- Timeouts: default 120s stamped via `pytest_collection_modifyitems` for items under `tests/paper_e2e/`; IB/research 180s; live-orders/restart 300s (override global 30s). **Do not** put `pytestmark` in `conftest.py` — it is a no-op there.
- Capability command probes: **always body `{}`**. Never send a body that could pass server-side validation for a risk-increasing command (validation is the last gate before the handler). Registered → `VALIDATION_ERROR`; unwired → `METHOD_NOT_ALLOWED`.
- Default `pytest tests/` must **not** collect/run paper e2e: `addopts = ["-m", "not paper_e2e"]` in `pyproject.toml`. Wrapper / explicit `-m paper_e2e` overrides. Optionally also require `MMR_PAPER_E2E=1` in the skip gate (wrapper exports it).
- Typed access only through the public wrapper (`.query.call` / `.command.call`) — never `MMR._typed_query()` / `._typed_command()`.
- Do not require CI to run these tests.

## File map

| File | Responsibility |
|------|----------------|
| `tests/paper_e2e/__init__.py` | Package marker (empty or docstring) |
| `tests/paper_e2e/conftest.py` | Autouse `paper_stack`, clients, `e2e_id`, teardown, `pytest_collection_modifyitems` |
| `tests/paper_e2e/_probe.py` | Pure helpers: capability probe, form CSRF derive, error-code classification |
| `tests/paper_e2e/_clients.py` | Dashboard httpx session + CSRF headers; typed RPC wrapper (`.query` / `.command`) |
| `tests/test_paper_e2e_probe.py` | Unit tests for probe/CSRF (**outside** `tests/paper_e2e/` so autouse never gates them) |
| `tests/paper_e2e/test_smoke.py` | Layer A |
| `tests/paper_e2e/test_dashboard_portfolios.py` | Portfolios |
| `tests/paper_e2e/test_dashboard_strategies.py` | Strategies |
| `tests/paper_e2e/test_dashboard_research.py` | Research |
| `tests/paper_e2e/test_dashboard_scaling.py` | Scaling |
| `tests/paper_e2e/test_dashboard_commands.py` | pause/resume/cancel/close |
| `tests/paper_e2e/test_typed_rpc_queries.py` | Layer C queries + BYPASS-absent |
| `tests/paper_e2e/test_typed_rpc_commands.py` | Layer C commands |
| `scripts/paper_e2e.sh` | Wrapper → `pytest -m paper_e2e` |
| `pyproject.toml` | Register markers |
| `docs/PAPER_AUTOMATION_SETUP.md` | Runbook + HMAC key-file (fix stale secret) |
| `CLAUDE.md` | Fix stale `MMR_HMAC_SECRET` mention in typed-RPC section |

---

### Task 1: Probe helpers + markers + unit tests (no Docker)

**Files:**
- Create: `tests/paper_e2e/__init__.py`
- Create: `tests/paper_e2e/_probe.py`
- Create: `tests/test_paper_e2e_probe.py` (outside the e2e package)
- Modify: `pyproject.toml` (`[tool.pytest.ini_options]` markers + `addopts`)

**Interfaces:**
- Produces:
  - `capability_from_remote_error(code: str) -> Literal["absent", "present"]` — `"absent"` iff `code == "METHOD_NOT_ALLOWED"`; else `"present"`
  - `derive_html_form_csrf(session_secret: str) -> str` — `HMAC-SHA256(secret, b"mmr-dashboard-html-form-csrf-v1").hexdigest()` when `len(secret) >= 32`; raises `ValueError` if shorter
  - `probe_command_registered(client, method: str) -> bool` — **always** calls `client.call(method, {}, dict)`. Returns `False` on `TypedRpcRemoteError` with `METHOD_NOT_ALLOWED`; returns `True` on success or any other remote error code (including `VALIDATION_ERROR`); re-raises connection/auth errors. **Invariant:** never pass a non-empty body — a validation-passing body must not be constructible by the probe for risk-arming commands.

- [ ] **Step 1: Write failing unit tests** in `tests/test_paper_e2e_probe.py`:

```python
import pytest
from tests.paper_e2e._probe import (
    capability_from_remote_error,
    derive_html_form_csrf,
)

def test_method_not_allowed_means_absent():
    assert capability_from_remote_error("METHOD_NOT_ALLOWED") == "absent"

@pytest.mark.parametrize("code", [
    "VALIDATION_ERROR", "PROPOSAL_NOT_FOUND", "COMMAND_NOT_FOUND", "INTERNAL_ERROR",
])
def test_handler_errors_mean_present(code):
    assert capability_from_remote_error(code) == "present"

def test_form_csrf_matches_dashboard_derivation():
    secret = "x" * 32
    import hashlib, hmac
    expected = hmac.new(secret.encode(), b"mmr-dashboard-html-form-csrf-v1", hashlib.sha256).hexdigest()
    assert derive_html_form_csrf(secret) == expected

def test_form_csrf_rejects_short_secret():
    with pytest.raises(ValueError):
        derive_html_form_csrf("short")
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_paper_e2e_probe.py -q --timeout=30`  
  Expected: FAIL (module missing).

- [ ] **Step 3: Implement `tests/paper_e2e/_probe.py`** with the three helpers above. `probe_command_registered` imports `TypedRpcRemoteError` from `trader.messaging.typed_rpc` and hardcodes body `{}`.

- [ ] **Step 4: Update `pyproject.toml`** under `[tool.pytest.ini_options]`:

```toml
# Keep ordinary `pytest tests/` runs off the live paper suite. Explicit
# `-m paper_e2e` (scripts/paper_e2e.sh) overrides this deselect.
addopts = ["-m", "not paper_e2e"]
markers = [
    "paper_e2e: live paper Docker stack (skip if down)",
    "paper_e2e_live_orders: places/arms real paper risk; needs MMR_PAPER_E2E_LIVE_ORDERS=1",
    "paper_e2e_restart: restarts trader container; needs MMR_PAPER_E2E_RESTART=1",
]
```

- [ ] **Step 5: Re-run unit tests — expect PASS** (and confirm they are not under `tests/paper_e2e/`).

- [ ] **Step 6: Commit** `test(paper_e2e): capability probe helpers and pytest markers`

---

### Task 2: Autouse `paper_stack`, clients, wrapper, skip-when-down

**Files:**
- Create: `tests/paper_e2e/_clients.py`
- Create: `tests/paper_e2e/conftest.py`
- Create: `scripts/paper_e2e.sh` (executable)
- Create: `tests/paper_e2e/test_stack_gate.py` (asserts skip/pass contract with mocks where possible)

**Interfaces:**
- Consumes: Task 1 helpers
- Produces fixtures:
  - `e2e_id: str` — `f"e2e_{os.getpid()}_{int(time.time())}"`
  - `paper_stack` (autouse) — dataclass/namespace with `.dashboard_url`, `.capabilities: frozenset[str]`, `.typed` (wrapper), `.live_orders: bool`, `.restart: bool`
  - `dashboard_client` — httpx.Client with cookies; methods `login()`, `csrf_headers() -> dict`, `get/post`
  - `typed_rpc` — thin wrapper around `MMR` typed clients with **public** `.query.call(method, body, model)` and `.command.call(method, body, model)` only (no private SDK leakage)
  - `require_capability(name: str)` helper used by tests → `pytest.skip` if missing
  - Finalizer teardown scoped to `e2e_id` (reject pending proposals / delete universes matching prefix; live-orders close positions + disarm)

Capability set keys: `trading_control`, `proposals`, `approval`, `strategy_control`, `allocation`, `paper_automation`.

**Command probe invariant (safety):** every command capability probe uses body `{}` exclusively via `probe_command_registered(client, method)`.

| Method | Body |
|--------|------|
| `approve_proposal` | `{}` |
| `reject_proposal` | `{}` |
| `create_proposal` | `{}` |
| `enable_strategy` | `{}` |
| `activate_allocation` | `{}` |
| `suspend_allocation` | `{}` |
| `activate_paper_automation` | `{}` |
| `deactivate_paper_automation` | `{}` |
| `pause_trading` | `{}` |

Queries (`list_proposals`, `get_trading_control`, `get_paper_automation_status`, …) may use `{}` successfully when registered.

**Marking / timeouts:** do **not** set `pytestmark` in `conftest.py` (ignored by pytest). Implement:

```python
# tests/paper_e2e/conftest.py
def pytest_collection_modifyitems(config, items):
    for item in items:
        path = str(getattr(item, "fspath", "") or item.path)
        if "/tests/paper_e2e/" not in path.replace("\\", "/"):
            continue
        item.add_marker(pytest.mark.paper_e2e)
        if not any(m.name == "timeout" for m in item.iter_markers()):
            item.add_marker(pytest.mark.timeout(120))
```

Individual tests that need 180/300 still add `@pytest.mark.timeout(...)` explicitly (overrides the default stamp).

**Opt-in:** at the top of `paper_stack`, if `os.environ.get("MMR_PAPER_E2E") != "1"`, `pytest.skip("set MMR_PAPER_E2E=1 or use scripts/paper_e2e.sh")` — defense in depth if someone overrides `addopts`. Wrapper always exports `MMR_PAPER_E2E=1`.

Skip gate order (exact):

1. Require `MMR_PAPER_E2E=1` or skip
2. `GET {url}/healthz` — assert `r.json()["ok"] is True` or skip
3. Build typed wrapper; `get_status` or skip on connection/HMAC errors
4. Paper mode + `ib_upstream_connected` or skip
5. Fill `capabilities` via `{}` probes

`dashboard_client.login()`: `POST /session` with form `token=<DASHBOARD_TOKEN or file contents>`.

`csrf_headers()`: `GET /api/commands/csrf-token` → `{"X-CSRF-Token": ...}`.

- [ ] **Step 1: Implement `_clients.py` + `conftest.py`** per interfaces (no placeholder stubs — full skip paths + `pytest_collection_modifyitems`).

- [ ] **Step 2: Write `test_stack_gate.py`:**

```python
def test_autouse_stack_fixture_provides_capabilities(paper_stack):
    assert isinstance(paper_stack.capabilities, frozenset)

def test_healthz_json_ok_field(dashboard_client, paper_stack):
    r = dashboard_client.get("/healthz")
    assert r.status_code == 200, f"healthz failed: {r.status_code} {r.text}"
    assert r.json()["ok"] is True
```

(Markers/timeouts come from `pytest_collection_modifyitems` — no module `pytestmark` required, but adding an explicit module `pytestmark` is fine as belt-and-suspenders.)

- [ ] **Step 3: Write `scripts/paper_e2e.sh`:**

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export MMR_PAPER_E2E=1
exec "${PYTHON:-.venv/bin/python}" -m pytest -m paper_e2e -v --timeout=120 "$@"
```

- [ ] **Step 4: Run with stack down:**  
  `MMR_PAPER_E2E=1 MMR_PAPER_E2E_DASHBOARD_URL=http://127.0.0.1:9 .venv/bin/python -m pytest -m paper_e2e tests/paper_e2e -q`  
  Expected: all skipped, exit 0.

  Also: plain `.venv/bin/python -m pytest tests/test_paper_e2e_probe.py -q` still runs (not deselected by `not paper_e2e`).

- [ ] **Step 5: Commit** `test(paper_e2e): autouse stack gate, clients, and runner script`

---

### Task 3: Layer A smoke

**Files:**
- Create: `tests/paper_e2e/test_smoke.py`

**Interfaces:**
- Consumes: `paper_stack`, `dashboard_client`, `typed_rpc`, `e2e_id`

- [ ] **Step 1: Write tests** (ordered via naming `test_01_...` or a single `test_smoke_ladder` with steps):

```python
import pytest

@pytest.mark.timeout(180)
def test_smoke_ladder(dashboard_client, typed_rpc, paper_stack, e2e_id, require_capability):
    require_capability("proposals")
    assert dashboard_client.get("/readyz").status_code == 200
    dashboard_client.login()
    h = dashboard_client.get("/api/cc-health")
    assert h.status_code == 200, f"login/cc-health failed: {h.status_code} {h.text}"
    status = typed_rpc.query.call("get_status", {}, dict)
    assert status  # non-empty
    # resolve AAPL via discover_instrument or resolve_instrument
    # create_proposal via POST /api/commands/proposals + csrf_headers
    # reject_proposal (typed or HTTP)
    # if paper_stack.live_orders: approve instead and schedule position close in teardown
```

Fill concrete bodies from `CreateProposal` request model in `production_api.py` / dashboard route (read file at implement time — use symbol AAPL, tiny qty, market, reasoning `e2e_id`).

- [ ] **Step 2: Run against live paper stack** `./scripts/paper_e2e.sh tests/paper_e2e/test_smoke.py -v` — expect PASS (or skip if IB down).

- [ ] **Step 3: Commit** `test(paper_e2e): layer A smoke ladder`

---

### Task 4: Portfolios (typed CRUD + HTTP members)

**Files:**
- Create: `tests/paper_e2e/test_dashboard_portfolios.py`

- [ ] **Step 1: Write tests:**

1. `test_universe_crud_via_typed_rpc` — `create_universe` `{e2e_id}` → `add_universe_symbols` AAPL → `get_universe` / `list_universes` → `remove_universe_symbol` → `delete_universe`. Teardown also deletes if mid-fail.
2. `test_watchlist_members_http_read` — after typed add, `GET /watchlists/{e2e_id}/members` with session; assert AAPL present.
3. Optional `test_legacy_watchlist_create_form` — `POST /watchlists/create` with form fields `name` + `csrf_token=derive_html_form_csrf(...)`; then typed delete. Skip if secret short.
4. `test_portfolio_survives_trader_restart` marked `paper_e2e_restart` + `timeout(300)` — skip unless `MMR_PAPER_E2E_RESTART=1`; restart via `docker compose -f docker-compose.yml restart trader`; poll healthz + `get_status` up to ~180s; assert universe members unchanged.

- [ ] **Step 2: Run** `./scripts/paper_e2e.sh tests/paper_e2e/test_dashboard_portfolios.py -v`

- [ ] **Step 3: Commit** `test(paper_e2e): Portfolios typed CRUD and members HTTP`

---

### Task 5: Strategies (enable/disable + params)

**Files:**
- Create: `tests/paper_e2e/test_dashboard_strategies.py`

- [ ] **Step 1: Write tests** (skip without `strategy_control`):

1. Snapshot contains `strategies` list; if empty, skip with “no deployed strategies”.
2. Pick first deployed `strategy_name`.
3. `GET /api/strategies/{strategy_name}/params` → 200 JSON with fields.
4. `POST .../disable` then `POST .../enable` with `csrf_headers` (restore enable in finally).
5. Params round-trip: POST same values from GET to `/api/commands/strategies/{strategy_name}/params`; GET equals.
6. Assert runtime row fields: class / bar_size / conids-or-universe / state present when enabled.

Do **not** deploy/undeploy operator YAML in v1 unless an `e2e_*` strategy name is already present.

- [ ] **Step 2: Run** suite file against paper stack.

- [ ] **Step 3: Commit** `test(paper_e2e): Strategies enable/disable and params editor`

---

### Task 6: Research GETs + propose→reject

**Files:**
- Create: `tests/paper_e2e/test_dashboard_research.py`

- [ ] **Step 1: Write tests** with `timeout(180)`:

1. `GET /api/research/presets` — 200, JSON list/dict.
2. `ideas` / `movers` / `snapshot` / `news` — 200 or structured soft entitlement error (not 5xx; not HTML).
3. Unauthenticated client (no cookie) → 401/403 on a research GET.
4. Flow: resolve AAPL → `POST /api/commands/proposals` with CSRF (group/reasoning include `e2e_id`) → reject. **No** `/api/research/*/propose`.

- [ ] **Step 2: Run** against paper stack.

- [ ] **Step 3: Commit** `test(paper_e2e): Research reads and propose-reject flow`

---

### Task 7: Scaling + live-orders teardown

**Files:**
- Create: `tests/paper_e2e/test_dashboard_scaling.py`
- Modify: `tests/paper_e2e/conftest.py` teardown for position close / disarm

- [ ] **Step 1: Default (safe) tests:**

1. Snapshot `scaling` view has keys `status`, `message` (and documents null gross when no events — not inferred green).
2. `get_paper_automation_status` query succeeds if capability present; assert unarmed/refuse shape without activating.

- [ ] **Step 2: Live-orders tests** (`paper_e2e_live_orders`, `timeout(300)`):

1. Skip unless env set and capabilities `allocation` / `paper_automation` / `approval` as needed.
2. Preflight nonce when HTTP activate_allocation requires it (`POST /api/preflight` then activate).
3. Typed or HTTP: `activate_allocation` / `suspend_allocation`; `activate_paper_automation` / `deactivate_paper_automation`.
4. Teardown **must** deactivate/suspend and **close any position** opened by approve in this run (track conIds on `paper_stack.opened_conids`).

- [ ] **Step 3: Run** default path; optionally `MMR_PAPER_E2E_LIVE_ORDERS=1 ./scripts/paper_e2e.sh tests/paper_e2e/test_dashboard_scaling.py -v`

- [ ] **Step 4: Commit** `test(paper_e2e): Scaling reads and gated live-order arm paths`

---

### Task 8: Remaining CC commands + typed RPC matrix + BYPASS

**Files:**
- Create: `tests/paper_e2e/test_dashboard_commands.py`
- Create: `tests/paper_e2e/test_typed_rpc_queries.py`
- Create: `tests/paper_e2e/test_typed_rpc_commands.py`

- [ ] **Step 1: Queries file** — call each: `get_status`, `get_positions`, `get_open_orders`, `list_universes`, `discover_instrument` (AAPL), `list_proposals`, `get_trading_control` (skip if no capability), `get_paper_automation_status` (skip if absent).

- [ ] **Step 2: Commands file** — universe CRUD already in Task 4; here: `create_proposal`→`reject_proposal`; `pause_trading`→`resume_trading` in finally (capability `trading_control`); strategy enable/disable if capability (restore).

- [ ] **Step 3: BYPASS-absent** — `typed_rpc.command.call("place_order_simple", {}, dict)` expects `TypedRpcRemoteError.code == "METHOD_NOT_ALLOWED"`.

- [ ] **Step 4: Dashboard commands** — pause/resume HTTP with CSRF; cancel paths only against e2e-created orders if any.

- [ ] **Step 5: Run full** `./scripts/paper_e2e.sh` against paper stack (default env).

- [ ] **Step 6: Commit** `test(paper_e2e): typed RPC matrix, BYPASS guard, and CC commands`

---

### Task 9: Docs runbook + HMAC key-file fix

**Files:**
- Modify: `docs/PAPER_AUTOMATION_SETUP.md` — add “Paper e2e” section; replace `MMR_HMAC_SECRET` with `service_hmac.key` / `MMR_SERVICE_HMAC_KEY_FILE`
- Modify: `CLAUDE.md` — same HMAC correction where `.env` documents `MMR_HMAC_SECRET` for typed RPC

Runbook snippet to add:

```markdown
## Paper Docker e2e

With the paper stack up (`./docker.sh -b -u`, IB upstream connected):

```bash
./scripts/paper_e2e.sh
# optional:
MMR_PAPER_E2E_LIVE_ORDERS=1 ./scripts/paper_e2e.sh
MMR_PAPER_E2E_RESTART=1 ./scripts/paper_e2e.sh -k restart
```

Uses `~/.config/mmr/service_hmac.key` (mode 0600) and dashboard `DASHBOARD_TOKEN`.
Stack down → all tests skip (exit 0).
```

- [ ] **Step 1: Edit docs** as above (grep for `MMR_HMAC_SECRET` and fix).

- [ ] **Step 2: Commit** `docs: paper e2e runbook and HMAC key-file correction`

---

## Spec coverage checklist

| Spec requirement | Task |
|------------------|------|
| Autouse skip gate + healthz JSON `ok` | 2 |
| Capability probe `METHOD_NOT_ALLOWED` | 1–2 |
| Timeouts 120/180/300 | 2–7 |
| Layer A smoke | 3 |
| Portfolios typed + members HTTP + optional form | 4 |
| Restart mark | 4 |
| Strategies enable/disable/params `{strategy_name}` | 5 |
| Research GETs + commands proposals only | 6 |
| Scaling read + live-orders activate_* ids + position teardown | 7 |
| Typed query/command matrix + BYPASS | 8 |
| `paper_e2e.sh` + markers | 1–2 |
| Runbook + HMAC docs | 9 |
| Teardown scoped to `e2e_id` | 2, 7 |

## Self-review notes

- No phantom research propose path.
- No `COMMAND_NOT_FOUND` as unwired signal; probes use body `{}` only (never validation-passing risk-arm bodies).
- Form CSRF isolated from JSON CSRF.
- Markers/timeouts via `pytest_collection_modifyitems` (not conftest `pytestmark`).
- Probe unit tests live in `tests/test_paper_e2e_probe.py` (outside autouse package).
- Default `addopts = -m "not paper_e2e"`; wrapper sets `MMR_PAPER_E2E=1` + `-m paper_e2e`.
- Typed access only through `.query` / `.command` wrapper.
- Task 3–8 need a live paper stack for green proof; Task 1 + stack-down skip + probe units are verifiable without IB.
