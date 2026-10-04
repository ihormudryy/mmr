"""Option expirations and chains from Alpaca's free indicative feed.

The indicative feed is not the OPRA NBBO: its quotes are derived and its trades
delayed, and greeks / implied volatility exist only for liquid contracts. Every
row says feed='indicative'. Contract lists come from the paper trading API.
"""

import datetime as dt
import math
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from trader.data_providers.alpaca._numbers import number_or_nan
from trader.data_providers.alpaca.quotes import AlpacaQuotes
from trader.data_providers.capabilities import make_option_row, sort_option_rows
from trader.data_providers.errors import ProviderError
from trader.data_providers.option_symbols import (
    OptionSymbol, parse_expiration_date, parse_provider_option_symbol, to_alpaca_option_symbol,
)
from trader.data_providers.symbols import to_alpaca_symbol

CONTRACTS_PATH = '/v2/options/contracts'
SNAPSHOTS_PATH = '/v1beta1/options/snapshots'
CHAIN_SNAPSHOTS_PATH = '/v1beta1/options/snapshots/{underlying}'
FEED = 'indicative'
# Verified 2026-10-04: contracts need limit < 10000; snapshots allow at most 1000 per page.
CONTRACTS_PAGE_LIMIT = 5000
SNAPSHOTS_PAGE_LIMIT = 1000
_ET = ZoneInfo('America/New_York')


def _today_et() -> dt.date:
    return dt.datetime.now(_ET).date()


class AlpacaOptions:
    def __init__(self, data_client, trading_client, today: Callable[[], dt.date] = _today_et):
        self._data = data_client
        self._trading = trading_client
        self._today = today

    def expirations(self, underlying: str) -> list[str]:
        # Without an expiration filter Alpaca returns only the next week's contracts.
        params = {'underlying_symbols': to_alpaca_symbol(underlying), 'status': 'active',
                  'expiration_date_gte': self._today().isoformat(), 'limit': CONTRACTS_PAGE_LIMIT}
        return sorted({contract['expiration_date']
                       for page in self._trading.paginate(CONTRACTS_PATH, params)
                       for contract in page.get('option_contracts') or []})

    def chain(self, underlying: str, expiration: str, contract_type: Optional[str] = None,
              strike_min: Optional[float] = None, strike_max: Optional[float] = None) -> list[dict]:
        symbol = to_alpaca_symbol(underlying)
        filters = _chain_filters(expiration, contract_type, strike_min, strike_max)
        # Contracts first: that endpoint rejects an unknown underlying (HTTP 422),
        # the snapshot endpoint just returns nothing.
        open_interest = self._open_interest(symbol, filters)
        underlying_price = self._underlying_price(symbol)
        snapshots = self._snapshots(symbol, filters)
        # A listed contract can lack a snapshot (no quote yet); it still gets a row.
        # Filtered again here: the snapshot endpoint's own filter support is unverified.
        options = sorted((option for option in set(open_interest) | set(snapshots) if _matches(option, filters)),
                         key=lambda option: option.occ)
        rows = [_option_row(option, snapshots.get(option, {}), symbol, underlying_price,
                            open_interest.get(option, float('nan')))
                for option in options]
        return sort_option_rows(rows)

    def contract(self, option: OptionSymbol) -> dict:
        alpaca_symbol = to_alpaca_option_symbol(option)
        # Contract details name the real underlying (BRKB options deliver BRK.B) and carry
        # open interest; an unknown contract is HTTP 404 here.
        details = self._trading.get_json(f'{CONTRACTS_PATH}/{alpaca_symbol}', {})
        underlying = details.get('underlying_symbol')
        if not underlying:
            raise ProviderError(f'alpaca contract {alpaca_symbol} has no underlying_symbol')
        page = self._data.get_json(SNAPSHOTS_PATH, {'symbols': alpaca_symbol, 'feed': FEED})
        snapshot = (page.get('snapshots') or {}).get(alpaca_symbol) or {}
        return _option_row(option, snapshot, underlying, self._underlying_price(underlying),
                           number_or_nan(details, 'open_interest'))

    def _open_interest(self, symbol: str, filters: dict) -> dict[OptionSymbol, float]:
        params = {**filters, 'underlying_symbols': symbol, 'limit': CONTRACTS_PAGE_LIMIT}
        return {parse_provider_option_symbol('alpaca', contract['symbol']): number_or_nan(contract, 'open_interest')
                for page in self._trading.paginate(CONTRACTS_PATH, params)
                for contract in page.get('option_contracts') or []}

    def _snapshots(self, symbol: str, filters: dict) -> dict[OptionSymbol, dict]:
        params = {**filters, 'feed': FEED, 'limit': SNAPSHOTS_PAGE_LIMIT}
        return {parse_provider_option_symbol('alpaca', key): snapshot
                for page in self._data.paginate(CHAIN_SNAPSHOTS_PATH.format(underlying=symbol), params)
                for key, snapshot in (page.get('snapshots') or {}).items()}

    def _underlying_price(self, symbol: str) -> float:
        (quote,) = AlpacaQuotes(self._data).quotes([symbol])
        return quote['last']


def _chain_filters(expiration: str, contract_type: Optional[str],
                   strike_min: Optional[float], strike_max: Optional[float]) -> dict:
    parse_expiration_date(expiration)
    filters = {'expiration_date': expiration}
    wanted_type = _contract_type(contract_type)
    if wanted_type:
        filters['type'] = wanted_type
    if strike_min is not None:
        filters['strike_price_gte'] = _strike_bound('strike_min', strike_min)
    if strike_max is not None:
        filters['strike_price_lte'] = _strike_bound('strike_max', strike_max)
    return filters


def _matches(option: OptionSymbol, filters: dict) -> bool:
    return (option.expiration.isoformat() == filters['expiration_date']
            and option.contract_type == filters.get('type', option.contract_type)
            and option.strike >= float(filters.get('strike_price_gte', option.strike))
            and option.strike <= float(filters.get('strike_price_lte', option.strike)))


def _contract_type(contract_type: Optional[str]) -> Optional[str]:
    if contract_type is None:
        return None
    normalised = contract_type.strip().lower() if isinstance(contract_type, str) and contract_type.isascii() else None
    if normalised not in ('call', 'put'):
        raise ValueError(f"contract_type must be 'call' or 'put', got {contract_type!r}")
    return normalised


def _strike_bound(name: str, value: float) -> str:
    try:
        bound = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be a number, got {value!r}') from None
    if not math.isfinite(bound):
        raise ValueError(f'{name} must be a finite number, got {value!r}')
    return str(bound)


def _option_row(option: OptionSymbol, snapshot: dict, underlying: str,
                underlying_price: float, open_interest: float) -> dict:
    quote = snapshot.get('latestQuote') or {}
    trade = snapshot.get('latestTrade') or {}
    greeks = snapshot.get('greeks') or {}
    return make_option_row(
        option,
        bid=number_or_nan(quote, 'bp'),
        ask=number_or_nan(quote, 'ap'),
        last=number_or_nan(trade, 'p'),
        volume=_session_volume(snapshot.get('dailyBar'), quote),
        open_interest=open_interest,
        iv=number_or_nan(snapshot, 'impliedVolatility') * 100,
        delta=number_or_nan(greeks, 'delta'),
        gamma=number_or_nan(greeks, 'gamma'),
        theta=number_or_nan(greeks, 'theta'),
        vega=number_or_nan(greeks, 'vega'),
        underlying=underlying,
        underlying_price=underlying_price,
        quote_time=quote.get('t', ''),
        last_time=trade.get('t', ''),
        provider='alpaca',
        feed=FEED,
    )


def _session_volume(daily_bar: Optional[dict], quote: dict) -> float:
    """Volume in the latest quote's session. A daily bar from an older session means no trades since;
    one from a newer session, or without a timestamp, cannot be matched to the quote."""
    if not daily_bar or not daily_bar.get('t') or not quote.get('t'):
        return float('nan')
    bar_date, quote_date = _et_date(daily_bar['t']), _et_date(quote['t'])
    if bar_date == quote_date:
        return number_or_nan(daily_bar, 'v')
    return 0.0 if bar_date < quote_date else float('nan')


def _et_date(timestamp: str) -> dt.date:
    return dt.datetime.fromisoformat(timestamp).astimezone(_ET).date()
