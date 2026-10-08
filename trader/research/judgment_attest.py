"""attest_from_judgment (SP2c spec 5.1): a durable DEPLOY judgment becomes the paper `llm` review, then the
unchanged attest/export signs the bundle. The trader's judgment record is the authority, never the caller."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from trader.automation.artifact_verifier import ArtifactVerifier, ArtifactVerifierError
from trader.automation.backtest_judge_wire import JEV_MODEL, JUDGMENT_ID
from trader.research.attest_export import (
    AttestExportError, attest_and_export, check_attestable, check_stored_attestation,
)
from trader.research.case_builder import json_safe
from trader.research.evaluation_case import CaseRefused, load_verified_case
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.review import (
    REVIEW_NARRATIVE_FIELDS, OperatorReview, OperatorReviewRepository, ReviewConflict,
)
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.strategy_key import split_strategy_key
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research.trader_port import TraderUnavailable

AI_RESEARCH = "ai_research"
RETRYABLE = frozenset({"TRADER_UNAVAILABLE", "ATTEST_EXPORT_FAILED"})
MAX_REFUSAL_DETAIL = 200
CALENDAR_UNAVAILABLE_CODES = frozenset({"COOLDOWN_CALENDAR_UNAVAILABLE", "DEPLOYMENT_CALENDAR_UNAVAILABLE"})
BINDING_FIELDS = {"artifact_id": "artifact_id", "family_id": "family_id", "params": "selected_params",
                  "conids": "conids", "bar_size": "bar_size", "strategy_file_hash": "strategy_file_hash"}


def is_paper_posture(environ: Mapping[str, str], trader_config: Mapping[str, Any]) -> bool:
    """Ruling 12: every posture signal must say paper."""
    configured = trader_config.get("trading_mode")
    return (environ.get("TRADING_MODE") == "paper" and configured in (None, "", "paper")
            and str(environ.get("IB_ACCOUNT", "")).startswith("DU"))


def is_loud_trader_code(code: str) -> bool:
    """The codes the trader answers as an RPC error (tampered record, bad case, no calendar), not a reply body.

    Same rule as ``ai_paper_actions.is_loud_refusal``, kept here so the research service does not import the
    trader's command stack."""
    return code.endswith("TAMPERED") or code.startswith("CASE_") or code in CALENDAR_UNAVAILABLE_CODES


class _Refused(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def _reply(status: str, bundle_digest=None, code=None, detail=None, binding=None) -> dict:
    return {"status": status, "bundle_digest": bundle_digest, "code": code, "detail": detail,
            "retryable": code in RETRYABLE, "binding": binding}


def _decided_at(raw: Any) -> dt.datetime:
    try:
        moment = dt.datetime.fromisoformat(str(raw))
    except ValueError:
        raise _Refused("JUDGMENT_INVALID", "decided_at is not ISO-8601") from None
    if moment.tzinfo is None:
        raise _Refused("JUDGMENT_INVALID", "decided_at has no offset")
    return moment


class JudgmentAttest:
    def __init__(self, *, research_db: Any, store: Any, trader: Any, signer: Any, artifacts_root: Path,
                 repo_root: Path, is_paper: Callable[[], bool], now: Callable[[], dt.datetime], ruleset=PAPER_V1):
        self._db, self._store, self._trader, self._signer = research_db, store, trader, signer
        self._artifacts_root, self._repo_root = artifacts_root, repo_root
        self._is_paper, self._now, self._ruleset = is_paper, now, ruleset
        self._lock = threading.Lock()

    def attest(self, body: Mapping[str, Any], caller: Any) -> dict:
        if caller.principal != AI_RESEARCH:
            return _reply("REFUSED", code="PRINCIPAL_FORBIDDEN", detail="ai_research only")
        try:
            judgment_id = body.get("judgment_id")
            if not isinstance(judgment_id, str) or not JUDGMENT_ID.fullmatch(judgment_id):
                raise _Refused("REQUEST_INVALID", "judgment_id must be a judgment id")
            with self._lock:
                status, bundle, binding = self._attest(judgment_id)
        except _Refused as refused:
            return _reply("REFUSED", code=refused.code, detail=refused.detail)
        return _reply(status, "sha256:" + bundle.name.removeprefix("sha256_"), binding=binding)

    def _attest(self, judgment_id: str) -> tuple[str, Path, dict]:
        if not self._is_paper():
            raise _Refused("ACCOUNT_NOT_PAPER", "attestation from a judgment is paper only")
        judgment = self._read_judgment(judgment_id)
        case = self._own_case(str(judgment.get("case_digest")))
        artifact = self._check_binding(case, judgment)
        review = self._review(case, judgment, artifact)
        self._require_attestable(case, artifact, review)
        status = self._record_review(review)
        try:
            bundle = attest_and_export(self._db, artifact_id=artifact.artifact_id, signer=self._signer,
                                       artifacts_root=self._artifacts_root, now=self._now(), ruleset=self._ruleset)
        except AttestExportError as exc:
            raise _Refused(exc.code, str(exc)) from None
        return status, bundle, self._bundle_binding(bundle, artifact.artifact_id, review.reviewer)

    def _require_attestable(self, case: Any, artifact: Any, review: OperatorReview) -> None:
        """What attest_and_export would refuse for reasons outside the review, found before any review is written."""
        try:
            _, _, decision = check_attestable(self._db, artifact.artifact_id, self._ruleset)
            check_stored_attestation(self._db, artifact.artifact_id, decision.digest, review.digest,
                                     self._signer, self._now())
        except AttestExportError as exc:
            raise _Refused(exc.code, str(exc)) from None
        if decision.digest != case.eligibility_decision_digest:
            raise _Refused("JUDGMENT_MISMATCH", "the case names another eligibility decision than the registry")

    def _read_judgment(self, judgment_id: str) -> Mapping[str, Any]:
        """The trader's durable record, read by id. Only a DEPLOY on an INITIAL case goes on."""
        try:
            judgment = self._trader.judgment(judgment_id=judgment_id)
        except TraderUnavailable as exc:
            raise _Refused("TRADER_UNAVAILABLE", str(exc)) from None
        except TypedRpcRemoteError as error:
            if not is_loud_trader_code(error.code):              # any other remote error is a bug: let it surface
                raise
            raise _Refused(error.code, error.message[:MAX_REFUSAL_DETAIL]) from None
        if judgment is None:
            raise _Refused("JUDGMENT_MISSING", f"the trader has no judgment {judgment_id}")
        if judgment.get("judgment_id") != judgment_id:
            raise _Refused("JUDGMENT_MISMATCH", "the trader answered with another judgment")
        if judgment.get("verdict") != "DEPLOY" or judgment.get("kind") != "INITIAL":
            raise _Refused("JUDGMENT_NOT_DEPLOY",
                           f"judgment {judgment_id} is {judgment.get('kind')} {judgment.get('verdict')}")
        return judgment

    def _bundle_binding(self, bundle: Path, artifact_id: str, reviewer: str) -> dict:
        """The facts Plan 2's binding_differences compares, read back from the bundle this service just signed."""
        try:
            verified = ArtifactVerifier([self._signer.public_key]).verify(bundle, "paper", artifact_id, self._now())
        except (ArtifactVerifierError, OSError, ValueError) as exc:
            raise _Refused("ATTEST_FAILED", f"the signed bundle does not verify: {type(exc).__name__}") from None
        attested = verified.attested_strategy
        if attested is None or attested.bar_size is None or attested.order_notional is None:
            raise _Refused("ATTEST_FAILED", "the signed bundle lacks the strategy, bar size or order notional")
        self._require_bundle_review(bundle, reviewer)
        return {"strategy_path": attested.strategy_path, "class_name": attested.class_name,
                "file_hash": "sha256:" + attested.source_digest, "params": json_safe(dict(verified.parameters)),
                "conids": sorted(int(conid) for conid in verified.allowlist), "bar_size": attested.bar_size,
                "order_notional": attested.order_notional}

    @staticmethod
    def _require_bundle_review(bundle: Path, reviewer: str) -> None:
        """Plan 2 registers only a bundle whose review is the llm review of this judgment."""
        try:
            review = json.loads((bundle / "review.json").read_text())      # checksum-verified with the bundle
            named = (review["reviewer"], review["reviewer_kind"])
        except (OSError, ValueError, KeyError, TypeError):
            raise _Refused("ATTEST_FAILED", "the signed bundle's review cannot be read") from None
        if named != (reviewer, "llm"):
            raise _Refused("ATTEST_FAILED", "the signed bundle's review is not this judgment's llm review")

    def _own_case(self, digest: str) -> Any:
        """The judgment must name a case this service signed; the file must still verify and qualify."""
        if self._store.case(case_digest=digest) is None:
            raise _Refused("CASE_UNKNOWN", f"case {digest} was not signed by this research service")
        try:
            case = load_verified_case(Path(self._artifacts_root) / "cases", digest,
                                      {self._signer.public_key_id: self._signer.public_key})
        except CaseRefused as refused:
            raise _Refused(refused.code, refused.detail) from None
        holdout = case.evidence.get("holdout") or {}
        qualifies = (case.kind == "INITIAL" and case.stage == "COMPLETE" and case.decision_state == "PAPER_ELIGIBLE"
                     and case.ruleset_digest == self._ruleset.digest and case.holdout_passed is True
                     and holdout.get("passed") is True
                     and case.final_rule_results and all(r.passed for r in case.final_rule_results))
        if not qualifies:
            raise _Refused("CASE_NOT_DEPLOYABLE", f"case stage {case.stage} cannot lead to a bundle")
        return case

    def _check_binding(self, case: Any, judgment: Mapping[str, Any]) -> Any:
        """The trader's binding, the case header, the registry and the file on disk name one candidate."""
        binding = judgment.get("binding") or {}
        if any(binding.get(key) != getattr(case, attr) for key, attr in BINDING_FIELDS.items()):
            raise _Refused("JUDGMENT_MISMATCH", "the judgment's binding and the case differ")
        registry = ExperimentRegistry(self._db)
        artifact = registry.get_artifact(case.artifact_id)
        family = None if artifact is None else registry.get_family(artifact.family_id)
        if artifact is None or family is None:
            raise _Refused("JUDGMENT_MISMATCH", "the case names an artifact the registry does not have")
        path, class_name = split_strategy_key(case.strategy_key)
        protocol = family.validation_protocol
        bound = (artifact.family_id == case.family_id
                 and dict(artifact.selected_parameters) == dict(case.selected_params)
                 and (family.strategy_path, family.class_name) == (path, class_name)
                 and "sha256:" + family.source_tree_digest == case.strategy_file_hash
                 and sorted(protocol["conids"]) == list(case.conids) and protocol["bar_size"] == case.bar_size
                 and artifact.holdout_opened is True and artifact.holdout_passed is True)
        if not bound:
            raise _Refused("JUDGMENT_MISMATCH", "the case and the registry name different bindings")
        try:
            current = (Path(self._repo_root) / path).read_bytes()
        except OSError:
            raise _Refused("STRATEGY_SOURCE_CHANGED", "the strategy file is no longer readable") from None
        if "sha256:" + hashlib.sha256(current).hexdigest() != case.strategy_file_hash:
            raise _Refused("STRATEGY_SOURCE_CHANGED", "the strategy file changed after the evaluation")
        return artifact

    def _review(self, case: Any, judgment: Mapping[str, Any], artifact: Any) -> OperatorReview:
        body = judgment.get("body")
        if not isinstance(body, Mapping):
            raise _Refused("JUDGMENT_INVALID", "the judgment has no body")
        jev_model = body.get("jev_model")              # the trader's record names the model, never the caller
        if not isinstance(jev_model, str) or not JEV_MODEL.fullmatch(jev_model):
            raise _Refused("JUDGMENT_INVALID", "the judgment names no valid jev_model")
        narrative = body.get("narrative") or {}
        if not isinstance(narrative, Mapping) or set(narrative) != set(REVIEW_NARRATIVE_FIELDS):
            raise _Refused("NARRATIVE_INVALID", "the judgment does not carry exactly the §8.5 narrative fields")
        try:
            return OperatorReview(
                artifact_id=artifact.artifact_id, eligibility_decision_digest=case.eligibility_decision_digest,
                reviewer=f"{jev_model}#{judgment['judgment_id']}",
                reviewed_at=_decided_at(body.get("decided_at")),
                holdout_opened_once_confirmed=artifact.holdout_opened,   # set by code from the registry
                reviewer_kind="llm", **{name: narrative[name] for name in REVIEW_NARRATIVE_FIELDS})
        except ValueError as exc:
            raise _Refused("NARRATIVE_INVALID", str(exc)) from None

    def _record_review(self, review: OperatorReview) -> str:
        try:
            inserted = OperatorReviewRepository(self._db).record_as_only_review(review)
        except ReviewConflict:
            raise _Refused("REVIEW_CONFLICT", "another review already exists for this decision") from None
        return "ATTESTED" if inserted else "DUPLICATE"
