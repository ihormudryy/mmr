"""Plan 3 Task 6: trading_filters.yaml on ai_paper entries (R6)."""
from __future__ import annotations

import os

import pytest

from tests.automation.ai_paper_fixtures import CONID, FakeUniverse, secdef
from trader.automation.ai_paper_filter import AiEntryFilter, MtimeCachedFilterLoader
from trader.trading.trading_filter import TradingFilter, TradingFilterError


def entry_filter(rows, **rule):
    return AiEntryFilter(universe=FakeUniverse({CONID: rows}), load_filter=lambda: TradingFilter(**rule))


@pytest.mark.parametrize("rule", [{"denylist": ["AAPL"]}, {"deny_exchanges": ["NASDAQ"]}, {"deny_sec_types": ["STK"]},
                                  {"allowlist": ["MSFT"]}, {"min_price": 500.0}])
def test_filter_uses_the_resolved_symbol_exchange_and_sec_type(rule):
    f = entry_filter(secdef("AAPL", "NASDAQ", "STK"), **rule)
    assert f.refusal(CONID, 100.0) == "TRADING_FILTER_DENIED"
    assert f.last_reason


def test_smart_routing_is_not_the_exchange():
    assert entry_filter(secdef(exchange="SMART"), deny_exchanges=["SMART"]).refusal(CONID, 100.0) is None


@pytest.mark.parametrize("rule", [{"exchanges": ["NASDAQ"]}, {"deny_exchanges": ["NYSE"]}, {}])
def test_a_missing_listing_exchange_is_unresolved(rule):                          # R6, R25: blank primaryExchange
    f = entry_filter(secdef("AAPL", "", "STK", exchange="SMART"), **rule)
    assert f.refusal(CONID, 100.0) == "INSTRUMENT_UNRESOLVED"                      # never "allowed", never SMART


def test_a_smart_routed_order_on_an_allowed_primary_listing_passes():
    assert entry_filter(secdef(exchange="SMART"), exchanges=["NASDAQ"]).refusal(CONID, 100.0) is None


def test_a_denylisted_primary_listing_is_refused_even_when_routed_by_smart():
    f = entry_filter(secdef(exchange="SMART"), deny_exchanges=["NASDAQ"])
    assert f.refusal(CONID, 100.0) == "TRADING_FILTER_DENIED"


def test_an_allowlist_of_only_smart_denies_every_entry():
    assert entry_filter(secdef(exchange="SMART"), exchanges=["SMART"]).refusal(CONID, 100.0) == "TRADING_FILTER_DENIED"


@pytest.mark.parametrize("rows", [[], [secdef("AAPL"), secdef("AAPL")], [secdef("AAPL", conid=4391)],
                                  [secdef("AAPL", conid=str(CONID))]])
def test_no_exact_single_instrument_is_unresolved(rows):
    assert entry_filter(rows).refusal(CONID, 100.0) == "INSTRUMENT_UNRESOLVED"


def test_a_failing_lookup_is_unresolved():
    class Broken:
        def resolve_symbol(self, *a, **k):
            raise RuntimeError("db down")
    assert AiEntryFilter(universe=Broken(), load_filter=TradingFilter).refusal(CONID, 100.0) == "INSTRUMENT_UNRESOLVED"


def test_an_unparsable_filter_is_unavailable():
    def broken():
        raise TradingFilterError("bad yaml")
    f = AiEntryFilter(universe=FakeUniverse({CONID: secdef()}), load_filter=broken)
    assert f.refusal(CONID, 100.0) == "TRADING_FILTER_UNAVAILABLE"


def test_a_filter_edit_is_seen_on_the_next_check(tmp_path):
    path = tmp_path / "trading_filters.yaml"
    path.write_text("denylist: []\n")
    f = AiEntryFilter(universe=FakeUniverse({CONID: secdef()}), load_filter=MtimeCachedFilterLoader(str(path)))
    assert f.refusal(CONID, 100.0) is None
    path.write_text("denylist: [AAPL]\n")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert f.refusal(CONID, 100.0) == "TRADING_FILTER_DENIED"


def test_the_loader_does_not_reparse_an_unchanged_file(tmp_path, monkeypatch):
    path = tmp_path / "trading_filters.yaml"
    path.write_text("denylist: []\n")
    loads = []
    real = TradingFilter.load
    monkeypatch.setattr(TradingFilter, "load", staticmethod(lambda p=None: loads.append(p) or real(p)))
    loader = MtimeCachedFilterLoader(str(path))
    loader(), loader()
    assert len(loads) == 1
