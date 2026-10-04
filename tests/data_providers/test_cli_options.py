import argparse
import datetime as dt
import json
import re
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from trader import mmr_cli
from trader.data_providers.capabilities import OPTION_FIELDS, make_option_row
from trader.data_providers.errors import ProviderEntitlementError, ProviderNotConfigured
from trader.data_providers.option_symbols import build_option_symbol

NO_KEYS = ProviderNotConfigured('alpaca', [('alpaca_api_key_id', 'ALPACA_API_KEY_ID'),
                                           ('alpaca_api_secret_key', 'ALPACA_API_SECRET_KEY')])


def _row(strike=250.0, **fields):
    defaults = dict(bid=82.65, ask=87.45, provider='alpaca', feed='indicative')
    return make_option_row(build_option_symbol('AAPL', '2026-11-20', strike, 'C'), **{**defaults, **fields})


def _json_out(capsys):
    return json.loads(capsys.readouterr().out.strip())


@pytest.fixture
def json_mode(monkeypatch):
    monkeypatch.setattr(mmr_cli, '_json_mode', True)


@pytest.mark.parametrize('argv', [
    ['options', 'expirations', 'AAPL'],
    ['options', 'chain', 'AAPL'],
    ['options', 'snapshot', 'AAPL261120C00250000'],
    ['options', 'implied', 'AAPL'],
    ['options', 'buy', 'AAPL', '-e', '3m', '-s', '250', '-r', 'C', '-q', '1', '--market'],
    ['options', 'sell', 'AAPL', '-e', '3m', '-s', '250', '-r', 'C', '-q', '1', '--market'],
])
def test_every_options_action_takes_source(argv):
    parser = mmr_cli.build_parser()
    assert parser.parse_args(argv).source is None
    assert parser.parse_args(argv + ['--source', 'massive']).source == 'massive'
    with pytest.raises(SystemExit):
        parser.parse_args(argv + ['--source', 'twelvedata'])


@pytest.mark.parametrize('expiration', ['foobar', '2026-3-20', '2026-11-31'])
def test_resolve_rejects_garbage_without_lookup(expiration):
    mmr = MagicMock()
    with pytest.raises(ValueError, match='YYYY-MM-DD or relative'):
        mmr_cli._resolve_expiration(mmr, 'AAPL', expiration)
    mmr.options_expirations.assert_not_called()


def test_resolve_relative_passes_source():
    mmr = MagicMock()
    mmr.options_expirations.return_value = [(dt.date.today() + dt.timedelta(days=88)).isoformat()]
    assert mmr_cli._resolve_expiration(mmr, 'AAPL', '3m', source='massive') is not None
    mmr.options_expirations.assert_called_once_with('AAPL', source='massive')


def test_format_option_number_and_feed_label():
    assert mmr_cli._format_option_number(float('nan'), '.1f', '%') == '—'
    assert mmr_cli._format_option_number(None, '.2f') == '—'
    assert mmr_cli._format_option_number(37.384, '.1f', '%') == '37.4%'
    assert 'not OPRA NBBO' in mmr_cli._option_feed_label('alpaca', 'indicative')
    assert mmr_cli._option_feed_label('massive', 'opra') == 'massive, OPRA feed'


def test_chain_table_renders_nan_as_dash(monkeypatch, capsys):
    monkeypatch.setattr(mmr_cli, '_json_mode', False)
    monkeypatch.setattr(mmr_cli.console, 'width', 250)
    mmr = MagicMock()
    mmr.options_chain.return_value = pd.DataFrame([_row()], columns=list(OPTION_FIELDS))
    mmr_cli._handle_options(mmr, argparse.Namespace(
        opt_action='chain', symbol='aapl', expiration=None, contract_type=None,
        strike_min=None, strike_max=None, source=None))
    out = capsys.readouterr().out
    assert '—' in out and 'nan' not in out.lower()
    assert 'indicative' in out and 'not OPRA NBBO' in out


def test_chain_json_has_labels_and_nulls(json_mode, capsys):
    mmr = MagicMock()
    mmr.options_chain.return_value = pd.DataFrame([_row()], columns=list(OPTION_FIELDS))
    mmr_cli._handle_options(mmr, argparse.Namespace(
        opt_action='chain', symbol='AAPL', expiration=None, contract_type=None,
        strike_min=None, strike_max=None, source='alpaca'))
    payload = _json_out(capsys)
    assert payload['data'][0]['feed'] == 'indicative' and payload['data'][0]['iv'] is None
    assert 'not OPRA NBBO' in payload['title']
    mmr.options_chain.assert_called_once_with('AAPL', expiration=None, contract_type=None,
                                              strike_min=None, strike_max=None, source='alpaca')


def test_snapshot_json_is_valid_without_nan_tokens(json_mode, capsys):
    mmr = MagicMock()
    mmr.options_snapshot.return_value = _row()
    mmr_cli._handle_options(mmr, argparse.Namespace(opt_action='snapshot', ticker='O:AAPL261120C00250000',
                                                    source=None))
    out = capsys.readouterr().out
    assert 'NaN' not in out
    payload = json.loads(out)
    assert payload['data']['iv'] is None and payload['data']['ticker'] == 'AAPL261120C00250000'


def test_provider_error_is_printed_not_raised(json_mode, capsys):
    mmr = MagicMock()
    mmr.options_expirations.side_effect = ProviderEntitlementError('massive refused options expirations')
    mmr_cli._handle_options(mmr, argparse.Namespace(opt_action='expirations', symbol='AAPL', source='massive'))
    payload = _json_out(capsys)
    assert payload['success'] is False and 'massive refused options expirations' in payload['message']


def test_buy_relative_expiry_without_keys_is_loud(json_mode, capsys):
    mmr = MagicMock()
    mmr.options_expirations.side_effect = NO_KEYS
    mmr_cli._handle_options(mmr, argparse.Namespace(
        opt_action='buy', symbol='AAPL', market=True, limit=None, expiration='3m',
        strike=250.0, right='C', quantity=1.0, source=None))
    payload = _json_out(capsys)
    assert payload['success'] is False and 'ALPACA_API_KEY_ID' in payload['message']
    mmr.buy_option.assert_not_called()


def test_buy_garbage_expiry_never_reaches_ib():
    mmr = MagicMock()
    with pytest.raises(ValueError, match='YYYY-MM-DD or relative'):
        mmr_cli._handle_options(mmr, argparse.Namespace(
            opt_action='buy', symbol='AAPL', market=True, limit=None, expiration='2026-3-20',
            strike=250.0, right='C', quantity=1.0, source=None))
    mmr.buy_option.assert_not_called()
    mmr.options_expirations.assert_not_called()


def test_sell_exact_date_places_order_without_data_provider(json_mode, capsys):
    mmr = MagicMock()
    mmr_cli._handle_options(mmr, argparse.Namespace(
        opt_action='sell', symbol='aapl', market=False, limit=3.5, expiration='2026-11-20',
        strike=250.0, right='P', quantity=2.0, source=None))
    mmr.sell_option.assert_called_once_with('AAPL', '2026-11-20', 250.0, 'P', 2.0, limit_price=3.5, market=False)
    mmr.options_expirations.assert_not_called()


def test_implied_json_carries_counts_and_labels(json_mode, capsys):
    mmr = MagicMock()
    mmr.options_implied.return_value = {'x': [1.0, 1.1], 'market_implied': [0.1], 'constant': [0.1],
                                        'strikes_used': 9, 'strikes_excluded': 4,
                                        'provider': 'alpaca', 'feed': 'indicative'}
    mmr_cli._handle_options(mmr, argparse.Namespace(opt_action='implied', symbol='AAPL',
                                                    expiration='2026-11-20', risk_free_rate=0.05, source=None))
    payload = _json_out(capsys)
    assert payload['data']['strikes_excluded'] == 4 and payload['data']['x'] == [1.0, 1.1]
    mmr.options_implied.assert_called_once_with('AAPL', '2026-11-20', 0.05, source=None)



def _reject_constant(token):
    raise ValueError(f'invalid JSON constant {token}')


def test_implied_json_has_no_nan_or_infinity_tokens(json_mode, capsys):
    mmr = MagicMock()
    mmr.options_implied.return_value = {
        'x': [250.0, float('inf')], 'market_implied': [float('nan'), 0.2],
        'constant': [np.float64('nan'), np.float64(0.3)], 'strikes_used': 9, 'strikes_excluded': 0,
        'provider': 'alpaca', 'feed': 'indicative', 'nested': ({'edge': float('-inf')},)}
    mmr_cli._handle_options(mmr, argparse.Namespace(opt_action='implied', symbol='AAPL',
                                                    expiration='2026-11-20', risk_free_rate=0.05, source=None))
    payload = json.loads(capsys.readouterr().out, parse_constant=_reject_constant)['data']
    assert payload['x'] == [250.0, None]
    assert payload['market_implied'] == [None, 0.2]
    assert payload['constant'] == [None, 0.3]
    assert payload['nested'] == [{'edge': None}]

def _mmr_with_default(provider='alpaca'):
    mmr = MagicMock()
    mmr._provider_default.return_value = provider
    return mmr


def test_resolve_tie_keeps_the_first_expiration():
    today = dt.date.today()
    target_days = 90
    earlier = (today + dt.timedelta(days=target_days - 5)).isoformat()
    later = (today + dt.timedelta(days=target_days + 5)).isoformat()
    mmr = MagicMock()
    mmr.options_expirations.return_value = [earlier, later]
    assert mmr_cli._resolve_expiration(mmr, 'AAPL', None) == earlier
    mmr.options_expirations.return_value = [later, earlier]
    assert mmr_cli._resolve_expiration(mmr, 'AAPL', None) == later


def test_snapshot_table_renders_missing_values_as_dash(monkeypatch, capsys):
    monkeypatch.setattr(mmr_cli, '_json_mode', False)
    monkeypatch.setattr(mmr_cli.console, 'width', 200)
    mmr = MagicMock()
    mmr.options_snapshot.return_value = _row(underlying='')
    mmr_cli._handle_options(mmr, argparse.Namespace(opt_action='snapshot', ticker='AAPL261120C00250000',
                                                    source=None))
    out = capsys.readouterr().out
    assert 'nan' not in out.lower() and '—' in out
    assert re.search(r'underlying\s*│\s*—', out)
    assert 'not OPRA NBBO' in out


def test_expirations_name_the_provider(json_mode, capsys):
    mmr = _mmr_with_default('alpaca')
    mmr.options_expirations.return_value = ['2026-11-20']
    mmr_cli._handle_options(mmr, argparse.Namespace(opt_action='expirations', symbol='aapl', source=None))
    payload = _json_out(capsys)
    assert payload['provider'] == 'alpaca' and 'alpaca' in payload['title']
    assert payload['data'][0]['expiration'] == '2026-11-20'


def test_expirations_table_title_names_the_provider(monkeypatch, capsys):
    monkeypatch.setattr(mmr_cli, '_json_mode', False)
    mmr = _mmr_with_default('alpaca')
    mmr.options_expirations.return_value = ['2026-11-20']
    mmr_cli._handle_options(mmr, argparse.Namespace(opt_action='expirations', symbol='AAPL', source='massive'))
    assert 'massive' in capsys.readouterr().out


def test_empty_expirations_name_underlying_and_provider(json_mode, capsys):
    mmr = _mmr_with_default('alpaca')
    mmr.options_expirations.return_value = []
    mmr_cli._handle_options(mmr, argparse.Namespace(opt_action='expirations', symbol='zzzzq', source=None))
    payload = _json_out(capsys)
    assert payload['success'] is False
    assert 'ZZZZQ' in payload['message'] and 'alpaca' in payload['message']


def test_empty_chain_says_no_chain_data_with_underlying_and_provider(json_mode, capsys):
    mmr = _mmr_with_default('alpaca')
    mmr.options_chain.return_value = pd.DataFrame(columns=list(OPTION_FIELDS))
    mmr_cli._handle_options(mmr, argparse.Namespace(
        opt_action='chain', symbol='AAPL', expiration=None, contract_type=None,
        strike_min=None, strike_max=None, source='massive'))
    payload = _json_out(capsys)
    assert payload['success'] is False
    assert 'No chain data for AAPL' in payload['message'] and 'massive' in payload['message']


def test_empty_chain_for_a_given_expiration_names_it(json_mode, capsys):
    mmr = _mmr_with_default('alpaca')
    mmr.options_chain.return_value = pd.DataFrame(columns=list(OPTION_FIELDS))
    mmr_cli._handle_options(mmr, argparse.Namespace(
        opt_action='chain', symbol='AAPL', expiration='2026-11-20', contract_type=None,
        strike_min=None, strike_max=None, source=None))
    message = _json_out(capsys)['message']
    assert '2026-11-20' in message and 'alpaca' in message
