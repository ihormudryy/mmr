"""P3 Task 3 — automated intent command saga (boundary only).

Routes a verified ``ExecutionIntent`` through the existing
``TradingCommandCoordinator``. Protective-order construction is Task 5; this
module validates principal + intent identity, re-verifies the artifact bundle,
audits authority digests (via the coordinator's RECEIVED audit of ``body``),
and dispatches through an injected port so tests can prove exactly-once
delivery without talking to IB.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol

from trader.automation.models import (
    EntryPolicy,
    ExecutionIntent,
    StopPolicy,
    TargetPolicy,
    TimeExitPolicy,
)
from trader.domain.commands import CommandReceipt
from trader.trading.command_coordinator import CommandRequest
from trader.trading.order_correlation import encode_order_ref

STRATEGY_PRINCIPAL = "strategy_service"
_ALLOWED_PRINCIPALS = frozenset({STRATEGY_PRINCIPAL})


class IntentDispatchPort(Protocol):
    def submit(self, *, intent: ExecutionIntent, order_group_id: str, order_ref: str) -> Any:
        ...


class ArtifactVerifierPort(Protocol):
    def verify(
        self,
        bundle_path: Path,
        expected_mode: str,
        expected_artifact_id: str,
        now: dt.datetime,
        *,
        revoked_digests: tuple[str, ...] = (),
    ) -> Any:
        ...


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _parse_ts(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return _as_utc(value)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        return _as_utc(dt.datetime.fromisoformat(text))
    raise ValueError(f"unsupported timestamp: {value!r}")


def _parse_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def intent_from_body(body: Mapping[str, Any]) -> ExecutionIntent:
    """Rebuild a frozen ``ExecutionIntent`` from a command body / wire payload."""
    entry = body["entry_policy"]
    stop = body["stop_policy"]
    target = body.get("target_policy")
    time_exit = body["time_exit_policy"]
    return ExecutionIntent(
        artifact_id=body["artifact_id"],
        session_id=body["session_id"],
        bar_id=body["bar_id"],
        signal_id=body["signal_id"],
        intent_id=body["intent_id"],
        command_id=body["command_id"],
        account_mode=body["account_mode"],
        conid=int(body["conid"]),
        side=body["side"],
        requested_quantity=(
            None if body.get("requested_quantity") is None
            else _parse_decimal(body["requested_quantity"])
        ),
        risk_fraction=_parse_decimal(body["risk_fraction"]),
        entry_policy=EntryPolicy(
            order_type=entry["order_type"],
            limit_offset_bps=_parse_decimal(entry["limit_offset_bps"]),
            tif=entry["tif"],
        ),
        stop_policy=StopPolicy(
            stop_price=_parse_decimal(stop["stop_price"]),
            order_type=stop["order_type"],
        ),
        target_policy=(
            None if target is None else TargetPolicy(
                target_price=_parse_decimal(target["target_price"]),
                order_type=target["order_type"],
            )
        ),
        time_exit_policy=TimeExitPolicy(
            max_hold_bars=time_exit.get("max_hold_bars"),
            close_by=_parse_ts(time_exit["close_by"]),
        ),
        artifact_digest=body["artifact_digest"],
        eligibility_attestation_digest=body["eligibility_attestation_digest"],
        signal_timestamp=_parse_ts(body["signal_timestamp"]),
        completed_bar_timestamp=_parse_ts(body["completed_bar_timestamp"]),
    )


class AutomatedIntentCommandService:
    """Saga handler for ``execute_automated_intent``."""

    def __init__(
        self,
        *,
        ledger: Any,
        audit: Any,
        journal: Any,
        controls: Any,
        dispatch: IntentDispatchPort,
        artifact_verifier: ArtifactVerifierPort,
        account_id: str,
        account_mode: str,
        now: Callable[[], dt.datetime],
        bundle_root: Path,
        schedule_reconcile: Optional[Callable[[str], None]] = None,
    ):
        self._ledger = ledger
        self._audit = audit
        self._journal = journal
        self._controls = controls
        self._dispatch = dispatch
        self._verifier = artifact_verifier
        self._account_id = account_id
        self._account_mode = account_mode
        self._now = now
        self._bundle_root = Path(bundle_root)
        self._schedule_reconcile = schedule_reconcile

    def execute(self, cmd: CommandRequest) -> CommandReceipt:
        if cmd.source not in _ALLOWED_PRINCIPALS:
            self._transition(cmd, "RECEIVED", "REJECTED", error_code="PRINCIPAL_FORBIDDEN")
            return self._receipt(cmd.command_id, "REJECTED", "PRINCIPAL_FORBIDDEN", False)

        try:
            intent = intent_from_body(cmd.body)
        except Exception as ex:
            self._transition(cmd, "RECEIVED", "REJECTED", error_code="INTENT_INVALID")
            return self._receipt(
                cmd.command_id, "REJECTED", "INTENT_INVALID", False,
                outcome={"detail": str(ex)},
            )

        if intent.command_id != cmd.command_id:
            self._transition(cmd, "RECEIVED", "REJECTED", error_code="COMMAND_ID_MISMATCH")
            return self._receipt(cmd.command_id, "REJECTED", "COMMAND_ID_MISMATCH", False)

        if intent.account_mode != self._account_mode:
            self._transition(cmd, "RECEIVED", "REJECTED", error_code="ACCOUNT_MODE_MISMATCH")
            return self._receipt(cmd.command_id, "REJECTED", "ACCOUNT_MODE_MISMATCH", False)

        bundle_digest = cmd.body.get("artifact_bundle_digest")
        if not bundle_digest:
            self._transition(cmd, "RECEIVED", "REJECTED", error_code="BUNDLE_DIGEST_MISSING")
            return self._receipt(cmd.command_id, "REJECTED", "BUNDLE_DIGEST_MISSING", False)

        bundle_path = self._bundle_path(str(bundle_digest))
        try:
            self._verifier.verify(
                bundle_path,
                intent.account_mode,
                intent.artifact_id,
                self._now_utc(),
            )
        except Exception as ex:
            self._transition(cmd, "RECEIVED", "REJECTED", error_code="ARTIFACT_UNVERIFIED")
            return self._receipt(
                cmd.command_id, "REJECTED", "ARTIFACT_UNVERIFIED", False,
                outcome={"detail": str(ex)},
            )

        self._transition(cmd, "RECEIVED", "VALIDATED")

        order_group_id = f"og-{cmd.command_id}"
        order_ref = encode_order_ref(order_group_id)

        def claim(conn, append):
            from trader.trading.command_coordinator import _command_updated_mutation, _noop_write

            self._controls.require_unpaused_in_tx(conn, self._account_id)
            self._ledger.transition_in_tx(conn, cmd.command_id, "VALIDATED", "SUBMITTING")
            append(
                _command_updated_mutation(cmd, "SUBMITTING", self._now_utc()),
                _noop_write,
                f"command:{cmd.command_id}:submitting",
            )

        try:
            self._journal.mutate_batch_work(self._journal.connect(), claim)
        except Exception as ex:
            code = "TRADING_PAUSED"
            if getattr(ex, "code", None):
                code = str(ex.code)
            self._transition(cmd, "VALIDATED", "REJECTED", error_code=code)
            return self._receipt(cmd.command_id, "REJECTED", code, True)

        try:
            submitted = self._dispatch.submit(
                intent=intent, order_group_id=order_group_id, order_ref=order_ref,
            )
        except Exception:
            self._transition(
                cmd, "SUBMITTING", "OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS",
            )
            if self._schedule_reconcile is not None:
                self._schedule_reconcile(cmd.command_id)
            return self._receipt(
                cmd.command_id, "OUTCOME_UNKNOWN", "DISPATCH_AMBIGUOUS", False,
            )

        order_ids = list(getattr(submitted, "order_ids", []) or [])
        outcome = {
            "order_ids": order_ids,
            "order_group_id": order_group_id,
            "intent_id": intent.intent_id,
            "artifact_id": intent.artifact_id,
            "artifact_bundle_digest": bundle_digest,
            "eligibility_attestation_digest": intent.eligibility_attestation_digest,
            "session_id": intent.session_id,
            "signal_id": intent.signal_id,
        }

        def finish(conn, append):
            from trader.trading.command_coordinator import _command_updated_mutation, _noop_write

            self._ledger.transition_in_tx(
                conn, cmd.command_id, "SUBMITTING", "SUBMITTED", outcome=outcome,
            )
            append(
                _command_updated_mutation(
                    cmd, "SUBMITTED", self._now_utc(), outcome=outcome,
                ),
                _noop_write,
                f"command:{cmd.command_id}:submitted",
            )

        try:
            self._journal.mutate_batch_work(self._journal.connect(), finish)
        except Exception:
            self._transition(
                cmd, "SUBMITTING", "OUTCOME_UNKNOWN",
                error_code="DISPATCH_AMBIGUOUS", outcome=outcome,
            )
            if self._schedule_reconcile is not None:
                self._schedule_reconcile(cmd.command_id)
            return self._receipt(
                cmd.command_id, "OUTCOME_UNKNOWN", "DISPATCH_AMBIGUOUS", False,
                outcome=outcome,
            )
        return self._receipt(cmd.command_id, "SUBMITTED", None, False, outcome=outcome)

    def _bundle_path(self, bundle_digest: str) -> Path:
        # Digests may contain ':' (sha256:...); map to a filesystem-safe directory.
        safe = bundle_digest.replace(":", "_")
        return self._bundle_root / safe

    def _transition(
        self,
        cmd: CommandRequest,
        from_state: str,
        to_state: str,
        *,
        outcome: Optional[dict[str, Any]] = None,
        error_code: Optional[str] = None,
    ) -> None:
        from trader.trading.command_coordinator import _command_updated_mutation

        now = self._now_utc()

        def _write(conn, _revision: int) -> None:
            self._ledger.transition_in_tx(
                conn, cmd.command_id, from_state, to_state,
                outcome=outcome, error_code=error_code, now=now,
            )

        self._journal.mutate(
            self._journal.connect(),
            _command_updated_mutation(
                cmd, to_state, now, outcome=outcome, error_code=error_code,
            ),
            _write,
            event_id=f"command:{cmd.command_id}:{to_state.lower()}",
        )

    @staticmethod
    def _receipt(
        command_id: str,
        state: str,
        error_code: Optional[str],
        retryable: bool,
        *,
        outcome: Optional[dict[str, Any]] = None,
    ) -> CommandReceipt:
        return CommandReceipt(
            command_id=command_id,
            correlation_id=command_id,
            state=state,
            outcome=outcome,
            error_code=error_code,
            retryable=retryable,
        )

    def _now_utc(self) -> dt.datetime:
        return _as_utc(self._now())
