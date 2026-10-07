"""Paper-only Alpaca IEX quote fallback (issue #74). Fake Alpaca transport; no network."""
from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace

import pytest

from trader.trading.paper_quote_fallback import (
    LATEST_QUOTES_PATH, AlpacaIexQuoteAuthority, FallbackQuoteAuthority, alpaca_symbol,
)
from trader.trading.proposal_command_service import ExecutableQuote

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 17, 15, 0, tzinfo=UTC)          # Friday 11:00 ET, regular session
AFTER_CLOSE = dt.datetime(2026, 7, 17, 21, 0, tzinfo=UTC)  # Friday 17:00 ET
CONID = 265598
SECRET = "very-secret-alpaca-key"


def security(**changes):
    fields = dict(conId=CONID, symbol="AAPL", secType="STK", currency="USD",
                  exchange="SMART", primaryExchange="NASDAQ")
    fields.update(changes)
    return SimpleNamespace(**fields)


def alpaca_quote(**changes):
    fields = {"t": "2026-07-17T14:59:59.123456789Z", "bp": 210.0, "ap": 210.05,
              "bs": 3, "as": 4, "bx": "V", "ax": "V", "c": ["R"], "z": "C"}
    fields.update(changes)
    return fields


class FakeAlpaca:
    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        if self.error is not None:
            raise self.error
        return self.body


def iex(client, *, resolved=None, now=NOW):
    resolved = security() if resolved is None else resolved
    return AlpacaIexQuoteAuthority(client, resolve_security=lambda conid: resolved, now=lambda: now)


def ib_quote(feed):
    return ExecutableQuote(conid=CONID, side="ask", price=210.1, market_timestamp=NOW,
                           feed_type=feed, session_state="continuous", bid=210.0, ask=210.1)


class FakeIb:
    def __init__(self, quote):
        self.quote = quote

    def executable_quote(self, conid, *, side):
        return self.quote


# --- symbol mapping ------------------------------------------------------------

def test_a_class_share_space_becomes_a_dot():
    assert alpaca_symbol(CONID, security(symbol="BRK B")) == "BRK.B"


@pytest.mark.parametrize("changes", [
    {"secType": "OPT"}, {"currency": "CAD"}, {"primaryExchange": "TSE"},
    {"primaryExchange": "PINK"}, {"primaryExchange": "SMART"}, {"primaryExchange": None},
    {"conId": CONID + 1}, {"symbol": "brk b"}, {"symbol": "BRK  B"}, {"symbol": " AAPL"},
    {"symbol": "BRK.B"}, {"symbol": "A B C"}, {"symbol": ""}, {"symbol": None},
])
def test_any_other_contract_shape_has_no_alpaca_symbol(changes):
    assert alpaca_symbol(CONID, security(**changes)) is None


def test_an_unresolved_conid_has_no_alpaca_symbol():
    assert alpaca_symbol(CONID, None) is None


@pytest.mark.parametrize("changes", [{"secType": "OPT"}, {"currency": "EUR"}, {"symbol": "A B C"}])
def test_a_refused_contract_never_reaches_alpaca(changes):
    client = FakeAlpaca(body={"quotes": {}})
    assert iex(client, resolved=security(**changes)).executable_quote(CONID, side="ask") is None
    assert client.calls == []


# --- the IEX quote -------------------------------------------------------------

def test_iex_quote_has_its_own_timestamp_book_sizes_and_honest_label():
    client = FakeAlpaca(body={"quotes": {"BRK.B": alpaca_quote()}})
    quote = iex(client, resolved=security(symbol="BRK B")).executable_quote(CONID, side="ask")
    assert client.calls == [(LATEST_QUOTES_PATH, {"symbols": "BRK.B", "feed": "iex"})]
    assert quote == ExecutableQuote(
        conid=CONID, side="ask", price=210.05,
        market_timestamp=dt.datetime(2026, 7, 17, 14, 59, 59, 123456, tzinfo=UTC),
        feed_type="iex_realtime", session_state="continuous",
        bid=210.0, ask=210.05, bid_size=3.0, ask_size=4.0)
    assert quote.market_timestamp.utcoffset() == dt.timedelta(0)


@pytest.mark.parametrize(("side", "price"), [("ask", 210.05), ("BUY", 210.05), ("bid", 210.0), ("SELL", 210.0)])
def test_executable_price_is_the_side_of_the_book_crossed(side, price):
    client = FakeAlpaca(body={"quotes": {"AAPL": alpaca_quote()}})
    quote = iex(client).executable_quote(CONID, side=side)
    assert (quote.side, quote.price) == (side, price)


@pytest.mark.parametrize("now", [
    AFTER_CLOSE,                                          # after the close
    dt.datetime(2026, 7, 17, 13, 0, tzinfo=UTC),          # 09:00 ET pre-market
    dt.datetime(2026, 7, 18, 15, 0, tzinfo=UTC),          # Saturday
    dt.datetime(2026, 7, 3, 15, 0, tzinfo=UTC),           # Independence Day (observed)
])
def test_outside_the_regular_session_is_not_continuous(now):
    stamp = now.isoformat().replace("+00:00", "Z")
    client = FakeAlpaca(body={"quotes": {"AAPL": alpaca_quote(t=stamp)}})
    quote = iex(client, now=now).executable_quote(CONID, side="ask")
    assert quote.session_state == "closed"


def test_a_regular_session_quote_read_after_the_close_is_not_continuous():
    client = FakeAlpaca(body={"quotes": {"AAPL": alpaca_quote()}})
    assert iex(client, now=AFTER_CLOSE).executable_quote(CONID, side="ask").session_state == "closed"


def test_the_iex_quote_never_reports_a_halt():
    client = FakeAlpaca(body={"quotes": {"AAPL": alpaca_quote(c=["H"])}})
    assert iex(client).executable_quote(CONID, side="ask").session_state == "continuous"


@pytest.mark.parametrize("body", [
    {"quotes": {}}, {}, None, {"quotes": {"AAPL": None}}, {"quotes": {"MSFT": alpaca_quote()}},
    {"quotes": {"AAPL": alpaca_quote(t=None)}}, {"quotes": {"AAPL": alpaca_quote(t="")}},
    {"quotes": {"AAPL": alpaca_quote(t="2026-07-17T14:59:59")}},     # no zone: never guessed
    {"quotes": {"AAPL": alpaca_quote(t="not a time")}},
    {"quotes": {"AAPL": alpaca_quote(bp=0)}}, {"quotes": {"AAPL": alpaca_quote(ap=0)}},
    {"quotes": {"AAPL": alpaca_quote(ap=None)}}, {"quotes": {"AAPL": alpaca_quote(bp=float("nan"))}},
])
def test_no_usable_alpaca_quote_fails_closed(body):
    assert iex(FakeAlpaca(body=body)).executable_quote(CONID, side="ask") is None


def test_alpaca_error_fails_closed_without_logging_secrets(caplog):
    client = FakeAlpaca(error=RuntimeError(f"HTTP 500 for key {SECRET}"))
    with caplog.at_level(logging.DEBUG):
        assert iex(client).executable_quote(CONID, side="ask") is None
    assert SECRET not in caplog.text
    assert "RuntimeError" in caplog.text


def test_a_resolver_error_fails_closed():
    def broken(conid):
        raise RuntimeError("universe unavailable")

    client = FakeAlpaca(body={"quotes": {"AAPL": alpaca_quote()}})
    authority = AlpacaIexQuoteAuthority(client, resolve_security=broken, now=lambda: NOW)
    assert authority.executable_quote(CONID, side="ask") is None
    assert client.calls == []


# --- the wrapper ---------------------------------------------------------------

def test_a_live_ib_quote_is_used_and_alpaca_is_never_called():
    client = FakeAlpaca(body={"quotes": {"AAPL": alpaca_quote()}})
    live = ib_quote("live")
    wrapper = FallbackQuoteAuthority(FakeIb(live), iex(client), account_mode="paper")
    assert wrapper.executable_quote(CONID, side="ask") is live
    assert client.calls == []


@pytest.mark.parametrize("ib", [None, ib_quote("delayed"), ib_quote("frozen"), ib_quote("unknown")])
def test_without_a_live_ib_quote_paper_uses_the_iex_quote(ib):
    client = FakeAlpaca(body={"quotes": {"AAPL": alpaca_quote()}})
    wrapper = FallbackQuoteAuthority(FakeIb(ib), iex(client), account_mode="paper")
    assert wrapper.executable_quote(CONID, side="ask").feed_type == "iex_realtime"


@pytest.mark.parametrize("ib", [None, ib_quote("delayed")])
def test_without_an_iex_quote_the_ib_answer_is_kept(ib):
    wrapper = FallbackQuoteAuthority(FakeIb(ib), iex(FakeAlpaca(body={"quotes": {}})), account_mode="paper")
    assert wrapper.executable_quote(CONID, side="ask") is ib


@pytest.mark.parametrize("mode", ["live", "", "LIVE", None])
def test_the_wrapper_refuses_any_account_but_paper(mode):
    with pytest.raises(ValueError, match="paper"):
        FallbackQuoteAuthority(FakeIb(None), iex(FakeAlpaca()), account_mode=mode)
