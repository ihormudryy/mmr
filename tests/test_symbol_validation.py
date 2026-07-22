"""Format gate for watchlist / universe symbol tokens."""
from __future__ import annotations

import pytest

from trader.common.symbol_validation import (
    SymbolValidationError,
    validate_symbol_format,
    validate_symbol_list,
)


@pytest.mark.parametrize('sym', ['AAPL', 'BRK.B', 'BF-B', '0700', '7203'])
def test_accepts_real_ticker_shapes(sym):
    assert validate_symbol_format(sym) == sym


@pytest.mark.parametrize('sym', ['', '!!!', 'AAPL!', '..', '-AAPL', 'AAPL.', 'a' * 40])
def test_rejects_trash(sym):
    with pytest.raises(SymbolValidationError):
        validate_symbol_format(sym)


def test_list_dedupes_and_normalizes():
    assert validate_symbol_list(['aapl', 'AAPL', 'msft']) == ['AAPL', 'MSFT']


def test_list_reports_all_bad_tokens():
    with pytest.raises(SymbolValidationError, match='invalid symbol'):
        validate_symbol_list(['AAPL', '!!!', '$$$'])
