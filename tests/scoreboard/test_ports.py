from trader.automation.ai_paper_decision import DecisionLink
from trader.scoreboard.ports import DecisionStoreAttribution


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
