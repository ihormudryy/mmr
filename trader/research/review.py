"""Mandatory qualitative operator review (P2 Task 7, design §8.5).

Before a strategy artifact can be attested ``PAPER_ELIGIBLE``, an operator must
sign off on a review covering the nine §8.5 concerns -- economic rationale, why
the edge survives costs, known failure regimes, data/survivorship limits,
parameter sensitivity, operational dependencies, capacity/decay, implausible
episode dominance, and confirmation the holdout was opened exactly once.

Two invariants make this review trustworthy rather than a rubber stamp:

* **It is mandatory and complete.** ``OperatorReview.__post_init__`` fails loud if
  ANY narrative field is blank, and requires ``holdout_opened_once_confirmed`` to
  be exactly ``True`` -- a review that cannot confirm the single holdout open is
  structurally invalid and cannot be recorded.
* **It can never override the quantitative gate.** This review is an INPUT to an
  attestation, not a decision: the attestation derives its eligibility state from
  the quantitative ``EligibilityDecision`` alone (see ``attestation.py``). The
  review is bound in by digest for audit, but it holds no state-flipping field.

The review is content-addressed (``digest``) so the exact reviewed text is pinned
into the attestation; editing a single character changes the digest and breaks
the binding. Persistence (research migration 8) is append-only: no update/delete.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, fields
from typing import Any, Optional

from trader.data.schema_migrations import SchemaMigrator
from trader.research.canonical import sha256_digest

REVIEW_DIGEST_PREFIX = "operator_review"
RESEARCH_MIGRATION_REVIEWS = 8

# Every narrative field that must be a non-empty, non-blank string. Kept as an
# explicit tuple so the validation and the persisted schema can never drift.
_NARRATIVE_FIELDS = (
    "artifact_id",
    "eligibility_decision_digest",
    "reviewer",
    "economic_rationale",
    "edge_survives_costs",
    "known_failure_regimes",
    "data_and_survivorship_limits",
    "parameter_sensitivity",
    "operational_dependencies",
    "capacity_and_decay",
    "episode_dominance",
)


@dataclass(frozen=True)
class OperatorReview:
    """The complete §8.5 qualitative review an operator signs off on.

    All narrative fields are required, non-empty strings; ``reviewed_at`` must be
    a timezone-aware datetime; and ``holdout_opened_once_confirmed`` must be
    exactly ``True``. Anything else fails loud at construction.
    """

    # linkage
    artifact_id: str
    eligibility_decision_digest: str
    reviewer: str
    reviewed_at: dt.datetime
    # §8.5 mandatory narrative
    economic_rationale: str
    edge_survives_costs: str
    known_failure_regimes: str
    data_and_survivorship_limits: str
    parameter_sensitivity: str
    operational_dependencies: str
    capacity_and_decay: str
    episode_dominance: str
    # §8.5 the one non-narrative confirmation
    holdout_opened_once_confirmed: bool

    def __post_init__(self) -> None:
        for name in _NARRATIVE_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"OperatorReview.{name} is mandatory and must be a non-empty "
                    f"narrative string")
        if not isinstance(self.reviewed_at, dt.datetime) or \
                self.reviewed_at.tzinfo is None or \
                self.reviewed_at.utcoffset() is None:
            raise ValueError(
                "OperatorReview.reviewed_at must be a timezone-aware datetime")
        # Exactly True -- a review that cannot confirm the single holdout open is
        # invalid (False, None, or any truthy-but-not-True value all fail).
        if self.holdout_opened_once_confirmed is not True:
            raise ValueError(
                "OperatorReview.holdout_opened_once_confirmed must be exactly "
                "True; a review that cannot confirm the holdout was opened once "
                "is not a valid review")

    def _body(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "eligibility_decision_digest": self.eligibility_decision_digest,
            "reviewer": self.reviewer,
            "reviewed_at": self.reviewed_at,
            "economic_rationale": self.economic_rationale,
            "edge_survives_costs": self.edge_survives_costs,
            "known_failure_regimes": self.known_failure_regimes,
            "data_and_survivorship_limits": self.data_and_survivorship_limits,
            "parameter_sensitivity": self.parameter_sensitivity,
            "operational_dependencies": self.operational_dependencies,
            "capacity_and_decay": self.capacity_and_decay,
            "episode_dominance": self.episode_dominance,
            "holdout_opened_once_confirmed": self.holdout_opened_once_confirmed,
        }

    @property
    def digest(self) -> str:
        """Content digest over the full reviewed body (deterministic)."""
        return sha256_digest(REVIEW_DIGEST_PREFIX, self._body())


# --------------------------------------------------------------------------- #
# Persistence (research migration 8)
# --------------------------------------------------------------------------- #
_REVIEW_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS operator_reviews (
        review_digest VARCHAR PRIMARY KEY,
        artifact_id VARCHAR NOT NULL,
        eligibility_decision_digest VARCHAR NOT NULL,
        reviewer VARCHAR NOT NULL,
        reviewed_at TIMESTAMPTZ NOT NULL,
        economic_rationale VARCHAR NOT NULL,
        edge_survives_costs VARCHAR NOT NULL,
        known_failure_regimes VARCHAR NOT NULL,
        data_and_survivorship_limits VARCHAR NOT NULL,
        parameter_sensitivity VARCHAR NOT NULL,
        operational_dependencies VARCHAR NOT NULL,
        capacity_and_decay VARCHAR NOT NULL,
        episode_dominance VARCHAR NOT NULL,
        holdout_opened_once_confirmed BOOLEAN NOT NULL
    )
    """,
)


def apply_review_migrations(migrator: SchemaMigrator) -> None:
    """Research DB migration 8 (idempotent): the append-only operator-review
    table in the SEPARATE offline research DuckDB."""
    migrator.apply(version=RESEARCH_MIGRATION_REVIEWS,
                   name="research_operator_reviews",
                   statements=list(_REVIEW_STATEMENTS))


class OperatorReviewRepository:
    """Append-only, digest-keyed store for operator reviews.

    ``record`` is idempotent by ``review_digest`` (content-addressed). There is
    deliberately no update/delete API -- a review is a signed-off fact.
    """

    def __init__(self, db: Any):
        self._db = db

    def record(self, review: OperatorReview) -> str:
        digest = review.digest

        def _tx(conn):
            if conn.execute(
                    "SELECT 1 FROM operator_reviews WHERE review_digest = ?",
                    [digest]).fetchone() is not None:
                return digest  # idempotent: content-addressed, already recorded
            conn.execute(
                "INSERT INTO operator_reviews (review_digest, artifact_id, "
                "eligibility_decision_digest, reviewer, reviewed_at, economic_rationale, "
                "edge_survives_costs, known_failure_regimes, data_and_survivorship_limits, "
                "parameter_sensitivity, operational_dependencies, capacity_and_decay, "
                "episode_dominance, holdout_opened_once_confirmed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [digest, review.artifact_id, review.eligibility_decision_digest,
                 review.reviewer, review.reviewed_at, review.economic_rationale,
                 review.edge_survives_costs, review.known_failure_regimes,
                 review.data_and_survivorship_limits, review.parameter_sensitivity,
                 review.operational_dependencies, review.capacity_and_decay,
                 review.episode_dominance, review.holdout_opened_once_confirmed])
            return digest

        return self._db.transaction(_tx)

    def get(self, review_digest: str) -> Optional[OperatorReview]:
        def _tx(conn):
            r = conn.execute(
                "SELECT artifact_id, eligibility_decision_digest, reviewer, reviewed_at, "
                "economic_rationale, edge_survives_costs, known_failure_regimes, "
                "data_and_survivorship_limits, parameter_sensitivity, operational_dependencies, "
                "capacity_and_decay, episode_dominance, holdout_opened_once_confirmed "
                "FROM operator_reviews WHERE review_digest = ?",
                [review_digest]).fetchone()
            if r is None:
                return None
            reviewed_at = r[3]
            if reviewed_at.tzinfo is None:
                reviewed_at = reviewed_at.replace(tzinfo=dt.timezone.utc)
            return OperatorReview(
                artifact_id=r[0], eligibility_decision_digest=r[1], reviewer=r[2],
                reviewed_at=reviewed_at, economic_rationale=r[4], edge_survives_costs=r[5],
                known_failure_regimes=r[6], data_and_survivorship_limits=r[7],
                parameter_sensitivity=r[8], operational_dependencies=r[9],
                capacity_and_decay=r[10], episode_dominance=r[11],
                holdout_opened_once_confirmed=bool(r[12]))

        return self._db.transaction(_tx)


# A convenience for callers/tests that want the mandatory field names.
NARRATIVE_FIELDS = _NARRATIVE_FIELDS
ALL_REVIEW_FIELDS = tuple(f.name for f in fields(OperatorReview))
