"""SP2 Plan 3 Task 5: exact IB contract details and the 20-session dollar volume for the scope rule."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.automation.ai_paper_fixtures import CONID, NOW, daily_frame, make_history, quote
from tests.automation.ai_paper_world import Clock
from trader.automation.production_evidence import (
    latest_closed_sessions, liquidity_from_history, liquidity_from_sessions, local_twenty_sessions,
)
from trader.automation.scope_evidence import (
    DollarVolumeSource, IbContractEvidenceSource, ScopeEvidenceUnavailable,
)


def details(conid=CONID, symbol="AAPL", primary="NASDAQ", stock_type="COMMON", sec_type="STK"):
    return SimpleNamespace(contract=SimpleNamespace(conId=conid, symbol=symbol, secType=sec_type, currency="USD",
                                                    primaryExchange=primary), stockType=stock_type)


def source(rows, remembered=None):
    calls = []

    def request(contract):
        calls.append(contract)
        if isinstance(rows, Exception):
            raise rows
        return rows
    src = IbContractEvidenceSource(request_details=request, remember=(remembered if remembered is not None
                                                                      else []).append, now=lambda: NOW)
    return src, calls


class FakeAlpacaHistory:
    def __init__(self, build):
        self.build = build
        self.calls = 0

    def get_history(self, ticker, bar_size, start, end):
        self.calls += 1
        return self.build()


def test_by_conid_needs_exactly_one_matching_detail():
    remembered = []
    src, calls = source([details(), details(conid=CONID + 1)], remembered)
    evidence = src.by_conid(CONID)
    assert evidence.primary_exchange == "NASDAQ" and evidence.fetched_at == NOW and len(remembered) == 1
    assert calls[0].conId == CONID
    for rows in ([], [details(), details()]):
        with pytest.raises(ScopeEvidenceUnavailable):
            source(rows)[0].by_conid(CONID)


def test_an_ib_error_names_only_its_type():
    with pytest.raises(ScopeEvidenceUnavailable) as exc:
        source(TimeoutError("token=abc"))[0].by_conid(CONID)
    assert "TimeoutError" in exc.value.reason and "abc" not in exc.value.reason


def test_a_failing_remember_does_not_lose_the_evidence():
    def broken(details):
        raise OSError("universe locked")
    src = IbContractEvidenceSource(request_details=lambda contract: [details()], remember=broken, now=lambda: NOW)
    assert src.by_conid(CONID).conid == CONID


@pytest.mark.parametrize("rows,status", [
    ([details(), details(conid=1, symbol="AAPLW")], "RESOLVED"), ([], "NOT_FOUND"),
    ([details(), details(conid=2, primary="BATS")], "AMBIGUOUS")])
def test_by_symbol_is_exact(rows, status):
    assert source(rows)[0].by_symbol("AAPL").status == status


def test_by_symbol_asks_for_a_usd_smart_stock():
    src, calls = source([details()])
    resolution = src.by_symbol("AAPL")
    assert (resolution.conid, resolution.contract.stock_type) == (CONID, "COMMON")
    assert (calls[0].symbol, calls[0].secType, calls[0].exchange, calls[0].currency) == ("AAPL", "STK", "SMART",
                                                                                         "USD")


def test_class_shares_are_not_looked_up():
    src, calls = source([details()])
    assert src.by_symbol("BRK.B").status == "SYMBOL_FORM_UNSUPPORTED" and calls == []


def test_volume_prefers_local_bars_then_alpaca_and_caches_per_session(tmp_path):
    clock = Clock()
    alpaca = FakeAlpacaHistory(lambda: daily_frame(clock()))
    volumes = DollarVolumeSource(history=make_history(str(tmp_path / "h.duckdb")), alpaca_history=lambda: alpaca,
                                 now=clock)
    assert volumes.twenty_sessions(CONID, "AAPL").source == "local_daily_bars"
    unknown = volumes.twenty_sessions(4242, "XYZ")
    assert unknown.source == "alpaca_daily_bars" and unknown.median_dollar_volume == 100_000_000.0
    assert volumes.twenty_sessions(4242, "XYZ") is unknown and alpaca.calls == 1
    assert volumes.cached(4242) is unknown
    clock.advance(days=3)                                            # a new session closed: stale
    assert volumes.cached(4242) is None
    volumes.twenty_sessions(4242, "XYZ")
    assert alpaca.calls == 2


def test_a_missing_session_anywhere_is_unavailable(tmp_path):
    alpaca = FakeAlpacaHistory(lambda: daily_frame().iloc[1:])
    volumes = DollarVolumeSource(history=make_history(str(tmp_path / "h.duckdb")), alpaca_history=lambda: alpaca,
                                 now=Clock())
    with pytest.raises(ScopeEvidenceUnavailable) as exc:
        volumes.twenty_sessions(4242, "XYZ")
    assert "local" in exc.value.reason and "alpaca" in exc.value.reason


def test_an_alpaca_error_is_named_by_type_only(tmp_path):
    def broken():
        raise RuntimeError("key=secret")
    volumes = DollarVolumeSource(history=make_history(str(tmp_path / "h.duckdb")), alpaca_history=broken,
                                 now=Clock())
    with pytest.raises(ScopeEvidenceUnavailable) as exc:
        volumes.twenty_sessions(4242, "XYZ")
    assert "RuntimeError" in exc.value.reason and "secret" not in exc.value.reason


def test_the_window_is_the_twenty_latest_closed_sessions():
    sessions = latest_closed_sessions(NOW)
    assert len(sessions) == 20 and sessions[-1] < NOW.date()                 # 11:00 ET: today is not closed


def test_liquidity_from_history_is_unchanged(tmp_path):
    history = make_history(str(tmp_path / "h.duckdb"))
    old = liquidity_from_history(history, CONID, quote(), NOW)
    assert old == liquidity_from_sessions(local_twenty_sessions(history, CONID, NOW), quote())
