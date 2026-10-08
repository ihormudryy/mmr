"""Which deployment versions may enter now (SP2c spec 5.2 items 5 and 8).

Status is computed on every read from the sealed versions, the withdrawals,
the judgments (Plan 1), the New York date and the cap. Session dates are stored on the version; the
calendar is used only when a version is sealed. Nothing is cached.
"""
from __future__ import annotations

import datetime as dt
import hmac
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from trader.automation.ai_deployments import STRATEGY_KIND, AiDeployment, DeploymentRefused
from trader.automation.ai_judgment_port import strategy_key

logger = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
ACTIVE, NOT_STARTED, EXPIRED = "ACTIVE", "NOT_STARTED", "EXPIRED"
WITHDRAWN, SUPERSEDED, JUDGMENT_ENDED, OVER_CAP = "WITHDRAWN", "SUPERSEDED", "JUDGMENT_ENDED", "OVER_CAP"
WIRE_STATE = {ACTIVE: "ACTIVE", NOT_STARTED: "ACTIVE", OVER_CAP: "ACTIVE", EXPIRED: "EXPIRED",
              WITHDRAWN: "WITHDRAWN", SUPERSEDED: "ENDED", JUDGMENT_ENDED: "ENDED"}


def ny_date(moment: dt.datetime) -> dt.date:
    if not isinstance(moment, dt.datetime) or moment.utcoffset() is None:
        raise ValueError("an aware datetime is required")
    return moment.astimezone(NEW_YORK).date()


def deployment_sessions(calendar: Any, *, registered_at: dt.datetime, sessions: int,
                        bundle_expires_at: dt.datetime) -> tuple[dt.date, dt.date]:
    """Ruling 5: from the first session after the registration day, ``sessions`` sessions inclusive,
    never past the last session before the bundle's New York expiry date."""
    if type(sessions) is not int or sessions < 1:
        raise ValueError("sessions must be an integer >= 1")
    start = ny_date(registered_at) + dt.timedelta(days=1)
    days = calendar.sessions_in_range(start, start + dt.timedelta(days=sessions * 2 + 14))
    if len(days) < sessions:
        raise ValueError(f"the calendar has fewer than {sessions} sessions after {start}")
    last_allowed = ny_date(bundle_expires_at) - dt.timedelta(days=1)
    allowed = [day for day in days[:sessions] if day <= last_allowed]
    if not allowed:
        raise DeploymentRefused("BUNDLE_EXPIRED", f"the bundle expires before the first session {days[0]}")
    return days[0], allowed[-1]


def classify(sealed, *, withdrawn: frozenset[str], stands: Mapping[str, bool], today: dt.date,
             max_active: int, unknown_stands: bool = False) -> dict[str, str]:
    """Ruling 6. ``unknown_stands`` decides a version whose judgment was not read yet (registration counts
    it as standing, so a race can only refuse, never exceed the cap)."""
    renewed = {s.version.prior_version for s in sealed if s.version.prior_version is not None}
    status: dict[str, str] = {}
    standing = []
    for s in sealed:
        if s.digest in withdrawn:
            status[s.digest] = WITHDRAWN
        elif s.digest in renewed:
            status[s.digest] = SUPERSEDED
        elif not stands.get(s.digest, unknown_stands):
            status[s.digest] = JUDGMENT_ENDED
        elif today > s.version.expiry_session:
            status[s.digest] = EXPIRED
        else:
            standing.append(s)
    standing.sort(key=lambda s: (s.version.first_session, s.sealed_at, s.digest))
    for rank, s in enumerate(standing):
        if rank >= max_active:
            status[s.digest] = OVER_CAP
        else:
            status[s.digest] = ACTIVE if today >= s.version.first_session else NOT_STARTED
    return status


def _same_digest(claimed: Any, sealed: str) -> bool:
    """The claim comes from a request body: any non-string, or non-ASCII text, is a mismatch."""
    if not isinstance(claimed, str) or not claimed.isascii():
        return False
    return hmac.compare_digest(claimed.encode("ascii"), sealed.encode("ascii"))


@dataclass(frozen=True)
class ActiveDeployment:
    version_digest: str
    version: Any            # DeploymentVersion
    deployment: AiDeployment


class DeploymentActivity:
    def __init__(self, *, versions: Any, deployments: Any, judgments: Any, cooldowns: Any, max_active: int,
                 now: Callable[[], dt.datetime]):
        if type(max_active) is not int or max_active < 0:
            raise ValueError("max_active must be an integer >= 0")
        self._versions, self._deployments = versions, deployments
        self._judgments, self._cooldowns = judgments, cooldowns
        self._max_active, self._now = max_active, now

    @property
    def max_active(self) -> int:
        return self._max_active

    def stands_for(self, sealed, *, skip: frozenset[str] = frozenset()) -> dict[str, bool]:
        """Read outside any journal transaction: the judgment store has its own lock.

        A digest in ``skip`` is not read and has no entry; the caller has already decided its status."""
        result = {}
        for s in sealed:
            if s.digest in skip:
                continue
            facts = self._judgments.get(s.version.judgment_id)
            result[s.digest] = (facts is not None and facts.verdict == "DEPLOY"
                                and all(v == "DEPLOY" for v in self._judgments.renewal_verdicts(s.digest)))
        return result

    def statuses(self) -> dict[str, str]:
        sealed = self._versions.sealed()
        withdrawn = self._versions.withdrawn()
        renewed = frozenset(s.version.prior_version for s in sealed if s.version.prior_version is not None)
        # Withdrawn and superseded rank above JUDGMENT_ENDED, so an unreadable old judgment cannot change them.
        stands = self.stands_for(sealed, skip=withdrawn | renewed)
        return classify(sealed, withdrawn=withdrawn, stands=stands, today=ny_date(self._now()), max_active=self._max_active)

    def status(self, digest: str) -> str:
        found = self.statuses().get(digest)
        if found is None:
            raise DeploymentRefused("DEPLOYMENT_VERSION_UNKNOWN", "no sealed version has this digest")
        return found

    def active(self) -> tuple[ActiveDeployment, ...]:
        chosen = []
        for digest, status in sorted(self.statuses().items()):
            if status == ACTIVE:
                version = self._versions.get(digest)
                chosen.append(ActiveDeployment(digest, version, self._deployments.get_sealed(version.base_digest)))
        return tuple(chosen)

    def entry_refusal(self, *, deployment_digest: str, version_digest: Optional[str],
                      source_digest: Optional[str]) -> Optional[str]:
        if version_digest is None or source_digest is None:
            return "DEPLOYMENT_VERSION_REQUIRED"
        try:
            version = self._versions.get(version_digest)
            status = self.status(version_digest)
        except DeploymentRefused as refused:
            if refused.code != "DEPLOYMENT_VERSION_UNKNOWN":
                raise                       # a tampered row must fail loudly, not look like "not active"
            return "DEPLOYMENT_NOT_ACTIVE"
        if version.base_digest != deployment_digest:
            return "DEPLOYMENT_NOT_ACTIVE"
        if status == EXPIRED:
            return "DEPLOYMENT_EXPIRED"
        if status != ACTIVE:
            return "DEPLOYMENT_NOT_ACTIVE"
        deployment = self._deployments.get_sealed(deployment_digest)
        if self._cooldowns.cooling_down(strategy_key(deployment.strategy_path, deployment.class_name), self._now()):
            return "FAMILY_COOLING_DOWN"
        if not _same_digest(source_digest, deployment.strategy_digest):
            return "STRATEGY_SOURCE_MISMATCH"
        return None


def deployment_version_gate(*, kind_of: Callable[[str], str], activity: DeploymentActivity):
    """Spec 5.2 item 8 at final dispatch, inside the saga's entry lock: journal reads only, no IB."""
    from trader.automation.ai_paper_evidence import AI_PAPER_ACTION

    def gate(request: Any, approval: Any, quote: Any, now: dt.datetime) -> Optional[str]:
        body = getattr(request, "body", None) or {}
        if getattr(request, "action", None) != AI_PAPER_ACTION or body.get("action") != "ENTER":
            return None
        digest = body.get("deployment_digest")
        if digest is None or kind_of(digest) != STRATEGY_KIND:
            return None                       # discretionary: the scope gate owns it
        try:
            return activity.entry_refusal(deployment_digest=digest, version_digest=body.get("deployment_version"),
                                          source_digest=body.get("source_digest"))
        except Exception as ex:
            # The guard maps any gate exception to a bare code; this log is the only place the cause survives.
            logger.error("deployment version gate failed at dispatch for %s: %s code=%s", body.get("decision_id"),
                         type(ex).__name__, getattr(ex, "code", None), exc_info=True)
            raise
    return gate
