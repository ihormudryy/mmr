"""The signed evaluation case (SP2c spec 5.1). Plan 3 builds and signs it; the trader verifies it.

A case never authorizes trading: it has no attestation, review or manifest. A case
file is the canonical JSON of ``{"case", "case_digest", "public_key_id", "signature"}``.
The digest and the signature cover the same bytes: the domain tag, a newline and the
canonical case body.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from trader.research.canonical import canonical_json_bytes, sha256_digest
from trader.research.eligibility import STATE_PAPER_ELIGIBLE
from trader.research.evaluation_request import (
    MAX_COHORT_POINTS_LIMIT, REQUEST_ID, ParamValue, check_bar_size, check_cohort, check_conids, check_day,
    check_params, is_cohort_point,
)
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.signing import (
    AttestationSigner, BadSignature, InvalidKeyType, MalformedKey, load_verify_key, public_key_id, verify_bytes,
)
from trader.research.strategy_key import is_strategy_key

CASE_DOMAIN = "mmr.research.evaluation-case.v1"
INITIAL_STAGES = ("PRE_HOLDOUT_FAILED", "HOLDOUT_FAILED", "COMPLETE", "FAILED")
RENEWAL_STAGES = ("FORWARD_COMPLETE", "FORWARD_INCOMPLETE")
FULL_MENU = ("DEPLOY", "SHADOW", "REJECT")
NO_DEPLOY_MENU = ("SHADOW", "REJECT")
_EXPECTED_HOLDOUT_PASSED = {"COMPLETE": True, "HOLDOUT_FAILED": False, "PRE_HOLDOUT_FAILED": None, "FAILED": None}
MAX_CASE_BYTES = 4 * 1024 * 1024
PAPER_V1_CODES = frozenset(rule.code for rule in PAPER_V1.rules)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ENVELOPE_KEYS = frozenset({"case", "case_digest", "public_key_id", "signature"})


class CaseRefused(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RuleOutcome(_Strict):
    code: str
    passed: bool


class RenewalFacts(_Strict):
    prior_deployment_version: str
    forward_sessions: int = Field(ge=0)
    incomplete_sessions: int = Field(ge=0)

    @field_validator("prior_deployment_version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("prior_deployment_version must be sha256:<hex>")
        return value


class EvaluationCase(_Strict):
    schema_version: Literal["mmr.research.evaluation-case.v1"]
    kind: Literal["INITIAL", "RENEWAL"]
    request_id: Optional[str]
    claim_day: Optional[str]
    strategy_key: str
    strategy_file_hash: str
    cohort: list[dict[str, ParamValue]]
    conids: list[int]
    bar_size: str
    stage: Literal["PRE_HOLDOUT_FAILED", "HOLDOUT_FAILED", "COMPLETE", "FAILED",
                   "FORWARD_COMPLETE", "FORWARD_INCOMPLETE"]
    selected_params: Optional[dict[str, ParamValue]]
    family_id: Optional[str]
    selected_trial_id: Optional[str]
    artifact_id: Optional[str]
    eligibility_decision_digest: Optional[str]
    decision_state: Optional[str]
    ruleset_digest: Optional[str]
    holdout_passed: Optional[bool]
    final_rule_results: list[RuleOutcome]
    renewal: Optional[RenewalFacts]
    created_at: str
    evidence: dict[str, Any]

    @model_validator(mode="after")
    def _shape(self) -> "EvaluationCase":
        if not is_strategy_key(self.strategy_key):
            raise ValueError("strategy_key must be strategies/<file>.py:<Class>")
        if not _DIGEST.fullmatch(self.strategy_file_hash):
            raise ValueError("strategy_file_hash must be sha256:<hex>")
        check_cohort(self.cohort)
        if self.selected_params is not None:
            check_params(self.selected_params)
        check_conids(self.conids)
        check_bar_size(self.bar_size)
        created = dt.datetime.fromisoformat(self.created_at)
        if created.tzinfo is None or created.utcoffset() is None:
            raise ValueError("created_at must carry an offset")
        for name in ("family_id", "selected_trial_id", "artifact_id", "eligibility_decision_digest",
                     "decision_state", "ruleset_digest"):
            value = getattr(self, name)
            if value is not None and not _ID.fullmatch(value):
                raise ValueError(f"{name} must match {_ID.pattern}")
        if self.kind == "INITIAL":
            self._initial_shape()
        else:
            self._renewal_shape()
        self._holdout_evidence_shape()
        if self.kind == "INITIAL":
            self._selected_point_shape()
        return self

    def _holdout_evidence_shape(self) -> None:
        """The signed holdout evidence and the header say the same thing (Plan 3: ``holdout`` is a dict or null)."""
        if self.kind == "RENEWAL":
            if self.evidence.get("holdout") is not None:
                raise ValueError("a RENEWAL case has no holdout evidence")
            return
        if "holdout" not in self.evidence:
            raise ValueError("an INITIAL case's evidence names its holdout (a result or null)")
        holdout = self.evidence["holdout"]
        if self.holdout_passed is None:
            if holdout is not None:
                raise ValueError(f"a {self.stage} case has null holdout evidence")
            return
        if not isinstance(holdout, dict) or type(holdout.get("passed")) is not bool:
            raise ValueError("evidence.holdout.passed must be true or false")
        if holdout["passed"] is not self.holdout_passed:
            raise ValueError("evidence.holdout.passed contradicts holdout_passed")

    def _selected_point_shape(self) -> None:
        """The signed selected point is the header's point (Plan 3 ``evidence.points[selected_index]``).

        Once the holdout opened, the point passed its pre-holdout gate and carries the final rule
        results, so its ``rules`` repeat ``final_rule_results`` code for code. A FAILED case binds nothing.
        """
        if self.stage == "FAILED":
            return
        index = self.evidence.get("selected_index")
        if self.selected_params is None:
            if index is not None:
                raise ValueError("a case without selected_params has no selected point")
            return
        points = self.evidence.get("points")
        if type(index) is not int or not isinstance(points, list) or not 0 <= index < len(points):
            raise ValueError("evidence.selected_index must name one of evidence.points")
        point = points[index]
        if not isinstance(point, dict) or type(point.get("index")) is not int or point["index"] != index:
            raise ValueError("the selected point must carry its own index")
        same_point = _same_json(point.get("params"), self.selected_params)
        if not same_point or point.get("trial_id") != self.selected_trial_id:
            raise ValueError("the selected point's params and trial must be the header's")
        holdout_opened = self.stage != "PRE_HOLDOUT_FAILED"
        if point.get("pre_holdout_passed") is not holdout_opened:
            raise ValueError("the selected point passed its pre-holdout gate exactly when the holdout opened")
        final_rules = [(rule.code, rule.passed) for rule in self.final_rule_results]
        if holdout_opened and _rule_pairs(point.get("rules")) != final_rules:
            raise ValueError("the selected point's rules must be the final rule results")

    def _initial_shape(self) -> None:
        if self.stage not in INITIAL_STAGES or self.renewal is not None:
            raise ValueError("an INITIAL case has an evaluation stage and no renewal facts")
        if self.request_id is None or not REQUEST_ID.fullmatch(self.request_id) or self.claim_day is None:
            raise ValueError("an INITIAL case names its request id and claim day")
        check_day(self.claim_day, "claim_day")
        if not 1 <= len(self.cohort) <= MAX_COHORT_POINTS_LIMIT:
            raise ValueError("an INITIAL case holds its whole cohort")
        sealed_fields = (self.selected_params, self.family_id, self.selected_trial_id, self.artifact_id,
                         self.eligibility_decision_digest, self.decision_state, self.ruleset_digest)
        if self.stage in ("COMPLETE", "HOLDOUT_FAILED") and (
                any(value is None for value in sealed_fields) or not self.final_rule_results):
            raise ValueError("a case that opened its holdout names its artifact, decision and rule results")
        if self.selected_params is not None and not is_cohort_point(self.selected_params, self.cohort):
            raise ValueError("selected_params must be one of the claimed cohort points")
        if self.stage == "PRE_HOLDOUT_FAILED" and (self.artifact_id is not None or self.final_rule_results):
            raise ValueError("a pre-holdout failure seals no artifact")
        if self.holdout_passed is not _EXPECTED_HOLDOUT_PASSED[self.stage]:
            raise ValueError("holdout_passed is True for COMPLETE, False for HOLDOUT_FAILED and null otherwise")

    def _renewal_shape(self) -> None:
        if self.stage not in RENEWAL_STAGES or self.renewal is None:
            raise ValueError("a RENEWAL case has a forward stage and renewal facts")
        if self.request_id is not None or self.claim_day is not None:
            raise ValueError("a RENEWAL case has no evaluation claim")
        if self.holdout_passed is not None:
            raise ValueError("a RENEWAL case has no holdout")
        if self.selected_params is None or self.cohort != [self.selected_params]:
            raise ValueError("a RENEWAL case names exactly its deployed parameters")


def _same_json(left: Any, right: Any) -> bool:
    try:
        return canonical_json_bytes(left) == canonical_json_bytes(right)
    except (TypeError, ValueError):
        return False


def _rule_pairs(rules: Any) -> Optional[list[tuple[str, bool]]]:
    """``[(code, passed)]`` of an evidence rule list; None when an entry is not ``{code: str, passed: bool}``."""
    if not isinstance(rules, list):
        return None
    pairs = []
    for rule in rules:
        if not isinstance(rule, dict):
            return None
        if not isinstance(rule.get("code"), str) or type(rule.get("passed")) is not bool:
            return None
        pairs.append((rule["code"], rule["passed"]))
    return pairs


def initial_deploy_allowed(case: EvaluationCase) -> bool:
    """Rules first: complete, a passed holdout and every paper-v1 rule passed."""
    results = case.final_rule_results
    return (case.kind == "INITIAL" and case.stage == "COMPLETE" and case.holdout_passed is True
            and case.decision_state == STATE_PAPER_ELIGIBLE and case.ruleset_digest == PAPER_V1.digest
            and len(results) == len(PAPER_V1.rules) and {r.code for r in results} == PAPER_V1_CODES
            and all(r.passed for r in results))


def renewal_forward_complete(case: EvaluationCase) -> bool:
    facts = case.renewal
    return (case.kind == "RENEWAL" and case.stage == "FORWARD_COMPLETE" and facts is not None
            and facts.forward_sessions >= 1 and facts.incomplete_sessions == 0)


def offered_menu(case: EvaluationCase) -> tuple[str, ...]:
    qualifies = initial_deploy_allowed(case) if case.kind == "INITIAL" else renewal_forward_complete(case)
    return FULL_MENU if qualifies else NO_DEPLOY_MENU


def case_digest(body: Mapping[str, Any]) -> str:
    return "sha256:" + sha256_digest(CASE_DOMAIN, body)


def _signed_message(body: Mapping[str, Any]) -> bytes:
    return CASE_DOMAIN.encode("utf-8") + b"\n" + canonical_json_bytes(body)


def case_path(cases_dir: Path, digest: str) -> Path:
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise CaseRefused("CASE_DIGEST_INVALID", "a case digest is sha256:<hex>")
    return Path(cases_dir) / f"sha256_{digest.split(':', 1)[1]}.json"


def default_cases_dir() -> Path:
    return Path("~/.local/share/mmr/artifacts/cases").expanduser()


def default_verify_dir() -> Path:
    return Path("~/.config/mmr/keys/verify").expanduser()


def write_evaluation_case(cases_dir: Path, case: EvaluationCase, signer: AttestationSigner) -> str:
    """Sign and store ``case`` once (immutable); an identical rewrite is a no-op."""
    body = case.model_dump()
    digest = case_digest(body)
    envelope = {"case": body, "case_digest": digest, "public_key_id": signer.public_key_id,
                "signature": signer.sign_message(_signed_message(body))}
    data = canonical_json_bytes(envelope)
    path = case_path(cases_dir, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise CaseRefused("CASE_EXISTS_DIFFERENT", f"{path.name} already holds other bytes")
        return digest
    staged = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    staged.write_bytes(data)
    os.replace(staged, path)
    return digest


def load_case_verify_keys(verify_dir: Path) -> dict[str, Ed25519PublicKey]:
    verify_dir = Path(verify_dir)
    paths = sorted(verify_dir.glob("*.pem")) if verify_dir.is_dir() else []
    keys: dict[str, Ed25519PublicKey] = {}
    for path in paths:
        try:
            key = load_verify_key(str(path))
        except (OSError, MalformedKey, InvalidKeyType) as exc:
            raise CaseRefused("CASE_VERIFY_KEYS_UNREADABLE", f"{path.name}: {type(exc).__name__}") from None
        keys[public_key_id(key)] = key
    if not keys:
        raise CaseRefused("CASE_VERIFY_KEYS_MISSING", f"no *.pem verify key under {verify_dir}")
    return keys


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not canonical JSON")


def load_verified_case(cases_dir: Path, digest: str, keys: Mapping[str, Ed25519PublicKey]) -> EvaluationCase:
    path = case_path(cases_dir, digest)
    if path.is_symlink() or not path.is_file():
        raise CaseRefused("CASE_NOT_FOUND", f"no case file {path.name}")
    data = path.read_bytes()
    if len(data) > MAX_CASE_BYTES:
        raise CaseRefused("CASE_MALFORMED", "the case file is too large")
    try:
        envelope = json.loads(data, parse_constant=_refuse_constant)
    except ValueError:
        raise CaseRefused("CASE_MALFORMED", "the case file is not JSON") from None
    if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_KEYS or not isinstance(envelope["case"], dict):
        raise CaseRefused("CASE_MALFORMED", f"a case file has exactly the keys {sorted(_ENVELOPE_KEYS)}")
    body = envelope["case"]
    try:
        recomputed = case_digest(body)
    except (TypeError, ValueError):
        raise CaseRefused("CASE_MALFORMED", "the case body is not canonical") from None
    if recomputed != digest or envelope["case_digest"] != digest:
        raise CaseRefused("CASE_DIGEST_MISMATCH", "the case body does not match its digest")
    key = keys.get(envelope["public_key_id"]) if isinstance(envelope["public_key_id"], str) else None
    if key is None:
        raise CaseRefused("CASE_KEY_UNKNOWN", "the case is signed by a key outside keys/verify")
    if not isinstance(envelope["signature"], str):
        raise CaseRefused("CASE_SIGNATURE_INVALID", "the signature is not text")
    try:
        verify_bytes(key, _signed_message(body), envelope["signature"])
    except BadSignature:
        raise CaseRefused("CASE_SIGNATURE_INVALID", "the case signature does not verify") from None
    try:
        return EvaluationCase.model_validate(body)
    except ValidationError as exc:
        raise CaseRefused("CASE_MALFORMED", f"{exc.error_count()} case field errors") from None
