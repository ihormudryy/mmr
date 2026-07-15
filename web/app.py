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
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from trader.strategy.inspect import scan_strategies

logger = logging.getLogger('web')

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

app = FastAPI(title='MMR Dashboard')
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
    if not secrets.compare_digest(supplied, _ACCESS_TOKEN):
        raise HTTPException(status_code=401, detail='unauthorized')


def _check_csrf(token: str) -> None:
    if not secrets.compare_digest(token or '', _CSRF_TOKEN):
        raise HTTPException(status_code=403, detail='CSRF token mismatch')

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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get('/healthz')
def healthz():
    return {'ok': True}


@app.get('/')
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
    return _TEMPLATES.TemplateResponse(request, 'dashboard.html', {
        'cash': sections.get('cash'),
        'snapshot': sections.get('snapshot'),
        'status': sections.get('status'),
        'risk': sections.get('risk'),
        'risk_limits': sections.get('risk_limits'),
        'positions': sections.get('positions') or [],
        'strategies': strategies,
        'available_strategies': available,
        'enabled_count': sum(1 for s in strategies if s.get('enabled')),
        'proposals': sections.get('proposals') or [],
        'errors': errors,
        'flash': flash,
        'csrf_token': _CSRF_TOKEN,
    })


@app.post('/proposals/{pid}/approve')
def approve(pid: int, request: Request, csrf_token: str = Form('')):
    """Approve a proposal — this PLACES A LIVE ORDER via trader_service."""
    _check_access(request)
    _check_csrf(csrf_token)
    try:
        result = _call(lambda m: m.approve(pid), retry=False)
        ok = getattr(result, 'success', None)
        if ok is False:
            msg = f'#{pid} approve failed: {getattr(result, "error", "unknown error")}'
        else:
            msg = f'#{pid} approved & submitted'
    except Exception as exc:  # noqa: BLE001
        logger.warning('approve #%s failed: %s', pid, exc)
        msg = f'#{pid} approve error: {type(exc).__name__}: {exc}'
    return RedirectResponse(url=f'/?flash={quote(msg)}', status_code=303)


@app.post('/proposals/{pid}/reject')
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


@app.post('/strategies/{name}/enable')
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


@app.post('/strategies/{name}/disable')
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


@app.post('/strategies/{name}/params')
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


def main():
    import uvicorn
    port = int(os.environ.get('WEB_PORT', '7424'))
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    logger.info('MMR dashboard starting on 0.0.0.0:%d', port)
    uvicorn.run(app, host='0.0.0.0', port=port, log_level='info')


if __name__ == '__main__':
    main()
