"""The Plan 5 runtime against SP1's real served trader: real coordinator, risk gates, ownership, saga and
reconciler over signed typed RPC. Only the broker is simulated (BrokerSim)."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

from tests.ai.fakes import config_text
from tests.sp1_acceptance.judged import judged_served_stack
from tests.sp1_acceptance.test_acceptance_run import entries_placed, market, settings as acceptance_settings
from tests.sp1_fixtures import MSFT, OTHER
from trader.ai.controller import ExperimentWatch
from trader.ai.engine import ProposedDecision
from trader.ai.journal import AttemptJournal
from trader.ai.leadership import Leadership, new_holder_id
from trader.ai.outbox import ReportingOutbox
from trader.ai.rpc_clients import AiRpcClients
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.schedule import SessionSlots
from trader.ai.store import AiStore
from trader.ai.submitter import Submitter
from trader.automation.ai_policy_file import load_policy_file
from trader.automation.risk_limits import PAPER_LIMITS


class TraderClock:
    """The ai side reads the served trader's clock, so both sides agree on time.

    ``frozen_monotonic`` plays a paused process: its own lease deadline never passes.
    ``real_sleep`` is for a whole service in the test loop (its loops must not move trader time)."""

    def __init__(self, served, *, frozen_monotonic=False, real_sleep=False):
        self._served, self._origin = served, served.now()
        self._frozen, self._real_sleep = frozen_monotonic, real_sleep

    def now(self):
        return self._served.now()

    def monotonic(self):
        return 0.0 if self._frozen else (self._served.now() - self._origin).total_seconds()

    async def sleep(self, seconds):
        if self._real_sleep:
            await asyncio.sleep(seconds)
        else:
            self._served.advance(seconds)
            await asyncio.sleep(0)


class NoResearchServer:
    """The SP2 runtime tests never call the research server: any call is refused before the send."""

    def call(self, method, body, response_model, timeout=None, **options):
        raise ConnectionError(f"typed RPC call to {method!r} could not be sent: no research server in this world")

    def close(self):
        pass


class FlakyClient:
    """Wraps a real TypedRpcClient. "down": refused before the send (ConnectionError, like IMMEDIATE=1);
    "lose_reply": the trader handles the request, then the reply is lost (TimeoutError)."""

    def __init__(self, inner):
        self.inner, self.steps = inner, {}

    def script(self, method, *steps):
        self.steps.setdefault(method, []).extend(steps)

    def call(self, method, body, response_model, timeout=None, **options):
        queue = self.steps.get(method) or []
        step = queue.pop(0) if queue else None
        if step == "down":
            raise ConnectionError(f"typed RPC call to {method!r} could not be sent: no route to server")
        reply = self.inner.call(method, body, response_model, timeout, **options)
        if step == "lose_reply":
            raise TimeoutError(f"typed RPC call to {method!r} timed out")
        return reply

    def close(self):
        self.inner.close()


class TraderWorld:
    """Served SP1 trader, ARMED experiment, operator policy (cli) and an ACTIVE judged deployment version."""

    def __init__(self, tmp_path, loop_thread, monkeypatch, *, identities=None, model_budget_usd_per_day=None,
                 prepare=None, policy_file=None):
        options = {"prepare": _with_daily_bars(prepare)}
        if identities is not None:
            options["identities"] = identities
        if model_budget_usd_per_day is not None:            # the trader.yaml ai_paper value the trader starts with
            options["model_budget_usd_per_day"] = model_budget_usd_per_day
        self.served = judged_served_stack(tmp_path, loop_thread, monkeypatch, **options)
        self.digest, self.version = self.served.deployment_digest, self.served.deployment_version
        self.source_digest = self.served.source_digest
        market(self.served)
        chosen = acceptance_settings()
        self.conid, self.quantity = chosen.conid_s, chosen.quantity_s
        limits = PAPER_LIMITS.to_json() if policy_file is None else load_policy_file(policy_file)
        published = self.served.call("cli", "publish_ai_risk_policy", {
            "command_id": "cli-pol-000000000001", "limits": limits,
            "reason": "operator initial policy"})
        assert published["state"] == "RESOLVED", published
        self.policy_revision = published["outcome"]["revision"]
        (tmp_path / "ai").mkdir(exist_ok=True)
        self.ai_db = tmp_path / "ai" / "ai.duckdb"

    def enter(self, action_key=None) -> ProposedDecision:
        ask = self.served.sim.quotes[self.conid][1]
        return ProposedDecision(action_key=action_key or f"enter:{self.conid}", action="ENTER", conid=self.conid,
                                side="BUY", decider="jev", evidence_digest="sha256:" + "e" * 64,
                                deployment_digest=self.digest, deployment_version=self.version,
                                source_digest=self.source_digest, policy_revision=self.policy_revision,
                                stop_price=round(ask * 0.98, 2), target_price=round(ask * 1.02, 2),
                                quantity=self.quantity)

    def node(self, *, clock=None, flaky=False) -> "AiNode":
        return AiNode(self, clock or TraderClock(self.served), flaky)

    def advance(self, seconds):
        self.served.advance_and_promote(seconds)        # time passes, the broker moves, the trader catches up

    def settle(self, max_steps=30):
        """Let the broker fill and the saga protect, one promoted second at a time, until protected."""
        for _ in range(max_steps):
            self.served.advance_and_promote(1.0)
            if self.entries() and self.protected():
                return
        raise AssertionError(f"not protected after {max_steps} steps; entries: {self.entries()}")

    def entries(self):
        return entries_placed(self.served)

    def protected(self) -> bool:
        rows = self.served.call("ai_supervisor", "get_broker_order_evidence", {"conid": self.conid})["orders"]
        return {"stop", "take_profit"} <= {r["leg"] for r in rows if r["status"] in ("Submitted", "PreSubmitted")}

    def receipt(self, decision_id):
        return self.served.stack.coordinator.get_command(f"aip-{decision_id}")

    def decision_row(self, decision_id):
        return self.served.stack.ai_paper.decision_store.row(decision_id)

    def strategy_signal(self, action="BUY", conid=None) -> str:
        """The strategy service's side: a bound signal of the judged instance into the durable record."""
        from trader.data.duckdb_store import DuckDBConnection
        from trader.data.strategy_signal_record import SignalEntry, StrategySignalRecord
        entry = SignalEntry.create(strategy_name="orb", conid=conid or self.conid, action=action, probability=0.7,
                                   signal_time=self.served.now(), deployment_digest=self.digest,
                                   deployment_version=self.version, source_digest=self.source_digest)
        StrategySignalRecord(DuckDBConnection.get_instance(self.served.stack.ai_paper.signals_path),
                             now=self.served.now).append(entry)
        return entry.source_event_id

    def hold_other_position(self, value_share_of_equity: float, conid: int = OTHER) -> float:
        """A filled position the experiment does not own, worth this share of net liquidation (marked at 100)."""
        quantity = value_share_of_equity * self.served.sim.net_liquidation / 100.0
        self.served.sim.held[conid] = quantity
        self.served.sim.promote()
        return quantity

    def close(self):
        self.served.close()


def _with_daily_bars(prepare):
    """Twenty closed daily bars for MSFT too (the scope rule and the baseline sizer read them), then ``prepare``."""
    def run(trader):
        from tests.automation.ai_paper_fixtures import daily_frame
        from trader.objects import BarSize
        trader.data.get_tickdata(BarSize.Days1).write(MSFT, daily_frame())
        if prepare is not None:
            prepare(trader)
    return run


class AiNode:
    """One ai process's runtime parts on the shared ai.duckdb; tests drive them step by step."""

    def __init__(self, world: TraderWorld, clock, flaky: bool):
        self.world, self.clock = world, clock
        self.store = AiStore(world.ai_db, clock=clock)
        self.store.migrate(ALL_MIGRATIONS)
        sockets = world.served.sockets
        raw = {"supervisor_command": sockets.client("ai_supervisor", "trader", "command", timeout=30.0),
               "supervisor_query": sockets.client("ai_supervisor", "trader", "query", timeout=30.0),
               "supervisor_discovery": sockets.client("ai_supervisor", "trader", "query", timeout=30.0),
               "research_command": sockets.client("ai_research", "trader", "command", timeout=30.0),
               "research_query": sockets.client("ai_research", "trader", "query", timeout=30.0)}
        raw.update(lab_command=NoResearchServer(), lab_query=NoResearchServer())   # Task 8 binds a real one
        self.sockets = {name: FlakyClient(client) for name, client in raw.items()} if flaky else raw
        self.clients = AiRpcClients.from_sockets(**self.sockets, timeout=30.0)
        self.leadership = Leadership(supervisor=self.clients.supervisor, store=self.store, clock=clock,
                                     holder_id=new_holder_id())
        self.clients.supervisor.bind_epoch(self.leadership.current_epoch)
        self.watch = ExperimentWatch(self.clients.supervisor)
        self.submitter = Submitter(store=self.store, supervisor=self.clients.supervisor, leadership=self.leadership,
                                   clock=clock, slots=SessionSlots(), experiment_state=self.watch.state)
        self.outbox = ReportingOutbox(store=self.store, journal=AttemptJournal(self.store),
                                      supervisor=self.clients.supervisor, clock=clock)

    async def plan(self, decision, *, source_id="sig-" + "7" * 32, ttl=300) -> str:
        import datetime as dt
        now = self.clock.now()
        return await self.store.atransaction(lambda conn: self.submitter.insert_in_tx(
            conn, source_kind="entry_signal", source_id=source_id, decision=decision,
            expires_at=now + dt.timedelta(seconds=ttl), epoch=self.leadership.last_epoch, now=now))


def write_service_config(tmp_path: Path, world: TraderWorld, heartbeat: Path) -> Path:
    """ai.yaml for a whole service against the served trader: its ports, fast loops, the shared ai.duckdb."""
    ports = world.served.sockets.ports
    block = (f"database_path: {world.ai_db}\n"
             "controller:\n"
             f"  trader_query_port: {ports[('trader', 'query')]}\n"
             f"  trader_command_port: {ports[('trader', 'command')]}\n"
             "  rpc_timeout_seconds: 30\n  renew_seconds: 5\n  held_retry_seconds: 0.2\n"
             "  signal_poll_seconds: 0.2\n  reconcile_seconds: 0.2\n  outbox_seconds: 0.2\n"
             "  experiment_poll_seconds: 0.2\n  heartbeat_seconds: 0.2\n"
             f"  heartbeat_path: {heartbeat}\n")
    path = tmp_path / "ai.yaml"
    path.write_text(config_text(extra_top_level=block))
    return path


async def wait_for_heartbeat(path: Path, matches: Callable[[dict], bool], task: Any = None,
                             timeout: float = 30.0) -> dict:
    status, deadline = None, time.monotonic() + timeout
    while time.monotonic() < deadline:
        if task is not None and task.done():
            task.result()
            raise AssertionError("the ai service stopped early")
        try:
            status = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            status = None
        if status is not None and matches(status):
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"the heartbeat never matched; last: {status}")
