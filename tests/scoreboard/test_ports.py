from types import SimpleNamespace

from tests.scoreboard.common import EXP_ID, NOW
from trader.automation.ai_paper_decision import DecisionLink
from trader.scoreboard.ports import DecisionStoreAttribution, DecisionStoreFacts


def _link(action="ENTER", **changes):
    values = dict(decision_id="d1", action=action, decider="jev", strategy_version="sv-1",
                  strategy_digest_provenance=None, policy_revision=3, effective_revision=3,
                  style="intraday_long", digest="dep-digest")
    values.update(changes)
    return DecisionLink(**values)


class _Store:
    def __init__(self, links):
        self.links = links

    def links_for_order_ref(self, ref):
        return tuple(self.links)


def test_decision_store_attribution_returns_the_single_enter_link():
    links = DecisionStoreAttribution(_Store([_link()])).links_for_order_ref("mmr:x")
    assert (links.decision_id, links.decider, links.strategy_version, links.policy_revision, links.style) == (
        "d1", "jev", "sv-1", "3", "intraday_long")
    assert len(links.digest) == 64


def test_close_links_or_no_link_or_two_entries_is_none():
    assert DecisionStoreAttribution(_Store([_link("CLOSE")])).links_for_order_ref("r") is None
    assert DecisionStoreAttribution(_Store([])).links_for_order_ref("r") is None
    assert DecisionStoreAttribution(_Store([_link(), _link(decision_id="d2")])).links_for_order_ref("r") is None


def test_digest_changes_with_any_attribution_field():
    base = DecisionStoreAttribution(_Store([_link()])).links_for_order_ref("r").digest
    for change in ({"decider": "other"}, {"policy_revision": 4}, {"digest": "dep-2"}, {"style": "x"}):
        assert DecisionStoreAttribution(_Store([_link(**change)])).links_for_order_ref("r").digest != base


def test_decision_store_facts_maps_a_row_and_hides_a_missing_one():
    row = SimpleNamespace(decision_id="dec-00000001", account_id="DU1", experiment_id=EXP_ID, conid=265598,
                          action="ENTER", received_at=NOW, state="RESOLVED", command_id="aip-dec-00000001")
    facts = DecisionStoreFacts(SimpleNamespace(row=lambda decision_id: row if decision_id == "dec-00000001"
                                               else SimpleNamespace(decision_id=None) if decision_id == "bare"
                                               else None))
    assert facts.get("dec-00000001").conid == 265598 and facts.get("dec-00000001").action == "ENTER"
    assert facts.get("dec-00000001").experiment_id == EXP_ID
    assert facts.get("dec-99999999") is None and facts.get("bare") is None


def _decision_row(decision_id, action="ENTER", state="RESOLVED"):
    return SimpleNamespace(decision_id=decision_id, account_id="DU1", experiment_id=EXP_ID, conid=265598,
                           action=action, received_at=NOW, state=state, command_id=f"aip-{decision_id}")


def test_a_placed_enter_reports_its_sized_quantity():
    rows = {d: _decision_row(d, action) for d, action in (
        ("dec-00000001", "ENTER"), ("dec-00000002", "CLOSE"), ("dec-00000003", "ENTER"), ("dec-00000004", "ENTER"))}
    receipts = {"aip-dec-00000001": SimpleNamespace(state="RESOLVED", outcome={"quantity": 7}),
                "aip-dec-00000002": SimpleNamespace(state="RESOLVED", outcome={"quantity": 7}),
                "aip-dec-00000004": SimpleNamespace(state="RESOLVED", outcome={"quantity": True})}
    facts = DecisionStoreFacts(SimpleNamespace(row=rows.get), SimpleNamespace(get=receipts.get))
    assert facts.get("dec-00000001").entry_quantity == 7
    assert facts.get("dec-00000002").entry_quantity is None        # a CLOSE row
    assert facts.get("dec-00000003").entry_quantity is None        # no receipt
    assert facts.get("dec-00000004").entry_quantity is None        # quantity: True is not an int


def test_a_close_is_final_when_its_command_is_resolved_in_the_ledger():
    # The close reconciler resolves the command ledger row; the decision row keeps OUTCOME_UNKNOWN.
    rows = {"dec-00000002": _decision_row("dec-00000002", "CLOSE", state="OUTCOME_UNKNOWN")}
    receipts = {"aip-dec-00000002": SimpleNamespace(state="RESOLVED", outcome={})}
    assert DecisionStoreFacts(SimpleNamespace(row=rows.get), SimpleNamespace(get=receipts.get)).get(
        "dec-00000002").state == "RESOLVED"
    assert DecisionStoreFacts(SimpleNamespace(row=rows.get), SimpleNamespace(get=lambda command_id: None)).get(
        "dec-00000002").state == "OUTCOME_UNKNOWN"                # no ledger row: the decision row's own state
    assert DecisionStoreFacts(SimpleNamespace(row=rows.get)).get("dec-00000002").state == "OUTCOME_UNKNOWN"
