import datetime as dt
from unittest.mock import MagicMock

import pytest

from trader.data_providers.capabilities import OPTION_FIELDS, Capability, make_option_row
from trader.data_providers.errors import ProviderNotConfigured
from trader.data_providers.option_symbols import build_option_symbol, parse_option_symbol

EXPIRATION = (dt.date.today() + dt.timedelta(days=40)).isoformat()


def _rows():
    return [make_option_row(build_option_symbol('AAPL', EXPIRATION, strike, 'C'),
                            iv=40.0 - (strike - 330) * 0.05, underlying_price=333.75,
                            provider='alpaca', feed='indicative')
            for strike in range(250, 420, 10)]


def _mmr(provider):
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    mmr._provider = MagicMock(return_value=provider)
    return mmr


def test_expirations_ask_the_default_options_provider():
    provider = MagicMock()
    provider.expirations.return_value = ['2026-10-16', '2026-11-20']
    mmr = _mmr(provider)
    assert mmr.options_expirations('AAPL') == ['2026-10-16', '2026-11-20']
    mmr._provider.assert_called_once_with(Capability.OPTIONS, None)


def test_source_is_passed_through():
    provider = MagicMock()
    provider.expirations.return_value = []
    mmr = _mmr(provider)
    mmr.options_expirations('AAPL', source='massive')
    mmr._provider.assert_called_once_with(Capability.OPTIONS, 'massive')


def test_chain_uses_nearest_expiration_and_shared_columns():
    provider = MagicMock()
    provider.expirations.return_value = [EXPIRATION, '2099-01-15']
    provider.chain.return_value = _rows()
    frame = _mmr(provider).options_chain('AAPL', contract_type='call', strike_min=250, strike_max=410)
    provider.chain.assert_called_once_with('AAPL', EXPIRATION, 'call', 250, 410)
    assert list(frame.columns) == list(OPTION_FIELDS) and len(frame) == 17


def test_chain_without_expirations_is_an_empty_frame_with_columns():
    provider = MagicMock()
    provider.expirations.return_value = []
    frame = _mmr(provider).options_chain('AAPL')
    assert frame.empty and list(frame.columns) == list(OPTION_FIELDS)
    provider.chain.assert_not_called()


@pytest.mark.parametrize('kwargs, message', [
    ({'expiration': 'next friday'}, 'YYYY-MM-DD'),
    ({'expiration': '2026-3-20'}, 'YYYY-MM-DD'),
    ({'contract_type': 'straddle'}, "'call' or 'put'"),
    ({'strike_min': 300.0, 'strike_max': 200.0}, 'above strike_max'),
])
def test_chain_rejects_bad_input_before_any_request(kwargs, message):
    provider = MagicMock()
    with pytest.raises(ValueError, match=message):
        _mmr(provider).options_chain('AAPL', **kwargs)
    provider.chain.assert_not_called()


def test_snapshot_accepts_both_forms():
    provider = MagicMock()
    provider.contract.return_value = {'ticker': 'AAPL261120C00250000'}
    mmr = _mmr(provider)
    mmr.options_snapshot('O:AAPL261120C00250000')
    mmr.options_snapshot('aapl261120c00250000')
    expected = parse_option_symbol('AAPL261120C00250000')
    assert [call.args[0] for call in provider.contract.call_args_list] == [expected, expected]


def test_snapshot_rejects_garbage_before_any_request():
    provider = MagicMock()
    with pytest.raises(ValueError, match='Cannot parse option symbol'):
        _mmr(provider).options_snapshot('O:AAPL')
    provider.contract.assert_not_called()


def test_implied_uses_call_rows_and_labels_the_result():
    provider = MagicMock()
    provider.chain.return_value = _rows()
    result = _mmr(provider).options_implied('AAPL', EXPIRATION, 0.04)
    provider.chain.assert_called_once_with('AAPL', EXPIRATION, 'call')
    assert result['strikes_used'] == 17 and result['strikes_excluded'] == 0
    assert (result['provider'], result['feed']) == ('alpaca', 'indicative')


def test_missing_alpaca_keys_name_the_env_vars():
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    mmr._container = MagicMock()
    mmr._container.config.return_value = {'massive_api_key': 'k'}
    with pytest.raises(ProviderNotConfigured, match='ALPACA_API_KEY_ID'):
        mmr.options_expirations('AAPL')


def test_implied_without_any_call_names_underlying_expiration_and_provider():
    provider = MagicMock()
    provider.chain.return_value = []
    mmr = _mmr(provider)
    mmr._provider_default = MagicMock(return_value='alpaca')
    with pytest.raises(ValueError, match=f'No call contracts for AAPL {EXPIRATION} from alpaca'):
        mmr.options_implied('AAPL', EXPIRATION)
    with pytest.raises(ValueError, match='from massive'):
        mmr.options_implied('AAPL', EXPIRATION, source='massive')
