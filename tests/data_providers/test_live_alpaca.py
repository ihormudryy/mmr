"""Real Alpaca calls. Run with: MMR_LIVE_TESTS=1 ALPACA_API_KEY_ID=... ALPACA_API_SECRET_KEY=... pytest -m live"""

import datetime as dt
import math
import os
import re

import pytest

from trader.data_providers.capabilities import Capability
from trader.data_providers.errors import ProviderEntitlementError, ProviderError
from trader.data_providers.option_symbols import parse_option_symbol
from trader.data_providers.registry import ProviderRegistry
from trader.objects import BarSize
from trader.tools.chain import implied_distribution

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv('MMR_LIVE_TESTS') != '1'
        or not os.getenv('ALPACA_API_KEY_ID') or not os.getenv('ALPACA_API_SECRET_KEY'),
        reason='live test: set MMR_LIVE_TESTS=1 and Alpaca keys',
    ),
]


def _provider():
    return ProviderRegistry.from_config({
        'alpaca_api_key_id': os.environ['ALPACA_API_KEY_ID'],
        'alpaca_api_secret_key': os.environ['ALPACA_API_SECRET_KEY'],
    }).get(Capability.HISTORY, 'alpaca')


def test_one_day_of_sip_minute_bars_with_extended_hours():
    df = _provider().get_history('AAPL', BarSize.Mins1, dt.datetime(2023, 1, 3), dt.datetime(2023, 1, 3))
    assert len(df) > 700                      # 04:00–20:00 ET session, ~960 bars
    assert df.index.min().hour == 4 and df.index.max().hour == 19


def test_nvda_split_is_adjusted_with_no_jump():
    # NVDA 10-for-1 split effective 2024-06-10. Split-adjusted closes stay ~$120.
    df = _provider().get_history('NVDA', BarSize.Days1, dt.datetime(2024, 6, 3), dt.datetime(2024, 6, 14))
    closes = df['close']
    assert closes.max() / closes.min() < 1.2
    assert closes.max() < 200


def _alpaca_config():
    return {
        'alpaca_api_key_id': os.environ['ALPACA_API_KEY_ID'],
        'alpaca_api_secret_key': os.environ['ALPACA_API_SECRET_KEY'],
    }


def _registry():
    return ProviderRegistry.from_config(_alpaca_config())


def test_live_quotes():
    aapl, unknown = _registry().get(Capability.QUOTES, 'alpaca').quotes(['AAPL', 'ZZZZQ'])
    assert aapl['error'] == '' and aapl['last'] > 0 and aapl['feed'] == 'iex'
    assert 'no snapshot' in unknown['error']


def test_live_news():
    items = _registry().get(Capability.NEWS, 'alpaca').news('AAPL', 3)
    assert 1 <= len(items) <= 3 and all(item['title'] for item in items)


def test_live_movers_are_clean():
    from trader.data_providers.builtin import alpaca_asset_directory
    from trader.data_providers.movers_filter import filter_stock_movers
    assets = alpaca_asset_directory(_alpaca_config())
    raw = _registry().get(Capability.MOVERS, 'alpaca').movers('stocks', 'gainers')
    clean = filter_stock_movers(raw, 1.0, assets)
    assert len(raw) > 0
    assert (clean['close'] >= 1.0).all()
    assert not clean['ticker'].map(assets.is_derivative_unit).any()
    assert assets.knows('AAPL') and assets.name('AAPL')


def test_live_crypto_movers():
    assert len(_registry().get(Capability.MOVERS, 'alpaca').movers('crypto', 'gainers')) > 0


def test_live_ideas_alpaca_momentum():
    from trader.tools.idea_scanner import IdeaScanner
    source = _registry().get(Capability.IDEAS, 'alpaca')
    df = IdeaScanner(source).scan(preset='momentum', top_n=5)
    notice = df.attrs.get('ideas_notice', '')
    assert re.search(r'Alpaca discovery: \d+ symbols', notice)
    assert 'not the full market' in notice and '15-minute delayed' in notice
    assert df.attrs['ideas_provider'] == 'alpaca'
    if not df.empty:
        assert (df['volume'] > 0).all()


def test_live_ideas_alpaca_tickers():
    from trader.tools.idea_scanner import IdeaScanner
    df = IdeaScanner(_registry().get(Capability.IDEAS, 'alpaca')).scan(
        preset='momentum', source='tickers', tickers=['AAPL', 'MSFT', 'ZZZZQ'], top_n=5,
        custom_filters={'min_change_pct': -100, 'max_change_pct': 100})
    assert 'ZZZZQ' in df.attrs.get('ideas_notice', '')
    assert df.attrs['ideas_provider'] == 'alpaca'
    if not df.empty:
        assert {'AAPL', 'MSFT'} & set(df['ticker'])


def test_live_etf_proxy_index_movers():
    registry = ProviderRegistry.from_config({
        'alpaca_api_key_id': os.environ['ALPACA_API_KEY_ID'],
        'alpaca_api_secret_key': os.environ['ALPACA_API_SECRET_KEY'],
    })
    frame = registry.get(Capability.MOVERS_INDICES, 'etf_proxy').movers('indices', 'gainers')
    assert len(frame) == 15
    assert frame['note'].str.startswith('ETF proxy for ').all()
    assert frame['change_pct'].notna().sum() >= 12


def _options():
    return ProviderRegistry.from_config({
        'alpaca_api_key_id': os.environ['ALPACA_API_KEY_ID'],
        'alpaca_api_secret_key': os.environ['ALPACA_API_SECRET_KEY'],
    }).get(Capability.OPTIONS, 'alpaca')


def _expiration_at_least(days):
    target = dt.date.today() + dt.timedelta(days=days)
    return next(d for d in _options().expirations('AAPL') if dt.date.fromisoformat(d) >= target)


def test_live_option_expirations_reach_months_ahead():
    dates = _options().expirations('AAPL')
    assert len(dates) > 10 and dates == sorted(dates)
    assert dt.date.fromisoformat(dates[0]) >= dt.date.today() - dt.timedelta(days=1)
    assert dt.date.fromisoformat(dates[-1]) - dt.date.today() > dt.timedelta(days=180)


def test_live_option_chain_is_indicative_and_never_invents_greeks():
    expiration = _expiration_at_least(30)
    rows = _options().chain('AAPL', expiration)
    assert len(rows) > 20
    assert all(r['feed'] == 'indicative' and r['provider'] == 'alpaca' for r in rows)
    assert all(r['expiration'] == expiration for r in rows)
    assert all(math.isnan(r['delta']) == math.isnan(r['iv']) for r in rows)   # greeks and IV come together
    assert any(not math.isnan(r['open_interest']) for r in rows)
    assert rows[0]['underlying_price'] > 0


def test_live_option_contract_matches_chain_row():
    expiration = _expiration_at_least(30)
    row = next(r for r in _options().chain('AAPL', expiration, 'call') if not math.isnan(r['iv']))
    contract = _options().contract(parse_option_symbol('O:' + row['ticker']))
    assert contract['ticker'] == row['ticker'] and contract['underlying'] == 'AAPL'
    assert contract['feed'] == 'indicative'


def test_live_unknown_underlying_is_loud():
    with pytest.raises(ProviderError, match='invalid underlying'):
        _options().expirations('ZZZZQ')
    with pytest.raises(ProviderError, match='invalid underlying'):
        _options().chain('ZZZZQ', _expiration_at_least(30))


def test_live_implied_distribution_from_indicative_chain():
    expiration = _expiration_at_least(30)
    rows = _options().chain('AAPL', expiration, 'call')
    result = implied_distribution(rows, expiration, 0.05, dt.date.today())
    assert result['strikes_used'] >= 8 and result['feed'] == 'indicative'


@pytest.mark.skipif(not os.getenv('MASSIVE_API_KEY'), reason='needs MASSIVE_API_KEY')
def test_live_massive_options_entitlement_is_loud():
    provider = ProviderRegistry.from_config({'massive_api_key': os.environ['MASSIVE_API_KEY']}) \
        .get(Capability.OPTIONS, 'massive')
    expiration = provider.expirations('AAPL')[0]     # the contracts list works on free Massive plans
    try:
        rows = provider.chain('AAPL', expiration)
    except ProviderEntitlementError as ex:
        assert 'NOT_AUTHORIZED' in str(ex) and '--source alpaca' in str(ex)
    else:
        assert rows and all(r['feed'] == 'opra' for r in rows)
