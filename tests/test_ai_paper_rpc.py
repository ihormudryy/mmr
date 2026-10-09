"""Plan 3 Task 9: the ai_paper methods over typed RPC, on the real command stack and registry.

Only the broker-facing authorities are fakes (patched in trader.trading.command_stack):
the fenced snapshot, quotes, the what-if and the order dispatch.
"""
from __future__ import annotations

import copy
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tests.automation.ai_paper_fixtures import CONID, NOW, make_history, quote, snapshot
from tests.automation.ai_paper_world import GOOD
from tests.automation.judged_deployment import install_seeded_judgments, seed_judged_deployment
from tests.rpc_identity_fixtures import ServedStack, make_identities
from tests.test_command_stack import _trader
from trader.automation.ai_paper_config import AiPaperConfig
from trader.automation.ai_paper_decision import AI_PAPER_ACTION
from trader.automation.ai_paper_experiment import ExperimentStateReader
from trader.automation.discretionary_deployment import DEFAULT_SCOPE_RULE
from trader.automation.risk_limits import PAPER_LIMITS
from trader.messaging.principals import TRADER_ACL
from trader.messaging.production_api import build_production_registry
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.trading.command_coordinator import CommandRequest, CommandValidationError
from trader.trading.command_policy import CommandAuthorityPolicy

ACCOUNT = "DU111111"
GOOD_SORTED = {**GOOD, "conids": sorted(GOOD["conids"])}
BUNDLE = "sha256:" + "b" * 64


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


def _served(tmp_path, monkeypatch, config, now=lambda: NOW, prepare=lambda trader: None):
    import trader.trading.command_stack as command_stack
    import trader.trading.trading_runtime as trading_runtime

    seeded = install_seeded_judgments(monkeypatch)
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
    prepare(trader)
    stack = command_stack.build_command_stack(trader, CommandAuthorityPolicy(enabled=True, max_drift_bps=50.0),
                                              now=now)
    ids = make_identities()
    registry = build_production_registry(trader, ids["trader"], command_stack=stack)
    from trader.automation.experiment_service import attach_production_identity
    attach_production_identity(stack.experiments, ids["trader"], registry)
    served = ServedStack({("trader", "command"): registry, ("trader", "query"): registry}, ids)
    served.stack, served.broker, served.orders, served.coordinator = stack, broker, orders, stack.coordinator
    served.trader, served.seeded = trader, seeded
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


def granted_epoch(served):
    return command(served, "ai_supervisor").call("grant_ai_controller_epoch", {
        "holder_id": "ctl-a", "current_epoch": None, "lease_seconds": 60}, dict)["epoch"]


def publish(served, command_id="pol-1", limits=PAPER_LIMITS):
    return command(served, "ai_supervisor").call(
        "publish_ai_risk_policy", {"command_id": command_id, "limits": limits.to_json(), "reason": "start"}, dict)


def register_version(served):
    """A sealed, ACTIVE judged deployment (the bundle path has its own tests): its base and version digests."""
    today = NOW.astimezone(ZoneInfo("America/New_York")).date()
    return seed_judged_deployment(served.stack.ai_paper, served.seeded, GOOD, today=today)


def register(served):
    return register_version(served)[0]


def bound_enter_body(served, **changes):
    """A strategy ENTER bound to a fresh judged version, as the ai controller builds it (SP2c ruling 9)."""
    digest, version = register_version(served)
    return enter_body(digest, deployment_version=version, source_digest=GOOD["strategy_digest"], **changes)


def registration_body(deployment=GOOD, judgment_id="jdg-none"):
    return {"judgment_id": judgment_id, "bundle_digest": BUNDLE, "deployment": deployment}


def enter_body(digest="sha256:" + "a" * 64, **changes):
    body = {"decision_id": "dec-00000001", "deployment_digest": digest, "decider": "jev", "action": "ENTER",
            "conid": CONID, "side": "BUY", "stop_price": 98.0, "target_price": None, "quantity": None,
            "policy_revision": 1, "evidence_digest": "sha256:" + "c" * 64,
            "expires_at": (NOW + dt.timedelta(minutes=5)).isoformat()}
    body.update(changes)
    return body


def valid_body(method):
    return {"register_ai_deployment": registration_body(),
            "submit_ai_paper_decision": enter_body(),
            "publish_ai_risk_policy": {"command_id": "pol-x", "limits": PAPER_LIMITS.to_json(), "reason": "r"}}[method]


def test_supervisor_publishes_and_research_registration_needs_a_judgment(served):
    assert publish(served)["outcome"]["revision"] == 1
    dep = command(served, "ai_research").call("register_ai_deployment", registration_body(), dict)
    assert (dep["state"], dep["error_code"]) == ("REJECTED", "JUDGMENT_MISSING")


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
        return CommandRequest(command_id=f"bypass-{action}-{principal}", action=action, account_id=ACCOUNT,
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
    receipt = served.coordinator.execute(request(
        "publish_ai_risk_policy", {"limits": PAPER_LIMITS.to_json(), "reason": "r"}, ("ai_policy", ACCOUNT),
        "dashboard"))
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
                                            registration_body({**GOOD, "conids": [True]}), dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_reregistering_with_reordered_conids_replays(served):
    first = command(served, "ai_research").call("register_ai_deployment", registration_body(), dict)
    again = command(served, "ai_research").call(
        "register_ai_deployment",
        registration_body({**GOOD, "conids": list(reversed(GOOD["conids"]))}), dict)
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
    out = command(served, "ai_supervisor").call("submit_ai_paper_decision", enter_body(digest), dict,
                                                controller_epoch=granted_epoch(served))
    assert (out["state"], out["error_code"]) == ("REJECTED", "NO_EXPERIMENT")
    assert isinstance(served.stack.ai_paper.decisions._experiments, ExperimentStateReader)


def test_end_to_end_enter_through_the_stack(served):
    publish(served)
    body = bound_enter_body(served)
    started = command(served, "cli").call("start_experiment", {"command_id": "start-1", "reason": "go"}, dict)
    assert started["outcome"]["state"] == "ARMED", started
    served.stack.experiments.monitor.recover()                       # trader_service does this before readiness
    out = command(served, "ai_supervisor").call("submit_ai_paper_decision", body, dict,
                                                controller_epoch=granted_epoch(served))
    assert out["state"] == "SUBMITTED", out
    ((group, proposal),) = served.orders.plans
    assert group == "og-aip-dec-00000001" and proposal.quantity == 499.0
    link = served.stack.ai_paper.decision_store.links_for_order_ref("mmr:og-aip-dec-00000001")[0]
    assert link.digest == body["deployment_digest"]


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


# --- SP2 Plan 3 Task 4: the operator's discretionary deployment ------------------------------------

def discretionary_body(**rule):
    return {"kind": "discretionary", "style": "intraday_long", "scope_rule": {**DEFAULT_SCOPE_RULE.to_json(), **rule},
            "attestation": {"operator": "owner", "statement": "paper only; rule as sealed",
                            "attested_at": "2026-07-17T10:00:00-04:00"}}


def register_discretionary(served, **rule):
    return command(served, "cli").call("register_discretionary_deployment",
                                       {"deployment": discretionary_body(**rule)}, dict)


def replace_account_mode(actions, mode):
    clone = copy.copy(actions)
    clone._account_mode = mode
    return clone


def test_operator_registers_and_everyone_reads_the_discretionary_label(served):
    receipt = register_discretionary(served)
    assert receipt["state"] == "RESOLVED" and receipt["outcome"]["kind"] == "discretionary"
    view = query(served, "ai_supervisor").call("get_ai_deployment", {"digest": receipt["outcome"]["digest"]}, dict)
    assert (view["kind"], view["strategy_digest_provenance"]) == ("discretionary", "OPERATOR_ATTESTED")
    assert view["deployment"]["scope_rule"] == DEFAULT_SCOPE_RULE.to_json()
    assert query(served, "ai_research").call("get_ai_deployment", {"digest": register(served)}, dict)["kind"] == "strategy"


def test_registering_twice_replays_one_command(served):
    first, again = register_discretionary(served), register_discretionary(served)
    assert again["command_id"] == first["command_id"] and again["outcome"] == first["outcome"]


@pytest.mark.parametrize("principal", ["ai_supervisor", "ai_research", "dashboard", "strategy"])
def test_only_the_cli_registers_a_discretionary_deployment(served, principal):
    with pytest.raises(TypedRpcRemoteError) as exc:
        command(served, principal).call("register_discretionary_deployment",
                                        {"deployment": discretionary_body()}, dict)
    assert exc.value.code == "PERMISSION_DENIED"


def test_the_service_refuses_a_bypass_of_the_allow_list(served):
    receipt = served.coordinator.execute(CommandRequest(
        command_id="bypass-d", action="register_discretionary_deployment", account_id=ACCOUNT,
        target_type="ai_deployment", target_id="x", expected_version=None, body=discretionary_body(),
        source="ai_research", principal="ai_research"))
    assert receipt.error_code == "PRINCIPAL_FORBIDDEN"


@pytest.mark.parametrize("method,principal", [("register_discretionary_deployment", "cli"),
                                              ("register_ai_deployment", "ai_research")])
def test_a_wider_or_foreign_body_is_refused_on_the_wire(served, method, principal):
    body = discretionary_body(stock_types=["COMMON", "WARRANT"])
    request = {"deployment": body} if method == "register_discretionary_deployment" else registration_body(body)
    with pytest.raises(TypedRpcRemoteError) as exc:
        command(served, principal).call(method, request, dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_a_live_account_never_registers(served):
    actions = replace_account_mode(served.stack.ai_paper.actions, "live")
    request = CommandRequest(command_id="d-live", action="register_discretionary_deployment", account_id=ACCOUNT,
                             target_type="ai_deployment", target_id="x", expected_version=None,
                             body=discretionary_body(), source="cli", principal="cli")
    with pytest.raises(CommandValidationError) as exc:
        actions.register_discretionary(request)
    assert exc.value.code == "ACCOUNT_NOT_PAPER"


def test_backtest_judge_methods_serve_on_the_real_paper_stack(served):               # SP2c Plan 1
    from trader.data.schema_migrations import SchemaMigrator
    from trader.research.evaluation_request import EvaluationRequestBody, evaluation_request_id

    zero = "sha256:" + "0" * 64
    assert query(served, "research").call("get_evaluation_claim", {"request_id": zero}, dict) == \
        {"found": False, "claim": None}
    body = {"strategy_key": "strategies/opening_range_breakout.py:OpeningRangeBreakout",
            "cohort": [{"RANGE_MINUTES": 15}], "conids": [265598], "bar_size": "5 mins", "research_day": "2026-10-08"}
    request_id = evaluation_request_id(EvaluationRequestBody.model_validate(body))
    reply = command(served, "research").call("claim_evaluation", {"request_id": request_id, "body": body}, dict)
    assert (reply["status"], reply["code"]) == ("REFUSED", "STRATEGY_NOT_ALLOWED")   # the default allowlist is empty
    assert {110, 111} <= SchemaMigrator(served.trader.journal_db).applied_versions()


def test_backtest_judge_methods_are_absent_when_ai_paper_is_off(served_disabled):    # SP2c Plan 1
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served_disabled, "research").call("get_evaluation_claim", {"request_id": "sha256:" + "0" * 64}, dict)
    assert exc.value.code == "METHOD_NOT_ALLOWED"
