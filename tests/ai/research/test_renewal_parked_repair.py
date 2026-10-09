"""The runbook's repair of a parked renewal (docs/OPERATIONAL_STATE.md, "Tampered record"), proven end to end:
the ai controller's real ResearchCycle talks to the real research EvaluationService and RenewalRequests (in
process, as ai_research) over a fake trader. The trader-side registry stays scripted."""
import json

import pytest
import pytest_asyncio

from tests.ai.research.cases import V1, recorded, renewed, version_reply
from tests.ai.research.rig import Rig, ruling
from tests.ai.research.test_research_renewal import NO_CANDIDATES, line, seed_live_line
from tests.research.renewal_fixtures import KEY, forward_view
from tests.research.service_fakes import AI, FakeTrader
from trader.ai.research_roles import BACKTEST_MARKER, RESEARCH_MARKER
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research.evaluation_service import EvaluationService
from trader.research.renewal_case import renewal_request_id
from trader.research.renewal_service import RenewalRequests
from trader.research.research_surface import GetEvaluationRequest, SubmitEvaluationRequest
from trader.research.schema import apply_research_migrations
from trader.research.service_store import ResearchStore
from trader.research.signing import AttestationSigner


class InProcessLab:
    """The research server's two lab methods, called as ai_research without the sockets."""

    def __init__(self, evaluations):
        self.evaluations = evaluations

    async def call(self, method, body):
        if method == "submit_evaluation":
            return self.evaluations.submit(SubmitEvaluationRequest.model_validate(body).to_service(), AI)
        if method == "get_evaluation":
            return self.evaluations.get(GetEvaluationRequest.model_validate(body).request_id, AI)
        raise AssertionError(f"unexpected lab call {method}")


class ResearchSide:
    def __init__(self, rig):
        self.root, self.clock = rig.tmp_path, rig.clock
        self.db = DuckDBConnection.get_instance(str(rig.tmp_path / "research.duckdb"))
        apply_research_migrations(SchemaMigrator(self.db))
        self.trader, self.signer = FakeTrader(), AttestationSigner.generate()
        self.trader.forward[V1] = forward_view()

    def start(self):
        """A fresh research service on the same database: what a restart after the repair runs."""
        store = ResearchStore(self.db)
        renewals = RenewalRequests(store=store, trader=self.trader, signer=self.signer,
                                   artifacts_root=self.root / "artifacts", repo_root=self.root,
                                   judge=BacktestJudgeConfig(strategy_allowlist=(KEY,)), warmup_sessions=5,
                                   incomplete_after_hours=16, now=self.clock.now)
        return InProcessLab(EvaluationService(
            store=store, trader=self.trader, build_spec=_never, evaluate=_never, signer=self.signer,
            artifacts_root=self.root / "artifacts", warmup_sessions=5, order_notional=1900.0, queue_max=2,
            now=self.clock.now, renewals=renewals))


def _never(*args):
    raise AssertionError("a renewal builds no cohort spec and runs no evaluation")


@pytest_asyncio.fixture
async def rig(tmp_path):
    built = Rig(tmp_path)
    await built.start()
    return built


async def night(rig, lab, pumps=6):
    rig.lab = lab
    rig.registry.script("get_ai_deployment_version", version_reply("EXPIRED"))
    rig.orchestrator.script(RESEARCH_MARKER, NO_CANDIDATES)
    cycle = rig.cycle()
    await cycle.run_due_slot()
    for _ in range(pumps):
        await cycle.pump()
        rig.clock.advance(31)


def repair_as_the_runbook_says(rig, research):
    """docs/OPERATIONAL_STATE.md, "A parked renewal is final": both services are stopped while this runs."""
    research.db.execute("DELETE FROM research_requests WHERE state = 'PARKED' AND body_json LIKE ?", [f"%{V1}%"])
    rig.store.db.execute("UPDATE ai_research_registrations SET line_state = 'LIVE', error_code = NULL "
                         "WHERE version_digest = ?", [V1])
    rig.store.db.execute("DELETE FROM ai_research_candidates WHERE kind = 'RENEWAL' AND prior_version_digest = ? "
                         "AND candidate_id LIKE 'rr-%'", [V1])


@pytest.mark.asyncio
async def test_a_parked_renewal_repaired_as_the_runbook_says_is_asked_again_and_renews(rig):
    seed_live_line(rig)
    research = ResearchSide(rig)

    def tampered(version):
        research.trader.forward_reads.append(version)
        raise TypedRpcRemoteError("FORWARD_EVIDENCE_TAMPERED", "shadow row 2024-04-02 fails its seal")
    research.trader.forward_evidence = tampered
    await night(rig, research.start())
    (state, parked_reason), = research.db.execute("SELECT state, parked_reason FROM research_requests", fetch="all")
    assert state == "PARKED" and parked_reason.startswith("FORWARD_EVIDENCE_TAMPERED")
    assert line(rig) == [("ENDED", "RENEWAL_EVALUATION_FAILED_NO_CASE")]
    assert rig.rows("SELECT end_code FROM ai_research_candidates") == [("EVALUATION_FAILED_NO_CASE",)]

    del research.trader.forward_evidence                                # the operator repaired the trader's record
    repair_as_the_runbook_says(rig, research)
    rig.clock.advance(24 * 3600)                                        # the next evening's slot
    rig.jev.script(BACKTEST_MARKER, ruling("DEPLOY"))
    rig.registry.script("record_backtest_judgment", recorded)
    rig.registry.script("register_ai_deployment", renewed())
    await night(rig, research.start())

    assert research.trader.forward_reads == [V1, V1]                    # asked again, once
    (request_state,), = research.db.execute("SELECT state FROM research_requests WHERE request_id = ?",
                                            [renewal_request_id(V1)], fetch="all")
    assert request_state == "DONE"
    (record,) = rig.registry.sent("record_backtest_judgment")
    assert (record["kind"], record["renewal_of_version"], record["verdict"]) == ("RENEWAL", V1, "DEPLOY")
    assert line(rig) == [("ENDED", "RENEWED")]
    assert rig.rows("SELECT state, line_state FROM ai_research_registrations WHERE kind = 'RENEWAL'") == [
        ("REGISTERED", "LIVE")]
    assert json.loads(rig.rows("SELECT body_json FROM ai_research_candidates")[0][0]) == {
        "kind": "RENEWAL", "prior_version_digest": V1}
