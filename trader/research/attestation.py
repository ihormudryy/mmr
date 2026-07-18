"""Signed eligibility attestations + production-safe verification (P2 Task 7).

An ``EligibilityAttestation`` (design §4.3) is the ONLY artifact that authorizes a
strategy to trade in a given account mode. It is minted OFFLINE by an operator who
holds the private signing key; the trader holds only the public verification key
and can verify but never mint (§4.3: "the trader has the verification key but no
signing key").

The invariants this module enforces structurally:

* **The qualitative review can never override the quantitative gate.**
  ``build_attestation`` derives ``eligibility_state`` from the ``EligibilityDecision``
  and sets ``permitted_account_mode = "paper"`` ONLY when the decision is
  ``PAPER_ELIGIBLE``. There is no argument by which a review flips a CANDIDATE
  decision into a paper-eligible attestation.
* **Any change to an authority field invalidates the signature.** The signature
  covers the canonical bytes of every field except ``signature`` itself
  (artifact/source/config/dataset/allowlist digests, boundaries, cost/capacity
  assumptions, ruleset identity, state, mode, allocation, instruments, timestamps,
  reason/evidence, the decision + review digests, AND the public-key id). Tamper
  with any of them and ``verify`` raises ``BadSignature``.
* **Verification fails closed.** ``AttestationVerifier.verify`` raises a specific
  ``AttestationError`` subclass for an unknown key, a bad signature, expiry,
  revocation, or a binding mismatch. It never returns a soft "maybe valid".
* **No private material is ever persisted.** The attestation and its DB row carry
  only the public-key id and the base64url signature.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, fields
from typing import Any, Mapping, Optional, Sequence

from trader.data.schema_migrations import SchemaMigrator
from trader.research import signing
from trader.research.canonical import canonical_json_bytes, sha256_digest
from trader.research.eligibility import (
    STATE_PAPER_ELIGIBLE,
    EligibilityDecision,
)
from trader.research.review import OperatorReview
from trader.research.signing import AttestationSigner

ATTESTATION_PAYLOAD_PREFIX = "eligibility_attestation"

# Account modes an attestation may permit. Only a PAPER_ELIGIBLE decision yields
# ``paper``; any non-passing decision yields ``none`` -- it authorizes nothing.
PERMITTED_MODE_PAPER = "paper"
PERMITTED_MODE_NONE = "none"

RESEARCH_MIGRATION_ATTESTATIONS = 9


# --------------------------------------------------------------------------- #
# The attestation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EligibilityAttestation:
    """The canonical, signed eligibility attestation (design §4.3).

    Every field except ``signature`` is covered by the signature. ``promoted_at``
    is optional (an attestation may not yet be promoted); everything else is
    required.
    """

    # digests binding the attestation to exact code / config / data / allowlist
    artifact_digest: str
    source_digest: str
    config_digest: str
    dataset_manifest_digest: str
    allowlist_digest: str
    # boundaries (opaque ISO/label strings -- what data window the evidence covers)
    training_boundary: str
    validation_boundary: str
    holdout_boundary: str
    evidence_boundary: str
    # assumptions the eligibility depends on
    cost_assumptions: Mapping[str, Any]
    capacity_assumptions: Mapping[str, Any]
    # ruleset identity (copied from the decision -- see build_attestation)
    ruleset_name: str
    ruleset_version: str
    ruleset_digest: str
    # authority
    eligibility_state: str
    permitted_account_mode: str
    max_gross_allocation: float
    permitted_instruments: tuple
    # timestamps
    created_at: dt.datetime
    expires_at: dt.datetime
    operator_approved_at: dt.datetime
    # provenance
    reason_codes: tuple
    evidence_refs: tuple
    eligibility_decision_digest: str
    review_digest: str
    # signing identity + the signature (the ONLY field not covered by itself)
    public_key_id: str
    signature: str
    # optional
    promoted_at: Optional[dt.datetime] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "permitted_instruments",
                           tuple(self.permitted_instruments))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "cost_assumptions", dict(self.cost_assumptions))
        object.__setattr__(self, "capacity_assumptions",
                           dict(self.capacity_assumptions))

    @property
    def payload_digest(self) -> str:
        return payload_digest(self)

    @property
    def unsigned_payload(self) -> dict:
        return unsigned_payload(self)


# --------------------------------------------------------------------------- #
# Canonical unsigned payload -- the exact bytes we sign / verify / digest over
# --------------------------------------------------------------------------- #
_SIGNATURE_FIELD = "signature"


def unsigned_payload(attestation_or_fields: Any) -> dict:
    """Every attestation field EXCEPT ``signature`` as a plain dict.

    Accepts either an ``EligibilityAttestation`` or a fields dict (as
    ``build_attestation`` returns) so the exact same bytes are produced whether
    you are about to sign or are verifying a persisted attestation.
    """
    if isinstance(attestation_or_fields, EligibilityAttestation):
        payload = {f.name: getattr(attestation_or_fields, f.name)
                   for f in fields(EligibilityAttestation)}
    else:
        payload = dict(attestation_or_fields)
    payload.pop(_SIGNATURE_FIELD, None)
    return payload


def attestation_payload_bytes(attestation_or_fields: Any) -> bytes:
    """Deterministic canonical-JSON bytes of the unsigned payload (what we sign)."""
    return canonical_json_bytes(unsigned_payload(attestation_or_fields))


def payload_digest(attestation_or_fields: Any) -> str:
    """Namespaced SHA-256 of the unsigned payload -- the attestation's stable id
    (used as the persistence PK and the revocation key)."""
    return sha256_digest(ATTESTATION_PAYLOAD_PREFIX,
                         unsigned_payload(attestation_or_fields))


# --------------------------------------------------------------------------- #
# Build (state DERIVED from the quantitative decision) + sign
# --------------------------------------------------------------------------- #
def build_attestation(
    *,
    decision: EligibilityDecision,
    review: OperatorReview,
    public_key_id: str,
    artifact_digest: str,
    source_digest: str,
    config_digest: str,
    dataset_manifest_digest: str,
    allowlist_digest: str,
    training_boundary: str,
    validation_boundary: str,
    holdout_boundary: str,
    evidence_boundary: str,
    cost_assumptions: Mapping[str, Any],
    capacity_assumptions: Mapping[str, Any],
    max_gross_allocation: float,
    permitted_instruments: Sequence[Any],
    created_at: dt.datetime,
    expires_at: dt.datetime,
    operator_approved_at: dt.datetime,
    reason_codes: Optional[Sequence[str]] = None,
    evidence_refs: Optional[Sequence[str]] = None,
    promoted_at: Optional[dt.datetime] = None,
) -> dict:
    """Assemble the UNSIGNED attestation fields (a dict) from a quantitative
    decision + a qualitative review.

    STRUCTURAL invariant (design §8.5): the eligibility state and permitted mode
    are functions of ``decision`` ALONE. ``eligibility_state = decision.state`` and
    ``permitted_account_mode`` is ``paper`` iff the decision is ``PAPER_ELIGIBLE``,
    else ``none``. A non-passing decision therefore cannot ever produce a
    paper-authorizing attestation, no matter what the review says. The ruleset
    identity and the decision/review digests are bound in so the attestation is
    inseparable from the exact quantitative evidence and reviewed text.
    """
    state = decision.state
    permitted_account_mode = (PERMITTED_MODE_PAPER if state == STATE_PAPER_ELIGIBLE
                              else PERMITTED_MODE_NONE)
    if reason_codes is None:
        # Default reason codes explain the state: the failing gate codes for a
        # CANDIDATE, or a single positive marker for a paper-eligible decision.
        if decision.passed:
            reason_codes = ("paper_eligible",)
        else:
            reason_codes = tuple(sorted(r.code for r in decision.failures))
    if evidence_refs is None:
        evidence_refs = tuple(sorted(set(decision.evidence_refs)))

    return {
        "artifact_digest": artifact_digest,
        "source_digest": source_digest,
        "config_digest": config_digest,
        "dataset_manifest_digest": dataset_manifest_digest,
        "allowlist_digest": allowlist_digest,
        "training_boundary": training_boundary,
        "validation_boundary": validation_boundary,
        "holdout_boundary": holdout_boundary,
        "evidence_boundary": evidence_boundary,
        "cost_assumptions": dict(cost_assumptions),
        "capacity_assumptions": dict(capacity_assumptions),
        "ruleset_name": decision.ruleset_name,
        "ruleset_version": decision.ruleset_version,
        "ruleset_digest": decision.ruleset_digest,
        "eligibility_state": state,
        "permitted_account_mode": permitted_account_mode,
        "max_gross_allocation": float(max_gross_allocation),
        "permitted_instruments": tuple(permitted_instruments),
        "created_at": created_at,
        "expires_at": expires_at,
        "operator_approved_at": operator_approved_at,
        "reason_codes": tuple(reason_codes),
        "evidence_refs": tuple(evidence_refs),
        "eligibility_decision_digest": decision.digest,
        "review_digest": review.digest,
        "public_key_id": public_key_id,
        "promoted_at": promoted_at,
    }


def _attestation_signer_sign(self: AttestationSigner,
                             unsigned_fields: Mapping[str, Any]
                             ) -> EligibilityAttestation:
    """Ed25519-sign the unsigned attestation fields and return the signed
    ``EligibilityAttestation``. Offline; deterministic.

    ``unsigned_fields`` must already carry ``public_key_id == self.public_key_id``
    (as ``build_attestation`` produces when called with the signer's id) -- the
    key id is part of the signed payload, so it must match the key doing the
    signing. A mismatch fails loud rather than minting an attestation whose
    recorded key id does not match its signature.
    """
    fields_dict = dict(unsigned_fields)
    declared = fields_dict.get("public_key_id")
    if declared != self.public_key_id:
        raise ValueError(
            f"attestation public_key_id {declared!r} does not match signing key "
            f"{self.public_key_id!r}")
    signature = self.sign_message(attestation_payload_bytes(fields_dict))
    return EligibilityAttestation(**fields_dict, signature=signature)


# Attach the domain-level signer method here so signing.py (the security core)
# stays free of any attestation-domain import.
AttestationSigner.sign = _attestation_signer_sign  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Verification -- fail closed
# --------------------------------------------------------------------------- #
class AttestationError(Exception):
    """Base class for every attestation verification failure (fail closed)."""


class UnknownKey(AttestationError):
    """The attestation is signed by a key id the verifier does not trust."""


class BadSignature(AttestationError):
    """The signature does not verify -- a wrong key OR any tampered field."""


class Expired(AttestationError):
    """``now >= expires_at``: the attestation is no longer valid."""


class Revoked(AttestationError):
    """The attestation's payload digest is in the revocation set."""


class BindingMismatch(AttestationError):
    """A validly-signed attestation does not match the caller's expected bindings
    (artifact / allowlist / ruleset / mode / allocation / instruments)."""


@dataclass(frozen=True)
class ExpectedBindings:
    """What the verifying party independently expects the attestation to authorize.

    The verifier confirms the (signed) attestation matches every one of these; a
    mismatch means the attestation -- though genuinely signed -- is not the one
    this deployment intends to run, and is rejected.
    """

    artifact_digest: str
    allowlist_digest: str
    ruleset_digest: str
    account_mode: str
    max_gross_allocation: float
    permitted_instruments: tuple

    def __post_init__(self) -> None:
        object.__setattr__(self, "permitted_instruments",
                           tuple(self.permitted_instruments))


@dataclass(frozen=True)
class VerifiedEligibility:
    """The trusted facts a successful verification returns. Only produced after
    the signature, expiry, revocation, and binding checks all pass."""

    eligibility_state: str
    permitted_account_mode: str
    max_gross_allocation: float
    permitted_instruments: tuple
    artifact_digest: str
    allowlist_digest: str
    ruleset_digest: str
    expires_at: dt.datetime
    public_key_id: str
    payload_digest: str


class AttestationVerifier:
    """Verifies attestations against a fixed trust store of public keys.

    Construct with one or more trusted Ed25519 public keys; the verifier indexes
    them by ``public_key_id`` so an attestation naming an untrusted key is
    rejected outright. Holds NO private material and can never mint an
    attestation -- it is safe to run inside the trader service.
    """

    def __init__(self, trusted_public_keys: Sequence[Any]):
        keys: dict[str, Any] = {}
        for pk in trusted_public_keys:
            keys[signing.public_key_id(pk)] = pk
        if not keys:
            raise ValueError("AttestationVerifier requires at least one trusted key")
        self._keys = keys

    @property
    def trusted_key_ids(self) -> frozenset:
        return frozenset(self._keys)

    def verify(self, attestation: EligibilityAttestation,
               expected: ExpectedBindings, *, now: dt.datetime,
               revoked_digests: frozenset = frozenset()) -> VerifiedEligibility:
        """Verify ``attestation`` and return the trusted facts, or raise.

        Order (each a fail-closed gate): (1) the signing key is trusted, (2) the
        signature verifies over the canonical unsigned payload -- this is what
        makes any tampered authority field a ``BadSignature``; only past this
        point are the attestation's fields trusted -- (3) not expired, (4) not
        revoked, (5) every expected binding matches.
        """
        public_key = self._keys.get(attestation.public_key_id)
        if public_key is None:
            raise UnknownKey(
                f"attestation signed by untrusted key id {attestation.public_key_id!r}")

        payload = unsigned_payload(attestation)
        try:
            signing.verify_bytes(public_key, canonical_json_bytes(payload),
                                 attestation.signature)
        except signing.BadSignature as exc:
            raise BadSignature("attestation signature does not verify") from exc

        # From here the attestation's fields are cryptographically trusted.
        if now >= attestation.expires_at:
            raise Expired(
                f"attestation expired at {attestation.expires_at.isoformat()} "
                f"(now {now.isoformat()})")

        pdigest = sha256_digest(ATTESTATION_PAYLOAD_PREFIX, payload)
        if pdigest in revoked_digests:
            raise Revoked(f"attestation {pdigest} is revoked")

        mismatches = []
        if attestation.artifact_digest != expected.artifact_digest:
            mismatches.append("artifact_digest")
        if attestation.allowlist_digest != expected.allowlist_digest:
            mismatches.append("allowlist_digest")
        if attestation.ruleset_digest != expected.ruleset_digest:
            mismatches.append("ruleset_digest")
        if attestation.permitted_account_mode != expected.account_mode:
            mismatches.append("permitted_account_mode")
        if attestation.max_gross_allocation != expected.max_gross_allocation:
            mismatches.append("max_gross_allocation")
        if tuple(attestation.permitted_instruments) != tuple(expected.permitted_instruments):
            mismatches.append("permitted_instruments")
        if mismatches:
            raise BindingMismatch(
                f"attestation does not match expected bindings: {', '.join(mismatches)}")

        return VerifiedEligibility(
            eligibility_state=attestation.eligibility_state,
            permitted_account_mode=attestation.permitted_account_mode,
            max_gross_allocation=attestation.max_gross_allocation,
            permitted_instruments=tuple(attestation.permitted_instruments),
            artifact_digest=attestation.artifact_digest,
            allowlist_digest=attestation.allowlist_digest,
            ruleset_digest=attestation.ruleset_digest,
            expires_at=attestation.expires_at,
            public_key_id=attestation.public_key_id,
            payload_digest=pdigest)


# --------------------------------------------------------------------------- #
# Persistence (research migration 9) -- public material ONLY
# --------------------------------------------------------------------------- #
_ATTESTATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS eligibility_attestations (
        payload_digest VARCHAR PRIMARY KEY,
        artifact_digest VARCHAR NOT NULL,
        source_digest VARCHAR NOT NULL,
        config_digest VARCHAR NOT NULL,
        dataset_manifest_digest VARCHAR NOT NULL,
        allowlist_digest VARCHAR NOT NULL,
        training_boundary VARCHAR NOT NULL,
        validation_boundary VARCHAR NOT NULL,
        holdout_boundary VARCHAR NOT NULL,
        evidence_boundary VARCHAR NOT NULL,
        cost_assumptions VARCHAR NOT NULL,
        capacity_assumptions VARCHAR NOT NULL,
        ruleset_name VARCHAR NOT NULL,
        ruleset_version VARCHAR NOT NULL,
        ruleset_digest VARCHAR NOT NULL,
        eligibility_state VARCHAR NOT NULL,
        permitted_account_mode VARCHAR NOT NULL,
        max_gross_allocation DOUBLE NOT NULL,
        permitted_instruments VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        operator_approved_at TIMESTAMPTZ NOT NULL,
        promoted_at TIMESTAMPTZ,
        reason_codes VARCHAR NOT NULL,
        evidence_refs VARCHAR NOT NULL,
        eligibility_decision_digest VARCHAR NOT NULL,
        review_digest VARCHAR NOT NULL,
        public_key_id VARCHAR NOT NULL,
        signature VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attestation_revocations (
        payload_digest VARCHAR PRIMARY KEY,
        reason VARCHAR NOT NULL,
        revoked_at TIMESTAMPTZ NOT NULL
    )
    """,
)


def apply_attestation_migrations(migrator: SchemaMigrator) -> None:
    """Research DB migration 9 (idempotent): eligibility-attestation +
    revocation tables in the SEPARATE offline research DuckDB. These store ONLY
    public material (public-key id + base64url signature); no private key bytes
    ever touch this schema."""
    migrator.apply(version=RESEARCH_MIGRATION_ATTESTATIONS,
                   name="research_eligibility_attestations",
                   statements=list(_ATTESTATION_STATEMENTS))


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


class AttestationRepository:
    """Append-only, digest-keyed store for signed attestations + a revocation
    ledger.

    ``record`` is idempotent by ``payload_digest``. There is deliberately no
    update/delete of an attestation -- it is a content-addressed fact; the only
    lifecycle mutation is ``revoke`` (append to the revocation ledger). The store
    holds ONLY public material.
    """

    def __init__(self, db: Any):
        self._db = db

    def record(self, attestation: EligibilityAttestation) -> str:
        digest = attestation.payload_digest

        def _tx(conn):
            if conn.execute(
                    "SELECT 1 FROM eligibility_attestations WHERE payload_digest = ?",
                    [digest]).fetchone() is not None:
                return digest  # idempotent: content-addressed, already recorded
            conn.execute(
                "INSERT INTO eligibility_attestations (payload_digest, artifact_digest, "
                "source_digest, config_digest, dataset_manifest_digest, allowlist_digest, "
                "training_boundary, validation_boundary, holdout_boundary, evidence_boundary, "
                "cost_assumptions, capacity_assumptions, ruleset_name, ruleset_version, "
                "ruleset_digest, eligibility_state, permitted_account_mode, max_gross_allocation, "
                "permitted_instruments, created_at, expires_at, operator_approved_at, promoted_at, "
                "reason_codes, evidence_refs, eligibility_decision_digest, review_digest, "
                "public_key_id, signature) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [digest, attestation.artifact_digest, attestation.source_digest,
                 attestation.config_digest, attestation.dataset_manifest_digest,
                 attestation.allowlist_digest, attestation.training_boundary,
                 attestation.validation_boundary, attestation.holdout_boundary,
                 attestation.evidence_boundary, json.dumps(dict(attestation.cost_assumptions)),
                 json.dumps(dict(attestation.capacity_assumptions)), attestation.ruleset_name,
                 attestation.ruleset_version, attestation.ruleset_digest,
                 attestation.eligibility_state, attestation.permitted_account_mode,
                 float(attestation.max_gross_allocation),
                 json.dumps(list(attestation.permitted_instruments)), attestation.created_at,
                 attestation.expires_at, attestation.operator_approved_at,
                 attestation.promoted_at, json.dumps(list(attestation.reason_codes)),
                 json.dumps(list(attestation.evidence_refs)),
                 attestation.eligibility_decision_digest, attestation.review_digest,
                 attestation.public_key_id, attestation.signature])
            return digest

        return self._db.transaction(_tx)

    def get(self, payload_digest_value: str) -> Optional[EligibilityAttestation]:
        def _tx(conn):
            r = conn.execute(
                "SELECT artifact_digest, source_digest, config_digest, dataset_manifest_digest, "
                "allowlist_digest, training_boundary, validation_boundary, holdout_boundary, "
                "evidence_boundary, cost_assumptions, capacity_assumptions, ruleset_name, "
                "ruleset_version, ruleset_digest, eligibility_state, permitted_account_mode, "
                "max_gross_allocation, permitted_instruments, created_at, expires_at, "
                "operator_approved_at, promoted_at, reason_codes, evidence_refs, "
                "eligibility_decision_digest, review_digest, public_key_id, signature "
                "FROM eligibility_attestations WHERE payload_digest = ?",
                [payload_digest_value]).fetchone()
            if r is None:
                return None
            attestation = EligibilityAttestation(
                artifact_digest=r[0], source_digest=r[1], config_digest=r[2],
                dataset_manifest_digest=r[3], allowlist_digest=r[4], training_boundary=r[5],
                validation_boundary=r[6], holdout_boundary=r[7], evidence_boundary=r[8],
                cost_assumptions=json.loads(r[9]), capacity_assumptions=json.loads(r[10]),
                ruleset_name=r[11], ruleset_version=r[12], ruleset_digest=r[13],
                eligibility_state=r[14], permitted_account_mode=r[15],
                max_gross_allocation=float(r[16]),
                permitted_instruments=tuple(json.loads(r[17])), created_at=_as_utc(r[18]),
                expires_at=_as_utc(r[19]), operator_approved_at=_as_utc(r[20]),
                promoted_at=_as_utc(r[21]), reason_codes=tuple(json.loads(r[22])),
                evidence_refs=tuple(json.loads(r[23])), eligibility_decision_digest=r[24],
                review_digest=r[25], public_key_id=r[26], signature=r[27])
            if attestation.payload_digest != payload_digest_value:
                raise AttestationError(
                    f"stored attestation {payload_digest_value!r} no longer matches its "
                    f"content digest {attestation.payload_digest!r} (corruption)")
            return attestation

        return self._db.transaction(_tx)

    def revoke(self, payload_digest_value: str, *, reason: str,
               revoked_at: dt.datetime) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("revocation reason must be a non-empty string")

        def _tx(conn):
            if conn.execute(
                    "SELECT 1 FROM eligibility_attestations WHERE payload_digest = ?",
                    [payload_digest_value]).fetchone() is None:
                raise AttestationError(
                    f"cannot revoke unknown attestation {payload_digest_value!r}")
            if conn.execute(
                    "SELECT 1 FROM attestation_revocations WHERE payload_digest = ?",
                    [payload_digest_value]).fetchone() is not None:
                return  # idempotent: already revoked
            conn.execute(
                "INSERT INTO attestation_revocations (payload_digest, reason, revoked_at) "
                "VALUES (?, ?, ?)", [payload_digest_value, reason, revoked_at])

        return self._db.transaction(_tx)

    def revoked_digests(self) -> frozenset:
        def _tx(conn):
            rows = conn.execute(
                "SELECT payload_digest FROM attestation_revocations").fetchall()
            return frozenset(r[0] for r in rows)

        return self._db.transaction(_tx)
