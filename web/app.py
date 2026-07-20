"""MMR web dashboard.

Renders per-currency cash, positions + P&L, strategy state (deployed and
on-disk), and trade proposals. Mutations: proposal approve / reject,
strategy enable / disable, and live strategy-param editing (persisted +
hot-swapped via update_strategy_params RPC). Everything else is read-only.

Runs *inside* the mmr container: it needs the proposals DuckDB (in the
``mmr_db_data`` named volume) plus the trader_service ZMQ RPC. Launched by
pycron as ``python3 -m web.app``; listens on ``0.0.0.0:7424`` (mapped to the
host as ``127.0.0.1:7424``).

Design notes:
- The MMR SDK's RPC client uses a *synchronous* ZMQ socket guarded by its own
  internal ``threading.Lock`` (``clientserver.RPCClient``), so a single shared
  connection is safe across FastAPI's threadpool.
- Route handlers are plain ``def`` (not ``async def``) so FastAPI runs them in
  its threadpool. The SDK internally calls ``asyncio.run()``, which would raise
  if invoked from inside an already-running event loop — the threadpool avoids
  that.
- Each dashboard section fetches independently and degrades to an error banner
  if trader_service is unreachable, so a service blip never blanks the page.
  Proposals are local DuckDB and keep working even when trader_service is down.
"""
from __future__ import annotations

import contextlib
import html as _html
import logging
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

import markdown as _markdown
import pandas as pd
import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from trader.operations.health import build_health_payload
from trader.strategy.inspect import scan_strategies
from web.command_center import (
    CommandCenter,
    CommandCenterConfig,
    GRACEFUL_SHUTDOWN_SECONDS,
    _assert_single_worker,
)
from web.command_center.flags import CommandFlags, load_command_flags
from web.command_center.health import create_health_router
from web.command_center.routes_commands import (
    _check_origin,
    install_command_routes,
    require_session,
)
from web.command_center.routes_read import create_read_router
from web.command_center.session import (
    SESSION_COOKIE,
    CredentialConfigError,
    DashboardCredentials,
    SessionSecurityMiddleware,
    create_session_router,
)

logger = logging.getLogger('web')

# [M1-C] Command feature flags (spec Section 10): both DASHBOARD_COMMANDS_
# ENABLED and DASHBOARD_LIVE_COMMANDS_ENABLED default false. Loaded HERE, at
# module import time (not lazily inside create_app/a request), so that an
# inconsistent configuration (e.g. live enabled without an exact account id
# or a positive finite notional cap) raises CommandFlagsError and kills the
# process before it ever binds a socket -- fail-closed, no permissive
# fallback. `python3 -m web.app` imports this module to build the
# module-level `app` below, so a bad config aborts that import outright;
# `create_app()` also stamps the already-validated flags onto
# `app.state.command_flags` for every app instance (including test-built
# ones via `create_app(stub_cc)`), since only THIS module-level load is
# guaranteed to run once, at import.
_COMMAND_FLAGS: CommandFlags = load_command_flags(os.environ)

# Proposal reasoning is UNTRUSTED: it originates from an LLM (prompt-injectable)
# and from scraped news headlines (attacker-controlled). It is rendered into the
# always-present DOM, so an <img onerror> / <script> payload would execute on
# page load and could same-origin POST to the approve endpoint. We therefore
# sanitize before marking it safe. Two layers:
#   1. Prefer nh3 (Rust ammonia bindings) if installed — a real HTML sanitizer.
#   2. Fallback (no nh3 in the image yet): HTML-escape the source BEFORE markdown
#      so any raw tag becomes inert text, then strip non-http(s)/mailto link
#      schemes from generated anchors (kills javascript: URIs).
try:
    import nh3 as _nh3  # type: ignore
except Exception:  # pragma: no cover - nh3 optional
    _nh3 = None

_MD = _markdown.Markdown(extensions=[
    'fenced_code', 'tables', 'sane_lists', 'nl2br', 'pymdownx.magiclink',
])

_ALLOWED_URL_SCHEMES = ('http:', 'https:', 'mailto:')
_ANCHOR_RE = re.compile(r'<a\b[^>]*\bhref\s*=\s*(["\'])(.*?)\1', re.IGNORECASE | re.DOTALL)


def _neutralize_bad_hrefs(html: str) -> str:
    """Replace anchors whose href is not an allowed scheme with inert text."""
    def repl(m: re.Match) -> str:
        href = (m.group(2) or '').strip().lower()
        if href.startswith(_ALLOWED_URL_SCHEMES) or href.startswith(('/', '#')) or href.startswith('www.'):
            return m.group(0)
        # Drop the href entirely (leaves <a ...> with no navigation).
        return '<a '
    return _ANCHOR_RE.sub(repl, html)


def _render_md(text: Any) -> str:
    if not text:
        return ''
    if _nh3 is not None:
        _MD.reset()
        raw = _MD.convert(str(text))
        html = _nh3.clean(
            raw,
            link_rel='noopener noreferrer nofollow',
        )
    else:
        # Escape first so raw HTML tags can never be emitted; markdown syntax
        # (*, _, [](), `) survives escaping untouched.
        _MD.reset()
        html = _MD.convert(_html.escape(str(text), quote=False))
        html = _neutralize_bad_hrefs(html)
    # Reasoning links point at external references — open them in a new tab.
    return html.replace('<a href=', '<a target="_blank" rel="noopener noreferrer" href=')


def _preview(text: Any, n: int = 90) -> str:
    """One-line, collapsed-whitespace snippet of the reasoning for the cell."""
    s = ' '.join(str(text or '').split())
    return (s[: n - 1] + '…') if len(s) > n else s

# `/readyz`'s readiness flag (G0 Task 6) -- flips false once the app starts a
# graceful shutdown (SIGTERM), so an external LB/orchestrator polling
# `/readyz` stops routing new requests to a draining process while
# in-flight ones finish. A standard readiness-probe pattern, not a
# dependency check. Wired via `lifespan` (not the deprecated `on_event`)
# so the flip happens exactly once, deterministically, on shutdown.
_READY = True


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    yield
    global _READY
    _READY = False


_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / 'templates'))


# CSRF: a per-process token embedded as a hidden field in every approve/reject
# form and verified on POST. This blocks the blind cross-origin / injected POST
# that could otherwise place a live order (the endpoints have no other auth).
# Even with reasoning now sanitized, defense-in-depth: a mutating endpoint that
# places real orders must not be triggerable by a forged request.
_CSRF_TOKEN = secrets.token_urlsafe(32)

# Optional shared-secret gate for the whole dashboard. When MMR_WEB_TOKEN is set,
# every request must present it (?token= or X-MMR-Token header). Unset ⇒ open,
# relying on the compose 127.0.0.1-only port mapping (documented default).
_ACCESS_TOKEN = os.environ.get('MMR_WEB_TOKEN', '').strip()


def _check_access(request: Request) -> None:
    if not _ACCESS_TOKEN:
        return
    supplied = request.headers.get('X-MMR-Token') or request.query_params.get('token') or ''
    if secrets.compare_digest(supplied, _ACCESS_TOKEN):
        return
    if _has_valid_dashboard_session(request):
        return
    raise HTTPException(status_code=401, detail='unauthorized')


def _has_valid_dashboard_session(request: Request) -> bool:
    """A valid command-center session cookie also satisfies this legacy
    token gate.

    ``_ACCESS_TOKEN`` is only non-empty when the deprecated ``MMR_WEB_TOKEN``
    alias is set (canonical ``DASHBOARD_TOKEN`` config leaves it empty and
    this check is a no-op above). In that deprecated-alias case the SAME env
    var also seeds the command-center's ``SessionManager`` token, so a
    cookie-authenticated browser never re-sends the raw token on every
    request -- without this, `_check_access` would demand it on top of the
    already-verified session cookie and permanently 401 the legacy page for
    a normal cookie login. Any failure here (no command center wired, no
    manager configured yet, malformed/expired cookie) degrades to False --
    the caller still falls through to the 401 below, never to an open gate.
    """
    center = getattr(getattr(request.app, 'state', None), 'command_center', None)
    if center is None:
        return False
    cookie = request.cookies.get(SESSION_COOKIE, '')
    if not cookie:
        return False
    try:
        manager = center.ensure_session_manager()
    except Exception:
        return False
    return manager.verify(cookie)


def _check_csrf(token: str) -> None:
    if not secrets.compare_digest(token or '', _CSRF_TOKEN):
        raise HTTPException(status_code=403, detail='CSRF token mismatch')


# ---------------------------------------------------------------------------
# [M1-C] Task 7 -- single-authority lockout.
#
# The command-center routes (web/command_center/routes_commands.py) are the
# ONLY mutation authority once DASHBOARD_COMMANDS_ENABLED is true. Before
# this task, a handful of legacy routes here (approve/reject proposals,
# enable/disable a strategy, edit its params) placed the exact same
# trades/RPC calls through a second, unguarded path -- no session/CSRF-bound
# auth beyond this module's own single shared token, no CAS/expected-version
# check, no command_id audit trail. That's a real second writer, and it's
# the F3 finding this closes: legacy strategy mutations bypassing the new
# command machinery. Watchlist CRUD and /strategies/deploy are deliberately
# OUT of this frozenset -- they aren't trading mutations and their eventual
# migration is tracked separately under [COMPAT].
# ---------------------------------------------------------------------------
LEGACY_TRADING_MUTATION_PATHS = frozenset({
    "/proposals/{pid}/approve",
    "/proposals/{pid}/reject",
    "/strategies/{name}/enable",
    "/strategies/{name}/disable",
    "/strategies/{name}/params",
})


class LegacyMutationDisabledError(HTTPException):
    """Raised by `require_legacy_mutations_enabled`. A plain
    `HTTPException(detail={...})` would serialize as FastAPI's default
    `{"detail": {...}}` wrapper; this subclass gets its OWN exception
    handler (registered in `create_app` below) so the response body is the
    stable `{code, message, retryable, correlation_id}` envelope directly --
    the same shape `web/command_center/routes_commands.py`'s
    `CommandApiError` already produces for the command routes, so a caller
    (or the dashboard JS) can branch on `body["code"]` in both places the
    same way.
    """

    def __init__(self) -> None:
        super().__init__(status_code=409, detail={
            "code": "MOVED_TO_COMMAND_CENTER",
            "message": "this action moved to /command-center; the legacy "
                       "dashboard is read-only for trading mutations while "
                       "dashboard commands are enabled",
            "retryable": False,
            "correlation_id": None,
        })


def _legacy_mutation_disabled_handler(request: Request,
                                      exc: LegacyMutationDisabledError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.detail)


def require_legacy_mutations_enabled(request: Request) -> None:
    """FastAPI dependency attached to each path in
    `LEGACY_TRADING_MUTATION_PATHS`. When the command center owns mutations,
    these legacy POSTs must fail closed -- BEFORE this module's own
    `_check_access`/`_check_csrf` even run (this is a route-level
    `dependencies=[...]` entry, resolved ahead of the endpoint body) -- so a
    stale/forged legacy-form submission can never race the guarded command
    saga. Reads (the dashboard page itself, `/api/health`, watchlist routes,
    `/strategies/deploy`) are untouched.
    """
    if request.app.state.command_flags.commands_enabled:
        raise LegacyMutationDisabledError()


def make_test_client(*, commands_enabled: bool):
    """Test-only helper (used by `tests/test_command_security_gate.py`):
    builds a real `create_app()` instance with `command_flags.commands_
    enabled` forced to the given value, independent of whatever
    DASHBOARD_COMMANDS_ENABLED this process actually loaded into the
    module-level `_COMMAND_FLAGS` at import time.

    The legacy mutation routes below sit behind `SessionSecurityMiddleware`
    regardless of the command-center flag (session auth is unconditional
    dashboard-wide), so a throwaway `CommandCenter` -- wired with inert
    bridge/quote-plane stand-ins, mirroring `tests/test_web_dashboard.py`'s
    `stub_cc` fixture -- supplies just enough to authenticate a client
    through `/session` without a real typed-RPC bridge thread or ticker
    PubSub connection. Imports `TestClient` lazily: `httpx` (fastapi.
    testclient's dependency) is a test-only extra, never a hard runtime
    dependency of this production module.
    """
    from fastapi.testclient import TestClient

    class _NullBridge:
        def health(self) -> dict:
            return {}

        def stop(self, timeout: float = 5.0) -> None:
            pass

    class _NullQuotePlane:
        def start(self) -> None:
            pass

        def stop(self, timeout: float = 5.0) -> None:
            pass

    token = secrets.token_urlsafe(16)
    center = CommandCenter(
        CommandCenterConfig(),
        credentials_loader=lambda: DashboardCredentials(
            token=token, session_secret=b't' * 32, legacy_alias_used=False),
        query_client_factory=lambda: None,
        feed_client_factory=lambda: None,
        bridge_factory=lambda *a, **k: _NullBridge(),
        quote_plane_factory=lambda *a, **k: _NullQuotePlane(),
        commands_enabled=commands_enabled,
    )
    application = create_app(center)
    # create_app() always stamps the process-wide _COMMAND_FLAGS onto
    # app.state -- overridden here so this helper can force either value
    # regardless of the real environment's DASHBOARD_COMMANDS_ENABLED.
    application.state.command_flags = CommandFlags(
        commands_enabled=commands_enabled, live_commands_enabled=False,
        live_account_id=None, live_max_order_notional=None)
    client = TestClient(application)
    client.post("/session", data={"token": token})
    # [COMPAT] Task 1: the watchlist routes now also require a matching
    # Origin header (`_check_origin`, reused from routes_commands.py) -- set
    # a same-origin default here so every existing caller of this helper
    # (tests/test_command_security_gate.py's watchlist/deploy-out-of-scope
    # check in particular) keeps getting the pre-existing 303/409 behavior
    # for those routes without needing to know about the new requirement.
    # Callers that explicitly pass their own `headers=` (e.g. HEADERS with
    # its own "Origin": "http://testserver") are unaffected -- same value.
    client.headers.update({"Origin": "http://testserver"})
    return client


# States that count as "live / enabled" (mirrors strategy_runtime semantics).
_ENABLED_STATES = {'RUNNING', 'WAITING_HISTORICAL_DATA', 'INSTALLED'}

# ---------------------------------------------------------------------------
# Shared SDK connection
# ---------------------------------------------------------------------------
_mmr_lock = threading.Lock()
_mmr: Optional[Any] = None


def _get_mmr():
    global _mmr
    if _mmr is None:
        from trader.sdk import MMR
        _mmr = MMR().connect()
    return _mmr


def _reset_mmr():
    global _mmr
    try:
        if _mmr is not None and hasattr(_mmr, 'close'):
            _mmr.close()
    except Exception:
        pass
    _mmr = None


def _call(fn: Callable[[Any], Any], *, retry: bool = True):
    """Run an SDK op under the shared lock, reconnecting once on drop."""
    with _mmr_lock:
        try:
            return fn(_get_mmr())
        except (ConnectionError, TimeoutError):
            if not retry:
                raise
            _reset_mmr()
            return fn(_get_mmr())


# ---------------------------------------------------------------------------
# Fetchers — each converts SDK output to plain JSON-friendly structures
# ---------------------------------------------------------------------------
def _records(df) -> list[dict]:
    """DataFrame -> list of clean dicts (NaN -> None, numpy scalars -> native)."""
    if df is None or not hasattr(df, 'to_dict') or getattr(df, 'empty', True):
        return []
    out = []
    for rec in df.to_dict('records'):
        clean: dict = {}
        for k, v in rec.items():
            if isinstance(v, float) and pd.isna(v):
                clean[k] = None
            elif hasattr(v, 'item') and not isinstance(v, (list, tuple)):
                try:
                    clean[k] = v.item()
                except Exception:
                    clean[k] = v
            else:
                clean[k] = v
        out.append(clean)
    return out


def _result_error(result) -> str:
    """Human-readable failure reason from a SuccessFail — `error` when set,
    else the carried exception (a timeout arrives as exception, error=None)."""
    return str(getattr(result, 'error', None)
               or getattr(result, 'exception', None)
               or 'unknown error')


def fetch_cash() -> Optional[dict]:
    return _call(lambda m: m.account_cash())


def fetch_snapshot() -> Optional[dict]:
    return _call(lambda m: m.portfolio_snapshot())


def fetch_status() -> Optional[dict]:
    return _call(lambda m: m.status())


def fetch_risk() -> Optional[dict]:
    return _call(lambda m: m.risk_report())


def fetch_risk_limits() -> Optional[dict]:
    return _call(lambda m: m.get_risk_limits())


def fetch_positions() -> list[dict]:
    return _records(_call(lambda m: m.portfolio()))


def _humanize_class_name(class_name: str) -> str:
    """CamelCase → spaced words: 'OpeningRangeBreakout' → 'Opening Range
    Breakout', 'VbtMacdBB' → 'Vbt Macd BB'."""
    if not class_name:
        return ''
    return re.sub(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])', ' ', class_name)


# Strategies live next to this package in the repo checkout; override for
# non-standard layouts with MMR_STRATEGIES_DIR.
_STRATEGIES_DIR = os.environ.get(
    'MMR_STRATEGIES_DIR', str(Path(__file__).parent.parent / 'strategies'))

# The runtime's actual config (same file strategy_service reads/reconciles).
_STRATEGY_CONFIG_PATH = Path('~/.config/mmr/strategy_runtime.yaml').expanduser()

_WATCHLIST_NAME_RE = re.compile(r'^[a-z0-9_-]{1,40}$')


def _get_accessor():
    """UniverseAccessor over the local DuckDB — watchlists ARE universes."""
    from trader.container import Container
    from trader.data.universe import UniverseAccessor
    cfg = Container.instance().config()
    return UniverseAccessor(cfg['duckdb_path'], cfg['universe_library'])


def fetch_watchlists() -> list[dict]:
    accessor = _get_accessor()
    rows = []
    for name, count in sorted(accessor.list_universes_count().items()):
        symbols = ''
        try:
            defs = accessor.get(name).security_definitions
            symbols = ', '.join(d.symbol for d in defs[:40])
            if count > 40:
                symbols += f', +{count - 40} more'
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning('watchlist %s read failed: %s', name, exc)
        rows.append({'name': name, 'count': count, 'symbols': symbols})
    return rows


def _split_symbols(raw: str) -> list[str]:
    return [s.strip().upper() for s in re.split(r'[,\s;]+', raw or '') if s.strip()]


def _resolve_symbols(symbols: list[str], exchange: str = '', currency: str = '',
                     sec_type: str = 'STK') -> tuple[list, list[str]]:
    """Resolve each symbol via IB (precision over convenience — never guess).
    Returns (resolved SecurityDefinitions, unresolved symbol names)."""
    resolved, missing = [], []
    for sym in symbols:
        try:
            defs = _call(lambda m: m.resolve(
                sym, sec_type=sec_type, exchange=exchange, currency=currency),
                retry=False)
        except Exception as exc:
            logger.warning('resolve %s failed: %s', sym, exc)
            defs = None
        if defs:
            resolved.append(defs[0])
        else:
            missing.append(sym)
    return resolved, missing


def fetch_strategies() -> list[dict]:
    rows = _records(_call(lambda m: m.strategies()))
    for r in rows:
        state = str(r.get('state') or '').upper()
        r['enabled'] = state in _ENABLED_STATES
        if isinstance(r.get('conids'), (list, tuple)):
            r['conids'] = ', '.join(str(c) for c in r['conids'])
        r['display_name'] = _humanize_class_name(str(r.get('class_name') or '')) or r.get('name')
        if not isinstance(r.get('params'), dict):
            r['params'] = {}
    return rows


def fetch_available_strategies() -> list[dict]:
    """Every Strategy subclass implemented under strategies/ (static AST
    facts — file, class, dispatch mode, tunables, docstring)."""
    rows = scan_strategies(_STRATEGIES_DIR)
    for r in rows:
        r['display_name'] = _humanize_class_name(r.get('class') or '') or r.get('file')
    return rows


def fetch_proposals() -> list[dict]:
    # No status filter -> every proposal, with a 'status' column.
    rows = _records(_call(lambda m: m.proposals(limit=100)))
    for r in rows:
        raw = r.get('reasoning') or ''
        r['reasoning_preview'] = _preview(raw)
        r['reasoning_html'] = _render_md(raw)
        r['has_reasoning'] = bool(str(raw).strip())
    return rows


def _flash(msg: str) -> RedirectResponse:
    # Deploy + watchlist POST routes redirect to /manage so the post/redirect/get
    # loop stays on the page the form was submitted from.
    return RedirectResponse(url=f'/manage?flash={quote(msg)}', status_code=303)


def _manage_page_context(flash: str = '') -> tuple[dict[str, Any], dict[str, str]]:
    """Fetch only the sections /manage needs — no trader RPC on the hot path."""
    sections: dict[str, Any] = {}
    errors: dict[str, str] = {}
    fetchers: dict[str, Callable[[], Any]] = {
        'strategies': fetch_strategies,
        'available': fetch_available_strategies,
        'watchlists': fetch_watchlists,
    }
    for key, fn in fetchers.items():
        try:
            sections[key] = fn()
        except Exception as exc:  # noqa: BLE001 - surface, don't crash the page
            logger.warning('manage section %s failed: %s', key, exc)
            sections[key] = None
            errors[key] = f'{type(exc).__name__}: {exc}'

    strategies = sections.get('strategies') or []
    deployed_classes = {s.get('class_name') for s in strategies if s.get('class_name')}
    available = sections.get('available') or []
    for a in available:
        a['deployed'] = a.get('class') in deployed_classes

    return ({
        'strategies': strategies,
        'available_strategies': available,
        'watchlists': sections.get('watchlists') or [],
        'deployed_count': len(strategies),
        'flash': flash,
        'csrf_token': _CSRF_TOKEN,
        'errors': errors,
    }, errors)


def _coerce_yaml_value(text: str):
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


# ---------------------------------------------------------------------------
# Health (G0 Task 6)
# ---------------------------------------------------------------------------
# `/healthz` and `/readyz` are UNAUTHENTICATED liveness/readiness probes (no
# `_check_access` gate) -- Compose's healthcheck and any external load
# balancer must be able to hit them without a token. Both are deliberately
# bare booleans: neither may leak dependency/internal detail to an
# unauthenticated caller (which service is down, why, what its address is,
# etc.) -- that detail only appears behind `/api/health`'s auth gate below.
#
# `_READY` (defined above, next to `app`'s construction) flips false once
# the app starts a graceful shutdown -- see the `_lifespan` docstring.
def _register_legacy_routes(application: FastAPI) -> None:
    @application.get('/healthz')
    def healthz():
        return {'ok': True}


    @application.get('/readyz')
    def readyz():
        return {'ready': _READY}


    @application.get('/api/health')
    def api_health(request: Request):
        """Authenticated: process state + each scheduled job's last
        success/failure, for operators/dashboards that need real detail (unlike
        `/healthz`/`/readyz` above). Every dependency fetch degrades to a
        reachable=False entry rather than raising -- a health probe must never
        500 because a sibling service is down; that IS the interesting case to
        report, not a reason to fail the request. Redaction is applied inside
        `build_health_payload` regardless of what these fetchers return.
        """
        _check_access(request)
        try:
            status = fetch_status()
            trader_dep: dict = {'reachable': status is not None, 'status': status}
        except Exception as exc:  # noqa: BLE001 - degrade, never 500 a health probe
            logger.warning('api_health: trader status fetch failed: %s', exc)
            trader_dep = {'reachable': False, 'error': type(exc).__name__}
        return build_health_payload(dependencies={'trader': trader_dep})


    # ---------------------------------------------------------------------------
    # Routes
    # ---------------------------------------------------------------------------


    @application.get('/')
    def home() -> RedirectResponse:
        # The command center (/cc) is the live dashboard. Root redirects there
        # so the default entry point never touches the legacy server-rendered
        # dashboard below (/legacy), whose 10 sequential blocking SDK fetchers
        # hang for minutes against a split-container trader that serves only the
        # typed sockets (42101/2/3), not the legacy full RPC (42001) — the
        # "stuck loading the page" symptom. Unauthenticated requests are already
        # bounced to /cc/login by SessionSecurityMiddleware before reaching here.
        # 307 (temporary, method-preserving) keeps this fully reversible — no
        # permanent browser caching of the redirect.
        return RedirectResponse('/cc', status_code=307)


    @application.get('/manage')
    def manage_page(request: Request, flash: str = ''):
        """Deploy-from-disk + watchlist CRUD — extracted from the legacy dashboard."""
        _check_access(request)
        ctx, _ = _manage_page_context(flash=flash)
        return _TEMPLATES.TemplateResponse(request, 'manage.html', ctx)


    @application.get('/legacy')
    def dashboard(request: Request, flash: str = ''):
        _check_access(request)
        sections: dict[str, Any] = {}
        errors: dict[str, str] = {}
        fetchers: dict[str, Callable[[], Any]] = {
            'cash': fetch_cash,
            'snapshot': fetch_snapshot,
            'status': fetch_status,
            'risk': fetch_risk,
            'risk_limits': fetch_risk_limits,
            'positions': fetch_positions,
            'strategies': fetch_strategies,
            'proposals': fetch_proposals,
            'available': fetch_available_strategies,
            'watchlists': fetch_watchlists,
        }
        for key, fn in fetchers.items():
            try:
                sections[key] = fn()
            except Exception as exc:  # noqa: BLE001 - surface, don't crash the page
                logger.warning('dashboard section %s failed: %s', key, exc)
                sections[key] = None
                errors[key] = f'{type(exc).__name__}: {exc}'

        strategies = sections.get('strategies') or []
        # Mark scanned classes that are already deployed so the "available"
        # table distinguishes on-disk-only strategies from live ones.
        deployed_classes = {s.get('class_name') for s in strategies if s.get('class_name')}
        available = sections.get('available') or []
        for a in available:
            a['deployed'] = a.get('class') in deployed_classes

        # Explicit risk tri-state — never infer "ok" from a missing/failed
        # report. "unavailable" takes priority even if a stale/None risk value
        # happens to carry no warnings; "ok" only applies when the fetch itself
        # succeeded.
        risk_obj = sections.get('risk')
        risk_warnings = (risk_obj.get('warnings') or []) if isinstance(risk_obj, dict) else []
        if 'risk' in errors:
            risk_state = 'unavailable'
        elif risk_warnings:
            risk_state = 'warning'
        else:
            risk_state = 'ok'

        return _TEMPLATES.TemplateResponse(request, 'dashboard.html', {
            'cash': sections.get('cash'),
            'snapshot': sections.get('snapshot'),
            'status': sections.get('status'),
            'risk': sections.get('risk'),
            'risk_state': risk_state,
            'risk_limits': sections.get('risk_limits'),
            'positions': sections.get('positions') or [],
            'strategies': strategies,
            'available_strategies': available,
            'watchlists': sections.get('watchlists') or [],
            'enabled_count': sum(1 for s in strategies if s.get('enabled')),
            'proposals': sections.get('proposals') or [],
            'errors': errors,
            'flash': flash,
            'csrf_token': _CSRF_TOKEN,
        })


    @application.post('/proposals/{pid}/approve',
                      dependencies=[Depends(require_legacy_mutations_enabled)])
    def approve(pid: int, request: Request, csrf_token: str = Form('')):
        """Approve a proposal — this PLACES A LIVE ORDER via trader_service."""
        _check_access(request)
        _check_csrf(csrf_token)
        try:
            result = _call(lambda m: m.approve(pid), retry=False)
            if not hasattr(result, 'is_success') or not result.is_success():
                msg = f'#{pid} approve failed: {_result_error(result)}'
            else:
                msg = f'#{pid} approved & order submitted'
        except Exception as exc:  # noqa: BLE001
            logger.warning('approve #%s failed: %s', pid, exc)
            msg = f'#{pid} approve error: {type(exc).__name__}: {exc}'
        return RedirectResponse(url=f'/?flash={quote(msg)}', status_code=303)


    @application.post('/proposals/{pid}/reject',
                      dependencies=[Depends(require_legacy_mutations_enabled)])
    def reject(pid: int, request: Request, reason: str = Form(''), csrf_token: str = Form('')):
        _check_access(request)
        _check_csrf(csrf_token)
        try:
            ok = _call(lambda m: m.reject(pid, reason), retry=False)
            msg = f'#{pid} rejected' if ok else f'#{pid} not rejected (not pending?)'
        except Exception as exc:  # noqa: BLE001
            logger.warning('reject #%s failed: %s', pid, exc)
            msg = f'#{pid} reject error: {type(exc).__name__}: {exc}'
        return RedirectResponse(url=f'/?flash={quote(msg)}', status_code=303)


    @application.post('/strategies/{name}/enable',
                      dependencies=[Depends(require_legacy_mutations_enabled)])
    def enable_strategy(name: str, request: Request, csrf_token: str = Form('')):
        _check_access(request)
        _check_csrf(csrf_token)
        try:
            result = _call(lambda m: m.enable_strategy(name), retry=False)
            if hasattr(result, 'is_success') and not result.is_success():
                msg = f'{name} enable failed: {_result_error(result)}'
            else:
                msg = f'{name} enabled'
        except Exception as exc:  # noqa: BLE001
            logger.warning('enable %s failed: %s', name, exc)
            msg = f'{name} enable error: {type(exc).__name__}: {exc}'
        return RedirectResponse(url=f'/?flash={quote(msg)}', status_code=303)


    @application.post('/strategies/{name}/disable',
                      dependencies=[Depends(require_legacy_mutations_enabled)])
    def disable_strategy(name: str, request: Request, csrf_token: str = Form('')):
        _check_access(request)
        _check_csrf(csrf_token)
        try:
            result = _call(lambda m: m.disable_strategy(name), retry=False)
            if hasattr(result, 'is_success') and not result.is_success():
                msg = f'{name} disable failed: {_result_error(result)}'
            else:
                msg = f'{name} disabled'
        except Exception as exc:  # noqa: BLE001
            logger.warning('disable %s failed: %s', name, exc)
            msg = f'{name} disable error: {type(exc).__name__}: {exc}'
        return RedirectResponse(url=f'/?flash={quote(msg)}', status_code=303)


    @application.post('/strategies/{name}/params',
                      dependencies=[Depends(require_legacy_mutations_enabled)])
    async def update_strategy_params(name: str, request: Request):
        """Persist edited params — the strategy is hot-swapped live server-side.

        Async so we can read the dynamic form fields (param_<KEY> inputs plus an
        optional new_key/new_value pair); the SDK call runs in the threadpool
        because it internally uses asyncio.run().
        """
        _check_access(request)
        form = await request.form()
        _check_csrf(str(form.get('csrf_token') or ''))

        params: dict[str, str] = {}
        for key, value in form.items():
            if key.startswith('param_'):
                params[key[len('param_'):]] = str(value)
        new_key = str(form.get('new_key') or '').strip()
        if new_key:
            params[new_key] = str(form.get('new_value') or '')

        if not params:
            return RedirectResponse(url=f'/?flash={quote("no params submitted")}', status_code=303)

        try:
            result = await run_in_threadpool(
                lambda: _call(lambda m: m.update_strategy_params(name, params), retry=False))
            if hasattr(result, 'is_success') and not result.is_success():
                msg = f'{name} params update failed: {_result_error(result)}'
            else:
                msg = f'{name} params updated (live — persisted to config)'
        except Exception as exc:  # noqa: BLE001
            logger.warning('params update %s failed: %s', name, exc)
            msg = f'{name} params error: {type(exc).__name__}: {exc}'
        return RedirectResponse(url=f'/?flash={quote(msg)}', status_code=303)


    # -------------------------------------------------------------------
    # [COMPAT] Task 1 -- watchlist CRUD + CSV upload under the command-center
    # session (spec 2026-07-15-realtime-trading-command-center-design.md
    # Sections 10/14.1). Watchlists ARE NOT a trading mutation (see
    # LEGACY_TRADING_MUTATION_PATHS above) -- they keep working regardless of
    # DASHBOARD_COMMANDS_ENABLED -- but they used to share the SAME weak gate
    # as everything else pre-[M1-R]: `_check_access`, a no-op in the
    # canonical DASHBOARD_TOKEN config (it only does something when the
    # deprecated MMR_WEB_TOKEN alias is set), plus `_check_csrf` against a
    # single process-wide secret with no origin check at all.
    #
    # These five routes now require `Depends(require_session)` -- the exact
    # same dependency `web/command_center/routes_commands.py`'s command
    # routes already enforce (reached the same way, via
    # `request.app.state.command_center.require_session`) -- and the same
    # `_check_origin` strict same-origin check, imported verbatim rather
    # than reimplemented. A request without a valid session cookie now hard
    # 401s (or is redirected by `SessionSecurityMiddleware` before even
    # reaching here, since that middleware already gates every non-exempt
    # path); a request whose Origin doesn't match Host now hard 403s -- a
    # protection no legacy route had before.
    #
    # CSRF verification is deliberately LEFT on the existing `_check_csrf`/
    # `_CSRF_TOKEN` pair rather than switched to `session_csrf_token` (the
    # per-session-derived token `require_command_auth` uses): `dashboard.html`
    # renders ONE shared `{{ csrf_token }}` Jinja slot, read by both these
    # watchlist forms AND the not-yet-migrated trading-mutation/deploy forms
    # (approve/reject/enable/disable/params/deploy — out of this task's
    # scope, and the template itself is out of scope to edit). Re-deriving
    # the rendered value from the session would silently break those other
    # forms' real submissions the moment `dashboard()` re-renders — a bigger
    # regression than the narrower theoretical gain of a per-session CSRF
    # secret in a single-operator dashboard. See the Task 1 report for the
    # full drift note.
    # -------------------------------------------------------------------
    @application.post('/watchlists/create')
    def watchlist_create(request: Request, name: str = Form(''), csrf_token: str = Form(''),
                         session: str = Depends(require_session)):
        _check_origin(request)
        _check_csrf(csrf_token)
        wl = (name or '').strip().lower()
        if not _WATCHLIST_NAME_RE.match(wl):
            return _flash(f'invalid watchlist name {name!r} — use a-z, 0-9, -, _ (max 40)')
        try:
            accessor = _get_accessor()
            if wl in accessor.list_universes_count():
                return _flash(f'watchlist "{wl}" already exists')
            universe = accessor.get(wl)          # creates-on-read semantics
            accessor.update(universe)            # persist the (empty) universe
            msg = f'watchlist "{wl}" created — add symbols or upload a CSV'
        except Exception as exc:  # noqa: BLE001
            logger.warning('watchlist create %s failed: %s', wl, exc)
            msg = f'create failed: {type(exc).__name__}: {exc}'
        return _flash(msg)


    @application.post('/watchlists/{name}/add')
    def watchlist_add(name: str, request: Request, symbols: str = Form(''),
                      exchange: str = Form(''), currency: str = Form(''),
                      csrf_token: str = Form(''),
                      session: str = Depends(require_session)):
        _check_origin(request)
        _check_csrf(csrf_token)
        syms = _split_symbols(symbols)
        if not syms:
            return _flash('no symbols given')
        try:
            resolved, missing = _resolve_symbols(syms, exchange=exchange, currency=currency)
            accessor = _get_accessor()
            for sd in resolved:
                accessor.insert(name, sd)
            parts = []
            if resolved:
                parts.append('added ' + ', '.join(f'{d.symbol} ({d.conId})' for d in resolved))
            if missing:
                parts.append('UNRESOLVED (not added): ' + ', '.join(missing)
                             + ' — for non-US listings set exchange/currency')
            msg = f'{name}: ' + ('; '.join(parts) or 'nothing to do')
        except Exception as exc:  # noqa: BLE001
            logger.warning('watchlist add %s failed: %s', name, exc)
            msg = f'{name} add failed: {type(exc).__name__}: {exc}'
        return _flash(msg)


    @application.post('/watchlists/{name}/upload')
    async def watchlist_upload(name: str, request: Request,
                               file: UploadFile = File(...),
                               csrf_token: str = Form(''),
                               session: str = Depends(require_session)):
        """CSV upload. Simple shape: a `symbol` column (optional exchange/
        currency/sectype columns) or one symbol per line — rows resolve via IB.
        Full SecurityDefinition exports (conId column) import directly."""
        _check_origin(request)
        _check_csrf(csrf_token)
        raw = await file.read()
        if len(raw) > 1_000_000:
            return _flash('CSV too large (max 1 MB)')
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            return _flash('file is not UTF-8 text — export as plain CSV')

        def _import() -> str:
            import csv as _csv
            import io
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if not lines:
                return 'CSV is empty'
            header = [h.strip().lower() for h in lines[0].split(',')]
            accessor = _get_accessor()
            if 'conid' in header:
                count = accessor.update_from_csv_str(name, text)
                return f'{name}: imported {count} security definitions'
            if 'symbol' in header:
                rows = list(_csv.DictReader(io.StringIO(text)))
                rows = [{k.strip().lower(): (v or '').strip() for k, v in r.items()} for r in rows]
            else:
                # headerless: one symbol per line
                rows = [{'symbol': ln.split(',')[0].strip()} for ln in lines]
            added, missing = [], []
            for r in rows:
                sym = (r.get('symbol') or '').upper()
                if not sym:
                    continue
                resolved, unres = _resolve_symbols(
                    [sym], exchange=r.get('exchange', ''), currency=r.get('currency', ''),
                    sec_type=r.get('sectype', 'STK') or 'STK')
                if resolved:
                    accessor.insert(name, resolved[0])
                    added.append(sym)
                else:
                    missing.extend(unres)
            msg = f'{name}: added {len(added)} symbol(s)'
            if missing:
                msg += f'; UNRESOLVED: {", ".join(missing[:15])}'
            return msg

        try:
            msg = await run_in_threadpool(_import)
        except Exception as exc:  # noqa: BLE001
            logger.warning('watchlist upload %s failed: %s', name, exc)
            msg = f'{name} upload failed: {type(exc).__name__}: {exc}'
        return _flash(msg)


    @application.post('/watchlists/{name}/remove')
    def watchlist_remove(name: str, request: Request, symbol: str = Form(''),
                         csrf_token: str = Form(''),
                         session: str = Depends(require_session)):
        _check_origin(request)
        _check_csrf(csrf_token)
        try:
            accessor = _get_accessor()
            universe = accessor.get(name)
            match = universe.find_symbol(symbol.strip())
            if not match:
                return _flash(f'"{symbol}" not in {name}')
            universe.security_definitions = [
                d for d in universe.security_definitions if d.conId != match.conId]
            accessor.update(universe)
            msg = f'removed {match.symbol} from {name}'
        except Exception as exc:  # noqa: BLE001
            logger.warning('watchlist remove %s failed: %s', name, exc)
            msg = f'{name} remove failed: {type(exc).__name__}: {exc}'
        return _flash(msg)


    @application.post('/watchlists/{name}/delete')
    def watchlist_delete(name: str, request: Request, csrf_token: str = Form(''),
                         session: str = Depends(require_session)):
        _check_origin(request)
        _check_csrf(csrf_token)
        try:
            _get_accessor().delete(name)
            msg = f'watchlist "{name}" deleted'
        except Exception as exc:  # noqa: BLE001
            logger.warning('watchlist delete %s failed: %s', name, exc)
            msg = f'{name} delete failed: {type(exc).__name__}: {exc}'
        return _flash(msg)


    @application.post('/strategies/deploy')
    async def deploy_strategy(request: Request, session: str = Depends(require_session)):
        """Deploy an on-disk strategy: validate against the scanner (keeps the
        strategies-dir sandbox), resolve/attach the target instruments, append
        the YAML entry atomically, then reload + enable via RPC."""
        _check_origin(request)
        form = await request.form()
        _check_csrf(str(form.get('csrf_token') or ''))

        file_name = str(form.get('file') or '').strip()
        class_name = str(form.get('class') or '').strip()
        name = str(form.get('name') or '').strip().lower()
        bar_size = str(form.get('bar_size') or '1 min').strip()
        days = str(form.get('days') or '90').strip()
        symbols = _split_symbols(str(form.get('symbols') or ''))
        watchlist = str(form.get('watchlist') or '').strip()
        auto_propose = bool(form.get('auto_propose'))
        params = {k[len('param_'):]: _coerce_yaml_value(str(v))
                  for k, v in form.items()
                  if k.startswith('param_') and str(v).strip() != ''}

        def _deploy() -> str:
            # 1. The (file, class) pair must come from the scanner — a forged
            # form must not be able to point the runtime at an arbitrary path.
            known = {(r['file'], r['class']) for r in scan_strategies(_STRATEGIES_DIR)}
            if (file_name, class_name) not in known:
                return f'unknown strategy {class_name} in {file_name} — not deploying'
            if not _WATCHLIST_NAME_RE.match(name or ''):
                return f'invalid deployment name {name!r} — use a-z, 0-9, -, _ (max 40)'
            if bool(symbols) == bool(watchlist):
                return 'give either symbols or a watchlist (exactly one)'

            # 2. Config: reject duplicate names before doing any work.
            if _STRATEGY_CONFIG_PATH.exists():
                config = yaml.safe_load(_STRATEGY_CONFIG_PATH.read_text()) or {}
            else:
                config = {}
            entries = config.setdefault('strategies', [])
            if any(e.get('name') == name for e in entries):
                return f'strategy "{name}" already deployed — undeploy first or pick another name'

            entry: dict = {
                'name': name,
                'description': f'Deployed from dashboard ({class_name} in {file_name})',
                'module': f'strategies/{file_name}',
                'class_name': class_name,
                'bar_size': bar_size,
                'historical_days_prior': int(days) if days.isdigit() else 90,
            }
            # 3. Target instruments. Symbols resolve via IB and register their
            # security definitions locally (strategy load needs resolve_symbol
            # to hit) in a per-deploy watchlist for provenance.
            if symbols:
                resolved, missing = _resolve_symbols(symbols)
                if missing:
                    return ('deploy aborted — unresolved: ' + ', '.join(missing)
                            + ' (nothing written)')
                accessor = _get_accessor()
                for sd in resolved:
                    accessor.insert(f'strat_{name}', sd)
                entry['conids'] = [sd.conId for sd in resolved]
            else:
                entry['universe'] = watchlist
            if auto_propose:
                entry['auto_execute'] = 'propose'
            if params:
                entry['params'] = params

            entries.append(entry)
            tmp = str(_STRATEGY_CONFIG_PATH) + '.tmp'
            with open(tmp, 'w') as f:
                yaml.safe_dump(config, f, sort_keys=False)
            os.replace(tmp, _STRATEGY_CONFIG_PATH)

            # 4. Load it now (not in 30s) and enable it, per the one-click ask.
            try:
                reload_result = _call(lambda m: m.reload_strategies(), retry=False)
                if hasattr(reload_result, 'is_success') and not reload_result.is_success():
                    return (f'"{name}" written to config but reload failed: '
                            f'{_result_error(reload_result)} — it loads on the next '
                            'reconcile; enable it from the Command Center')
                enable_result = _call(lambda m: m.enable_strategy(name), retry=False)
                if hasattr(enable_result, 'is_success') and not enable_result.is_success():
                    return (f'"{name}" deployed but enable failed: '
                            f'{_result_error(enable_result)} — enable it from the '
                            'Command Center')
            except Exception as exc:
                return (f'"{name}" written to config but service call failed '
                        f'({type(exc).__name__}: {exc}) — it loads on the next '
                        'reconcile; enable it from the Command Center')
            target = ', '.join(symbols) if symbols else f'watchlist {watchlist}'
            return f'deployed & enabled "{name}" ({class_name}) on {target}'

        try:
            msg = await run_in_threadpool(_deploy)
        except Exception as exc:  # noqa: BLE001
            logger.warning('deploy failed: %s', exc)
            msg = f'deploy error: {type(exc).__name__}: {exc}'
        return _flash(msg)


def create_app(cc: CommandCenter | None = None) -> FastAPI:
    """Build the FastAPI application: the command center (session gate, SSE
    fan-out, typed read model) plus the legacy SDK-backed dashboard, sharing
    one process and one `/session` login.

    `cc` lets tests inject a `CommandCenter` wired with fakes; the bare
    module-level `app = create_app()` below (used by `python3 -m web.app`
    and by any test that merely imports `web.app`) builds a real one from
    `CommandCenterConfig.from_env()`. Constructing it here never reads any
    credentials -- `CommandCenter`'s constructor is pure, and building the
    session/read routers below resolves the session manager lazily
    per-request (see `create_session_router`'s `manager_provider` and
    `create_read_router`'s `_require_session`), not at router-build time --
    so importing this module never itself requires `DASHBOARD_TOKEN`/
    `DASHBOARD_SESSION_SECRET` to be configured. `ensure_session_manager()`
    stays the one hard failure point, at lifespan startup or first request.
    """
    center = cc or CommandCenter(CommandCenterConfig.from_env(),
                                 commands_enabled=_COMMAND_FLAGS.commands_enabled)

    @contextlib.asynccontextmanager
    async def _app_lifespan(fastapi_app: FastAPI):
        # Compose the command center's lifespan (bridge/quote-plane startup
        # and teardown) with the pre-existing `_lifespan` (the `_READY`
        # readiness flip, G0 Task 6).
        #
        # `center.lifespan` DEGRADES to inert on a startup failure rather than
        # aborting (see its docstring), so the inner `_lifespan` -- and the
        # always-on `/healthz`/`/readyz` ops probes -- ALWAYS run regardless of
        # dashboard configuration: the app boots even with no dill-strict / no
        # credentials, and a misconfigured dashboard fails loud per request
        # instead of taking the whole process (and its probes) down at startup.
        #
        # Nesting `_lifespan` INSIDE `center.lifespan` keeps the teardown order:
        # its post-yield code (`_READY = False`) runs on the way OUT before
        # `center.lifespan`'s own `finally` (bridge.stop()/quote_plane.stop()),
        # so `/readyz` goes false as soon as shutdown begins, not only once that
        # teardown finishes.
        async with center.lifespan(fastapi_app):
            async with _lifespan(fastapi_app):
                yield

    application = FastAPI(title='MMR Dashboard', lifespan=_app_lifespan)
    application.state.command_center = center
    # [M1-C] Already-validated at module import (see `_COMMAND_FLAGS` above) --
    # every app instance (including test-built ones via `create_app(stub_cc)`)
    # gets the same fail-closed flags on `app.state`, not a per-instance reload.
    application.state.command_flags = _COMMAND_FLAGS
    # [M1-C] Task 3 -- the router is installed UNCONDITIONALLY so a disabled
    # deployment still answers `/api/commands/*` with a stable 403
    # `COMMANDS_DISABLED` instead of a bare 404; the gateway itself (and its
    # HMAC-key/typed-socket requirement) is only constructed when commands
    # are enabled, so a paper-only or read-only deployment never pays that
    # startup cost or needs that credential configured at all.
    #
    # [M1-C] Task 3 fix (I-1): the gateway is NOT built here. Building it
    # eagerly at `create_app()` time (outside any try/except) meant a
    # bad/missing service HMAC key raised straight out of `create_app()` --
    # taking the whole ASGI boot, and its always-on `/healthz`/`/readyz`
    # probes, down with it. It is now built inside
    # `CommandCenter._start_or_degrade` (see `web/command_center/__init__.py`),
    # the SAME degrade-tolerant try/except that already brings up the bridge
    # + quote plane: a build failure there degrades the center to inert
    # (`center.command_gateway` stays `None`) instead of aborting startup.
    # Routes read it per-request via `request.app.state.command_center.
    # command_gateway`, and return 503 `COMMAND_GATEWAY_UNAVAILABLE` (not the
    # 403 `COMMANDS_DISABLED` used for the feature simply being off) when
    # commands are enabled but the gateway never came up -- see
    # `routes_commands.py`'s `_gateway()`.
    install_command_routes(application)
    # [M1-C] Task 7: serializes LegacyMutationDisabledError as the stable
    # {code, message, retryable, correlation_id} envelope instead of
    # FastAPI's default {"detail": {...}} wrapper -- registered for the
    # subclass only, so every other HTTPException in this module (the
    # single shared-token/CSRF checks, watchlist validation, etc.) keeps
    # FastAPI's ordinary {"detail": ...} handling untouched.
    application.add_exception_handler(LegacyMutationDisabledError,
                                      _legacy_mutation_disabled_handler)
    application.mount('/static', StaticFiles(
        directory=str(Path(__file__).parent / 'static')), name='static')

    def _middleware_manager():
        """None-TOLERANT session-manager provider for the middleware.

        Attempts lazy construction (so a valid cookie presented before the
        lifespan/first request ever built the manager still authenticates,
        removing the ordering coupling), but a *misconfigured* dashboard
        (missing credentials -> `CredentialConfigError`) degrades to "no
        manager". The middleware turns "no manager" into a redirect to
        `/cc/login` (or 401 for `/api/*`) -- a gated request must never 500
        from the middleware just because the manager can't be built. The hard,
        fail-loud failure stays per-request in `require_session` ->
        `ensure_session_manager` for the routes that truly need a manager.
        """
        try:
            return center.ensure_session_manager()
        except CredentialConfigError:
            return None

    application.add_middleware(
        SessionSecurityMiddleware,
        manager_provider=_middleware_manager)
    # `center.ensure_session_manager` (a bound method, callable) is passed
    # rather than a manager instance: create_session_router wraps any
    # callable as its lazy manager_provider, so building this router never
    # itself constructs a SessionManager (and never needs credentials).
    application.include_router(create_session_router(
        center.ensure_session_manager, center.limiter,
        cookie_secure=center.config.cookie_secure))
    application.include_router(create_read_router(center, _TEMPLATES))
    # NEW route only: `/api/cc-health`. Never touches the G0 `/healthz` /
    # `/readyz` / `/api/health` routes registered by `_register_legacy_routes`
    # below -- see the M1-R Task 7 addendum for why those must stay as-is.
    application.include_router(create_health_router(center))
    _register_legacy_routes(application)
    return application


app = create_app()


def main():
    import uvicorn
    # Hard, unconditional fail-loud guard: this process pins workers=1 below,
    # but an operator setting WEB_CONCURRENCY/UVICORN_WORKERS (e.g. copying a
    # gunicorn-style multi-worker convention) would otherwise silently launch
    # with a single worker anyway while believing more were requested. There
    # is no ops probe to protect pre-startup here (unlike the in-lifespan
    # `CommandCenter._start_or_degrade`, which intentionally DEGRADES rather
    # than aborts so /healthz and /readyz keep serving) -- so this one aborts
    # the process outright, before uvicorn.run ever binds a socket.
    _assert_single_worker()
    port = int(os.environ.get('WEB_PORT', '7424'))
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    logger.info('MMR dashboard starting on 0.0.0.0:%d (single worker)', port)
    uvicorn.run(app, host='0.0.0.0', port=port, log_level='info',
                workers=1, timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS)


if __name__ == '__main__':
    main()
