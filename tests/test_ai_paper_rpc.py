"""Plan 3 Task 9: the ai_paper methods over typed RPC, on the real command stack and registry.

Only the broker-facing authorities are fakes (patched in trader.trading.command_stack):
the fenced snapshot, quotes, the what-if and the order dispatch.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.automation.ai_paper_fixtures import CONID, NOW, make_history, quote, snapshot
from tests.automation.ai_paper_world import GOOD
from tests.rpc_identity_fixtures import ServedStack, make_identities
from tests.test_command_stack import _trader
from trader.automation.ai_paper_config import AiPaperConfig
from trader.automation.ai_paper_decision import AI_PAPER_ACTION
from trader.automation.ai_paper_experiment import ExperimentStateReader
from trader.automation.risk_limits import PAPER_LIMITS
from trader.messaging.principals import TRADER_ACL
from trader.messaging.production_api import build_production_registry
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.trading.command_coordinator import CommandRequest
from trader.trading.command_policy import CommandAuthorityPolicy

ACCOUNT = "DU111111"
GOOD_SORTED = {**GOOD, "conids": sorted(GOOD["conids"])}


class FakeBroker:
    def __init__(self, **_kwargs):
        self.fail_capture = False
        self.snapshot = replace(snapshot(), account_id=ACCOUNT)

    def capture(self, account_id):
        if self.fail_capture:
            raise RuntimeError("broker down")
        return self.snapshot


class FakeQuotes:
    def __init__(self, *_args, **_kwargs):
        pass

    def executable_quote(self, conid, *, side):
        return replace(quote(conid=conid), side=side)


class FakeMargin:
    def __init__(self, *_args, **_kwargs):
        pass

    def what_if_margin(self, conid, side, quantity):
        return {"initMarginAfter": 5_000.0, "equityWithLoanAfter": 995_000.0}


class FakeOrders:
    def __init__(self, *_args, **_kwargs):
        self.plans = []

    def submit(self, *, proposal, order_ref, order_group_id):
        self.plans.append((order_group_id, proposal))
        return SimpleNamespace(order_ids=[len(self.plans)])

    def find_by_order_ref(self, account_id, order_ref):
        return []

    def enumeration_complete(self):
        return True


class FakeOrphanEvidence:
    """The latest complete broker enumeration the saga stamps on a send."""

    def __init__(self, **_kwargs):
        pass

    def latest_complete_enumeration(self, account_id):
        from trader.automation.protective_order_saga import BrokerEnumeration
        return BrokerEnumeration(generation_id=5, started_at=NOW)


def _served(tmp_path, monkeypatch, config):
    import trader.trading.command_stack as command_stack
    import trader.trading.trading_runtime as trading_runtime

    broker, orders = FakeBroker(), FakeOrders()
    monkeypatch.setattr(command_stack, "TraderBrokerRiskSnapshotAuthority", lambda **kw: broker)
    monkeypatch.setattr(command_stack, "TraderQuoteAuthority", FakeQuotes)
    monkeypatch.setattr(command_stack, "TraderBrokerAuthority", FakeMargin)
    monkeypatch.setattr(command_stack, "BrokerStateOrphanEvidence", FakeOrphanEvidence)
    monkeypatch.setattr(trading_runtime, "TradingRuntimeOrderDispatch", lambda *a, **k: orders)
    yaml_path = tmp_path / "trader.yaml"          # paper automation status() reads it: never the developer's
    yaml_path.write_text("{}\n")
    monkeypatch.setenv("TRADER_CONFIG", str(yaml_path))
    trader = _trader(tmp_path)
    trader.ai_paper_config = config
    trader.data = make_history(str(tmp_path / "history.duckdb"))
    stack = command_stack.build_command_stack(trader, CommandAuthorityPolicy(enabled=True, max_drift_bps=50.0),
                                              now=lambda: NOW)
    ids = make_identities()
    registry = build_production_registry(trader, ids["trader"], command_stack=stack)
    from trader.automation.experiment_service import attach_production_identity
    attach_production_identity(stack.experiments, ids["trader"], registry)
    served = ServedStack({("trader", "command"): registry, ("trader", "query"): registry}, ids)
    served.stack, served.broker, served.orders, served.coordinator = stack, broker, orders, stack.coordinator
    return served


@pytest.fixture
def served(tmp_path, monkeypatch):
    stack = _served(tmp_path, monkeypatch, AiPaperConfig(enabled=True))
    yield stack
    stack.close()


@pytest.fixture
def served_disabled(tmp_path, monkeypatch):
    stack = _served(tmp_path, monkeypatch, AiPaperConfig(enabled=False))
    yield stack
    stack.close()


def command(served, principal):
    return served.client(principal, "trader", "command")


def query(served, principal):
    return served.client(principal, "trader", "query")


def publish(served, command_id="pol-1", limits=PAPER_LIMITS):
    return command(served, "ai_supervisor").call(
        "publish_ai_risk_policy", {"command_id": command_id, "limits": limits.to_json(), "reason": "start"}, dict)


def register(served):
    return command(served, "ai_research").call("register_ai_deployment", {"deployment": GOOD}, dict)["outcome"]["digest"]


def enter_body(digest="sha256:" + "a" * 64, **changes):
    body = {"decision_id": "dec-00000001", "deployment_digest": digest, "decider": "jev", "action": "ENTER",
            "conid": CONID, "side": "BUY", "stop_price": 98.0, "target_price": None, "quantity": None,
            "policy_revision": 1, "evidence_digest": "sha256:" + "c" * 64,
            "expires_at": (NOW + dt.timedelta(minutes=5)).isoformat()}
    body.update(changes)
    return body


def valid_body(method):
    return {"register_ai_deployment": {"deployment": GOOD},
            "submit_ai_paper_decision": enter_body(),
            "publish_ai_risk_policy": {"command_id": "pol-x", "limits": PAPER_LIMITS.to_json(), "reason": "r"}}[method]


def test_supervisor_publishes_and_research_registers(served):
    assert publish(served)["outcome"]["revision"] == 1
    dep = command(served, "ai_research").call("register_ai_deployment", {"deployment": GOOD}, dict)
    assert dep["state"] == "RESOLVED" and dep["outcome"]["digest"].startswith("sha256:")
    assert dep["outcome"]["strategy_digest_provenance"] == "CLAIMED_NOT_VERIFIED"


@pytest.mark.parametrize("principal,method", [
    ("ai_supervisor", "register_ai_deployment"), ("ai_research", "submit_ai_paper_decision"),
    ("ai_research", "publish_ai_risk_policy"), ("cli", "submit_ai_paper_decision"),
    ("dashboard", "publish_ai_risk_policy"), ("strategy", "submit_ai_paper_decision")])
def test_wrong_principal_is_denied_by_the_allow_list(served, principal, method):
    with pytest.raises(TypedRpcRemoteError) as exc:
        command(served, principal).call(method, valid_body(method), dict)
    assert exc.value.code == "PERMISSION_DENIED"


def test_wrong_principal_is_refused_by_the_service_too(served):
    def request(action, body, target, principal):
        return CommandRequest(command_id=f"bypass-{action}", action=action, account_id=ACCOUNT,
                              target_type=target[0], target_id=target[1], expected_version=None,
                              body=body, source=principal, principal=principal)
    receipt = served.coordinator.execute(
        request(AI_PAPER_ACTION, enter_body(), ("conid", str(CONID)), "cli"))
    assert receipt.error_code == "PRINCIPAL_FORBIDDEN"
    receipt = served.coordinator.execute(
        request("register_ai_deployment", GOOD_SORTED, ("ai_deployment", "x"), "ai_supervisor"))
    assert receipt.error_code == "PRINCIPAL_FORBIDDEN"
    receipt = served.coordinator.execute(request(
        "publish_ai_risk_policy", {"limits": PAPER_LIMITS.to_json(), "reason": "r"}, ("ai_policy", ACCOUNT),
        "ai_research"))
    assert receipt.error_code == "PRINCIPAL_FORBIDDEN"


@pytest.mark.parametrize("patch", [{"conid": True}, {"conid": 1.0}, {"quantity": 1.5}, {"stop_price": "98"},
                                   {"extra": 1}, {"expires_at": 1}, {"decision_id": "dec:0001"},
                                   {"quantity": True}, {"policy_revision": "1"}])
def test_wire_is_strict(served, patch):
    with pytest.raises(TypedRpcRemoteError) as exc:
        command(served, "ai_supervisor").call("submit_ai_paper_decision", {**enter_body(), **patch}, dict)
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.parametrize("bad", [{**PAPER_LIMITS.to_json(), "max_positions": True},
                                 {**PAPER_LIMITS.to_json(), "gross_fraction": "0.06"},
                                 {**PAPER_LIMITS.to_json(), "max_positions": 3.0},
                                 {"max_positions": 3}])
def test_policy_limits_on_the_wire_are_strict(served, bad):
    with pytest.raises(TypedRpcRemoteError) as exc:
        command(served, "ai_supervisor").call(
            "publish_ai_risk_policy", {"command_id": "p", "limits": bad, "reason": "r"}, dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_an_invalid_deployment_is_refused_on_the_wire(served):
    with pytest.raises(TypedRpcRemoteError) as exc:
        command(served, "ai_research").call("register_ai_deployment",
                                            {"deployment": {**GOOD, "conids": [True]}}, dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_reregistering_with_reordered_conids_replays(served):
    first = command(served, "ai_research").call("register_ai_deployment", {"deployment": GOOD}, dict)
    again = command(served, "ai_research").call(
        "register_ai_deployment", {"deployment": {**GOOD, "conids": list(reversed(GOOD["conids"]))}}, dict)
    assert again["command_id"] == first["command_id"] and again["outcome"] == first["outcome"]


def test_publish_with_the_broker_down_is_a_retryable_refusal(served):
    served.broker.fail_capture = True
    out = publish(served, "pol-9")
    assert (out["state"], out["error_code"], out["retryable"]) == ("REJECTED", "BROKER_SNAPSHOT_UNAVAILABLE", True)


def test_policy_above_the_owner_ceiling_is_refused(served):
    out = publish(served, "pol-2", replace(PAPER_LIMITS, gross_fraction=0.07))
    assert (out["state"], out["error_code"]) == ("REJECTED", "POLICY_ABOVE_CEILING")


def test_every_decision_is_refused_without_an_experiment(served):
    publish(served)
    digest = register(served)
    out = command(served, "ai_supervisor").call("submit_ai_paper_decision", enter_body(digest), dict)
    assert (out["state"], out["error_code"]) == ("REJECTED", "NO_EXPERIMENT")
    assert isinstance(served.stack.ai_paper.decisions._experiments, ExperimentStateReader)


def test_end_to_end_enter_through_the_stack(served):
    publish(served)
    digest = register(served)
    started = command(served, "cli").call("start_experiment", {"command_id": "start-1", "reason": "go"}, dict)
    assert started["outcome"]["state"] == "ARMED", started
    served.stack.experiments.monitor.recover()                       # trader_service does this before readiness
    out = command(served, "ai_supervisor").call("submit_ai_paper_decision", enter_body(digest), dict)
    assert out["state"] == "SUBMITTED", out
    ((group, proposal),) = served.orders.plans
    assert group == "og-aip-dec-00000001" and proposal.quantity == 499.0
    assert served.stack.ai_paper.decision_store.links_for_order_ref("mmr:og-aip-dec-00000001")[0].digest == digest


def test_reads(served):
    view = query(served, "ai_supervisor").call("get_ai_risk_policy", {}, dict)
    assert set(view) >= {"latest_published_revision", "effective", "effective_revision", "queued", "ceiling",
                         "latch_code"}
    digest = register(served)
    read = query(served, "ai_research").call("get_ai_deployment", {"digest": digest}, dict)
    assert read["deployment"] == GOOD_SORTED and read["strategy_digest_provenance"] == "CLAIMED_NOT_VERIFIED"
    assert query(served, "dashboard").call("get_ai_deployment", {"digest": "sha256:" + "b" * 64},
                                           dict)["error_code"] == "DEPLOYMENT_NOT_SEALED"


@pytest.mark.parametrize("principal", ["strategy", "ai_research"])
def test_policy_read_is_not_for_strategy_or_research(served, principal):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, principal).call("get_ai_risk_policy", {}, dict)
    assert exc.value.code == "PERMISSION_DENIED"


def test_disabled_ai_paper_registers_nothing(served_disabled):
    with pytest.raises(TypedRpcRemoteError) as exc:
        command(served_disabled, "ai_supervisor").call("submit_ai_paper_decision", enter_body(), dict)
    assert exc.value.code == "METHOD_NOT_ALLOWED"
    assert served_disabled.stack.ai_paper is None


def test_no_method_writes_the_owner_ceiling():
    methods = {method for _, method in TRADER_ACL}
    assert not [method for method in methods if "ceiling" in method]
