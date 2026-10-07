"""SP2 Plan 3 Task 6: the scope rule as a pure evaluation, and the check table (migration 100)."""
from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from tests.automation.ai_paper_fixtures import CONID, NOW, quote
from trader.automation.ai_paper_filter import MtimeCachedFilterLoader
from trader.automation.discretionary_deployment import DEFAULT_SCOPE_RULE
from trader.automation.discretionary_scope import (
    ScopeCheckStore, ScopeInputs, apply_scope_check_migration, evaluate_scope, trading_filter_refusal,
)
from trader.automation.production_evidence import TwentySessionVolume, latest_closed_sessions
from trader.automation.scope_evidence import ContractEvidence
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.quote_feeds import LIVE_ONLY_FEEDS, PAPER_IEX_FEEDS

DIGEST = "sha256:" + "d" * 64


def contract(**fields):
    base = ContractEvidence(conid=CONID, symbol="AAPL", sec_type="STK", currency="USD", primary_exchange="NASDAQ",
                            stock_type="COMMON", fetched_at=NOW)
    return replace(base, **fields)


def volume(median=100e6, sessions_ending_days_ago=0):
    sessions = latest_closed_sessions(NOW - dt.timedelta(days=sessions_ending_days_ago))
    return TwentySessionVolume(conid=CONID, sessions=sessions, closes=(100.0,) * 20,
                               volumes=(median / 100.0,) * 20, source="local_daily_bars")


def inputs(**changes):
    base = dict(contract=contract(), quote=quote(), volume=volume(), order_notional=50_000.0,
                filter_refusal=lambda c, p: None, accepted_feeds=LIVE_ONLY_FEEDS, missing=())
    base.update(changes)
    return ScopeInputs(**base)


@pytest.fixture
def db(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    assert apply_scope_check_migration(SchemaMigrator(db))
    return db


def test_a_good_instrument_passes():
    assert evaluate_scope(DEFAULT_SCOPE_RULE, inputs(), NOW).passed


@pytest.mark.parametrize("changes,part", [
    ({"contract": contract(primary_exchange="AMEX")}, "exchange"),
    ({"contract": contract(primary_exchange="SMART")}, "exchange"),
    ({"contract": contract(primary_exchange="PINK")}, "exchange"),
    ({"contract": contract(sec_type="WAR")}, "instrument_type"),
    ({"contract": contract(stock_type="RIGHT")}, "instrument_type"),
    ({"contract": contract(stock_type="UNIT")}, "instrument_type"),
    ({"contract": contract(stock_type="ADR")}, "instrument_type"),
    ({"contract": contract(currency="CAD")}, "instrument_type"),
    ({"quote": quote(bid=4.99, ask=5.0)}, "price"),
    ({"volume": volume(median=19_000_000.0)}, "dollar_volume"),
    ({"volume": volume(median=30_000_000.0)}, "dollar_volume"),        # above the rule, below SP1's floor
    ({"order_notional": 1_000_001.0}, "liquidity"),                    # 1% of $100M is $1,000,000
    ({"filter_refusal": lambda c, p: "symbol AAPL is denied"}, "trading_filter"),
    ({"contract": None, "missing": ("IB timeout",)}, "evidence_stale"),
    ({"quote": None}, "evidence_stale"),
    ({"quote": quote(age=6.0)}, "evidence_stale"),
    ({"quote": quote(feed="delayed")}, "evidence_stale"),
    ({"quote": quote(feed="iex_realtime")}, "evidence_stale"),          # IEX without the paper fallback
    ({"quote": quote(feed="delayed"), "accepted_feeds": PAPER_IEX_FEEDS}, "evidence_stale"),
    ({"quote": quote(session_state="closed")}, "evidence_stale"),
    ({"quote": quote(bid=101.0, ask=100.0)}, "evidence_stale"),
    ({"volume": None, "missing": ("no bars",)}, "evidence_stale"),
    ({"volume": volume(sessions_ending_days_ago=3)}, "evidence_stale"),
])
def test_each_part(changes, part):
    verdict = evaluate_scope(DEFAULT_SCOPE_RULE, inputs(**changes), NOW)
    assert (verdict.passed, verdict.part) == (False, part)


def test_an_iex_quote_passes_only_with_the_paper_fallback_set():       # owner #74
    iex = quote(feed="iex_realtime")
    assert evaluate_scope(DEFAULT_SCOPE_RULE, inputs(quote=iex, accepted_feeds=PAPER_IEX_FEEDS), NOW).passed
    refused = evaluate_scope(DEFAULT_SCOPE_RULE, inputs(quote=iex, accepted_feeds=LIVE_ONLY_FEEDS), NOW)
    assert refused.part == "evidence_stale" and "iex_realtime" in refused.reason and "live" in refused.reason
    assert evaluate_scope(DEFAULT_SCOPE_RULE, inputs(quote=quote(bid=4.99, ask=5.0, feed="iex_realtime"),
                                                     accepted_feeds=PAPER_IEX_FEEDS), NOW).part == "price"


def test_blank_stock_type_is_instrument_type():                       # review focus 1
    verdict = evaluate_scope(DEFAULT_SCOPE_RULE, inputs(contract=contract(stock_type="")), NOW)
    assert verdict.part == "instrument_type" and "(blank)" in verdict.reason


def test_the_dollar_volume_reason_names_both_floors():
    reason = evaluate_scope(DEFAULT_SCOPE_RULE, inputs(volume=volume(median=30_000_000.0)), NOW).reason
    assert "20,000,000" in reason and "50,000,000" in reason


def test_exactly_one_percent_passes():
    assert evaluate_scope(DEFAULT_SCOPE_RULE, inputs(order_notional=1_000_000.0), NOW).passed


def test_a_raising_filter_is_a_trading_filter_refusal():
    def broken(contract, price):
        raise OSError("unreadable")
    assert evaluate_scope(DEFAULT_SCOPE_RULE, inputs(filter_refusal=broken), NOW).part == "trading_filter"


def test_filter_uses_the_ib_identity(tmp_path):
    path = tmp_path / "trading_filters.yaml"
    path.write_text('denylist: ["AAPL"]\n')
    refusal = trading_filter_refusal(MtimeCachedFilterLoader(str(path)))
    assert refusal(contract(), 100.0) and refusal(contract(symbol="MSFT"), 100.0) is None


def test_the_evidence_names_the_feed_and_the_accepted_set():
    evidence = evaluate_scope(DEFAULT_SCOPE_RULE, inputs(quote=quote(feed="iex_realtime"),
                                                         accepted_feeds=PAPER_IEX_FEEDS), NOW).evidence
    assert evidence["quote"]["feed_type"] == "iex_realtime"
    assert evidence["accepted_feeds"] == ["iex_realtime", "live"]
    assert evidence["contract"]["stock_type"] == "COMMON" and evidence["volume"]["source"] == "local_daily_bars"


def test_checks_are_recorded_once_per_phase(db):
    store = ScopeCheckStore(db, now=lambda: NOW)
    verdict = evaluate_scope(DEFAULT_SCOPE_RULE, inputs(quote=quote(bid=4.0, ask=4.1)), NOW)
    detail = store.record(command_id="aip-d1", phase="admission", deployment_digest=DIGEST, conid=CONID,
                          verdict=verdict)
    assert detail == {"part": "price", "reason": verdict.reason, "phase": "admission",
                      "check_id": "admission:aip-d1"}
    store.record(command_id="aip-d1", phase="admission", deployment_digest=DIGEST, conid=CONID,
                 verdict=evaluate_scope(DEFAULT_SCOPE_RULE, inputs(), NOW))
    assert store.detail("aip-d1", "admission")["part"] == "price"          # a replay keeps the first verdict
    assert store.detail("aip-d1", "dispatch") is None
    assert db.execute("SELECT version FROM schema_migrations WHERE version = 100", fetch="one")


def test_an_unknown_phase_is_refused(db):
    with pytest.raises(ValueError):
        ScopeCheckStore(db, now=lambda: NOW).record(command_id="aip-d1", phase="later", deployment_digest=DIGEST,
                                                    conid=CONID, verdict=evaluate_scope(DEFAULT_SCOPE_RULE,
                                                                                        inputs(), NOW))
