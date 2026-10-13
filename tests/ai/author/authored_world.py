"""SP3 Plan 5 Task 12: the whole authored-strategy loop in one world, over signed RPC.

Real: SP2c's research world (SP1's served trader with BrokerSim, the research service on its own signed sockets,
the ai controller, the strategy runtime), Plan 3's authored store and gates, Plan 4's ``AuthoredEvaluator`` and live
proxy, Plan 2's sandbox supervisor and server on real Unix sockets, and this plan's author loop and research offers.
Fakes: the broker, the model providers (scripted, behind the real adapter), the market data, and the sandbox's
spawner (Plan 2's in-process test mode: the child runtime loads the file in this process, statically checked first).

Mappings from the plan text to the code as built (adapted here, plus the research world's seams):
- Pre-flight C5: SP2c's world evaluates with plain ``evaluate_cohort``; production evaluates through
  ``AuthoredEvaluator`` (``research_service.py``). ``research_evaluate`` returns the production evaluator, so an
  authored key really reaches the research sandbox (STARTED, the smoke stage, the cohort).
- Pre-flight C6: ``StrategyRuntime`` builds ``_authored_deployments`` only in ``connect()``, which the test node never
  calls, so patching ``LIVE_SOCKET`` would do nothing. ``strategy_options`` hands the node an ``AuthoredDeployments``
  on the live socket instead, with its own strategy query client at the proof timeout (as ``connect`` builds it)
  and the world's clock (the live proxy skips a bar dated after ``now()`` in New York).
- Pre-flight C7: ``StrategyNode.feed_bar`` filters on ``conids`` and does not stamp ``_dispatch_conid``; an authored
  instance's conids are ``bound_conids`` and it decides nothing without a dispatch conid. ``AuthoredStrategyNode``
  dispatches it through the runtime's own ``_call_on_prices`` (which stamps the conid). It runs inline on the test
  thread: there is no running loop, so this acceptance does not exercise Plan 4's per-instance worker thread.
- Pre-flight C8: ``AuthoredStrategyStore`` copies its config at build, so ``set_switch`` replaces the store's
  ``_config`` (Plan 3's own seam) as well as ``trader.ai_paper_config``.
- ``AuthorDecisionNode`` is ``DecisionNode.__init__`` with the author ai.yaml and a third gateway client:
  ``DecisionNode`` has no seam for either.
- The research world builds its strategy node from ``STRATEGY_NODE_CLASS`` (a seam beyond the brief's three).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from tests.ai.author.helpers import author_text
from tests.ai.decisions.fakes import ScriptedProvider
from tests.ai.fakes import write_config
from tests.ai.research.research_world import NEW_YORK, ResearchDecisionNode, ResearchStrategyNode, ResearchWorld
from tests.research.evaluation_fixtures import holdout_ruleset
from tests.sandbox.fakes import InProcessSpawner
from trader.ai.budget_cap import BudgetCapSync, CapGatedGateway
from trader.ai.config import load_ai_config
from trader.ai.controller import AiController
from trader.ai.gateway import ModelGateway
from trader.ai.replay import ReplayRecorder
from trader.ai.rpc_clients import (AUTHOR_COMMANDS, AUTHOR_QUERIES, LAB_COMMANDS, LAB_QUERIES, AiRpcClients,
                                   PrincipalClient, ReadOnlySupervisor)
from trader.ai.schedule import SessionSlots
from trader.ai.signal_intake import SignalIntake
from trader.ai_service import EngineDeps, build_author_loop, build_engine, build_research_cycle, build_session_slots
from trader.authored.sandbox_client import SandboxClient
from trader.data.duckdb_store import DuckDBDataStore
from trader.research.authored_evaluation import AuthoredEvaluator
from trader.research.evaluation import EvaluationPaths
from trader.sandbox.server import serve
from trader.sandbox.supervisor import LIVE_POOL_SIZE, RESEARCH_POOL_SIZE, Supervisor
from trader.strategy.authored_runtime import PROOF_TIMEOUT_SECONDS, AuthoredDeployments, AuthoredLiveStrategy

SANDBOX_TEST_IMAGE = "mmr-build-test"            # a build id (Plan 3 ruling 22)
FRIDAY = dt.date(2026, 7, 17)
MONDAY = dt.date(2026, 7, 20)
TUESDAY = dt.date(2026, 7, 21)
AUTHORING_BLOCK = "authoring:\n  run_after_close_minutes: 0\n"     # due from the close: Friday 16:31 is inside it
AUTHOR_MODEL = "vendor/author-1"
BAR_STEP = 1.0006                                # each rising bar closes 0.06 % above the one before


class AuthorDecisionNode(ResearchDecisionNode):
    """``DecisionNode.__init__`` with an author role in ai.yaml and its scripted provider as a third client."""

    def __init__(self, world, tmp_path, block: str, *, clock=None, node=None):
        self.world, self.tmp_path, self.block = world, tmp_path, block
        self.node = node or world.node(clock=clock)
        config_dir = tmp_path / "ai-config"
        config_dir.mkdir(exist_ok=True)
        text = author_text(research=block, authoring=AUTHORING_BLOCK)
        self.config = load_ai_config(str(write_config(config_dir, text)))
        self.orchestrator, self.jev = ScriptedProvider("vendor/orch-1"), ScriptedProvider("vendor/jev-1")
        self.author = ScriptedProvider(AUTHOR_MODEL)
        self.gateway = ModelGateway(config=self.config, store=self.node.store, clock=self.node.clock,
                                    clients={"orchestrator": self.orchestrator.adapter("vendor/orch-1"),
                                             "jev": self.jev.adapter("vendor/jev-1"),
                                             "author": self.author.adapter(AUTHOR_MODEL)})
        self.cap_sync = BudgetCapSync(supervisor=self.node.clients.supervisor, budget=self.gateway.budget,
                                      clock=self.node.clock)
        self.gated = CapGatedGateway(self.gateway, self.cap_sync)
        self.engine = build_engine(EngineDeps(self.config, self.gated, ReadOnlySupervisor(self.node.clients.supervisor),
                                              self.node.clock, ReplayRecorder(self.node.store), self.node.store))
        self.controller = AiController(
            config=self.config.controller, store=self.node.store, clock=self.node.clock,
            supervisor=self.node.clients.supervisor, leadership=self.node.leadership, watch=self.node.watch,
            submitter=self.node.submitter, outbox=self.node.outbox,
            intake=SignalIntake(store=self.node.store, supervisor=self.node.clients.supervisor, clock=self.node.clock),
            slots=SessionSlots(), engine=self.engine, gateway=self.gated, cap_sync=self.cap_sync)


class AuthoredStrategyNode(ResearchStrategyNode):
    """``feed_bar`` dispatches an authored instance on its ``bound_conids`` through the runtime's ``_call_on_prices``
    (it stamps the dispatch conid), inline; every other AI instance as ``StrategyNode`` does."""

    def feed_bar(self, conid: int, frame: pd.DataFrame) -> Optional[str]:
        before = len(self._signals())
        for instance in self.runtime.ai_instances().values():
            if isinstance(instance, AuthoredLiveStrategy):
                if conid not in instance.bound_conids:
                    continue
                signal = self.runtime._call_on_prices(instance, conid, frame)
            elif conid in instance.conids:
                signal = instance.on_prices(frame)
            else:
                continue
            if signal is not None:
                self.runtime._dispatch_once(instance, signal, conid, frame)
        recorded = self._signals()[before:]
        if not recorded:
            return None
        (signal,) = recorded
        return signal.entry.source_event_id


class AuthoredWorld(ResearchWorld):
    NODE_CLASS = AuthorDecisionNode
    STRATEGY_NODE_CLASS = AuthoredStrategyNode

    @classmethod
    async def build(cls, tmp_path: Path, loop_thread: Any, monkeypatch: Any) -> "AuthoredWorld":
        return await super().build(tmp_path, loop_thread, monkeypatch)

    async def _build(self, tmp_path, loop_thread, monkeypatch, *args) -> None:
        self._start_sandbox(tmp_path, loop_thread, monkeypatch)
        await super()._build(tmp_path, loop_thread, monkeypatch, *args)
        self._wire_author()

    def _start_sandbox(self, tmp_path: Path, loop_thread: Any, monkeypatch: Any) -> None:
        """Plan 3's store directory, and Plan 2's sandbox in process on its two sockets."""
        self.store_dir = tmp_path / "store"
        self.store_dir.mkdir()
        monkeypatch.setenv("MMR_AUTHORED_STORE_DIR", str(self.store_dir))
        self.spawners = {"live": InProcessSpawner(), "research": InProcessSpawner()}
        supervisor = Supervisor(spawners=self.spawners,
                                pool_sizes={"live": LIVE_POOL_SIZE, "research": RESEARCH_POOL_SIZE},
                                store_dir=str(self.store_dir), image_digest=SANDBOX_TEST_IMAGE, posture={},
                                wiper=lambda slot: True)       # the real wiper forks a helper against /tmp/s2000X
        sockets_dir = tempfile.mkdtemp(prefix="sbx", dir="/tmp")    # macOS caps a socket path at 104 bytes
        self._closers.append(lambda: shutil.rmtree(sockets_dir, ignore_errors=True))
        self.live_socket = os.path.join(sockets_dir, "live.sock")
        self.research_socket = os.path.join(sockets_dir, "research.sock")
        server = loop_thread.run(serve(supervisor, {"live": self.live_socket, "research": self.research_socket},
                                       socket_gid=os.getgid()))
        self._closers.append(lambda: loop_thread.run(server.close()))

    def _wire_author(self) -> None:
        """The ai_author client, the research cycle rebuilt on it (so it offers stored authored files) and the
        author loop, all as ``ai_service`` wires them."""
        served, ai = self.world.served, self.node.node
        self.author_client = PrincipalClient(
            "ai_author", command=served.sockets.client("ai_author", "trader", "command", timeout=30.0),
            query=served.sockets.client("ai_author", "trader", "query", timeout=30.0),
            commands=AUTHOR_COMMANDS, queries=AUTHOR_QUERIES, timeout=30.0)
        lab = PrincipalClient("ai_research", command=self.lab_socket("command"), query=self.lab_socket("query"),
                              commands=LAB_COMMANDS, queries=LAB_QUERIES, timeout=30.0,
                              unreachable_code="RESEARCH_UNREACHABLE")
        clients = AiRpcClients(ai.clients.supervisor, ai.clients.research, lab, self.author_client)
        slots = build_session_slots(self.node.config)
        self.cycle = build_research_cycle(self.node.config, store=ai.store, clock=ai.clock, slots=slots,
                                          leadership=ai.leadership, watch=ai.watch, clients=clients,
                                          gateway=self.node.gated, strategies_root=self.repo)
        self.author = build_author_loop(self.node.config, store=ai.store, clock=ai.clock, slots=slots,
                                        leadership=ai.leadership, watch=ai.watch, clients=clients,
                                        gateway=self.node.gated)

    # -- the research world's seams ---------------------------------------------------------------------------
    def extra_trader_prepare(self, trader: Any) -> None:
        self._trader = trader
        authored = dataclasses.replace(trader.ai_paper_config.authored_strategies, enabled=False, max_active=3,
                                       bar_sizes=("15 mins",))
        trader.ai_paper_config = dataclasses.replace(trader.ai_paper_config, authored_strategies=authored)

    def evaluation_path_overrides(self) -> dict:
        return {"sandbox_socket": self.research_socket}

    def research_evaluate(self, paths: EvaluationPaths, now: Any) -> Any:
        return AuthoredEvaluator(research_db=self.research_db, paths=paths, now=now, trader=self.trader_port,
                                 ruleset=holdout_ruleset()).evaluate

    def strategy_options(self) -> dict:
        served = self.world.served
        proof_query = served.sockets.client("strategy", "trader", "query", timeout=PROOF_TIMEOUT_SECONDS)
        return {"authored_deployments": AuthoredDeployments(
            client=SandboxClient(self.live_socket), trader_query=proof_query,
            trader_command=served.client("strategy", "command"), now=served.now)}

    # -- steps ----------------------------------------------------------------------------------------------------
    async def night(self, author: bool = True) -> None:
        """The author slot (when ``author``), then SP2c's research night."""
        await self.node.cap_sync.sync()
        await self._hold_the_lease()
        await self.node.controller.refresh_experiment()
        if author:
            await self.author.run_due_slot()
        await super().night()

    def set_switch(self, enabled: bool) -> None:
        """``ai_paper.authored_strategies.enabled``. The store reads its own copy taken at build (pre-flight C8), so
        that copy is replaced too."""
        authored = dataclasses.replace(self._trader.ai_paper_config.authored_strategies, enabled=enabled)
        self._trader.ai_paper_config = dataclasses.replace(self._trader.ai_paper_config, authored_strategies=authored)
        self.world.served.stack.ai_paper.authored._config = authored

    def advance(self, seconds: float) -> None:
        self.world.advance(seconds)

    def record_isolation_proof(self) -> dict:
        """What ``mmr ai-author prove-isolation`` records for the running research image."""
        image_digest = SandboxClient(self.research_socket).ping().image_digest
        return self.trader_call("cli", "record_isolation_proof", {
            "image_digest": image_digest, "passed": True,
            "checks": [{"name": "in_process_test_mode", "passed": True, "detail": "SP3 Plan 5 acceptance"}],
            "ran_at": self.world.served.now().isoformat()})

    def morning_at(self, day: dt.date, hour: int, minute: int) -> None:
        at = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=NEW_YORK).astimezone(dt.timezone.utc)
        self._refresh_daily_bars(at)
        self.world.served.run_session(at)

    def rising_bars(self, conid: int, day: dt.date, until: tuple[int, int]) -> pd.DataFrame:
        """The fixture history, then ``day``'s 15-minute bars from 09:30 to ``until``, each 0.06 % above the one
        before, so the ORB port's 10:00 close beats the 09:30 and 09:45 highs."""
        history = DuckDBDataStore(self.bars).read(str(conid))
        index = pd.date_range(pd.Timestamp(dt.datetime.combine(day, dt.time(9, 30)), tz=NEW_YORK),
                              pd.Timestamp(dt.datetime.combine(day, dt.time(*until)), tz=NEW_YORK),
                              freq="15min").tz_convert("UTC")
        last = history.iloc[-1]
        prices = [float(last["close"]) * BAR_STEP ** (step + 1) for step in range(len(index))]
        today = pd.DataFrame({column: [last[column]] * len(index) for column in history.columns}, index=index)
        today["open"] = today["close"] = prices
        today["high"] = [price * 1.0005 for price in prices]
        today["low"] = [price * 0.9995 for price in prices]
        frame = pd.concat([history, today])
        frame.index.name = "date"
        return frame

    # -- reads ----------------------------------------------------------------------------------------------------
    def stored_file(self, digest: str) -> bytes:
        return (self.store_dir / f"{digest.split(':', 1)[1]}.py").read_bytes()

    def authored_state(self, digest: str) -> str:
        records = self.trader_call("cli", "list_authored_strategies", {})["records"]
        (state,) = [record["state"] for record in records if record["digest"] == digest]
        return state

    def sandbox_sessions(self, pool: str) -> int:
        return self.spawners[pool].spawned

    def scoreboard(self) -> dict:
        self.world.served.stack.scoreboard.service.refresh()
        return self.trader_call("cli", "get_scoreboard", {})
