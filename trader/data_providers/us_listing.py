"""REST history providers look bars up by ticker and know no conId, so only a US listing may be stored under a conId.

Alpaca, Massive and TwelveData are all used as US-only here. TwelveData can take an exchange, but the repo has no
verified map from IB primaryExchange to its codes, so it is refused like the others. A future REST history source
is refused too until someone proves its listing rules.
"""

# The listing exchange (IB primaryExchange). SMART is a route, not a listing, and never counts.
US_PRIMARY_EXCHANGES = frozenset({'NYSE', 'NASDAQ', 'ARCA', 'AMEX', 'BATS', 'IEX', 'ISLAND'})


def non_us_refusal_code(source: str) -> str:
    """`ALPACA_NON_US_INSTRUMENT`, `MASSIVE_NON_US_INSTRUMENT` or `TWELVEDATA_NON_US_INSTRUMENT`."""
    return f'{source.upper()}_NON_US_INSTRUMENT'


class NonUsInstrumentError(ValueError):
    """The instrument is not known to be US-listed; a ticker lookup could return another market's bars."""

    def __init__(self, source: str, security) -> None:
        self.code = non_us_refusal_code(source)
        listed_on = (getattr(security, 'primaryExchange', '') or '').strip().upper() or 'unknown'
        super().__init__(
            f'{self.code}: {security.symbol} (conId {security.conId}) is listed on {listed_on}, '
            f'not a US exchange; {source} looks bars up by ticker and could return a US instrument. Use source ib.')


def require_us_listing(source: str, security) -> None:
    primary_exchange = (getattr(security, 'primaryExchange', '') or '').strip().upper()
    if primary_exchange not in US_PRIMARY_EXCHANGES:
        raise NonUsInstrumentError(source, security)
