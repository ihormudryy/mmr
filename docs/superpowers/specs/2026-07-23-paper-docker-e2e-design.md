# Paper Docker E2E Design

**Date:** 2026-07-23  
**Status:** approved (design); awaiting implementation plan  
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

## Run prerequisites

Operator machine:

1. `./docker.sh -b -u` with `TRADING_MODE=paper` and IB Gateway upstream connected.
2. `command_authority.enabled: true` in `~/.config/mmr/trader.yaml`.
3. Dashboard: `DASHBOARD_TOKEN` (+ session secret) available to the test process (compose env or host env).
4. `DASHBOARD_COMMANDS_ENABLED=true` for mutation tests.
5. HMAC: `~/.config/mmr/service_hmac.key` (mode `0600`) — same file containers mount.
6. Optional: `MMR_PAPER_E2E_LIVE_ORDERS=1` for approve / allocation activate / paper-automation activate that can place or arm real paper risk.

Defaults (overridable by env):

| Env | Default |
|-----|---------|
| `MMR_PAPER_E2E_DASHBOARD_URL` | `http://127.0.0.1:7424` |
| `MMR_TYPED_QUERY_ENDPOINT` | `tcp://127.0.0.1:42101` |
| `MMR_TYPED_COMMAND_ENDPOINT` | `tcp://127.0.0.1:42102` |
| `MMR_SERVICE_HMAC_KEY_FILE` | `~/.config/mmr/service_hmac.key` |
| `MMR_PAPER_E2E_LIVE_ORDERS` | unset / `0` |

## Skip gate (session fixture)

`paper_stack` fixture probes in order; any hard failure → `pytest.skip(reason)`:

1. `GET {dashboard}/healthz` → `ok`
2. Typed `get_status` via SDK/HMAC
3. Assert paper / non-live trading mode (from status or config)
4. Assert `ib_upstream_connected` is true (else skip with IB/VNC hint)

Missing HMAC key, connection refused, or upstream down → **skip**, never red CI on a laptop without Docker.

## Fixtures

`tests/paper_e2e/conftest.py`:

| Fixture | Role |
|---------|------|
| `paper_stack` | Skip gate + resolved URLs / mode flags |
| `dashboard_client` | httpx client; `POST /session` with token; cookie jar; `csrf_headers()` from `GET /api/commands/csrf-token` |
| `typed_sdk` | SDK (or thin typed client) on host `42101`/`42102` + key file |
| `e2e_id` | Unique suffix `e2e_{pid}_{ts}` for temp portfolios / cleanup |

Markers:

- `paper_e2e` — all tests in this package
- `paper_e2e_live_orders` — mutations that can place paper orders or arm automation / allocation; skipped unless `MMR_PAPER_E2E_LIVE_ORDERS=1`
- `paper_e2e_restart` — optional Portfolios-survive-trader-restart; skipped unless `MMR_PAPER_E2E_RESTART=1`

Teardown: delete temp universes `e2e_*`, reject leftover `e2e` proposals; never delete operator portfolios (`keep`, `paper_strats`, …).

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

Register marker in `pytest.ini` / `pyproject.toml` (`markers = paper_e2e: ...`).

## Layer A — smoke (`test_smoke.py`)

Ordered ladder (fail fast):

1. `GET /healthz`, `GET /readyz`
2. Session login → `GET /api/cc-health` (or `/api/health` as allowed)
3. Typed `get_status` + portfolio/positions read
4. Resolve one liquid US symbol (e.g. `AAPL` / known conId)
5. Create proposal (market or limit, tiny size) → **reject** by default  
   - If `MMR_PAPER_E2E_LIVE_ORDERS=1`: approve once and assert order/proposal terminal state

## Layer B — dashboard HTTP (tabs)

All mutations use session + `X-CSRF-Token` unless route is documented exempt.

### Portfolios (watchlists)

HTTP flows:

- `POST /watchlists/create` → `POST .../add` symbols → `GET /watchlists/{name}/members`
- Remove one member → optional CSV `.../upload` → `POST .../delete`
- Name always `e2e_{suffix}`

Typed parity (Layer C or same file with dual client): `create_universe`, `add_universe_symbols`, `get_universe`, `remove_universe_symbol`, `delete_universe`.

Optional restart mark: after add, `docker compose restart trader`, wait for health/RPC, assert members unchanged (guards volume/seed regressions).

### Strategies

- Read: `/api/snapshot` strategies rows; `/api/strategies/{name}/params`
- Enable / disable via `POST /api/commands/strategies/{name}/enable|disable`
- Params round-trip: GET → POST a documented harmless tunable (or no-op same values) → GET equals
- Deploy/undeploy form paths: exercise only when a fixture strategy module is available under `strategies/`; otherwise skip with reason — do not mutate operator `strategy_runtime.yaml` without an isolated temp name `e2e_*`

Runtime panel contract: enabled strategy row exposes class / bar_size / universe-or-conids / state (non-empty where deployed).

### Research

- `GET /api/research/presets`
- `GET /api/research/ideas`, `movers`, `snapshot`, `news` (query params per research-tab design)
- Soft-pass: known entitlement / empty-universe responses (assert structured error, not 5xx)
- Hard-fail: 401/403 without session, 5xx, malformed HTML on JSON routes
- Flow: research row / resolve → `POST /api/commands/proposals` (or research propose path) → reject (default) / approve (live-orders)

### Scaling

- Snapshot `scaling` view: never treat “no events” as healthy green; assert shape (`status`, `message`, `max_gross_allocation`, …)
- Paper-automation status query (HTTP and/or typed `get_paper_automation_status`)
- Default: assert refuse/unarmed behaviour without activating
- Live-orders only: preflight when required → `allocation/activate` / `suspend`; `paper-automation/activate` / `deactivate` with compensate (deactivate in teardown)

### Other command-center commands

Cover remaining production command routes used by the CC (pause/resume, cancel order / cancel-all, close position) under live-orders or read-only preflight as appropriate — paper-safe defaults prefer reject/cancel of e2e-created state only.

## Layer C — typed RPC

Query smoke (representative, not every alias): `get_status`, `get_positions`, `get_open_orders`, `list_universes`, `get_universe`, `discover_instrument` / `resolve_instrument`, `list_proposals`, `get_proposal`, `get_trading_control`, `get_paper_automation_status`.

Command smoke (idempotent / cleaned up):

- Universe CRUD for `e2e_*`
- `create_proposal` → `reject_proposal` (default) / `approve_proposal` (live-orders)
- `enable_strategy` / `disable_strategy` / `update_strategy_params` on a designated e2e or already-deployed strategy
- `pause_trading` / `resume_trading` (restore resume in teardown)
- Allocation / paper-automation commands only under live-orders mark

Assert BYPASS methods remain absent (`place_order_simple`, etc.) via a small negative test if cheap.

Strategy control ports stay **internal**; host tests go through trader command surface or `docker exec` CLI only if a dedicated helper is needed — prefer trader `42102`.

## Auth matrix (must hold)

| Surface | Auth |
|---------|------|
| `/healthz`, `/readyz` | none |
| Dashboard reads/commands | session cookie; commands + CSRF |
| Typed RPC | HMAC service key file |

Tests that omit auth must expect 401/403.

## Failure / flake policy

- IB pacing / market-data gaps: retry once with backoff on resolve/snapshot; then skip or xfail with explicit reason — do not silent-pass.
- Entitlement-limited research: soft assert.
- Never leave armed paper automation or suspended allocation from a failed test without teardown best-effort.

## Success criteria

1. With stack down: `pytest -m paper_e2e` exits 0 with skips only.
2. With paper stack up: Layer A green; Portfolios/Strategies/Research/Scaling suites green under default (no live-orders).
3. With `MMR_PAPER_E2E_LIVE_ORDERS=1`: approve + scaling arm paths green and teardown restores safe state.
4. Docs: short runbook section in `docs/PAPER_AUTOMATION_SETUP.md` or sibling pointing at `scripts/paper_e2e.sh`.

## Implementation order

1. Marker + conftest skip gate + `paper_e2e.sh`
2. Layer A smoke
3. Portfolios HTTP + typed CRUD
4. Strategies enable/disable + params
5. Research GETs + propose→reject flow
6. Scaling read + live-orders arm paths
7. Remaining CC commands + typed query matrix
8. Optional restart mark for Portfolios

## Open points (resolved in this design)

| Topic | Choice |
|-------|--------|
| Curl vs pytest | pytest + httpx + SDK |
| CI | opt-in local marker only |
| Live orders default | off; env gate |
| Research empty/entitlement | soft |
| Strategy host ports | not published; use trader RPC / dashboard |
| Temp data naming | `e2e_*` only |
