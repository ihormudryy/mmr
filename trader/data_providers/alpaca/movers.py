"""Top gainers/losers from Alpaca's screener (stocks and crypto; at most 50 each way)."""

import pandas as pd

from trader.data_providers.capabilities import sort_movers
from trader.data_providers.errors import CapabilityNotSupported

MAX_TOP = 50


class AlpacaMovers:
    markets = frozenset({'stocks', 'crypto'})

    def __init__(self, client):
        self._client = client

    def movers(self, market: str, direction: str) -> pd.DataFrame:
        if market not in self.markets:
            raise CapabilityNotSupported(f'{market} movers', 'alpaca', ['massive'])
        payload = self._client.get_json(f'/v1beta1/screener/{market}/movers', {'top': MAX_TOP})
        as_of = (payload.get('last_updated') or '')[:19]
        note = f'as of {as_of}Z' if as_of else ''
        frame = pd.DataFrame([{
            'ticker': e.get('symbol', ''),
            'close': float(e.get('price', float('nan'))),
            'volume': float('nan'),
            'change': float(e.get('change', float('nan'))),
            'change_pct': float(e.get('percent_change', float('nan'))),
            'provider': 'alpaca',
            'note': note,
        } for e in payload.get(direction) or []],
            columns=['ticker', 'close', 'volume', 'change', 'change_pct', 'provider', 'note'])
        return sort_movers(frame, direction)
