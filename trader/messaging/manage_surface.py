"""Typed RPC handlers for dashboard /manage (universes + symbol discovery).

Registered on the trader's production typed registry so the dashboard
process can manage watchlists and resolve symbols without legacy dill RPC
or a co-located DuckDB file.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from typing import Any, Dict, TYPE_CHECKING

from ib_async import Contract

from trader.messaging.manage_contracts import (
    AddUniverseSymbolsRequest,
    CreateUniverseRequest,
    DeleteUniverseRequest,
    DiscoverInstrumentRequest,
    DiscoverInstrumentResponse,
    GetUniverseRequest,
    GetUniverseResponse,
    ImportUniverseCsvRequest,
    ListUniversesRequest,
    ListUniversesResponse,
    RemoveUniverseSymbolRequest,
    UniverseSummary,
)
from trader.messaging.trader_service_api import TraderServiceApi
from trader.messaging.typed_rpc import TypedRpcRegistry, _DispatchProblem
from trader.sdk import MMR

if TYPE_CHECKING:
    from trader.trading.trading_runtime import Trader

logger = logging.getLogger(__name__)


def _instrument_to_wire(definition: Any) -> Dict[str, Any]:
    return {
        'instrument_id': int(definition.conId),
        'symbol': str(definition.symbol),
        'exchange': str(definition.exchange),
        'primary_exchange': str(definition.primaryExchange),
        'currency': str(definition.currency),
        'security_type': str(definition.secType),
        'time_zone_id': str(definition.timeZoneId),
    }


def _accessor(trader: 'Trader'):
    return trader.universe_accessor


def _list_universes_handler(api: TraderServiceApi):
    def _handler(_parsed: ListUniversesRequest) -> Dict[str, Any]:
        counts = _accessor(api.trader).list_universes_count()
        payload = ListUniversesResponse(
            universes=[UniverseSummary(name=name, count=count)
                       for name, count in sorted(counts.items())],
        )
        return payload.model_dump()
    return _handler


def _get_universe_handler(api: TraderServiceApi):
    def _handler(parsed: GetUniverseRequest) -> Dict[str, Any]:
        accessor = _accessor(api.trader)
        counts = accessor.list_universes_count()
        count = counts.get(parsed.name, 0)
        symbols: list[str] = []
        try:
            defs = accessor.get(parsed.name).security_definitions
            symbols = [str(d.symbol) for d in defs[:parsed.symbol_limit]]
        except Exception as exc:  # noqa: BLE001
            logger.warning('get_universe %s failed: %s', parsed.name, exc)
        return GetUniverseResponse(name=parsed.name, count=count, symbols=symbols).model_dump()
    return _handler


def _create_universe_handler(api: TraderServiceApi):
    def _handler(parsed: CreateUniverseRequest) -> Dict[str, Any]:
        accessor = _accessor(api.trader)
        if parsed.name in accessor.list_universes_count():
            raise _DispatchProblem('ALREADY_EXISTS', f'watchlist {parsed.name!r} already exists')
        universe = accessor.get(parsed.name)
        accessor.update(universe)
        return {'ok': True, 'name': parsed.name}
    return _handler


async def _discover_one(api: TraderServiceApi, symbol: str, *,
                        exchange: str = '', currency: str = '', sec_type: str = 'STK'):
    local = await api.resolve_symbol(symbol, exchange=exchange, sec_type=sec_type)
    if local:
        return local
    if sec_type == 'CASH':
        pair = str(symbol).replace('/', '').replace('C:', '').upper()
        base = pair[:3] if len(pair) == 6 else pair
        quote_ccy = pair[3:] if len(pair) == 6 else 'USD'
        contract = Contract(symbol=base, secType='CASH', exchange='IDEALPRO', currency=quote_ccy)
    else:
        contract = Contract(
            symbol=str(symbol),
            exchange=exchange,
            secType=sec_type or 'STK',
            currency=currency,
        )
    candidates = await api.resolve_contract(contract)
    return MMR._dedupe_venue_duplicates(candidates)


def _discover_instrument_handler(api: TraderServiceApi):
    async def _handler(parsed: DiscoverInstrumentRequest) -> Dict[str, Any]:
        instruments = await _discover_one(
            api, parsed.symbol.upper(),
            exchange=parsed.exchange, currency=parsed.currency, sec_type=parsed.sec_type,
        )
        return DiscoverInstrumentResponse(
            instruments=[_instrument_to_wire(d) for d in instruments],
        ).model_dump()
    return _handler


def _add_universe_symbols_handler(api: TraderServiceApi):
    async def _handler(parsed: AddUniverseSymbolsRequest) -> Dict[str, Any]:
        # IB resolve is the slow part (often multi-second per symbol). Run a
        # bounded fan-out so a 5–10 symbol watchlist add fits inside the
        # dashboard manage client's RPC budget instead of serialising until
        # TimeoutError at 10s.
        symbols = [s.strip().upper() for s in parsed.symbols if s and s.strip()]
        if not symbols:
            return {'added': [], 'missing': []}

        sem = asyncio.Semaphore(4)

        async def _one(sym: str):
            async with sem:
                instruments = await _discover_one(
                    api, sym, exchange=parsed.exchange, currency=parsed.currency,
                    sec_type=parsed.sec_type,
                )
            return sym, instruments

        results = await asyncio.gather(*(_one(s) for s in symbols))
        accessor = _accessor(api.trader)
        added, missing = [], []
        # Preserve request order for the flash message.
        by_sym = {sym: instruments for sym, instruments in results}
        for sym in symbols:
            instruments = by_sym.get(sym) or []
            if instruments:
                accessor.insert(parsed.name, instruments[0])
                added.append({'symbol': sym, 'instrument_id': int(instruments[0].conId)})
            else:
                missing.append(sym)
        return {'added': added, 'missing': missing}
    return _handler


def _remove_universe_symbol_handler(api: TraderServiceApi):
    def _handler(parsed: RemoveUniverseSymbolRequest) -> Dict[str, Any]:
        accessor = _accessor(api.trader)
        universe = accessor.get(parsed.name)
        match = universe.find_symbol(parsed.symbol.strip())
        if match is None:
            raise _DispatchProblem('NOT_FOUND', f'{parsed.symbol!r} not in {parsed.name!r}')
        universe.security_definitions = [
            d for d in universe.security_definitions if d.conId != match.conId]
        accessor.update(universe)
        return {'ok': True, 'symbol': parsed.symbol.strip().upper()}
    return _handler


def _delete_universe_handler(api: TraderServiceApi):
    def _handler(parsed: DeleteUniverseRequest) -> Dict[str, Any]:
        _accessor(api.trader).delete(parsed.name)
        return {'ok': True, 'name': parsed.name}
    return _handler


def _import_universe_csv_handler(api: TraderServiceApi):
    async def _handler(parsed: ImportUniverseCsvRequest) -> Dict[str, Any]:
        text = parsed.csv_text
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            raise _DispatchProblem('VALIDATION_ERROR', 'CSV is empty')
        accessor = _accessor(api.trader)
        header = [h.strip().lower() for h in lines[0].split(',')]
        if 'conid' in header:
            count = accessor.update_from_csv_str(parsed.name, text)
            return {'imported': count, 'added': [], 'missing': []}
        if 'symbol' in header:
            rows = list(csv.DictReader(io.StringIO(text)))
            rows = [{k.strip().lower(): (v or '').strip() for k, v in r.items()} for r in rows]
        else:
            rows = [{'symbol': ln.split(',')[0].strip()} for ln in lines]
        added, missing = [], []
        for row in rows:
            sym = (row.get('symbol') or '').upper()
            if not sym:
                continue
            instruments = await _discover_one(
                api, sym,
                exchange=row.get('exchange', ''),
                currency=row.get('currency', ''),
                sec_type=row.get('sectype', 'STK') or 'STK',
            )
            if instruments:
                accessor.insert(parsed.name, instruments[0])
                added.append(sym)
            else:
                missing.append(sym)
        return {'imported': len(added), 'added': added, 'missing': missing}
    return _handler


def register_manage_surface(registry: TypedRpcRegistry, api: TraderServiceApi) -> None:
    """Wire universe read/write + symbol discovery onto the trader typed registry."""
    registry.register(
        'query', 'list_universes', ListUniversesRequest, ListUniversesResponse,
        _list_universes_handler(api), execution='thread',
    )
    registry.register(
        'query', 'get_universe', GetUniverseRequest, GetUniverseResponse,
        _get_universe_handler(api), execution='thread',
    )
    registry.register(
        'query', 'discover_instrument', DiscoverInstrumentRequest, DiscoverInstrumentResponse,
        _discover_instrument_handler(api),
    )
    registry.register(
        'command', 'create_universe', CreateUniverseRequest, dict,
        _create_universe_handler(api), execution='thread',
    )
    registry.register(
        'command', 'add_universe_symbols', AddUniverseSymbolsRequest, dict,
        _add_universe_symbols_handler(api),
    )
    registry.register(
        'command', 'remove_universe_symbol', RemoveUniverseSymbolRequest, dict,
        _remove_universe_symbol_handler(api), execution='thread',
    )
    registry.register(
        'command', 'delete_universe', DeleteUniverseRequest, dict,
        _delete_universe_handler(api), execution='thread',
    )
    registry.register(
        'command', 'import_universe_csv', ImportUniverseCsvRequest, dict,
        _import_universe_csv_handler(api),
    )
