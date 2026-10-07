"""PR #83 thread 4210055756: a stored definition that disagrees with IB never prices or admits an entry.

Through ``build_command_stack`` and signed RPC: the trader's universe says conid 265598 is AAPL, IB's own
details say it is MSFT. IB has no live quote, so the paper IEX fallback is asked. Fake IB, fake Alpaca, no network.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from tests.automation.ai_paper_fixtures import CONID, quote
from tests.automation.discovery_fakes import FakeAlpacaHistory, FakeIbDetails, details
from tests.test_ai_paper_rpc import (
    FakeQuotes, _served, command, enter_body, granted_epoch, publish, register_discretionary,
)
from trader.automation.ai_paper_config import AiPaperConfig
from trader.data_providers.capabilities import Capability


class RecordingAlpaca:
    """Stands in for AlpacaClient: answers a fresh IEX quote for whatever symbol is asked."""
    requests: list = []

    def __init__(self, *_args, **_kwargs):
        pass

    def get_json(self, path, params):
        type(self).requests.append((path, dict(params)))
        symbol = params.get("symbols")
        return {"quotes": {symbol: {"t": "2026-07-17T15:00:00Z", "bp": 99.95, "ap": 100.0, "bs": 10_000,
                                    "as": 10_000}}}


@pytest.fixture
def served(tmp_path, monkeypatch):
    import trader.data_providers.alpaca.client as client_module

    RecordingAlpaca.requests = []
    monkeypatch.setattr(client_module, "AlpacaClient", RecordingAlpaca)
    monkeypatch.setattr(FakeQuotes, "executable_quote",
                        lambda self, conid, *, side: replace(quote(conid=conid, feed="delayed"), side=side))

    def prepare(trader):
        trader.automation_quote_fallback = "alpaca_iex"
        trader.alpaca_api_key_id, trader.alpaca_api_secret_key = "k", "s"
        trader.contract_details_port = FakeIbDetails({"MSFT": [details(conid=CONID, symbol="MSFT")]})
        trader.provider_factory = lambda capability: {Capability.HISTORY: FakeAlpacaHistory()}[capability]
    stack = _served(tmp_path, monkeypatch, AiPaperConfig(enabled=True), prepare=prepare)
    yield stack
    stack.close()


def test_a_conflicting_stored_row_refuses_the_entry_and_never_prices_the_wrong_symbol(served):
    assert served.trader.universe_accessor.resolve_symbol(CONID)[0].symbol == "AAPL"      # the stale stored row
    publish(served)
    digest = register_discretionary(served)["outcome"]["digest"]
    started = command(served, "cli").call("start_experiment", {"command_id": "start-1", "reason": "go"}, dict)
    assert started["outcome"]["state"] == "ARMED", started
    served.stack.experiments.monitor.recover()
    out = command(served, "ai_supervisor").call("submit_ai_paper_decision", enter_body(digest), dict,
                                                controller_epoch=granted_epoch(served))
    assert (out["state"], out["error_code"]) == ("REJECTED", "OUT_OF_DISCRETIONARY_SCOPE"), out
    detail = out["outcome"]["detail"]
    assert detail["part"] == "evidence_stale" and detail["reason"].startswith("INSTRUMENT_CONFLICT")
    assert served.orders.plans == []                                                       # no order sent
    asked = [params["symbols"] for _, params in RecordingAlpaca.requests]
    assert "AAPL" not in asked and set(asked) <= {"MSFT"}                     # never AAPL for the MSFT conid
