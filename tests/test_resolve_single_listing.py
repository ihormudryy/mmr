"""Issue #179: a ticker with several IB listings is refused with each listing named, never the first one taken.

`mmr universe add downloads JPM XOM` stored the TSE (Toronto) listings, because IB answered them first.
"""
from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from trader import mmr_cli

JPM_NYSE = SimpleNamespace(symbol="JPM", conId=1520593, exchange="NYSE", primaryExchange="NYSE", currency="USD")
JPM_TSE = SimpleNamespace(symbol="JPM", conId=530147463, exchange="TSE", primaryExchange="TSE", currency="CAD")


def test_one_listing_is_returned():
    assert mmr_cli._single_listing("JPM", [JPM_NYSE]) is JPM_NYSE


def test_venue_duplicates_of_one_conid_are_one_listing():
    assert mmr_cli._single_listing("JPM", [JPM_NYSE, SimpleNamespace(**{**vars(JPM_NYSE), "exchange": "SMART"})]).conId == 1520593


def test_several_listings_are_refused_and_each_is_named():
    with pytest.raises(mmr_cli.AmbiguousListingError) as refused:
        mmr_cli._single_listing("JPM", [JPM_TSE, JPM_NYSE])
    message = str(refused.value)
    assert "530147463 TSE CAD" in message and "1520593 NYSE USD" in message
    assert "--exchange NYSE --currency USD" in message                    # the one USD listing, as the example


def test_the_example_names_no_exchange_when_there_is_no_single_usd_listing():
    cad = SimpleNamespace(**{**vars(JPM_TSE), "conId": 1})
    with pytest.raises(mmr_cli.AmbiguousListingError) as refused:
        mmr_cli._single_listing("JPM", [JPM_TSE, cad])
    assert "--exchange X --currency Y" in str(refused.value)


class _NoLocal:
    def resolve_symbol(self, symbol):
        return []


class _Trader:
    def __init__(self, defs):
        self.defs = defs

    def resolve(self, symbol):
        return self.defs


def test_a_download_target_with_several_listings_is_refused_and_not_registered(monkeypatch):
    registered = []
    monkeypatch.setattr(mmr_cli, "_register_in_downloads_universe", lambda accessor, d: registered.append(d))
    with pytest.raises(mmr_cli.DownloadTargetError) as refused:
        mmr_cli._resolve_download_symbol(_NoLocal(), _Trader([JPM_TSE, JPM_NYSE]), "JPM")
    assert "1520593 NYSE USD" in str(refused.value)
    assert registered == []


def test_universe_add_skips_a_ticker_with_several_listings(monkeypatch):
    inserted = []

    class Accessor:
        def __init__(self, *args):
            pass

        def insert(self, name, definition):
            inserted.append((name, definition.conId))

    class Sdk:
        def resolve(self, symbol, sec_type="STK", exchange="", currency=""):
            return [JPM_TSE, JPM_NYSE] if symbol == "JPM" else [SimpleNamespace(**{**vars(JPM_NYSE), "symbol": symbol,
                                                                                    "conId": 265598})]

    monkeypatch.setattr("trader.data.universe.UniverseAccessor", Accessor)
    monkeypatch.setattr("trader.container.Container.instance",
                        classmethod(lambda cls: SimpleNamespace(config=lambda: {"duckdb_path": "x", "universe_library": "U"})))
    args = argparse.Namespace(uni_action="add", name="downloads", symbols=["JPM", "AAPL"], exchange="", currency="",
                              sectype="STK")
    mmr_cli._handle_universe(Sdk(), args)
    assert inserted == [("downloads", 265598)]
