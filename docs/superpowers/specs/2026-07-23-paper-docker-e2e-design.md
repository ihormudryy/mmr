# Paper Docker E2E Design

**Date:** 2026-07-23  
**Status:** approved (design); revised after review — awaiting implementation plan  
**Depends on:** split Compose (`./docker.sh -b -u`), command-center HTTP + CSRF, typed HMAC RPC (`42101`/`42102`), paper IB Gateway  
**Related:** `docs/PAPER_AUTOMATION_SETUP.md`, `docs/superpowers/specs/2026-07-22-dashboard-research-tab-design.md`, `docs/superpowers/specs/2026-07-20-dashboard-paper-automation-activation-design.md`, `docs/superpowers/specs/2026-07-15-watchlists-and-ui-deploy-design.md`

## Problem

There is no automated end-to-end suite that exercises a **real paper** stack after `./docker.sh -b -u`. Coverage today is in-process / FastAPI `TestClient` / fake-broker fullstack supervision. Regressions in dashboard auth, Portfolios persistence, Strategies enable/disable, Research propose flow, Scaling, and typed RPC only show up manually.

## Decision

**Harness:** pytest + httpx (dashboard) + typed SDK/HMAC (trader RPC).  
**Marker:** `@pytest.mark.paper_e2e` — **skip** (not fail) when the live stack is unreachable.  
**CI:** not required by default (no flaky IB dependency).  
**Wrapper:** `scripts/paper_e2e.sh` → `pytest -m paper_e2e -v`.

Milestones **A + B + C** ship in one suite with layered files; tab domains **Scaling, Strategies, Portfolios, Research** are first-class.

### Non-goals (v1)

- Browser / Playwright UI driving (separate from API e2e).
- Publishing strategy typed ports `42104`/`42105` to the host.
- Fake-broker / `docker-compose.test.override.yml` as the paper target.
- Legacy dill RPC `42001` / direct `buy`/`sell`.
- Making paper IB a required GitHub Actions gate.
- Live trading account mode.
- Exercising legacy HTML-form watchlist/strategy POSTs as the primary mutation path (see CSRF below).

## Run prerequisites

Operator machine:

1. `./docker.sh -b -u` with `TRADING_MODE=paper` and IB Gateway upstream connected.
2. `command_authority.enabled: true` in `~/.config/mmr/trader.yaml`.
3. Dashboard: `DASHBOARD_TOKEN` (+ `DASHBOARD_SESSION_SECRET` ≥32 chars) available to the test process (compose env or host env).
4. `DASHBOARD_COMMANDS_ENABLED=true` for mutation tests.
5. HMAC: `~/.config/mmr/service_hmac.key` (mode `0600`) — same file containers mount. **Not** `MMR_HMAC_SECRET` (stale docs in `PAPER_AUTOMATION_SETUP.md` / `CLAUDE.md`; runbook + those docs must use the key-file mechanism).
6. Optional: `MMR_PAPER_E2E_LIVE_ORDERS=1` for approve / `activate_allocation` / `activate_paper_automation` that can place or arm real paper risk.
7. Optional: `MMR_PAPER_E2E_RESTART=1` for Portfolios-survive-trader-restart.

Defaults (overridable by env):

| Env | Default |
|-----|---------|
| `MMR_PAPER_E2E_DASHBOARD_URL` | `http://127.0.0.1:7424` |
| `MMR_TYPED_QUERY_ENDPOINT` | `tcp://127.0.0.1:42101` |
| `MMR_TYPED_COMMAND_ENDPOINT` | `tcp://127.0.0.1:42102` |
| `MMR_SERVICE_HMAC_KEY_FILE` | `~/.config/mmr/service_hmac.key` |
| `MMR_PAPER_E2E_LIVE_ORDERS` | unset / `0` |
| `MMR_PAPER_E2E_RESTART` | unset / `0` |

## Skip gate (autouse)

`paper_stack` is an **`autouse=True`** fixture in `tests/paper_e2e/conftest.py` so every test in the package is gated without depending on authors remembering the fixture name. Marker alone is not enough.

Probes in order; any hard failure → `pytest.skip(reason)`:

1. `GET {dashboard}/healthz` → JSON body `{"ok": true}` (assert `response.json()["ok"] is True`, not string equality with `"ok"`).
2. Typed `get_status` via SDK/HMAC.
3. Assert paper / non-live trading mode (from status or config).
4. Assert `ib_upstream_connected` is true (else skip with IB/VNC hint).
5. **Capability probe** — attempt cheap typed queries/commands that Layer B/C need; record a `capabilities` frozenset on the fixture. If a service is unwired, the method string is simply never registered: `registry.resolve()` returns `None` and the typed RPC layer raises **`METHOD_NOT_ALLOWED`** (`typed_rpc.py` authenticate → resolve → validate → run). Tests that need a missing capability **skip** on that code — they must not fail red.

   Do **not** key the probe off `COMMAND_NOT_FOUND`: that is a handler-level code from the already-registered `get_command` path when a receipt id is missing (same family as `PROPOSAL_NOT_FOUND`). Catching it would miss the unwired case and leave tests red — the opposite of this gate.

   Side-effect-free command probe: **always call with body `{}`**. Never send fields that could pass server-side validation for a risk-increasing command (validation is the last gate before the handler runs). Branch on the error code:

   - `METHOD_NOT_ALLOWED` → method unregistered → capability **absent** → skip
   - `VALIDATION_ERROR` / `PROPOSAL_NOT_FOUND` / `COMMAND_NOT_FOUND` (or a successful **query**) → method **is** registered (capability present); for commands, `{}` must fail validation before the handler — never execute the real action

Capability keys (exact typed method ids):

| Capability | Probe / evidence |
|------------|------------------|
| `trading_control` | `get_trading_control` succeeds |
| `proposals` | `list_proposals` succeeds; mutations: `probe_command_registered(..., "create_proposal")` etc. with body `{}` |
| `approval` | `approve_proposal` with `{}` — `VALIDATION_ERROR` = present; `METHOD_NOT_ALLOWED` = absent (live-orders only) |
| `strategy_control` | `enable_strategy` with `{}` — not `METHOD_NOT_ALLOWED` = registered |
| `allocation` | `activate_allocation` / `suspend_allocation` with `{}` |
| `paper_automation` | `get_paper_automation_status` succeeds; activate/deactivate probed with `{}` under live-orders only |

Missing HMAC key, connection refused, or upstream down → **skip**, never red on a laptop without Docker.

## Timeouts

Global suite default is `timeout = 30` / `timeout_method = thread` (`pyproject.toml`). That will kill real Docker + IB e2e mid-flight.

Design rules:

| Scope | Timeout |
|-------|---------|
| Default paper_e2e tests | `@pytest.mark.timeout(120)` (module-level pytestmark in `conftest` or each file) |
| IB resolve / snapshot / research with one retry | `@pytest.mark.timeout(180)` on those tests |
| `paper_e2e_live_orders` (approve + terminal wait) | `@pytest.mark.timeout(300)` |
| `paper_e2e_restart` (compose restart + health wait) | `@pytest.mark.timeout(300)` |

`scripts/paper_e2e.sh` may also pass `--timeout=120` as a floor; per-mark overrides still win for slower cases.

## Fixtures

`tests/paper_e2e/conftest.py`:

| Fixture | Role |
|---------|------|
| `paper_stack` (**autouse**) | Skip gate + URLs + `capabilities` + mode flags |
| `dashboard_client` | httpx client; `POST /session` with token; cookie jar; **`csrf_headers()`** from `GET /api/commands/csrf-token` → `X-CSRF-Token` for `/api/commands/*` JSON routes |
| `typed_sdk` | SDK (or thin typed client) on host `42101`/`42102` + key file |
| `e2e_id` | Unique run id `e2e_{pid}_{ts}` — **all** temp names and teardown are scoped to this exact prefix |

### CSRF (two mechanisms — do not conflate)

| Surface | CSRF | Fixture / approach |
|---------|------|--------------------|
| `POST /api/commands/*` (JSON) | Session-bound header from `GET /api/commands/csrf-token` → `X-CSRF-Token` | `csrf_headers()` |
| Legacy HTML forms (`POST /watchlists/*`, `/strategies/deploy`, `/strategies/{name}/enable-live`, …) | Form field `csrf_token` checked against process-global `_CSRF_TOKEN` = `HMAC-SHA256(DASHBOARD_SESSION_SECRET, "mmr-dashboard-html-form-csrf-v1")` when secret ≥32 chars (`web/app.py`) | **Not the primary portfolio path** (see Portfolios). If a test must hit a legacy form, use optional `form_csrf` fixture that re-derives that HMAC (or scrapes the rendered page). |

**Preferred Portfolios mutations:** typed RPC on `42102` (`create_universe`, `add_universe_symbols`, `remove_universe_symbol`, `delete_universe`, `import_universe_csv`). HTTP proves the read path `GET /watchlists/{name}/members` (session) and optionally **one** form create with `form_csrf` to prove the legacy route still works — not the full CRUD matrix via forms.

Markers:

- `paper_e2e` — all tests in this package (register in `pyproject.toml`)
- `paper_e2e_live_orders` — mutations that can place paper orders or arm automation / allocation; skipped unless `MMR_PAPER_E2E_LIVE_ORDERS=1`
- `paper_e2e_restart` — Portfolios-survive-trader-restart; skipped unless `MMR_PAPER_E2E_RESTART=1`

### Teardown (scoped to this run)

Using **this run’s** `e2e_id` only (never glob-delete all `e2e_*`):

1. Reject leftover **PENDING** proposals tagged/created under this `e2e_id`.
2. Delete universes named exactly `{e2e_id}` or `{e2e_id}_*` created by this run.
3. Best-effort: `deactivate_paper_automation` / `suspend_allocation` if this run armed them.
4. **`paper_e2e_live_orders`:** flatten any position this run opened (close via dashboard/typed liquidate-close for those conIds). Executed proposals are terminal — reject is impossible; position close is mandatory.
5. Never delete operator portfolios (`keep`, `paper_strats`, `asx`, …).

## Suite layout

```text
tests/paper_e2e/
  conftest.py
  test_smoke.py              # A
  test_dashboard_portfolios.py
  test_dashboard_strategies.py
  test_dashboard_research.py
  test_dashboard_scaling.py
  test_dashboard_commands.py # remaining CC commands (pause/resume, cancel, …)
  test_typed_rpc_queries.py  # C reads
  test_typed_rpc_commands.py # C mutations (safe defaults)
scripts/paper_e2e.sh
```

## Layer A — smoke (`test_smoke.py`)

Ordered ladder (fail fast); module timeout ≥120s:

1. `GET /healthz` → `{"ok": true}`; `GET /readyz` per current contract
2. Session login → `GET /api/cc-health` (or `/api/health` as allowed)
3. Typed `get_status` + portfolio/positions read
4. Resolve one liquid US symbol (e.g. `AAPL` / known conId) — one retry with backoff; then skip if IB pacing
5. `POST /api/commands/proposals` (JSON + `X-CSRF-Token`) → **reject** by default  
   - If live-orders: `approve_proposal` / approve HTTP once; wait for terminal state; teardown closes any opened position

## Layer B — dashboard HTTP (tabs)

JSON command mutations use session + `X-CSRF-Token` only.

### Portfolios (watchlists)

**Mutations (primary):** typed `42102` universe CRUD for name `{e2e_id}`.

**HTTP:**

- `GET /watchlists/{name}/members` after typed add (session) — assert symbols/conIds
- Optional single `POST /watchlists/create` with `form_csrf` to prove legacy form route (not full matrix)
- Do **not** require form-based add/remove/upload/delete for v1 green

Optional restart mark (`timeout=300`): after typed add, `docker compose restart trader`, wait for healthz + typed `get_status`, assert `get_universe` / members unchanged.

### Strategies

- Read: `/api/snapshot` strategies rows; `GET /api/strategies/{strategy_name}/params` (path param is **`strategy_name`**, not `name`)
- Enable / disable via `POST /api/commands/strategies/{strategy_name}/enable|disable` (+ CSRF header)
- Params round-trip: GET → POST `/api/commands/strategies/{strategy_name}/params` → GET equals
- Skip if capability `strategy_control` missing
- Deploy/undeploy **legacy forms**: only if explicitly marked and `form_csrf` present; prefer skip rather than mutate operator YAML. Isolated `e2e_*` deploy names only.

Runtime panel contract: enabled strategy row exposes class / bar_size / universe-or-conids / state (non-empty where deployed).

### Research

- `GET /api/research/presets`
- `GET /api/research/ideas`, `movers`, `snapshot`, `news` (query params per research-tab design)
- Soft-pass: known entitlement / empty-universe responses (assert structured error, not 5xx)
- Hard-fail: 401/403 without session, 5xx, malformed HTML on JSON routes
- Flow: research row / resolve → **`POST /api/commands/proposals` only** (there is no `/api/research/*/propose` route) → reject (default) / approve (live-orders)

### Scaling

- Snapshot `scaling` view: never treat “no events” as healthy green; assert shape (`status`, `message`, `max_gross_allocation`, …)
- Typed / HTTP: `get_paper_automation_status`
- Default: assert refuse/unarmed behaviour without activating; skip activate tests if capability missing
- Live-orders only (exact typed / command ids):
  - `activate_allocation` / `suspend_allocation` (HTTP: `POST /api/commands/allocation/activate|suspend` with preflight nonce when required)
  - `activate_paper_automation` / `deactivate_paper_automation` (HTTP: `POST /api/commands/paper-automation/activate|deactivate`)
  - Teardown always deactivates / suspends and closes positions opened by the run

### Other command-center commands

Cover remaining production command routes used by the CC (pause/resume, cancel order / cancel-all, close position) under live-orders or read-only preflight as appropriate — paper-safe defaults prefer reject/cancel of **this run’s** e2e-created state only. Skip per missing capability.

## Layer C — typed RPC

Exact method ids (implementation must use these strings):

**Queries:** `get_status`, `get_positions`, `get_open_orders`, `list_universes`, `get_universe`, `discover_instrument`, `resolve_instrument`, `list_proposals`, `get_proposal`, `get_trading_control`, `get_paper_automation_status`.

**Commands:**

- Universe: `create_universe`, `add_universe_symbols`, `remove_universe_symbol`, `delete_universe`, `import_universe_csv`
- Proposals: `create_proposal`, `reject_proposal`, `approve_proposal` (live-orders)
- Strategies: `enable_strategy`, `disable_strategy`, `update_strategy_params`
- Controls: `pause_trading`, `resume_trading` (always resume in teardown)
- Allocation: `activate_allocation`, `suspend_allocation` (live-orders)
- Paper automation: `activate_paper_automation`, `deactivate_paper_automation` (live-orders)

Skip individual tests when the corresponding capability was not probed.

Negative: `place_order_simple` (and siblings documented as BYPASS) must not be registered on the typed command surface.

Strategy control ports stay **internal**; host tests use trader `42102` / dashboard.

## Auth matrix (must hold)

| Surface | Auth |
|---------|------|
| `/healthz`, `/readyz` | none |
| Dashboard JSON commands | session cookie + `X-CSRF-Token` |
| Legacy HTML forms (optional) | session + form `csrf_token` (`form_csrf`) |
| Typed RPC | HMAC service key file (`MMR_SERVICE_HMAC_KEY_FILE`) |

Tests that omit auth must expect 401/403.

## Failure / flake policy

- IB pacing / market-data gaps: retry once with backoff on resolve/snapshot; then **skip** with explicit reason — do not silent-pass or hang past the per-test timeout.
- Entitlement-limited research: soft assert.
- Unwired command services: **skip** via capability probe on `METHOD_NOT_ALLOWED`, not a red fail.
- Never leave armed paper automation, active allocation, or **open e2e paper positions** without teardown best-effort.

## Success criteria

1. With stack down: `pytest -m paper_e2e` exits 0 with **skips only** (autouse `paper_stack`).
2. With paper stack up: Layer A green; Portfolios/Strategies/Research/Scaling suites green under default (no live-orders).
3. With `MMR_PAPER_E2E_LIVE_ORDERS=1`: approve + scaling arm paths green; teardown closes positions and disarms.
4. Docs: short runbook in `docs/PAPER_AUTOMATION_SETUP.md` pointing at `scripts/paper_e2e.sh`, using **key-file** HMAC (and fix stale `MMR_HMAC_SECRET` mentions there + `CLAUDE.md` in the same change).

## Implementation order

1. Marker + autouse skip gate + capability probe + timeouts + `paper_e2e.sh`
2. Layer A smoke
3. Portfolios typed CRUD + HTTP members read (+ optional form create)
4. Strategies enable/disable + params (`{strategy_name}`)
5. Research GETs + `POST /api/commands/proposals` → reject
6. Scaling read + live-orders `activate_allocation` / `activate_paper_automation` + position teardown
7. Remaining CC commands + typed query matrix + BYPASS-absent
8. Optional restart mark for Portfolios

## Open points (resolved in this design)

| Topic | Choice |
|-------|--------|
| Curl vs pytest | pytest + httpx + SDK |
| CI | opt-in local marker only |
| Live orders default | off; env gate |
| Research empty/entitlement | soft |
| Strategy host ports | not published; use trader RPC / dashboard |
| Temp data naming | this-run `e2e_{pid}_{ts}` prefix only |
| Portfolios CSRF | typed RPC mutations; form CSRF only for optional legacy probe |
| Skip delivery | autouse `paper_stack` |
| Unwired commands | capability probe on `METHOD_NOT_ALLOWED` via body `{}` only |
| Autouse + markers | `paper_stack` autouse; stamps via `pytest_collection_modifyitems`; default `addopts -m "not paper_e2e"` |
| Global 30s timeout | per-mark overrides 120 / 180 / 300 |
| Live-orders cleanup | close positions; cannot reject EXECUTED |
| HMAC docs drift | runbook + fix `PAPER_AUTOMATION_SETUP` / `CLAUDE.md` key-file |
