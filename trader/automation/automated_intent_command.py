"""P3 Task 3 — automated intent command saga (boundary only).

Routes a verified ``ExecutionIntent`` through the existing
``TradingCommandCoordinator``. Protective-order construction is Task 5; this
module validates principal + intent identity, re-verifies the artifact bundle,
audits authority digests (via the coordinator's RECEIVED audit of ``body``),
and dispatches through an injected port so tests can prove exactly-once
delivery without talking to IB.

When a ``ProtectiveOrderSaga`` is injected, session risk + DispatchGuard +
bracket submit run inside ``saga.start`` (Task 5) instead of a bare dispatch.
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
from trader.trading.command_coordinator import BrokerRejectedError, CommandRequest
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


class ProtectiveSagaPort(Protocol):
    def start(self, *, intent, approval, request, artifact, session_state, allocation) -> Any:
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
        conid=body["conid"],  # never coerced: ExecutionIntent refuses 1.5, True or "1" (#21)
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
        expected_artifact_id: str,
        schedule_reconcile: Optional[Callable[[str], None]] = None,
        protective_saga: Optional[ProtectiveSagaPort] = None,
        approval_factory: Optional[Callable[..., Any]] = None,
        session_state_factory: Optional[Callable[..., Any]] = None,
        allocation_factory: Optional[Callable[..., Any]] = None,
        configured_bundle_path: Optional[Path] = None,
        bundle_evidence_validator: Optional[Callable[[Path], None]] = None,
        liquidation: Optional[Any] = None,
        broker: Optional[Any] = None,
        close_deadline_seconds: float = 300.0,
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
        self._liquidation = liquidation
        self._broker = broker
        self._close_deadline_seconds = close_deadline_seconds
        self._bundle_root = Path(bundle_root)
        if not expected_artifact_id:
            raise ValueError("expected_artifact_id is required: the service verifies only the armed artifact")
        self._expected_artifact_id = expected_artifact_id
        self._schedule_reconcile = schedule_reconcile
        self._protective_saga = protective_saga
        self._approval_factory = approval_factory
        self._session_state_factory = session_state_factory
        self._allocation_factory = allocation_factory
        self._configured_bundle_path = (
            Path(configured_bundle_path) if configured_bundle_path is not None else None
        )
        self._bundle_evidence_validator = bundle_evidence_validator

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

        if intent.artifact_id != self._expected_artifact_id:
            self._transition(cmd, "RECEIVED", "REJECTED", error_code="ARTIFACT_NOT_ARMED")
            return self._receipt(
                cmd.command_id, "REJECTED", "ARTIFACT_NOT_ARMED", False,
                outcome={"detail": f"intent names artifact {intent.artifact_id}, "
                                   f"armed artifact is {self._expected_artifact_id}"},
            )

        bundle_digest = cmd.body.get("artifact_bundle_digest")
        if not bundle_digest:
            self._transition(cmd, "RECEIVED", "REJECTED", error_code="BUNDLE_DIGEST_MISSING")
            return self._receipt(cmd.command_id, "REJECTED", "BUNDLE_DIGEST_MISSING", False)

        # Production pins the armed bundle directory. A manifest digest
        # is content identity, not the artifact directory name on disk.
        bundle_path = self._configured_bundle_path or self._bundle_path(str(bundle_digest))
        try:
            artifact = self._verifier.verify(
                bundle_path,
                intent.account_mode,
                self._expected_artifact_id,
                self._now_utc(),
            )
            if self._bundle_evidence_validator is not None:
                self._bundle_evidence_validator(bundle_path)
        except Exception as ex:
            self._transition(cmd, "RECEIVED", "REJECTED", error_code="ARTIFACT_UNVERIFIED")
            return self._receipt(
                cmd.command_id, "REJECTED", "ARTIFACT_UNVERIFIED", False,
                outcome={"detail": str(ex)},
            )

        if self._configured_bundle_path is not None:
            manifest_digest = artifact.manifest_digest
            expected_digest = (
                manifest_digest if manifest_digest.startswith("sha256:")
                else f"sha256:{manifest_digest}"
            )
            if bundle_digest != expected_digest:
                code = "BUNDLE_DIGEST_MISMATCH"
                self._transition(cmd, "RECEIVED", "REJECTED", error_code=code)
                return self._receipt(cmd.command_id, "REJECTED", code, False)

        source_mismatch = self._strategy_source_mismatch(
            artifact, cmd.body.get("strategy_source_digest"),
        )
        if source_mismatch is not None:
            self._transition(cmd, "RECEIVED", "REJECTED", error_code="STRATEGY_SOURCE_MISMATCH")
            return self._receipt(
                cmd.command_id, "REJECTED", "STRATEGY_SOURCE_MISMATCH", False,
                outcome={"detail": source_mismatch},
            )

        self._transition(cmd, "RECEIVED", "VALIDATED")

        # A SELL on the long-only path is an exit, never a bracket (spec 5.1). It adds no
        # exposure, so the pause on new exposure does not stop it (R32).
        if intent.side == "SELL":
            return self._execute_close(cmd, intent)

        order_group_id = f"og-{cmd.command_id}"
        order_ref = encode_order_ref(order_group_id)

        try:
            self._claim(cmd, require_unpaused=True)
        except Exception as ex:
            code = "TRADING_PAUSED"
            if getattr(ex, "code", None):
                code = str(ex.code)
            self._transition(cmd, "VALIDATED", "REJECTED", error_code=code)
            return self._receipt(cmd.command_id, "REJECTED", code, True)

        # Task 5 path: protective saga owns risk + guard + bracket submit.
        if self._protective_saga is not None and self._approval_factory is not None:
            return self._execute_via_saga(
                cmd, intent, artifact, order_group_id, bundle_digest,
            )

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
        return self._finish_submitted(
            cmd, intent, order_group_id, order_ids, bundle_digest,
        )

    def _execute_via_saga(
        self, cmd, intent, artifact, order_group_id, bundle_digest,
    ) -> CommandReceipt:
        assert self._approval_factory is not None
        assert self._protective_saga is not None
        try:
            approval = self._approval_factory(intent=intent, command=cmd)
            session_state = (
                self._session_state_factory(intent=intent, command=cmd, approval=approval)
                if self._session_state_factory is not None else None
            )
            allocation = (
                self._allocation_factory(intent=intent, artifact=artifact, command=cmd)
                if self._allocation_factory is not None else None
            )
        except Exception as ex:
            # No broker mutation has been attempted. Do not strand the command
            # in SUBMITTING or manufacture an ambiguous dispatch to reconcile.
            code = getattr(ex, "code", None) or "AUTOMATION_EVIDENCE_UNAVAILABLE"
            self._transition(cmd, "SUBMITTING", "REJECTED", error_code=code)
            return self._receipt(cmd.command_id, "REJECTED", code, False)
        try:
            saga_state = self._protective_saga.start(
                intent=intent,
                approval=approval,
                request=cmd,
                artifact=artifact,
                session_state=session_state,
                allocation=allocation,
            )
        except BrokerRejectedError:
            self._transition(cmd, "SUBMITTING", "REJECTED", error_code="BROKER_REJECTED")
            return self._receipt(cmd.command_id, "REJECTED", "BROKER_REJECTED", False)
        except Exception:
            self._transition(
                cmd, "SUBMITTING", "OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS",
            )
            if self._schedule_reconcile is not None:
                self._schedule_reconcile(cmd.command_id)
            return self._receipt(
                cmd.command_id, "OUTCOME_UNKNOWN", "DISPATCH_AMBIGUOUS", False,
            )

        if saga_state.state == "CLOSED" and saga_state.error_code:
            self._transition(
                cmd, "SUBMITTING", "REJECTED", error_code=saga_state.error_code,
            )
            return self._receipt(
                cmd.command_id, "REJECTED", saga_state.error_code, False,
            )
        if saga_state.state == "OUTCOME_UNKNOWN":
            self._transition(
                cmd, "SUBMITTING", "OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS",
            )
            if self._schedule_reconcile is not None:
                self._schedule_reconcile(cmd.command_id)
            return self._receipt(
                cmd.command_id, "OUTCOME_UNKNOWN", "DISPATCH_AMBIGUOUS", False,
            )

        order_ids = list(saga_state.submitted_order_ids or [])
        return self._finish_submitted(
            cmd, intent, order_group_id, order_ids, bundle_digest,
        )

    def _claim(self, cmd, *, require_unpaused: bool) -> None:
        """VALIDATED -> SUBMITTING in one journal transaction; an entry also checks the pause."""
        from trader.trading.command_coordinator import _command_updated_mutation, _noop_write

        def claim(conn, append):
            if require_unpaused:
                self._controls.require_unpaused_in_tx(conn, self._account_id)
            self._ledger.transition_in_tx(conn, cmd.command_id, "VALIDATED", "SUBMITTING")
            append(
                _command_updated_mutation(cmd, "SUBMITTING", self._now_utc()),
                _noop_write,
                f"command:{cmd.command_id}:submitting",
            )
        self._journal.mutate_batch_work(self._journal.connect(), claim)

    def _execute_close(self, cmd, intent) -> CommandReceipt:
        """Prove the SELL reduces the held long on the account's broker snapshot, then close.

        The proof is a reduction check, not a sizing: the close sizes every
        reduce from its own fenced generations, and the reduce-only boundary
        checks IB's live position again (Task 14).
        """
        from trader.trading.exit_owner import ExitInProgress
        from trader.trading.liquidation_service import LiquidationRefused

        if self._liquidation is None or self._broker is None:
            # Fail loudly: never fall back to the bracket path, which would add a reverse stop.
            self._transition(cmd, "VALIDATED", "REJECTED", error_code="CLOSE_PATH_UNAVAILABLE")
            return self._receipt(cmd.command_id, "REJECTED", "CLOSE_PATH_UNAVAILABLE", False)
        if cmd.account_id != self._account_id:
            self._transition(cmd, "VALIDATED", "REJECTED", error_code="ACCOUNT_MISMATCH")
            return self._receipt(cmd.command_id, "REJECTED", "ACCOUNT_MISMATCH", False)
        self._claim(cmd, require_unpaused=False)
        try:
            snapshot = self._broker.capture(self._account_id)
            if getattr(snapshot, "account_id", None) != self._account_id:
                raise RuntimeError("broker snapshot is for another account")
        except Exception as ex:
            return self._reject_close(cmd, "BROKER_SNAPSHOT_UNAVAILABLE", {"detail": str(ex)})
        held = float(snapshot.reducible_quantity(intent.conid))
        requested = None if intent.requested_quantity is None else float(intent.requested_quantity)
        if held <= 0 or (requested is not None and requested > held):
            return self._reject_close(cmd, "NOT_A_REDUCTION", {"held": held, "requested": requested})
        # A close of the whole position takes the broker quantity at reduce time (ruling 10).
        quantity = None if requested is None or requested >= held else requested
        deadline = self._now_utc() + dt.timedelta(seconds=self._close_deadline_seconds)
        try:
            receipt = self._liquidation.start(
                self._account_id, cmd.command_id, deadline, scope="conid", conid=intent.conid, quantity=quantity,
            )
        except ExitInProgress as ex:
            return self._reject_close(cmd, "EXIT_IN_PROGRESS", {"close_root_id": ex.root_id})
        except LiquidationRefused as ex:
            return self._reject_close(cmd, ex.code, {"detail": str(ex)})
        except Exception as ex:
            self._transition(cmd, "SUBMITTING", "OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS")
            if self._schedule_reconcile is not None:
                self._schedule_reconcile(cmd.command_id)
            return self._receipt(cmd.command_id, "OUTCOME_UNKNOWN", "DISPATCH_AMBIGUOUS", False,
                                 outcome={"detail": str(ex)})

        outcome = {"close_root_id": receipt.cause_command_id, "liquidation_state": receipt.state,
                   "generation_id": receipt.generation_id, "detail": receipt.detail}
        self._transition(cmd, "SUBMITTING", "OUTCOME_UNKNOWN", error_code="CLOSE_PENDING", outcome=outcome)
        if self._schedule_reconcile is not None:
            # R17: the reconciler resolves this command from the exact root it started or joined.
            self._schedule_reconcile(cmd.command_id)
        return self._receipt(cmd.command_id, "OUTCOME_UNKNOWN", "CLOSE_PENDING", False, outcome=outcome)

    def _reject_close(self, cmd, code: str, outcome: dict) -> CommandReceipt:
        self._transition(cmd, "SUBMITTING", "REJECTED", error_code=code)
        return self._receipt(cmd.command_id, "REJECTED", code, False, outcome=outcome)

    def _finish_submitted(
        self, cmd, intent, order_group_id, order_ids, bundle_digest,
    ) -> CommandReceipt:
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

    @staticmethod
    def _strategy_source_mismatch(artifact: Any, sent_digest: Optional[str]) -> Optional[str]:
        """Why the intent's strategy file is not the attested one, or None when it is."""
        attested = getattr(artifact, "attested_strategy", None)
        if attested is None:
            return "bundle attests no strategy"
        if not sent_digest or sent_digest != attested.source_digest:
            return "intent strategy source digest does not match the attested file"
        return None

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
