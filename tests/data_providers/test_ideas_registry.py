from argparse import Namespace
from unittest.mock import MagicMock

import pandas as pd

from trader.data_providers.capabilities import Capability
from trader.data_providers.registry import ProviderRegistry


def test_ideas_sources_registered():
    from trader.data_providers.builtin import source_choices
    assert {'massive', 'twelvedata'} <= set(source_choices(Capability.IDEAS))


def test_massive_ideas_builder():
    from trader.data_providers.massive.scan import MassiveScanSource
    registry = ProviderRegistry.from_config({'massive_api_key': 'k'})
    assert isinstance(registry.get(Capability.IDEAS, 'massive'), MassiveScanSource)


def test_ideas_do_not_inherit_default_data_source():
    registry = ProviderRegistry.from_config({'default_data_source': 'twelvedata'})
    assert registry.default_source(Capability.IDEAS) == 'alpaca'


def test_scan_ideas_uses_registry_source():
    from trader.sdk import MMR
    from trader.data_providers.capabilities import Discovery
    mmr = object.__new__(MMR)

    class Source:
        name = 'fake'
        supports_fundamentals = True

        def discover(self, *a):
            return Discovery([{'ticker': 'AAA', 'price': 50.0, 'change_pct': 6.0, 'volume': 2_000_000,
                               'gap_pct': 1.0, 'rel_vol': 2.0, 'range_pct': 3.0, 'spread_pct': 0.05,
                               'vwap': 50.0}])

        def indicators(self, tickers, needed):
            return {}

        def names(self, t):
            return {}

        def fundamentals(self, t):
            return {}

        def news(self, t):
            return {}

    mmr._provider = MagicMock(return_value=Source())
    mmr._container = MagicMock()
    df = mmr.scan_ideas(preset='momentum', data_source='fake-source')
    mmr._provider.assert_called_once_with(Capability.IDEAS, 'fake-source')
    assert df.attrs['ideas_provider'] == 'fake'


def test_cli_ideas_source_from_registry_and_default_none():
    from trader.mmr_cli import build_parser
    parser = build_parser()
    assert parser.parse_args(['ideas']).source is None
    assert parser.parse_args(['ideas', '--source', 'twelvedata']).source == 'twelvedata'


def test_cli_ideas_prints_provider_error(capsys):
    from trader.data_providers.errors import ProviderNotConfigured
    from trader.mmr_cli import _handle_ideas
    mmr = MagicMock()
    mmr.scan_ideas.side_effect = ProviderNotConfigured('alpaca', [('alpaca_api_key_id', 'ALPACA_API_KEY_ID')])
    args = Namespace(presets=False, tickers=None, universe=None, fundamentals=False, detail=False, news=False,
                     source=None, location=None, preset='momentum', num=15, min_price=None, max_price=None,
                     min_volume=None, min_change=None, max_change=None, news_bodies=False, news_bodies_limit=3)
    _handle_ideas(mmr, args)
    assert 'ALPACA_API_KEY_ID' in capsys.readouterr().out


def test_alpaca_ideas_builder_wires_assets_history_news():
    from trader.data_providers.alpaca.scan import AlpacaScanSource
    registry = ProviderRegistry.from_config({'alpaca_api_key_id': 'k', 'alpaca_api_secret_key': 's'})
    source = registry.get(Capability.IDEAS)
    assert isinstance(source, AlpacaScanSource)
    assert source._history is not None and source._news is not None and source._assets is not None


def test_data_providers_override_keeps_massive():
    registry = ProviderRegistry.from_config({'data_providers': {'ideas': 'massive'}})
    assert registry.default_source(Capability.IDEAS) == 'massive'


def test_alpaca_is_a_registered_ideas_source():
    from trader.data_providers.builtin import source_choices
    assert 'alpaca' in source_choices(Capability.IDEAS)


def test_cli_ideas_prints_idea_scanner_error_without_traceback(capsys):
    from trader.mmr_cli import _handle_ideas
    from trader.tools.idea_scanner import IdeaScannerError
    mmr = MagicMock()
    mmr.scan_ideas.side_effect = IdeaScannerError('ideas --fundamentals has no free source yet')
    args = Namespace(presets=False, tickers=None, universe=None, fundamentals=True, detail=False, news=False,
                     source=None, location=None, preset='momentum', num=15, min_price=None, max_price=None,
                     min_volume=None, min_change=None, max_change=None, news_bodies=False, news_bodies_limit=3)
    _handle_ideas(mmr, args)
    assert 'no free source yet' in capsys.readouterr().out


def _ideas_args(**overrides):
    values = dict(presets=False, tickers=None, universe=None, fundamentals=False, detail=False, news=False,
                  source=None, location=None, preset='momentum', num=15, min_price=None, max_price=None,
                  min_volume=None, min_change=None, max_change=None, news_bodies=False, news_bodies_limit=3)
    values.update(overrides)
    return Namespace(**values)


def test_cli_ideas_detail_asks_for_fundamentals_only_if_available():
    from trader.mmr_cli import _handle_ideas
    mmr = MagicMock()
    mmr.scan_ideas.return_value = pd.DataFrame()
    _handle_ideas(mmr, _ideas_args(detail=True))
    kwargs = mmr.scan_ideas.call_args.kwargs
    assert kwargs['fundamentals'] is False
    assert kwargs['fundamentals_if_available'] is True
    assert kwargs['news'] is True and kwargs['names'] is True


def test_cli_ideas_fundamentals_flag_is_strict():
    from trader.mmr_cli import _handle_ideas
    mmr = MagicMock()
    mmr.scan_ideas.return_value = pd.DataFrame()
    _handle_ideas(mmr, _ideas_args(fundamentals=True))
    kwargs = mmr.scan_ideas.call_args.kwargs
    assert kwargs['fundamentals'] is True and kwargs['fundamentals_if_available'] is False


def test_scan_ideas_fallback_forwards_fundamentals_if_available(monkeypatch):
    from trader.sdk import MMR
    from trader.tools import idea_scanner
    mmr = object.__new__(MMR)
    scans = []

    class RecordingScanner:
        def __init__(self, source):
            self.source = source

        def scan(self, **kwargs):
            scans.append(kwargs)
            if len(scans) == 1:
                raise idea_scanner.IdeaScannerError('NOT_AUTHORIZED: plan does not include snapshots')
            return pd.DataFrame()

    monkeypatch.setattr(idea_scanner, 'IdeaScanner', RecordingScanner)
    monkeypatch.setattr(idea_scanner, 'is_data_entitlement_error', lambda ex: True)
    mmr._provider = MagicMock(return_value=MagicMock())
    mmr._provider_default = MagicMock(return_value='massive')
    mmr._container = MagicMock()
    monkeypatch.setattr(MMR, '_twelvedata_client', MagicMock())
    mmr.scan_ideas(preset='momentum', fundamentals_if_available=True)
    assert len(scans) == 2
    assert all(call['fundamentals_if_available'] is True for call in scans)


def _labelled_frame(provider, notice=None):
    df = pd.DataFrame([{'ticker': 'AAPL', 'price': 10.0, 'change_pct': 1.0, 'volume': 1000, 'score': 50.0,
                        'signal': 'BUY'}])
    df.attrs['ideas_provider'] = provider
    if notice:
        df.attrs['ideas_notice'] = notice
    return df


def test_cli_ideas_json_carries_provider_and_notice(monkeypatch, capsys):
    import json
    from trader import mmr_cli
    monkeypatch.setattr(mmr_cli, '_json_mode', True)
    mmr = MagicMock()
    mmr.scan_ideas.return_value = _labelled_frame('alpaca', 'Alpaca prices are 15-minute delayed.')
    mmr_cli._handle_ideas(mmr, _ideas_args())
    out = json.loads(capsys.readouterr().out)
    assert out['provider'] == 'alpaca'
    assert out['notice'] == 'Alpaca prices are 15-minute delayed.'
    assert out['data'][0]['ticker'] == 'AAPL'


def test_cli_ideas_json_empty_frame_keeps_labels(monkeypatch, capsys):
    import json
    from trader import mmr_cli
    monkeypatch.setattr(mmr_cli, '_json_mode', True)
    mmr = MagicMock()
    empty = pd.DataFrame()
    empty.attrs['ideas_provider'] = 'alpaca'
    mmr.scan_ideas.return_value = empty
    mmr_cli._handle_ideas(mmr, _ideas_args())
    out = json.loads(capsys.readouterr().out)
    assert out['data'] == [] and out['provider'] == 'alpaca' and out['notice'] == ''


def test_cli_ideas_title_names_the_provider_that_answered(monkeypatch, capsys):
    import json
    from trader import mmr_cli
    monkeypatch.setattr(mmr_cli, '_json_mode', True)
    mmr = MagicMock()
    mmr.scan_ideas.return_value = _labelled_frame('twelvedata', 'fell back to TwelveData quotes')
    mmr_cli._handle_ideas(mmr, _ideas_args(source='massive'))
    out = json.loads(capsys.readouterr().out)
    assert out['title'].endswith('twelvedata')
    assert out['provider'] == 'twelvedata'


def test_cli_ideas_no_news_note_follows_the_provider_that_answered(capsys):
    from trader.mmr_cli import _handle_ideas
    mmr = MagicMock()
    mmr.scan_ideas.return_value = _labelled_frame('twelvedata')
    _handle_ideas(mmr, _ideas_args(news=True))
    assert 'no news endpoint' in capsys.readouterr().out


def test_cli_ideas_no_news_note_absent_for_other_providers(capsys):
    from trader.mmr_cli import _handle_ideas
    mmr = MagicMock()
    mmr.scan_ideas.return_value = _labelled_frame('alpaca')
    _handle_ideas(mmr, _ideas_args(news=True, source='twelvedata'))
    assert 'no news endpoint' not in capsys.readouterr().out


class _RaisingSource:
    supports_fundamentals = False

    def __init__(self, name, error):
        self.name = name
        self.error = error

    def discover(self, *args):
        raise self.error


def _mmr_with_source(source, default):
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    mmr._provider = MagicMock(return_value=source)
    mmr._provider_default = MagicMock(return_value=default)
    mmr._container = MagicMock()
    return mmr


def test_massive_not_authorized_falls_back_to_twelvedata_with_notice(monkeypatch):
    from trader.data_providers.capabilities import Discovery
    from trader.data_providers.twelvedata import scan as td_scan
    from trader.sdk import MMR
    from trader.tools.idea_scanner import IdeaScannerError

    class TwelveDataFake:
        name = 'twelvedata'
        supports_fundamentals = True

        def __init__(self, client):
            pass

        def discover(self, *args):
            return Discovery([{'ticker': 'AAPL', 'price': 50.0, 'change_pct': 6.0, 'volume': 2_000_000,
                               'gap_pct': 1.0, 'rel_vol': 2.0, 'range_pct': 3.0, 'spread_pct': 0.05,
                               'vwap': 50.0}])

        def indicators(self, tickers, needed):
            return {}

    monkeypatch.setattr(td_scan, 'TwelveDataScanSource', TwelveDataFake)
    monkeypatch.setattr(MMR, '_twelvedata_client', MagicMock())
    massive = _RaisingSource('massive', IdeaScannerError('NOT_AUTHORIZED: plan does not include snapshots'))
    df = _mmr_with_source(massive, 'massive').scan_ideas(preset='momentum')
    assert df.attrs['ideas_provider'] == 'twelvedata'
    assert 'fell back to TwelveData quotes' in df.attrs['ideas_notice']
    assert list(df['ticker']) == ['AAPL']


def test_alpaca_entitlement_error_propagates_without_fallback(monkeypatch):
    import pytest
    from trader.data_providers.errors import ProviderEntitlementError
    from trader.data_providers.twelvedata import scan as td_scan

    constructed = []
    monkeypatch.setattr(td_scan, 'TwelveDataScanSource', lambda *a, **k: constructed.append(a))
    alpaca = _RaisingSource('alpaca', ProviderEntitlementError('alpaca', 'NOT_AUTHORIZED: plan does not allow'))
    with pytest.raises(ProviderEntitlementError):
        _mmr_with_source(alpaca, 'alpaca').scan_ideas(preset='momentum')
    assert constructed == []
