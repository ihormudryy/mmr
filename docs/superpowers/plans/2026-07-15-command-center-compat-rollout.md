# COMPAT Parity, Soak, Rollback, and Legacy Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the command center matches the legacy dashboard field-for-field, survive the eight-hour soak and failure drills, migrate the two retained legacy mutations onto the common auth and command boundaries, and retire the legacy dashboard and the `MMR_WEB_TOKEN` alias only after the recorded checklist passes.

**Architecture:** The legacy dashboard stays at `/` while the command center runs at `/cc`. Watchlist CRUD and CSV upload move from the per-process token/CSRF pair onto the `[M1-R]` session; deploy-from-disk stops writing `strategy_runtime.yaml` from the web process and becomes a `[M1-F3]` coordinator command with ledger idempotency, audit, and a strategy-service receipt. A parity tool reads both surfaces and exits non-zero on unexplained field divergence; a soak runner wraps `scripts/soak_harness.py` with recorded thresholds and failure injection. Retirement is a gated code-removal task that runs only after the committed checklist is fully recorded.

**Tech Stack:** CPython 3.12.13, FastAPI/Jinja2, typed JSON RPC over ZeroMQ, DuckDB, Docker Compose, pycron, urllib, docker stats, pytest.

## Global Constraints

- Source spec: `docs/superpowers/specs/2026-07-15-realtime-trading-command-center-design.md` Sections 10, 13.3, 14, and 14.1. Prerequisites merged and deployed: `[S0]`, `[G0]`, `[M1-F1..F3]`, `[M1-R]`, `[M1-C]`. This plan is the last gate before live enablement and legacy retirement.
- During the live comparison session both surfaces are read-only: `DASHBOARD_COMMANDS_ENABLED=false` and `DASHBOARD_LIVE_COMMANDS_ENABLED=false`.
- No command type is exposed on both surfaces at once. `[M1-C]` already returns `410 Gone` from the overlapping legacy approve/reject/enable/disable/params routes whenever `commands_enabled()` is true; this plan verifies that behavior and never re-implements it.
- The only retained legacy mutations are watchlist CRUD + CSV upload (session auth, existing store validation) and strategy deploy-from-disk (typed coordinator, idempotency, audit). The `dashboard` Compose service keeps its universe-store volume as a documented `[COMPAT]` exception until Task 6 or the `[M2]` watchlist redesign, whichever lands first.
- Parity tolerances: floats compare within `1e-6` absolute; timestamps compare at one-second resolution after UTC normalization; everything else is exact. Any unexplained divergence exits non-zero.
- Soak thresholds are recorded constants and fail closed on missing data: one-hour warm-up; RSS growth ≤ 20% from end of warm-up to end of run; average dashboard CPU < 1 core; p95 critical-event latency ≤ 500 ms; zero unhandled errors; zero unresolved test commands; replay ring ≤ 10,000 events / five minutes; per-client domain FIFO ≤ 1,000; terminal collections ≤ 500 rows.
- Automatic no-go/rollback triggers: a duplicate command, an unresolved command older than 15 minutes, a source-coherence failure, an audit write failure, bypass-capability exposure, or any violated soak threshold.
- Live enablement requires the exact `DASHBOARD_LIVE_ACCOUNT_ID` and a mandatory `DASHBOARD_MAX_ORDER_NOTIONAL`; there is no permissive fallback for a missing live limit.
- The old `MMR_WEB_TOKEN` credential is rotated before live commands are enabled because it may exist in URL history or logs; the alias code is removed only at retirement (Task 6).
- Commits are tagged `feat(compat)`, `ops(compat)`, or `docs(compat)`.

### Frozen interfaces consumed from other plans

A rename in any source plan requires updating this plan before implementation continues:

- `[M1-R]` `web/command_center/session.py`: `require_session` (FastAPI dependency returning `DashboardSession`; raises `HTTPException(401)` without a valid cookie), `DashboardSession.csrf_token: str`, and `verify_csrf(session, supplied)` (constant-time compare, raises `HTTPException(403)`).
- `[M1-R]` routes: `/cc`, `POST /session` (form field `token`, replies with the session `Set-Cookie`), `GET /api/snapshot` (`{"schema_version", "stream_id", "sequence", "generated_at", "entities": {entity_type: [payload, ...]}}`), authenticated `GET /api/health` (per-dependency `state` of `live|degraded|disconnected`).
- `[M1-R]` `scripts/soak_harness.py` CLI: `--duration-minutes N --instruments 100 --quote-hz 4 --domain-eps 20 --tabs 3 --commands-per-hour 12 --metrics-out PATH`; metrics JSON keys `p95_critical_ms`, `unhandled_errors`, `unresolved_commands`, `max_replay_ring_events`, `max_client_fifo_depth`, `max_terminal_rows`.
- `[M1-C]` `web/command_center/flags.py`: `commands_enabled()`, `live_commands_enabled()`; browser command routes under `/api/commands/*`; `web/command_center/gateway.py`: `get_command_gateway() -> DashboardCommandGateway` holding a `[G0]` `TypedRpcClient` on the command socket.
- `[M1-F3]` `trader/trading/command_coordinator.py`: `TradingCommandCoordinator`, `CommandLedger.claim(command_id, *, action, target, request_hash) -> LedgerClaim` where `LedgerClaim.status` is `NEW|DUPLICATE|CONFLICT` and `LedgerClaim.receipt` is the recorded `CommandReceipt` or `None`; `CommandLedger.resolve(command_id, receipt)`; `AuditLog.record(command_id, *, action, target, inputs)` which raises on persistence failure (fail closed, per spec §9.1). Strategy side: `StrategyReceiptLedger.get(command_id)` / `.record(command_id, *, state, outcome=None, error_code=None, retryable=False)` and `StrategyRevisionStore.prepare(name, *, prior, proposed, expected_control_revision)` / `.commit(revision)` / `.rolled_back(revision)`.
- `[M1-F2]` `trader/trading/order_tracker.py`: `normalize_ib_status(status: str) -> str`; SDK typed adapter `MMR.fills() -> pd.DataFrame` with columns `execution_id, conid, side, quantity, price, time, commission`.
- Plan-index freeze: `CommandReceipt(command_id, correlation_id, state, outcome, error_code, retryable)`.

---

### Task 1: Watchlist CRUD and CSV upload under command-center session auth

**Files:**
- Modify: `web/app.py:110-135,339-386,389-485,488-631`
- Modify: `tests/test_web_dashboard.py:95-110`
- Create: `tests/test_watchlist_session_auth.py`

**Interfaces:**
- Consumes: `require_session`, `DashboardSession.csrf_token`, `verify_csrf` from `[M1-R]`; `commands_enabled()` and the 410 overlap-disable behavior from `[M1-C]`.
- Produces: every legacy mutation route (watchlists, upload, and — until they are removed — approve/reject/enable/disable/params/deploy) authenticated by the shared session with a session-bound CSRF token.
- Removes: module globals `_ACCESS_TOKEN`, `_check_access`, `_CSRF_TOKEN`, `_check_csrf` from `web/app.py`. `MMR_WEB_TOKEN` handling lives only in the `[M1-R]` session module's deprecated-alias path until Task 6.

- [ ] **Step 1: Write failing session-auth and overlap-verification tests**

Create `tests/test_watchlist_session_auth.py`:

```python
"""[COMPAT] Task 1: legacy watchlist CRUD/CSV under the M1-R session.

Spec: 2026-07-15-realtime-trading-command-center-design.md §10, §14.1.
"""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import web.app as webapp
from web.command_center.session import require_session


class _FakeSession:
    session_id = 'compat-test-session'
    csrf_token = 'csrf-test-token'


class _StubAccessor:
    def __init__(self):
        self.universes: dict[str, list] = {}

    def list_universes_count(self):
        return {k: len(v) for k, v in self.universes.items()}

    def get(self, name):
        from types import SimpleNamespace
        return SimpleNamespace(name=name,
                               security_definitions=list(self.universes.get(name, [])),
                               find_symbol=lambda s: None)

    def update(self, universe):
        self.universes.setdefault(universe.name, list(universe.security_definitions))

    def insert(self, name, sd):
        self.universes.setdefault(name, []).append(sd)

    def delete(self, name):
        self.universes.pop(name, None)


class _StubSDK:
    def account_cash(self): return None
    def portfolio_snapshot(self): return None
    def status(self): return None
    def risk_report(self): return None
    def get_risk_limits(self): return None
    def portfolio(self): return pd.DataFrame()
    def proposals(self, limit=100): return pd.DataFrame()
    def strategies(self): return pd.DataFrame()


@pytest.fixture
def stub(monkeypatch):
    accessor = _StubAccessor()
    monkeypatch.setattr(webapp, '_mmr', _StubSDK())
    monkeypatch.setattr(webapp, '_get_accessor', lambda: accessor)
    monkeypatch.setattr(webapp, 'scan_strategies', lambda *a, **k: [])
    return accessor


@pytest.fixture
def anon_client(stub):
    return TestClient(webapp.app)


@pytest.fixture
def client(stub):
    webapp.app.dependency_overrides[require_session] = lambda: _FakeSession()
    try:
        yield TestClient(webapp.app)
    finally:
        webapp.app.dependency_overrides.pop(require_session, None)


WATCHLIST_POSTS = [
    ('/watchlists/create', {'name': 'asx'}),
    ('/watchlists/asx/add', {'symbols': 'BHP'}),
    ('/watchlists/asx/remove', {'symbol': 'BHP'}),
    ('/watchlists/asx/delete', {}),
]


@pytest.mark.parametrize('path,data', WATCHLIST_POSTS)
def test_watchlist_mutation_requires_session(anon_client, path, data):
    r = anon_client.post(path, data={**data, 'csrf_token': 'anything'},
                         follow_redirects=False)
    assert r.status_code == 401


def test_watchlist_create_accepts_session_csrf(client, stub):
    r = client.post('/watchlists/create',
                    data={'name': 'asx', 'csrf_token': _FakeSession.csrf_token},
                    follow_redirects=False)
    assert r.status_code == 303
    assert 'created' in r.headers['location']
    assert 'asx' in stub.universes


def test_watchlist_create_rejects_wrong_csrf(client):
    r = client.post('/watchlists/create',
                    data={'name': 'asx', 'csrf_token': 'stale-or-forged'},
                    follow_redirects=False)
    assert r.status_code == 403


def test_per_process_csrf_globals_are_gone():
    assert not hasattr(webapp, '_CSRF_TOKEN')
    assert not hasattr(webapp, '_ACCESS_TOKEN')


def test_csv_upload_requires_session(anon_client):
    r = anon_client.post('/watchlists/asx/upload',
                         files={'file': ('w.csv', b'symbol\nBHP\n', 'text/csv')},
                         data={'csrf_token': 'anything'}, follow_redirects=False)
    assert r.status_code == 401


def test_query_token_never_authenticates(anon_client):
    r = anon_client.get('/?token=legacy-value', follow_redirects=False)
    assert r.status_code == 401


OVERLAPPING_POSTS = [
    '/proposals/7/approve',
    '/proposals/7/reject',
    '/strategies/orb_googl/enable',
    '/strategies/orb_googl/disable',
    '/strategies/orb_googl/params',
]


@pytest.mark.parametrize('path', OVERLAPPING_POSTS)
def test_overlapping_legacy_mutations_gone_when_commands_enabled(
        client, monkeypatch, path):
    monkeypatch.setenv('DASHBOARD_COMMANDS_ENABLED', 'true')
    r = client.post(path, data={'csrf_token': _FakeSession.csrf_token,
                                'param_A': '1'}, follow_redirects=False)
    assert r.status_code == 410


def test_watchlist_routes_survive_commands_enabled(client, monkeypatch, stub):
    monkeypatch.setenv('DASHBOARD_COMMANDS_ENABLED', 'true')
    r = client.post('/watchlists/create',
                    data={'name': 'keep', 'csrf_token': _FakeSession.csrf_token},
                    follow_redirects=False)
    assert r.status_code == 303
    assert 'keep' in stub.universes
```

- [ ] **Step 2: Run the new tests and verify failure**

Run: `uv run --frozen pytest tests/test_watchlist_session_auth.py -q`

Expected: FAIL — the routes still use `_check_access`/`_check_csrf`, `_CSRF_TOKEN` still exists, and unauthenticated posts return 303/403 rather than 401.

- [ ] **Step 3: Replace the per-process token/CSRF pair with the session**

In `web/app.py`, delete `_ACCESS_TOKEN`, `_check_access`, `_CSRF_TOKEN`, and `_check_csrf`, and import the session contract:

```python
from fastapi import Depends

from web.command_center.session import DashboardSession, require_session, verify_csrf
```

Give every route the dependency and verify the session-bound token. The page route feeds the session token into the existing `{{ csrf_token }}` template slot, so `dashboard.html` is unchanged:

```python
@app.get('/')
def dashboard(request: Request, flash: str = '',
              session: DashboardSession = Depends(require_session)):
    ...
    return _TEMPLATES.TemplateResponse(request, 'dashboard.html', {
        ...
        'csrf_token': session.csrf_token,
    })
```

Pattern for every mutation route (shown for `watchlist_create`; apply identically to `add`, `upload`, `remove`, `delete`, `approve`, `reject`, `enable_strategy`, `disable_strategy`, `update_strategy_params`, and `deploy_strategy`):

```python
@app.post('/watchlists/create')
def watchlist_create(request: Request, name: str = Form(''), csrf_token: str = Form(''),
                     session: DashboardSession = Depends(require_session)):
    verify_csrf(session, csrf_token)
    ...
```

For the two `async` form routes read the token from the parsed form: `verify_csrf(session, str(form.get('csrf_token') or ''))`. Keep all store validation (`_WATCHLIST_NAME_RE`, resolve-or-fail symbol handling, 1 MB CSV cap, UTF-8 check) exactly as it is.

- [ ] **Step 4: Update the existing dashboard test fixtures to the session override**

In `tests/test_web_dashboard.py`, replace the `client` fixture and `_csrf()` helper:

```python
from web.command_center.session import require_session


class _FakeSession:
    session_id = 'test-session'
    csrf_token = 'csrf-test-token'


@pytest.fixture
def client(stub):
    webapp.app.dependency_overrides[require_session] = lambda: _FakeSession()
    try:
        yield TestClient(webapp.app)
    finally:
        webapp.app.dependency_overrides.pop(require_session, None)


def _csrf():
    return _FakeSession.csrf_token
```

- [ ] **Step 5: Run both web test files**

Run: `uv run --frozen pytest tests/test_watchlist_session_auth.py tests/test_web_dashboard.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/app.py tests/test_watchlist_session_auth.py tests/test_web_dashboard.py
git commit -m "feat(compat): watchlist CRUD and legacy mutations under session auth"
```

### Task 2: Deploy-from-disk through the typed command coordinator

**Files:**
- Create: `trader/strategy/strategy_deploy.py`
- Modify: `trader/trading/command_coordinator.py` (add `StrategyDeploySpec` + `deploy_strategy`)
- Modify: `trader/messaging/production_api.py` (register `deploy_strategy` on the command socket)
- Modify: `trader/strategy/strategy_runtime.py` (wire `StrategyDeployService`, expose `apply_strategy_deploy` / `get_strategy_receipt` on the strategy service's typed socket)
- Modify: `web/command_center/gateway.py` (add `DashboardCommandGateway.deploy_strategy`)
- Modify: `web/app.py:633-746`
- Modify: `web/templates/dashboard.html:390-460` (hidden `command_id` in the deploy form)
- Create: `tests/test_deploy_via_coordinator.py`

**Interfaces:**
- Consumes: `CommandLedger`, `LedgerClaim`, `AuditLog`, `StrategyReceiptLedger`, `StrategyRevisionStore` from `[M1-F3]`; `CommandReceipt` from the index freeze; `get_command_gateway()` from `[M1-C]`.
- Produces: `StrategyDeploySpec` (Pydantic, `extra="forbid"`), coordinator command `deploy_strategy(command_id: str, spec: StrategyDeploySpec) -> CommandReceipt` on the `command` socket role, strategy-service typed methods `apply_strategy_deploy` and `get_strategy_receipt`, and `DashboardCommandGateway.deploy_strategy(command_id, spec) -> CommandReceipt`.
- Removes: all `strategy_runtime.yaml` reads/writes from `web/app.py` (`_STRATEGY_CONFIG_PATH` disappears from the web process).

- [ ] **Step 1: Write failing coordinator, strategy-side, and web-route tests**

Create `tests/test_deploy_via_coordinator.py`:

```python
"""[COMPAT] Task 2: deploy-from-disk is a coordinator command, not a web YAML write."""
import dataclasses
from pathlib import Path

import pytest
import yaml

from trader.trading.command_coordinator import (
    CommandReceipt, LedgerClaim, StrategyDeploySpec)


class FakeLedger:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    def claim(self, command_id, *, action, target, request_hash):
        row = self.rows.get(command_id)
        if row is None:
            self.rows[command_id] = {'hash': request_hash, 'receipt': None}
            return LedgerClaim(status='NEW', receipt=None)
        if row['hash'] != request_hash:
            return LedgerClaim(status='CONFLICT', receipt=None)
        return LedgerClaim(status='DUPLICATE', receipt=row['receipt'])

    def resolve(self, command_id, receipt):
        self.rows[command_id]['receipt'] = receipt
        return receipt


class FakeAudit:
    def __init__(self, fail=False):
        self.records, self.fail = [], fail

    def record(self, command_id, *, action, target, inputs):
        if self.fail:
            raise IOError('audit store unavailable')
        self.records.append((command_id, action, target))


class FakeStrategyClient:
    def __init__(self):
        self.calls = []
        self.receipt_state = 'COMMITTED'

    def call(self, method, body, response_model):
        self.calls.append((method, body))
        return CommandReceipt(command_id=body['command_id'], correlation_id=body['command_id'],
                              state=self.receipt_state,
                              outcome={'strategy': 'orb_test', 'control_revision': 1},
                              error_code=None, retryable=False)


SPEC = StrategyDeploySpec(
    name='orb_test', module='strategies/opening_range_breakout.py',
    class_name='OpeningRangeBreakout', bar_size='1 min',
    historical_days_prior=90, conids=None, universe='asx_mining',
    params={'RANGE_MINUTES': 45}, auto_execute='propose')


@pytest.fixture
def coordinator():
    from trader.trading.command_coordinator import TradingCommandCoordinator
    coord = TradingCommandCoordinator.__new__(TradingCommandCoordinator)
    coord._ledger = FakeLedger()
    coord._audit = FakeAudit()
    coord._strategy_client = FakeStrategyClient()
    return coord


class TestCoordinatorDeploy:
    def test_deploy_dispatches_once_per_command_id(self, coordinator):
        first = coordinator.deploy_strategy('deploy-1', SPEC)
        second = coordinator.deploy_strategy('deploy-1', SPEC)
        assert first.state == 'COMMITTED' and second == first
        dispatches = [c for c in coordinator._strategy_client.calls
                      if c[0] == 'apply_strategy_deploy']
        assert len(dispatches) == 1

    def test_same_id_different_payload_conflicts(self, coordinator):
        coordinator.deploy_strategy('deploy-1', SPEC)
        other = dataclasses.replace if False else SPEC.model_copy(update={'name': 'other'})
        receipt = coordinator.deploy_strategy('deploy-1', other)
        assert receipt.state == 'REJECTED'
        assert receipt.error_code == 'COMMAND_ID_CONFLICT'

    def test_audit_failure_blocks_dispatch(self, coordinator):
        coordinator._audit = FakeAudit(fail=True)
        with pytest.raises(IOError):
            coordinator.deploy_strategy('deploy-2', SPEC)
        assert coordinator._strategy_client.calls == []

    def test_unresolved_duplicate_requeries_strategy_receipt(self, coordinator):
        coordinator._ledger.rows['deploy-3'] = {
            'hash': coordinator._deploy_request_hash(SPEC), 'receipt': None}
        receipt = coordinator.deploy_strategy('deploy-3', SPEC)
        assert ('get_strategy_receipt', {'command_id': 'deploy-3'}) in \
            coordinator._strategy_client.calls
        assert receipt.state == 'COMMITTED'


class FakeReceipts:
    def __init__(self):
        self.rows: dict[str, CommandReceipt] = {}

    def get(self, command_id):
        return self.rows.get(command_id)

    def record(self, command_id, *, state, outcome=None, error_code=None, retryable=False):
        receipt = CommandReceipt(command_id=command_id, correlation_id=command_id,
                                 state=state, outcome=outcome,
                                 error_code=error_code, retryable=retryable)
        self.rows[command_id] = receipt
        return receipt


class FakeRevisions:
    def __init__(self):
        self.committed, self.rolled = [], []
        self._n = 0

    def prepare(self, name, *, prior, proposed, expected_control_revision):
        from types import SimpleNamespace
        self._n += 1
        return SimpleNamespace(name=name, control_revision=self._n)

    def commit(self, revision):
        self.committed.append(revision.name)

    def rolled_back(self, revision):
        self.rolled.append(revision.name)


@pytest.fixture
def deploy_service(tmp_path):
    from trader.strategy.strategy_deploy import StrategyDeployService
    strategies_dir = tmp_path / 'strategies'
    strategies_dir.mkdir()
    (strategies_dir / 'opening_range_breakout.py').write_text('# strategy module\n')
    config_path = tmp_path / 'strategy_runtime.yaml'
    config_path.write_text('strategies: []\n')
    activations = []
    svc = StrategyDeployService(
        receipts=FakeReceipts(), revisions=FakeRevisions(),
        config_path=config_path, strategies_directory=str(strategies_dir),
        activate=lambda name, entry: activations.append(name))
    svc.test_activations = activations
    return svc


class TestStrategyDeployService:
    def test_commit_appends_entry_and_activates(self, deploy_service):
        receipt = deploy_service.apply('deploy-1', SPEC.model_dump())
        assert receipt.state == 'COMMITTED'
        config = yaml.safe_load(deploy_service._config_path.read_text())
        assert config['strategies'][0]['name'] == 'orb_test'
        assert config['strategies'][0]['universe'] == 'asx_mining'
        assert deploy_service.test_activations == ['orb_test']

    def test_replay_returns_recorded_receipt_without_rewriting(self, deploy_service):
        first = deploy_service.apply('deploy-1', SPEC.model_dump())
        second = deploy_service.apply('deploy-1', SPEC.model_dump())
        assert second == first
        assert deploy_service.test_activations == ['orb_test']

    def test_duplicate_name_rejected(self, deploy_service):
        deploy_service.apply('deploy-1', SPEC.model_dump())
        receipt = deploy_service.apply('deploy-2', SPEC.model_dump())
        assert receipt.state == 'REJECTED'
        assert receipt.error_code == 'DUPLICATE_STRATEGY_NAME'

    def test_traversal_module_rejected(self, deploy_service):
        bad = SPEC.model_dump() | {'module': 'strategies/../../etc/passwd'}
        receipt = deploy_service.apply('deploy-3', bad)
        assert receipt.state == 'REJECTED'
        assert receipt.error_code == 'MODULE_OUTSIDE_SANDBOX'

    def test_activation_failure_rolls_back_config(self, deploy_service):
        def boom(name, entry):
            raise RuntimeError('load failed')
        deploy_service._activate = boom
        receipt = deploy_service.apply('deploy-4', SPEC.model_dump())
        assert receipt.state == 'ROLLED_BACK'
        config = yaml.safe_load(deploy_service._config_path.read_text())
        assert config['strategies'] == []
        assert deploy_service._revisions.rolled == ['orb_test']
```

Append the web-route tests to the same file:

```python
import web.app as webapp
from fastapi.testclient import TestClient
from web.command_center.session import require_session


class _FakeSession:
    csrf_token = 'csrf-test-token'


class StubGateway:
    def __init__(self):
        self.calls = []

    def deploy_strategy(self, command_id, spec):
        self.calls.append((command_id, spec))
        return CommandReceipt(command_id=command_id, correlation_id=command_id,
                              state='COMMITTED',
                              outcome={'strategy': spec.name, 'control_revision': 1},
                              error_code=None, retryable=False)


_SCANNED = [{'file': 'opening_range_breakout.py', 'class': 'OpeningRangeBreakout',
             'mode': 'precompute', 'tunables': {'RANGE_MINUTES': 30},
             'docstring': 'ORB.', 'docstring_full': 'ORB.'}]


@pytest.fixture
def web_client(monkeypatch):
    gateway = StubGateway()
    monkeypatch.setattr(webapp, 'scan_strategies', lambda *a, **k: list(_SCANNED))
    monkeypatch.setattr(webapp, 'get_command_gateway', lambda: gateway)
    webapp.app.dependency_overrides[require_session] = lambda: _FakeSession()
    try:
        yield TestClient(webapp.app), gateway
    finally:
        webapp.app.dependency_overrides.pop(require_session, None)


DEPLOY_FORM = {
    'csrf_token': 'csrf-test-token',
    'command_id': 'deploy-6f31c9de-2f6f-4c65-9f4e-a6a9a1a54321',
    'file': 'opening_range_breakout.py', 'class': 'OpeningRangeBreakout',
    'name': 'orb_test', 'bar_size': '1 min', 'days': '90',
    'watchlist': 'asx_mining', 'auto_propose': 'on', 'param_RANGE_MINUTES': '45',
}


class TestDeployRoute:
    def test_route_submits_spec_and_writes_no_yaml(self, web_client, tmp_path):
        client, gateway = web_client
        r = client.post('/strategies/deploy', data=DEPLOY_FORM, follow_redirects=False)
        assert r.status_code == 303 and 'deployed' in r.headers['location']
        command_id, spec = gateway.calls[0]
        assert command_id == DEPLOY_FORM['command_id']
        assert spec.module == 'strategies/opening_range_breakout.py'
        assert spec.universe == 'asx_mining' and spec.conids is None
        assert spec.params == {'RANGE_MINUTES': 45}
        assert spec.auto_execute == 'propose'
        assert not hasattr(webapp, '_STRATEGY_CONFIG_PATH')

    def test_route_requires_well_formed_command_id(self, web_client):
        client, gateway = web_client
        r = client.post('/strategies/deploy',
                        data={**DEPLOY_FORM, 'command_id': ''}, follow_redirects=False)
        assert 'missing command id' in r.headers['location']
        assert gateway.calls == []
```

- [ ] **Step 2: Run the new tests and verify failure**

Run: `uv run --frozen pytest tests/test_deploy_via_coordinator.py -q`

Expected: FAIL — `StrategyDeploySpec`, `deploy_strategy`, `StrategyDeployService`, and `get_command_gateway` usage do not exist yet.

- [ ] **Step 3: Add the spec model and coordinator command**

In `trader/trading/command_coordinator.py`, beside the `[M1-F3]` strategy command models:

```python
import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trader.messaging.typed_rpc import canonical_json


class StrategyDeploySpec(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(pattern=r'^[a-z0-9_-]{1,40}$')
    module: str = Field(pattern=r'^strategies/[A-Za-z0-9_]+\.py$')
    class_name: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')
    bar_size: str
    historical_days_prior: int = Field(ge=1, le=3650)
    conids: list[int] | None = None
    universe: str | None = None
    params: dict[str, int | float | bool | str] = {}
    auto_execute: Literal['propose'] | None = None


class StrategyDeployCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')

    command_id: str = Field(pattern=r'^deploy-[0-9a-fA-F-]{36}$')
    spec: StrategyDeploySpec
```

Coordinator methods (same ledger/audit/receipt discipline as the other strategy commands; spec §9.1 and §9.5):

```python
def _deploy_request_hash(self, spec: StrategyDeploySpec) -> str:
    return hashlib.sha256(canonical_json(spec.model_dump())).hexdigest()

def deploy_strategy(self, command_id: str, spec: StrategyDeploySpec) -> CommandReceipt:
    claim = self._ledger.claim(command_id, action='strategy.deploy',
                               target=spec.name,
                               request_hash=self._deploy_request_hash(spec))
    if claim.status == 'CONFLICT':
        return CommandReceipt(command_id=command_id, correlation_id=command_id,
                              state='REJECTED', outcome=None,
                              error_code='COMMAND_ID_CONFLICT', retryable=False)
    if claim.status == 'DUPLICATE':
        if claim.receipt is not None:
            return claim.receipt
        recorded = self._strategy_client.call(
            'get_strategy_receipt', {'command_id': command_id}, CommandReceipt)
        if recorded is not None and recorded.state in ('COMMITTED', 'ROLLED_BACK', 'REJECTED'):
            return self._ledger.resolve(command_id, recorded)
        return CommandReceipt(command_id=command_id, correlation_id=command_id,
                              state='OUTCOME_UNKNOWN', outcome=None,
                              error_code=None, retryable=True)
    # Fail closed: an audit failure here raises before any dispatch (§9.1).
    self._audit.record(command_id, action='strategy.deploy', target=spec.name,
                       inputs=spec.model_dump())
    receipt = self._strategy_client.call(
        'apply_strategy_deploy',
        {'command_id': command_id, 'spec': spec.model_dump()}, CommandReceipt)
    return self._ledger.resolve(command_id, receipt)

def handle_deploy_strategy(self, command: StrategyDeployCommand) -> CommandReceipt:
    return self.deploy_strategy(command.command_id, command.spec)
```

Register it in `trader/messaging/production_api.py` next to the other coordinator commands:

```python
registry.register('command', 'deploy_strategy', StrategyDeployCommand, CommandReceipt,
                  coordinator.handle_deploy_strategy)
```

- [ ] **Step 4: Implement the strategy-service deploy service**

Create `trader/strategy/strategy_deploy.py`:

```python
"""[COMPAT] Deploy-from-disk applied inside strategy_service.

Order of operations follows spec §9.3: record the revision PREPARED, stage the
YAML, activate the runtime, atomically rename the staged file, then COMMIT.
Any failure before the rename leaves the on-disk config untouched and marks
the revision ROLLED_BACK.
"""
from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

import yaml

from trader.trading.command_coordinator import CommandReceipt, StrategyDeploySpec


class StrategyDeployService:
    def __init__(self, receipts, revisions, config_path, strategies_directory, activate):
        self._receipts = receipts
        self._revisions = revisions
        self._config_path = Path(config_path)
        self._strategies_directory = strategies_directory
        self._activate = activate

    def apply(self, command_id: str, spec_data: dict) -> CommandReceipt:
        recorded = self._receipts.get(command_id)
        if recorded is not None:
            return recorded
        spec = StrategyDeploySpec.model_validate(spec_data)
        try:
            self._resolve_module_path(spec.module)
        except ValueError:
            return self._receipts.record(command_id, state='REJECTED',
                                         error_code='MODULE_OUTSIDE_SANDBOX',
                                         retryable=False)
        config = self._read_config()
        entries = config.setdefault('strategies', [])
        if any(e.get('name') == spec.name for e in entries):
            return self._receipts.record(command_id, state='REJECTED',
                                         error_code='DUPLICATE_STRATEGY_NAME',
                                         retryable=False)
        entry = self._entry_for(spec)
        revision = self._revisions.prepare(spec.name, prior=None, proposed=entry,
                                           expected_control_revision=0)
        staged = f'{self._config_path}.staged-{command_id}'
        with open(staged, 'w') as f:
            yaml.safe_dump({**config, 'strategies': entries + [entry]}, f, sort_keys=False)
        try:
            self._activate(spec.name, entry)
            os.replace(staged, self._config_path)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(staged)
            self._revisions.rolled_back(revision)
            logging.exception('deploy %s rolled back', spec.name)
            return self._receipts.record(command_id, state='ROLLED_BACK',
                                         error_code='ACTIVATION_FAILED', retryable=True)
        self._revisions.commit(revision)
        return self._receipts.record(
            command_id, state='COMMITTED',
            outcome={'strategy': spec.name, 'control_revision': revision.control_revision})

    def _resolve_module_path(self, module: str) -> Path:
        if module != os.path.normpath(module):
            raise ValueError(f'module path not normalized: {module!r}')
        strategies_dir = Path(self._strategies_directory).expanduser().resolve()
        candidate = (strategies_dir / Path(module).name).resolve()
        if candidate.parent != strategies_dir or not candidate.is_file():
            raise ValueError(f'module outside sandbox or missing: {module!r}')
        return candidate

    def _read_config(self) -> dict:
        if self._config_path.exists():
            return yaml.safe_load(self._config_path.read_text()) or {}
        return {}

    def _entry_for(self, spec: StrategyDeploySpec) -> dict:
        entry: dict = {
            'name': spec.name,
            'description': f'Deployed from dashboard ({spec.class_name} in {spec.module})',
            'module': spec.module,
            'class_name': spec.class_name,
            'bar_size': spec.bar_size,
            'historical_days_prior': spec.historical_days_prior,
        }
        if spec.conids:
            entry['conids'] = list(spec.conids)
        else:
            entry['universe'] = spec.universe
        if spec.auto_execute:
            entry['auto_execute'] = spec.auto_execute
        if spec.params:
            entry['params'] = dict(spec.params)
        return entry
```

In `trader/strategy/strategy_runtime.py`, construct one `StrategyDeployService` with the runtime's `StrategyReceiptLedger`, `StrategyRevisionStore`, `strategy_config_file`, `strategies_directory`, and an `activate` closure that calls the existing `config_loader`-based load for the single new entry followed by `enable_strategy(name)`. Register `apply_strategy_deploy` (delegating to `service.apply`) and `get_strategy_receipt` (delegating to `receipts.get`) on the strategy service's typed socket next to the `[M1-F3]` parameter-mutation handlers.

- [ ] **Step 5: Rewrite the web route as a gateway call**

In `web/app.py`, delete `_STRATEGY_CONFIG_PATH` and `_coerce_yaml_value`, import `StrategyDeploySpec` and `get_command_gateway`, and replace the body of `/strategies/deploy`:

```python
from trader.trading.command_coordinator import StrategyDeploySpec
from web.command_center.gateway import get_command_gateway

_COMMAND_ID_RE = re.compile(r'^deploy-[0-9a-fA-F-]{36}$')


@app.post('/strategies/deploy')
async def deploy_strategy(request: Request,
                          session: DashboardSession = Depends(require_session)):
    """Deploy an on-disk strategy through the typed command coordinator.

    The web process validates for UX only (scanner sandbox, name shape,
    exactly-one target) and resolves symbols through the read path. The
    authoritative validation, YAML staging, revision commit, idempotency,
    and audit happen behind the [M1-F3] coordinator — this process writes
    no strategy_runtime.yaml."""
    form = await request.form()
    verify_csrf(session, str(form.get('csrf_token') or ''))

    file_name = str(form.get('file') or '').strip()
    class_name = str(form.get('class') or '').strip()
    name = str(form.get('name') or '').strip().lower()
    command_id = str(form.get('command_id') or '').strip()
    bar_size = str(form.get('bar_size') or '1 min').strip()
    days = str(form.get('days') or '90').strip()
    symbols = _split_symbols(str(form.get('symbols') or ''))
    watchlist = str(form.get('watchlist') or '').strip()
    auto_propose = bool(form.get('auto_propose'))
    params = {k[len('param_'):]: str(v) for k, v in form.items()
              if k.startswith('param_') and str(v).strip() != ''}

    known = {(r['file'], r['class']) for r in scan_strategies(_STRATEGIES_DIR)}
    if (file_name, class_name) not in known:
        return _flash(f'unknown strategy {class_name} in {file_name} — not deploying')
    if not _WATCHLIST_NAME_RE.match(name or ''):
        return _flash(f'invalid deployment name {name!r} — use a-z, 0-9, -, _ (max 40)')
    if not _COMMAND_ID_RE.match(command_id):
        return _flash('missing command id — reload the page and retry')
    if bool(symbols) == bool(watchlist):
        return _flash('give either symbols or a watchlist (exactly one)')

    def _submit() -> str:
        conids = None
        if symbols:
            resolved, missing = _resolve_symbols(symbols)
            if missing:
                return ('deploy aborted — unresolved: ' + ', '.join(missing)
                        + ' (nothing submitted)')
            accessor = _get_accessor()
            for sd in resolved:
                accessor.insert(f'strat_{name}', sd)
            conids = [sd.conId for sd in resolved]
        spec = StrategyDeploySpec(
            name=name, module=f'strategies/{file_name}', class_name=class_name,
            bar_size=bar_size,
            historical_days_prior=int(days) if days.isdigit() else 90,
            conids=conids, universe=watchlist or None,
            params={k: _coerce_param(v) for k, v in params.items()},
            auto_execute='propose' if auto_propose else None)
        receipt = get_command_gateway().deploy_strategy(command_id, spec)
        if receipt.state == 'COMMITTED':
            target = ', '.join(symbols) if symbols else f'watchlist {watchlist}'
            return f'deployed & enabled "{name}" ({class_name}) on {target}'
        if receipt.state in ('REJECTED', 'ROLLED_BACK'):
            return f'deploy {receipt.state.lower()}: {receipt.error_code}'
        return (f'deploy {receipt.state} — outcome pending; command {command_id} '
                'is idempotent, retry the same form or check the command ledger')

    try:
        msg = await run_in_threadpool(_submit)
    except Exception as exc:  # noqa: BLE001
        logger.warning('deploy failed: %s', exc)
        msg = f'deploy error: {type(exc).__name__}: {exc}'
    return _flash(msg)


def _coerce_param(text: str):
    t = (text or '').strip()
    if t.lower() in ('true', 'false'):
        return t.lower() == 'true'
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t
```

Add `DashboardCommandGateway.deploy_strategy` in `web/command_center/gateway.py`:

```python
def deploy_strategy(self, command_id: str, spec: StrategyDeploySpec) -> CommandReceipt:
    return self._command_client.call(
        'deploy_strategy',
        {'command_id': command_id, 'spec': spec.model_dump()},
        CommandReceipt,
    )
```

In `web/templates/dashboard.html`, add a hidden field inside each deploy form and populate it once per page render, so a double-submit retries the same idempotent command:

```html
<input type="hidden" name="command_id" class="deploy-cmd-id" value="">
```

```html
<script>
  document.querySelectorAll('.deploy-cmd-id').forEach(
    el => { el.value = 'deploy-' + crypto.randomUUID(); });
</script>
```

- [ ] **Step 6: Run deploy, reconcile, and web regressions**

Run: `uv run --frozen pytest tests/test_deploy_via_coordinator.py tests/test_strategy_runtime_reconcile.py tests/test_web_dashboard.py tests/test_watchlist_session_auth.py -q`

Expected: PASS. The reconcile tests still pass because the strategy service remains the only `strategy_runtime.yaml` writer.

- [ ] **Step 7: Commit**

```bash
git add trader/strategy/strategy_deploy.py trader/strategy/strategy_runtime.py trader/trading/command_coordinator.py trader/messaging/production_api.py web/command_center/gateway.py web/app.py web/templates/dashboard.html tests/test_deploy_via_coordinator.py
git commit -m "feat(compat): route deploy-from-disk through the typed coordinator"
```

### Task 3: Field-level parity comparison tool

**Files:**
- Create: `scripts/parity_compare.py`
- Create: `tests/test_parity_compare.py`
- Modify: `config_defaults/pycron.yaml` (scheduled one-shot `parity_compare` job)

**Interfaces:**
- Consumes: `GET /api/snapshot` and `POST /session` from `[M1-R]`; `proposal_display_status` from `[S0]`; `normalize_ib_status` and `MMR.fills()` from `[M1-F2]`; legacy fetchers in `web/app.py`.
- Produces: `values_match(a, b, kind)`, `compare_surfaces(legacy, center, allow) -> ParityReport`, `FieldDivergence`, `login(base_url, token) -> str` (session cookie), CLI exit codes `0` (parity), `1` (unexplained divergence), `2` (collection failure), and a JSON report at `~/.local/share/mmr/reports/parity_<UTC-ts>.json`.

- [ ] **Step 1: Write failing comparison-core tests**

Create `tests/test_parity_compare.py`:

```python
"""[COMPAT] Task 3: field-level parity core (tolerances, keys, exit codes)."""
import json

import pytest

from scripts.parity_compare import (
    FieldDivergence, ParityReport, compare_keyed, compare_surfaces,
    normalize_center_proposal, normalize_legacy_proposal, values_match)


class TestValuesMatch:
    def test_float_within_tolerance(self):
        assert values_match(100.0000004, 100.0000009, 'float')

    def test_float_beyond_tolerance_diverges(self):
        assert not values_match(100.0, 100.000002, 'float')

    def test_timestamps_compare_at_second_resolution(self):
        assert values_match('2026-07-15T13:42:17.201Z',
                            '2026-07-15T13:42:17.899+00:00', 'timestamp')
        assert not values_match('2026-07-15T13:42:17Z',
                                '2026-07-15T13:42:18Z', 'timestamp')

    def test_naive_timestamp_is_treated_as_utc(self):
        assert values_match('2026-07-15T13:42:17', '2026-07-15T13:42:17Z', 'timestamp')

    def test_none_on_one_side_diverges(self):
        assert not values_match(None, 0.0, 'float')
        assert values_match(None, None, 'float')

    def test_loose_map_mixes_numeric_and_text(self):
        assert values_match({'RANGE_MINUTES': 45, 'TZ': 'Australia/Sydney'},
                            {'RANGE_MINUTES': '45', 'TZ': 'Australia/Sydney'},
                            'loose_map')
        assert not values_match({'RANGE_MINUTES': 45}, {'RANGE_MINUTES': 30},
                                'loose_map')


class TestProposalNormalization:
    def test_executed_maps_to_order_submitted_on_both_sides(self):
        legacy = normalize_legacy_proposal(
            {'id': 7, 'storage_status': 'EXECUTED', 'display_status': 'ORDER_SUBMITTED',
             'symbol': 'AMD', 'action': 'BUY', 'quantity': 10, 'amount': None,
             'confidence': 0.7})
        center = normalize_center_proposal(
            {'id': 7, 'status': 'EXECUTED', 'symbol': 'AMD', 'action': 'BUY',
             'quantity': 10, 'amount': None, 'confidence': 0.7})
        assert legacy['display_status'] == center['display_status'] == 'ORDER_SUBMITTED'
        assert legacy['storage_status'] == center['storage_status'] == 'EXECUTED'


class TestCompareKeyed:
    FIELDS = {'quantity': 'float'}

    def test_presence_divergence_when_row_missing(self):
        divs = compare_keyed('positions', [{'key': 5437, 'quantity': 100.0}], [],
                             self.FIELDS, allow=[])
        assert divs == [FieldDivergence('positions', '5437', '<presence>',
                                        True, False, False)]

    def test_allow_pattern_marks_explained(self):
        divs = compare_keyed('positions',
                             [{'key': 5437, 'quantity': 100.0}],
                             [{'key': 5437, 'quantity': 99.0}],
                             self.FIELDS, allow=['positions:5437:quantity'])
        assert divs[0].explained is True


class TestReport:
    def _report(self, explained):
        return ParityReport(generated_at='2026-07-15T14:00:00Z', counts={'positions': 1},
                            divergences=[FieldDivergence('positions', '5437', 'quantity',
                                                         100.0, 99.0, explained)])

    def test_unexplained_divergence_exits_one(self):
        assert self._report(explained=False).exit_code() == 1

    def test_explained_divergence_exits_zero_but_is_reported(self):
        report = self._report(explained=True)
        assert report.exit_code() == 0
        assert json.loads(report.to_json())['divergences'][0]['explained'] is True


def test_compare_surfaces_covers_every_required_section():
    empty = {s: [] for s in ('account', 'cash', 'positions', 'proposals',
                             'strategies', 'risk', 'orders', 'fills')}
    report = compare_surfaces(empty, empty, allow=[])
    assert set(report.counts) == set(empty)
    assert report.exit_code() == 0
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_parity_compare.py -q`

Expected: FAIL — `scripts/parity_compare.py` does not exist.

- [ ] **Step 3: Implement the tool**

Create `scripts/parity_compare.py`:

```python
#!/usr/bin/env python3
"""[COMPAT] Parity: compare the legacy dashboard's data against /api/snapshot.

Spec §14.1: account/mode, cash + net liquidation, positions, proposals
(storage + display status), strategy state/params, risk warnings + limits,
orders, and fills must reconcile field-for-field. Floats compare within 1e-6,
timestamps at one-second resolution; everything else is exact.

Exit codes: 0 parity (explained divergences allowed), 1 unexplained
divergence, 2 collection failure. Runnable ad hoc and from the pycron
`parity_compare` one-shot.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fnmatch
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FLOAT_TOL = 1e-6


@dataclass(frozen=True)
class FieldDivergence:
    section: str
    key: str
    field: str
    legacy: Any
    center: Any
    explained: bool


@dataclass
class ParityReport:
    generated_at: str
    counts: dict[str, int]
    divergences: list[FieldDivergence]

    @property
    def unexplained(self) -> list[FieldDivergence]:
        return [d for d in self.divergences if not d.explained]

    def exit_code(self) -> int:
        return 1 if self.unexplained else 0

    def to_json(self) -> str:
        return json.dumps({
            'generated_at': self.generated_at,
            'counts': self.counts,
            'unexplained_count': len(self.unexplained),
            'divergences': [dataclasses.asdict(d) for d in self.divergences],
        }, indent=2, default=str)


class CollectionError(RuntimeError):
    """A surface could not be read — exit 2, never report false parity."""


def to_epoch_second(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        d = value
    else:
        d = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp())


def values_match(a: Any, b: Any, kind: str = 'exact') -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if kind == 'float':
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            return False
        if math.isnan(fa) or math.isnan(fb):
            return math.isnan(fa) and math.isnan(fb)
        return abs(fa - fb) <= FLOAT_TOL
    if kind == 'timestamp':
        try:
            return to_epoch_second(a) == to_epoch_second(b)
        except ValueError:
            return False
    if kind == 'bool':
        return bool(a) == bool(b)
    if kind == 'sorted_list':
        return sorted(map(str, a)) == sorted(map(str, b))
    if kind == 'loose_map':
        if not isinstance(a, dict) or not isinstance(b, dict) or set(a) != set(b):
            return False
        for k in a:
            try:
                if not values_match(float(a[k]), float(b[k]), 'float'):
                    return False
            except (TypeError, ValueError):
                if str(a[k]) != str(b[k]):
                    return False
        return True
    return a == b


SECTION_FIELDS: dict[str, dict[str, str]] = {
    'account':    {'account_id': 'exact', 'mode': 'exact', 'net_liquidation': 'float'},
    'cash':       {'amount': 'float'},
    'positions':  {'quantity': 'float', 'avg_cost': 'float',
                   'market_value': 'float', 'unrealized_pnl': 'float'},
    'proposals':  {'storage_status': 'exact', 'display_status': 'exact',
                   'symbol': 'exact', 'action': 'exact', 'quantity': 'float',
                   'amount': 'float', 'confidence': 'float'},
    'strategies': {'enabled': 'bool', 'params': 'loose_map'},
    'risk':       {'warnings': 'sorted_list', 'limits': 'loose_map'},
    'orders':     {'status': 'exact', 'action': 'exact', 'quantity': 'float',
                   'filled': 'float', 'avg_fill_price': 'float',
                   'limit_price': 'float'},
    'fills':      {'side': 'exact', 'quantity': 'float', 'price': 'float',
                   'commission': 'float', 'time': 'timestamp'},
}


def _allowed(allow: list[str], section: str, key: str, field: str) -> bool:
    probe = f'{section}:{key}:{field}'
    return any(fnmatch.fnmatch(probe, pattern) for pattern in allow)


def compare_keyed(section: str, legacy_rows: list[dict], center_rows: list[dict],
                  fields: dict[str, str], allow: list[str]) -> list[FieldDivergence]:
    lmap = {str(r['key']): r for r in legacy_rows}
    cmap = {str(r['key']): r for r in center_rows}
    out: list[FieldDivergence] = []
    for k in sorted(set(lmap) | set(cmap)):
        if k not in lmap or k not in cmap:
            out.append(FieldDivergence(section, k, '<presence>', k in lmap, k in cmap,
                                       _allowed(allow, section, k, '<presence>')))
            continue
        for field, kind in fields.items():
            a, b = lmap[k].get(field), cmap[k].get(field)
            if not values_match(a, b, kind):
                out.append(FieldDivergence(section, k, field, a, b,
                                           _allowed(allow, section, k, field)))
    return out


def compare_surfaces(legacy: dict, center: dict, allow: list[str]) -> ParityReport:
    divergences: list[FieldDivergence] = []
    counts: dict[str, int] = {}
    for section, fields in SECTION_FIELDS.items():
        lrows, crows = legacy.get(section, []), center.get(section, [])
        counts[section] = max(len(lrows), len(crows))
        divergences.extend(compare_keyed(section, lrows, crows, fields, allow))
    now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return ParityReport(generated_at=now, counts=counts, divergences=divergences)


# --- normalizers -----------------------------------------------------------

def normalize_legacy_proposal(row: dict) -> dict:
    return {'key': row.get('id'), 'storage_status': row.get('storage_status'),
            'display_status': row.get('display_status'), 'symbol': row.get('symbol'),
            'action': row.get('action'), 'quantity': row.get('quantity'),
            'amount': row.get('amount'), 'confidence': row.get('confidence')}


def normalize_center_proposal(payload: dict) -> dict:
    from trader.sdk import proposal_display_status
    return {'key': payload.get('id'), 'storage_status': payload.get('status'),
            'display_status': proposal_display_status(str(payload.get('status'))),
            'symbol': payload.get('symbol'), 'action': payload.get('action'),
            'quantity': payload.get('quantity'), 'amount': payload.get('amount'),
            'confidence': payload.get('confidence')}


def _mode_for_account(account: str | None) -> str | None:
    if not account:
        return None
    return 'paper' if account.startswith('DU') else 'live'


def collect_legacy() -> dict:
    """Read the legacy surface through its own fetchers + the SDK."""
    import web.app as webapp
    from trader.trading.order_tracker import normalize_ib_status

    status = webapp.fetch_status()
    snapshot = webapp.fetch_snapshot()
    cash = webapp.fetch_cash()
    risk = webapp.fetch_risk()
    limits = webapp.fetch_risk_limits()
    if status is None or snapshot is None:
        raise CollectionError('legacy: trader_service unreachable')

    positions = [{'key': r.get('conId') or r.get('symbol'),
                  'quantity': r.get('position'), 'avg_cost': r.get('avgCost'),
                  'market_value': r.get('marketValue'),
                  'unrealized_pnl': r.get('unrealizedPNL')}
                 for r in webapp.fetch_positions()]
    proposals = [normalize_legacy_proposal(r) for r in webapp.fetch_proposals()]
    strategies = [{'key': r.get('name'), 'enabled': r.get('enabled'),
                   'params': r.get('params') or {}}
                  for r in webapp.fetch_strategies()]
    orders = [{'key': r.get('orderId'),
               'status': normalize_ib_status(str(r.get('status'))),
               'action': r.get('action'), 'quantity': r.get('quantity'),
               'filled': r.get('filled'), 'avg_fill_price': r.get('avgFillPrice'),
               'limit_price': r.get('lmtPrice')}
              for r in webapp._records(webapp._call(lambda m: m.orders()))]
    fills = [{'key': r.get('execution_id'), 'side': r.get('side'),
              'quantity': r.get('quantity'), 'price': r.get('price'),
              'commission': r.get('commission'), 'time': r.get('time')}
             for r in webapp._records(webapp._call(lambda m: m.fills()))]
    return {
        'account': [{'key': 'account', 'account_id': status.get('account'),
                     'mode': _mode_for_account(status.get('account')),
                     'net_liquidation': snapshot.get('net_liquidation')}],
        'cash': [{'key': ccy, 'amount': row.get('cash')}
                 for ccy, row in ((cash or {}).get('currencies') or {}).items()],
        'positions': positions,
        'proposals': proposals,
        'strategies': strategies,
        'risk': [{'key': 'risk', 'warnings': (risk or {}).get('warnings') or [],
                  'limits': limits or {}}],
        'orders': orders,
        'fills': fills,
    }


def login(base_url: str, token: str) -> str:
    """POST /session and return the session cookie ('name=value')."""
    req = urllib.request.Request(
        base_url + '/session',
        data=urllib.parse.urlencode({'token': token}).encode(), method='POST')
    with urllib.request.urlopen(req, timeout=10) as resp:
        cookie = (resp.headers.get('Set-Cookie') or '').split(';', 1)[0]
    if not cookie:
        raise CollectionError('center: /session returned no session cookie')
    return cookie


def collect_center(base_url: str, token: str) -> dict:
    cookie = login(base_url, token)
    req = urllib.request.Request(base_url + '/api/snapshot',
                                 headers={'Cookie': cookie})
    with urllib.request.urlopen(req, timeout=30) as resp:
        entities = json.load(resp)['entities']
    accounts = entities.get('account') or []
    positions = entities.get('position') or []
    return {
        'account': [{'key': 'account', 'account_id': a.get('account_id'),
                     'mode': a.get('mode'),
                     'net_liquidation': a.get('net_liquidation')} for a in accounts],
        'cash': [{'key': ccy, 'amount': bal.get('cash')}
                 for a in accounts
                 for ccy, bal in (a.get('balances') or {}).items()],
        'positions': [{'key': p.get('conid'), 'quantity': p.get('quantity'),
                       'avg_cost': p.get('avg_cost'),
                       'market_value': p.get('market_value'),
                       'unrealized_pnl': p.get('unrealized_pnl')} for p in positions],
        'proposals': [normalize_center_proposal(p)
                      for p in entities.get('proposal') or []],
        'strategies': [{'key': s.get('name'), 'enabled': s.get('enabled'),
                        'params': s.get('params') or {}}
                       for s in entities.get('strategy') or []],
        'risk': [{'key': 'risk', 'warnings': r.get('warnings') or [],
                  'limits': r.get('limits') or {}}
                 for r in entities.get('risk') or []
                 if str(r.get('id', '')).startswith('projection:')],
        'orders': [{'key': o.get('client_order_id'), 'status': o.get('status'),
                    'action': o.get('action'), 'quantity': o.get('quantity'),
                    'filled': o.get('filled'),
                    'avg_fill_price': o.get('avg_fill_price'),
                    'limit_price': o.get('limit_price')}
                   for o in entities.get('order') or []],
        'fills': [{'key': f.get('execution_id'), 'side': f.get('side'),
                   'quantity': f.get('quantity'), 'price': f.get('price'),
                   'commission': f.get('commission'), 'time': f.get('time')}
                  for f in entities.get('fill') or []],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:7424')
    parser.add_argument('--token-file',
                        default=os.environ.get('DASHBOARD_TOKEN_FILE', ''))
    parser.add_argument('--allow', action='append', default=[],
                        help="explained divergence pattern 'section:key:field'")
    parser.add_argument('--report-dir',
                        default=str(Path('~/.local/share/mmr/reports').expanduser()))
    parser.add_argument('--json', action='store_true',
                        help='print the report JSON to stdout')
    args = parser.parse_args()

    try:
        token = Path(args.token_file).read_text().strip()
        legacy = collect_legacy()
        center = collect_center(args.base_url, token)
    except Exception as exc:  # noqa: BLE001 — fail loudly, never false parity
        print(f'parity collection failed: {type(exc).__name__}: {exc}',
              file=sys.stderr)
        return 2

    report = compare_surfaces(legacy, center, allow=args.allow)
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out_path = report_dir / f'parity_{stamp}.json'
    out_path.write_text(report.to_json())
    if args.json:
        print(report.to_json())
    else:
        print(f'{len(report.unexplained)} unexplained / '
              f'{len(report.divergences)} total divergences -> {out_path}')
    return report.exit_code()


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Schedule it as a pycron one-shot**

Add to the `jobs:` list in `config_defaults/pycron.yaml` (one-shot shape — never restarted; cron in the container's `TZ`, `America/New_York` by default, so `9-16` covers the US session):

```yaml
    - name: parity_compare
      description: "[COMPAT] legacy vs command-center field parity (spec 14.1); nonzero exit on unexplained divergence"
      command: python3
      arguments: /home/trader/mmr/scripts/parity_compare.py --report-dir /home/trader/.local/share/mmr/reports
      start: "*/30 9-16 * * mon-fri"
      start_on_pycron_start: False
      restart_if_found: False
      restart_if_finished: False
      delay: 0
```

The `[G0]` scheduler receipt for this job surfaces its exit code in authenticated `/api/health`, so a divergence during the live session is visible without tailing logs.

- [ ] **Step 5: Run the parity tests and an ad-hoc smoke run**

Run: `uv run --frozen pytest tests/test_parity_compare.py -q`

Expected: PASS.

Run (paper stack up): `docker compose exec dashboard python3 scripts/parity_compare.py --json; echo "exit=$?"`

Expected: `exit=0` and a report JSON whose `counts` include all eight sections; on a freshly started stack every section may legitimately count 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/parity_compare.py tests/test_parity_compare.py config_defaults/pycron.yaml
git commit -m "feat(compat): field-level parity comparison across both surfaces"
```

### Task 4: Eight-hour paper soak runner with failure injection

**Files:**
- Create: `scripts/run_paper_soak.py`
- Create: `tests/test_soak_thresholds.py`

**Interfaces:**
- Consumes: `scripts/soak_harness.py` CLI and metrics JSON from `[M1-R]`; `login` from `scripts/parity_compare.py`; authenticated `GET /api/health`; `[G0]` Compose service names `trader`, `strategy`, `dashboard`, `ib-gateway`.
- Produces: `SoakThresholds`, `Sample`, `Scenario`, `evaluate_soak(samples, harness_metrics, scenario_outcomes, thresholds) -> SoakReport`, and a machine-readable `soak_report_<ts>.json`. Exit code 0 only when every threshold and every scenario passes.

- [ ] **Step 1: Write failing threshold-evaluation tests**

Create `tests/test_soak_thresholds.py`:

```python
"""[COMPAT] Task 4: recorded soak thresholds (spec §13.3) evaluated fail-closed."""
from scripts.run_paper_soak import Sample, SoakThresholds, evaluate_soak

T = SoakThresholds()

GOOD_HARNESS = {'p95_critical_ms': 180.0, 'unhandled_errors': 0,
                'unresolved_commands': 0, 'max_replay_ring_events': 6000,
                'max_client_fifo_depth': 400, 'max_terminal_rows': 480}


def _samples(rss_start=1.0e9, rss_end=1.1e9, cpu=0.4, minutes=480):
    # one sample per minute; warm-up is the first 60
    step = (rss_end - rss_start) / max(minutes - T.warmup_minutes, 1)
    out = []
    for m in range(minutes):
        rss = rss_start if m < T.warmup_minutes else \
            rss_start + step * (m - T.warmup_minutes)
        out.append(Sample(minute=m, rss_bytes=rss, cpu_cores=cpu))
    return out


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


def test_healthy_run_passes():
    report = evaluate_soak(_samples(), GOOD_HARNESS, {'trader_outage': 'coherent'}, T)
    assert report.passed


def test_rss_growth_at_exactly_twenty_percent_passes():
    report = evaluate_soak(_samples(rss_end=1.2e9), GOOD_HARNESS, {}, T)
    assert _check(report, 'rss_growth').passed


def test_rss_growth_beyond_twenty_percent_fails():
    report = evaluate_soak(_samples(rss_end=1.21e9), GOOD_HARNESS, {}, T)
    assert not _check(report, 'rss_growth').passed and not report.passed


def test_warmup_hour_is_excluded_from_rss_baseline():
    # A big jump entirely inside the warm-up hour must not count as growth.
    samples = _samples()
    for s in samples[:T.warmup_minutes]:
        object.__setattr__(s, 'rss_bytes', 0.5e9)
    report = evaluate_soak(samples, GOOD_HARNESS, {}, T)
    assert _check(report, 'rss_growth').passed


def test_cpu_average_of_one_core_or_more_fails():
    report = evaluate_soak(_samples(cpu=1.0), GOOD_HARNESS, {}, T)
    assert not _check(report, 'cpu_avg_cores').passed


def test_p95_over_500ms_fails():
    harness = {**GOOD_HARNESS, 'p95_critical_ms': 500.1}
    assert not evaluate_soak(_samples(), harness, {}, T).passed


def test_any_unresolved_command_fails():
    harness = {**GOOD_HARNESS, 'unresolved_commands': 1}
    assert not evaluate_soak(_samples(), harness, {}, T).passed


def test_bound_violations_fail():
    harness = {**GOOD_HARNESS, 'max_client_fifo_depth': 1001}
    assert not evaluate_soak(_samples(), harness, {}, T).passed


def test_missing_samples_fail_closed():
    report = evaluate_soak([], GOOD_HARNESS, {}, T)
    assert not report.passed
    assert not _check(report, 'samples_present').passed


def test_failed_scenario_fails_run():
    report = evaluate_soak(_samples(), GOOD_HARNESS, {'trader_outage': 'failed'}, T)
    assert not report.passed
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --frozen pytest tests/test_soak_thresholds.py -q`

Expected: FAIL — `scripts/run_paper_soak.py` does not exist.

- [ ] **Step 3: Implement the soak runner**

Create `scripts/run_paper_soak.py`:

```python
#!/usr/bin/env python3
"""[COMPAT] Eight-hour representative paper soak with failure injection.

Wraps scripts/soak_harness.py ([M1-R]) at the spec §13.3 load profile
(100 instruments, 4 Hz quotes, 20 domain events/s, 3 tabs, periodic paper
test commands), samples dashboard RSS/CPU via `docker stats`, injects
dependency failures, and evaluates the recorded thresholds. Every scenario
must end coherent or explicitly degraded — never silently inconsistent.

Exit 0 only when every threshold and scenario passes.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parity_compare import login  # noqa: E402


@dataclass(frozen=True)
class SoakThresholds:
    warmup_minutes: int = 60
    rss_growth_max: float = 0.20
    cpu_avg_cores_max: float = 1.0          # strictly below one core
    p95_critical_ms_max: float = 500.0
    max_unhandled_errors: int = 0
    max_unresolved_commands: int = 0
    replay_ring_max: int = 10_000
    client_fifo_max: int = 1_000
    terminal_rows_max: int = 500


@dataclass(frozen=True)
class Sample:
    minute: int
    rss_bytes: float
    cpu_cores: float


@dataclass(frozen=True)
class Check:
    name: str
    limit: float
    observed: float | None
    passed: bool


@dataclass
class SoakReport:
    started_at: str
    checks: list[Check]
    scenarios: dict[str, str]
    passed: bool

    def to_json(self) -> str:
        return json.dumps({
            'started_at': self.started_at,
            'passed': self.passed,
            'checks': [dataclasses.asdict(c) for c in self.checks],
            'scenarios': self.scenarios,
        }, indent=2)


def evaluate_soak(samples: list[Sample], harness: dict,
                  scenario_outcomes: dict[str, str],
                  t: SoakThresholds) -> SoakReport:
    checks: list[Check] = []
    post = [s for s in samples if s.minute >= t.warmup_minutes]
    checks.append(Check('samples_present', 1, len(post), len(post) >= 2))
    if len(post) >= 2:
        baseline, final = post[0].rss_bytes, post[-1].rss_bytes
        growth = (final - baseline) / baseline if baseline > 0 else float('inf')
        checks.append(Check('rss_growth', t.rss_growth_max, round(growth, 4),
                            growth <= t.rss_growth_max))
        cpu = sum(s.cpu_cores for s in post) / len(post)
        checks.append(Check('cpu_avg_cores', t.cpu_avg_cores_max, round(cpu, 3),
                            cpu < t.cpu_avg_cores_max))
    else:
        checks.append(Check('rss_growth', t.rss_growth_max, None, False))
        checks.append(Check('cpu_avg_cores', t.cpu_avg_cores_max, None, False))

    def metric(name: str, limit: float, strict_leq=True) -> None:
        observed = harness.get(name)
        ok = observed is not None and float(observed) <= limit
        checks.append(Check(name.replace('max_', '') if name.startswith('max_')
                            else name, limit, observed, ok))

    metric('p95_critical_ms', t.p95_critical_ms_max)
    metric('unhandled_errors', t.max_unhandled_errors)
    metric('unresolved_commands', t.max_unresolved_commands)
    metric('max_replay_ring_events', t.replay_ring_max)
    metric('max_client_fifo_depth', t.client_fifo_max)
    metric('max_terminal_rows', t.terminal_rows_max)

    scenarios_ok = all(v in ('coherent', 'explicit-degraded')
                       for v in scenario_outcomes.values())
    checks.append(Check('scenarios_coherent', 1,
                        int(scenarios_ok), scenarios_ok))
    return SoakReport(
        started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        checks=checks, scenarios=scenario_outcomes,
        passed=all(c.passed for c in checks))


# --- sampling ---------------------------------------------------------------

_MEM_UNITS = {'B': 1, 'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3}


def parse_mem(mem_usage: str) -> float:
    """'512.3MiB / 24GiB' -> bytes of the first term."""
    m = re.match(r'([\d.]+)(B|KiB|MiB|GiB)', mem_usage.strip())
    if not m:
        raise ValueError(f'unparsable MemUsage: {mem_usage!r}')
    return float(m.group(1)) * _MEM_UNITS[m.group(2)]


def sample_container(service: str, minute: int) -> Sample:
    cid = subprocess.run(['docker', 'compose', 'ps', '-q', service],
                         capture_output=True, text=True, check=True).stdout.strip()
    out = subprocess.run(['docker', 'stats', '--no-stream', '--format',
                          '{{json .}}', cid],
                         capture_output=True, text=True, check=True)
    row = json.loads(out.stdout)
    return Sample(minute=minute, rss_bytes=parse_mem(row['MemUsage']),
                  cpu_cores=float(row['CPUPerc'].rstrip('%')) / 100.0)


# --- failure injection -------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    name: str
    offset_minutes: int
    stop_cmd: tuple[str, ...]
    start_cmd: tuple[str, ...]
    outage_seconds: int
    dependency: str            # key in /api/health that must go degraded


SCENARIOS: tuple[Scenario, ...] = (
    Scenario('trader_outage', 120, ('docker', 'compose', 'stop', 'trader'),
             ('docker', 'compose', 'start', 'trader'), 180, 'trader_service'),
    Scenario('strategy_outage', 240, ('docker', 'compose', 'stop', 'strategy'),
             ('docker', 'compose', 'start', 'strategy'), 180, 'strategy_service'),
    Scenario('broker_disconnect', 300, ('docker', 'compose', 'stop', 'ib-gateway'),
             ('docker', 'compose', 'start', 'ib-gateway'), 120, 'ib'),
    Scenario('dashboard_restart', 360,
             ('docker', 'compose', 'restart', 'dashboard'),
             ('true',), 0, 'browser-stream'),
)


def _health(base_url: str, cookie: str) -> dict:
    req = urllib.request.Request(base_url + '/api/health',
                                 headers={'Cookie': cookie})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _wait_for(predicate, timeout_s: int, interval_s: int = 5) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 — health may be briefly unreachable
            pass
        time.sleep(interval_s)
    return False


def run_scenario(s: Scenario, base_url: str, token: str,
                 parity_cmd: list[str] | None) -> str:
    subprocess.run(s.stop_cmd, check=True)
    cookie = login(base_url, token)
    degraded = _wait_for(
        lambda: _health(base_url, cookie)[s.dependency]['state']
        in ('degraded', 'disconnected'), timeout_s=30)
    if s.outage_seconds:
        time.sleep(s.outage_seconds)
    subprocess.run(s.start_cmd, check=True)
    cookie = login(base_url, token)   # dashboard_restart invalidates the session
    recovered = _wait_for(
        lambda: all(dep['state'] == 'live'
                    for dep in _health(base_url, cookie).values()
                    if isinstance(dep, dict) and 'state' in dep),
        timeout_s=300)
    if s.name != 'dashboard_restart' and not degraded:
        return 'failed'          # outage was never surfaced — silently wrong
    if not recovered:
        # Explicit degraded is acceptable; silence is not.
        state = _health(base_url, cookie).get(s.dependency, {}).get('state')
        return 'explicit-degraded' if state in ('degraded', 'disconnected') else 'failed'
    if parity_cmd is not None:
        if subprocess.run(parity_cmd).returncode != 0:
            return 'failed'      # recovered but incoherent
    return 'coherent'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hours', type=float, default=8.0)
    parser.add_argument('--base-url', default='http://127.0.0.1:7424')
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--skip-scenarios', action='store_true')
    parser.add_argument('--parity-cmd', default='',
                        help='post-recovery coherence command, e.g. '
                             '"python3 scripts/parity_compare.py"')
    parser.add_argument('--out', default='')
    args = parser.parse_args()

    token = Path(args.token_file).read_text().strip()
    minutes = int(args.hours * 60)
    metrics_path = Path('/tmp/soak_harness_metrics.json')
    harness = subprocess.Popen([
        sys.executable, str(Path(__file__).parent / 'soak_harness.py'),
        '--duration-minutes', str(minutes), '--instruments', '100',
        '--quote-hz', '4', '--domain-eps', '20', '--tabs', '3',
        '--commands-per-hour', '12', '--metrics-out', str(metrics_path)])

    samples: list[Sample] = []
    outcomes: dict[str, str] = {}
    pending = [] if args.skip_scenarios else \
        sorted((s for s in SCENARIOS if s.offset_minutes < minutes),
               key=lambda s: s.offset_minutes)
    parity_cmd = args.parity_cmd.split() if args.parity_cmd else None

    for minute in range(minutes):
        samples.append(sample_container('dashboard', minute))
        while pending and pending[0].offset_minutes <= minute:
            scenario = pending.pop(0)
            outcomes[scenario.name] = run_scenario(
                scenario, args.base_url, token, parity_cmd)
        if harness.poll() is not None:
            break
        time.sleep(60)

    harness.wait(timeout=600)
    harness_metrics = json.loads(metrics_path.read_text()) \
        if metrics_path.exists() else {}
    report = evaluate_soak(samples, harness_metrics, outcomes, SoakThresholds())

    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = Path(args.out) if args.out else \
        Path('~/.local/share/mmr/reports').expanduser() / f'soak_report_{stamp}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.to_json())
    print(f'{"PASS" if report.passed else "FAIL"} -> {out}')
    return 0 if report.passed else 1


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Run the threshold tests and a short rehearsal**

Run: `uv run --frozen pytest tests/test_soak_thresholds.py -q`

Expected: PASS.

Run (paper stack up, six-minute rehearsal without failure injection):

```bash
python3 scripts/run_paper_soak.py --hours 0.1 --skip-scenarios \
  --token-file "$DASHBOARD_TOKEN_FILE" --out /tmp/soak_rehearsal.json
echo "exit=$?"; python3 -c "import json;print(json.load(open('/tmp/soak_rehearsal.json'))['checks'])"
```

Expected: `exit=1` is acceptable here only via `samples_present`/`rss_growth` (a six-minute run has fewer than two post-warm-up samples — the evaluator fails closed by design); every harness-metric check present in the output must be `passed: true`. The full-length run in Task 5 is the real gate.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_paper_soak.py tests/test_soak_thresholds.py
git commit -m "ops(compat): paper soak runner with failure injection and thresholds"
```

### Task 5: Recorded retirement checklist, eight-hour soak, live read-only session, and rollback drill

**Files:**
- Create: `docs/superpowers/rollout/command-center-retirement-checklist.md`

**Interfaces:**
- Produces: the committed §14.1 checklist record — the sole artifact that gates Task 6 and live enablement. Procedures below are recorded into it as they execute.

- [ ] **Step 1: Create the checklist record**

Create `docs/superpowers/rollout/command-center-retirement-checklist.md`:

```markdown
# Command Center Retirement Checklist — [COMPAT] gate record

Spec: `docs/superpowers/specs/2026-07-15-realtime-trading-command-center-design.md`
§14.1, §13.3, §10. The single operator records every item; any unchecked item
or triggered no-go condition blocks live enablement and legacy retirement.

Operator: ____________________  Record opened: ____________________

## A. Eight-hour paper soak (§13.3)

- [ ] Soak report path: ____________________ (`"passed": true`)
- [ ] RSS growth ≤ 20% after the warm-up hour (observed: ______)
- [ ] Average dashboard CPU < 1 core (observed: ______)
- [ ] p95 critical-event latency ≤ 500 ms (observed: ______)
- [ ] Zero unhandled errors and zero unresolved test commands
- [ ] Replay ring / client FIFO / terminal-row bounds respected
- [ ] Scenarios trader_outage, strategy_outage, broker_disconnect,
      dashboard_restart each ended `coherent` or `explicit-degraded`

## B. Live read-only market session (both surfaces)

- [ ] Session date + market: ____________________
- [ ] `DASHBOARD_COMMANDS_ENABLED=false` and
      `DASHBOARD_LIVE_COMMANDS_ENABLED=false` for the entire session
- [ ] Every scheduled `parity_compare` run exited 0 (report files listed below)
- [ ] Account and mode reconcile: ____
- [ ] Cash and net liquidation reconcile: ____
- [ ] Positions reconcile: ____
- [ ] Proposals reconcile (storage + display status): ____
- [ ] Strategy state and parameters reconcile: ____
- [ ] Risk warnings and limits reconcile: ____
- [ ] Orders reconcile: ____
- [ ] Fills reconcile: ____

Parity report files: ____________________

## C. Capability disposition (§8.5 [COMPAT] items)

| Capability | Disposition | Owning surface | Verified |
|---|---|---|---|
| Proposal reasoning/rationale detail | migrated | `/cc` proposal drawer | [ ] |
| Sanitized Markdown popups | migrated | `/cc` | [ ] |
| Approve / reject | migrated | `/cc` commands | [ ] |
| Strategy enable/disable | migrated | `/cc` commands | [ ] |
| Schema-driven parameter editing | migrated | `/cc` commands | [ ] |
| Strategy discovery + deploy-from-disk | retained via coordinator | `/manage` (Task 6) | [ ] |
| Watchlist CRUD | retained | `/manage` (Task 6) | [ ] |
| CSV import | retained | `/manage` (Task 6) | [ ] |

## D. Rollback drill (recorded observations)

- [ ] Live-command flag disabled; only the `dashboard` container was
      recreated (container-id diff for trader/strategy/data was empty)
- [ ] Dashboard dropped to read-only: `POST /api/commands/...` returned 403
      with a `COMMANDS_DISABLED` / `LIVE_COMMANDS_DISABLED` code
- [ ] Trading services continued: `mmr --json status` healthy, container
      restart counts unchanged
- [ ] Legacy read-only view exercised at `/` (renders; no active mutation
      surface — CLI remains the fallback command path)

## E. Automatic no-go triggers (all must be clean)

- [ ] No duplicate command (query below returned 0 rows)
- [ ] No unresolved command older than 15 minutes (query below returned 0 rows)
- [ ] No source-coherence failure during soak or live session
- [ ] No audit write failure in trader logs
- [ ] No bypass-capability exposure ([G0] negative security suite green)
- [ ] No violated soak threshold

Ledger queries (run inside the trader container against the trader DB):

```sql
SELECT order_ref, COUNT(DISTINCT command_id) AS n
  FROM command_ledger GROUP BY order_ref HAVING n > 1;

SELECT command_id, state, created_at FROM command_ledger
 WHERE state NOT IN ('RESOLVED', 'REJECTED')
   AND created_at < now() - INTERVAL 15 MINUTE;
```

## F. MMR_WEB_TOKEN credential rotation (§10 — before live enablement)

- [ ] New canonical token generated (never the recycled `MMR_WEB_TOKEN` value)
- [ ] `MMR_WEB_TOKEN` removed from `.env`
- [ ] Old credential verified rejected (URL and header forms)

## G. Live enablement gate (single operator go/no-go)

- [ ] `DASHBOARD_LIVE_ACCOUNT_ID` = ____________ (exact IB account, no wildcard)
- [ ] `DASHBOARD_MAX_ORDER_NOTIONAL` = ____________ (mandatory; no fallback)
- [ ] Existing broker/risk limits confirmed active
- [ ] Go / no-go decision: ____________  Date: ____________

## H. Documentation and operator surface (verified at Task 6)

- [ ] `docs/OPERATIONAL_STATE.md` updated
- [ ] Runbooks and browser bookmarks point at `/cc` (and `/manage`)
- [ ] Health checks reference `/healthz`, `/readyz`, `/api/health` only

## I. Sign-off

Legacy dashboard retirement approved by: ____________  Date: ____________
```

- [ ] **Step 2: Commit the empty record**

```bash
git add docs/superpowers/rollout/command-center-retirement-checklist.md
git commit -m "docs(compat): add retirement checklist gate record"
```

- [ ] **Step 3: Run the eight-hour paper soak (release gate)**

With the paper Compose stack healthy and `[M1-C]` paper commands enabled on the command center only:

```bash
docker compose ps --format '{{.Name}} {{.Status}}'      # all services Up (healthy)
nohup python3 scripts/run_paper_soak.py --hours 8 \
  --token-file "$DASHBOARD_TOKEN_FILE" \
  --parity-cmd "docker compose exec -T dashboard python3 scripts/parity_compare.py" \
  > /tmp/soak_run.log 2>&1 &
```

After completion:

```bash
tail -1 /tmp/soak_run.log
python3 - <<'PY'
import glob, json
report = json.load(open(sorted(glob.glob(
    __import__('os').path.expanduser('~/.local/share/mmr/reports/soak_report_*.json')))[-1]))
assert report['passed'], report
print('soak PASS', {c['name']: c['observed'] for c in report['checks']})
print('scenarios', report['scenarios'])
PY
```

Expected: `PASS -> .../soak_report_<ts>.json`; the assertion prints `soak PASS` and all four scenarios are `coherent` or `explicit-degraded`. Record section A of the checklist. Any failed check or `failed` scenario is an automatic no-go: fix, then rerun the full eight hours.

- [ ] **Step 4: Run one complete live read-only market session with both surfaces**

Before the session opens:

```bash
grep -E '^DASHBOARD_(COMMANDS|LIVE_COMMANDS)_ENABLED=false$' .env | wc -l   # expect 2
docker compose up -d dashboard
curl -fsS http://127.0.0.1:7424/readyz                                      # expect {"ready": true}
```

Keep both `/` and `/cc` open through the full session (US: 09:30–16:00 ET). The `parity_compare` pycron job runs every 30 minutes; run one ad-hoc check at the open and one after the close:

```bash
docker compose exec dashboard python3 scripts/parity_compare.py --json; echo "exit=$?"
```

Expected: every run exits 0. If a run exits 1, the divergence is investigated; either it is a bug (fix, restart the session gate another day) or a documented explained divergence added via `--allow` with its justification written into checklist section B. Record section B with the report file list.

- [ ] **Step 5: Execute and record the rollback drill (paper stack)**

```bash
docker compose ps -q trader strategy data > /tmp/compat_ids_before
sed -i.bak 's/^DASHBOARD_LIVE_COMMANDS_ENABLED=.*/DASHBOARD_LIVE_COMMANDS_ENABLED=false/' .env
docker compose up -d dashboard
docker compose ps -q trader strategy data | diff - /tmp/compat_ids_before && echo "services untouched"
```

Expected: `services untouched` (only `dashboard` recreated). Then verify read-only and continued trading:

```bash
TOKEN=$(cat "$DASHBOARD_TOKEN_FILE")
curl -s -c /tmp/cc.jar -o /dev/null -X POST http://127.0.0.1:7424/session -d "token=$TOKEN"
curl -s -b /tmp/cc.jar -o /tmp/resp.json -w '%{http_code}\n' \
  -X POST http://127.0.0.1:7424/api/commands/proposals/1/approve \
  -H 'Content-Type: application/json' -d '{"command_id":"drill-1","expected_version":1}'
cat /tmp/resp.json
mmr --json status
```

Expected: `403` with a stable `LIVE_COMMANDS_DISABLED` (or `COMMANDS_DISABLED` once the paper flag is also lowered) error code; `mmr --json status` shows `"connected": true` and `"ib_upstream_connected": true`. Now the restore-legacy-read-only exercise:

```bash
sed -i.bak 's/^DASHBOARD_COMMANDS_ENABLED=.*/DASHBOARD_COMMANDS_ENABLED=false/' .env
docker compose up -d dashboard
curl -s -b /tmp/cc.jar -o /dev/null -w '%{http_code}\n' http://127.0.0.1:7424/   # expect 200
```

Expected: the legacy view renders read-only (200); the command center stays read-only; no mutation surface is exposed on either page — the CLI remains the fallback command path. Record section D, then run both section-E ledger queries and record section E.

- [ ] **Step 6: Rotate the legacy web credential (§10, before live enablement)**

The old `MMR_WEB_TOKEN` value may exist in browser URL history or logs (`?token=` era), so the canonical credential must be a fresh secret:

```bash
umask 077 && mkdir -p ~/.local/share/mmr/secrets
openssl rand -hex 32 > ~/.local/share/mmr/secrets/dashboard_token
OLD_TOKEN=$(grep '^MMR_WEB_TOKEN=' .env | cut -d= -f2)
grep -v '^MMR_WEB_TOKEN=' .env > .env.new && mv .env.new .env
docker compose up -d dashboard
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:7424/?token=$OLD_TOKEN"      # expect 401
curl -s -o /dev/null -w '%{http_code}\n' -H "X-MMR-Token: $OLD_TOKEN" http://127.0.0.1:7424/  # expect 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:7424/session \
  -d "token=$(cat ~/.local/share/mmr/secrets/dashboard_token)"                          # expect 2xx/303
```

Expected: both old-credential forms return 401; the new token opens a session. Record section F.

- [ ] **Step 7: Configure and record the live enablement gate**

```bash
grep -E '^DASHBOARD_LIVE_ACCOUNT_ID=U[0-9]+$' .env
grep -E '^DASHBOARD_MAX_ORDER_NOTIONAL=[0-9]+(\.[0-9]+)?$' .env
```

Expected: both greps match exactly one line each — an exact IB live account (not a mode label or wildcard) and a numeric maximum order notional. If either is absent, live commands stay off; there is no permissive fallback. The single operator records the exact values and the go/no-go decision in section G. Only after go: `sed -i.bak 's/^DASHBOARD_LIVE_COMMANDS_ENABLED=.*/DASHBOARD_LIVE_COMMANDS_ENABLED=true/' .env && docker compose up -d dashboard`.

- [ ] **Step 8: Commit the filled record**

```bash
git add docs/superpowers/rollout/command-center-retirement-checklist.md
git commit -m "docs(compat): record soak, live session, rotation, and rollback drill results"
```

### Task 6: Retire the legacy dashboard and the MMR_WEB_TOKEN alias

**Files:**
- Modify: `web/app.py` (remove legacy page + overlapping mutation routes; add `/` redirect and `/manage`)
- Delete: `web/templates/dashboard.html`
- Create: `web/templates/manage.html`
- Modify: `web/command_center/session.py` (remove the deprecated `MMR_WEB_TOKEN` alias path)
- Modify: `tests/test_web_dashboard.py` (trim to `/manage` coverage)
- Delete: `scripts/parity_compare.py`, `tests/test_parity_compare.py` (subject surface no longer exists; reports and git history remain)
- Modify: `config_defaults/pycron.yaml` (remove the `parity_compare` job)
- Modify: `docs/OPERATIONAL_STATE.md`
- Modify: `docs/superpowers/rollout/command-center-retirement-checklist.md` (sections H, I)

**Interfaces:**
- Produces: `GET /` → permanent redirect to `/cc`; session-gated `GET /manage` hosting the two retained `[COMPAT]` capabilities (watchlist CRUD/CSV and coordinator-backed deploy) until `[M2]` replaces them.
- Removes: legacy dashboard rendering, overlapping legacy mutation routes, legacy read fetchers used only by them, the `MMR_WEB_TOKEN` alias, and the parity job.

- [ ] **Step 1: Verify the gate before touching code**

```bash
grep -c '\[ \]' docs/superpowers/rollout/command-center-retirement-checklist.md
grep -n 'retirement approved by' docs/superpowers/rollout/command-center-retirement-checklist.md
```

Expected: `0` unchecked boxes and a filled sign-off line (section I). If either fails, STOP — retirement is blocked by spec §14.1.

- [ ] **Step 2: Write failing retirement tests**

In `tests/test_web_dashboard.py`, remove the legacy rendering/tab/tooltip test classes and replace them with:

```python
class TestRetirement:
    def test_root_redirects_permanently_to_command_center(self, client):
        r = client.get('/', follow_redirects=False)
        assert r.status_code == 308
        assert r.headers['location'] == '/cc'

    @pytest.mark.parametrize('path', [
        '/proposals/7/approve', '/proposals/7/reject',
        '/strategies/x/enable', '/strategies/x/disable', '/strategies/x/params',
    ])
    def test_overlapping_legacy_routes_are_removed(self, client, path):
        assert client.post(path, data={'csrf_token': _csrf()}).status_code == 404

    def test_manage_page_retains_watchlists_and_deploy(self, client):
        html = client.get('/manage').text
        assert 'Watchlists' in html
        assert '/watchlists/create' in html
        assert '/strategies/deploy' in html
        assert 'csrf_token' in html

    def test_manage_requires_session(self, stub):
        anon = TestClient(webapp.app)
        assert anon.get('/manage', follow_redirects=False).status_code == 401

    def test_web_token_alias_is_gone(self, monkeypatch):
        monkeypatch.setenv('MMR_WEB_TOKEN', 'zombie-credential')
        import importlib
        import web.command_center.session as session_mod
        importlib.reload(session_mod)
        assert 'MMR_WEB_TOKEN' not in session_mod.__dict__.values().__repr__()
        assert not hasattr(session_mod, 'legacy_token_alias')
```

Run: `uv run --frozen pytest tests/test_web_dashboard.py -q`

Expected: FAIL — `/` still renders the legacy template and the routes still exist.

- [ ] **Step 3: Remove the legacy surface and add `/manage`**

In `web/app.py`:

- Delete `dashboard()`, `approve()`, `reject()`, `enable_strategy()`, `disable_strategy()`, `update_strategy_params()`, and the fetchers they alone used (`fetch_cash`, `fetch_snapshot`, `fetch_status`, `fetch_risk`, `fetch_risk_limits`, `fetch_positions`, `fetch_strategies`, `fetch_proposals`, `_render_md`, `_preview`, `_neutralize_bad_hrefs`, and the markdown/nh3 setup). Keep `_get_accessor`, `fetch_watchlists`, `_split_symbols`, `_resolve_symbols`, `fetch_available_strategies`, `_records`, `_call`, all `/watchlists/*` routes, and `/strategies/deploy`.
- Add the redirect and the retained management page:

```python
@app.get('/')
def root_redirect():
    """Legacy dashboard retired ([COMPAT] Task 6); the command center owns /cc."""
    return RedirectResponse(url='/cc', status_code=308)


@app.get('/manage')
def manage(request: Request, flash: str = '',
           session: DashboardSession = Depends(require_session)):
    """Retained [COMPAT] surface: watchlist CRUD/CSV + deploy-from-disk.

    Replaced by the [M2] watchlist redesign; until then this is the explicit
    retained owner recorded in the retirement checklist."""
    watchlists, available, errors = [], [], {}
    try:
        watchlists = fetch_watchlists()
    except Exception as exc:  # noqa: BLE001
        errors['watchlists'] = f'{type(exc).__name__}: {exc}'
    try:
        available = fetch_available_strategies()
    except Exception as exc:  # noqa: BLE001
        errors['available'] = f'{type(exc).__name__}: {exc}'
    return _TEMPLATES.TemplateResponse(request, 'manage.html', {
        'watchlists': watchlists,
        'available_strategies': available,
        'errors': errors,
        'flash': flash,
        'csrf_token': session.csrf_token,
    })
```

Point `_flash()` at the retained page: `return RedirectResponse(url=f'/manage?flash={quote(msg)}', status_code=303)`.

Delete `web/templates/dashboard.html` and create `web/templates/manage.html` by extracting the watchlist and deploy sections from it verbatim (tables, create/add/upload/remove/delete forms, per-strategy deploy forms with the hidden `command_id` field and its `crypto.randomUUID()` initializer, and the shared form/table CSS), wrapped in this shell:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>MMR Manage — watchlists &amp; deploy</title>
  <!-- CSS: copy the :root palette, table, form.inline, .param-form, .chip,
       and .flash rules from the retired dashboard.html unchanged -->
</head>
<body>
  <header><h1>Manage</h1>
    <nav><a href="/cc">← command center</a></nav>
    {% if flash %}<div class="flash">{{ flash }}</div>{% endif %}
    {% for key, err in errors.items() %}<div class="flash">{{ key }}: {{ err }}</div>{% endfor %}
  </header>
  <section id="watchlists"><h2>Watchlists</h2>
    <!-- watchlist table + create/add/upload/remove/delete forms,
         each with: <input type="hidden" name="csrf_token" value="{{ csrf_token }}"> -->
  </section>
  <section id="deploy"><h2>Strategies on disk</h2>
    <!-- available_strategies table + per-strategy deploy form,
         each with csrf_token + <input type="hidden" name="command_id"
         class="deploy-cmd-id" value=""> -->
  </section>
  <script>
    document.querySelectorAll('.deploy-cmd-id').forEach(
      el => { el.value = 'deploy-' + crypto.randomUUID(); });
  </script>
</body>
</html>
```

In `web/command_center/session.py`, delete the deprecated-alias block that reads `MMR_WEB_TOKEN` when no canonical token is set (including its startup deprecation warning and the fail-on-both check — with the alias gone, only `DASHBOARD_TOKEN_FILE`/`DASHBOARD_TOKEN` remain and setting `MMR_WEB_TOKEN` has no effect).

Delete `scripts/parity_compare.py` and `tests/test_parity_compare.py`, remove the `parity_compare` job block from `config_defaults/pycron.yaml`, and remove the `--parity-cmd` default usage note from any runbook text that referenced it (the soak runner keeps the flag; it is simply unused post-retirement).

- [ ] **Step 4: Run the full suite and grep for stragglers**

Run: `uv run --frozen pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py`

Expected: PASS.

Run: `grep -rn 'MMR_WEB_TOKEN' --include='*.py' --include='*.yaml' --include='*.sh' web/ trader/ config_defaults/ scripts/ docker-compose.yml`

Expected: no matches (spec and plan documents under `docs/` are the only remaining mentions, as history).

Run: `grep -rn "dashboard.html" web/ tests/`

Expected: no matches.

- [ ] **Step 5: Update the operational record**

In `docs/OPERATIONAL_STATE.md`, add under Infrastructure:

```markdown
- **Web surface:** the command center at `http://127.0.0.1:7424/cc` is the only
  dashboard (legacy `/` retired — permanent redirect). Watchlist CRUD/CSV and
  deploy-from-disk live at `/manage` (session-authenticated; deploy goes through
  the trader command coordinator) until the [M2] redesign. Auth: `POST /session`
  with the token from `DASHBOARD_TOKEN_FILE`; `MMR_WEB_TOKEN` is removed — the
  old credential was rotated before live enablement (see
  docs/superpowers/rollout/command-center-retirement-checklist.md). Health:
  `/healthz`, `/readyz`, authenticated `/api/health`. Update the "Last updated"
  line and date this entry.
```

Check checklist section H boxes (docs updated, runbooks/bookmarks pointed at `/cc` and `/manage`, health checks reference the new endpoints only) and fill section I sign-off.

- [ ] **Step 6: Commit**

```bash
git add web/app.py web/templates/manage.html web/command_center/session.py tests/test_web_dashboard.py config_defaults/pycron.yaml docs/OPERATIONAL_STATE.md docs/superpowers/rollout/command-center-retirement-checklist.md
git rm web/templates/dashboard.html scripts/parity_compare.py tests/test_parity_compare.py
git commit -m "feat(compat): retire legacy dashboard and MMR_WEB_TOKEN alias"
```
