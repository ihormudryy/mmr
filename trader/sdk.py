"""MMR SDK — programmatic access to the trader_service.

Usage::

    from trader.sdk import MMR

    with MMR() as mmr:
        print(mmr.portfolio())
        mmr.buy("AMD", market=True, quantity=10)
"""

from expression import pipe
from expression.collections import seq
from ib_async.contract import Contract
from ib_async.objects import PortfolioItem, Position
from ib_async.order import Order, Trade
from ib_async.ticker import Ticker
from trader.common.reactivex import SuccessFail
from trader.data.data_access import PortfolioSummary, SecurityDefinition
from trader.data.universe import Universe
from trader.data_providers.builtin import IB_FOREX_SOURCE
from trader.messaging.clientserver import consume, RPCClient, TopicPubSub, pack, unpack
from trader.messaging.data_service_api import DataServiceApi
from trader.messaging.trader_service_api import TraderServiceApi
from trader.trading.strategy import StrategyConfig, StrategyState, is_dispatchable_strategy_state
from typing import Any, Callable, Dict, List, Optional, Union

import asyncio
import dataclasses
import datetime as dt
import logging
import os
import pandas as pd
import threading
import zmq

logger = logging.getLogger(__name__)


def _quote_to_snapshot(quote: dict) -> dict:
    nan = float('nan')
    return {
        'symbol': quote['symbol'], 'conId': '', 'time': quote['time'],
        'bid': quote['bid'], 'bidSize': quote['bid_size'], 'ask': quote['ask'], 'askSize': quote['ask_size'],
        'last': quote['last'], 'lastSize': nan, 'open': quote['open'], 'high': quote['high'],
        'low': quote['low'], 'close': quote['close'], 'volume': quote['volume'],
        'previous_close': quote['previous_close'], 'change': quote['change'],
        'change_pct': quote['change_pct'], 'halted': nan, 'exchange': quote['exchange'],
        'currency': quote['currency'], 'name': quote['name'], 'feed': quote['feed'],
    }


def _quote_to_batch_row(quote: dict) -> dict:
    keys = ('symbol', 'time', 'bid', 'ask', 'last', 'open', 'high', 'low', 'close', 'volume',
            'previous_close', 'change', 'change_pct', 'exchange', 'currency', 'feed', 'error')
    return {key: quote[key] for key in keys}


class Subscription:
    """Handle returned by :meth:`MMR.subscribe_ticks`.  Call :meth:`stop` to unsubscribe."""

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def compute_resize_deltas(
    positions: list[dict],
    max_bound: Optional[float],
    min_bound: Optional[float],
) -> tuple[float, list[dict]]:
    """Compute proportional resize plan from portfolio positions.

    Returns (scale_factor, adjustments).

    Each adjustment: {symbol, conId, current_qty, target_qty, delta_qty, action,
                      current_value, target_value, mkt_price}
    """
    import math

    # Prefer a base-currency-normalised value when the caller supplied one
    # (compute_resize_plan sets 'marketValueBase'); fall back to raw marketValue
    # for the single-currency case and existing pure-function callers/tests.
    def _val(p):
        return p.get('marketValueBase', p.get('marketValue', 0)) or 0

    total_value = sum(abs(_val(p)) for p in positions)

    if total_value == 0:
        return 1.0, []

    scale_factor = 1.0
    if max_bound is not None and total_value > max_bound:
        scale_factor = max_bound / total_value
    elif min_bound is not None and total_value < min_bound:
        scale_factor = min_bound / total_value

    if scale_factor == 1.0:
        return 1.0, []

    adjustments = []
    for p in positions:
        current_qty = p.get('position', 0)
        if current_qty == 0:
            continue

        mkt_price = p.get('mktPrice', 0) or 0
        current_value = p.get('marketValue', 0) or 0

        target_qty = int(current_qty * scale_factor)
        delta_qty = target_qty - current_qty

        if delta_qty == 0:
            continue

        # For long positions: selling reduces, buying increases
        # For short positions: buying reduces (covers), selling increases
        if current_qty > 0:
            action = 'BUY' if delta_qty > 0 else 'SELL'
        else:
            action = 'SELL' if delta_qty < 0 else 'BUY'

        target_value = target_qty * mkt_price if mkt_price else current_value * scale_factor

        adjustments.append({
            'symbol': p.get('symbol', ''),
            'conId': p.get('conId', 0),
            'current_qty': current_qty,
            'target_qty': target_qty,
            'delta_qty': delta_qty,
            'action': action,
            'current_value': current_value,
            'target_value': target_value,
            'mkt_price': mkt_price,
        })

    return scale_factor, adjustments


def proposal_display_status(storage_status: str) -> str:
    """Map a `ProposalStore` storage-layer status to a user-facing label.

    `EXECUTED` is a state-machine implementation detail (see
    `proposal_store.py`'s transition table) — from the trader's point of
    view what actually happened is that an order was submitted to the
    broker, which is what the dashboard/CLI should say.
    """
    return "ORDER_SUBMITTED" if storage_status == "EXECUTED" else storage_status


class MMR:
    """Synchronous Python SDK for the MMR trader_service.

    Parameters
    ----------
    config_file : str
        Path to trader.yaml.  Defaults to ``~/.config/mmr/trader.yaml``.
    rpc_address : str, optional
        Override ZMQ RPC server address (e.g. ``tcp://127.0.0.1``).
    rpc_port : int, optional
        Override ZMQ RPC server port (e.g. ``42001``).
    pubsub_address : str, optional
        Override ZMQ PubSub server address.
    pubsub_port : int, optional
        Override ZMQ PubSub server port.
    timeout : int
        RPC timeout in seconds.
    """

    def __init__(
        self,
        config_file: str = '',
        rpc_address: Optional[str] = None,
        rpc_port: Optional[int] = None,
        pubsub_address: Optional[str] = None,
        pubsub_port: Optional[int] = None,
        timeout: int = 30,
    ):
        from trader.container import Container

        self._container = Container(config_file) if config_file else Container.instance()
        cfg = self._container.config()

        # Address resolution precedence: explicit ctor arg > env var > raw
        # YAML config. The env-var layer matters for the split-Compose
        # topology (G0 Task 5): the dashboard container reaches trader_service
        # over the private network via ZMQ_RPC_SERVER_ADDRESS=tcp://trader,
        # NOT the loopback baked into the shared trader.yaml. Every OTHER
        # service already honours these env vars (they go through
        # Container.resolve, which consults env first) -- the SDK was the lone
        # exception because it read cfg[...] (the raw YAML dict) directly, so
        # the env override was silently ignored and a dashboard container
        # targeted its own empty loopback. os.getenv('') is treated as unset
        # (an explicitly-empty env var must not blank a valid config value).
        self._rpc_address = rpc_address or (os.getenv('ZMQ_RPC_SERVER_ADDRESS') or None) or cfg['zmq_rpc_server_address']
        self._rpc_port = rpc_port or cfg['zmq_rpc_server_port']
        self._pubsub_address = pubsub_address or (os.getenv('ZMQ_PUBSUB_SERVER_ADDRESS') or None) or cfg['zmq_pubsub_server_address']
        self._pubsub_port = pubsub_port or cfg['zmq_pubsub_server_port']
        self._data_rpc_address = (os.getenv('ZMQ_DATA_RPC_SERVER_ADDRESS') or None) or cfg.get('zmq_data_rpc_server_address', 'tcp://127.0.0.1')
        self._data_rpc_port = cfg.get('zmq_data_rpc_server_port', 42003)
        self._timeout = timeout

        # [M1-F3] Task 8: typed, Ed25519-authenticated query/command sockets --
        # propose / resolve / approve / manage go here. Legacy dill RPC
        # (42001) is NOT bound in the split-container topology, so clients
        # must dial the typed query/command ports (42101/42102). Address
        # resolution: TRADER_TYPED_ADDRESS / TYPED_RPC_SERVER_ADDRESS /
        # MMR_TYPED_*_ENDPOINT host, then yaml typed_bind with 0.0.0.0→loopback.
        # Lazily connected on first use (see `_ensure_typed_clients`).
        self._typed_address = self._resolve_typed_client_address(cfg)
        self._typed_query_port = cfg.get('typed_query_port', 42101)
        self._typed_command_port = cfg.get('typed_command_port', 42102)
        # The SDK signs as `cli` unless MMR_RPC_PRINCIPAL picks another client
        # principal (ai_supervisor / ai_research). Server and dashboard
        # identities are refused here. The key is loaded lazily, on the first
        # typed call, so commands that need no service need no key.
        self._rpc_principal = self._client_principal_from_env()
        self._rpc_keys_dir = (cfg.get('rpc_keys_dir') or '').strip() or None
        self._rpc_identity: Optional['ServiceIdentity'] = None
        self._typed_query_client: Optional['TypedRpcClient'] = None
        self._typed_command_client: Optional['TypedRpcClient'] = None
        # Strategy-service typed ports (list/enable/disable/reload). Defaults
        # match compose's published strategy endpoints; override via
        # MMR_TYPED_STRATEGY_*_ENDPOINT or STRATEGY_TYPED_ADDRESS.
        self._strategy_typed_address = self._resolve_strategy_typed_address(cfg)
        self._strategy_typed_query_port = cfg.get('strategy_typed_query_port', 42105)
        self._strategy_typed_command_port = cfg.get('strategy_typed_command_port', 42104)
        self._strategy_typed_query_client: Optional['TypedRpcClient'] = None
        self._strategy_typed_command_client: Optional['TypedRpcClient'] = None

        self._client: Optional[RPCClient[TraderServiceApi]] = None
        self._data_client: Optional[RPCClient[DataServiceApi]] = None
        self._massive_rest_client = None
        self._twelvedata_rest_client = None
        self._subscriptions: List[Subscription] = []

        # position_map: row_number -> symbol string (set by portfolio/positions)
        self._position_map: Dict[int, str] = {}
        # contract_map: symbol -> Contract object (set by portfolio, used by close_position)
        self._contract_map: Dict[str, Contract] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    _LEGACY_UNAVAILABLE = (
        '{op} requires the offline-simulation legacy RPC (port 42001), which '
        'is not bound in the split-container production topology. '
        'Use `mmr propose` → `mmr approve` for new trades; cancel/close via '
        'the dashboard command center where available; or run with '
        '`unsafe_legacy_rpc: true` + `--simulation True` for direct orders.'
    )

    def connect(self) -> 'MMR':
        """Connect to trader_service.  Returns *self* for chaining."""
        self._client = RPCClient[TraderServiceApi](
            zmq_server_address=self._rpc_address,
            zmq_server_port=self._rpc_port,
            timeout=self._timeout,
        )
        asyncio.run(self._client.connect())
        return self

    def _connect_data_service(self) -> None:
        """Connect to data_service RPC. Called lazily on first data request."""
        if self._data_client is None:
            self._data_client = RPCClient[DataServiceApi](
                zmq_server_address=self._data_rpc_address,
                zmq_server_port=self._data_rpc_port,
                timeout=120,
            )
            asyncio.run(self._data_client.connect())

    @property
    def _data_rpc(self) -> RPCClient[DataServiceApi]:
        self._connect_data_service()
        if self._data_client is None or not self._data_client.is_setup:
            raise ConnectionError("Not connected to data_service.")
        return self._data_client

    @staticmethod
    def _client_principal_from_env() -> str:
        from trader.messaging.principals import CLIENT_PRINCIPALS
        principal = (os.getenv('MMR_RPC_PRINCIPAL') or '').strip() or 'cli'
        if principal not in CLIENT_PRINCIPALS:
            raise ValueError(
                f'MMR_RPC_PRINCIPAL={principal!r} is not a client principal; '
                f'use one of {sorted(CLIENT_PRINCIPALS)}')
        return principal

    def _load_rpc_identity(self) -> 'ServiceIdentity':
        if self._rpc_identity is None:
            from trader.messaging.typed_rpc import ServiceIdentity
            self._rpc_identity = ServiceIdentity.load(self._rpc_principal, self._rpc_keys_dir)
        return self._rpc_identity

    def _ensure_typed_clients(self) -> None:
        """Lazily build+connect the typed query/command clients toward
        trader_service's command-authority coordinator. Called only by
        `_typed_query`/`_typed_command` (i.e. only when a typed command
        is actually attempted) so unrelated commands never need an RPC key."""
        if self._typed_query_client is not None and self._typed_command_client is not None:
            return
        from trader.messaging.typed_rpc import TypedRpcClient
        identity = self._load_rpc_identity()
        query_client = TypedRpcClient(
            'query', identity, server='trader', address=self._typed_address,
            port=self._typed_query_port, timeout=self._timeout,
        )
        query_client.connect()
        command_client = TypedRpcClient(
            'command', identity, server='trader', address=self._typed_address,
            port=self._typed_command_port, timeout=self._timeout,
        )
        command_client.connect()
        self._typed_query_client = query_client
        self._typed_command_client = command_client

    def _ensure_strategy_typed_clients(self) -> None:
        if self._strategy_typed_query_client is not None and self._strategy_typed_command_client is not None:
            return
        from trader.messaging.typed_rpc import TypedRpcClient
        identity = self._load_rpc_identity()
        query_client = TypedRpcClient(
            'query', identity, server='strategy', address=self._strategy_typed_address,
            port=self._strategy_typed_query_port, timeout=self._timeout,
        )
        query_client.connect()
        command_client = TypedRpcClient(
            'command', identity, server='strategy', address=self._strategy_typed_address,
            port=self._strategy_typed_command_port, timeout=self._timeout,
        )
        command_client.connect()
        self._strategy_typed_query_client = query_client
        self._strategy_typed_command_client = command_client

    @property
    def _typed_query(self) -> 'TypedRpcClient':
        self._ensure_typed_clients()
        return self._typed_query_client

    @property
    def _typed_command(self) -> 'TypedRpcClient':
        self._ensure_typed_clients()
        return self._typed_command_client

    @property
    def _strategy_typed_query(self) -> 'TypedRpcClient':
        self._ensure_strategy_typed_clients()
        return self._strategy_typed_query_client

    @property
    def _strategy_typed_command(self) -> 'TypedRpcClient':
        self._ensure_strategy_typed_clients()
        return self._strategy_typed_command_client

    def close(self) -> None:
        """Disconnect and clean up resources."""
        for sub in self._subscriptions:
            sub.stop()
        self._subscriptions.clear()
        if self._client:
            self._client.close()
            self._client = None
        if self._data_client:
            self._data_client.close()
            self._data_client = None
        if self._typed_query_client:
            self._typed_query_client.close()
            self._typed_query_client = None
        if self._typed_command_client:
            self._typed_command_client.close()
            self._typed_command_client = None
        if self._strategy_typed_query_client:
            self._strategy_typed_query_client.close()
            self._strategy_typed_query_client = None
        if self._strategy_typed_command_client:
            self._strategy_typed_command_client.close()
            self._strategy_typed_command_client = None

    def __enter__(self) -> 'MMR':
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def _rpc(self) -> RPCClient[TraderServiceApi]:
        if self._client is None or not self._client.is_setup:
            raise ConnectionError("Not connected. Call .connect() first.")
        return self._client

    def _legacy_or_raise(self, op: str):
        """Return legacy RPC client, or raise a clear production-topology error."""
        try:
            return self._rpc
        except ConnectionError as exc:
            raise ConnectionError(self._LEGACY_UNAVAILABLE.format(op=op)) from exc

    def _map_legacy_route_error(self, op: str, exc: BaseException) -> None:
        """Rewrite ZMQ 'no route to server' into an actionable message."""
        text = str(exc).lower()
        if 'no route to server' in text or 'could not be sent' in text:
            raise ConnectionError(self._LEGACY_UNAVAILABLE.format(op=op)) from exc
        raise exc

    @staticmethod
    def _resolve_strategy_typed_address(cfg: dict) -> str:
        for key in ('STRATEGY_TYPED_ADDRESS',):
            val = (os.getenv(key) or '').strip()
            if val:
                return val
        for key in ('MMR_TYPED_STRATEGY_QUERY_ENDPOINT', 'MMR_TYPED_STRATEGY_COMMAND_ENDPOINT'):
            endpoint = (os.getenv(key) or '').strip()
            if not endpoint:
                continue
            from urllib.parse import urlparse
            parsed = urlparse(endpoint)
            if parsed.scheme and parsed.hostname:
                return f'{parsed.scheme}://{parsed.hostname}'
        # Same-host default: strategy typed ports live beside trader.
        return MMR._resolve_typed_client_address(cfg)

    @staticmethod
    def _resolve_typed_client_address(cfg: dict) -> str:
        """Host for typed query/command clients (not the bind address).

        Precedence: ``TRADER_TYPED_ADDRESS`` / ``TYPED_RPC_SERVER_ADDRESS`` →
        host of ``MMR_TYPED_QUERY_ENDPOINT`` / ``MMR_TYPED_COMMAND_ENDPOINT`` →
        yaml ``typed_bind_address`` with all-interfaces remapped to loopback
        (a client cannot dial ``tcp://0.0.0.0``).
        """
        for key in ('TRADER_TYPED_ADDRESS', 'TYPED_RPC_SERVER_ADDRESS'):
            val = (os.getenv(key) or '').strip()
            if val:
                return val
        for key in ('MMR_TYPED_QUERY_ENDPOINT', 'MMR_TYPED_COMMAND_ENDPOINT'):
            endpoint = (os.getenv(key) or '').strip()
            if not endpoint:
                continue
            from urllib.parse import urlparse
            parsed = urlparse(endpoint)
            if parsed.scheme and parsed.hostname:
                return f'{parsed.scheme}://{parsed.hostname}'
        bind = (cfg.get('typed_bind_address') or 'tcp://127.0.0.1').strip()
        # Server bind env (TYPED_BIND_ADDRESS) may override yaml in compose;
        # honour it only when remapping the client target.
        env_bind = (os.getenv('TYPED_BIND_ADDRESS') or '').strip()
        if env_bind:
            bind = env_bind
        if bind in ('tcp://0.0.0.0', 'tcp://*', 'tcp://[::]', 'tcp://::'):
            return 'tcp://127.0.0.1'
        return bind or 'tcp://127.0.0.1'

    @staticmethod
    def _instrument_id(symbol: Union[str, int]) -> Optional[int]:
        """Treat ints and all-digit strings as exact conIds (never as tickers)."""
        if type(symbol) is int:
            return symbol
        if type(symbol) is str and symbol.isnumeric():
            return int(symbol)
        return None

    @staticmethod
    def _security_definition_from_wire(row: dict) -> SecurityDefinition:
        """Rebuild a SecurityDefinition from typed-RPC instrument wire fields."""
        return SecurityDefinition(
            symbol=str(row.get('symbol') or ''),
            exchange=str(row.get('exchange') or ''),
            conId=int(row.get('instrument_id') or 0),
            secType=str(row.get('security_type') or 'STK'),
            primaryExchange=str(row.get('primary_exchange') or ''),
            currency=str(row.get('currency') or ''),
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
            timeZoneId=str(row.get('time_zone_id') or ''),
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

    # ------------------------------------------------------------------
    # Symbol resolution (internal helper)
    # ------------------------------------------------------------------

    def resolve(
        self,
        symbol: Union[str, int],
        sec_type: str = 'STK',
        exchange: str = '',
        universe: str = '',
        currency: str = '',
    ) -> List[SecurityDefinition]:
        """Resolve a symbol string or conId to SecurityDefinition(s) via trader_service.

        Uses the typed query socket (``discover_instrument`` /
        ``resolve_instrument``) — legacy dill RPC port 42001 is not bound in
        the split-container topology.

        Lookup order (server-side):
          1. Local universe DB. ``exchange`` filters the query; ``universe`` is
             accepted for API compatibility but not forwarded on the typed
             wire (discover has no universe field).
          2. For conIds (int or all-digit string): exact ``resolve_instrument``
             — may qualify the same conId via IB ``Contract(conId=N)``, never
             a fuzzy ticker search (``4391`` must not become TSEJ ``"4391"``).
          3. For string symbols: IB discovery with the caller's exchange/
             currency hints (empty included — no SMART/USD defaults).

        Returns candidates after collapsing venue duplicates (same conId +
        currency). Dual listings with different conIds survive as ambiguity.
        """
        del universe  # typed discover_instrument has no universe filter
        instrument_id = self._instrument_id(symbol)
        if instrument_id is not None:
            response = self._typed_query.call(
                'resolve_instrument',
                {'instrument_id': instrument_id},
                dict,
            )
            rows = response.get('instruments') or []
            return [self._security_definition_from_wire(r) for r in rows]

        response = self._typed_query.call(
            'discover_instrument',
            {
                'symbol': str(symbol),
                'exchange': exchange or '',
                'currency': currency or '',
                'sec_type': sec_type or 'STK',
            },
            dict,
        )
        rows = response.get('instruments') or []
        candidates = [self._security_definition_from_wire(r) for r in rows]
        return self._dedupe_venue_duplicates(candidates)

    @staticmethod
    def _dedupe_venue_duplicates(
        candidates: List[SecurityDefinition],
    ) -> List[SecurityDefinition]:
        """Collapse venue duplicates while preserving dual-listings.

        IB's ``reqContractDetails`` returns one row per exchange the
        instrument trades on — so a US stock comes back as NASDAQ + BATS +
        ARCA + ISLAND + …, all with the same ``conId``. We want one row
        per *listing*, not per venue. Keying on ``(conId, currency)`` does
        the right thing: venue duplicates share a conId so they collapse;
        the ADR on a different currency has a different conId anyway so it
        survives as real ambiguity.

        Preference within a duplicate group: keep the row whose
        ``exchange`` matches its own ``primaryExchange`` (i.e. the "home"
        listing rather than a venue-routed copy). Falls back to first-seen
        order when no row has that match (rare).
        """
        if not candidates:
            return candidates
        by_key: Dict[tuple, SecurityDefinition] = {}
        for c in candidates:
            key = (c.conId, c.currency)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = c
                continue
            # Prefer the one where exchange == primaryExchange
            if c.exchange == c.primaryExchange and existing.exchange != existing.primaryExchange:
                by_key[key] = c
        return list(by_key.values())

    def _resolve_contract(self, symbol: Union[str, int], sec_type: str = 'STK',
                          exchange: str = '', currency: str = '') -> Contract:
        """Resolve *symbol* to a single Contract, raising on ambiguity.

        When *exchange* or *currency* are provided, prefer definitions matching
        those hints (e.g. exchange='ASX', currency='AUD' for Australian stocks).
        Otherwise, prefers USD contracts on US exchanges.
        """
        definitions = self.resolve(symbol, sec_type=sec_type, exchange=exchange, currency=currency)
        if not definitions:
            raise ValueError(f"Could not resolve symbol: {symbol}")

        sec = definitions[0]
        if exchange or currency:
            # Prefer definition matching the exchange/currency hint
            for d in definitions:
                d_exchange = getattr(d, 'exchange', '') or ''
                d_primary = getattr(d, 'primaryExchange', '') or ''
                d_currency = getattr(d, 'currency', '') or ''
                if exchange and exchange.upper() not in (d_exchange.upper(), d_primary.upper()):
                    continue
                if currency and d_currency.upper() != currency.upper():
                    continue
                sec = d
                break
            else:
                # No definition matched the hints — ask discover again with
                # the caller's exchange/currency (SMART/USD only when the
                # hint itself was empty). Same role as the old IB
                # resolve_contract fallback when the local universe lied.
                if isinstance(symbol, str) and not self._instrument_id(symbol):
                    response = self._typed_query.call(
                        'discover_instrument',
                        {
                            'symbol': str(symbol),
                            'exchange': exchange or 'SMART',
                            'currency': currency or 'USD',
                            'sec_type': sec_type or 'STK',
                        },
                        dict,
                    )
                    direct = [
                        self._security_definition_from_wire(r)
                        for r in (response.get('instruments') or [])
                    ]
                    if direct:
                        sec = direct[0]
        elif len(definitions) > 1:
            # Default: prefer USD on a US exchange
            us_exchanges = {'SMART', 'NYSE', 'NASDAQ', 'AMEX', 'ARCA', 'BATS', 'IEX', 'ISLAND'}
            for d in definitions:
                d_currency = getattr(d, 'currency', '')
                d_exchange = getattr(d, 'exchange', '')
                d_primary = getattr(d, 'primaryExchange', '')
                if d_currency == 'USD' and (d_exchange in us_exchanges or d_primary in us_exchanges):
                    sec = d
                    break
            else:
                # Fallback: prefer USD even if exchange isn't explicitly US
                for d in definitions:
                    if getattr(d, 'currency', '') == 'USD':
                        sec = d
                        break

        # Use SMART routing for non-US exchanges to avoid IB Error 10311
        # ("direct routed orders may result in higher trade fees").
        # Keep the real exchange in primaryExchange so IB routes correctly.
        sec_exchange = sec.exchange or ''
        sec_primary = getattr(sec, 'primaryExchange', '') or ''
        us_smart = {'SMART', 'NYSE', 'NASDAQ', 'AMEX', 'ARCA', 'BATS', 'IEX', 'ISLAND'}
        if sec_exchange.upper() in us_smart:
            order_exchange = sec_exchange
            primary_exchange = sec_primary
        else:
            # Non-US exchange (ASX, TSE, SEHK, etc.) — use SMART routing
            order_exchange = 'SMART'
            primary_exchange = sec_primary or sec_exchange

        return Contract(
            conId=sec.conId,
            symbol=sec.symbol,
            secType=sec.secType,
            exchange=order_exchange,
            primaryExchange=primary_exchange,
            currency=sec.currency,
        )

    # ------------------------------------------------------------------
    # Portfolio & Positions
    # ------------------------------------------------------------------

    @staticmethod
    def _to_contract(contract) -> Contract:
        """Build a Contract from a Contract object or dict (msgpack-deserialized)."""
        us = {'SMART', 'NYSE', 'NASDAQ', 'AMEX', 'ARCA', 'BATS', 'IEX', 'ISLAND'}
        if isinstance(contract, dict):
            exchange = contract.get('exchange', '') or ''
            primary = contract.get('primaryExchange', '') or ''
            if exchange.upper() not in us:
                primary = primary or exchange
                exchange = 'SMART'
            return Contract(
                conId=contract.get('conId', 0),
                symbol=contract.get('symbol', ''),
                secType=contract.get('secType', 'STK'),
                exchange=exchange,
                primaryExchange=primary,
                currency=contract.get('currency', ''),
            )
        # Contract object (or any object with matching attributes)
        exchange = getattr(contract, 'exchange', '') or ''
        primary = getattr(contract, 'primaryExchange', '') or ''
        if exchange.upper() not in us:
            primary = primary or exchange
            exchange = 'SMART'
        return Contract(
            conId=contract.conId,
            symbol=contract.symbol,
            secType=getattr(contract, 'secType', 'STK') or 'STK',
            exchange=exchange,
            primaryExchange=primary,
            currency=getattr(contract, 'currency', ''),
        )

    @staticmethod
    def _extract_contract(contract) -> dict:
        """Extract contract fields whether it's a Contract object or a dict."""
        if isinstance(contract, dict):
            return {
                'conId': contract.get('conId', ''),
                'symbol': contract.get('localSymbol') or contract.get('symbol', ''),
                'secType': contract.get('secType', ''),
                'currency': contract.get('currency', ''),
            }
        return {
            'conId': contract.conId,
            'symbol': contract.localSymbol or contract.symbol,
            'secType': contract.secType,
            'currency': contract.currency,
        }

    def portfolio(self) -> pd.DataFrame:
        """Portfolio with P&L (matches the old ``portfolio`` CLI command).

        Uses typed ``get_portfolio_summary`` — legacy dill port 42001 is not
        bound in the split-container topology.
        """
        response = self._typed_query.call('get_portfolio_summary', {}, dict)
        rows = []
        for p in response.get('positions') or []:
            symbol = str(p.get('symbol') or '')
            daily = p.get('daily_pnl')
            unrealized = p.get('unrealized_pnl')
            realized = p.get('realized_pnl')
            rows.append({
                'account': p.get('account', ''),
                'conId': int(p.get('instrument_id') or 0),
                'symbol': symbol,
                'position': float(p.get('position') or 0.0),
                'mktPrice': float(p.get('market_price') or 0.0),
                'avgCost': float(p.get('average_cost') or 0.0),
                'marketValue': float(p.get('market_value') or 0.0),
                'currency': p.get('currency', ''),
                'unrealizedPNL': float('nan') if unrealized is None else float(unrealized),
                'realizedPNL': float('nan') if realized is None else float(realized),
                'dailyPNL': float('nan') if daily is None else float(daily),
            })
            # Cache contract for close_position (avoids re-resolution which
            # fails for international stocks not in the local universe).
            if symbol:
                self._contract_map[symbol] = Contract(
                    conId=int(p.get('instrument_id') or 0),
                    symbol=symbol,
                    secType=str(p.get('security_type') or 'STK'),
                    exchange=str(p.get('exchange') or 'SMART'),
                    primaryExchange=str(p.get('primary_exchange') or ''),
                    currency=str(p.get('currency') or ''),
                )

        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values(by='dailyPNL', ascending=False).reset_index(drop=True)
            self._position_map = {
                i + 1: row['symbol'] for i, row in df.iterrows()
            }
        return df

    def _account_values(self) -> dict:
        """Account value tags via typed query (JSON-safe dict)."""
        return self._typed_query.call('get_account_values', {}, dict) or {}

    def _fx_rates(self) -> dict:
        """Per-currency multipliers to the account's base currency (base → 1.0).

        Degrades to an empty dict on RPC failure; callers then treat unknown
        currencies as already-base (rate 1.0).
        """
        try:
            response = self._typed_query.call('get_fx_rates', {}, dict) or {}
            rates = response.get('rates')
            return rates if isinstance(rates, dict) else {}
        except Exception as ex:
            logging.warning('could not fetch FX rates, treating values as base: %s', ex)
            return {}

    @staticmethod
    def _to_base(value: float, currency: str, fx_rates: dict) -> float:
        """Convert a local-currency amount to base using fx_rates.

        An unknown currency (or empty rates) falls back to rate 1.0 — i.e. it is
        treated as already base. That's the safe degradation: no worse than the
        old unconverted behaviour, and correct for the common single-currency case.
        """
        if not currency:
            return value
        return value * float(fx_rates.get(currency, 1.0) or 1.0)

    def positions(self) -> pd.DataFrame:
        """Raw positions (no P&L) via typed ``get_positions``."""
        response = self._typed_query.call('get_positions', {}, dict)
        rows = []
        for p in response.get('positions') or []:
            qty = float(p.get('position') or 0.0)
            avg = float(p.get('average_cost') or 0.0)
            symbol = str(p.get('symbol') or '')
            rows.append({
                'account': p.get('account', ''),
                'conId': int(p.get('instrument_id') or 0),
                'symbol': symbol,
                'secType': p.get('security_type', 'STK'),
                'position': qty,
                'avgCost': avg,
                'currency': p.get('currency', ''),
                'total': qty * avg,
            })
            if symbol:
                self._contract_map[symbol] = Contract(
                    conId=int(p.get('instrument_id') or 0),
                    symbol=symbol,
                    secType=str(p.get('security_type') or 'STK'),
                    exchange=str(p.get('exchange') or 'SMART'),
                    primaryExchange=str(p.get('primary_exchange') or ''),
                    currency=str(p.get('currency') or ''),
                )

        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values(by='currency').reset_index(drop=True)
            self._position_map = {
                i + 1: row['symbol'] for i, row in df.iterrows()
            }
        return df

    # ------------------------------------------------------------------
    # Order Book
    # ------------------------------------------------------------------

    def orders(self) -> pd.DataFrame:
        """Open orders with full detail (symbol, name, prices, account %)."""
        response = self._typed_query.call('get_open_orders', {}, dict)
        orders = response.get('orders') or []
        if not orders:
            return pd.DataFrame()

        net_liq = 0.0
        try:
            acct_vals = self._account_values()
            net_liq = float(acct_vals.get('NetLiquidation', {}).get('value', 0))
        except Exception:
            pass

        snap_cache: dict[str, dict] = {}
        ids = sorted({
            int(o.get('instrument_id') or 0)
            for o in orders if o.get('instrument_id')
        })
        ids = [i for i in ids if i > 0]
        if ids:
            try:
                batch = self._typed_query.call(
                    'get_snapshots_batch',
                    {'instrument_ids': ids, 'delayed': True},
                    dict,
                )
                for snap in batch.get('snapshots') or []:
                    snap_cache[str(snap.get('symbol') or '')] = {
                        'bid': snap.get('bid'),
                        'ask': snap.get('ask'),
                        'last': snap.get('last'),
                    }
            except Exception:
                pass

        rows = []
        for o in orders:
            symbol = str(o.get('symbol') or '')
            snap = snap_cache.get(symbol, {})
            _lmt = o.get('limit_price') or 0
            _aux = o.get('aux_price') or 0
            price = _lmt or _aux or 0
            qty = float(o.get('quantity') or 0)
            order_value = price * qty if price else None
            acct_pct = (order_value / net_liq * 100) if order_value and net_liq > 0 else None
            rows.append({
                'orderId': o.get('order_id'),
                'symbol': symbol,
                'name': '',
                'action': o.get('action'),
                'orderType': o.get('order_type'),
                'quantity': qty,
                'lmtPrice': _lmt or None,
                'auxPrice': _aux or None,
                'orderValue': round(order_value, 2) if order_value else None,
                'acctPct': round(acct_pct, 1) if acct_pct else None,
                'status': o.get('status'),
                'filled': o.get('filled'),
                'remaining': o.get('remaining'),
                'avgFillPrice': o.get('avg_fill_price'),
                'tif': o.get('tif'),
                'parentId': None,
                'bid': snap.get('bid'),
                'ask': snap.get('ask'),
                'last': snap.get('last'),
            })

        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values(by='orderId', ascending=True)
        return df

    def trades(self) -> pd.DataFrame:
        """Active trades in the book."""
        response = self._typed_query.call('get_trades', {}, dict)
        rows = []
        for t in response.get('trades') or []:
            rows.append({
                'conId': t.get('instrument_id'),
                'symbol': t.get('symbol'),
                'orderId': t.get('order_id'),
                'action': t.get('action'),
                'status': t.get('status'),
                'filled': t.get('filled'),
                'orderType': t.get('order_type'),
                'lmtPrice': t.get('limit_price'),
                'totalQuantity': t.get('quantity'),
            })

        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values(by='orderId', ascending=True)
        return df

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

    def _place_order(
        self,
        symbol: Union[str, int],
        action: str,
        amount: Optional[float] = None,
        quantity: Optional[float] = None,
        limit_price: Optional[float] = None,
        market: bool = False,
        stop_loss_percentage: float = 0.0,
        debug: bool = False,
        sec_type: str = 'STK',
        exchange: str = '',
        currency: str = '',
    ) -> SuccessFail:
        if not market and limit_price is None:
            raise ValueError("Specify market=True or provide a limit_price")
        if amount is None and quantity is None:
            raise ValueError("Specify amount (dollar value) or quantity")

        # Use cached contract from portfolio if available (international stocks).
        contract = self._contract_map.get(symbol) if isinstance(symbol, str) else None
        if contract is None:
            contract = self._resolve_contract(symbol, sec_type=sec_type,
                                              exchange=exchange, currency=currency)

        try:
            return consume(
                self._legacy_or_raise('direct buy/sell').rpc(
                    return_type=SuccessFail[Trade]
                ).place_order_simple(
                    contract=contract,
                    action=action,
                    equity_amount=amount,
                    quantity=quantity,
                    limit_price=limit_price,
                    market_order=market,
                    stop_loss_percentage=stop_loss_percentage,
                    debug=debug,
                )
            )
        except Exception as exc:
            self._map_legacy_route_error('direct buy/sell', exc)

    def buy(
        self,
        symbol: Union[str, int],
        amount: Optional[float] = None,
        quantity: Optional[float] = None,
        limit_price: Optional[float] = None,
        market: bool = False,
        stop_loss_percentage: float = 0.0,
        debug: bool = False,
        sec_type: str = 'STK',
        exchange: str = '',
        currency: str = '',
    ) -> SuccessFail:
        """Place a buy order."""
        return self._place_order(
            symbol, 'BUY', amount, quantity, limit_price, market,
            stop_loss_percentage, debug, sec_type=sec_type,
            exchange=exchange, currency=currency,
        )

    def sell(
        self,
        symbol: Union[str, int],
        amount: Optional[float] = None,
        quantity: Optional[float] = None,
        limit_price: Optional[float] = None,
        market: bool = False,
        stop_loss_percentage: float = 0.0,
        debug: bool = False,
        sec_type: str = 'STK',
        exchange: str = '',
        currency: str = '',
    ) -> SuccessFail:
        """Place a sell order."""
        return self._place_order(
            symbol, 'SELL', amount, quantity, limit_price, market,
            stop_loss_percentage, debug, sec_type=sec_type,
            exchange=exchange, currency=currency,
        )

    def cancel(self, order_id: int) -> SuccessFail:
        """Cancel a single order by ID."""
        try:
            return self._legacy_or_raise('cancel').rpc(
                return_type=SuccessFail[Trade]
            ).cancel_order(order_id)
        except Exception as exc:
            self._map_legacy_route_error('cancel', exc)

    def cancel_all(self) -> SuccessFail:
        """Cancel all open orders."""
        try:
            return self._legacy_or_raise('cancel-all').rpc(
                return_type=SuccessFail[list[int]]
            ).cancel_all()
        except Exception as exc:
            self._map_legacy_route_error('cancel-all', exc)

    def to_market(self, order_id: int) -> SuccessFail:
        """Cancel an open limit order and re-place as market, preserving stop-loss children."""
        # Needs cancel + place_expressive_order on legacy RPC (not on typed production surface).
        try:
            rpc = self._legacy_or_raise('to-market')
        except ConnectionError:
            raise
        try:
            trades_raw: dict[int, list[Trade]] = rpc.rpc(
                return_type=dict[int, list[Trade]]
            ).get_trades()
        except Exception as exc:
            self._map_legacy_route_error('to-market', exc)

        if not trades_raw or order_id not in trades_raw:
            return SuccessFail.fail(error=f'Order #{order_id} not found')

        trade = trades_raw[order_id][0]
        contract = trade.contract
        order = trade.order
        action = order.action

        if order.orderType == 'MKT':
            return SuccessFail.fail(error=f'Order #{order_id} is already a market order')

        def _remaining(tr) -> float:
            # Replace only the UNFILLED portion. totalQuantity is the original
            # order size; using it double-counts anything already filled and
            # oversizes the resulting position.
            st = getattr(tr, 'orderStatus', None)
            total = float(tr.order.totalQuantity)
            try:
                rem = getattr(st, 'remaining', None)
                rem = float(rem) if rem is not None else None
            except (TypeError, ValueError):
                rem = None
            try:
                filled = float(getattr(st, 'filled', 0.0) or 0.0)
            except (TypeError, ValueError):
                filled = 0.0
            # Unpopulated status (fresh/unacked order): treat as fully unfilled.
            if (rem is None or rem == 0.0) and filled == 0.0:
                return total
            if rem is not None and rem > 0.0:
                return rem
            return total - filled

        remaining = _remaining(trade)
        if remaining <= 0:
            return SuccessFail.fail(
                error=f'Order #{order_id} has no unfilled quantity to convert to market')

        # Find stop-loss child (parentId == this order)
        stop_price = None
        for tid, tlist in trades_raw.items():
            child = tlist[0]
            if child.order.parentId == order_id and child.order.orderType in ('STP', 'STP LMT'):
                stop_price = child.order.auxPrice
                break

        # Cancel parent (IB auto-cancels bracket children) and REQUIRE the cancel
        # to be accepted before placing the market order — otherwise a cancel that
        # is rejected because the limit just filled would leave both orders live
        # and double the position.
        cancel_result = self.cancel(order_id)
        if not getattr(cancel_result, 'is_success', lambda: True)():
            return SuccessFail.fail(
                error=(f'Order #{order_id}: cancel not confirmed '
                       f'({getattr(cancel_result, "error", "?")}); not placing market '
                       f'order to avoid double execution'))

        # Re-read the order to pick up any fill that landed during cancellation and
        # recompute the still-unfilled quantity. If it fully filled in the window,
        # there is nothing left to convert.
        try:
            trades_after = rpc.rpc(
                return_type=dict[int, list[Trade]]
            ).get_trades()
            if trades_after and order_id in trades_after:
                remaining = _remaining(trades_after[order_id][0])
        except Exception as ex:
            logging.warning(f'to_market: could not re-read order #{order_id} after cancel: {ex}')
        if remaining <= 0:
            return SuccessFail.fail(
                error=f'Order #{order_id} filled during cancellation; nothing left to convert')

        # Build execution spec and re-place the remaining quantity as market
        from trader.trading.proposal import ExecutionSpec
        if stop_price:
            spec = ExecutionSpec(order_type='MARKET', exit_type='STOP_LOSS', stop_loss_price=stop_price)
        else:
            spec = ExecutionSpec(order_type='MARKET')

        return consume(
            self._legacy_or_raise('place expressive order').rpc(
                return_type=SuccessFail[list[Trade]]
            ).place_expressive_order(
                contract=contract,
                action=action,
                quantity=remaining,
                execution_spec=spec.to_dict(),
                algo_name='to_market',
            )
        )

    # ------------------------------------------------------------------
    # Trade Proposals
    # ------------------------------------------------------------------

    def _proposal_store(self):
        """Lazy-init ProposalStore from config duckdb_path."""
        if not hasattr(self, '_prop_store'):
            cfg = self._container.config()
            duckdb_path = cfg.get('duckdb_path', '')
            if not duckdb_path:
                raise ValueError("duckdb_path not configured")
            from trader.data.proposal_store import ProposalStore
            self._prop_store = ProposalStore(duckdb_path)
        return self._prop_store

    def _get_portfolio_state(self):
        """Build a PortfolioState snapshot. Gracefully degrades if trader_service unavailable.

        Per-source failures are recorded on ``state.rpc_errors`` and logged rather
        than silently swallowed — callers (e.g. ``session_status``) surface them so
        consumers can distinguish "account is empty" from "the RPC call failed".
        """
        from trader.trading.position_sizing import PortfolioState
        state = PortfolioState()
        try:
            acct_vals = self._account_values()
            if acct_vals:
                state.net_liquidation = float(acct_vals.get('NetLiquidation', {}).get('value', 0))
                state.gross_position_value = float(acct_vals.get('GrossPositionValue', {}).get('value', 0))
                state.available_funds = float(acct_vals.get('AvailableFunds', {}).get('value', 0))
        except Exception as e:
            logger.warning('get_account_values RPC failed: %s', e)
            state.rpc_errors.append(f'account_values: {type(e).__name__}: {e}')

        try:
            portfolio_df = self.portfolio()
            if portfolio_df is not None and not portfolio_df.empty:
                state.position_count = len(portfolio_df)
                if 'dailyPNL' in portfolio_df.columns:
                    col = portfolio_df['dailyPNL']
                    # .sum() skips NaN, so a position whose PnL subscription is
                    # missing counts as 0 and the daily-loss circuit breaker
                    # under-fires. Surface the gap so daily_pnl isn't trusted as
                    # a hard floor when incomplete.
                    nan_count = int(col.isna().sum())
                    state.daily_pnl = float(col.sum())
                    if nan_count:
                        state.rpc_errors.append(
                            f'daily_pnl incomplete: {nan_count} position(s) missing PnL '
                            f'(subscription gap) — daily-loss limit may under-count')
        except Exception as e:
            logger.warning('portfolio() RPC failed: %s', e)
            state.rpc_errors.append(f'portfolio: {type(e).__name__}: {e}')

        try:
            store = self._proposal_store()
            pending = store.query(status='PENDING')
            if pending:
                state.pending_proposal_value = sum(
                    p.amount for p in pending if p.amount is not None and p.amount == p.amount
                )
        except Exception as e:
            logger.warning('proposal_store query failed: %s', e)
            state.rpc_errors.append(f'proposal_store: {type(e).__name__}: {e}')

        return state

    def session_status(self) -> dict:
        """Return position sizing config, portfolio state, capacity, and recommended sizes."""
        from trader.trading.position_sizing import PositionSizingConfig, PositionSizer
        config = PositionSizingConfig.load()
        state = self._get_portfolio_state()
        return PositionSizer(config).session_summary(state)

    def reconcile(self) -> dict:
        """Report-only broker-truth reconciliation. Requires trader_service.

        Returns a divergence report comparing recent proposals + current
        positions against live IB open-orders / executions / positions. Places
        or cancels nothing.
        """
        return self._typed_query.call('reconcile_with_broker', {}, dict) or {}

    def risk_report(self) -> dict:
        """Generate a portfolio risk report. Requires trader_service for portfolio data."""
        from trader.trading.portfolio_risk import PortfolioRiskAnalyzer
        cfg = self._container.config()
        duckdb_path = cfg.get('duckdb_path', '')
        history_duckdb_path = cfg.get('history_duckdb_path', '') or duckdb_path

        portfolio_df = self.portfolio()
        positions = portfolio_df.to_dict('records') if portfolio_df is not None and not portfolio_df.empty else []

        # Convert each position's marketValue to the account base currency before
        # the analyzer compares it against NetLiquidation (base). The analyzer is
        # currency-agnostic — it just needs every value in the same currency — so
        # we normalise here. Preserves sign (shorts stay negative).
        fx_rates = self._fx_rates()
        for p in positions:
            p['marketValue'] = self._to_base(
                float(p.get('marketValue', 0) or 0), p.get('currency', ''), fx_rates)

        # Let RPC failures propagate — silently defaulting net_liq to 0 makes
        # every exposure %/HHI/group-budget number wrong without signaling why.
        acct_vals = self._account_values()
        net_liq = float(acct_vals.get('NetLiquidation', {}).get('value', 0)) if acct_vals else 0.0

        analyzer = PortfolioRiskAnalyzer(duckdb_path, history_duckdb_path)
        try:
            gs = self._group_store()
        except Exception:
            gs = None

        report = analyzer.analyze(positions, net_liq, group_store=gs)
        return report.to_dict()

    def portfolio_snapshot(self) -> dict:
        """Compact portfolio snapshot for LLM loop monitoring.

        Returns a small JSON object with just the key metrics an LLM needs
        to decide whether to dig deeper: total value, daily P&L, position count,
        and the top movers (biggest daily % changes). Requires trader_service.
        """
        import math

        portfolio_df = self.portfolio()

        # Fetch account values before the empty-position check: a flat
        # account still holds cash, and net_liquidation must reflect that
        # instead of silently reporting 0 just because there are no positions.
        acct_vals = self._account_values()
        net_liq = float(acct_vals.get('NetLiquidation', {}).get('value', 0)) if acct_vals else 0.0

        if portfolio_df is None or portfolio_df.empty:
            return {'total_value': 0.0, 'net_liquidation': round(net_liq, 2),
                    'daily_pnl': 0, 'position_count': 0,
                    'exposure_pct': 0, 'movers': [], 'timestamp': str(dt.datetime.now())}

        total_value = 0.0
        daily_pnl = 0.0
        movers = []
        for _, row in portfolio_df.iterrows():
            mv = float(row.get('marketValue', 0) or 0)
            dp = float(row.get('dailyPNL', 0) or 0)
            price = float(row.get('mktPrice', 0) or 0)
            avg = float(row.get('avgCost', 0) or 0)
            if isinstance(mv, float) and math.isnan(mv):
                mv = 0.0
            if isinstance(dp, float) and math.isnan(dp):
                dp = 0.0
            total_value += abs(mv)
            daily_pnl += dp

            # Compute daily change % from dailyPNL / marketValue
            change_pct = (dp / abs(mv)) if abs(mv) > 0 else 0.0
            movers.append({
                'symbol': row.get('symbol', ''),
                'change_pct': round(change_pct, 4),
                'daily_pnl': round(dp, 2),
                'value': round(abs(mv), 2),
            })

        # Sort by absolute change — biggest movers first
        movers.sort(key=lambda m: abs(m['change_pct']), reverse=True)

        exposure_pct = total_value / net_liq if net_liq > 0 else 0.0

        return {
            'total_value': round(total_value, 2),
            'net_liquidation': round(net_liq, 2),
            'daily_pnl': round(daily_pnl, 2),
            'position_count': len(portfolio_df),
            'exposure_pct': round(exposure_pct, 4),
            'movers': movers[:10],  # top 10 by absolute change
            'timestamp': str(dt.datetime.now()),
        }

    def portfolio_diff(self) -> dict:
        """Compare current portfolio to the last stored snapshot.

        Returns {changed, new, removed, unchanged_count, prev_timestamp}.
        Stores current snapshot in DuckDB for next call. First call returns
        all positions as 'new' (no previous snapshot exists).
        """
        import math
        from trader.data.duckdb_store import DuckDBObjectStore

        portfolio_df = self.portfolio()
        cfg = self._container.config()
        duckdb_path = cfg.get('duckdb_path', '')

        # Build current snapshot as symbol -> {value, daily_pnl, position}
        current = {}
        if portfolio_df is not None and not portfolio_df.empty:
            for _, row in portfolio_df.iterrows():
                sym = row.get('symbol', '')
                mv = float(row.get('marketValue', 0) or 0)
                dp = float(row.get('dailyPNL', 0) or 0)
                pos = float(row.get('position', 0) or 0)
                if isinstance(mv, float) and math.isnan(mv):
                    mv = 0.0
                if isinstance(dp, float) and math.isnan(dp):
                    dp = 0.0
                current[sym] = {
                    'value': round(abs(mv), 2),
                    'daily_pnl': round(dp, 2),
                    'position': pos,
                }

        # Load previous snapshot
        prev = {}
        prev_ts = None
        if duckdb_path:
            try:
                obj_store = DuckDBObjectStore(duckdb_path)
                stored = obj_store.read('_portfolio_snapshot')
                if stored and isinstance(stored, dict):
                    prev = stored.get('positions', {})
                    prev_ts = stored.get('timestamp')
            except Exception:
                pass

        # Compute diff
        new_symbols = []
        removed_symbols = []
        changed = []
        unchanged_count = 0

        all_symbols = set(list(current.keys()) + list(prev.keys()))
        for sym in sorted(all_symbols):
            cur = current.get(sym)
            prv = prev.get(sym)

            if cur and not prv:
                new_symbols.append({'symbol': sym, **cur})
            elif prv and not cur:
                removed_symbols.append({'symbol': sym, **prv})
            elif cur and prv:
                # Check if value changed by more than 0.5%
                pct_change = abs(cur['value'] - prv['value']) / prv['value'] if prv['value'] > 0 else 0
                if pct_change > 0.005 or cur['position'] != prv['position']:
                    changed.append({
                        'symbol': sym,
                        'value': cur['value'],
                        'prev_value': prv['value'],
                        'value_change': round(cur['value'] - prv['value'], 2),
                        'daily_pnl': cur['daily_pnl'],
                        'position': cur['position'],
                        'prev_position': prv['position'],
                    })
                else:
                    unchanged_count += 1

        # Store current snapshot for next diff
        if duckdb_path:
            try:
                obj_store = DuckDBObjectStore(duckdb_path)
                obj_store.write('_portfolio_snapshot', {
                    'positions': current,
                    'timestamp': str(dt.datetime.now()),
                })
            except Exception:
                pass

        # Sort changed by absolute value change
        changed.sort(key=lambda c: abs(c.get('value_change', 0)), reverse=True)

        return {
            'changed': changed,
            'new': new_symbols,
            'removed': removed_symbols,
            'unchanged_count': unchanged_count,
            'prev_timestamp': prev_ts,
            'timestamp': str(dt.datetime.now()),
        }

    def _group_store(self):
        """Lazy-init PositionGroupStore from config duckdb_path."""
        if not hasattr(self, '_grp_store'):
            cfg = self._container.config()
            duckdb_path = cfg.get('duckdb_path', '')
            if not duckdb_path:
                raise ValueError("duckdb_path not configured")
            from trader.data.position_groups import PositionGroupStore
            self._grp_store = PositionGroupStore(duckdb_path)
        return self._grp_store

    def propose(
        self,
        symbol: str,
        action: str,
        quantity: Optional[float] = None,
        amount: Optional[float] = None,
        execution=None,
        reasoning: str = '',
        confidence: float = 0.0,
        thesis: str = '',
        source: str = 'manual',
        metadata: Optional[dict] = None,
        sec_type: str = 'STK',
        exchange: str = '',
        currency: str = '',
        group: str = '',
    ) -> SuccessFail:
        """Create a trade proposal via the command-authority coordinator's
        ``create_proposal`` command. REQUIRES trader_service (typed command
        socket) -- filter/sizing/quote/group-registration checks and the
        proposal write itself all now happen server-side
        (``ProposalCommandService.create_proposal``); this adapter only
        resolves *symbol* to a conId and translates the keyword surface into
        the wire body. Returns a ``SuccessFail`` whose ``.obj`` is the
        created proposal's payload (normalized to always carry a
        ``proposal_id`` key) on success.

        Note: the typed ``create_proposal`` wire contract
        (``CreateProposalRequest``) does not yet carry an execution spec, a
        caller-supplied ``source`` label, or free-form ``metadata`` -- those
        fields are accepted here for signature compatibility but a non-default
        *execution* is refused loudly (rather than silently downgraded to a
        plain market order) and *source*/*metadata* are currently inert.
        """
        import uuid
        from trader.domain.commands import CommandReceipt
        from trader.trading.proposal import ExecutionSpec

        spec = execution if isinstance(execution, ExecutionSpec) else ExecutionSpec()
        validation_errors = spec.validate()
        if validation_errors:
            return SuccessFail.fail(error=f'Invalid execution spec: {"; ".join(validation_errors)}')
        if spec != ExecutionSpec():
            return SuccessFail.fail(error=(
                'propose(): the command-authority create_proposal endpoint only '
                'supports a plain MARKET entry with no exit strategy right now -- '
                'bracket/stop-loss/trailing-stop/limit execution specs are not yet '
                'carried by CreateProposalRequest. Use the default ExecutionSpec() '
                'for now, or place the order directly via `mmr buy`/`mmr sell`.'
            ))

        try:
            contract = self._resolve_contract(symbol, sec_type=sec_type, exchange=exchange, currency=currency)
        except Exception as ex:
            return SuccessFail.fail(error=f'Could not resolve symbol {symbol}: {ex}', exception=ex)

        body = {
            'command_id': f'sdk-{uuid.uuid4()}',
            'conid': contract.conId,
            'action': action,
            'quantity': quantity,
            'amount': amount,
            'reasoning': reasoning,
            'confidence': confidence,
            'thesis': thesis,
            'group': group,
        }
        try:
            receipt = self._typed_command.call('create_proposal', body, CommandReceipt)
        except (TimeoutError, ConnectionError) as ex:
            return SuccessFail.fail(
                error=f'create_proposal for {symbol} did not complete: {ex}', exception=ex)
        if receipt.error_code or receipt.state not in ('RESOLVED', 'SUBMITTED'):
            return SuccessFail.fail(error=(
                f'proposal for {symbol} rejected: {receipt.error_code or receipt.state}'))

        outcome = dict(receipt.outcome or {})
        # Normalize so callers can always read `proposal_id` regardless of
        # whether the server's payload key is `id` (the real
        # ProposalRecord.to_payload() shape) or `proposal_id`.
        outcome.setdefault('proposal_id', outcome.get('id'))
        return SuccessFail.success(obj=outcome)

    def proposals(self, status: Optional[str] = None, limit: int = 50) -> pd.DataFrame:
        """List proposals via the command-authority coordinator's
        ``list_proposals`` query. REQUIRES trader_service (typed query
        socket)."""
        response = self._typed_query.call(
            'list_proposals', {'status': status, 'limit': limit}, dict)
        props = response.get('proposals') or []
        if not props:
            return pd.DataFrame()

        rows = []
        for p in props:
            quantity = p.get('quantity')
            amount = p.get('amount')
            # Build concise size column: "100 sh" or "$5,000"
            if quantity is not None and quantity == quantity:  # not NaN
                size = f'{quantity:g} sh'
            elif amount is not None and amount == amount:
                size = f'${amount:,.0f}'
            else:
                size = '-'

            execution = p.get('execution') or {}
            order_type = execution.get('order_type', 'MARKET')
            limit_price = execution.get('limit_price')
            # Build concise order description: "MKT" or "LMT @165"
            order = 'MKT' if order_type == 'MARKET' or limit_price is None else f'LMT @{limit_price:g}'

            # Compact exit type
            exit_type = execution.get('exit_type', 'NONE')
            exit_abbrev = {'NONE': '-', 'BRACKET': 'BKT', 'TRAILING_STOP': 'TSL', 'STOP_LOSS': 'SL'}
            exit_label = exit_abbrev.get(exit_type, (exit_type or '-')[:3])

            # Compact timestamp — full detail in `proposals show N`
            created = ''
            created_at = p.get('created_at')
            if created_at:
                try:
                    created = dt.datetime.fromisoformat(str(created_at)).strftime('%m/%d %H:%M')
                except ValueError:
                    created = str(created_at)

            status_value = p.get('status')
            row = {
                'id': p.get('id'),
                'symbol': p.get('symbol'),
                'action': p.get('action'),
                'size': size,
                'order': order,
                'exit': exit_label,
                # Numeric confidence, not a formatted percent string — a
                # percent string loses the value for downstream consumers
                # (dashboard, LLM loop) that need to compare/sort/threshold it.
                'confidence': float(p.get('confidence') or 0.0),
                'created': created,
                'source': p.get('source') or 'manual',
                'reasoning': p.get('reasoning') or '',
                # Raw storage-layer status (state machine value, e.g.
                # 'EXECUTED') alongside a user-facing label — callers that
                # need the authoritative state machine value still have it,
                # while UI/LLM consumers get plain English.
                'storage_status': status_value,
                'display_status': proposal_display_status(status_value),
                # [M1-F3] Task 8: the command-authority row carries these
                # natively -- surfaced so a caller can check the exact
                # revision to approve against and how fresh the reference
                # quote/expiry are, without a separate `proposals show` call.
                'revision': p.get('revision'),
                'expires_at': p.get('expires_at'),
                'reference_price': p.get('reference_price'),
            }
            # Include status column only when showing mixed statuses (--all)
            if status is None:
                row['status'] = status_value
            rows.append(row)
        return pd.DataFrame(rows)

    def proposal_detail(self, proposal_id: int) -> Optional[dict]:
        """Get full proposal detail as dict. No trader_service needed."""
        p = self._proposal_store().get(proposal_id)
        if not p:
            return None
        detail = {
            'id': p.id,
            'symbol': p.symbol,
            'action': p.action,
            'quantity': p.quantity,
            'amount': p.amount,
            'currency': getattr(p, 'currency', None),
            'sec_type': p.sec_type,
            'order_type': p.execution.order_type,
            'limit_price': p.execution.limit_price,
            'exit_type': p.execution.exit_type,
            'take_profit_price': p.execution.take_profit_price,
            'stop_loss_price': p.execution.stop_loss_price,
            'trailing_stop_percent': p.execution.trailing_stop_percent,
            'trailing_stop_amount': p.execution.trailing_stop_amount,
            'tif': p.execution.tif,
            'outside_rth': p.execution.outside_rth,
            'good_till_date': p.execution.good_till_date,
            'reasoning': p.reasoning,
            'confidence': p.confidence,
            'thesis': p.thesis,
            'source': p.source,
            'metadata': p.metadata,
            'status': p.status,
            'created_at': p.created_at,
            'updated_at': p.updated_at,
            'order_ids': p.order_ids,
            'rejection_reason': p.rejection_reason,
            'group': p.group,
        }

        # Include snapshot from metadata if present
        snapshot = p.metadata.get('snapshot')
        if snapshot:
            detail['snapshot_bid'] = snapshot.get('bid')
            detail['snapshot_ask'] = snapshot.get('ask')
            detail['snapshot_last'] = snapshot.get('last')
            detail['snapshot_time'] = snapshot.get('time')

        # Include leverage estimate from metadata if present
        leverage = p.metadata.get('leverage_estimate')
        if leverage:
            detail['leverage_current'] = leverage.get('current_leverage')
            detail['leverage_estimated'] = leverage.get('estimated_leverage')
            detail['uses_margin'] = leverage.get('uses_margin', False)

        return detail

    def reject(self, proposal_id: int, reason: str = '') -> bool:
        """Reject a proposal via the command-authority coordinator's
        ``reject_proposal`` command. REQUIRES trader_service (typed command
        socket) -- the state-machine transition now happens server-side."""
        import uuid
        from trader.domain.commands import CommandReceipt

        try:
            receipt = self._typed_command.call(
                'reject_proposal',
                {'command_id': f'sdk-{uuid.uuid4()}', 'proposal_id': proposal_id, 'reason': reason},
                CommandReceipt,
            )
        except (TimeoutError, ConnectionError):
            return False
        return receipt.state == 'RESOLVED'

    def approve(self, proposal_id: int, expected_version: Optional[int] = None) -> SuccessFail:
        """Approve and execute a proposal via the command-authority
        coordinator's guarded ``approve_proposal`` saga. REQUIRES
        trader_service (typed query + command sockets) -- claim,
        expiry/drift/risk checks, quote lookup, sizing, and order dispatch
        all now happen server-side (``ApprovalCommandService.approve``);
        this adapter only fetches the current revision (when the caller
        doesn't supply one) and translates the receipt.

        *expected_version* pins the exact proposal revision being approved
        (a CAS guard against approving a proposal that changed since the
        caller last reviewed it). When omitted, the SDK fetches the
        proposal's current revision via ``get_proposal`` first.

        On **live**, SDK/LLM approve is refused (``LLM_LIVE_APPROVE_FORBIDDEN``)
        — use the Command Center live ceremony. On **paper**, the LLM may
        approve after evaluation; this call stamps ``source=sdk``.
        """
        import uuid
        from trader.domain.commands import CommandReceipt
        from trader.messaging.typed_rpc import TypedRpcRemoteError

        # Always fetch for the live gate (and revision when the caller omitted it).
        try:
            view = self._typed_query.call(
                'get_proposal', {'proposal_id': proposal_id}, dict)
        except TypedRpcRemoteError as ex:
            # e.g. PROPOSAL_NOT_FOUND — a clean, expected refusal, not a
            # transport failure.
            return SuccessFail.fail(
                error=f'Proposal #{proposal_id}: {ex.code}: {ex.message}', exception=ex)
        except (TimeoutError, ConnectionError) as ex:
            return SuccessFail.fail(
                error=f'could not fetch proposal #{proposal_id} to approve: {ex}',
                exception=ex)

        if (view.get('account_mode') or '').lower() == 'live':
            return SuccessFail.fail(error=(
                f'Proposal #{proposal_id}: LLM_LIVE_APPROVE_FORBIDDEN — '
                f'SDK/LLM cannot approve on a live account. Use the Command '
                f'Center live ceremony (human + preflight), or reject the '
                f'proposal if it should not trade.'
            ))

        if expected_version is None:
            expected_version = view.get('revision')

        try:
            receipt = self._typed_command.call(
                'approve_proposal',
                {'command_id': f'sdk-{uuid.uuid4()}', 'proposal_id': proposal_id,
                 'expected_version': expected_version},
                CommandReceipt,
            )
        except (TimeoutError, ConnectionError) as ex:
            return SuccessFail.fail(
                error=f'approve_proposal for #{proposal_id} did not complete: {ex}. '
                      f'Check `mmr proposals` / `mmr orders` before retrying.',
                exception=ex)

        if receipt.state == 'SUBMITTED':
            return SuccessFail.success(obj=(receipt.outcome or {}).get('order_ids', []))
        if receipt.state == 'OUTCOME_UNKNOWN':
            # Loud ambiguity, never a silent failure: the order MAY be live
            # at the broker even though this call can't confirm it (see
            # ApprovalCommandService's OUTCOME_UNKNOWN contract).
            return SuccessFail.fail(error=(
                f'Proposal #{proposal_id}: outcome unknown — trader_service is '
                f'reconciling by orderRef (command {receipt.command_id}). '
                f'Do NOT re-approve; watch `mmr proposals` for the resolution.'))
        return SuccessFail.fail(
            error=f'Proposal #{proposal_id} approve rejected: '
                  f'{receipt.error_code or receipt.state}')

    # ------------------------------------------------------------------
    # [P4 Task 5] Signed live-canary activation
    # ------------------------------------------------------------------

    def activate_live_canary(self, attestation: Dict[str, Any], reason: str) -> SuccessFail:
        """Activate a signed canary authority via the command-authority
        coordinator's ``activate_live_canary`` command. REQUIRES
        trader_service (typed query + command sockets).

        ``attestation`` is the JSON wire form of an offline-signed
        ``CanaryAttestation`` (produced entirely by ``mmr research canary
        sign`` -- see ``canary_attestation_to_wire``); it carries a
        signature and public key ID, NEVER a private key, so nothing this
        method sends over the wire can ever leak signing material.

        ``activate_live_canary`` is always risk-increasing (a canary
        authority is definitionally live-only), so unlike ``approve``, this
        method itself drives the two-step preflight-nonce ceremony
        (``preflight_command`` mint, then the confirming command) rather
        than skipping it — there is no paper-mode carve-out to lean on.
        """
        import uuid
        from trader.domain.commands import CommandReceipt
        from trader.messaging.typed_rpc import TypedRpcRemoteError

        command_id = f'sdk-{uuid.uuid4()}'
        session_fingerprint = uuid.uuid4().hex

        try:
            preflight = self._typed_command.call(
                'preflight_command',
                {
                    'command_id': command_id, 'action': 'activate_live_canary',
                    'params': {'attestation': attestation, 'reason': reason},
                    'session_fingerprint': session_fingerprint,
                },
                dict,
            )
        except TypedRpcRemoteError as ex:
            return SuccessFail.fail(error=f'canary activation preflight refused: {ex.code}: {ex.message}',
                                    exception=ex)
        except (TimeoutError, ConnectionError) as ex:
            return SuccessFail.fail(error=f'canary activation preflight did not complete: {ex}', exception=ex)

        try:
            receipt = self._typed_command.call(
                'activate_live_canary',
                {
                    'command_id': command_id, 'attestation': attestation, 'reason': reason,
                    'preflight_nonce': preflight['nonce'], 'session_fingerprint': session_fingerprint,
                },
                CommandReceipt,
            )
        except TypedRpcRemoteError as ex:
            return SuccessFail.fail(error=f'canary activation rejected: {ex.code}: {ex.message}', exception=ex)
        except (TimeoutError, ConnectionError) as ex:
            return SuccessFail.fail(
                error=f'activate_live_canary did not complete: {ex}. Check `mmr proposals`/status before retrying.',
                exception=ex)

        if receipt.state == 'RESOLVED':
            return SuccessFail.success(obj=receipt.outcome)
        return SuccessFail.fail(error=f'canary activation rejected: {receipt.error_code or receipt.state}')

    def activate_allocation(self, attestation: Dict[str, Any], reason: str) -> SuccessFail:
        """Activate a signed allocation authority via ``activate_allocation``.
        REQUIRES trader_service. Risk-increasing — drives preflight nonce ceremony."""
        import uuid
        from trader.domain.commands import CommandReceipt
        from trader.messaging.typed_rpc import TypedRpcRemoteError

        command_id = f'sdk-{uuid.uuid4()}'
        session_fingerprint = uuid.uuid4().hex

        try:
            preflight = self._typed_command.call(
                'preflight_command',
                {
                    'command_id': command_id, 'action': 'activate_allocation',
                    'params': {'attestation': attestation, 'reason': reason},
                    'session_fingerprint': session_fingerprint,
                },
                dict,
            )
        except TypedRpcRemoteError as ex:
            return SuccessFail.fail(error=f'allocation activation preflight refused: {ex.code}: {ex.message}',
                                    exception=ex)
        except (TimeoutError, ConnectionError) as ex:
            return SuccessFail.fail(error=f'allocation activation preflight did not complete: {ex}', exception=ex)

        try:
            receipt = self._typed_command.call(
                'activate_allocation',
                {
                    'command_id': command_id, 'attestation': attestation, 'reason': reason,
                    'preflight_nonce': preflight['nonce'], 'session_fingerprint': session_fingerprint,
                },
                CommandReceipt,
            )
        except TypedRpcRemoteError as ex:
            return SuccessFail.fail(error=f'allocation activation rejected: {ex.code}: {ex.message}', exception=ex)
        except (TimeoutError, ConnectionError) as ex:
            return SuccessFail.fail(
                error=f'activate_allocation did not complete: {ex}. Check status before retrying.',
                exception=ex)

        if receipt.state == 'RESOLVED':
            return SuccessFail.success(obj=receipt.outcome)
        return SuccessFail.fail(error=f'allocation activation rejected: {receipt.error_code or receipt.state}')

    def suspend_allocation(self, reason: str) -> SuccessFail:
        """Suspend the account's active allocation authority via ``suspend_allocation``.
        REQUIRES trader_service. Risk-reducing: no preflight nonce needed,
        mirroring ``deactivate_live_canary``."""
        import uuid
        from trader.domain.commands import CommandReceipt
        from trader.messaging.typed_rpc import TypedRpcRemoteError

        try:
            receipt = self._typed_command.call(
                'suspend_allocation',
                {'command_id': f'sdk-{uuid.uuid4()}', 'reason': reason},
                CommandReceipt,
            )
        except TypedRpcRemoteError as ex:
            return SuccessFail.fail(error=f'allocation suspension rejected: {ex.code}: {ex.message}', exception=ex)
        except (TimeoutError, ConnectionError) as ex:
            return SuccessFail.fail(error=f'suspend_allocation did not complete: {ex}', exception=ex)

        if receipt.state == 'RESOLVED':
            return SuccessFail.success(obj=receipt.outcome)
        return SuccessFail.fail(error=f'allocation suspension rejected: {receipt.error_code or receipt.state}')

    # ------------------------------------------------------------------
    # SP1 ai_paper experiments (Plan 4)
    # ------------------------------------------------------------------

    def experiment_status(self) -> dict:
        """``get_experiment``: the newest experiment, its entry block and both kill lines.
        REQUIRES trader_service."""
        return self._typed_query.call('get_experiment', {}, dict)

    # ------------------------------------------------------------------
    # SP1 scoreboard (Plan 5): reads only. REQUIRES trader_service.
    # ------------------------------------------------------------------

    def scoreboard(self, experiment_id: Optional[str] = None) -> dict:
        """``get_scoreboard``: the PAPER report of the latest (or named) experiment."""
        body = {} if experiment_id is None else {'experiment_id': experiment_id}
        return self._typed_query.call('get_scoreboard', body, dict)

    def verify_scoreboard(self, experiment_id: Optional[str] = None) -> dict:
        """``verify_scoreboard``: rebuild every number from stored inputs (humans only)."""
        body = {} if experiment_id is None else {'experiment_id': experiment_id}
        return self._typed_query.call('verify_scoreboard', body, dict)

    def flatten(self, reason: str, command_id: Optional[str] = None) -> dict:
        """``liquidate_account`` (SP1 Plan 6): flatten the whole account. Paper only from the CLI."""
        import uuid
        body = {'command_id': command_id or f'flatten-{uuid.uuid4().hex[:16]}', 'reason': reason}
        return self._typed_command.call('liquidate_account', body, dict)

    def account_id(self) -> str:
        return str(self._typed_query.call('get_ib_account', {}, dict).get('account_id') or '')

    def flat_state(self, command_id: str) -> dict:
        """The command and the broker: open positions and orders that may still fill on a promoted generation."""
        from trader.messaging.typed_rpc import TypedRpcRemoteError
        try:
            command = self._typed_query.call('get_command', {'command_id': command_id}, dict)
        except TypedRpcRemoteError as ex:
            command = {'state': None, 'error_code': ex.code}
        positions = [p for p in self._typed_query.call('get_positions', {}, dict).get('positions') or []
                     if float(p.get('position') or 0.0)]
        from trader.acceptance.order_status import may_still_fill
        evidence = self._typed_query.call('get_broker_order_evidence', {'conid': None}, dict)
        working = [o for o in evidence.get('orders') or [] if may_still_fill(o)]
        return {'command': command, 'positions': positions, 'working_orders': working,
                'capture_error': evidence.get('capture_error')}

    def wait_flat(self, command_id: str, timeout: float = 300.0, *, sleep=None, clock=None,
                  poll: float = 2.0) -> dict:
        """Poll until the command resolved and the broker shows no position and no working order."""
        import time as _time
        sleep, clock = sleep or _time.sleep, clock or _time.monotonic
        deadline = clock() + timeout
        while True:
            state = self.flat_state(command_id)
            resolved = (state['command'] or {}).get('state') == 'RESOLVED'
            state['flat'] = (resolved and not state['positions'] and not state['working_orders']
                             and not state['capture_error'])
            if state['flat'] or clock() >= deadline:
                return state
            sleep(poll)

    def acceptance_preflight(self) -> dict:
        """``get_acceptance_preflight`` (SP1 Plan 6): one reading of the clean-account gate, signed as cli."""
        return self._typed_query.call('get_acceptance_preflight', {}, dict)

    def acceptance_endpoints(self) -> 'Endpoints':
        """Where ``mmr experiment acceptance`` dials the trader and finds the RPC keys (host only)."""
        from trader.acceptance.runner import Endpoints
        return Endpoints(address=self._typed_address, query_port=self._typed_query_port,
                         command_port=self._typed_command_port, keys_dir=self._rpc_keys_dir,
                         timeout=float(self._timeout))

    def experiment_trips(self, experiment_id: str) -> dict:
        """``get_experiment_trips``: conid, quantities and state of each round trip."""
        return self._typed_query.call('get_experiment_trips', {'experiment_id': experiment_id}, dict)

    def _experiment_command(self, method: str, reason: str, experiment_id: Optional[str]) -> SuccessFail:
        import uuid
        from trader.domain.commands import CommandReceipt
        from trader.messaging.typed_rpc import TypedRpcRemoteError

        body = {'command_id': f'sdk-{uuid.uuid4()}', 'reason': reason}
        if method != 'start_experiment':
            if not experiment_id:
                experiment = (self.experiment_status() or {}).get('experiment') or {}
                experiment_id = experiment.get('experiment_id')
                if not experiment_id:
                    return SuccessFail.fail(error=f'{method}: there is no experiment')
            body['experiment_id'] = experiment_id
        try:
            receipt = self._typed_command.call(method, body, CommandReceipt)
        except TypedRpcRemoteError as ex:
            return SuccessFail.fail(error=f'{method} rejected: {ex.code}: {ex.message}', exception=ex)
        except (TimeoutError, ConnectionError) as ex:
            return SuccessFail.fail(error=f'{method} did not complete: {ex}. Check status before retrying.',
                                    exception=ex)
        if receipt.state == 'RESOLVED':
            return SuccessFail.success(obj=receipt.outcome)
        message = (receipt.outcome or {}).get('message') if isinstance(receipt.outcome, dict) else None
        detail = f'{receipt.error_code or receipt.state}' + (f': {message}' if message else '')
        return SuccessFail.fail(error=f'{method} rejected: {detail}')

    def ai_policy_publish(self, limits: dict, reason: str, command_id: Optional[str] = None) -> SuccessFail:
        """Publish the PAPER AI risk policy as the operator (SP2 spec 6.7).

        An identical retry with the same ``command_id`` replays from the ledger
        instead of publishing a second revision.
        """
        import uuid
        from trader.domain.commands import CommandReceipt
        from trader.messaging.typed_rpc import TypedRpcRemoteError

        command_id = command_id or f'cli-pol-{uuid.uuid4().hex}'
        body = {'command_id': command_id, 'limits': limits, 'reason': reason}
        try:
            receipt = self._typed_command.call('publish_ai_risk_policy', body, CommandReceipt)
        except TypedRpcRemoteError as ex:
            return SuccessFail.fail(error=f'{ex.code}: {ex.message}', exception=ex)
        except (TimeoutError, ConnectionError) as ex:
            return SuccessFail.fail(
                error=f'{ex}. Retry with --command-id {command_id} to replay the same publish.', exception=ex)
        if receipt.state == 'RESOLVED':
            return SuccessFail.success(obj=receipt.outcome)
        message = (receipt.outcome or {}).get('message') if isinstance(receipt.outcome, dict) else None
        detail = f'{receipt.error_code or receipt.state}' + (f': {message}' if message else '')
        return SuccessFail.fail(error=f'publish_ai_risk_policy rejected: {detail}')

    def ai_policy_view(self) -> dict:
        """The PAPER AI risk policy: published, effective and queued limits."""
        return self._typed_query.call('get_ai_risk_policy', {}, dict)

    def experiment_start(self, reason: str) -> SuccessFail:
        """Arm a new paper experiment (operators only; the account must be flat)."""
        return self._experiment_command('start_experiment', reason, None)

    def experiment_pause(self, reason: str, experiment_id: Optional[str] = None) -> SuccessFail:
        return self._experiment_command('pause_experiment', reason, experiment_id)

    def experiment_resume(self, reason: str, experiment_id: Optional[str] = None) -> SuccessFail:
        """Resume a PAUSED experiment. A KILLED experiment is never resumed."""
        return self._experiment_command('resume_experiment', reason, experiment_id)

    def experiment_stop(self, reason: str, experiment_id: Optional[str] = None) -> SuccessFail:
        """Stop the experiment once the account is flat. Final."""
        return self._experiment_command('stop_experiment', reason, experiment_id)

    def deactivate_live_canary(self, strategy_id: str, reason: str) -> SuccessFail:
        """Suspend an ACTIVE canary authority via ``deactivate_live_canary``.
        REQUIRES trader_service. Risk-reducing: no preflight nonce needed,
        mirroring ``reject``."""
        import uuid
        from trader.domain.commands import CommandReceipt
        from trader.messaging.typed_rpc import TypedRpcRemoteError

        try:
            receipt = self._typed_command.call(
                'deactivate_live_canary',
                {'command_id': f'sdk-{uuid.uuid4()}', 'strategy_id': strategy_id, 'reason': reason},
                CommandReceipt,
            )
        except TypedRpcRemoteError as ex:
            return SuccessFail.fail(error=f'canary deactivation rejected: {ex.code}: {ex.message}', exception=ex)
        except (TimeoutError, ConnectionError) as ex:
            return SuccessFail.fail(error=f'deactivate_live_canary did not complete: {ex}', exception=ex)

        if receipt.state == 'RESOLVED':
            return SuccessFail.success(obj=receipt.outcome)
        return SuccessFail.fail(error=f'canary deactivation rejected: {receipt.error_code or receipt.state}')

    # ------------------------------------------------------------------
    # Protective orders for existing positions
    # ------------------------------------------------------------------

    def place_protective_order(
        self,
        symbol: Union[str, int],
        action: str,
        quantity: float,
        order_type: str,
        aux_price: float = 0.0,
        limit_price: float = 0.0,
        trailing_percent: float = 0.0,
        tif: str = 'GTC',
        sec_type: str = 'STK',
        exchange: str = '',
        currency: str = '',
        con_id: Optional[int] = None,
    ) -> SuccessFail:
        """Place a standalone protective order (STP / TRAIL / LMT) for an existing
        position. Resolves the contract (preferring a cached one from the last
        portfolio() call, else exchange/currency-hinted resolution) and submits
        via the trader_service. order_type: 'STP', 'TRAIL', or 'LMT'."""
        order_type = (order_type or '').upper()
        if order_type not in ('STP', 'TRAIL', 'LMT'):
            return SuccessFail.fail(error=f"order_type must be STP/TRAIL/LMT, got {order_type!r}")
        if quantity is None or quantity <= 0:
            return SuccessFail.fail(error='quantity must be positive')

        contract = self._contract_map.get(symbol)
        if contract is None:
            try:
                contract = self._resolve_contract(
                    con_id or symbol, sec_type=sec_type, exchange=exchange, currency=currency)
            except Exception as ex:
                return SuccessFail.fail(error=f"Could not resolve symbol {symbol}: {ex}")

        return consume(
            self._legacy_or_raise('place standalone order').rpc(
                return_type=SuccessFail[Trade]
            ).place_standalone_order(
                contract=contract,
                action=action.upper(),
                quantity=quantity,
                order_type=order_type,
                aux_price=aux_price,
                limit_price=limit_price,
                trailing_percent=trailing_percent,
                tif=tif,
            )
        )

    # ------------------------------------------------------------------
    # Position closing
    # ------------------------------------------------------------------

    def close_position(self, symbol: str, quantity: Optional[float] = None,
                       con_id: Optional[int] = None,
                       skip_risk_gate: bool = False,
                       _pos_size: Optional[float] = None) -> SuccessFail:
        """Close (or reduce) a position.

        If *quantity* is None, sells the entire position at market.
        Uses the cached contract from the last portfolio() call when available,
        avoiding re-resolution (which fails for international stocks not in the
        local universe).
        If *skip_risk_gate* is True, bypasses risk gate checks (used by close-all).
        If *_pos_size* is provided, skip the portfolio lookup (caller already has it).
        """
        if _pos_size is not None:
            pos_size = _pos_size
        else:
            portfolio_df = self.portfolio()
            if portfolio_df.empty:
                return SuccessFail.fail(error=f"No positions found")

            match = portfolio_df[portfolio_df['symbol'] == symbol]
            if match.empty:
                return SuccessFail.fail(error=f"No position for {symbol}")

            pos_size = float(match.iloc[0]['position'])

        if pos_size == 0:
            return SuccessFail.fail(error=f"Position for {symbol} is zero")

        close_qty = abs(quantity) if quantity is not None else abs(pos_size)
        # Clamp: closing more than we hold would flip the position into an
        # equal-and-opposite one at market instead of flattening it.
        if close_qty > abs(pos_size):
            logging.warning(
                'close_position(%s): requested %s exceeds position %s; clamping to '
                'position size to avoid a flip', symbol, close_qty, abs(pos_size))
            close_qty = abs(pos_size)
        action = 'SELL' if pos_size > 0 else 'BUY'

        # Prefer cached contract from portfolio (works for all exchanges).
        # Fall back to resolve only if no cached contract exists.
        contract = self._contract_map.get(symbol)
        if contract is None:
            try:
                contract = self._resolve_contract(con_id or symbol)
            except ValueError:
                return SuccessFail.fail(error=f"Could not resolve symbol: {symbol}")
        return consume(
            self._legacy_or_raise('place order').rpc(
                return_type=SuccessFail[Trade]
            ).place_order_simple(
                contract=contract,
                action=action,
                equity_amount=None,
                quantity=close_qty,
                limit_price=None,
                market_order=True,
                stop_loss_percentage=0.0,
                debug=False,
                skip_risk_gate=skip_risk_gate,
            )
        )

    # ------------------------------------------------------------------
    # Portfolio Resizing
    # ------------------------------------------------------------------

    def compute_resize_plan(
        self,
        max_bound: Optional[float] = None,
        min_bound: Optional[float] = None,
    ) -> dict:
        """Compute a resize plan for the portfolio.

        Returns a dict with scale_factor, current_total, target_total,
        and adjustments (each with associated_orders info).
        """
        portfolio_df = self.portfolio()
        if portfolio_df.empty:
            return {'scale_factor': 1.0, 'current_total': 0, 'target_total': 0, 'adjustments': []}

        positions = portfolio_df.to_dict('records')

        # Normalise each position's marketValue (reported in the instrument's own
        # currency) to the account base currency before summing against the
        # user-supplied bounds (which are in base). Without this, an AUD/USD
        # position in a CAD account is scaled by the wrong total and the plan
        # over/under-shoots the bound by the FX factor.
        fx_rates = self._fx_rates()
        for p in positions:
            p['marketValueBase'] = self._to_base(
                float(p.get('marketValue', 0) or 0), p.get('currency', ''), fx_rates)
        total_value = sum(abs(p.get('marketValueBase', 0) or 0) for p in positions)

        scale_factor, adjustments = compute_resize_deltas(positions, max_bound, min_bound)

        if scale_factor == 1.0:
            return {
                'scale_factor': 1.0,
                'current_total': total_value,
                'target_total': total_value,
                'adjustments': [],
            }

        target_total = total_value * scale_factor

        # Find associated protective orders for each position (typed open orders).
        open_orders: list = []
        try:
            open_orders = (
                self._typed_query.call('get_open_orders', {}, dict).get('orders') or []
            )
        except Exception:
            pass

        for adj in adjustments:
            associated = []
            con_id = adj['conId']
            pos_direction = 'LONG' if adj['current_qty'] > 0 else 'SHORT'
            for o in open_orders:
                t_con_id = int(o.get('instrument_id') or 0)
                if t_con_id != con_id:
                    continue
                order_type = o.get('order_type') or ''
                order_action = o.get('action') or ''

                # Protective orders are opposite direction to position
                is_protective = (
                    (pos_direction == 'LONG' and order_action == 'SELL') or
                    (pos_direction == 'SHORT' and order_action == 'BUY')
                )
                is_stop_type = order_type in ('STP', 'STP LMT', 'TRAIL')
                is_tp = order_type == 'LMT' and int(o.get('parent_id') or 0) > 0

                if is_protective and (is_stop_type or is_tp):
                    aux = o.get('aux_price') or 0
                    lmt = o.get('limit_price') or 0
                    associated.append({
                        'orderId': o.get('order_id'),
                        'orderType': order_type,
                        'action': order_action,
                        'quantity': float(o.get('quantity') or 0),
                        'auxPrice': float(aux),
                        'lmtPrice': float(lmt),
                        'trailingPercent': 0.0,
                        'tif': o.get('tif') or 'GTC',
                    })

            adj['associated_orders'] = associated

        return {
            'scale_factor': scale_factor,
            'current_total': total_value,
            'target_total': target_total,
            'adjustments': adjustments,
        }

    def execute_resize_plan(self, plan: dict) -> dict:
        """Execute a resize plan: cancel protective orders, place deltas, re-create protectives.

        Returns a summary dict with successes, failures, and warnings.
        """
        results = {
            'successes': [],
            'failures': [],
            'warnings': [],
        }

        for adj in plan.get('adjustments', []):
            symbol = adj['symbol']
            delta_qty = adj['delta_qty']
            action = adj['action']
            target_qty = adj['target_qty']
            associated = adj.get('associated_orders', [])

            # 1. Place the delta market order FIRST, while the existing protective
            #    orders are still live. If the delta fails (risk gate, timeout, IB
            #    reject) we simply skip this symbol: the position is unchanged and
            #    its original protectives still match it exactly. This is the fix
            #    for the naked-position hole — the old ordering cancelled stops
            #    first and then `continue`d past a delta failure, leaving the full
            #    position with no stop-loss.
            try:
                cached_contract = self._contract_map.get(symbol)
                if cached_contract:
                    order_result = consume(
                        self._legacy_or_raise('place order').rpc(
                            return_type=SuccessFail[Trade]
                        ).place_order_simple(
                            contract=cached_contract,
                            action=action,
                            equity_amount=None,
                            quantity=abs(delta_qty),
                            limit_price=None,
                            market_order=True,
                            stop_loss_percentage=0.0,
                            debug=False,
                        )
                    )
                else:
                    order_result = self._place_order(
                        symbol=symbol,
                        action=action,
                        quantity=abs(delta_qty),
                        market=True,
                    )
                if order_result.is_success():
                    results['successes'].append(
                        f'{symbol}: {action} {abs(delta_qty)} shares'
                    )
                else:
                    results['failures'].append(
                        f'{symbol}: {action} {abs(delta_qty)} failed — {order_result.error} '
                        f'(protective orders left untouched; position still protected)'
                    )
                    continue
            except TimeoutError as ex:
                # Ambiguous: the trim may have executed. Do NOT touch protectives —
                # if it filled they're now slightly oversized (still protective),
                # if it didn't they still match. Flag for manual reconciliation.
                results['failures'].append(
                    f'{symbol}: {action} {abs(delta_qty)} TIMED OUT — status unknown, '
                    f'protectives left in place; reconcile manually — {ex}'
                )
                continue
            except Exception as ex:
                results['failures'].append(
                    f'{symbol}: {action} {abs(delta_qty)} error — {ex} '
                    f'(protective orders left untouched)'
                )
                continue

            # 2. The delta filled: now bring each protective to the new quantity,
            #    one at a time — cancel, confirm, then re-create. If a cancel does
            #    NOT confirm we must not place a second protective (that would
            #    double-cover the position and can flip it on a trigger), so we
            #    leave the old one and flag it. If the re-create fails after a
            #    confirmed cancel the position is momentarily unprotected — that
            #    is surfaced as a loud, explicit warning for manual action.
            contract = self._contract_map.get(symbol)
            if contract is None:
                try:
                    contract = self._resolve_contract(symbol)
                except Exception as ex:
                    results['warnings'].append(
                        f'{symbol}: RESIZED but could not resolve contract to reset protectives '
                        f'({ex}); old protectives remain at old quantity — manual review needed'
                    )
                    continue
            new_qty = abs(target_qty)

            for order_info in associated:
                oid = order_info['orderId']
                otype = order_info['orderType']
                try:
                    cancel_result = self.cancel(oid)
                    cancelled = cancel_result.is_success()
                except Exception as ex:
                    cancelled = False
                    cancel_result = None
                    results['warnings'].append(f'{symbol}: error cancelling {otype} #{oid}: {ex}')

                if not cancelled:
                    err = getattr(cancel_result, 'error', 'unknown') if cancel_result else 'exception'
                    results['warnings'].append(
                        f'{symbol}: could not confirm cancel of {otype} #{oid} ({err}); '
                        f'leaving it at old quantity to avoid double protection — manual review needed'
                    )
                    continue

                try:
                    place_result = consume(
                        self._legacy_or_raise('place standalone order').rpc(
                            return_type=SuccessFail[Trade]
                        ).place_standalone_order(
                            contract=contract,
                            action=order_info['action'],
                            quantity=new_qty,
                            order_type=otype,
                            aux_price=order_info['auxPrice'],
                            limit_price=order_info['lmtPrice'],
                            trailing_percent=order_info['trailingPercent'],
                            tif=order_info['tif'],
                        )
                    )
                    if getattr(place_result, 'is_success', lambda: True)():
                        results['successes'].append(
                            f'{symbol}: re-created {otype} {order_info["action"]} {new_qty}'
                        )
                    else:
                        results['warnings'].append(
                            f'{symbol}: CANCELLED {otype} #{oid} but re-create FAILED '
                            f'({getattr(place_result, "error", "?")}) — POSITION UNPROTECTED, act now'
                        )
                except Exception as ex:
                    results['warnings'].append(
                        f'{symbol}: CANCELLED {otype} #{oid} but re-create raised ({ex}) — '
                        f'POSITION UNPROTECTED, act now'
                    )

        return results

    # ------------------------------------------------------------------
    # Market Data
    # ------------------------------------------------------------------

    @staticmethod
    def _reject_exchange_hints_for_rest_source(source: str, exchange: str, currency: str) -> None:
        if exchange or currency:
            raise ValueError(f"--exchange/--currency need --source ib; {source} covers US listings only")

    @staticmethod
    def _reject_conids_for_rest_source(source: str, symbols: list) -> None:
        if any(isinstance(symbol, int) or str(symbol).isdigit() for symbol in symbols):
            raise ValueError(f"conIds need --source ib; {source} takes ticker symbols")

    def snapshot(self, symbol: Union[str, int], delayed: bool = False,
                 exchange: str = '', currency: str = '',
                 source: str = 'ib') -> dict:
        """Get a price snapshot for *symbol*.

        Parameters
        ----------
        source : str
            'ib' (default) routes via trader_service / IB. Any other value is a
            registry quotes source (e.g. 'alpaca' — IEX prices, 'twelvedata' — no bid/ask).
        """
        if source != 'ib':
            self._reject_exchange_hints_for_rest_source(source, exchange, currency)
            self._reject_conids_for_rest_source(source, [symbol])
            from trader.data_providers import Capability
            quote = self._provider(Capability.QUOTES, source).quotes([str(symbol)])[0]
            if quote['error']:
                raise ValueError(quote['error'])
            return _quote_to_snapshot(quote)
        contract = self._resolve_contract(symbol, exchange=exchange, currency=currency)
        response = self._typed_query.call(
            'get_snapshot',
            {'instrument_id': int(contract.conId), 'delayed': bool(delayed)},
            dict,
        )
        snap = response.get('snapshot') or {}
        def _nan(v):
            return float('nan') if v is None else v
        return {
            'symbol': snap.get('symbol') or contract.symbol,
            'conId': snap.get('instrument_id') or contract.conId,
            'time': snap.get('time'),
            'bid': _nan(snap.get('bid')),
            'bidSize': _nan(snap.get('bid_size')),
            'ask': _nan(snap.get('ask')),
            'askSize': _nan(snap.get('ask_size')),
            'last': _nan(snap.get('last')),
            'lastSize': _nan(snap.get('last_size')),
            'open': _nan(snap.get('open')),
            'high': _nan(snap.get('high')),
            'low': _nan(snap.get('low')),
            'close': _nan(snap.get('close')),
            'halted': _nan(snap.get('halted')),
        }

    def snapshot_batch(self, symbols: list[str], exchange: str = '',
                       currency: str = '', source: str = 'ib') -> list[dict]:
        """Get price snapshots for multiple *symbols* in one batch.

        Parameters
        ----------
        source : str
            'ib' (default) routes via trader_service / IB. Any other value is a
            registry quotes source (e.g. 'alpaca' — IEX prices, 'twelvedata' — no bid/ask).
        """
        if source != 'ib':
            self._reject_exchange_hints_for_rest_source(source, exchange, currency)
            self._reject_conids_for_rest_source(source, symbols)
            from trader.data_providers import Capability
            return [_quote_to_batch_row(q) for q in self._provider(Capability.QUOTES, source).quotes(symbols)]

        ids = []
        for sym in symbols:
            contract = self._resolve_contract(sym, exchange=exchange, currency=currency)
            ids.append(int(contract.conId))
        if not ids:
            return []
        response = self._typed_query.call(
            'get_snapshots_batch',
            {'instrument_ids': ids, 'delayed': True},
            dict,
        )
        rows = []
        for snap in response.get('snapshots') or []:
            rows.append({
                'conId': snap.get('instrument_id'),
                'symbol': snap.get('symbol'),
                'exchange': snap.get('exchange'),
                'currency': snap.get('currency'),
                'bid': snap.get('bid'),
                'ask': snap.get('ask'),
                'last': snap.get('last'),
                'open': snap.get('open'),
                'high': snap.get('high'),
                'low': snap.get('low'),
                'close': snap.get('close'),
                'volume': snap.get('volume'),
            })
        return rows

    def depth(self, symbol: Union[str, int], num_rows: int = 5,
              exchange: str = '', currency: str = '',
              is_smart_depth: bool = False) -> dict:
        """Get Level 2 market depth (order book) for *symbol*."""
        contract = self._resolve_contract(symbol, exchange=exchange, currency=currency)
        response = self._typed_query.call(
            'get_market_depth',
            {
                'instrument_id': int(contract.conId),
                'num_rows': int(num_rows),
                'is_smart_depth': bool(is_smart_depth),
            },
            dict,
        )
        return response.get('depth') or {'bids': [], 'asks': []}

    def subscribe_ticks(
        self,
        symbol: Union[str, int],
        callback: Callable[[Any], None],
        topic: str = 'ticker',
        delayed: bool = False,
        exchange: str = '',
        currency: str = '',
    ) -> Subscription:
        """Subscribe to live tick data for *symbol*.

        *callback* fires on a background thread each time a tick arrives.
        Call ``subscription.stop()`` to unsubscribe.
        """
        contract = self._resolve_contract(symbol, exchange=exchange, currency=currency)
        # Tell trader_service to publish ticks for this contract (typed query)
        self._typed_query.call(
            'publish_instrument',
            {'instrument_id': int(contract.conId), 'delayed': bool(delayed)},
            dict,
        )

        sub = Subscription()

        def _listener():
            ctx = zmq.Context()
            socket = ctx.socket(zmq.SUB)
            socket.connect(f'{self._pubsub_address}:{self._pubsub_port}')
            socket.setsockopt_string(zmq.SUBSCRIBE, topic)
            socket.setsockopt(zmq.RCVTIMEO, 1000)  # 1s poll

            try:
                while not sub._stop_event.is_set():
                    try:
                        frames = socket.recv_multipart()
                        if len(frames) >= 2:
                            obj = unpack(frames[1])
                            callback(obj)
                    except zmq.Again:
                        continue
                    except zmq.ZMQError:
                        break
            finally:
                socket.close()
                ctx.term()

        sub._thread = threading.Thread(target=_listener, daemon=True)
        sub._thread.start()
        self._subscriptions.append(sub)
        return sub

    # ------------------------------------------------------------------
    # Strategies
    # ------------------------------------------------------------------

    def strategies(self) -> pd.DataFrame:
        """List configured strategies via strategy typed query (42105)."""
        response = self._strategy_typed_query.call('list_strategies', {}, dict)
        rows = []
        for s in response.get('strategies') or []:
            state_name = str(s.get('state') or '')
            row = {
                'name': s.get('name'),
                'state': state_name,
                'dispatchable': is_dispatchable_strategy_state(state_name),
                'bar_size': str(s.get('bar_size') or ''),
                'conids': s.get('conids') or [],
                'hist_days_prior': s.get('historical_days_prior'),
                'auto_execute': s.get('auto_execute', False),
                'class_name': s.get('class_name') or '',
                'description': s.get('description') or '',
            }
            params = s.get('params')
            if params:
                row['params'] = params
            rows.append(row)
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    def enable_strategy(self, name: str) -> SuccessFail:
        """Enable a strategy by name (strategy typed command)."""
        try:
            response = self._strategy_typed_command.call(
                'enable_strategy_by_name', {'strategy_name': name}, dict,
            )
            if response.get('ok'):
                return SuccessFail.success(response)
            return SuccessFail.fail(error=response.get('error') or f'enable failed: {name}')
        except Exception as exc:
            return SuccessFail.fail(error=str(exc))

    def disable_strategy(self, name: str) -> SuccessFail:
        """Disable a strategy by name (strategy typed command)."""
        try:
            response = self._strategy_typed_command.call(
                'disable_strategy_by_name', {'strategy_name': name}, dict,
            )
            if response.get('ok'):
                return SuccessFail.success(response)
            return SuccessFail.fail(error=response.get('error') or f'disable failed: {name}')
        except Exception as exc:
            return SuccessFail.fail(error=str(exc))

    def update_strategy_params(self, name: str, params: dict) -> SuccessFail:
        """Update a strategy's params — requires command-center ceremony in production.

        Direct dill RPC is unbound; use the dashboard command center
        ``update_strategy_params`` flow (command_id + control revision).
        """
        try:
            return consume(
                self._legacy_or_raise('update_strategy_params').rpc().update_strategy_params(
                    name, params,
                )
            )
        except Exception as exc:
            self._map_legacy_route_error('update_strategy_params', exc)

    def reload_strategies(self) -> SuccessFail:
        """Reload strategies from YAML config and re-subscribe to instruments."""
        try:
            response = self._strategy_typed_command.call('reload_strategies', {}, dict)
            if response.get('ok'):
                return SuccessFail.success(response)
            return SuccessFail.fail(error=response.get('error') or 'reload failed')
        except Exception as exc:
            return SuccessFail.fail(error=str(exc))

    def check_ib_upstream(self) -> Optional[str]:
        """Check if IB Gateway has upstream connectivity. Returns error string or None if OK."""
        try:
            svc_status = self._typed_query.call('get_status', {}, dict)
            if not svc_status.get('ib_upstream_connected', True):
                return svc_status.get('ib_upstream_error', 'IB Gateway is not connected to IBKR servers')
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Historical Data (via data_service RPC)
    # ------------------------------------------------------------------

    def pull_history(
        self,
        source: str,
        symbols: Optional[List[str]] = None,
        universe: Optional[str] = None,
        bar_size: str = '1 day',
        prev_days: int = 30,
    ) -> dict:
        """Download historical data from any registry history source via the data_service."""
        return consume(
            self._data_rpc.rpc(return_type=dict).pull_history(
                source=source, symbols=symbols, universe=universe, bar_size=bar_size, prev_days=prev_days,
            )
        )

    def pull_massive(
        self,
        symbols: Optional[List[str]] = None,
        universe: Optional[str] = None,
        bar_size: str = '1 day',
        prev_days: int = 30,
    ) -> dict:
        """Download historical data from Massive.com via the data_service."""
        return consume(
            self._data_rpc.rpc(return_type=dict).pull_massive(
                symbols=symbols, universe=universe, bar_size=bar_size, prev_days=prev_days,
            )
        )

    def pull_twelvedata(
        self,
        symbols: Optional[List[str]] = None,
        universe: Optional[str] = None,
        bar_size: str = '1 day',
        prev_days: int = 30,
    ) -> dict:
        """Download historical data from TwelveData via the data_service."""
        return consume(
            self._data_rpc.rpc(return_type=dict).pull_twelvedata(
                symbols=symbols, universe=universe, bar_size=bar_size, prev_days=prev_days,
            )
        )

    def pull_ib(
        self,
        symbols: Optional[List[str]] = None,
        universe: Optional[str] = None,
        bar_size: str = '1 min',
        prev_days: int = 5,
        ib_client_id: int = 10,
    ) -> dict:
        """Download historical data from IB via the data_service."""
        return consume(
            self._data_rpc.rpc(return_type=dict).pull_ib(
                symbols=symbols, universe=universe, bar_size=bar_size,
                prev_days=prev_days, ib_client_id=ib_client_id,
            )
        )

    def data_service_status(self) -> dict:
        """Check data_service status (running jobs, etc.)."""
        return consume(self._data_rpc.rpc(return_type=dict).status())

    def history_list(self, symbol: Optional[str] = None, bar_size: Optional[str] = None) -> pd.DataFrame:
        """List downloaded history in DuckDB, grouped by symbol and bar_size.

        Returns a DataFrame with columns: symbol, name, bar_size, start, end, rows.
        Resolves conIds to ticker names via local universes (no service needed).
        """
        from trader.data.duckdb_store import DuckDBDataStore
        from trader.data.universe import UniverseAccessor

        cfg = self._container.config()
        db_path = cfg.get('duckdb_path', '')
        store = DuckDBDataStore(db_path)

        # Build the query with optional filters
        conditions = []
        params: list = []
        if symbol:
            # Could be a ticker name or conId — we'll filter after resolving
            pass
        if bar_size:
            conditions.append("bar_size = ?")
            params.append(bar_size)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        def _query(conn):
            result = conn.execute(f"""
                SELECT symbol, bar_size, MIN(date) as start, MAX(date) as end, COUNT(*) as rows
                FROM {store.TABLE_NAME}
                {where}
                GROUP BY symbol, bar_size
                ORDER BY symbol, bar_size
            """, params or None)
            return result.fetchdf()

        df = store._db.execute_atomic(_query)
        if df.empty:
            return pd.DataFrame(columns=['symbol', 'name', 'bar_size', 'start', 'end', 'rows'])

        # Build a conId → ticker name mapping from universes
        universe_lib = cfg.get('universe_library', 'Universes')
        accessor = UniverseAccessor(db_path, universe_lib)
        conid_map: Dict[str, str] = {}
        try:
            for u in accessor.get_all():
                for d in u.security_definitions:
                    conid_map[str(d.conId)] = d.symbol
        except Exception:
            pass

        df['name'] = df['symbol'].map(lambda s: conid_map.get(s, ''))

        # If filtering by ticker name, apply now
        if symbol:
            # Try matching by conId string or by resolved name
            mask = (df['symbol'] == symbol) | (df['name'].str.upper() == symbol.upper())
            df = df[mask]

        # Reorder columns
        df = df[['symbol', 'name', 'bar_size', 'start', 'end', 'rows']]
        return df.reset_index(drop=True)

    # ------------------------------------------------------------------
    # Account / Status
    # ------------------------------------------------------------------

    def account(self) -> str:
        """Return the IB account ID."""
        response = self._typed_query.call('get_ib_account', {}, dict)
        return str(response.get('account_id') or '')

    def account_cash(self) -> dict:
        """Per-currency cash balances for the configured account.

        Returns ``{account, base_currency, currencies: {CUR: {cash,
        exchange_rate, base_value}}, total_base_value}``. Scoped to the
        configured ``ib_account`` — a multi-account login won't leak another
        account's cash.
        """
        return self._typed_query.call('get_account_cash_by_currency', {}, dict) or {}

    def get_risk_limits(self) -> dict:
        """Get current risk gate limits from trader_service."""
        return self._typed_query.call('get_risk_limits', {}, dict) or {}

    def set_risk_limits(self, **kwargs) -> dict:
        """Update risk gate limits — not on the typed production surface."""
        try:
            return consume(
                self._legacy_or_raise('set_risk_limits').rpc(
                    return_type=dict
                ).set_risk_limits(**kwargs)
            )
        except Exception as exc:
            self._map_legacy_route_error('set_risk_limits', exc)

    # ------------------------------------------------------------------
    # Trading filters (local YAML, no RPC needed)
    # ------------------------------------------------------------------

    def get_filters(self) -> dict:
        """Load current trading filters from YAML config."""
        from trader.trading.trading_filter import TradingFilter
        return TradingFilter.load().to_dict()

    def set_filters(self, **kwargs) -> dict:
        """Update specific filter fields and save."""
        from trader.trading.trading_filter import TradingFilter
        tf = TradingFilter.load()
        for key, value in kwargs.items():
            if hasattr(tf, key):
                setattr(tf, key, value)
        tf.save()
        return tf.to_dict()

    def add_to_filter_list(self, list_name: str, symbols: list[str]) -> dict:
        """Add symbols to a named list (denylist, allowlist, etc.)."""
        from trader.trading.trading_filter import TradingFilter
        tf = TradingFilter.load()
        current = getattr(tf, list_name, [])
        for sym in symbols:
            s = sym.upper()
            if s not in current:
                current.append(s)
        setattr(tf, list_name, current)
        tf.save()
        return tf.to_dict()

    def remove_from_filter_list(self, list_name: str, symbols: list[str]) -> dict:
        """Remove symbols from a named list."""
        from trader.trading.trading_filter import TradingFilter
        tf = TradingFilter.load()
        current = getattr(tf, list_name, [])
        upper_syms = {s.upper() for s in symbols}
        setattr(tf, list_name, [s for s in current if s.upper() not in upper_syms])
        tf.save()
        return tf.to_dict()

    def reset_filters(self) -> dict:
        """Reset all filters to defaults."""
        from trader.trading.trading_filter import TradingFilter
        tf = TradingFilter.default()
        tf.save()
        return tf.to_dict()

    def status(self) -> dict:
        """Check connectivity to trader_service via ZMQ RPC with diagnostics.

        Returns raw structured values — no Rich markup, no pre-formatted
        currency strings. The CLI's status command formats these for
        human display; programmatic consumers (and `--json` output)
        get clean numbers.

        Schema:
            connected: bool
            account: str (account number) — only when connected
            ib_upstream_connected: bool
            ib_upstream_error: str (only when ib_upstream_connected is False)
            account_values: dict[str, dict] — for each of NetLiquidation,
                TotalCashValue, AvailableFunds, BuyingPower:
                    {value: float, currency: str}
                (omitted when account values aren\'t streaming)
            account_values_warning: str (only when values can\'t be fetched)
            leverage: float (gross_position / net_liquidation), nullable
            margin_used: dict {init_margin: float, net_liq: float, currency: str}, nullable
            cushion: float, nullable
            positions: int (count of non-zero positions)
            pnl: dict {daily, unrealized, realized, total} as floats
                (in account base currency; same as NetLiquidation\'s)
            open_orders: int
            streaming: int (count of contracts being streamed)
            pending_proposals: int
        """
        try:
            acct = self.account()
            if not acct:
                return {'connected': False}
        except Exception:
            return {'connected': False}

        result: dict = {'connected': True, 'account': acct}

        try:
            svc_status = self._typed_query.call('get_status', {}, dict)
            result['ib_upstream_connected'] = svc_status.get('ib_upstream_connected', True)
            if not result['ib_upstream_connected']:
                result['ib_upstream_error'] = svc_status.get('ib_upstream_error', 'unknown')
        except Exception:
            pass

        try:
            acct_vals = self._account_values()
            if acct_vals and 'NetLiquidation' in acct_vals:
                # Raw structured account values (value + currency separated).
                # Lets consumers do math; CLI formats for display.
                account_values: dict[str, dict] = {}
                for key in ('NetLiquidation', 'TotalCashValue', 'AvailableFunds', 'BuyingPower'):
                    entry = acct_vals.get(key)
                    if entry:
                        account_values[key] = {
                            'value': float(entry['value']),
                            'currency': entry['currency'],
                        }
                if account_values:
                    result['account_values'] = account_values

                # Leverage / margin info
                net_liq_entry = acct_vals.get('NetLiquidation')
                gross_pos_entry = acct_vals.get('GrossPositionValue')
                init_margin_entry = acct_vals.get('InitMarginReq')
                cushion_entry = acct_vals.get('Cushion')

                if net_liq_entry and gross_pos_entry:
                    net_liq = float(net_liq_entry['value'])
                    gross_pos = float(gross_pos_entry['value'])
                    if net_liq > 0:
                        result['leverage'] = round(gross_pos / net_liq, 4)

                if init_margin_entry and net_liq_entry:
                    init_margin = float(init_margin_entry['value'])
                    net_liq = float(net_liq_entry['value'])
                    result['margin_used'] = {
                        'init_margin': init_margin,
                        'net_liq': net_liq,
                        'currency': init_margin_entry.get('currency', net_liq_entry.get('currency', '')),
                    }

                if cushion_entry:
                    try:
                        result['cushion'] = float(cushion_entry['value'])
                    except (TypeError, ValueError):
                        result['cushion'] = None
            else:
                # acct_vals is empty / missing NetLiquidation. This is the
                # classic IB-Gateway-demo-account symptom (DUM* accounts don't
                # stream account values), or the gateway hasn't authenticated
                # yet. Surface it instead of silently producing a status with
                # no balance fields.
                hint = (
                    f"No account values streaming for {acct} "
                    "(IB Gateway demo account / not yet authenticated / "
                    "real account login required). "
                    "VNC into ib-gateway at vnc://localhost:5901 and re-enter "
                    "your real IBKR paper or live credentials, then update "
                    "ib_paper_account in trader.yaml."
                )
                result['account_values_warning'] = hint
                logger.warning(hint)
        except Exception as exc:
            # Don't swallow silently — emit at debug so production logs aren't
            # spammed but devs can `--debug` to see the underlying failure.
            logger.debug("status(): get_account_values RPC failed", exc_info=exc)
            result['account_values_warning'] = (
                f"Failed to fetch account values: {type(exc).__name__} "
                "(see server logs)"
            )

        try:
            portfolio_df = self.portfolio()
            positions = []
            if portfolio_df is not None and not portfolio_df.empty:
                for _, row in portfolio_df.iterrows():
                    position = float(row.get('position') or 0.0)
                    if abs(position) > 0:
                        positions.append((
                            row.get('unrealizedPNL'),
                            row.get('realizedPNL'),
                            row.get('dailyPNL'),
                        ))

            result['positions'] = len(positions)

            # Raw P&L floats. NaN-guard so json serializers don't emit
            # non-conformant `NaN` (which the agent saw as "[red]-$nan[/red]"
            # bleed-through under the old Rich-formatted path).
            def _safe(val):
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    return None
                return None if val != val else val  # NaN check

            unrealized = sum(_safe(u) or 0.0 for u, _, _ in positions)
            realized = sum(_safe(r) or 0.0 for _, r, _ in positions)
            daily = sum(_safe(d) or 0.0 for _, _, d in positions)

            result['pnl'] = {
                'daily': daily,
                'unrealized': unrealized,
                'realized': realized,
                'total': unrealized + realized,
            }
        except Exception:
            result['positions'] = None

        try:
            orders_resp = self._typed_query.call('get_open_orders', {}, dict)
            result['open_orders'] = len(orders_resp.get('orders') or [])
        except Exception:
            result['open_orders'] = None

        try:
            pubs = self._typed_query.call('get_published_contracts', {}, dict)
            result['streaming'] = len(pubs.get('instrument_ids') or [])
        except Exception:
            pass

        try:
            store = self._proposal_store()
            pending = store.query(status='PENDING')
            result['pending_proposals'] = len(pending)
        except Exception:
            pass

        return result

    # ------------------------------------------------------------------
    # Financial Statements (via Massive.com REST API)
    # ------------------------------------------------------------------

    def _provider(self, capability, source: Optional[str] = None):
        from trader.data_providers import ProviderRegistry
        return ProviderRegistry.from_config(self._container.config()).get(capability, source)

    def _provider_default(self, capability) -> str:
        from trader.data_providers import ProviderRegistry
        return ProviderRegistry.from_config(self._container.config()).default_source(capability)

    @property
    def _massive_client(self):
        """Lazy-init Massive.com REST client."""
        if self._massive_rest_client is None:
            from massive import RESTClient
            cfg = self._container.config()
            api_key = cfg.get('massive_api_key', '')
            if not api_key:
                raise ValueError("massive_api_key not configured in trader.yaml")
            self._massive_rest_client = RESTClient(api_key=api_key)
        return self._massive_rest_client

    @property
    def _twelvedata_client(self):
        """Lazy-init TwelveData REST client.

        Reads the api key from the Container config. The Container already
        applies the TWELVEDATA_API_KEY env-var fallback when it loads the
        config — we deliberately don't re-read the env var here, so the DI
        contract stays one-way (all config flows through Container).
        """
        if self._twelvedata_rest_client is None:
            from twelvedata import TDClient
            cfg = self._container.config()
            api_key = cfg.get('twelvedata_api_key', '')
            if not api_key:
                raise ValueError(
                    "twelvedata_api_key not configured (set in trader.yaml "
                    "or the TWELVEDATA_API_KEY env var)"
                )
            self._twelvedata_rest_client = TDClient(apikey=api_key)
        return self._twelvedata_rest_client

    @staticmethod
    def _flatten_td_dict(d: Any, prefix: str = '', sep: str = '.') -> Dict[str, Any]:
        """Flatten a TwelveData nested dict response to single-level columns.

        TwelveData fundamentals (balance_sheet / income_statement / cash_flow
        / statistics) return deeply-nested structures like
        ``{"operating_activities": {"net_income": ..., "depreciation": ...}}``.
        To render them as a clean one-row-per-period DataFrame, we dot-join
        nested keys: ``operating_activities.net_income``. The passthrough
        preserves all fields without inventing a cross-provider schema.
        """
        flat: Dict[str, Any] = {}
        for k, v in d.items():
            full_key = f'{prefix}{sep}{k}' if prefix else k
            if isinstance(v, dict):
                flat.update(MMR._flatten_td_dict(v, full_key, sep))
            else:
                flat[full_key] = v
        return flat

    def _td_fundamentals_to_df(self, payload: Dict[str, Any], list_key: str) -> pd.DataFrame:
        """Convert a TwelveData fundamentals payload into a DataFrame.

        Each entry in the ``<list_key>`` array (e.g. "balance_sheet") becomes
        one row; nested group dicts are flattened with dot-notation column
        names. Newest period first.
        """
        if not payload or list_key not in payload:
            return pd.DataFrame()
        entries = payload.get(list_key) or []
        rows = [self._flatten_td_dict(entry) for entry in entries if isinstance(entry, dict)]
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df = df.dropna(axis=1, how='all')
        if 'fiscal_date' in df.columns:
            df = df.sort_values('fiscal_date', ascending=False).reset_index(drop=True)
        return df

    def _financials_to_df(self, results, limit: int = 0) -> pd.DataFrame:
        """Convert an iterator of financial dataclass objects to a DataFrame."""
        rows = []
        for item in results:
            row = dataclasses.asdict(item)
            rows.append(row)
            if limit and len(rows) >= limit:
                break
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        # Drop columns that are all None
        df = df.dropna(axis=1, how='all')
        # Sort by period descending if available
        if 'period_end' in df.columns:
            df = df.sort_values('period_end', ascending=False).reset_index(drop=True)
        return df

    # Shape note: Massive and TwelveData return fundamentally different JSON
    # shapes. We pass through each source's native schema rather than merging
    # into a synthetic common schema — over-normalising always loses fields
    # at the margins. Callers inspect df.columns to see what's available.
    def balance_sheet(
        self,
        symbol: str,
        limit: int = 4,
        timeframe: str = 'quarterly',
        source: str = 'massive',
    ) -> pd.DataFrame:
        """Get balance sheet data.

        Parameters
        ----------
        symbol : str
            Stock ticker (e.g. "AAPL").
        limit : int
            Number of periods to return (default 4).
        timeframe : str
            "quarterly" or "annual" (default "quarterly").
        source : str
            "massive" (default) or "twelvedata". TwelveData requires a
            Pro-tier or above api key for fundamentals.
        """
        if source == 'twelvedata':
            period = 'annual' if timeframe == 'annual' else 'quarterly'
            payload = self._twelvedata_client.get_balance_sheet(
                symbol=symbol, period=period,
            ).as_json()
            df = self._td_fundamentals_to_df(payload, 'balance_sheet')
            return df.head(limit) if limit and not df.empty else df
        results = self._massive_client.list_financials_balance_sheets(
            tickers=symbol, timeframe=timeframe, limit=limit,
        )
        return self._financials_to_df(results, limit=limit)

    def income_statement(
        self,
        symbol: str,
        limit: int = 4,
        timeframe: str = 'quarterly',
        source: str = 'massive',
    ) -> pd.DataFrame:
        """Get income statement data. See balance_sheet for args."""
        if source == 'twelvedata':
            period = 'annual' if timeframe == 'annual' else 'quarterly'
            payload = self._twelvedata_client.get_income_statement(
                symbol=symbol, period=period,
            ).as_json()
            df = self._td_fundamentals_to_df(payload, 'income_statement')
            return df.head(limit) if limit and not df.empty else df
        results = self._massive_client.list_financials_income_statements(
            tickers=symbol, timeframe=timeframe, limit=limit,
        )
        return self._financials_to_df(results, limit=limit)

    def cash_flow(
        self,
        symbol: str,
        limit: int = 4,
        timeframe: str = 'quarterly',
        source: str = 'massive',
    ) -> pd.DataFrame:
        """Get cash flow statement data. See balance_sheet for args."""
        if source == 'twelvedata':
            period = 'annual' if timeframe == 'annual' else 'quarterly'
            payload = self._twelvedata_client.get_cash_flow(
                symbol=symbol, period=period,
            ).as_json()
            df = self._td_fundamentals_to_df(payload, 'cash_flow')
            return df.head(limit) if limit and not df.empty else df
        results = self._massive_client.list_financials_cash_flow_statements(
            tickers=symbol, timeframe=timeframe, limit=limit,
        )
        return self._financials_to_df(results, limit=limit)

    def ratios(self, symbol: str, source: str = 'massive') -> pd.DataFrame:
        """Get financial ratios / key statistics.

        Massive returns Polygon-style ratios (PE, D/E, ROE, ...). TwelveData
        returns a wider set (valuation_metrics + financials.income_statement
        + financials.balance_sheet + technicals) flattened to a single row.
        """
        if source == 'twelvedata':
            payload = self._twelvedata_client.get_statistics(symbol=symbol).as_json()
            stats = (payload or {}).get('statistics')
            if not stats:
                return pd.DataFrame()
            flat = self._flatten_td_dict(stats)
            # Carry through top-level meta if present so the row identifies itself.
            for meta_key in ('symbol', 'name', 'exchange', 'currency'):
                if meta_key in (payload or {}):
                    flat.setdefault(meta_key, payload[meta_key])
            return pd.DataFrame([flat])
        results = self._massive_client.list_financials_ratios(
            ticker=symbol, limit=1,
        )
        return self._financials_to_df(results, limit=1)

    def filing_sections(
        self,
        symbol: str,
        section: str = 'business',
        limit: int = 1,
    ) -> List[dict]:
        """Get 10-K filing sections from Massive.com.

        Parameters
        ----------
        symbol : str
            Stock ticker (e.g. "AAPL").
        section : str
            Section name: "business" or "risk_factors" (default "business").
        limit : int
            Number of filings to return (default 1, i.e. most recent).

        Returns
        -------
        List[dict]
            Each dict has: ticker, cik, filing_date, period_end, section,
            text, filing_url.
        """
        params = {
            'ticker': symbol,
            'section': section,
            'limit': limit,
        }
        resp = self._massive_client._get(
            '/stocks/filings/10-K/v1/sections',
            params=params,
            result_key='results',
            raw=False,
        )
        if isinstance(resp, list):
            return resp
        return list(resp) if resp else []

    # ------------------------------------------------------------------
    # Options — Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_massive_option_ticker(ticker: str) -> dict:
        """Parse a Massive option ticker like ``O:AAPL260320C00250000`` into components.

        Returns dict with keys: symbol, expiration, right, strike.

        Delegates to `trader.tools.options_data.parse_option_ticker` — the shared
        implementation used by the CLI and the dashboard research provider — so
        there is exactly one parser to keep correct.
        """
        from trader.tools.options_data import parse_option_ticker
        return parse_option_ticker(ticker)

    @staticmethod
    def _build_massive_option_ticker(symbol: str, expiration: str, strike: float, right: str) -> str:
        """Build a Massive option ticker from components.

        Parameters
        ----------
        symbol : str
            Underlying symbol (e.g. "AAPL").
        expiration : str
            Expiration date as YYYY-MM-DD.
        strike : float
            Strike price.
        right : str
            "C" for call, "P" for put.

        Returns
        -------
        str
            Ticker like ``O:AAPL260320C00250000``.

        Delegates to `trader.tools.options_data.build_option_ticker` — see
        `_parse_massive_option_ticker` for why.
        """
        from trader.tools.options_data import build_option_ticker
        return build_option_ticker(symbol, expiration, strike, right)

    def _resolve_option_contract(
        self,
        symbol: str,
        expiration: str,
        strike: float,
        right: str,
    ) -> Contract:
        """Build a partial option Contract and resolve it via IB to get the conId.

        Parameters
        ----------
        symbol : str
            Underlying symbol.
        expiration : str
            Expiration as YYYY-MM-DD.
        strike : float
            Strike price.
        right : str
            "C" for call, "P" for put.

        Returns
        -------
        Contract
            Fully resolved IB Contract with conId.
        """
        # IB wants lastTradeDateOrContractMonth as YYYYMMDD
        last_trade_date = expiration.replace('-', '')

        partial = Contract(
            symbol=symbol,
            secType='OPT',
            exchange='SMART',
            currency='USD',
            lastTradeDateOrContractMonth=last_trade_date,
            strike=strike,
            right=right.upper(),
            multiplier='100',
        )

        try:
            defs: List[SecurityDefinition] = consume(
                self._legacy_or_raise('options resolve').rpc(
                    return_type=list[SecurityDefinition]
                ).resolve_contract(partial)
            )
        except Exception as exc:
            self._map_legacy_route_error('options resolve', exc)
        if not defs:
            raise ValueError(
                f"Could not resolve option contract: {symbol} {expiration} {strike} {right}"
            )
        d = defs[0]
        return Contract(
            conId=d.conId,
            symbol=d.symbol,
            secType=d.secType,
            exchange=d.exchange,
            primaryExchange=getattr(d, 'primaryExchange', ''),
            currency=d.currency,
            lastTradeDateOrContractMonth=last_trade_date,
            strike=strike,
            right=right.upper(),
            multiplier='100',
        )

    # ------------------------------------------------------------------
    # Options — Data (OPTIONS capability; default alpaca indicative feed, no trader_service needed)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_chain_filters(contract_type: Optional[str], strike_min: Optional[float],
                             strike_max: Optional[float]) -> None:
        if contract_type not in (None, 'call', 'put'):
            raise ValueError(f"contract_type must be 'call' or 'put', got {contract_type!r}")
        if strike_min is not None and strike_max is not None and strike_min > strike_max:
            raise ValueError(f'strike_min {strike_min} is above strike_max {strike_max}')

    def options_expirations(self, symbol: str, source: Optional[str] = None) -> List[str]:
        """Sorted YYYY-MM-DD expirations that have not passed.

        `source=None` uses `data_providers.options`, else alpaca; options never inherit
        `default_data_source`.
        """
        from trader.data_providers import Capability
        return self._provider(Capability.OPTIONS, source).expirations(symbol)

    def options_chain(
        self,
        symbol: str,
        expiration: Optional[str] = None,
        contract_type: Optional[str] = None,
        strike_min: Optional[float] = None,
        strike_max: Optional[float] = None,
        source: Optional[str] = None,
    ) -> pd.DataFrame:
        """One expiration's chain (nearest when `expiration` is None), columns OPTION_FIELDS.

        Rows carry `provider` and `feed`; numbers the provider did not send are NaN.
        """
        from trader.data_providers import OPTION_FIELDS, Capability
        from trader.data_providers.option_symbols import parse_expiration_date
        self._check_chain_filters(contract_type, strike_min, strike_max)
        if expiration:
            expiration = parse_expiration_date(expiration).isoformat()
        provider = self._provider(Capability.OPTIONS, source)
        if not expiration:
            dates = provider.expirations(symbol)
            if not dates:
                return pd.DataFrame(columns=list(OPTION_FIELDS))
            expiration = dates[0]
        rows = provider.chain(symbol, expiration, contract_type, strike_min, strike_max)
        return pd.DataFrame(rows, columns=list(OPTION_FIELDS))

    def options_snapshot(self, option_ticker: str, source: Optional[str] = None) -> dict:
        """One contract; accepts `O:AAPL261120C00250000` or `AAPL261120C00250000`."""
        from trader.data_providers import Capability
        from trader.data_providers.option_symbols import parse_option_symbol
        option = parse_option_symbol(option_ticker)
        return self._provider(Capability.OPTIONS, source).contract(option)

    def options_implied(
        self,
        symbol: str,
        expiration: str,
        risk_free_rate: float = 0.05,
        source: Optional[str] = None,
    ) -> Dict:
        """Market-implied vs constant-vol distribution from the expiration's calls.

        Calls without an implied volatility are excluded and counted
        (`strikes_used`, `strikes_excluded`); too few usable strikes raise ValueError.
        """
        from trader.data_providers import Capability
        from trader.data_providers.option_symbols import parse_expiration_date
        from trader.tools.chain import implied_distribution
        expiration = parse_expiration_date(expiration).isoformat()
        rows = self._provider(Capability.OPTIONS, source).chain(symbol, expiration, 'call')
        if not any(row['type'] == 'call' and row['expiration'] == expiration for row in rows):
            provider_name = source or self._provider_default(Capability.OPTIONS)
            raise ValueError(f'No call contracts for {symbol} {expiration} from {provider_name}')
        return implied_distribution(rows, expiration, risk_free_rate, dt.date.today())

    # ------------------------------------------------------------------
    # Options — Trading (IB via trader_service)
    # ------------------------------------------------------------------

    def buy_option(
        self,
        symbol: str,
        expiration: str,
        strike: float,
        right: str,
        quantity: float,
        limit_price: Optional[float] = None,
        market: bool = False,
    ) -> SuccessFail:
        """Place a buy order for an option contract.

        Parameters
        ----------
        symbol : str
            Underlying symbol (e.g. "AAPL").
        expiration : str
            Expiration date as YYYY-MM-DD.
        strike : float
            Strike price.
        right : str
            "C" for call, "P" for put.
        quantity : float
            Number of contracts.
        limit_price : float, optional
            Limit price per contract. Required if market=False.
        market : bool
            True for market order.
        """
        if not market and limit_price is None:
            raise ValueError("Specify market=True or provide a limit_price")

        contract = self._resolve_option_contract(symbol, expiration, strike, right)

        return consume(
            self._legacy_or_raise('place order').rpc(
                return_type=SuccessFail[Trade]
            ).place_order_simple(
                contract=contract,
                action='BUY',
                equity_amount=None,
                quantity=quantity,
                limit_price=limit_price,
                market_order=market,
                stop_loss_percentage=0.0,
                debug=False,
            )
        )

    def sell_option(
        self,
        symbol: str,
        expiration: str,
        strike: float,
        right: str,
        quantity: float,
        limit_price: Optional[float] = None,
        market: bool = False,
    ) -> SuccessFail:
        """Place a sell order for an option contract.

        Parameters
        ----------
        symbol : str
            Underlying symbol (e.g. "AAPL").
        expiration : str
            Expiration date as YYYY-MM-DD.
        strike : float
            Strike price.
        right : str
            "C" for call, "P" for put.
        quantity : float
            Number of contracts.
        limit_price : float, optional
            Limit price per contract. Required if market=False.
        market : bool
            True for market order.
        """
        if not market and limit_price is None:
            raise ValueError("Specify market=True or provide a limit_price")

        contract = self._resolve_option_contract(symbol, expiration, strike, right)

        return consume(
            self._legacy_or_raise('place order').rpc(
                return_type=SuccessFail[Trade]
            ).place_order_simple(
                contract=contract,
                action='SELL',
                equity_amount=None,
                quantity=quantity,
                limit_price=limit_price,
                market_order=market,
                stop_loss_percentage=0.0,
                debug=False,
            )
        )

    # ------------------------------------------------------------------
    # Forex
    # ------------------------------------------------------------------

    def forex_snapshot(self, pair: str, source: str = IB_FOREX_SOURCE) -> dict:
        """Snapshot for a currency pair ('EURUSD', 'EUR/USD' or 'C:EURUSD').

        source 'ib' (default) resolves the IDEALPRO CASH contract via trader_service. Any other
        value is a registry forex source: 'frankfurter' (ECB daily reference rate, not live; no
        bid/ask), 'massive' or 'twelvedata' (no bid/ask).
        """
        from trader.data_providers.symbols import parse_forex_pair
        base, quote_currency = parse_forex_pair(pair)
        if source == IB_FOREX_SOURCE:
            return self._ib_forex_snapshot(base, quote_currency)
        return self._forex_rate(base, quote_currency, source)

    def forex_quote(self, from_currency: str, to_currency: str, source: str = IB_FOREX_SOURCE) -> dict:
        """Last quote for a currency pair. 'ib' (default) gives IB bid/ask; registry sources give
        the same shared rate dict as :meth:`forex_snapshot`."""
        from trader.data_providers.symbols import parse_forex_codes
        base, quote_currency = parse_forex_codes(from_currency, to_currency)
        if source != IB_FOREX_SOURCE:
            return self._forex_rate(base, quote_currency, source)
        snapshot = self._ib_forex_snapshot(base, quote_currency)
        return {key: snapshot[key] for key in ('pair', 'bid', 'ask', 'last', 'time')}

    def _forex_rate(self, base: str, quote_currency: str, source: str) -> dict:
        from trader.data_providers import Capability
        return self._provider(Capability.FOREX, source).rate(base, quote_currency)

    def _ib_forex_snapshot(self, base: str, quote_currency: str) -> dict:
        contract = self._resolve_contract(base, sec_type='CASH', exchange='IDEALPRO', currency=quote_currency)
        response = self._typed_query.call(
            'get_snapshot',
            {'instrument_id': int(contract.conId), 'delayed': False},
            dict,
        )
        snapshot = response.get('snapshot') or {}
        return {
            'pair': f'{base}/{quote_currency}',
            'bid': snapshot.get('bid'),
            'bidSize': snapshot.get('bid_size'),
            'ask': snapshot.get('ask'),
            'askSize': snapshot.get('ask_size'),
            'last': snapshot.get('last'),
            'open': snapshot.get('open'),
            'high': snapshot.get('high'),
            'low': snapshot.get('low'),
            'close': snapshot.get('close'),
            'time': snapshot.get('time'),
        }

    def forex_convert(self, from_currency: str, to_currency: str, amount: float,
                      source: Optional[str] = None) -> dict:
        """Convert `amount` with a registry forex source (default: `data_providers.forex`, else builtin)."""
        import math
        from trader.data_providers import Capability
        from trader.data_providers.symbols import parse_forex_codes
        base, quote_currency = parse_forex_codes(from_currency, to_currency)
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError(f'amount must be a positive number, got {amount!r}')
        return self._provider(Capability.FOREX, source).convert(base, quote_currency, float(amount))

    def forex_snapshot_all(self, base: str = 'USD', symbols: Optional[List[str]] = None,
                           source: Optional[str] = None) -> pd.DataFrame:
        """Rates of `base` against each of `symbols` (None = every currency the source has)."""
        from trader.data_providers import Capability
        from trader.data_providers.symbols import parse_currency, parse_forex_codes
        base = parse_currency(base)
        quotes = list(dict.fromkeys(parse_forex_codes(base, symbol)[1] for symbol in symbols)) if symbols else None
        return self._provider(Capability.FOREX, source).rates(base, quotes)

    def forex_movers(self, direction: str = 'gainers', source: Optional[str] = None) -> pd.DataFrame:
        """Forex movers from a registry source (default: `data_providers.movers_forex`, else builtin)."""
        from trader.data_providers import Capability
        return self._provider(Capability.MOVERS_FOREX, source).movers('forex', direction)

    def _alpaca_assets(self):
        from trader.data_providers.builtin import alpaca_asset_directory
        directory = alpaca_asset_directory(self._container.config())
        return directory.load() if directory else None

    def _movers_asset_directory(self):
        """The asset list for movers enrichment and the note to show when it is missing.

        A failure here never fails movers; it only turns the warrant filter off.
        """
        import requests
        from trader.data_providers import ProviderError
        from trader.data_providers.movers_filter import (
            ASSET_LIST_UNAVAILABLE_NOTE, INSTRUMENT_FILTER_OFF_NOTE)
        try:
            directory = self._alpaca_assets()
        except (ProviderError, requests.RequestException, TypeError, ValueError, AttributeError) as ex:
            logger.warning('alpaca asset list unavailable, movers warrant filter off: %s', ex)
            return None, ASSET_LIST_UNAVAILABLE_NOTE
        return directory, INSTRUMENT_FILTER_OFF_NOTE

    @staticmethod
    def _keeps_stock_mover(snap, min_price: float, assets) -> bool:
        """Same rule as `filter_stock_movers`, for a raw Massive snapshot."""
        close = getattr(snap.day, 'close', None) if snap.day else None
        if close is None or pd.isna(close) or close < min_price:
            return False
        return assets is None or not assets.is_derivative_unit(snap.ticker or '')

    def movers(
        self,
        market: str = 'stocks',
        direction: str = 'gainers',
        source: Optional[str] = None,
        min_price: float = 1.0,
    ) -> pd.DataFrame:
        """Top movers for `market` from a registry movers source.

        Indices default to ETF proxies and forex to rates computed from the ECB daily rates.

        Stock movers drop names under `min_price` and, when Alpaca is configured, warrants,
        rights and units. `source=None` uses `data_providers.movers` (or `movers_indices` /
        `movers_forex`), else the builtin default; movers never inherit `default_data_source`.
        """
        from trader.data_providers import movers_capability
        from trader.data_providers.movers_filter import filter_stock_movers
        frame = self._provider(movers_capability(market), source).movers(market, direction)
        if market == 'stocks':
            assets, off_note = self._movers_asset_directory()
            frame = filter_stock_movers(frame, min_price, assets, off_note)
        return frame

    def movers_detail(
        self,
        market: str = 'stocks',
        direction: str = 'gainers',
        num: int = 20,
        source: Optional[str] = None,
        min_price: float = 1.0,
    ) -> list[dict]:
        """Get movers enriched with company name, ratios, and news.

        Parameters
        ----------
        source : str
            Defaults to the registry movers default (Alpaca).
            'alpaca' (and any other registry source) — names and the latest
            headline per ticker. No ratios until phase 4.
            'massive' — full enrichment via Massive snapshots,
            ticker details, ratios, and news.
            'twelvedata' — composes :meth:`movers` (TD) + :meth:`ratios`
            (TD) per ticker. Skips news (TD has no news endpoint) and
            description (not exposed on the TD movers payload). Each
            ratios call costs ~100 TD credits, so this scales linearly
            in your plan's credit budget.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from trader.data_providers import movers_capability

        source = source or self._provider_default(movers_capability(market))

        if source == 'twelvedata':
            df = self.movers(market=market, direction=direction, source='twelvedata', min_price=min_price)
            if df.empty:
                return []
            rows = df.head(num).to_dict('records')

            def fetch_td_ratios(t):
                try:
                    rdf = self.ratios(symbol=t, source='twelvedata')
                    if rdf.empty:
                        return (t, {})
                    r = rdf.iloc[0]
                    out: dict = {}
                    field_map = [
                        ('valuations_metrics.trailing_pe', 'pe'),
                        ('valuations_metrics.forward_pe', 'fwd_pe'),
                        ('financials.income_statement.diluted_eps_ttm', 'eps'),
                        ('dividends_and_splits.forward_dividend_yield', 'div_yield'),
                        ('valuations_metrics.market_capitalization', 'market_cap'),
                    ]
                    for col, label in field_map:
                        if col in r.index:
                            v = r[col]
                            if v is not None and v == v:  # not NaN
                                try:
                                    out[label] = round(float(v), 4)
                                except (TypeError, ValueError):
                                    pass
                    return (t, out)
                except Exception:
                    return (t, {})

            ratios_map: dict = {}
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {pool.submit(fetch_td_ratios, m['ticker']): m['ticker']
                           for m in rows if m.get('ticker')}
                for fut in as_completed(futures):
                    t, data = fut.result()
                    ratios_map[t] = data

            results: list[dict] = []
            for m in rows:
                t = m.get('ticker', '')
                results.append({
                    'ticker': t,
                    'open': None,  # TD movers payload has last/high/low but not open
                    'close': m.get('close'),
                    'volume': m.get('volume'),
                    'change': m.get('change'),
                    'change_pct': m.get('change_pct'),
                    'details': {
                        'name': m.get('name', ''),
                        'exchange': m.get('exchange', ''),
                        'description': '',
                    },
                    'ratios': ratios_map.get(t, {}),
                    'news': {},  # TD has no news endpoint
                })
            return results

        if source == 'massive':
            snaps = self._massive_client.get_snapshot_direction(
                market_type=market, direction=direction,
            )

            if market == 'stocks':
                assets = self._movers_asset_directory()[0]
                snaps = [snap for snap in snaps if self._keeps_stock_mover(snap, min_price, assets)]

            # Build base data from snapshots
            movers = []
            for snap in snaps[:num]:
                ticker = snap.ticker or ''
                if not ticker:
                    continue
                row = {
                    'ticker': ticker,
                    'open': getattr(snap.day, 'open', None) if snap.day else None,
                    'close': getattr(snap.day, 'close', None) if snap.day else None,
                    'volume': getattr(snap.day, 'volume', None) if snap.day else None,
                    'change': snap.todays_change,
                    'change_pct': snap.todays_change_percent,
                }
                movers.append(row)

            if not movers:
                return []

            tickers = [m['ticker'] for m in movers]

            # Parallel fetch: ticker details, ratios, news
            details_map = {}
            ratios_map = {}
            news_map = {}

            def fetch_details(t):
                try:
                    d = self._massive_client.get_ticker_details(t)
                    return (t, {'name': d.name, 'market_cap': d.market_cap, 'description': d.description})
                except Exception:
                    return (t, {})

            def fetch_ratios(t):
                try:
                    results = list(self._massive_client.list_financials_ratios(ticker=t, limit=1))
                    if not results:
                        return (t, {})
                    r = results[0]
                    data = {}
                    for attr, label in [
                        ('price_to_earnings', 'pe'), ('debt_to_equity', 'de'),
                        ('return_on_equity', 'roe'), ('earnings_per_share', 'eps'),
                        ('dividend_yield', 'div_yield'),
                    ]:
                        val = getattr(r, attr, None)
                        if val is not None:
                            data[label] = round(float(val), 2)
                    return (t, data)
                except Exception:
                    return (t, {})

            def fetch_news(t):
                try:
                    articles = list(self._massive_client.list_ticker_news(ticker=t, limit=1))
                    if not articles:
                        return (t, {})
                    a = articles[0]
                    sentiment = ''
                    if a.insights:
                        sentiments = [i.sentiment for i in a.insights if i.sentiment]
                        sentiment = ', '.join(sentiments)
                    return (t, {'headline': a.title, 'sentiment': sentiment})
                except Exception:
                    return (t, {})

            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = []
                for t in tickers:
                    futures.append(pool.submit(fetch_details, t))
                    futures.append(pool.submit(fetch_ratios, t))
                    futures.append(pool.submit(fetch_news, t))

                for future in as_completed(futures):
                    ticker, data = future.result()
                    # Determine which map to update based on keys
                    if 'name' in data:
                        details_map[ticker] = data
                    elif 'headline' in data:
                        news_map[ticker] = data
                    elif data and 'name' not in data and 'headline' not in data:
                        ratios_map[ticker] = data

            # Merge into results
            for m in movers:
                t = m['ticker']
                m['details'] = details_map.get(t, {})
                m['ratios'] = ratios_map.get(t, {})
                m['news'] = news_map.get(t, {})

            return movers

        return self._movers_detail_from_capabilities(market, direction, num, source, min_price)

    def _movers_detail_from_capabilities(self, market, direction, num, source, min_price) -> list[dict]:
        from concurrent.futures import ThreadPoolExecutor
        from trader.data_providers import Capability
        frame = self.movers(market=market, direction=direction, source=source, min_price=min_price).head(num)
        assets = self._movers_asset_directory()[0] if market == 'stocks' else None
        news_provider = self._provider(Capability.NEWS)

        def latest_headline(ticker: str) -> dict:
            try:
                items = news_provider.news(ticker, 1)
            except Exception as ex:
                logger.warning('headline fetch failed for %s: %s', ticker, ex)
                return {}
            return {'headline': items[0]['title'], 'sentiment': items[0]['sentiment']} if items else {}

        tickers = frame['ticker'].tolist()
        with ThreadPoolExecutor(max_workers=5) as pool:
            headlines = dict(zip(tickers, pool.map(latest_headline, tickers)))
        return [{
            'ticker': row.ticker,
            'open': None,
            'close': row.close,
            'volume': None if pd.isna(row.volume) else row.volume,
            'change': row.change,
            'change_pct': row.change_pct,
            'details': {'name': row.name or '', 'exchange': assets.exchange(row.ticker) if assets else '',
                        'description': ''},
            'ratios': {},
            'news': headlines.get(row.ticker, {}),
            'provider': row.provider,
            'note': row.note,
        } for row in frame.itertuples(index=False)]

    def scan_ideas(
        self,
        preset: str = 'momentum',
        source: str = 'movers',
        tickers: Optional[List[str]] = None,
        universe: Optional[str] = None,
        top_n: int = 15,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        min_volume: Optional[int] = None,
        min_change_pct: Optional[float] = None,
        max_change_pct: Optional[float] = None,
        fundamentals: bool = False,
        news: bool = False,
        names: bool = False,
        location: Optional[str] = None,
        data_source: Optional[str] = None,
        fundamentals_if_available: bool = False,
    ) -> pd.DataFrame:
        """Scan for trading ideas using preset-based scoring.

        Parameters
        ----------
        preset : str
            Scoring preset (momentum, gap-up, gap-down, mean-reversion, breakout, volatile).
        source : str
            'movers' (default), 'tickers', or 'universe'.
        tickers : list of str, optional
            Explicit ticker list (when source='tickers').
        universe : str, optional
            Universe name to scan (when source='universe').
        top_n : int
            Max results to return.
        min_price, max_price, min_volume, min_change_pct, max_change_pct
            Override preset filter defaults.
        fundamentals : bool
            If True, enrich results with financial ratios (PE, D/E, ROE, etc.).
        news : bool
            If True, enrich results with latest news headline and sentiment.
        location : str, optional
            IB market location code (e.g. STK.AU.ASX, STK.CA). When set, uses
            IB scanner API instead of Massive.com (for international markets).
        data_source : str, optional
            Registry source for US equities (e.g. 'massive', 'twelvedata').
            Default: ``data_providers.ideas`` from the config, else the builtin
            default. Ignored when ``location`` is set.
        """
        # Build custom filter overrides
        custom_filters = {}
        if min_price is not None:
            custom_filters['min_price'] = min_price
        if max_price is not None:
            custom_filters['max_price'] = max_price
        if min_volume is not None:
            custom_filters['min_volume'] = min_volume
        if min_change_pct is not None:
            custom_filters['min_change_pct'] = min_change_pct
        if max_change_pct is not None:
            custom_filters['max_change_pct'] = max_change_pct

        # IB path: use IBIdeaScanner for international markets
        if location:
            # IBIdeaScanner still needs legacy dill RPC for scanner/history/
            # fundamentals/news — those aren't on the typed production surface yet.
            try:
                self._legacy_or_raise('ideas --location')
            except ConnectionError:
                raise ConnectionError(
                    'ideas --location (IB international path) requires the '
                    'offline-simulation legacy RPC (port 42001), which is not '
                    'bound in the split-container production topology. '
                    'Use `ideas` without --location for US (Alpaca by default; --source massive|twelvedata), '
                    'or run with `unsafe_legacy_rpc: true` + `--simulation True`.'
                ) from None

            from trader.tools.idea_scanner import IBIdeaScanner, RpcScannerProvider

            # Resolve universe to symbol list for IB path
            ib_universe_symbols = None
            if source == 'universe' and universe:
                from trader.data.universe import UniverseAccessor
                cfg = self._container.config()
                accessor = UniverseAccessor(
                    cfg.get('duckdb_path', ''),
                    cfg.get('universe_library', 'Universes'),
                )
                u = accessor.get(universe)
                if u.security_definitions:
                    ib_universe_symbols = [d.symbol for d in u.security_definitions]
                else:
                    return pd.DataFrame()

            scanner = IBIdeaScanner(RpcScannerProvider(self._rpc))
            return scanner.scan(
                preset=preset,
                location=location,
                top_n=top_n,
                custom_filters=custom_filters or None,
                fundamentals=fundamentals or fundamentals_if_available,
                news=news,
                tickers=tickers if source == 'tickers' else None,
                universe_symbols=ib_universe_symbols,
            )

        # Resolve universe to symbol list (shared by both US data sources)
        universe_symbols = None
        if source == 'universe' and universe:
            from trader.data.universe import UniverseAccessor
            cfg = self._container.config()
            accessor = UniverseAccessor(
                cfg.get('duckdb_path', ''),
                cfg.get('universe_library', 'Universes'),
            )
            u = accessor.get(universe)
            if u.security_definitions:
                universe_symbols = [d.symbol for d in u.security_definitions]
            else:
                return pd.DataFrame()

        from trader.data_providers import Capability
        from trader.tools.idea_scanner import (
            IdeaScanner,
            IdeaScannerError,
            LIQUID_US_FALLBACK_TICKERS,
            entitlement_fallback_notice,
            is_data_entitlement_error,
        )
        resolved = data_source or self._provider_default(Capability.IDEAS)
        scan_source = self._provider(Capability.IDEAS, resolved)
        scan_kwargs = dict(
            preset=preset,
            source=source,
            tickers=tickers,
            universe_symbols=universe_symbols,
            top_n=top_n,
            custom_filters=custom_filters or None,
            fundamentals=fundamentals,
            news=news,
            names=names,
            fundamentals_if_available=fundamentals_if_available,
        )
        if resolved != 'massive':
            return IdeaScanner(scan_source).scan(**scan_kwargs)

        # Massive: Stocks Basic has no snapshots — fall back to TwelveData
        # quotes on entitlement errors so bare `ideas` still works when the
        # user has a TD key (common for history). Removed in phase 3c.
        from trader.data_providers.twelvedata.scan import TwelveDataScanSource
        try:
            return IdeaScanner(scan_source).scan(**scan_kwargs)
        except Exception as ex:
            if not is_data_entitlement_error(ex):
                raise
            notice = entitlement_fallback_notice('massive', str(ex))
            logging.warning(notice)
            fb_source = source
            fb_tickers = tickers
            fb_universe = universe_symbols
            if source == 'movers' or (not tickers and not universe_symbols):
                fb_source = 'tickers'
                fb_tickers = list(LIQUID_US_FALLBACK_TICKERS)
                fb_universe = None
            try:
                td = IdeaScanner(TwelveDataScanSource(self._twelvedata_client))
                df = td.scan(
                    preset=preset,
                    source=fb_source,
                    tickers=fb_tickers,
                    universe_symbols=fb_universe,
                    top_n=top_n,
                    custom_filters=custom_filters or None,
                    fundamentals=fundamentals,
                    news=False,  # TD has no news
                    names=names,
                    fundamentals_if_available=fundamentals_if_available,
                )
            except Exception as td_ex:
                raise IdeaScannerError(
                    f'{notice} TwelveData fallback also failed: {td_ex}'
                ) from td_ex
            df.attrs['ideas_notice'] = notice
            return df

    def scan(
        self,
        scan_code: str = 'TOP_PERC_GAIN',
        instrument: str = 'STK',
        location_code: str = 'STK.US.MAJOR',
        num_rows: int = 20,
        above_price: float = 0.0,
        above_volume: int = 0,
        market_cap_above: float = 0.0,
    ) -> pd.DataFrame:
        """Run an IB market scanner."""
        # Check location against trading filters
        from trader.trading.trading_filter import TradingFilter
        tf = TradingFilter.load()
        if not tf.is_empty():
            allowed, reason = tf.is_allowed('', location=location_code)
            if not allowed:
                raise ValueError(f'Trading filter blocked location {location_code}: {reason}')
        try:
            results = consume(
                self._legacy_or_raise('scan').rpc(return_type=list[dict]).scanner_data(
                    scan_code=scan_code,
                    instrument=instrument,
                    location_code=location_code,
                    num_rows=num_rows,
                    above_price=above_price,
                    above_volume=above_volume,
                    market_cap_above=market_cap_above,
                )
            )
        except Exception as exc:
            self._map_legacy_route_error('scan', exc)
        return pd.DataFrame(results) if results else pd.DataFrame()

    # ------------------------------------------------------------------
    # News (provider registry)
    # ------------------------------------------------------------------

    def news(self, ticker: Optional[str] = None, limit: int = 10,
             source: Optional[str] = None) -> pd.DataFrame:
        """News headlines from a registry news source ('alpaca', 'polygon', 'benzinga')."""
        from trader.data_providers import Capability
        items = self._provider(Capability.NEWS, source).news(ticker, limit)
        frame = pd.DataFrame([{
            'published': i['published'], 'title': i['title'], 'tickers': ', '.join(i['tickers']),
            'author': i['author'], 'url': i['url'], 'summary': i['summary'], 'sentiment': i['sentiment'],
        } for i in items], columns=['published', 'title', 'tickers', 'author', 'url', 'summary', 'sentiment'])
        if not frame['sentiment'].astype(bool).any():
            frame = frame.drop(columns='sentiment')
        return frame

    def news_detail(self, ticker: Optional[str] = None, limit: int = 5,
                    source: Optional[str] = None) -> List[dict]:
        """News with summaries and (where the provider has it) per-ticker sentiment insights."""
        from trader.data_providers import Capability
        keys = ('title', 'published', 'author', 'tickers', 'url', 'summary', 'insights')
        return [{key: item[key] for key in keys}
                for item in self._provider(Capability.NEWS, source).news(ticker, limit)]

    # ------------------------------------------------------------------
    # Market hours (local-only, no service needed)
    # ------------------------------------------------------------------

    _MARKET_CALENDARS = [
        ('XNYS',  'NYSE',       'US'),
        ('XNAS',  'NASDAQ',     'US'),
        ('XASX',  'ASX',        'Australia'),
        ('XTSE',  'TSX',        'Canada'),
        ('XLON',  'LSE',        'UK'),
        ('XHKG',  'HKEX',       'Hong Kong'),
        ('XTKS',  'TSE',        'Japan'),
        ('XFRA',  'Frankfurt',  'Germany'),
        ('XPAR',  'Euronext',   'France'),
    ]

    def market_hours(self) -> List[Dict]:
        """Return open/close status for major exchanges. No RPC needed."""
        import exchange_calendars as xcals

        now = pd.Timestamp.now(tz='UTC')
        today = pd.Timestamp(now.date())  # tz-naive date for session lookups
        results = []

        for cal_code, name, region in self._MARKET_CALENDARS:
            cal = xcals.get_calendar(cal_code)
            is_open = cal.is_open_on_minute(now)
            status = 'OPEN' if is_open else 'CLOSED'

            if is_open:
                # Next event is market close
                session = cal.minute_to_session(now)
                next_event_time = cal.session_close(session)
                next_event = 'closes'
            else:
                # Next event is market open — search upcoming sessions
                next_event_time = None
                next_event = 'opens'
                try:
                    for offset in range(5):
                        candidate = today + pd.Timedelta(days=offset)
                        if cal.is_session(candidate):
                            open_time = cal.session_open(candidate)
                            if open_time > now:
                                next_event_time = open_time
                                break
                except Exception:
                    pass

            if next_event_time is not None:
                delta = next_event_time - now
                total_minutes = int(delta.total_seconds() // 60)
                hours, minutes = divmod(total_minutes, 60)
                if hours > 0:
                    relative = f'in {hours}h {minutes}m'
                else:
                    relative = f'in {minutes}m'
            else:
                relative = ''

            results.append({
                'exchange': name,
                'region': region,
                'status': status,
                'next_event': next_event,
                'next_event_time': str(next_event_time)[:19] if next_event_time else '',
                'relative': relative,
            })

        return results
