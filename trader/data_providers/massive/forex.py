"""Forex from Massive (Polygon): ticker snapshots and real-time conversion."""

import json
from contextlib import contextmanager
from typing import Optional, Sequence

import pandas as pd
from massive.exceptions import AuthError, BadResponse
from urllib3.exceptions import HTTPError, MaxRetryError

from trader.data_providers.capabilities import make_fx_conversion, make_fx_rate, round_significant, sort_fx_rates
from trader.data_providers.errors import ProviderEntitlementError, ProviderError, ProviderRateLimited

_RATES_FRAME_COLUMNS = ['pair', 'last', 'previous_close', 'change', 'change_pct', 'as_of', 'source', 'note',
                        'open', 'high', 'low', 'volume']


class MassiveForex:
    def __init__(self, client):
        self._client = client

    def rate(self, base: str, quote: str) -> dict:
        ticker = f'C:{base}{quote}'
        with _provider_errors():
            # The typed TickerSnapshot parses lastQuote with the stock parser (keys P/p),
            # which loses forex bid/ask (keys b/a), so read the raw JSON for this one call.
            response = self._client.get_snapshot_ticker(market_type='forex', ticker=ticker, raw=True)
            payload = json.loads(response.data)
        snap = payload.get('ticker') if isinstance(payload, dict) else None
        if not isinstance(snap, dict):
            raise ProviderError(f'massive returned no forex snapshot for {ticker}')
        day = snap.get('day')
        close = _json_number(day, 'c')
        return make_fx_rate(
            base, quote,
            last=close,
            bid=_json_number(snap.get('lastQuote'), 'b'), ask=_json_number(snap.get('lastQuote'), 'a'),
            open=_json_number(day, 'o'), high=_json_number(day, 'h'), low=_json_number(day, 'l'),
            close=close, volume=_json_number(day, 'v'),
            previous_close=_json_number(snap.get('prevDay'), 'c'),
            change=_json_number(snap, 'todaysChange'), change_pct=_json_number(snap, 'todaysChangePerc'),
            as_of=_text(snap.get('updated')), source='massive',
        )

    def rates(self, base: str, symbols: Optional[Sequence[str]]) -> pd.DataFrame:
        tickers = [f'C:{base}{symbol}' for symbol in symbols] if symbols else None
        prefix = f'C:{base}'
        with _provider_errors():
            snaps = list(self._client.get_snapshot_all(market_type='forex', tickers=tickers))
        rows = [_rates_row(base, snap, prefix) for snap in snaps if (snap.ticker or '').startswith(prefix)]
        _require_every_symbol(base, symbols, rows)
        if not rows:
            raise ProviderError(f'massive returned no forex rates against {base}')
        return sort_fx_rates(pd.DataFrame(rows, columns=_RATES_FRAME_COLUMNS))

    def convert(self, base: str, quote: str, amount: float) -> dict:
        with _provider_errors():
            result = self._client.get_real_time_currency_conversion(base, quote, amount=amount)
        last = getattr(result, 'last', None)
        converted = _number(result, 'converted')
        return make_fx_conversion(
            base, quote, amount,
            converted=converted, rate=round_significant(converted / amount) if amount > 0 else float('nan'),
            bid=_number(last, 'bid'), ask=_number(last, 'ask'),
            as_of=_text(getattr(last, 'timestamp', None)), source='massive',
        )


def _rates_row(base: str, snap, prefix: str) -> dict:
    return {
        'pair': f'{base}/{snap.ticker[len(prefix):]}',
        'last': _number(snap.day, 'close'),
        'previous_close': _number(snap.prev_day, 'close'),
        'change': _number(snap, 'todays_change'),
        'change_pct': _number(snap, 'todays_change_percent'),
        'as_of': _text(snap.updated),
        'source': 'massive',
        'note': '',
        'open': _number(snap.day, 'open'),
        'high': _number(snap.day, 'high'),
        'low': _number(snap.day, 'low'),
        'volume': _number(snap.day, 'volume'),
    }


def _require_every_symbol(base: str, symbols: Optional[Sequence[str]], rows: list[dict]) -> None:
    if not symbols:
        return
    returned = {row['pair'] for row in rows}
    missing = [f'{base}/{symbol}' for symbol in symbols if f'{base}/{symbol}' not in returned]
    if missing:
        raise ValueError(f"massive has no forex rate for: {', '.join(missing)}")


@contextmanager
def _provider_errors():
    try:
        yield
    except AuthError as ex:
        raise ProviderEntitlementError(f'massive forex request refused: {ex}') from ex
    except json.JSONDecodeError as ex:
        raise ProviderError(f'massive forex response was not valid JSON: {ex}') from ex
    except HTTPError as ex:
        message = f'massive forex request failed: {ex}'
        if isinstance(ex, MaxRetryError) and 'too many 429' in str(ex.reason):
            raise ProviderRateLimited(message) from ex
        raise ProviderError(message) from ex
    except BadResponse as ex:
        if 'NOT_AUTHORIZED' in str(ex):
            raise ProviderEntitlementError(f'massive forex request not authorized: {ex}') from ex
        raise ProviderError(f'massive forex request failed: {ex}') from ex


def _number(obj, attr: str) -> float:
    value = getattr(obj, attr, None) if obj is not None else None
    return float('nan') if value is None else float(value)


def _json_number(mapping, key: str) -> float:
    value = mapping.get(key) if isinstance(mapping, dict) else None
    return float('nan') if value is None else float(value)


def _text(value) -> str:
    return '' if value is None else str(value)
