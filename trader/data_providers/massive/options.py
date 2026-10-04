"""Option expirations, chains and contract snapshots from Massive (Polygon): OPRA data, paid plan."""

from typing import Callable, Optional

from massive.exceptions import AuthError, BadResponse
from urllib3.exceptions import HTTPError, MaxRetryError

from trader.data_providers.capabilities import make_option_row, sort_option_rows
from trader.data_providers.errors import ProviderEntitlementError, ProviderError, ProviderRateLimited
from trader.data_providers.option_symbols import (
    OptionSymbol, parse_provider_option_symbol, to_massive_option_ticker,
)

FEED = 'opra'
CONTRACTS_PAGE_LIMIT = 1000


class MassiveOptions:
    def __init__(self, client):
        self._client = client

    def expirations(self, underlying: str) -> list[str]:
        contracts = _call_massive('expirations', lambda: list(self._client.list_options_contracts(
            underlying_ticker=underlying.strip().upper(), expired=False, limit=CONTRACTS_PAGE_LIMIT,
            sort='expiration_date', order='asc')))
        return sorted({contract.expiration_date for contract in contracts if contract.expiration_date})

    def chain(self, underlying: str, expiration: str, contract_type: Optional[str] = None,
              strike_min: Optional[float] = None, strike_max: Optional[float] = None) -> list[dict]:
        wanted_type = _contract_type(contract_type)
        symbol = underlying.strip().upper()
        snaps = _call_massive('chain', lambda: list(self._client.list_snapshot_options_chain(
            underlying_asset=symbol, params={'expiration_date': expiration})))
        rows = []
        for snap in snaps:
            if not snap.details or not snap.details.ticker:
                continue
            option = parse_provider_option_symbol('massive', snap.details.ticker)
            if wanted_type and option.contract_type != wanted_type:
                continue
            if strike_min is not None and option.strike < strike_min:
                continue
            if strike_max is not None and option.strike > strike_max:
                continue
            rows.append(_option_row(option, snap, symbol))
        return sort_option_rows(rows)

    def contract(self, option: OptionSymbol) -> dict:
        snap = _call_massive('snapshot', lambda: self._client.get_snapshot_option(
            underlying_asset=option.root, option_contract=to_massive_option_ticker(option)))
        underlying = getattr(snap.underlying_asset, 'ticker', None) or ''
        return _option_row(option, snap, underlying)


def _option_row(option: OptionSymbol, snap, underlying: str) -> dict:
    greeks = snap.greeks
    return make_option_row(
        option,
        bid=_number(getattr(snap.last_quote, 'bid', None)),
        ask=_number(getattr(snap.last_quote, 'ask', None)),
        last=_number(getattr(snap.last_trade, 'price', None)),
        volume=_number(getattr(snap.day, 'volume', None)),
        open_interest=_number(snap.open_interest),
        iv=_number(snap.implied_volatility) * 100,
        delta=_number(getattr(greeks, 'delta', None)),
        gamma=_number(getattr(greeks, 'gamma', None)),
        theta=_number(getattr(greeks, 'theta', None)),
        vega=_number(getattr(greeks, 'vega', None)),
        break_even=_number(snap.break_even_price),
        underlying=underlying,
        underlying_price=_number(getattr(snap.underlying_asset, 'price', None)),
        provider='massive',
        feed=FEED,
    )


def _contract_type(contract_type: Optional[str]) -> Optional[str]:
    if contract_type is None:
        return None
    normalised = contract_type.strip().lower() if isinstance(contract_type, str) and contract_type.isascii() else None
    if normalised not in ('call', 'put'):
        raise ValueError(f"contract_type must be 'call' or 'put', got {contract_type!r}")
    return normalised


def _number(value) -> float:
    return float('nan') if value is None else float(value)


def _call_massive(what: str, request: Callable):
    try:
        return request()
    except AuthError as ex:
        raise ProviderEntitlementError(
            f'massive refused options {what}: {ex}; use --source alpaca') from ex
    except BadResponse as ex:
        if 'NOT_AUTHORIZED' in str(ex):
            raise ProviderEntitlementError(
                f'massive refused options {what} (NOT_AUTHORIZED: this Massive plan has no options data); '
                'use --source alpaca') from ex
        raise ProviderError(f'massive options {what} failed: {ex}') from ex
    except HTTPError as ex:
        if _retries_exhausted_on_429(ex):
            raise ProviderRateLimited(f'massive rate-limited options {what}: {ex}') from ex
        raise ProviderError(f'massive options {what} failed: {ex}') from ex


def _retries_exhausted_on_429(ex: HTTPError) -> bool:
    return isinstance(ex, MaxRetryError) and 'too many 429' in str(ex.reason)
