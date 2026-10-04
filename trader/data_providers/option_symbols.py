"""OCC option symbols in the spellings our providers use. Never guesses.

Massive writes `O:AAPL261120C00250000`, Alpaca `AAPL261120C00250000`: a root
(1-6 letters, plus one digit for adjusted contracts), YYMMDD expiry, C or P,
and the strike in thousandths as 8 digits.
"""

import datetime as dt
import math
import numbers
import re
from dataclasses import dataclass

from trader.data_providers.errors import ProviderError

_OPTION_SYMBOL = re.compile(
    r'(?:O:)?(?P<root>[A-Z]{1,6}[0-9]?)(?P<date>[0-9]{6})(?P<right>[CP])(?P<strike>[0-9]{8})')
_ROOT = re.compile(r'[A-Z]{1,6}[0-9]?')
_ALPACA_ROOT = re.compile(r'[A-Z]{1,5}[0-9]?')
_ISO_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_MAX_STRIKE_THOUSANDTHS = 99_999_999
# OCC writes the year as two digits; strptime('%y') reads 69-99 as 19xx and 00-68 as 20xx.
_EARLIEST_YEAR, _LATEST_YEAR = 1969, 2068
_EXPECTED_SHAPE = 'expected ROOT + YYMMDD + C|P + 8-digit strike, e.g. AAPL261120C00250000 or O:AAPL261120C00250000'


@dataclass(frozen=True)
class OptionSymbol:
    root: str
    expiration: dt.date
    right: str
    strike_thousandths: int

    @property
    def strike(self) -> float:
        return self.strike_thousandths / 1000

    @property
    def contract_type(self) -> str:
        return 'call' if self.right == 'C' else 'put'

    @property
    def occ(self) -> str:
        return f'{self.root}{self.expiration:%y%m%d}{self.right}{self.strike_thousandths:08d}'


def parse_option_symbol(text: str) -> OptionSymbol:
    # Check ASCII before upper-casing: 'ß'.upper() == 'SS' and 'ı'.upper() == 'I' would build a wrong symbol.
    if not isinstance(text, str) or not text.isascii():
        raise ValueError(f'Cannot parse option symbol {text!r}: {_EXPECTED_SHAPE}')
    match = _OPTION_SYMBOL.fullmatch(text.strip().upper())
    if not match:
        raise ValueError(f'Cannot parse option symbol {text!r}: {_EXPECTED_SHAPE}')
    try:
        expiration = dt.datetime.strptime(match['date'], '%y%m%d').date()
    except ValueError:
        raise ValueError(f"Cannot parse option symbol {text!r}: {match['date']} is not a calendar date") from None
    strike_thousandths = int(match['strike'])
    if strike_thousandths == 0:
        raise ValueError(f'Cannot parse option symbol {text!r}: strike is zero')
    return OptionSymbol(match['root'], expiration, match['right'], strike_thousandths)


def parse_provider_option_symbol(provider: str, text: str) -> OptionSymbol:
    try:
        return parse_option_symbol(text)
    except ValueError as ex:
        raise ProviderError(f'{provider} returned an option symbol MMR cannot parse: {text!r}') from ex


def parse_expiration_date(text: str) -> dt.date:
    if not isinstance(text, str) or not _ISO_DATE.fullmatch(text):
        raise ValueError(f'expiration must be a YYYY-MM-DD date, got {text!r}')
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError(f'expiration must be a real YYYY-MM-DD date, got {text!r}') from None


def build_option_symbol(root: str, expiration: str | dt.date, strike: float, right: str) -> OptionSymbol:
    root = _ascii_upper('option root', root)
    if not _ROOT.fullmatch(root):
        raise ValueError(f'not a valid option root: {root!r}')
    right = _ascii_upper('option right', right)
    if right not in ('C', 'P'):
        raise ValueError(f"option right must be 'C' or 'P', got {right!r}")
    return OptionSymbol(root, _expiration_date(expiration), right, _strike_thousandths(strike))


def _ascii_upper(what: str, value: str) -> str:
    if not isinstance(value, str) or not value.isascii():
        raise ValueError(f'{what} must be ASCII text, got {value!r}')
    return value.strip().upper()


def _expiration_date(expiration: str | dt.date) -> dt.date:
    if isinstance(expiration, str):
        expiration = parse_expiration_date(expiration)
    if isinstance(expiration, dt.datetime) or not isinstance(expiration, dt.date):
        raise ValueError(f'expiration must be a YYYY-MM-DD string or a date, got {expiration!r}')
    if not _EARLIEST_YEAR <= expiration.year <= _LATEST_YEAR:
        raise ValueError(f'expiration {expiration.isoformat()} is outside what a two-digit OCC year can name')
    return expiration


def _strike_thousandths(strike: float) -> int:
    if isinstance(strike, bool) or not isinstance(strike, numbers.Real):
        raise ValueError(f'strike must be a number, got {strike!r}')
    if not math.isfinite(strike) or strike <= 0:
        raise ValueError(f'strike must be a positive number, got {strike!r}')
    thousandths = round(strike * 1000)
    if abs(thousandths - strike * 1000) > 1e-6:
        raise ValueError(f'strike {strike!r} is not a multiple of 0.001')
    if thousandths > _MAX_STRIKE_THOUSANDTHS:
        raise ValueError(f'strike {strike!r} does not fit an OCC symbol')
    return thousandths


def to_alpaca_option_symbol(option: OptionSymbol) -> str:
    if not _ALPACA_ROOT.fullmatch(option.root):
        raise ValueError(f'alpaca accepts option roots of at most 5 letters, got {option.root!r}')
    return option.occ


def to_massive_option_ticker(option: OptionSymbol) -> str:
    return f'O:{option.occ}'
