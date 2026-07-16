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
  already JSON-safe dicts). It registers NOTHING on the ``command`` role:
  mutating trade actions are intentionally absent until ``[M1-F3]`` adds
  them through the coordinator, with per-call authorization designed for
  that surface (not just "has a valid HMAC key"). In particular, none of the
  five methods that today let a caller bypass proposal review
  (``place_order_simple``, ``place_expressive_order``,
  ``place_standalone_order``, ``set_risk_limits``, ``cancel_all``) are
  reachable through this registry, on either role.

[M1-F1] Task 5 addition -- event-foundation read methods
-----------------------------------------------------------
``build_production_registry`` also accepts two OPTIONAL keyword-only
services: ``snapshot_service`` (a ``DomainSnapshotService``) and
``feed_service`` (a ``DomainFeedService``). When supplied, they register
``snapshot_with_cursor`` on role ``query`` and ``read_domain_events`` on
role ``feed`` respectively -- never on ``command`` (reserved for
``[M1-F3]``, same as everything else here). Both default to ``None`` and
are simply omitted from the registry when absent, so every existing call
site (``Trader.connect()``, and ``_FakeTrader``-based tests like
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
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from trader.domain.feed_service import CURSOR_EXPIRED, CursorExpired, DomainFeedService, domain_event_to_wire
from trader.domain.snapshot_service import SNAPSHOT_NOT_READY, DomainSnapshotService, SnapshotNotReady
from trader.messaging.trader_service_api import TraderServiceApi
from trader.messaging.typed_rpc import HmacServiceAuthenticator, TypedRpcRegistry, _DispatchProblem


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


def build_production_registry(
    trader,
    authenticator: HmacServiceAuthenticator,
    *,
    snapshot_service: Optional[DomainSnapshotService] = None,
    feed_service: Optional[DomainFeedService] = None,
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
    objects are supplied. No ``command``-role methods at all.
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

    if snapshot_service is not None:
        registry.register(
            'query', 'snapshot_with_cursor', dict, dict, _snapshot_with_cursor_handler(snapshot_service)
        )
    if feed_service is not None:
        registry.register(
            'feed', 'read_domain_events', dict, dict, _read_domain_events_handler(feed_service)
        )

    return registry
