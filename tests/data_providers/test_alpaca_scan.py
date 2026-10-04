import json
from pathlib import Path

import pandas as pd
import pytest

from trader.data_providers.alpaca.scan import AlpacaScanSource, candidate_from_snapshot
from trader.data_providers.capabilities import make_news_item
from trader.data_providers.errors import ProviderEntitlementError, ProviderNotConfigured, ProviderRateLimited
from trader.tools.idea_scanner import IdeaScannerError, compute_ema, compute_rsi, compute_sma

FIX = Path(__file__).parent / 'fixtures'
SNAP = json.loads((FIX / 'alpaca_snapshots_delayed_sip_aapl.json').read_text())
MOVERS = json.loads((FIX / 'alpaca_movers_stocks.json').read_text())
ACTIVES = json.loads((FIX / 'alpaca_most_actives.json').read_text())


class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        return self.routes(path, params)


class FakeAssets:
    def is_derivative_unit(self, symbol):
        return symbol.endswith('W')

    def name(self, symbol):
        return {'AAPL': 'Apple Inc. Common Stock'}.get(symbol, '')


def _routes(path, params):
    if path.endswith('/movers'):
        return MOVERS
    if path.endswith('/most-actives'):
        return ACTIVES
    if path == '/v2/stocks/snapshots':
        return {s: SNAP['AAPL'] for s in params['symbols'].split(',') if s in ('AAPL', 'AMOD', 'MN', 'SDEV')}
    raise AssertionError(path)


def test_candidate_uses_consolidated_volume_and_vwap():
    c = candidate_from_snapshot('AAPL', SNAP['AAPL'])
    assert c['price'] == 333.69 and c['volume'] == 34261610 and c['vwap'] == 333.16
    assert c['change_pct'] == pytest.approx(round((333.69 - 330.32) / 330.32 * 100, 2))
    assert c['gap_pct'] == pytest.approx(round((333.26 - 330.32) / 330.32 * 100, 2))
    assert c['rel_vol'] == pytest.approx(round(34261610 / 36472464, 2))
    assert c['range_pct'] == pytest.approx(round((334.54 - 330.61) / 330.61 * 100, 2))
    assert c['spread_pct'] == pytest.approx(round((333.69 - 333.35) / 333.69 * 100, 3))


def test_candidate_skips_missing_daily_bar_or_price():
    assert candidate_from_snapshot('X', {}) is None
    assert candidate_from_snapshot('X', {'dailyBar': {'c': None}}) is None


def test_tickers_path_requests_delayed_sip_snapshots():
    client = FakeClient(_routes)
    discovery = AlpacaScanSource(client).discover('tickers', ['aapl'], None, False)
    assert client.calls == [('/v2/stocks/snapshots', {'symbols': 'AAPL', 'feed': 'delayed_sip'})]
    assert [c['ticker'] for c in discovery.candidates] == ['AAPL']
    assert '15-minute delayed' in discovery.notice


def test_unknown_tickers_are_named_in_notice():
    discovery = AlpacaScanSource(FakeClient(_routes)).discover('tickers', ['AAPL', 'ZZZZQ'], None, False)
    assert [c['ticker'] for c in discovery.candidates] == ['AAPL']
    assert 'ZZZZQ' in discovery.notice


def test_invalid_ticker_is_named_without_request():
    client = FakeClient(_routes)
    discovery = AlpacaScanSource(client).discover('tickers', ['AAPL;DROP'], None, False)
    assert discovery.candidates == [] and 'AAPL;DROP' in discovery.notice
    assert client.calls == []


def test_market_scan_preset_uses_movers_and_actives_with_notice():
    client = FakeClient(_routes)
    discovery = AlpacaScanSource(client, assets=FakeAssets()).discover('movers', None, None, True)
    paths = [p for p, _ in client.calls]
    assert '/v1beta1/screener/stocks/movers' in paths and '/v1beta1/screener/stocks/most-actives' in paths
    requested = client.calls[-1][1]['symbols'].split(',')
    assert 'HPAIW' not in requested                    # derivative units never requested
    assert len(requested) == len(set(requested))       # union, no duplicates
    assert 'not the full market' in discovery.notice
    assert f'{len(discovery.candidates)} symbols' in discovery.notice   # returned, not screened


def test_universe_path_uses_given_symbols():
    client = FakeClient(_routes)
    AlpacaScanSource(client).discover('universe', None, ['AAPL', 'MN'], False)
    assert client.calls[0][1]['symbols'] == 'AAPL,MN'


def _snapshot_without(key, value=None):
    snapshot = json.loads(json.dumps(SNAP['AAPL']))
    if value is None:
        snapshot.pop(key, None)
    else:
        snapshot[key]['c'] = value
    return snapshot


def _routes_with(snapshot_by_symbol):
    def routes(path, params):
        if path.endswith('/movers'):
            return MOVERS
        if path.endswith('/most-actives'):
            return ACTIVES
        return {s: snapshot_by_symbol[s] for s in params['symbols'].split(',') if s in snapshot_by_symbol}
    return routes


def test_null_prev_daily_bar_is_dropped_and_named():
    routes = _routes_with({'AAPL': SNAP['AAPL'], 'NOPREV': _snapshot_without('prevDailyBar')})
    discovery = AlpacaScanSource(FakeClient(routes)).discover('tickers', ['AAPL', 'NOPREV'], None, False)
    assert [c['ticker'] for c in discovery.candidates] == ['AAPL']
    assert 'No previous close from Alpaca for: NOPREV (dropped).' in discovery.notice


def test_zero_prev_close_is_dropped_and_named():
    routes = _routes_with({'ZERO': _snapshot_without('prevDailyBar', 0)})
    discovery = AlpacaScanSource(FakeClient(routes)).discover('tickers', ['ZERO'], None, False)
    assert discovery.candidates == []
    assert 'No previous close from Alpaca for: ZERO (dropped).' in discovery.notice
    assert candidate_from_snapshot('ZERO', _snapshot_without('prevDailyBar', 0)) is None


def test_market_notice_keeps_inner_warnings_and_counts_returned_candidates():
    routes = _routes_with({'AMOD': SNAP['AAPL'], 'SDEV': _snapshot_without('prevDailyBar')})
    discovery = AlpacaScanSource(FakeClient(routes), assets=FakeAssets()).discover('movers', None, None, True)
    assert [c['ticker'] for c in discovery.candidates] == ['AMOD']
    assert '1 symbols from top movers + most-actives (not the full market)' in discovery.notice
    assert 'No Alpaca snapshot for:' in discovery.notice
    assert 'No previous close from Alpaca for: SDEV (dropped).' in discovery.notice


def test_duplicate_tickers_are_requested_once():
    client = FakeClient(_routes)
    discovery = AlpacaScanSource(client).discover('tickers', ['AAPL', 'aapl', 'AAPL'], None, False)
    assert client.calls[0][1]['symbols'] == 'AAPL'
    assert [c['ticker'] for c in discovery.candidates] == ['AAPL']


def test_more_than_chunk_size_symbols_use_two_snapshot_calls():
    client = FakeClient(lambda path, params: {})
    symbols = [f'T{i}' for i in range(101)]
    AlpacaScanSource(client).discover('universe', None, symbols, False)
    sizes = [len(params['symbols'].split(',')) for _, params in client.calls]
    assert sizes == [100, 1]


def test_screener_requests_top_50():
    client = FakeClient(_routes)
    AlpacaScanSource(client, assets=FakeAssets()).discover('movers', None, None, True)
    screener = {path: params for path, params in client.calls if 'screener' in path}
    assert screener['/v1beta1/screener/stocks/movers'] == {'top': 50}
    assert screener['/v1beta1/screener/stocks/most-actives'] == {'by': 'volume', 'top': 50}


def test_client_errors_propagate_on_tickers_and_market_paths():
    from trader.data_providers.errors import ProviderEntitlementError

    def failing(path, params):
        raise ProviderEntitlementError('not entitled')

    with pytest.raises(ProviderEntitlementError):
        AlpacaScanSource(FakeClient(failing)).discover('tickers', ['AAPL'], None, False)
    with pytest.raises(ProviderEntitlementError):
        AlpacaScanSource(FakeClient(failing)).discover('movers', None, None, True)


class FakeHistory:
    def __init__(self, closes):
        self.closes = closes
        self.calls = []

    def get_history(self, ticker, bar_size, start_date, end_date, timezone='US/Eastern'):
        self.calls.append((ticker, str(bar_size)))
        index = pd.date_range('2026-05-01', periods=len(self.closes), freq='B', tz='US/Eastern', name='date')
        return pd.DataFrame({'close': self.closes}, index=index)


class FakeNews:
    def news(self, ticker, limit):
        return [make_news_item(title=f'{ticker} headline', published='2026-10-02T11:00:00', tickers=[ticker])]


def test_indicators_computed_locally_from_daily_bars():
    closes = [100 + i * 0.5 for i in range(80)]
    history = FakeHistory(closes)
    source = AlpacaScanSource(FakeClient(_routes), history=history)
    out = source.indicators(['AAPL'], ['rsi', 'ema_9', 'sma_20', 'sma_50'])
    assert history.calls == [('AAPL', '1 day')]
    assert out['AAPL']['rsi'] == compute_rsi(closes, period=14)
    assert out['AAPL']['ema_9'] == compute_ema(closes, window=9)
    assert out['AAPL']['sma_20'] == compute_sma(closes, window=20)
    assert out['AAPL']['sma_50'] == compute_sma(closes, window=50)


class FailingHistory(FakeHistory):
    def __init__(self, closes, failures):
        super().__init__(closes)
        self.failures = failures

    def get_history(self, ticker, *args, **kwargs):
        if ticker in self.failures:
            raise self.failures[ticker]
        return super().get_history(ticker, *args, **kwargs)


def test_indicator_failure_for_one_ticker_skips_only_that_ticker():
    history = FailingHistory([100 + i for i in range(30)], {'AAPL': RuntimeError('boom')})
    out = AlpacaScanSource(FakeClient(_routes), history=history).indicators(['AAPL', 'MSFT'], ['rsi'])
    assert out['AAPL'] == {}
    assert out['MSFT']['rsi'] is not None


def test_indicator_failure_for_every_ticker_raises():
    history = FailingHistory([], {t: RuntimeError(f'boom {t}') for t in ('AAPL', 'MSFT', 'NVDA')})
    with pytest.raises(IdeaScannerError, match='all 3 tickers: boom AAPL'):
        AlpacaScanSource(FakeClient(_routes), history=history).indicators(['AAPL', 'MSFT', 'NVDA'], ['rsi'])


@pytest.mark.parametrize('error', [
    ProviderEntitlementError('plan does not allow this'),
    ProviderRateLimited('quota exhausted'),
    ProviderNotConfigured('alpaca', [('alpaca_api_key_id', 'ALPACA_API_KEY_ID')]),
])
def test_auth_and_rate_limit_errors_propagate_from_indicators(error):
    history = FailingHistory([100 + i for i in range(30)], {'AAPL': error})
    with pytest.raises(type(error)):
        AlpacaScanSource(FakeClient(_routes), history=history).indicators(['AAPL', 'MSFT'], ['rsi'])


def test_too_few_bars_leave_long_window_indicator_empty():
    closes = [100 + i for i in range(30)]
    out = AlpacaScanSource(FakeClient(_routes), history=FakeHistory(closes)).indicators(
        ['AAPL'], ['sma_20', 'sma_50'])
    assert out['AAPL']['sma_20'] == compute_sma(closes, window=20)
    assert out['AAPL']['sma_50'] is None


def test_empty_history_frame_gives_empty_indicators_without_error():
    out = AlpacaScanSource(FakeClient(_routes), history=FakeHistory([])).indicators(['AAPL'], ['rsi', 'sma_20'])
    assert out == {'AAPL': {'rsi': None, 'sma_20': None}}


def test_names_from_asset_list():
    assert AlpacaScanSource(FakeClient(_routes), assets=FakeAssets()).names(['AAPL', 'ZZZ']) == {
        'AAPL': 'Apple Inc. Common Stock'}


def test_news_headline_without_sentiment():
    out = AlpacaScanSource(FakeClient(_routes), news=FakeNews()).news(['AAPL'])
    assert out['AAPL'] == {'headline': 'AAPL headline', 'news_date': '2026-10-02', 'sentiment': '', 'catalyst': ''}


def test_fundamentals_raise_until_phase_4():
    with pytest.raises(IdeaScannerError, match='--source massive'):
        AlpacaScanSource(FakeClient(_routes)).fundamentals(['AAPL'])


def test_missing_history_or_news_provider_fails_loudly():
    source = AlpacaScanSource(FakeClient(_routes))
    with pytest.raises(IdeaScannerError):
        source.indicators(['AAPL'], ['rsi'])
    with pytest.raises(IdeaScannerError):
        source.news(['AAPL'])


def test_discovery_survives_asset_list_failure_and_says_so():
    class FailingAssets:
        def is_derivative_unit(self, symbol):
            raise ProviderEntitlementError('alpaca', 'asset list not allowed')

    source = AlpacaScanSource(FakeClient(_routes), assets=FailingAssets())
    discovery = source.discover('movers', None, None, True)
    assert discovery.candidates
    assert 'warrant check falls back to the ticker-suffix rule' in discovery.notice
    assert source._assets is None


def test_asset_list_failure_notice_repeats_on_later_discovery():
    class FailingAssets:
        def is_derivative_unit(self, symbol):
            raise ProviderEntitlementError('alpaca', 'asset list not allowed')

    source = AlpacaScanSource(FakeClient(_routes), assets=FailingAssets())
    source.discover('movers', None, None, True)
    second = source.discover('movers', None, None, True)
    assert 'warrant check falls back to the ticker-suffix rule' in second.notice


def test_discovery_falls_back_to_suffix_rule_when_asset_list_fails():
    class FailingAssets:
        def is_derivative_unit(self, symbol):
            raise ProviderEntitlementError('alpaca', 'asset list not allowed')

    client = FakeClient(_routes)
    AlpacaScanSource(client, assets=FailingAssets()).discover('movers', None, None, True)
    requested = client.calls[-1][1]['symbols'].split(',')
    assert 'HPAIW' not in requested and 'CYCUW' not in requested


def _single_mover_routes(symbol):
    def routes(path, params):
        if path.endswith('/movers'):
            return {'gainers': [{'symbol': symbol}], 'losers': []}
        if path.endswith('/most-actives'):
            return {'most_actives': []}
        return {s: SNAP['AAPL'] for s in params['symbols'].split(',') if s == symbol}
    return routes


def test_asset_list_decides_warrants_so_nws_survives():
    client = FakeClient(_single_mover_routes('NWS'))
    discovery = AlpacaScanSource(client, assets=FakeAssets()).discover('movers', None, None, True)
    assert [c['ticker'] for c in discovery.candidates] == ['NWS']


def test_suffix_rule_applies_without_asset_list():
    client = FakeClient(_single_mover_routes('NWS'))
    discovery = AlpacaScanSource(client).discover('movers', None, None, True)
    assert discovery.candidates == []


@pytest.mark.parametrize('field', ['o', 'h', 'l'])
def test_incomplete_daily_bar_is_dropped_and_named(field):
    snapshot = json.loads(json.dumps(SNAP['AAPL']))
    snapshot['dailyBar'].pop(field)
    assert candidate_from_snapshot('PART', snapshot) is None
    routes = _routes_with({'AAPL': SNAP['AAPL'], 'PART': snapshot})
    discovery = AlpacaScanSource(FakeClient(routes)).discover('tickers', ['AAPL', 'PART'], None, False)
    assert [c['ticker'] for c in discovery.candidates] == ['AAPL']
    assert 'Incomplete Alpaca daily bar for: PART (dropped).' in discovery.notice
    assert 'No previous close' not in discovery.notice


def test_nan_daily_bar_field_is_dropped():
    snapshot = json.loads(json.dumps(SNAP['AAPL']))
    snapshot['dailyBar']['l'] = float('nan')
    assert candidate_from_snapshot('PART', snapshot) is None


@pytest.mark.timeout(10)
def test_fatal_indicator_error_does_not_wait_for_queued_tickers():
    import threading
    release = threading.Event()
    finished = []

    class BlockingHistory(FakeHistory):
        def get_history(self, ticker, *args, **kwargs):
            self.calls.append((ticker, '1 day'))
            if ticker == 'AAPL':
                raise ProviderRateLimited('quota exhausted')
            release.wait(5)
            finished.append(ticker)
            return super().get_history(ticker, *args, **kwargs)

    history = BlockingHistory([100 + i for i in range(30)])
    tickers = ['AAPL'] + [f'T{i}' for i in range(29)]
    try:
        with pytest.raises(ProviderRateLimited):
            AlpacaScanSource(FakeClient(_routes), history=history).indicators(tickers, ['rsi'])
        # Joining the pool would let the blocked workers finish first.
        assert finished == []
        assert len(history.calls) < len(tickers)
    finally:
        release.set()


class FailingNameAssets:
    def is_derivative_unit(self, symbol):
        return False

    def name(self, symbol):
        raise ProviderEntitlementError('alpaca', 'asset list not allowed')


def test_names_degrade_to_empty_when_asset_list_fails():
    source = AlpacaScanSource(FakeClient(_routes), assets=FailingNameAssets())
    assert source.names(['AAPL']) == {}
    assert source._assets is None


class FlakyNews(FakeNews):
    def __init__(self, failures):
        self.failures = failures

    def news(self, ticker, limit):
        if ticker in self.failures:
            raise self.failures[ticker]
        return super().news(ticker, limit)


def test_news_failure_for_one_ticker_skips_only_that_ticker():
    news = FlakyNews({'AAPL': RuntimeError('HTTP 500')})
    out = AlpacaScanSource(FakeClient(_routes), news=news).news(['AAPL', 'MSFT'])
    assert 'AAPL' not in out
    assert out['MSFT']['headline'] == 'MSFT headline'


@pytest.mark.parametrize('error', [
    ProviderEntitlementError('plan does not allow this'),
    ProviderRateLimited('quota exhausted'),
    ProviderNotConfigured('alpaca', [('alpaca_api_key_id', 'ALPACA_API_KEY_ID')]),
])
def test_fatal_news_errors_propagate(error):
    with pytest.raises(type(error)):
        AlpacaScanSource(FakeClient(_routes), news=FlakyNews({'AAPL': error})).news(['AAPL', 'MSFT'])


def _bar(close, prev_close, volume, prev_volume):
    return {
        'dailyBar': {'o': prev_close, 'h': close * 1.01, 'l': prev_close * 0.99, 'c': close, 'v': volume,
                     'vw': close},
        'prevDailyBar': {'c': prev_close, 'v': prev_volume},
        'latestQuote': {'bp': close - 0.01, 'ap': close + 0.01},
    }


def test_end_to_end_default_scan_with_detail_on_real_source():
    from trader.tools.idea_scanner import IdeaScanner

    class NamedAssets(FakeAssets):
        def name(self, symbol):
            return {'MN': 'Manning & Napier', 'NIVF': 'NewGenIvf', 'AMOD': 'Alpha Modus'}.get(symbol, '')

    routes = _routes_with({
        'AMOD': _bar(20.0, 19.4, 1_000_000, 1_000_000),
        'MN': _bar(55.0, 50.0, 2_000_000, 1_000_000),
        'NIVF': _bar(10.6, 10.0, 600_000, 600_000),
        'FNGR': _bar(10.05, 10.0, 900_000, 900_000),   # +0.5%: below the momentum change filter
    })
    source = AlpacaScanSource(FakeClient(routes), assets=NamedAssets(),
                              history=FakeHistory([1.0] * 30), news=FakeNews())
    df = IdeaScanner(source).scan(preset='momentum', top_n=5, names=True, news=True,
                                  fundamentals_if_available=True)

    assert list(df['ticker']) == ['MN', 'NIVF', 'AMOD']
    assert list(df['price']) == [55.0, 10.6, 20.0]
    assert list(df['change_pct']) == [10.0, 6.0, 3.09]
    assert list(df['name']) == ['Manning & Napier', 'NewGenIvf', 'Alpha Modus']
    assert list(df['headline']) == ['MN headline', 'NIVF headline', 'AMOD headline']
    assert 'pe_ratio' not in df.columns
    notice = df.attrs['ideas_notice']
    assert df.attrs['ideas_provider'] == 'alpaca'
    assert 'not the full market' in notice and '15-minute delayed' in notice
    assert 'No Alpaca snapshot for: SDEV' in notice
    assert 'Fundamentals are not available from Alpaca yet' in notice
