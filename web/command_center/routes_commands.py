"""[M1-C] Authenticated command routes (spec Sections 9.1, 9.6, 10, 11).

Thin translation layer: session + CSRF + origin + flags, then forward to the
frozen typed methods through ``DashboardCommandGateway``. ``202`` means
received only; the browser resolves outcomes from correlated
``command.updated`` events -- this module never renders success from the
HTTP response itself.

Source-vs-brief drift (see ``.superpowers/sdd/m1c-task-3-report.md`` for the
full account) -- two real interfaces this router forwards to landed
DIFFERENTLY from what the plan anticipated when it was written:

1. ``web/command_center/session.py`` ([M1-R], out of this task's scope --
   the coordination brief for this task forbids touching it) exports
   ``build_require_session(manager) -> Callable[[Request], str]``, not a
   bare module-level ``require_session`` / ``DashboardSession`` dataclass /
   ``session_csrf_token`` helper. This module defines its own
   ``require_session`` (production wiring reuses
   ``CommandCenter.require_session`` -- the exact same object
   ``routes_read.py``'s ``_require_session`` already calls -- reached via
   ``request.app.state.command_center``) and its own ``session_csrf_token``
   (a keyed hash of the session's signed cookie value, so the token is
   bound to one session without needing any change to session.py). The
   session identity threaded through this module is therefore the raw
   cookie ``str`` session.py already returns, not a new dataclass.

2. The [M1-F3] typed ``create_proposal`` wire contract
   (``trader.messaging.production_api.CreateProposalRequest``) is FROZEN
   today as a flat, MARKET-only shape: ``{command_id, conid, action,
   quantity, amount, reasoning, confidence, thesis, group,
   max_price_drift_bps, preflight_nonce}`` -- no ``symbol`` (conId only, so
   there is no "cross-market fuzzy resolve" step to reproduce here), no
   ``order_type``/``limit_price``, no bracket/stop-loss/trailing-stop, no
   TIF. ``trader/sdk.py``'s own ``propose()`` documents the same limit and
   refuses loudly on a non-default ``ExecutionSpec`` rather than silently
   dropping it. This router mirrors that: the request bodies below only
   expose fields the coordinator can actually honor today, and an unknown
   field (e.g. ``order_type``) is rejected as a plain 422 validation error
   (``extra="forbid"``) instead of being silently swallowed. There is also
   no dedicated "close position" command -- a close is just a regular
   ``create_proposal`` call where the browser supplies the reducing
   ``action``/``quantity`` for the position it's looking at; the
   coordinator verifies the broker-reported reducible quantity server-side
   (``ProposalCommandService._is_reducing_close``).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any, Literal

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trader.domain.commands import CommandReceipt
from web.command_center.flags import CommandFlags
from web.command_center.gateway import DashboardCommandGateway, GatewayError
from web.command_center.session import CredentialConfigError

router = APIRouter()

# Section 11's stable error->HTTP mapping, extended with the real refusal
# codes `ProposalCommandService.create_proposal` actually raises today
# (`INVALID_ACTION`, `INVALID_SIZE`, `UNKNOWN_CONID`,
# `TRADING_FILTER_REJECTED`, `QUOTE_UNAVAILABLE`, `DUPLICATE_PENDING`,
# `SIZING_BLOCKED`) alongside the spec-vocabulary codes
# (`QUOTE_MISSING`/`QUOTE_STALE`) `DashboardCommandGateway._RETRYABLE_CODES`
# already anticipates for a future quote-authority check. Any code not
# listed here still degrades to 502 rather than raising unhandled.
_HTTP_STATUS = {
    "COMMANDS_DISABLED": 403, "LIVE_COMMANDS_DISABLED": 403,
    "CSRF_REJECTED": 403, "ORIGIN_REJECTED": 403, "SESSION_REQUIRED": 401,
    "VALIDATION_FAILED": 422, "NOT_FOUND": 404, "UNKNOWN_CONID": 404,
    "INVALID_ACTION": 422, "INVALID_SIZE": 422, "SIZING_BLOCKED": 422,
    "VERSION_CONFLICT": 409, "COMMAND_CONFLICT": 409, "COMMAND_REJECTED": 409,
    "TRADING_PAUSED": 409, "TRADING_FILTER_REJECTED": 409,
    "DUPLICATE_PENDING": 409,
    "QUOTE_MISSING": 409, "QUOTE_STALE": 409, "QUOTE_UNAVAILABLE": 409,
    "PREFLIGHT_REQUIRED": 428, "PREFLIGHT_EXPIRED": 410,
    "PREFLIGHT_CONSUMED": 410, "PREFLIGHT_MISMATCH": 409,
    "DEPENDENCY_UNAVAILABLE": 503, "COMMAND_CHANNEL_DOWN": 503,
    # [M1-C] Task 3 fix (M-6): commands enabled but the gateway never came up
    # (degraded startup, see `_gateway()` below) -- transient/retryable, NOT
    # the 403 `COMMANDS_DISABLED` used when the feature is simply off.
    "COMMAND_GATEWAY_UNAVAILABLE": 503,
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
        super().__init__(f"{code}: {message}")


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


def require_session(request: Request) -> str:
    """Resolve this request's session identity.

    ``session.py`` (frozen [M1-R] surface this task must not touch) exports
    ``build_require_session(manager) -> Callable[[Request], str]`` bound to
    a concrete ``SessionManager`` -- there is no bare module-level
    ``require_session`` there to import. Production wiring here reuses the
    exact same machinery every other command-center route already uses:
    ``CommandCenter.require_session`` (a property returning
    ``build_require_session(...)`` against the lazily-built
    ``SessionManager``), reached via ``request.app.state.command_center`` --
    precisely how ``routes_read.py``'s own ``_require_session`` wrapper
    works. ``SessionSecurityMiddleware`` already 401s any un-cookied
    ``/api/*`` request before this dependency ever runs in the real app, so
    this doubles as the source of the CSRF-binding identity below.
    """
    center = getattr(request.app.state, "command_center", None)
    if center is None:
        raise CommandApiError(401, "SESSION_REQUIRED", "session required")
    try:
        return center.require_session(request)
    except HTTPException as exc:
        raise CommandApiError(401, "SESSION_REQUIRED", "session required") from exc
    except CredentialConfigError as exc:
        # [M1-C] Task 3 fix (M-4): a missing-credentials dashboard config
        # (`ensure_session_manager` -> `load_dashboard_credentials`) must
        # still surface the stable 401 `SESSION_REQUIRED` a caller can branch
        # on, not an unhandled 500 -- the credential problem is real, but a
        # request without a session was never going to succeed anyway.
        raise CommandApiError(401, "SESSION_REQUIRED", "session required") from exc


# Process-local: ties the derived CSRF token to a secret this process alone
# knows, so a token can only ever be recomputed on this process (never
# guessed offline from the session id format).
_CSRF_SECRET = secrets.token_bytes(32)


def session_csrf_token(session: str) -> str:
    """Derive a CSRF token bound to this request's session identity.

    ``session.py`` has no CSRF helper to import (see ``require_session``
    above for the same gap). Keyed-hashing the session's signed cookie
    value here ties the token to ONE session -- a token captured for
    session A is worthless replayed with session B's cookie -- without any
    change to session.py.
    """
    return hmac.new(_CSRF_SECRET, session.encode(), hashlib.sha256).hexdigest()


def require_command_auth(
    request: Request,
    session: str = Depends(require_session),
) -> str:
    _check_origin(request)
    supplied = request.headers.get("X-CSRF-Token", "")
    if not secrets.compare_digest(supplied, session_csrf_token(session)):
        raise CommandApiError(403, "CSRF_REJECTED", "session CSRF token mismatch")
    # [M1-C] Task 3 (I-2): this gate checks `commands_enabled` ONLY, not
    # `live_commands_enabled` -- deliberately. Both routes behind this
    # dependency (`create_proposal`, `close_position`) only ever create a
    # NON-EXECUTING PENDING proposal; nothing here places or transmits an
    # order. The live-command gate (`DASHBOARD_LIVE_COMMANDS_ENABLED` /
    # the `LIVE_COMMANDS_DISABLED` code already reserved in `_HTTP_STATUS`
    # above) belongs on the EXECUTING command -- the approve route -- which
    # is [M1-C] Task 4, not this one. Do not add a live-commands check here.
    flags: CommandFlags = request.app.state.command_flags
    if not flags.commands_enabled:
        raise CommandApiError(403, "COMMANDS_DISABLED",
                              "dashboard commands are disabled "
                              "(DASHBOARD_COMMANDS_ENABLED=false)")
    return session


def _gateway(request: Request) -> DashboardCommandGateway:
    """Resolve the live gateway, distinguishing "feature is off" (403) from
    "feature is on but the gateway isn't up" (503).

    [M1-C] Task 3 fix (I-1/M-6): the gateway is now built lazily by
    `CommandCenter._start_or_degrade` (never eagerly in `create_app()`), so
    it lives on `request.app.state.command_center.command_gateway` --
    reached the same way `require_session` above already reaches
    `command_center` -- not on a separate `app.state.command_gateway`
    attribute set once at boot.
    """
    center = getattr(request.app.state, "command_center", None)
    gateway = getattr(center, "command_gateway", None) if center is not None else None
    if gateway is not None:
        return gateway
    flags: CommandFlags = request.app.state.command_flags
    if not flags.commands_enabled:
        raise CommandApiError(403, "COMMANDS_DISABLED",
                              "dashboard commands are disabled "
                              "(DASHBOARD_COMMANDS_ENABLED=false)")
    # Commands ARE enabled but the gateway never came up -- the command
    # center degraded to inert at startup (bad/missing service HMAC key,
    # unreachable command socket, ...; see `_start_or_degrade`). That is a
    # TRANSIENT condition, not "the feature is off", so it is 503/retryable
    # rather than the 403 `COMMANDS_DISABLED` above.
    raise CommandApiError(503, "COMMAND_GATEWAY_UNAVAILABLE",
                          "the command gateway is temporarily unavailable; "
                          "retry shortly", retryable=True)


def _receipt_json(receipt: CommandReceipt) -> JSONResponse:
    return JSONResponse(status_code=202, content={
        "command_id": receipt.command_id,
        "correlation_id": receipt.correlation_id,
        "state": receipt.state,
    })


# [M1-C] Task 3 fix (M-7): `command_id` was previously length-checked only,
# so a 36-char value smuggling a `:` (e.g. a hyphen swapped for a colon)
# passed this web layer and was only rejected one hop later, at the
# coordinator (`trader.trading.command_coordinator`'s
# `CommandRequest.__post_init__`, mirrored web-adjacent by
# `trader.messaging.production_api._reject_colon_in_command_id`): a colon
# inside `command_id` corrupts `encode_order_ref`'s `mmr:<command_id>`
# orderRef encoding, which reserves `:` as its own separator. The pattern
# below enforces the full UUID shape (8-4-4-4-12 hex groups) up front, which
# also excludes `:` by construction -- failing fast, at THIS boundary,
# instead of a plausible-looking 202 that the coordinator rejects later.
_COMMAND_ID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_COMMAND_ID = Field(min_length=36, max_length=36, pattern=_COMMAND_ID_PATTERN)


class CreateProposalBody(BaseModel):
    """Mirrors ``trader.messaging.production_api.CreateProposalRequest``
    field-for-field (minus ``preflight_nonce``, which this single-POST task
    doesn't use -- preflight confirmation is a later task). ``conid``, not
    ``symbol``: the coordinator only knows conIds (see module docstring)."""

    model_config = ConfigDict(extra="forbid")
    command_id: str = _COMMAND_ID
    conid: int = Field(gt=0)
    action: Literal["BUY", "SELL"]
    quantity: float | None = Field(default=None, gt=0)
    amount: float | None = Field(default=None, gt=0)
    reasoning: str = Field(default="", max_length=8000)
    confidence: float = Field(default=0.0, ge=0, le=1)
    thesis: str = Field(default="", max_length=4000)
    group: str = Field(default="", max_length=40)
    max_price_drift_bps: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _cross_field(self) -> "CreateProposalBody":
        if self.quantity is not None and self.amount is not None:
            raise ValueError("give quantity OR amount; leave both empty "
                             "for automatic position sizing")
        return self


class ClosePositionBody(BaseModel):
    """A close is a regular reducing ``create_proposal`` (module docstring)
    -- the browser already knows the position's sign from the row it's
    rendering the close button next to, so it supplies ``action`` the same
    way the CLI's ``close`` command does."""

    model_config = ConfigDict(extra="forbid")
    command_id: str = _COMMAND_ID
    action: Literal["BUY", "SELL"]
    quantity: float = Field(gt=0)
    reasoning: str = Field(default="", max_length=8000)


@router.get("/api/commands/csrf-token")
def csrf_token(request: Request, session: str = Depends(require_session)):
    """Lets the browser learn its session-bound CSRF token.

    The plan this router was drafted against assumed a
    ``<meta name="cc-csrf-token">`` already rendered into the page by an
    earlier task; no such tag exists (``session.py``'s cookie is HttpOnly
    and [M1-R] never minted a CSRF token at all -- see the module
    docstring). A plain authenticated GET is the standard way an SPA
    consuming an HttpOnly session cookie learns a CSRF token; it needs no
    CSRF/origin check itself since it isn't a mutation.
    """
    return {"csrf_token": session_csrf_token(session)}


@router.post("/api/commands/proposals", status_code=202)
def create_proposal(body: CreateProposalBody, request: Request,
                    session: str = Depends(require_command_auth)):
    # Source, account, and mode are derived server-side by the coordinator
    # (spec 9.6) -- never sent by the browser.
    receipt = _gateway(request).execute("create_proposal", body.model_dump())
    return _receipt_json(receipt)


@router.post("/api/commands/positions/{account}/{conid}/close", status_code=202)
def close_position(account: str, conid: int, body: ClosePositionBody,
                   request: Request,
                   session: str = Depends(require_command_auth)):
    # `account` is accepted for URL/audit clarity (it identifies which
    # account's position row the browser is closing) but is NOT part of the
    # frozen `create_proposal` wire contract -- the coordinator's account is
    # server-configured, not request-supplied (mirrors `sdk.propose()`'s
    # documented inert `source`/`metadata` kwargs) -- so it is deliberately
    # left out of the forwarded body below rather than smuggled in as an
    # extra field the pydantic model on the other end would reject anyway.
    receipt = _gateway(request).execute("create_proposal", {
        "command_id": body.command_id,
        "conid": conid,
        "action": body.action,
        "quantity": body.quantity,
        "reasoning": body.reasoning,
    })
    return _receipt_json(receipt)


@router.get("/api/commands/{command_id}")
def get_command(
    request: Request,
    # [M1-C] Task 3 fix (M-7): the path param gets the SAME UUID-shape/
    # colon-free enforcement as the body-carried `command_id` fields above --
    # this one was previously unvalidated (not even length-checked).
    command_id: str = Path(min_length=36, max_length=36,
                           pattern=_COMMAND_ID_PATTERN),
    session: str = Depends(require_session),
):
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
