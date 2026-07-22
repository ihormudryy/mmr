"""P4 Task 5 -- the signed live-canary activation authority.

A ``CanaryAttestation`` is a SEPARATE, DISTINCT signed artifact from the P2
``EligibilityAttestation`` (``trader.research.attestation``). Where the P2
attestation authorizes a strategy to run in PAPER mode off a research
decision + qualitative review, a ``CanaryAttestation`` authorizes exactly
ONE strategy to trade a strict, small slice of exactly ONE LIVE account,
bound to the exact artifact/allowlist/ruleset digests active when it was
prepared, for a bounded time window. ``PromotionController.prepare_canary``
(``trader/promotion/controller.py``) builds the UNSIGNED payload offline
from a strategy's current PASSED paper evidence; an operator holding the
Ed25519 private key signs it via ``sign_canary_payload`` (reusing
``trader.research.signing`` -- the ONLY place a private key is ever
handled); the trader holds only the public verification key and can
verify but never mint one -- the exact asymmetric-trust shape P2 already
established.

The canary and paper-eligibility payloads are namespaced under different
SHA-256 prefixes (``CANARY_PAYLOAD_PREFIX`` vs P2's
``ATTESTATION_PAYLOAD_PREFIX``) and carry structurally different fields
(``eligibility_state`` is pinned to ``CANARY_ELIGIBLE`` and
``account_mode`` to ``"live"`` -- never caller-supplied), so a validly
signed ``EligibilityAttestation`` can never verify as a canary authority,
and vice versa, no matter what field values are copied across.

``CanaryAuthorityVerifier.verify`` independently RE-CHECKS every structural
policy constraint (exactly one permitted instrument, allocation within the
6% ceiling, ``CANARY_ELIGIBLE``/``"live"``) even though ``build_canary_payload``
already enforces them at mint time -- a valid Ed25519 signature only proves
"the key holder approved these exact bytes," never "these bytes satisfy
policy." Skipping the independent re-check would let a hand-built (bypassing
``build_canary_payload``), validly-signed payload smuggle a second
instrument or an over-cap allocation past the trader. Fail closed: any
mismatch on signature, trust, structural policy, expiry, revocation, or
binding raises a specific error -- verification never returns a soft
"maybe valid".
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, fields
from typing import Any, Mapping, Optional, Sequence

from trader.research import signing
from trader.research.canonical import canonical_json_bytes, sha256_digest
from trader.research.signing import AttestationSigner

CANARY_PAYLOAD_PREFIX = "canary_activation_authority"

# Frozen, non-negotiable structural constants (plan Task 5 / Global
# Constraints): a canary authority always names the live account mode and
# the CANARY_ELIGIBLE state -- never derived from caller input.
CANARY_ACCOUNT_MODE = "live"
CANARY_ELIGIBLE = "CANARY_ELIGIBLE"
MAX_CANARY_GROSS_ALLOCATION = 0.06

_SIGNATURE_FIELD = "signature"

__all__ = [
    "CANARY_PAYLOAD_PREFIX",
    "CANARY_ACCOUNT_MODE",
    "CANARY_ELIGIBLE",
    "MAX_CANARY_GROSS_ALLOCATION",
    "CanaryAttestation",
    "CanaryValidationError",
    "CanaryAccountInvalid",
    "CanaryAllocationExceeded",
    "CanaryMultiStrategy",
    "build_canary_payload",
    "canary_unsigned_payload",
    "canary_payload_bytes",
    "canary_payload_digest",
    "sign_canary_payload",
    "CanaryAuthorityError",
    "CanaryUnknownKey",
    "CanaryBadSignature",
    "CanaryExpired",
    "CanaryRevoked",
    "CanaryBindingMismatch",
    "CanaryPolicyViolation",
    "ExpectedCanaryBindings",
    "VerifiedCanaryAuthority",
    "CanaryAuthorityVerifier",
    "canary_attestation_from_wire",
    "canary_attestation_to_wire",
]


# --------------------------------------------------------------------------- #
# The attestation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CanaryAttestation:
    """The canonical, signed canary-activation authority.

    Every field except ``signature`` is covered by the signature -- tamper
    with any of them (including ``eligibility_state``/``account_mode``,
    which are structurally pinned but still signed) and verification fails
    closed with ``CanaryBadSignature``.
    """

    strategy_id: str
    account_id: str
    account_mode: str
    eligibility_state: str
    artifact_digest: str
    allowlist_digest: str
    ruleset_digest: str
    max_gross_allocation: float
    permitted_instruments: tuple
    paper_evidence_digest: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    operator: str
    reason: str
    public_key_id: str
    signature: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "permitted_instruments", tuple(self.permitted_instruments))

    @property
    def payload_digest(self) -> str:
        return canary_payload_digest(self)

    @property
    def unsigned_payload(self) -> dict:
        return canary_unsigned_payload(self)


# --------------------------------------------------------------------------- #
# Canonical unsigned payload -- the exact bytes we sign / verify / digest over
# --------------------------------------------------------------------------- #
def canary_unsigned_payload(attestation_or_fields: Any) -> dict:
    """Every field EXCEPT ``signature`` as a plain dict.

    Accepts either a ``CanaryAttestation`` or a fields dict (as
    ``build_canary_payload`` returns) so identical bytes are produced
    whether about to sign or verifying a wire-transmitted attestation.
    """
    if isinstance(attestation_or_fields, CanaryAttestation):
        payload = {f.name: getattr(attestation_or_fields, f.name)
                   for f in fields(CanaryAttestation)}
    else:
        payload = dict(attestation_or_fields)
    payload.pop(_SIGNATURE_FIELD, None)
    return payload


def canary_payload_bytes(attestation_or_fields: Any) -> bytes:
    """Deterministic canonical-JSON bytes of the unsigned payload (what we sign)."""
    return canonical_json_bytes(canary_unsigned_payload(attestation_or_fields))


def canary_payload_digest(attestation_or_fields: Any) -> str:
    """Namespaced SHA-256 of the unsigned payload -- the authority's stable
    id (persistence key + revocation key). Namespaced under
    ``CANARY_PAYLOAD_PREFIX`` so it can never collide with, or be confused
    for, a P2 ``EligibilityAttestation`` payload digest."""
    return sha256_digest(CANARY_PAYLOAD_PREFIX, canary_unsigned_payload(attestation_or_fields))


# --------------------------------------------------------------------------- #
# Build (structural invariants enforced at mint time) + sign
# --------------------------------------------------------------------------- #
class CanaryValidationError(Exception):
    """Raised by ``build_canary_payload`` when the requested authority itself
    violates a non-negotiable structural constraint."""


class CanaryAccountInvalid(CanaryValidationError):
    """``account_id`` is missing/blank."""


class CanaryAllocationExceeded(CanaryValidationError):
    """``max_gross_allocation`` is not in ``(0, MAX_CANARY_GROSS_ALLOCATION]``."""


class CanaryMultiStrategy(CanaryValidationError):
    """``permitted_instruments`` does not contain EXACTLY one instrument --
    a canary authority is always bound to exactly one strategy/instrument."""


def build_canary_payload(
    *,
    strategy_id: str,
    account_id: str,
    artifact_digest: str,
    allowlist_digest: str,
    ruleset_digest: str,
    max_gross_allocation: float,
    permitted_instruments: Sequence[Any],
    paper_evidence_digest: str,
    issued_at: dt.datetime,
    expires_at: dt.datetime,
    operator: str,
    reason: str,
    public_key_id: str,
) -> dict:
    """Assemble + VALIDATE the unsigned canary authority fields.

    ``account_mode`` and ``eligibility_state`` are ALWAYS ``"live"`` /
    ``CANARY_ELIGIBLE`` -- never derived from caller input, so there is no
    argument by which a caller can mint an authority claiming to be
    anything other than a live canary. ``max_gross_allocation`` must be
    strictly positive and no more than the frozen 6% ceiling; exceeding it
    (or supplying zero/negative) raises ``CanaryAllocationExceeded``.
    ``permitted_instruments`` must name EXACTLY one instrument -- zero or
    more than one raises ``CanaryMultiStrategy``.
    """
    if not strategy_id:
        raise CanaryValidationError("strategy_id is required")
    if not account_id:
        raise CanaryAccountInvalid("account_id is required")
    if not operator:
        raise CanaryValidationError("operator is required")
    if not reason:
        raise CanaryValidationError("reason is required")
    if not public_key_id:
        raise CanaryValidationError("public_key_id is required")
    if not artifact_digest or not allowlist_digest or not ruleset_digest:
        raise CanaryValidationError(
            "artifact_digest, allowlist_digest, and ruleset_digest are all required"
        )
    if not paper_evidence_digest:
        raise CanaryValidationError("paper_evidence_digest is required")
    allocation = float(max_gross_allocation)
    if allocation <= 0 or allocation > MAX_CANARY_GROSS_ALLOCATION:
        raise CanaryAllocationExceeded(
            f"max_gross_allocation {allocation!r} must be in (0, {MAX_CANARY_GROSS_ALLOCATION}]"
        )
    instruments = tuple(permitted_instruments)
    if len(instruments) != 1:
        raise CanaryMultiStrategy(
            f"permitted_instruments must name exactly one instrument, got {instruments!r}"
        )
    issued = _as_utc(issued_at)
    expires = _as_utc(expires_at)
    if expires <= issued:
        raise CanaryValidationError("expires_at must be after issued_at")

    return {
        "strategy_id": strategy_id,
        "account_id": account_id,
        "account_mode": CANARY_ACCOUNT_MODE,
        "eligibility_state": CANARY_ELIGIBLE,
        "artifact_digest": artifact_digest,
        "allowlist_digest": allowlist_digest,
        "ruleset_digest": ruleset_digest,
        "max_gross_allocation": allocation,
        "permitted_instruments": instruments,
        "paper_evidence_digest": paper_evidence_digest,
        "issued_at": issued,
        "expires_at": expires,
        "operator": operator,
        "reason": reason,
        "public_key_id": public_key_id,
    }


def sign_canary_payload(signer: AttestationSigner, unsigned_fields: Mapping[str, Any]) -> CanaryAttestation:
    """Ed25519-sign the unsigned canary fields; offline, deterministic.

    ``unsigned_fields`` must already carry ``public_key_id ==
    signer.public_key_id`` -- the key id is part of the signed payload, so a
    mismatch fails loud rather than minting an authority whose recorded key
    id does not match the key that actually signed it.
    """
    fields_dict = dict(unsigned_fields)
    declared = fields_dict.get("public_key_id")
    if declared != signer.public_key_id:
        raise ValueError(
            f"canary payload public_key_id {declared!r} does not match signing key "
            f"{signer.public_key_id!r}"
        )
    signature = signer.sign_message(canary_payload_bytes(fields_dict))
    return CanaryAttestation(**fields_dict, signature=signature)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Verification -- fail closed
# --------------------------------------------------------------------------- #
class CanaryAuthorityError(Exception):
    """Base class for every canary-authority verification failure."""


class CanaryUnknownKey(CanaryAuthorityError):
    """The authority is signed by a key id the verifier does not trust."""


class CanaryBadSignature(CanaryAuthorityError):
    """The signature does not verify -- a wrong key OR any tampered field."""


class CanaryExpired(CanaryAuthorityError):
    """``now >= expires_at``: the authority is no longer valid."""


class CanaryRevoked(CanaryAuthorityError):
    """The authority's payload digest is in the revocation set."""


class CanaryBindingMismatch(CanaryAuthorityError):
    """A validly-signed authority does not match the exact expected
    account/artifact/allowlist/ruleset bindings."""


class CanaryPolicyViolation(CanaryAuthorityError):
    """A validly-signed authority nonetheless violates a non-negotiable
    structural constraint (state, mode, allocation cap, single-instrument)
    -- defense-in-depth against a hand-built payload that bypassed
    ``build_canary_payload``'s own validation before being signed."""


@dataclass(frozen=True)
class ExpectedCanaryBindings:
    """What the trader independently expects the authority to bind to."""

    account_id: str
    artifact_digest: str
    allowlist_digest: str
    ruleset_digest: str


@dataclass(frozen=True)
class VerifiedCanaryAuthority:
    """The trusted facts a successful verification returns."""

    strategy_id: str
    account_id: str
    max_gross_allocation: float
    permitted_instruments: tuple
    artifact_digest: str
    allowlist_digest: str
    ruleset_digest: str
    expires_at: dt.datetime
    public_key_id: str
    payload_digest: str


class CanaryAuthorityVerifier:
    """Verifies canary attestations against a fixed trust store of public
    keys. Holds NO private material and can never mint an authority -- safe
    to run inside the trader service."""

    def __init__(self, trusted_public_keys: Sequence[Any]):
        keys: dict[str, Any] = {}
        for pk in trusted_public_keys:
            keys[signing.public_key_id(pk)] = pk
        if not keys:
            raise ValueError("CanaryAuthorityVerifier requires at least one trusted key")
        self._keys = keys

    @property
    def trusted_key_ids(self) -> frozenset:
        return frozenset(self._keys)

    def verify(
        self,
        attestation: CanaryAttestation,
        expected: ExpectedCanaryBindings,
        *,
        now: dt.datetime,
        revoked_digests: frozenset = frozenset(),
    ) -> VerifiedCanaryAuthority:
        """Verify ``attestation`` and return the trusted facts, or raise.

        Order (each a fail-closed gate): (1) signing key trusted, (2)
        signature verifies over the canonical unsigned payload, (3)
        structural policy re-check (CANARY_ELIGIBLE / live / <=6% /
        exactly-one-instrument) -- defense-in-depth against a hand-built,
        never-through-``build_canary_payload`` payload, (4) not expired,
        (5) not revoked, (6) every expected binding matches exactly.
        """
        public_key = self._keys.get(attestation.public_key_id)
        if public_key is None:
            raise CanaryUnknownKey(
                f"canary authority signed by untrusted key id {attestation.public_key_id!r}"
            )

        payload = canary_unsigned_payload(attestation)
        try:
            signing.verify_bytes(public_key, canonical_json_bytes(payload), attestation.signature)
        except signing.BadSignature as exc:
            raise CanaryBadSignature("canary authority signature does not verify") from exc

        # From here every field is cryptographically trusted -- but a valid
        # signature alone never proves policy compliance (see module
        # docstring); re-check independently.
        violations: list[str] = []
        if attestation.eligibility_state != CANARY_ELIGIBLE:
            violations.append("eligibility_state")
        if attestation.account_mode != CANARY_ACCOUNT_MODE:
            violations.append("account_mode")
        if not (0 < attestation.max_gross_allocation <= MAX_CANARY_GROSS_ALLOCATION):
            violations.append("max_gross_allocation")
        if len(attestation.permitted_instruments) != 1:
            violations.append("permitted_instruments")
        if violations:
            raise CanaryPolicyViolation(
                f"canary authority violates structural policy: {', '.join(violations)}"
            )

        if _as_utc(now) >= attestation.expires_at:
            raise CanaryExpired(
                f"canary authority expired at {attestation.expires_at.isoformat()} "
                f"(now {_as_utc(now).isoformat()})"
            )

        pdigest = sha256_digest(CANARY_PAYLOAD_PREFIX, payload)
        if pdigest in revoked_digests:
            raise CanaryRevoked(f"canary authority {pdigest} is revoked")

        mismatches: list[str] = []
        if attestation.account_id != expected.account_id:
            mismatches.append("account_id")
        if attestation.artifact_digest != expected.artifact_digest:
            mismatches.append("artifact_digest")
        if attestation.allowlist_digest != expected.allowlist_digest:
            mismatches.append("allowlist_digest")
        if attestation.ruleset_digest != expected.ruleset_digest:
            mismatches.append("ruleset_digest")
        if mismatches:
            raise CanaryBindingMismatch(
                f"canary authority does not match expected bindings: {', '.join(mismatches)}"
            )

        return VerifiedCanaryAuthority(
            strategy_id=attestation.strategy_id,
            account_id=attestation.account_id,
            max_gross_allocation=attestation.max_gross_allocation,
            permitted_instruments=tuple(attestation.permitted_instruments),
            artifact_digest=attestation.artifact_digest,
            allowlist_digest=attestation.allowlist_digest,
            ruleset_digest=attestation.ruleset_digest,
            expires_at=attestation.expires_at,
            public_key_id=attestation.public_key_id,
            payload_digest=pdigest,
        )


# --------------------------------------------------------------------------- #
# Wire (de)serialization -- public material ONLY, never a private key. Used
# by both the CLI (offline prepare/sign/verify) and the trader-side
# activate/deactivate command handlers so a signed authority round-trips
# identically over JSON/RPC.
# --------------------------------------------------------------------------- #
def canary_attestation_to_wire(attestation: CanaryAttestation) -> dict:
    """JSON-safe projection of a signed ``CanaryAttestation`` -- every field
    is either already JSON-safe or an ISO-8601 datetime string. Safe to
    write to a file or send over RPC: contains no private key material."""
    payload = canary_unsigned_payload(attestation)
    payload["permitted_instruments"] = list(payload["permitted_instruments"])
    payload["issued_at"] = _as_utc(payload["issued_at"]).isoformat()
    payload["expires_at"] = _as_utc(payload["expires_at"]).isoformat()
    payload["signature"] = attestation.signature
    return payload


def _parse_dt(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return _as_utc(value)
    return _as_utc(dt.datetime.fromisoformat(str(value)))


def canary_attestation_from_wire(data: Mapping[str, Any]) -> CanaryAttestation:
    """Reconstruct a ``CanaryAttestation`` from its JSON-safe wire form
    (as produced by ``canary_attestation_to_wire``). Raises ``KeyError``/
    ``ValueError`` on a malformed payload -- never silently coerces."""
    raw = dict(data)
    return CanaryAttestation(
        strategy_id=raw["strategy_id"],
        account_id=raw["account_id"],
        account_mode=raw["account_mode"],
        eligibility_state=raw["eligibility_state"],
        artifact_digest=raw["artifact_digest"],
        allowlist_digest=raw["allowlist_digest"],
        ruleset_digest=raw["ruleset_digest"],
        max_gross_allocation=float(raw["max_gross_allocation"]),
        permitted_instruments=tuple(raw["permitted_instruments"]),
        paper_evidence_digest=raw["paper_evidence_digest"],
        issued_at=_parse_dt(raw["issued_at"]),
        expires_at=_parse_dt(raw["expires_at"]),
        operator=raw["operator"],
        reason=raw["reason"],
        public_key_id=raw["public_key_id"],
        signature=raw["signature"],
    )
