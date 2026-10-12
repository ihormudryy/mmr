"""SP3 Plan 3 Task 8: isolation proofs, staged deploy and the entry gate (spec 7.9, 8, 10 Staged deploy and
Authority; rulings 10, 12, 16, 21)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tests.automation.authored_fixtures import (
    IMAGE, OTHER_IMAGE, authored_journal, digest_of, proof_request, request_id, source, submit_request,
)
from tests.automation.backtest_judge_fixtures import Clock
from tests.automation.judged_deployment import Cooldowns, SeededJudgments, deploy_facts
from tests.automation.test_ai_deployment_registration import FakeBundles
from trader.automation.ai_bundle_check import BundleFacts
from trader.automation.ai_deployment_activity import DeploymentActivity, deployment_version_gate
from trader.automation.ai_deployment_registration import AiDeploymentRegistrar, registration_command_id
from trader.automation.ai_deployment_versions import AiDeploymentVersionStore, apply_ai_deployment_version_migrations
from trader.automation.ai_deployments import (
    AiDeployment, AiDeploymentStore, DeploymentRefused, apply_ai_deployment_migration,
)
from trader.automation.ai_paper_decision import apply_ai_paper_decision_migration
from trader.automation.ai_paper_evidence import AI_PAPER_ACTION
from trader.automation.authored_config import AuthoredStrategiesConfig
from trader.automation.authored_files import AuthoredFileStore
from trader.automation.authored_isolation import IsolationProofs
from trader.automation.authored_strategies import (
    DEPLOYED, EVALUATING, SHADOW, AuthoredStrategyStore, move_in_tx, record_in_tx,
)
from trader.automation.authored_wire import ReportAuthoredEvaluationRequest, RetireAuthoredStrategyRequest
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.protective_order_saga import apply_protective_order_saga_migration
from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.research.strategy_key import authored_strategy_path

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 9, 21, 0, tzinfo=UTC)         # Friday after the close
FIRST_SESSION = dt.datetime(2026, 10, 12, 15, 0, tzinfo=UTC)


# -- proofs ------------------------------------------------------------------------------------------------

def test_a_proof_is_recorded_once_and_read_back(tmp_path):
    proofs = IsolationProofs(authored_journal(tmp_path), now=Clock(NOW))
    first = proofs.record(proof_request(), principal="cli")
    assert (first["status"], first["proof"]["image_digest"], first["proof"]["passed"]) == ("RECORDED", IMAGE, True)
    assert proofs.record(proof_request(), principal="cli") == {"status": "EXISTING", "proof": first["proof"]}
    assert proofs.latest(IMAGE).to_json() == first["proof"]
    assert proofs.latest(None).image_digest == IMAGE and proofs.passing_for(IMAGE)


def test_a_later_failing_run_of_the_same_image_withdraws_the_proof(tmp_path):
    clock = Clock(NOW)
    proofs = IsolationProofs(authored_journal(tmp_path), now=clock)
    proofs.record(proof_request(), principal="cli")
    clock.now = NOW + dt.timedelta(hours=1)
    proofs.record(proof_request(passed=False, ran_at=clock.now), principal="cli")
    assert not proofs.passing_for(IMAGE)


def test_each_image_needs_its_own_run(tmp_path):                                   # spec 10 Staged deploy
    proofs = IsolationProofs(authored_journal(tmp_path), now=Clock(NOW))
    proofs.record(proof_request(IMAGE), principal="cli")
    assert proofs.passing_for(IMAGE) and not proofs.passing_for(OTHER_IMAGE) and not proofs.passing_for(None)


@pytest.mark.parametrize("change", [
    {"passed": True, "checks": [{"name": "a", "passed": False, "detail": ""}]},      # ruling 21
    {"checks": [{"name": "a", "passed": True, "detail": ""}, {"name": "a", "passed": True, "detail": ""}]},
    {"checks": []},
    {"ran_at": "2026-10-09T21:00:00"},                                                # no offset
    {"image_digest": "unset"},                                                         # Plan 2: never started
    {"image_digest": "has space"},
])
def test_an_inconsistent_proof_body_never_passes_the_wire(change):
    raw = {**proof_request().model_dump(), **change}
    with pytest.raises(ValidationError):
        type(proof_request()).model_validate(raw)


# -- registration and entries ------------------------------------------------------------------------------

class Stack:
    """SP2c registration and activity over one journal, wired to an authored store as the trader wires them."""

    def __init__(self, tmp_path):
        self.tmp_path, self.now = tmp_path, NOW
        self.db = authored_journal(tmp_path)
        migrator = SchemaMigrator(self.db)
        for apply in (apply_ai_deployment_migration, apply_protective_order_saga_migration,
                      apply_ai_paper_decision_migration, apply_ai_deployment_version_migrations):
            apply(migrator)
        self.journal = DomainJournal(self.db)
        self.journal.migrate(migrator)
        clock = lambda: self.now                                                   # noqa: E731
        self.proofs = IsolationProofs(self.db, now=clock)
        self.deployments = AiDeploymentStore(self.db, now=clock)
        self.versions = AiDeploymentVersionStore(self.journal, now=clock)
        self.judgments, self.cooldowns, self.bundles = SeededJudgments(), Cooldowns(), FakeBundles()
        self.rebuild(enabled=True)

    def rebuild(self, *, enabled: bool) -> None:
        """A trader restart with the switch set to ``enabled``."""
        clock = lambda: self.now                                                   # noqa: E731
        self.store = AuthoredStrategyStore(self.db, config=AuthoredStrategiesConfig(enabled=enabled),
                                           files=AuthoredFileStore(self.tmp_path / "store"), now=clock,
                                           proofs=self.proofs)
        self.activity = DeploymentActivity(versions=self.versions, deployments=self.deployments,
                                           judgments=self.judgments, cooldowns=self.cooldowns, max_active=3,
                                           now=clock, authored=self.store)
        self.registrar = AiDeploymentRegistrar(
            journal=self.journal, deployments=self.deployments, versions=self.versions, activity=self.activity,
            judgments=self.judgments, cooldowns=self.cooldowns, bundles=self.bundles,
            calendar=XNYSCalendarPolicy(), expiry_sessions=20, now=clock, authored=self.store)

    def awaiting(self, *, range_bars: int = 15, judgment_id: str = "jdg-00000001", image: str = IMAGE):
        """Stored, claimed, STARTED on ``image`` and judged DEPLOY: SHADOW AWAITING_REGISTRATION."""
        digest = self.store.submit(submit_request(source(range_bars)))["record"]["digest"]
        claim = request_id(range_bars)
        self.db.transaction(lambda conn: move_in_tx(
            conn, record_in_tx(conn, digest), EVALUATING, event="CLAIMED", reason=None, now=self.now,
            request_id=claim, evaluation_request_id=claim))
        self.store.report(ReportAuthoredEvaluationRequest.model_validate({
            "digest": digest, "request_id": claim, "event": "STARTED", "sandbox_image_digest": image,
            "detail": ""}))
        self.db.transaction(lambda conn: move_in_tx(
            conn, record_in_tx(conn, digest), SHADOW, event="JUDGED", reason="AWAITING_REGISTRATION", now=self.now,
            verdict="DEPLOY", judgment_id=judgment_id, evaluation_request_id=None))
        record = self.deployment_record(digest, range_bars)
        self.judge(judgment_id, record)
        return digest, record

    @staticmethod
    def deployment_record(digest: str, n: int) -> dict:
        return {"strategy_path": authored_strategy_path(digest), "strategy_digest": digest,
                "class_name": "RangeBreak", "params": {"RANGE_BARS": 10}, "conids": [265598, 272093],
                "bar_size": "5 mins", "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
                "evidence_ref": f"sha256:{n:064x}", "evidence_order_notional": 2000.0}

    def judge(self, judgment_id: str, record: dict) -> None:
        self.judgments.seed(deploy_facts(record, judgment_id))
        self.bundles.facts[record["evidence_ref"]] = BundleFacts(
            record["evidence_ref"], "art-1", "fam-1", record["strategy_path"], record["class_name"],
            record["strategy_digest"], record["params"], tuple(sorted(record["conids"])), record["bar_size"],
            record["evidence_order_notional"], f"jev-model#{judgment_id}", "llm", NOW + dt.timedelta(days=90))

    def register(self, judgment_id: str, record: dict) -> dict:
        body = {"judgment_id": judgment_id, "bundle_digest": record["evidence_ref"],
                "deployment": AiDeployment.from_json(record).to_json()}
        return self.registrar.register(body, principal="ai_research",
                                       command_id=registration_command_id(body, self.now.date()))

    def refused(self, judgment_id: str, record: dict) -> str:
        with pytest.raises(DeploymentRefused) as refused:
            self.register(judgment_id, record)
        return refused.value.code


def test_staged_deploy_registers_nothing_until_the_running_image_has_a_passing_proof(tmp_path):  # spec 10
    s = Stack(tmp_path)
    digest, record = s.awaiting()
    assert s.store.running_image() == IMAGE
    assert s.refused("jdg-00000001", record) == "AUTHORED_ISOLATION_UNPROVEN"
    assert s.versions.sealed() == () and s.store.get(digest).state == SHADOW
    s.proofs.record(proof_request(OTHER_IMAGE), principal="cli")
    assert s.refused("jdg-00000001", record) == "AUTHORED_ISOLATION_UNPROVEN"
    s.proofs.record(proof_request(IMAGE), principal="cli")
    outcome = s.register("jdg-00000001", record)
    assert outcome["created"] is True
    stored = s.store.get(digest)
    assert (stored.state, stored.deployment_version) == (DEPLOYED, outcome["version_digest"])


def test_a_rebuilt_image_needs_a_new_run(tmp_path):                                           # spec 7.9
    s = Stack(tmp_path)
    s.proofs.record(proof_request(IMAGE), principal="cli")
    _first, first_record = s.awaiting(range_bars=15, judgment_id="jdg-00000001", image=IMAGE)
    assert s.register("jdg-00000001", first_record)["created"] is True
    s.now = NOW + dt.timedelta(minutes=5)              # the sandbox was rebuilt; research's next STARTED says so
    _second, second_record = s.awaiting(range_bars=16, judgment_id="jdg-00000002", image=OTHER_IMAGE)
    assert s.store.running_image() == OTHER_IMAGE
    assert s.refused("jdg-00000002", second_record) == "AUTHORED_ISOLATION_UNPROVEN"
    s.proofs.record(proof_request(OTHER_IMAGE), principal="cli")
    assert s.register("jdg-00000002", second_record)["created"] is True


def test_a_failed_run_recorded_after_the_preflight_stops_the_seal(tmp_path):              # review r1 #5
    s = Stack(tmp_path)
    s.proofs.record(proof_request(IMAGE), principal="cli")
    digest, record = s.awaiting()
    preflight = s.store.registration_refusal

    def preflight_then_a_failed_run(*args, **kwargs):
        preflight(*args, **kwargs)                       # the preflight passed on the old proof
        s.now = s.now + dt.timedelta(minutes=1)
        s.proofs.record(proof_request(IMAGE, passed=False, ran_at=s.now), principal="cli")
    s.store.registration_refusal = preflight_then_a_failed_run
    assert s.refused("jdg-00000001", record) == "AUTHORED_ISOLATION_UNPROVEN"
    assert s.versions.sealed() == () and s.store.get(digest).state == SHADOW


def test_the_evaluated_build_and_the_latest_reported_build_must_both_be_proven(tmp_path):  # review r1 #5
    s = Stack(tmp_path)
    s.proofs.record(proof_request(IMAGE), principal="cli")
    _first, first_record = s.awaiting(range_bars=15, judgment_id="jdg-00000001", image=IMAGE)
    s.now = NOW + dt.timedelta(minutes=5)
    s.awaiting(range_bars=16, judgment_id="jdg-00000002", image=OTHER_IMAGE)   # research now sees a rebuilt sandbox
    assert s.store.running_image() == OTHER_IMAGE
    assert s.refused("jdg-00000001", first_record) == "AUTHORED_ISOLATION_UNPROVEN"
    s.proofs.record(proof_request(OTHER_IMAGE), principal="cli")
    assert s.register("jdg-00000001", first_record)["created"] is True


def test_registration_is_refused_while_the_switch_is_off(tmp_path):                           # ruling 8
    s = Stack(tmp_path)
    s.proofs.record(proof_request(IMAGE), principal="cli")
    _digest, record = s.awaiting()
    s.rebuild(enabled=False)
    assert s.refused("jdg-00000001", record) == "AUTHORED_STRATEGIES_DISABLED"


def test_registration_needs_the_record_to_await_that_judgment(tmp_path):
    s = Stack(tmp_path)
    s.proofs.record(proof_request(IMAGE), principal="cli")
    _digest, record = s.awaiting(judgment_id="jdg-00000001")
    s.judge("jdg-00000002", record)                     # another DEPLOY judgment of the same deployment
    assert s.refused("jdg-00000002", record) == "AUTHORED_STATE_CONFLICT"


def test_a_path_naming_another_file_than_the_digest_is_a_mismatch(tmp_path):
    s = Stack(tmp_path)
    s.proofs.record(proof_request(IMAGE), principal="cli")
    digest, record = s.awaiting()
    other = {**record, "strategy_path": authored_strategy_path(digest_of(source(29)))}
    s.judge("jdg-00000001", other)
    assert s.refused("jdg-00000001", other) == "JUDGMENT_MISMATCH"


def registered(s: Stack):
    s.proofs.record(proof_request(IMAGE), principal="cli")
    digest, record = s.awaiting()
    outcome = s.register("jdg-00000001", record)
    s.now = FIRST_SESSION
    return digest, outcome["digest"], outcome["version_digest"]


def test_an_authored_entry_needs_the_switch_and_a_deployed_record(tmp_path):                 # ruling 16
    s = Stack(tmp_path)
    digest, base, version = registered(s)
    assert s.activity.entry_refusal(deployment_digest=base, version_digest=version, source_digest=digest) is None
    assert [active.version_digest for active in s.activity.active()] == [version]
    s.rebuild(enabled=False)
    assert s.activity.entry_refusal(deployment_digest=base, version_digest=version,
                                    source_digest=digest) == "AUTHORED_STRATEGIES_DISABLED"
    assert s.activity.active() == ()                      # the strategy service does not load it either
    s.rebuild(enabled=True)
    s.store.retire(RetireAuthoredStrategyRequest.model_validate(
        {"digest": digest, "code": "SANDBOX_ERROR", "detail": "child crashed"}))
    assert s.activity.entry_refusal(deployment_digest=base, version_digest=version,
                                    source_digest=digest) == "DEPLOYMENT_NOT_ACTIVE"
    assert s.activity.active() == ()                      # a retired strategy is not reloaded after a restart


def test_switch_off_refuses_an_authored_enter_at_dispatch_but_never_a_close(tmp_path):      # spec 10 Authority
    s = Stack(tmp_path)
    digest, base, version = registered(s)
    s.rebuild(enabled=False)
    gate = deployment_version_gate(kind_of=s.deployments.kind_of, activity=s.activity)
    enter = SimpleNamespace(action=AI_PAPER_ACTION, body={
        "action": "ENTER", "deployment_digest": base, "deployment_version": version, "source_digest": digest,
        "decision_id": "dec-1"})
    close = SimpleNamespace(action=AI_PAPER_ACTION, body={"action": "CLOSE", "deployment_digest": None,
                                                           "decision_id": "dec-2"})
    assert gate(enter, None, None, s.now) == "AUTHORED_STRATEGIES_DISABLED"
    assert gate(close, None, None, s.now) is None


def test_an_authored_deployment_without_a_store_never_enters(tmp_path):
    s = Stack(tmp_path)
    digest, base, version = registered(s)
    bare = DeploymentActivity(versions=s.versions, deployments=s.deployments, judgments=s.judgments,
                              cooldowns=s.cooldowns, max_active=3, now=lambda: s.now)
    assert bare.entry_refusal(deployment_digest=base, version_digest=version,
                              source_digest=digest) == "AUTHORED_STRATEGIES_DISABLED"
