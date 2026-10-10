"""Alpaca fetches bars by ticker and knows no conId, so only a US listing may be stored under a conId."""

ALPACA_SOURCE = 'alpaca'
ALPACA_NON_US_INSTRUMENT = 'ALPACA_NON_US_INSTRUMENT'

# The listing exchange (IB primaryExchange). SMART is a route, not a listing, and never counts.
US_PRIMARY_EXCHANGES = frozenset({'NYSE', 'NASDAQ', 'ARCA', 'AMEX', 'BATS', 'IEX', 'ISLAND'})


class NonUsInstrumentError(ValueError):
    """The instrument is not known to be US-listed; an Alpaca ticker lookup could return another market's bars."""

    code = ALPACA_NON_US_INSTRUMENT


def require_us_listing(security) -> None:
    primary_exchange = (getattr(security, 'primaryExchange', '') or '').strip().upper()
    if primary_exchange in US_PRIMARY_EXCHANGES:
        return
    listed_on = primary_exchange or 'unknown'
    raise NonUsInstrumentError(
        f'{ALPACA_NON_US_INSTRUMENT}: {security.symbol} (conId {security.conId}) is listed on {listed_on}, '
        f'not a US exchange; Alpaca looks bars up by ticker and could return a US instrument. Use source ib.')
