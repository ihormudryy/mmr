"""submit_evaluation, kind RENEWAL (SP2c spec 6.2): sign a renewal case from the trader's forward evidence.

No claim, no queue, no backtest, no trial, no holdout (rulings 2 and 3). Anything that makes the renewal
impossible is refused before a case exists, so Jev is never asked about it (ruling 8). A tampered record at the
trader ends the request for good: it is parked loudly and every resend answers DUPLICATE. Any other trader error
is a retryable TRADER_ERROR refusal."""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research.case_builder import evaluation_summary
from trader.research.evaluation_case import write_evaluation_case
from trader.research.evaluation_service import PARK_DETAIL_MAX, _submit_reply as submit_reply, wire_state
from trader.research.forward_evidence_view import ForwardEvidenceView
from trader.research.renewal_case import build_renewal_case, pending_sessions, renewal_request_id
from trader.research.strategy_key import split_strategy_key
from trader.research.trader_port import TraderUnavailable

logger = logging.getLogger(__name__)
TAMPERED_SUFFIX = "TAMPERED"


class RenewalRequests:
    def __init__(self, *, store: Any, trader: Any, signer: Any, artifacts_root: Path, repo_root: Path, judge: Any,
                 warmup_sessions: int, incomplete_after_hours: float, now: Callable[[], dt.datetime]):
        self._store, self._trader, self._signer = store, trader, signer
        self._cases_dir, self._repo_root = Path(artifacts_root) / "cases", Path(repo_root)
        self._judge, self._warmup_sessions = judge, warmup_sessions
        self._incomplete_after_hours, self._now = incomplete_after_hours, now
        self._lock = threading.Lock()

    def submit(self, prior_version_digest: str) -> dict:
        request_id = renewal_request_id(prior_version_digest)
        body = {"kind": "RENEWAL", "prior_version_digest": prior_version_digest}
        with self._lock:
            row = self._store.get(request_id)
            if row is not None:
                return submit_reply("DUPLICATE", request_id, wire_state(row["state"]))
            try:
                reply = self._trader.forward_evidence(prior_version_digest)
            except TraderUnavailable as exc:
                return submit_reply("REFUSED", request_id, code="TRADER_UNAVAILABLE", detail=str(exc), retryable=True)
            except TypedRpcRemoteError as remote:
                if remote.code.endswith(TAMPERED_SUFFIX):
                    return self._park(request_id, body, remote.code, remote.message)
                return self._trader_error(request_id, remote)
            if reply["status"] != "FOUND":
                return submit_reply("REFUSED", request_id, code=reply["code"] or "FORWARD_EVIDENCE_REFUSED",
                                    detail=reply["detail"])
            view = ForwardEvidenceView.model_validate(reply["evidence"])     # a bad shape raises: fail loudly
            if view.version_digest != prior_version_digest:
                raise ValueError(f"the trader answered for {view.version_digest}, not {prior_version_digest}")
            refusal = self._refusal(view)
            if refusal is not None:
                code, detail, retryable = refusal
                return submit_reply("REFUSED", request_id, code=code, detail=detail, retryable=retryable)
            self._sign_and_record(request_id, body, view)
            return submit_reply("ACCEPTED", request_id, "DONE")

    def _park(self, request_id: str, body: dict, code: str, detail: str) -> dict:
        """The trader's record is tampered: never a business refusal, never asked again (the operator must look).

        get_evaluation reads the parked request as FAILED with no case."""
        logger.error("renewal %s parked: the trader answered %s: %s; it will not be asked again and needs the "
                     "operator", request_id, code, detail)
        self._store.record_parked_renewal(request_id, body, f"{code}: {detail}"[:PARK_DETAIL_MAX], now=self._now())
        return submit_reply("ACCEPTED", request_id, wire_state("PARKED"), code=code, detail=detail[:PARK_DETAIL_MAX])

    @staticmethod
    def _trader_error(request_id: str, remote: TypedRpcRemoteError) -> dict:
        """A bug or a passing fault at the trader (no calendar): no row, so the controller's resend asks again."""
        detail = f"{remote.code}: {remote.message}"[:PARK_DETAIL_MAX]
        logger.error("renewal %s: the trader's forward evidence read failed: %s; refused for now", request_id, detail)
        return submit_reply("REFUSED", request_id, code="TRADER_ERROR", detail=detail, retryable=True)

    def _refusal(self, view: ForwardEvidenceView) -> Optional[tuple[str, str, bool]]:
        if not view.renewable.ok:
            return view.renewable.code or "RENEWAL_REFUSED", view.renewable.detail, False
        if not self._judge.allows(view.binding.strategy_key):
            return "STRATEGY_NOT_ALLOWED", f"{view.binding.strategy_key} is off strategy_allowlist", False
        if self._file_hash(view.binding.strategy_key) != view.binding.strategy_file_hash:
            return "STRATEGY_SOURCE_CHANGED", "the strategy file on disk is not the deployed bytes", False
        waiting = pending_sessions(view, now=self._now(), incomplete_after_hours=self._incomplete_after_hours)
        if waiting:
            return "FORWARD_EVIDENCE_PENDING", f"shadow rows not final yet for {waiting}", True
        return None

    def _file_hash(self, strategy_key: str) -> Optional[str]:
        """Read by the key, not the stored path: a stored path may be absolute (a container path)."""
        path, _ = split_strategy_key(strategy_key)
        try:
            return "sha256:" + hashlib.sha256((self._repo_root / path).read_bytes()).hexdigest()
        except OSError:
            return None

    def _sign_and_record(self, request_id: str, body: dict, view: ForwardEvidenceView) -> None:
        created_at = self._now()
        case = build_renewal_case(view, created_at=created_at, warmup_sessions=self._warmup_sessions)
        digest = write_evaluation_case(self._cases_dir, case, self._signer)
        self._store.record_renewal(
            request_id, body, strategy_key=case.strategy_key, file_hash=case.strategy_file_hash, case_digest=digest,
            stage=case.stage, summary=evaluation_summary(case, order_notional=view.binding.order_notional),
            now=created_at)
