"""SP2c Plan 5 Task 7: an EXPIRED line asks for a renewal; the verdict renews it or ends it."""
import hashlib
import json
import logging

import pytest
import pytest_asyncio

from tests.ai.research.cases import (
    BASE, BUNDLE, KEY, RENEWAL_CASE, RENEWAL_REQUEST, V1, V2, binding, judgment_refused, recorded, refused,
    renewal_done, renewal_summary, renewed, submitted, version_reply, view,
)
from tests.ai.research.rig import Rig, ruling
from trader.ai.backtest_judge import judgment_id_for
from trader.ai.ids import canonical_json
from trader.ai.research_cycle import candidate_id_for, registration_body, renewal_candidate_id
from trader.ai.research_roles import BACKTEST_MARKER, RESEARCH_MARKER
from trader.ai.research_wire import Binding, parse_reply
from trader.ai.rpc_clients import RpcOutcomeUnknown, RpcRefused

NO_CANDIDATES = json.dumps({"candidates": []})
LOGGER = "trader.ai.research_cycle"


@pytest_asyncio.fixture
async def rig(tmp_path):
    built = Rig(tmp_path)
    await built.start()
    return built


def seed_live_line(rig):
    """A registered INITIAL line as Plan 4 leaves it; returns its registration body."""
    body = canonical_json(registration_body(parse_reply(Binding, "binding", binding()), judgment_id="jdg-old",
                                            bundle_digest=BUNDLE))
    rig.store.db.execute(
        "INSERT INTO ai_research_registrations (judgment_id, kind, strategy_key, bundle_digest, body_json, "
        "body_sha256, state, base_digest, version_digest, expiry_session, line_state, next_try_at, created_at, "
        "updated_at) VALUES ('jdg-old', 'INITIAL', ?, ?, ?, ?, 'REGISTERED', ?, ?, '2026-10-07', 'LIVE', now(), "
        "now(), now())", [KEY, BUNDLE, body, hashlib.sha256(body.encode()).hexdigest(), BASE, V1])
    return json.loads(body)


async def renewal_night(rig, *, verdict="DEPLOY", submit=None, version_state="EXPIRED", pumps=6,
                        proposal=NO_CANDIDATES, evaluation=None, record=None, register=None, **summary_fields):
    rig.registry.script("get_ai_deployment_version", version_reply(version_state))
    rig.orchestrator.script(RESEARCH_MARKER, proposal)
    rig.lab.script("submit_evaluation", *(submit or [submitted(request_id=RENEWAL_REQUEST, state="DONE")]))
    rig.lab.script("get_evaluation", *(evaluation or [renewal_done(**summary_fields)]))
    rig.jev.script(BACKTEST_MARKER, ruling(verdict))
    rig.registry.script("record_backtest_judgment", *(record or [recorded]))
    rig.registry.script("register_ai_deployment", *(register or [renewed()]))
    cycle = rig.cycle()
    await cycle.run_due_slot()
    for _ in range(pumps):
        await cycle.pump()
        rig.clock.advance(31)
    return cycle


def line(rig, version=V1):
    return rig.rows("SELECT line_state, error_code FROM ai_research_registrations WHERE version_digest = ?", [version])


def errors(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.ERROR]


@pytest.mark.asyncio
async def test_an_expired_line_is_renewed_on_the_same_bundle_without_attestation(rig):     # review focus 1
    prior_body = seed_live_line(rig)
    await renewal_night(rig)
    assert rig.lab.sent("submit_evaluation") == [{"kind": "RENEWAL", "prior_version_digest": V1}]
    (record,) = rig.registry.sent("record_backtest_judgment")
    assert (record["kind"], record["renewal_of_version"], record["case_digest"], record["menu"]) == (
        "RENEWAL", V1, RENEWAL_CASE, ["DEPLOY", "SHADOW", "REJECT"])
    assert record["judgment_id"] == judgment_id_for(RENEWAL_CASE, "RENEWAL") != judgment_id_for(RENEWAL_CASE)
    assert rig.lab.sent("attest_from_judgment") == []
    assert rig.registry.sent("register_ai_deployment") == [{**prior_body, "judgment_id": record["judgment_id"]}]
    assert rig.rows("SELECT kind, prior_version_digest, state, version_digest, line_state FROM "
                    "ai_research_registrations WHERE kind = 'RENEWAL'") == [("RENEWAL", V1, "REGISTERED", V2, "LIVE")]
    assert line(rig) == [("ENDED", "RENEWED")]


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["SHADOW", "REJECT"])
async def test_a_non_deploy_renewal_ends_the_line(rig, verdict):                             # review focus 5
    seed_live_line(rig)
    await renewal_night(rig, verdict=verdict)
    assert line(rig) == [("ENDED", f"RENEWAL_{verdict}")]
    assert rig.registry.sent("register_ai_deployment") == []
    assert rig.rows("SELECT source FROM ai_research_cooldowns") == ([("REJECT",)] if verdict == "REJECT" else [])


@pytest.mark.asyncio
async def test_an_incomplete_forward_window_never_offers_deploy(rig):
    seed_live_line(rig)
    await renewal_night(rig, verdict="DEPLOY", complete=False)
    (record,) = rig.registry.sent("record_backtest_judgment")
    assert (record["verdict"], record["menu"]) == ("NO_VERDICT", ["SHADOW", "REJECT"])
    assert line(rig) == [("ENDED", "RENEWAL_NO_VERDICT")]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["BUNDLE_EXPIRED", "RENEWAL_LINE_ENDED", "FAMILY_COOLING_DOWN",
                                  "STRATEGY_SOURCE_CHANGED"])
async def test_a_refused_renewal_ends_the_line_without_asking_jev(rig, code):
    seed_live_line(rig)
    await renewal_night(rig, submit=[refused(code, request_id=RENEWAL_REQUEST)])
    assert rig.rows("SELECT state, end_code FROM ai_research_candidates") == [("CLOSED", f"REFUSED_{code}")]
    assert line(rig) == [("ENDED", f"RENEWAL_REFUSED_{code}")]
    assert rig.jev.requests == []


@pytest.mark.asyncio
async def test_a_pending_forward_window_is_asked_again(rig):
    seed_live_line(rig)
    await renewal_night(rig, submit=[refused("FORWARD_EVIDENCE_PENDING", request_id=RENEWAL_REQUEST, retryable=True),
                                     submitted(request_id=RENEWAL_REQUEST, state="DONE")], pumps=8)
    assert len(rig.lab.sent("submit_evaluation")) == 2
    assert line(rig) == [("ENDED", "RENEWED")]


@pytest.mark.asyncio
async def test_a_trader_error_behind_the_research_service_keeps_the_line_renewing(rig):    # pins: final review I1
    seed_live_line(rig)
    await renewal_night(rig, submit=[refused("TRADER_ERROR", request_id=RENEWAL_REQUEST, retryable=True),
                                     submitted(request_id=RENEWAL_REQUEST, state="DONE")], pumps=1)
    assert line(rig) == [("RENEWING", None)]
    assert rig.rows("SELECT state, end_code FROM ai_research_candidates") == [("NEW", None)]
    cycle = rig.cycle()
    for _ in range(6):
        await cycle.pump()
        rig.clock.advance(31)
    assert len(rig.lab.sent("submit_evaluation")) == 2
    assert line(rig) == [("ENDED", "RENEWED")]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["WITHDRAWN", "ENDED"])
async def test_a_withdrawn_or_ended_version_ends_its_line_without_a_renewal(rig, state):
    seed_live_line(rig)
    await renewal_night(rig, version_state=state, pumps=0)
    assert line(rig) == [("ENDED", state)] and rig.lab.calls == []
    assert rig.rows("SELECT COUNT(*) FROM ai_research_candidates") == [(0,)]


@pytest.mark.asyncio
async def test_a_renewal_is_requested_once_per_version(rig):
    seed_live_line(rig)
    await renewal_night(rig, pumps=0)
    assert line(rig) == [("RENEWING", None)]
    rig.clock.advance(24 * 3600)                                        # the next evening's slot
    rig.orchestrator.script(RESEARCH_MARKER, NO_CANDIDATES)
    await rig.cycle().run_due_slot()
    assert rig.rows("SELECT candidate_id, kind, prior_version_digest FROM ai_research_candidates") == [
        (renewal_candidate_id(V1), "RENEWAL", V1)]
    assert len(rig.registry.sent("get_ai_deployment_version")) == 1     # a RENEWING line is not read again


@pytest.mark.asyncio
async def test_a_case_for_another_version_is_a_wire_error(rig, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER)
    seed_live_line(rig)
    await renewal_night(rig, prior=V2)                                  # the summary names another version
    assert rig.rows("SELECT state, end_code FROM ai_research_candidates") == [("CLOSED", "RESEARCH_REPLY_MISMATCH")]
    assert line(rig) == [("ENDED", "RENEWAL_RESEARCH_REPLY_MISMATCH")]
    assert any("RESEARCH_REPLY_MISMATCH" in message for message in errors(caplog))
    assert rig.jev.requests == []


INITIAL_PICK = json.dumps({"candidates": [{"strategy": "S1", "universe": "U1", "bar_size": "B3",
                                           "points": [{"ENTRY_MINUTE": 615}], "thesis": "same evening"}]})


@pytest.mark.asyncio
async def test_an_initial_cap_refusal_leaves_the_renewal_eligible(rig):                  # PR #91 4218219455
    seed_live_line(rig)
    renewal_replies = [refused("FORWARD_EVIDENCE_PENDING", request_id=RENEWAL_REQUEST, retryable=True),
                       submitted(request_id=RENEWAL_REQUEST, state="DONE")]

    def submit(body):                       # either order: the renewal is still NEW when the INITIAL is refused
        if body["kind"] == "INITIAL":
            return refused("EVALUATION_LIMIT_REACHED")
        return renewal_replies.pop(0) if len(renewal_replies) > 1 else renewal_replies[0]
    await renewal_night(rig, submit=[submit], pumps=8, proposal=INITIAL_PICK)
    assert rig.rows("SELECT kind, end_code FROM ai_research_candidates ORDER BY kind") == [
        ("INITIAL", "REFUSED_EVALUATION_LIMIT_REACHED"), ("RENEWAL", "JUDGED_DEPLOY")]
    (record,) = rig.registry.sent("record_backtest_judgment")
    assert (record["kind"], record["renewal_of_version"]) == ("RENEWAL", V1)
    assert line(rig) == [("ENDED", "RENEWED")]
    assert rig.rows("SELECT state, version_digest, line_state FROM ai_research_registrations "
                    "WHERE kind = 'RENEWAL'") == [("REGISTERED", V2, "LIVE")]


@pytest.mark.asyncio
async def test_a_renewing_line_closed_without_a_judgment_is_asked_again_at_the_next_slot(rig):
    seed_live_line(rig)
    await renewal_night(rig, pumps=0)                                   # RENEWING, candidate NEW
    rig.store.db.execute("UPDATE ai_research_candidates SET state = 'CLOSED', "     # what the old cap close did
                         "end_code = 'NOT_SUBMITTED_EVALUATION_LIMIT_REACHED'")
    rig.clock.advance(24 * 3600)                                        # the next evening's slot
    rig.orchestrator.script(RESEARCH_MARKER, NO_CANDIDATES)
    cycle = rig.cycle()
    await cycle.run_due_slot()
    assert rig.rows("SELECT state, end_code, cycle_id FROM ai_research_candidates") == [
        ("NEW", None, "rcy-20261009")]                                  # this slot's cycle: not closed as stale
    for _ in range(6):
        await cycle.pump()
        rig.clock.advance(31)
    assert line(rig) == [("ENDED", "RENEWED")]
    assert len(rig.registry.sent("get_ai_deployment_version")) == 1     # reopening reads no version


# -- every end of a renewal candidate ends its line in the same transaction (Plan 5 preflight ruling) ------------
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["submit_evaluation", "get_evaluation"])
async def test_a_refused_research_call_ends_the_line_loudly(rig, caplog, method):
    caplog.set_level(logging.ERROR, logger=LOGGER)
    seed_live_line(rig)
    refusal = [RpcRefused("CASE_UNREADABLE", "loud")]
    await renewal_night(rig, **({"submit": refusal} if method == "submit_evaluation" else {"evaluation": refusal}))
    assert rig.rows("SELECT state, end_code FROM ai_research_candidates") == [("CLOSED", "RPC_CASE_UNREADABLE")]
    assert line(rig) == [("ENDED", "RENEWAL_RPC_CASE_UNREADABLE")]
    (error,) = errors(caplog)
    assert method in error and "RPC_CASE_UNREADABLE" in error
    assert rig.jev.requests == []


@pytest.mark.asyncio
async def test_a_parked_tampered_renewal_ends_the_line(rig, caplog):         # Task 6: ACCEPTED, then FAILED, no case
    caplog.set_level(logging.ERROR, logger=LOGGER)
    seed_live_line(rig)
    await renewal_night(rig, submit=[submitted(request_id=RENEWAL_REQUEST, state="FAILED")],
                        evaluation=[view("FAILED", request_id=RENEWAL_REQUEST)])
    assert rig.rows("SELECT state, end_code FROM ai_research_candidates") == [("CLOSED", "EVALUATION_FAILED_NO_CASE")]
    assert line(rig) == [("ENDED", "RENEWAL_EVALUATION_FAILED_NO_CASE")]
    assert any("EVALUATION_FAILED_NO_CASE" in message for message in errors(caplog))
    assert rig.jev.requests == []


@pytest.mark.asyncio
async def test_a_stale_renewal_evaluation_ends_the_line(rig):
    seed_live_line(rig)
    await renewal_night(rig, evaluation=[view("RUNNING", request_id=RENEWAL_REQUEST)], pumps=2)
    assert line(rig) == [("RENEWING", None)]
    rig.clock.advance(25 * 3600)                                        # the next evening, past evaluation_stale_hours
    rig.orchestrator.script(RESEARCH_MARKER, NO_CANDIDATES)
    cycle = rig.cycle()
    await cycle.run_due_slot()
    await cycle.pump()
    assert rig.rows("SELECT state, end_code FROM ai_research_candidates") == [("CLOSED", "EVALUATION_STALE")]
    assert line(rig) == [("ENDED", "RENEWAL_EVALUATION_STALE")]
    rig.clock.advance(24 * 3600)                                        # an ended line is never asked again
    rig.orchestrator.script(RESEARCH_MARKER, NO_CANDIDATES)
    await rig.cycle().run_due_slot()
    assert rig.rows("SELECT state FROM ai_research_candidates") == [("CLOSED",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply, code", [
    (lambda body: judgment_refused(body["judgment_id"], "RENEWAL_ALREADY_JUDGED"), "RENEWAL_ALREADY_JUDGED"),
    (RpcRefused("JUDGMENT_TAMPERED", "loud"), "RPC_JUDGMENT_TAMPERED")])
async def test_a_refused_renewal_judgment_ends_the_line(rig, caplog, reply, code):
    caplog.set_level(logging.ERROR, logger=LOGGER)
    seed_live_line(rig)
    await renewal_night(rig, record=[reply])
    assert rig.rows("SELECT state, error_code FROM ai_backtest_judgments") == [("REFUSED", code)]
    assert line(rig) == [("ENDED", f"RENEWAL_JUDGMENT_{code}")]
    assert rig.registry.sent("register_ai_deployment") == []
    assert any(code in message for message in errors(caplog))


@pytest.mark.asyncio
async def test_a_retryable_judgment_refusal_keeps_the_line_renewing(rig):
    seed_live_line(rig)
    await renewal_night(rig, record=[lambda body: judgment_refused(body["judgment_id"], "TRY_AGAIN_LATER",
                                                                   retryable=True)], pumps=3)
    assert rig.rows("SELECT state FROM ai_backtest_judgments") == [("DECIDED",)]
    assert line(rig) == [("RENEWING", None)]


@pytest.mark.asyncio
async def test_a_renewal_deploy_without_the_prior_registration_body_ends_the_line(rig, caplog):  # final review M3
    caplog.set_level(logging.ERROR, logger=LOGGER)
    seed_live_line(rig)
    rig.store.db.execute("UPDATE ai_research_registrations SET body_json = NULL")      # a damaged prior row
    cycle = await renewal_night(rig)
    assert rig.rows("SELECT state FROM ai_backtest_judgments") == [("RECORDED",)]
    assert line(rig) == [("ENDED", "RENEWAL_REGISTRATION_BODY_MISSING")]
    assert rig.rows("SELECT COUNT(*) FROM ai_research_registrations WHERE kind = 'RENEWAL'") == [(0,)]
    assert rig.registry.sent("register_ai_deployment") == []
    assert any("RENEWAL_REGISTRATION_BODY_MISSING" in message for message in errors(caplog))
    await cycle.pump()                                                  # nothing is asked again
    assert len(rig.registry.sent("record_backtest_judgment")) == 1


def rejected(code, retryable=False):
    return {**renewed(), "state": "REJECTED", "outcome": None, "error_code": code, "retryable": retryable}


@pytest.mark.asyncio
@pytest.mark.parametrize("reply, code", [(rejected("RENEWAL_PRIOR_INVALID"), "RENEWAL_PRIOR_INVALID"),
                                         (RpcRefused("DEPLOYMENT_CALENDAR_UNAVAILABLE", "loud"),
                                          "RPC_DEPLOYMENT_CALENDAR_UNAVAILABLE")])
async def test_a_refused_renewal_registration_ends_the_line(rig, caplog, reply, code):
    caplog.set_level(logging.ERROR, logger=LOGGER)
    seed_live_line(rig)
    await renewal_night(rig, register=[reply])
    assert rig.rows("SELECT state, error_code, line_state FROM ai_research_registrations WHERE kind = 'RENEWAL'") == [
        ("REFUSED", code, None)]
    assert line(rig) == [("ENDED", f"RENEWAL_REGISTER_{code}")]
    assert any(code in message for message in errors(caplog))


@pytest.mark.asyncio
@pytest.mark.parametrize("reply, state", [({**renewed(), "state": "SUBMITTED", "outcome": None}, "REGISTERING"),
                                          (rejected("AUDIT_UNAVAILABLE", retryable=True), "REGISTERING"),
                                          (rejected("DEPLOY_CAP_REACHED"), "WAITING_CAP")])
async def test_a_registration_that_waits_keeps_the_line_renewing(rig, reply, state):
    seed_live_line(rig)
    await renewal_night(rig, register=[reply])
    assert rig.rows("SELECT state FROM ai_research_registrations WHERE kind = 'RENEWAL'") == [(state,)]
    assert line(rig) == [("RENEWING", None)]


@pytest.mark.asyncio
async def test_a_full_cap_renews_the_line_on_the_next_evening(rig):
    seed_live_line(rig)
    await renewal_night(rig, register=[rejected("DEPLOY_CAP_REACHED"), renewed()])
    rig.clock.advance(24 * 3600)
    rig.orchestrator.script(RESEARCH_MARKER, NO_CANDIDATES)
    cycle = rig.cycle()
    await cycle.run_due_slot()
    await cycle.pump()
    assert line(rig) == [("ENDED", "RENEWED")]
    assert line(rig, V2) == [("LIVE", None)]


@pytest.mark.asyncio
async def test_a_renewal_cut_mid_judgment_is_recorded_as_no_verdict_and_ends_the_line(rig):
    seed_live_line(rig)
    cycle = await renewal_night(rig, pumps=0)
    judgment_id = judgment_id_for(RENEWAL_CASE, "RENEWAL")
    rig.store.db.execute("UPDATE ai_research_candidates SET state = 'EVALUATED', request_id = ?, case_digest = ?, "
                         "summary_json = ?, accepted_at = now()",
                         [RENEWAL_REQUEST, RENEWAL_CASE, json.dumps(renewal_summary())])
    rig.store.db.execute(                                               # a judgment a dead process opened
        "INSERT INTO ai_backtest_judgments (judgment_id, candidate_id, case_digest, kind, prior_version_digest, "
        "menu_json, state, created_at, updated_at) SELECT ?, candidate_id, case_digest, kind, prior_version_digest, "
        "?, 'JUDGING', now(), now() FROM ai_research_candidates", [judgment_id, json.dumps(["DEPLOY", "SHADOW",
                                                                                            "REJECT"])])
    await cycle.recover()
    await cycle.pump()
    (record,) = rig.registry.sent("record_backtest_judgment")
    assert (record["verdict"], record["kind"], record["renewal_of_version"]) == ("NO_VERDICT", "RENEWAL", V1)
    assert line(rig) == [("ENDED", "RENEWAL_NO_VERDICT")]
    assert rig.jev.requests == []


@pytest.mark.asyncio
async def test_the_judgment_row_carries_the_kind_and_prior_of_its_case(rig):
    seed_live_line(rig)
    await renewal_night(rig, verdict="SHADOW")
    assert rig.rows("SELECT judgment_id, kind, prior_version_digest FROM ai_backtest_judgments") == [
        (judgment_id_for(RENEWAL_CASE, "RENEWAL"), "RENEWAL", V1)]


# -- fix round 1 ----------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_renewal_waiting_when_its_window_closes_is_asked_again_in_the_next_slot(rig, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER)
    seed_live_line(rig)
    await renewal_night(rig, submit=[refused("FORWARD_EVIDENCE_PENDING", request_id=RENEWAL_REQUEST, retryable=True),
                                     submitted(request_id=RENEWAL_REQUEST, state="DONE")], pumps=1)
    assert rig.rows("SELECT state, cycle_id FROM ai_research_candidates") == [("NEW", "rcy-20261008")]
    rig.clock.advance(24 * 3600)                                        # the window closed; the next evening
    rig.orchestrator.script(RESEARCH_MARKER, NO_CANDIDATES)
    cycle = rig.cycle()
    await cycle.run_due_slot()                                          # the slot runs before the pump's stale close
    assert rig.rows("SELECT state, cycle_id FROM ai_research_candidates") == [("NEW", "rcy-20261009")]
    for _ in range(6):
        await cycle.pump()
        rig.clock.advance(31)
    assert len(rig.lab.sent("submit_evaluation")) == 2
    assert line(rig) == [("ENDED", "RENEWED")]
    assert errors(caplog) == []                                         # no lost night, no reopen


@pytest.mark.asyncio
async def test_a_renewal_waiting_when_its_window_closes_is_not_closed_stale_by_an_early_pump(rig, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    seed_live_line(rig)
    await renewal_night(rig, submit=[refused("FORWARD_EVIDENCE_PENDING", request_id=RENEWAL_REQUEST, retryable=True),
                                     submitted(request_id=RENEWAL_REQUEST, state="DONE")], pumps=1)
    rig.clock.advance(24 * 3600)                                        # the window closed; the next evening
    rig.orchestrator.script(RESEARCH_MARKER, NO_CANDIDATES)
    cycle = rig.cycle()
    await cycle.pump()                                                  # the pump runs before the slot
    assert rig.rows("SELECT state, end_code FROM ai_research_candidates") == [("CLOSED", "JUDGED_DEPLOY")]
    await cycle.run_due_slot()                                          # no stranded renewal to reopen
    assert rig.lab.sent("submit_evaluation") == [{"kind": "RENEWAL", "prior_version_digest": V1}] * 2
    assert line(rig) == [("ENDED", "RENEWED")]
    assert errors(caplog) == []
    assert not any("STALE_NOT_SUBMITTED" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_a_lost_submit_reply_is_resent_and_the_line_renews(rig):
    seed_live_line(rig)
    await renewal_night(rig, submit=[RpcOutcomeUnknown("REPLY_TIMEOUT"),
                                     submitted(request_id=RENEWAL_REQUEST, state="DONE")])
    first, again = rig.lab.sent("submit_evaluation")
    assert first == again == {"kind": "RENEWAL", "prior_version_digest": V1}
    assert line(rig) == [("ENDED", "RENEWED")]


@pytest.mark.asyncio
async def test_a_lost_registration_reply_is_resent_and_the_line_renews(rig):
    seed_live_line(rig)
    await renewal_night(rig, register=[RpcOutcomeUnknown("REPLY_TIMEOUT"), renewed()])
    first, again = rig.registry.sent("register_ai_deployment")
    assert first == again
    assert line(rig) == [("ENDED", "RENEWED")]
    assert line(rig, V2) == [("LIVE", None)]


@pytest.mark.asyncio
async def test_a_duplicate_request_ends_the_renewal_line(rig):
    seed_live_line(rig)
    rig.store.db.execute(                                               # another candidate already holds the request
        "INSERT INTO ai_research_candidates (candidate_id, cycle_id, kind, strategy_key, body_json, body_sha256, "
        "state, request_id, next_try_at, created_at, updated_at) VALUES (?, 'rcy-20261007', 'INITIAL', ?, '{}', 'x', "
        "'CLOSED', ?, now(), now() - INTERVAL 1 DAY, now())",
        [candidate_id_for("rcy-20261007", KEY), KEY, RENEWAL_REQUEST])
    await renewal_night(rig)
    assert rig.rows("SELECT end_code FROM ai_research_candidates WHERE kind = 'RENEWAL'") == [("DUPLICATE_REQUEST",)]
    assert line(rig) == [("ENDED", "RENEWAL_DUPLICATE_REQUEST")]


@pytest.mark.asyncio
async def test_no_renewal_candidate_is_written_for_a_line_that_is_no_longer_live(rig):
    seed_live_line(rig)
    rig.store.db.execute("UPDATE ai_research_registrations SET line_state = 'ENDED', error_code = 'WITHDRAWN'")
    cycle = rig.cycle()
    slot = cycle._slots.research_slot(rig.clock.now())
    await rig.store.atransaction(lambda conn: cycle._request_renewal_in_tx(conn, slot, V1, KEY))
    assert rig.rows("SELECT COUNT(*) FROM ai_research_candidates") == [(0,)]
    assert line(rig) == [("ENDED", "WITHDRAWN")]
