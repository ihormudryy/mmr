"""Production typed-RPC registry — the ONLY RPC surface a live ``trader_service``
exposes (G0 Task 4).

Tasks 2/3 built the authenticated typed transport (``TypedRpcRegistry``,
``TypedRpcServer``, ``HmacServiceAuthenticator``) but nothing had registered
any methods on it yet. Its legacy sibling — the dill/msgpack ``RPCServer``
in ``clientserver.py`` serving ``trader_service_api.TraderServiceApi`` (and,
before this task, its direct-order methods) — is a production
authorization-bypass surface: it returns raw Python objects (a ``dill``
fallback covers anything msgpack can't natively encode) with no schema
validation and no typed-transport authentication.

This module is the other half of the split:

- ``validate_rpc_mode`` is the fail-closed guard making sure the legacy
  dill-capable server (now ``legacy_offline_api.LegacyOfflineTraderServiceApi``)
  can never be started outside an explicit offline-simulation opt-in. There
  is no flag combination that lets ``unsafe_legacy_rpc`` run against a live
  trader_service — see ``Trader.connect()``, which calls this before it ever
  considers starting the legacy ``RPCServer``.
- ``build_production_registry`` wires a handful of SAFE, read-only/health
  methods onto the typed ``query`` role using plain-dict request/response
  schemas (nothing here needs a bespoke Pydantic model yet — the values are
  already JSON-safe dicts). By default (no ``[M1-F3]`` services supplied) it
  registers NOTHING on the ``command`` role. In particular, none of the five
  methods that today let a caller bypass proposal review
  (``place_order_simple``, ``place_expressive_order``,
  ``place_standalone_order``, ``set_risk_limits``, ``cancel_all``) are ever
  reachable through this registry, on either role — that surface stays behind
  the offline-simulation-only legacy path (see ``validate_rpc_mode`` above).

[M1-F1] Task 5 addition -- event-foundation read methods
-----------------------------------------------------------
``build_production_registry`` also accepts two OPTIONAL keyword-only
services: ``snapshot_service`` (a ``DomainSnapshotService``) and
``feed_service`` (a ``DomainFeedService``). When supplied, they register
``snapshot_with_cursor`` on role ``query`` and ``read_domain_events`` on
role ``feed`` respectively. Both default to ``None`` and are simply omitted
from the registry when absent, so every existing call site
(``Trader.connect()``, and ``_FakeTrader``-based tests like
``test_production_rpc_security.py``) keeps working unchanged.

The optional arguments keep isolated registry tests and non-trader callers
lightweight. ``Trader.connect()`` now constructs the journal against its
dedicated ``journal_duckdb_path`` and supplies both services, so these read
methods are reachable on the live typed sockets.

Both handlers translate their domain-layer exceptions into wire-level
``RpcProblem`` codes via ``typed_rpc._DispatchProblem`` (the same mechanism
``snapshot_service.py``'s own docstring documents): ``SnapshotNotReady`` ->
``SNAPSHOT_NOT_READY`` (dormant in F1 per BLOCKER-2 -- never actually raised
yet, but the mapping is wired now) and ``CursorExpired`` -> ``CURSOR_EXPIRED``
(RA-5). Without this, both would fall through to the generic scrubbed
``INTERNAL_ERROR`` catch-all in ``TypedRpcServer._handle_request``, and a
caller could never distinguish "must re-snapshot" from "the server broke".

[M1-F3] Task 3 addition -- command authority
-----------------------------------------------
``build_production_registry`` also accepts three OPTIONAL keyword-only
collaborators: ``command_coordinator`` (a ``TradingCommandCoordinator``),
``proposal_service`` (a ``ProposalCommandService``), and
``proposal_repository`` (a ``ProposalRepository``). When ALL THREE are
supplied, ``register_command_authority`` wires ``create_proposal`` and
``reject_proposal`` onto the ``command`` role, and ``get_command``,
``get_proposal``, and ``list_proposals`` onto ``query``. Every request model
here is Pydantic with ``extra="forbid"`` and carries no risk-bypass field —
there is no ``skip_risk_gate`` anywhere on this surface, and risk-limit
administration (``set_risk_limits``) is still not registered on ``command``
by this or any other addition. Account id is derived from the trusted
``trader.ib_account`` server-side value, never from the caller's request
body. ``CreateProposalRequest``/``RejectProposalRequest`` also reject a
colon-bearing ``command_id`` at the field-validator level (see
``_reject_colon_in_command_id``) so a malformed wire value fails with a
clean ``VALIDATION_ERROR`` at request-coercion time instead of a bare
``ValueError`` raised deep inside the handler by
``CommandRequest.__post_init__`` -- which ``TypedRpcServer``'s catch-all
would otherwise scrub to an opaque ``INTERNAL_ERROR``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import logging
import os
from dataclasses import asdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional

from ib_async import Contract
from pydantic import BaseModel, ConfigDict, Field, field_validator

from trader.data.proposal_repository import ProposalRepository
from trader.domain.commands import CommandReceipt
from trader.domain.feed_service import CURSOR_EXPIRED, CursorExpired, DomainFeedService, domain_event_to_wire
from trader.domain.snapshot_service import SNAPSHOT_NOT_READY, DomainSnapshotService, SnapshotNotReady
from trader.messaging.strategy_trader_contracts import (
    PublishInstrumentRequest,
    PublishInstrumentResponse,
    ResolveInstrumentRequest,
    ResolveInstrumentResponse,
)
from trader.messaging.manage_surface import register_manage_surface
from trader.messaging.trader_service_api import TraderServiceApi
from trader.messaging.typed_rpc import (
    HmacServiceAuthenticator,
    TypedRpcClient,
    TypedRpcRegistry,
    TypedRpcRemoteError,
    _DispatchProblem,
)
from trader.strategy.strategy_revisions import StrategyCommandReceipt
from trader.trading.command_coordinator import (
    ApprovalCommandService,
    CancelCommandService,
    CommandRequest,
    CommandValidationError,
    StrategyControlCommandService,
    TradingCommandCoordinator,
    acknowledge_strategy_state,
    canonical_request_hash,
)
from trader.trading.proposal_command_service import (
    ProposalCommandService,
    ProposalCreateRequest,
    ProposalCreationRefused,
)

if TYPE_CHECKING:
    from trader.trading.command_stack import CommandStack
from trader.trading.trading_control import (
    PauseRevisionConflict,
    PauseStateUnavailable,
    TradingControlStore,
)


def validate_rpc_mode(simulation: bool, unsafe_legacy_rpc: bool) -> None:
    """Fail closed: refuse ``unsafe_legacy_rpc`` outside offline simulation.

    This is the single choke point that makes "legacy dill/object RPC in
    production" structurally impossible: ``Trader.connect()`` calls this
    unconditionally, before it ever considers starting the legacy
    ``RPCServer``, so there is no flag combination that reaches
    ``LegacyOfflineTraderServiceApi`` unless BOTH ``simulation`` and
    ``unsafe_legacy_rpc`` are explicitly ``True``. ``simulation=True`` alone
    is not enough (an offline backtest/dry-run shouldn't silently get the
    dill-capable RPC either) — the caller must opt in explicitly.
    """
    if unsafe_legacy_rpc and not simulation:
        raise ValueError(
            'unsafe_legacy_rpc=True requires simulation=True (offline simulation '
            'only) -- the dill-capable legacy RPC path must never run against a '
            'live/production trader_service.'
        )


def _no_arg_handler(fn):
    """Adapt a zero-argument read method to the ``(body: dict) -> dict``
    handler shape ``TypedRpcRegistry``/``TypedRpcServer`` expect. The typed
    transport always hands the handler a parsed body even for argument-less
    calls (an empty ``{}``), so every registered handler here takes exactly
    one positional parameter regardless of whether it uses it.
    """
    def _handler(_body: Dict[str, Any]) -> Dict[str, Any]:
        return fn()
    return _handler


def _instrument_to_wire(definition: Any) -> Dict[str, Any]:
    """Project a resolved ``SecurityDefinition`` down to the JSON-safe fields
    the strategy runtime needs: the six that build an IB ``Contract`` plus the
    IANA timezone the historical fetch reads. Coerced to plain ``int``/``str``
    so a DuckDB-backed numeric conId still validates under the response
    model's strict mode."""
    return {
        "instrument_id": int(definition.conId),
        "symbol": str(definition.symbol),
        "exchange": str(definition.exchange),
        "primary_exchange": str(definition.primaryExchange),
        "currency": str(definition.currency),
        "security_type": str(definition.secType),
        "time_zone_id": str(definition.timeZoneId),
    }


_INSTRUMENTS_UNIVERSE = '_instruments'


def _stub_security_definition(instrument_id: int):
    """Minimal SecurityDefinition for fake-broker / offline seeding.

    Carries enough fields for ``_instrument_to_wire`` + Contract construction.
    Symbol is ``C{conId}`` — operators should replace via real IB discovery
    before live trading; this exists so paper/fake multi-strategy books with
    YAML conIds don't stay stuck in ERROR on an empty universe DB.
    """
    from trader.data.data_access import SecurityDefinition
    return SecurityDefinition(
        symbol=f'C{int(instrument_id)}',
        exchange='SMART',
        conId=int(instrument_id),
        secType='STK',
        primaryExchange='NASDAQ',
        currency='USD',
        tradingClass='',
        includeExpired=False,
        secIdType='',
        secId='',
        description='',
        minTick=0.01,
        orderTypes='',
        validExchanges='',
        priceMagnifier=1.0,
        longName='',
        category='',
        subcategory='',
        tradingHours='',
        timeZoneId='America/New_York',
        liquidHours='',
        stockType='',
        minSize=1.0,
        sizeIncrement=1.0,
        suggestedSizeIncrement=1.0,
        bondType='',
        couponType='',
        callable=False,
        putable=False,
        coupon=0.0,
        convertable=False,
        maturity='',
        issueDate='',
        nextOptionDate='',
        nextOptionPartial=False,
        nextOptionType='',
        marketRuleIds='',
    )


def _cache_resolved_instrument(api: TraderServiceApi, definition) -> None:
    """Persist a newly qualified SecurityDefinition into a bookkeeping
    universe so later local resolves and publish_instrument succeed."""
    try:
        trader = api.trader
        accessor = getattr(trader, 'universe_accessor', None)
        if accessor is None:
            from trader.data.universe import UniverseAccessor
            accessor = UniverseAccessor(trader.duckdb_path, trader.universe_library)
        universe = accessor.get(_INSTRUMENTS_UNIVERSE)
        if any(int(d.conId) == int(definition.conId) for d in universe.security_definitions):
            return
        accessor.insert(_INSTRUMENTS_UNIVERSE, definition)
    except Exception as exc:  # noqa: BLE001 - resolve still succeeds; cache is best-effort
        logging.warning(
            'failed to cache instrument %s into %s: %s',
            getattr(definition, 'conId', '?'), _INSTRUMENTS_UNIVERSE, exc,
        )


async def _ensure_instrument(api: TraderServiceApi, instrument_id: int) -> list:
    """Local resolve, then exact-conId IB qualify, then fake-broker stub.

    Shared by ``resolve_instrument`` and ``publish_instrument`` so a strategy
    that just resolved an instrument can also publish it without a second
    local-only miss.
    """
    definitions = await api.resolve_symbol(instrument_id)
    if definitions:
        return definitions
    try:
        definitions = await api.resolve_contract(Contract(conId=instrument_id))
    except Exception as exc:  # noqa: BLE001 - fall through to stub/empty
        logging.warning(
            'resolve_contract(%s) failed during instrument resolve: %s',
            instrument_id, exc,
        )
        definitions = []
    if not definitions and os.environ.get('MMR_FAKE_BROKER') == '1':
        # Env check (not _fake_broker_enabled()) — connect() already enforced
        # the fail-loud gate; calling it again from a query handler can raise
        # mid-request if account flags drift.
        definitions = [_stub_security_definition(instrument_id)]
    if definitions:
        _cache_resolved_instrument(api, definitions[0])
        cached = await api.resolve_symbol(instrument_id)
        if cached:
            return cached
    return definitions


def _resolve_instrument_handler(api: TraderServiceApi):
    """Resolve a conId for strategy subscription.

    Prefer the trader's local universe DB (same as legacy ``resolve_symbol``).
    On a miss, qualify the *exact* conId via IB ``reqContractDetails`` —
    ``Contract(conId=N)`` is an unambiguous primary-key lookup, not a fuzzy
    symbol search — and cache the definition so subsequent resolves and
    ``publish_instrument`` hit the local DB.

    Under ``MMR_FAKE_BROKER=1`` (no IB), seed a stub definition so multi-
    strategy YAML books with hardcoded conIds can leave ERROR and subscribe.
    """
    async def _handler(parsed: ResolveInstrumentRequest) -> Dict[str, Any]:
        definitions = await _ensure_instrument(api, int(parsed.instrument_id))
        return {"instruments": [_instrument_to_wire(d) for d in definitions]}
    return _handler


def _publish_instrument_handler(api: TraderServiceApi):
    """Start streaming an instrument's ticks to the pubsub. The trader resolves
    the conId ITSELF and never trusts a client-supplied contract — a
    partially-specified contract can resolve to the wrong listing (the
    ``4391 -> TSEJ`` class of bug), so the strategy passes only the conId. The
    ``publish_contract`` call runs inline on the trader loop where ib_async
    lives, matching the legacy RPC's behaviour."""
    async def _handler(parsed: PublishInstrumentRequest) -> Dict[str, Any]:
        definitions = await _ensure_instrument(api, int(parsed.instrument_id))
        if not definitions:
            raise _DispatchProblem(
                "INSTRUMENT_NOT_FOUND",
                f"no instrument {parsed.instrument_id!r} in the trader universe",
            )
        d = definitions[0]
        # The same six fields SecurityDefinition.to_contract builds, kept
        # explicit so the subscription contract is decoupled from the full
        # SecurityDefinition type (and testable with a lightweight fake).
        contract = Contract(
            conId=d.conId, symbol=d.symbol, secType=d.secType, exchange=d.exchange,
            primaryExchange=d.primaryExchange, currency=d.currency,
        )
        try:
            api.publish_contract(contract=contract, delayed=parsed.delayed)
        except ConnectionError:
            # Fake-broker / disconnected IB: instrument is resolved and cached
            # but there is no live market-data socket. Treat as published so
            # multi-strategy startup does not crash; reconcile retries later.
            if os.environ.get('MMR_FAKE_BROKER') != '1':
                raise
            logging.warning(
                'publish_contract(%s) skipped under MMR_FAKE_BROKER (not connected)',
                parsed.instrument_id,
            )
        return {"published": True}
    return _handler


def _read_domain_events_handler(feed_service: DomainFeedService):
    # ASYNC + off-loop: read_domain_events long-polls via a *synchronous*
    # threading.Condition wait (up to wait_ms, 10s in production). All three
    # typed servers share trader_service's single asyncio event loop -- the
    # SAME loop ib_async runs IB socket/market-data processing on. Running the
    # blocking wait inline would freeze that loop for the full wait_ms every
    # poll (the dashboard re-polls immediately, so effectively continuously),
    # starving IB tick/heartbeat handling -> stale/empty quotes and spurious
    # 1100 disconnects. asyncio flags this as "Executing <Task ...> took 10s".
    # Delegating the blocking read to a worker thread lets `_handle_request`
    # await it and yield the loop; DomainFeedService.read_domain_events is
    # thread-safe (Condition wakeups + DuckDB via the per-db-locked wrapper).
    async def _handler(body: Dict[str, Any]) -> Dict[str, Any]:
        def _blocking_read():
            return feed_service.read_domain_events(
                after_cursor=body['after_cursor'],
                limit=body['limit'],
                wait_ms=body['wait_ms'],
            )
        try:
            result = await asyncio.to_thread(_blocking_read)
        except CursorExpired as exc:
            raise _DispatchProblem(CURSOR_EXPIRED, str(exc)) from exc
        return {
            'events': [domain_event_to_wire(event) for event in result.events],
            'newest_cursor': result.newest_cursor,
        }
    return _handler


def _snapshot_with_cursor_handler(snapshot_service: DomainSnapshotService):
    def _handler(_body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            snapshot = snapshot_service.snapshot_with_cursor()
        except SnapshotNotReady as exc:
            raise _DispatchProblem(SNAPSHOT_NOT_READY, str(exc)) from exc
        return {
            'source_cursor': snapshot.source_cursor,
            'broker_generation': snapshot.broker_generation,
            'entities': snapshot.entities,
        }
    return _handler


# ---------------------------------------------------------------------------
# [M1-F3] Task 3 — command authority: create_proposal / reject_proposal on
# the ``command`` role, get_command / get_proposal / list_proposals on
# ``query``. Every request model is ``extra="forbid"`` (Global Constraint:
# "No production command schema carries a caller-controlled risk bypass") —
# there is deliberately no ``skip_risk_gate`` field anywhere on this surface,
# and risk-limit administration (``set_risk_limits``) is not registered here
# at all, on either role.
# ---------------------------------------------------------------------------

def _reject_colon_in_command_id(value: str) -> str:
    """Shared ``command_id`` field-validator body for the two mutating
    request models below.

    Mirrors ``CommandRequest.__post_init__`` (``command_coordinator.py``):
    ``encode_order_ref`` reserves ``':'`` for the ``mmr:`` orderRef prefix,
    so a colon inside ``command_id`` would corrupt that encoding. That
    ``__post_init__`` check only fires once ``CommandRequest`` is
    constructed -- deep inside the RPC handler -- so a malformed wire
    ``command_id`` previously surfaced as a bare ``ValueError`` that
    ``TypedRpcServer``'s unhandled-exception catch-all scrubs to an opaque
    ``INTERNAL_ERROR`` ("internal error"), giving the caller no clue their
    own input was malformed. Validating here, at request-coercion time,
    turns that into a clean ``VALIDATION_ERROR`` instead. This is
    defense-in-depth ALONGSIDE (not a replacement for) the
    ``CommandRequest.__post_init__`` check, which still guards every other
    ``CommandRequest`` construction path.
    """
    if ":" in value:
        raise ValueError(
            f"command_id must not contain ':' (encode_order_ref reserves it "
            f"for the mmr: orderRef prefix): {value!r}"
        )
    return value


class CreateProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    conid: int
    action: str
    quantity: Optional[float] = None
    amount: Optional[float] = None
    reasoning: str = ""
    confidence: float = 0.0
    thesis: str = ""
    group: str = ""
    max_price_drift_bps: Optional[float] = None
    preflight_nonce: Optional[str] = None

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)


class RejectProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    proposal_id: int
    reason: str

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)


class PauseTradingRequest(BaseModel):
    """Risk-reducing absolute pause; account and mode are trader-owned."""
    model_config = ConfigDict(extra="forbid")
    command_id: str
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)

    @field_validator("reason")
    @classmethod
    def _reason_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


class LiquidateAccountRequest(BaseModel):
    """Authenticated emergency flatten request; the account is server-pinned."""
    model_config = ConfigDict(extra="forbid")
    command_id: str
    reason: str = Field(min_length=1, max_length=200)
    preflight_nonce: Optional[str] = None
    session_fingerprint: Optional[str] = None

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)


class ResumeTradingRequest(BaseModel):
    """Risk-increasing resume; mode is pinned by trader_service."""
    model_config = ConfigDict(extra="forbid")
    command_id: str
    expected_control_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=200)
    preflight_nonce: Optional[str] = None
    session_fingerprint: Optional[str] = None

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)

    @field_validator("reason")
    @classmethod
    def _reason_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


class PreflightCommandRequest(BaseModel):
    """Authenticated confirmation request used to mint a bound nonce."""
    model_config = ConfigDict(extra="forbid")
    command_id: str
    action: str
    params: dict[str, Any]
    expected_version: Optional[int] = Field(default=None, ge=1)
    session_fingerprint: str = Field(min_length=16, max_length=256)

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        allowed = {
            "approve_proposal", "resume_trading", "cancel_order", "cancel_orders",
            "liquidate_account", "activate_live_canary", "activate_allocation",
        }
        if value not in allowed:
            raise ValueError(f"action must be one of {sorted(allowed)}")
        return value


class ApproveProposalRequest(BaseModel):
    """[M1-F3] Task 5. Approving a proposal is the ONE command that dispatches
    a real order. Live mode requires a preflight nonce; paper mode uses the
    authenticated single POST. ``expected_version`` is the exact proposal
    ``revision`` the caller reviewed -- a stale approval is rejected
    (``REVISION_MISMATCH``) rather than acting on a proposal that changed.
    The account is the coordinator's own configured account, never
    request-supplied (there is no ``account_id`` field)."""

    model_config = ConfigDict(extra="forbid")

    command_id: str
    proposal_id: int
    expected_version: int
    preflight_nonce: Optional[str] = None
    session_fingerprint: Optional[str] = None

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)


class _EntryPolicyWire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_type: Literal["LIMIT", "MARKETABLE_LIMIT"]
    limit_offset_bps: Decimal
    tif: Literal["DAY"]


class _StopPolicyWire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stop_price: Decimal
    order_type: Literal["STP", "STP_LMT"]


class _TargetPolicyWire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_price: Decimal
    order_type: Literal["LMT"]


class _TimeExitPolicyWire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_hold_bars: Optional[int] = None
    close_by: dt.datetime


class ExecuteAutomatedIntentRequest(BaseModel):
    """[P3 Task 3] Strategy-service-only automated intent command.

    Carries the full frozen ``ExecutionIntent`` fields plus the artifact
    bundle digest. ``account_id`` is deliberately absent -- the trader pins
    its own account. Only the strategy-service principal may invoke this
    action; dashboard/browser/CLI must not register an HTTP route for it.
    """

    model_config = ConfigDict(extra="forbid")

    command_id: str
    artifact_id: str
    session_id: str
    bar_id: str
    signal_id: str
    intent_id: str
    account_mode: Literal["paper", "live"]
    conid: int
    side: Literal["BUY", "SELL"]
    requested_quantity: Optional[Decimal] = None
    risk_fraction: Decimal
    entry_policy: _EntryPolicyWire
    stop_policy: _StopPolicyWire
    target_policy: Optional[_TargetPolicyWire] = None
    time_exit_policy: _TimeExitPolicyWire
    artifact_digest: str
    eligibility_attestation_digest: str
    signal_timestamp: dt.datetime
    completed_bar_timestamp: dt.datetime
    artifact_bundle_digest: str

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)


class CancelOrderRequest(BaseModel):
    """[M1-F3] Task 6. Cancels one working order by its [M1-F2] entity id.

    Whether this needs a preflight nonce is NOT static here (unlike
    ``ApproveProposalRequest``): it is derived inside the coordinator's
    ``cancel_order`` saga from the order's classification (an entry-leg
    cancel is risk-REDUCING and never needs one; a protective-leg or
    unclassifiable-leg cancel is risk-INCREASING and does). The account is
    the coordinator's own configured account, never request-supplied.
    """

    model_config = ConfigDict(extra="forbid")

    command_id: str
    order_entity_id: str
    preflight_nonce: Optional[str] = None
    session_fingerprint: Optional[str] = None

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)


class CancelOrdersRequest(BaseModel):
    """[M1-F3] Task 6. Cancels a batch of working orders under one root
    command; the coordinator fans this out into one colon-free child
    ``cancel_order`` command per entry (see ``CancelCommandService.cancel_orders``)."""

    model_config = ConfigDict(extra="forbid")

    command_id: str
    order_entity_ids: list[str]
    preflight_nonce: Optional[str] = None
    session_fingerprint: Optional[str] = None

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)


class EnableStrategyRequest(BaseModel):
    """[M1-F3] Task 7. Forwarded through ``StrategyControlCommandService`` to
    strategy_service via ``StrategyControlPort.forward``. ``expected_control_revision``
    is the CAS guard strategy_service checks before applying the mutation --
    a stale value is rejected (``ControlRevisionConflict``) rather than acting
    on a strategy whose control state changed since the caller last read it."""

    model_config = ConfigDict(extra="forbid")

    command_id: str
    strategy_name: str
    expected_control_revision: int

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)


class DisableStrategyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    strategy_name: str
    expected_control_revision: int

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)


class UpdateStrategyParamsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    strategy_name: str
    expected_control_revision: int
    params: Dict[str, Any] = {}

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)


class RecordStateAcknowledgedRequest(BaseModel):
    """[M1-F3] Task 7. NOT routed through ``TradingCommandCoordinator`` --
    this is an internal strategy_service -> trader acknowledgement (backstop
    path for a state_revision bump whose original forwarded command's reply
    was lost), not a user-initiated mutation, so it carries no ``command_id``/
    ledger entry of its own. Idempotent: acknowledging the same
    ``state_revision`` twice is a safe no-op (see
    ``StrategyControlCommandService.acknowledge_state``)."""

    model_config = ConfigDict(extra="forbid")

    strategy_name: str
    state_revision: int
    control_revision: int
    payload: Dict[str, Any] = {}


class ActivateLiveCanaryRequest(BaseModel):
    """[P4 Task 5] Activates a signed canary authority for exactly one
    strategy on the trader's OWN pinned live account. ``attestation`` is the
    full wire form of a ``CanaryAttestation`` produced entirely OFFLINE by
    ``mmr research canary sign`` -- it carries a signature and public key ID,
    never a private key. Risk-increasing and live-only, so this ALWAYS
    requires a preflight nonce (unlike ``resume_trading``, which only
    requires one when the pinned account mode is live -- a canary authority
    is definitionally live-only, so there is no paper-mode carve-out here).
    """

    model_config = ConfigDict(extra="forbid")

    command_id: str
    attestation: Dict[str, Any]
    reason: str = Field(min_length=1, max_length=200)
    preflight_nonce: Optional[str] = None
    session_fingerprint: Optional[str] = None

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)

    @field_validator("reason")
    @classmethod
    def _reason_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


class DeactivateLiveCanaryRequest(BaseModel):
    """[P4 Task 5] Suspends an ACTIVE canary authority for ``strategy_id``.
    Risk-reducing (mirrors ``pause_trading``): never requires a preflight
    nonce."""

    model_config = ConfigDict(extra="forbid")

    command_id: str
    strategy_id: str
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)

    @field_validator("reason")
    @classmethod
    def _reason_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


class ActivateAllocationRequest(BaseModel):
    """[P5 Task 3] Activates a signed allocation authority for the trader's
    pinned account. ``attestation`` is the wire form from offline
    ``research allocation sign``. Risk-increasing — requires preflight nonce."""

    model_config = ConfigDict(extra="forbid")

    command_id: str
    attestation: Dict[str, Any]
    reason: str = Field(min_length=1, max_length=200)
    preflight_nonce: Optional[str] = None
    session_fingerprint: Optional[str] = None

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)

    @field_validator("reason")
    @classmethod
    def _reason_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


class SuspendAllocationRequest(BaseModel):
    """Suspends the account's active allocation authority (risk-reducing).
    Mirrors ``deactivate_live_canary``: never requires a preflight nonce."""

    model_config = ConfigDict(extra="forbid")

    command_id: str
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)

    @field_validator("reason")
    @classmethod
    def _reason_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


class ActivatePaperAutomationRequest(BaseModel):
    """Prepare restart-required paper automation materials and configuration."""

    model_config = ConfigDict(extra="forbid")

    command_id: str
    strategy_name: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=200)
    preflight_nonce: Optional[str] = None

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)

    @field_validator("strategy_name", "reason")
    @classmethod
    def _value_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value.strip()


class DeactivatePaperAutomationRequest(BaseModel):
    """Persist paper automation disablement; takes effect after restart."""

    model_config = ConfigDict(extra="forbid")

    command_id: str
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("command_id")
    @classmethod
    def _command_id_has_no_colon(cls, value: str) -> str:
        return _reject_colon_in_command_id(value)

    @field_validator("reason")
    @classmethod
    def _reason_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


class GetPaperAutomationStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetTradingControlRequest(BaseModel):
    """No fields: this always reads the coordinator's own configured
    account, exactly like the command above never accepts one."""

    model_config = ConfigDict(extra="forbid")


class GetCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str


class GetProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: int


class ListProposalsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = None
    limit: int = 50


def _receipt_to_dict(receipt: CommandReceipt) -> Dict[str, Any]:
    return asdict(receipt)


def _create_proposal_action(proposal_service: ProposalCommandService):
    """The coordinator-registered inner action for ``create_proposal``.

    Runs strictly AFTER the command has been claimed (ledger row +
    mandatory audit record already committed) — see
    ``TradingCommandCoordinator.execute``. Translates
    ``ProposalCreationRefused`` into ``CommandValidationError`` so the
    coordinator transitions the command to ``REJECTED`` with the refusal's
    code instead of leaking a raw domain exception.
    """
    def _action(command: CommandRequest) -> Dict[str, Any]:
        body = command.body
        request = ProposalCreateRequest(
            conid=body["conid"], action=body["action"],
            quantity=body.get("quantity"), amount=body.get("amount"),
            reasoning=body.get("reasoning", ""), confidence=body.get("confidence", 0.0),
            thesis=body.get("thesis", ""), group=body.get("group", ""),
            max_price_drift_bps=body.get("max_price_drift_bps"),
        )
        try:
            record = proposal_service.create_proposal(
                request, source=command.source, correlation_id=command.command_id,
            )
        except ProposalCreationRefused as exc:
            raise CommandValidationError(exc.code, exc.message) from exc
        return record.to_payload()
    return _action


def _reject_proposal_action(proposal_service: ProposalCommandService):
    def _action(command: CommandRequest) -> Dict[str, Any]:
        body = command.body
        try:
            record = proposal_service.reject_proposal(
                int(body["proposal_id"]), body["reason"], command.command_id,
            )
        except ProposalCreationRefused as exc:
            raise CommandValidationError(exc.code, exc.message) from exc
        return record.to_payload()
    return _action


def _create_proposal_rpc_handler(coordinator: TradingCommandCoordinator, account_id: Optional[str]):
    def _handler(parsed: CreateProposalRequest) -> Dict[str, Any]:
        payload = parsed.model_dump(exclude={"command_id", "preflight_nonce"})
        request = CommandRequest(
            command_id=parsed.command_id, action="create_proposal", account_id=account_id,
            target_type="proposal", target_id="", expected_version=None,
            body=payload, source="dashboard", preflight_nonce=parsed.preflight_nonce,
        )
        receipt = coordinator.execute(request)
        return _receipt_to_dict(receipt)
    return _handler


def _reject_proposal_rpc_handler(coordinator: TradingCommandCoordinator, account_id: Optional[str]):
    def _handler(parsed: RejectProposalRequest) -> Dict[str, Any]:
        payload = parsed.model_dump(exclude={"command_id"})
        request = CommandRequest(
            command_id=parsed.command_id, action="reject_proposal", account_id=account_id,
            target_type="proposal", target_id=str(parsed.proposal_id), expected_version=None,
            body=payload, source="dashboard", preflight_nonce=None,
        )
        receipt = coordinator.execute(request)
        return _receipt_to_dict(receipt)
    return _handler


def _approve_proposal_rpc_handler(coordinator: TradingCommandCoordinator, account_id: Optional[str]):
    """[M1-F3] Task 5. Builds the ``approve_proposal`` command envelope and
    drives it through the coordinator (which dispatches to the registered
    ``ApprovalCommandService.approve`` saga). ``target_id`` is the proposal id
    so ``unresolved_for_target`` can enforce the one-live-command-per-proposal
    rule (``COMMAND_IN_FLIGHT``); ``expected_version`` rides the envelope so
    the atomic claim can reject a stale approval race-safely."""
    def _handler(parsed: ApproveProposalRequest) -> Dict[str, Any]:
        request = CommandRequest(
            command_id=parsed.command_id, action="approve_proposal", account_id=account_id,
            target_type="proposal", target_id=str(parsed.proposal_id),
            expected_version=parsed.expected_version,
            body={"proposal_id": parsed.proposal_id}, source="dashboard",
            preflight_nonce=parsed.preflight_nonce,
            session_fingerprint=parsed.session_fingerprint,
        )
        receipt = coordinator.execute(request)
        return _receipt_to_dict(receipt)
    return _handler


def _execute_automated_intent_rpc_handler(
    coordinator: TradingCommandCoordinator, account_id: Optional[str],
):
    """[P3 Task 3] Strategy-service principal only — never ``source=dashboard``."""

    def _handler(parsed: ExecuteAutomatedIntentRequest) -> Dict[str, Any]:
        # JSON mode keeps ledger/audit persistence free of datetime objects.
        body = parsed.model_dump(mode="json")
        request = CommandRequest(
            command_id=parsed.command_id,
            action="execute_automated_intent",
            account_id=account_id,
            target_type="intent",
            target_id=parsed.intent_id,
            expected_version=None,
            body=body,
            source="strategy_service",
        )
        receipt = coordinator.execute(request)
        return _receipt_to_dict(receipt)

    return _handler


def _cancel_order_rpc_handler(coordinator: TradingCommandCoordinator, account_id: Optional[str]):
    """[M1-F3] Task 6. Builds the ``cancel_order`` command envelope and drives
    it through the coordinator (which dispatches to the registered
    ``CancelCommandService.cancel_order`` saga). ``target_id`` is the order's
    [M1-F2] entity id."""
    def _handler(parsed: CancelOrderRequest) -> Dict[str, Any]:
        request = CommandRequest(
            command_id=parsed.command_id, action="cancel_order", account_id=account_id,
            target_type="order", target_id=parsed.order_entity_id, expected_version=None,
            body={"order_entity_id": parsed.order_entity_id}, source="dashboard",
            preflight_nonce=parsed.preflight_nonce,
            session_fingerprint=parsed.session_fingerprint,
        )
        receipt = coordinator.execute(request)
        return _receipt_to_dict(receipt)
    return _handler


def _cancel_orders_rpc_handler(coordinator: TradingCommandCoordinator, account_id: Optional[str]):
    """[M1-F3] Task 6. Builds the root ``cancel_orders`` command envelope;
    ``CancelCommandService.cancel_orders`` (a non-saga action) fans it out
    into per-order children through the SAME coordinator."""
    def _handler(parsed: CancelOrdersRequest) -> Dict[str, Any]:
        request = CommandRequest(
            command_id=parsed.command_id, action="cancel_orders", account_id=account_id,
            target_type="order_group", target_id="", expected_version=None,
            body={"order_entity_ids": parsed.order_entity_ids}, source="dashboard",
            preflight_nonce=parsed.preflight_nonce,
            session_fingerprint=parsed.session_fingerprint,
        )
        receipt = coordinator.execute(request)
        return _receipt_to_dict(receipt)
    return _handler


def _strategy_receipt_to_dict(receipt: StrategyCommandReceipt) -> Dict[str, Any]:
    return dataclasses.asdict(receipt)


def _strategy_control_rpc_handler(
    coordinator: TradingCommandCoordinator, account_id: Optional[str], action: str,
):
    """[M1-F3] Task 7. Shared handler body for ``enable_strategy``/
    ``disable_strategy``/``update_strategy_params`` -- builds the
    ``CommandRequest`` envelope (``expected_version`` carries
    ``expected_control_revision``, ``target_type="strategy"``) and drives it
    through the coordinator, which dispatches to
    ``StrategyControlCommandService``'s forwarding saga."""
    def _handler(parsed) -> Dict[str, Any]:
        body: Dict[str, Any] = {"strategy_name": parsed.strategy_name}
        if action == "update_strategy_params":
            body["params"] = parsed.params
        request = CommandRequest(
            command_id=parsed.command_id, action=action, account_id=account_id,
            target_type="strategy", target_id=parsed.strategy_name,
            expected_version=parsed.expected_control_revision,
            body=body, source="dashboard",
        )
        receipt = coordinator.execute(request)
        return _receipt_to_dict(receipt)
    return _handler


def register_strategy_state_ingest(registry: "TypedRpcRegistry", journal) -> None:
    """Register ONLY ``record_state_acknowledged`` on the command role.

    This is the minimal command-socket surface the split-container production
    trader needs so strategy_service's state announcements/ack backstop can
    reach the domain journal (and therefore the command center's Strategies
    panel). It is deliberately NOT the command-authority wiring: the ack is
    an internal strategy_service -> trader notification with no market
    impact, no ledger row, and no preflight ceremony (see
    ``RecordStateAcknowledgedRequest``'s docstring), so exposing it does not
    open any user-facing command. ``enable_strategy`` / ``approve_proposal``
    / every other real command stays unregistered until the
    command-authority integration gate wires the coordinator."""
    from trader.data.domain_journal import DomainJournal
    if not isinstance(journal, DomainJournal):
        raise TypeError(f"journal must be a DomainJournal, got {type(journal)!r}")

    def _handler(parsed: RecordStateAcknowledgedRequest) -> Dict[str, Any]:
        entity_revision = acknowledge_strategy_state(
            journal, parsed.strategy_name, parsed.state_revision,
            parsed.control_revision, parsed.payload,
            correlation_id=f"strategy:{parsed.strategy_name}:ack",
        )
        return {"entity_revision": entity_revision}

    registry.register(
        "command", "record_state_acknowledged", RecordStateAcknowledgedRequest, dict,
        _handler,
    )


def _record_state_acknowledged_handler(strategy_control_service: StrategyControlCommandService):
    def _handler(parsed: RecordStateAcknowledgedRequest) -> Dict[str, Any]:
        entity_revision = strategy_control_service.acknowledge_state(
            parsed.strategy_name, parsed.state_revision, parsed.control_revision,
            parsed.payload, correlation_id=f"strategy:{parsed.strategy_name}:ack",
        )
        return {"entity_revision": entity_revision}
    return _handler


def _control_action(
    controls: TradingControlStore,
    account_id: Optional[str],
    *,
    paused: bool,
    resume_ready=None,
    reconciliation_complete=None,
):
    """Build one trader-owned absolute control mutation."""
    def _action(command: CommandRequest) -> Dict[str, Any]:
        body = command.body
        if not paused:
            if resume_ready is None or not resume_ready():
                raise CommandValidationError(
                    "TRADER_NOT_READY", "trader/broker readiness is not current",
                )
            if reconciliation_complete is None or not reconciliation_complete(command.command_id):
                raise CommandValidationError(
                    "RECONCILIATION_INCOMPLETE",
                    "unresolved command reconciliation blocks resume",
                )
        try:
            state = controls.set(
                account_id,
                paused,
                body.get("expected_control_revision") if not paused else None,
                command.command_id,
                body["reason"],
                dt.datetime.now(dt.timezone.utc),
            )
        except PauseRevisionConflict as exc:
            raise CommandValidationError("PAUSE_REVISION_CONFLICT", str(exc)) from exc
        except PauseStateUnavailable as exc:
            raise CommandValidationError("PAUSE_STATE_UNAVAILABLE", str(exc)) from exc
        return state.to_payload()
    return _action


def _pause_trading_rpc_handler(coordinator: TradingCommandCoordinator, account_id: Optional[str]):
    def _handler(parsed: PauseTradingRequest) -> Dict[str, Any]:
        request = CommandRequest(
            command_id=parsed.command_id, action="pause_trading", account_id=account_id,
            target_type="trading_control", target_id=account_id or "",
            expected_version=None, body={"reason": parsed.reason}, source="dashboard",
        )
        receipt = coordinator.execute(request)
        return _receipt_to_dict(receipt)
    return _handler


def _activate_live_canary_rpc_handler(coordinator: TradingCommandCoordinator, account_id: Optional[str]):
    def _handler(parsed: ActivateLiveCanaryRequest) -> Dict[str, Any]:
        strategy_id = str(parsed.attestation.get("strategy_id", ""))
        request = CommandRequest(
            command_id=parsed.command_id, action="activate_live_canary", account_id=account_id,
            target_type="canary_authority", target_id=strategy_id, expected_version=None,
            body={"attestation": parsed.attestation, "reason": parsed.reason},
            # Human-operator-only surface: never "dashboard" (browser
            # automation) or "strategy_service" (a strategy can never
            # activate its own live canary) -- enforced again, redundantly,
            # inside CanaryActivationService.activate itself.
            source="operator",
            preflight_nonce=parsed.preflight_nonce, session_fingerprint=parsed.session_fingerprint,
        )
        return _receipt_to_dict(coordinator.execute(request))
    return _handler


def _deactivate_live_canary_rpc_handler(coordinator: TradingCommandCoordinator, account_id: Optional[str]):
    def _handler(parsed: DeactivateLiveCanaryRequest) -> Dict[str, Any]:
        request = CommandRequest(
            command_id=parsed.command_id, action="deactivate_live_canary", account_id=account_id,
            target_type="canary_authority", target_id=parsed.strategy_id, expected_version=None,
            body={"strategy_id": parsed.strategy_id, "reason": parsed.reason},
            source="operator",
        )
        return _receipt_to_dict(coordinator.execute(request))
    return _handler


def _activate_allocation_rpc_handler(coordinator: TradingCommandCoordinator, account_id: Optional[str]):
    def _handler(parsed: ActivateAllocationRequest) -> Dict[str, Any]:
        strategy_id = str(parsed.attestation.get("strategy_id", ""))
        request = CommandRequest(
            command_id=parsed.command_id, action="activate_allocation", account_id=account_id,
            target_type="allocation_authority", target_id=strategy_id, expected_version=None,
            body={"attestation": parsed.attestation, "reason": parsed.reason},
            source="operator",
            preflight_nonce=parsed.preflight_nonce, session_fingerprint=parsed.session_fingerprint,
        )
        return _receipt_to_dict(coordinator.execute(request))
    return _handler


def _suspend_allocation_rpc_handler(coordinator: TradingCommandCoordinator, account_id: Optional[str]):
    def _handler(parsed: SuspendAllocationRequest) -> Dict[str, Any]:
        request = CommandRequest(
            command_id=parsed.command_id, action="suspend_allocation", account_id=account_id,
            target_type="allocation_authority", target_id=account_id or "", expected_version=None,
            body={"reason": parsed.reason},
            source="operator",
        )
        return _receipt_to_dict(coordinator.execute(request))
    return _handler


def _paper_automation_action(paper_automation_service, *, activate: bool):
    """Translate coded activation refusals into command receipts."""
    from trader.automation.paper_activation import PaperAutomationActivationError

    def _action(command: CommandRequest) -> Dict[str, Any]:
        try:
            if activate:
                return paper_automation_service.activate(
                    strategy_name=command.body["strategy_name"],
                    reason=command.body["reason"],
                )
            return paper_automation_service.deactivate(reason=command.body["reason"])
        except PaperAutomationActivationError as exc:
            raise CommandValidationError(exc.code, str(exc)) from exc

    return _action


def _activate_paper_automation_rpc_handler(
    coordinator: TradingCommandCoordinator, account_id: Optional[str],
):
    def _handler(parsed: ActivatePaperAutomationRequest) -> Dict[str, Any]:
        request = CommandRequest(
            command_id=parsed.command_id,
            action="activate_paper_automation",
            account_id=account_id,
            target_type="paper_automation",
            target_id=parsed.strategy_name,
            expected_version=None,
            body={"strategy_name": parsed.strategy_name, "reason": parsed.reason},
            source="operator",
            preflight_nonce=parsed.preflight_nonce,
        )
        return _receipt_to_dict(coordinator.execute(request))

    return _handler


def _deactivate_paper_automation_rpc_handler(
    coordinator: TradingCommandCoordinator, account_id: Optional[str],
):
    def _handler(parsed: DeactivatePaperAutomationRequest) -> Dict[str, Any]:
        request = CommandRequest(
            command_id=parsed.command_id,
            action="deactivate_paper_automation",
            account_id=account_id,
            target_type="paper_automation",
            target_id=account_id or "",
            expected_version=None,
            body={"reason": parsed.reason},
            source="operator",
        )
        return _receipt_to_dict(coordinator.execute(request))

    return _handler


def _get_paper_automation_status_handler(paper_automation_service):
    def _handler(_parsed: GetPaperAutomationStatusRequest) -> Dict[str, Any]:
        return dataclasses.asdict(paper_automation_service.status())

    return _handler


def _liquidate_account_rpc_handler(coordinator: TradingCommandCoordinator, account_id: Optional[str]):
    def _handler(parsed: LiquidateAccountRequest) -> Dict[str, Any]:
        request = CommandRequest(
            command_id=parsed.command_id, action="liquidate_account", account_id=account_id,
            target_type="account", target_id=account_id or "", expected_version=None,
            body={"reason": parsed.reason}, source="dashboard",
            preflight_nonce=parsed.preflight_nonce, session_fingerprint=parsed.session_fingerprint,
        )
        return _receipt_to_dict(coordinator.execute(request))
    return _handler


def _resume_trading_rpc_handler(coordinator: TradingCommandCoordinator, account_id: Optional[str]):
    def _handler(parsed: ResumeTradingRequest) -> Dict[str, Any]:
        request = CommandRequest(
            command_id=parsed.command_id, action="resume_trading", account_id=account_id,
            target_type="trading_control", target_id=account_id or "",
            expected_version=parsed.expected_control_revision,
            body={
                "expected_control_revision": parsed.expected_control_revision,
                "reason": parsed.reason,
            },
            source="dashboard", preflight_nonce=parsed.preflight_nonce,
            session_fingerprint=parsed.session_fingerprint,
        )
        receipt = coordinator.execute(request)
        return _receipt_to_dict(receipt)
    return _handler


def _get_trading_control_handler(controls: TradingControlStore, account_id: Optional[str]):
    def _handler(_parsed: GetTradingControlRequest) -> Dict[str, Any]:
        try:
            state = controls.get(account_id)
        except PauseStateUnavailable as exc:
            raise _DispatchProblem("TRADING_CONTROL_UNAVAILABLE", str(exc)) from exc
        return state.to_payload()
    return _handler


def _preflight_command_handler(
    nonces,
    repository: ProposalRepository,
    controls: TradingControlStore,
    account_id: Optional[str],
    account_mode: str,
):
    """Mint a nonce bound to the exact command envelope later submitted."""
    def reject(message: str):
        raise _DispatchProblem("PREFLIGHT_INVALID", message)

    def exact_params(params: dict[str, Any], required: set[str]) -> None:
        if set(params) != required:
            reject(f"params must contain exactly {sorted(required)}")

    def _handler(parsed: PreflightCommandRequest) -> Dict[str, Any]:
        params = parsed.params
        expected = parsed.expected_version
        summary: dict[str, Any] = {
            "side": None,
            "instrument": None,
            "quantity": None,
            "notional": None,
            "order_type": None,
            "latest_price": None,
            "drift_bps": None,
            "warnings": [],
            "account_id": account_id,
            "account_mode": account_mode,
        }

        if parsed.action == "approve_proposal":
            exact_params(params, {"proposal_id"})
            if expected is None:
                reject("approve_proposal requires expected_version")
            try:
                proposal_id = int(params["proposal_id"])
            except (TypeError, ValueError):
                reject("proposal_id must be an integer")
            proposal = repository.get(proposal_id)
            if proposal is None:
                reject("proposal not found")
            if proposal.account_id != account_id or proposal.account_mode != account_mode:
                reject("proposal account or mode does not match pinned trader")
            if proposal.revision != expected:
                reject("proposal revision changed; refresh before confirming")
            request = CommandRequest(
                command_id=parsed.command_id,
                action=parsed.action,
                account_id=account_id,
                target_type="proposal",
                target_id=str(proposal_id),
                expected_version=expected,
                body={"proposal_id": proposal_id},
                source="dashboard",
                session_fingerprint=parsed.session_fingerprint,
            )
            price = proposal.reference_price
            notional = proposal.amount
            if notional is None and proposal.quantity is not None and price is not None:
                notional = abs(proposal.quantity * price)
            summary.update({
                "side": proposal.action,
                "instrument": proposal.symbol or proposal.conid,
                "quantity": proposal.quantity,
                "notional": notional,
                "order_type": (proposal.execution or {}).get("order_type", "MARKET"),
                "latest_price": price,
                "drift_bps": 0.0,
                "warnings": ["Market and broker evidence are revalidated immediately before dispatch."],
            })
        elif parsed.action == "resume_trading":
            exact_params(params, {"reason"})
            if expected is None:
                reject("resume_trading requires expected_version")
            reason = str(params["reason"]).strip()
            if not reason:
                reject("resume reason must not be blank")
            state = controls.get(account_id)
            if state.revision != expected:
                reject("trading-control revision changed; refresh before confirming")
            request = CommandRequest(
                command_id=parsed.command_id,
                action=parsed.action,
                account_id=account_id,
                target_type="trading_control",
                target_id=account_id or "",
                expected_version=expected,
                body={"expected_control_revision": expected, "reason": reason},
                source="dashboard",
                session_fingerprint=parsed.session_fingerprint,
            )
            summary.update({
                "side": "RESUME",
                "instrument": "new trading",
                "order_type": "CONTROL",
                "warnings": [
                    "Resume permits new exposure; readiness and reconciliation are checked again on submit."
                ],
            })
        elif parsed.action == "activate_live_canary":
            exact_params(params, {"attestation", "reason"})
            attestation = params["attestation"]
            if not isinstance(attestation, dict):
                reject("attestation must be an object")
            reason = str(params["reason"]).strip()
            if not reason:
                reject("activation reason must not be blank")
            strategy_id = str(attestation.get("strategy_id", ""))
            request = CommandRequest(
                command_id=parsed.command_id,
                action=parsed.action,
                account_id=account_id,
                target_type="canary_authority",
                target_id=strategy_id,
                expected_version=None,
                body={"attestation": attestation, "reason": reason},
                source="operator",
                session_fingerprint=parsed.session_fingerprint,
            )
            summary.update({
                "side": "ACTIVATE_CANARY",
                "instrument": strategy_id,
                "order_type": "LIVE_CANARY_AUTHORITY",
                "warnings": [
                    "Live canary authority is re-verified in full (signature, policy, "
                    "expiry, revocation, exact bindings) again on submit.",
                ],
            })
        elif parsed.action == "activate_allocation":
            exact_params(params, {"attestation", "reason"})
            attestation = params["attestation"]
            if not isinstance(attestation, dict):
                reject("attestation must be an object")
            reason = str(params["reason"]).strip()
            if not reason:
                reject("activation reason must not be blank")
            strategy_id = str(attestation.get("strategy_id", ""))
            request = CommandRequest(
                command_id=parsed.command_id,
                action=parsed.action,
                account_id=account_id,
                target_type="allocation_authority",
                target_id=strategy_id,
                expected_version=None,
                body={"attestation": attestation, "reason": reason},
                source="operator",
                session_fingerprint=parsed.session_fingerprint,
            )
            summary.update({
                "side": "ACTIVATE_ALLOCATION",
                "instrument": strategy_id,
                "order_type": "ALLOCATION_AUTHORITY",
                "warnings": [
                    "Allocation authority is re-verified in full (signature, policy, "
                    "expiry, revocation, exact bindings) again on submit.",
                ],
            })
        elif parsed.action == "cancel_order":
            exact_params(params, {"order_entity_id"})
            order_entity_id = str(params["order_entity_id"]).strip()
            if not order_entity_id:
                reject("order_entity_id must not be blank")
            request = CommandRequest(
                command_id=parsed.command_id,
                action=parsed.action,
                account_id=account_id,
                target_type="order",
                target_id=order_entity_id,
                expected_version=None,
                body={"order_entity_id": order_entity_id},
                source="dashboard",
                session_fingerprint=parsed.session_fingerprint,
            )
            summary.update({
                "side": "CANCEL",
                "instrument": order_entity_id,
                "order_type": "ORDER CONTROL",
                "warnings": ["Protective-order risk is classified again by trader_service."],
            })
        else:
            exact_params(params, {"order_entity_ids"})
            ids = params["order_entity_ids"]
            if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
                reject("order_entity_ids must be a non-empty list of strings")
            request = CommandRequest(
                command_id=parsed.command_id,
                action=parsed.action,
                account_id=account_id,
                target_type="order_group",
                target_id="",
                expected_version=None,
                body={"order_entity_ids": ids},
                source="dashboard",
                session_fingerprint=parsed.session_fingerprint,
            )
            summary.update({
                "side": "CANCEL ALL",
                "instrument": f"{len(ids)} working orders",
                "order_type": "ORDER CONTROL",
                "warnings": ["Every target is classified again by trader_service."],
            })

        nonce, expires_at = nonces.issue_with_expiry(
            command_id=parsed.command_id,
            account_id=account_id,
            account_mode=account_mode,
            session_fingerprint=parsed.session_fingerprint,
            request_hash=canonical_request_hash(request),
        )
        return {
            "command_id": parsed.command_id,
            "nonce": nonce,
            "expires_at": expires_at.isoformat(),
            "summary": summary,
        }

    return _handler


def _get_command_handler(coordinator: TradingCommandCoordinator):
    def _handler(parsed: GetCommandRequest) -> Dict[str, Any]:
        receipt = coordinator.get_command(parsed.command_id)
        if receipt is None:
            raise _DispatchProblem("COMMAND_NOT_FOUND", f"no command {parsed.command_id!r}")
        return _receipt_to_dict(receipt)
    return _handler


def _get_proposal_handler(repository: ProposalRepository):
    def _handler(parsed: GetProposalRequest) -> Dict[str, Any]:
        record = repository.get(parsed.proposal_id)
        if record is None:
            raise _DispatchProblem("PROPOSAL_NOT_FOUND", f"no proposal {parsed.proposal_id!r}")
        return record.to_payload()
    return _handler


def _list_proposals_handler(repository: ProposalRepository):
    def _handler(parsed: ListProposalsRequest) -> Dict[str, Any]:
        records = repository.list(parsed.status, parsed.limit)
        return {"proposals": [record.to_payload() for record in records]}
    return _handler


def register_command_authority(
    registry: TypedRpcRegistry,
    coordinator: TradingCommandCoordinator,
    proposal_service: ProposalCommandService,
    repository: ProposalRepository,
    *,
    account_id: Optional[str] = None,
    account_mode: Optional[str] = None,
    controls: Optional[TradingControlStore] = None,
    preflight_nonces=None,
    resume_ready=None,
    reconciliation_complete=None,
    approval_service: Optional[ApprovalCommandService] = None,
    cancel_service: Optional[CancelCommandService] = None,
    liquidation_service=None,
    strategy_control_service: Optional[StrategyControlCommandService] = None,
    automated_intent_service=None,
    canary_service=None,
    allocation_service=None,
    paper_automation_service=None,
) -> None:
    """Wire the command-authority surface onto ``registry``.

    Registers the inner per-action handlers on ``coordinator`` (so
    ``execute()`` has something to dispatch to) AND the typed RPC methods
    (``create_proposal``/``reject_proposal`` on ``command``,
    ``get_command``/``get_proposal``/``list_proposals`` on ``query``).
    Neither ``create_proposal`` nor ``reject_proposal`` requires a preflight
    nonce — creating a proposal has no market impact (execution only happens
    on a later, separate approval command) and rejecting one is always safe.
    ``account_id`` is supplied by the caller (derived server-side from the
    authenticated trader_service's own configuration) — it is never read
    from the request body, so a client cannot act on an account it doesn't
    own. When ``preflight_nonces`` is supplied, ``preflight_command`` is
    registered on the command role and mints a nonce bound to the exact
    trader-owned command envelope and browser-session fingerprint.

    [M1-F3] Task 4: when ``controls`` (a ``TradingControlStore``) is also
    supplied, ALSO registers explicit ``pause_trading`` and
    ``resume_trading`` commands plus ``get_trading_control``. Pause is
    risk-reducing and never requires preflight. Resume is risk-increasing:
    its preflight rule is derived from the pinned trader account mode and
    it additionally requires current readiness and complete reconciliation.
    No request field can override the account or mode.

    [M1-F3] Task 5: when ``approval_service`` (an ``ApprovalCommandService``)
    is also supplied, ALSO registers ``approve_proposal`` on ``command`` --
    the ONE command that dispatches a real order. It is a SAGA action
    (``saga=True``: the service drives its own SUBMITTING/SUBMITTED/
    OUTCOME_UNKNOWN transitions) and ALWAYS requires a preflight nonce. Omitted
    (the default) leaves the approval surface unregistered.

    [M1-F3] Task 6: when ``cancel_service`` (a ``CancelCommandService``) is
    also supplied, ALSO registers ``cancel_order`` (saga -- the coordinator's
    own static preflight gate is bypassed; the service derives the ceremony
    requirement itself from the order's risk classification) and
    ``cancel_orders`` (non-saga -- fans out into per-order ``cancel_order``
    children) onto ``command``. Omitted (the default) leaves the cancel
    surface unregistered.

    [M1-F3] Task 7: when ``strategy_control_service`` (a
    ``StrategyControlCommandService``) is also supplied, ALSO registers
    ``enable_strategy``, ``disable_strategy``, and ``update_strategy_params``
    (each a SAGA action -- the service forwards to strategy_service and
    drives its own RESOLVED/OUTCOME_UNKNOWN transitions; none requires a
    preflight nonce, mirroring create/reject_proposal's "no market impact at
    this step" rationale) plus ``record_state_acknowledged`` (registered
    directly on ``registry``, NOT through the coordinator -- it is an
    internal strategy_service -> trader acknowledgement, not a
    user-initiated command, so it carries no ledger/audit row of its own).
    Omitted (the default) leaves the strategy-control surface unregistered.

    [P4 Task 5]: when ``canary_service`` (a ``CanaryActivationService``) is
    also supplied, ALSO registers ``activate_live_canary`` (non-saga --
    stage-machine + authority-store transitions are the whole action, no
    broker order is dispatched; ALWAYS requires a preflight nonce, since a
    canary authority is definitionally live-only) and
    ``deactivate_live_canary`` (non-saga, risk-reducing, never requires a
    preflight nonce -- mirrors ``pause_trading``). Omitted (the default)
    leaves the live-canary surface unregistered, which is the case until an
    operator's canary public-key ring is configured (see
    ``command_stack.build_command_stack``).
    """
    if any(service is not None for service in (controls, preflight_nonces, approval_service,
                                                cancel_service)):
        if account_mode not in ("paper", "live"):
            raise ValueError(
                "account_mode must be pinned to 'paper' or 'live' for market-impact controls"
            )

    coordinator.register_action(
        "create_proposal", _create_proposal_action(proposal_service), requires_preflight=False,
    )
    coordinator.register_action(
        "reject_proposal", _reject_proposal_action(proposal_service), requires_preflight=False,
    )

    registry.register(
        "command", "create_proposal", CreateProposalRequest, dict,
        _create_proposal_rpc_handler(coordinator, account_id),
    )
    registry.register(
        "command", "reject_proposal", RejectProposalRequest, dict,
        _reject_proposal_rpc_handler(coordinator, account_id),
    )
    registry.register("query", "get_command", GetCommandRequest, dict, _get_command_handler(coordinator))
    registry.register("query", "get_proposal", GetProposalRequest, dict, _get_proposal_handler(repository))
    registry.register(
        "query", "list_proposals", ListProposalsRequest, dict, _list_proposals_handler(repository),
    )

    if preflight_nonces is not None:
        if controls is None or account_mode not in ("paper", "live"):
            raise ValueError("preflight requires pinned controls and account_mode")
        registry.register(
            "command", "preflight_command", PreflightCommandRequest, dict,
            _preflight_command_handler(
                preflight_nonces, repository, controls, account_id, account_mode,
            ),
        )

    if controls is not None:
        coordinator.register_action(
            "pause_trading",
            _control_action(controls, account_id, paused=True),
            requires_preflight=False,
        )
        coordinator.register_action(
            "resume_trading",
            _control_action(
                controls, account_id, paused=False,
                resume_ready=resume_ready,
                reconciliation_complete=reconciliation_complete,
            ),
            requires_preflight=account_mode == "live",
        )
        registry.register(
            "command", "pause_trading", PauseTradingRequest, dict,
            _pause_trading_rpc_handler(coordinator, account_id),
        )
        registry.register(
            "command", "resume_trading", ResumeTradingRequest, dict,
            _resume_trading_rpc_handler(coordinator, account_id),
        )
        registry.register(
            "query", "get_trading_control", GetTradingControlRequest, dict,
            _get_trading_control_handler(controls, account_id),
        )

    if liquidation_service is not None:
        coordinator.register_action(
            "liquidate_account", liquidation_service.liquidate,
            requires_preflight=account_mode == "live", saga=True,
        )
        registry.register(
            "command", "liquidate_account", LiquidateAccountRequest, dict,
            _liquidate_account_rpc_handler(coordinator, account_id),
        )

    if approval_service is not None:
        coordinator.register_action(
            "approve_proposal", approval_service.approve,
            requires_preflight=account_mode == "live", saga=True,
        )
        registry.register(
            "command", "approve_proposal", ApproveProposalRequest, dict,
            _approve_proposal_rpc_handler(coordinator, account_id),
        )

    if cancel_service is not None:
        coordinator.register_action(
            "cancel_order", cancel_service.cancel_order, requires_preflight=False, saga=True,
        )
        coordinator.register_action(
            "cancel_orders", cancel_service.cancel_orders, requires_preflight=False,
        )
        registry.register(
            "command", "cancel_order", CancelOrderRequest, dict,
            _cancel_order_rpc_handler(coordinator, account_id),
        )
        registry.register(
            "command", "cancel_orders", CancelOrdersRequest, dict,
            _cancel_orders_rpc_handler(coordinator, account_id),
        )

    if strategy_control_service is not None:
        coordinator.register_action(
            "enable_strategy", strategy_control_service.enable_strategy,
            requires_preflight=False, saga=True,
        )
        coordinator.register_action(
            "disable_strategy", strategy_control_service.disable_strategy,
            requires_preflight=False, saga=True,
        )
        coordinator.register_action(
            "update_strategy_params", strategy_control_service.update_strategy_params,
            requires_preflight=False, saga=True,
        )
        registry.register(
            "command", "enable_strategy", EnableStrategyRequest, dict,
            _strategy_control_rpc_handler(coordinator, account_id, "enable_strategy"),
        )
        registry.register(
            "command", "disable_strategy", DisableStrategyRequest, dict,
            _strategy_control_rpc_handler(coordinator, account_id, "disable_strategy"),
        )
        registry.register(
            "command", "update_strategy_params", UpdateStrategyParamsRequest, dict,
            _strategy_control_rpc_handler(coordinator, account_id, "update_strategy_params"),
        )
        registry.register(
            "command", "record_state_acknowledged", RecordStateAcknowledgedRequest, dict,
            _record_state_acknowledged_handler(strategy_control_service),
        )

    if canary_service is not None:
        coordinator.register_action(
            "activate_live_canary", canary_service.activate, requires_preflight=True,
        )
        coordinator.register_action(
            "deactivate_live_canary", canary_service.deactivate, requires_preflight=False,
        )
        registry.register(
            "command", "activate_live_canary", ActivateLiveCanaryRequest, dict,
            _activate_live_canary_rpc_handler(coordinator, account_id),
        )
        registry.register(
            "command", "deactivate_live_canary", DeactivateLiveCanaryRequest, dict,
            _deactivate_live_canary_rpc_handler(coordinator, account_id),
        )

    if allocation_service is not None:
        coordinator.register_action(
            "activate_allocation", allocation_service.activate, requires_preflight=True,
        )
        coordinator.register_action(
            "suspend_allocation", allocation_service.suspend, requires_preflight=False,
        )
        registry.register(
            "command", "activate_allocation", ActivateAllocationRequest, dict,
            _activate_allocation_rpc_handler(coordinator, account_id),
        )
        registry.register(
            "command", "suspend_allocation", SuspendAllocationRequest, dict,
            _suspend_allocation_rpc_handler(coordinator, account_id),
        )

    if paper_automation_service is not None:
        coordinator.register_action(
            "activate_paper_automation",
            _paper_automation_action(paper_automation_service, activate=True),
            requires_preflight=True,
        )
        coordinator.register_action(
            "deactivate_paper_automation",
            _paper_automation_action(paper_automation_service, activate=False),
            requires_preflight=False,
        )
        registry.register(
            "command", "activate_paper_automation", ActivatePaperAutomationRequest, dict,
            _activate_paper_automation_rpc_handler(coordinator, account_id),
        )
        registry.register(
            "command", "deactivate_paper_automation", DeactivatePaperAutomationRequest, dict,
            _deactivate_paper_automation_rpc_handler(coordinator, account_id),
        )
        registry.register(
            "query", "get_paper_automation_status", GetPaperAutomationStatusRequest, dict,
            _get_paper_automation_status_handler(paper_automation_service),
        )

    if automated_intent_service is not None:
        # P3 Task 3: strategy-service principal only. No dashboard HTTP route.
        coordinator.register_action(
            "execute_automated_intent", automated_intent_service.execute,
            requires_preflight=False, saga=True,
        )
        registry.register(
            "command", "execute_automated_intent", ExecuteAutomatedIntentRequest, dict,
            _execute_automated_intent_rpc_handler(coordinator, account_id),
        )


def _dict_to_strategy_receipt(data: Dict[str, Any]) -> StrategyCommandReceipt:
    return StrategyCommandReceipt(
        command_id=data["command_id"], strategy_name=data["strategy_name"],
        action=data["action"], state=data["state"],
        control_revision=data["control_revision"], state_revision=data["state_revision"],
        error=data.get("error"),
    )


class TypedStrategyControlPort:
    """[M1-F3] Task 7. Concrete ``StrategyControlPort`` backed by a pair of
    ``TypedRpcClient``s (``command``/``query`` roles) against
    strategy_service's typed sockets (``strategy_typed_command_port`` /
    ``strategy_typed_query_port`` -- 42104/42105 by default).

    Production wiring of an instance of this class into ``Trader.connect()``
    (constructing the two ``TypedRpcClient``s and passing this as
    ``strategy_control_service``'s ``port``) is deferred -- ``trading_runtime.py``/
    ``trader_service.py`` are outside this task's file scope (mirroring how
    Tasks 3-6's own coordinator/approval/cancel services are similarly not
    yet threaded into a live ``Trader.connect()`` call). This class is ready
    for that follow-on wiring.
    """

    def __init__(self, command_client: TypedRpcClient, query_client: TypedRpcClient):
        self._command_client = command_client
        self._query_client = query_client

    def forward(self, request: CommandRequest) -> StrategyCommandReceipt:
        body: Dict[str, Any] = {
            "command_id": request.command_id,
            "strategy_name": request.body["strategy_name"],
            "expected_control_revision": request.expected_version,
        }
        if request.action == "update_strategy_params":
            body["params"] = request.body.get("params") or {}
        response = self._command_client.call(request.action, body, dict)
        return _dict_to_strategy_receipt(response)

    def get_receipt(self, command_id: str) -> Optional[StrategyCommandReceipt]:
        try:
            response = self._query_client.call(
                "get_strategy_receipt", {"command_id": command_id}, dict,
            )
        except TypedRpcRemoteError as exc:
            if exc.code == "COMMAND_NOT_FOUND":
                return None
            raise
        return _dict_to_strategy_receipt(response)


def build_production_registry(
    trader,
    authenticator: HmacServiceAuthenticator,
    *,
    snapshot_service: Optional[DomainSnapshotService] = None,
    feed_service: Optional[DomainFeedService] = None,
    command_coordinator: Optional[TradingCommandCoordinator] = None,
    proposal_service: Optional[ProposalCommandService] = None,
    proposal_repository: Optional[ProposalRepository] = None,
    trading_control: Optional[TradingControlStore] = None,
    approval_service: Optional[ApprovalCommandService] = None,
    cancel_service: Optional[CancelCommandService] = None,
    strategy_control_service: Optional[StrategyControlCommandService] = None,
    command_stack: Optional["CommandStack"] = None,
) -> TypedRpcRegistry:
    """Build the typed-RPC registry a production ``trader_service`` serves.

    ``authenticator`` isn't consulted by the handlers below (the typed
    transport already authenticates every request before a handler ever
    runs) — it's required here, and type-checked, so a caller can't
    accidentally wire this up with something that isn't a real
    ``HmacServiceAuthenticator`` and only discover it once the first request
    fails to verify. It also keeps this function's signature stable for
    ``[M1-F3]``, which will need the authenticator when it adds
    coordinator-authorized command methods.

    Registers ``query``-role health/read methods, plus (opt-in, see module
    docstring's "[M1-F1] Task 5 addition") ``snapshot_with_cursor`` on
    ``query`` and ``read_domain_events`` on ``feed`` when their service
    objects are supplied.

    [M1-F3] Task 3 addition: when ``command_coordinator``, ``proposal_service``,
    AND ``proposal_repository`` are all supplied, ``register_command_authority``
    wires ``create_proposal``/``reject_proposal`` onto ``command`` and
    ``get_command``/``get_proposal``/``list_proposals`` onto ``query`` —
    account derived from ``trader.ib_account``, never from the request body.
    Any other combination (including all three absent, the default) leaves
    the ``command`` role empty, exactly as before this task. There is still
    no typed `set_risk_limits` anywhere on this registry, on either role —
    mutating risk limits stays behind the offline-simulation legacy path.

    [M1-F3] Task 4 addition: ``trading_control`` (a ``TradingControlStore``)
    is an additional OPTIONAL keyword, only consulted when the base three
    command-authority services are ALSO present — see
    ``register_command_authority``'s own docstring for what it adds
    (``pause_trading`` / ``resume_trading`` / ``get_trading_control``).
    Omitted, the default,
    changes nothing.

    [M1-F3] Task 6 addition: ``cancel_service`` (a ``CancelCommandService``)
    is likewise an additional OPTIONAL keyword, only consulted when the base
    three command-authority services are ALSO present — see
    ``register_command_authority``'s own docstring for what it adds
    (``cancel_order`` / ``cancel_orders``). Omitted, the default, changes
    nothing.

    [M1-F3] Task 7 addition: ``strategy_control_service`` (a
    ``StrategyControlCommandService``) is likewise an additional OPTIONAL
    keyword, only consulted when the base three command-authority services
    are ALSO present — see ``register_command_authority``'s own docstring
    for what it adds (``enable_strategy`` / ``disable_strategy`` /
    ``update_strategy_params`` / ``record_state_acknowledged``). Omitted,
    the default, changes nothing.
    """
    if not isinstance(authenticator, HmacServiceAuthenticator):
        raise TypeError(
            f'authenticator must be an HmacServiceAuthenticator, got {type(authenticator).__name__}'
        )

    # Every production handler may touch DuckDB, IB state, or another service.
    # Keep that work off the ROUTER event loop by default; isolated registries
    # elsewhere retain TypedRpcRegistry's inline default.
    registry = TypedRpcRegistry(default_execution="thread")
    api = TraderServiceApi(trader)

    # Health: service connectivity (IB, storage, upstream) — the same dict
    # the CLI's `status` command and dashboard health check already consume.
    registry.register('query', 'get_status', dict, dict, _no_arg_handler(api.get_status))
    # Read: account balances (cash, net liquidation, buying power, etc.),
    # scoped to the configured account — see TraderServiceApi.get_account_values.
    registry.register('query', 'get_account_values', dict, dict, _no_arg_handler(api.get_account_values))
    # Read: current risk-gate limits. Note this is the READ half only —
    # there is deliberately no typed `set_risk_limits` query or command here;
    # mutating limits stays behind the offline-simulation legacy path until
    # [M1-F3] adds an authorized command equivalent.
    registry.register('query', 'get_risk_limits', dict, dict, _no_arg_handler(api.get_risk_limits))
    # Strategy runtime instrument path: resolve a conId to a contract, and
    # start tick publication for it. These replace the legacy dill-RPC
    # resolve_symbol/publish_contract the split-container trader no longer
    # binds (port 42001), which is why strategy startup used to log
    # "resolve_symbol ... no route to server" and no strategy got live ticks.
    # Registered on the QUERY role deliberately: both must ALWAYS be available
    # (core strategy operation, not a gated user command), neither has ledger
    # or market impact, and the trader resolves the conId itself — so a
    # strategy can only ever subscribe to instruments already in the trader's
    # universe.
    registry.register(
        'query', 'resolve_instrument', ResolveInstrumentRequest,
        ResolveInstrumentResponse, _resolve_instrument_handler(api),
    )
    registry.register(
        'query', 'publish_instrument', PublishInstrumentRequest,
        PublishInstrumentResponse, _publish_instrument_handler(api),
    )
    register_manage_surface(registry, api)

    if command_stack is not None:
        register_command_authority(
            registry,
            command_stack.coordinator,
            command_stack.proposal_service,
            command_stack.repository,
            account_id=getattr(trader, 'ib_account', None),
            account_mode=command_stack.account_mode,
            controls=command_stack.controls,
            preflight_nonces=command_stack.nonces,
            resume_ready=command_stack.resume_ready,
            reconciliation_complete=command_stack.reconciliation_complete,
            approval_service=command_stack.approval_service,
            cancel_service=command_stack.cancel_service,
            liquidation_service=command_stack.liquidation_service,
            canary_service=command_stack.canary_service,
            allocation_service=command_stack.allocation_service,
            paper_automation_service=command_stack.paper_automation_service,
            automated_intent_service=command_stack.automated_intent_service,
        )
        register_strategy_state_ingest(registry, command_stack.journal)
    elif command_coordinator is not None and proposal_service is not None and proposal_repository is not None:
        register_command_authority(
            registry, command_coordinator, proposal_service, proposal_repository,
            account_id=getattr(trader, 'ib_account', None),
            account_mode=("paper" if getattr(trader, "paper_trading", False) else "live"),
            controls=trading_control,
            preflight_nonces=None,
            resume_ready=(lambda: bool(getattr(trader, "is_ib_connected", lambda: False)())),
            reconciliation_complete=(
                lambda command_id: command_coordinator.reconciliation_complete_for_account(
                    getattr(trader, 'ib_account', None), exclude_command_id=command_id,
                )
            ),
            approval_service=approval_service,
            cancel_service=cancel_service,
            strategy_control_service=strategy_control_service,
        )

    if snapshot_service is not None:
        registry.register(
            'query', 'snapshot_with_cursor', dict, dict, _snapshot_with_cursor_handler(snapshot_service)
        )
    if feed_service is not None:
        registry.register(
            'feed', 'read_domain_events', dict, dict, _read_domain_events_handler(feed_service)
        )

    return registry
