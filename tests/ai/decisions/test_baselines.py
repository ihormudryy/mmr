"""SP2 Plan 6 Task 5: deterministic baselines; missing evidence is sent incomplete, never invented (spec 7)."""
import dataclasses

import pytest

from tests.ai.decisions.fakes import AAPL, NOW, STRATEGY_DIGEST
from trader.ai.baselines import (fixed_rule, follow_signal, incomplete, incomplete_reason_for, matched_entry,
                                 no_trade, pick_fixed_rule)
from trader.ai.discovery_client import EligibleCandidate
from trader.ai.engine import OwnedPosition, SignalOpportunity
from trader.ai.evidence import PricedEntry, Quote

PRICED = PricedEntry(AAPL, NOW, Quote(AAPL, 229.9, 230.0, NOW.isoformat(), "live"), 225.4, 239.2)
SIGNAL = SignalOpportunity("sig-" + "1" * 32, 7, "orb", AAPL, "BUY", 0.7, NOW, NOW)
DEC = "dec-" + "9" * 32
CLOSE_1, CLOSE_2 = "dec-" + "c" * 32, "dec-" + "e" * 32


def candidate(ref, symbol, change, volume, conid=AAPL):
    return EligibleCandidate(ref, symbol, conid, ("gainer",), 100.0, change, 1e6, volume, NOW.isoformat(), ())


def test_follow_signal_takes_the_quote_and_leaves_the_size_to_the_trader():
    b = follow_signal(SIGNAL, PRICED, STRATEGY_DIGEST)
    assert (b.baseline_id, b.cohort, b.opportunity_id, b.decided_at) == ("follow_signal.v1", "strategy_signal",
                                                                         SIGNAL.opportunity_id, NOW)
    assert (b.quantity, b.reference_price, b.stop_price, b.target_price) == (None, 230.0, 225.4, 239.2)
    assert b.deployment_digest == STRATEGY_DIGEST                      # the trader sizes it (Plan 2 Ruling 19)


@pytest.mark.parametrize("code,reason", [
    ("QUOTE_FEED_NOT_ACCEPTED", "feed_not_accepted"), ("QUOTE_STALE", "quote_not_executable"),
    ("QUOTE_UNAVAILABLE", "quote_unavailable"), ("QUOTE_INVALID", "quote_not_executable"),
    ("QUOTE_NOT_CONTINUOUS", "quote_not_executable"), ("BRACKET_INVALID", "quote_not_executable"),
    ("TRADER_UNREACHABLE", "quote_unavailable")])
def test_a_missing_quote_is_an_incomplete_record_with_no_invented_value(code, reason):
    b = incomplete("follow_signal.v1", "strategy_signal", SIGNAL.opportunity_id, NOW, conid=AAPL,
                   reason=incomplete_reason_for(code), deployment_digest=STRATEGY_DIGEST)
    assert (b.incomplete_reason, b.conid) == (reason, AAPL)
    assert (b.side, b.quantity, b.reference_price, b.stop_price, b.target_price) == (None,) * 5


def test_fixed_rule_ranks_by_change_then_volume_then_symbol():
    ranked = (candidate("C1", "BBB", 2.0, 5e7), candidate("C2", "AAA", 2.0, 5e7), candidate("C3", "CCC", 1.0, 9e9),
              candidate("C4", "DDD", None, 9e9), candidate("C5", "EEE", 2.0, 4e7))
    assert pick_fixed_rule(ranked).symbol == "AAA"
    assert pick_fixed_rule((candidate("C1", "X", None, 1e9),)) is None


def test_the_fixed_rule_record_names_the_cycle_and_the_deployment():
    b = fixed_rule("cyc-entry-20260717-1100", PRICED, STRATEGY_DIGEST)
    assert (b.baseline_id, b.cohort, b.opportunity_id, b.quantity, b.reference_price) == (
        "fixed_rule.v1", "self_found", "cyc-entry-20260717-1100", None, 230.0)


def test_no_trade_is_the_cycle_and_nothing_else():
    b = no_trade("cyc-entry-20260717-1100", NOW)
    assert (b.conid, b.side, b.quantity, b.reference_price) == (None, None, None, None)


def test_matched_entry_records_the_entry_price_for_the_closed_shares():
    position = OwnedPosition("rt-1", AAPL, "AAPL", 4.0, NOW, DEC, 230.05, 10.0)
    b = matched_entry(position, {"stop_price": 225.4, "target_price": 239.2}, close_decision_id=CLOSE_1,
                      closed_quantity=4)
    assert (b.opportunity_id, b.decided_at, b.quantity, b.reference_price, b.linked_decision_id) == (
        CLOSE_1, NOW, 4, 230.05, DEC)
    assert b.linked_round_trip_id == "rt-1"


def test_an_unrankable_fixed_rule_is_incomplete_without_a_conid():           # second PR #75 review
    b = incomplete("fixed_rule.v1", "self_found", "cyc-entry-20260717-1100", NOW, conid=None,
                   reason="ranking_unavailable", deployment_digest=STRATEGY_DIGEST)
    assert (b.incomplete_reason, b.conid, b.reference_price, b.quantity) == ("ranking_unavailable", None, None, None)
    assert pick_fixed_rule((candidate("C1", "X", None, 1e9), candidate("C2", "Y", None, 2e9))) is None


def test_two_partial_closes_of_one_trip_are_two_records_with_their_requests():   # PR #75
    position = OwnedPosition("rt-1", AAPL, "AAPL", 10.0, NOW, DEC, 230.05, 10.0)
    body = {"stop_price": 225.4, "target_price": 239.2}
    first = matched_entry(position, body, close_decision_id=CLOSE_1, closed_quantity=4)          # PARTIAL_CLOSE 4
    second = matched_entry(dataclasses.replace(position, open_quantity=6.0), body, close_decision_id=CLOSE_2,
                           closed_quantity=6)                                                     # CLOSE the rest
    assert (first.opportunity_id, second.opportunity_id) == (CLOSE_1, CLOSE_2)
    assert {first.linked_round_trip_id, second.linked_round_trip_id} == {"rt-1"}
    assert (first.quantity, second.quantity) == (4, 6)          # requests; the trader proves the real shares


def test_a_close_of_less_than_one_share_has_no_record():
    position = OwnedPosition("rt-1", AAPL, "AAPL", 0.5, NOW, DEC, 230.05, 10.0)
    assert matched_entry(position, {"stop_price": 225.4, "target_price": 239.2}, close_decision_id=CLOSE_1,
                         closed_quantity=0) is None


@pytest.mark.parametrize("position,body", [
    (OwnedPosition("rt-1", AAPL, "AAPL", 4.0, NOW, DEC, None, 10.0), {"stop_price": 225.4, "target_price": 239.2}),
    (OwnedPosition("rt-1", AAPL, "AAPL", 4.0, NOW, None, 230.0, 10.0), {"stop_price": 225.4, "target_price": 239.2}),
    (OwnedPosition("rt-1", AAPL, "AAPL", 4.0, NOW, DEC, 230.0, 10.0), None),
    (OwnedPosition("rt-1", AAPL, "AAPL", 4.0, NOW, DEC, 230.0, 10.0), {"stop_price": 231.0, "target_price": 239.2})])
def test_matched_entry_is_not_invented_from_missing_evidence(position, body):
    assert matched_entry(position, body, close_decision_id=CLOSE_1, closed_quantity=4) is None
