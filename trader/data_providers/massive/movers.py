"""Market movers from Massive (Polygon) snapshots."""

import pandas as pd

from trader.data_providers.capabilities import sort_movers


class MassiveMovers:
    markets = frozenset({'stocks', 'crypto', 'indices', 'options', 'futures', 'forex'})

    def __init__(self, client):
        self._client = client

    def movers(self, market: str, direction: str) -> pd.DataFrame:
        snaps = self._client.get_snapshot_direction(market_type=market, direction=direction)
        rows = [{
            'ticker': snap.ticker or '',
            'close': getattr(snap.day, 'close', None) if snap.day else None,
            'volume': getattr(snap.day, 'volume', None) if snap.day else None,
            'change': snap.todays_change,
            'change_pct': snap.todays_change_percent,
            'provider': 'massive',
        } for snap in snaps]
        frame = pd.DataFrame(rows, columns=['ticker', 'close', 'volume', 'change', 'change_pct', 'provider'])
        return sort_movers(frame.astype({'close': float, 'volume': float, 'change': float, 'change_pct': float}),
                           direction)
