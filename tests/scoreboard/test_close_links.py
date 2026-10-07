"""SP2 Plan 2 Ruling 21: a model close's reduce orders, and the shares it is broker-proven to have removed."""
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.scoreboard.common import ACCOUNT, EXP_ID, NOW
from trader.automation.ai_deployments import apply_ai_deployment_migration
from trader.automation.ai_paper_decision import AiPaperDecisionStore, DecisionRow, apply_ai_paper_decision_migration
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.scoreboard.close_fills import JournalCloseFills
from trader.scoreboard.ports import CloseFill, DecisionFact, DecisionStoreAttribution, DecisionStoreCloseLinks
from trader.scoreboard.round_trips import FillFact
from trader.trading.order_correlation import encode_order_ref, liquidation_child_id

CONID = 265598


def _row(command_id, decision_id, action, state, close_root_id=None):
    return DecisionRow(command_id=command_id, account_id=ACCOUNT, body_json="{}", state=state, received_at=NOW,
                       updated_at=NOW, decision_id=decision_id, conid=CONID, action=action, decider="jev",
                       close_root_id=close_root_id)


@pytest.fixture
def decision_store(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_ai_deployment_migration(migrator)
    apply_ai_paper_decision_migration(migrator)
    store = AiPaperDecisionStore(journal)
    rows = [_row("aip-dec-e", "dec-e", "ENTER", "SUBMITTED"),
            _row("aip-dec-c", "dec-c", "PARTIAL_CLOSE", "OUTCOME_UNKNOWN", close_root_id="root-1"),
            # EXIT_IN_PROGRESS: a refused close carries the root of the close that owns the position.
            _row("aip-dec-x", "dec-x", "CLOSE", "REJECTED", close_root_id="root-1")]
    db.transaction(lambda conn: [store.upsert_in_tx(conn, row) for row in rows])
    return store


def test_a_reduce_child_resolves_to_its_close_and_nothing_else_does(decision_store):
    links = DecisionStoreCloseLinks(decision_store)
    reduce_ref = encode_order_ref(liquidation_child_id("root-1", "reduce", 265598, 1))
    assert links.close_decision_for_order_ref(reduce_ref) == "dec-c"             # the REJECTED dec-x owns nothing
    for kind in ("reprotect-stop", "reprotect-target", "cancel"):
        ref = encode_order_ref(liquidation_child_id("root-1", kind, 265598, 1))
        assert links.close_decision_for_order_ref(ref) is None
    assert links.close_decision_for_order_ref(encode_order_ref("og-aip-dec-e")) is None


def test_the_enter_only_attribution_is_unchanged(decision_store):
    attribution = DecisionStoreAttribution(decision_store)
    assert attribution.links_for_order_ref(encode_order_ref("og-aip-dec-e")).decision_id == "dec-e"
    reduce_ref = encode_order_ref(liquidation_child_id("root-1", "reduce", 265598, 1))
    assert attribution.links_for_order_ref(reduce_ref) is None                   # close links only: still None


# -- JournalCloseFills ----------------------------------------------------------------------------------------

REDUCE_C = encode_order_ref(liquidation_child_id("root-1", "reduce", CONID, 1))
REDUCE_OTHER = encode_order_ref(liquidation_child_id("root-2", "reduce", CONID, 1))
ENTRY_STOP = encode_order_ref("og-aip-dec-e")
REPROTECT = encode_order_ref(liquidation_child_id("root-1", "reprotect-stop", CONID, 1))
UNKNOWN = encode_order_ref("og-someone-else")


def fill(exec_id, side, quantity, ref):
    return FillFact(exec_id, CONID, side, Decimal(str(quantity)), Decimal("100"), Decimal("1"), NOW, ref)


def fact(decision_id, state):
    return DecisionFact(decision_id, ACCOUNT, EXP_ID, CONID, "PARTIAL_CLOSE", NOW, None, state)


class Links:
    """reduce children of root-1 belong to dec-c, of root-2 to dec-d."""

    def close_decision_for_order_ref(self, ref):
        return {REDUCE_C: "dec-c", REDUCE_OTHER: "dec-d"}.get(ref)


def close_fills(executions, state="RESOLVED"):
    decisions = SimpleNamespace(get=lambda decision_id: fact(decision_id, state))
    return JournalCloseFills(None, Links(), decisions, lambda round_trip_id: executions)


def test_proven_shares_are_the_close_own_reduce_sells():
    executions = [fill("e1", "BUY", 10, ENTRY_STOP), fill("e2", "SELL", 3, REDUCE_C), fill("e3", "SELL", 2, REDUCE_C),
                  fill("e4", "SELL", 4, REDUCE_OTHER), fill("e5", "SELL", 1, ENTRY_STOP), fill("e6", "SELL", 0, REPROTECT)]
    assert close_fills(executions).removed("rt-1", "dec-c") == CloseFill(True, 5)


def test_a_rejected_close_without_executions_removed_nothing():
    assert close_fills([fill("e1", "BUY", 10, ENTRY_STOP)], state="REJECTED").removed("rt-1", "dec-c") == CloseFill(
        True, 0)


@pytest.mark.parametrize("state", ["OUTCOME_UNKNOWN", "SUBMITTED", None])
def test_a_close_that_is_not_final_is_unproven(state):
    assert close_fills([fill("e2", "SELL", 3, REDUCE_C)], state=state).removed("rt-1", "dec-c") == CloseFill(False, None)


@pytest.mark.parametrize("ref", [UNKNOWN, None])
def test_a_sell_nobody_owns_makes_the_count_unproven(ref):
    executions = [fill("e2", "SELL", 3, REDUCE_C), fill("e3", "SELL", 2, ref)]
    assert close_fills(executions).removed("rt-1", "dec-c") == CloseFill(False, None)


def test_unknown_close_or_unreadable_trip_is_unproven():
    unknown = JournalCloseFills(None, Links(), SimpleNamespace(get=lambda decision_id: None), lambda trip: [])
    assert unknown.removed("rt-1", "dec-c") == CloseFill(False, None)
    assert close_fills(None).removed("rt-1", "dec-c") == CloseFill(False, None)


def test_the_session_ledger_lists_a_trip_executions(world, scoreboard):
    base = dt.datetime(2026, 10, 6, 14, 0, tzinfo=dt.timezone.utc)
    world.fill("e1", "BUY", 10, 100, 1.0, base, ref=ENTRY_STOP)
    world.fill("e2", "SELL", 4, 101, 1.0, base + dt.timedelta(hours=1), ref=REDUCE_C)
    scoreboard.refresh()
    trip = world.store.fetch("round_trips", {})[0]
    executions = world.ledger().trip_executions(trip["round_trip_id"])
    assert [(e.exec_id, e.side, int(e.quantity), e.order_ref) for e in executions] == [
        ("e1", "BUY", 10, ENTRY_STOP), ("e2", "SELL", 4, REDUCE_C)]
    assert world.ledger().trip_executions("rt-missing") is None
