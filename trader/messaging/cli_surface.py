"""Typed query handlers for CLI/SDK reads that used to hit legacy dill RPC.

Port 42001 is unbound in the split-container production topology. These
handlers expose JSON-safe equivalents on the trader typed query socket
(42101) so ``mmr portfolio`` / ``orders`` / ``snapshot`` / ``account`` /
``status`` / etc. work without the offline-simulation legacy server.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from ib_async import Contract
from pydantic import BaseModel, ConfigDict, Field

from trader.messaging.trader_service_api import TraderServiceApi
from trader.messaging.typed_rpc import TypedRpcRegistry


def _finite_or_none(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float('inf'), float('-inf')):
        return None
    return number


def _contract_fields(contract: Any) -> Dict[str, Any]:
    if isinstance(contract, dict):
        return {
            'instrument_id': int(contract.get('conId') or 0),
            'symbol': str(contract.get('localSymbol') or contract.get('symbol') or ''),
            'security_type': str(contract.get('secType') or 'STK'),
            'currency': str(contract.get('currency') or ''),
            'exchange': str(contract.get('exchange') or ''),
            'primary_exchange': str(contract.get('primaryExchange') or ''),
        }
    return {
        'instrument_id': int(getattr(contract, 'conId', 0) or 0),
        'symbol': str(getattr(contract, 'localSymbol', None) or getattr(contract, 'symbol', '') or ''),
        'security_type': str(getattr(contract, 'secType', 'STK') or 'STK'),
        'currency': str(getattr(contract, 'currency', '') or ''),
        'exchange': str(getattr(contract, 'exchange', '') or ''),
        'primary_exchange': str(getattr(contract, 'primaryExchange', '') or ''),
    }


def _sanitize_numbers(obj: Any) -> Any:
    """Recursively replace NaN/Inf with None for allow_nan=False JSON."""
    if isinstance(obj, dict):
        return {k: _sanitize_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_numbers(v) for v in obj]
    if isinstance(obj, float):
        return _finite_or_none(obj)
    return obj


def _ticker_to_wire(ticker: Any, contract: Optional[Contract] = None) -> Dict[str, Any]:
    con = contract or getattr(ticker, 'contract', None)
    fields = _contract_fields(con) if con is not None else {
        'instrument_id': 0, 'symbol': '', 'security_type': 'STK',
        'currency': '', 'exchange': '', 'primary_exchange': '',
    }
    return {
        **fields,
        'time': str(getattr(ticker, 'time', '') or '') or None,
        'bid': _finite_or_none(getattr(ticker, 'bid', None)),
        'bid_size': _finite_or_none(getattr(ticker, 'bidSize', None)),
        'ask': _finite_or_none(getattr(ticker, 'ask', None)),
        'ask_size': _finite_or_none(getattr(ticker, 'askSize', None)),
        'last': _finite_or_none(getattr(ticker, 'last', None)),
        'last_size': _finite_or_none(getattr(ticker, 'lastSize', None)),
        'open': _finite_or_none(getattr(ticker, 'open', None)),
        'high': _finite_or_none(getattr(ticker, 'high', None)),
        'low': _finite_or_none(getattr(ticker, 'low', None)),
        'close': _finite_or_none(getattr(ticker, 'close', None)),
        'volume': _finite_or_none(getattr(ticker, 'volume', None)),
        'halted': _finite_or_none(getattr(ticker, 'halted', None)),
    }


def _trade_to_wire(trade: Any) -> Dict[str, Any]:
    order = trade.order
    status = trade.orderStatus
    fields = _contract_fields(trade.contract)
    return {
        **fields,
        'order_id': int(getattr(order, 'orderId', 0) or 0),
        'perm_id': int(getattr(order, 'permId', 0) or 0),
        'action': str(getattr(order, 'action', '') or ''),
        'order_type': str(getattr(order, 'orderType', '') or ''),
        'quantity': float(getattr(order, 'totalQuantity', 0) or 0),
        'limit_price': _finite_or_none(getattr(order, 'lmtPrice', None)),
        'aux_price': _finite_or_none(getattr(order, 'auxPrice', None)),
        'tif': str(getattr(order, 'tif', '') or ''),
        'parent_id': int(getattr(order, 'parentId', 0) or 0) or None,
        'status': str(getattr(status, 'status', '') or ''),
        'filled': _finite_or_none(getattr(status, 'filled', None)) or 0.0,
        'remaining': _finite_or_none(getattr(status, 'remaining', None)) or 0.0,
        'avg_fill_price': _finite_or_none(getattr(status, 'avgFillPrice', None)),
    }


def _position_to_wire(position: Any) -> Dict[str, Any]:
    if isinstance(position, (list, tuple)) and not hasattr(position, 'account'):
        account, contract, qty, avg_cost = position[0], position[1], position[2], position[3]
    else:
        account = position.account
        contract = position.contract
        qty = position.position
        avg_cost = position.avgCost
    fields = _contract_fields(contract)
    return {
        **fields,
        'account': str(account or ''),
        'position': float(qty or 0.0),
        'average_cost': _finite_or_none(avg_cost) or 0.0,
    }


class GetSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instrument_id: int = Field(gt=0)
    delayed: bool = False


class GetSnapshotsBatchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instrument_ids: list[int] = Field(min_length=1, max_length=200)
    delayed: bool = False


class GetMarketDepthRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instrument_id: int = Field(gt=0)
    num_rows: int = Field(default=5, ge=1, le=50)
    is_smart_depth: bool = False


def _ib_account_handler(api: TraderServiceApi):
    def _handler(_body: Dict[str, Any]) -> Dict[str, Any]:
        return {'account_id': str(api.trader.ib_account or '')}
    return _handler


def _fx_rates_handler(api: TraderServiceApi):
    def _handler(_body: Dict[str, Any]) -> Dict[str, Any]:
        return {'rates': api.get_fx_rates() or {}}
    return _handler


def _account_cash_handler(api: TraderServiceApi):
    def _handler(_body: Dict[str, Any]) -> Dict[str, Any]:
        return api.get_account_cash_by_currency() or {}
    return _handler


def _published_contracts_handler(api: TraderServiceApi):
    def _handler(_body: Dict[str, Any]) -> Dict[str, Any]:
        return {'instrument_ids': [int(x) for x in (api.get_published_contracts() or [])]}
    return _handler


def _positions_handler(api: TraderServiceApi):
    def _handler(_body: Dict[str, Any]) -> Dict[str, Any]:
        return {'positions': [_position_to_wire(p) for p in (api.get_positions() or [])]}
    return _handler


def _open_orders_handler(api: TraderServiceApi):
    def _handler(_body: Dict[str, Any]) -> Dict[str, Any]:
        trades_raw = api.get_trades() or {}
        terminal = {'Cancelled', 'Filled', 'Inactive', 'ApiCancelled'}
        rows: List[Dict[str, Any]] = []
        for trade_list in trades_raw.values():
            if not trade_list:
                continue
            trade = trade_list[0]
            status = str(getattr(trade.orderStatus, 'status', '') or '')
            if status in terminal:
                continue
            rows.append(_trade_to_wire(trade))
        return {'orders': rows}
    return _handler


def _trades_handler(api: TraderServiceApi):
    """All book trades (including terminal) — used by ``mmr trades`` / to-market."""
    def _handler(_body: Dict[str, Any]) -> Dict[str, Any]:
        trades_raw = api.get_trades() or {}
        rows = []
        for trade_list in trades_raw.values():
            if trade_list:
                rows.append(_trade_to_wire(trade_list[0]))
        return {'trades': rows}
    return _handler


def _diagnose_portfolio_handler(api: TraderServiceApi):
    def _handler(_body: Dict[str, Any]) -> Dict[str, Any]:
        return _sanitize_numbers(api.diagnose_portfolio_feed() or {})
    return _handler


def _reconcile_handler(api: TraderServiceApi):
    async def _handler(_body: Dict[str, Any]) -> Dict[str, Any]:
        return _sanitize_numbers(await api.reconcile_with_broker())
    return _handler


def _get_snapshot_handler(api: TraderServiceApi):
    async def _handler(parsed: GetSnapshotRequest) -> Dict[str, Any]:
        contract = Contract(conId=int(parsed.instrument_id))
        # Qualify so exchange/currency are filled for the response.
        defs = await api.resolve_contract(contract)
        if defs:
            d = defs[0]
            contract = Contract(
                conId=int(d.conId), symbol=str(d.symbol), secType=str(d.secType),
                exchange=str(d.exchange or 'SMART'),
                primaryExchange=str(d.primaryExchange or ''),
                currency=str(d.currency or ''),
            )
        ticker = await api.get_snapshot(contract, parsed.delayed)
        return {'snapshot': _ticker_to_wire(ticker, contract)}
    return _handler


def _get_snapshots_batch_handler(api: TraderServiceApi):
    async def _handler(parsed: GetSnapshotsBatchRequest) -> Dict[str, Any]:
        contracts = [Contract(conId=int(i)) for i in parsed.instrument_ids]
        raw = await api.get_snapshots_batch(contracts, parsed.delayed)
        snapshots = []
        for row in raw or []:
            if not row:
                continue
            snapshots.append(_sanitize_numbers({
                'instrument_id': int(row.get('conId') or 0),
                'symbol': str(row.get('symbol') or ''),
                'exchange': str(row.get('exchange') or ''),
                'currency': str(row.get('currency') or ''),
                'bid': row.get('bid'), 'ask': row.get('ask'), 'last': row.get('last'),
                'open': row.get('open'), 'high': row.get('high'), 'low': row.get('low'),
                'close': row.get('close'), 'volume': row.get('volume'),
            }))
        return {'snapshots': snapshots}
    return _handler


def _get_market_depth_handler(api: TraderServiceApi):
    async def _handler(parsed: GetMarketDepthRequest) -> Dict[str, Any]:
        defs = await api.resolve_contract(Contract(conId=int(parsed.instrument_id)))
        if not defs:
            return {'depth': {'bids': [], 'asks': []}}
        d = defs[0]
        contract = Contract(
            conId=int(d.conId), symbol=str(d.symbol), secType=str(d.secType),
            exchange=str(d.exchange or 'SMART'),
            primaryExchange=str(d.primaryExchange or ''),
            currency=str(d.currency or ''),
        )
        depth = await api.get_market_depth(contract, parsed.num_rows, parsed.is_smart_depth)
        return {'depth': _sanitize_numbers(depth or {'bids': [], 'asks': []})}
    return _handler


def register_cli_surface(registry: TypedRpcRegistry, api: TraderServiceApi) -> None:
    """Wire CLI/SDK typed query reads onto the trader production registry."""
    registry.register('query', 'get_ib_account', dict, dict, _ib_account_handler(api))
    registry.register('query', 'get_fx_rates', dict, dict, _fx_rates_handler(api))
    registry.register(
        'query', 'get_account_cash_by_currency', dict, dict, _account_cash_handler(api),
    )
    registry.register(
        'query', 'get_published_contracts', dict, dict, _published_contracts_handler(api),
    )
    registry.register('query', 'get_positions', dict, dict, _positions_handler(api))
    registry.register('query', 'get_open_orders', dict, dict, _open_orders_handler(api))
    registry.register('query', 'get_trades', dict, dict, _trades_handler(api))
    registry.register(
        'query', 'diagnose_portfolio_feed', dict, dict, _diagnose_portfolio_handler(api),
    )
    registry.register('query', 'reconcile_with_broker', dict, dict, _reconcile_handler(api))
    registry.register(
        'query', 'get_snapshot', GetSnapshotRequest, dict, _get_snapshot_handler(api),
    )
    registry.register(
        'query', 'get_snapshots_batch', GetSnapshotsBatchRequest, dict,
        _get_snapshots_batch_handler(api),
    )
    registry.register(
        'query', 'get_market_depth', GetMarketDepthRequest, dict,
        _get_market_depth_handler(api),
    )


# Re-export for tests that build portfolio wires via production_api
__all__ = ['register_cli_surface']
