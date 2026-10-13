import pytest

from tests.ai.author.helpers import authored_reply, record, submit_reply
from trader.ai.author_wire import parse_authored_strategies, parse_submit_reply
from trader.ai.research_wire import WireError
from trader.ai.rpc_clients import (AUTHOR_COMMANDS, AUTHOR_QUERIES, SUPERVISOR_QUERIES, AiRpcClients,
                                   MethodNotAllowedLocally, PrincipalClient, RpcRefused)

DIGEST = "sha256:" + "d" * 64


def test_a_reply_parses_with_room_bar_sizes_and_the_judged_count():
    reply = parse_authored_strategies(authored_reply(counted=2, judged_count=4, strategies=[
        record("a"), record("b", state="STORED", reason=None)]))
    assert reply.enabled and reply.cap_has_room and reply.judged_count == 4
    assert reply.bar_sizes == ["5 mins", "15 mins"] and [r.state for r in reply.records] == ["REJECTED", "STORED"]
    assert (reply.records[0].tunables["WINDOW"].low, reply.records[0].tunables["WINDOW"].default) == (2.0, 5.0)


def test_a_full_cap_has_no_room():
    assert parse_authored_strategies(authored_reply(counted=3, max_active=3)).cap_has_room is False


@pytest.mark.parametrize("change", [lambda r: r.update(surprise=1), lambda r: r.pop("room"),
                                    lambda r: r["records"].append({**record(), "state": "DONE"}),
                                    lambda r: r["records"].append({**record(), "digest": "md5:x"}),
                                    lambda r: r["records"].append({**record(), "key": "authored:md5:x:Old"}),
                                    lambda r: r["records"][0].pop("note"),
                                    lambda r: r["records"][0].update(attempts="0")])
def test_a_reply_of_another_shape_is_a_wire_error(change):
    reply = authored_reply(strategies=[record()])
    change(reply)
    with pytest.raises(WireError, match="get_authored_strategies"):
        parse_authored_strategies(reply)


def test_submit_replies():
    stored = parse_submit_reply(submit_reply(digest=DIGEST))
    assert (stored.status, stored.record.digest, stored.record.key) == (
        "STORED", DIGEST, f"authored:{DIGEST}:OpeningRangeBreakout")
    refused = parse_submit_reply(submit_reply("REFUSED", code="AUTHORED_CAP_FULL"))
    assert (refused.status, refused.code, refused.record, refused.retryable) == (
        "REFUSED", "AUTHORED_CAP_FULL", None, False)
    with pytest.raises(WireError, match="without a record"):
        parse_submit_reply({**submit_reply("EXISTING", digest=DIGEST), "record": None})


def test_a_submit_reply_with_an_unknown_or_missing_key_is_a_wire_error():
    with pytest.raises(WireError, match="submit_authored_strategy"):
        parse_submit_reply({**submit_reply(digest=DIGEST), "judged_count": 1})
    missing = submit_reply(digest=DIGEST)
    del missing["retryable"]
    with pytest.raises(WireError, match="submit_authored_strategy"):
        parse_submit_reply(missing)


def test_the_author_client_holds_its_two_methods_only():
    client = PrincipalClient("ai_author", command=object(), query=object(), commands=AUTHOR_COMMANDS,
                             queries=AUTHOR_QUERIES, timeout=1.0)
    assert client.methods == {"submit_authored_strategy", "get_authored_strategies"}
    assert "get_scoreboard" in SUPERVISOR_QUERIES


def test_the_author_sets_match_plan_3s_allow_list():
    from trader.messaging.principals import TRADER_ACL
    for role, methods in (("command", AUTHOR_COMMANDS), ("query", AUTHOR_QUERIES)):
        for method in methods:
            assert TRADER_ACL[(role, method)] == frozenset({"ai_author"}), method       # spec 5
    assert "ai_supervisor" in TRADER_ACL[("query", "get_scoreboard")]


@pytest.mark.asyncio
async def test_a_method_outside_the_set_is_refused_before_sending():
    client = PrincipalClient("ai_author", command=object(), query=object(), commands=AUTHOR_COMMANDS,
                             queries=AUTHOR_QUERIES, timeout=1.0)
    with pytest.raises(MethodNotAllowedLocally):
        await client.call("submit_ai_paper_decision", {})


@pytest.mark.asyncio
async def test_an_unusable_authored_store_arrives_as_a_refusal_error_not_a_reply():
    from trader.messaging.typed_rpc import TypedRpcRemoteError

    class BrokenStore:
        def call(self, method, body, model, timeout, **options):
            raise TypedRpcRemoteError("AUTHORED_STORE_UNAVAILABLE", "the store directory is not writable")
    client = PrincipalClient("ai_author", command=BrokenStore(), query=BrokenStore(), commands=AUTHOR_COMMANDS,
                             queries=AUTHOR_QUERIES, timeout=1.0)
    with pytest.raises(RpcRefused) as caught:
        await client.call("submit_authored_strategy", {})
    assert caught.value.code == "AUTHORED_STORE_UNAVAILABLE"


@pytest.mark.parametrize("author,principals", [(False, ["ai_supervisor", "ai_research"]),
                                               (True, ["ai_supervisor", "ai_research", "ai_author"])])
def test_connect_loads_the_author_key_only_when_authoring_is_on(monkeypatch, author, principals):
    import trader.messaging.typed_rpc as typed_rpc
    loaded = []

    class Identity:
        @staticmethod
        def load(principal, keys_dir):
            loaded.append(principal)
            return principal

    class Client:
        def __init__(self, role, identity, *, server, address, port, timeout):
            self.identity = identity

        def connect(self):
            return None

        def close(self):
            return None
    monkeypatch.setattr(typed_rpc, "ServiceIdentity", Identity)
    monkeypatch.setattr(typed_rpc, "TypedRpcClient", Client)
    clients = AiRpcClients.connect(keys_dir=None, address="tcp://t", query_port=1, command_port=2,
                                   research_address="tcp://r", research_query_port=3, research_command_port=4,
                                   timeout=1.0, author=author)
    assert loaded == principals and (clients.author is not None) is author
    clients.close()
