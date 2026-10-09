"""Plan 3 Task 7: ai_paper ENTER decisions through the real coordinator, saga, session_risk and guard."""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal

import pytest

from tests.automation.ai_paper_fixtures import ACCOUNT, CONID, NOW, OTHER, order, pos
from tests.automation.ai_paper_world import GOOD, World
from trader.automation.ai_deployments import AiDeployment
from trader.automation.ai_paper_decision import command_id_for
from trader.automation.ai_paper_experiment import ExperimentView
from trader.automation.protective_order_saga import BrokerOrderEvent
from trader.automation.risk_limits import PAPER_LIMITS

SATURDAY = dt.datetime(2026, 7, 18, 15, 0, tzinfo=dt.timezone.utc)
AFTER_CUTOFF = dt.datetime(2026, 7, 17, 19, 31, tzinfo=dt.timezone.utc)   # 15:31 ET


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def decision_id_of(receipt) -> str:
    return receipt.command_id[len("aip-"):]


def test_enter_submits_one_protective_bracket(world):
    receipt = world.submit()
    assert receipt.state == "SUBMITTED", receipt
    plan = world.dispatch.plans[0]
    # Sized on the entry limit 100.10 (ask + 10 bps): 5% position = 499 shares.
    assert [leg.role for leg in plan.legs] == ["entry", "stop"] and plan.legs[0].quantity == 499
    assert plan.order_ref == "mmr:og-aip-dec-00000001"
    assert world.decisions.row("dec-00000001").state == "SUBMITTED"
    assert world.ledger.get("aip-dec-00000001").state == "SUBMITTED"


def test_the_entry_uses_the_trader_order_type(world):                         # R18
    world.submit(target_price=120.0)
    entry = world.dispatch.plans[0].legs[0]
    assert (entry.order_type, entry.limit_price) == ("LMT", Decimal("100.10"))   # ask 100.00 + 10 bps
    assert [leg.role for leg in world.dispatch.plans[0].legs] == ["entry", "take_profit", "stop"]


def test_same_decision_id_replays_and_a_changed_body_conflicts(world):
    first = world.submit()
    second = world.submit()
    assert (second.state, second.command_id) == (first.state, first.command_id)
    assert len(world.dispatch.plans) == 1
    assert world.submit(quantity=10).error_code == "COMMAND_CONFLICT"


def test_new_decision_id_cannot_retry_a_conid_with_an_unknown_outcome(world):
    world.dispatch.raise_ambiguous = True
    assert world.submit().state == "OUTCOME_UNKNOWN"
    world.dispatch.raise_ambiguous = False
    assert world.submit(decision_id="dec-00000002").error_code == "OUTCOME_UNKNOWN_PENDING"
    assert world.scheduled == ["aip-dec-00000001"]


def test_unacknowledged_submitted_entry_blocks_its_conid(world):              # R13, owner answer
    world.submit()                                                              # broker does not show og-aip-... yet
    assert world.submit(decision_id="dec-00000002", quantity=1).error_code == "OUTCOME_UNKNOWN_PENDING"


def test_acknowledged_working_entry_blocks_a_duplicate_and_counts_as_pending(world):
    world.submit()
    world.broker.show_working_entry("og-aip-dec-00000001", quantity=499)
    assert world.submit(decision_id="dec-00000002", quantity=1).error_code == "ENTRY_ALREADY_WORKING"
    world.broker.add_working_entries(other_conids=2)                            # 3 working entries now
    assert world.submit(decision_id="dec-00000003", conid=OTHER).error_code == "MAX_PENDING_ENTRIES"
    assert len(world.dispatch.plans) == 1


def test_acknowledged_entry_counts_toward_gross(world):
    world.submit()
    world.broker.show_working_entry("og-aip-dec-00000001", quantity=500)       # 50,050 = 5% working
    assert world.submit(decision_id="dec-00000002", conid=OTHER, quantity=200).error_code == "QUANTITY_ABOVE_MAXIMUM"


def test_filled_entry_allows_a_later_entry_within_limits(world):
    world.submit()
    # The broker filled 100 of 499 and cancelled the rest; the saga recorded the events.
    group = "og-aip-dec-00000001"
    for n, (leg, status, filled) in enumerate((("stop", "PreSubmitted", 0.0), ("entry", "Submitted", 100.0),
                                               ("entry", "Cancelled", 100.0)), start=1):
        world.saga.on_broker_event(BrokerOrderEvent(
            order_group_id=group, leg=leg, status=status, filled_quantity=filled, total_quantity=499.0,
            order_id=n, event_id=f"evt-{n}", source_timestamp=NOW))
    world.broker.set(positions=(pos(CONID, 100.0, 10_000.0),))
    receipt = world.submit(decision_id="dec-00000002", quantity=100)
    assert receipt.state == "SUBMITTED", receipt


def test_concurrent_decisions_never_create_a_duplicate_entry(world):
    with ThreadPoolExecutor(4) as pool:
        receipts = list(pool.map(lambda i: world.submit(decision_id=f"dec-0000010{i}"), range(4)))
    assert len(world.dispatch.plans) == 1
    assert sorted(r.error_code or "ok" for r in receipts) == ["OUTCOME_UNKNOWN_PENDING"] * 3 + ["ok"]


def _setup(world, setup):
    """Apply one admission fault; returns the body changes it needs."""
    if setup == "no_experiment":
        world.experiments.view = None
    elif setup in ("paused", "killed", "stopped"):
        world.experiments.view = ExperimentView("exp1", setup.upper())
    elif setup == "weekend":
        world.clock.now = SATURDAY
    elif setup == "latched":
        world.start_session()
        world.policy.latch("DAILY_LOSS", "earlier")
    elif setup == "no_policy":
        world.db.execute("DELETE FROM ai_risk_policy_revisions")
    elif setup == "tampered":
        world.db.execute("UPDATE ai_deployments SET record_json = replace(record_json, '60000.0', '90000.0')")
    elif setup == "shadow_verdict":
        digest, _ = world.deployments.register(
            AiDeployment.from_json({**GOOD, "decider_verdict": "SHADOW"}), principal="ai_research",
            command_id="dep-2")
        return {"deployment_digest": digest}
    elif setup == "exit_owner_active":
        world.exit_owners.claim_scoped(account_id=ACCOUNT, conid=CONID, root_id="time-exit-1",
                                       goal_quantity=None, now=NOW)
    elif setup == "paused_trading":
        world.controls.set(ACCOUNT, True, None, "pause-1", "test", NOW)
    return {
        "expired": {"expires_at": (world.clock() - dt.timedelta(seconds=1)).isoformat()},
        "expiry_too_far": {"expires_at": (world.clock() + dt.timedelta(minutes=16)).isoformat()},
        "stale_revision": {"policy_revision": 2},
        "unsealed": {"deployment_digest": "sha256:" + "b" * 64},
        "conid_outside": {"conid": 999_999},
        "sell_entry": {"side": "SELL"},
    }.get(setup, {})


@pytest.mark.parametrize("setup,code", [
    ("principal_cli", "PRINCIPAL_FORBIDDEN"), ("no_experiment", "NO_EXPERIMENT"),
    ("paused", "EXPERIMENT_NOT_ARMED"), ("killed", "EXPERIMENT_NOT_ARMED"), ("stopped", "EXPERIMENT_STOPPED"),
    ("expired", "DECISION_EXPIRED"), ("expiry_too_far", "DECISION_EXPIRY_TOO_FAR"),
    ("weekend", "SESSION_CLOSED"), ("latched", "RISK_LATCHED"), ("no_policy", "NO_ACCEPTED_POLICY"),
    ("stale_revision", "POLICY_REVISION_STALE"), ("unsealed", "DEPLOYMENT_NOT_SEALED"),
    ("tampered", "DEPLOYMENT_TAMPERED"), ("shadow_verdict", "DEPLOYMENT_NOT_DEPLOYABLE"),
    ("conid_outside", "CONID_NOT_IN_DEPLOYMENT"), ("sell_entry", "SIDE_NOT_ENABLED"),
    ("exit_owner_active", "EXIT_IN_PROGRESS"), ("paused_trading", "TRADING_PAUSED")])
def test_each_admission_rule_refuses_with_its_own_code(world, setup, code):
    changes = _setup(world, setup)
    principal = "cli" if setup == "principal_cli" else "ai_supervisor"
    receipt = world.submit(principal=principal, **changes)
    assert (receipt.state, receipt.error_code) == ("REJECTED", code)
    assert world.decisions.row(decision_id_of(receipt)).error_code == code
    assert world.ledger.get(receipt.command_id).state == "REJECTED"
    assert world.dispatch.plans == []


def test_an_invalid_body_is_still_recorded(world):
    body = {**world.body(), "conid": True}
    receipt = world.coordinator.execute(world.request(body, command_id="aip-dec-00000009", target_id="?"))
    row = world.decisions.row_by_command("aip-dec-00000009")
    assert (receipt.error_code, row.error_code, row.conid) == ("DECISION_INVALID", "DECISION_INVALID", None)
    assert '"conid": true' in row.body_json


def test_a_command_id_that_is_not_derived_from_the_decision_is_invalid(world):
    receipt = world.coordinator.execute(world.request(world.body(), command_id="aip-other-0001"))
    assert receipt.error_code == "DECISION_INVALID"


def test_a_queued_looser_field_is_not_in_force_at_admission(world):
    world.broker.set(positions=(pos(OTHER, 300.0, 30_000.0),))                # 3% gross held
    world.start_session()                                                       # revision 1 (gross 6%) in force
    world.policy.publish(replace(PAPER_LIMITS, gross_fraction=0.06), reason="r", principal="ai_supervisor",
                         command_id="pol-x", broker=world.broker.capture(ACCOUNT))
    restarted = world._policy(ceiling=replace(PAPER_LIMITS, gross_fraction=0.10))
    restarted.publish(replace(PAPER_LIMITS, gross_fraction=0.08), reason="r", principal="ai_supervisor",
                      command_id="pol-y", broker=world.broker.capture(ACCOUNT))   # revision 3, queued
    receipt = world.submit(policy_revision=3, quantity=400)                      # fits 8%, not 6%
    assert receipt.error_code == "QUANTITY_ABOVE_MAXIMUM"


def _fault(world, fault):
    if fault == "wrong_account_snapshot":
        world.broker.set(account="DU999")
    elif fault == "stale_quote":
        world.quotes.age = 6.0
    elif fault == "no_what_if":
        world.margin.response = None
    elif fault == "invalid_what_if":
        world.margin.response = {"initMarginAfter": float("nan"), "equityWithLoanAfter": 1.0}
    elif fault == "leverage":
        world.risk_gate.approved = False
    elif fault == "after_cutoff":
        world.clock.now = AFTER_CUTOFF
    elif fault == "pending_entries":
        world.broker.add_working_entries(other_conids=3)
    elif fault == "expired_at_dispatch":
        world.evidence.after = lambda: world.clock.advance(seconds=2)
        return {"expires_at": (world.clock() + dt.timedelta(seconds=1)).isoformat()}
    elif fault == "denylisted_at_dispatch":
        world.on_before_guard(lambda: world.filter_file.write(denylist=["AAPL"]))
    return {"stop_above_price": {"stop_price": 101.0}, "over_notional": {"quantity": 700},
            "quantity_over_max": {"quantity": 600}}.get(fault, {})


@pytest.mark.parametrize("fault,code", [                                       # coordinator checks reached from a decision
    ("wrong_account_snapshot", "ACCOUNT_MISMATCH"), ("stale_quote", "QUOTE_STALE"),
    ("no_what_if", "MARGIN_UNAVAILABLE"), ("leverage", "LEVERAGE_REJECTED"),
    ("after_cutoff", "ENTRY_CUTOFF"), ("stop_above_price", "STOP_INVALID"),
    ("pending_entries", "MAX_PENDING_ENTRIES"), ("over_notional", "ORDER_EXCEEDS_ATTESTED_NOTIONAL"),
    ("quantity_over_max", "QUANTITY_ABOVE_MAXIMUM"), ("invalid_what_if", "MARGIN_INVALID"),
    ("expired_at_dispatch", "DECISION_EXPIRED"), ("denylisted_at_dispatch", "TRADING_FILTER_DENIED")])
def test_existing_checks_refuse_with_their_own_code(world, fault, code):
    changes = _fault(world, fault)
    receipt = world.submit(**changes)
    assert (receipt.state, receipt.error_code) == ("REJECTED", code)
    assert world.decisions.row("dec-00000001").error_code == code
    assert world.dispatch.plans == []


def test_denylisted_entry_is_refused(world):                                  # owner answer 4
    world.filter_file.write(denylist=["AAPL"])                                  # CONID resolves to AAPL / NASDAQ / STK
    receipt = world.submit()
    assert (receipt.state, receipt.error_code, world.dispatch.plans) == ("REJECTED", "TRADING_FILTER_DENIED", [])


def test_session_anchor_is_durable_before_admission(world):                   # R7, owner answer 1
    world.evidence.fail_with("EVIDENCE_UNAVAILABLE")                            # admission refuses after step 7
    assert world.submit().error_code == "EVIDENCE_UNAVAILABLE"
    assert world.policy_restarted().current().anchor == 1_000_000.0            # row survived the refused decision


def test_daily_loss_refusal_latches_the_session(world):
    world.broker.set(net_liquidation=995_000.0, daily_pnl=-5_000.0)            # anchor 1,000,000: budget 5,000
    assert world.submit().error_code == "DAILY_LOSS"
    world.broker.set(daily_pnl=0.0)
    assert world.submit(decision_id="dec-00000002").error_code == "RISK_LATCHED"
    assert world.policy.current().latch_code == "DAILY_LOSS"


def test_policy_tightened_between_approval_and_dispatch_refuses(world):        # spec 6, ai_paper path
    world.start_session()
    world.on_before_guard(lambda: world.policy_publish(replace(PAPER_LIMITS, position_fraction=0.01)))
    assert world.submit().error_code == "LIMIT_TIGHTENED_BEFORE_DISPATCH"


def test_loss_breached_after_approval_refuses_with_unchanged_policy(world):    # review #31
    world.start_session()
    world.on_before_guard(lambda: world.broker.set(daily_pnl=-6_000.0))        # anchor 1,000,000: budget 5,000
    receipt = world.submit()
    assert (receipt.error_code, world.dispatch.plans) == ("DAILY_LOSS", [])


def test_attribution_links_for_the_entry_order_ref(world):
    world.submit()
    (link,) = world.decisions.links_for_order_ref("mmr:og-aip-dec-00000001")
    assert (link.decision_id, link.decider, link.strategy_version, link.policy_revision, link.style, link.digest) == (
        "dec-00000001", "jev", GOOD["strategy_digest"], 1, "intraday_long", world.digest)
    assert (link.action, link.strategy_digest_provenance, link.effective_revision) == (
        "ENTER", "CLAIMED_NOT_VERIFIED", 1)


def test_no_links_for_foreign_order_refs(world):
    world.submit()
    assert world.decisions.links_for_order_ref("mmr:og-other") == ()
    assert world.decisions.links_for_order_ref("not-ours") == ()


# --- SP1 Plan 6 (found by the acceptance harness): an ENTER must not stay SUBMITTED for ever.
# A SUBMITTED command keeps reconciliation_safe() false, so experiment stop refused
# RECONCILIATION_INCOMPLETE and the kill flatten was never proven FLAT after any AI entry.

def _entry_reconciler(world, found, *, complete=True, closes=None):
    from types import SimpleNamespace
    from trader.trading.command_coordinator import OutcomeReconciler
    refs, alerts = [], []

    def find_by_order_ref(account_id, order_ref):
        refs.append((account_id, order_ref))
        return list(found)
    orders = SimpleNamespace(find_by_order_ref=find_by_order_ref, enumeration_complete=lambda: complete)
    reconciler = OutcomeReconciler(
        journal=world.journal, ledger=world.ledger, orders=orders, strategy=SimpleNamespace(),
        alerts=SimpleNamespace(raise_alert=lambda *a: alerts.append(a)), now=world.clock, closes=closes)
    return reconciler, refs


ENTRY_GROUP = "og-aip-dec-00000001"


def _bracket_row(leg, status, *, filled=0.0):
    return order(leg=leg, status=status, filled=filled, group=ENTRY_GROUP)


def test_a_submitted_entry_is_scheduled_and_resolved_once_the_broker_shows_its_entry_order(world):
    receipt = world.submit()
    assert receipt.state == "SUBMITTED" and world.scheduled == [receipt.command_id]
    found = [_bracket_row("entry", "Submitted"), _bracket_row("stop", "PreSubmitted")]
    reconciler, refs = _entry_reconciler(world, found)
    assert reconciler.reconcile_once(receipt.command_id, NOW).resolved
    row = world.ledger.get(receipt.command_id)
    assert row.state == "RESOLVED" and row.outcome["order_group_id"] == ENTRY_GROUP
    assert row.outcome["broker_acknowledged"] is True and row.outcome["quantity"] == 499
    assert row.outcome["broker_statuses"] == ["Submitted", "PreSubmitted"]     # same fields as the automated path
    assert refs == [(ACCOUNT, f"mmr:{ENTRY_GROUP}")]
    assert world.ledger.unresolved_for_account(ACCOUNT) == []


def test_a_filled_entry_is_resolved(world):
    receipt = world.submit()
    found = [_bracket_row("entry", "Filled", filled=499.0), _bracket_row("stop", "Submitted")]
    reconciler, _ = _entry_reconciler(world, found)
    assert reconciler.reconcile_once(receipt.command_id, NOW).resolved
    assert world.ledger.get(receipt.command_id).state == "RESOLVED"


# Issue #70: the broker shows the group, but not an accepted entry order.

def test_a_rejected_entry_with_a_working_stop_leg_is_not_resolved(world):
    receipt = world.submit()
    found = [_bracket_row("entry", "Inactive"), _bracket_row("stop", "Submitted")]
    reconciler, _ = _entry_reconciler(world, found)
    assert not reconciler.reconcile_once(receipt.command_id, NOW).resolved
    assert world.ledger.get(receipt.command_id).state == "SUBMITTED"


def test_protective_legs_alone_never_resolve_the_entry(world):
    receipt = world.submit()
    found = [_bracket_row("stop", "Submitted"), _bracket_row("take_profit", "PreSubmitted")]
    reconciler, _ = _entry_reconciler(world, found)
    assert not reconciler.reconcile_once(receipt.command_id, NOW).resolved
    assert world.ledger.get(receipt.command_id).state == "SUBMITTED"


@pytest.mark.parametrize("status", ["Inactive", "Cancelled", "ApiCancelled"])
def test_an_entry_the_broker_rejected_or_cancelled_with_no_fill_is_rejected(world, status):
    receipt = world.submit()
    found = [_bracket_row("entry", status), _bracket_row("stop", "Cancelled")]
    reconciler, _ = _entry_reconciler(world, found)
    assert reconciler.reconcile_once(receipt.command_id, NOW).resolved
    row = world.ledger.get(receipt.command_id)
    assert (row.state, row.error_code) == ("REJECTED", "BROKER_REJECTED")
    assert row.outcome["broker_acknowledged"] is False and row.outcome["quantity"] == 499
    assert world.ledger.unresolved_for_account(ACCOUNT) == []


def test_a_rejected_entry_needs_a_complete_enumeration(world):
    receipt = world.submit()
    found = [_bracket_row("entry", "Inactive"), _bracket_row("stop", "Cancelled")]
    reconciler, _ = _entry_reconciler(world, found, complete=False)
    assert not reconciler.reconcile_once(receipt.command_id, NOW).resolved
    assert world.ledger.get(receipt.command_id).state == "SUBMITTED"


def test_an_entry_the_broker_has_not_shown_stays_submitted(world):
    receipt = world.submit()
    reconciler, _ = _entry_reconciler(world, [])
    assert not reconciler.reconcile_once(receipt.command_id, NOW).resolved
    assert world.ledger.get(receipt.command_id).state == "SUBMITTED"         # never rejected by absence alone


# PR #126 review: a local echo is not acceptance (liquidation_service treats it as UNKNOWN).

@pytest.mark.parametrize("status", ["PendingSubmit", "ApiPending", "PendingCancel"])
def test_an_ambiguous_entry_status_with_a_stop_row_stays_submitted(world, status):
    receipt = world.submit()
    found = [_bracket_row("entry", status), _bracket_row("stop", "PreSubmitted")]
    reconciler, _ = _entry_reconciler(world, found)
    assert not reconciler.reconcile_once(receipt.command_id, NOW).resolved
    assert world.ledger.get(receipt.command_id).state == "SUBMITTED"

