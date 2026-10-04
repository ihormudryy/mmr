"""Forex from TwelveData REST: /quote for a pair, /currency_conversion for amounts. REST has no bid/ask."""

import json
from contextlib import contextmanager
from typing import Optional, Sequence

import pandas as pd
from requests.exceptions import RequestException
from twelvedata.exceptions import InvalidApiKeyError, TwelveDataError

from trader.data_providers.capabilities import make_fx_conversion, make_fx_rate
from trader.data_providers.errors import CapabilityNotSupported, ProviderEntitlementError, ProviderError


class TwelveDataForex:
    def __init__(self, client):
        self._client = client

    def rate(self, base: str, quote: str) -> dict:
        with _provider_errors():
            payload = self._client.quote(symbol=f'{base}/{quote}').as_json()
        note = ''
        if 'is_market_open' in payload:
            note = 'market open' if payload['is_market_open'] else 'market closed'
        return make_fx_rate(
            base, quote,
            last=_number(payload, 'close'),
            open=_number(payload, 'open'), high=_number(payload, 'high'), low=_number(payload, 'low'),
            close=_number(payload, 'close'), previous_close=_number(payload, 'previous_close'),
            change=_number(payload, 'change'), change_pct=_number(payload, 'percent_change'),
            volume=_number(payload, 'volume'),
            as_of=str(payload.get('datetime') or ''), source='twelvedata', note=note,
        )

    def rates(self, base: str, symbols: Optional[Sequence[str]]) -> pd.DataFrame:
        raise CapabilityNotSupported('forex rates (snapshot-all)', 'twelvedata', ['frankfurter', 'massive'])

    def convert(self, base: str, quote: str, amount: float) -> dict:
        with _provider_errors():
            payload = self._client.currency_conversion(symbol=f'{base}/{quote}', amount=amount).as_json()
        return make_fx_conversion(
            base, quote, amount,
            converted=_number(payload, 'amount'), rate=_number(payload, 'rate'),
            as_of=str(payload.get('timestamp') or ''), source='twelvedata',
        )


@contextmanager
def _provider_errors():
    try:
        yield
    except InvalidApiKeyError as ex:
        raise ProviderEntitlementError(f'twelvedata forex request refused: {ex}') from ex
    except TwelveDataError as ex:
        raise ProviderError(f'twelvedata forex request failed: {ex}') from ex
    except (RequestException, json.JSONDecodeError) as ex:
        raise ProviderError(f'twelvedata forex request failed: {ex}') from ex


def _number(payload: dict, key: str) -> float:
    try:
        return float(payload.get(key))
    except (TypeError, ValueError):
        return float('nan')
