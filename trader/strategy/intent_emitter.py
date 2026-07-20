"""Typed strategy → trader intent emission (P3 Task 9).

Builds canonical ``ExecutionIntent`` messages from completed, session-valid
bars and submits them exclusively via the trader's authenticated typed
``execute_automated_intent`` command. Never constructs IB orders, never
opens the domain journal, and never uses legacy dill RPC.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional

from trader.automation.artifact_verifier import VerifiedArtifact
from trader.automation.intent_ids import derive_command_id, derive_intent_id
from trader.automation.models import (
    EntryPolicy,
    ExecutionIntent,
    StopPolicy,
    TargetPolicy,
    TimeExitPolicy,
)
from trader.domain.commands import CommandReceipt
from trader.objects import Action
from trader.research.canonical import sha256_digest
from trader.trading.strategy import Signal

UTC = dt.timezone.utc


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _dec(value: Any, default: Optional[Decimal] = None) -> Decimal:
    if value is None:
        if default is not None:
            return default
        raise ValueError("required decimal is missing")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _parse_ts(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return _as_utc(value)
    text = str(value).replace("Z", "+00:00")
    return _as_utc(dt.datetime.fromisoformat(text))


@dataclass(frozen=True)
class IntentEmitterContext:
    """Immutable authority context captured at strategy load / verification."""

    enabled: bool
    live_enabled: bool
    strategy_name: str
    artifact: VerifiedArtifact
    artifact_digest: str
    eligibility_attestation_digest: str
    artifact_bundle_digest: str
    account_mode: str  # paper | live


class IntentEmitter:
    """Thin typed adapter: Signal + completed bar → ``execute_automated_intent``.

    Holds no DuckDB / journal handle. The only mutation surface is
    ``command_client.call("execute_automated_intent", ...)``.
    """

    def __init__(
        self,
        command_client: Any,
        context: IntentEmitterContext,
        *,
        now: Optional[Callable[[], dt.datetime]] = None,
    ):
        self._client = command_client
        self._ctx = context
        self._now = now or (lambda: dt.datetime.now(UTC))
        self.last_intent: Optional[ExecutionIntent] = None
        self._emitted_command_ids: set[str] = set()

    @property
    def context(self) -> IntentEmitterContext:
        return self._ctx

    def on_signal(
        self,
        *,
        strategy_name: str,
        signal: Signal,
        completed_bar_timestamp: dt.datetime,
        session_id: str,
    ) -> Optional[CommandReceipt]:
        if not self._ctx.enabled:
            return None
        if not self._ctx.strategy_name or strategy_name != self._ctx.strategy_name:
            return None
        # P3 paper vertical slice: live automation stays disabled. P4 flips
        # live_enabled + account_mode together under a new attestation.
        if self._ctx.account_mode == "live" and not self._ctx.live_enabled:
            logging.error(
                "intent emitter refusing strategy %s: live account without "
                "automation.live_enabled",
                strategy_name,
            )
            return None
        if self._ctx.live_enabled and self._ctx.account_mode != "live":
            logging.error(
                "intent emitter refusing strategy %s: live_enabled set but "
                "account_mode=%r",
                strategy_name, self._ctx.account_mode,
            )
            return None
        if self._ctx.account_mode not in ("paper", "live"):
            logging.error(
                "intent emitter refusing strategy %s: account_mode=%r",
                strategy_name, self._ctx.account_mode,
            )
            return None

        side = {Action.BUY: "BUY", Action.SELL: "SELL"}.get(signal.action)
        if side is None:
            return None
        conid = int(signal.conid or 0)
        if conid <= 0:
            logging.error(
                "signal from %s has no conid — cannot emit intent", strategy_name)
            return None

        bar_ts = _as_utc(completed_bar_timestamp)
        signal_ts = _as_utc(signal.date_time) if signal.date_time else self._now()
        if signal_ts < bar_ts:
            signal_ts = bar_ts

        try:
            intent = self.build_intent(
                signal=signal,
                side=side,
                conid=conid,
                completed_bar_timestamp=bar_ts,
                signal_timestamp=signal_ts,
                session_id=session_id,
            )
        except Exception:
            logging.exception(
                "failed to build ExecutionIntent for %s conid %s",
                strategy_name, conid,
            )
            return None

        self.last_intent = intent
        body = self._to_wire(intent)
        try:
            response = self._client.call("execute_automated_intent", body, dict)
        except Exception:
            logging.exception(
                "typed execute_automated_intent failed for %s intent %s",
                strategy_name, intent.intent_id,
            )
            return None

        self._emitted_command_ids.add(intent.command_id)
        command_id = str(response.get("command_id", intent.command_id))
        state = str(response.get("state", "UNKNOWN"))
        return CommandReceipt(
            command_id=command_id,
            correlation_id=str(response.get("correlation_id", command_id)),
            state=state,
            outcome=response.get("outcome"),
            error_code=response.get("error_code"),
            retryable=bool(response.get("retryable", False)),
        )

    def build_intent(
        self,
        *,
        signal: Signal,
        side: str,
        conid: int,
        completed_bar_timestamp: dt.datetime,
        signal_timestamp: dt.datetime,
        session_id: str,
    ) -> ExecutionIntent:
        meta = dict(signal.metadata or {})
        params = dict(self._ctx.artifact.parameters or {})

        bar_id = f"bar-{completed_bar_timestamp.isoformat()}"
        signal_body = {
            "artifact_id": self._ctx.artifact.artifact_id,
            "bar_id": bar_id,
            "conid": conid,
            "side": side,
            "completed_bar_timestamp": completed_bar_timestamp.isoformat(),
        }
        signal_id = f"signal-{sha256_digest('signal', signal_body)}"

        stop_price = _dec(meta.get("stop_price", params.get("stop_price")))
        risk_fraction = _dec(
            meta.get("risk_fraction", params.get("risk_fraction", "0.002")),
        )
        limit_offset = _dec(
            meta.get("limit_offset_bps", params.get("limit_offset_bps", "5")),
            default=Decimal("5"),
        )
        entry_type = str(
            meta.get("entry_order_type", params.get("entry_order_type", "LIMIT"))
        )
        if entry_type not in ("LIMIT", "MARKETABLE_LIMIT"):
            entry_type = "LIMIT"
        stop_type = str(
            meta.get("stop_order_type", params.get("stop_order_type", "STP"))
        )
        if stop_type not in ("STP", "STP_LMT"):
            stop_type = "STP"

        target_raw = meta.get("target_price", params.get("target_price"))
        target_policy = (
            None if target_raw is None else TargetPolicy(
                target_price=_dec(target_raw), order_type="LMT",
            )
        )

        max_hold = meta.get("max_hold_bars", params.get("max_hold_bars"))
        if max_hold is not None:
            max_hold = int(max_hold)
        close_by_raw = meta.get("close_by", params.get("close_by"))
        if close_by_raw is None and signal.close_by_time is not None:
            # Interpret strategy close_by_time on the completed-bar calendar day (UTC).
            close_by = completed_bar_timestamp.replace(
                hour=signal.close_by_time.hour,
                minute=signal.close_by_time.minute,
                second=getattr(signal.close_by_time, "second", 0),
                microsecond=0,
            )
        elif close_by_raw is None:
            close_by = completed_bar_timestamp + dt.timedelta(hours=2)
        else:
            close_by = _parse_ts(close_by_raw)

        qty = None
        if signal.quantity and signal.quantity > 0:
            qty = _dec(signal.quantity)
        elif meta.get("requested_quantity") is not None:
            qty = _dec(meta["requested_quantity"])

        fields: dict[str, Any] = {
            "artifact_id": self._ctx.artifact.artifact_id,
            "session_id": session_id,
            "bar_id": bar_id,
            "signal_id": signal_id,
            "account_mode": self._ctx.account_mode,
            "conid": conid,
            "side": side,
            "requested_quantity": qty,
            "risk_fraction": risk_fraction,
            "entry_policy": EntryPolicy(
                order_type=entry_type,  # type: ignore[arg-type]
                limit_offset_bps=limit_offset,
                tif="DAY",
            ),
            "stop_policy": StopPolicy(
                stop_price=stop_price,
                order_type=stop_type,  # type: ignore[arg-type]
            ),
            "target_policy": target_policy,
            "time_exit_policy": TimeExitPolicy(
                max_hold_bars=max_hold, close_by=close_by,
            ),
            "artifact_digest": self._ctx.artifact_digest,
            "eligibility_attestation_digest": self._ctx.eligibility_attestation_digest,
            "signal_timestamp": signal_timestamp,
            "completed_bar_timestamp": completed_bar_timestamp,
        }
        dict_fields = {
            k: (asdict(v) if hasattr(v, "__dataclass_fields__") else v)
            for k, v in fields.items()
        }
        intent_id = derive_intent_id(dict_fields)
        command_id = derive_command_id(intent_id)
        return ExecutionIntent(
            **fields, intent_id=intent_id, command_id=command_id,
        )

    def _to_wire(self, intent: ExecutionIntent) -> dict[str, Any]:
        def _dec_s(v: Any) -> Any:
            return str(v) if isinstance(v, Decimal) else v

        def _ts(v: dt.datetime) -> str:
            return _as_utc(v).isoformat().replace("+00:00", "Z")

        return {
            "command_id": intent.command_id,
            "artifact_id": intent.artifact_id,
            "session_id": intent.session_id,
            "bar_id": intent.bar_id,
            "signal_id": intent.signal_id,
            "intent_id": intent.intent_id,
            "account_mode": intent.account_mode,
            "conid": intent.conid,
            "side": intent.side,
            "requested_quantity": _dec_s(intent.requested_quantity),
            "risk_fraction": _dec_s(intent.risk_fraction),
            "entry_policy": {
                "order_type": intent.entry_policy.order_type,
                "limit_offset_bps": _dec_s(intent.entry_policy.limit_offset_bps),
                "tif": intent.entry_policy.tif,
            },
            "stop_policy": {
                "stop_price": _dec_s(intent.stop_policy.stop_price),
                "order_type": intent.stop_policy.order_type,
            },
            "target_policy": (
                None if intent.target_policy is None else {
                    "target_price": _dec_s(intent.target_policy.target_price),
                    "order_type": intent.target_policy.order_type,
                }
            ),
            "time_exit_policy": {
                "max_hold_bars": intent.time_exit_policy.max_hold_bars,
                "close_by": _ts(intent.time_exit_policy.close_by),
            },
            "artifact_digest": intent.artifact_digest,
            "eligibility_attestation_digest": intent.eligibility_attestation_digest,
            "signal_timestamp": _ts(intent.signal_timestamp),
            "completed_bar_timestamp": _ts(intent.completed_bar_timestamp),
            "artifact_bundle_digest": self._ctx.artifact_bundle_digest,
        }
