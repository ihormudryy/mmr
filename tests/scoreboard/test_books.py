import datetime as dt

from trader.scoreboard.books import build_books, summarize_costs


def decision(record_id, baseline="follow_signal.v1", cohort="strategy_signal"):
    return {"record_id": record_id, "baseline_id": baseline, "cohort": cohort}


def outcome(record_id, status="COMPLETE", pnl=1.0, reason=None, trades=1):
    return {"record_id": record_id, "status": status, "pnl_usd": None if status == "INCOMPLETE" else pnl,
            "reason": reason, "trades": trades}


def test_books_are_separate_and_never_summed():
    books = build_books(
        [decision("a"), decision("b"), decision("c", "fixed_rule.v1", "self_found"),
         decision("d", "no_trade.v1", "self_found")],
        [outcome("a", pnl=10.0), outcome("b", pnl=5.0), outcome("c", pnl=-3.0), outcome("d", pnl=0.0, trades=0)])
    by_id = {(b["baseline_id"], b["cohort"]): b for b in books}
    assert [b["baseline_id"] for b in books] == ["fixed_rule.v1", "follow_signal.v1", "no_trade.v1"]
    assert by_id[("follow_signal.v1", "strategy_signal")]["pnl_usd"] == 15.0
    assert by_id[("fixed_rule.v1", "self_found")]["pnl_usd"] == -3.0
    assert by_id[("no_trade.v1", "self_found")]["pnl_usd"] == 0.0
    assert all(b["status"] == "COMPLETE" and b["label"] == "simulated" for b in books)


def test_the_same_baseline_in_two_cohorts_is_two_books():
    books = build_books([decision("a"), decision("b", cohort="self_found")], [outcome("a"), outcome("b")])
    assert len(books) == 2


def test_incomplete_book_does_not_hide_complete_books():
    books = build_books([decision("a"), decision("b"), decision("c", "fixed_rule.v1", "self_found")],
                        [outcome("a", pnl=10.0), outcome("b", "INCOMPLETE", reason="alpaca:NO_BARS"),
                         outcome("c", pnl=2.0)])
    bad, good = books[1], books[0]
    assert (good["baseline_id"], good["status"], good["pnl_usd"]) == ("fixed_rule.v1", "COMPLETE", 2.0)
    assert (bad["status"], bad["pnl_usd"], bad["known_pnl_usd"], bad["complete"], bad["incomplete"]) == (
        "INCOMPLETE", None, 10.0, 1, 1)
    assert bad["incomplete_reasons"] == {"alpaca:NO_BARS": 1}


def test_a_record_without_an_outcome_is_pending_not_zero():
    book = build_books([decision("a"), decision("b")], [outcome("a", pnl=4.0)])[0]
    assert (book["status"], book["pending"], book["pnl_usd"], book["known_pnl_usd"]) == ("PENDING", 1, None, 4.0)
    only = build_books([decision("a")], [])[0]
    assert (only["status"], only["known_pnl_usd"]) == ("PENDING", None)


def cost(record_id, status="confirmed", usd=1.0, corrects=None, seq=0):
    return {"record_id": record_id, "cost_status": status, "cost_usd": None if status == "unknown" else usd,
            "corrects_record_id": corrects, "correction_seq": seq}


def test_no_cost_rows_is_none_not_zero():
    summary = summarize_costs([])
    assert (summary["status"], summary["total_usd"], summary["calls"]) == ("NONE", None, 0)


def test_cost_statuses_are_labelled_and_unknown_hides_the_total():
    assert summarize_costs([cost("a"), cost("b", usd=2.0)])["status"] == "CONFIRMED"
    est = summarize_costs([cost("a"), cost("b", "estimated", 0.5)])
    assert (est["status"], est["confirmed_usd"], est["estimated_usd"], est["total_usd"]) == ("ESTIMATED", 1.0, 0.5, 1.5)
    unknown = summarize_costs([cost("a"), cost("b", "unknown")])
    assert (unknown["status"], unknown["unknown_calls"], unknown["total_usd"]) == ("INCOMPLETE", 1, None)


def test_correction_is_counted_once_and_status_labelled():
    rows = [cost("a", "estimated", 0.4), cost("b", "confirmed", 0.55, corrects="a", seq=1),
            cost("c", "confirmed", 0.56, corrects="a", seq=2)]
    summary = summarize_costs(rows)
    assert (summary["calls"], summary["corrections"], summary["confirmed_usd"], summary["status"]) == (
        1, 2, 0.56, "CONFIRMED")
    assert summary["total_usd"] == 0.56
