"""P3 Task 7 — authoritative trade attribution ledger.

``AttributionLedger.append`` stores append-only evidence. ``rebuild_trade``
recomputes derived ``TradeAttribution`` from raw evidence only — never from
dashboard state. Promotion queries exclude unresolved trades and report them
explicitly.
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional, Sequence

from trader.data.attribution_store import AttributionStore
from trader.domain.events import DomainMutation
from trader.domain.identity import command_entity_id

_EXIT_LEGS = frozenset({"stop", "take_profit", "exit", "flatten"})
_ENTRY_SIDES = frozenset({"BOT", "BUY", "bot", "buy"})
_EXIT_SIDES = frozenset({"SLD", "SELL", "sld", "sell"})


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _opt_dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return _dec(value)


def _parse_ts(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return _as_utc(value)
    text = str(value).replace("Z", "+00:00")
    return _as_utc(dt.datetime.fromisoformat(text))


@dataclass(frozen=True)
class AttributionEvidenceEvent:
    evidence_key: str
    trade_id: str
    event_kind: str
    payload: Mapping[str, Any]
    source_timestamp: dt.datetime

    def __post_init__(self) -> None:
        if not self.evidence_key:
            raise ValueError("evidence_key is required")
        if not self.trade_id:
            raise ValueError("trade_id is required")
        if not self.event_kind:
            raise ValueError("event_kind is required")
        if self.source_timestamp.tzinfo is None:
            raise ValueError("source_timestamp must be timezone-aware")


@dataclass(frozen=True)
class TradeAttribution:
    trade_id: str
    resolved: bool
    artifact_id: Optional[str] = None
    dataset_id: Optional[str] = None
    signal_id: Optional[str] = None
    intent_id: Optional[str] = None
    command_id: Optional[str] = None
    order_group_id: Optional[str] = None
    approval_context_id: Optional[str] = None
    fills: tuple[dict[str, Any], ...] = ()
    exit_fills: tuple[dict[str, Any], ...] = ()
    policy_decisions: tuple[dict[str, Any], ...] = ()
    rejection_refs: tuple[dict[str, Any], ...] = ()
    breaker_refs: tuple[dict[str, Any], ...] = ()
    operator_actions: tuple[dict[str, Any], ...] = ()
    gross_pnl: Optional[Decimal] = None
    net_pnl: Optional[Decimal] = None
    total_commission: Decimal = Decimal("0")
    spread_bps: Optional[Decimal] = None
    slippage_bps: Optional[Decimal] = None
    latency_ms: Optional[Decimal] = None
    mfe: Optional[Decimal] = None
    mae: Optional[Decimal] = None
    unresolved_reasons: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        body = asdict(self)
        for key in ("gross_pnl", "net_pnl", "total_commission", "spread_bps",
                    "slippage_bps", "latency_ms", "mfe", "mae"):
            if body[key] is not None:
                body[key] = str(body[key])
        return body

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TradeAttribution":
        data = dict(payload)
        for key in ("gross_pnl", "net_pnl", "total_commission", "spread_bps",
                    "slippage_bps", "latency_ms", "mfe", "mae"):
            if data.get(key) is not None:
                data[key] = Decimal(str(data[key]))
        for key in ("fills", "exit_fills", "policy_decisions", "rejection_refs",
                    "breaker_refs", "operator_actions", "unresolved_reasons"):
            if key in data and not isinstance(data[key], tuple):
                data[key] = tuple(data[key])
        return cls(**data)


@dataclass(frozen=True)
class PromotionAttributionReport:
    resolved: tuple[TradeAttribution, ...]
    unresolved: tuple[TradeAttribution, ...]


@dataclass(frozen=True)
class _FillState:
    exec_id: str
    leg: str
    side: str
    quantity: Decimal
    price: Decimal
    order_entity_id: Optional[str] = None
    correction_seq: int = 0


def rebuild_attribution_from_evidence(
    trade_id: str,
    evidence: Sequence[Mapping[str, Any]],
) -> TradeAttribution:
    """Pure rebuild of derived attribution from ordered evidence rows."""
    artifact_id: Optional[str] = None
    dataset_id: Optional[str] = None
    signal_id: Optional[str] = None
    intent_id: Optional[str] = None
    command_id: Optional[str] = None
    order_group_id: Optional[str] = None
    approval_context_id: Optional[str] = None
    expected_entry: Optional[Decimal] = None
    expected_exit: Optional[Decimal] = None
    policy_decisions: list[dict[str, Any]] = []
    rejection_refs: list[dict[str, Any]] = []
    breaker_refs: list[dict[str, Any]] = []
    operator_actions: list[dict[str, Any]] = []
    fills_by_exec: dict[str, _FillState] = {}
    commissions: dict[str, Decimal] = {}
    position_qty: Optional[Decimal] = None
    samples: list[Decimal] = []
    quote_bid: Optional[Decimal] = None
    quote_ask: Optional[Decimal] = None
    latency_ms: Optional[Decimal] = None
    signal_ts: Optional[dt.datetime] = None
    fill_ts: Optional[dt.datetime] = None

    # Deterministic order: source_timestamp then evidence_key (caller should
    # already sort; re-sort defensively). Arrival/recorded_at order must not
    # change derived results — evidence carries its own timestamps.
    ordered = sorted(
        evidence,
        key=lambda e: (
            str(e.get("source_timestamp") or ""),
            str(e.get("evidence_key") or ""),
        ),
    )

    for row in ordered:
        kind = row["event_kind"]
        payload = dict(row.get("payload") or {})

        if kind == "artifact":
            artifact_id = payload.get("artifact_id") or artifact_id
            dataset_id = payload.get("dataset_id") or dataset_id
        elif kind == "dataset":
            dataset_id = payload.get("dataset_id") or dataset_id
        elif kind == "signal":
            signal_id = payload.get("signal_id") or signal_id
        elif kind == "intent":
            intent_id = payload.get("intent_id") or intent_id
            command_id = payload.get("command_id") or command_id
        elif kind == "context":
            approval_context_id = payload.get("approval_context_id") or approval_context_id
            if payload.get("expected_entry") is not None:
                expected_entry = _dec(payload["expected_entry"])
            if payload.get("expected_exit") is not None:
                expected_exit = _dec(payload["expected_exit"])
        elif kind == "policy":
            policy_decisions.append(payload)
        elif kind == "command":
            command_id = payload.get("command_id") or command_id or trade_id
            order_group_id = payload.get("order_group_id") or order_group_id
        elif kind == "order":
            order_group_id = payload.get("order_group_id") or order_group_id
        elif kind == "fill":
            exec_id = str(payload["exec_id"])
            if exec_id not in fills_by_exec:
                fills_by_exec[exec_id] = _FillState(
                    exec_id=exec_id,
                    leg=str(payload.get("leg") or "entry"),
                    side=str(payload.get("side") or ""),
                    quantity=_dec(payload.get("quantity")),
                    price=_dec(payload.get("price")),
                    order_entity_id=payload.get("order_entity_id"),
                    correction_seq=0,
                )
        elif kind == "fill_correction":
            exec_id = str(payload["exec_id"])
            seq = int(payload.get("correction_seq") or 0)
            current = fills_by_exec.get(exec_id)
            if current is None or seq >= current.correction_seq:
                fills_by_exec[exec_id] = _FillState(
                    exec_id=exec_id,
                    leg=str(payload.get("leg") or (current.leg if current else "entry")),
                    side=str(payload.get("side") or (current.side if current else "")),
                    quantity=_dec(payload.get("quantity", current.quantity if current else 0)),
                    price=_dec(payload.get("price", current.price if current else 0)),
                    order_entity_id=(
                        payload.get("order_entity_id")
                        or (current.order_entity_id if current else None)
                    ),
                    correction_seq=seq,
                )
        elif kind == "commission":
            exec_id = str(payload["exec_id"])
            # First commission wins for a given exec_id (duplicate-safe).
            if exec_id not in commissions:
                commissions[exec_id] = _dec(payload.get("commission"))
        elif kind == "position":
            position_qty = _dec(payload.get("quantity"))
        elif kind == "mfe_mae_sample":
            samples.append(_dec(payload["price"]))
        elif kind == "quote_timing":
            quote_bid = _opt_dec(payload.get("quote_bid"))
            quote_ask = _opt_dec(payload.get("quote_ask"))
            signal_ts = _parse_ts(payload.get("signal_ts"))
            fill_ts = _parse_ts(payload.get("fill_ts"))
            if signal_ts is not None and fill_ts is not None:
                latency_ms = Decimal(str(int((fill_ts - signal_ts).total_seconds() * 1000)))
        elif kind == "rejection":
            rejection_refs.append(payload)
        elif kind == "breaker":
            breaker_refs.append(payload)
        elif kind == "operator":
            operator_actions.append(payload)

    fills = tuple(
        {
            "exec_id": f.exec_id,
            "leg": f.leg,
            "side": f.side,
            "quantity": str(f.quantity),
            "price": str(f.price),
            "order_entity_id": f.order_entity_id,
        }
        for f in sorted(fills_by_exec.values(), key=lambda x: x.exec_id)
    )

    entry_fills = [
        f for f in fills_by_exec.values()
        if f.leg == "entry" or f.side in _ENTRY_SIDES
    ]
    exit_fill_states = [
        f for f in fills_by_exec.values()
        if f.leg in _EXIT_LEGS or f.side in _EXIT_SIDES
    ]
    # Prefer leg classification; avoid double-counting if side alone matched entry.
    exit_fill_states = [
        f for f in exit_fill_states
        if f.leg in _EXIT_LEGS or f.exec_id not in {e.exec_id for e in entry_fills}
    ]
    exit_fills = tuple(
        {
            "exec_id": f.exec_id,
            "leg": f.leg,
            "side": f.side,
            "quantity": str(f.quantity),
            "price": str(f.price),
            "order_entity_id": f.order_entity_id,
        }
        for f in sorted(exit_fill_states, key=lambda x: x.exec_id)
    )

    total_commission = sum(commissions.values(), Decimal("0"))

    unresolved: list[str] = []
    if not entry_fills:
        unresolved.append("missing_entry_fill")
    if not exit_fill_states:
        unresolved.append("missing_exit_fill")
    if position_qty is not None and position_qty != 0:
        unresolved.append("open_position")

    flat_or_unknown = position_qty is None or position_qty == 0
    closed = bool(entry_fills and exit_fill_states and flat_or_unknown)
    if closed:
        missing_comm = [
            f.exec_id for f in list(entry_fills) + list(exit_fill_states)
            if f.exec_id not in commissions
        ]
        if missing_comm:
            unresolved.append("missing_commission")

    gross_pnl: Optional[Decimal] = None
    net_pnl: Optional[Decimal] = None
    # Never assume zero P&L/cost for unresolved trades (promotion safety).
    if closed and "missing_commission" not in unresolved:
        entry_notional = sum((f.price * f.quantity for f in entry_fills), Decimal("0"))
        exit_notional = sum((f.price * f.quantity for f in exit_fill_states), Decimal("0"))
        # Long-only: buy entry, sell exit → exit - entry.
        gross_pnl = exit_notional - entry_notional
        net_pnl = gross_pnl - total_commission
        unresolved = [
            r for r in unresolved
            if r not in ("missing_exit_fill", "open_position", "missing_entry_fill")
        ]

    avg_entry: Optional[Decimal] = None
    entry_qty = sum((f.quantity for f in entry_fills), Decimal("0"))
    if entry_fills and entry_qty > 0:
        avg_entry = sum((f.price * f.quantity for f in entry_fills), Decimal("0")) / entry_qty

    mfe: Optional[Decimal] = None
    mae: Optional[Decimal] = None
    if samples:
        mfe = max(samples)
        mae = min(samples)

    spread_bps: Optional[Decimal] = None
    slippage_bps: Optional[Decimal] = None
    if quote_bid is not None and quote_ask is not None and quote_bid > 0:
        mid = (quote_bid + quote_ask) / Decimal("2")
        if mid > 0:
            spread_bps = ((quote_ask - quote_bid) / mid) * Decimal("10000")
            if avg_entry is not None:
                slippage_bps = ((avg_entry - mid) / mid) * Decimal("10000")

    resolved = (
        len(unresolved) == 0
        and gross_pnl is not None
        and net_pnl is not None
    )

    return TradeAttribution(
        trade_id=trade_id,
        resolved=resolved,
        artifact_id=artifact_id,
        dataset_id=dataset_id,
        signal_id=signal_id,
        intent_id=intent_id,
        command_id=command_id or trade_id,
        order_group_id=order_group_id,
        approval_context_id=approval_context_id,
        fills=fills,
        exit_fills=exit_fills,
        policy_decisions=tuple(policy_decisions),
        rejection_refs=tuple(rejection_refs),
        breaker_refs=tuple(breaker_refs),
        operator_actions=tuple(operator_actions),
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        total_commission=total_commission,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        latency_ms=latency_ms,
        mfe=mfe,
        mae=mae,
        unresolved_reasons=tuple(unresolved),
    )


class AttributionLedger:
    """Authoritative attribution: append evidence, rebuild derived trades."""

    def __init__(
        self,
        journal: Any,
        db: Any,
        account_id: str,
        now: Optional[Callable[[], dt.datetime]] = None,
        store: Optional[AttributionStore] = None,
    ):
        self.journal = journal
        self.db = db
        self.account_id = account_id
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self._store = store or AttributionStore(db)

    @property
    def store(self) -> AttributionStore:
        return self._store

    def append(self, evidence_event: AttributionEvidenceEvent) -> bool:
        """Append raw evidence. Returns False on duplicate evidence_key."""
        now = _as_utc(self._now())
        inserted = {"ok": False}

        mutation = DomainMutation(
            event_type="attribution.evidence_appended",
            entity_type="attribution_evidence",
            entity_id=evidence_event.evidence_key,
            operation="upsert",
            account_id=self.account_id,
            source="trader_service",
            source_timestamp=_as_utc(evidence_event.source_timestamp),
            correlation_id=evidence_event.trade_id,
            payload={
                "evidence_key": evidence_event.evidence_key,
                "trade_id": evidence_event.trade_id,
                "event_kind": evidence_event.event_kind,
                "payload": dict(evidence_event.payload),
            },
        )

        def write(conn: Any, _revision: int) -> None:
            inserted["ok"] = self._store.insert_evidence_in_tx(
                conn,
                evidence_key=evidence_event.evidence_key,
                trade_id=evidence_event.trade_id,
                event_kind=evidence_event.event_kind,
                account_id=self.account_id,
                payload=evidence_event.payload,
                source_timestamp=evidence_event.source_timestamp,
                recorded_at=now,
            )

        # Use evidence_key as journal event_id for idempotent domain emission.
        # If raw insert is a duplicate, skip domain mutation entirely.
        existing = self.db.execute(
            "SELECT 1 FROM automation_decisions WHERE evidence_key = ?",
            [evidence_event.evidence_key],
            fetch="one",
        )
        if existing is not None:
            return False

        self.journal.mutate(
            self.journal.connect(),
            mutation,
            write,
            event_id=f"attr-ev:{evidence_event.evidence_key}",
        )
        return bool(inserted["ok"])

    def rebuild_trade(self, trade_id: str) -> TradeAttribution:
        evidence = self._store.list_evidence(trade_id)
        attr = rebuild_attribution_from_evidence(trade_id, evidence)
        now = _as_utc(self._now())
        cost_payload = {
            "trade_id": trade_id,
            "total_commission": str(attr.total_commission),
            "spread_bps": str(attr.spread_bps) if attr.spread_bps is not None else None,
            "slippage_bps": str(attr.slippage_bps) if attr.slippage_bps is not None else None,
            "latency_ms": str(attr.latency_ms) if attr.latency_ms is not None else None,
            "gross_pnl": str(attr.gross_pnl) if attr.gross_pnl is not None else None,
            "net_pnl": str(attr.net_pnl) if attr.net_pnl is not None else None,
        }

        mutation = DomainMutation(
            event_type="attribution.trade_rebuilt",
            entity_type="trade_attribution",
            entity_id=command_entity_id(trade_id),
            operation="upsert",
            account_id=self.account_id,
            source="trader_service",
            source_timestamp=now,
            correlation_id=trade_id,
            payload=attr.to_payload(),
        )

        def write(conn: Any, _revision: int) -> None:
            self._store.save_derived_in_tx(
                conn,
                trade_id=trade_id,
                account_id=self.account_id,
                resolved=attr.resolved,
                attribution_payload=attr.to_payload(),
                cost_payload=cost_payload,
                rebuilt_at=now,
            )

        # Unique event id so a crash that wiped derived rows can re-run write.
        # Dashboard observes rebuilds; it is never the source of truth.
        self.journal.mutate(
            self.journal.connect(),
            mutation,
            write,
            event_id=f"attr-rebuild:{trade_id}:{uuid.uuid4().hex}",
        )
        return attr

    def promotion_attribution(self) -> PromotionAttributionReport:
        """Resolved trades for promotion; unresolved reported separately."""
        resolved: list[TradeAttribution] = []
        unresolved: list[TradeAttribution] = []
        for trade_id in self._store.list_trade_ids():
            attr = self.rebuild_trade(trade_id)
            if attr.resolved:
                resolved.append(attr)
            else:
                unresolved.append(attr)
        return PromotionAttributionReport(
            resolved=tuple(resolved),
            unresolved=tuple(unresolved),
        )
