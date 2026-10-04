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


_CURRENCY = re.compile(r'[A-Z]{3}')


def parse_currency(code: str) -> str:
    stripped = str(code).strip()
    # Check before upper(): it maps some non-ASCII letters (e.g. 'ß') onto ASCII ones.
    if not (stripped.isascii() and _CURRENCY.fullmatch(stripped.upper())):
        raise ValueError(f'not a 3-letter currency code: {code!r}')
    return stripped.upper()


def parse_forex_codes(base: str, quote: str) -> tuple[str, str]:
    base, quote = parse_currency(base), parse_currency(quote)
    if base == quote:
        raise ValueError(f'base and quote are the same currency: {base}')
    return base, quote


def parse_forex_pair(pair: str) -> tuple[str, str]:
    """'EURUSD', 'EUR/USD' or 'C:EURUSD' -> ('EUR', 'USD'). Anything else is an error, never a guess."""
    stripped = str(pair).strip()
    error = ValueError(f'not a currency pair: {pair!r}; use EURUSD, EUR/USD or C:EURUSD')
    if not stripped.isascii():
        raise error
    text = stripped.upper()
    has_prefix = text.startswith('C:')
    text = text.removeprefix('C:')
    if not has_prefix and len(text) == 7 and text[3] == '/':
        text = text[:3] + text[4:]
    if len(text) != 6:
        raise error
    return parse_forex_codes(text[:3], text[3:])
