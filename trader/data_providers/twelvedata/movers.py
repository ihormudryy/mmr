"""Market movers from TwelveData /market_movers (needs a Pro+ plan)."""

import pandas as pd

from trader.data_providers.capabilities import sort_movers
from trader.data_providers.errors import CapabilityNotSupported


class TwelveDataMovers:
    markets = frozenset({'stocks'})

    def __init__(self, client):
        self._client = client

    def movers(self, market: str, direction: str) -> pd.DataFrame:
        if market not in self.markets:
            raise CapabilityNotSupported(f'{market} movers', 'twelvedata', ['massive'])
        payload = self._client.get_market_movers(market=market, direction=direction).as_json()
        entries = payload if isinstance(payload, list) else payload.get('values', [])
        frame = pd.DataFrame([{
            'ticker': e.get('symbol', ''),
            'name': e.get('name', ''),
            'close': e.get('last'),
            'volume': e.get('volume'),
            'change': e.get('change'),
            'change_pct': e.get('percent_change'),
            'provider': 'twelvedata',
            'exchange': e.get('exchange', ''),
        } for e in entries], columns=['ticker', 'name', 'close', 'volume', 'change', 'change_pct',
                                      'provider', 'exchange'])
        for column in ('close', 'volume', 'change', 'change_pct'):
            frame[column] = pd.to_numeric(frame[column], errors='coerce')
        return sort_movers(frame, direction)
