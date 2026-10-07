"""A real ai_paper stack on a tmp DuckDB for Tasks 7-8: coordinator, ledger, journal, policy,
deployment and decision stores, the real ProtectiveOrderSaga, SessionRiskController and
DispatchGuard. Only the broker, quotes, margin, history and the order dispatch are fakes.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from tests.automation.ai_paper_fixtures import (
    ACCOUNT, CONID, NOW, OTHER, FakeUniverse, make_history, order, pos, quote, secdef, snapshot,
)
from trader.automation.ai_deployments import AiDeployment, AiDeploymentStore, apply_ai_deployment_migration
from trader.automation.ai_paper_config import AiPaperConfig
from trader.automation.ai_paper_decision import (
    AI_PAPER_ACTION, AiPaperDecisionService, AiPaperDecisionStore, apply_ai_paper_decision_migration,
    command_id_for,
)
from trader.automation.ai_paper_evidence import AiPaperEvidence, ai_entry_gate
from trader.automation.ai_paper_experiment import ExperimentView
from trader.automation.ai_paper_filter import AiEntryFilter, MtimeCachedFilterLoader
from trader.automation.ai_risk_policy import AiRiskPolicyService, apply_ai_risk_policy_migration
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.controller_epoch import ControllerEpochs, apply_controller_epoch_migration
from trader.automation.protective_order_saga import ProtectiveOrderSaga, apply_protective_order_saga_migration
from trader.automation.risk_limits import PAPER_LIMITS
from trader.automation.session_risk import SessionRiskController
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.promotion.allocation_policy import AllocationPolicy
from trader.promotion.canary_risk import apply_canary_risk_migration
from trader.trading.command_coordinator import (
    CommandAudit, CommandLedger, CommandRequest, TradingCommandCoordinator, apply_command_ledger_migration,
)
from trader.trading.command_policy import CommandAuthorityPolicy
from trader.trading.dispatch_guard import DispatchGuard
from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration
from trader.trading.liquidation_service import apply_liquidation_migration
from trader.trading.quote_feeds import LIVE_ONLY_FEEDS
from trader.trading.trading_control import TradingControlStore, apply_trading_control_migration

GOOD_MARGIN = {"initMarginAfter": 5_000.0, "equityWithLoanAfter": 995_000.0}
GOOD = {"strategy_path": "strategies/opening_range_breakout.py", "strategy_digest": "sha256:" + "a" * 64,
        "class_name": "OpeningRangeBreakout", "params": {"RANGE_MINUTES": 15}, "conids": [OTHER, CONID],
        "bar_size": "1 min", "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
        "evidence_ref": "trial:42", "evidence_order_notional": 60_000.0}


def enter_body(digest: str, now: dt.datetime, **changes) -> dict:
    body = {"decision_id": "dec-00000001", "deployment_digest": digest, "decider": "jev",
            "action": "ENTER", "conid": CONID, "side": "BUY", "stop_price": 98.0, "target_price": None,
            "quantity": None, "policy_revision": 1, "evidence_digest": "sha256:" + "c" * 64,
            "expires_at": (now + dt.timedelta(minutes=5)).isoformat()}
    body.update(changes)
    return body


class Clock:
    def __init__(self, now=NOW):
        self.now = now
        self._lock = threading.Lock()

    def __call__(self) -> dt.datetime:
        with self._lock:
            return self.now

    def advance(self, **delta) -> None:
        with self._lock:
            self.now = self.now + dt.timedelta(**delta)


class Broker:
    def __init__(self, clock):
        self.clock = clock
        self.snapshot = snapshot()
        self.fail = False
        self.lock = threading.Lock()
        self.held = False          # read by the liquidation test dispatch

    @property
    def last(self) -> int:
        return self.snapshot.generation_id

    def capture(self, account_id):
        if self.fail:
            raise RuntimeError("broker down")
        return self.snapshot

    def set(self, **changes):
        with self.lock:
            fields = {"positions": self.snapshot.positions, "working": self.snapshot.working_orders,
                      "net_liquidation": self.snapshot.net_liquidation, "daily_pnl": self.snapshot.daily_pnl,
                      "account": self.snapshot.account_id, "generation": self.snapshot.generation_id,
                      "cursor": self.snapshot.source_cursor}
            fields.update(changes)
            self.snapshot = snapshot(**fields)

    def show_working_entry(self, group, *, conid=CONID, quantity=500, filled=0.0, limit=100.10):
        working = self.snapshot.working_orders + (
            order(conid=conid, group=group, leg="entry", total=quantity, filled=filled, limit=limit),)
        self.set(working=working, generation=self.snapshot.generation_id + 1)

    def add_working_entries(self, *, other_conids=2):
        working = self.snapshot.working_orders + tuple(
            order(conid=9000 + n, group=f"og-other-{n}", leg="entry", total=1, limit=100.0)
            for n in range(other_conids))
        self.set(working=working, generation=self.snapshot.generation_id + 1)


class Quotes:
    def __init__(self, clock):
        self.clock = clock
        self.age = 0.0
        self.changes: dict = {}

    def set(self, **changes) -> None:
        """ExecutableQuote fields for every later quote; the price follows the ask."""
        self.changes.update(changes)
        if "ask" in changes:
            self.changes["price"] = changes["ask"]

    def executable_quote(self, conid, *, side):
        return replace(quote(conid=conid, age=self.age), side=side,
                       market_timestamp=self.clock() - dt.timedelta(seconds=self.age), **self.changes)


class Margin:
    def __init__(self):
        self.response = dict(GOOD_MARGIN)
        self.error: Optional[BaseException] = None

    def what_if_margin(self, conid, side, quantity):
        if self.error is not None:
            raise self.error
        return self.response


class RiskGate:
    def __init__(self):
        self.approved = True

    def check_leverage(self, margin, net_liquidation):
        return SimpleNamespace(approved=self.approved, reason="leverage above the limit")


class RecordingDispatch:
    def __init__(self):
        self.plans = []
        self.raise_ambiguous = False

    def submit_bracket(self, *, plan, intent, account_id):
        if self.raise_ambiguous:
            raise RuntimeError("connection reset after send")
        self.plans.append(plan)
        return SimpleNamespace(order_ids=[len(self.plans)])


class Experiments:
    def __init__(self):
        self.view: Optional[ExperimentView] = ExperimentView("exp1", "ARMED")

    def current(self, account_id):
        return self.view


class FailingEvidence:
    """Wraps AiPaperEvidence; ``fail_with`` makes the next prepare_entry refuse."""

    def __init__(self, inner):
        self.inner = inner
        self.code: Optional[str] = None
        self.after = None

    def fail_with(self, code):
        self.code = code

    def prepare_entry(self, **kwargs):
        from trader.trading.approval_context import ApprovalContextError
        if self.code is not None:
            raise ApprovalContextError(self.code, "forced by the test")
        prepared = self.inner.prepare_entry(**kwargs)
        if self.after is not None:
            self.after()
        return prepared


class FilterFile:
    def __init__(self, path: Path):
        self.path = path
        self.write()

    def write(self, **rules):
        lines = [f"{key}: {value!r}".replace("'", '"') for key, value in rules.items()]
        self.path.write_text("\n".join(lines) + "\n")
        stat = self.path.stat()
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


class World:
    def __init__(self, tmp_path: Path, *, real_liquidation: bool = False,
                 accepted_feeds: frozenset[str] = LIVE_ONLY_FEEDS):
        self.clock = Clock()
        self.accepted_feeds = accepted_feeds
        self.db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
        migrator = SchemaMigrator(self.db)
        self.journal = DomainJournal(self.db)
        self.journal.migrate(migrator)
        for apply in (apply_command_ledger_migration, apply_trading_control_migration,
                      apply_protective_order_saga_migration, apply_ai_risk_policy_migration,
                      apply_ai_deployment_migration, apply_ai_paper_decision_migration,
                      apply_canary_risk_migration, apply_exit_owner_migration, apply_liquidation_migration,
                      apply_controller_epoch_migration):
            apply(migrator)
        self.epochs = ControllerEpochs(journal=self.journal, now=self.clock)
        self.epoch = self.epochs.grant(holder_id="world", current_epoch=None, lease_seconds=60).epoch
        self.controls = TradingControlStore(self.journal)
        self.db.transaction(lambda conn: self.controls.seed_in_tx(conn, [(ACCOUNT, "paper")], NOW))
        self.ledger = CommandLedger(self.journal)
        self.broker, self.quotes, self.margin = Broker(self.clock), Quotes(self.clock), Margin()
        self.risk_gate, self.dispatch, self.experiments = RiskGate(), RecordingDispatch(), Experiments()
        self.filter_file = FilterFile(tmp_path / "trading_filters.yaml")
        self.filter_loads = 0
        loader = MtimeCachedFilterLoader(str(self.filter_file.path))

        def counted_loader():
            self.filter_loads += 1
            return loader()
        self.entry_filter = AiEntryFilter(
            universe=FakeUniverse({CONID: secdef("AAPL", "NASDAQ"), OTHER: secdef("MSFT", "NASDAQ", conid=OTHER)}),
            load_filter=counted_loader)
        self.exit_owners = ExitOwnerRegistry(self.db)
        self.policy = self._policy()
        self.deployments = AiDeploymentStore(self.db, now=self.clock)
        self.digest, _ = self.deployments.register(AiDeployment.from_json(GOOD), principal="ai_research",
                                                   command_id="dep-1")
        self.policy_publish(PAPER_LIMITS)
        self.evidence = FailingEvidence(AiPaperEvidence(
            broker=self.broker, quotes=self.quotes, margin=self.margin,
            history=make_history(str(tmp_path / "history.duckdb")), journal=self.journal,
            account_id=ACCOUNT, account_mode="paper", now=self.clock, max_drift_bps=50.0,
            entry_offset_bps=Decimal("10"), entry_filter=self.entry_filter, accepted_feeds=accepted_feeds))
        self.guard = DispatchGuard(
            broker=self.broker, quotes=self.quotes, margin=self.margin, controls=self.controls,
            risk_gate=self.risk_gate, policy=CommandAuthorityPolicy(
                enabled=True, live_enabled=False, live_account_id=None, max_order_notional=1e9,
                max_drift_bps=50.0),
            account_id=ACCOUNT, account_mode="paper", allocation_policy=AllocationPolicy(now=self.clock),
            current_limits=lambda request: (self.policy.effective_limits() if request.action == AI_PAPER_ACTION
                                            else PAPER_LIMITS),
            ai_entry_gate=ai_entry_gate(entry_filter=self.entry_filter),
            strict_margin_actions=frozenset({AI_PAPER_ACTION}), accepted_feeds=accepted_feeds)
        self.liquidation = (self._real_liquidation() if real_liquidation
                            else SimpleNamespace(start=lambda *a, **k: None))
        self.saga = ProtectiveOrderSaga(
            journal=self.journal, ledger=self.ledger, dispatch=self.dispatch, dispatch_guard=self.guard,
            session_risk=SessionRiskController(calendar=XNYSCalendarPolicy(), now=self.clock),
            breaker=SimpleNamespace(record=lambda signal: None), liquidation=self.liquidation,
            account_id=ACCOUNT, account_mode="paper", now=self.clock, db=self.db)
        self.scheduled: list[str] = []
        self.decisions = AiPaperDecisionStore(self.journal)
        self.service = AiPaperDecisionService(
            ledger=self.ledger, journal=self.journal, controls=self.controls, policy=self.policy,
            deployments=self.deployments, evidence=self.evidence, saga=self.saga,
            experiments=self.experiments, exit_owners=self.exit_owners, liquidation=self.liquidation,
            broker=self.broker, config=AiPaperConfig(enabled=True), account_id=ACCOUNT, now=self.clock,
            schedule_reconcile=self.scheduled.append, decisions=self.decisions, epochs=self.epochs)

        class _Nonces:
            def consume_in_tx(self, *args, **kwargs):
                return True
        self.coordinator = TradingCommandCoordinator(
            journal=self.journal, ledger=self.ledger, audit=CommandAudit(self.journal), nonces=_Nonces(),
            now=self.clock)
        self.coordinator.register_action(AI_PAPER_ACTION, self.service.execute, requires_preflight=False,
                                         saga=True)

    def _real_liquidation(self):
        from tests.test_liquidation_service import _Breaker, _Dispatch
        from trader.trading.liquidation_service import LiquidationRunStore, LiquidationService
        self.liquidation_dispatch = _Dispatch(self.broker)
        self.liquidation_scheduled: list[str] = []
        return LiquidationService(
            self.broker, self.liquidation_dispatch, store=LiquidationRunStore(self.db), registry=self.exit_owners,
            now=self.clock, breaker=_Breaker(), schedule_reconcile=self.liquidation_scheduled.append)

    def liquidation_runs(self) -> set[str]:
        return {row[0] for row in self.db.execute("SELECT cause_command_id FROM liquidation_runs", fetch="all")}

    def _policy(self, ceiling=PAPER_LIMITS):
        return AiRiskPolicyService(db=self.db, account_id=ACCOUNT, ceiling=ceiling,
                                   calendar=XNYSCalendarPolicy(), now=self.clock)

    def policy_restarted(self):
        return self._policy()

    def policy_publish(self, limits):
        self._policy_commands = getattr(self, "_policy_commands", 0) + 1
        return self.policy.publish(limits, reason="test", principal="ai_supervisor",
                                   command_id=f"pol-{self._policy_commands}", broker=snapshot())

    def start_session(self):
        return self.policy.ensure_session(self.broker.capture(ACCOUNT))

    def on_before_guard(self, callback):
        real = self.guard.revalidate

        def wrapped(*args, **kwargs):
            callback()
            return real(*args, **kwargs)
        self.guard.revalidate = wrapped

    def request(self, body, *, principal="ai_supervisor", command_id=None, target_id=None):
        return CommandRequest(
            command_id=command_id or command_id_for(body["decision_id"]), action=AI_PAPER_ACTION,
            account_id=ACCOUNT, target_type="conid", target_id=target_id or str(body["conid"]),
            expected_version=None, body=body, source=principal, principal=principal,
            controller_epoch=self.epoch)

    def body(self, **changes):
        return enter_body(self.digest, self.clock(), **changes)

    def submit(self, body=None, *, principal="ai_supervisor", **changes):
        body = self.body(**changes) if body is None else body
        return self.coordinator.execute(self.request(body, principal=principal))

    def held(self, conid=CONID, quantity=300.0):
        self.broker.set(positions=self.broker.snapshot.positions + (pos(conid, quantity),))

    def owned(self, conid=CONID, quantity=300.0, decision_id="dec-owned-00"):
        """An ENTER of the current experiment whose saga reports a fill, then its broker position."""
        assert self.submit(decision_id=decision_id, conid=conid).state == "SUBMITTED"
        command_id = command_id_for(decision_id)
        (raw,) = self.db.execute("SELECT payload FROM automated_order_sagas WHERE command_id = ?",
                                 [command_id], fetch="one")
        payload = {**json.loads(raw), "filled_quantity": str(quantity)}
        self.db.execute("UPDATE automated_order_sagas SET payload = ? WHERE command_id = ?",
                        [json.dumps(payload), command_id])
        self.held(conid, quantity)
        self.dispatch.plans.clear()
        self.scheduled.clear()
