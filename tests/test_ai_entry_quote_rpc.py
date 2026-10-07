"""SP2 Plan 3 Task 10: ``get_ai_entry_quote`` serves the command stack's own quote authority and accepted feeds.

The IB quote authority is ``FakeQuotes`` (patched by ``_served``); the Alpaca IEX authority is a fake too, so no
network is used. The ``ai`` side reads the feed and the accepted set from the reply and never decides the set.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from tests.automation.ai_paper_fixtures import CONID, quote
from tests.test_ai_paper_rpc import FakeQuotes, _served, query
from trader.automation.ai_paper_config import AiPaperConfig
from trader.messaging.typed_rpc import TypedRpcRemoteError


class FakeIex:
    def __init__(self, *_args, **_kwargs):
        pass

    def executable_quote(self, conid, *, side):
        return replace(quote(conid=conid, feed="iex_realtime", bid=99.90, ask=100.05), side=side)


def ib_answers(monkeypatch, answer):
    def executable_quote(self, conid, *, side):
        if isinstance(answer, Exception):
            raise answer
        return None if answer is None else replace(answer, conid=conid, side=side)
    monkeypatch.setattr(FakeQuotes, "executable_quote", executable_quote)


def served_stack(tmp_path, monkeypatch, *, fallback: bool):
    import trader.trading.paper_quote_fallback as paper_quote_fallback
    monkeypatch.setattr(paper_quote_fallback, "AlpacaIexQuoteAuthority", FakeIex)

    def prepare(trader):
        trader.automation_quote_fallback = "alpaca_iex" if fallback else ""
        trader.alpaca_api_key_id, trader.alpaca_api_secret_key = "k", "s"
    return _served(tmp_path, monkeypatch, AiPaperConfig(enabled=True), prepare=prepare)


def read(stack, conid=CONID, principal="ai_supervisor"):
    return query(stack, principal).call("get_ai_entry_quote", {"conid": conid}, dict)


def test_the_entry_quote_carries_its_feed_and_the_accepted_set(tmp_path, monkeypatch):     # owner #74
    ib_answers(monkeypatch, quote(feed="delayed"))
    stack = served_stack(tmp_path, monkeypatch, fallback=True)
    try:
        reply = read(stack)
        assert (reply["conid"], reply["account_mode"], reply["accepted_feeds"]) == (CONID, "paper",
                                                                                   ["iex_realtime", "live"])
        assert reply["quote"]["feed"] == "iex_realtime"
        assert (reply["quote"]["bid"], reply["quote"]["ask"], reply["quote"]["session_state"]) == (
            99.90, 100.05, "continuous")
        assert reply["read_at"] and reply["quote"]["market_timestamp"]
    finally:
        stack.close()


def test_without_the_fallback_only_live_is_accepted(tmp_path, monkeypatch):
    ib_answers(monkeypatch, quote(feed="delayed"))
    stack = served_stack(tmp_path, monkeypatch, fallback=False)
    try:
        reply = read(stack)
        assert reply["accepted_feeds"] == ["live"] and reply["quote"]["feed"] == "delayed"
    finally:
        stack.close()


@pytest.mark.parametrize("answer", [None, RuntimeError("token=abc")])
def test_no_quote_is_null_not_an_error(tmp_path, monkeypatch, answer):
    ib_answers(monkeypatch, answer)
    stack = served_stack(tmp_path, monkeypatch, fallback=False)
    try:
        assert read(stack)["quote"] is None
    finally:
        stack.close()


@pytest.mark.parametrize("principal", ["ai_research", "cli", "dashboard", "strategy"])
def test_only_the_supervisor_reads_entry_quotes(tmp_path, monkeypatch, principal):
    stack = served_stack(tmp_path, monkeypatch, fallback=False)
    try:
        with pytest.raises(TypedRpcRemoteError) as exc:
            read(stack, principal=principal)
        assert exc.value.code == "PERMISSION_DENIED"
    finally:
        stack.close()


@pytest.mark.parametrize("bad", [{"conid": 0}, {"conid": True}, {"conid": "1"}, {"conid": 1, "extra": 1}])
def test_the_entry_quote_request_is_strict(tmp_path, monkeypatch, bad):
    stack = served_stack(tmp_path, monkeypatch, fallback=False)
    try:
        with pytest.raises(TypedRpcRemoteError) as exc:
            query(stack, "ai_supervisor").call("get_ai_entry_quote", bad, dict)
        assert exc.value.code == "VALIDATION_ERROR"
    finally:
        stack.close()
