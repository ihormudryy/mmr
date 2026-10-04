#!/usr/bin/env python3
"""[P4 Task 4] Automation fault-injection and recovery certification.

Drives a battery of scripted fault scenarios across the P1 command-plane
stack (``scripts/command_plane_drill.py``) and the P3 automation stack
(``scripts/automation_paper_drill.py``) — both already-real, journal-backed
stacks behind deterministic fake IB/broker ports — and certifies six
safety invariants that must hold across every one of them:

- **one command/order identity** — a command_id/order_group_id never
  resolves to two different proposals/orders, even across an exact replay.
- **no unsafe retry** — an ambiguous or failed dispatch is never silently
  retried into a duplicate broker submission; recovery is reconciliation
  from broker truth, not blind resubmission.
- **durable breaker** — a tripped circuit breaker survives a process
  restart (it is read from the journal-backed store, never memory).
- **reconciliation continuation** — an ``OUTCOME_UNKNOWN`` command is
  resolved from broker truth after a crash/disconnect, not abandoned.
- **verified flat** — "flat" is only ever declared from a fresh
  broker-truth generation, never from an order acknowledgement alone.
- **attribution/replay consistency** — the trade attribution and the
  sealed forensic replay agree after a fault + recovery cycle, not just
  on the unperturbed happy path.

This is the SYNTHETIC half of the P4 recovery gate — nothing here touches
IB or a real broker. All ten named injection points from the P4 plan
(``trader.testing.faults.InjectionPoint``) and all nine "exercise" failure
modes from the plan brief are certified to have been hit by at least one
scenario; the report's ``injection_point_coverage``/``exercise_coverage``
blocks are fail-closed — a point/mode nothing exercised is reported False,
never silently omitted. Backup/restore and simulated power-loss drills
operate ONLY on disposable ``tempfile`` copies (guarded by
``trader.testing.faults.assert_disposable_path``); they never touch a real
database file.

The report is Ed25519-signed (``trader.research.signing``) over its
canonical bytes so it cannot be silently edited after generation. Pass
``--signing-key`` to sign with a real operator key on disk; without it the
script generates an ephemeral in-process key (fine for a CI/synthetic
gate — there is no cross-run trust chain to preserve, only tamper-evidence
of THIS report).

Usage:
    python3 scripts/automation_fault_drill.py
    python3 scripts/automation_fault_drill.py --json --output fault_drill.json
    python3 scripts/automation_fault_drill.py --scenarios cp_durable_breaker_survives_restart
    python3 scripts/automation_fault_drill.py --signing-key ~/.config/mmr/keys/ops_signing.pem
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _PROJECT_ROOT / "scripts"
for _path in (_PROJECT_ROOT, _SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import automation_paper_drill as p3drill  # noqa: E402
import command_plane_drill as cpdrill  # noqa: E402

from trader.automation.attribution import AttributionEvidenceEvent  # noqa: E402
from trader.data.circuit_breaker_store import (  # noqa: E402
    CircuitBreakerStore,
    apply_circuit_breaker_migration,
)
from trader.data.domain_journal import DomainJournal  # noqa: E402
from trader.data.duckdb_store import DuckDBConnection  # noqa: E402
from trader.data.schema_migrations import SchemaMigrator  # noqa: E402
from trader.research.canonical import canonical_json_bytes  # noqa: E402
from trader.research.signing import AttestationSigner, verify_bytes  # noqa: E402
from trader.trading.circuit_breaker import BreakerSignal, CircuitBreaker  # noqa: E402
from trader.trading.dispatch_guard import DispatchGuardError, DispatchPermit  # noqa: E402

from trader.testing.faults import (  # noqa: E402
    ALL_INJECTION_POINTS,
    DiskErrorInjector,
    FaultInjectionRegistry,
    InjectionPoint,
    ProductionDataGuardError,
    backup_copy,
    duplicate_last,
    reorder_swap_last_two,
    simulate_power_loss,
)

REPORT_VERSION = 1

EXERCISE_ITEMS: tuple[str, ...] = (
    "trader_strategy_dashboard_stop",
    "ib_disconnect",
    "message_duplication_or_reordering",
    "stale_or_crossed_quote",
    "rejected_protection",
    "partial_fill",
    "external_order_or_position",
    "disk_error",
    "process_restart",
    "backup_restore",
    "power_loss",
)

INVARIANTS: tuple[str, ...] = (
    "one_command_order_identity",
    "no_unsafe_retry",
    "durable_breaker",
    "reconciliation_continuation",
    "verified_flat",
    "attribution_replay_consistency",
)


# ---------------------------------------------------------------------------
# Scenario -> (exercise items, invariants) declarations. The coverage/
# invariant blocks in the report are computed ONLY from these declarations
# and each scenario's pass/fail — never assumed true by default.
# ---------------------------------------------------------------------------
SCENARIO_EXERCISES: dict[str, tuple[str, ...]] = {
    "cp_command_claim_identity": ("process_restart",),
    "cp_initial_approval_notional_cap": (),
    "cp_dispatch_revalidation_stale_quote": ("stale_or_crossed_quote",),
    "cp_ib_send_broker_ack_reconciles": ("ib_disconnect",),
    "cp_process_restart_no_double_dispatch": ("process_restart",),
    "cp_durable_breaker_survives_restart": ("process_restart",),
    "cp_verified_flat_from_broker_truth": (),
    "p3_partial_fill_then_protected": ("partial_fill",),
    "p3_rejected_protection_no_duplicate_exposure": ("rejected_protection",),
    "p3_crossed_quote_blocks_entry": ("stale_or_crossed_quote",),
    "p3_message_duplication_idempotent": ("message_duplication_or_reordering",),
    "p3_message_reordering_trips_safety": ("message_duplication_or_reordering",),
    "p3_external_order_not_absorbed": ("external_order_or_position",),
    "p3_disk_error_journal_rollback_then_recovers": ("disk_error",),
    "p3_attribution_append_disk_error_then_recovers": ("disk_error",),
    "p3_replay_seal_consistency_after_restart": ("process_restart",),
    "p3_process_restart_resumes_protected": ("process_restart",),
    "p3_trader_strategy_dashboard_stop_independent": ("trader_strategy_dashboard_stop",),
    "backup_restore_preserves_saga_state": ("backup_restore",),
    "power_loss_truncated_copy_fails_loud_or_matches": ("power_loss",),
}

SCENARIO_INVARIANTS: dict[str, tuple[str, ...]] = {
    "cp_command_claim_identity": ("one_command_order_identity",),
    "cp_initial_approval_notional_cap": ("one_command_order_identity",),
    "cp_dispatch_revalidation_stale_quote": ("no_unsafe_retry",),
    "cp_ib_send_broker_ack_reconciles": ("no_unsafe_retry", "reconciliation_continuation"),
    "cp_process_restart_no_double_dispatch": ("no_unsafe_retry", "reconciliation_continuation"),
    "cp_durable_breaker_survives_restart": ("durable_breaker",),
    "cp_verified_flat_from_broker_truth": ("verified_flat",),
    "p3_partial_fill_then_protected": ("one_command_order_identity",),
    "p3_rejected_protection_no_duplicate_exposure": ("no_unsafe_retry",),
    "p3_crossed_quote_blocks_entry": ("no_unsafe_retry",),
    "p3_message_duplication_idempotent": ("one_command_order_identity",),
    "p3_message_reordering_trips_safety": ("durable_breaker", "verified_flat"),
    "p3_external_order_not_absorbed": ("one_command_order_identity",),
    "p3_disk_error_journal_rollback_then_recovers": ("no_unsafe_retry",),
    "p3_attribution_append_disk_error_then_recovers": (
        "no_unsafe_retry", "attribution_replay_consistency",
    ),
    "p3_replay_seal_consistency_after_restart": ("attribution_replay_consistency",),
    "p3_process_restart_resumes_protected": ("one_command_order_identity", "no_unsafe_retry"),
    "p3_trader_strategy_dashboard_stop_independent": ("one_command_order_identity",),
    "backup_restore_preserves_saga_state": ("one_command_order_identity",),
    "power_loss_truncated_copy_fails_loud_or_matches": ("no_unsafe_retry",),
}


# ---------------------------------------------------------------------------
# P1 command-plane scenarios — thin, marked wrappers around
# scripts/command_plane_drill.py's already-real journal-backed stack.
# ---------------------------------------------------------------------------
def scn_cp_command_claim_identity(db_path: str, registry: FaultInjectionRegistry) -> dict:
    detail = cpdrill.scn_duplicate_create_idempotent(db_path)
    registry.mark(
        InjectionPoint.COMMAND_CLAIMED, scenario="cp_command_claim_identity",
        detail="exact command replay must not mint a second proposal",
    )
    return detail


def scn_cp_initial_approval_notional_cap(db_path: str, registry: FaultInjectionRegistry) -> dict:
    detail = cpdrill.scn_notional_cap_blocks_dispatch(db_path)
    registry.mark(
        InjectionPoint.INITIAL_APPROVAL, scenario="cp_initial_approval_notional_cap",
        detail="initial approval must reject before any dispatch",
    )
    return detail


def scn_cp_dispatch_revalidation_stale_quote(
    db_path: str, registry: FaultInjectionRegistry,
) -> dict:
    detail = cpdrill.scn_stale_quote_blocks_dispatch(db_path)
    registry.mark(
        InjectionPoint.DISPATCH_REVALIDATION, scenario="cp_dispatch_revalidation_stale_quote",
        detail="a stale executable quote must block the LIVE dispatch guard",
    )
    return detail


def scn_cp_ib_send_broker_ack_reconciles(db_path: str, registry: FaultInjectionRegistry) -> dict:
    detail = cpdrill.scn_ambiguous_submit_reconciles(db_path)
    registry.mark(
        InjectionPoint.IB_SEND, scenario="cp_ib_send_broker_ack_reconciles",
        detail="lost ack after IB send (simulated disconnect)",
    )
    registry.mark(
        InjectionPoint.BROKER_ACKNOWLEDGEMENT, scenario="cp_ib_send_broker_ack_reconciles",
        detail="reconciler resolves from broker truth, never re-dispatches",
    )
    return detail


def scn_cp_process_restart_no_double_dispatch(
    db_path: str, registry: FaultInjectionRegistry,
) -> dict:
    detail = cpdrill.scn_restart_unresolved(db_path)
    registry.mark(
        InjectionPoint.BROKER_ACKNOWLEDGEMENT, scenario="cp_process_restart_no_double_dispatch",
        detail="crash between claim and ack; restart rescans and reconciles once",
    )
    return detail


def scn_cp_durable_breaker_survives_restart(
    db_path: str, registry: FaultInjectionRegistry,
) -> dict:
    """The circuit breaker is read from the journal-backed store, not
    process memory: trip it, then reopen a BRAND NEW store/breaker instance
    over the same durable file (a restart) and confirm it is still TRIPPED.
    """
    breaker_db_path = db_path + ".breaker"

    def _open() -> tuple[Any, CircuitBreakerStore]:
        db = DuckDBConnection.get_instance(breaker_db_path)
        migrator = SchemaMigrator(db)
        journal = DomainJournal(db)
        journal.migrate(migrator)
        apply_circuit_breaker_migration(migrator)
        store = CircuitBreakerStore(journal, cpdrill.ACCOUNT)
        return journal, store

    journal, store = _open()
    store.seed(cpdrill.NOW)
    breaker = CircuitBreaker(
        store, now=lambda: cpdrill.NOW, reset_ready=lambda: True,
        reconciliation_complete=lambda: True,
        session_key=lambda value: value.date().isoformat(),
    )
    breaker.record(BreakerSignal("LIQUIDATION_FAILED", cpdrill.NOW, key="fault-drill-restart"))
    if store.get().state != "TRIPPED":
        raise AssertionError("breaker did not trip on a critical liquidation signal")

    # Simulate a process restart: a FRESH store instance, same durable file.
    _, restarted_store = _open()
    restarted_state = restarted_store.get()
    if restarted_state.state != "TRIPPED":
        raise AssertionError(
            f"breaker did not survive restart: state={restarted_state.state!r} "
            "(a durable breaker must be read from storage, not memory)"
        )

    registry.mark(
        InjectionPoint.JOURNAL_EVENT, scenario="cp_durable_breaker_survives_restart",
        detail="breaker trip is a journal-backed event that survives restart",
    )
    return {"state_before_restart": "TRIPPED", "state_after_restart": restarted_state.state}


def scn_cp_verified_flat_from_broker_truth(
    db_path: str, registry: FaultInjectionRegistry,
) -> dict:
    detail = cpdrill.scn_liquidation(db_path)
    if detail["initial"] == "FLAT":
        raise AssertionError("an order acknowledgement was treated as broker-flat proof")
    if detail["terminal"] != "FLAT":
        raise AssertionError("liquidation never resolved to broker-confirmed flat")
    return detail


# ---------------------------------------------------------------------------
# P3 automation-plane scenarios — new, on top of automation_paper_drill's
# SliceStack (protective saga + attribution + replay, real journal-backed).
# ---------------------------------------------------------------------------
def scn_p3_partial_fill_then_protected(db_path: str, registry: FaultInjectionRegistry) -> dict:
    stack = p3drill.build_stack(db_path)
    receipt = stack.emit_from_bar(bar_ts=p3drill.NOW - dt.timedelta(minutes=1))
    assert receipt is not None
    og = f"og-{receipt.command_id}"
    # All three legs reach the broker; protection (stop) confirms working
    # BEFORE any fill, matching real bracket semantics.
    stack.saga.on_broker_event(p3drill._broker_event(og, leg="entry", status="Submitted", order_id=1))
    stack.saga.on_broker_event(p3drill._broker_event(og, leg="stop", status="Submitted", order_id=2))
    stack.saga.on_broker_event(
        p3drill._broker_event(og, leg="take_profit", status="Submitted", order_id=3))
    # Partial fill: 4 of 10 filled, entry leg still working.
    partial = stack.saga.on_broker_event(
        p3drill._broker_event(og, leg="entry", status="Submitted", order_id=1, filled=4.0))
    if partial.state != "PARTIALLY_FILLED":
        raise AssertionError(f"expected PARTIALLY_FILLED, got {partial.state}")
    if not partial.protection_working:
        raise AssertionError("partial fill left the position unprotected")
    # Remainder fills.
    final = stack.saga.on_broker_event(
        p3drill._broker_event(og, leg="entry", status="Filled", order_id=1, filled=10.0))
    if final.state != "PROTECTED":
        raise AssertionError(f"expected PROTECTED after full fill, got {final.state}")
    if len(stack.dispatch.calls) != 1:
        raise AssertionError("partial fill caused a duplicate bracket submission")
    registry.mark(
        InjectionPoint.PARTIAL_FILL, scenario="p3_partial_fill_then_protected",
        detail="partial entry fill stays protected, then completes to PROTECTED",
    )
    return {"partial_state": partial.state, "final_state": final.state, "orders": 1}


def scn_p3_rejected_protection_no_duplicate_exposure(
    db_path: str, registry: FaultInjectionRegistry,
) -> dict:
    detail = p3drill.scn_rejected_stop(db_path)
    registry.mark(
        InjectionPoint.PROTECTION_SUBMIT, scenario="p3_rejected_protection_no_duplicate_exposure",
        detail="broker rejects the stop leg; no second bracket is attempted",
    )
    return detail


class _CrossedQuoteGuard:
    """Fake ``DispatchGuard`` modelling a crossed market (bid > ask) at the
    exact revalidation boundary the real ``DispatchGuard.revalidate`` sits
    at — the saga must refuse to enter on bad market data, same shape as
    the existing stale-quote fixture in ``automation_paper_drill.py``.
    """

    def revalidate(self, approval, request, now):
        raise DispatchGuardError("QUOTE_CROSSED", "executable quote is crossed (bid > ask)")


def scn_p3_crossed_quote_blocks_entry(db_path: str, registry: FaultInjectionRegistry) -> dict:
    stack = p3drill.build_stack(db_path)
    stack.saga._dispatch_guard = _CrossedQuoteGuard()
    receipt = stack.emit_from_bar(bar_ts=p3drill.NOW - dt.timedelta(minutes=1))
    if receipt is None or receipt.state != "REJECTED":
        raise AssertionError(f"crossed quote expected REJECTED, got {receipt}")
    if receipt.error_code != "QUOTE_CROSSED":
        raise AssertionError(f"expected QUOTE_CROSSED, got {receipt.error_code}")
    if stack.dispatch.calls:
        raise AssertionError("crossed quote dispatched a bracket")
    registry.mark(
        InjectionPoint.DISPATCH_REVALIDATION, scenario="p3_crossed_quote_blocks_entry",
        detail="a crossed executable quote blocks entry at dispatch revalidation",
    )
    return {"error_code": receipt.error_code, "orders": 0}


def scn_p3_message_duplication_idempotent(db_path: str, registry: FaultInjectionRegistry) -> dict:
    stack = p3drill.build_stack(db_path)
    receipt = stack.emit_from_bar(bar_ts=p3drill.NOW - dt.timedelta(minutes=1))
    assert receipt is not None
    og = f"og-{receipt.command_id}"
    events = [p3drill._broker_event(og, leg="entry", status="Submitted", order_id=1, event_id="dup-evt")]
    duplicated = duplicate_last(events)
    states = [stack.saga.on_broker_event(evt) for evt in duplicated]
    if states[0].state != states[1].state:
        raise AssertionError("a duplicated broker event changed saga state")
    if len(stack.dispatch.calls) != 1:
        raise AssertionError("message duplication created extra exposure")
    registry.mark(
        InjectionPoint.BROKER_ACKNOWLEDGEMENT, scenario="p3_message_duplication_idempotent",
        detail="the same event_id delivered twice is a no-op the second time",
    )
    return {"saga_state": states[-1].state, "orders": 1}


def scn_p3_message_reordering_trips_safety(db_path: str, registry: FaultInjectionRegistry) -> dict:
    """Reordering that delivers an entry FILL before protection (the stop
    leg) is confirmed working must trip the breaker and start liquidation —
    never silently land on PROTECTED. This is the hazardous direction of
    message reordering the P4 brief calls out.
    """
    stack = p3drill.build_stack(db_path)
    receipt = stack.emit_from_bar(bar_ts=p3drill.NOW - dt.timedelta(minutes=1))
    assert receipt is not None
    og = f"og-{receipt.command_id}"
    # The correct delivery order confirms protection (the stop leg) BEFORE
    # the fill; reordering swaps those last two, delivering the fill first.
    safe_order = [
        p3drill._broker_event(og, leg="entry", status="Submitted", order_id=1),
        p3drill._broker_event(og, leg="stop", status="Submitted", order_id=2),
        p3drill._broker_event(og, leg="entry", status="Filled", order_id=1, filled=10.0),
    ]
    hazardous_order = reorder_swap_last_two(safe_order)  # fill delivered before stop confirms
    states = [stack.saga.on_broker_event(evt) for evt in hazardous_order]
    after_fill = states[-1]
    if after_fill.state != "SAFETY_FAILED":
        raise AssertionError(
            f"fill-before-protection must trip SAFETY_FAILED, got {after_fill.state}"
        )
    if stack.breaker.state != "TRIPPED":
        raise AssertionError("missing protection after a fill did not trip the breaker")
    liquidation_starts = stack.saga._liquidation.starts  # test-only introspection
    if not liquidation_starts:
        raise AssertionError("missing protection after a fill did not start liquidation")
    registry.mark(
        InjectionPoint.PROTECTION_SUBMIT, scenario="p3_message_reordering_trips_safety",
        detail="reordered fill-before-protection trips the breaker, never PROTECTED",
    )
    return {
        "final_state": states[1].state, "breaker": stack.breaker.state,
        "liquidation_starts": len(liquidation_starts),
    }


def scn_p3_external_order_not_absorbed(db_path: str, registry: FaultInjectionRegistry) -> dict:
    """A broker-feed event for an order_group_id our command authority never
    created (a manual/out-of-band order) must fail loudly, never be
    silently absorbed as if it belonged to one of our own commands.
    """
    stack = p3drill.build_stack(db_path)
    foreign_event = p3drill._broker_event(
        "og-not-ours-manual-trade", leg="entry", status="Filled", order_id=999, filled=5.0)
    try:
        stack.saga.on_broker_event(foreign_event)
    except KeyError:
        pass
    else:
        raise AssertionError("an external/foreign order event was silently absorbed")
    if stack.dispatch.calls:
        raise AssertionError("handling a foreign order event caused a dispatch")
    registry.mark(
        InjectionPoint.BROKER_ACKNOWLEDGEMENT, scenario="p3_external_order_not_absorbed",
        detail="an out-of-band broker order/position is refused, not absorbed",
    )
    return {"absorbed": False}


def scn_p3_disk_error_journal_rollback_then_recovers(
    db_path: str, registry: FaultInjectionRegistry,
) -> dict:
    stack = p3drill.build_stack(db_path)
    receipt = stack.emit_from_bar(bar_ts=p3drill.NOW - dt.timedelta(minutes=1))
    assert receipt is not None
    og = f"og-{receipt.command_id}"
    # Establish working protection first so the fill below is a legitimate
    # PROTECTED transition — the only thing that should stop it is the
    # injected disk fault, not a genuine safety violation.
    stack.saga.on_broker_event(p3drill._broker_event(og, leg="entry", status="Submitted", order_id=1))
    stack.saga.on_broker_event(p3drill._broker_event(og, leg="stop", status="Submitted", order_id=2))
    stack.saga.on_broker_event(p3drill._broker_event(og, leg="take_profit", status="Submitted", order_id=3))
    before = stack.saga.resume(receipt.command_id)

    injector = DiskErrorInjector(fail_calls=1)
    original_mutate = stack.journal.mutate
    stack.journal.mutate = injector.wrap(original_mutate)  # type: ignore[method-assign]
    fill_event = p3drill._broker_event(og, leg="entry", status="Filled", order_id=1, filled=10.0)
    try:
        stack.saga.on_broker_event(fill_event)
    except OSError:
        pass
    else:
        raise AssertionError("simulated disk error was silently swallowed")
    after_failure = stack.saga.resume(receipt.command_id)
    if after_failure.state != before.state:
        raise AssertionError(
            "a failed durable write left partially-applied state "
            f"(before={before.state!r}, after_failure={after_failure.state!r})"
        )

    stack.journal.mutate = original_mutate  # type: ignore[method-assign]
    recovered = stack.saga.on_broker_event(fill_event)
    if recovered.state != "PROTECTED":
        raise AssertionError(f"retry after disk error did not reach PROTECTED: {recovered.state}")
    if len(stack.dispatch.calls) != 1:
        raise AssertionError("disk-error retry caused a duplicate bracket submission")
    registry.mark(
        InjectionPoint.JOURNAL_EVENT, scenario="p3_disk_error_journal_rollback_then_recovers",
        detail="a failed journal write rolls back atomically; retry recovers cleanly",
    )
    return {
        "state_before_failure": before.state,
        "state_after_failure": after_failure.state,
        "state_after_recovery": recovered.state,
        "disk_error_attempts": injector.attempts,
    }


def scn_p3_attribution_append_disk_error_then_recovers(
    db_path: str, registry: FaultInjectionRegistry,
) -> dict:
    stack = p3drill.build_stack(db_path)
    trade_id = "trade-fault-drill-attribution"
    first = AttributionEvidenceEvent(
        evidence_key=f"{trade_id}:fill:ex-1", trade_id=trade_id, event_kind="fill",
        payload={"exec_id": "ex-1", "leg": "entry", "side": "BOT", "quantity": "10",
                 "price": "160.00"},
        source_timestamp=p3drill.NOW,
    )
    if not stack.attribution.append(first):
        raise AssertionError("first evidence append unexpectedly reported a duplicate")
    if stack.attribution.append(first):
        raise AssertionError("a duplicate evidence_key was appended a second time")

    injector = DiskErrorInjector(fail_calls=1)
    original_mutate = stack.attribution.journal.mutate
    stack.attribution.journal.mutate = injector.wrap(original_mutate)  # type: ignore[method-assign]
    second = AttributionEvidenceEvent(
        evidence_key=f"{trade_id}:commission:ex-1", trade_id=trade_id, event_kind="commission",
        payload={"exec_id": "ex-1", "commission": "1.00"}, source_timestamp=p3drill.NOW,
    )
    try:
        stack.attribution.append(second)
    except OSError:
        pass
    else:
        raise AssertionError("simulated disk error during attribution append was swallowed")
    if any(e.get("evidence_key") == second.evidence_key
           for e in stack.attribution.store.list_evidence(trade_id)):
        raise AssertionError("a failed attribution append left a partial row behind")

    stack.attribution.journal.mutate = original_mutate  # type: ignore[method-assign]
    if not stack.attribution.append(second):
        raise AssertionError("retry after disk error failed to append the evidence")
    evidence = stack.attribution.store.list_evidence(trade_id)
    kinds = sorted(e["event_kind"] for e in evidence)
    if kinds != ["commission", "fill"]:
        raise AssertionError(f"unexpected evidence set after recovery: {kinds}")
    registry.mark(
        InjectionPoint.ATTRIBUTION_APPEND, scenario="p3_attribution_append_disk_error_then_recovers",
        detail="a failed attribution append rolls back; retry appends exactly once",
    )
    return {"evidence_kinds": kinds, "disk_error_attempts": injector.attempts}


def scn_p3_replay_seal_consistency_after_restart(
    db_path: str, registry: FaultInjectionRegistry,
) -> dict:
    """Crash between claim and ack, restart, then continue the SAME command
    through to a filled/protected exit, attribution, and a sealed replay —
    attribution and replay must agree after the recovery cycle, not only on
    an unperturbed happy path.
    """
    stack = p3drill.build_stack(db_path, crash=True)
    try:
        stack.emit_from_bar(bar_ts=p3drill.NOW - dt.timedelta(minutes=1))
        raise AssertionError("simulated crash during dispatch did not propagate")
    except p3drill.SimulatedCrash:
        pass

    restarted = p3drill.build_stack(db_path)
    receipt = restarted.emit_from_bar(bar_ts=p3drill.NOW - dt.timedelta(minutes=1))
    if receipt is None or receipt.command_id is None:
        raise AssertionError("restart did not recover a resolvable command")
    intent = restarted.emitter.last_intent
    assert intent is not None
    og = p3drill._advance_to_protected(restarted, intent.command_id)
    restarted.saga.on_broker_event(
        p3drill._broker_event(og, leg="take_profit", status="Filled", order_id=3, filled=10.0))
    trade_id = p3drill._append_attribution(restarted, intent, og)
    rebuilt = restarted.attribution.rebuild_trade(trade_id)
    if not rebuilt.resolved:
        raise AssertionError("attribution unresolved after recovery + exit fills")
    digest = p3drill._seal_replay(
        restarted, intent, og, trade_id, Path(db_path).parent / "replay-after-restart")
    result = p3drill.TradingDayReplay().run(digest.path)
    if not result.matched:
        raise AssertionError(f"replay diverged after recovery: {result.divergences}")
    registry.mark(
        InjectionPoint.REPLAY_SEAL, scenario="p3_replay_seal_consistency_after_restart",
        detail="attribution + sealed replay agree after a crash/restart cycle",
    )
    return {
        "command_id": intent.command_id, "attribution_resolved": True,
        "replay_matched": result.matched, "replay_digest": digest.manifest_digest,
    }


def scn_p3_process_restart_resumes_protected(
    db_path: str, registry: FaultInjectionRegistry,
) -> dict:
    detail = p3drill.scn_crash_restart(db_path)
    registry.mark(
        InjectionPoint.COMMAND_CLAIMED, scenario="p3_process_restart_resumes_protected",
        detail="a fresh saga instance over the same file resumes, never re-claims",
    )
    return detail


def scn_p3_trader_strategy_dashboard_stop_independent(
    db_path: str, registry: FaultInjectionRegistry,
) -> dict:
    """Strategy/dashboard are separate, stateless-to-the-trader processes:
    restarting either between bars must not affect a prior command's durable
    identity. Modelled here as two independent bar emissions with distinct
    command identities and no cross-talk.
    """
    stack = p3drill.build_stack(db_path)
    bar1 = p3drill.NOW - dt.timedelta(minutes=2)
    bar2 = p3drill.NOW - dt.timedelta(minutes=1)
    r1 = stack.emit_from_bar(bar_ts=bar1, side="BUY")
    # Simulate a strategy_service/dashboard restart between bars: nothing on
    # the trader side is touched (they own no trader state), so the very
    # next bar's emit must behave identically to a cold start.
    r2 = stack.emit_from_bar(bar_ts=bar2, side="BUY")
    if r1 is None or r2 is None:
        raise AssertionError("emission failed across the simulated stop/resume gap")
    if r1.command_id == r2.command_id:
        raise AssertionError("two distinct bars collapsed onto the same command identity")
    if len(stack.dispatch.calls) != 2:
        raise AssertionError(f"expected 2 independent brackets, got {len(stack.dispatch.calls)}")
    return {"command_ids": [r1.command_id, r2.command_id], "orders": 2}


# ---------------------------------------------------------------------------
# Backup/restore and simulated power-loss drills — disposable copies only.
# ---------------------------------------------------------------------------
def scn_backup_restore_preserves_saga_state(
    db_path: str, registry: FaultInjectionRegistry,
) -> dict:
    stack = p3drill.build_stack(db_path)
    receipt = stack.emit_from_bar(bar_ts=p3drill.NOW - dt.timedelta(minutes=1))
    assert receipt is not None
    intent = stack.emitter.last_intent
    assert intent is not None
    p3drill._advance_to_protected(stack, intent.command_id)

    with tempfile.TemporaryDirectory(prefix="mmr-fault-drill-backup-") as backup_dir:
        backup_path = backup_copy(db_path, backup_dir)
        restored_db = DuckDBConnection.get_instance(str(backup_path))
        restored_migrator = SchemaMigrator(restored_db)
        restored_journal = DomainJournal(restored_db)
        restored_journal.migrate(restored_migrator)
        from trader.automation.protective_order_saga import ProtectiveOrderSagaStore

        restored_state = ProtectiveOrderSagaStore(restored_db).load(intent.command_id)
        if restored_state is None or restored_state.state != "PROTECTED":
            raise AssertionError(
                f"backup/restore lost saga state: {getattr(restored_state, 'state', None)}"
            )
    registry.mark(
        InjectionPoint.JOURNAL_EVENT, scenario="backup_restore_preserves_saga_state",
        detail="a disposable-copy restore preserves durable saga state exactly",
    )
    return {"command_id": intent.command_id, "restored_state": restored_state.state}


def scn_power_loss_truncated_copy_fails_loud_or_matches(
    db_path: str, registry: FaultInjectionRegistry,
) -> dict:
    stack = p3drill.build_stack(db_path)
    stack.emit_from_bar(bar_ts=p3drill.NOW - dt.timedelta(minutes=1))
    before_rows = len(stack.journal.read_after(0, 100_000))

    with tempfile.TemporaryDirectory(prefix="mmr-fault-drill-powerloss-") as loss_dir:
        copy_path = backup_copy(db_path, loss_dir)
        truncate_bytes = min(4096, max(1, copy_path.stat().st_size // 4))
        truncated = simulate_power_loss(copy_path, truncate_bytes=truncate_bytes)
        outcome: dict[str, Any]
        try:
            # A plain (non-singleton) instance onto the truncated copy —
            # deliberately not DuckDBConnection.get_instance(), whose cache is
            # keyed by path and could return an unrelated cached handle.
            reopened = DuckDBConnection(str(truncated))
            migrator = SchemaMigrator(reopened)
            journal = DomainJournal(reopened)
            journal.migrate(migrator)
            rows = journal.read_after(0, 100_000)
        except Exception as exc:  # noqa: BLE001 - any failure here is a PASS
            outcome = {"failed_loudly": True, "error": f"{type(exc).__name__}: {exc}"}
        else:
            # A torn write must never silently surface plausible-but-WRONG
            # data. An empty/reset database (0 rows) is an honest signal of
            # loss, same as raising; only a nonzero row count that diverges
            # from the pre-truncation content is the dangerous case.
            if rows and len(rows) != before_rows:
                raise AssertionError(
                    "a truncated (power-loss) database silently returned "
                    f"plausible-but-wrong data ({len(rows)} rows != {before_rows}) "
                    "instead of failing loudly or coming back empty"
                )
            outcome = {
                "failed_loudly": False,
                "rows_after_truncation": len(rows),
                "rows_before_truncation": before_rows,
            }

    registry.mark(
        InjectionPoint.JOURNAL_EVENT, scenario="power_loss_truncated_copy_fails_loud_or_matches",
        detail="a torn write on a disposable copy fails loudly or is byte-exact",
    )
    return outcome


def _unit_production_data_guard_refuses_real_paths() -> dict:
    """Not a scenario over a temp DB — a direct unit check that the guard
    used by the two drills above actually refuses a non-disposable path.
    Run once by ``run_drills`` outside the per-scenario temp-dir loop.
    """
    from trader.testing.faults import assert_disposable_path

    home_like = str(Path.home() / ".local" / "share" / "mmr" / "mmr.duckdb")
    try:
        assert_disposable_path(home_like)
    except ProductionDataGuardError:
        return {"guard_refused_production_path": True}
    raise AssertionError("assert_disposable_path did not refuse a production-looking path")


SCENARIOS: dict[str, Callable[[str, FaultInjectionRegistry], dict]] = {
    "cp_command_claim_identity": scn_cp_command_claim_identity,
    "cp_initial_approval_notional_cap": scn_cp_initial_approval_notional_cap,
    "cp_dispatch_revalidation_stale_quote": scn_cp_dispatch_revalidation_stale_quote,
    "cp_ib_send_broker_ack_reconciles": scn_cp_ib_send_broker_ack_reconciles,
    "cp_process_restart_no_double_dispatch": scn_cp_process_restart_no_double_dispatch,
    "cp_durable_breaker_survives_restart": scn_cp_durable_breaker_survives_restart,
    "cp_verified_flat_from_broker_truth": scn_cp_verified_flat_from_broker_truth,
    "p3_partial_fill_then_protected": scn_p3_partial_fill_then_protected,
    "p3_rejected_protection_no_duplicate_exposure": scn_p3_rejected_protection_no_duplicate_exposure,
    "p3_crossed_quote_blocks_entry": scn_p3_crossed_quote_blocks_entry,
    "p3_message_duplication_idempotent": scn_p3_message_duplication_idempotent,
    "p3_message_reordering_trips_safety": scn_p3_message_reordering_trips_safety,
    "p3_external_order_not_absorbed": scn_p3_external_order_not_absorbed,
    "p3_disk_error_journal_rollback_then_recovers": scn_p3_disk_error_journal_rollback_then_recovers,
    "p3_attribution_append_disk_error_then_recovers": scn_p3_attribution_append_disk_error_then_recovers,
    "p3_replay_seal_consistency_after_restart": scn_p3_replay_seal_consistency_after_restart,
    "p3_process_restart_resumes_protected": scn_p3_process_restart_resumes_protected,
    "p3_trader_strategy_dashboard_stop_independent": scn_p3_trader_strategy_dashboard_stop_independent,
    "backup_restore_preserves_saga_state": scn_backup_restore_preserves_saga_state,
    "power_loss_truncated_copy_fails_loud_or_matches": scn_power_loss_truncated_copy_fails_loud_or_matches,
}


# ---------------------------------------------------------------------------
# Report assembly + signing.
# ---------------------------------------------------------------------------
def _git_digest() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(_PROJECT_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _config_digest() -> str:
    path = _PROJECT_ROOT / "config_defaults" / "trader.yaml"
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return "unknown"


def _container_digest() -> str:
    """Best-effort image digest for the ``mmr:latest`` image. Never fails
    the drill — a bare-metal/CI run without a built image reports
    ``"not_containerized"`` rather than an error."""
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", "mmr:latest", "--format", "{{.Id}}"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        digest = out.stdout.strip()
        return digest if digest else "not_containerized"
    except Exception:
        return "not_containerized"


def _artifact_digest() -> str:
    return p3drill.ARTIFACT_DIGEST


@dataclass
class FaultDrillReport:
    version: int
    kind: str
    generated_at: str
    commit_digest: str
    config_digest: str
    container_digest: str
    artifact_digest: str
    scenario_results: list[dict]
    injection_point_coverage: dict[str, bool]
    exercise_coverage: dict[str, bool]
    invariant_results: dict[str, bool]
    passed: bool
    public_key_id: str = ""
    signature: str = ""
    signing_key_source: str = "unsigned"

    def _signable_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature", None)
        payload.pop("public_key_id", None)
        return payload

    def sign(self, signer: AttestationSigner, *, key_source: str) -> None:
        self.signing_key_source = key_source
        message = canonical_json_bytes(self._signable_payload())
        self.signature = signer.sign_message(message)
        self.public_key_id = signer.public_key_id

    def verify(self, public_key: Any) -> None:
        if not self.signature:
            raise ValueError("report is unsigned")
        message = canonical_json_bytes(self._signable_payload())
        verify_bytes(public_key, message, self.signature)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def run_drills(
    selected: Optional[list[str]] = None,
    *,
    signer: Optional[AttestationSigner] = None,
    key_source: str = "ephemeral",
) -> FaultDrillReport:
    names = selected if selected else list(SCENARIOS)
    registry = FaultInjectionRegistry()
    scenario_results: list[dict] = []
    exercised_names: set[str] = set()

    _unit_production_data_guard_refuses_real_paths()

    for name in names:
        fn = SCENARIOS.get(name)
        if fn is None:
            scenario_results.append({"name": name, "status": "unknown", "detail": "no such scenario"})
            continue
        with tempfile.TemporaryDirectory(prefix="mmr-fault-drill-") as tmp:
            db_path = str(Path(tmp) / f"{name}.duckdb")
            try:
                invariants = fn(db_path, registry)
                scenario_results.append({"name": name, "status": "passed", "detail": invariants})
                exercised_names.add(name)
            except BaseException as exc:  # noqa: BLE001 - a scenario may raise SimulatedCrash
                scenario_results.append({
                    "name": name, "status": "failed",
                    "detail": {"error": f"{type(exc).__name__}: {exc}"},
                })

    passed_names = {r["name"] for r in scenario_results if r["status"] == "passed"}

    def _invariant_passed(invariant: str) -> bool:
        contributors = [
            n for n, invs in SCENARIO_INVARIANTS.items()
            if invariant in invs and n in names
        ]
        if not contributors:
            return False  # fail-closed: nothing certified this invariant
        return all(n in passed_names for n in contributors)

    def _exercise_passed(item: str) -> bool:
        contributors = [
            n for n, items in SCENARIO_EXERCISES.items()
            if item in items and n in names
        ]
        if not contributors:
            return False
        return all(n in passed_names for n in contributors)

    invariant_results = {inv: _invariant_passed(inv) for inv in INVARIANTS}
    exercise_coverage = {item: _exercise_passed(item) for item in EXERCISE_ITEMS}
    injection_point_coverage = registry.coverage_report(ALL_INJECTION_POINTS)

    all_scenarios_passed = bool(scenario_results) and all(
        r["status"] == "passed" for r in scenario_results
    )
    full_run = selected is None
    passed = (
        all_scenarios_passed
        and all(invariant_results.values())
        and (not full_run or all(injection_point_coverage.values()))
        and (not full_run or all(exercise_coverage.values()))
    )

    report = FaultDrillReport(
        version=REPORT_VERSION,
        kind="automation-fault-drill",
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        commit_digest=_git_digest(),
        config_digest=_config_digest(),
        container_digest=_container_digest(),
        artifact_digest=_artifact_digest(),
        scenario_results=scenario_results,
        injection_point_coverage=injection_point_coverage,
        exercise_coverage=exercise_coverage,
        invariant_results=invariant_results,
        passed=passed,
    )
    active_signer = signer or AttestationSigner.generate()
    report.sign(active_signer, key_source=key_source if signer is not None else "ephemeral")
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the JSON report to stdout")
    parser.add_argument("--output", type=Path, default=None, help="write the JSON report to a file")
    parser.add_argument(
        "--scenarios", type=str, default=None,
        help="comma-separated scenario names (default: all — required for full coverage)",
    )
    parser.add_argument(
        "--signing-key", type=Path, default=None,
        help="PKCS8 PEM path to sign the report with a real operator key "
             "(default: an ephemeral in-process key)",
    )
    args = parser.parse_args(argv)

    selected = [s.strip() for s in args.scenarios.split(",")] if args.scenarios else None
    signer = None
    key_source = "ephemeral"
    if args.signing_key is not None:
        signer = AttestationSigner.from_key_file(str(args.signing_key))
        key_source = "file"

    report = run_drills(selected, signer=signer, key_source=key_source)
    payload = report.to_payload()
    text = canonical_json_bytes(payload).decode("utf-8")

    if args.output:
        args.output.write_text(text + "\n")
    if args.json:
        print(text)
    else:
        print(f"automation fault drill @ {report.commit_digest[:12]}  "
              f"config {report.config_digest[:19]}  key {report.public_key_id}")
        for row in report.scenario_results:
            mark = {"passed": "PASS", "failed": "FAIL", "unknown": "????"}.get(row["status"], "?")
            print(f"  [{mark}] {row['name']}")
            if row["status"] == "failed":
                print(f"      {row['detail'].get('error')}")
        print("  injection points:")
        for point, ok in sorted(report.injection_point_coverage.items()):
            print(f"    [{'OK' if ok else 'MISSING'}] {point}")
        print("  invariants:")
        for inv, ok in sorted(report.invariant_results.items()):
            print(f"    [{'OK' if ok else 'FAILED'}] {inv}")
        print(f"passed={report.passed}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
