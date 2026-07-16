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
"""

from __future__ import annotations

from typing import Any, Dict

from trader.messaging.trader_service_api import TraderServiceApi
from trader.messaging.typed_rpc import HmacServiceAuthenticator, TypedRpcRegistry


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


def build_production_registry(trader, authenticator: HmacServiceAuthenticator) -> TypedRpcRegistry:
    """Build the typed-RPC registry a production ``trader_service`` serves.

    ``authenticator`` isn't consulted by the handlers below (the typed
    transport already authenticates every request before a handler ever
    runs) — it's required here, and type-checked, so a caller can't
    accidentally wire this up with something that isn't a real
    ``HmacServiceAuthenticator`` and only discover it once the first request
    fails to verify. It also keeps this function's signature stable for
    ``[M1-F3]``, which will need the authenticator when it adds
    coordinator-authorized command methods.

    Registers ONLY ``query``-role health/read methods for now — no
    ``command``-role methods at all.
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

    return registry
