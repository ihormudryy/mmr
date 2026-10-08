"""``register_ai_deployment`` bound to a durable DEPLOY judgment and a verified bundle (SP2c spec 5.2 item 4).

Registration reads the artifacts directory and the journal only. It places no IB call and writes nothing
outside the journal.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Any, Callable, Mapping, Optional, Sequence

from trader.automation.ai_bundle_check import BundleFacts
from trader.automation.ai_deployment_activity import (
    ACTIVE, NOT_STARTED, classify, deployment_sessions, ny_date,
)
from trader.automation.ai_deployment_versions import INITIAL, DeploymentVersion
from trader.automation.ai_deployments import (
    STRATEGY_DIGEST_PROVENANCE, AiDeployment, DeploymentRefused, deployment_digest,
)
from trader.automation.ai_judgment_port import JudgmentFacts, strategy_key
from trader.automation.strategy_binding import bar_size_key, same_values
from trader.research.canonical import canonical_json_bytes
from trader.research.strategy_paths import normalize_strategy_path

KEYS = frozenset({"judgment_id", "bundle_digest", "deployment"})
COMMAND_PREFIX = "aidep-"
CALENDAR_UNAVAILABLE = "DEPLOYMENT_CALENDAR_UNAVAILABLE"
_JUDGMENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_BUNDLE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def registration_command_id(body: Mapping[str, Any], day: dt.date) -> str:
    """Ruling 1: a same-day retry replays the ledger; another body or day reaches the handler."""
    material = canonical_json_bytes(dict(body)) + b"|" + day.isoformat().encode()
    return COMMAND_PREFIX + hashlib.sha256(material).hexdigest()[:48]


def request_digest(body: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(dict(body))).hexdigest()


def binding_differences(deployment: AiDeployment, bundle: BundleFacts, cases: Sequence[JudgmentFacts], *,
                        bundle_digest: str, initial_judgment_id: str) -> list[str]:
    """Rulings 3-4: every field the bundle, each judgment's case and the body must agree on."""
    problems: list[str] = []

    def same(name: str, *values: Any) -> None:
        if any(value != values[0] for value in values[1:]):
            problems.append(f"{name} differs: {list(values)!r}")
    same("strategy_path", *(normalize_strategy_path(p) for p in
                            (deployment.strategy_path, bundle.strategy_path, *(c.strategy_path for c in cases))))
    same("class_name", deployment.class_name, bundle.class_name, *(c.class_name for c in cases))
    same("file_hash", deployment.strategy_digest, bundle.file_hash, *(c.file_hash for c in cases))
    if not all(same_values(dict(deployment.params), dict(p)) for p in (bundle.params, *(c.params for c in cases))):
        problems.append("params differ")
    same("conids", tuple(deployment.conids), bundle.conids, *(tuple(sorted(c.conids)) for c in cases))
    same("bar_size", *(bar_size_key(b) for b in (deployment.bar_size, bundle.bar_size or "",
                                                 *(c.bar_size for c in cases))))
    same("evidence_ref", deployment.evidence_ref, bundle_digest)
    same("order_notional", deployment.evidence_order_notional, bundle.order_notional)
    for case in cases:
        if case.artifact_id is not None:
            same("artifact_id", case.artifact_id, bundle.artifact_id)
            same("family_id", case.family_id, bundle.family_id)
    model, _, reviewed_id = bundle.reviewer.rpartition("#")
    if bundle.reviewer_kind != "llm" or not model or reviewed_id != initial_judgment_id:
        problems.append(f"the bundle review {bundle.reviewer!r} does not name judgment {initial_judgment_id}")
    if deployment.decider_verdict != "DEPLOY":
        problems.append("decider_verdict must be DEPLOY")
    return problems


class AiDeploymentRegistrar:
    def __init__(self, *, db: Any, deployments: Any, versions: Any, activity: Any, judgments: Any, cooldowns: Any,
                 bundles: Any, calendar: Any, expiry_sessions: int, now: Callable[[], dt.datetime]):
        self._db, self._deployments, self._versions, self._activity = db, deployments, versions, activity
        self._judgments, self._cooldowns, self._bundles = judgments, cooldowns, bundles
        self._calendar, self._expiry_sessions, self._now = calendar, expiry_sessions, now

    def register(self, body: Mapping[str, Any], *, principal: str, command_id: str) -> dict:
        judgment_id, bundle_digest, deployment = self._parse(body)
        digest_of_request = request_digest(body)
        judgment = self._deploy_judgment(judgment_id)
        bound = self._versions.bound_to_judgment(judgment_id)
        if bound is not None:                       # exact retry on a later day, or another body
            return self._existing_outcome(bound, digest_of_request)
        prior, initial = self._line(judgment, deployment)
        now = self._now()
        if self._bundles.manifest_artifact_id(bundle_digest) != initial.artifact_id:
            raise DeploymentRefused("JUDGMENT_MISMATCH", "the bundle names another evaluation's artifact")
        bundle = self._bundles.check(bundle_digest, artifact_id=initial.artifact_id, now=now)
        cases = (initial,) if prior is None else (initial, judgment)
        problems = binding_differences(deployment, bundle, cases, bundle_digest=bundle_digest,
                                       initial_judgment_id=initial.judgment_id)
        if problems:
            raise DeploymentRefused("JUDGMENT_MISMATCH", "; ".join(problems))
        if self._cooldowns.cooling_down(strategy_key(deployment.strategy_path, deployment.class_name), now):
            raise DeploymentRefused("FAMILY_COOLING_DOWN", "the strategy key is cooling down")
        first, expiry = self._sessions(now, bundle.expires_at)
        version = DeploymentVersion(base_digest=deployment_digest(deployment), judgment_id=judgment_id,
                                    kind=judgment.kind, prior_version=prior, first_session=first,
                                    expiry_session=expiry)
        sealed, withdrawn = self._versions.sealed(), self._versions.withdrawn()
        renewed = frozenset(s.version.prior_version for s in sealed if s.version.prior_version is not None)
        # Read the judgments before the transaction: their store has its own lock. Decided versions are skipped.
        stands = self._activity.stands_for(sealed, skip=withdrawn | renewed)

        def write(conn) -> dict:
            raced = self._versions.bound_in_tx(conn, judgment_id)
            if raced is not None:                   # another request sealed this judgment since the read above
                return self._existing_outcome(raced, digest_of_request)
            if prior is not None and prior in self._versions.withdrawn_in_tx(conn):
                raise DeploymentRefused("RENEWAL_PRIOR_INVALID", "the prior version was withdrawn")
            self._require_room_in_tx(conn, stands, today=ny_date(now), excluding=prior)
            self._deployments.register_in_tx(conn, deployment, principal=principal, command_id=command_id)
            digest, created = self._versions.seal_in_tx(conn, version, request_digest=digest_of_request,
                                                        principal=principal, command_id=command_id)
            return self._outcome(digest, version, created=created)
        return self._db.transaction(write)

    def _existing_outcome(self, bound: Any, digest_of_request: str) -> dict:
        if bound.request_digest != digest_of_request:
            raise DeploymentRefused("JUDGMENT_ALREADY_BOUND", f"judgment {bound.version.judgment_id} has a version")
        return self._outcome(bound.digest, bound.version, created=False)

    @staticmethod
    def _parse(body: Mapping[str, Any]) -> tuple[str, str, AiDeployment]:
        if not isinstance(body, Mapping) or set(body) != KEYS:
            raise DeploymentRefused("DEPLOYMENT_INVALID", f"registration has exactly the keys {sorted(KEYS)}")
        if not isinstance(body["judgment_id"], str) or not _JUDGMENT_ID.fullmatch(body["judgment_id"]):
            raise DeploymentRefused("DEPLOYMENT_INVALID", "judgment_id must match ^[A-Za-z0-9_.:-]{1,128}$")
        if not isinstance(body["bundle_digest"], str) or not _BUNDLE_DIGEST.fullmatch(body["bundle_digest"]):
            raise DeploymentRefused("DEPLOYMENT_INVALID", "bundle_digest must be sha256:<64 hex>")
        return body["judgment_id"], body["bundle_digest"], AiDeployment.from_json(body["deployment"])

    def _deploy_judgment(self, judgment_id: str) -> JudgmentFacts:
        judgment = self._judgments.get(judgment_id)
        if judgment is None:
            raise DeploymentRefused("JUDGMENT_MISSING", f"no durable judgment {judgment_id}")
        if judgment.verdict != "DEPLOY":
            raise DeploymentRefused("JUDGMENT_NOT_DEPLOY", f"judgment {judgment_id} is {judgment.verdict}")
        return judgment

    def _line(self, judgment: JudgmentFacts, deployment: AiDeployment) -> tuple[Optional[str], JudgmentFacts]:
        """(prior version digest, the INITIAL judgment of the line) — ruling 7."""
        if judgment.kind == INITIAL:
            return None, judgment
        prior = judgment.renews_version
        prior_version = self._sealed_version(prior)
        if prior_version.base_digest != deployment_digest(deployment):
            raise DeploymentRefused("RENEWAL_PRIOR_INVALID", "a renewal must keep the prior base deployment")
        if prior in self._versions.withdrawn():
            raise DeploymentRefused("RENEWAL_PRIOR_INVALID", "the prior version was withdrawn")
        if any(s.version.prior_version == prior for s in self._versions.sealed()):
            raise DeploymentRefused("RENEWAL_PRIOR_INVALID", "the prior version was already renewed")
        first = prior_version
        while first.prior_version is not None:
            first = self._sealed_version(first.prior_version)
        return prior, self._deploy_judgment(first.judgment_id)

    def _sealed_version(self, digest: Optional[str]) -> DeploymentVersion:
        try:
            return self._versions.get(digest)
        except DeploymentRefused as refused:
            if refused.code != "DEPLOYMENT_VERSION_UNKNOWN":
                raise                               # a tampered row must fail loudly
            raise DeploymentRefused("RENEWAL_PRIOR_INVALID", "the renewal names no sealed version") from None

    def _sessions(self, registered_at: dt.datetime, bundle_expires_at: dt.datetime) -> tuple[dt.date, dt.date]:
        try:
            return deployment_sessions(self._calendar, registered_at=registered_at, sessions=self._expiry_sessions,
                                       bundle_expires_at=bundle_expires_at)
        except DeploymentRefused:
            raise
        except Exception as ex:                     # the calendar could not serve (ValueError, DateOutOfBounds)
            raise DeploymentRefused(CALENDAR_UNAVAILABLE,
                                    f"the session calendar cannot serve: {type(ex).__name__}") from None

    def _require_room_in_tx(self, conn, stands, *, today: dt.date, excluding: Optional[str]) -> None:
        statuses = classify(self._versions.sealed_in_tx(conn), withdrawn=self._versions.withdrawn_in_tx(conn),
                            stands=stands, today=today, max_active=10 ** 6, unknown_stands=True)
        standing = [d for d, s in statuses.items() if s in (ACTIVE, NOT_STARTED) and d != excluding]
        if len(standing) >= self._activity.max_active:
            raise DeploymentRefused("DEPLOY_CAP_REACHED", f"{len(standing)} AI deployments already stand")

    @staticmethod
    def _outcome(digest: str, version: DeploymentVersion, *, created: bool) -> dict:
        return {"digest": version.base_digest, "version_digest": digest, "kind": version.kind,
                "first_session": version.first_session.isoformat(),
                "expiry_session": version.expiry_session.isoformat(), "created": created,
                "strategy_digest_provenance": STRATEGY_DIGEST_PROVENANCE}
