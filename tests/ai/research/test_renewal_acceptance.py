"""SP2c spec 9 "Renewal version" over signed RPC: expiry -> forward evidence -> renewal case -> Jev -> record ->
register on the same bundle -> the strategy service loads a fresh instance. SP1's real coordinator, no IB.

The trader refuses shadow rows of a session that has not closed, so each test first moves the trader clock to the
renewal evening (``rw.evening(day)``) and only then records the forward rows of the earlier sessions."""
import datetime as dt
import json

import pytest

from tests.ai.decisions.test_flows_acceptance import loop_thread  # noqa: F401
from tests.ai.research.research_world import DEPLOY_PROPOSAL, KEY, RESEARCH_CONIDS, ResearchWorld
from tests.ai.research.rig import NARRATIVE, ruling
from trader.ai.research_roles import BACKTEST_MARKER, RESEARCH_MARKER
from trader.research.evaluation_case import write_evaluation_case
from trader.research.evaluation_request import EvaluationRequestBody, evaluation_request_id
from trader.research.renewal_case import build_renewal_case
from trader.strategy.ai_deployment_source import ai_instance_name

pytestmark = pytest.mark.timeout(300)
NO_CANDIDATES = json.dumps({"candidates": []})
SESSIONS = (dt.date(2026, 7, 20), dt.date(2026, 7, 21), dt.date(2026, 7, 22))   # the first version's sessions


async def deployed(rw):
    """Friday 2026-07-17 evening: Plan 4's INITIAL chain ends in a LIVE version."""
    rw.node.orchestrator.script(RESEARCH_MARKER, DEPLOY_PROPOSAL)
    rw.node.jev.script(BACKTEST_MARKER, ruling("DEPLOY"))
    await rw.night()
    (judgment_id, version), = rw.node_rows("SELECT judgment_id, version_digest FROM ai_research_registrations "
                                           "WHERE state = 'REGISTERED'")
    return judgment_id, version


async def renewal_night(rw, verdict):
    """The renewal evening's research night; the caller has moved the clock there with ``rw.evening(day)``."""
    rw.node.orchestrator.script(RESEARCH_MARKER, NO_CANDIDATES)
    rw.node.jev.script(BACKTEST_MARKER, ruling(verdict))
    await rw.night()


@pytest.mark.asyncio
async def test_a_renewal_deploy_on_the_same_bundle_gets_a_fresh_version(tmp_path, loop_thread, monkeypatch):
    rw = await ResearchWorld.build(tmp_path, loop_thread, monkeypatch, deploy_expiry_sessions=3, renewals=True)
    try:
        initial, v1 = await deployed(rw)
        assert (rw.version(v1)["first_session"], rw.version(v1)["expiry_session"]) == ("2026-07-20", "2026-07-22")
        trials, holdouts, bundles, reviews = rw.strategy_trials(), rw.opened_holdouts(), rw.bundles(), rw.reviews()
        rw.evening(dt.date(2026, 7, 23))
        rw.record_forward_rows(initial, SESSIONS)
        await renewal_night(rw, "DEPLOY")

        (renewal, v2, prior, line, body_json), = rw.node_rows(
            "SELECT judgment_id, version_digest, prior_version_digest, line_state, body_json "
            "FROM ai_research_registrations WHERE kind = 'RENEWAL'")
        assert (prior, line) == (v1, "LIVE") and v2 != v1
        fresh = rw.version(v2)
        assert (fresh["kind"], fresh["base_digest"], fresh["prior_version_digest"], fresh["first_session"]) == (
            "RENEWAL", rw.version(v1)["base_digest"], v1, "2026-07-24")
        assert rw.version(v1)["state"] == "ENDED"                   # superseded: never active again (ruling 13)
        assert rw.node_rows("SELECT line_state, error_code FROM ai_research_registrations WHERE version_digest = ?",
                            [v1]) == [("ENDED", "RENEWED")]
        judged = rw.trader_call("cli", "get_backtest_judgment", {"judgment_id": renewal, "case_digest": None})
        binding = judged["judgment"]["binding"]
        assert (judged["judgment"]["kind"], binding["prior_deployment_version"], binding["stage"]) == (
            "RENEWAL", v1, "FORWARD_COMPLETE")
        # spec 9: a renewal opens no holdout and adds no trial; the bundle and its review are the line's
        assert (rw.strategy_trials(), rw.opened_holdouts(), rw.bundles(), rw.reviews()) == (
            trials, holdouts, bundles, reviews)

        same_day = rw.trader_call("ai_research", "register_ai_deployment", json.loads(body_json))
        assert same_day["outcome"]["version_digest"] == v2           # the same renewal registration twice

        rw.morning(dt.date(2026, 7, 24), 10, 1)
        rw.strategy.reconcile()
        assert rw.strategy.instances() == {v2: ai_instance_name(v2)} and ai_instance_name(v2) != ai_instance_name(v1)
        next_day = rw.trader_call("ai_research", "register_ai_deployment", json.loads(body_json))
        assert (next_day["outcome"]["version_digest"], next_day["outcome"]["created"]) == (v2, False)
    finally:
        rw.close()


@pytest.mark.asyncio
async def test_a_renewal_after_the_bundle_expired_is_refused(tmp_path, loop_thread, monkeypatch):
    monkeypatch.setattr("trader.research.attest_export.ATTESTATION_LIFETIME", dt.timedelta(days=4))
    rw = await ResearchWorld.build(tmp_path, loop_thread, monkeypatch, deploy_expiry_sessions=3, renewals=True)
    try:
        initial, v1 = await deployed(rw)                             # bundle valid until Tuesday 07-21 ~16:32 ET
        assert rw.version(v1)["expiry_session"] == "2026-07-20"      # capped by the bundle (Plan 2 ruling 5)
        rw.evening(dt.date(2026, 7, 21))
        rw.record_forward_rows(initial, SESSIONS[:1])
        jev_calls = len(rw.node.jev.requests)
        await renewal_night(rw, "DEPLOY")
        assert rw.node_rows("SELECT state, end_code FROM ai_research_candidates WHERE kind = 'RENEWAL'") == [
            ("CLOSED", "REFUSED_BUNDLE_EXPIRED")]
        assert rw.node_rows("SELECT line_state, error_code FROM ai_research_registrations WHERE version_digest = ?",
                            [v1]) == [("ENDED", "RENEWAL_REFUSED_BUNDLE_EXPIRED")]
        assert len(rw.node.jev.requests) == jev_calls                # Jev was not asked

        view = rw.forward_view(v1)                                   # a direct signed caller, same answer
        assert (view.renewable.ok, view.renewable.code) == (False, "BUNDLE_EXPIRED")
        case = build_renewal_case(view, created_at=rw.world.served.now(), warmup_sessions=5)
        digest = write_evaluation_case(rw.cases_dir, case, rw.research_signer)
        body = {"judgment_id": "jdg-renewal-direct", "case_digest": digest, "kind": "RENEWAL",
                "renewal_of_version": v1, "verdict": "DEPLOY", "menu": ["DEPLOY", "SHADOW", "REJECT"],
                "jev_model": "vendor/jev-1", "jev_attempt_ref": "att-direct",
                "decided_at": rw.world.served.now().isoformat(), "narrative": NARRATIVE}
        refused = rw.trader_call("ai_research", "record_backtest_judgment", body)
        assert (refused["status"], refused["code"]) == ("REFUSED", "BUNDLE_EXPIRED")
        shadow = rw.trader_call("ai_research", "record_backtest_judgment",
                                {**body, "verdict": "SHADOW", "narrative": None})
        assert shadow["status"] == "RECORDED" and rw.version(v1)["state"] == "ENDED"
    finally:
        rw.close()


@pytest.mark.asyncio
async def test_a_renewal_reject_cools_the_key_down_and_ends_the_line(tmp_path, loop_thread, monkeypatch):
    rw = await ResearchWorld.build(tmp_path, loop_thread, monkeypatch, deploy_expiry_sessions=3, renewals=True)
    try:
        initial, v1 = await deployed(rw)
        rw.evening(dt.date(2026, 7, 23))
        rw.record_forward_rows(initial, SESSIONS[:2])                # 07-22 never gets a row
        await renewal_night(rw, "REJECT")
        (judgment_id, menu, verdict, state), = rw.node_rows(
            "SELECT judgment_id, menu_json, verdict, state FROM ai_backtest_judgments WHERE kind = 'RENEWAL'")
        assert (json.loads(menu), verdict, state) == (["SHADOW", "REJECT"], "REJECT", "RECORDED")
        judged = rw.trader_call("cli", "get_backtest_judgment",
                                {"judgment_id": judgment_id, "case_digest": None})["judgment"]
        assert judged["binding"]["stage"] == "FORWARD_INCOMPLETE" and judged["cooldown_until_session"] is not None
        assert rw.version(v1)["state"] == "ENDED"
        assert rw.node_rows("SELECT line_state, error_code FROM ai_research_registrations WHERE version_digest = ?",
                            [v1]) == [("ENDED", "RENEWAL_REJECT")]
        assert rw.node_rows("SELECT strategy_key, source FROM ai_research_cooldowns") == [(KEY, "REJECT")]
        again = rw.research_client("ai_research", "command").call(
            "submit_evaluation", {"kind": "RENEWAL", "prior_version_digest": v1}, dict)
        assert again["status"] == "DUPLICATE"                        # one renewal case per version
        body = EvaluationRequestBody.model_validate({
            "strategy_key": KEY, "cohort": [{"ENTRY_MINUTE": 615, "EXIT_MINUTE": 660}],
            "conids": sorted(RESEARCH_CONIDS), "bar_size": "15 mins", "research_day": "2026-07-23"})
        claim = rw.trader_call("research", "claim_evaluation",       # the trader's claim is the authority
                               {"request_id": evaluation_request_id(body), "body": body.model_dump()})
        assert (claim["status"], claim["code"]) == ("REFUSED", "FAMILY_COOLING_DOWN")
        rw.morning(dt.date(2026, 7, 24), 10, 1)
        rw.strategy.reconcile()
        assert rw.strategy.instances() == {}
    finally:
        rw.close()
