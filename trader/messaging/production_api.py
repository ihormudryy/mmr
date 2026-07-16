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
body.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict

from trader.data.proposal_repository import ProposalRepository
from trader.domain.commands import CommandReceipt
from trader.domain.feed_service import CURSOR_EXPIRED, CursorExpired, DomainFeedService, domain_event_to_wire
from trader.domain.snapshot_service import SNAPSHOT_NOT_READY, DomainSnapshotService, SnapshotNotReady
from trader.messaging.trader_service_api import TraderServiceApi
from trader.messaging.typed_rpc import HmacServiceAuthenticator, TypedRpcRegistry, _DispatchProblem
from trader.trading.command_coordinator import CommandRequest, CommandValidationError, TradingCommandCoordinator
from trader.trading.proposal_command_service import (
    ProposalCommandService,
    ProposalCreateRequest,
    ProposalCreationRefused,
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


def _read_domain_events_handler(feed_service: DomainFeedService):
    def _handler(body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = feed_service.read_domain_events(
                after_cursor=body['after_cursor'],
                limit=body['limit'],
                wait_ms=body['wait_ms'],
            )
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


class RejectProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    proposal_id: int
    reason: str


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
    own.
    """
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


def build_production_registry(
    trader,
    authenticator: HmacServiceAuthenticator,
    *,
    snapshot_service: Optional[DomainSnapshotService] = None,
    feed_service: Optional[DomainFeedService] = None,
    command_coordinator: Optional[TradingCommandCoordinator] = None,
    proposal_service: Optional[ProposalCommandService] = None,
    proposal_repository: Optional[ProposalRepository] = None,
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
    """
    if not isinstance(authenticator, HmacServiceAuthenticator):
        raise TypeError(
            f'authenticator must be an HmacServiceAuthenticator, got {type(authenticator).__name__}'
        )

    registry = TypedRpcRegistry()
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

    if command_coordinator is not None and proposal_service is not None and proposal_repository is not None:
        register_command_authority(
            registry, command_coordinator, proposal_service, proposal_repository,
            account_id=getattr(trader, 'ib_account', None),
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
