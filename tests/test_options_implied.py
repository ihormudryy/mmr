import datetime as dt
import math
from types import SimpleNamespace as NS

import numpy as np
import pytest

from trader.data_providers.capabilities import make_option_row
from trader.data_providers.option_symbols import build_option_symbol
from trader.tools import chain
from trader.tools.chain import MIN_IMPLIED_STRIKES, implied_distribution, implied_inputs

NAN = float('nan')
TODAY = dt.date(2026, 10, 4)
EXPIRATION = '2026-11-20'   # 47 calendar days after TODAY


def _row(strike, iv, right='C', underlying_price=333.75, provider='alpaca', feed='indicative'):
    return make_option_row(build_option_symbol('AAPL', EXPIRATION, strike, right), iv=iv,
                           underlying_price=underlying_price, provider=provider, feed=feed)


def _smile(strikes=range(250, 420, 10)):
    return [_row(strike, 40.0 - (strike - 330) * 0.05) for strike in strikes]   # 17 calls


def test_inputs_use_fraction_iv_calendar_year_and_spot():
    frame, usable, excluded = implied_inputs(_smile(), EXPIRATION, TODAY)
    assert len(frame) == 17 and len(usable) == 17 and excluded == 0
    assert list(frame.columns) == ['IV', 'K', 'S', 'T']
    assert frame['T'].tolist() == pytest.approx([47 / 365] * 17)
    assert (frame['S'] == 333.75).all()
    assert frame['IV'].iloc[0] == pytest.approx(0.44)
    assert frame['K'].is_monotonic_increasing


def test_nan_and_zero_iv_rows_are_excluded_and_counted():
    rows = _smile() + [_row(255, NAN), _row(265, 0.0), _row(275, -1.0), _row(300, 30.0, right='P')]
    frame, usable, excluded = implied_inputs(rows, EXPIRATION, TODAY)
    assert len(frame) == 17 and excluded == 3      # the put is not a call, so not "excluded"
    assert frame['IV'].gt(0).all() and frame['IV'].notna().all()


def test_nan_rows_do_not_change_the_distribution():
    clean = implied_distribution(_smile(), EXPIRATION, 0.05, TODAY)
    noisy = implied_distribution(_smile() + [_row(255, NAN), _row(265, 0.0)], EXPIRATION, 0.05, TODAY)
    assert noisy['x'] == clean['x']
    assert np.allclose(noisy['market_implied'], clean['market_implied'])
    assert (noisy['strikes_used'], noisy['strikes_excluded']) == (17, 2)


def test_too_few_usable_strikes_raises():
    rows = _smile(range(250, 320, 10)) + [_row(strike, NAN) for strike in range(320, 420, 10)]
    with pytest.raises(ValueError, match=f'only 7 of 17 call strikes have an implied volatility; need at least {MIN_IMPLIED_STRIKES}'):
        implied_inputs(rows, EXPIRATION, TODAY)


def test_no_call_rows_at_all_gets_a_distinct_message():
    puts_only = [_row(strike, 40.0, right='P') for strike in range(250, 420, 10)]
    with pytest.raises(ValueError, match=f'no call contracts for {EXPIRATION}'):
        implied_inputs(puts_only, EXPIRATION, TODAY)
    with pytest.raises(ValueError, match='no call contracts'):
        implied_inputs([], EXPIRATION, TODAY)


def test_exactly_the_minimum_usable_strikes_passes():
    rows = _smile(range(250, 250 + 10 * MIN_IMPLIED_STRIKES, 10)) + [_row(strike, NAN) for strike in range(400, 420, 10)]
    frame, usable, excluded = implied_inputs(rows, EXPIRATION, TODAY)
    assert len(frame) == len(usable) == MIN_IMPLIED_STRIKES == 8 and excluded == 2


def test_calls_of_other_expirations_are_ignored():
    other = [make_option_row(build_option_symbol('AAPL', '2026-12-18', strike, 'C'), iv=50.0,
                             underlying_price=333.75, provider='alpaca', feed='indicative')
             for strike in range(250, 420, 10)]
    frame, usable, excluded = implied_inputs(_smile() + other, EXPIRATION, TODAY)
    assert len(frame) == 17 and excluded == 0
    assert {row['expiration'] for row in usable} == {EXPIRATION}


def test_boolean_iv_is_not_a_usable_volatility():
    row = _row(300, 40.0)
    row['iv'] = True
    frame, _, excluded = implied_inputs(_smile() + [row], EXPIRATION, TODAY)
    assert len(frame) == 17 and excluded == 1


def test_same_day_expiration_raises():
    with pytest.raises(ValueError, match='is not after'):
        implied_inputs(_smile(), EXPIRATION, dt.date(2026, 11, 20))


def test_missing_underlying_price_raises():
    rows = [_row(strike, 40.0, underlying_price=NAN) for strike in range(250, 420, 10)]
    with pytest.raises(ValueError, match='underlying price'):
        implied_inputs(rows, EXPIRATION, TODAY)


def test_distribution_result_labels_and_plain_lists():
    result = implied_distribution(_smile(), EXPIRATION, 0.05, TODAY)
    assert set(result) == {'x', 'market_implied', 'constant', 'strikes_used', 'strikes_excluded', 'provider', 'feed'}
    assert all(type(result[key]) is list for key in ('x', 'market_implied', 'constant'))
    assert all(type(value) is float for value in result['x'][:3])
    assert len(result['market_implied']) == len(result['x']) - 1 == len(result['constant'])
    assert (result['provider'], result['feed']) == ('alpaca', 'indicative')



def test_distribution_grid_stays_inside_the_usable_strikes():
    rows = _smile() + [_row(200, NAN), _row(450, 0.0)]   # unusable strikes must not widen the grid
    result = implied_distribution(rows, EXPIRATION, 0.05, TODAY)
    assert min(result['x']) >= 250 and max(result['x']) <= 410
    assert min(result['x']) == pytest.approx(250)


def test_steep_put_skew_gives_no_negative_probabilities():
    # A degree-5 fit of this skew blows up below the lowest strike; the grid must not go there.
    rows = [_row(strike, 25.0 + 20.0 * math.exp(-(strike - 220) / 25.0), underlying_price=260.0)
            for strike in range(220, 305, 5)]
    result = implied_distribution(rows, EXPIRATION, 0.05, TODAY)
    assert min(result['market_implied']) >= 0
    assert all(math.isfinite(value) for value in result['market_implied'] + result['constant'])

def test_get_option_dates_routes_through_massive_adapter(monkeypatch):
    contracts = [NS(expiration_date='2026-11-20'), NS(expiration_date='2026-10-16')]
    monkeypatch.setattr(chain, '_get_massive_client',
                        lambda api_key='': NS(list_options_contracts=lambda **kwargs: iter(contracts)))
    assert chain.get_option_dates('AAPL', api_key='k') == ['2026-10-16', '2026-11-20']


def test_implied_constant_dashboard_path_excludes_missing_iv(monkeypatch):
    expiration = (dt.date.today() + dt.timedelta(days=40)).isoformat()

    def snap(strike, iv):
        option = build_option_symbol('AAPL', expiration, strike, 'C')
        return NS(details=NS(ticker=f'O:{option.occ}'), last_quote=None, last_trade=None, day=None, greeks=None,
                  open_interest=None, implied_volatility=iv, break_even_price=None,
                  underlying_asset=NS(price=333.75))

    snaps = [snap(strike, 0.40 - (strike - 330) * 0.0005) for strike in range(250, 350, 10)]
    snaps += [snap(355, None), snap(365, None), snap(375, 0.0)]
    monkeypatch.setattr(chain, '_get_massive_client',
                        lambda api_key='': NS(list_snapshot_options_chain=lambda **kwargs: iter(snaps)))
    result = chain.implied_constant('AAPL', expiration, 0.05, api_key='k')
    assert (result['strikes_used'], result['strikes_excluded']) == (10, 3)
    assert (result['provider'], result['feed']) == ('massive', 'opra')
