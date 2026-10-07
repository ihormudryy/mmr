"""SP1 Plan 5 Task 7: scoreboard reads over a real typed RPC server with the production ACL."""
import pytest

from tests.rpc_identity_fixtures import ServedStack, build_full_production_registry, make_identities
from tests.scoreboard.common import ACCOUNT, EXP_ID
from tests.scoreboard.ledger_world import END_FLAT
from trader.messaging.principals import TRADER_ACL
from trader.messaging.scoreboard_surface import register_scoreboard_surface
from trader.messaging.typed_rpc import TypedRpcRegistry, TypedRpcRemoteError

READERS = ("cli", "dashboard", "ai_supervisor")
OUTSIDERS = ("ai_research", "strategy")


@pytest.fixture
def served(scoreboard):
    registry = TypedRpcRegistry(acl=TRADER_ACL, default_execution="thread")
    register_scoreboard_surface(registry, scoreboard)
    stack = ServedStack({("trader", "query"): registry}, make_identities())
    stack.registry = registry
    yield stack
    stack.close()


def code(stack, principal, method, body=None):
    with pytest.raises(TypedRpcRemoteError) as exc:
        stack.client(principal).call(method, body or {}, dict)
    return exc.value.code


def test_every_principal_outcome_for_get_scoreboard(served):
    for principal in READERS:
        assert served.client(principal).call("get_scoreboard", {}, dict)["label"] == "PAPER"
    for principal in OUTSIDERS:
        assert code(served, principal, "get_scoreboard") == "PERMISSION_DENIED"


def test_verify_scoreboard_is_human_only(served):
    for principal in ("cli", "dashboard"):
        assert served.client(principal).call("verify_scoreboard", {}, dict)["ok"] is True
    for principal in ("ai_supervisor", *OUTSIDERS):
        assert code(served, principal, "verify_scoreboard") == "PERMISSION_DENIED"


def test_get_experiment_trips_returns_identity_and_quantity_per_trip(served, world):
    world.round_trip(buy=("e1", 3, 100, "1.00"), sell=("e2", 3, 101, "1.00"))
    world.fill("e9", "BUY", 2, 50, 1.0, world.clock[0], conid=4815747, symbol="NVDA")
    body = served.client("ai_supervisor").call("get_experiment_trips", {"experiment_id": EXP_ID}, dict)
    trips = body["trips"]
    assert [(t["conid"], t["opened_quantity"], t["closed_quantity"], t["state"]) for t in trips] == [
        (265598, 3.0, 3.0, "CLOSED"), (4815747, 2.0, 0.0, "OPEN")]
    assert trips[0]["exec_ids"] == ["e1", "e2"] and trips[0]["net_pnl_usd"] == 1.0
    assert trips[1]["net_pnl_usd"] is None and trips[1]["closed_at"] is None
    assert body["experiment_id"] == EXP_ID and set(trips[0]) == {
        "round_trip_id", "conid", "symbol", "direction", "opened_at", "closed_at", "opened_quantity",
        "closed_quantity", "exec_ids", "net_pnl_usd", "decision_id", "strategy_ref", "state"}


def test_get_experiment_trips_acl_and_unknown_experiment(served):
    for principal in OUTSIDERS:
        assert code(served, principal, "get_experiment_trips", {"experiment_id": EXP_ID}) == "PERMISSION_DENIED"
    body = served.client("cli").call("get_experiment_trips", {"experiment_id": "exp-" + "f" * 20}, dict)
    assert body["error_code"] == "EXPERIMENT_NOT_FOUND" and body["trips"] is None


@pytest.mark.parametrize("method,body", [
    ("get_scoreboard", {"nope": 1}), ("get_scoreboard", {"experiment_id": "e1"}),
    ("verify_scoreboard", {"experiment_id": 5}), ("get_experiment_trips", {}),
    ("get_experiment_trips", {"experiment_id": EXP_ID, "x": 1})])
def test_unknown_or_malformed_body_is_a_validation_error(served, method, body):
    assert code(served, "cli", method, body) == "VALIDATION_ERROR"


def test_no_scoreboard_service_registers_nothing():
    registry = TypedRpcRegistry(acl=TRADER_ACL)
    register_scoreboard_surface(registry, None)
    assert not list(registry.registrations())


class SpyService:
    def __init__(self):
        self.calls = []

    def refresh(self, experiment_id=None):
        self.calls.append("refresh")

    def report(self, experiment_id=None):
        self.calls.append("report")
        return {"label": "PAPER"}

    def verify(self, experiment_id=None):
        self.calls.append("verify")
        return {"ok": True}


def _handler(registry, method):
    return next(r.handler for r in registry.registrations() if r.method == method)


def test_get_scoreboard_refreshes_before_reading_and_verify_never_refreshes():
    from trader.messaging.scoreboard_surface import GetScoreboardRequest
    registry, spy = TypedRpcRegistry(acl=TRADER_ACL), SpyService()
    register_scoreboard_surface(registry, spy)
    _handler(registry, "get_scoreboard")(GetScoreboardRequest())
    assert spy.calls == ["refresh", "report"]
    spy.calls.clear()
    _handler(registry, "verify_scoreboard")(GetScoreboardRequest())
    assert spy.calls == ["verify"]
    assert {r.execution for r in registry.registrations()} == {"thread"}


def test_a_session_row_shows_on_the_wire(served, world):
    world.ledger().record_session_end(END_FLAT)
    report = served.client("dashboard").call("get_scoreboard", {"experiment_id": EXP_ID}, dict)
    assert report["account"]["sessions"] == 1 and report["sessions"][0]["end_state"] == "FLAT"


def test_scoreboard_rights_are_exact():
    assert {key: set(TRADER_ACL[key]) for key in (
        ("query", "get_scoreboard"), ("query", "verify_scoreboard"), ("query", "get_experiment_trips"))} == {
        ("query", "get_scoreboard"): set(READERS), ("query", "verify_scoreboard"): {"cli", "dashboard"},
        ("query", "get_experiment_trips"): set(READERS)}


def test_ai_principals_have_no_write_path_to_scoreboard_tables():
    for principal in ("ai_supervisor", "ai_research"):
        rights = {key for key, allowed in TRADER_ACL.items() if principal in allowed}
        assert not {k for k in rights if k[0] == "command" and any(
            word in k[1] for word in ("scoreboard", "ai_cost", "simulated", "benchmark", "equity"))}


def test_the_full_production_registry_registers_the_scoreboard_reads():
    registered = {(r.socket_role, r.method) for r in build_full_production_registry().registrations()}
    assert {("query", "get_scoreboard"), ("query", "verify_scoreboard"),
            ("query", "get_experiment_trips")} <= registered
    assert ACCOUNT  # the fixture account is the one the world uses
