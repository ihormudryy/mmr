"""Strategy runtime resolves instruments + starts market-data publication
through the trader's TYPED query socket, not the legacy dill RPC (port 42001).

In the split-container production posture the trader binds only the typed
sockets; the legacy full RPC is never bound. Before this migration the
strategy runtime resolved conIds and published contracts over an ``RPCClient``
pointed at the dead 42001, so every startup subscription logged
``resolve_symbol ... no route to server`` and no strategy ever received live
ticks. These tests pin the typed replacement end-to-end:

- ``StrategyTraderGateway`` maps the typed wire response to a lightweight
  ``StrategyInstrument`` (Contract-buildable, carries the fields the history
  fetch needs) and sends the right publish body.
- The trader-side handlers (registered on the ``query`` role) resolve the
  conId against the trader's own universe and drive ``publish_contract``,
  returning a validated typed response over a real TypedRpcServer round-trip.
"""
from __future__ import annotations

import asyncio
import socket as socket_module
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from trader.messaging.typed_rpc import (
    HmacServiceAuthenticator,
    TypedRpcClient,
    TypedRpcRegistry,
    TypedRpcRemoteError,
    TypedRpcServer,
)
from trader.messaging.strategy_trader_contracts import (
    PublishInstrumentRequest,
    PublishInstrumentResponse,
    ResolveInstrumentRequest,
    ResolveInstrumentResponse,
)
from trader.messaging.production_api import (
    _publish_instrument_handler,
    _resolve_instrument_handler,
)
from trader.strategy.trader_gateway import StrategyInstrument, StrategyTraderGateway


HMAC_KEY = b"k" * 32


def _fake_secdef(conId=265598, symbol="AMD"):
    """A duck-typed SecurityDefinition carrying exactly the fields the
    resolve/publish handlers read (matches the suite's other fakes)."""
    return SimpleNamespace(
        conId=conId, symbol=symbol, exchange="SMART", primaryExchange="NASDAQ",
        currency="USD", secType="STK", timeZoneId="US/Eastern",
    )


class _FakeTraderApi:
    """Stands in for TraderServiceApi: an async local-DB ``resolve_symbol`` and
    a synchronous ``publish_contract`` that records what it was asked to
    stream (the real one wires an IB market-data subscription into pubsub)."""

    def __init__(self, secdefs, *, contract_secdefs=None):
        self._secdefs = secdefs  # conId -> secdef | absent
        self._contract_secdefs = contract_secdefs or {}
        self.published = []
        self.cached = []

    async def resolve_symbol(self, conId):
        sd = self._secdefs.get(conId)
        return [sd] if sd is not None else []

    async def resolve_contract(self, contract):
        sd = self._contract_secdefs.get(getattr(contract, 'conId', None))
        return [sd] if sd is not None else []

    def publish_contract(self, contract, delayed):
        self.published.append((contract.conId, delayed))
        return True


# ---------------------------------------------------------------------------
# Part A: gateway unit behaviour (fake typed client, no ZMQ)
# ---------------------------------------------------------------------------

class _FakeQueryClient:
    def __init__(self, responder):
        self._responder = responder
        self.calls = []

    def call(self, method, body, response_model, timeout=None):
        self.calls.append((method, body))
        return response_model.model_validate(self._responder(method, body))


def test_resolve_instrument_maps_wire_to_strategy_instrument():
    client = _FakeQueryClient(lambda method, body: {
        "instruments": [{
            "instrument_id": body["instrument_id"], "symbol": "AMD",
            "exchange": "SMART", "primary_exchange": "NASDAQ", "currency": "USD",
            "security_type": "STK", "time_zone_id": "US/Eastern",
        }]
    })
    gateway = StrategyTraderGateway(query_client=client)

    instrument = gateway.resolve_instrument(265598)

    assert client.calls == [("resolve_instrument", {"instrument_id": 265598})]
    assert isinstance(instrument, StrategyInstrument)
    assert instrument.conId == 265598
    assert instrument.symbol == "AMD"
    assert instrument.primaryExchange == "NASDAQ"
    assert instrument.secType == "STK"
    assert instrument.timeZoneId == "US/Eastern"
    # Must be Contract-buildable for the subscribe path.
    contract = instrument.to_contract()
    assert contract.conId == 265598
    assert contract.symbol == "AMD"


def test_resolve_instrument_returns_none_when_no_match():
    client = _FakeQueryClient(lambda method, body: {"instruments": []})
    gateway = StrategyTraderGateway(query_client=client)

    assert gateway.resolve_instrument(999999) is None


def test_publish_instrument_sends_conid_and_delayed():
    client = _FakeQueryClient(lambda method, body: {"published": True})
    gateway = StrategyTraderGateway(query_client=client)

    gateway.publish_instrument(265598, delayed=False)

    assert client.calls == [
        ("publish_instrument", {"instrument_id": 265598, "delayed": False})
    ]


# ---------------------------------------------------------------------------
# Part B: trader-side handlers over a real typed round-trip
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _serve_query(registry):
    """Serve ``registry`` on a fresh query socket in a background loop and
    yield a connected query-role client."""
    authenticator = HmacServiceAuthenticator(HMAC_KEY, now=time.time)
    port = _free_port()
    server = TypedRpcServer("query", registry, authenticator, port=port)

    ready = threading.Event()
    state = {}

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        state["loop"] = loop
        loop.run_until_complete(server.serve())
        ready.set()
        try:
            loop.run_forever()
        finally:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert ready.wait(timeout=3.0), "typed query server did not signal ready"
    time.sleep(0.1)  # let the ROUTER finish binding

    client = TypedRpcClient(
        "query", HmacServiceAuthenticator(HMAC_KEY, now=time.time), port=port, timeout=3.0)
    client.connect()
    try:
        yield client
    finally:
        client.close()
        loop = state.get("loop")
        if loop:
            loop.call_soon_threadsafe(server.close)
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=3.0)


def test_resolve_instrument_typed_roundtrip():
    api = _FakeTraderApi({265598: _fake_secdef()})
    registry = TypedRpcRegistry()
    registry.register(
        "query", "resolve_instrument", ResolveInstrumentRequest,
        ResolveInstrumentResponse, _resolve_instrument_handler(api),
    )
    with _serve_query(registry) as client:
        gateway = StrategyTraderGateway(query_client=client)
        instrument = gateway.resolve_instrument(265598)

    assert instrument is not None
    assert instrument.conId == 265598
    assert instrument.symbol == "AMD"
    assert instrument.primaryExchange == "NASDAQ"
    assert instrument.timeZoneId == "US/Eastern"


def test_resolve_instrument_typed_roundtrip_unknown_returns_none():
    api = _FakeTraderApi({})
    registry = TypedRpcRegistry()
    registry.register(
        "query", "resolve_instrument", ResolveInstrumentRequest,
        ResolveInstrumentResponse, _resolve_instrument_handler(api),
    )
    with _serve_query(registry) as client:
        gateway = StrategyTraderGateway(query_client=client)
        assert gateway.resolve_instrument(4391) is None


def test_resolve_instrument_ib_fallback_qualifies_exact_conid(monkeypatch):
    """Local miss + Contract(conId=N) hit must return the instrument (exact
    primary-key qualify — not a fuzzy symbol search)."""
    from trader.messaging import production_api as prod

    cached = []

    def _cache(api, definition):
        cached.append(definition.conId)
        api._secdefs[definition.conId] = definition

    monkeypatch.setattr(prod, '_cache_resolved_instrument', _cache)
    api = _FakeTraderApi({}, contract_secdefs={51529211: _fake_secdef(51529211, "GLD")})
    registry = TypedRpcRegistry()
    registry.register(
        "query", "resolve_instrument", ResolveInstrumentRequest,
        ResolveInstrumentResponse, _resolve_instrument_handler(api),
    )
    with _serve_query(registry) as client:
        gateway = StrategyTraderGateway(query_client=client)
        instrument = gateway.resolve_instrument(51529211)

    assert instrument is not None
    assert instrument.conId == 51529211
    assert instrument.symbol == "GLD"
    assert cached == [51529211]


def test_resolve_instrument_fake_broker_seeds_stub(monkeypatch):
    from trader.messaging import production_api as prod

    cached = []
    monkeypatch.setenv('MMR_FAKE_BROKER', '1')
    monkeypatch.setattr(prod, '_cache_resolved_instrument',
                        lambda api, d: cached.append(d.conId))
    api = _FakeTraderApi({})
    registry = TypedRpcRegistry()
    registry.register(
        "query", "resolve_instrument", ResolveInstrumentRequest,
        ResolveInstrumentResponse, _resolve_instrument_handler(api),
    )
    with _serve_query(registry) as client:
        gateway = StrategyTraderGateway(query_client=client)
        instrument = gateway.resolve_instrument(5437)

    assert instrument is not None
    assert instrument.conId == 5437
    assert instrument.symbol == "C5437"
    assert cached == [5437]


def test_publish_instrument_typed_roundtrip_drives_publish_contract():
    api = _FakeTraderApi({265598: _fake_secdef()})
    registry = TypedRpcRegistry()
    registry.register(
        "query", "publish_instrument", PublishInstrumentRequest,
        PublishInstrumentResponse, _publish_instrument_handler(api),
    )
    with _serve_query(registry) as client:
        gateway = StrategyTraderGateway(query_client=client)
        gateway.publish_instrument(265598, delayed=False)

    assert api.published == [(265598, False)]


def test_publish_instrument_unknown_conid_raises_remote_error():
    api = _FakeTraderApi({})  # conId not in the trader's universe
    registry = TypedRpcRegistry()
    registry.register(
        "query", "publish_instrument", PublishInstrumentRequest,
        PublishInstrumentResponse, _publish_instrument_handler(api),
    )
    with _serve_query(registry) as client:
        gateway = StrategyTraderGateway(query_client=client)
        with pytest.raises(TypedRpcRemoteError):
            gateway.publish_instrument(999999, delayed=False)

    assert api.published == []
