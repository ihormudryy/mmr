import io
import json
from argparse import Namespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from rich.console import Console

from trader.data_providers.builtin import BUILTIN_DEFAULTS
from trader.data_providers.capabilities import Capability
from trader.data_providers.errors import ProviderNotConfigured
from trader.data_providers.registry import ProviderRegistry


@pytest.fixture
def trader_config(tmp_path, monkeypatch):
    monkeypatch.delenv('MMR_DEFAULT_DATA_SOURCE', raising=False)

    def write(yaml_text: str):
        path = tmp_path / 'trader.yaml'
        path.write_text(yaml_text)
        monkeypatch.setenv('TRADER_CONFIG', str(path))

    return write


def test_forex_defaults_ignore_default_data_source(trader_config, monkeypatch):
    from trader.mmr_cli import build_parser
    trader_config('default_data_source: twelvedata\n')
    monkeypatch.setenv('MMR_DEFAULT_DATA_SOURCE', 'twelvedata')
    parser = build_parser()
    assert parser.parse_args(['forex', 'snapshot', 'EURUSD']).source == 'ib'
    assert parser.parse_args(['forex', 'quote', 'EUR', 'USD']).source == 'ib'
    assert parser.parse_args(['forex', 'convert', 'EUR', 'USD', '100']).source is None
    assert parser.parse_args(['forex', 'snapshot-all']).source is None
    assert parser.parse_args(['forex', 'movers']).source is None
    registry = ProviderRegistry.from_config({'default_data_source': 'twelvedata'})
    assert registry.default_source(Capability.FOREX) == BUILTIN_DEFAULTS[Capability.FOREX]
    assert registry.default_source(Capability.MOVERS_FOREX) == BUILTIN_DEFAULTS[Capability.MOVERS_FOREX]


def test_forex_quote_default_honours_data_providers_forex(trader_config):
    from trader.mmr_cli import build_parser
    trader_config('data_providers: {forex: twelvedata}\n')
    parser = build_parser()
    assert parser.parse_args(['forex', 'snapshot', 'EURUSD']).source == 'twelvedata'
    assert parser.parse_args(['forex', 'quote', 'EUR', 'USD']).source == 'twelvedata'


def test_forex_quote_default_returns_unsupported_override_so_command_fails_loudly(trader_config, capsys):
    from trader.mmr_cli import _forex_quote_source_default, _handle_forex, build_parser
    from trader.sdk import MMR
    trader_config('data_providers: {forex: alpaca}\n')
    assert _forex_quote_source_default() == 'alpaca'
    args = build_parser().parse_args(['forex', 'snapshot', 'EURUSD'])
    assert args.source == 'alpaca'
    mmr = object.__new__(MMR)
    mmr._provider = lambda capability, source=None: ProviderRegistry.from_config({}).get(capability, source)
    _handle_forex(mmr, args)
    out = capsys.readouterr().out
    assert "source 'alpaca' does not support forex" in out
    assert 'massive' in out


def test_snapshot_all_arguments(trader_config):
    from trader.mmr_cli import build_parser
    trader_config('')
    args = build_parser().parse_args(['forex', 'snapshot-all', 'EUR', 'JPY', '--base', 'GBP'])
    assert args.symbols == ['EUR', 'JPY'] and args.base == 'GBP'
    assert build_parser().parse_args(['forex', 'snapshot-all']).base == 'USD'


@pytest.mark.parametrize('argv, needs_trader', [
    (['forex', 'snapshot', 'EURUSD'], True),
    (['forex', 'quote', 'EUR', 'USD'], True),
    (['fx', 'snapshot', 'EURUSD'], True),
    (['forex', 'snapshot', 'EURUSD', '--source', 'massive'], False),
    (['forex', 'quote', 'EUR', 'USD', '--source', 'twelvedata'], False),
    (['forex', 'convert', 'EUR', 'USD', '100'], False),
    (['forex', 'snapshot-all'], False),
    (['forex', 'movers'], False),
])
def test_forex_needs_trader_only_for_ib(trader_config, argv, needs_trader):
    from trader.mmr_cli import _forex_needs_trader, build_parser
    trader_config('')
    assert _forex_needs_trader(build_parser().parse_args(argv)) is needs_trader


def test_handle_forex_prints_provider_error(capsys):
    from trader.mmr_cli import _handle_forex
    mmr = MagicMock()
    mmr._provider_default.return_value = 'massive'
    mmr.forex_convert.side_effect = ProviderNotConfigured('massive', [('massive_api_key', 'MASSIVE_API_KEY')])
    _handle_forex(mmr, Namespace(fx_action='convert', from_currency='EUR', to_currency='USD', amount=100.0,
                                 source=None))
    assert 'MASSIVE_API_KEY' in capsys.readouterr().out


def test_handle_forex_prints_value_error(capsys):
    from trader.mmr_cli import _handle_forex
    mmr = MagicMock()
    mmr.forex_snapshot.side_effect = ValueError("not a currency pair: 'EUR'")
    _handle_forex(mmr, Namespace(fx_action='snapshot', pair='EUR', source='ib'))
    assert 'not a currency pair' in capsys.readouterr().out


@pytest.mark.parametrize('amount', ['0', '-5', 'nan', 'inf'])
def test_convert_with_bad_amount_prints_clean_error(trader_config, capsys, amount):
    from trader.mmr_cli import _handle_forex, build_parser
    from trader.sdk import MMR
    trader_config('')
    args = build_parser().parse_args(['forex', 'convert', 'EUR', 'USD', amount])
    mmr = object.__new__(MMR)
    mmr._provider = MagicMock()
    mmr._provider_default = MagicMock(return_value='massive')
    _handle_forex(mmr, args)
    assert 'positive' in capsys.readouterr().out
    mmr._provider.assert_not_called()


def test_handle_forex_passes_raw_codes_so_the_sdk_checks_them_before_upper_casing():
    from trader.mmr_cli import _handle_forex
    mmr = MagicMock()
    mmr._provider_default.return_value = 'massive'
    mmr.forex_snapshot_all.return_value = pd.DataFrame()
    _handle_forex(mmr, Namespace(fx_action='snapshot-all', base='usd', symbols=['eur'], source=None))
    mmr._provider_default.assert_called_once_with(Capability.FOREX)
    mmr.forex_snapshot_all.assert_called_once_with(base='usd', symbols=['eur'], source='massive')


def test_handle_forex_movers_resolves_movers_forex_default():
    from trader.mmr_cli import _handle_forex
    mmr = MagicMock()
    mmr._provider_default.return_value = 'massive'
    mmr.forex_movers.return_value = pd.DataFrame()
    _handle_forex(mmr, Namespace(fx_action='movers', losers=True, source=None))
    mmr._provider_default.assert_called_once_with(Capability.MOVERS_FOREX)
    mmr.forex_movers.assert_called_once_with(direction='losers', source='massive')


def _reject_constant(name):
    raise ValueError(f'not strict JSON: {name}')


def _strict_json(text):
    return json.loads(text, parse_constant=_reject_constant)


@pytest.fixture
def wide_console(monkeypatch):
    from trader import mmr_cli
    buffer = io.StringIO()
    monkeypatch.setattr(mmr_cli, 'console', Console(file=buffer, width=200, color_system=None))
    return buffer


@pytest.fixture
def json_mode(monkeypatch):
    from trader import mmr_cli
    monkeypatch.setattr(mmr_cli, '_json_mode', True)


def test_print_dict_json_turns_non_finite_numbers_into_null(json_mode, capsys):
    from trader.data_providers.capabilities import make_fx_conversion
    from trader.mmr_cli import print_dict
    record = make_fx_conversion('EUR', 'USD', 100.0, converted=112.25, rate=1.1225, as_of='2026-10-02')
    record['extra'] = {'f32': np.float32('nan'), 'items': [float('inf'), np.float64('-inf'), 1.5]}
    print_dict(record, title='Convert')
    payload = _strict_json(capsys.readouterr().out)
    assert payload['data']['bid'] is None and payload['data']['ask'] is None
    assert payload['data']['rate'] == 1.1225 and payload['data']['as_of'] == '2026-10-02'
    assert payload['data']['extra'] == {'f32': None, 'items': [None, None, 1.5]}


def test_forex_convert_json_parses_strictly(json_mode, capsys):
    from trader.data_providers.capabilities import make_fx_conversion
    from trader.mmr_cli import _handle_forex
    mmr = MagicMock()
    mmr._provider_default.return_value = 'frankfurter'
    mmr.forex_convert.return_value = make_fx_conversion('EUR', 'USD', 100.0, converted=112.25, rate=1.1225)
    _handle_forex(mmr, Namespace(fx_action='convert', from_currency='EUR', to_currency='USD', amount=100.0,
                                 source=None))
    assert _strict_json(capsys.readouterr().out)['data']['bid'] is None


def test_print_dict_table_shows_dash_for_missing_values(wide_console):
    from trader.data_providers.capabilities import make_fx_rate
    from trader.mmr_cli import print_dict
    print_dict(dict(make_fx_rate('EUR', 'USD', last=1.1225), extra=None), title='Forex Snapshot')
    out = wide_console.getvalue()
    assert 'nan' not in out.lower() and 'None' not in out
    assert '1.1225' in out and ' - ' in out


def _fx_rates_frame():
    from trader.data_providers.frankfurter import ECB_NOTE
    frame = pd.DataFrame([{'pair': 'USD/GBP', 'last': 0.7612, 'previous_close': 0.7625, 'change': -0.0013,
                           'change_pct': -0.004, 'as_of': '2026-10-02', 'source': 'frankfurter',
                           'note': ECB_NOTE}])
    frame.attrs['as_of'] = '2026-10-02'
    return frame


def test_print_df_keeps_two_decimals_by_default(wide_console):
    from trader.mmr_cli import print_df
    print_df(_fx_rates_frame())
    out = wide_console.getvalue()
    assert '0.76 ' in out and '-0.00 ' in out


def test_print_df_decimals_shows_fx_precision(wide_console):
    from trader.mmr_cli import FX_DECIMALS, print_df
    print_df(_fx_rates_frame(), decimals=FX_DECIMALS)
    out = wide_console.getvalue()
    assert '0.76120' in out and '-0.00130' in out and '-0.00400%' in out


def test_print_df_wraps_note_instead_of_cutting_it(monkeypatch):
    from trader import mmr_cli
    buffer = io.StringIO()
    monkeypatch.setattr(mmr_cli, 'console', Console(file=buffer, width=130, color_system=None))
    mmr_cli.print_df(_fx_rates_frame(), decimals=mmr_cli.FX_DECIMALS)
    out = buffer.getvalue()
    assert '…' not in out and 'quote' in out


def test_snapshot_all_uses_fx_precision_and_ecb_title(wide_console):
    from trader.mmr_cli import _handle_forex
    mmr = MagicMock()
    mmr._provider_default.return_value = 'frankfurter'
    mmr.forex_snapshot_all.return_value = _fx_rates_frame()
    _handle_forex(mmr, Namespace(fx_action='snapshot-all', base='USD', symbols=[], source=None))
    out = wide_console.getvalue()
    assert 'ECB daily, not live, 2026-10-02' in out
    assert '0.76120' in out


def test_forex_movers_title_names_ecb_date(json_mode, capsys):
    from trader.mmr_cli import _handle_forex
    frame = pd.DataFrame([{'ticker': 'EURUSD', 'name': 'EUR/USD', 'close': 1.1225, 'volume': float('nan'),
                           'change': -0.0073, 'change_pct': -0.646, 'provider': 'computed_fx',
                           'note': 'ECB daily rates 2026-10-02 vs 2026-10-01 (daily, not live)'}])
    frame.attrs['as_of'] = '2026-10-02'
    mmr = MagicMock()
    mmr._provider_default.return_value = 'computed_fx'
    mmr.forex_movers.return_value = frame
    _handle_forex(mmr, Namespace(fx_action='movers', losers=False, source=None))
    payload = _strict_json(capsys.readouterr().out)
    assert 'ECB daily, not live, 2026-10-02' in payload['title']
    assert payload['data'][0]['volume'] is None


def test_massive_forex_title_has_no_ecb_label(json_mode, capsys):
    from trader.mmr_cli import _handle_forex
    mmr = MagicMock()
    mmr.forex_snapshot_all.return_value = _fx_rates_frame().assign(source='massive', note='')
    _handle_forex(mmr, Namespace(fx_action='snapshot-all', base='USD', symbols=[], source='massive'))
    assert 'ECB' not in _strict_json(capsys.readouterr().out)['title']
