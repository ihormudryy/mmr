"""Exact, per-provider ticker spelling. Never guesses."""

import re

_ALPACA_SYMBOL = re.compile(r'^[A-Z0-9]+(\.[A-Z0-9]+)?$')


def to_alpaca_symbol(symbol: str) -> str:
    """IB writes class shares as 'BRK B'; Alpaca expects 'BRK.B'."""
    if not symbol.isascii():
        raise ValueError(f'not a valid Alpaca stock symbol: {symbol!r}')
    candidate = symbol.strip().upper().replace(' ', '.')
    if not _ALPACA_SYMBOL.match(candidate):
        raise ValueError(f'not a valid Alpaca stock symbol: {symbol!r}')
    return candidate
