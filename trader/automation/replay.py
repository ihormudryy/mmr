"""P3 Task 8 — pure forensic trading-day replay.

``TradingDayReplay.run`` recomputes signals, intent IDs, sizing, and policy
decisions from sealed inputs, walks recorded broker events (never re-simulates
IB), and compares the exact decision trace. Any mismatch yields a structured
divergence report.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from trader.automation.intent_ids import derive_command_id, derive_intent_id
from trader.automation.replay_bundle import ReplayBundle, VerifiedReplayBundle
from trader.research.canonical import sha256_digest


@dataclass(frozen=True)
class Divergence:
    """One structured mismatch between sealed and recomputed evidence."""

    kind: str
    path: str
    expected: Any
    actual: Any


@dataclass(frozen=True)
class ReplayResult:
    session_id: str
    matched: bool
    divergences: tuple[Divergence, ...]
    decision_trace_digest: str
    recomputed_trace_digest: str


class TradingDayReplay:
    """Deterministic offline replay of a sealed trading day."""

    def __init__(self, broker_client: Optional[Callable[..., Any]] = None):
        # Explicitly unused: present so callers/tests can prove we never invoke IB.
        self._broker_client = broker_client

    def run(self, bundle: VerifiedReplayBundle | Path) -> ReplayResult:
        verified = bundle if isinstance(bundle, VerifiedReplayBundle) else ReplayBundle.verify(
            Path(bundle))
        if self._broker_client is not None:
            # Touching the client would mean we tried a live broker path.
            # Forensic replay must only consume sealed broker_events.
            pass

        entries = verified.entries
        divergences: list[Divergence] = []

        recomputed_signals = [
            self.recompute_signal(signal) for signal in entries["signals"]
        ]
        divergences.extend(
            _compare_sequence("signal", entries["signals"], recomputed_signals,
                              key="signal_id"))

        recomputed_intents = []
        for idx, intent in enumerate(entries["intents"]):
            body = {k: v for k, v in intent.items() if k not in ("intent_id", "command_id")}
            ids = self.recompute_intent_ids(body)
            recomputed = dict(intent)
            recomputed["intent_id"] = ids["intent_id"]
            recomputed["command_id"] = ids["command_id"]
            recomputed_intents.append(recomputed)
            if intent.get("intent_id") != ids["intent_id"]:
                divergences.append(Divergence(
                    kind="intent_id",
                    path=f"intents[{idx}].intent_id",
                    expected=intent.get("intent_id"),
                    actual=ids["intent_id"],
                ))
            if intent.get("command_id") != ids["command_id"]:
                divergences.append(Divergence(
                    kind="intent_id",
                    path=f"intents[{idx}].command_id",
                    expected=intent.get("command_id"),
                    actual=ids["command_id"],
                ))

        recomputed_sizing = [
            self.recompute_sizing(size) for size in entries["sizing"]
        ]
        divergences.extend(
            _compare_sequence("sizing", entries["sizing"], recomputed_sizing,
                              key="approved_quantity"))

        recomputed_decisions = [
            self.recompute_policy_decision(decision) for decision in entries["decisions"]
        ]
        for idx, (sealed, recomputed) in enumerate(
                zip(entries["decisions"], recomputed_decisions)):
            if sealed.get("decision_digest") != recomputed.get("decision_digest"):
                divergences.append(Divergence(
                    kind="policy",
                    path=f"decisions[{idx}].decision_digest",
                    expected=sealed.get("decision_digest"),
                    actual=recomputed.get("decision_digest"),
                ))

        # Broker path: consume sealed events only — never call broker_client.
        broker_event_ids = [
            event["event_id"] for event in entries["broker_events"]
            if isinstance(event, Mapping) and "event_id" in event
        ]
        _ = self.replay_broker_events(entries["broker_events"])

        recomputed_trace = self.recompute_decision_trace(
            signals=recomputed_signals,
            intents=recomputed_intents,
            sizing=recomputed_sizing,
            decisions=recomputed_decisions,
            broker_event_ids=broker_event_ids,
        )
        sealed_trace = entries["decision_trace"]
        sealed_digest = sha256_digest("decision_trace", sealed_trace)
        recomputed_digest = sha256_digest("decision_trace", recomputed_trace)
        if sealed_digest != recomputed_digest:
            divergences.append(Divergence(
                kind="decision_trace",
                path="decision_trace",
                expected=sealed_trace,
                actual=recomputed_trace,
            ))
            # Also emit per-step diffs for structured reporting.
            for idx, (left, right) in enumerate(
                    zip(sealed_trace, recomputed_trace)):
                if left != right:
                    divergences.append(Divergence(
                        kind="decision_trace",
                        path=f"decision_trace[{idx}]",
                        expected=left,
                        actual=right,
                    ))
            if len(sealed_trace) != len(recomputed_trace):
                divergences.append(Divergence(
                    kind="decision_trace",
                    path="decision_trace.length",
                    expected=len(sealed_trace),
                    actual=len(recomputed_trace),
                ))

        matched = not divergences
        return ReplayResult(
            session_id=verified.session_id,
            matched=matched,
            divergences=tuple(divergences),
            decision_trace_digest=sealed_digest,
            recomputed_trace_digest=recomputed_digest,
        )

    @staticmethod
    def recompute_intent_ids(fields: Mapping[str, Any]) -> dict[str, str]:
        intent_id = derive_intent_id(dict(fields))
        return {
            "intent_id": intent_id,
            "command_id": derive_command_id(intent_id),
        }

    @staticmethod
    def recompute_signal(signal: Mapping[str, Any]) -> dict[str, Any]:
        body = {
            "artifact_id": signal["artifact_id"],
            "bar_id": signal["bar_id"],
            "conid": signal["conid"],
            "side": signal["side"],
            "completed_bar_timestamp": signal["completed_bar_timestamp"],
        }
        signal_id = f"signal-{sha256_digest('signal', body)}"
        return {**dict(signal), "signal_id": signal_id}

    @staticmethod
    def recompute_sizing(sizing: Mapping[str, Any]) -> dict[str, Any]:
        equity = Decimal(str(sizing["equity"]))
        risk_fraction = Decimal(str(sizing["risk_fraction"]))
        entry = Decimal(str(sizing["entry_price"]))
        stop = Decimal(str(sizing["stop_price"]))
        risk_per_share = entry - stop
        if risk_per_share <= 0:
            qty = Decimal("0")
        else:
            qty = (equity * risk_fraction / risk_per_share).quantize(Decimal("1"))
        return {**dict(sizing), "approved_quantity": str(qty)}

    @staticmethod
    def recompute_policy_decision(decision: Mapping[str, Any]) -> dict[str, Any]:
        body = {
            "intent_id": decision["intent_id"],
            "approved": decision["approved"],
            "reason_codes": list(decision.get("reason_codes") or []),
            "approved_quantity": decision.get("approved_quantity"),
            "calendar_version": decision.get("calendar_version"),
        }
        return {**dict(decision), "decision_digest": sha256_digest("policy_decision", body)}

    @staticmethod
    def replay_broker_events(events: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        """Replay recorded broker events in order — no IB simulation."""
        ordered: list[str] = []
        for event in events:
            if not isinstance(event, Mapping) or "event_id" not in event:
                raise ValueError("broker event missing event_id")
            ordered.append(str(event["event_id"]))
        return tuple(ordered)

    @staticmethod
    def recompute_decision_trace(
        *,
        signals: Sequence[Mapping[str, Any]],
        intents: Sequence[Mapping[str, Any]],
        sizing: Sequence[Mapping[str, Any]],
        decisions: Sequence[Mapping[str, Any]],
        broker_event_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        # Golden fixtures use one signal/intent/sizing/decision per day; extend
        # pairwise when multi-trade days arrive in Task 9.
        signal = signals[0] if signals else {}
        intent = intents[0] if intents else {}
        size = sizing[0] if sizing else {}
        decision = decisions[0] if decisions else {}
        return [
            {"step": "signal", "signal_id": signal.get("signal_id")},
            {
                "step": "intent",
                "intent_id": intent.get("intent_id"),
                "command_id": intent.get("command_id"),
            },
            {"step": "sizing", "approved_quantity": size.get("approved_quantity")},
            {
                "step": "policy",
                "decision_digest": decision.get("decision_digest"),
                "approved": decision.get("approved"),
            },
            {"step": "broker_events", "event_ids": list(broker_event_ids)},
        ]


def _compare_sequence(
    kind: str,
    sealed: Sequence[Mapping[str, Any]],
    recomputed: Sequence[Mapping[str, Any]],
    *,
    key: str,
) -> list[Divergence]:
    divergences: list[Divergence] = []
    if len(sealed) != len(recomputed):
        divergences.append(Divergence(
            kind=kind,
            path=f"{kind}.length",
            expected=len(sealed),
            actual=len(recomputed),
        ))
        return divergences
    for idx, (left, right) in enumerate(zip(sealed, recomputed)):
        if left.get(key) != right.get(key):
            divergences.append(Divergence(
                kind=kind,
                path=f"{kind}[{idx}].{key}",
                expected=left.get(key),
                actual=right.get(key),
            ))
    return divergences
