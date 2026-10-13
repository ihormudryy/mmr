"""The strict author wire reads what the BUILT trader store sends: overview() and the submit replies."""
import pytest

from tests.ai.author.helpers import authored_reply, record
from tests.automation.authored_fixtures import (authored_world, deployed, evaluating, request_id, source, stored,
                                                submit_request)
from trader.ai.author_wire import parse_authored_strategies, parse_submit_reply
from trader.ai.research_wire import WireError


def test_the_real_overview_parses_with_records_in_several_states(tmp_path):
    world = authored_world(tmp_path)
    waiting = stored(world, 15)
    running = stored(world, 16)
    evaluating(world, running, request_id(16))
    reply = parse_authored_strategies(world.store.overview())
    assert (reply.enabled, reply.max_active, reply.counted, reply.room, reply.judged_count) == (True, 3, 2, 1, 0)
    assert reply.cap_has_room and reply.bar_sizes == list(world.store.config.bar_sizes)
    assert {r.digest: r.state for r in reply.records} == {waiting: "STORED", running: "EVALUATING"}
    first = reply.records[0]
    assert first.key == f"authored:{first.digest}:RangeBreak" and set(first.tunables) == {"RANGE_BARS", "MIN_PROBABILITY"}
    assert (first.tunables["RANGE_BARS"].low, first.tunables["RANGE_BARS"].high) == (5, 30)


def test_a_real_overview_with_a_deployed_record_and_a_full_cap_has_no_room(tmp_path):
    world = authored_world(tmp_path, max_active=1)
    digest = deployed(world)
    reply = parse_authored_strategies(world.store.overview())
    assert [(r.digest, r.state) for r in reply.records] == [(digest, "DEPLOYED")]
    assert reply.cap_has_room is False and reply.records[0].deployment_version is not None
    assert [entry["event"] for entry in reply.records[0].history][0] == "SUBMITTED"


def test_the_real_overview_of_a_disabled_store_says_so(tmp_path):
    world = authored_world(tmp_path, enabled=False)
    assert parse_authored_strategies(world.store.overview()).enabled is False


def test_the_real_overview_and_the_test_builder_have_the_same_keys(tmp_path):
    world = authored_world(tmp_path)
    stored(world)
    real = world.store.overview()
    assert set(real) == set(authored_reply())
    assert set(real["records"][0]) == set(record())


def test_the_real_submit_replies_parse(tmp_path):
    world = authored_world(tmp_path, max_active=1)
    first = parse_submit_reply(world.store.submit(submit_request(source(15))))
    assert first.status == "STORED" and first.record.state == "STORED" and first.record.reason is None
    again = parse_submit_reply(world.store.submit(submit_request(source(15))))
    assert (again.status, again.record.digest) == ("EXISTING", first.record.digest)
    full = parse_submit_reply(world.store.submit(submit_request(source(16))))
    assert (full.status, full.code, full.retryable, full.record) == ("REFUSED", "AUTHORED_CAP_FULL", False, None)


def test_a_real_refusal_for_a_disabled_store_parses(tmp_path):
    world = authored_world(tmp_path, enabled=False)
    refused = parse_submit_reply(world.store.submit(submit_request()))
    assert (refused.status, refused.code, refused.record) == ("REFUSED", "AUTHORED_STRATEGIES_DISABLED", None)


def test_a_real_reply_with_an_extra_key_is_refused_by_the_parser(tmp_path):
    world = authored_world(tmp_path)
    stored(world)
    with pytest.raises(WireError, match="get_authored_strategies"):
        parse_authored_strategies({**world.store.overview(), "next_slot": "tonight"})
