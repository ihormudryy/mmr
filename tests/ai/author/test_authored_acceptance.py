"""SP3 spec 11 step 4, end to end over signed RPC: switch on -> the author (scripted model) writes the ORB port ->
the trader checks and stores it -> research evaluates it through the sandbox (Plan 2's in-process mode) -> Jev
(scripted) DEPLOY -> registration refused AUTHORED_ISOLATION_UNPROVEN -> an isolation proof for the running image ->
registered the next evening -> live signals through the live proxy -> switch off refuses authored entries, and the
strategy's own exit still closes."""
import pytest

from tests.ai.author.authored_world import MONDAY, TUESDAY, AuthoredWorld
from tests.ai.author.helpers import ORB_SOURCE, answer_json
from tests.ai.decisions.test_flows_acceptance import loop_thread, ruling as entry_ruling, settle_close  # noqa: F401
from tests.ai.research.rig import ruling
from tests.sp1_fixtures import CONID, MSFT
from trader.ai.author_roles import AUTHOR_MARKER
from trader.ai.ids import attempt_ref, derive_decision_id
from trader.ai.research_roles import BACKTEST_MARKER, RESEARCH_MARKER
from trader.ai.roles import JEV_MARKER
from trader.messaging.typed_rpc import TypedRpcRemoteError

pytestmark = pytest.mark.timeout(600)
EMPTY_PROPOSAL = '{"candidates": []}'


@pytest.mark.asyncio
async def test_an_authored_strategy_goes_from_the_author_to_live_signals(tmp_path, loop_thread, monkeypatch):
    aw = await AuthoredWorld.build(tmp_path, loop_thread, monkeypatch)
    try:
        aw.set_switch(True)
        aw.node.orchestrator.script(RESEARCH_MARKER, EMPTY_PROPOSAL)
        aw.node.author.script(AUTHOR_MARKER, answer_json())
        aw.node.jev.script(BACKTEST_MARKER, ruling("DEPLOY"))
        await aw.night()                                                  # Friday after the close

        (state, digest, key), = aw.node_rows("SELECT state, digest, authored_key FROM author_attempts")
        assert state == "STORED" and aw.stored_file(digest) == ORB_SOURCE.encode("utf-8")
        assert aw.sandbox_sessions("research") >= 1 and aw.sandbox_sessions("live") == 0
        (verdict, judged), = aw.node_rows(
            "SELECT j.verdict, j.state FROM ai_backtest_judgments j JOIN ai_research_candidates c "
            "ON c.candidate_id = j.candidate_id WHERE c.strategy_key = ?", [key])
        assert (verdict, judged) == ("DEPLOY", "RECORDED")
        assert aw.node_rows("SELECT state, error_code FROM ai_research_registrations") == [
            ("WAITING_CAP", "AUTHORED_ISOLATION_UNPROVEN")]                     # review focus 4
        assert aw.authored_state(digest) in ("EVALUATING", "SHADOW")
        await aw.node.report()                                            # the author's cost reaches the trader
        (cost_role, cost_attempt, cost_served), = aw.trader_rows(
            "SELECT role, attempt_id, served_id FROM ai_costs WHERE role = 'author'")
        (author_key,), = aw.node_rows("SELECT attempt_key FROM ai_model_attempts WHERE request_key LIKE 'aut-%'")
        assert cost_role == "author" and cost_attempt == attempt_ref(author_key) and cost_served.startswith("aut-")

        aw.record_isolation_proof()                     # the running image's digest, as prove-isolation records it
        aw.evening(MONDAY)
        aw.node.orchestrator.script(RESEARCH_MARKER, EMPTY_PROPOSAL)
        await aw.night(author=False)                                      # the waiting registration goes through
        (version, line), = aw.node_rows("SELECT version_digest, line_state FROM ai_research_registrations "
                                        "WHERE state = 'REGISTERED'")
        assert line == "LIVE" and aw.authored_state(digest) == "DEPLOYED"

        aw.morning_at(TUESDAY, 10, 16)                                    # the 10:00 bar closed at 10:15 (#146)
        aw.strategy.reconcile()
        assert version in aw.strategy.instances()
        aw.node.jev.script(JEV_MARKER, entry_ruling("TAKE"), entry_ruling("TAKE"))
        source = aw.strategy.feed_bar(CONID, aw.rising_bars(CONID, TUESDAY, until=(10, 0)))
        assert source is not None and aw.sandbox_sessions("live") >= 1   # the ORB port bought through the sandbox
        await aw.node.signals()
        entry_id = derive_decision_id(source, f"enter:{CONID}")
        assert aw.world.decision_row(entry_id).deployment_version == version
        aw.world.settle()
        assert len(aw.world.entries()) == 1 and aw.world.protected()

        aw.set_switch(False)                                              # the owner turns authored strategies off
        refused = aw.strategy.feed_bar(MSFT, aw.rising_bars(MSFT, TUESDAY, until=(10, 0)))
        await aw.node.signals()
        assert aw.world.decision_row(derive_decision_id(refused, f"enter:{MSFT}")).error_code == \
            "AUTHORED_STRATEGIES_DISABLED"
        assert len(aw.world.entries()) == 1

        aw.morning_at(TUESDAY, 11, 16)                                    # the 11:00 bar closed at 11:15
        exit_source = aw.strategy.feed_bar(CONID, aw.rising_bars(CONID, TUESDAY, until=(11, 0)))   # ORB sells
        await aw.node.signals()
        await settle_close(aw.world, aw.node, derive_decision_id(exit_source, f"close:{CONID}"))
        assert not aw.world.protected()                                   # closes always stay allowed

        view = aw.scoreboard()["authored_vs_catalogue"]
        assert view["label"] == "PAPER" and view["reason"] != "SCOPE_UNKNOWN"   # the reader saw Plan 3's table
    finally:
        aw.close()


@pytest.mark.asyncio
async def test_only_ai_author_may_submit_or_read_authored_strategies(tmp_path, loop_thread, monkeypatch):
    aw = await AuthoredWorld.build(tmp_path, loop_thread, monkeypatch)
    try:
        for principal in ("ai_supervisor", "ai_research", "research", "strategy", "cli"):
            for method in ("submit_authored_strategy", "get_authored_strategies"):
                with pytest.raises(TypedRpcRemoteError) as exc:
                    aw.trader_call(principal, method, {})
                assert exc.value.code == "PERMISSION_DENIED", (principal, method)
    finally:
        aw.close()


@pytest.mark.asyncio
async def test_switch_off_skips_the_night_without_a_model_call(tmp_path, loop_thread, monkeypatch):
    aw = await AuthoredWorld.build(tmp_path, loop_thread, monkeypatch)
    try:
        aw.set_switch(False)
        aw.node.orchestrator.script(RESEARCH_MARKER, EMPTY_PROPOSAL)              # the research slot still runs
        await aw.night()
        assert aw.node_rows("SELECT state, reason FROM author_attempts") == [
            ("SKIPPED", "AUTHORED_STRATEGIES_DISABLED")]
        assert aw.node.author.requests == []
        assert aw.node_rows("SELECT COUNT(*) FROM ai_research_candidates") == [(0,)]
    finally:
        aw.close()
