"""Alpaca's list of US equities: names, exchanges, and which symbols are warrants/rights/units.

The list is ~14k symbols (6.5 MB), so it is cached on disk for a day. A cache
that cannot be written (read-only container) is not an error.
"""

import datetime as dt
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Optional

ALPACA_PAPER_TRADING_URL = 'https://paper-api.alpaca.markets'
ASSETS_PATH = '/v2/assets'
DEFAULT_CACHE_PATH = Path('~/.local/share/mmr/cache/alpaca_assets.json').expanduser()
CACHE_MAX_AGE = dt.timedelta(hours=24)

# Security-type word at the end of the name, or followed by ", each" / "," (SPAC unit wording).
# "Unit Corporation Common Stock" does not match.
_DERIVATIVE_UNIT = re.compile(r'\b(warrants?|rights?|units?)\b\s*(,|$)', re.IGNORECASE)

logger = logging.getLogger(__name__)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class AlpacaAssetDirectory:
    def __init__(self, client, cache_path: Optional[Path] = DEFAULT_CACHE_PATH,
                 now: Callable[[], dt.datetime] = _utc_now):
        self._client = client
        self._cache_path = cache_path
        self._now = now
        self._assets: Optional[dict[str, dict]] = None

    def load(self) -> 'AlpacaAssetDirectory':
        if self._assets is None:
            self._assets = self._read_cache() or self._fetch()
        return self

    def name(self, symbol: str) -> str:
        return self._lookup(symbol).get('name', '')

    def exchange(self, symbol: str) -> str:
        return self._lookup(symbol).get('exchange', '')

    def knows(self, symbol: str) -> bool:
        return bool(self._lookup(symbol))

    def is_derivative_unit(self, symbol: str) -> bool:
        return bool(_DERIVATIVE_UNIT.search(self.name(symbol)))

    def _lookup(self, symbol: str) -> dict:
        return self.load()._assets.get(symbol.strip().upper(), {})

    def _read_cache(self) -> Optional[dict[str, dict]]:
        if self._cache_path is None or not self._cache_path.exists():
            return None
        try:
            cached = json.loads(self._cache_path.read_text())
            fetched_at = dt.datetime.fromisoformat(cached['fetched_at'])
            if self._now() - fetched_at > CACHE_MAX_AGE:
                return None
            return cached['assets']
        except (OSError, ValueError, KeyError, TypeError) as ex:
            logger.warning('ignoring unreadable alpaca asset cache %s: %s', self._cache_path, ex)
            return None

    def _fetch(self) -> dict[str, dict]:
        raw = self._client.get_json(ASSETS_PATH, {'status': 'active', 'asset_class': 'us_equity'})
        assets = {a['symbol'].upper(): {'name': a.get('name') or '', 'exchange': a.get('exchange') or ''}
                  for a in raw if a.get('symbol')}
        self._write_cache(assets)
        return assets

    def _write_cache(self, assets: dict[str, dict]) -> None:
        if self._cache_path is None:
            return
        temp_name = None
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({'fetched_at': self._now().isoformat(), 'assets': assets})
            with tempfile.NamedTemporaryFile('w', dir=self._cache_path.parent, delete=False) as tmp:
                temp_name = tmp.name
                tmp.write(payload)
            os.replace(temp_name, self._cache_path)
        except OSError as ex:
            logger.warning('could not write alpaca asset cache %s: %s', self._cache_path, ex)
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)
