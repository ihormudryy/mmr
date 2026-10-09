"""Tests only: one judged DEPLOY line on a real journal, the real renewal ports and a fake bundle check."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from tests.automation.backtest_judge_fixtures import (
    FILE_HASH, NOW, World, finished, judge_config, judgment, renewal_case_body, world,
)
from trader.automation.ai_bundle_check import BundleFacts, BundleRefused
from trader.automation.ai_deployment_activity import DeploymentActivity
from trader.automation.ai_deployment_registration import AiDeploymentRegistrar
from trader.automation.ai_deployment_versions import (
    INITIAL, AiDeploymentVersionStore, DeploymentVersion, apply_ai_deployment_version_migrations,
)
from trader.automation.ai_deployments import (
    AiDeployment, AiDeploymentStore, apply_ai_deployment_migration, deployment_digest,
)
from trader.automation.ai_judgment_port import cooldown_reader_for, judgment_reader_for
from trader.automation.ai_paper_decision import apply_ai_paper_decision_migration
from trader.automation.backtest_judgments import BacktestJudgments
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.deployment_renewal import RenewalGate, TraderRenewalChecks, VersionForwardEvidence
from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.research.evaluation_case import EvaluationCase, write_evaluation_case
from trader.scoreboard.schema import apply_scoreboard_migrations
from trader.scoreboard.shadow_ingest import RecordShadowResultRequest, ShadowIngest
from trader.scoreboard.store import ScoreboardStore

UTC = dt.timezone.utc
BUNDLE = "sha256:" + "e" * 64
FIRST, EXPIRY = dt.date(2026, 10, 9), dt.date(2026, 10, 13)       # three sessions: 10-09, 10-12, 10-13
SESSIONS = ("2026-10-09", "2026-10-12", "2026-10-13")
AFTER_EXPIRY = dt.datetime(2026, 10, 14, 22, 0, tzinfo=UTC)          # Wednesday 18:00 New York
RESEARCH = SimpleNamespace(principal="research")
RECORD = {"strategy_path": "strategies/opening_range_breakout.py", "strategy_digest": FILE_HASH,
          "class_name": "OpeningRangeBreakout", "params": {"RANGE_MINUTES": 15}, "conids": [265598, 272093],
          "bar_size": "5 mins", "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
          "evidence_ref": BUNDLE, "evidence_order_notional": 1900.0}


class FakeBundles:
    """ResearchBundleCheck's contract: artifact art-1 of the fixture line, attested until ``expires_at``."""

    def __init__(self, expires_at: dt.datetime = dt.datetime(2026, 12, 31, tzinfo=UTC)):
        self.expires_at = expires_at

    def manifest_artifact_id(self, digest: str) -> str:
        return "art-1"

    def check(self, digest: str, *, artifact_id: str, now: dt.datetime) -> BundleFacts:
        if digest != BUNDLE or artifact_id != "art-1":
            raise BundleRefused("BUNDLE_INVALID", f"{digest} does not attest {artifact_id}")
        if now >= self.expires_at:
            raise BundleRefused("BUNDLE_EXPIRED", f"the attestation expired at {self.expires_at.isoformat()}")
        return BundleFacts(BUNDLE, "art-1", "fam-1", RECORD["strategy_path"], RECORD["class_name"], FILE_HASH,
                           dict(RECORD["params"]), tuple(RECORD["conids"]), RECORD["bar_size"], 1900.0,
                           "openrouter/jev-1#jdg-00000001", "llm", self.expires_at)


@dataclass
class RenewalWorld:
    base: World
    journal: DomainJournal
    judgments: BacktestJudgments
    reader: Any
    versions: AiDeploymentVersionStore
    deployments: AiDeploymentStore
    activity: DeploymentActivity
    forward: VersionForwardEvidence
    bundles: FakeBundles
    shadow: ShadowIngest
    scoreboard: ScoreboardStore

    @property
    def clock(self):
        return self.base.clock

    def deploy_initial(self, *, first: dt.date = FIRST, expiry: dt.date = EXPIRY) -> str:
        """The INITIAL line: Plan 1's recorded DEPLOY and a sealed version (Plan 2's stores, no bundle path)."""
        assert self.judgments.record(judgment(finished(self.base), "DEPLOY"))["status"] == "RECORDED"
        deployment = AiDeployment.from_json(RECORD)
        version = DeploymentVersion(deployment_digest(deployment), "jdg-00000001", INITIAL, None, first, expiry)

        def write(conn):
            self.deployments.register_in_tx(conn, deployment, principal="ai_research", command_id="c-initial")
            return self.versions.seal_in_tx(conn, version, request_digest="sha256:" + "1" * 64,
                                            principal="ai_research", command_id="c-initial")[0]
        return self.base.db.transaction(write)

    def shadow_row(self, session: str, status: str = "COMPLETE", judgment_id: str = "jdg-00000001") -> str:
        """One row through Plan 3's real ingest (window check, seal). The session must have closed by now."""
        stored = self.judgments.get(judgment_id)
        numbers = ({"reason": None, "pnl_usd": 4.0, "fees_usd": 1.0, "trades": 1, "end_equity_usd": 100_004.0}
                   if status == "COMPLETE" else
                   {"reason": "BARS_MISSING: fixture", "pnl_usd": None, "fees_usd": None, "trades": None,
                    "end_equity_usd": None})
        request = RecordShadowResultRequest(judgment_id=judgment_id, case_digest=stored.case_digest,
                                            verdict=stored.verdict, session_date=session, status=status,
                                            bar_size="5 mins", **numbers)
        return self.shadow.record(request, RESEARCH)["status"]

    def paper_trip(self, version_digest: str, round_trip_id: str, *, net_pnl: float) -> None:
        """An ENTER decision bound to ``version_digest`` and the round trip it opened."""
        self.base.db.execute(
            "INSERT INTO ai_paper_decisions (command_id, decision_id, account_id, conid, action, body_json, state, "
            "received_at, updated_at, deployment_version) VALUES (?, ?, 'DU1', 265598, 'ENTER', '{}', 'FINAL', ?, ?, ?)",
            [f"cmd-{round_trip_id}", f"dec-{round_trip_id}", NOW, NOW, version_digest])
        self.scoreboard.replace_round_trips(f"exp-{round_trip_id}", [{
            "round_trip_id": round_trip_id, "experiment_id": f"exp-{round_trip_id}", "account_id": "DU1",
            "conid": 265598, "symbol": "AAPL", "direction": "LONG", "status": "CLOSED", "opened_at": NOW,
            "closed_at": NOW, "opened_session": FIRST, "closed_session": FIRST, "entry_qty": 10.0, "exit_qty": 10.0,
            "entry_avg": 100.0, "exit_avg": 101.0, "gross_pnl_usd": net_pnl + 1.0, "fees_usd": 1.0,
            "net_pnl_usd": net_pnl, "fees_complete": True, "notional_traded_usd": 2010.0, "strategy_version": None,
            "decider": "jev", "policy_revision": None, "style": "intraday_long",
            "decision_id": f"dec-{round_trip_id}", "links_digest": None, "exec_ids": "[]", "fills_digest": "f"}])

    def renewal_case(self, prior: str, *, forward_sessions: int = 3, incomplete_sessions: int = 0,
                     **changes) -> str:
        raw = renewal_case_body(**changes)
        raw["renewal"] = {"prior_deployment_version": prior, "forward_sessions": forward_sessions,
                          "incomplete_sessions": incomplete_sessions}
        return write_evaluation_case(self.base.keys.cases_dir, EvaluationCase.model_validate(raw),
                                     self.base.keys.signer)

    def renew(self, case_digest: str, prior: str, verdict: str = "DEPLOY", *,
              judgment_id: str = "jdg-renewal-1", **changes) -> dict:
        request = judgment(case_digest, verdict, kind="RENEWAL", renewal_of_version=prior, judgment_id=judgment_id,
                           decided_at=self.clock.now.isoformat(), **changes)
        return self.judgments.record(request)

    def registrar(self) -> AiDeploymentRegistrar:
        return AiDeploymentRegistrar(
            journal=self.journal, deployments=self.deployments, versions=self.versions, activity=self.activity,
            judgments=self.reader, cooldowns=cooldown_reader_for(self.base.db), bundles=self.bundles,
            calendar=XNYSCalendarPolicy(), expiry_sessions=3, now=self.clock)


def renewal_world(tmp_path, *, bundles: FakeBundles | None = None) -> RenewalWorld:
    base = world(tmp_path, deploy_expiry_sessions=3)
    migrator = SchemaMigrator(base.db)
    journal = DomainJournal(base.db)
    journal.migrate(migrator)
    apply_ai_deployment_migration(migrator)
    apply_ai_deployment_version_migrations(migrator)
    apply_ai_paper_decision_migration(migrator)
    apply_scoreboard_migrations(migrator)
    config, calendar, bundles = judge_config(deploy_expiry_sessions=3), XNYSCalendarPolicy(), bundles or FakeBundles()
    scoreboard = ScoreboardStore(base.db, now=base.clock)
    deployments = AiDeploymentStore(base.db, now=base.clock)
    versions = AiDeploymentVersionStore(journal, now=base.clock)
    cooldowns = cooldown_reader_for(base.db)
    # Plan 5 ruling 14: the lambdas run only after `judgments` and `activity` exist.
    gate = RenewalGate(renewals_of=lambda digest: judgments.renewals_of(digest), bundles=bundles,
                       cooldowns=cooldowns, calendar=calendar, expiry_sessions=3)
    forward = VersionForwardEvidence(db=base.db, scoreboard=scoreboard, versions=versions, deployments=deployments,
                                     status_of=lambda digest: activity.status(digest),
                                     judgment_of=lambda judgment_id: judgments.get(judgment_id), gate=gate,
                                     calendar=calendar, config=config, now=base.clock)
    judgments = BacktestJudgments(base.db, config=config, calendar=calendar, cases_dir=base.keys.cases_dir,
                                  verify_dir=base.keys.verify_dir, now=base.clock,
                                  renewals=TraderRenewalChecks(forward=forward, gate=gate))
    reader = judgment_reader_for(judgments, cases_dir=base.keys.cases_dir, verify_dir=base.keys.verify_dir,
                                 renewals_of=judgments.renewals_of)
    activity = DeploymentActivity(versions=versions, deployments=deployments, judgments=reader,
                                  cooldowns=cooldowns, max_active=3, now=base.clock)
    shadow = ShadowIngest(store=scoreboard, judgments=judgments, versions=versions, config=config, now=base.clock)
    return RenewalWorld(base, journal, judgments, reader, versions, deployments, activity, forward, bundles, shadow,
                        scoreboard)
