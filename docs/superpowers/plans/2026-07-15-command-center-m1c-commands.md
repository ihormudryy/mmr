# M1-C Authenticated Command Surfaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add authenticated paper and live command surfaces to the command center — proposal create/approve/reject, position close, order cancel, strategy control, and pause/resume — where the browser never renders success from an HTTP acknowledgement.

**Architecture:** A dedicated `DashboardCommandGateway` owns its own typed RPC connection to the trader `TradingCommandCoordinator` (command socket 42102), with its own timeout and serialization lock so slow reads or bridge reconnection never delay an urgent action. Thin FastAPI routes validate session, session-bound CSRF, strict origin/host, and feature flags, then forward client-generated `command_id` bodies to the frozen typed methods. The browser drives single-POST paper commands and two-stage signed-nonce live ceremonies; `202 Accepted` renders only **Pending confirmation**, and outcomes come exclusively from correlated `command.updated` / domain events.

**Tech Stack:** CPython 3.12.13, FastAPI, Pydantic, Jinja2, browser JavaScript (`crypto.randomUUID`, `fetch`, `AbortSignal.timeout`), typed canonical-JSON RPC over ZeroMQ, pytest, Playwright.

## Global Constraints

- The source specification is `docs/superpowers/specs/2026-07-15-realtime-trading-command-center-design.md`; this plan covers `[M1-C]` (Sections 5.3, 9.1–9.7, 10, 11, 13, 14 step 4).
- The cross-plan interface freeze in `2026-07-15-command-center-plan-index.md` is binding: typed methods `create_proposal`, `approve_proposal`, `reject_proposal`, `set_trading_pause`, `enable_strategy`, `disable_strategy`, `update_strategy_params`, `cancel_order`, `cancel_orders`, `get_command`, and the `CommandReceipt(command_id, correlation_id, state, outcome, error_code, retryable)` dataclass. Browser HTTP routes call typed methods only through `DashboardCommandGateway`.
- Authoritative validation, idempotency, the pause gate, drift/freshness guards, and nonce verification live in `trader_service` (`[M1-F3]`). The web process forwards and renders; it never becomes a second validation authority.
- `DASHBOARD_COMMANDS_ENABLED` and `DASHBOARD_LIVE_COMMANDS_ENABLED` default to false. Live commands additionally require an exact `DASHBOARD_LIVE_ACCOUNT_ID` and a configured `DASHBOARD_LIVE_MAX_ORDER_NOTIONAL`; there is no permissive fallback and inconsistent configuration fails startup.
- Every mutation requires the session, a session-bound CSRF token, and strict origin/host validation, in paper and live modes alike.
- `command_id` is `crypto.randomUUID()`, created **before** preflight and reused across confirmation and every retry. A retry never mints a new ID.
- The signed preflight nonce is 30-second, single-use, and bound to command ID, action, params, `expected_version`, account, mode, and session; it is minted and verified by the `[M1-F3]` coordinator, never by the web process.
- Commands are never queued while a dependency is down; while the realtime stream is degraded the client disables exposure-increasing controls. Server validation remains final.
- An order whose risk classification cannot be established is treated as protective.
- HTTP `202 Accepted` means received only. Error responses use the stable envelope `{code, message, retryable, correlation_id}`.
- Commits are tagged `feat(m1-c):` (fixes `fix(m1-c):`, tests `test(m1-c):`).

Interfaces consumed from other plans (do not redefine):

- `[G0]` `trader/messaging/typed_rpc.py`: `TypedRpcClient(address, port, authenticator, timeout_s)` with `call(method, body, response_model)`, `TypedRpcRemoteError` (carries `.code`), `HmacServiceAuthenticator`; command socket port `42102`; key file `MMR_SERVICE_HMAC_KEY_FILE`.
- `[M1-F3]` `trader/domain/commands.py`: the frozen `CommandReceipt` dataclass; coordinator typed command methods above plus `preflight_command` (Task 2 pins its body).
- `[M1-R]` `web/command_center/session.py`: `DashboardSession(session_id: str, epoch: int, expires_at: datetime)`, FastAPI dependency `require_session(request) -> DashboardSession` (raises 401), and `session_csrf_token(session: DashboardSession) -> str`.
- `[M1-R]` `web/command_center/state.py` and the SSE plane: `command` and `proposal` entities updated from `command.updated` / `proposal.updated`; `/cc` page skeleton embedding `<meta name="cc-csrf-token">`; browser namespace `window.CC` with `CC.onEvent(eventType, handler)`, `CC.entity(type, id)`, `CC.entities(type)`, `CC.health()`, and `CC.streamState()` returning `"live" | "degraded"`.
- `[M1-F2]` order entity payloads carry `order_group_id` and `leg_role` (`"entry" | "parent" | "take_profit" | "stop" | "trailing_stop" | null`) used for cancel classification.

---

### Task 1: Command flags and startup validation

**Files:**
- Create: `web/command_center/flags.py`
- Modify: `web/app.py:107-121` (load flags at startup, next to the `app = FastAPI(...)` block)
- Create: `tests/test_command_flags.py`

**Interfaces:**
- Produces: `CommandFlags(commands_enabled: bool, live_commands_enabled: bool, live_account_id: str | None, live_max_order_notional: float | None)` (frozen dataclass).
- Produces: `load_command_flags(env: Mapping[str, str]) -> CommandFlags` and `CommandFlagsError(ValueError)`.
- Produces: `web.app._COMMAND_FLAGS` module value and `app.state.command_flags`, both set at import/startup so a bad config kills the process.

- [ ] **Step 1: Write failing flag tests**

Create `tests/test_command_flags.py`:

```python
import pytest

from web.command_center.flags import CommandFlags, CommandFlagsError, load_command_flags


def test_defaults_are_disabled():
    flags = load_command_flags({})
    assert flags == CommandFlags(False, False, None, None)


def test_live_requires_paper_commands_enabled():
    with pytest.raises(CommandFlagsError, match="DASHBOARD_COMMANDS_ENABLED"):
        load_command_flags({
            "DASHBOARD_COMMANDS_ENABLED": "false",
            "DASHBOARD_LIVE_COMMANDS_ENABLED": "true",
        })


@pytest.mark.parametrize("account", ["", "  ", "U*", "DU%", "live"])
def test_live_requires_exact_account_id(account):
    with pytest.raises(CommandFlagsError, match="exact DASHBOARD_LIVE_ACCOUNT_ID"):
        load_command_flags({
            "DASHBOARD_COMMANDS_ENABLED": "true",
            "DASHBOARD_LIVE_COMMANDS_ENABLED": "true",
            "DASHBOARD_LIVE_ACCOUNT_ID": account,
            "DASHBOARD_LIVE_MAX_ORDER_NOTIONAL": "25000",
        })


@pytest.mark.parametrize("notional", ["", "0", "-1", "nan", "inf", "lots"])
def test_live_requires_positive_finite_notional(notional):
    with pytest.raises(CommandFlagsError, match="DASHBOARD_LIVE_MAX_ORDER_NOTIONAL"):
        load_command_flags({
            "DASHBOARD_COMMANDS_ENABLED": "true",
            "DASHBOARD_LIVE_COMMANDS_ENABLED": "true",
            "DASHBOARD_LIVE_ACCOUNT_ID": "U1234567",
            "DASHBOARD_LIVE_MAX_ORDER_NOTIONAL": notional,
        })


def test_valid_live_configuration():
    flags = load_command_flags({
        "DASHBOARD_COMMANDS_ENABLED": "true",
        "DASHBOARD_LIVE_COMMANDS_ENABLED": "true",
        "DASHBOARD_LIVE_ACCOUNT_ID": "U1234567",
        "DASHBOARD_LIVE_MAX_ORDER_NOTIONAL": "25000",
    })
    assert flags == CommandFlags(True, True, "U1234567", 25000.0)


def test_malformed_boolean_fails_loudly():
    with pytest.raises(CommandFlagsError, match="boolean"):
        load_command_flags({"DASHBOARD_COMMANDS_ENABLED": "enabled"})
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --frozen pytest tests/test_command_flags.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'web.command_center.flags'`.

- [ ] **Step 3: Implement flag loading**

Create `web/command_center/flags.py`:

```python
"""[M1-C] Dashboard command feature flags (spec Section 10).

Both flags default false. Live commands have no permissive fallback: they
require an exact account ID and a finite positive maximum order notional.
Inconsistent configuration raises at import so startup fails loudly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping


class CommandFlagsError(ValueError):
    """Inconsistent dashboard command configuration; startup must fail."""


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
_WILDCARDS = set("*?%")


def _boolean(env: Mapping[str, str], name: str) -> bool:
    raw = str(env.get(name, "")).strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise CommandFlagsError(f"{name} must be a boolean flag, got {raw!r}")


@dataclass(frozen=True)
class CommandFlags:
    commands_enabled: bool
    live_commands_enabled: bool
    live_account_id: str | None
    live_max_order_notional: float | None


def load_command_flags(env: Mapping[str, str]) -> CommandFlags:
    commands = _boolean(env, "DASHBOARD_COMMANDS_ENABLED")
    live = _boolean(env, "DASHBOARD_LIVE_COMMANDS_ENABLED")
    account = str(env.get("DASHBOARD_LIVE_ACCOUNT_ID", "")).strip() or None
    raw_notional = str(env.get("DASHBOARD_LIVE_MAX_ORDER_NOTIONAL", "")).strip()

    notional: float | None = None
    if raw_notional:
        try:
            notional = float(raw_notional)
        except ValueError as exc:
            raise CommandFlagsError(
                f"DASHBOARD_LIVE_MAX_ORDER_NOTIONAL must be a number, got {raw_notional!r}"
            ) from exc
        if not math.isfinite(notional) or notional <= 0:
            raise CommandFlagsError(
                f"DASHBOARD_LIVE_MAX_ORDER_NOTIONAL must be finite and positive, got {raw_notional!r}"
            )

    if live and not commands:
        raise CommandFlagsError(
            "DASHBOARD_LIVE_COMMANDS_ENABLED requires DASHBOARD_COMMANDS_ENABLED"
        )
    if live and (account is None or _WILDCARDS & set(account) or account.lower() in {"live", "paper"}):
        raise CommandFlagsError(
            "live commands require an exact DASHBOARD_LIVE_ACCOUNT_ID; "
            "a mode label or wildcard account is insufficient"
        )
    if live and notional is None:
        raise CommandFlagsError(
            "live commands require DASHBOARD_LIVE_MAX_ORDER_NOTIONAL; "
            "there is no permissive fallback for a missing live limit"
        )
    return CommandFlags(commands, live, account, notional)
```

- [ ] **Step 4: Wire startup validation into the web app**

In `web/app.py`, immediately after `app = FastAPI(title='MMR Dashboard')`:

```python
from web.command_center.flags import load_command_flags

# [M1-C] Fails the process at startup on inconsistent command configuration.
_COMMAND_FLAGS = load_command_flags(os.environ)
app.state.command_flags = _COMMAND_FLAGS
```

- [ ] **Step 5: Run flag and dashboard tests**

Run: `uv run --frozen pytest tests/test_command_flags.py tests/test_web_dashboard.py -q`

Expected: PASS (default env resolves to all-disabled flags, so the legacy dashboard tests are unaffected).

- [ ] **Step 6: Commit**

```bash
git add web/command_center/flags.py web/app.py tests/test_command_flags.py
git commit -m "feat(m1-c): gate dashboard commands behind validated flags"
```

### Task 2: DashboardCommandGateway and the stable error contract

**Files:**
- Create: `web/command_center/gateway.py`
- Create: `tests/test_command_gateway.py`

**Interfaces:**
- Consumes: `TypedRpcClient`, `TypedRpcRemoteError`, `HmacServiceAuthenticator` from `trader/messaging/typed_rpc.py` (`[G0]`); `CommandReceipt` from `trader/domain/commands.py` (`[M1-F3]`).
- Consumes (pins the `[M1-F3]` contract): typed command method `preflight_command(body: dict) -> dict` with body `{command_id, action, params, expected_version, session_fingerprint}` returning `{command_id, nonce, expires_at, summary}` where `summary` carries `side, instrument, quantity, notional, order_type, latest_price, drift_bps, warnings, account_id, account_mode`.
- Produces: `DashboardCommandGateway(client_factory: Callable[[], TypedRpcClient], timeout_s: float = 5.0)` with `execute(method: str, body: dict) -> CommandReceipt`, `preflight(body: dict) -> PreflightTicket`, `get_command(command_id: str) -> CommandReceipt`.
- Produces: `GatewayError(Exception)` frozen dataclass `(code: str, message: str, retryable: bool, correlation_id: str | None)`.
- Produces: `PreflightTicket` frozen dataclass `(command_id: str, nonce: str, expires_at: str, summary: dict)`.
- Produces: `build_command_gateway(env: Mapping[str, str]) -> DashboardCommandGateway` for production wiring.

- [ ] **Step 1: Write failing gateway tests**

Create `tests/test_command_gateway.py`:

```python
import threading

import pytest

from trader.domain.commands import CommandReceipt
from trader.messaging.typed_rpc import TypedRpcRemoteError
from web.command_center.gateway import (
    DashboardCommandGateway,
    GatewayError,
    PreflightTicket,
)


class FakeTypedClient:
    def __init__(self):
        self.calls = []
        self.response = {
            "command_id": "c-1", "correlation_id": "c-1", "state": "RECEIVED",
            "outcome": None, "error_code": None, "retryable": False,
        }
        self.raise_exc: Exception | None = None
        self.closed = False

    def call(self, method, body, response_model):
        self.calls.append((method, body))
        if self.raise_exc is not None:
            raise self.raise_exc
        return dict(self.response, command_id=body.get("command_id", "c-1"))

    def close(self):
        self.closed = True


@pytest.fixture()
def fake():
    return FakeTypedClient()


@pytest.fixture()
def gateway(fake):
    return DashboardCommandGateway(client_factory=lambda: fake, timeout_s=0.5)


def test_execute_returns_receipt(gateway, fake):
    receipt = gateway.execute("approve_proposal", {"command_id": "cmd-9", "proposal_id": 7})
    assert isinstance(receipt, CommandReceipt)
    assert receipt.command_id == "cmd-9"
    assert receipt.state == "RECEIVED"
    assert fake.calls == [("approve_proposal", {"command_id": "cmd-9", "proposal_id": 7})]


def test_rejected_receipt_raises_gateway_error(gateway, fake):
    fake.response = {
        "command_id": "cmd-9", "correlation_id": "cmd-9", "state": "REJECTED",
        "outcome": {"message": "new trading is paused for DU123"},
        "error_code": "TRADING_PAUSED", "retryable": False,
    }
    with pytest.raises(GatewayError) as exc:
        gateway.execute("create_proposal", {"command_id": "cmd-9"})
    assert exc.value.code == "TRADING_PAUSED"
    assert exc.value.message == "new trading is paused for DU123"
    assert exc.value.retryable is False
    assert exc.value.correlation_id == "cmd-9"


def test_remote_error_maps_to_stable_contract(gateway, fake):
    fake.raise_exc = TypedRpcRemoteError(code="VERSION_CONFLICT", message="revision 4 expected 3")
    with pytest.raises(GatewayError) as exc:
        gateway.execute("approve_proposal", {"command_id": "cmd-9", "expected_version": 3})
    assert exc.value.code == "VERSION_CONFLICT"
    assert exc.value.retryable is False
    assert exc.value.correlation_id == "cmd-9"


def test_timeout_maps_to_outcome_unknown_and_resets_client(fake):
    made = []

    def factory():
        made.append(FakeTypedClient() if made else fake)
        return made[-1]

    gateway = DashboardCommandGateway(client_factory=factory, timeout_s=0.5)
    fake.raise_exc = TimeoutError("no reply in 0.5s")
    with pytest.raises(GatewayError) as exc:
        gateway.execute("cancel_order", {"command_id": "cmd-2", "order_entity_id": "o-1"})
    assert exc.value.code == "OUTCOME_UNKNOWN"
    assert exc.value.retryable is False
    assert exc.value.correlation_id == "cmd-2"
    assert fake.closed  # stale DEALER identity discarded
    # next call builds a fresh client
    gateway.get_command("cmd-2")
    assert len(made) == 2


def test_connection_error_is_retryable(gateway, fake):
    fake.raise_exc = ConnectionError("command socket down")
    with pytest.raises(GatewayError) as exc:
        gateway.execute("reject_proposal", {"command_id": "cmd-3", "proposal_id": 7})
    assert exc.value.code == "COMMAND_CHANNEL_DOWN"
    assert exc.value.retryable is True


def test_preflight_parses_ticket(gateway, fake):
    fake.response = {
        "command_id": "cmd-4", "nonce": "n-1", "expires_at": "2026-07-15T13:42:47Z",
        "summary": {"side": "BUY", "instrument": "AAPL", "drift_bps": 12.0,
                    "warnings": [], "account_id": "U1234567", "account_mode": "live",
                    "quantity": 10, "notional": 2350.0, "order_type": "MARKET",
                    "latest_price": 235.0},
    }
    ticket = gateway.preflight({"command_id": "cmd-4", "action": "approve_proposal",
                                "params": {"proposal_id": 7}, "expected_version": 3,
                                "session_fingerprint": "fp"})
    assert isinstance(ticket, PreflightTicket)
    assert ticket.nonce == "n-1"
    assert ticket.summary["account_mode"] == "live"
    assert fake.calls[0][0] == "preflight_command"


def test_gateway_serializes_calls_with_its_own_lock(gateway):
    assert isinstance(gateway._lock, type(threading.Lock()))
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --frozen pytest tests/test_command_gateway.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'web.command_center.gateway'`.

- [ ] **Step 3: Implement the gateway**

Create `web/command_center/gateway.py`:

```python
"""[M1-C] DashboardCommandGateway (spec Section 5.3).

A command-only typed RPC connection, independent from the event bridge and
snapshot clients, with its own timeout and serialization lock. Maps
TypedRpcRemoteError and transport failures to the stable Section 11 error
contract: {code, safe message, retryable, correlation_id}.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from trader.domain.commands import CommandReceipt
from trader.messaging.typed_rpc import (
    HmacServiceAuthenticator,
    TypedRpcClient,
    TypedRpcRemoteError,
)

logger = logging.getLogger("web.command_center.gateway")

# Codes a client may retry after fixing the stated condition. Everything
# else is final for that command_id (retrying returns the ledger outcome).
_RETRYABLE_CODES = frozenset({
    "QUOTE_MISSING", "QUOTE_STALE", "PREFLIGHT_EXPIRED",
    "DEPENDENCY_UNAVAILABLE", "COMMAND_CHANNEL_DOWN",
})


@dataclass(frozen=True)
class GatewayError(Exception):
    code: str
    message: str
    retryable: bool
    correlation_id: str | None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class PreflightTicket:
    command_id: str
    nonce: str
    expires_at: str
    summary: dict[str, Any]


class DashboardCommandGateway:
    def __init__(self, client_factory: Callable[[], TypedRpcClient],
                 timeout_s: float = 5.0):
        self._client_factory = client_factory
        self._client: TypedRpcClient | None = None
        self._lock = threading.Lock()  # command-only; never shared with reads
        self._timeout_s = timeout_s

    # -- typed call plumbing -------------------------------------------------
    def _call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        correlation = body.get("command_id")
        with self._lock:
            try:
                if self._client is None:
                    self._client = self._client_factory()
                return self._client.call(method, body, dict)
            except TypedRpcRemoteError as exc:
                code = getattr(exc, "code", "UPSTREAM_ERROR") or "UPSTREAM_ERROR"
                raise GatewayError(
                    code=code,
                    message=getattr(exc, "message", None) or str(exc),
                    retryable=code in _RETRYABLE_CODES,
                    correlation_id=correlation,
                ) from exc
            except TimeoutError as exc:
                self._reset_locked()
                raise GatewayError(
                    code="OUTCOME_UNKNOWN",
                    message="no acknowledgement from the command coordinator; "
                            "reconciling by command id",
                    retryable=False,
                    correlation_id=correlation,
                ) from exc
            except (ConnectionError, OSError) as exc:
                self._reset_locked()
                raise GatewayError(
                    code="COMMAND_CHANNEL_DOWN",
                    message="command channel unavailable",
                    retryable=True,
                    correlation_id=correlation,
                ) from exc

    def _reset_locked(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - already discarding
                logger.debug("discarding command client failed", exc_info=True)

    # -- public surface ------------------------------------------------------
    def execute(self, method: str, body: dict[str, Any]) -> CommandReceipt:
        raw = self._call(method, body)
        receipt = CommandReceipt(
            command_id=raw["command_id"],
            correlation_id=raw["correlation_id"],
            state=raw["state"],
            outcome=raw.get("outcome"),
            error_code=raw.get("error_code"),
            retryable=bool(raw.get("retryable", False)),
        )
        if receipt.state == "REJECTED":
            message = (receipt.outcome or {}).get("message", "command rejected")
            raise GatewayError(
                code=receipt.error_code or "COMMAND_REJECTED",
                message=message,
                retryable=receipt.retryable,
                correlation_id=receipt.correlation_id,
            )
        return receipt

    def preflight(self, body: dict[str, Any]) -> PreflightTicket:
        raw = self._call("preflight_command", body)
        return PreflightTicket(
            command_id=raw["command_id"],
            nonce=raw["nonce"],
            expires_at=raw["expires_at"],
            summary=dict(raw["summary"]),
        )

    def get_command(self, command_id: str) -> CommandReceipt:
        raw = self._call("get_command", {"command_id": command_id})
        return CommandReceipt(
            command_id=raw["command_id"],
            correlation_id=raw["correlation_id"],
            state=raw["state"],
            outcome=raw.get("outcome"),
            error_code=raw.get("error_code"),
            retryable=bool(raw.get("retryable", False)),
        )


def build_command_gateway(env: Mapping[str, str] = os.environ) -> DashboardCommandGateway:
    key = Path(env["MMR_SERVICE_HMAC_KEY_FILE"]).read_bytes()
    address = env.get("MMR_TRADER_HOST", "127.0.0.1")
    port = int(env.get("MMR_TYPED_COMMAND_PORT", "42102"))
    timeout_s = float(env.get("DASHBOARD_COMMAND_TIMEOUT_S", "5.0"))
    return DashboardCommandGateway(
        client_factory=lambda: TypedRpcClient(
            address=address, port=port,
            authenticator=HmacServiceAuthenticator(key),
            timeout_s=timeout_s,
        ),
        timeout_s=timeout_s,
    )
```

- [ ] **Step 4: Run gateway tests**

Run: `uv run --frozen pytest tests/test_command_gateway.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/command_center/gateway.py tests/test_command_gateway.py
git commit -m "feat(m1-c): add command gateway with stable error contract"
```

### Task 3: Command router, proposal creation, and position close

**Files:**
- Create: `web/command_center/routes_commands.py`
- Modify: `web/app.py` (install router + gateway after the Task 1 flag block)
- Modify: `web/templates/command_center.html` (append drawers + pending/banner containers at the end of the `[M1-R]` skeleton's body)
- Modify: `web/static/command_center.js` (append the `[M1-C]` command section)
- Create: `tests/test_command_routes.py`

**Interfaces:**
- Consumes: `require_session`, `DashboardSession`, `session_csrf_token` from `web/command_center/session.py` (`[M1-R]`); `DashboardCommandGateway`, `GatewayError` from Task 2; `CommandFlags` from Task 1.
- Produces: `install_command_routes(app: FastAPI) -> None` (includes the router and registers error handlers).
- Produces: `require_command_auth(request, session=Depends(require_session)) -> DashboardSession` (origin → CSRF → flags, in that order).
- Produces: `CommandApiError(status_code: int, code: str, message: str, retryable: bool = False, correlation_id: str | None = None)`.
- Produces routes: `POST /api/commands/proposals`, `POST /api/commands/positions/{account}/{conid}/close`, `GET /api/commands/{command_id}` (reconciliation read).
- Produces JS: `ccNewCommandId()`, `ccPost(url, body, opts)`, `ccSubmitCommand(kind, label, url, body)`, `ccRequireAvailable(kind)`, pending-confirmation rendering, outcome-unknown banner + `ccReconcileCommand`, and the `command.updated` resolution hook. Every later task reuses these.

- [ ] **Step 1: Write failing route tests**

Create `tests/test_command_routes.py`:

```python
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import web.command_center.routes_commands as routes_commands
from trader.domain.commands import CommandReceipt
from web.command_center.flags import CommandFlags
from web.command_center.gateway import GatewayError, PreflightTicket
from web.command_center.routes_commands import install_command_routes
from web.command_center.session import DashboardSession, require_session

CMD_ID = "0f9b2c1a-5b7e-4c1d-9e2f-3a4b5c6d7e8f"
SESSION = DashboardSession(session_id="s-1", epoch=1,
                           expires_at=datetime(2026, 7, 16, tzinfo=timezone.utc))
HEADERS = {"X-CSRF-Token": "test-csrf", "Origin": "http://testserver",
           "Host": "testserver"}


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.error: GatewayError | None = None
        self.receipt = CommandReceipt("", "corr-1", "RECEIVED", None, None, False)
        self.ticket = PreflightTicket(CMD_ID, "n-1", "2026-07-15T13:42:47Z", {})

    def execute(self, method, body):
        self.calls.append((method, body))
        if self.error is not None:
            raise self.error
        return replace(self.receipt, command_id=body["command_id"])

    def preflight(self, body):
        self.calls.append(("preflight_command", body))
        if self.error is not None:
            raise self.error
        return self.ticket

    def get_command(self, command_id):
        self.calls.append(("get_command", {"command_id": command_id}))
        return replace(self.receipt, command_id=command_id, state="SUBMITTED")


@pytest.fixture(autouse=True)
def _fixed_csrf(monkeypatch):
    monkeypatch.setattr(routes_commands, "session_csrf_token", lambda s: "test-csrf")


@pytest.fixture()
def gateway():
    return FakeGateway()


def make_client(gateway, flags=CommandFlags(True, False, None, None)):
    app = FastAPI()
    app.state.command_flags = flags
    app.state.command_gateway = gateway
    install_command_routes(app)
    app.dependency_overrides[require_session] = lambda: SESSION
    return TestClient(app)


def _proposal_body(**overrides):
    body = {"command_id": CMD_ID, "symbol": "AAPL", "side": "BUY",
            "order_type": "MARKET", "tif": "DAY", "reasoning": "breakout"}
    body.update(overrides)
    return body


def test_create_returns_202_receipt_and_forwards_typed_body(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/proposals", json=_proposal_body(), headers=HEADERS)
    assert r.status_code == 202
    assert r.json() == {"command_id": CMD_ID, "correlation_id": "corr-1",
                        "state": "RECEIVED"}
    method, body = gateway.calls[0]
    assert method == "create_proposal"
    assert body["command_id"] == CMD_ID
    assert body["proposal"]["symbol"] == "AAPL"
    assert "command_id" not in body["proposal"]


def test_empty_quantity_and_amount_means_auto_sizing(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/proposals", json=_proposal_body(), headers=HEADERS)
    assert r.status_code == 202
    proposal = gateway.calls[0][1]["proposal"]
    assert proposal["quantity"] is None and proposal["amount"] is None


@pytest.mark.parametrize("bad, match", [
    (dict(order_type="LIMIT"), "limit_price"),
    (dict(limit_price=165.0), "market"),
    (dict(quantity=10, amount=5000.0), "quantity OR amount"),
    (dict(bracket_take_profit=180.0), "bracket"),
    (dict(stop_loss=150.0, trailing_stop_pct=2.0), "one exit"),
    (dict(confidence=1.5), "confidence"),
    (dict(unknown_field=1), "unknown_field"),
])
def test_create_body_validation(gateway, bad, match):
    client = make_client(gateway)
    r = client.post("/api/commands/proposals", json=_proposal_body(**bad),
                    headers=HEADERS)
    assert r.status_code == 422
    assert match.lower() in str(r.json()).lower()
    assert gateway.calls == []


def test_missing_quote_refusal_is_surfaced_verbatim(gateway):
    gateway.error = GatewayError("QUOTE_MISSING",
                                 "no fresh quote for AAPL; proposal refused",
                                 retryable=True, correlation_id=CMD_ID)
    client = make_client(gateway)
    r = client.post("/api/commands/proposals", json=_proposal_body(), headers=HEADERS)
    assert r.status_code == 409
    assert r.json() == {"code": "QUOTE_MISSING",
                        "message": "no fresh quote for AAPL; proposal refused",
                        "retryable": True, "correlation_id": CMD_ID}


def test_pause_gate_error_is_surfaced_verbatim(gateway):
    gateway.error = GatewayError("TRADING_PAUSED",
                                 "new trading is paused for DU123",
                                 retryable=False, correlation_id=CMD_ID)
    client = make_client(gateway)
    r = client.post("/api/commands/proposals", json=_proposal_body(), headers=HEADERS)
    assert r.status_code == 409
    assert r.json()["message"] == "new trading is paused for DU123"


def test_close_position_builds_reducing_payload(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/positions/DU123/265598/close",
                    json={"command_id": CMD_ID, "quantity": 40.0,
                          "order_type": "MARKET", "reasoning": "trim"},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "create_proposal"
    assert body["close"] == {"account_id": "DU123", "conid": 265598,
                             "quantity": 40.0, "order_type": "MARKET",
                             "limit_price": None, "reasoning": "trim"}


def test_commands_disabled_returns_403_before_gateway(gateway):
    client = make_client(gateway, flags=CommandFlags(False, False, None, None))
    r = client.post("/api/commands/proposals", json=_proposal_body(), headers=HEADERS)
    assert r.status_code == 403
    assert r.json()["code"] == "COMMANDS_DISABLED"
    assert gateway.calls == []


def test_get_command_returns_receipt_for_reconciliation(gateway):
    client = make_client(gateway)
    r = client.get(f"/api/commands/{CMD_ID}", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["state"] == "SUBMITTED"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --frozen pytest tests/test_command_routes.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'web.command_center.routes_commands'`.

- [ ] **Step 3: Implement the router, auth dependency, and create/close routes**

Create `web/command_center/routes_commands.py`:

```python
"""[M1-C] Authenticated command routes (spec Sections 9.1, 9.6, 10, 11).

Thin translation layer: session + CSRF + origin + flags, then forward to the
frozen typed methods through DashboardCommandGateway. 202 means received
only; the browser resolves outcomes from correlated command.updated events.
"""
from __future__ import annotations

import secrets
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trader.domain.commands import CommandReceipt
from web.command_center.flags import CommandFlags
from web.command_center.gateway import DashboardCommandGateway, GatewayError
from web.command_center.session import (
    DashboardSession,
    require_session,
    session_csrf_token,
)

router = APIRouter()

_HTTP_STATUS = {
    "COMMANDS_DISABLED": 403, "LIVE_COMMANDS_DISABLED": 403,
    "CSRF_REJECTED": 403, "ORIGIN_REJECTED": 403,
    "VALIDATION_FAILED": 422, "NOT_FOUND": 404,
    "VERSION_CONFLICT": 409, "COMMAND_CONFLICT": 409, "COMMAND_REJECTED": 409,
    "TRADING_PAUSED": 409, "QUOTE_MISSING": 409, "QUOTE_STALE": 409,
    "PREFLIGHT_REQUIRED": 428, "PREFLIGHT_EXPIRED": 410,
    "PREFLIGHT_CONSUMED": 410, "PREFLIGHT_MISMATCH": 409,
    "DEPENDENCY_UNAVAILABLE": 503, "COMMAND_CHANNEL_DOWN": 503,
    "OUTCOME_UNKNOWN": 504,
}


class CommandApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str,
                 retryable: bool = False, correlation_id: str | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.correlation_id = correlation_id


def _envelope(code: str, message: str, retryable: bool,
              correlation_id: str | None) -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": retryable,
            "correlation_id": correlation_id}


def _gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    return JSONResponse(status_code=_HTTP_STATUS.get(exc.code, 502),
                        content=_envelope(exc.code, exc.message, exc.retryable,
                                          exc.correlation_id))


def _command_api_error_handler(request: Request, exc: CommandApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code,
                        content=_envelope(exc.code, exc.message, exc.retryable,
                                          exc.correlation_id))


def _check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    if not origin or not host:
        raise CommandApiError(403, "ORIGIN_REJECTED",
                              "mutations require a same-origin browser request")
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https") or parsed.netloc != host:
        raise CommandApiError(403, "ORIGIN_REJECTED",
                              "cross-origin mutation rejected")


def require_command_auth(
    request: Request,
    session: DashboardSession = Depends(require_session),
) -> DashboardSession:
    _check_origin(request)
    supplied = request.headers.get("X-CSRF-Token", "")
    if not secrets.compare_digest(supplied, session_csrf_token(session)):
        raise CommandApiError(403, "CSRF_REJECTED", "session CSRF token mismatch")
    flags: CommandFlags = request.app.state.command_flags
    if not flags.commands_enabled:
        raise CommandApiError(403, "COMMANDS_DISABLED",
                              "dashboard commands are disabled "
                              "(DASHBOARD_COMMANDS_ENABLED=false)")
    return session


def _gateway(request: Request) -> DashboardCommandGateway:
    return request.app.state.command_gateway


def _receipt_json(receipt: CommandReceipt) -> JSONResponse:
    return JSONResponse(status_code=202, content={
        "command_id": receipt.command_id,
        "correlation_id": receipt.correlation_id,
        "state": receipt.state,
    })


_COMMAND_ID = Field(min_length=36, max_length=36)


class CreateProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = _COMMAND_ID
    symbol: str = Field(min_length=1, max_length=12)
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_price: float | None = Field(default=None, gt=0)
    quantity: float | None = Field(default=None, gt=0)
    amount: float | None = Field(default=None, gt=0)
    bracket_take_profit: float | None = Field(default=None, gt=0)
    bracket_stop_loss: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    trailing_stop_pct: float | None = Field(default=None, gt=0, le=50)
    tif: Literal["DAY", "GTC", "GTD", "IOC", "OPG"] = "DAY"
    confidence: float | None = Field(default=None, ge=0, le=1,
                                     description="confidence 0-1")
    group: str | None = Field(default=None, max_length=40)
    reasoning: str = Field(default="", max_length=8000)

    @model_validator(mode="after")
    def _cross_field(self) -> "CreateProposalBody":
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.order_type == "MARKET" and self.limit_price is not None:
            raise ValueError("market orders must not carry limit_price")
        if self.quantity is not None and self.amount is not None:
            raise ValueError("give quantity OR amount; leave both empty "
                             "for automatic position sizing")
        if (self.bracket_take_profit is None) != (self.bracket_stop_loss is None):
            raise ValueError("bracket requires both take-profit and stop-loss")
        exits = [self.bracket_take_profit is not None,
                 self.stop_loss is not None,
                 self.trailing_stop_pct is not None]
        if sum(exits) > 1:
            raise ValueError("choose one exit: bracket, stop-loss, or trailing stop")
        return self


class ClosePositionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = _COMMAND_ID
    quantity: float = Field(gt=0)
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_price: float | None = Field(default=None, gt=0)
    reasoning: str = Field(default="", max_length=8000)


@router.post("/api/commands/proposals")
def create_proposal(body: CreateProposalBody, request: Request,
                    session: DashboardSession = Depends(require_command_auth)):
    # Source, account, mode, reference price/feed guards are derived
    # server-side by the coordinator (spec 9.6) — never sent by the browser.
    receipt = _gateway(request).execute("create_proposal", {
        "command_id": body.command_id,
        "proposal": body.model_dump(exclude={"command_id"}),
    })
    return _receipt_json(receipt)


@router.post("/api/commands/positions/{account}/{conid}/close")
def close_position(account: str, conid: int, body: ClosePositionBody,
                   request: Request,
                   session: DashboardSession = Depends(require_command_auth)):
    # Reducing proposal via the same create path; the coordinator verifies
    # the broker position and the reducible quantity (spec 8.2 / 9.6).
    receipt = _gateway(request).execute("create_proposal", {
        "command_id": body.command_id,
        "close": {"account_id": account, "conid": conid,
                  **body.model_dump(exclude={"command_id"})},
    })
    return _receipt_json(receipt)


@router.get("/api/commands/{command_id}")
def get_command(command_id: str, request: Request,
                session: DashboardSession = Depends(require_session)):
    # Read-only reconciliation lookup for the outcome-unknown banner.
    receipt = _gateway(request).get_command(command_id)
    return {"command_id": receipt.command_id,
            "correlation_id": receipt.correlation_id,
            "state": receipt.state, "outcome": receipt.outcome,
            "error_code": receipt.error_code, "retryable": receipt.retryable}


def install_command_routes(app: FastAPI) -> None:
    app.include_router(router)
    app.add_exception_handler(GatewayError, _gateway_error_handler)
    app.add_exception_handler(CommandApiError, _command_api_error_handler)
```

- [ ] **Step 4: Wire the router and gateway into the web app**

In `web/app.py`, extend the Task 1 block:

```python
from web.command_center.routes_commands import install_command_routes

install_command_routes(app)
if _COMMAND_FLAGS.commands_enabled:
    from web.command_center.gateway import build_command_gateway
    app.state.command_gateway = build_command_gateway(os.environ)
```

The router is always installed so disabled commands return a stable 403 `COMMANDS_DISABLED` instead of a 404; the gateway — and its HMAC-key requirement — is constructed only when commands are enabled.

- [ ] **Step 5: Run route tests**

Run: `uv run --frozen pytest tests/test_command_routes.py tests/test_command_flags.py -q`

Expected: PASS.

- [ ] **Step 6: Add the command lifecycle JS and the drawers**

Append to `web/static/command_center.js`:

```javascript
/* ===================== [M1-C] command surfaces ===================== */
/* 202 == received only. Success renders ONLY from a correlated
 * command.updated event; timeout renders "Outcome unknown — reconciling". */

CC.commands = {
  pending: new Map(),   // command_id -> {label}
  unknown: new Map(),   // command_id -> {label}
  availability: {},     // command kind -> {enabled, reason} (Task 6)
};

function ccCsrfToken() {
  return document.querySelector('meta[name="cc-csrf-token"]').content;
}

function ccNewCommandId() {
  // Created BEFORE preflight; reused across confirmation and every retry.
  return crypto.randomUUID();
}

function ccToast(kind, text) {
  const el = document.createElement('div');
  el.className = `cc-toast cc-toast-${kind}`;
  el.textContent = text;
  document.getElementById('cc-toasts').appendChild(el);
  setTimeout(() => el.remove(), 8000);
}

async function ccPost(url, body, {okStatus = 202} = {}) {
  const timeoutMs = window.CC_COMMAND_TIMEOUT_MS || 8000;
  let res;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json',
                'X-CSRF-Token': ccCsrfToken()},
      credentials: 'same-origin',
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (err) {
    return {ok: false, outcomeUnknown: true,
            error: {code: 'OUTCOME_UNKNOWN',
                    message: 'no acknowledgement — reconciling',
                    retryable: false, correlation_id: body.command_id}};
  }
  const data = await res.json().catch(() => ({}));
  if (res.status === okStatus) return {ok: true, data};
  const unknown = res.status === 504 || data.code === 'OUTCOME_UNKNOWN';
  return {ok: false, outcomeUnknown: unknown, error: data};
}

function ccRenderPending() {
  const box = document.getElementById('cc-pending-commands');
  box.replaceChildren(...[...CC.commands.pending.entries()].map(([id, p]) => {
    const chip = document.createElement('div');
    chip.className = 'cc-pending';
    chip.dataset.commandId = id;
    chip.textContent = `${p.label} — Pending confirmation`;
    return chip;
  }));
}

function ccShowOutcomeUnknown(commandId, label) {
  const banner = document.getElementById('cc-outcome-unknown');
  CC.commands.unknown.set(commandId, {label});
  banner.hidden = false;
  banner.textContent =
      `Outcome unknown — reconciling: ${
        [...CC.commands.unknown.values()].map((u) => u.label).join(', ')}`;
}

function ccClearOutcomeUnknown(commandId) {
  CC.commands.unknown.delete(commandId);
  if (CC.commands.unknown.size === 0) {
    document.getElementById('cc-outcome-unknown').hidden = true;
  }
}

async function ccReconcileCommand(commandId, label) {
  // Authoritative refresh: poll the ledger until a terminal state or a
  // correlated command.updated event clears the banner first.
  for (let i = 0; i < 12 && CC.commands.unknown.has(commandId); i += 1) {
    await new Promise((r) => setTimeout(r, window.CC_RECONCILE_MS || 5000));
    try {
      const res = await fetch(`/api/commands/${commandId}`,
                              {credentials: 'same-origin'});
      if (!res.ok) continue;
      const receipt = await res.json();
      if (['SUBMITTED', 'REJECTED', 'RESOLVED'].includes(receipt.state)) {
        ccResolveCommand(commandId, receipt.state, receipt.error_code, label);
        return;
      }
    } catch (err) { /* keep reconciling */ }
  }
}

function ccResolveCommand(commandId, state, errorCode, label) {
  const pending = CC.commands.pending.get(commandId);
  const name = label || (pending && pending.label) || commandId;
  CC.commands.pending.delete(commandId);
  ccClearOutcomeUnknown(commandId);
  ccRenderPending();
  if (state === 'REJECTED') {
    ccToast('error', `${name}: rejected (${errorCode || 'no code'})`);
  } else {
    ccToast('ok', `${name}: ${state.toLowerCase()}`);
  }
}

CC.onEvent('command.updated', (evt) => {
  const p = evt.payload || {};
  const id = p.command_id || evt.entity_id;
  if (!CC.commands.pending.has(id) && !CC.commands.unknown.has(id)) return;
  if (['SUBMITTED', 'REJECTED', 'RESOLVED'].includes(p.state)) {
    ccResolveCommand(id, p.state, p.error_code, null);
  }
});

function ccRequireAvailable(kind) {
  const a = CC.commands.availability[kind];
  if (a && !a.enabled) {
    ccToast('error',
            `Command unavailable — ${a.reason}. Commands are never queued.`);
    return false;
  }
  return true;
}

async function ccSubmitCommand(kind, label, url, body) {
  if (!ccRequireAvailable(kind)) return;
  CC.commands.pending.set(body.command_id, {label});
  ccRenderPending();
  const result = await ccPost(url, body);
  if (result.ok) return;  // stays "Pending confirmation" until command.updated
  CC.commands.pending.delete(body.command_id);
  ccRenderPending();
  if (result.outcomeUnknown) {
    ccShowOutcomeUnknown(body.command_id, label);
    ccReconcileCommand(body.command_id, label);
    return;
  }
  ccToast('error',
          `${label}: ${result.error.message} (${result.error.code})`);
}

/* ---- New proposal drawer (full CLI `propose` expressiveness) ---- */

function ccOpenProposalDrawer() {
  document.getElementById('cc-proposal-drawer').hidden = false;
}

function ccProposalBody(form) {
  const f = new FormData(form);
  const num = (k) => (f.get(k) ? Number(f.get(k)) : null);
  const body = {
    command_id: ccNewCommandId(),
    symbol: String(f.get('symbol') || '').trim().toUpperCase(),
    side: f.get('side'),
    order_type: f.get('order_type'),
    limit_price: f.get('order_type') === 'LIMIT' ? num('limit_price') : null,
    quantity: num('quantity'),       // both empty -> server auto-sizing
    amount: num('amount'),
    bracket_take_profit: null, bracket_stop_loss: null,
    stop_loss: null, trailing_stop_pct: null,
    tif: f.get('tif'),
    confidence: num('confidence'),
    group: String(f.get('group') || '').trim() || null,
    reasoning: String(f.get('reasoning') || ''),
  };
  const exit = f.get('exit_kind');
  if (exit === 'bracket') {
    body.bracket_take_profit = num('bracket_take_profit');
    body.bracket_stop_loss = num('stop_loss_price');
  } else if (exit === 'stop_loss') {
    body.stop_loss = num('stop_loss_price');
  } else if (exit === 'trailing_stop_pct') {
    body.trailing_stop_pct = num('trailing_stop_pct');
  }
  return body;
}

document.getElementById('cc-proposal-form').addEventListener('submit',
    async (evt) => {
      evt.preventDefault();
      const body = ccProposalBody(evt.target);
      document.getElementById('cc-proposal-drawer').hidden = true;
      await ccSubmitCommand('create_proposal',
          `New proposal ${body.side} ${body.symbol}`,
          '/api/commands/proposals', body);
    });

/* ---- Close position drawer (pre-filled reducing proposal) ---- */

function ccOpenCloseDrawer(position) {
  const d = document.getElementById('cc-close-drawer');
  d.querySelector('[data-field=instrument]').textContent =
      `${position.symbol} (${position.conid})`;
  d.querySelector('[data-field=side]').textContent =
      position.quantity > 0 ? 'SELL' : 'BUY';  // opposite, reducing
  const qty = d.querySelector('input[name=quantity]');
  qty.value = Math.abs(position.quantity);
  qty.max = Math.abs(position.quantity);  // never exceed reducible quantity
  d.dataset.account = position.account_id;
  d.dataset.conid = position.conid;
  d.hidden = false;
}

document.getElementById('cc-close-form').addEventListener('submit',
    async (evt) => {
      evt.preventDefault();
      const d = document.getElementById('cc-close-drawer');
      const body = {
        command_id: ccNewCommandId(),
        quantity: Number(d.querySelector('input[name=quantity]').value),
        order_type: 'MARKET',  // market by default (spec 8.2)
        reasoning: d.querySelector('textarea[name=reasoning]').value,
      };
      d.hidden = true;
      await ccSubmitCommand('create_proposal',
          `Close position ${d.dataset.conid}`,
          `/api/commands/positions/${d.dataset.account}/` +
          `${d.dataset.conid}/close`, body);
    });
```

Append to `web/templates/command_center.html` (inside the body, after the `[M1-R]` panels):

```html
<!-- [M1-C] command containers -->
<div id="cc-toasts" aria-live="polite"></div>
<div id="cc-pending-commands" aria-live="polite"></div>
<div id="cc-outcome-unknown" class="cc-banner cc-banner-warn" hidden></div>

<!-- [M1-C] New proposal drawer — same expressiveness as CLI `propose` -->
<aside id="cc-proposal-drawer" class="cc-drawer" hidden>
  <h2>New proposal</h2>
  <form id="cc-proposal-form">
    <label>Symbol <input name="symbol" required maxlength="12"></label>
    <label>Side
      <select name="side"><option>BUY</option><option>SELL</option></select>
    </label>
    <label>Order type
      <select name="order_type">
        <option value="MARKET" selected>Market</option>
        <option value="LIMIT">Limit</option>
      </select>
    </label>
    <label>Limit price
      <input name="limit_price" type="number" step="0.01" min="0.01"></label>
    <fieldset>
      <legend>Size — leave both empty for automatic position sizing</legend>
      <label>Quantity <input name="quantity" type="number" step="any" min="0"></label>
      <label>Notional amount <input name="amount" type="number" step="0.01" min="0"></label>
    </fieldset>
    <fieldset>
      <legend>Exit</legend>
      <label>Kind
        <select name="exit_kind">
          <option value="" selected>None</option>
          <option value="bracket">Bracket (TP + SL)</option>
          <option value="stop_loss">Stop loss</option>
          <option value="trailing_stop_pct">Trailing stop %</option>
        </select>
      </label>
      <label>Take profit
        <input name="bracket_take_profit" type="number" step="0.01" min="0.01"></label>
      <label>Stop loss
        <input name="stop_loss_price" type="number" step="0.01" min="0.01"></label>
      <label>Trailing %
        <input name="trailing_stop_pct" type="number" step="0.1" min="0.1" max="50"></label>
    </fieldset>
    <label>Time in force
      <select name="tif">
        <option selected>DAY</option><option>GTC</option><option>GTD</option>
        <option>IOC</option><option>OPG</option>
      </select>
    </label>
    <label>Confidence (0–1)
      <input name="confidence" type="number" step="0.05" min="0" max="1"></label>
    <label>Group tag <input name="group" maxlength="40"></label>
    <label>Reasoning <textarea name="reasoning" rows="4"></textarea></label>
    <button type="submit" data-cc-command="create_proposal">Create proposal</button>
  </form>
</aside>

<!-- [M1-C] Close position drawer — pre-filled reducing proposal -->
<aside id="cc-close-drawer" class="cc-drawer" hidden>
  <h2>Close position</h2>
  <p><span data-field=instrument></span> — reducing
     <strong data-field=side></strong> (market by default)</p>
  <form id="cc-close-form">
    <label>Quantity (max = reducible quantity)
      <input name="quantity" type="number" step="any" min="0.000001" required></label>
    <label>Reasoning <textarea name="reasoning" rows="2"></textarea></label>
    <button type="submit" data-cc-command="create_proposal">
      Create close proposal</button>
  </form>
</aside>
```

- [ ] **Step 7: Run the route suite again (JS/HTML are exercised in Task 7's browser gate)**

Run: `uv run --frozen pytest tests/test_command_routes.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add web/command_center/routes_commands.py web/app.py web/templates/command_center.html web/static/command_center.js tests/test_command_routes.py
git commit -m "feat(m1-c): add proposal create and position close surfaces"
```

### Task 4: Approve, reject, preflight, and the live confirmation ceremony

**Files:**
- Modify: `web/command_center/routes_commands.py` (approve/reject/preflight routes)
- Modify: `web/command_center/session.py` (add `session_fingerprint`)
- Modify: `web/static/command_center.js` (ceremony + confirmation drawer + approve/reject actions)
- Modify: `web/templates/command_center.html` (live confirmation drawer)
- Modify: `tests/test_command_routes.py`

**Interfaces:**
- Consumes: `PreflightTicket`, `gateway.preflight` from Task 2; proposal entities with `entity_revision` and `account_mode` from `DashboardState` (`[M1-R]`).
- Produces: `session_fingerprint(session: DashboardSession) -> str` — an HMAC-derived opaque value binding a preflight nonce to the browser session; safe to send to the trader, never the raw cookie.
- Produces routes: `POST /api/preflight`, `POST /api/commands/proposals/{pid}/approve`, `POST /api/commands/proposals/{pid}/reject`.
- Produces JS: `ccRequestPreflight(commandId, action, params, expectedVersion)`, `ccOpenConfirmDrawer(ticket, onConfirm, onExpired)`, `ccApproveProposal(proposal)`, `ccRejectProposal(proposal)`, `ccIsLive(accountMode)`.

- [ ] **Step 1: Write failing approve/reject/preflight route tests**

Add to `tests/test_command_routes.py`:

```python
LIVE_FLAGS = CommandFlags(True, True, "U1234567", 25000.0)


def test_paper_approve_is_single_post_with_expected_version(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/proposals/7/approve",
                    json={"command_id": CMD_ID, "expected_version": 3},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "approve_proposal"
    assert body == {"command_id": CMD_ID, "proposal_id": 7,
                    "expected_version": 3, "preflight_nonce": None,
                    "session_fingerprint": body["session_fingerprint"]}
    assert body["session_fingerprint"]  # opaque, non-empty


def test_live_approve_forwards_nonce_with_same_command_id(gateway):
    client = make_client(gateway, flags=LIVE_FLAGS)
    r = client.post("/api/commands/proposals/7/approve",
                    json={"command_id": CMD_ID, "expected_version": 3,
                          "preflight_nonce": "n-1"},
                    headers=HEADERS)
    assert r.status_code == 202
    assert gateway.calls[0][1]["preflight_nonce"] == "n-1"
    assert gateway.calls[0][1]["command_id"] == CMD_ID


def test_reject_is_immediate_without_expected_version(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/proposals/7/reject",
                    json={"command_id": CMD_ID, "reason": "changed thesis"},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "reject_proposal"
    assert body == {"command_id": CMD_ID, "proposal_id": 7,
                    "reason": "changed thesis"}


def test_preflight_requires_live_commands_enabled(gateway):
    client = make_client(gateway)  # paper-only flags
    r = client.post("/api/preflight",
                    json={"command_id": CMD_ID, "action": "approve_proposal",
                          "params": {"proposal_id": 7}, "expected_version": 3},
                    headers=HEADERS)
    assert r.status_code == 403
    assert r.json()["code"] == "LIVE_COMMANDS_DISABLED"
    assert gateway.calls == []


def test_preflight_returns_ticket_bound_to_session(gateway):
    gateway.ticket = PreflightTicket(
        CMD_ID, "n-9", "2026-07-15T13:42:47Z",
        {"side": "BUY", "instrument": "AAPL", "quantity": 10,
         "notional": 2350.0, "order_type": "MARKET", "latest_price": 235.0,
         "drift_bps": 12.0, "warnings": ["quote is 4s old"],
         "account_id": "U1234567", "account_mode": "live"})
    client = make_client(gateway, flags=LIVE_FLAGS)
    r = client.post("/api/preflight",
                    json={"command_id": CMD_ID, "action": "approve_proposal",
                          "params": {"proposal_id": 7}, "expected_version": 3},
                    headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["nonce"] == "n-9"
    assert r.json()["summary"]["drift_bps"] == 12.0
    sent = gateway.calls[0][1]
    assert sent["session_fingerprint"]
    assert sent["action"] == "approve_proposal"


def test_expired_preflight_maps_to_410(gateway):
    gateway.error = GatewayError("PREFLIGHT_EXPIRED", "nonce expired after 30s",
                                 retryable=True, correlation_id=CMD_ID)
    client = make_client(gateway, flags=LIVE_FLAGS)
    r = client.post("/api/commands/proposals/7/approve",
                    json={"command_id": CMD_ID, "expected_version": 3,
                          "preflight_nonce": "n-old"},
                    headers=HEADERS)
    assert r.status_code == 410
    assert r.json()["retryable"] is True
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --frozen pytest tests/test_command_routes.py -q`

Expected: FAIL with 404s for the approve/reject/preflight routes and `ImportError` for `session_fingerprint`.

- [ ] **Step 3: Add `session_fingerprint` to the session module**

Append to `web/command_center/session.py` (uses that module's existing signing secret; `_session_secret()` is the `[M1-R]` accessor for the loaded `DASHBOARD_SESSION_SECRET`):

```python
def session_fingerprint(session: DashboardSession) -> str:
    """Opaque per-session value the trader binds preflight nonces to.

    Derived, not the raw cookie: leaking it cannot replay the session, and
    the trader never learns browser credentials (spec 9.1 session binding).
    """
    return hmac.new(_session_secret(),
                    f"preflight:{session.session_id}:{session.epoch}".encode(),
                    hashlib.sha256).hexdigest()
```

- [ ] **Step 4: Implement the approve, reject, and preflight routes**

Add to `web/command_center/routes_commands.py` (import `session_fingerprint` alongside `session_csrf_token`):

```python
_PREFLIGHT_ACTIONS = ("approve_proposal", "set_trading_pause",
                      "enable_strategy", "disable_strategy",
                      "update_strategy_params", "cancel_order", "cancel_orders")


class ApproveProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = _COMMAND_ID
    expected_version: int = Field(ge=1)   # proposal entity_revision (spec 6.1)
    preflight_nonce: str | None = None    # required for live; trader enforces


class RejectProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = _COMMAND_ID
    reason: str = Field(default="", max_length=2000)


class PreflightBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = _COMMAND_ID
    action: Literal[_PREFLIGHT_ACTIONS]
    params: dict[str, Any]
    expected_version: int | None = Field(default=None, ge=1)


@router.post("/api/commands/proposals/{pid}/approve")
def approve_proposal(pid: int, body: ApproveProposalBody, request: Request,
                     session: DashboardSession = Depends(require_command_auth)):
    receipt = _gateway(request).execute("approve_proposal", {
        "command_id": body.command_id,
        "proposal_id": pid,
        "expected_version": body.expected_version,
        "preflight_nonce": body.preflight_nonce,
        "session_fingerprint": session_fingerprint(session),
    })
    return _receipt_json(receipt)


@router.post("/api/commands/proposals/{pid}/reject")
def reject_proposal(pid: int, body: RejectProposalBody, request: Request,
                    session: DashboardSession = Depends(require_command_auth)):
    # Risk-reducing: idempotent PENDING->REJECTED CAS server-side; a stale
    # view must never block it, so no expected_version (spec 6.1).
    receipt = _gateway(request).execute("reject_proposal", {
        "command_id": body.command_id,
        "proposal_id": pid,
        "reason": body.reason,
    })
    return _receipt_json(receipt)


@router.post("/api/preflight")
def preflight(body: PreflightBody, request: Request,
              session: DashboardSession = Depends(require_command_auth)):
    flags: CommandFlags = request.app.state.command_flags
    if not flags.live_commands_enabled:
        raise CommandApiError(403, "LIVE_COMMANDS_DISABLED",
                              "live commands are disabled "
                              "(DASHBOARD_LIVE_COMMANDS_ENABLED=false)")
    ticket = _gateway(request).preflight({
        "command_id": body.command_id,
        "action": body.action,
        "params": body.params,
        "expected_version": body.expected_version,
        "session_fingerprint": session_fingerprint(session),
    })
    return {"command_id": ticket.command_id, "nonce": ticket.nonce,
            "expires_at": ticket.expires_at, "summary": ticket.summary}
```

- [ ] **Step 5: Run route tests**

Run: `uv run --frozen pytest tests/test_command_routes.py -q`

Expected: PASS.

- [ ] **Step 6: Add the ceremony JS and the live confirmation drawer**

Append to `web/static/command_center.js`:

```javascript
/* ---- Live two-stage ceremony (spec 9.1) ----
 * One command_id before preflight, reused for confirmation and retries. */

function ccIsLive(accountMode) {
  return String(accountMode || '').toLowerCase() === 'live';
}

async function ccRequestPreflight(commandId, action, params, expectedVersion) {
  const result = await ccPost('/api/preflight', {
    command_id: commandId, action, params,
    expected_version: expectedVersion,
  }, {okStatus: 200});
  if (!result.ok) {
    ccToast('error',
            `Preflight failed: ${result.error.message} (${result.error.code})`);
    return null;
  }
  return result.data;  // {command_id, nonce, expires_at, summary}
}

function ccOpenConfirmDrawer(ticket, onConfirm, onExpired) {
  const d = document.getElementById('cc-confirm-drawer');
  const s = ticket.summary;
  const set = (name, value) => {
    d.querySelector(`[data-field=${name}]`).textContent =
        value === null || value === undefined ? '—' : String(value);
  };
  set('side', s.side);
  set('instrument', s.instrument);
  set('quantity', s.quantity);
  set('notional', s.notional);
  set('order_type', s.order_type);
  set('latest_price', s.latest_price);
  set('drift_bps', s.drift_bps);
  set('account', `${s.account_id} (${String(s.account_mode).toUpperCase()})`);
  const warnings = d.querySelector('[data-field=warnings]');
  warnings.replaceChildren(...(s.warnings || []).map((w) => {
    const li = document.createElement('li');
    li.textContent = w;
    return li;
  }));

  const confirmBtn = d.querySelector('#cc-confirm-button');
  const countdown = d.querySelector('#cc-confirm-countdown');
  confirmBtn.disabled = false;
  const expiresAt = Date.parse(ticket.expires_at);
  const timer = setInterval(() => {
    const left = Math.max(0, Math.round((expiresAt - Date.now()) / 1000));
    countdown.textContent = `${left}s`;
    if (left <= 0) {
      clearInterval(timer);
      confirmBtn.disabled = true;
      countdown.textContent = 'Preflight expired — re-run to confirm';
      if (onExpired) onExpired();
    }
  }, 250);

  confirmBtn.onclick = () => {
    clearInterval(timer);
    d.hidden = true;
    onConfirm(ticket.nonce);
  };
  d.querySelector('#cc-confirm-cancel').onclick = () => {
    clearInterval(timer);
    d.hidden = true;
  };
  d.hidden = false;
}

async function ccRunLiveCeremony(kind, label, action, params,
                                 expectedVersion, submit) {
  if (!ccRequireAvailable(kind)) return;
  const commandId = ccNewCommandId();  // reused across retries
  const run = async () => {
    const ticket = await ccRequestPreflight(commandId, action, params,
                                            expectedVersion);
    if (!ticket) return;
    ccOpenConfirmDrawer(ticket,
        (nonce) => submit(commandId, nonce),
        () => ccToast('warn', `${label}: preflight expired — reopen to retry`));
  };
  await run();
}

/* ---- Proposal approve / reject actions (spec 9.2) ---- */

async function ccApproveProposal(proposal) {
  const expectedVersion = proposal.entity_revision;
  const url = `/api/commands/proposals/${proposal.id}/approve`;
  if (!ccIsLive(proposal.account_mode)) {
    await ccSubmitCommand('approve_proposal', `Approve #${proposal.id}`, url, {
      command_id: ccNewCommandId(),
      expected_version: expectedVersion,
      preflight_nonce: null,
    });
    return;
  }
  await ccRunLiveCeremony('approve_proposal', `Approve #${proposal.id}`,
      'approve_proposal', {proposal_id: proposal.id}, expectedVersion,
      (commandId, nonce) => ccSubmitCommand('approve_proposal',
          `Approve #${proposal.id}`, url, {
            command_id: commandId,
            expected_version: expectedVersion,
            preflight_nonce: nonce,
          }));
}

async function ccRejectProposal(proposal) {
  // Immediate, idempotent, risk-reducing in both modes.
  await ccSubmitCommand('reject_proposal', `Reject #${proposal.id}`,
      `/api/commands/proposals/${proposal.id}/reject`, {
        command_id: ccNewCommandId(),
        reason: '',
      });
}
```

Append to `web/templates/command_center.html`:

```html
<!-- [M1-C] Live confirmation drawer — repeats the authoritative summary -->
<aside id="cc-confirm-drawer" class="cc-drawer cc-drawer-live" hidden>
  <h2>Confirm live command</h2>
  <dl>
    <dt>Side</dt><dd data-field=side></dd>
    <dt>Instrument</dt><dd data-field=instrument></dd>
    <dt>Quantity</dt><dd data-field=quantity></dd>
    <dt>Notional</dt><dd data-field=notional></dd>
    <dt>Order type</dt><dd data-field=order_type></dd>
    <dt>Latest price</dt><dd data-field=latest_price></dd>
    <dt>Drift (bps)</dt><dd data-field=drift_bps></dd>
    <dt>Account</dt><dd data-field=account></dd>
  </dl>
  <ul data-field=warnings class="cc-warnings"></ul>
  <p>Nonce expires in <span id="cc-confirm-countdown"></span></p>
  <button id="cc-confirm-button" type="button">Confirm</button>
  <button id="cc-confirm-cancel" type="button">Cancel</button>
</aside>
```

- [ ] **Step 7: Run the route and gateway suites**

Run: `uv run --frozen pytest tests/test_command_routes.py tests/test_command_gateway.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add web/command_center/routes_commands.py web/command_center/session.py web/static/command_center.js web/templates/command_center.html tests/test_command_routes.py
git commit -m "feat(m1-c): add approve reject and live preflight ceremony"
```

### Task 5: Order cancel and cancel-all with classification ceremony

**Files:**
- Modify: `web/command_center/routes_commands.py` (cancel routes)
- Modify: `web/static/command_center.js` (classification + cancel actions + cancel-all confirmation)
- Modify: `web/templates/command_center.html` (protective-cancel and cancel-all dialogs)
- Modify: `tests/test_command_routes.py`

**Interfaces:**
- Consumes: order entities from `CC.entities('order')` (`[M1-R]` state) whose payloads carry `order_group_id` and `leg_role` (`[M1-F2]`); positions via `CC.entity('position', id)`.
- Produces routes: `POST /api/commands/orders/{order_entity_id}/cancel`, `POST /api/commands/orders/cancel-all`.
- Produces JS: `ccClassifyOrder(order) -> 'entry' | 'protective'`, `ccCancelOrder(order)`, `ccCancelAll()`.

- [ ] **Step 1: Write failing cancel route tests**

Add to `tests/test_command_routes.py`:

```python
def test_cancel_single_order_forwards_entity_id(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/orders/grp-9:entry/cancel",
                    json={"command_id": CMD_ID}, headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "cancel_order"
    assert body == {"command_id": CMD_ID, "order_entity_id": "grp-9:entry",
                    "preflight_nonce": None,
                    "session_fingerprint": body["session_fingerprint"]}


def test_protective_cancel_forwards_nonce(gateway):
    client = make_client(gateway, flags=LIVE_FLAGS)
    r = client.post("/api/commands/orders/grp-9:stop/cancel",
                    json={"command_id": CMD_ID, "preflight_nonce": "n-1"},
                    headers=HEADERS)
    assert r.status_code == 202
    assert gateway.calls[0][1]["preflight_nonce"] == "n-1"


def test_cancel_all_sends_every_order_under_one_command(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/orders/cancel-all",
                    json={"command_id": CMD_ID,
                          "order_entity_ids": ["grp-9:entry", "grp-9:stop"]},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "cancel_orders"
    assert body["order_entity_ids"] == ["grp-9:entry", "grp-9:stop"]
    # one correlation id: the coordinator expands per-order commands under it
    assert body["command_id"] == CMD_ID


def test_cancel_all_requires_at_least_one_order(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/orders/cancel-all",
                    json={"command_id": CMD_ID, "order_entity_ids": []},
                    headers=HEADERS)
    assert r.status_code == 422
    assert gateway.calls == []


def test_terminal_order_cancel_reports_authoritative_state(gateway):
    gateway.error = GatewayError("COMMAND_REJECTED",
                                 "order grp-9:entry already FILLED; no-op",
                                 retryable=False, correlation_id=CMD_ID)
    client = make_client(gateway)
    r = client.post("/api/commands/orders/grp-9:entry/cancel",
                    json={"command_id": CMD_ID}, headers=HEADERS)
    assert r.status_code == 409
    assert "already FILLED" in r.json()["message"]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --frozen pytest tests/test_command_routes.py -q`

Expected: FAIL with 404 for the cancel routes.

- [ ] **Step 3: Implement the cancel routes**

Add to `web/command_center/routes_commands.py`:

```python
class CancelOrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = _COMMAND_ID
    preflight_nonce: str | None = None  # protective cancel on live accounts


class CancelAllBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = _COMMAND_ID
    order_entity_ids: list[str] = Field(min_length=1, max_length=200)
    preflight_nonce: str | None = None


@router.post("/api/commands/orders/cancel-all")
def cancel_all_orders(body: CancelAllBody, request: Request,
                      session: DashboardSession = Depends(require_command_auth)):
    # The coordinator expands to per-order commands under this command_id
    # as the single correlation id (spec 9.7).
    receipt = _gateway(request).execute("cancel_orders", {
        "command_id": body.command_id,
        "order_entity_ids": body.order_entity_ids,
        "preflight_nonce": body.preflight_nonce,
        "session_fingerprint": session_fingerprint(session),
    })
    return _receipt_json(receipt)


@router.post("/api/commands/orders/{order_entity_id}/cancel")
def cancel_order(order_entity_id: str, body: CancelOrderBody, request: Request,
                 session: DashboardSession = Depends(require_command_auth)):
    # Classification-aware ceremony happens client-side for UX; the
    # coordinator re-derives the classification from durable order-group
    # state and enforces the nonce requirement itself (spec 9.7).
    receipt = _gateway(request).execute("cancel_order", {
        "command_id": body.command_id,
        "order_entity_id": order_entity_id,
        "preflight_nonce": body.preflight_nonce,
        "session_fingerprint": session_fingerprint(session),
    })
    return _receipt_json(receipt)
```

Route order matters: register `cancel-all` before the parameterized path (as above) so `"cancel-all"` is never captured as an `order_entity_id`.

- [ ] **Step 4: Run route tests**

Run: `uv run --frozen pytest tests/test_command_routes.py -q`

Expected: PASS.

- [ ] **Step 5: Add classification-aware cancel JS and dialogs**

Append to `web/static/command_center.js`:

```javascript
/* ---- Working-order cancel (spec 9.7) ----
 * Entry cancel is risk-reducing: immediate single POST.
 * Protective cancel removes protection: risk-increasing ceremony that
 * names the position left unprotected. Unknown classification is
 * treated as protective. */

const CC_ENTRY_ROLES = new Set(['entry', 'parent']);

function ccClassifyOrder(order) {
  const role = String(order.leg_role || '').toLowerCase();
  return CC_ENTRY_ROLES.has(role) ? 'entry' : 'protective';
}

function ccUnprotectedPositionLabel(order) {
  const position = CC.entity('position',
                             `${order.account_id}:${order.conid}`);
  const symbol = order.symbol || (position && position.symbol) || order.conid;
  const qty = position ? position.quantity : '?';
  return `${symbol} (${order.account_id}, qty ${qty})`;
}

async function ccCancelOrder(order) {
  const url = `/api/commands/orders/${encodeURIComponent(order.order_entity_id)}/cancel`;
  const label = `Cancel order ${order.order_entity_id}`;

  if (ccClassifyOrder(order) === 'entry') {
    // Risk-reducing: immediate, idempotent, both modes.
    await ccSubmitCommand('cancel_entry', label, url,
        {command_id: ccNewCommandId(), preflight_nonce: null});
    return;
  }

  const positionLabel = ccUnprotectedPositionLabel(order);
  if (!ccIsLive(order.account_mode)) {
    // Paper ceremony: one authenticated POST, but an explicit confirm
    // dialog that names the position left unprotected.
    const ok = window.confirm(
        `Cancel PROTECTIVE order ${order.order_entity_id}?\n` +
        `This leaves ${positionLabel} unprotected.`);
    if (!ok) return;
    await ccSubmitCommand('cancel_protective', label, url,
        {command_id: ccNewCommandId(), preflight_nonce: null});
    return;
  }

  // Live: signed two-stage preflight; drawer summary + warning names the
  // unprotected position (the coordinator includes it in summary.warnings).
  await ccRunLiveCeremony('cancel_protective', label, 'cancel_order',
      {order_entity_id: order.order_entity_id}, null,
      (commandId, nonce) => ccSubmitCommand('cancel_protective', label, url,
          {command_id: commandId, preflight_nonce: nonce}));
}

/* ---- Cancel all: one confirmation listing every order + classification,
 * expanded server-side to per-order commands under one correlation id. */

function ccOpenCancelAllDialog() {
  const orders = CC.entities('order').filter((o) => o.is_working);
  if (orders.length === 0) {
    ccToast('warn', 'No working orders to cancel');
    return;
  }
  const d = document.getElementById('cc-cancel-all-dialog');
  const list = d.querySelector('#cc-cancel-all-list');
  list.replaceChildren(...orders.map((o) => {
    const li = document.createElement('li');
    const cls = ccClassifyOrder(o);
    li.textContent = `${o.order_entity_id} — ${o.symbol || o.conid} ` +
        `${o.side || ''} ${o.quantity || ''} [${cls.toUpperCase()}]` +
        (cls === 'protective'
            ? ` — leaves ${ccUnprotectedPositionLabel(o)} unprotected` : '');
    return li;
  }));
  d.dataset.orderIds = JSON.stringify(orders.map((o) => o.order_entity_id));
  d.dataset.hasProtective =
      String(orders.some((o) => ccClassifyOrder(o) === 'protective'));
  d.dataset.hasLive =
      String(orders.some((o) => ccIsLive(o.account_mode)));
  d.hidden = false;
}

document.getElementById('cc-cancel-all-confirm').addEventListener('click',
    async () => {
      const d = document.getElementById('cc-cancel-all-dialog');
      d.hidden = true;
      const orderIds = JSON.parse(d.dataset.orderIds || '[]');
      const riskIncreasing = d.dataset.hasProtective === 'true' &&
                             d.dataset.hasLive === 'true';
      const submit = (commandId, nonce) => ccSubmitCommand('cancel_protective',
          `Cancel all (${orderIds.length} orders)`,
          '/api/commands/orders/cancel-all', {
            command_id: commandId,
            order_entity_ids: orderIds,
            preflight_nonce: nonce,
          });
      if (riskIncreasing) {
        await ccRunLiveCeremony('cancel_protective',
            `Cancel all (${orderIds.length} orders)`, 'cancel_orders',
            {order_entity_ids: orderIds}, null, submit);
      } else {
        await submit(ccNewCommandId(), null);
      }
    });
document.getElementById('cc-cancel-all-abort').addEventListener('click',
    () => { document.getElementById('cc-cancel-all-dialog').hidden = true; });
```

Append to `web/templates/command_center.html`:

```html
<!-- [M1-C] Cancel-all confirmation: every order with its classification -->
<aside id="cc-cancel-all-dialog" class="cc-drawer" hidden>
  <h2>Cancel all working orders</h2>
  <p>The following orders will be cancelled under one correlation id.
     Protective legs leave their position unprotected:</p>
  <ul id="cc-cancel-all-list"></ul>
  <button id="cc-cancel-all-confirm" type="button"
          data-cc-command="cancel_protective">Cancel all listed orders</button>
  <button id="cc-cancel-all-abort" type="button">Keep orders</button>
</aside>
```

- [ ] **Step 6: Run the route suite**

Run: `uv run --frozen pytest tests/test_command_routes.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add web/command_center/routes_commands.py web/static/command_center.js web/templates/command_center.html tests/test_command_routes.py
git commit -m "feat(m1-c): add classification-aware order cancel surfaces"
```

### Task 6: Strategy control and pause/resume surfaces

**Files:**
- Modify: `web/command_center/routes_commands.py` (strategy + pause routes)
- Modify: `web/static/command_center.js` (strategy enable/disable/params + pause/resume actions)
- Modify: `web/templates/command_center.html` (params drawer, pause control, resume ceremony affordance)
- Modify: `tests/test_command_routes.py`

**Interfaces:**
- Consumes: strategy entities from `CC.entities('strategy')` whose payloads carry `control_revision`, `is_running`, `account_mode`, and the exposure hint `owns_exposure` (`[M1-R]`/`[M1-F3]`); the pause entity from `CC.entity('trading_control', account_id)` with `new_exposure_paused` and `revision`.
- Consumes typed methods `enable_strategy`, `disable_strategy`, `update_strategy_params`, `set_trading_pause` and the `[M1-F3]` request contracts: strategy commands carry `command_id`, `strategy_name`, `expected_version` (the strategy's `control_revision`), and — for params — `params`; `SetTradingPauseRequest(command_id, paused, expected_version, reason)` supplies no account (the coordinator pins its configured account).
- Produces routes: `POST /api/commands/strategies/{strategy_name}/enable`, `POST /api/commands/strategies/{strategy_name}/disable`, `POST /api/commands/strategies/{strategy_name}/params`, `POST /api/commands/pause`.
- Produces JS: `ccEnableStrategy(strategy)`, `ccDisableStrategy(strategy)`, `ccUpdateStrategyParams(strategy, params)`, `ccSetPause(accountId, paused, revision)`.

- [ ] **Step 1: Write failing strategy and pause route tests**

Add to `tests/test_command_routes.py`:

```python
def test_strategy_enable_forwards_control_revision(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/strategies/smi_crossover/enable",
                    json={"command_id": CMD_ID, "expected_version": 4},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "enable_strategy"
    assert body == {"command_id": CMD_ID, "strategy_name": "smi_crossover",
                    "expected_version": 4, "preflight_nonce": None,
                    "session_fingerprint": body["session_fingerprint"]}


def test_strategy_params_forwards_typed_params(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/strategies/smi_crossover/params",
                    json={"command_id": CMD_ID, "expected_version": 4,
                          "params": {"EMA_PERIOD": 15}},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "update_strategy_params"
    assert body["params"] == {"EMA_PERIOD": 15}
    assert body["expected_version"] == 4


def test_strategy_params_rejects_non_object_params(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/strategies/smi_crossover/params",
                    json={"command_id": CMD_ID, "expected_version": 4,
                          "params": [1, 2, 3]},
                    headers=HEADERS)
    assert r.status_code == 422           # params must be a JSON object
    assert gateway.calls == []


def test_live_strategy_disable_requires_nonce(gateway):
    client = make_client(gateway, flags=LIVE_FLAGS)
    r = client.post("/api/commands/strategies/smi_crossover/disable",
                    json={"command_id": CMD_ID, "expected_version": 4,
                          "preflight_nonce": "n-7"},
                    headers=HEADERS)
    assert r.status_code == 202
    assert gateway.calls[0][1]["preflight_nonce"] == "n-7"


def test_pause_sets_absolute_boolean_without_account(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/pause",
                    json={"command_id": CMD_ID, "paused": True,
                          "reason": "operator hold"},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "set_trading_pause"
    assert body["paused"] is True
    assert "account_id" not in body      # coordinator pins its configured account
    assert body["expected_version"] is None  # pausing is idempotent from a stale view


def test_resume_forwards_exact_revision(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/pause",
                    json={"command_id": CMD_ID, "paused": False,
                          "expected_version": 12, "reason": "resume"},
                    headers=HEADERS)
    assert r.status_code == 202
    assert gateway.calls[0][1]["expected_version"] == 12


def test_resume_without_revision_is_rejected(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/pause",
                    json={"command_id": CMD_ID, "paused": False,
                          "reason": "resume"},
                    headers=HEADERS)
    assert r.status_code == 422           # resume is CAS: an exact revision is mandatory
    assert gateway.calls == []
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --frozen pytest tests/test_command_routes.py -q`

Expected: FAIL with 404 for the strategy and pause routes.

- [ ] **Step 3: Implement the strategy and pause routes**

Add to `web/command_center/routes_commands.py`:

```python
from pydantic import model_validator


class StrategyControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = _COMMAND_ID
    expected_version: int = Field(ge=0)   # the strategy's control_revision (CAS)
    preflight_nonce: str | None = None    # live enable/disable ceremony


class StrategyParamsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = _COMMAND_ID
    expected_version: int = Field(ge=0)
    params: dict[str, Any] = Field(min_length=1)  # a JSON object, never a list
    preflight_nonce: str | None = None


class SetPauseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = _COMMAND_ID
    paused: bool
    expected_version: int | None = None   # required for resume, ignored for pause
    reason: str = Field(min_length=1, max_length=200)
    preflight_nonce: str | None = None

    @model_validator(mode="after")
    def _resume_requires_revision(self) -> "SetPauseBody":
        # Setting paused=false increases risk (spec 9.4): it is a compare-and-set
        # against the exact current revision. Pausing is idempotent even from a
        # stale view, so it carries no revision.
        if self.paused is False and self.expected_version is None:
            raise ValueError("resume requires the exact current revision")
        return self


@router.post("/api/commands/strategies/{strategy_name}/enable")
def enable_strategy(strategy_name: str, body: StrategyControlBody, request: Request,
                    session: DashboardSession = Depends(require_command_auth)):
    receipt = _gateway(request).execute("enable_strategy", {
        "command_id": body.command_id,
        "strategy_name": strategy_name,
        "expected_version": body.expected_version,
        "preflight_nonce": body.preflight_nonce,
        "session_fingerprint": session_fingerprint(session),
    })
    return _receipt_json(receipt)


@router.post("/api/commands/strategies/{strategy_name}/disable")
def disable_strategy(strategy_name: str, body: StrategyControlBody, request: Request,
                     session: DashboardSession = Depends(require_command_auth)):
    # Exposure-ownership validation is the coordinator's (spec 9.3); the web
    # process never decides whether a disable orphans an exit.
    receipt = _gateway(request).execute("disable_strategy", {
        "command_id": body.command_id,
        "strategy_name": strategy_name,
        "expected_version": body.expected_version,
        "preflight_nonce": body.preflight_nonce,
        "session_fingerprint": session_fingerprint(session),
    })
    return _receipt_json(receipt)


@router.post("/api/commands/strategies/{strategy_name}/params")
def update_strategy_params(strategy_name: str, body: StrategyParamsBody,
                           request: Request,
                           session: DashboardSession = Depends(require_command_auth)):
    receipt = _gateway(request).execute("update_strategy_params", {
        "command_id": body.command_id,
        "strategy_name": strategy_name,
        "expected_version": body.expected_version,
        "params": body.params,
        "preflight_nonce": body.preflight_nonce,
        "session_fingerprint": session_fingerprint(session),
    })
    return _receipt_json(receipt)


@router.post("/api/commands/pause")
def set_trading_pause(body: SetPauseBody, request: Request,
                      session: DashboardSession = Depends(require_command_auth)):
    # The account is the coordinator's configured account, never request-supplied
    # (spec 9.4). Pausing is immediate/idempotent; resume was validated as CAS above.
    receipt = _gateway(request).execute("set_trading_pause", {
        "command_id": body.command_id,
        "paused": body.paused,
        "expected_version": body.expected_version,
        "reason": body.reason,
        "preflight_nonce": body.preflight_nonce,
        "session_fingerprint": session_fingerprint(session),
    })
    return _receipt_json(receipt)
```

`params` uses `dict[str, Any]`, so a JSON list or scalar fails validation with 422 before any forward — the coordinator still re-validates every parameter against the strategy's versioned schema, allowed types, and finite ranges (spec §9.3); the route only guarantees the shape is an object.

- [ ] **Step 4: Run route tests**

Run: `uv run --frozen pytest tests/test_command_routes.py -q`

Expected: PASS.

- [ ] **Step 5: Add strategy and pause JS actions**

Append to `web/static/command_center.js`:

```javascript
/* ---- Strategy control (spec 9.3) ----
 * Paper enable/disable/params: one authenticated POST with a compare-and-set
 * control_revision. Live enable/disable and every live parameter change require
 * the signed two-stage before/after preflight. Disable exposure-ownership
 * validation is the coordinator's; the client only carries the revision. */

async function ccEnableStrategy(strategy) {
  const url = `/api/commands/strategies/${encodeURIComponent(strategy.name)}/enable`;
  const label = `Enable ${strategy.name}`;
  const revision = strategy.control_revision;
  if (!ccIsLive(strategy.account_mode)) {
    await ccSubmitCommand('enable_strategy', label, url,
        {command_id: ccNewCommandId(), expected_version: revision,
         preflight_nonce: null});
    return;
  }
  await ccRunLiveCeremony('enable_strategy', label, 'enable_strategy',
      {strategy_name: strategy.name}, revision,
      (commandId, nonce) => ccSubmitCommand('enable_strategy', label, url,
          {command_id: commandId, expected_version: revision, preflight_nonce: nonce}));
}

async function ccDisableStrategy(strategy) {
  const url = `/api/commands/strategies/${encodeURIComponent(strategy.name)}/disable`;
  const label = `Disable ${strategy.name}`;
  const revision = strategy.control_revision;
  const warn = strategy.owns_exposure
      ? `\n${strategy.name} may own an open exit — the coordinator confirms a ` +
        `remaining owner or refuses.` : '';
  if (!ccIsLive(strategy.account_mode)) {
    if (strategy.owns_exposure &&
        !window.confirm(`Disable ${strategy.name}?${warn}`)) return;
    await ccSubmitCommand('disable_strategy', label, url,
        {command_id: ccNewCommandId(), expected_version: revision,
         preflight_nonce: null});
    return;
  }
  await ccRunLiveCeremony('disable_strategy', label, 'disable_strategy',
      {strategy_name: strategy.name}, revision,
      (commandId, nonce) => ccSubmitCommand('disable_strategy', label, url,
          {command_id: commandId, expected_version: revision, preflight_nonce: nonce}));
}

async function ccUpdateStrategyParams(strategy, params) {
  const url = `/api/commands/strategies/${encodeURIComponent(strategy.name)}/params`;
  const label = `Update ${strategy.name} params`;
  const revision = strategy.control_revision;
  // Any parameter whose risk effect is unspecified is risk-increasing, so live
  // params always take the ceremony; paper takes the single CAS POST.
  if (!ccIsLive(strategy.account_mode)) {
    await ccSubmitCommand('update_strategy_params', label, url,
        {command_id: ccNewCommandId(), expected_version: revision, params,
         preflight_nonce: null});
    return;
  }
  await ccRunLiveCeremony('update_strategy_params', label, 'update_strategy_params',
      {strategy_name: strategy.name, params}, revision,
      (commandId, nonce) => ccSubmitCommand('update_strategy_params', label, url,
          {command_id: commandId, expected_version: revision, params,
           preflight_nonce: nonce}));
}

/* ---- Pause new trading (spec 9.4) ----
 * paused=true is risk-reducing: immediate single POST, no revision, both modes.
 * paused=false (resume) is risk-increasing: paper sends the exact current
 * revision in one POST; live runs the signed ceremony. */

async function ccSetPause(accountId, paused, revision) {
  const label = paused ? `Pause new trading (${accountId})`
                       : `Resume new trading (${accountId})`;
  const url = '/api/commands/pause';
  const reason = paused ? 'operator pause' : 'operator resume';
  const control = CC.entity('trading_control', accountId) || {};
  if (paused) {
    await ccSubmitCommand('set_trading_pause', label, url,
        {command_id: ccNewCommandId(), paused: true, expected_version: null,
         reason, preflight_nonce: null});
    return;
  }
  if (!ccIsLive(control.account_mode)) {
    await ccSubmitCommand('set_trading_pause', label, url,
        {command_id: ccNewCommandId(), paused: false, expected_version: revision,
         reason, preflight_nonce: null});
    return;
  }
  await ccRunLiveCeremony('set_trading_pause', label, 'set_trading_pause',
      {paused: false}, revision,
      (commandId, nonce) => ccSubmitCommand('set_trading_pause', label, url,
          {command_id: commandId, paused: false, expected_version: revision,
           reason, preflight_nonce: nonce}));
}
```

Append to `web/templates/command_center.html`:

```html
<!-- [M1-C] Strategy parameter editor: typed fields, CAS on control_revision -->
<aside id="cc-strategy-params-dialog" class="cc-drawer" hidden>
  <h2>Edit <span id="cc-params-strategy-name"></span> parameters</h2>
  <p class="cc-hint">Server validates every value against the strategy's
     versioned schema; unspecified risk effects are treated as risk-increasing.</p>
  <form id="cc-strategy-params-form"></form>
  <button id="cc-params-apply" type="button"
          data-cc-command="update_strategy_params">Apply</button>
  <button id="cc-params-cancel" type="button">Cancel</button>
</aside>

<!-- [M1-C] Pause control: absolute set, resume carries the current revision -->
<div id="cc-pause-control" class="cc-pause">
  <button id="cc-pause-toggle" type="button"
          data-cc-command="set_trading_pause">Pause new trading</button>
  <span id="cc-pause-state" class="cc-pause-state"></span>
</div>
```

- [ ] **Step 6: Run the route suite**

Run: `uv run --frozen pytest tests/test_command_routes.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add web/command_center/routes_commands.py web/static/command_center.js web/templates/command_center.html tests/test_command_routes.py
git commit -m "feat(m1-c): add strategy control and pause/resume surfaces"
```

### Task 7: Disable overlapping legacy mutations and the `[M1-C]` security gate

**Files:**
- Modify: `web/app.py` (gate the legacy trading-mutation routes when commands are enabled)
- Create: `tests/test_command_security_gate.py`
- Modify: `tests/test_command_routes.py`

**Interfaces:**
- Consumes: the `[M1-R]` session/CSRF/origin stack (`require_command_auth`), the command feature flags (`CommandFlags`), and the legacy FastAPI handlers `approve`, `reject`, `enable_strategy`, `disable_strategy`, and the strategy-params handler in `web/app.py` (`web/app.py:389-489`).
- Produces: `LEGACY_TRADING_MUTATION_PATHS: frozenset[str]` — the legacy POST paths whose authority moves to `[M1-C]` — and a `require_legacy_mutations_enabled` FastAPI dependency that returns `409 Conflict` with the stable error envelope pointing at the command center whenever `DASHBOARD_COMMANDS_ENABLED` is true. Watchlist and `strategies/deploy` routes are out of scope here (their migration is `[COMPAT]`).

- [ ] **Step 1: Write the failing security-gate matrix**

Create `tests/test_command_security_gate.py`. This is the plan's guarantee that no command route ever renders success from an HTTP acknowledgement, that authentication is mandatory in both modes, and that live routes cannot arm without an exact account and a notional ceiling:

```python
import pytest

from web.command_center.routes_commands import CommandFlags
from tests.test_command_routes import (
    CMD_ID, HEADERS, LIVE_FLAGS, make_client, FakeGateway,
)

# Every mutating command route, with a minimally valid body for each.
COMMAND_ROUTES = [
    ("POST", "/api/commands/proposals", {"symbol": "AAPL", "action": "BUY",
                                          "command_id": CMD_ID}),
    ("POST", "/api/commands/proposals/7/approve",
     {"command_id": CMD_ID, "expected_version": 3}),
    ("POST", "/api/commands/proposals/7/reject",
     {"command_id": CMD_ID, "reason": "no"}),
    ("POST", "/api/commands/positions/U1:265598/close", {"command_id": CMD_ID}),
    ("POST", "/api/commands/orders/grp-9:entry/cancel", {"command_id": CMD_ID}),
    ("POST", "/api/commands/orders/cancel-all",
     {"command_id": CMD_ID, "order_entity_ids": ["grp-9:entry"]}),
    ("POST", "/api/commands/strategies/smi/enable",
     {"command_id": CMD_ID, "expected_version": 4}),
    ("POST", "/api/commands/strategies/smi/disable",
     {"command_id": CMD_ID, "expected_version": 4}),
    ("POST", "/api/commands/strategies/smi/params",
     {"command_id": CMD_ID, "expected_version": 4, "params": {"EMA_PERIOD": 15}}),
    ("POST", "/api/commands/pause",
     {"command_id": CMD_ID, "paused": True, "reason": "hold"}),
]


@pytest.mark.parametrize("method,path,body", COMMAND_ROUTES)
def test_every_command_route_requires_session(method, path, body):
    gateway = FakeGateway()
    client = make_client(gateway, session=None)          # no authenticated session
    r = client.request(method, path, json=body, headers=HEADERS)
    assert r.status_code == 401
    assert gateway.calls == []


@pytest.mark.parametrize("method,path,body", COMMAND_ROUTES)
def test_every_command_route_requires_csrf(method, path, body):
    gateway = FakeGateway()
    client = make_client(gateway)
    headers = {k: v for k, v in HEADERS.items() if k != "X-CSRF-Token"}
    r = client.request(method, path, json=body, headers=headers)
    assert r.status_code == 403
    assert gateway.calls == []


@pytest.mark.parametrize("method,path,body", COMMAND_ROUTES)
def test_every_command_route_rejects_foreign_origin(method, path, body):
    gateway = FakeGateway()
    client = make_client(gateway)
    headers = {**HEADERS, "Origin": "http://evil.example"}
    r = client.request(method, path, json=body, headers=headers)
    assert r.status_code == 403
    assert gateway.calls == []


@pytest.mark.parametrize("method,path,body", COMMAND_ROUTES)
def test_every_command_route_is_404_when_commands_disabled(method, path, body):
    gateway = FakeGateway()
    client = make_client(gateway, flags=CommandFlags(False, False, None, None))
    r = client.request(method, path, json=body, headers=HEADERS)
    assert r.status_code == 404          # the router is not mounted at all
    assert gateway.calls == []


@pytest.mark.parametrize("method,path,body", COMMAND_ROUTES)
def test_every_command_route_forbids_extra_fields(method, path, body):
    gateway = FakeGateway()
    client = make_client(gateway)
    r = client.request(method, path, json={**body, "skip_risk_gate": True},
                       headers=HEADERS)
    assert r.status_code == 422          # extra="forbid" on every request model
    assert gateway.calls == []


@pytest.mark.parametrize("method,path,body", COMMAND_ROUTES)
def test_202_never_carries_a_success_flag(method, path, body):
    gateway = FakeGateway()                              # returns a SUBMITTED receipt
    client = make_client(gateway)
    r = client.request(method, path, json=body, headers=HEADERS)
    assert r.status_code == 202
    payload = r.json()
    assert "success" not in payload and payload.get("state") != "RESOLVED"
    assert payload["command_id"] == CMD_ID              # correlation only


def test_live_commands_require_exact_account_and_notional_at_startup():
    # An inconsistent live configuration fails closed at construction, never
    # falling back to a permissive default (spec 11).
    with pytest.raises(ValueError):
        CommandFlags(commands_enabled=True, live_enabled=True,
                     live_account_id=None, live_max_notional=25000.0).validate()
    with pytest.raises(ValueError):
        CommandFlags(commands_enabled=True, live_enabled=True,
                     live_account_id="U1234567", live_max_notional=None).validate()
    ok = CommandFlags(True, True, "U1234567", 25000.0)
    ok.validate()                                        # no raise


def test_legacy_approve_is_409_when_commands_enabled(monkeypatch):
    from web import app as webapp
    client = webapp.make_test_client(commands_enabled=True)
    r = client.post("/proposals/7/approve",
                    data={"csrf_token": "test-csrf"}, headers=HEADERS)
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "MOVED_TO_COMMAND_CENTER"
    assert "/command-center" in body["message"]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --frozen pytest tests/test_command_security_gate.py -q`

Expected: FAIL — `make_client(..., session=None)` and `CommandFlags.validate()` do not yet exist, and the legacy `/proposals/7/approve` route still returns its legacy status. Extend the `tests/test_command_routes.py` fixtures with `FakeGateway`, the `session=` and `flags=` knobs already used above, and a `SUBMITTED`-receipt default so this module can import them.

- [ ] **Step 3: Implement `CommandFlags.validate` and disable the overlapping legacy mutations**

In `web/command_center/routes_commands.py`, give `CommandFlags` an explicit fail-closed validator (called once at startup by the `[M1-C]` mount from Task 1):

```python
@dataclass(frozen=True)
class CommandFlags:
    commands_enabled: bool
    live_enabled: bool
    live_account_id: str | None
    live_max_notional: float | None

    def validate(self) -> "CommandFlags":
        if self.live_enabled and not self.commands_enabled:
            raise ValueError("DASHBOARD_LIVE_COMMANDS_ENABLED requires "
                             "DASHBOARD_COMMANDS_ENABLED")
        if self.live_enabled:
            if not self.live_account_id:
                raise ValueError("live commands require an exact "
                                 "DASHBOARD_LIVE_ACCOUNT_ID")
            if self.live_max_notional is None or self.live_max_notional <= 0:
                raise ValueError("live commands require a positive "
                                 "DASHBOARD_LIVE_MAX_ORDER_NOTIONAL")
        return self
```

In `web/app.py`, add the single-authority guard and attach it to the legacy trading-mutation handlers whose authority now lives in `[M1-C]`:

```python
LEGACY_TRADING_MUTATION_PATHS = frozenset({
    "/proposals/{pid}/approve",
    "/proposals/{pid}/reject",
    "/strategies/{name}/enable",
    "/strategies/{name}/disable",
    "/strategies/{name}/params",
})


def require_legacy_mutations_enabled(request: Request) -> None:
    # When the command center owns mutations, the legacy surface must not be a
    # second writer (spec 14 step 4). Reads stay; these POSTs fail closed with a
    # pointer to the command center.
    if request.app.state.command_flags.commands_enabled:
        raise HTTPException(status_code=409, detail={
            "code": "MOVED_TO_COMMAND_CENTER",
            "message": "This action moved to /command-center; the legacy "
                       "dashboard is read-only while commands are enabled.",
            "retryable": False,
            "correlation_id": None,
        })
```

Add `dependencies=[Depends(require_legacy_mutations_enabled)]` to each of the five legacy routes at `web/app.py:389-489` (watchlist and `strategies/deploy` are untouched — `[COMPAT]` migrates them). Register a small exception handler so the `HTTPException(detail=...)` dict serializes as the stable `{code, message, retryable, correlation_id}` envelope rather than the default `{"detail": ...}`. Expose `web.app.make_test_client(commands_enabled=...)` used by the gate test, wiring `app.state.command_flags` from a `CommandFlags(...).validate()`.

- [ ] **Step 4: Run the security gate**

Run: `uv run --frozen pytest tests/test_command_security_gate.py tests/test_command_routes.py -q`

Expected: PASS — every command route enforces session, CSRF, origin, feature-flag mounting, `extra="forbid"`, and the 202-is-not-success contract; live flags fail closed without an exact account and a notional ceiling; and the overlapping legacy mutations return `409` the moment commands are enabled.

- [ ] **Step 5: Run the `[M1-C]` integration gate**

Run: `uv run --frozen pytest tests/test_command_gateway.py tests/test_command_routes.py tests/test_command_security_gate.py tests/test_preflight_ceremony.py -q`

Expected: PASS — the gateway's own connection/timeout/serialization contract, every command route (proposal create/approve/reject, position close, order cancel/cancel-all, strategy enable/disable/params, pause/resume), the security matrix, and the two-stage live ceremony all hold together.

Then run the canonical full suite to prove no regression in the legacy surface or anywhere else:

Run: `uv run --frozen pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py`

Expected: PASS with no new failures or warnings introduced by `[M1-C]`. The Playwright command-drive browser test lives in `[M1-R]`'s browser gate and is extended there with a paper approve/cancel happy path once this router is mounted.

- [ ] **Step 6: Commit**

```bash
git add web/app.py web/command_center/routes_commands.py tests/test_command_security_gate.py tests/test_command_routes.py
git commit -m "feat(m1-c): single-authority legacy lockout and command security gate"
```

