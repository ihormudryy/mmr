"""Movers computed locally where no free provider ranks them."""

import pandas as pd

from trader.data_providers.capabilities import MOVER_COLUMNS, sort_movers
from trader.data_providers.errors import CapabilityNotSupported, ProviderError
from trader.data_providers.frankfurter import percent_change, rounded_change

PROVIDER = 'computed_fx'

MAJOR_FX_PAIRS: tuple[tuple[str, str], ...] = (
    ('EUR', 'USD'), ('USD', 'JPY'), ('GBP', 'USD'), ('USD', 'CHF'), ('AUD', 'USD'), ('USD', 'CAD'), ('NZD', 'USD'),
    ('EUR', 'GBP'), ('EUR', 'JPY'), ('GBP', 'JPY'),
)


class ComputedFxMovers:
    """Day-over-day change of the FX majors between the two latest ECB daily rates (daily, not live)."""

    markets = frozenset({'forex'})

    def __init__(self, forex):
        self._forex = forex

    def movers(self, market: str, direction: str) -> pd.DataFrame:
        if market not in self.markets:
            raise CapabilityNotSupported(f'{market} movers', PROVIDER, ['massive'])
        rates = self._forex.daily_rates({currency for pair in MAJOR_FX_PAIRS for currency in pair})
        note = f'ECB daily rates {rates.latest_date} vs {rates.previous_date} (daily, not live)'
        rows = []
        for base, quote in MAJOR_FX_PAIRS:
            previous, latest = rates.pair(base, quote)
            rows.append({'ticker': f'{base}{quote}', 'name': f'{base}/{quote}', 'close': latest,
                         'volume': float('nan'), 'change': rounded_change(previous, latest),
                         'change_pct': percent_change(previous, latest),
                         'provider': PROVIDER, 'note': note})
        frame = sort_movers(pd.DataFrame(rows, columns=list(MOVER_COLUMNS)), direction)
        frame.attrs['as_of'] = rates.latest_date
        return frame


INDEX_PROXY_ETFS: tuple[tuple[str, str], ...] = (
    ('SPY', 'S&P 500'),
    ('QQQ', 'Nasdaq-100'),
    ('DIA', 'Dow Jones Industrial Average'),
    ('IWM', 'Russell 2000'),
    ('XLK', 'S&P 500 Technology sector'),
    ('XLF', 'S&P 500 Financials sector'),
    ('XLE', 'S&P 500 Energy sector'),
    ('XLV', 'S&P 500 Health Care sector'),
    ('XLI', 'S&P 500 Industrials sector'),
    ('XLY', 'S&P 500 Consumer Discretionary sector'),
    ('XLP', 'S&P 500 Consumer Staples sector'),
    ('XLU', 'S&P 500 Utilities sector'),
    ('XLB', 'S&P 500 Materials sector'),
    ('XLRE', 'S&P 500 Real Estate sector'),
    ('XLC', 'S&P 500 Communication Services sector'),
)


class EtfProxyMovers:
    """Index movers approximated by index-tracking ETFs. Rows are ETFs, never the indices themselves."""

    markets = frozenset({'indices'})

    def __init__(self, quotes):
        self._quotes = quotes

    def movers(self, market: str, direction: str) -> pd.DataFrame:
        if market not in self.markets:
            raise CapabilityNotSupported(f'{market} movers', 'etf_proxy', ['massive'])
        quotes = self._quotes.quotes([etf for etf, _ in INDEX_PROXY_ETFS])
        if all(quote['error'] for quote in quotes):
            raise ProviderError(f"etf_proxy: no ETF quote available ({quotes[0]['error']})")
        rows = [_proxy_row(etf, index, quote) for (etf, index), quote in zip(INDEX_PROXY_ETFS, quotes)]
        return sort_movers(pd.DataFrame(rows, columns=list(MOVER_COLUMNS)), direction)


def _proxy_row(etf: str, index: str, quote: dict) -> dict:
    note = f'ETF proxy for {index}; IEX prices'
    if quote['error']:
        note = f"{note}; {quote['error']}"
    elif quote['time']:
        note = f"{note} as of {quote['time'][:19]}Z"
    # IEX volume is a few percent of the market, so it is not shown as volume.
    return {'ticker': etf, 'name': f'{index} (ETF proxy)', 'close': quote['last'], 'volume': float('nan'),
            'change': quote['change'], 'change_pct': quote['change_pct'], 'provider': 'etf_proxy', 'note': note}
