"""SP2 Plan 6 Task 3: only in-scope candidates reach a model; coverage is recorded as it really was (spec 9, 10)."""
import json

import pytest

from tests.ai.decisions.fakes import AAPL, DISCRETIONARY_DIGEST, MSFT, NOW, FakeReads, trader_down
from tests.ai.fakes import FakeClock
from trader.ai.config import DiscoverySettings
from trader.ai.discovery_client import DiscoveryClient, NewsLine
from trader.ai.replay import ReplayRecorder
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.store import AiStore
from trader.ai.tools import LiveTools

CYCLE = "cyc-entry-20260717-1100"


def candidate(symbol, conid, status="PASS", part=None, resolution="RESOLVED", change=2.0):
    return {"symbol": symbol, "origins": ["gainer"], "conid": conid, "resolution": resolution,
            "primary_exchange": "NASDAQ", "stock_type": "COMMON", "price": 100.0, "change_pct": change,
            "volume": 1e6, "source_timestamp": "2026-07-17T14:59:00+00:00", "delayed": True,
            "scope_precheck": {"status": status, "part": part, "reason": "x", "median_dollar_volume_20d": 1e8},
            "news_status": "OK", "news": [{"id": "1", "published": "2026-07-17T14:00:00+00:00", "title": "t",
                                           "summary": "s", "url": "https://example.test/1", "source": "benzinga"}]}


def response(candidates, *, complete=True, movers_failed=False):
    source = {"requested": 10, "returned": 2, "failed": False, "error_code": None, "as_of": None}
    return {"read_at": NOW.isoformat(), "source": "alpaca", "delayed": True, "delay_minutes": 15,
            "deployment_digest": DISCRETIONARY_DIGEST,
            "coverage": {"movers": {**source, "failed": movers_failed,
                                    "error_code": "ProviderError" if movers_failed else None},
                         "most_actives": source, "watchlist": {**source, "requested": 0, "returned": 0},
                         "news": {"requested_symbols": 2, "returned_symbols": 2, "failed_symbols": []},
                         "resolution": {"requested": 2, "resolved": 2, "unresolved": 0, "failed": 0},
                         "complete": complete},
            "candidates": candidates}


async def read_with(tmp_path, reply, settings=None):
    clock = FakeClock(NOW)
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    store.migrate(ALL_MIGRATIONS)
    reads = FakeReads(discover_ai_candidates=reply)
    tools = LiveTools(unit_key=CYCLE, reads=reads, recorder=ReplayRecorder(store), clock=clock, gateway=None,
                      deadline=None)
    client = DiscoveryClient(store=store, settings=settings or DiscoverySettings(),
                             deployment_digest=DISCRETIONARY_DIGEST, clock=clock)
    return await client.read(tools, CYCLE), store, reads


@pytest.mark.asyncio
async def test_only_pass_candidates_reach_the_model(tmp_path):                          # review focus 4
    read, store, reads = await read_with(tmp_path, response([
        candidate("AAPL", AAPL), candidate("PINKY", 99, status="FAIL", part="exchange"),
        candidate("NEWCO", 98, status="NOT_CHECKED"), candidate("GHOST", None, resolution="NOT_FOUND"),
        candidate("MSFT", MSFT)]))
    assert [(c.ref, c.symbol, c.conid) for c in read.eligible] == [("C1", "AAPL", AAPL), ("C2", "MSFT", MSFT)]
    assert read.dropped == {"SCOPE_exchange": 1, "SCOPE_NOT_CHECKED": 1, "CONID_MISSING": 1}
    assert reads.calls[0][1]["deployment_digest"] == DISCRETIONARY_DIGEST
    row = store.db.execute("SELECT status, complete, seen, eligible, dropped_json FROM ai_discovery_reads",
                           fetch="one")
    assert row[:4] == ("OK", True, 5, 2) and json.loads(row[4])["SCOPE_exchange"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    response([candidate("AAPL", AAPL)], complete=False),
    response([candidate("AAPL", AAPL)], complete=True, movers_failed=True),            # contradicting flag
    response([candidate("AAPL", AAPL), candidate("LATE", None, resolution="RESOLUTION_BUDGET")], complete=True)])
async def test_partial_scan_is_never_recorded_complete(tmp_path, reply):               # review focus 4
    read, store, _ = await read_with(tmp_path, reply)
    assert read.ok and read.complete is False
    assert store.db.execute("SELECT complete FROM ai_discovery_reads", fetch="one") == (False,)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,code", [
    (trader_down(), "TRADER_UNREACHABLE"), ({"candidates": []}, "DISCOVERY_REPLY_INVALID"),
    ({**response([]), "deployment_digest": "sha256:" + "e" * 64}, "DISCOVERY_DEPLOYMENT_MISMATCH")])
async def test_a_failed_read_is_recorded_and_presents_no_candidates(tmp_path, reply, code):
    read, store, _ = await read_with(tmp_path, reply)
    assert (read.ok, read.error_code, read.eligible, read.complete) == (False, code, (), False)
    assert store.db.execute("SELECT status, error_code FROM ai_discovery_reads", fetch="one") == ("FAILED", code)


@pytest.mark.asyncio
async def test_candidates_are_capped_in_discovery_order(tmp_path):
    read, store, _ = await read_with(tmp_path, response([candidate("AAPL", AAPL), candidate("MSFT", MSFT)]),
                                     settings=DiscoverySettings(max_candidates_to_model=1))
    assert [c.ref for c in read.eligible] == ["C1"] and read.eligible[0].symbol == "AAPL"
    assert read.dropped == {"OVER_LIMIT": 1}


@pytest.mark.asyncio
async def test_news_is_kept_as_untrusted_lines_only_for_eligible_candidates(tmp_path):
    read, _, _ = await read_with(tmp_path, response([
        candidate("AAPL", AAPL), candidate("PINKY", 99, status="FAIL", part="exchange")]))
    (only,) = read.eligible
    assert only.news == (NewsLine("2026-07-17T14:00:00+00:00", "t", "s", "benzinga"),)
    assert "https://example.test/1" not in repr(read.eligible)               # the URL is never shown to a model
    assert only.median_dollar_volume == 1e8 and only.change_pct == 2.0
