"""SP2 Plan 6 Task 1: the decisions section of ai.yaml and the decision tables."""
import datetime as dt

import pytest

from tests.ai.fakes import FakeClock, config_text, load_test_config, write_config
from trader.ai.config import AiConfigError, load_ai_config
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.store import AiStore

DIGEST = "sha256:" + "a" * 64


def load(tmp_path, block):
    return load_ai_config(str(write_config(tmp_path, config_text(extra_top_level=block))))


def test_defaults_pin_the_fixed_rule_and_need_no_deployment(tmp_path):
    decisions = load_test_config(tmp_path).decisions
    assert (decisions.fixed_rule.version, decisions.fixed_rule.stop_fraction,
            decisions.fixed_rule.target_fraction) == ("fixed_rule.v1", 0.02, 0.04)
    assert decisions.discretionary_deployment_digest is None and dict(decisions.strategies) == {}
    assert (decisions.max_entries_per_cycle, decisions.quote_max_age_seconds) == (2, 15)


def test_a_strategy_bracket_is_read(tmp_path):
    block = (f"decisions:\n  strategies:\n    orb: {{deployment_digest: \"{DIGEST}\", stop_fraction: 0.015,"
             " target_fraction: 0.03}\n")
    assert load(tmp_path, block).decisions.strategies["orb"].stop_fraction == 0.015


def test_the_decisions_section_changes_the_config_digest(tmp_path):
    plain = load_test_config(tmp_path)
    block = f"decisions:\n  discretionary_deployment_digest: \"{DIGEST}\"\n"
    assert load(tmp_path, block).digest() != plain.digest()


@pytest.mark.parametrize("block,code", [
    ("decisions:\n  fixed_rule: {version: fixed_rule.v1, stop_fraction: 0.03, target_fraction: 0.04}\n",
     "FIXED_RULE_VERSION_MISMATCH"),
    ("decisions:\n  strategies: {orb: {deployment_digest: abc, stop_fraction: 0.02, target_fraction: 0.04}}\n",
     "AI_CONFIG_INVALID"),
    ("decisions:\n  strategies: {'Bad Name': {deployment_digest: \"" + DIGEST + "\", stop_fraction: 0.02,"
     " target_fraction: 0.04}}\n", "AI_CONFIG_INVALID"),
    ("decisions:\n  discovery: {watchlist: [brk.b]}\n", "AI_CONFIG_INVALID"),
    ("decisions:\n  self_found_bracket: {stop_fraction: 0.0, target_fraction: 0.04}\n", "AI_CONFIG_INVALID"),
    ("decisions:\n  surprise: 1\n", "AI_CONFIG_INVALID")])
def test_bad_decisions_config_fails_loudly(tmp_path, block, code):
    with pytest.raises(AiConfigError) as exc:
        load(tmp_path, block)
    assert exc.value.code == code


def test_trips_carry_the_entry_price_into_owned_positions():
    from trader.ai.engine import owned_positions_from_trips
    now = dt.datetime(2026, 7, 17, 15, tzinfo=dt.timezone.utc)
    trip = {"round_trip_id": "rt-1", "conid": 265598, "symbol": "AAPL", "direction": "LONG",
            "opened_at": now.isoformat(), "closed_at": None, "opened_quantity": 10.0, "closed_quantity": 6.0,
            "decision_id": "dec-" + "9" * 32, "state": "OPEN", "entry_avg_price": 230.05}
    (position,) = owned_positions_from_trips({"experiment_id": "exp-" + "a" * 20, "trips": [trip]})
    assert (position.open_quantity, position.entry_price, position.entry_quantity) == (4.0, 230.05, 10.0)


def test_a_trip_without_an_entry_price_keeps_it_unknown_and_a_bad_one_fails():
    from trader.ai.engine import owned_positions_from_trips
    now = dt.datetime(2026, 7, 17, 15, tzinfo=dt.timezone.utc)
    trip = {"round_trip_id": "rt-1", "conid": 265598, "symbol": "AAPL", "opened_at": now.isoformat(),
            "opened_quantity": 10.0, "closed_quantity": 0.0, "decision_id": None, "state": "OPEN"}
    (position,) = owned_positions_from_trips({"trips": [{**trip, "entry_avg_price": None}]})
    assert position.entry_price is None
    with pytest.raises(ValueError):
        owned_positions_from_trips({"trips": [{**trip, "entry_avg_price": -1.0}]})


def test_the_decision_tables_migrate_with_the_runtime(tmp_path):
    store = AiStore(tmp_path / "ai.duckdb", clock=FakeClock(dt.datetime(2026, 7, 17, 15, tzinfo=dt.timezone.utc)))
    assert {20, 21} <= set(store.migrate(ALL_MIGRATIONS))
    tables = {row[0] for row in store.db.execute("SELECT table_name FROM information_schema.tables", fetch="all")}
    assert {"ai_rulings", "ai_discovery_reads"} <= tables
