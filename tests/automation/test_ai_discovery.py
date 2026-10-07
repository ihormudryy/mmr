"""SP2 Plan 3 Task 9: the trader-owned Alpaca discovery reader (movers, most-actives, news; exact symbols)."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.automation.ai_paper_fixtures import CONID, NOW, make_history
from tests.automation.ai_paper_world import Clock
from tests.automation.discovery_fakes import (
    ACTIVES, ACTIVES_PATH, ARTICLE, MOVERS, MOVERS_PATH, NEWS_PATH, FakeAlpacaHistory, FakeIbDetails, FakeSession,
    NoWaitLimiter,
)
from tests.automation.test_ai_deployments import GOOD
from tests.automation.test_discretionary_deployment import BODY
from trader.automation.ai_deployments import AiDeployment, AiDeploymentStore, apply_ai_deployment_migration
from trader.automation.ai_discovery import AiDiscoveryReader, DiscoveryRefused, SymbolResolver
from trader.automation.ai_discovery_wire import DiscoverAiCandidatesRequest
from trader.automation.discretionary_deployment import DiscretionaryDeployment
from trader.automation.scope_evidence import DollarVolumeSource, IbContractEvidenceSource
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.data_providers.alpaca.client import AlpacaClient
from trader.data_providers.alpaca.movers import AlpacaMovers
from trader.data_providers.alpaca.news import AlpacaNews
from trader.data_providers.capabilities import Capability
from trader.data_providers.errors import ProviderNotConfigured


def request(digest, **changes):
    return DiscoverAiCandidatesRequest(**{"deployment_digest": digest, "movers_top": 10, "most_actives_top": 10,
                                          "watchlist": ["MSFT"], "news_per_symbol": 2, "news_symbols_max": 5,
                                          **changes})


@pytest.fixture
def reader(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    apply_ai_deployment_migration(SchemaMigrator(db))
    store = AiDeploymentStore(db, now=lambda: NOW)
    digest, _ = store.register_discretionary(DiscretionaryDeployment.from_json(BODY), principal="cli",
                                             command_id="aidep-1")
    strategy_digest, _ = store.register(AiDeployment.from_json(GOOD), principal="ai_research", command_id="s-1")
    history = make_history(str(tmp_path / "history.duckdb"))

    def build(*, movers=MOVERS, actives=ACTIVES, news=None, keys=True, max_lookups=40, deny=()):
        session = FakeSession({MOVERS_PATH: movers, ACTIVES_PATH: actives, NEWS_PATH: {"news": []}}, news=news)
        client = AlpacaClient("k", "s", session=session, limiter=NoWaitLimiter())

        def providers(capability):
            if not keys:
                raise ProviderNotConfigured("alpaca", [("alpaca_api_key_id", "ALPACA_API_KEY_ID")])
            return {Capability.MOVERS: AlpacaMovers(client), Capability.NEWS: AlpacaNews(client)}[capability]
        contracts = FakeIbDetails()
        clock = Clock()
        source = IbContractEvidenceSource(request_details=contracts, remember=lambda d: None, now=clock)
        alpaca_history = FakeAlpacaHistory()
        built = AiDiscoveryReader(
            providers=providers, resolver=SymbolResolver(contracts=source, now=clock, max_lookups=max_lookups),
            volumes=DollarVolumeSource(history=history, alpaca_history=lambda: alpaca_history, now=clock),
            deployments=store, filter_refusal=lambda c, p: f"{c.symbol} denied" if c.symbol in deny else None,
            now=clock)
        built.contracts, built.session, built.clock, built.alpaca_history = contracts, session, clock, alpaca_history
        built.digest, built.strategy_digest = digest, strategy_digest
        return built
    return build


def test_candidates_are_timestamped_delayed_and_merged(reader):
    r = reader()
    out = r.read(request(r.digest))
    assert (out.source, out.delayed, out.delay_minutes, out.deployment_digest) == ("alpaca", True, 15, r.digest)
    by = {c.symbol: c for c in out.candidates}
    assert by["AAPL"].origins == ["gainer", "most_active"] and by["AAPL"].conid == CONID
    assert by["AAPL"].source_timestamp == "2026-07-17T14:59:00Z" and by["AAPL"].delayed
    assert (by["AAPL"].price, by["AAPL"].change_pct, by["AAPL"].volume) == (101.0, 2.0, 5e7)
    assert by["MSFT"].origins == ["watchlist"] and by["PINKY"].origins == ["loser"]
    assert [c.symbol for c in out.candidates] == ["AAPL", "BRK.B", "PINKY", "SPY", "MSFT"]
    assert (out.coverage.movers.requested, out.coverage.movers.returned) == (20, 3)
    assert (out.coverage.most_actives.returned, out.coverage.most_actives.as_of) == (2, "2026-07-17T14:58:00Z")
    assert out.coverage.watchlist.returned == 1


def test_the_sources_are_asked_for_the_requested_sizes(reader):
    r = reader()
    r.read(request(r.digest, movers_top=7, most_actives_top=60))
    params = {path: params for path, params in r.session.requests if path != NEWS_PATH}
    assert params[MOVERS_PATH] == {"top": 7} and params[ACTIVES_PATH] == {"by": "volume", "top": 60}


def test_unresolved_symbols_are_reported_not_guessed(reader):
    r = reader(movers={**MOVERS, "losers": [{"symbol": "DUAL", "price": 9.0, "change": 1, "percent_change": 1}]})
    out = r.read(request(r.digest))
    by = {c.symbol: c for c in out.candidates}
    assert (by["BRK.B"].resolution, by["BRK.B"].conid) == ("SYMBOL_FORM_UNSUPPORTED", None)
    assert (by["DUAL"].resolution, by["DUAL"].conid) == ("AMBIGUOUS", None)
    assert by["DUAL"].scope_precheck.status == "NOT_CHECKED"
    assert out.coverage.resolution.unresolved == 2 and out.coverage.resolution.resolved == 3


def test_the_precheck_names_the_failed_part(reader):
    r = reader()
    by = {c.symbol: c for c in r.read(request(r.digest)).candidates}
    assert (by["PINKY"].scope_precheck.status, by["PINKY"].scope_precheck.part) == ("FAIL", "exchange")
    assert by["AAPL"].scope_precheck.status == "PASS"
    assert by["AAPL"].scope_precheck.median_dollar_volume_20d == 100_000_000.0
    assert (by["AAPL"].primary_exchange, by["AAPL"].stock_type) == ("NASDAQ", "COMMON")
    assert (by["MSFT"].scope_precheck.status, by["MSFT"].scope_precheck.part) == ("NOT_CHECKED", "price")


def test_a_denied_symbol_fails_the_filter_part(reader):
    r = reader(deny=("AAPL",))
    by = {c.symbol: c for c in r.read(request(r.digest)).candidates}
    assert (by["AAPL"].scope_precheck.status, by["AAPL"].scope_precheck.part) == ("FAIL", "trading_filter")


def test_a_low_delayed_price_fails_the_price_part(reader):
    r = reader(movers={**MOVERS, "gainers": [{"symbol": "AAPL", "price": 4.5, "change": 1, "percent_change": 30}]})
    by = {c.symbol: c for c in r.read(request(r.digest)).candidates}
    assert (by["AAPL"].scope_precheck.status, by["AAPL"].scope_precheck.part) == ("FAIL", "price")


def test_a_failed_source_is_reported_not_hidden(reader):                    # review focus 4
    r = reader(movers=500)
    out = r.read(request(r.digest))
    assert out.coverage.movers.failed and out.coverage.movers.error_code == "ProviderError"
    assert not out.coverage.most_actives.failed and out.coverage.complete is False
    assert {c.symbol for c in out.candidates} == {"SPY", "AAPL", "MSFT"}


def test_a_malformed_payload_is_a_failed_source(reader):
    r = reader(actives={"most_actives": "not a list"})
    out = r.read(request(r.digest))
    assert out.coverage.most_actives.failed and out.coverage.complete is False


def test_news_is_per_symbol_and_its_failures_count(reader):
    r = reader(news={"AAPL": [ARTICLE], "SPY": 500})
    out = r.read(request(r.digest))
    by = {c.symbol: c for c in out.candidates}
    assert by["AAPL"].news_status == "OK" and by["AAPL"].news[0].title == ARTICLE["headline"]
    assert by["SPY"].news_status == "FAILED" and out.coverage.news.failed_symbols == ["SPY"]
    assert by["PINKY"].news_status == "SKIPPED"                                   # a FAIL gets no news call
    assert by["BRK.B"].news_status == "SKIPPED"                                   # no conid, no news call
    assert (out.coverage.news.requested_symbols, out.coverage.news.returned_symbols) == (3, 2)
    assert out.coverage.complete is False


def test_news_is_bounded_and_can_be_off(reader):
    r = reader()
    out = r.read(request(r.digest, news_symbols_max=1))
    assert [c.news_status for c in out.candidates if c.news_status != "SKIPPED"] == ["OK"]
    out = r.read(request(r.digest, news_per_symbol=0))
    assert {c.news_status for c in out.candidates} == {"SKIPPED"} and out.coverage.news.requested_symbols == 0


def test_the_lookup_budget_is_reported(reader):
    r = reader(max_lookups=1)
    out = r.read(request(r.digest))
    assert any(c.resolution == "RESOLUTION_BUDGET" for c in out.candidates) and not out.coverage.complete


def test_an_ib_error_is_a_failed_resolution_and_not_cached(reader):
    r = reader()

    def broken(contract):
        r.contracts.calls += 1
        raise TimeoutError("socket")
    r._resolver._contracts._request_details = broken
    out = r.read(request(r.digest))
    assert {c.resolution for c in out.candidates} == {"RESOLUTION_FAILED", "SYMBOL_FORM_UNSUPPORTED"}
    assert out.coverage.resolution.failed == 4 and not out.coverage.complete


def test_blank_keys_fail_the_read_loudly(reader):
    r = reader(keys=False)
    with pytest.raises(DiscoveryRefused) as exc:
        r.read(request(r.digest))
    assert exc.value.code == "DISCOVERY_SOURCE_UNAVAILABLE"


def test_a_strategy_digest_is_refused(reader):
    r = reader()
    with pytest.raises(DiscoveryRefused) as exc:
        r.read(request(r.strategy_digest))
    assert exc.value.code == "DEPLOYMENT_KIND_MISMATCH"


def test_an_unknown_digest_is_refused(reader):
    r = reader()
    with pytest.raises(DiscoveryRefused) as exc:
        r.read(request("sha256:" + "0" * 64))
    assert exc.value.code == "DEPLOYMENT_NOT_SEALED"


def test_resolutions_are_cached_for_the_session(reader):
    r = reader()
    r.read(request(r.digest))
    calls = r.contracts.calls
    r.read(request(r.digest))
    assert r.contracts.calls == calls
    r.clock.advance(days=3)                                           # a new ET session date clears the cache
    r.read(request(r.digest))
    assert r.contracts.calls > calls


def test_volume_fetches_are_bounded_per_read(reader):
    r = reader()
    r._volume_budget = 0
    by = {c.symbol: c for c in r.read(request(r.digest)).candidates}
    assert (by["AAPL"].scope_precheck.status, by["AAPL"].scope_precheck.part) == ("NOT_CHECKED", "dollar_volume")
    assert r.alpaca_history.calls == 0
