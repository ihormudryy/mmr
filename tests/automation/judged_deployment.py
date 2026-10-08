"""Tests only: judgment facts and cooldowns for stacks that do not run the research chain."""
from __future__ import annotations

from trader.automation.ai_judgment_port import JudgmentFacts


class SeededJudgments:
    def __init__(self, inner=None):
        self._facts, self._renewals, self._inner = {}, {}, inner

    def seed(self, facts: JudgmentFacts) -> None:
        self._facts[facts.judgment_id] = facts

    def end_line(self, version_digest: str, verdict: str = "SHADOW") -> None:
        self._renewals[version_digest] = self._renewals.get(version_digest, ()) + (verdict,)

    def get(self, judgment_id):
        found = self._facts.get(judgment_id)
        return found if found is not None or self._inner is None else self._inner.get(judgment_id)

    def renewal_verdicts(self, version_digest):
        inner = () if self._inner is None else self._inner.renewal_verdicts(version_digest)
        return self._renewals.get(version_digest, ()) + tuple(inner)


class Cooldowns:
    def __init__(self):
        self.keys: set[str] = set()

    def cooling_down(self, key, now):
        return key in self.keys

    def cooling_down_in_tx(self, conn, key, now):
        return key in self.keys


def deploy_facts(record: dict, judgment_id: str, *, verdict="DEPLOY", kind="INITIAL", renews=None,
                 model_id="jev-model", artifact_id="art-1", family_id="fam-1") -> JudgmentFacts:
    initial = kind == "INITIAL"
    return JudgmentFacts(judgment_id, verdict, kind, renews, model_id, record["strategy_path"],
                         record["class_name"], record["strategy_digest"], dict(record["params"]),
                         tuple(sorted(record["conids"])), record["bar_size"],
                         artifact_id if initial else None, family_id if initial else None)


def install_seeded_judgments(monkeypatch) -> SeededJudgments:
    """Before a stack is built: the trader's judgment reader answers seeded DEPLOY judgments only."""
    import trader.automation.ai_judgment_port as port
    seeded = SeededJudgments()
    monkeypatch.setattr(port, "judgment_reader_for", lambda judgments, **paths: seeded)
    return seeded


def seed_judged_deployment(services, judgments: SeededJudgments, record: dict, *, today, sessions: int = 20,
                           judgment_id: str = "jdg-seed-00000001") -> tuple[str, str]:
    """A base deployment and an ACTIVE version from ``today``, without the bundle path."""
    import datetime as dt
    import hashlib
    from trader.automation.ai_deployment_versions import INITIAL, DeploymentVersion
    from trader.automation.ai_deployments import AiDeployment, deployment_digest
    deployment = AiDeployment.from_json(record)
    judgments.seed(deploy_facts(record, judgment_id))
    version = DeploymentVersion(deployment_digest(deployment), judgment_id, INITIAL, None, today,
                                today + dt.timedelta(days=sessions * 2))
    request = "sha256:" + hashlib.sha256(judgment_id.encode()).hexdigest()

    def write(conn):
        services.deployments.register_in_tx(conn, deployment, principal="ai_research",
                                            command_id=f"seed-{judgment_id}")
        return services.versions.seal_in_tx(conn, version, request_digest=request, principal="ai_research",
                                            command_id=f"seed-{judgment_id}")[0]
    return deployment_digest(deployment), services.versions._db.transaction(write)
