from types import SimpleNamespace as NS

import pytest

from trader.tools.options_data import (
    build_option_ticker,
    chain_records,
    contract_snapshot,
    parse_option_ticker,
)


def test_parse_option_ticker_extracts_components():
    parsed = parse_option_ticker("O:AAPL260320C00250000")
    assert parsed == {
        "symbol": "AAPL",
        "expiration": "2026-03-20",
        "right": "C",
        "strike": 250.0,
    }


def test_build_option_ticker_round_trips():
    ticker = build_option_ticker("AAPL", "2026-03-20", 250.0, "c")
    assert ticker == "O:AAPL260320C00250000"
    assert parse_option_ticker(ticker)["strike"] == 250.0


def test_parse_option_ticker_rejects_garbage():
    with pytest.raises(ValueError):
        parse_option_ticker("O:AAPL")


def test_chain_records_normalizes_and_filters_by_strike():
    snap = NS(
        details=NS(ticker="O:AAPL260320C00250000", contract_type="call",
                   strike_price=250.0, expiration_date="2026-03-20"),
        last_quote=NS(bid=12.0, ask=12.4),
        last_trade=NS(price=12.2),
        day=NS(volume=1500),
        greeks=NS(delta=0.55, gamma=0.02, theta=-0.03, vega=0.10),
        open_interest=800, implied_volatility=0.31,
        break_even_price=262.0, underlying_asset=NS(price=248.0),
    )
    low = NS(details=NS(ticker="x", contract_type="call", strike_price=100.0,
                        expiration_date="2026-03-20"),
             last_quote=None, last_trade=None, day=None, greeks=None,
             open_interest=0, implied_volatility=0.0, break_even_price=0.0,
             underlying_asset=None)
    client = NS(list_snapshot_options_chain=lambda **kw: [snap, low])

    rows = chain_records(client, "AAPL", expiration="2026-03-20",
                         contract_type="call", strike_min=200.0, strike_max=None)

    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "O:AAPL260320C00250000"
    assert row["strike"] == 250.0
    assert row["mid"] == pytest.approx(12.2)
    assert row["iv"] == pytest.approx(31.0)  # percent
    assert row["delta"] == 0.55
    assert row["underlying_price"] == 248.0


def test_contract_snapshot_normalizes_single_contract():
    client = NS(get_snapshot_option=lambda **kw: NS(
        break_even_price=262.0, implied_volatility=0.31, open_interest=800,
        last_quote=NS(bid=12.0, ask=12.4), last_trade=NS(price=12.2),
        greeks=NS(delta=0.55, gamma=0.02, theta=-0.03, vega=0.10),
        underlying_asset=NS(price=248.0), day=NS(volume=1500),
    ))
    result = contract_snapshot(client, "O:AAPL260320C00250000")
    assert result["symbol"] == "AAPL"
    assert result["strike"] == 250.0
    assert result["right"] == "C"
    assert result["bid"] == 12.0
    assert result["implied_volatility"] == "31.00%"


def test_chain_records_omits_greeks_and_iv_when_absent():
    from types import SimpleNamespace as NS
    snap = NS(
        details=NS(ticker="O:AAPL260320P00250000", contract_type="put",
                   strike_price=250.0, expiration_date="2026-03-20"),
        last_quote=NS(bid=1.0, ask=1.2), last_trade=None, day=None,
        greeks=None, open_interest=0, implied_volatility=None,
        break_even_price=0.0, underlying_asset=None)
    client = NS(list_snapshot_options_chain=lambda **kw: [snap])
    rows = chain_records(client, "AAPL", expiration="2026-03-20",
                         contract_type=None, strike_min=None, strike_max=None)
    assert len(rows) == 1
    row = rows[0]
    assert "iv" not in row
    assert "delta" not in row and "gamma" not in row
    assert "theta" not in row and "vega" not in row


def test_contract_snapshot_omits_iv_and_greeks_when_absent():
    from types import SimpleNamespace as NS
    client = NS(get_snapshot_option=lambda **kw: NS(
        break_even_price=0.0, implied_volatility=None, open_interest=0,
        last_quote=None, last_trade=None, greeks=None,
        underlying_asset=None, day=None))
    result = contract_snapshot(client, "O:AAPL260320C00250000")
    assert "implied_volatility" not in result
    assert "delta" not in result
