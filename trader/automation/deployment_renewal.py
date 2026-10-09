"""Renewal of an expired AI deployment version (SP2c spec 5.2 items 2 and 7, 6.2 "Renewal after expiry").

VersionForwardEvidence answers get_deployment_forward_evidence: the version's sessions, its sealed shadow rows
and its paper trips (recomputed from the broker fills; the stored round_trips projection is only checked).
RenewalGate says whether a version may be renewed now. TraderRenewalChecks is Plan 1's RenewalChecks port. Every read happens before any write transaction. A tampered record is raised by name, never
answered as a business refusal.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trader.automation.ai_bundle_check import BundleRefused
from trader.automation.ai_deployment_activity import deployment_sessions
from trader.automation.ai_deployments import AiDeployment, DeploymentRefused
from trader.automation.ai_judgment_port import strategy_key
from trader.automation.backtest_judgments import JUDGMENT_TAMPERED, JudgmentRefused, RenewalStatus
from trader.automation.forward_evidence import ForwardEvidenceRefused
from trader.automation.strategy_binding import bar_size_key, same_values
from trader.research.evaluation_case import EvaluationCase
from trader.research.forward_evidence_view import (
    ForwardEvidenceView, ForwardSession, LineFacts, PaperTrip, Renewability, VersionBinding,
)
from trader.research.shadow_window import session_close_utc, shadow_window
from trader.research.strategy_key import split_strategy_key
from trader.research.strategy_paths import normalize_strategy_path
from trader.scoreboard.round_trips import RoundTrip, project_round_trips
from trader.scoreboard.service import _differences
from trader.scoreboard.session_ledger import experiment_fills
from trader.scoreboard.shadow_ingest import _is_intact, shadow_record_id
from trader.scoreboard.store import row_key

FORWARD_EVIDENCE_TAMPERED = "FORWARD_EVIDENCE_TAMPERED"
DEPLOYMENT_VERSION_TAMPERED = "DEPLOYMENT_VERSION_TAMPERED"
SESSION_NOT_CLOSED = "SESSION_NOT_CLOSED"
NOT_DUE = frozenset({"ACTIVE", "NOT_STARTED", "OVER_CAP"})
FEE_COLUMNS = ("fees_usd", "net_pnl_usd", "fees_complete")
NOT_FILL_DERIVED = frozenset({"experiment_id", "account_id", *FEE_COLUMNS})
Refusal = tuple[str, str]


def _money(value: Any) -> Optional[float]:
    return None if value is None else float(value)


@dataclass(frozen=True)
class VersionFacts:
    digest: str
    version: Any                 # DeploymentVersion
    status: str                  # Plan 2's raw status
    base: AiDeployment
    line: LineFacts
    recorded_at: dt.datetime     # of the version's own judgment: it anchors the shadow window (Plan 3)


class RenewalGate:
    """May this version be renewed now? One answer for the forward evidence and for the judgment check."""

    def __init__(self, *, renewals_of: Callable[[str], tuple], bundles: Any, cooldowns: Any, calendar: Any,
                 expiry_sessions: int):
        self._renewals_of, self._bundles, self._cooldowns = renewals_of, bundles, cooldowns
        self._calendar, self._expiry_sessions = calendar, expiry_sessions

    def line_refusal(self, version_digest: str, status: str) -> Optional[Refusal]:
        """Ruling 1: only an EXPIRED version, never renewed or judged for renewal before."""
        if status in NOT_DUE:
            return "RENEWAL_NOT_DUE", f"the version is {status}; a renewal starts after its expiry"
        if status == "WITHDRAWN":
            return "RENEWAL_PRIOR_INVALID", "the version was withdrawn"
        if status == "SUPERSEDED":
            return "RENEWAL_PRIOR_INVALID", "the version was already renewed"
        if status == "JUDGMENT_ENDED":
            return "RENEWAL_LINE_ENDED", "a judgment ended this line"
        if self._renewals_of(version_digest):
            return "RENEWAL_ALREADY_JUDGED", "a renewal judgment already names this version"
        return None

    def deploy_block(self, base: AiDeployment, line: LineFacts, now: dt.datetime) -> Optional[Refusal]:
        """Rulings 7 and 8: the registration's own bundle and session rule, and no cooldown."""
        try:
            bundle = self._bundles.check(base.evidence_ref, artifact_id=line.artifact_id, now=now)
            deployment_sessions(self._calendar, registered_at=now, sessions=self._expiry_sessions,
                                bundle_expires_at=bundle.expires_at)
        except BundleRefused as refused:
            return refused.code, refused.message
        except DeploymentRefused as refused:            # BUNDLE_EXPIRED: no session left before the expiry
            return refused.code, refused.message
        if self._cooldowns.cooling_down(strategy_key(base.strategy_path, base.class_name), now):
            return "FAMILY_COOLING_DOWN", "the strategy key is cooling down"
        return None


class VersionForwardEvidence:
    """Plan 1's ForwardEvidenceSource over Plan 2's versions and Plan 3's sealed shadow rows (ruling 4)."""

    def __init__(self, *, db: Any, scoreboard: Any, experiments: Any, links: Any, versions: Any, deployments: Any,
                 status_of: Callable[[str], str], judgment_of: Callable[[str], Any], gate: RenewalGate,
                 calendar: Any, config: Any, now: Callable[[], dt.datetime]):
        self._db, self._scoreboard, self._versions, self._deployments = db, scoreboard, versions, deployments
        self._experiments, self._links = experiments, links
        self._status_of, self._judgment_of, self._gate = status_of, judgment_of, gate
        self._calendar, self._config, self._now = calendar, config, now

    def read(self, version_digest: str) -> dict:
        facts = self.facts(version_digest)
        now = self._now()
        refusal = self._gate.line_refusal(version_digest, facts.status) \
            or self._gate.deploy_block(facts.base, facts.line, now)
        version = facts.version
        return ForwardEvidenceView(
            version_digest=version_digest, base_digest=version.base_digest, judgment_id=version.judgment_id,
            kind=version.kind, prior_version_digest=version.prior_version, status=facts.status,
            first_session=version.first_session.isoformat(), expiry_session=version.expiry_session.isoformat(),
            binding=self._binding(facts.base), line=facts.line,
            renewable=Renewability(ok=refusal is None, code=None if refusal is None else refusal[0],
                                   detail="" if refusal is None else refusal[1]),
            sessions=self.sessions(facts), trips=self.trips(version_digest), as_of=now.isoformat()).to_wire()

    def facts(self, version_digest: str) -> VersionFacts:
        """Only ``version_digest`` itself may be unknown. Every record behind a sealed version (its base, its
        chain, its own and its INITIAL judgment) must be there: a miss is tampering, never a business refusal."""
        try:
            version = self._versions.get(version_digest)
        except DeploymentRefused as refused:
            raise ForwardEvidenceRefused(refused.code, refused.message) from None
        try:
            status = self._status_of(version_digest)
            base = self._deployments.get_sealed(version.base_digest)
            first = version
            while first.prior_version is not None:            # the INITIAL version of the line
                first = self._versions.get(first.prior_version)
        except DeploymentRefused as refused:
            code = refused.code if refused.code.endswith("TAMPERED") else DEPLOYMENT_VERSION_TAMPERED
            raise ForwardEvidenceRefused(code, f"behind sealed version {version_digest}: {refused.message}") from None
        except JudgmentRefused as refused:                     # the status reads every version's judgment
            raise ForwardEvidenceRefused(refused.code, refused.detail) from None
        own, initial = self._judgment(version.judgment_id), self._judgment(first.judgment_id)
        binding = initial.binding
        line = LineFacts(initial_judgment_id=initial.judgment_id, family_id=binding["family_id"],
                         selected_trial_id=binding["selected_trial_id"], artifact_id=binding["artifact_id"],
                         eligibility_decision_digest=binding["eligibility_decision_digest"])
        return VersionFacts(version_digest, version, status, base, line, own.recorded_at)

    def sessions(self, facts: VersionFacts) -> list[ForwardSession]:
        version = facts.version
        window_first, window_last = shadow_window(
            facts.recorded_at, "DEPLOY", deploy_expiry_sessions=self._config.deploy_expiry_sessions)
        days = self._calendar.sessions_in_range(version.first_session, version.expiry_session)
        rows = self._sealed_rows(version.judgment_id, days)
        now = self._now()
        found = []
        for day in days:
            row = rows.get(day)
            if not window_first <= day <= window_last:
                found.append(_session(day, "NOT_REPLAYED", "outside the judgment's shadow window"))
            elif row is None:
                found.append(_session(day, "MISSING", SESSION_NOT_CLOSED if session_close_utc(day) > now else None))
            else:
                found.append(ForwardSession(
                    session_date=day.isoformat(), state=row["status"], reason=row["reason"],
                    pnl_usd=_money(row["pnl_usd"]), fees_usd=_money(row["fees_usd"]),
                    trades=None if row["trades"] is None else int(row["trades"]),
                    end_equity_usd=_money(row["end_equity_usd"])))
        return found

    def _judgment(self, judgment_id: str) -> Any:
        try:
            judgment = self._judgment_of(judgment_id)
        except JudgmentRefused as refused:                     # a tampered row is named, never read as missing
            raise ForwardEvidenceRefused(refused.code, refused.detail) from None
        if judgment is None:                                   # a sealed version names it
            raise ForwardEvidenceRefused(JUDGMENT_TAMPERED, f"the judgment {judgment_id} of a sealed version is gone")
        return judgment

    def _sealed_rows(self, judgment_id: str, days: list[dt.date]) -> dict[dt.date, dict]:
        """Ruling 9: every row must hold its body digest, its record id and its scoreboard seal, and every
        sealed row of ``days`` must still be there (a deleted or re-keyed row is not a missing one)."""
        sealed = self._scoreboard.sealed_digests("shadow_results")
        found = {}
        for row in self._scoreboard.fetch("shadow_results", {"judgment_id": judgment_id}):
            if not _is_intact(row, sealed.get(row_key(row, ("record_id",)))):
                raise ForwardEvidenceRefused(FORWARD_EVIDENCE_TAMPERED,
                                             f"shadow row {row['record_id']} does not match its body digest or seal")
            found[row["session_date"]] = row
        for day in days:
            if day not in found and shadow_record_id(judgment_id, day.isoformat()) in sealed:
                raise ForwardEvidenceRefused(FORWARD_EVIDENCE_TAMPERED,
                                             f"the sealed shadow row of {judgment_id} on {day} is gone")
        return found

    def trips(self, version_digest: str) -> list[PaperTrip]:
        """The version's trips recomputed from the broker fills and the decision links, as scoreboard verify does.
        The stored projection is never the evidence: a stored trip that its own fills contradict is tampering."""
        decisions = self._db.execute(
            "SELECT DISTINCT decision_id, experiment_id FROM ai_paper_decisions "
            "WHERE action = 'ENTER' AND deployment_version = ? AND decision_id IS NOT NULL",
            [version_digest], fetch="all")
        decision_ids = {decision_id for decision_id, _ in decisions}
        if not decision_ids:
            return []
        experiment_ids = {experiment_id for _, experiment_id in decisions if experiment_id is not None}
        experiment_ids |= self._stored_trip_experiments(decision_ids)
        trips = [trip for experiment_id in sorted(experiment_ids)
                 for trip in self._verified_trips(experiment_id, decision_ids)]
        trips.sort(key=lambda trip: (trip.opened_session, trip.round_trip_id))
        return [PaperTrip(round_trip_id=t.round_trip_id, conid=t.conid, status=t.status,
                          opened_session=t.opened_session.isoformat(),
                          closed_session=None if t.closed_session is None else t.closed_session.isoformat(),
                          net_pnl_usd=t.net_pnl_usd, fees_complete=t.fees_complete) for t in trips]

    def _stored_trip_experiments(self, decision_ids: set[str]) -> set[str]:
        markers = ", ".join("?" for _ in decision_ids)
        rows = self._db.execute(f"SELECT DISTINCT experiment_id FROM round_trips WHERE decision_id IN ({markers})",
                                sorted(decision_ids), fetch="all")
        return {row[0] for row in rows}

    def _verified_trips(self, experiment_id: str, decision_ids: set[str]) -> list[RoundTrip]:
        experiment = self._experiments.get(experiment_id)
        if experiment is None:
            raise ForwardEvidenceRefused(FORWARD_EVIDENCE_TAMPERED,
                                         f"the experiment {experiment_id} behind the version's trips is gone")
        projection = project_round_trips(experiment_fills(self._db, experiment),
                                         links_for=self._links.links_for_order_ref, account_id=experiment.account_id)
        recomputed = {trip.round_trip_id: trip for trip in projection.trips}
        for row in self._scoreboard.fetch("round_trips", {"experiment_id": experiment_id}):
            if row["decision_id"] not in decision_ids:
                continue
            trip = recomputed.get(row["round_trip_id"])
            if trip is None:
                raise ForwardEvidenceRefused(FORWARD_EVIDENCE_TAMPERED,
                                             f"round trip {row['round_trip_id']} has no broker fills behind it")
            edited = _projection_edits(row, self._scoreboard.prepare(
                "round_trips", trip.as_row(experiment_id, experiment.account_id)))
            if edited:
                raise ForwardEvidenceRefused(FORWARD_EVIDENCE_TAMPERED,
                                             f"round trip {row['round_trip_id']} differs from its broker fills: "
                                             f"{', '.join(edited)}")
        return [trip for trip in projection.trips if trip.decision_id in decision_ids]

    @staticmethod
    def _binding(base: AiDeployment) -> VersionBinding:
        return VersionBinding(
            strategy_key=strategy_key(base.strategy_path, base.class_name), strategy_path=base.strategy_path,
            class_name=base.class_name, strategy_file_hash=base.strategy_digest, params=dict(base.params),
            conids=sorted(int(c) for c in base.conids), bar_size=base.bar_size,
            order_notional=float(base.evidence_order_notional), bundle_digest=base.evidence_ref)


def _projection_edits(stored: dict, recomputed: dict) -> list[str]:
    """Columns of a stored trip that its own fills contradict. Other fills than the stored ones, or a commission
    that arrived after the stored fees were incomplete, are the 30 s refresh lagging, not an edit."""
    if stored["fills_digest"] != recomputed["fills_digest"]:
        return []
    edited = _differences(stored, recomputed, [name for name in recomputed if name not in NOT_FILL_DERIVED])
    if stored["fees_complete"]:
        edited.update(_differences(stored, recomputed, FEE_COLUMNS))
    elif stored["fees_usd"] is not None or stored["net_pnl_usd"] is not None:
        edited["fees_usd"] = "incomplete fees carry an amount"
    return sorted(edited)


def _session(day: dt.date, state: str, reason: Optional[str]) -> ForwardSession:
    return ForwardSession(session_date=day.isoformat(), state=state, reason=reason, pnl_usd=None, fees_usd=None,
                          trades=None, end_equity_usd=None)


def case_differences(case: EvaluationCase, facts: VersionFacts, sessions: list[ForwardSession]) -> list[str]:
    """Ruling 6: the renewal case must name exactly the renewed version's binding and window."""
    base, line = facts.base, facts.line
    path, class_name = split_strategy_key(case.strategy_key)
    problems = []
    if normalize_strategy_path(path) != normalize_strategy_path(base.strategy_path) or class_name != base.class_name:
        problems.append(f"strategy key differs: {case.strategy_key}")
    if case.strategy_file_hash != base.strategy_digest:
        problems.append("file hash differs")
    if not same_values(dict(case.selected_params or {}), dict(base.params)):
        problems.append("params differ")
    if list(case.conids) != sorted(int(c) for c in base.conids):
        problems.append("conids differ")
    if bar_size_key(case.bar_size) != bar_size_key(base.bar_size):
        problems.append("bar size differs")
    for name in ("artifact_id", "family_id"):
        value = getattr(case, name)
        if value is not None and value != getattr(line, name):
            problems.append(f"{name} differs")
    if case.renewal.forward_sessions != len(sessions):
        problems.append(f"forward_sessions {case.renewal.forward_sessions} is not the window's {len(sessions)}")
    return problems


class TraderRenewalChecks:
    """Plan 1's RenewalChecks port (spec 5.2 item 2): called before the judgment's write transaction."""

    def __init__(self, *, forward: VersionForwardEvidence, gate: RenewalGate):
        self._forward, self._gate = forward, gate

    def status(self, case: EvaluationCase, *, now: dt.datetime) -> RenewalStatus:
        prior = case.renewal.prior_deployment_version
        try:
            facts = self._forward.facts(prior)
            sessions = self._forward.sessions(facts)
        except ForwardEvidenceRefused as refused:
            if refused.code.endswith("TAMPERED"):
                raise JudgmentRefused(refused.code, refused.detail) from None
            # facts() lets only the prior digest itself be unknown; anything behind it is tampering
            code = "RENEWAL_PRIOR_INVALID" if refused.code == "DEPLOYMENT_VERSION_UNKNOWN" else refused.code
            return RenewalStatus(refusal_code=code, detail=refused.detail)
        refusal = self._gate.line_refusal(prior, facts.status)
        if refusal is not None:
            return RenewalStatus(refusal_code=refusal[0], detail=refusal[1])
        differences = case_differences(case, facts, sessions)
        if differences:
            return RenewalStatus(refusal_code="RENEWAL_CASE_MISMATCH", detail="; ".join(differences))
        block = self._gate.deploy_block(facts.base, facts.line, now)
        if block is not None:
            return RenewalStatus(deploy_block_code=block[0], detail=block[1])
        open_sessions = [s.session_date for s in sessions if s.state != "COMPLETE"]
        if open_sessions:
            return RenewalStatus(deploy_block_code="FORWARD_INCOMPLETE",
                                 detail=f"forward sessions not COMPLETE: {open_sessions}")
        return RenewalStatus()
