import datetime as dt

import pytest

from tests.scoreboard.common import ACCOUNT, NOW
from tests.scoreboard.fills import set_commission
from tests.scoreboard.ledger_world import CAL, D, D_NEXT, END, END2
from trader.scoreboard.benchmark import BenchmarkBook
from trader.scoreboard.inputs import record_ai_cost, record_simulated_row
from trader.scoreboard.ports import AttributionLinks, SessionEnd
from trader.scoreboard.service import ScoreboardService


class FakeLinks:
    def __init__(self):
        self.digest = "dig-1"
        self.present = True

    def links_for_order_ref(self, ref):
        if not self.present:
            return None
        return AttributionLinks("d1", "jev", "sv-1", "3", "intraday_long", self.digest)


def checks(result):
    return {m["check"] for m in result["mismatches"]}


def _service(world):
    book = BenchmarkBook(world.store, lambda start, end: {D: 500.0, D_NEXT: 501.0}, CAL, now=lambda: NOW)
    return ScoreboardService(store=world.store, db=world.db, experiments=world.experiments, ledger=world.ledger(),
                             book=book, links=world.links, calendar=CAL, now=lambda: NOW)


@pytest.fixture
def links(world):
    world.links = FakeLinks()
    return world.links


@pytest.fixture
def service(world, links):
    world.round_trip(D, buy=("e1", 10, 100, "1.00"), sell=("e2", 10, 101, "1.00"))
    world.fill("e1", "BUY", 10, 100, 1.0, dt.datetime(2026, 10, 6, 14, 0, tzinfo=dt.timezone.utc),
               ref="mmr:og-aip-1")
    world.round_trip(D_NEXT, buy=("e3", 5, 50, "1.00"), sell=("e4", 5, 49, "1.00"))
    ledger = world.ledger()
    ledger.record_session_end(SessionEnd(ACCOUNT, D, "FLAT", END))
    ledger.record_session_end(SessionEnd(ACCOUNT, D_NEXT, "FLAT", END2))
    service = _service(world)
    service.refresh()
    service.book.refresh(D, D_NEXT)
    return service


def test_clean_books_verify_ok(service):
    result = service.verify()
    assert result["ok"] is True and result["mismatches"] == []
    assert result["checked"] == {"seals": 5, "round_trips": 2, "sessions": 2}


def test_edited_equity_row_is_detected(service, db):
    db.execute("UPDATE equity_daily SET end_nlv_usd = end_nlv_usd + 1")
    assert "ROW_EDITED" in checks(service.verify())


@pytest.mark.parametrize("table,sql", [
    ("equity_adjustments", "UPDATE equity_adjustments SET amount_usd = amount_usd + 1"),
    ("benchmark_prices", "UPDATE benchmark_prices SET close = close + 1"),
    ("ai_costs", "UPDATE ai_costs SET cost_usd = 9"),
    ("simulated_books", "UPDATE simulated_books SET pnl_usd = 9"),
])
def test_edited_adjustment_benchmark_ai_cost_and_simulated_rows_are_detected(service, world, db, table, sql):
    set_commission(db, ACCOUNT, "e2", 1.30)
    service.refresh()
    record_ai_cost(world.store, call_id="c1", provider="p", model="m", input_tokens=1, output_tokens=1,
                   cost_usd=0.1, called_at=NOW, served_kind="job", served_id="j")
    record_simulated_row(world.store, book_id="b1", experiment_id=world.experiments.record.experiment_id,
                         session_date=D, baseline="b", pnl_usd=1.0, trades=1)
    assert service.verify()["ok"] is True
    db.execute(sql)
    result = service.verify()
    assert result["ok"] is False and any(m["check"] == "ROW_EDITED" and m["table"] == table
                                         for m in result["mismatches"])


def test_edited_round_trip_row_is_detected(service, db):
    db.execute("UPDATE round_trips SET net_pnl_usd = 999")
    assert "ROUND_TRIP_MISMATCH" in checks(service.verify())


def test_deleted_or_extra_round_trip_is_detected(service, db):
    db.execute("DELETE FROM round_trips WHERE conid = 265598 AND opened_session = DATE '2026-10-07'")
    db.execute("UPDATE round_trips SET round_trip_id = 'bogus' WHERE opened_session = DATE '2026-10-06'")
    assert {"ROUND_TRIP_MISSING", "ROUND_TRIP_EXTRA"} <= checks(service.verify())


def test_edited_fill_price_is_detected_through_the_fills_digest(service, db):
    db.execute("UPDATE broker_fills SET price = price + 1 WHERE exec_id = 'e1'")
    assert "SESSION_FILLS_CHANGED" in checks(service.verify())


@pytest.mark.parametrize("sql", [
    "UPDATE broker_fills SET conid = 272093 WHERE exec_id IN ('e1', 'e2')",       # review #33: a whole trip
    "UPDATE broker_order_aliases SET alias_value = 'mmr:og-other' WHERE alias_value = 'mmr:og-aip-1'"])
def test_changed_fill_identity_after_sealing_fails_verify_with_an_incident(service, db, sql):
    db.execute(sql)
    service.refresh()                                   # derived trips follow the changed fill
    result = service.verify()
    assert result["ok"] is False and "SESSION_FILLS_CHANGED" in checks(result)
    assert any("SESSION_FILLS_CHANGED" in incident["key"] for incident in result["incidents"])


def test_edited_commission_without_an_adjustment_is_detected(service, db):
    set_commission(db, ACCOUNT, "e1", 5.0)
    assert "COMMISSION_MISMATCH" in checks(service.verify())


def test_late_commission_with_its_adjustment_verifies_ok(service, db):
    set_commission(db, ACCOUNT, "e1", 1.30)
    service.refresh()
    assert service.verify()["ok"] is True


def test_changed_decision_record_is_detected(service, links):
    links.digest = "other"
    assert "ATTRIBUTION_CHANGED" in checks(service.verify())


def test_vanished_decision_link_is_detected(service, links):
    links.present = False
    assert "ATTRIBUTION_CHANGED" in checks(service.verify())


def test_verify_never_writes(service, db):
    db.execute("UPDATE round_trips SET net_pnl_usd = 999")
    before = db.execute("SELECT COUNT(*) FROM scoreboard_seals", fetch="one")
    service.verify()
    assert db.execute("SELECT net_pnl_usd FROM round_trips LIMIT 1", fetch="one")[0] == 999
    assert db.execute("SELECT COUNT(*) FROM scoreboard_seals", fetch="one") == before


def test_verify_with_no_experiment_checks_only_the_seals(world):
    world.experiments.record = None
    result = _service(world).verify()
    assert result["ok"] is True and result["checked"] == {"seals": 0, "round_trips": 0, "sessions": 0}


def test_incidents_are_listed_but_do_not_fail_verify(service, world):
    world.store.record_incident("FX_EVIDENCE_MISSING", f"{world.experiments.record.experiment_id}:x", "d")
    result = service.verify()
    assert result["ok"] and result["incidents"]


def test_unknown_experiment_is_a_body_error(service):
    assert service.verify("exp-ffffffffffffffffffff")["error_code"] == "EXPERIMENT_NOT_FOUND"


def test_a_mismatch_is_recorded_as_an_incident_and_listed(service, world, db):
    db.execute("UPDATE equity_daily SET end_nlv_usd = end_nlv_usd + 1")
    result = service.verify()
    assert "VERIFY_MISMATCH" in {i["kind"] for i in world.store.incidents()}
    assert "VERIFY_MISMATCH" in {i["kind"] for i in result["incidents"]}


def test_a_stale_projection_fails_verify_but_writes_no_incident(service, world):
    world.fill("new", "BUY", 1, 100, 1.0, dt.datetime(2026, 10, 8, 17, 0, tzinfo=dt.timezone.utc))
    result = service.verify()
    assert result["ok"] is False and "ROUND_TRIP_MISSING" in checks(result)
    assert "VERIFY_MISMATCH" not in {i["kind"] for i in world.store.incidents()}
