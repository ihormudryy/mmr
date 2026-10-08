"""SP2c Plan 2 Task 5: registration bound to a DEPLOY judgment (spec 5.2 items 4-5, 9)."""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.automation.judged_deployment import Cooldowns, SeededJudgments, deploy_facts
from trader.automation.ai_bundle_check import BundleFacts, BundleRefused
from trader.automation.ai_deployment_activity import SUPERSEDED, DeploymentActivity
from trader.automation.ai_deployment_registration import AiDeploymentRegistrar, registration_command_id
from trader.automation.ai_deployment_versions import AiDeploymentVersionStore, apply_ai_deployment_version_migrations
from trader.automation.ai_deployments import (
    AiDeployment, AiDeploymentStore, DeploymentRefused, apply_ai_deployment_migration,
)
from trader.automation.ai_paper_actions import AiPaperActions
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_coordinator import CommandRequest, CommandValidationError

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 9, 21, 0, tzinfo=UTC)


def record(n=0, **changes):
    return {"strategy_path": "strategies/opening_range_breakout.py", "strategy_digest": "sha256:" + "a" * 64,
            "class_name": "OpeningRangeBreakout", "params": {"RANGE_MINUTES": 15}, "conids": [265598 + n, 272093],
            "bar_size": "1 min", "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
            "evidence_ref": f"sha256:{n + 1:064x}", "evidence_order_notional": 2000.0, **changes}


class FakeBundles:
    def __init__(self):
        self.facts: dict[str, BundleFacts] = {}

    def manifest_artifact_id(self, digest):
        return self.facts[digest].artifact_id

    def check(self, digest, *, artifact_id, now):
        facts = self.facts.get(digest)
        if facts is None:
            raise BundleRefused("BUNDLE_MISSING", digest)
        if now >= facts.expires_at:
            raise BundleRefused("BUNDLE_EXPIRED", digest)
        return facts


class Env:
    def __init__(self, tmp_path, max_active=3):
        self.now = NOW
        self.db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
        migrator = SchemaMigrator(self.db)
        apply_ai_deployment_migration(migrator)
        apply_ai_deployment_version_migrations(migrator)
        journal = DomainJournal(self.db)
        journal.migrate(migrator)
        clock = lambda: self.now
        self.deployments = AiDeploymentStore(self.db, now=clock)
        self.versions = AiDeploymentVersionStore(journal, now=clock)
        self.judgments, self.cooldowns, self.bundles = SeededJudgments(), Cooldowns(), FakeBundles()
        self.activity = DeploymentActivity(versions=self.versions, deployments=self.deployments,
                                           judgments=self.judgments, cooldowns=self.cooldowns,
                                           max_active=max_active, now=clock)
        self.registrar = AiDeploymentRegistrar(
            db=self.db, deployments=self.deployments, versions=self.versions, activity=self.activity,
            judgments=self.judgments, cooldowns=self.cooldowns, bundles=self.bundles,
            calendar=XNYSCalendarPolicy(), expiry_sessions=20, now=clock)

    def judge(self, judgment_id, rec, *, initial=None, expires_at=NOW + dt.timedelta(days=90), **kw):
        self.judgments.seed(deploy_facts(rec, judgment_id, **kw))
        self.bundles.facts[rec["evidence_ref"]] = BundleFacts(
            rec["evidence_ref"], "art-1", "fam-1", rec["strategy_path"], rec["class_name"], rec["strategy_digest"],
            rec["params"], tuple(sorted(rec["conids"])), rec["bar_size"], rec["evidence_order_notional"],
            f"jev-model#{initial or judgment_id}", "llm", expires_at)

    def register(self, judgment_id, rec):
        body = {"judgment_id": judgment_id, "bundle_digest": rec["evidence_ref"],
                "deployment": AiDeployment.from_json(rec).to_json()}
        return self.registrar.register(body, principal="ai_research",
                                       command_id=registration_command_id(body, self.now.date()))

    def refused(self, judgment_id, rec) -> str:
        with pytest.raises((DeploymentRefused, BundleRefused)) as refused:
            self.register(judgment_id, rec)
        return refused.value.code


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_registration_refusals_bind_the_judgment(env):
    assert env.refused("jdg-none", record()) == "JUDGMENT_MISSING"
    env.judge("jdg-rej", record(), verdict="REJECT")
    assert env.refused("jdg-rej", record()) == "JUDGMENT_NOT_DEPLOY"
    env.judge("jdg-1", record())
    for change in ({"params": {"RANGE_MINUTES": 16}}, {"conids": [265598, 999999]}, {"bar_size": "5 mins"},
                   {"strategy_digest": "sha256:" + "f" * 64}, {"evidence_order_notional": 1000.0}):
        assert env.refused("jdg-1", record(**change)) == "JUDGMENT_MISMATCH"
    env.judgments.seed(deploy_facts(record(), "jdg-1", artifact_id="art-other"))    # another evaluation
    assert env.refused("jdg-1", record()) == "JUDGMENT_MISMATCH"
    env.judge("jdg-2", record(), model_id="jev-model", initial="jdg-someone-else")   # review names another
    assert env.refused("jdg-2", record()) == "JUDGMENT_MISMATCH"


def test_a_bundle_reviewed_by_another_model_is_refused(env):
    """Ruling 4: the review is exactly <the judgment's Jev model>#<initial judgment id>, not any model."""
    env.judge("jdg-1", record(), model_id="other-model")         # the bundle review says jev-model#jdg-1
    with pytest.raises(DeploymentRefused) as refused:
        env.register("jdg-1", record())
    assert refused.value.code == "JUDGMENT_MISMATCH"
    assert "'jev-model#jdg-1'" in refused.value.message and "'other-model#jdg-1'" in refused.value.message
    assert env.versions.sealed() == ()
    assert env.db.execute("SELECT COUNT(*) FROM ai_deployments", fetch="one") == (0,)


def test_exact_retry_returns_the_same_versions(env):
    env.judge("jdg-1", record())
    first = env.register("jdg-1", record())
    assert (first["first_session"], first["expiry_session"], first["created"]) == ("2026-10-12", "2026-11-06", True)
    env.now = NOW + dt.timedelta(days=3)                                             # a later day: still the same
    again = env.register("jdg-1", record())
    assert (again["digest"], again["version_digest"], again["created"]) == (first["digest"], first["version_digest"],
                                                                             False)


def test_another_body_for_a_bound_judgment_is_refused(env):
    env.judge("jdg-1", record())
    env.register("jdg-1", record())
    env.bundles.facts[record(5)["evidence_ref"]] = replace(env.bundles.facts[record()["evidence_ref"]],
                                                           bundle_digest=record(5)["evidence_ref"])
    assert env.refused("jdg-1", record(evidence_ref=record(5)["evidence_ref"])) == "JUDGMENT_ALREADY_BOUND"


def test_cooldown_and_cap(env):
    env.cooldowns.keys.add("strategies/opening_range_breakout.py:OpeningRangeBreakout")
    env.judge("jdg-1", record())
    assert env.refused("jdg-1", record()) == "FAMILY_COOLING_DOWN"
    env.cooldowns.keys.clear()
    for n in range(3):
        env.judge(f"jdg-c{n}", record(n))
        env.register(f"jdg-c{n}", record(n))
    env.judge("jdg-c3", record(3))
    assert env.refused("jdg-c3", record(3)) == "DEPLOY_CAP_REACHED"


class RejectLandsWhileWaitingForTheLock:
    """A journal whose ``transaction`` first records a REJECT: it committed after the early cooldown read."""

    def __init__(self, db, strategy_key, until):
        self._db, self._strategy_key, self._until = db, strategy_key, until

    def transaction(self, fn):
        from tests.automation.backtest_judge_fixtures import insert_reject
        insert_reject(self._db, self._strategy_key, self._until)
        return self._db.transaction(fn)


def test_a_reject_recorded_before_the_registration_transaction_refuses_it(env):
    from trader.automation.ai_judgment_port import Plan1Cooldowns
    from trader.automation.backtest_judge_schema import apply_backtest_judge_migrations
    apply_backtest_judge_migrations(SchemaMigrator(env.db))
    key = "strategies/opening_range_breakout.py:OpeningRangeBreakout"
    env.registrar._cooldowns = Plan1Cooldowns(env.db)
    env.registrar._db = RejectLandsWhileWaitingForTheLock(env.db, key, dt.date(2026, 10, 22))
    env.judge("jdg-1", record())
    assert env.refused("jdg-1", record()) == "FAMILY_COOLING_DOWN"
    assert env.versions.sealed() == ()
    assert env.db.execute("SELECT COUNT(*) FROM ai_deployments", fetch="one") == (0,)


def test_two_concurrent_registrations_for_the_last_slot(tmp_path):
    env = Env(tmp_path, max_active=1)
    for n in (0, 1):
        env.judge(f"jdg-{n}", record(n))

    def attempt(n):
        try:
            return env.register(f"jdg-{n}", record(n))["version_digest"]
        except DeploymentRefused as ex:
            return ex.code
    with ThreadPoolExecutor(2) as pool:
        results = sorted(pool.map(attempt, (0, 1)))
    assert results.count("DEPLOY_CAP_REACHED") == 1 and len(env.versions.sealed()) == 1


def renew(env, prior_version, judgment_id="jdg-r1", rec=None):
    rec = rec or record()
    env.judgments.seed(deploy_facts(rec, judgment_id, kind="RENEWAL", renews=prior_version))
    return env.register(judgment_id, rec)


def test_renewal_gets_a_fresh_version_and_supersedes(env):
    env.judge("jdg-1", record())
    initial = env.register("jdg-1", record())
    env.now = dt.datetime(2026, 11, 6, 21, 0, tzinfo=UTC)                           # evening of the last session
    renewal = renew(env, initial["version_digest"])
    assert renewal["digest"] == initial["digest"] and renewal["version_digest"] != initial["version_digest"]
    assert (renewal["kind"], renewal["first_session"]) == ("RENEWAL", "2026-11-09")
    assert env.activity.status(initial["version_digest"]) == SUPERSEDED


def test_the_same_renewal_twice_returns_the_same_version(env):
    env.judge("jdg-1", record())
    initial = env.register("jdg-1", record())
    first = renew(env, initial["version_digest"])
    again = renew(env, initial["version_digest"])
    assert (again["version_digest"], first["created"], again["created"]) == (first["version_digest"], True, False)


def test_renewal_is_refused_after_the_bundle_expires(env):
    env.judge("jdg-1", record(), expires_at=dt.datetime(2026, 11, 20, tzinfo=UTC))
    initial = env.register("jdg-1", record())
    env.now = dt.datetime(2026, 11, 20, 21, 0, tzinfo=UTC)
    with pytest.raises(BundleRefused) as refused:
        renew(env, initial["version_digest"])
    assert refused.value.code == "BUNDLE_EXPIRED"


def test_a_withdrawn_prior_cannot_be_renewed(env):
    env.judge("jdg-1", record())
    initial = env.register("jdg-1", record())
    env.versions.withdraw(initial["version_digest"], reason="x", principal="cli", command_id="w")
    with pytest.raises(DeploymentRefused) as refused:
        renew(env, initial["version_digest"])
    assert refused.value.code == "RENEWAL_PRIOR_INVALID"


def test_a_renewal_names_a_sealed_prior_with_the_same_base(env):
    env.judge("jdg-1", record())
    initial = env.register("jdg-1", record())
    unknown = "sha256:" + "9" * 64
    assert _refusal(lambda: renew(env, unknown, "jdg-r1")) == "RENEWAL_PRIOR_INVALID"
    other_base = record(params={"RANGE_MINUTES": 30})
    with pytest.raises(DeploymentRefused) as refused:
        renew(env, initial["version_digest"], "jdg-r2", other_base)
    assert refused.value.code == "RENEWAL_PRIOR_INVALID"
    assert len(env.versions.sealed()) == 1                                           # nothing half written


def test_a_second_renewal_of_a_renewed_prior_is_refused_before_the_cap(env):
    env.judge("jdg-1", record())
    initial = env.register("jdg-1", record())
    renew(env, initial["version_digest"], "jdg-r1")
    for n in (1, 2):
        env.judge(f"jdg-f{n}", record(n))
        env.register(f"jdg-f{n}", record(n))                                         # the cap of 3 is full
    assert _refusal(lambda: renew(env, initial["version_digest"], "jdg-r2")) == "RENEWAL_PRIOR_INVALID"


def test_an_exact_retry_at_a_full_cap_returns_the_existing_version(env, monkeypatch):
    env.judge("jdg-1", record())
    first = env.register("jdg-1", record())
    for n in (1, 2):
        env.judge(f"jdg-f{n}", record(n))
        env.register(f"jdg-f{n}", record(n))
    monkeypatch.setattr(env.versions, "bound_to_judgment", lambda judgment_id: None)   # as if another request raced
    again = env.register("jdg-1", record())
    assert (again["version_digest"], again["digest"], again["created"]) == (first["version_digest"],
                                                                             first["digest"], False)
    assert len(env.versions.sealed()) == 3
    other_body = record(evidence_ref=record(5)["evidence_ref"])
    env.bundles.facts[other_body["evidence_ref"]] = replace(env.bundles.facts[record()["evidence_ref"]],
                                                            bundle_digest=other_body["evidence_ref"])
    assert env.refused("jdg-1", other_body) == "JUDGMENT_ALREADY_BOUND"


def test_the_bundle_digest_is_checked_by_shape(env):
    body = registration_body(env, "jdg-1", record())
    for bad in ("sha256:" + "a" * 64 + "\n", "sha256:" + "A" * 64, 5, "x"):
        with pytest.raises(DeploymentRefused) as refused:
            env.registrar.register({**body, "bundle_digest": bad}, principal="ai_research", command_id="c")
        assert refused.value.code == "DEPLOYMENT_INVALID"


def test_a_tampered_version_is_not_reported_as_not_found(env):
    env.judge("jdg-1", record())
    version = env.register("jdg-1", record())["version_digest"]
    env.db.execute("UPDATE ai_deployment_versions SET record_json = ? WHERE digest = ?", ["{}", version])
    with pytest.raises(DeploymentRefused) as refused:
        actions_for(env).version_view(version)
    assert refused.value.code == "DEPLOYMENT_VERSION_TAMPERED"


def _refusal(call) -> str:
    with pytest.raises((DeploymentRefused, BundleRefused)) as refused:
        call()
    return refused.value.code


class BrokenCalendar:
    def sessions_in_range(self, start, end):
        raise ValueError("calendar out of range")


def test_a_calendar_that_cannot_serve_is_a_coded_refusal(env):
    env.judge("jdg-1", record())
    env.registrar._calendar = BrokenCalendar()
    assert env.refused("jdg-1", record()) == "DEPLOYMENT_CALENDAR_UNAVAILABLE"
    assert env.versions.sealed() == ()


def test_a_bundle_that_expires_before_the_first_session_is_refused(env):
    env.judge("jdg-1", record(), expires_at=NOW + dt.timedelta(hours=12))            # NY day 10-10, first session 10-12
    assert env.refused("jdg-1", record()) == "BUNDLE_EXPIRED"


# -- AiPaperActions: withdraw, views, register mapping ------------------------------------------


class Config:
    def __init__(self, enabled=True):
        self.enabled = enabled

    def style_enabled(self, style):
        return self.enabled


def actions_for(env, *, mode="paper", account="DU123", enabled=True):
    return AiPaperActions(policy=None, deployments=env.deployments, broker=None, config=Config(enabled),
                          account_id=account, account_mode=mode, ledger=None, journal=None, controls=None,
                          now=lambda: env.now, registrar=env.registrar, versions=env.versions,
                          activity=env.activity)


def command(principal, body, command_id="c-1"):
    return SimpleNamespace(principal=principal, body=body, command_id=command_id, account_id="DU123")


def registration_body(env, judgment_id, rec):
    return {"judgment_id": judgment_id, "bundle_digest": rec["evidence_ref"],
            "deployment": AiDeployment.from_json(rec).to_json()}


def test_actions_register_maps_refusals_to_validation_errors(env):
    actions = actions_for(env)
    body = registration_body(env, "jdg-none", record())
    with pytest.raises(CommandValidationError) as refused:
        actions.register(command("ai_research", body, actions.registration_command_id(body)))
    assert refused.value.code == "JUDGMENT_MISSING"
    env.judge("jdg-1", record())
    body = registration_body(env, "jdg-1", record())
    reply = actions.register(command("ai_research", body, actions.registration_command_id(body)))
    assert reply["created"] is True and reply["kind"] == "INITIAL"
    for wrong in (command("cli", body), command("strategy", body)):
        with pytest.raises(CommandValidationError) as refused:
            actions.register(wrong)
        assert refused.value.code == "PRINCIPAL_FORBIDDEN"
    with pytest.raises(CommandValidationError) as refused:
        actions_for(env, mode="live", account="U123").register(command("ai_research", body))
    assert refused.value.code == "ACCOUNT_NOT_PAPER"


def test_actions_withdraw_and_views(env):
    env.judge("jdg-1", record())
    body = registration_body(env, "jdg-1", record())
    actions = actions_for(env)
    version = actions.register(command("ai_research", body, actions.registration_command_id(body)))["version_digest"]
    env.now = dt.datetime(2026, 10, 12, 15, 0, tzinfo=UTC)                           # the first session
    seen = actions.active_view()
    assert seen["account_mode"] == "paper"
    assert [d["version_digest"] for d in seen["deployments"]] == [version]
    assert actions.version_view(version)["version"]["state"] == "ACTIVE"
    assert actions.version_view("sha256:" + "9" * 64) == {"found": False, "version": None}
    withdraw = {"version_digest": version, "reason": "stop"}
    for wrong in ("ai_research", "ai_supervisor", "strategy"):
        with pytest.raises(CommandValidationError) as refused:
            actions.withdraw(command(wrong, withdraw))
        assert refused.value.code == "PRINCIPAL_FORBIDDEN"
    assert actions.withdraw(command("cli", withdraw)) == {"version_digest": version, "withdrawn": True,
                                                         "already_withdrawn": False}
    assert actions.withdraw(command("dashboard", withdraw))["already_withdrawn"] is True
    assert actions.active_view()["deployments"] == []
    assert actions.version_view(version)["version"]["state"] == "WITHDRAWN"
    with pytest.raises(CommandValidationError) as refused:
        actions.withdraw(command("cli", {"version_digest": "sha256:" + "9" * 64, "reason": "x"}))
    assert refused.value.code == "DEPLOYMENT_VERSION_UNKNOWN"


@pytest.fixture(scope="module")
def signed_bundle(tmp_path_factory):
    from tests.automation.judged_bundle import export_judged_bundle
    repo = tmp_path_factory.mktemp("chain")
    return export_judged_bundle(repo, str(repo / "market.duckdb"), judgment_id="jdg-chain", model_id="openrouter/jev")


def real_bundle_env(tmp_path, bundle, monkeypatch, artifacts_root=None):
    """An Env whose registrar verifies real signed bundles under ``artifacts_root``; the record and the judgment
    are built from the untouched bundle."""
    from tests.research.evaluation_fixtures import FIXED_NOW, judge_qualified_evidence_by_holdout_ruleset
    from trader.automation.ai_bundle_check import ResearchBundleCheck
    judge_qualified_evidence_by_holdout_ruleset(monkeypatch)
    env = Env(tmp_path)
    env.now = FIXED_NOW + dt.timedelta(days=1)
    facts = ResearchBundleCheck(artifacts_root=bundle.bundle_path.parent, verify_dir=bundle.verify_dir).check(
        bundle.bundle_digest, artifact_id=bundle.artifact_id, now=env.now)
    env.registrar._bundles = ResearchBundleCheck(artifacts_root=artifacts_root or bundle.bundle_path.parent,
                                                 verify_dir=bundle.verify_dir)
    rec = {"strategy_path": facts.strategy_path, "strategy_digest": facts.file_hash, "class_name": facts.class_name,
           "params": dict(facts.params), "conids": list(facts.conids), "bar_size": facts.bar_size,
           "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
           "evidence_ref": bundle.bundle_digest, "evidence_order_notional": facts.order_notional}
    env.judgments.seed(deploy_facts(rec, "jdg-chain", model_id="openrouter/jev", artifact_id=bundle.artifact_id,
                                    family_id=facts.family_id))
    return env, rec


@pytest.mark.timeout(240)
def test_registration_through_a_real_signed_bundle(tmp_path, signed_bundle, monkeypatch):
    env, rec = real_bundle_env(tmp_path, signed_bundle, monkeypatch)
    assert env.register("jdg-chain", rec)["created"] is True
    assert env.refused("jdg-chain", {**rec, "params": {**rec["params"], "ENTRY_MINUTE": 615}}) == \
        "JUDGMENT_ALREADY_BOUND"


def _edit_bundle_file(path, edit):
    import json
    import os
    os.chmod(path, 0o644)
    document = json.loads(path.read_text())
    edit(document)
    path.write_text(json.dumps(document))
    os.chmod(path, 0o444)


def _set_conids(attestation):
    attestation["permitted_instruments"] = [*attestation["permitted_instruments"][:-1], "999999"]


BUNDLE_TAMPERING = {                       # spec 9: changed parameters, conids, bar size or strategy file
    "params": ("artifact.json", lambda a: a["selected_parameters"].update(ENTRY_MINUTE=601)),
    "conids": ("attestation.json", _set_conids),
    "bar_size": ("family.json", lambda f: f["validation_protocol"].update(bar_size="5 mins")),
    "strategy_path": ("family.json", lambda f: f.update(strategy_path="strategies/other.py")),
    "strategy_source": ("attestation.json", lambda a: a.update(source_digest="0" * 64)),
}


@pytest.mark.timeout(240)
@pytest.mark.parametrize("tampered", sorted(BUNDLE_TAMPERING))
def test_a_tampered_bundle_registers_nothing(tmp_path, signed_bundle, monkeypatch, tampered):
    import shutil
    root = tmp_path / "artifacts"
    copy = root / signed_bundle.bundle_path.name
    shutil.copytree(signed_bundle.bundle_path, copy)
    env, rec = real_bundle_env(tmp_path, signed_bundle, monkeypatch, artifacts_root=root)
    file_name, edit = BUNDLE_TAMPERING[tampered]
    _edit_bundle_file(copy / file_name, edit)
    assert env.refused("jdg-chain", rec) == "BUNDLE_INVALID"
    assert env.versions.sealed() == ()
    assert env.db.execute("SELECT COUNT(*) FROM ai_deployments", fetch="one") == (0,)
