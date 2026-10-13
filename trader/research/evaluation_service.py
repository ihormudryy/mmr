"""submit_evaluation / get_evaluation (SP2c spec 5.1): claim at the trader first, then queue; one worker."""
from __future__ import annotations

import datetime as dt
import logging
import queue
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional
from zoneinfo import ZoneInfo

from trader.authored.keys import is_authored_key, parse_authored_key
from trader.research.authored_outcome import AUTHORED_END_CODE, AuthoredEnd, AuthoredEvaluationEnded
from trader.research.case_builder import build_failed_case, build_initial_case, evaluation_summary
from trader.research.cohort import RequestRefused, request_body
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research.evaluation_case import CaseRefused, load_verified_case, write_evaluation_case
from trader.research.evaluation_request import EvaluationRequestBody, evaluation_request_id
from trader.research.judgment_attest import is_loud_trader_code
from trader.research.service_store import CLAIM_CLOSE_REFUSED, CLAIM_CLOSED, OPEN_STATES
from trader.research.strategy_key import split_evaluation_key
from trader.research.trader_port import TraderUnavailable

logger = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
AI_RESEARCH = "ai_research"
READERS = frozenset({"ai_research", "cli"})
FINISHED_CLAIM_STATES = ("DONE", "FAILED")
RETRYABLE_STATES = ("CLAIMING", "REFUSED")
PARK_DETAIL_MAX = 200
# Review r1 #7: an authored end the trader refused for good is parked, resent hourly (and at a restart), and the
# claim stays open; refusal is never delivery.
PARKED_RESEND_SECONDS = 3600
# PR #161 review: a restart found an authored evaluation RUNNING with its holdout opened and no recorded end.
AUTHORED_OUTCOME_UNKNOWN = "AUTHORED_OUTCOME_UNKNOWN"
CASE_ERROR_MESSAGE_MAX = 120
_UNSAFE_ERROR_TEXT = re.compile(
    r"[/\\]"                                                                  # a file path
    r"|\b(?:select|insert|update|delete|create|drop|alter)\b.*\b(?:from|into|table|set|where)\b"   # SQL
    r"|-----|\b(?:private|secret|token|password|pem)\b|[A-Za-z0-9+=_-]{32,}",  # key material, long tokens
    re.IGNORECASE)


class ServiceStateError(RuntimeError):
    """The research DB and the trader's claims disagree; the operator must look."""


def case_error(label: str, message: str) -> str:
    """The FAILED case's error: the exception type or refusal code, plus its first line when short and safe.

    The case is signed and Jev reads it, so a message with a path, SQL text or key material is dropped;
    the service log keeps the full exception for the operator.
    """
    lines = message.strip().splitlines()
    first = lines[0].strip() if lines else ""
    if not first or _UNSAFE_ERROR_TEXT.search(first):
        return label
    return f"{label}: {first[:CASE_ERROR_MESSAGE_MAX]}"


def wire_state(state: Optional[str]) -> Optional[str]:
    """A parked request is a terminal non-success; Plan 4's wire has no PARKED, so it reads as FAILED (no case)."""
    return "FAILED" if state == "PARKED" else state


def _submit_reply(status: str, request_id: Optional[str] = None, state: Optional[str] = None,
                  code: Optional[str] = None, detail: Optional[str] = None, retryable: bool = False) -> dict:
    """``retryable``: the caller may resend the same body later (nothing was decided)."""
    return {"status": status, "request_id": request_id, "state": state, "code": code, "detail": detail,
            "retryable": retryable}


class EvaluationService:
    def __init__(self, *, store: Any, trader: Any, build_spec: Callable[[EvaluationRequestBody], Any],
                 evaluate: Callable[[Any], Any], signer: Any, artifacts_root: Path, warmup_sessions: int,
                 order_notional: float, queue_max: int, now: Callable[[], dt.datetime], renewals: Any = None,
                 holdout_opened: Callable[[str], bool] = lambda strategy_key: False):
        self._store, self._trader, self._build_spec, self._evaluate = store, trader, build_spec, evaluate
        self._holdout_opened = holdout_opened                   # holdout_opened_in(registry) in the service
        self._renewals = renewals                               # kind RENEWAL (SP2c Plan 5); None: refused
        self._signer, self._cases_dir = signer, Path(artifacts_root) / "cases"
        self._warmup_sessions, self._order_notional = warmup_sessions, order_notional
        self._queue_max, self._now = queue_max, now
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._submit_lock = threading.Lock()
        self._worker_lock = threading.Lock()                    # plan ruling 20: one evaluation at a time
        self._refused_at: dict[str, dt.datetime] = {}         # request id -> when the trader last refused its end

    def _today(self) -> dt.date:
        return self._now().astimezone(NEW_YORK).date()

    # -- submit_evaluation ------------------------------------------------------------

    def submit(self, raw: Mapping[str, Any], caller: Any) -> dict:
        if caller.principal != AI_RESEARCH:
            return _submit_reply("REFUSED", code="PRINCIPAL_FORBIDDEN", detail="only ai_research submits")
        if raw.get("kind") == "RENEWAL":
            if self._renewals is None:
                return _submit_reply("REFUSED", code="RENEWAL_NOT_SUPPORTED", detail="this service builds no renewals")
            return self._renewals.submit(raw["prior_version_digest"])
        if raw.get("kind") != "INITIAL":
            return _submit_reply("REFUSED", code="REQUEST_INVALID", detail="kind must be INITIAL or RENEWAL")
        try:
            body = request_body({k: v for k, v in raw.items() if k != "kind"}, self._today())
        except RequestRefused as refusal:                       # nothing is claimed
            return _submit_reply("REFUSED", code=refusal.code, detail=refusal.detail, retryable=refusal.retryable)
        request_id = evaluation_request_id(body)
        with self._submit_lock:
            row = self._store.get(request_id)
            if row is not None and row["state"] not in RETRYABLE_STATES:
                return _submit_reply("DUPLICATE", request_id, wire_state(row["state"]))   # before any recheck
            try:
                return self._claim_new_or_retried(request_id, body, row)
            except TraderUnavailable as exc:
                return _submit_reply("REFUSED", request_id, code="CLAIM_UNKNOWN", detail=str(exc), retryable=True)

    def _claim_new_or_retried(self, request_id: str, body: EvaluationRequestBody, row: Optional[dict]) -> dict:
        """A claim the trader already holds has spent its slot and runs; anything else is checked, then claimed.

        The queue bounds new claims only.
        """
        if row is not None and row["state"] == "CLAIMING":
            held = self._trader.claim_readback(request_id)
            if held is not None:
                return self._accept(request_id, {"status": "EXISTING", "claim": held})
        try:
            spec = self._build_spec(body)
        except RequestRefused as refusal:                       # nothing is claimed
            return _submit_reply("REFUSED", code=refusal.code, detail=refusal.detail, retryable=refusal.retryable)
        # A parked authored end stays RUNNING, so it holds a slot until the trader accepts it.
        if self._store.count(OPEN_STATES) >= self._queue_max:
            return _submit_reply("REFUSED", request_id, code="QUEUE_FULL",
                                 detail=f"{self._queue_max} evaluations are already waiting", retryable=True)
        self._store.begin(request_id, body.model_dump(), spec.strategy_key, spec.file_hash, self._now())
        return self._accept(request_id, self._claim(request_id, body.model_dump()))

    def _claim(self, request_id: str, body: dict) -> dict:
        """Spec 5.1 step 5: after a lost reply, read the claim back before any retry."""
        try:
            return self._trader.claim(request_id, body)
        except TraderUnavailable:
            claim = self._trader.claim_readback(request_id)
            if claim is not None:
                return {"status": "EXISTING", "claim": claim, "code": None, "detail": None}
            return self._trader.claim(request_id, body)   # same id and body: never a second slot

    def _accept(self, request_id: str, reply: dict) -> dict:
        if reply["status"] == "REFUSED":
            self._store.set_state(request_id, "REFUSED", now=self._now(), code=reply["code"], detail=reply["detail"])
            return _submit_reply("REFUSED", request_id, code=reply["code"], detail=reply["detail"],
                                 retryable=bool(reply.get("retryable")))
        claim = reply["claim"]
        if claim is None or claim["request_id"] != request_id:
            raise ServiceStateError(f"the trader answered {reply['status']} without the claim for {request_id}")
        claim_day = dt.date.fromisoformat(claim["ny_day"])
        if claim["state"] in FINISHED_CLAIM_STATES:
            self._store.set_state(request_id, "FAILED", now=self._now(), ny_day=claim_day,
                                  code="CLAIM_ALREADY_FINISHED", detail="the trader already closed this claim")
            return _submit_reply("REFUSED", request_id, "FAILED", "CLAIM_ALREADY_FINISHED")
        self._store.set_state(request_id, "QUEUED", now=self._now(), ny_day=claim_day)
        self._queue.put(request_id)
        return _submit_reply("ACCEPTED", request_id, "QUEUED")

    # -- get_evaluation ----------------------------------------------------------------

    def get(self, request_id: str, caller: Any) -> dict:
        row = self._store.get(request_id) if caller.principal in READERS else None
        if row is None:
            return {"found": False, "request_id": request_id, "state": None, "case_digest": None, "summary": None}
        if row["pending_report"] is not None:                 # ruling 24: the trader's claim is not closed yet
            return {"found": True, "request_id": request_id, "state": "RUNNING", "case_digest": None, "summary": None}
        if row["state"] == "PARKED":                          # whatever case it had is not to be judged
            return {"found": True, "request_id": request_id, "state": wire_state("PARKED"), "case_digest": None,
                    "summary": None}
        return {"found": True, "request_id": request_id, "state": row["state"], "case_digest": row["case_digest"],
                "summary": row["summary"]}

    # -- restart and the worker --------------------------------------------------------

    def recover(self) -> None:
        """Spec 5.1 step 6: resume our QUEUED and RUNNING work under the same claim; report finished states."""
        for row in self._store.pending():
            if row["pending_report"] is None:                 # signed already: only its report is owed (below)
                with self._parking(row["request_id"]):
                    self._recover_row(row)
        self.report_pending()

    def _recover_row(self, row: dict) -> None:
        claim = self._trader.claim_readback(row["request_id"])
        if claim is None:
            if row["state"] == "CLAIMING":
                return                                        # never accepted; the caller's retry claims again
            raise ServiceStateError(f"{row['request_id']} is {row['state']} here but the trader has no claim")
        if row["state"] == "CLAIMING":
            self._accept(row["request_id"], {"status": "EXISTING", "claim": claim})
        else:
            self._queue.put(row["request_id"])

    @contextmanager
    def _parking(self, request_id: str) -> Iterator[None]:
        """A request that can never finish is parked loudly so the worker and a restart move on.

        Infrastructure failures (the trader down, a bad signature, an internal error) are not about this
        request: they pass through and end the service.
        """
        try:
            yield
        except CaseRefused as refused:
            self._park(request_id, refused.code, refused.detail)
        except ServiceStateError as mismatch:
            self._park(request_id, "SERVICE_STATE_MISMATCH", str(mismatch))
        except TypedRpcRemoteError as remote:
            if not is_loud_trader_code(remote.code):
                raise
            self._park(request_id, remote.code, remote.message)

    def _park(self, request_id: str, code: str, detail: str) -> None:
        logger.error("evaluation %s parked: %s: %s; it will not run again and needs the operator",
                     request_id, code, detail)
        reason = f"{code}: {detail}"[:PARK_DETAIL_MAX]
        row = self._store.get(request_id)
        if row is None or not is_authored_key(row["strategy_key"]):
            self._store.park(request_id, reason, now=self._now())
            return
        self._store.park_owing_claim_close(request_id, reason, now=self._now())
        self._close_parked_claim(request_id)

    def _close_parked_claims(self) -> None:
        for row in self._store.parked_owing_claim_close():
            self._close_parked_claim(row["request_id"])

    def _close_parked_claim(self, request_id: str) -> None:
        """Plan 4 F5: a parked authored request never finishes, so its claim closes FAILED, and Plan 3's grace
        returns a record left EVALUATING to STORED. Best effort and never raised: tried again every tick."""
        try:
            reply = self._trader.update_claim(request_id, "FAILED")
        except TraderUnavailable:
            if not self._claim_reads(request_id, "FAILED"):
                logger.warning("parked evaluation %s: claim not closed FAILED yet; tried again next tick", request_id)
                return
            reply = {"status": "UNCHANGED"}
        except TypedRpcRemoteError as remote:
            reply = {"status": "REFUSED", "code": remote.code, "retryable": False}
        if reply.get("status") in ("UPDATED", "UNCHANGED"):
            self._store.settle_claim_close(request_id, CLAIM_CLOSED, now=self._now())
        elif reply.get("retryable"):
            logger.warning("parked evaluation %s: the trader asks to resend the claim close (%s)", request_id,
                           reply.get("code"))
        else:
            logger.error("parked evaluation %s: the trader refused to close its claim FAILED (%s); the operator "
                         "must close it", request_id, reply.get("code"))
            self._store.settle_claim_close(request_id, CLAIM_CLOSE_REFUSED, now=self._now())

    def report_pending(self) -> bool:
        """Ruling 24: send every owed DONE/FAILED again; true when the trader confirmed at least one."""
        self._close_parked_claims()
        confirmed = False
        for row in self._store.pending_reports():
            with self._parking(row["request_id"]):
                confirmed |= self._report_end(row["request_id"], row["pending_report"])
        return confirmed

    def run_next(self) -> bool:
        """One service tick: owed reports first (no restart needed), then one queued evaluation.

        A tick that finds another tick still running does nothing and returns False.
        """
        if not self._worker_lock.acquire(blocking=False):
            return False
        try:
            reported = self.report_pending()
            try:
                request_id = self._queue.get_nowait()
            except queue.Empty:
                return reported
            self._run(request_id)
            return True
        finally:
            self._worker_lock.release()

    def serve_forever(self, stop: threading.Event, idle_seconds: float = 1.0) -> None:
        while not stop.is_set():
            if not self.run_next():
                stop.wait(idle_seconds)

    def _run(self, request_id: str) -> None:
        with self._parking(request_id):
            self._run_row(request_id)

    def _run_row(self, request_id: str) -> None:
        row = self._store.get(request_id)
        if row["state"] not in OPEN_STATES or row["pending_report"] is not None:
            return
        if AuthoredEnd.from_row(row) is not None:             # decided before a crash: never run again
            self._end_without_case(request_id)
            return
        self._refuse_lost_authored_outcome(row)
        self._store.set_state(request_id, "RUNNING", now=self._now())
        signed = self._store.case(request_id=request_id)
        if signed is None:
            self._report_running(request_id)
            try:
                digest, case = self._evaluate_and_sign(row)
            except AuthoredEvaluationEnded as ended:
                logger.warning("authored evaluation %s ended %s (%s); no case is signed", request_id,
                               ended.end.event, ended.end.detail)
                self._store.set_state(request_id, "RUNNING", now=self._now(), code=AUTHORED_END_CODE,
                                      detail=ended.end.marker())
                self._end_without_case(request_id)
                return
        else:                                                   # signed before a crash: never run twice
            digest = signed["case_digest"]
            case = load_verified_case(self._cases_dir, digest, {self._signer.public_key_id: self._signer.public_key})
        final = "FAILED" if case.stage == "FAILED" else "DONE"   # the trader refuses CASE_CLAIM_MISMATCH otherwise
        self._store.hold_report(request_id, final, now=self._now(), case_digest=digest,
                                summary=evaluation_summary(case, order_notional=self._order_notional))
        self._report_end(request_id, final)

    def _refuse_lost_authored_outcome(self, row: dict) -> None:
        """An authored run cut by a crash after its holdout opened may have ended in a strategy fault whose end
        was never recorded. A rerun cannot tell: the holdout is spent, so it would sign an ordinary FAILED case
        and the fault would never reach the trader. The request is parked instead, its claim closes FAILED
        without a case, and Plan 3 returns the record to STORED or FAILED (PR #161 review)."""
        if row["state"] != "RUNNING" or not is_authored_key(row["strategy_key"]):
            return
        if self._store.case(request_id=row["request_id"]) is not None or not self._holdout_opened(row["strategy_key"]):
            return
        raise CaseRefused(AUTHORED_OUTCOME_UNKNOWN, "a restart found this authored evaluation running after its "
                                                    "holdout opened, with no recorded end; no case is signed")

    def _end_without_case(self, request_id: str) -> None:
        """Plan 4 ruling 11: get_evaluation reads FAILED with no case, so no model ever judges it."""
        self._store.hold_report(request_id, "FAILED", now=self._now(), case_digest=None, summary=None)
        self._report_end(request_id, "FAILED")

    def _evaluate_and_sign(self, row: dict) -> tuple[str, Any]:
        created_at = self._now()
        body = EvaluationRequestBody.model_validate(row["body"])
        claim_day = str(row["ny_day"])

        def failed(error: str):
            return build_failed_case(body, request_id=row["request_id"], claim_day=claim_day,
                                     file_hash=row["file_hash"], error=error, created_at=created_at,
                                     warmup_sessions=self._warmup_sessions)
        try:
            spec = self._build_spec(body)
            if spec.file_hash != row["file_hash"]:
                raise RequestRefused("STRATEGY_SOURCE_CHANGED", "the strategy file changed after the claim")
            case = build_initial_case(spec, claim_day, self._evaluate(spec), created_at=created_at,
                                      warmup_sessions=self._warmup_sessions)
        except (AuthoredEvaluationEnded, ServiceStateError, TraderUnavailable):
            raise                       # not a case: an authored end, a parked request or an unreachable trader
        except RequestRefused as refusal:
            logger.warning("evaluation %s refused after its claim: %s", row["request_id"], refusal.code)
            case = failed(case_error(refusal.code, refusal.detail))
        except Exception as exc:                                # recorded, logged, never swallowed silently
            logger.exception("evaluation %s failed", row["request_id"])
            case = failed(case_error(type(exc).__name__, str(exc)))
        digest = write_evaluation_case(self._cases_dir, case, self._signer)
        self._store.record_case(digest, row["request_id"], case.stage, created_at)
        return digest, case

    def _report_running(self, request_id: str) -> None:
        """Best effort: a lost RUNNING blocks nothing (Plan 1 allows QUEUED -> DONE)."""
        try:
            self._trader.update_claim(request_id, "RUNNING")
        except TraderUnavailable:
            logger.warning("claim %s: RUNNING not reported; the end report follows anyway", request_id)

    def _report_end(self, request_id: str, final: str) -> bool:
        """Ruling 24: confirmed by UPDATED or UNCHANGED, or after a lost reply by reading the claim back."""
        if not self._authored_outcome_delivered(request_id):
            return False
        try:
            reply = self._trader.update_claim(request_id, final)
            confirmed = reply["status"] in ("UPDATED", "UNCHANGED")
            if not confirmed:
                logger.error("claim %s: the trader refused %s (%s); the case stays withheld", request_id, final,
                             reply.get("code"))
        except TraderUnavailable:
            confirmed = self._claim_reads(request_id, final)
        if confirmed:
            self._store.confirm_report(request_id, now=self._now())
        else:
            logger.warning("claim %s: %s not confirmed by the trader yet; sent again next tick", request_id, final)
        return confirmed

    def _authored_outcome_delivered(self, request_id: str) -> bool:
        """Plan 4 ruling 10: an authored end reaches the trader before its claim closes, so the record never
        stays EVALUATING behind a closed claim. Resent every tick until the trader answers; a final refusal parks
        the end (ruling 10, review r1 #7): never treated as delivered."""
        row = self._store.get(request_id)
        end = AuthoredEnd.from_row(row)
        if end is None:
            return True
        refused_at = self._refused_at.get(request_id)
        if refused_at is not None and self._now() < refused_at + dt.timedelta(seconds=PARKED_RESEND_SECONDS):
            return False
        digest, _ = parse_authored_key(row["strategy_key"])
        try:
            reply = self._trader.report_authored_evaluation(end.report_body(request_id, digest))
        except TraderUnavailable:
            logger.warning("authored evaluation %s: %s not reported yet; sent again next tick", request_id, end.event)
            return False
        except TypedRpcRemoteError as remote:
            if not is_loud_trader_code(remote.code):      # as update_claim: an infrastructure error ends the service
                raise
            reply = {"status": "REFUSED", "code": remote.code, "detail": remote.message, "retryable": False}
        if reply.get("status") in ("UPDATED", "UNCHANGED"):
            self._refused_at.pop(request_id, None)
            return True
        if reply.get("retryable"):
            logger.warning("authored evaluation %s: the trader asks to resend (%s); sent again next tick",
                           request_id, reply.get("code"))
            return False
        self._park_end(request_id, end, digest, reply)
        return False

    def _park_end(self, request_id: str, end: AuthoredEnd, digest: str, reply: Mapping[str, Any]) -> None:
        first_refusal = request_id not in self._refused_at
        self._refused_at[request_id] = self._now()
        if not first_refusal:
            logger.warning("authored evaluation %s: the trader still refuses %s (%s); sent again in %s s",
                           request_id, end.event, reply.get("code"), PARKED_RESEND_SECONDS)
            return
        logger.error("AUTHORED_REPORT_REFUSED: authored evaluation %s: the trader refused %s (%s: %s); the claim "
                     "stays open and the end is sent again in %s s (after a restart: at once); check "
                     "`mmr ai-author list` for digest %s", request_id, end.event, reply.get("code"),
                     reply.get("detail"), PARKED_RESEND_SECONDS, digest)

    def _claim_reads(self, request_id: str, state: str) -> bool:
        try:
            claim = self._trader.claim_readback(request_id)
        except TraderUnavailable:
            return False
        return claim is not None and claim["state"] == state


def holdout_opened_in(registry: Any) -> Callable[[str], bool]:
    """Whether a holdout of the key's strategy was ever opened (SP2c: an opened window counts as revealed)."""
    return lambda strategy_key: bool(registry.opened_holdout_windows(*split_evaluation_key(strategy_key)))
