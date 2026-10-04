import datetime as dt
import json
import os
from pathlib import Path

import pytest

from trader.data_providers.alpaca.assets import AlpacaAssetDirectory

SAMPLE = json.loads((Path(__file__).parent / 'fixtures' / 'alpaca_assets_sample.json').read_text())
NOW = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)


class FakeClient:
    def __init__(self):
        self.calls = 0

    def get_json(self, path, params):
        self.calls += 1
        assert path == '/v2/assets' and params == {'status': 'active', 'asset_class': 'us_equity'}
        return SAMPLE


def test_fetches_once_and_answers_lookups(tmp_path):
    client = FakeClient()
    assets = AlpacaAssetDirectory(client, cache_path=tmp_path / 'a.json', now=lambda: NOW)
    assert assets.name('AAPL') == 'Apple Inc. Common Stock' and assets.exchange('BRK.B') == 'NYSE'
    assert assets.knows('aapl') and not assets.knows('ZZZZQ')
    assert assets.name('ZZZZQ') == ''
    assert client.calls == 1


def test_load_fetches_eagerly_and_returns_self(tmp_path):
    client = FakeClient()
    assets = AlpacaAssetDirectory(client, cache_path=tmp_path / 'a.json', now=lambda: NOW)
    assert assets.load() is assets and client.calls == 1
    assets.name('AAPL')
    assert client.calls == 1


def test_derivative_units_are_detected():
    assets = AlpacaAssetDirectory(FakeClient(), cache_path=None, now=lambda: NOW)
    flagged = {s['symbol'] for s in SAMPLE if assets.is_derivative_unit(s['symbol'])}
    assert flagged == {'HPAIW', 'GLLRF', 'KVAUF', 'BBAI.WS'}


def test_fresh_cache_is_reused(tmp_path):
    cache = tmp_path / 'a.json'
    AlpacaAssetDirectory(FakeClient(), cache_path=cache, now=lambda: NOW).name('AAPL')
    second = FakeClient()
    assert AlpacaAssetDirectory(second, cache_path=cache, now=lambda: NOW + dt.timedelta(hours=23)).name('AAPL')
    assert second.calls == 0


def test_stale_cache_is_refreshed(tmp_path):
    cache = tmp_path / 'a.json'
    AlpacaAssetDirectory(FakeClient(), cache_path=cache, now=lambda: NOW).name('AAPL')
    later = FakeClient()
    AlpacaAssetDirectory(later, cache_path=cache, now=lambda: NOW + dt.timedelta(hours=25)).name('AAPL')
    assert later.calls == 1


def test_unwritable_cache_still_returns_assets(tmp_path):
    read_only = tmp_path / 'ro'
    read_only.mkdir()
    os.chmod(read_only, 0o500)
    try:
        assets = AlpacaAssetDirectory(FakeClient(), cache_path=read_only / 'sub' / 'a.json', now=lambda: NOW)
        assert assets.name('AAPL') == 'Apple Inc. Common Stock'
    finally:
        os.chmod(read_only, 0o700)


def test_corrupt_cache_is_refetched(tmp_path):
    cache = tmp_path / 'a.json'
    cache.write_text('{not json')
    client = FakeClient()
    assert AlpacaAssetDirectory(client, cache_path=cache, now=lambda: NOW).knows('AAPL')
    assert client.calls == 1


REAL_NAMES = {
    'MMVXF': ('MULTIMETAVERSE HLDGS LTD Warrant   01/04/2028', True),
    'HGASW': ('Global Gas Corporation Warrant Exp 12/21/2028', True),
    'HCVIU': ('HENNESSY CAP INVT CORP VI UNIT 1 CL A & 1/3 WT', True),
    'ALSTF': ('ALPHA STAR ACQUISITION CORP Rights   12/13/2026', True),
    'BLUWU': ('Blue Water Acquisition Corp. III Unit.', True),
    'OXY.WS': ('Occidental Petroleum Corporation Warrants to Purchase Common Stock', True),
    'HSPUF': ('HORIZON SPACE ACQUISITION I CORP USD UNITS CONSISTING ONE ORD SH & ONE RED WT & ONE RT '
              '(Cayman Islands)', True),
    'ASGI.RT': ('abrdn Global Infrastructure Income Fund Rights (expiring October 15, 2026)', True),
    'ET': ('Energy Transfer LP Common Units representing limited partner interests', False),
    'SPLP': ('Steel Partners Holdings L.P. Common Units, no par value', False),
    'WDH': ('Waterdrop Inc. American Depositary Shares (each representing the right to receive '
            '10 Class A Ordinary Shares)', False),
    'OBTC': ('Osprey Bitcoin Trust Common Units of Beneficial Interest', False),
    'PAGP': ('Plains GP Holdings, L.P. Class A Units representing Limited Partner Interests', False),
}


class RealNamesClient:
    def get_json(self, path, params):
        return [{'symbol': symbol, 'name': name} for symbol, (name, _) in REAL_NAMES.items()]


@pytest.mark.parametrize('symbol,expected', [(symbol, flagged) for symbol, (_, flagged) in REAL_NAMES.items()])
def test_real_alpaca_names_are_classified(symbol, expected):
    assets = AlpacaAssetDirectory(RealNamesClient(), cache_path=None, now=lambda: NOW)
    assert assets.is_derivative_unit(symbol) is expected
