# G0 Runtime and RPC Security Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a reproducible test runtime, one supervisor per process, and a typed authenticated production RPC boundary before any command-center capability is trusted.

**Architecture:** Keep the existing Python-object RPC only for explicitly enabled offline/test use. Add dedicated query, command, and feed ZeroMQ sockets that accept canonical JSON envelopes signed with a service credential, then split the all-in-one container into independently supervised Compose services while pycron owns scheduled one-shot jobs only.

**Tech Stack:** CPython 3.12.13, uv, GitHub Actions, JSON, Pydantic, HMAC-SHA256, ZeroMQ ROUTER/DEALER, Docker Compose, pycron, pytest.

## Global Constraints

- Part of the command-center suite indexed by `2026-07-15-command-center-plan-index.md`; the source specification is `docs/superpowers/specs/2026-07-15-realtime-trading-command-center-design.md` at or after commit `740a242`. This is the `[G0]` runtime/security foundation.
- `.python-version` and `Dockerfile` both pin `3.12.13`; CI rejects a different patch version.
- Typed sockets use ports `42101` (query), `42102` (command), and `42103` (private long-poll feed).
- Signatures cover method, request ID, UTC timestamp, replay nonce, and canonical body. Clock skew is at most 30 seconds and replay nonces live for 60 seconds.
- The service HMAC secret comes from `MMR_SERVICE_HMAC_KEY_FILE`; raw values are never logged or embedded in images.
- The production profile never publishes raw ports `42001`, `42002`, `42003`, `42005`, or `42006` to host interfaces.
- Only typed query and command ports publish to host `127.0.0.1`; the feed stays on the private Compose network.
- Long-lived `data`, `trader`, `strategy`, and `dashboard` processes are Compose services. The scheduler service runs pycron one-shots only.
- Every container runs as the existing unprivileged `trader` user, uses a read-only root filesystem and ephemeral `/tmp`, and receives only its required writable volume.

---

### Task 1: Pin the interpreter and canonical CI jobs

**Files:**
- Create: `.python-version`
- Create: `.github/workflows/ci.yml`
- Create: `tests/test_runtime_version.py`
- Modify: `Dockerfile:1`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `README.md:520-540`

**Interfaces:**
- Produces: one canonical `uv sync --python 3.12.13 --frozen --extra test` environment.
- Produces: required main-suite and quarantined-async CI jobs.

- [ ] **Step 1: Add a failing interpreter assertion**

Create `tests/test_runtime_version.py`:

```python
import platform


def test_runtime_matches_pinned_python():
    assert platform.python_version() == "3.12.13"
```

- [ ] **Step 2: Pin runtime and test extras**

Set `.python-version` to exactly:

```text
3.12.13
```

Change the Docker base to `python:3.12.13-slim-bookworm`. Add the browser extra:

```toml
browser-test = [
    "playwright==1.55.0",
]
```

Run: `uv lock --python 3.12.13`

Expected: `uv.lock` resolves with Python 3.12 compatibility.

- [ ] **Step 3: Add exact GitHub Actions jobs**

Create jobs that run these commands on `ubuntu-latest`:

```yaml
- uses: astral-sh/setup-uv@v6
- run: uv python install 3.12.13
- run: uv sync --python 3.12.13 --frozen --extra test
- run: uv run --frozen python -c 'import platform; assert platform.python_version() == "3.12.13"'
- run: uv run --frozen pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py
```

The separate `async-ibrx` job runs `uv run --frozen pytest tests/test_ibrx_async.py --timeout=30 -q` and is required, not allowed to fail.

- [ ] **Step 4: Verify the pinned environment**

Run: `uv sync --python "$(cat .python-version)" --frozen --extra test`

Run: `uv run --frozen pytest tests/test_runtime_version.py -q`

Expected: PASS under Python 3.12.13.

- [ ] **Step 5: Commit**

```bash
git add .python-version .github/workflows/ci.yml Dockerfile pyproject.toml uv.lock README.md tests/test_runtime_version.py
git commit -m "build(g0): pin canonical Python and CI jobs"
```

### Task 2: Canonical JSON authentication primitives

**Files:**
- Create: `trader/messaging/typed_rpc.py`
- Create: `tests/test_typed_rpc.py`

**Interfaces:**
- Produces: `TypedRpcRequest`, `TypedRpcResponse`, `RpcProblem`, `HmacServiceAuthenticator`, and `ReplayNonceCache`.
- Produces: `canonical_json(value: object) -> bytes`.

- [ ] **Step 1: Write signature, clock, and replay tests**

```python
def test_signature_binds_method_id_nonce_and_body():
    auth = HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0)
    request = auth.sign("approve_proposal", "req-1", "nonce-1", {"proposal_id": 7})
    auth.verify(request)
    changed = request.model_copy(update={"body": {"proposal_id": 8}})
    with pytest.raises(AuthenticationError):
        auth.verify(changed)


def test_nonce_cannot_be_replayed():
    auth = HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0)
    request = auth.sign("get_status", "req-1", "nonce-1", {})
    auth.verify(request)
    with pytest.raises(ReplayError):
        auth.verify(request)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run --frozen pytest tests/test_typed_rpc.py -q`

Expected: FAIL because the typed RPC module does not exist.

- [ ] **Step 3: Implement deterministic signing**

Use Pydantic models with `extra="forbid"` and this canonical form:

```python
def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def signing_bytes(request: TypedRpcRequest) -> bytes:
    return canonical_json({
        "method": request.method,
        "request_id": request.request_id,
        "timestamp": request.timestamp,
        "nonce": request.nonce,
        "body": request.body,
    })


def _digest(key: bytes, request: TypedRpcRequest) -> str:
    return hmac.new(key, signing_bytes(request), hashlib.sha256).hexdigest()
```

`verify()` checks timestamp skew, constant-time signature equality, then atomically claims the nonce. Reject missing fields, non-finite JSON numbers, duplicate JSON keys, and bodies above 1 MiB.

- [ ] **Step 4: Run typed authentication tests**

Run: `uv run --frozen pytest tests/test_typed_rpc.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/messaging/typed_rpc.py tests/test_typed_rpc.py
git commit -m "feat(g0): add authenticated canonical JSON envelopes"
```

### Task 3: Dedicated typed ZeroMQ clients and servers

**Files:**
- Modify: `trader/messaging/typed_rpc.py`
- Create: `tests/test_typed_rpc_transport.py`
- Modify: `trader/config.py`
- Modify: `config_defaults/trader.yaml`

**Interfaces:**
- Produces: `TypedRpcRegistry.register(socket_role, method, request_model, response_model, handler)`.
- Produces: `TypedRpcServer.serve()` and `TypedRpcClient.call(method, body, response_model)`.
- Socket roles are `query`, `command`, and `feed`; a method is registered on exactly one role.

- [ ] **Step 1: Write allowlist and socket-role tests**

```python
def test_command_is_rejected_on_query_socket(typed_servers, query_client):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query_client.call("approve_proposal", {"proposal_id": 7}, dict)
    assert exc.value.code == "METHOD_NOT_ALLOWED"


def test_unknown_and_dotted_methods_are_rejected(query_client):
    for method in ("trader.client.ib.reqGlobalCancel", "_private", "missing"):
        with pytest.raises(TypedRpcRemoteError):
            query_client.call(method, {}, dict)
```

- [ ] **Step 2: Run transport tests and verify failure**

Run: `uv run --frozen pytest tests/test_typed_rpc_transport.py -q`

Expected: FAIL because the typed transport classes do not exist.

- [ ] **Step 3: Implement raw-JSON ROUTER/DEALER transport**

The server decodes bytes with `json.loads`, validates `TypedRpcRequest`, authenticates, resolves only the exact `(role, method)` registration, validates the body model, invokes the handler, validates the response model, and returns `TypedRpcResponse`. It never calls `pack`, `unpack`, `dill.loads`, or object traversal from `clientserver.py`.

Use `LINGER=0`, `IMMEDIATE=1`, request-ID reply matching, a 1 MiB maximum frame, and a fresh DEALER identity after timeout. Feed clients use a separate instance and lock from query and command clients.

- [ ] **Step 4: Add configuration**

Add exact defaults:

```yaml
typed_query_port: 42101
typed_command_port: 42102
typed_feed_port: 42103
service_hmac_key_file: ""
unsafe_legacy_rpc: false
```

Production startup fails if the key file is absent, not mode `0600`, empty, or shorter than 32 bytes.

- [ ] **Step 5: Run transport and existing RPC tests**

Run: `uv run --frozen pytest tests/test_typed_rpc.py tests/test_typed_rpc_transport.py tests/test_clientserver_rpc.py -q`

Expected: PASS; the legacy offline tests remain intact.

- [ ] **Step 6: Commit**

```bash
git add trader/messaging/typed_rpc.py trader/config.py config_defaults/trader.yaml tests/test_typed_rpc_transport.py
git commit -m "feat(g0): add typed query command and feed sockets"
```

### Task 4: Close production bypass capabilities

**Files:**
- Create: `trader/messaging/production_api.py`
- Create: `trader/messaging/legacy_offline_api.py`
- Modify: `trader/messaging/trader_service_api.py`
- Modify: `trader/trading/trading_runtime.py:250-320`
- Modify: `trader/trader_service.py`
- Create: `tests/test_production_rpc_security.py`

**Interfaces:**
- Produces: `build_production_registry(trader, authenticator) -> TypedRpcRegistry`.
- Preserves raw object RPC only when both `simulation=True` and `unsafe_legacy_rpc=True`.
- Production query methods initially expose health/read schemas; command methods are registered by `[M1-F3]` through the coordinator.

- [ ] **Step 1: Write negative capability tests**

```python
@pytest.mark.parametrize("method", [
    "place_order_simple",
    "place_expressive_order",
    "place_standalone_order",
    "set_risk_limits",
    "cancel_all",
])
def test_production_registry_has_no_legacy_mutation(method, production_registry):
    assert not production_registry.contains("command", method)


def test_unsafe_legacy_rpc_requires_simulation():
    with pytest.raises(ValueError, match="offline simulation"):
        validate_rpc_mode(simulation=False, unsafe_legacy_rpc=True)
```

- [ ] **Step 2: Run security tests and verify failure**

Run: `uv run --frozen pytest tests/test_production_rpc_security.py -q`

Expected: FAIL because production and legacy capabilities are not separated.

- [ ] **Step 3: Split the API registration paths**

Move object-returning and unrestricted direct-order methods behind `LegacyOfflineTraderServiceApi`. `Trader.connect()` starts `RPCServer` for that class only in offline simulation with the explicit flag. Production always starts the typed servers and never exposes caller-controlled `skip_risk_gate` or `set_risk_limits`.

Keep `skip_risk_gate` as an internal keyword on `Executioner` only until `[M1-F3]` replaces it with coordinator-computed `RiskDirection`; no RPC schema contains that field.

- [ ] **Step 4: Run security and trading regressions**

Run: `uv run --frozen pytest tests/test_production_rpc_security.py tests/test_proposal_approval_gate.py tests/test_executioner.py tests/test_trading_runtime.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/messaging/production_api.py trader/messaging/legacy_offline_api.py trader/messaging/trader_service_api.py trader/trading/trading_runtime.py trader/trader_service.py tests/test_production_rpc_security.py
git commit -m "security(g0): remove production order bypass RPCs"
```

### Task 5: Split Compose supervision and scheduler ownership

**Files:**
- Modify: `docker-compose.yml`
- Modify: `Dockerfile`
- Modify: `scripts/docker-entrypoint.sh`
- Modify: `start_mmr.sh`
- Modify: `config_defaults/pycron.yaml`
- Modify: `config_defaults/no_docker_pycron.yaml`
- Create: `tests/test_compose_topology.py`
- Modify: `scripts/test_pycron.py`

**Interfaces:**
- Produces Compose services: `data`, `trader`, `strategy`, `dashboard`, and `scheduler`.
- Produces local launcher behavior: any mandatory child death terminates `start_mmr.sh` non-zero after stopping siblings.

- [ ] **Step 1: Write topology tests**

```python
def test_long_lived_services_are_not_pycron_jobs():
    config = yaml.safe_load(Path("config_defaults/pycron.yaml").read_text())
    names = {job["name"] for job in config["jobs"]}
    assert names.isdisjoint({"data_service", "trader_service", "strategy_service", "web_dashboard"})


def test_compose_has_one_service_per_process():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    for name in ("data", "trader", "strategy", "dashboard", "scheduler"):
        assert name in compose["services"]
        assert compose["services"][name]["restart"] == "unless-stopped"
```

- [ ] **Step 2: Run topology tests and verify failure**

Run: `uv run --frozen pytest tests/test_compose_topology.py scripts/test_pycron.py -q`

Expected: FAIL because Compose currently owns one `mmr` process and pycron still lists long-lived services.

- [ ] **Step 3: Define the split services**

Use one built image with service-specific commands:

```yaml
data:
  command: ["python", "-m", "trader.data_service"]
trader:
  command: ["python", "-m", "trader.trader_service"]
strategy:
  command: ["python", "-m", "trader.strategy_service"]
dashboard:
  command: ["python", "-m", "web.app"]
scheduler:
  command: ["python", "-m", "pycron.pycron", "--config", "/home/trader/.config/mmr/pycron.yaml"]
```

Apply `user: trader`, `read_only: true`, `tmpfs: /tmp`, health checks, restart policy, explicit memory/CPU limits, and the private `mmr-internal` network. Give `trader` the database volume, `strategy` the strategy-config volume, `scheduler` the backup/config paths it needs, and `dashboard` no database volume. Publish dashboard HTTP and typed query/command ports on host loopback only.

Remove long-lived service jobs from both pycron templates. Keep refresh, reconciliation, maintenance, and backup one-shots.

- [ ] **Step 4: Make the local launcher fail fast**

Replace the monitor loop with `wait -n`, record the failed child, terminate remaining children, wait for them, and exit with the failed child's non-zero status. A clean signal-driven shutdown exits zero.

- [ ] **Step 5: Validate rendered Compose and process death**

Run: `docker compose config --quiet`

Run: `uv run --frozen pytest tests/test_compose_topology.py scripts/test_pycron.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml Dockerfile scripts/docker-entrypoint.sh start_mmr.sh config_defaults/pycron.yaml config_defaults/no_docker_pycron.yaml tests/test_compose_topology.py scripts/test_pycron.py
git commit -m "ops(g0): assign one supervisor to each process"
```

### Task 6: G0 health, secret, and process-kill gate

**Files:**
- Create: `trader/operations/health.py`
- Create: `tests/test_service_health.py`
- Create: `tests/fullstack/test_process_supervision.py`
- Modify: `web/app.py` health endpoints only
- Modify: `docker-compose.yml` (add the `test`-profile `fullstack-tests` runner)
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces unauthenticated boolean `/healthz` and `/readyz`.
- Produces authenticated `/api/health` data including process state and each scheduled job's last success/failure.

- [ ] **Step 1: Write health redaction and process-kill tests**

```python
def test_public_health_has_no_dependency_detail(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert set(client.get("/readyz").json()) == {"ready"}


def test_health_redacts_secrets(health_payload):
    encoded = json.dumps(health_payload)
    assert "service_hmac" not in encoded.lower()
    assert "dashboard_token" not in encoded.lower()
```

The full-stack test stops `dashboard` and asserts `trader` and `strategy` container IDs and restart counts remain unchanged; it then stops `trader` and asserts Compose restarts only `trader` while health reports degraded until readiness returns.

- [ ] **Step 2: Implement health aggregation and pycron receipts**

Persist one-shot results as JSON lines under the scheduler's state volume with `job`, `started_at`, `completed_at`, `success`, and a redacted safe error. The authenticated dashboard health adapter combines typed service health with the newest receipt per job.

- [ ] **Step 3: Run G0 unit and negative security tests**

Run: `uv run --frozen pytest tests/test_runtime_version.py tests/test_typed_rpc.py tests/test_typed_rpc_transport.py tests/test_production_rpc_security.py tests/test_compose_topology.py tests/test_service_health.py -q`

Expected: PASS.

- [ ] **Step 4: Add the test-profile runner and run the smoke gate**

Define the runner service in `docker-compose.yml` so the gate is reproducible:

```yaml
fullstack-tests:
  profiles: ["test"]
  build: .
  user: trader
  command: ["python", "-m", "pytest", "tests/fullstack", "-q", "--timeout=300"]
  environment:
    TRADING_MODE: paper
    MMR_FAKE_BROKER: "1"
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock:ro
  networks: [mmr-internal]
  depends_on: [data, trader, strategy, dashboard, scheduler]
```

The read-only Docker socket lets the supervision test stop and inspect sibling
containers; it exists only in the `test` profile, never in production.

Run: `docker compose --profile test up --build --abort-on-container-exit fullstack-tests`

Expected: exit 0; no live credentials are present and raw RPC ports are not published.

- [ ] **Step 5: Commit**

```bash
git add trader/operations/health.py web/app.py tests/test_service_health.py tests/fullstack/test_process_supervision.py .github/workflows/ci.yml
git commit -m "test(g0): gate supervision and RPC hardening"
```
