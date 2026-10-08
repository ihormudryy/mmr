import datetime as dt
import hashlib
import json
import shutil
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.research.evaluation_fixtures import CONIDS, evaluate_synthetic, holdout_ruleset, submit_review
from tests.research.service_fakes import AI, CLI, FakeTrader, judgment_view
from trader.data.duckdb_store import DuckDBConnection
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research.attest_export import ATTESTATION_LIFETIME
from trader.research.bundle import ResearchBundle
from trader.research.eligibility import EligibilityDecisionRepository
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.evaluation_case import CASE_DOMAIN, EvaluationCase, load_verified_case, write_evaluation_case
from trader.research.judgment_attest import JudgmentAttest, is_loud_trader_code, is_paper_posture
from trader.research.review import REVIEW_NARRATIVE_FIELDS, OperatorReviewRepository, ReviewConflict
from trader.research.service_store import ResearchStore
from trader.research.signing import AttestationSigner

NOW = dt.datetime(2026, 10, 8, 21, tzinfo=dt.timezone.utc)
NARRATIVE = {name: f"jev on {name}" for name in REVIEW_NARRATIVE_FIELDS}
KEY = "strategies/time_of_day.py:TimeOfDay"

pytestmark = pytest.mark.timeout(240)


@pytest.fixture(scope="module")
def evaluated(tmp_path_factory):
    repo = tmp_path_factory.mktemp("evaluated")
    evaluation = evaluate_synthetic(repo, str(repo / "bars.duckdb"))
    assert evaluation.result.stage == "complete"
    return repo, evaluation.result, evaluation.spec


def make_case(result, spec, db, file_hash, **changes) -> EvaluationCase:
    decision = EligibilityDecisionRepository(db).get(result.decision_digest)
    rules = [{"code": r.code, "passed": r.passed} for r in decision.results]
    raw = {"schema_version": CASE_DOMAIN, "kind": "INITIAL", "request_id": "sha256:" + "1" * 64,
           "claim_day": "2026-10-08", "strategy_key": KEY, "strategy_file_hash": file_hash,
           "cohort": [dict(spec.params)], "conids": sorted(CONIDS), "bar_size": "15 mins", "stage": "COMPLETE",
           "holdout_passed": True, "selected_params": dict(spec.params), "family_id": result.family_id,
           "selected_trial_id": "t1",
           "artifact_id": result.artifact_id, "eligibility_decision_digest": result.decision_digest,
           "decision_state": decision.state, "ruleset_digest": decision.ruleset_digest,
           "final_rule_results": rules,
           "renewal": None, "created_at": NOW.isoformat(),
           "evidence": {"holdout": {"start": "x", "end": "y", "passed": True, "detail": ""}, "selected_index": 0,
                        "points": [{"index": 0, "params": dict(spec.params), "trial_id": "t1",
                                    "pre_holdout_passed": True, "rules": rules}]}}
    raw.update(changes)
    return EvaluationCase.model_validate(raw)


@pytest.fixture
def world(evaluated, tmp_path):
    source, result, spec = evaluated
    repo = tmp_path / "repo"
    shutil.copytree(source, repo)
    db = DuckDBConnection.get_instance(str(repo / "research.duckdb"))
    store, trader, signer = ResearchStore(db), FakeTrader(), AttestationSigner.generate()
    file_hash = "sha256:" + hashlib.sha256((repo / "strategies" / "time_of_day.py").read_bytes()).hexdigest()

    def sign(**changes):
        case = make_case(result, spec, db, file_hash, **changes)
        digest = write_evaluation_case(repo / "artifacts" / "cases", case, signer)
        store.record_case(digest, case.request_id, case.stage, NOW)
        return digest, case

    digest, case = sign()
    paper = {"value": True}
    attest = JudgmentAttest(research_db=db, store=store, trader=trader, signer=signer,
                            artifacts_root=repo / "artifacts", repo_root=repo, is_paper=lambda: paper["value"],
                            now=lambda: NOW, ruleset=holdout_ruleset())

    def judge(verdict="DEPLOY", narrative=NARRATIVE, on=None, binding=None, **view):
        on_digest, on_case = on or (digest, case)
        trader.judgments["jdg-00000001"] = judgment_view(
            "jdg-00000001", on_digest, verdict, decided_at="2026-10-08T20:30:00+00:00", narrative=narrative,
            binding=binding or {"artifact_id": on_case.artifact_id, "family_id": on_case.family_id,
                                "params": on_case.selected_params, "conids": on_case.conids,
                                "bar_size": on_case.bar_size, "strategy_file_hash": on_case.strategy_file_hash},
            **view)
    return SimpleNamespace(attest=attest, judge=judge, sign=sign, db=db, paper=paper, result=result, repo=repo,
                           spec=spec, file_hash=file_hash, trader=trader)


def reviews(world):
    return world.db.execute("SELECT reviewer, reviewer_kind, holdout_opened_once_confirmed FROM operator_reviews "
                            "WHERE artifact_id = ?", [world.result.artifact_id], fetch="all")


def call(world):
    return world.attest.attest({"judgment_id": "jdg-00000001"}, AI)


def test_no_judgment_is_refused_and_writes_no_review(world):
    assert call(world)["code"] == "JUDGMENT_MISSING" and reviews(world) == []


def test_a_deploy_judgment_becomes_one_llm_review_and_one_bundle(world):
    world.judge()
    first = call(world)
    assert first["status"] == "ATTESTED" and first["bundle_digest"].startswith("sha256:")
    assert reviews(world) == [("openrouter/jev-1#jdg-00000001", "llm", True)]
    again = call(world)
    assert (again["status"], again["bundle_digest"]) == ("DUPLICATE", first["bundle_digest"])
    assert len(reviews(world)) == 1


def test_the_reply_carries_the_binding_read_from_the_signed_bundle(world):
    world.judge()
    first = call(world)
    binding = first["binding"]
    assert set(binding) == {"strategy_path", "class_name", "file_hash", "params", "conids", "bar_size",
                            "order_notional"}
    assert (binding["strategy_path"], binding["class_name"]) == ("strategies/time_of_day.py", "TimeOfDay")
    assert binding["file_hash"] == world.file_hash and binding["params"] == dict(world.spec.params)
    assert (binding["conids"], binding["bar_size"]) == (sorted(CONIDS), "15 mins")
    assert call(world)["binding"] == binding                             # a repeat answers the same binding
    world.paper.update(value=False)
    refused = call(world)
    assert (refused["status"], refused["binding"], refused["retryable"]) == ("REFUSED", None, False)


def test_an_unreachable_trader_is_a_retryable_refusal(world, monkeypatch):
    from trader.research.trader_port import TraderUnavailable

    def down(**kwargs):
        raise TraderUnavailable("no route to the trader")
    monkeypatch.setattr(world.trader, "judgment", down)
    reply = call(world)
    assert (reply["code"], reply["retryable"]) == ("TRADER_UNAVAILABLE", True) and reviews(world) == []


@pytest.mark.parametrize("setup,code", [
    (lambda w: w.judge(verdict="SHADOW"), "JUDGMENT_NOT_DEPLOY"),
    (lambda w: w.judge(verdict="NO_VERDICT"), "JUDGMENT_NOT_DEPLOY"),
    (lambda w: (w.judge(), w.paper.update(value=False)), "ACCOUNT_NOT_PAPER"),
    (lambda w: w.judge(narrative={**NARRATIVE, "episode_dominance": " "}), "NARRATIVE_INVALID"),
    (lambda w: w.judge(narrative=None), "NARRATIVE_INVALID"),
    (lambda w: w.judge(binding={"artifact_id": "other"}), "JUDGMENT_MISMATCH"),
    (lambda w: (w.judge(), (w.repo / "strategies" / "time_of_day.py").write_text("# changed\n")),
     "STRATEGY_SOURCE_CHANGED"),
])
def test_refusals_write_no_review(world, setup, code):
    setup(world)
    assert call(world)["code"] == code and reviews(world) == []


def test_a_case_this_service_did_not_sign_is_refused(world):
    world.judge(on=("sha256:" + "e" * 64, SimpleNamespace(artifact_id=None, family_id=None, selected_params=None,
                                                          conids=[], bar_size="", strategy_file_hash="")))
    assert call(world)["code"] == "CASE_UNKNOWN" and reviews(world) == []


def test_a_case_whose_binding_differs_from_the_registry_is_a_mismatch(world):
    other = world.sign(bar_size="5 mins", request_id="sha256:" + "9" * 64)
    world.judge(on=other)
    assert call(world)["code"] == "JUDGMENT_MISMATCH" and reviews(world) == []


def test_another_review_for_the_decision_is_a_conflict(world):
    world.judge()
    submit_review(world.db, world.result.artifact_id, world.result.decision_digest, kind="human")
    assert call(world)["code"] == "REVIEW_CONFLICT"


def test_only_ai_research_attests():
    attest = JudgmentAttest(research_db=None, store=None, trader=None, signer=None, artifacts_root=None,
                            repo_root=None, is_paper=lambda: True, now=lambda: NOW)
    assert attest.attest({"judgment_id": "jdg-00000001"}, CLI)["code"] == "PRINCIPAL_FORBIDDEN"


def test_paper_posture_needs_every_signal():
    assert is_paper_posture({"TRADING_MODE": "paper", "IB_ACCOUNT": "DU123"}, {"trading_mode": "paper"})
    assert not is_paper_posture({"TRADING_MODE": "paper", "IB_ACCOUNT": "U123"}, {})
    assert not is_paper_posture({"TRADING_MODE": "live", "IB_ACCOUNT": "DU123"}, {})
    assert not is_paper_posture({"TRADING_MODE": "paper", "IB_ACCOUNT": "DU123"}, {"trading_mode": "live"})


def judge_with(world, **changes):
    world.judge(**changes)
    return call(world)


def test_the_reviewer_is_the_trader_model_and_judgment_never_the_callers_word(world):
    world.judge(jev_model="openrouter/jev-9")
    reply = world.attest.attest({"judgment_id": "jdg-00000001", "jev_model": "evil/model", "reviewer": "me"}, AI)
    assert reply["status"] == "ATTESTED"
    assert reviews(world) == [("openrouter/jev-9#jdg-00000001", "llm", True)]


def test_the_signed_bundle_holds_the_llm_review_plan_two_asks_for(world):
    world.judge()
    bundle = world.repo / "artifacts" / ("sha256_" + call(world)["bundle_digest"].removeprefix("sha256:"))
    review = json.loads((bundle / "review.json").read_text())
    model, _, judgment_id = review["reviewer"].rpartition("#")
    assert (review["reviewer_kind"], model, judgment_id) == ("llm", "openrouter/jev-1", "jdg-00000001")


@pytest.mark.parametrize("kind,verdict", [("RENEWAL", "DEPLOY"), ("INITIAL", "REJECT")])
def test_only_a_deploy_on_an_initial_case_is_attested(world, kind, verdict):
    reply = judge_with(world, kind=kind, verdict=verdict)
    assert reply["code"] == "JUDGMENT_NOT_DEPLOY" and reviews(world) == []


@pytest.mark.parametrize("bad", [None, "", "short", "jdg 0000001", 7, "x" * 97])
def test_a_malformed_judgment_id_is_refused_before_any_trader_call(world, monkeypatch, bad):
    monkeypatch.setattr(world.trader, "judgment", lambda **kwargs: pytest.fail("the trader must not be called"))
    reply = world.attest.attest({"judgment_id": bad}, AI)
    assert (reply["code"], reply["status"]) == ("REQUEST_INVALID", "REFUSED")


def test_a_non_paper_posture_is_refused_before_the_trader_is_asked(world, monkeypatch):
    world.judge()
    world.paper.update(value=False)
    monkeypatch.setattr(world.trader, "judgment", lambda **kwargs: pytest.fail("the trader must not be called"))
    assert call(world)["code"] == "ACCOUNT_NOT_PAPER" and reviews(world) == []


def test_a_trader_answer_for_another_judgment_is_refused(world, monkeypatch):
    world.judge()
    other = {**world.trader.judgments["jdg-00000001"], "judgment_id": "jdg-00000002"}
    monkeypatch.setattr(world.trader, "judgment", lambda **kwargs: other)
    assert call(world)["code"] == "JUDGMENT_MISMATCH" and reviews(world) == []


@pytest.mark.parametrize("key,value", [("artifact_id", "other"), ("family_id", "other"), ("params", {"A": 1}),
                                       ("conids", [1]), ("bar_size", "5 mins"), ("strategy_file_hash", "sha256:0")])
def test_each_bound_fact_of_the_trader_record_must_match_the_case(world, key, value):
    world.judge()
    world.trader.judgments["jdg-00000001"]["binding"][key] = value
    assert call(world)["code"] == "JUDGMENT_MISMATCH" and reviews(world) == []


@pytest.mark.parametrize("code", ["JUDGMENT_TAMPERED", "CASE_MALFORMED", "CASE_SIGNATURE_INVALID"])
def test_a_loud_trader_error_becomes_a_coded_refusal_never_a_review(world, monkeypatch, code):
    def loud(**kwargs):
        raise TypedRpcRemoteError(code, "the stored record is bad")
    monkeypatch.setattr(world.trader, "judgment", loud)
    reply = call(world)
    assert (reply["status"], reply["code"], reply["retryable"], reply["binding"]) == ("REFUSED", code, False, None)
    assert reviews(world) == []


def test_any_other_remote_error_is_raised_loudly(world, monkeypatch):
    def denied(**kwargs):
        raise TypedRpcRemoteError("PERMISSION_DENIED", "no")
    monkeypatch.setattr(world.trader, "judgment", denied)
    with pytest.raises(TypedRpcRemoteError):
        call(world)
    assert reviews(world) == []


def test_a_judgment_without_a_valid_model_is_refused(world):
    world.judge()
    world.trader.judgments["jdg-00000001"]["body"]["jev_model"] = "bad model#1"
    assert call(world)["code"] == "JUDGMENT_INVALID" and reviews(world) == []


def test_a_signer_loaded_from_a_key_file_attests(world, tmp_path):
    from trader.research.signing import generate_private_key_pem
    key_path = tmp_path / "research_signing.pem"
    key_path.write_bytes(generate_private_key_pem())
    key_path.chmod(0o600)
    loaded = AttestationSigner.from_key_file(str(key_path))
    # the case must be signed by the key the service loads, so sign a fresh one with it
    other = JudgmentAttest(research_db=world.db, store=world.attest._store, trader=world.trader, signer=loaded,
                           artifacts_root=world.repo / "artifacts", repo_root=world.repo, is_paper=lambda: True,
                           now=lambda: NOW, ruleset=holdout_ruleset())
    case = make_case(world.result, world.spec, world.db, world.file_hash, request_id="sha256:" + "5" * 64)
    digest = write_evaluation_case(world.repo / "artifacts" / "cases", case, loaded)
    world.attest._store.record_case(digest, case.request_id, case.stage, NOW)
    world.judge(on=(digest, case))
    assert other.attest({"judgment_id": "jdg-00000001"}, AI)["status"] == "ATTESTED"


def test_this_service_never_generates_a_signing_key():
    import trader.research.judgment_attest as module
    source = open(module.__file__).read()
    assert "generate" not in source


@pytest.mark.parametrize("code", ["JUDGMENT_TAMPERED", "VERSION_TAMPERED", "CASE_NOT_FOUND", "COOLDOWN_CALENDAR_UNAVAILABLE",
                                  "DEPLOYMENT_CALENDAR_UNAVAILABLE", "PERMISSION_DENIED", "VALIDATION_ERROR",
                                  "JUDGMENT_MISSING", ""])
def test_the_loud_code_rule_matches_the_trader_rule(code):
    from trader.automation.ai_paper_actions import is_loud_refusal
    assert is_loud_trader_code(code) is is_loud_refusal(code)


def test_a_failed_export_is_retryable_and_the_retry_answers_with_the_real_binding(world, monkeypatch):
    world.judge()
    real_export = ResearchBundle.export
    calls = []

    def flaky(self, artifact_id, path):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("disk full")
        return real_export(self, artifact_id, path)
    monkeypatch.setattr(ResearchBundle, "export", flaky)
    first = call(world)
    assert (first["status"], first["code"], first["retryable"], first["binding"]) == (
        "REFUSED", "ATTEST_EXPORT_FAILED", True, None)
    assert len(reviews(world)) == 1 and not calls[0].exists()          # the half-built export is cleaned up
    again = call(world)
    assert (again["status"], again["retryable"]) == ("DUPLICATE", False)
    assert again["binding"]["file_hash"] == world.file_hash and again["bundle_digest"].startswith("sha256:")


def test_a_retired_artifact_is_refused_before_any_review_is_written(world):
    world.judge()
    world.db.execute("UPDATE strategy_artifacts SET state = 'RETIRED' WHERE artifact_id = ?",
                     [world.result.artifact_id])
    reply = call(world)
    assert (reply["code"], reply["retryable"]) == ("ARTIFACT_NOT_ATTESTABLE", False) and reviews(world) == []


def test_an_expired_stored_attestation_is_refused_before_any_review(world):
    world.judge()
    assert call(world)["status"] == "ATTESTED"
    world.db.execute("DELETE FROM operator_reviews WHERE artifact_id = ?", [world.result.artifact_id])
    world.attest._now = lambda: NOW + ATTESTATION_LIFETIME + dt.timedelta(seconds=1)
    assert call(world)["code"] == "ATTESTATION_EXPIRED" and reviews(world) == []


def test_a_missing_strategy_file_is_a_source_change(world):
    world.judge()
    (world.repo / "strategies" / "time_of_day.py").unlink()
    assert call(world)["code"] == "STRATEGY_SOURCE_CHANGED" and reviews(world) == []


def test_two_different_reviews_for_one_decision_cannot_both_be_recorded(world):
    world.judge()
    first = world.attest._review(*review_inputs(world))
    other = replace(first, reviewer="openrouter/jev-2#jdg-00000002")
    outcomes = []

    def record(review):
        try:
            outcomes.append(OperatorReviewRepository(world.db).record_as_only_review(review))
        except ReviewConflict:
            outcomes.append("conflict")
    threads = [threading.Thread(target=record, args=(r,)) for r in (first, other)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(map(str, outcomes)) == ["True", "conflict"] and len(reviews(world)) == 1


def review_inputs(world):
    judgment = world.trader.judgments["jdg-00000001"]
    case = load_verified_case(world.repo / "artifacts" / "cases", judgment["case_digest"],
                              {world.attest._signer.public_key_id: world.attest._signer.public_key})
    artifact = ExperimentRegistry(world.db).get_artifact(case.artifact_id)
    return case, judgment, artifact


def test_positive_paper_posture_without_a_yaml_trading_mode():
    assert is_paper_posture({"TRADING_MODE": "paper", "IB_ACCOUNT": "DU1"}, {})
