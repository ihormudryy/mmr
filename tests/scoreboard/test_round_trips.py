import datetime as dt

import pytest

from tests.scoreboard.conftest import ACCOUNT
from tests.scoreboard.fills import T0, T1, T2, UTC, broker_store, fill, put_fill
from trader.scoreboard.ports import AttributionLinks
from trader.scoreboard.round_trips import Projection, ProjectionError, load_fill_facts, project_round_trips

NONE = lambda ref: None  # noqa: E731


def test_long_round_trip_net_of_fees():
    p = project_round_trips([fill("e1", "BUY", 10, 100, "1.00", T0), fill("e2", "SELL", 10, 101, "1.00", T1)],
                            links_for=NONE)
    t, = p.trips
    assert (t.direction, t.status, t.gross_pnl_usd, t.fees_usd, t.net_pnl_usd) == ("LONG", "CLOSED", 10.0, 2.0, 8.0)
    assert t.fees_complete is True and (t.entry_avg, t.exit_avg) == (100.0, 101.0)


def test_partial_exits_stay_in_one_trip():
    p = project_round_trips([fill("e1", "BUY", 10, 100, "0", T0), fill("e2", "SELL", 4, 102, "0", T1),
                             fill("e3", "SELL", 6, 101, "0", T2)], links_for=NONE)
    assert len(p.trips) == 1 and p.trips[0].gross_pnl_usd == 4 * 2 + 6 * 1
    assert p.trips[0].exec_ids == ("e1", "e2", "e3")


def test_short_round_trip_profits_when_price_falls():
    p = project_round_trips([fill("e1", "SELL", 10, 100, "0", T0), fill("e2", "BUY", 10, 98, "0", T1)],
                            links_for=NONE)
    assert (p.trips[0].direction, p.trips[0].net_pnl_usd) == ("SHORT", 20.0)


def test_fill_crossing_zero_closes_one_trip_and_opens_the_next():
    p = project_round_trips([fill("e1", "BUY", 10, 100, "0", T0), fill("e2", "SELL", 15, 101, "3.00", T1)],
                            links_for=NONE)
    closed, opened = sorted(p.trips, key=lambda t: (t.opened_at, t.status))
    assert (closed.status, closed.fees_usd) == ("CLOSED", 2.0)          # 3.00 * 10/15
    assert (opened.status, opened.direction, opened.entry_qty, opened.fees_usd) == ("OPEN", "SHORT", 5.0, 1.0)
    assert closed.round_trip_id != opened.round_trip_id


def test_open_trip_has_unknown_net_not_zero():
    t, = project_round_trips([fill("e1", "BUY", 10, 100, "1.00", T0)], links_for=NONE).trips
    assert t.status == "OPEN" and t.closed_at is None and t.net_pnl_usd is None and t.exit_avg is None


def test_unknown_commission_makes_net_unknown_and_flags_it():
    t, = project_round_trips([fill("e1", "BUY", 10, 100, None, T0), fill("e2", "SELL", 10, 101, "1.00", T1)],
                             links_for=NONE).trips
    assert (t.net_pnl_usd, t.fees_usd, t.fees_complete, t.gross_pnl_usd) == (None, None, False, 10.0)


def test_conids_are_independent_and_tie_broken_by_exec_id():
    p = project_round_trips([fill("b", "SELL", 5, 10, "0", T1, conid=2), fill("a", "BUY", 5, 9, "0", T1, conid=2),
                             fill("x", "BUY", 1, 5, "0", T0, conid=1)], links_for=NONE)
    assert {t.conid for t in p.trips} == {1, 2}
    assert [t for t in p.trips if t.conid == 2][0].direction == "LONG"      # exec "a" sorts before "b"


def test_attribution_comes_from_the_first_opening_fill_only():
    links = AttributionLinks("d1", "jev", "sv-1", "3", "intraday_long", "dig")
    seen = []
    p = project_round_trips([fill("e1", "BUY", 10, 100, "0", T0, ref="r-entry"),
                             fill("e2", "SELL", 10, 101, "0", T1, ref="r-flatten")],
                            links_for=lambda ref: seen.append(ref) or links)
    assert seen == ["r-entry"] and p.trips[0].decider == "jev" and p.trips[0].links_digest == "dig"


def test_unattributed_trip_keeps_null_links():
    t, = project_round_trips([fill("e1", "BUY", 1, 1, "0", T0)], links_for=NONE).trips
    assert (t.decider, t.strategy_version, t.decision_id, t.links_digest) == (None,) * 4


def test_session_date_uses_et_across_dst():
    # DST ends Sun 2026-11-01: 2026-11-02 00:30 UTC is 19:30 EST on Nov 1, still the Nov 1 date
    p = project_round_trips([fill("e1", "BUY", 1, 1, "0", dt.datetime(2026, 11, 2, 0, 30, tzinfo=UTC)),
                             fill("e2", "SELL", 1, 1, "0", dt.datetime(2026, 11, 2, 15, 0, tzinfo=UTC))],
                            links_for=NONE)
    assert p.pieces[0].session_date == dt.date(2026, 11, 1) and p.pieces[1].session_date == dt.date(2026, 11, 2)


@pytest.mark.parametrize("bad", [dict(quantity=0), dict(price=-1), dict(side="HOLD"), dict(naive=True)])
def test_bad_fills_fail_loudly(bad):
    kwargs = dict(exec_id="e1", side="BUY", quantity=1, price=1, commission="0", when=T0)
    naive = bad.pop("naive", False)
    kwargs.update(bad)
    with pytest.raises(ProjectionError):
        project_round_trips([fill(kwargs["exec_id"], kwargs["side"], kwargs["quantity"], kwargs["price"],
                                  kwargs["commission"], kwargs["when"], naive=naive)], links_for=NONE)


def test_duplicate_exec_id_fails_loudly():
    with pytest.raises(ProjectionError):
        project_round_trips([fill("e1", "BUY", 1, 1, "0", T0), fill("e1", "SELL", 1, 1, "0", T1)], links_for=NONE)


def test_empty_input_is_empty_projection():
    assert project_round_trips([], links_for=NONE) == Projection((), ())


def test_round_trip_id_is_stable_across_rebuilds():
    fills = [fill("e1", "BUY", 1, 1, "0", T0), fill("e2", "SELL", 1, 2, "0", T1)]
    assert project_round_trips(fills, links_for=NONE) == project_round_trips(list(reversed(fills)), links_for=NONE)


def test_load_fill_facts_reads_ref_symbol_and_usd_only_commission(db, migrator):
    store = broker_store(db, migrator)
    put_fill(db, store, ACCOUNT, "e1", "BUY", 10, 100, 1.0, T0, ref="mmr:og-aip-x", symbol="AAPL")
    put_fill(db, store, ACCOUNT, "e2", "SELL", 10, 101, 1.0, T1, currency="EUR")
    put_fill(db, store, ACCOUNT, "old", "BUY", 1, 1, 0.0, T0 - dt.timedelta(days=3))
    put_fill(db, store, "OTHER", "x9", "BUY", 1, 1, 0.0, T1)
    facts = load_fill_facts(db, ACCOUNT, T0 - dt.timedelta(hours=1))
    assert [f.exec_id for f in facts] == ["e1", "e2"]
    assert (facts[0].order_ref, facts[0].symbol, str(facts[0].commission)) == ("mmr:og-aip-x", "AAPL", "1.0")
    assert facts[1].commission is None and facts[1].order_ref is None


def test_replace_round_trips_swaps_the_projection_of_one_experiment(store):
    a = project_round_trips([fill("e1", "BUY", 1, 1, "0", T0)], links_for=NONE).trips
    b = project_round_trips([fill("e2", "BUY", 1, 1, "0", T1)], links_for=NONE).trips
    store.replace_round_trips("exp-a", [t.as_row("exp-a", ACCOUNT) for t in a])
    store.replace_round_trips("exp-b", [t.as_row("exp-b", ACCOUNT) for t in b])
    store.replace_round_trips("exp-a", [])
    assert [r["experiment_id"] for r in store.fetch("round_trips", {})] == ["exp-b"]
