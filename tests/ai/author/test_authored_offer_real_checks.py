"""The body the research cycle builds for an authored record passes the BUILT trader claim (Plan 3 ruling 7) and the
BUILT research cohort check (Plan 4 ruling 5). Plan 4 F6: every point names exactly the stored tunables and point
0 is the stored defaults."""
import datetime as dt

import pytest

from tests.authored.fake_sandbox import caller_source
from tests.automation.authored_fixtures import authored_world, submit_request
from tests.research.authored_fixtures import RESEARCH_DAY, research_world
from tests.research.evaluation_fixtures import CONIDS
from trader.ai.author_wire import parse_authored_strategies
from trader.ai.authored_offers import authored_submit_body, default_point
from trader.automation.authored_strategies import EVALUATING, STORED
from trader.automation.evaluation_claims import ClaimRefused
from trader.research.cohort import request_body
from trader.research.evaluation_request import evaluation_request_id

PORT_TUNABLES = {"ENTRY_MINUTE": {"low": 585, "high": 630, "default": 600},
                 "EXIT_MINUTE": {"low": 630, "high": 720, "default": 660}}


@pytest.fixture
def trader(tmp_path):
    (tmp_path / "trader").mkdir()
    world = authored_world(tmp_path / "trader")
    reply = world.store.submit(submit_request(caller_source("time_of_day_port"), tunables=PORT_TUNABLES,
                                              bar_size="15 mins"))
    assert reply["status"] == "STORED", reply
    return world


def offered_record(trader):
    (record,) = parse_authored_strategies(trader.store.overview()).records    # the reply the ai service reads
    return record


def claim_body(body: dict):
    """What the research service makes of the ai body before it claims (evaluation_service.submit)."""
    return request_body({key: value for key, value in body.items() if key != "kind"},
                        dt.date.fromisoformat(RESEARCH_DAY))


def claim(trader, request) -> None:
    trader.db.transaction(lambda conn: trader.store.claim_in_tx(conn, evaluation_request_id(request), request,
                                                                trader.clock()))


def test_the_offered_body_passes_the_trader_claim_and_the_research_cohort_check(trader, tmp_path, tmp_duckdb_path):
    record = offered_record(trader)
    request = claim_body(authored_submit_body(record, CONIDS, default_point(record)))
    assert request.cohort == [{"ENTRY_MINUTE": 600.0, "EXIT_MINUTE": 660.0}]
    claim(trader, request)
    assert trader.store.get(record.digest).state == EVALUATING
    (tmp_path / "research").mkdir()
    spec = research_world(tmp_path / "research", tmp_duckdb_path).build_body(request)
    assert spec.file_hash == record.digest and spec.cohort == ({"ENTRY_MINUTE": 600.0, "EXIT_MINUTE": 660.0},)


@pytest.mark.parametrize("point", [{"ENTRY_MINUTE": 600.0}, {"ENTRY_MINUTE": 615.0, "EXIT_MINUTE": 660.0}],
                         ids=["a_tunable_left_out", "point_0_off_the_defaults"])
def test_the_trader_claim_would_refuse_another_point(trader, point):
    """The control for the test above: the claim really checks the point the offer builds."""
    record = offered_record(trader)
    with pytest.raises(ClaimRefused) as refused:
        claim(trader, claim_body(authored_submit_body(record, CONIDS, point)))
    assert refused.value.code == "STRATEGY_NOT_ALLOWED"
    assert trader.store.get(record.digest).state == STORED
