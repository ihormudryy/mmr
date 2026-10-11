"""SP2c spec 9: propose -> claim -> evaluate on fixture bars -> case -> judge -> record -> attest -> register ->
the strategy service loads -> signal -> Jev on the ENTER, on SP1's real coordinator over signed RPC."""
import pytest

from tests.ai.decisions.test_flows_acceptance import loop_thread, ruling as entry_ruling  # noqa: F401
from tests.ai.research.research_world import DEPLOY_PROPOSAL, ResearchWorld
from tests.ai.research.rig import ruling
from tests.sp1_fixtures import CONID
from trader.ai.backtest_judge import replay_backtest_judgment
from trader.ai.ids import derive_decision_id
from trader.ai.replay import COMPLETE, ExternalAdapterCounter
from trader.ai.research_roles import BACKTEST_MARKER, RESEARCH_MARKER
from trader.ai.roles import JEV_MARKER
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research.trader_port import TraderUnavailable

pytestmark = pytest.mark.timeout(300)


@pytest.mark.asyncio
async def test_end_to_end_propose_to_jev_on_the_enter(tmp_path, loop_thread, monkeypatch):
    rw = await ResearchWorld.build(tmp_path, loop_thread, monkeypatch)
    try:
        rw.node.orchestrator.script(RESEARCH_MARKER, DEPLOY_PROPOSAL)
        rw.node.jev.script(BACKTEST_MARKER, ruling("DEPLOY"))
        await rw.night()
        (judgment_id, verdict, state), = rw.node_rows("SELECT judgment_id, verdict, state FROM ai_backtest_judgments")
        assert (verdict, state) == ("DEPLOY", "RECORDED")
        (version, line), = rw.node_rows("SELECT version_digest, line_state FROM ai_research_registrations "
                                        "WHERE state = 'REGISTERED'")
        assert line == "LIVE"
        assert rw.reviews() == [(f"vendor/jev-1#{judgment_id}", "llm", True)]   # reviewer, kind, holdout once
        assert len(rw.bundles()) == 1

        rw.next_morning(10, 15, 30)                                       # Monday: the version is active
        rw.strategy.reconcile()
        assert version in rw.strategy.instances()
        rw.node.jev.script(JEV_MARKER, entry_ruling("TAKE"))
        source = rw.strategy.feed_bar(CONID, rw.morning_bar(CONID))       # TimeOfDay buys on the 10:00 bar (closed 10:15)
        await rw.node.signals()
        decision_id = derive_decision_id(source, f"enter:{CONID}")
        assert rw.world.decision_row(decision_id).deployment_version == version
        assert (await rw.node.node.submitter.get(decision_id)).state in ("ACCEPTED", "FINAL")
        rw.world.settle()
        assert len(rw.world.entries()) == 1 and rw.world.protected()

        counter = ExternalAdapterCounter()
        replayed = await replay_backtest_judgment(rw.node.node.store, judgment_id, config=rw.node.config,
                                                  counter=counter)
        assert (replayed.status, replayed.value["verdict"], counter.total) == (COMPLETE, "DEPLOY", 0)
    finally:
        rw.close()


@pytest.mark.asyncio
async def test_rule_failure_reaches_jev_and_leaves_no_bundle(tmp_path, loop_thread, monkeypatch):  # review focus 1
    rw = await ResearchWorld.build(tmp_path, loop_thread, monkeypatch, holdout_drift=-0.004)   # the holdout fails
    try:
        rw.node.orchestrator.script(RESEARCH_MARKER, DEPLOY_PROPOSAL)
        rw.node.jev.script(BACKTEST_MARKER, ruling("DEPLOY"))
        trials_before = rw.strategy_trials()
        await rw.night()
        assert rw.node_rows("SELECT menu_json, verdict, code, state FROM ai_backtest_judgments") == [
            ('["SHADOW", "REJECT"]', "NO_VERDICT", "JEV_OFF_MENU", "RECORDED")]
        assert rw.bundles() == [] and rw.reviews() == []
        trials_after = rw.strategy_trials()
        assert trials_after > trials_before                               # the evaluation's trials count
        (judgment_id, case_digest), = rw.node_rows("SELECT judgment_id, case_digest FROM ai_backtest_judgments")
        attempt = rw.research_client("ai_research", "command").call("attest_from_judgment",
                                                                    {"judgment_id": judgment_id}, dict)
        assert attempt["status"] == "REFUSED" and rw.bundles() == []      # no review handoff without a DEPLOY
        receipt = rw.trader_call("ai_research", "register_ai_deployment", rw.registration_with_evidence(case_digest))
        assert receipt["state"] == "REJECTED"                             # a judgment without DEPLOY never registers
        assert receipt["error_code"] == "JUDGMENT_NOT_DEPLOY"
        assert rw.strategy_trials() == trials_after                       # a judgment adds no trial
    finally:
        rw.close()


@pytest.mark.asyncio
async def test_a_lost_submit_reply_uses_one_claim(tmp_path, loop_thread, monkeypatch):           # review focus 3
    rw = await ResearchWorld.build(tmp_path, loop_thread, monkeypatch, flaky_lab=True)
    try:
        rw.node.orchestrator.script(RESEARCH_MARKER, DEPLOY_PROPOSAL)
        rw.node.jev.script(BACKTEST_MARKER, ruling("SHADOW"))
        rw.lab_socket("command").script("submit_evaluation", "lose_reply")
        await rw.night()
        assert rw.trader_rows("SELECT COUNT(*) FROM evaluation_claims") == [(1,)]
        assert rw.node_rows("SELECT verdict, state FROM ai_backtest_judgments") == [("SHADOW", "RECORDED")]
    finally:
        rw.close()


@pytest.mark.asyncio
async def test_a_lost_terminal_claim_update_is_sent_again_and_the_judgment_records(tmp_path, loop_thread,
                                                                                    monkeypatch):  # PR #91 4218218688
    rw = await ResearchWorld.build(tmp_path, loop_thread, monkeypatch)
    try:
        rw.node.orchestrator.script(RESEARCH_MARKER, DEPLOY_PROPOSAL)
        rw.node.jev.script(BACKTEST_MARKER, ruling("SHADOW"))
        send, lost = rw.trader_port.update_claim, []

        def lose_the_first_end(request_id, state):
            if state in ("DONE", "FAILED") and not lost:
                lost.append(state)
                raise TraderUnavailable("update_claim: request lost")       # never reaches the trader
            return send(request_id, state)
        monkeypatch.setattr(rw.trader_port, "update_claim", lose_the_first_end)
        await rw.night()                                                   # both services up, no restart
        assert lost == ["DONE"]
        assert rw.trader_rows("SELECT state FROM evaluation_claims") == [("DONE",)]
        assert rw.node_rows("SELECT verdict, state FROM ai_backtest_judgments") == [("SHADOW", "RECORDED")]
    finally:
        rw.close()


@pytest.mark.asyncio
async def test_research_methods_from_the_wrong_signer_are_refused(tmp_path, loop_thread, monkeypatch):
    rw = await ResearchWorld.build(tmp_path, loop_thread, monkeypatch)
    try:
        for principal, role, method in (("ai_supervisor", "command", "submit_evaluation"),
                                        ("strategy", "command", "attest_from_judgment")):
            with pytest.raises(TypedRpcRemoteError) as exc:
                rw.research_client(principal, role).call(method, {}, dict)
            assert exc.value.code in ("PERMISSION_DENIED", "AUTHENTICATION_ERROR")
        with pytest.raises(TypedRpcRemoteError) as exc:
            rw.trader_call("ai_supervisor", "record_backtest_judgment", {})
        assert exc.value.code == "PERMISSION_DENIED"
    finally:
        rw.close()
