"""P5 Task 1 — signed allocation authority attestations."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from trader.research import signing
from trader.research.canonical import canonical_json_bytes, sha256_digest
from trader.research.signing import AttestationSigner

ALLOCATION_PAYLOAD_PREFIX = "allocation_authority"

STAGE_CANARY = "CANARY"
STAGE_SCALE_1 = "SCALE_1"
STAGE_SCALE_2 = "SCALE_2"
STAGE_STEADY = "STEADY"

STAGE_MAX_CEILING: dict[str, Decimal] = {
    STAGE_CANARY: Decimal("0.06"),
    STAGE_SCALE_1: Decimal("0.09"),
    STAGE_SCALE_2: Decimal("0.135"),
    STAGE_STEADY: Decimal("0.15"),
}

_SIGNATURE_FIELD = "signature"

__all__ = [
    "ALLOCATION_PAYLOAD_PREFIX",
    "STAGE_CANARY",
    "STAGE_SCALE_1",
    "STAGE_SCALE_2",
    "STAGE_STEADY",
    "STAGE_MAX_CEILING",
    "AllocationAttestation",
    "AllocationValidationError",
    "AllocationCeilingExceeded",
    "build_allocation_payload",
    "allocation_payload_bytes",
    "allocation_payload_digest",
    "sign_allocation_payload",
    "AllocationAuthorityError",
    "AllocationUnknownKey",
    "AllocationBadSignature",
    "AllocationExpired",
    "AllocationRevoked",
    "AllocationBindingMismatch",
    "AllocationPolicyViolation",
    "ExpectedAllocationBindings",
    "VerifiedAllocationAuthority",
    "AllocationAttestationVerifier",
    "validate_signed_ceiling",
    "allocation_attestation_from_wire",
    "allocation_attestation_to_wire",
]


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def validate_signed_ceiling(stage: str, max_gross_allocation: Decimal | float) -> None:
    if stage not in STAGE_MAX_CEILING:
        raise AllocationValidationError(f"unknown allocation stage {stage!r}")
    ceiling = STAGE_MAX_CEILING[stage]
    value = Decimal(str(max_gross_allocation))
    if value <= 0:
        raise AllocationValidationError("max_gross_allocation must be positive")
    if value > ceiling:
        raise AllocationCeilingExceeded(
            f"stage {stage!r} caps gross allocation at {ceiling}; got {value}"
        )


@dataclass(frozen=True)
class AllocationAttestation:
    strategy_id: str
    account_id: str
    account_mode: str
    stage: str
    artifact_digest: str
    allowlist_digest: str
    ruleset_digest: str
    max_gross_allocation: float
    evidence_digest: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    operator: str
    reason: str
    public_key_id: str
    signature: str

    def __post_init__(self) -> None:
        validate_signed_ceiling(self.stage, self.max_gross_allocation)
        if self.account_mode not in ("paper", "live"):
            raise AllocationValidationError(f"invalid account_mode {self.account_mode!r}")
        if _as_utc(self.expires_at) <= _as_utc(self.issued_at):
            raise AllocationValidationError("expires_at must be after issued_at")


class AllocationValidationError(ValueError):
    pass


class AllocationCeilingExceeded(AllocationValidationError):
    pass


def allocation_unsigned_payload(attestation_or_fields: Any) -> dict[str, Any]:
    if isinstance(attestation_or_fields, AllocationAttestation):
        payload = {f.name: getattr(attestation_or_fields, f.name) for f in fields(AllocationAttestation)}
    else:
        payload = dict(attestation_or_fields)
    payload.pop(_SIGNATURE_FIELD, None)
    return payload


def allocation_payload_bytes(attestation_or_fields: Any) -> bytes:
    return canonical_json_bytes(allocation_unsigned_payload(attestation_or_fields))


def allocation_payload_digest(attestation_or_fields: Any) -> str:
    return sha256_digest(ALLOCATION_PAYLOAD_PREFIX, allocation_unsigned_payload(attestation_or_fields))


def build_allocation_payload(
    *,
    strategy_id: str,
    account_id: str,
    account_mode: str,
    stage: str,
    artifact_digest: str,
    allowlist_digest: str,
    ruleset_digest: str,
    max_gross_allocation: float,
    evidence_digest: str,
    issued_at: dt.datetime,
    expires_at: dt.datetime,
    operator: str,
    reason: str,
    public_key_id: str,
) -> dict[str, Any]:
    validate_signed_ceiling(stage, max_gross_allocation)
    issued = _as_utc(issued_at)
    expires = _as_utc(expires_at)
    if expires <= issued:
        raise AllocationValidationError("expires_at must be after issued_at")
    return {
        "strategy_id": strategy_id,
        "account_id": account_id,
        "account_mode": account_mode,
        "stage": stage,
        "artifact_digest": artifact_digest,
        "allowlist_digest": allowlist_digest,
        "ruleset_digest": ruleset_digest,
        "max_gross_allocation": float(max_gross_allocation),
        "evidence_digest": evidence_digest,
        "issued_at": issued,
        "expires_at": expires,
        "operator": operator,
        "reason": reason,
        "public_key_id": public_key_id,
    }


def sign_allocation_payload(signer: AttestationSigner, unsigned_fields: Mapping[str, Any]) -> AllocationAttestation:
    fields_dict = dict(unsigned_fields)
    declared = fields_dict.get("public_key_id")
    if declared != signer.public_key_id:
        raise ValueError(
            f"allocation payload public_key_id {declared!r} does not match signing key "
            f"{signer.public_key_id!r}"
        )
    signature = signer.sign_message(allocation_payload_bytes(fields_dict))
    fields_dict[_SIGNATURE_FIELD] = signature
    return allocation_attestation_from_wire(fields_dict)


class AllocationAuthorityError(Exception):
    pass


class AllocationUnknownKey(AllocationAuthorityError):
    pass


class AllocationBadSignature(AllocationAuthorityError):
    pass


class AllocationExpired(AllocationAuthorityError):
    pass


class AllocationRevoked(AllocationAuthorityError):
    pass


class AllocationBindingMismatch(AllocationAuthorityError):
    pass


class AllocationPolicyViolation(AllocationAuthorityError):
    pass


@dataclass(frozen=True)
class ExpectedAllocationBindings:
    account_id: str
    account_mode: str
    artifact_digest: str
    allowlist_digest: str
    ruleset_digest: str
    strategy_id: str


@dataclass(frozen=True)
class VerifiedAllocationAuthority:
    strategy_id: str
    account_id: str
    account_mode: str
    stage: str
    artifact_digest: str
    allowlist_digest: str
    ruleset_digest: str
    max_gross_allocation: float
    evidence_digest: str
    public_key_id: str
    expires_at: dt.datetime
    payload_digest: str


class AllocationAttestationVerifier:
    """Production-side verifier — public keys only, never signs."""

    def __init__(self, trusted_public_keys: Mapping[str, Any], *, revoked_key_ids: frozenset[str] = frozenset()):
        self._keys = dict(trusted_public_keys)
        self._revoked = revoked_key_ids

    def verify(
        self,
        attestation: AllocationAttestation,
        *,
        expected: ExpectedAllocationBindings,
        now: dt.datetime,
    ) -> VerifiedAllocationAuthority:
        if attestation.public_key_id in self._revoked:
            raise AllocationRevoked(f"public key {attestation.public_key_id!r} is revoked")
        public_key = self._keys.get(attestation.public_key_id)
        if public_key is None:
            raise AllocationUnknownKey(f"untrusted public_key_id {attestation.public_key_id!r}")

        payload = allocation_unsigned_payload(attestation)
        try:
            signing.verify_bytes(public_key, allocation_payload_bytes(payload), attestation.signature)
        except signing.BadSignature as exc:
            raise AllocationBadSignature("allocation authority signature does not verify") from exc

        validate_signed_ceiling(attestation.stage, attestation.max_gross_allocation)
        resolved_now = _as_utc(now)
        if _as_utc(attestation.expires_at) <= resolved_now:
            raise AllocationExpired("allocation authority expired")

        if attestation.account_id != expected.account_id:
            raise AllocationBindingMismatch("account_id mismatch")
        if attestation.account_mode != expected.account_mode:
            raise AllocationBindingMismatch("account_mode mismatch")
        if attestation.strategy_id != expected.strategy_id:
            raise AllocationBindingMismatch("strategy_id mismatch")
        if attestation.artifact_digest != expected.artifact_digest:
            raise AllocationBindingMismatch("artifact_digest mismatch")
        if attestation.allowlist_digest != expected.allowlist_digest:
            raise AllocationBindingMismatch("allowlist_digest mismatch")
        if attestation.ruleset_digest != expected.ruleset_digest:
            raise AllocationBindingMismatch("ruleset mismatch")

        digest = allocation_payload_digest(attestation)
        return VerifiedAllocationAuthority(
            strategy_id=attestation.strategy_id,
            account_id=attestation.account_id,
            account_mode=attestation.account_mode,
            stage=attestation.stage,
            artifact_digest=attestation.artifact_digest,
            allowlist_digest=attestation.allowlist_digest,
            ruleset_digest=attestation.ruleset_digest,
            max_gross_allocation=float(attestation.max_gross_allocation),
            evidence_digest=attestation.evidence_digest,
            public_key_id=attestation.public_key_id,
            expires_at=_as_utc(attestation.expires_at),
            payload_digest=digest,
        )


def allocation_attestation_from_wire(raw: Mapping[str, Any]) -> AllocationAttestation:
    def _parse_ts(key: str) -> dt.datetime:
        text = str(raw[key]).replace("Z", "+00:00")
        return _as_utc(dt.datetime.fromisoformat(text))

    return AllocationAttestation(
        strategy_id=str(raw["strategy_id"]),
        account_id=str(raw["account_id"]),
        account_mode=str(raw["account_mode"]),
        stage=str(raw["stage"]),
        artifact_digest=str(raw["artifact_digest"]),
        allowlist_digest=str(raw["allowlist_digest"]),
        ruleset_digest=str(raw["ruleset_digest"]),
        max_gross_allocation=float(raw["max_gross_allocation"]),
        evidence_digest=str(raw["evidence_digest"]),
        issued_at=_parse_ts("issued_at"),
        expires_at=_parse_ts("expires_at"),
        operator=str(raw["operator"]),
        reason=str(raw["reason"]),
        public_key_id=str(raw["public_key_id"]),
        signature=str(raw[_SIGNATURE_FIELD]),
    )


def allocation_attestation_to_wire(attestation: AllocationAttestation) -> dict[str, Any]:
    payload = allocation_unsigned_payload(attestation)
    payload["issued_at"] = _as_utc(payload["issued_at"]).isoformat().replace("+00:00", "Z")
    payload["expires_at"] = _as_utc(payload["expires_at"]).isoformat().replace("+00:00", "Z")
    payload[_SIGNATURE_FIELD] = attestation.signature
    return payload


class AllocationAttestationSigner:
    """Offline-only wrapper — never imported on trader_service hot path."""

    def __init__(self, signer: AttestationSigner):
        self._signer = signer

    def sign(self, unsigned_fields: Mapping[str, Any]) -> AllocationAttestation:
        return sign_allocation_payload(self._signer, unsigned_fields)

    @property
    def public_key_id(self) -> str:
        return self._signer.public_key_id
