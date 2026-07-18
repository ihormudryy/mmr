"""Typed trader capabilities consumed by the strategy runtime.

The strategy runtime used to resolve conIds and publish market-data contracts
over an ``RPCClient`` bound to the trader's legacy dill RPC (port 42001). In
the split-container production posture the trader binds only the typed sockets,
so 42001 has no route and every subscription failed with
``resolve_symbol ... no route to server``. This gateway routes the same two
operations through the trader's authenticated typed ``query`` socket instead.

It holds no DuckDB handle and no legacy client — just the outbound typed query
client the runtime already builds toward the trader.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ib_async import Contract

from trader.messaging.strategy_trader_contracts import (
    PublishInstrumentRequest,
    PublishInstrumentResponse,
    ResolveInstrumentRequest,
    ResolveInstrumentResponse,
)


@dataclass(frozen=True)
class StrategyInstrument:
    """Lightweight instrument projection returned to the strategy runtime.

    Exposes IB-style aliases (``conId``/``primaryExchange``/``secType``/
    ``timeZoneId``) so it is a drop-in for the ``SecurityDefinition`` the
    runtime previously fed to ``SecurityDefinition.to_contract(...)`` and to
    the historical-fetch path (which reads ``timeZoneId``/``symbol``/``conId``
    and, via the exchange-calendar helper, ``primaryExchange``/``exchange``)."""

    instrument_id: int
    symbol: str
    exchange: str
    primary_exchange: str
    currency: str
    security_type: str
    time_zone_id: str

    @property
    def conId(self) -> int:
        return self.instrument_id

    @property
    def primaryExchange(self) -> str:
        return self.primary_exchange

    @property
    def secType(self) -> str:
        return self.security_type

    @property
    def timeZoneId(self) -> str:
        return self.time_zone_id

    def to_contract(self) -> Contract:
        return Contract(
            conId=self.instrument_id, symbol=self.symbol, exchange=self.exchange,
            primaryExchange=self.primary_exchange, currency=self.currency,
            secType=self.security_type,
        )


class StrategyTraderGateway:
    """Strategy-owned facade over the trader's typed query socket.

    Transport failures (``ConnectionError``/``TimeoutError`` from the typed
    client when the trader is down or restarting) propagate unchanged so the
    runtime's existing ``except (TimeoutError, ConnectionError)`` reconcile
    guards keep degrading gracefully and retrying. A ``TypedRpcRemoteError``
    (e.g. the trader doesn't know a conId) is a real, non-transient fault and
    is left to surface."""

    def __init__(self, query_client: Any):
        self._query_client = query_client

    def resolve_instrument(self, instrument_id: int) -> Optional[StrategyInstrument]:
        response = self._query_client.call(
            "resolve_instrument",
            ResolveInstrumentRequest(instrument_id=instrument_id).model_dump(),
            ResolveInstrumentResponse,
        )
        if not response.instruments:
            return None
        return StrategyInstrument(**response.instruments[0].model_dump())

    def publish_instrument(self, instrument_id: int, delayed: bool) -> None:
        self._query_client.call(
            "publish_instrument",
            PublishInstrumentRequest(instrument_id=instrument_id, delayed=delayed).model_dump(),
            PublishInstrumentResponse,
        )
