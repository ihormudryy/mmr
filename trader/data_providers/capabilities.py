"""What a market-data provider can do, as structural interfaces."""

import datetime as dt
import math
import numbers
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

import pandas as pd

from trader.data_providers.option_symbols import OptionSymbol
from trader.objects import BarSize


class Capability(str, Enum):
    HISTORY = 'history'
    QUOTES = 'quotes'
    MOVERS = 'movers'
    NEWS = 'news'
    IDEAS = 'ideas'
    FOREX = 'forex'
    MOVERS_FOREX = 'movers_forex'
    MOVERS_INDICES = 'movers_indices'
    OPTIONS = 'options'


HISTORY_COLUMNS: tuple[str, ...] = (
    'open', 'high', 'low', 'close', 'volume', 'average', 'bar_count', 'bar_size', 'what_to_show',
)


@runtime_checkable
class HistoryProvider(Protocol):
    def get_history(
        self,
        ticker: str,
        bar_size: BarSize,
        start_date: dt.datetime,
        end_date: dt.datetime,
        timezone: str = 'US/Eastern',
    ) -> pd.DataFrame:
        """Bars for whole days start_date..end_date inclusive, indexed by tz-aware `date`."""
        ...


QUOTE_FIELDS: tuple[str, ...] = (
    'symbol', 'time', 'last', 'bid', 'ask', 'bid_size', 'ask_size', 'open', 'high', 'low', 'close',
    'volume', 'previous_close', 'change', 'change_pct', 'exchange', 'currency', 'name', 'feed', 'error',
)
_QUOTE_TEXT_FIELDS = frozenset({'symbol', 'time', 'exchange', 'currency', 'name', 'feed', 'error'})

MOVER_COLUMNS: tuple[str, ...] = (
    'ticker', 'name', 'close', 'volume', 'change', 'change_pct', 'provider', 'note',
)

NEWS_FIELDS: tuple[str, ...] = (
    'id', 'published', 'title', 'summary', 'url', 'author', 'source', 'tickers', 'sentiment', 'insights',
)
_NEWS_LIST_FIELDS = frozenset({'tickers', 'insights'})


def make_quote(symbol: str, **fields: Any) -> dict:
    unknown = set(fields) - set(QUOTE_FIELDS)
    if unknown:
        raise TypeError(f'unknown quote field(s): {sorted(unknown)}')
    quote = {name: ('' if name in _QUOTE_TEXT_FIELDS else float('nan')) for name in QUOTE_FIELDS}
    quote.update(fields)
    quote['symbol'] = symbol.strip().upper()
    return quote


def make_news_item(**fields: Any) -> dict:
    unknown = set(fields) - set(NEWS_FIELDS)
    if unknown:
        raise TypeError(f'unknown news field(s): {sorted(unknown)}')
    item = {name: ([] if name in _NEWS_LIST_FIELDS else '') for name in NEWS_FIELDS}
    item.update(fields)
    return item


@runtime_checkable
class QuoteProvider(Protocol):
    def quotes(self, symbols: Sequence[str]) -> list[dict]:
        """One make_quote() dict per requested symbol, in request order; failures set `error`."""
        ...


@runtime_checkable
class MoversProvider(Protocol):
    markets: frozenset

    def movers(self, market: str, direction: str) -> pd.DataFrame:
        """Frame starting with MOVER_COLUMNS, sorted by change_pct for `direction`."""
        ...


@runtime_checkable
class NewsProvider(Protocol):
    def news(self, ticker: Optional[str], limit: int) -> list[dict]:
        """Newest first, at most `limit` make_news_item() dicts; ticker None means general news."""
        ...


def sort_movers(frame: pd.DataFrame, direction: str) -> pd.DataFrame:
    """Order a movers frame: MOVER_COLUMNS first, biggest move first for `direction`."""
    for column in MOVER_COLUMNS:
        if column not in frame.columns:
            frame[column] = '' if column in ('ticker', 'name', 'provider', 'note') else float('nan')
    extras = [c for c in frame.columns if c not in MOVER_COLUMNS]
    frame = frame[list(MOVER_COLUMNS) + extras]
    return frame.sort_values('change_pct', ascending=(direction == 'losers'), na_position='last') \
                .reset_index(drop=True)


@dataclass(frozen=True)
class Discovery:
    """Scanner candidates from one provider, plus a user-facing notice (may be empty)."""
    candidates: list
    notice: str = ''


@runtime_checkable
class ScanSource(Protocol):
    name: str
    supports_fundamentals: bool

    def discover(self, source: str, tickers, universe_symbols, use_market_scan: bool) -> Discovery:
        ...

    def indicators(self, tickers: list, needed: list) -> dict:
        ...

    def names(self, tickers: list) -> dict:
        ...

    def fundamentals(self, tickers: list) -> dict:
        ...

    def news(self, tickers: list) -> dict:
        ...


FX_RATE_FIELDS: tuple[str, ...] = (
    'pair', 'base', 'quote', 'last', 'bid', 'ask', 'open', 'high', 'low', 'close',
    'previous_close', 'change', 'change_pct', 'volume', 'as_of', 'source', 'note',
)
FX_RATES_COLUMNS: tuple[str, ...] = (
    'pair', 'last', 'previous_close', 'change', 'change_pct', 'as_of', 'source', 'note',
)
FX_CONVERSION_FIELDS: tuple[str, ...] = (
    'from', 'to', 'amount', 'converted', 'rate', 'bid', 'ask', 'as_of', 'source', 'note',
)
_FX_TEXT_FIELDS = frozenset({'pair', 'base', 'quote', 'from', 'to', 'as_of', 'source', 'note'})
_FX_RATE_IDENTITY = frozenset({'pair', 'base', 'quote'})
_FX_CONVERSION_IDENTITY = frozenset({'from', 'to', 'amount'})


def _fx_record(names: tuple[str, ...]) -> dict:
    return {name: ('' if name in _FX_TEXT_FIELDS else float('nan')) for name in names}


def round_significant(value: float, digits: int = 10) -> float:
    """Drop float noise such as 0.0073000000000001 (NaN stays NaN)."""
    return float(f'{value:.{digits}g}')


def make_fx_rate(base: str, quote: str, **fields: Any) -> dict:
    unknown = set(fields) - (set(FX_RATE_FIELDS) - _FX_RATE_IDENTITY)
    if unknown:
        raise TypeError(f'unknown fx rate field(s): {sorted(unknown)}')
    record = _fx_record(FX_RATE_FIELDS)
    record.update(fields)
    base, quote = base.strip().upper(), quote.strip().upper()
    record.update(pair=f'{base}/{quote}', base=base, quote=quote)
    return record


def make_fx_conversion(base: str, quote: str, amount: float, **fields: Any) -> dict:
    unknown = set(fields) - (set(FX_CONVERSION_FIELDS) - _FX_CONVERSION_IDENTITY)
    if unknown:
        raise TypeError(f'unknown fx conversion field(s): {sorted(unknown)}')
    record = _fx_record(FX_CONVERSION_FIELDS)
    record.update(fields)
    record.update({'from': base.strip().upper(), 'to': quote.strip().upper(), 'amount': float(amount)})
    return record


def sort_fx_rates(frame: pd.DataFrame) -> pd.DataFrame:
    """Order a forex rates frame: FX_RATES_COLUMNS first, biggest gain first."""
    for column in FX_RATES_COLUMNS:
        if column not in frame.columns:
            frame[column] = '' if column in _FX_TEXT_FIELDS else float('nan')
    extras = [c for c in frame.columns if c not in FX_RATES_COLUMNS]
    frame = frame[list(FX_RATES_COLUMNS) + extras]
    return frame.sort_values('change_pct', ascending=False, na_position='last').reset_index(drop=True)


@runtime_checkable
class ForexProvider(Protocol):
    def rate(self, base: str, quote: str) -> dict:
        """One make_fx_rate() dict for base/quote (codes already validated and upper case)."""
        ...

    def rates(self, base: str, symbols: Optional[Sequence[str]]) -> pd.DataFrame:
        """Frame starting with FX_RATES_COLUMNS: base against each symbol (None = every symbol the source has)."""
        ...

    def convert(self, base: str, quote: str, amount: float) -> dict:
        """One make_fx_conversion() dict."""
        ...


# The registry keeps one default per capability, and movers defaults differ per market.
_MOVERS_CAPABILITY_BY_MARKET = {'forex': Capability.MOVERS_FOREX, 'indices': Capability.MOVERS_INDICES}


def movers_capability(market: str) -> Capability:
    return _MOVERS_CAPABILITY_BY_MARKET.get(market, Capability.MOVERS)


OPTION_FIELDS: tuple[str, ...] = (
    'ticker', 'type', 'strike', 'expiration', 'bid', 'ask', 'mid', 'last', 'volume', 'open_interest',
    'iv', 'delta', 'gamma', 'theta', 'vega', 'break_even', 'underlying_price',
    'underlying', 'quote_time', 'last_time', 'provider', 'feed',
)
_OPTION_TEXT_FIELDS = frozenset({
    'ticker', 'type', 'expiration', 'underlying', 'quote_time', 'last_time', 'provider', 'feed',
})
_OPTION_DERIVED_FIELDS = frozenset({'ticker', 'type', 'strike', 'expiration', 'mid'})


def option_mid(bid: float, ask: float) -> float:
    """Midpoint of a sane quote; NaN for missing, zero-ask or crossed quotes."""
    if not (math.isfinite(bid) and math.isfinite(ask)) or bid < 0 or ask <= 0 or bid > ask:
        return float('nan')
    return (bid + ask) / 2


def make_option_row(option: OptionSymbol, **fields: Any) -> dict:
    unknown = set(fields) - set(OPTION_FIELDS)
    if unknown:
        raise TypeError(f'unknown option field(s): {sorted(unknown)}')
    derived = set(fields) & _OPTION_DERIVED_FIELDS
    if derived:
        raise TypeError(f'option field(s) {sorted(derived)} come from the option symbol and quote')
    row = {name: ('' if name in _OPTION_TEXT_FIELDS else float('nan')) for name in OPTION_FIELDS}
    row.update({name: _option_value(name, value) for name, value in fields.items()})
    row.update(ticker=option.occ, type=option.contract_type, strike=option.strike,
               expiration=option.expiration.isoformat())
    row['mid'] = option_mid(row['bid'], row['ask'])
    return row


def _option_value(name: str, value: Any) -> Any:
    if name in _OPTION_TEXT_FIELDS:
        return '' if value is None else value
    if value is None:
        return float('nan')
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f'option field {name!r} must be a number or None, got {value!r}')
    return value


def sort_option_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: (row['type'], row['strike']))


@runtime_checkable
class OptionsProvider(Protocol):
    def expirations(self, underlying: str) -> list[str]:
        """Sorted YYYY-MM-DD expirations that have not passed."""
        ...

    def chain(self, underlying: str, expiration: str, contract_type: Optional[str] = None,
              strike_min: Optional[float] = None, strike_max: Optional[float] = None) -> list[dict]:
        """make_option_row() dicts for one expiration, sorted by sort_option_rows()."""
        ...

    def contract(self, option: OptionSymbol) -> dict:
        """One make_option_row() dict for an exact contract; an unknown contract raises."""
        ...
