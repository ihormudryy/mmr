"""SP2c Plan 2 Task 3: sessions, status, cap and the entry refusal (spec 5.2 items 5 and 8)."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.automation.judged_deployment import Cooldowns, SeededJudgments, deploy_facts
from trader.automation.ai_deployment_activity import (
    ACTIVE, EXPIRED, JUDGMENT_ENDED, NOT_STARTED, OVER_CAP, SUPERSEDED, WITHDRAWN, DeploymentActivity,
    deployment_sessions, deployment_version_gate,
)
from trader.automation.ai_deployment_versions import (
    INITIAL, RENEWAL, AiDeploymentVersionStore, DeploymentVersion, apply_ai_deployment_version_migrations,
)
from trader.automation.ai_deployments import (
    AiDeployment, AiDeploymentStore, DeploymentRefused, apply_ai_deployment_migration,
)
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

UTC = dt.timezone.utc
FRIDAY_EVENING = dt.datetime(2026, 10, 9, 21, 0, tzinfo=UTC)
RECORD = {"strategy_path": "strategies/opening_range_breakout.py", "strategy_digest": "sha256:" + "a" * 64,
          "class_name": "OpeningRangeBreakout", "params": {"RANGE_MINUTES": 15}, "conids": [265598, 272093],
          "bar_size": "1 min", "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
          "evidence_ref": "sha256:" + "b" * 64, "evidence_order_notional": 2000.0}


def test_sessions_start_after_registration_and_end_inclusive():
    first, expiry = deployment_sessions(XNYSCalendarPolicy(), registered_at=FRIDAY_EVENING, sessions=20,
                                        bundle_expires_at=FRIDAY_EVENING + dt.timedelta(days=90))
    assert (first, expiry) == (dt.date(2026, 10, 12), dt.date(2026, 11, 6))


def test_expiry_is_capped_by_the_bundle():
    _, expiry = deployment_sessions(XNYSCalendarPolicy(), registered_at=FRIDAY_EVENING, sessions=20,
                                    bundle_expires_at=dt.datetime(2026, 10, 20, 12, tzinfo=UTC))
    assert expiry == dt.date(2026, 10, 19)
    with pytest.raises(DeploymentRefused) as refused:
        deployment_sessions(XNYSCalendarPolicy(), registered_at=FRIDAY_EVENING, sessions=20,
                            bundle_expires_at=dt.datetime(2026, 10, 12, 12, tzinfo=UTC))
    assert refused.value.code == "BUNDLE_EXPIRED"


class World:
    def __init__(self, tmp_path, max_active=3):
        self.now = dt.datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
        db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
        migrator = SchemaMigrator(db)
        apply_ai_deployment_migration(migrator)
        apply_ai_deployment_version_migrations(migrator)
        journal = DomainJournal(db)
        journal.migrate(migrator)
        self.db, self.judgments, self.cooldowns = db, SeededJudgments(), Cooldowns()
        self.deployments = AiDeploymentStore(db, now=lambda: self.now)
        self.versions = AiDeploymentVersionStore(journal, now=lambda: self.now)
        self.activity = DeploymentActivity(versions=self.versions, deployments=self.deployments,
                                           judgments=self.judgments, cooldowns=self.cooldowns,
                                           max_active=max_active, now=lambda: self.now)

    def seal(self, n, *, first=dt.date(2026, 10, 12), expiry=dt.date(2026, 11, 6), kind=INITIAL, prior=None):
        record = {**RECORD, "conids": [265598 + n]}
        self.judgments.seed(deploy_facts(record, f"jdg-{n}", kind=kind, renews=prior))
        deployment = AiDeployment.from_json(record)
        version = lambda base: DeploymentVersion(base, f"jdg-{n}", kind, prior, first, expiry)

        def write(conn):
            base, _ = self.deployments.register_in_tx(conn, deployment, principal="ai_research", command_id="c")
            return self.versions.seal_in_tx(conn, version(base), request_digest=f"sha256:{n:064x}",
                                            principal="ai_research", command_id="c")[0]
        return self.db.transaction(write), record


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_status_order(world):
    withdrawn, _ = world.seal(1)
    world.versions.withdraw(withdrawn, reason="x", principal="cli", command_id="w")
    renewed, _ = world.seal(2)
    world.seal(3, kind=RENEWAL, prior=renewed, first=dt.date(2026, 10, 15))
    ended, _ = world.seal(4)
    world.judgments.end_line(ended, "NO_VERDICT")
    old, _ = world.seal(5, first=dt.date(2026, 9, 1), expiry=dt.date(2026, 10, 13))
    statuses = world.activity.statuses()
    assert [statuses[d] for d in (withdrawn, renewed, ended, old)] == [WITHDRAWN, SUPERSEDED, JUDGMENT_ENDED,
                                                                       EXPIRED]


def test_status_order_when_several_reasons_apply(world):
    past = {"first": dt.date(2026, 9, 1), "expiry": dt.date(2026, 10, 13)}
    renewed_and_expired, _ = world.seal(1, **past)
    world.seal(2, kind=RENEWAL, prior=renewed_and_expired, first=dt.date(2026, 10, 14))
    withdrawn_and_renewed, _ = world.seal(3)
    world.seal(4, kind=RENEWAL, prior=withdrawn_and_renewed, first=dt.date(2026, 10, 14))
    world.versions.withdraw(withdrawn_and_renewed, reason="x", principal="cli", command_id="w")
    ended_and_expired, _ = world.seal(5, **past)
    world.judgments.end_line(ended_and_expired, "NO_VERDICT")
    statuses = world.activity.statuses()
    assert [statuses[d] for d in (renewed_and_expired, withdrawn_and_renewed, ended_and_expired)] == [
        SUPERSEDED, WITHDRAWN, JUDGMENT_ENDED]


def test_expiry_session_is_inclusive_and_not_started_waits(world):
    today, _ = world.seal(1, expiry=dt.date(2026, 10, 14))
    later, _ = world.seal(2, first=dt.date(2026, 10, 15))
    assert world.activity.statuses() == {today: ACTIVE, later: NOT_STARTED}


def test_a_lowered_cap_keeps_the_oldest(tmp_path):
    world = World(tmp_path, max_active=1)
    older, _ = world.seal(1, first=dt.date(2026, 10, 12))
    newer, _ = world.seal(2, first=dt.date(2026, 10, 13))
    assert world.activity.statuses() == {older: ACTIVE, newer: OVER_CAP}
    assert [a.version_digest for a in world.activity.active()] == [older]


def test_entry_refusal_codes(world):
    digest, record = world.seal(1)
    base = world.versions.get(digest).base_digest
    refuse = lambda **kw: world.activity.entry_refusal(**{"deployment_digest": base, "version_digest": digest,
                                                          "source_digest": record["strategy_digest"], **kw})
    assert refuse() is None
    assert refuse(version_digest=None) == "DEPLOYMENT_VERSION_REQUIRED"
    assert refuse(deployment_digest="sha256:" + "e" * 64) == "DEPLOYMENT_NOT_ACTIVE"
    assert refuse(version_digest="sha256:" + "e" * 64) == "DEPLOYMENT_NOT_ACTIVE"
    assert refuse(source_digest="sha256:" + "f" * 64) == "STRATEGY_SOURCE_MISMATCH"
    world.cooldowns.keys.add("strategies/opening_range_breakout.py:OpeningRangeBreakout")
    assert refuse() == "FAMILY_COOLING_DOWN"
    world.cooldowns.keys.clear()
    world.now = dt.datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
    assert refuse() == "DEPLOYMENT_EXPIRED"


def test_a_source_digest_that_is_not_ascii_text_is_a_mismatch(world):
    digest, _ = world.seal(1)
    base = world.versions.get(digest).base_digest
    for claimed in (b"sha256:aa", 7, "sha256:é"):
        assert world.activity.entry_refusal(deployment_digest=base, version_digest=digest,
                                            source_digest=claimed) == "STRATEGY_SOURCE_MISMATCH"


def test_sessions_must_be_a_positive_integer():
    for bad in (0, -1, True, 2.0):
        with pytest.raises(ValueError):
            deployment_sessions(XNYSCalendarPolicy(), registered_at=FRIDAY_EVENING, sessions=bad,
                                bundle_expires_at=FRIDAY_EVENING + dt.timedelta(days=90))


class _Request:
    def __init__(self, body, action="submit_ai_paper_decision"):
        self.action, self.body = action, body


def _gate_for(world, kinds=None):
    kind_of = (lambda digest: kinds[digest]) if kinds else world.deployments.kind_of
    return deployment_version_gate(kind_of=kind_of, activity=world.activity)


def test_gate_refuses_a_strategy_enter_without_a_version(world):
    digest, record = world.seal(1)
    base = world.versions.get(digest).base_digest
    gate = _gate_for(world)
    enter = {"action": "ENTER", "deployment_digest": base, "deployment_version": digest,
             "source_digest": record["strategy_digest"]}
    assert gate(_Request(enter), None, None, world.now) is None
    assert gate(_Request({**enter, "deployment_version": None}), None, None, world.now) == \
        "DEPLOYMENT_VERSION_REQUIRED"
    world.versions.withdraw(digest, reason="x", principal="cli", command_id="w")
    assert gate(_Request(enter), None, None, world.now) == "DEPLOYMENT_NOT_ACTIVE"


def test_gate_ignores_other_actions_reductions_and_discretionary(world):
    base = "sha256:" + "d" * 64
    gate = _gate_for(world, kinds={base: "discretionary"})
    assert gate(_Request({"action": "ENTER", "deployment_digest": base}), None, None, world.now) is None
    strategy_gate = _gate_for(world, kinds={base: "strategy"})
    assert strategy_gate(_Request({"action": "CLOSE", "deployment_digest": base}), None, None, world.now) is None
    assert strategy_gate(_Request({"action": "ENTER", "deployment_digest": base}, action="propose"),
                         None, None, world.now) is None


def test_a_tampered_version_row_fails_entry_checks_loudly(world):
    healthy, record = world.seal(1)
    tampered, _ = world.seal(2)
    world.db.execute("UPDATE ai_deployment_versions SET record_json = ? WHERE digest = ?", ["{}", tampered])
    base = world.versions.get(healthy).base_digest
    with pytest.raises(DeploymentRefused) as refused:
        world.activity.entry_refusal(deployment_digest=base, version_digest=healthy,
                                     source_digest=record["strategy_digest"])
    assert refused.value.code == "DEPLOYMENT_VERSION_TAMPERED"


def test_decided_versions_do_not_read_their_judgment(world):
    withdrawn, _ = world.seal(1)
    world.versions.withdraw(withdrawn, reason="x", principal="cli", command_id="w")
    superseded, _ = world.seal(2)
    world.seal(3, kind=RENEWAL, prior=superseded, first=dt.date(2026, 10, 14))
    healthy, record = world.seal(4)
    read_ids = []
    original_get = world.judgments.get

    def get(judgment_id):
        read_ids.append(judgment_id)
        if judgment_id in ("jdg-1", "jdg-2"):
            raise RuntimeError("case file is gone")
        return original_get(judgment_id)
    world.judgments.get = get
    base = world.versions.get(healthy).base_digest
    assert world.activity.entry_refusal(deployment_digest=base, version_digest=healthy,
                                        source_digest=record["strategy_digest"]) is None
    assert {a.version_digest for a in world.activity.active()} >= {healthy}
    assert "jdg-1" not in read_ids and "jdg-2" not in read_ids


def test_an_unreadable_judgment_of_an_undecided_version_still_fails_closed(world):
    digest, record = world.seal(1)
    world.judgments.get = lambda judgment_id: (_ for _ in ()).throw(RuntimeError("case file is gone"))
    base = world.versions.get(digest).base_digest
    with pytest.raises(RuntimeError):
        world.activity.entry_refusal(deployment_digest=base, version_digest=digest,
                                     source_digest=record["strategy_digest"])
