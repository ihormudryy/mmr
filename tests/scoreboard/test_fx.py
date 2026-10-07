import pytest

from tests.scoreboard.common import NOW
from trader.scoreboard.fx import FxEvidenceError, IbFxEvidence


def _fx(cash):
    return IbFxEvidence(lambda: cash, now=lambda: NOW).evidence()


def test_usd_base_is_rate_one_and_says_so():
    e = _fx({"base_currency": "USD", "currencies": {}})
    assert (e.base_currency, e.usd_per_base, e.source, e.as_of) == ("USD", 1.0, "base_is_usd", NOW)


def test_cad_base_inverts_the_ib_rate():
    e = _fx({"base_currency": "CAD", "currencies": {"USD": {"exchange_rate": 1.36}}})
    assert e.usd_per_base == pytest.approx(0.7353, abs=1e-4) and e.source == "ib_account_values"


@pytest.mark.parametrize("currencies", [{}, {"USD": {"exchange_rate": None}}, {"USD": {"exchange_rate": 0}},
                                        {"USD": {"exchange_rate": float("nan")}}, {"USD": {"exchange_rate": "1.1"}}])
def test_eur_base_without_a_usable_usd_rate_is_unknown(currencies):
    assert _fx({"base_currency": "EUR", "currencies": currencies}).usd_per_base is None


@pytest.mark.parametrize("cash", [{}, {"base_currency": None}, {"base_currency": "eu"}, None])
def test_missing_base_currency_raises(cash):
    with pytest.raises(FxEvidenceError):
        _fx(cash)
