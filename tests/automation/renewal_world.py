"""Tests only: one judged DEPLOY line on a real journal, the real renewal ports and a fake bundle check."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

from tests.automation.backtest_judge_fixtures import (
    FILE_HASH, NOW, World, finished, judge_config, judgment, renewal_case_body, world,
)
from tests.scoreboard.fills import broker_store, put_fill
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
from trader.automation.ai_paper_decision import (
    AiPaperDecisionStore, apply_ai_paper_decision_migration, command_id_for,
)
from trader.automation.backtest_judgments import BacktestJudgments
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.deployment_renewal import RenewalGate, TraderRenewalChecks, VersionForwardEvidence
from trader.automation.forward_evidence import ForwardEvidenceRefused
from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.research.evaluation_case import EvaluationCase, write_evaluation_case
from trader.research.forward_evidence_view import forward_evidence_digest
from trader.scoreboard.ports import DecisionStoreAttribution
from trader.scoreboard.round_trips import project_round_trips
from trader.scoreboard.schema import apply_scoreboard_migrations
from trader.scoreboard.session_ledger import experiment_fills
from trader.scoreboard.shadow_ingest import RecordShadowResultRequest, ShadowIngest
from trader.scoreboard.store import ScoreboardStore
from trader.trading.order_correlation import encode_order_ref

UTC = dt.timezone.utc
BUNDLE = "sha256:" + "e" * 64
FIRST, EXPIRY = dt.date(2026, 10, 9), dt.date(2026, 10, 13)       # three sessions: 10-09, 10-12, 10-13
SESSIONS = ("2026-10-09", "2026-10-12", "2026-10-13")
AFTER_EXPIRY = dt.datetime(2026, 10, 14, 22, 0, tzinfo=UTC)          # Wednesday 18:00 New York
RESEARCH = SimpleNamespace(principal="research")
EXPERIMENT = SimpleNamespace(experiment_id="exp-1", account_id="DU1",
                             started_at=dt.datetime(2026, 10, 1, tzinfo=UTC), stopped_at=None)
FIRST_FILL = dt.datetime(2026, 10, 9, 14, 0, tzinfo=UTC)            # the version's first session, 10:00 New York
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


class FakeExperiments:
    """ExperimentStore's read: the one experiment the fixture's paper trips run in."""

    def get(self, experiment_id: str):
        return EXPERIMENT if experiment_id == EXPERIMENT.experiment_id else None


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
    broker: Any
    links: DecisionStoreAttribution

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

    def paper_trip(self, version_digest: str, label: str, *, net_pnl: float) -> str:
        """An ENTER decision bound to ``version_digest`` and the closed trip its order filled: real broker fills
        (10 shares, $0.50 commission each way), then the scoreboard's refresh. Returns the round trip id."""
        decision_id = f"dec-{label}"
        self.base.db.execute(
            "INSERT INTO ai_paper_decisions (command_id, decision_id, account_id, conid, action, decider, body_json, "
            "state, received_at, updated_at, experiment_id, deployment_version) "
            "VALUES (?, ?, 'DU1', 265598, 'ENTER', 'jev', '{}', 'FINAL', ?, ?, ?, ?)",
            [command_id_for(decision_id), decision_id, NOW, NOW, EXPERIMENT.experiment_id, version_digest])
        opened = FIRST_FILL + dt.timedelta(minutes=10 * self._fills_placed())
        ref = encode_order_ref(f"og-{command_id_for(decision_id)}")
        exit_price = 100.0 + (net_pnl + 1.0) / 10
        put_fill(self.base.db, self.broker, "DU1", f"x-{label}-in", "BUY", 10, 100.0, 0.5, opened, ref=ref)
        put_fill(self.base.db, self.broker, "DU1", f"x-{label}-out", "SELL", 10, exit_price, 0.5,
                 opened + dt.timedelta(minutes=5), ref=ref)
        trips = self.refresh_trips()
        (trip,) = [t for t in trips if t.decision_id == decision_id]
        return trip.round_trip_id

    def _fills_placed(self) -> int:
        return self.base.db.execute("SELECT COUNT(*) FROM broker_fills", fetch="one")[0]

    def refresh_trips(self) -> tuple:
        """ScoreboardService.refresh: the stored round_trips projection of the experiment, rebuilt from its fills."""
        projection = project_round_trips(experiment_fills(self.base.db, EXPERIMENT),
                                         links_for=self.links.links_for_order_ref, account_id="DU1")
        self.scoreboard.replace_round_trips(EXPERIMENT.experiment_id, [
            t.as_row(EXPERIMENT.experiment_id, "DU1") for t in projection.trips])
        return projection.trips

    def renewal_case(self, prior: str, *, forward_sessions: int = 3, incomplete_sessions: int = 0,
                     **changes) -> str:
        """A signed renewal case bound, like the research service's, to the trader's forward evidence now."""
        raw = renewal_case_body(**changes)
        raw["renewal"] = {"prior_deployment_version": prior, "forward_sessions": forward_sessions,
                          "incomplete_sessions": incomplete_sessions}
        if "evidence" not in changes:
            raw["evidence"] = {**raw["evidence"], "forward_evidence_digest": self.forward_digest(prior)}
        return write_evaluation_case(self.base.keys.cases_dir, EvaluationCase.model_validate(raw),
                                     self.base.keys.signer)

    def forward_digest(self, prior: str) -> Optional[str]:
        try:
            facts = self.forward.facts(prior)
            return forward_evidence_digest(self.forward.sessions(facts), self.forward.trips(prior))
        except ForwardEvidenceRefused:          # an unknown prior: the trader refuses that case before the digest
            return None

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
    broker = broker_store(base.db, migrator)
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
    links = DecisionStoreAttribution(AiPaperDecisionStore(journal))
    forward = VersionForwardEvidence(db=base.db, scoreboard=scoreboard, experiments=FakeExperiments(), links=links,
                                     versions=versions, deployments=deployments,
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
                        scoreboard, broker, links)
