"""SP2 Plan 3 Task 10: ``discover_ai_candidates`` over signed typed RPC on the real command stack.

The caller holds no Alpaca key; the trader's own Alpaca adapters answer through a fake HTTP session, and
the IB scanner is asserted unused (spec 12, "Discovery route").
"""
from __future__ import annotations

import inspect

import pytest

from tests.automation.ai_paper_fixtures import CONID
from tests.automation.discovery_fakes import (
    ACTIVES, ACTIVES_PATH, MOVERS_PATH, NEWS_PATH, SPY, FakeAlpacaHistory, FakeIbDetails, FakeSession, NoWaitLimiter,
)
from tests.test_ai_paper_rpc import _served, query, register, register_discretionary
from trader.automation.ai_discovery_wire import DiscoverAiCandidatesResponse
from trader.automation.ai_paper_config import AiPaperConfig
from trader.container import Container
from trader.data_providers.alpaca.client import AlpacaClient
from trader.data_providers.alpaca.movers import AlpacaMovers
from trader.data_providers.alpaca.news import AlpacaNews
from trader.data_providers.capabilities import Capability
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.trading.trading_runtime import Trader

SCANNER_METHODS = ("reqScannerDataAsync", "reqScannerSubscription", "reqScannerData")


def body(digest, **changes):
    return {"deployment_digest": digest, "movers_top": 5, "most_actives_top": 5, "watchlist": [],
            "news_per_symbol": 1, "news_symbols_max": 3, **changes}


@pytest.fixture
def served(tmp_path, monkeypatch):
    for name in ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)                     # the caller side holds no Alpaca key
    session = FakeSession({MOVERS_PATH: 500, ACTIVES_PATH: ACTIVES, NEWS_PATH: {"news": []}})
    client = AlpacaClient("k", "s", session=session, limiter=NoWaitLimiter())
    scanned = []

    def prepare(trader):
        trader.provider_factory = lambda capability: {Capability.MOVERS: AlpacaMovers(client),
                                                      Capability.NEWS: AlpacaNews(client),
                                                      Capability.HISTORY: FakeAlpacaHistory()}[capability]
        trader.contract_details_port = FakeIbDetails()
        for name in SCANNER_METHODS:
            monkeypatch.setattr(trader.client.ib, name, lambda *a, **k: scanned.append(a), raising=False)
    stack = _served(tmp_path, monkeypatch, AiPaperConfig(enabled=True), prepare=prepare)
    stack.scanned, stack.session = scanned, session
    yield stack
    stack.close()


def test_discovery_over_signed_rpc_reports_partial_coverage_and_never_scans(served):   # review focus 4
    digest = register_discretionary(served)["outcome"]["digest"]
    out = query(served, "ai_supervisor").call("discover_ai_candidates", body(digest), DiscoverAiCandidatesResponse)
    assert out.coverage.movers.failed and not out.coverage.complete and out.delayed
    assert {c.symbol for c in out.candidates} == {"SPY", "AAPL"}
    assert {c.conid for c in out.candidates} == {SPY, CONID}
    assert served.scanned == []
    assert {path for path, _ in served.session.requests} <= {MOVERS_PATH, ACTIVES_PATH, NEWS_PATH}


def test_a_strategy_digest_is_refused_over_rpc(served):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, "ai_supervisor").call("discover_ai_candidates", body(register(served)), dict)
    assert exc.value.code == "DEPLOYMENT_KIND_MISMATCH"


@pytest.mark.parametrize("principal", ["ai_research", "cli", "dashboard", "strategy"])
def test_only_the_supervisor_reads_discovery(served, principal):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, principal).call("discover_ai_candidates", body("sha256:" + "a" * 64), dict)
    assert exc.value.code == "PERMISSION_DENIED"


@pytest.mark.parametrize("patch", [{"movers_top": 0}, {"movers_top": True}, {"watchlist": ["brk.b"]},
                                   {"news_symbols_max": 31}, {"most_actives_top": 101}, {"extra": 1},
                                   {"deployment_digest": "sha256:xyz"}])
def test_the_request_is_strict(served, patch):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, "ai_supervisor").call("discover_ai_candidates", body("sha256:" + "a" * 64, **patch), dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_blank_trader_keys_fail_loudly(tmp_path, monkeypatch):
    def blank_keys(trader):
        trader.alpaca_api_key_id, trader.alpaca_api_secret_key = "", ""
    stack = _served(tmp_path, monkeypatch, AiPaperConfig(enabled=True), prepare=blank_keys)   # real registry
    try:
        digest = register_discretionary(stack)["outcome"]["digest"]
        with pytest.raises(TypedRpcRemoteError) as exc:
            query(stack, "ai_supervisor").call("discover_ai_candidates", body(digest, news_per_symbol=0), dict)
        assert exc.value.code == "DISCOVERY_SOURCE_UNAVAILABLE"
    finally:
        stack.close()


def test_the_trader_takes_its_alpaca_keys_by_these_names(monkeypatch, tmp_path):
    parameters = inspect.signature(Trader.__init__).parameters
    assert parameters["alpaca_api_key_id"].default == "" and parameters["alpaca_api_secret_key"].default == ""

    class KeyHolder:                          # the Container fills a parameter from the upper-case env var
        def __init__(self, alpaca_api_key_id: str = "", alpaca_api_secret_key: str = ""):
            self.keys = (alpaca_api_key_id, alpaca_api_secret_key)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "id-1")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret-1")
    path = tmp_path / "trader.yaml"
    path.write_text("trading_mode: paper\nib_account: DU111111\n")
    assert Container.create(str(path)).resolve(KeyHolder).keys == ("id-1", "secret-1")
