import datetime as dt

import pytest

from trader.data_providers.errors import ProviderError
from trader.data_providers.option_symbols import (
    OptionSymbol, build_option_symbol, parse_expiration_date, parse_option_symbol,
    parse_provider_option_symbol, to_alpaca_option_symbol, to_massive_option_ticker,
)

AAPL_CALL = OptionSymbol('AAPL', dt.date(2026, 11, 20), 'C', 250_000)


def test_parses_both_provider_forms():
    assert parse_option_symbol('O:AAPL261120C00250000') == AAPL_CALL
    assert parse_option_symbol('AAPL261120C00250000') == AAPL_CALL
    assert parse_option_symbol('  o:aapl261120c00250000 ') == AAPL_CALL


def test_fields_and_provider_spellings():
    assert AAPL_CALL.strike == 250.0 and AAPL_CALL.contract_type == 'call'
    assert AAPL_CALL.occ == 'AAPL261120C00250000'
    assert to_alpaca_option_symbol(AAPL_CALL) == 'AAPL261120C00250000'
    assert to_massive_option_ticker(AAPL_CALL) == 'O:AAPL261120C00250000'


def test_fractional_and_small_strikes():
    assert parse_option_symbol('O:SPY260619P00520500').strike == 520.5
    put = parse_option_symbol('O:F260320P00012500')
    assert (put.root, put.strike, put.contract_type) == ('F', 12.5, 'put')


def test_adjusted_root_with_digit():
    option = parse_option_symbol('AAPL1261120C00250000')
    assert option.root == 'AAPL1' and option.expiration == dt.date(2026, 11, 20)
    assert option.occ == 'AAPL1261120C00250000'


@pytest.mark.parametrize('text', [
    'O:AAPL',                      # no date/right/strike
    'AAPL261120X00250000',         # right must be C or P
    'AAPL261131C00250000',         # 31 November
    'AAPL  261120C00250000',       # OCC space padding inside the symbol
    'AAPL261120C0025000',          # 7-digit strike
    'AAPL261120C00000000',         # zero strike
    'TOOLONGX261120C00250000',     # root longer than 6 letters
    '',
    'ÄAPL261120C00250000',
    'AAPL261120C00250000 extra',
    None,
    'ﬁ261120C00250000',            # .upper() would turn the ligature into 'FI'
    'ıBM261120C00250000',          # dotless i upper-cases to ASCII 'I'
    'STRAßE261120C00250000',       # 'ß'.upper() == 'SS'
    'AAPL２６１１２０C00250000',   # full-width digits
    'AAPL261120C00250000 ',   # non-breaking space is not outer ASCII whitespace
])
def test_rejects_malformed_symbols(text):
    with pytest.raises(ValueError, match='Cannot parse option symbol'):
        parse_option_symbol(text)


def test_build_rounds_float_strike_exactly():
    # int(2.01 * 1000) == 2009: the old builder named a different contract.
    assert build_option_symbol('F', '2026-03-20', 2.01, 'c').occ == 'F260320C00002010'
    assert build_option_symbol('spy', dt.date(2026, 6, 19), 520.5, 'P').occ == 'SPY260619P00520500'


@pytest.mark.parametrize('root, expiration, strike, right', [
    ('AAPL', '2026-03-20', 250.0005, 'C'),     # finer than 1/1000
    ('AAPL', '2026-03-20', 0.0, 'C'),
    ('AAPL', '2026-03-20', -5.0, 'C'),
    ('AAPL', '2026-03-20', 100_000.0, 'C'),    # does not fit 8 digits
    ('AAPL', '2026-03-20', float('nan'), 'C'),
    ('AAPL', '2026-03-20', float('inf'), 'C'),
    ('AAPL', '2026-03-20', True, 'C'),
    ('AAPL', '2026-03-20', '250', 'C'),
    ('AAPL', '2026-3-20', 250.0, 'C'),
    ('AAPL', '2026-03-20', 250.0, 'X'),
    ('BRK B', '2026-03-20', 250.0, 'C'),
    ('ß', '2026-03-20', 250.0, 'C'),           # 'ß'.upper() == 'SS'
    ('ﬁ', '2026-03-20', 250.0, 'C'),           # 'ﬁ'.upper() == 'FI'
    ('ı', '2026-03-20', 250.0, 'C'),           # 'ı'.upper() == 'I'
    (None, '2026-03-20', 250.0, 'C'),
    ('AAPL', '2026-03-20', 250.0, None),
    ('AAPL', None, 250.0, 'C'),
    ('AAPL', dt.datetime(2026, 3, 20, 9, 30), 250.0, 'C'),
    ('AAPL', dt.date(2070, 3, 20), 250.0, 'C'),  # two-digit year would read back as 1970
])
def test_build_refuses_inexact_input(root, expiration, strike, right):
    with pytest.raises(ValueError):
        build_option_symbol(root, expiration, strike, right)


def test_build_symbol_round_trips_through_parse():
    option = build_option_symbol(' spy ', '2026-06-19', 520.5, ' p ')
    assert parse_option_symbol(option.occ) == option


def test_parse_expiration_date_is_strict():
    assert parse_expiration_date('2026-11-20') == dt.date(2026, 11, 20)
    for bad in ('2026-3-20', '20261120', '2026-11-31', 'next friday', '2026-W47-5', '',
                '2026-11-20\n', '２０２６-11-20', None):
        with pytest.raises(ValueError, match='YYYY-MM-DD'):
            parse_expiration_date(bad)


def test_alpaca_refuses_six_letter_roots():
    with pytest.raises(ValueError, match='alpaca'):
        to_alpaca_option_symbol(OptionSymbol('ABCDEF', dt.date(2026, 11, 20), 'C', 250_000))


def test_provider_symbol_parse_failure_is_a_provider_error():
    with pytest.raises(ProviderError, match="alpaca returned an option symbol MMR cannot parse: 'GARBAGE'"):
        parse_provider_option_symbol('alpaca', 'GARBAGE')


def test_options_data_helpers_delegate():
    from trader.tools.options_data import build_option_ticker, parse_option_ticker
    assert build_option_ticker('F', '2026-03-20', 2.01, 'C') == 'O:F260320C00002010'
    assert parse_option_ticker('AAPL260320C00250000') == {
        'symbol': 'AAPL', 'expiration': '2026-03-20', 'right': 'C', 'strike': 250.0}
    with pytest.raises(ValueError, match='Cannot parse'):
        parse_option_ticker('O:X')


def test_build_accepts_numpy_strikes():
    import numpy as np
    assert build_option_symbol('AAPL', '2026-11-20', np.int64(250), 'C').strike_thousandths == 250_000
    assert build_option_symbol('AAPL', '2026-11-20', np.float64(2.01), 'C').strike_thousandths == 2_010
