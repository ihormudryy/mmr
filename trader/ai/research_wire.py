"""Reply shapes of the research cycle's calls. The only file to adapt when a server's reply changes.

Each model matches the reply builder of the server that sends it, key for key:
  SubmitReply, EvaluationView, CaseSummary  trader/research/evaluation_service.py, case_builder.evaluation_summary
  AttestReply, Binding                      trader/research/judgment_attest.py
  JudgmentReceipt, JudgmentView             trader/automation/backtest_judgments.py (record / get)
  VersionReply                              trader/automation/ai_paper_actions.py (version_view)
  parse_registration                        the command ledger receipt of register_ai_deployment

``extra="forbid"`` and no defaults: a new key, a missing key or a wrong type raises WireError. A reply that
does not parse is never read as "nothing happened".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, ValidationError

DIGEST = r"^sha256:[0-9a-f]{64}$"
SESSION_DAY = r"^\d{4}-\d{2}-\d{2}$"
Scalar = Union[StrictBool, StrictInt, StrictFloat, StrictStr]
Verdict = Literal["DEPLOY", "SHADOW", "REJECT", "NO_VERDICT"]
DeploymentKind = Literal["INITIAL", "RENEWAL"]
# A PARKED request reads FAILED on the wire (EvaluationService.wire_state); a withheld case reads RUNNING.
REQUEST_STATE = Literal["CLAIMING", "REFUSED", "QUEUED", "RUNNING", "DONE", "FAILED"]


class WireError(ValueError):
    """A reply without the agreed shape. Never read as 'nothing happened'."""


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PointSummary(_Wire):
    """One cohort point of evaluation_summary. failed_rules are the pre-holdout rules."""
    index: StrictInt
    params: dict[StrictStr, Scalar]
    pre_holdout_passed: StrictBool
    failed_rules: list[StrictStr]
    missing_rules: list[StrictStr]
    metrics: dict[StrictStr, Optional[StrictFloat]]


class RuleResult(_Wire):
    """The final decision's rule results, per point."""
    point: StrictInt
    rule: StrictStr
    passed: StrictBool


class CaseSummary(_Wire):
    """evaluation_summary(case): the code-computed view Jev judges. A FAILED case has no points or metrics."""
    kind: Literal["INITIAL", "RENEWAL"]
    strategy_key: StrictStr
    strategy_path: StrictStr
    class_name: StrictStr
    file_hash: StrictStr = Field(pattern=DIGEST)
    params: Optional[dict[StrictStr, Scalar]]
    conids: list[StrictInt]
    bar_size: StrictStr
    stage: Literal["PRE_HOLDOUT_FAILED", "HOLDOUT_FAILED", "COMPLETE", "FAILED", "FORWARD_COMPLETE",
                   "FORWARD_INCOMPLETE"]
    rules_passed: StrictBool                                  # offered_menu(case) == FULL_MENU
    holdout_passed: Optional[StrictBool]
    eligibility: Optional[StrictStr]
    renewal_checks_passed: Optional[StrictBool]
    prior_version_digest: Optional[StrictStr]
    order_notional: StrictFloat
    strategy_trials: StrictInt = Field(ge=0)
    prior_holdouts: StrictInt = Field(ge=0)
    previously_revealed_sessions: StrictInt = Field(ge=0)
    selected_index: Optional[StrictInt]
    error: Optional[StrictStr]
    metrics: dict[StrictStr, Optional[StrictFloat]]
    points: list[PointSummary]
    rule_results: list[RuleResult]
    forward: Optional[dict[StrictStr, Any]]


class SubmitReply(_Wire):
    """submit_evaluation. The service sets research_day and the request id. A refusal before any claim
    (PRINCIPAL_FORBIDDEN, RENEWAL_NOT_SUPPORTED, a bad request) has no request id."""
    status: Literal["ACCEPTED", "DUPLICATE", "REFUSED"]
    request_id: Optional[StrictStr] = Field(pattern=DIGEST)
    state: Optional[REQUEST_STATE]
    code: Optional[StrictStr]
    detail: Optional[StrictStr]
    retryable: StrictBool                                     # the same body may be sent again later


class EvaluationView(_Wire):
    """get_evaluation. case_digest and summary appear only once the trader has the DONE/FAILED report."""
    found: StrictBool
    request_id: StrictStr = Field(pattern=DIGEST)
    state: Optional[REQUEST_STATE]
    case_digest: Optional[StrictStr] = Field(pattern=DIGEST)
    summary: Optional[CaseSummary]


class Binding(_Wire):
    """What the signed bundle binds, read back from the bundle. ATTESTED and DUPLICATE both carry it."""
    strategy_path: StrictStr
    class_name: StrictStr
    file_hash: StrictStr = Field(pattern=DIGEST)
    params: dict[StrictStr, Union[Scalar, list[Scalar]]]
    conids: list[StrictInt] = Field(min_length=1, max_length=20)
    bar_size: StrictStr
    order_notional: StrictFloat = Field(gt=0)


class AttestReply(_Wire):
    """attest_from_judgment. DUPLICATE is read exactly like ATTESTED. ``retryable`` is true for
    ATTEST_EXPORT_FAILED, and also for a deterministic bundle error: bound the retries."""
    status: Literal["ATTESTED", "DUPLICATE", "REFUSED"]
    bundle_digest: Optional[StrictStr] = Field(pattern=DIGEST)
    code: Optional[StrictStr]
    detail: Optional[StrictStr]
    retryable: StrictBool
    binding: Optional[Binding]


class JudgmentReceipt(_Wire):
    """record_backtest_judgment. A refusal carries verdict and cooldown as null."""
    status: Literal["RECORDED", "EXISTING", "REFUSED"]
    judgment_id: StrictStr
    code: Optional[StrictStr]
    detail: Optional[StrictStr]
    retryable: StrictBool
    verdict: Optional[Verdict]
    cooldown_until_session: Optional[StrictStr] = Field(pattern=SESSION_DAY)


class JudgmentRecord(_Wire):
    """BacktestJudgment.to_json(): the trader's durable record. body and binding are the trader's own JSON."""
    judgment_id: StrictStr
    case_digest: StrictStr = Field(pattern=DIGEST)
    request_id: Optional[StrictStr]
    kind: DeploymentKind
    verdict: Verdict
    strategy_key: StrictStr
    body: dict[StrictStr, Any]
    binding: dict[StrictStr, Any]
    cooldown_until_session: Optional[StrictStr] = Field(pattern=SESSION_DAY)
    recorded_at: StrictStr


class JudgmentView(_Wire):
    """get_backtest_judgment, by judgment_id or by case_digest."""
    found: StrictBool
    judgment: Optional[JudgmentRecord]


class VersionView(_Wire):
    version_digest: StrictStr = Field(pattern=DIGEST)
    base_digest: StrictStr = Field(pattern=DIGEST)
    judgment_id: StrictStr
    kind: DeploymentKind
    prior_version_digest: Optional[StrictStr] = Field(pattern=DIGEST)
    first_session: StrictStr = Field(pattern=SESSION_DAY)
    expiry_session: StrictStr = Field(pattern=SESSION_DAY)
    state: Literal["ACTIVE", "EXPIRED", "WITHDRAWN", "ENDED"]


class VersionReply(_Wire):
    """get_ai_deployment_version."""
    found: StrictBool
    version: Optional[VersionView]


def parse_reply(model: type[_Wire], method: str, reply: Any) -> Any:
    try:
        return model.model_validate_json(json.dumps(reply, allow_nan=False))
    except (ValidationError, TypeError, ValueError) as exc:
        raise WireError(f"{method}: reply has an unexpected shape ({type(exc).__name__})") from None


class _Receipt(_Wire):
    """asdict(CommandReceipt): the command ledger's answer to register_ai_deployment."""
    command_id: StrictStr
    correlation_id: StrictStr
    state: StrictStr
    outcome: Optional[dict[StrictStr, Any]]
    error_code: Optional[StrictStr]
    retryable: StrictBool


class _RegistrationOutcome(_Wire):
    """AiDeploymentRegistrar._outcome: ``digest`` is the base deployment, ``version_digest`` this registration."""
    digest: StrictStr = Field(pattern=DIGEST)
    version_digest: StrictStr = Field(pattern=DIGEST)
    kind: DeploymentKind
    first_session: StrictStr = Field(pattern=SESSION_DAY)
    expiry_session: StrictStr = Field(pattern=SESSION_DAY)
    created: StrictBool
    strategy_digest_provenance: StrictStr


@dataclass(frozen=True)
class Registered:
    base_digest: str
    version_digest: str
    expiry_session: str


@dataclass(frozen=True)
class RegisterRefused:
    code: str
    retryable: bool = False                   # the trader kept no ledger row: the same body may be sent again


def parse_registration(receipt: Any) -> Union[Registered, RegisterRefused, None]:
    """A ledger receipt: RESOLVED -> Registered, REJECTED -> RegisterRefused, anything else -> None (ask again)."""
    parsed = parse_reply(_Receipt, "register_ai_deployment", receipt)
    if parsed.state == "REJECTED":
        return RegisterRefused(parsed.error_code or "REJECTED_WITHOUT_CODE", parsed.retryable)
    if parsed.state != "RESOLVED":
        return None
    outcome = parse_reply(_RegistrationOutcome, "register_ai_deployment", parsed.outcome)
    return Registered(outcome.digest, outcome.version_digest, outcome.expiry_session)
