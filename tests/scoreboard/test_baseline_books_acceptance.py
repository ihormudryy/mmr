import datetime as dt

import pytest

from tests.scoreboard.common import EXP_ID
from tests.scoreboard.ingest_world import make_ingest, no_trade_body, sim, sim_body
from tests.scoreboard.test_session_simulator import FakeSource, QUIET, SESSION, minute_bars
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.scoreboard.session_simulator import GRACE, SessionSimulator
from trader.scoreboard.simulator import Bar


@pytest.fixture
def world_ingest(world):
    return make_ingest(world.store)


class ByConid:
    name = "alpaca"

    def __init__(self, by_conid):
        self.by_conid = by_conid

    def bars(self, conid, start, end):
        return [b for b in self.by_conid.get(conid, []) if start <= b.start < end]


def books_of(scoreboard):
    scoreboard.refresh()
    return {(b["baseline_id"], b["cohort"]): b for b in scoreboard.report(EXP_ID)["benchmarks"]["books"]}


def run(world, sources, extra):
    clock = world.clock
    clock[0] = SessionSimulator.data_ready_at(SESSION) + extra
    SessionSimulator(store=world.store, calendar=XNYSCalendarPolicy(), sources=sources, now=lambda: clock[0]).run_due()


def seed_three_books(ingest):
    sim(ingest, sim_body())                                                     # follow_signal, conid 265598
    sim(ingest, sim_body(record_id="sim-0000002", baseline_id="fixed_rule.v1", cohort="self_found",
                         opportunity_id="cycle-1", conid=4815747))
    sim(ingest, no_trade_body())


def test_three_baselines_report_as_three_separate_books(scoreboard, world, world_ingest):
    seed_three_books(world_ingest)
    run(world, [FakeSource("alpaca", minute_bars(SESSION, QUIET, 101.0))], dt.timedelta(minutes=1))
    books = books_of(scoreboard)
    assert set(books) == {("follow_signal.v1", "strategy_signal"), ("fixed_rule.v1", "self_found"),
                          ("no_trade.v1", "self_found")}
    assert books[("follow_signal.v1", "strategy_signal")]["pnl_usd"] == 10.0
    assert books[("fixed_rule.v1", "self_found")]["pnl_usd"] == 10.0
    assert books[("no_trade.v1", "self_found")]["pnl_usd"] == 0.0 and books[("no_trade.v1", "self_found")]["trades"] == 0
    assert world.store.verify_seals() == []


def test_incomplete_book_does_not_hide_complete_books(scoreboard, world, world_ingest):
    seed_three_books(world_ingest)
    source = ByConid({265598: minute_bars(SESSION, QUIET, 101.0)})        # no bars for conid 4815747
    run(world, [source], GRACE)
    books = books_of(scoreboard)
    bad = books[("fixed_rule.v1", "self_found")]
    assert (bad["status"], bad["pnl_usd"], bad["incomplete"]) == ("INCOMPLETE", None, 1)
    assert books[("follow_signal.v1", "strategy_signal")]["status"] == "COMPLETE"
    assert books[("no_trade.v1", "self_found")]["status"] == "COMPLETE"


def test_an_incomplete_baseline_is_counted_not_hidden(scoreboard, world, world_ingest):
    from tests.scoreboard.ingest_world import incomplete_body
    seed_three_books(world_ingest)
    sim(world_ingest, incomplete_body("quote_unavailable"))                 # a follow_signal without a quote
    run(world, [FakeSource("alpaca", minute_bars(SESSION, QUIET, 101.0))], dt.timedelta(minutes=1))
    follow = books_of(scoreboard)[("follow_signal.v1", "strategy_signal")]
    assert (follow["status"], follow["records"], follow["complete"], follow["incomplete"]) == ("INCOMPLETE", 2, 1, 1)
    assert follow["incomplete_reasons"] == {"quote_unavailable": 1} and follow["known_pnl_usd"] == 10.0


def test_nothing_in_the_report_adds_books_together(scoreboard, world, world_ingest):
    seed_three_books(world_ingest)
    run(world, [FakeSource("alpaca", minute_bars(SESSION, QUIET, 101.0))], dt.timedelta(minutes=1))
    scoreboard.refresh()
    benchmarks = scoreboard.report(EXP_ID)["benchmarks"]
    assert "simulated" not in benchmarks and len(benchmarks["books"]) == 3
    assert all(set(b) >= {"baseline_id", "cohort", "status", "pnl_usd", "label"} for b in benchmarks["books"])


def test_costs_in_the_report_carry_status_and_a_correction_changes_the_total_once(scoreboard, world_ingest):
    from tests.scoreboard.ingest_world import cost
    cost(world_ingest, cost_status="estimated", cost_usd=0.4)
    cost(world_ingest, record_id="cost-0000002", attempt_id="att-0000002", cost_status="unknown", cost_usd=None)
    scoreboard.refresh()
    bench = scoreboard.report(EXP_ID)["benchmarks"]
    assert (bench["ai_cost"]["status"], bench["ai_cost_usd"], bench["pnl_minus_ai_cost_usd"]) == ("INCOMPLETE", None, None)
    cost(world_ingest, record_id="cost-0000003", attempt_id="att-0000002", corrects_record_id="cost-0000002",
         cost_status="confirmed", cost_usd=0.6)
    cost(world_ingest, record_id="cost-0000004", corrects_record_id="cost-0000001", cost_status="confirmed", cost_usd=0.45)
    bench = scoreboard.report(EXP_ID)["benchmarks"]
    assert (bench["ai_cost"]["status"], bench["ai_cost"]["calls"], bench["ai_cost"]["corrections"]) == ("CONFIRMED", 2, 2)
    assert bench["ai_cost"]["total_usd"] == pytest.approx(1.05)


def test_the_report_reads_through_the_signed_rpc(scoreboard, world_ingest):
    from tests.rpc_identity_fixtures import ServedStack, make_identities
    from tests.scoreboard.ingest_world import cost_body
    from trader.messaging.ai_ingest_surface import register_ai_ingest_surface
    from trader.messaging.principals import TRADER_ACL
    from trader.messaging.scoreboard_surface import register_scoreboard_surface
    from trader.messaging.typed_rpc import TypedRpcRegistry
    command = TypedRpcRegistry(acl=TRADER_ACL, default_execution="thread")
    register_ai_ingest_surface(command, world_ingest)
    query = TypedRpcRegistry(acl=TRADER_ACL, default_execution="thread")
    register_scoreboard_surface(query, scoreboard)
    stack = ServedStack({("trader", "command"): command, ("trader", "query"): query}, make_identities())
    try:
        ai = stack.client("ai_supervisor", role="command")
        assert ai.call("record_ai_cost", cost_body(), dict)["status"] == "INSERTED"
        assert ai.call("record_simulated_decision", no_trade_body(), dict)["status"] == "INSERTED"
        report = stack.client("dashboard").call("get_scoreboard", {"experiment_id": EXP_ID}, dict)
    finally:
        stack.close()
    bench = report["benchmarks"]
    assert [b["baseline_id"] for b in bench["books"]] == ["no_trade.v1"] and bench["books"][0]["pnl_usd"] == 0.0
    assert (bench["ai_cost"]["calls"], bench["ai_cost"]["status"]) == (1, "CONFIRMED")
