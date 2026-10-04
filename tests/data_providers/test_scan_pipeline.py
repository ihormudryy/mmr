from trader.data_providers.capabilities import Discovery, ScanSource
import pytest

from trader.tools.idea_scanner import IdeaScanner, IdeaScannerError


class FakeSource:
    name = 'fake'
    supports_fundamentals = True

    def __init__(self, candidates, notice=''):
        self._discovery = Discovery(candidates, notice)
        self.calls = []

    def discover(self, source, tickers, universe_symbols, use_market_scan):
        self.calls.append(('discover', source, use_market_scan))
        return self._discovery

    def indicators(self, tickers, needed):
        self.calls.append(('indicators', tuple(tickers), tuple(needed)))
        return {t: {'rsi': 55.0, 'ema_9': 1.0} for t in tickers}

    def names(self, tickers):
        return {t: f'{t} Inc' for t in tickers}

    def fundamentals(self, tickers):
        return {t: {'pe_ratio': 10.0} for t in tickers}

    def news(self, tickers):
        return {t: {'headline': f'{t} up', 'news_date': '2026-10-02', 'sentiment': '', 'catalyst': ''}
                for t in tickers}


def _candidate(ticker, change_pct, volume=2_000_000, price=50.0):
    return {'ticker': ticker, 'price': price, 'change_pct': change_pct, 'volume': volume,
            'gap_pct': 1.0, 'rel_vol': 2.0, 'range_pct': 3.0, 'spread_pct': 0.05, 'vwap': price}


def test_fake_source_satisfies_protocol():
    assert isinstance(FakeSource([]), ScanSource)


def test_pipeline_ranks_enriches_and_labels():
    source = FakeSource([_candidate('AAA', 6.0), _candidate('BBB', 4.0)], notice='heads up')
    df = IdeaScanner(source).scan(preset='momentum', top_n=5, names=True, fundamentals=True, news=True)
    assert list(df['ticker'])[:2] == ['AAA', 'BBB']
    assert set(df['name']) == {'AAA Inc', 'BBB Inc'}
    assert df.attrs['ideas_notice'] == 'heads up'
    assert df.attrs['ideas_provider'] == 'fake'
    assert set(df['pe_ratio']) == {10.0}
    assert set(df['headline']) == {'AAA up', 'BBB up'}
    assert ('discover', 'movers', False) in source.calls


def test_market_scan_preset_passes_flag():
    source = FakeSource([_candidate('AAA', -6.0)])
    IdeaScanner(source).scan(preset='gap-down', top_n=5)
    assert source.calls[0] == ('discover', 'movers', True)


def test_empty_discovery_returns_empty_frame_with_notice():
    df = IdeaScanner(FakeSource([], notice='nothing found')).scan(preset='momentum')
    assert df.empty and df.attrs.get('ideas_notice') == 'nothing found'


class NoNamesSource(FakeSource):
    def names(self, tickers):
        return {}


def test_provider_supplied_name_kept_when_lookup_has_none():
    candidate = dict(_candidate('AAA', 6.0), name='Alpha Corp')
    df = IdeaScanner(NoNamesSource([candidate])).scan(preset='momentum', names=True)
    assert df.loc[0, 'name'] == 'Alpha Corp'


def test_looked_up_name_wins_over_provider_name():
    candidate = dict(_candidate('AAA', 6.0), name='Alpha Corp')
    df = IdeaScanner(FakeSource([candidate])).scan(preset='momentum', names=True)
    assert df.loc[0, 'name'] == 'AAA Inc'


def test_twelvedata_source_keeps_movers_fallback_notice(monkeypatch):
    from trader.data_providers.twelvedata.scan import TwelveDataScanSource
    from trader.tools.idea_scanner import IdeaScannerError, LIQUID_US_FALLBACK_TICKERS

    source = TwelveDataScanSource(td_client=None)
    entitlement = IdeaScannerError('TwelveData movers discovery failed for all directions: 403 Pro or Ultra plan')
    monkeypatch.setattr(source, '_discover', lambda *a, **k: (_ for _ in ()).throw(entitlement))
    seen = {}

    def fake_batch(symbols):
        seen['symbols'] = list(symbols)
        return [{'symbol': 'AAPL', 'close': '10', 'open': '9', 'high': '11', 'low': '9',
                 'previous_close': '9', 'volume': '1000', 'average_volume': '500', 'percent_change': '11'}]

    monkeypatch.setattr(source, '_batch_quote', fake_batch)
    discovery = source.discover('movers', None, None, False)
    assert seen['symbols'] == list(LIQUID_US_FALLBACK_TICKERS)
    assert discovery.candidates[0]['ticker'] == 'AAPL'
    assert discovery.notice


def test_twelvedata_source_news_and_names_are_empty():
    from trader.data_providers.twelvedata.scan import TwelveDataScanSource
    source = TwelveDataScanSource(td_client=None)
    assert source.news(['AAPL']) == {} and source.names(['AAPL']) == {}


class NoRatiosSource(FakeSource):
    name = 'alpaca'
    supports_fundamentals = False

    def fundamentals(self, tickers):
        raise IdeaScannerError('no ratios')


NO_RATIOS_NOTICE = ('Fundamentals are not available from Alpaca yet; '
                    'use --source massive or --source twelvedata for ratios.')


def test_fundamentals_if_available_skips_and_says_so_without_ratios():
    source = NoRatiosSource([_candidate('AAA', 6.0)], notice='15-minute delayed.')
    df = IdeaScanner(source).scan(preset='momentum', fundamentals_if_available=True)
    assert df.attrs['ideas_notice'] == f'15-minute delayed. {NO_RATIOS_NOTICE}'
    assert 'pe_ratio' not in df.columns


def test_fundamentals_if_available_notice_without_discovery_notice():
    df = IdeaScanner(NoRatiosSource([_candidate('AAA', 6.0)])).scan(preset='momentum',
                                                                    fundamentals_if_available=True)
    assert df.attrs['ideas_notice'] == NO_RATIOS_NOTICE


def test_explicit_fundamentals_still_raises_without_ratios():
    with pytest.raises(IdeaScannerError):
        IdeaScanner(NoRatiosSource([_candidate('AAA', 6.0)])).scan(preset='momentum', fundamentals=True)


def test_fundamentals_if_available_fetches_when_supported():
    source = FakeSource([_candidate('AAA', 6.0)], notice='heads up')
    df = IdeaScanner(source).scan(preset='momentum', fundamentals_if_available=True)
    assert set(df['pe_ratio']) == {10.0}
    assert df.attrs['ideas_notice'] == 'heads up'


def test_explicit_fundamentals_without_ratios_fails_before_discovery():
    source = NoRatiosSource([_candidate('AAA', 6.0)])
    with pytest.raises(IdeaScannerError, match='no ratios'):
        IdeaScanner(source).scan(preset='momentum', fundamentals=True)
    assert source.calls == []
