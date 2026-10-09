"""SP2c Plan 4 Task 8: the research cycle end to end over signed RPC.

Real: SP1's served trader (coordinator, risk gates, ownership, saga, claims, judgments, registrar) with BrokerSim;
Plan 3's research service (EvaluationService on fixture bars, JudgmentAttest, the research registry) on its own
signed sockets; the ai controller's ResearchCycle, gateway, Jev judge and decision engine; Plan 2's strategy runtime.
Fakes: the broker, the model providers (scripted, behind the real adapter) and the market data.

Mappings from the plan text to the helpers as built (adapted here only):
- ``served_stack(..., start=et(16, 31))``: ``TraderWorld`` has no start option. It is built at its usual 11:00 and
  the trader clock is then set to Friday 16:31, before the ai node or the research service exists.
- ``trader.yaml`` ``ai_paper.backtest_judge.strategy_allowlist``: the served stack builds ``ai_paper_config`` from
  options, not from the file, so the ``prepare`` seam replaces its ``backtest_judge`` and sets the trader's
  ``research_artifacts_root`` / ``research_verify_dir`` (the research signer's key is ``keys/verify/research.pem``).
- Synthetic bars pass only the holdout subset of paper-v1. ``judge_qualified_evidence_by_holdout_ruleset`` makes the
  bundle gate demand it; the case's own deploy gate (``evaluation_case.initial_deploy_allowed``) is set to the same
  subset here. Every other check runs for real.
- ``TraderWorld`` is a judged stack whose judgment reader answers seeded judgments only; the real Plan 1 reader is
  set as its fallback, so the judgments this world records are the ones the registrar reads.
- ``StrategyNode.feed_bar`` returns nothing; ``.strategy.feed_bar`` returns the recorded signal's source_event_id.
- ``ResearchCycle`` is built by ``ai_service.build_research_cycle`` (the production wiring) on the node's store,
  clock, leadership, watch, cap-gated gateway and ``clients.research``, with a lab ``PrincipalClient`` over the
  research sockets.
- The running service renews its lease every 20 s and polls the owner cap; a test that jumps trader time does both
  before each step (``night()`` each round, ``.node.signals()`` on Monday).
- Monday: ``next_morning`` first refreshes the trader's daily bars (twenty closed sessions as of Monday), as the
  data service does each night; the trader's history fixture otherwise ends on Thursday.
- ``strategy_trials()`` is the count of the registry's trials of ``KEY``.

SP2c Plan 5 Task 8 (renewal) mappings:
- ``deploy_expiry_sessions`` goes to both judges: the trader's (set by the ``prepare`` seam, not ``trader.yaml``) and
  the research side's own ``BacktestJudgeConfig``.
- The seeded reader's Plan 1 fallback gets ``renewals_of`` from the stack's ``BacktestJudgments``, as the production
  reader does, so a SHADOW/REJECT renewal ends the old version.
- ``RenewalRequests`` takes ``incomplete_after_hours`` from this world's ``ResearchServiceConfig`` (the default 16 h).
- ``research_signer`` is the world's ``signer``; ``node_rows`` takes ``params``.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from tests.ai.decisions.decision_world import DecisionNode, TraderMarket, decisions_block, register_discretionary
from tests.ai.runtime.trader_world import FlakyClient, TraderWorld
from tests.automation.ai_paper_fixtures import daily_frame
from tests.research.evaluation_fixtures import (TIME_OF_DAY_STRATEGY, holdout_ruleset,
                                                judge_qualified_evidence_by_holdout_ruleset, write_costs_config,
                                                write_trend_bars)
from tests.rpc_identity_fixtures import ServedStack as Sockets, _identity_class
from tests.sp1_fixtures import CONID, MSFT, et
from tests.strategy.ai_deployment_fixtures import StrategyNode
from tests.test_execution_costs import _definition
from trader.ai.research_cycle import registration_body
from trader.ai.research_wire import Binding
from trader.ai.rpc_clients import LAB_COMMANDS, LAB_QUERIES, AiRpcClients, PrincipalClient
from trader.ai_service import build_research_cycle, build_session_slots
from trader.automation.ai_judgment_port import judgment_reader_for as plan1_judgment_reader_for
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.data.duckdb_store import DuckDBConnection, DuckDBDataStore
from trader.data.schema_migrations import SchemaMigrator
from trader.data.universe import UniverseAccessor
from trader.messaging.principals import peers_for
from trader.messaging.rpc_keys import RpcKeyring
from trader.messaging.typed_rpc import ReplayNonceCache
from trader.objects import BarSize
from trader.research.cohort import build_cohort_spec
from trader.research.cohort_evaluation import evaluate_cohort
from trader.research.evaluation import EvaluationPaths
from trader.research.evaluation_service import EvaluationService
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.forward_evidence_view import ForwardEvidenceView
from trader.research.judgment_attest import JudgmentAttest
from trader.research.renewal_service import RenewalRequests
from trader.research.research_surface import build_research_registry
from trader.research.schema import apply_research_migrations
from trader.research.service_config import ResearchServiceConfig
from trader.research.service_store import ResearchStore
from trader.research.signing import AttestationSigner
from trader.research.trader_port import TraderPort
from trader.simulation.execution_costs import load_execution_costs_config

KEY = "strategies/time_of_day.py:TimeOfDay"
STRATEGY_PATH, STRATEGY_CLASS = KEY.split(":")
RESEARCH_CONIDS = (CONID, MSFT, 1003, 1004, 1005, 1006, 1007, 1008)
RESEARCH_BLOCK = ("research:\n  enabled: true\n  strategy_keys: [\"strategies/time_of_day.py:TimeOfDay\"]\n"
                  f"  universes: {{us_eight: {list(RESEARCH_CONIDS)}}}\n  bar_sizes: [\"15 mins\"]\n"
                  "  max_candidates_per_cycle: 1\n  max_cohort_points: 1\n")
# One point (the defaults: buy at 10:00), so the deployed instance's entry minute is known before the evening.
DEPLOY_PROPOSAL = json.dumps({"candidates": [{"strategy": "S1", "universe": "U1", "bar_size": "B1",
                                              "points": [{}], "thesis": "morning drift"}]})
AI_DEPLOYMENTS_BRACKET = "  ai_deployments: {stop_fraction: 0.02, target_fraction: 0.04}\n"
NEW_YORK = ZoneInfo("America/New_York")
FRIDAY_AFTER_CLOSE = et(16, 31)                     # Friday 2026-07-17: the research day
MONDAY = dt.date(2026, 7, 20)
BARS_START, BARS_END = "2026-05-01", "2026-07-16"
SYMBOLS = {CONID: "AAPL", MSFT: "MSFT"}
RESEARCH_TABLES = ("ai_research_cycles", "ai_research_candidates", "ai_backtest_judgments",
                   "ai_research_registrations")
MAX_NIGHT_ROUNDS = 20
NIGHT_STEP_SECONDS = 31


class ResearchDecisionNode(DecisionNode):
    """The decision node of SP2 Plan 6. Like the running service, it polls the owner cap before a step,
    because a test may have moved trader time into another budget window."""

    async def signals(self) -> None:
        await self.cap_sync.sync()
        await super().signals()


class ResearchStrategyNode(StrategyNode):
    """Plan 2's strategy node; ``feed_bar`` returns the source_event_id of the one signal it recorded."""

    def feed_bar(self, conid: int, frame: pd.DataFrame) -> Optional[str]:
        before = len(self._signals())
        super().feed_bar(conid, frame)
        recorded = self._signals()[before:]
        if not recorded:
            return None
        (signal,) = recorded
        return signal.entry.source_event_id

    def _signals(self) -> tuple:
        return self.runtime.signal_record.read(0, 500).signals


class _TraderResearchFiles:
    """What the trader reads from the research service: the artifacts root and the research signer's key."""

    def __init__(self, root: Path, signer: AttestationSigner, deploy_expiry_sessions: int):
        self.deploy_expiry_sessions = deploy_expiry_sessions
        self.artifacts = root / "artifacts"
        self.verify = root / "keys" / "verify"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.verify.mkdir(parents=True, exist_ok=True)
        (self.verify / "research.pem").write_bytes(signer.public_key_pem())

    def prepare(self, trader: Any) -> None:
        judge = BacktestJudgeConfig(strategy_allowlist=(KEY,), deploy_expiry_sessions=self.deploy_expiry_sessions)
        trader.ai_paper_config = dataclasses.replace(trader.ai_paper_config, backtest_judge=judge)
        trader.research_artifacts_root = str(self.artifacts)
        trader.research_verify_dir = str(self.verify)


def _judge_case_menu_by_holdout_ruleset(monkeypatch) -> None:
    """The case's own deploy gate (``initial_deploy_allowed``: the research summary's menu, the trader's record
    check) compares with paper-v1 too. Synthetic bars pass only the holdout subset, so it demands that subset,
    like the bundle gate of ``judge_qualified_evidence_by_holdout_ruleset``."""
    import trader.research.evaluation_case as evaluation_case

    subset = holdout_ruleset()
    monkeypatch.setattr(evaluation_case, "PAPER_V1", subset)
    monkeypatch.setattr(evaluation_case, "PAPER_V1_CODES", frozenset(rule.code for rule in subset.rules))


def _write_universe(path: str) -> None:
    universes = UniverseAccessor(path, "Universes")
    for conid in RESEARCH_CONIDS:
        universes.insert("evaluation", _definition(conid, SYMBOLS.get(conid, f"S{conid}"), "NASDAQ"))


class ResearchWorld:
    def __init__(self) -> None:
        self._closers: list = []
        self._flaky_lab = False
        self._lab_sockets: dict[str, Any] = {}
        self._research_clients: dict[tuple[str, str], Any] = {}

    @classmethod
    async def build(cls, tmp_path: Path, loop_thread: Any, monkeypatch: Any, *, holdout_drift: Optional[float] = None,
                    flaky_lab: bool = False, deploy_expiry_sessions: int = 20,
                    renewals: bool = False) -> "ResearchWorld":
        """``renewals`` serves submit_evaluation kind RENEWAL (SP2c Plan 5); without it the research service
        refuses RENEWAL_NOT_SUPPORTED, as before Plan 5."""
        self = cls()
        try:
            await self._build(tmp_path, loop_thread, monkeypatch, holdout_drift, flaky_lab, deploy_expiry_sessions,
                              renewals)
        except BaseException:
            self.close()
            raise
        return self

    async def _build(self, tmp_path, loop_thread, monkeypatch, holdout_drift, flaky_lab, deploy_expiry_sessions,
                     renewals) -> None:
        # The trader reads trading_filters.yaml from here, never from the developer's ~/.config/mmr.
        monkeypatch.setattr("trader.trading.trading_filter._default_path", lambda: tmp_path / "trading_filters.yaml")
        monkeypatch.setenv("MMR_STRATEGIES_EXTRA_ROOT", str(tmp_path))
        judge_qualified_evidence_by_holdout_ruleset(monkeypatch)
        _judge_case_menu_by_holdout_ruleset(monkeypatch)
        self.repo = tmp_path / "repo"
        (self.repo / "strategies").mkdir(parents=True)
        (self.repo / STRATEGY_PATH).write_text(TIME_OF_DAY_STRATEGY)
        self.signer = AttestationSigner.generate()
        trader_side = _TraderResearchFiles(tmp_path, self.signer, deploy_expiry_sessions)
        self.artifacts = trader_side.artifacts

        market = TraderMarket()
        market.extra_prepare = trader_side.prepare
        self.world = TraderWorld(tmp_path, loop_thread, monkeypatch, prepare=market.prepare)
        self._closers.append(self.world.close)
        market.now = self.world.served.now
        served = self.world.served
        judgments = served.stack.ai_paper.judgments
        served.seeded._inner = plan1_judgment_reader_for(judgments, cases_dir=self.cases_dir,
                                                         verify_dir=trader_side.verify,
                                                         renewals_of=judgments.renewals_of)
        served.clock[0] = FRIDAY_AFTER_CLOSE

        self._start_research_service(tmp_path / "research", holdout_drift, deploy_expiry_sessions, renewals)
        self._flaky_lab = flaky_lab

        digest = register_discretionary(self.world)
        block = decisions_block(self.world, digest, AI_DEPLOYMENTS_BRACKET) + RESEARCH_BLOCK
        self.node = ResearchDecisionNode(self.world, tmp_path, block)
        await self.node.start()
        lab = PrincipalClient("ai_research", command=self.lab_socket("command"), query=self.lab_socket("query"),
                              commands=LAB_COMMANDS, queries=LAB_QUERIES, timeout=30.0,
                              unreachable_code="RESEARCH_UNREACHABLE")
        ai = self.node.node
        clients = AiRpcClients(ai.clients.supervisor, ai.clients.research, lab)
        self.cycle = build_research_cycle(self.node.config, store=ai.store, clock=ai.clock,
                                          slots=build_session_slots(self.node.config), leadership=ai.leadership,
                                          watch=ai.watch, clients=clients, gateway=self.node.gated,
                                          strategies_root=self.repo)
        self.strategy = ResearchStrategyNode(served, strategies_dir=self.repo / "strategies")

    def _start_research_service(self, root: Path, holdout_drift: Optional[float], deploy_expiry_sessions: int,
                                renewals: bool) -> None:
        served = self.world.served
        root.mkdir()
        bars = str(root / "bars.duckdb")
        _write_universe(bars)
        write_trend_bars(bars, drift=0.0006, start=BARS_START, end=BARS_END, conids=RESEARCH_CONIDS,
                         holdout_drift=holdout_drift)
        self.bars = bars
        costs_path = write_costs_config(root / "execution_costs.yaml")
        costs = load_execution_costs_config(str(costs_path))
        self.research_db = DuckDBConnection.get_instance(str(root / "research.duckdb"))
        apply_research_migrations(SchemaMigrator(self.research_db))
        self.registry = ExperimentRegistry(self.research_db)
        store = ResearchStore(self.research_db)
        self.trader_port = TraderPort(served.sockets.client("research", "trader", "query", timeout=30.0),
                                      served.sockets.client("research", "trader", "command", timeout=30.0))
        config = ResearchServiceConfig(period_sessions=40, folds=2, embargo_sessions=1, holdout_sessions=5)
        judge = BacktestJudgeConfig(strategy_allowlist=(KEY,), deploy_expiry_sessions=deploy_expiry_sessions)
        paths = EvaluationPaths(bars, bars, "Universes", str(costs_path), self.repo, root / "reports",
                                self.artifacts / "evaluations")
        universe = UniverseAccessor(bars, "Universes")
        now = served.now

        def build_spec(body):
            return build_cohort_spec(body, config=config, judge=judge, universe_accessor=universe,
                                     costs_config=costs, repo_root=self.repo, registry=self.registry,
                                     history_db=bars)

        self.evaluations = EvaluationService(
            store=store, trader=self.trader_port, build_spec=build_spec,
            evaluate=lambda spec: evaluate_cohort(spec, research_db=self.research_db, paths=paths, now=now,
                                                  ruleset=holdout_ruleset()),
            signer=self.signer, artifacts_root=self.artifacts, warmup_sessions=judge.shadow_warmup_sessions,
            order_notional=config.order_notional, queue_max=config.queue_max, now=now,
            renewals=RenewalRequests(store=store, trader=self.trader_port, signer=self.signer,
                                     artifacts_root=self.artifacts, repo_root=self.repo, judge=judge,
                                     warmup_sessions=judge.shadow_warmup_sessions,
                                     incomplete_after_hours=config.shadow_incomplete_after_hours,
                                     now=now) if renewals else None)
        attest = JudgmentAttest(research_db=self.research_db, store=store, trader=self.trader_port,
                                signer=self.signer, artifacts_root=self.artifacts, repo_root=self.repo,
                                is_paper=lambda: True, now=now, ruleset=holdout_ruleset())
        registry = build_research_registry(evaluations=self.evaluations, attest=attest)
        self.research_sockets = Sockets({("research", "command"): registry, ("research", "query"): registry},
                                        served.identities)
        self._closers.append(self.research_sockets.close)

    # -- the ai side --------------------------------------------------------------------------------------------
    def lab_socket(self, role: str) -> Any:
        """The ai_research socket to the research server (wrapped in FlakyClient when ``flaky_lab``)."""
        if role not in self._lab_sockets:
            client = self.research_sockets.client("ai_research", server="research", role=role, timeout=30.0)
            self._lab_sockets[role] = FlakyClient(client) if self._flaky_lab else client
        return self._lab_sockets[role]

    async def _hold_the_lease(self) -> None:
        await self.node.node.leadership.grant_once()

    async def night(self) -> None:
        """The evening after the close: the slot, then pump and research ticks until nothing moves.

        Raises AssertionError when the rows still move after MAX_NIGHT_ROUNDS rounds."""
        await self.node.cap_sync.sync()
        await self._hold_the_lease()
        await self.node.controller.refresh_experiment()
        await self.cycle.run_due_slot()
        changed: list[str] = []
        for _ in range(MAX_NIGHT_ROUNDS):
            before = self._research_state()
            await self._hold_the_lease()
            await self.cycle.pump()
            moved = self.evaluations.run_next()
            self.world.served.advance(NIGHT_STEP_SECONDS)
            after = self._research_state()
            changed = [table for table in RESEARCH_TABLES if before[table] != after[table]]
            if not moved and not changed:
                return
        raise AssertionError(f"the night never settled after {MAX_NIGHT_ROUNDS} rounds; last round changed "
                             f"{', '.join(changed) or 'no table'} (research tick moved: {moved})")

    def _research_state(self) -> dict[str, list]:
        return {table: self.node_rows(f"SELECT * FROM {table} ORDER BY 1") for table in RESEARCH_TABLES}

    # -- Monday ---------------------------------------------------------------------------------------------------
    def next_morning(self, hour: int, minute: int) -> None:
        at = dt.datetime.combine(MONDAY, dt.time(hour, minute), tzinfo=NEW_YORK).astimezone(dt.timezone.utc)
        self._refresh_daily_bars(at)
        self.world.served.run_session(at)

    def _refresh_daily_bars(self, at: dt.datetime) -> None:
        """The data service's nightly refresh: the trader's daily bars end at the last closed session again."""
        daily = self.world.served.trader.data.get_tickdata(BarSize.Days1)
        for conid in (CONID, MSFT):
            daily.write(conid, daily_frame(at))

    def morning_bar(self, conid: int) -> pd.DataFrame:
        """The accumulated 15-minute frame: the fixture history, then Monday's bars up to the one labelled 10:00."""
        history = DuckDBDataStore(self.bars).read(str(conid))
        monday = pd.date_range(pd.Timestamp(f"{MONDAY} 09:30", tz=NEW_YORK),
                               pd.Timestamp(f"{MONDAY} 10:00", tz=NEW_YORK), freq="15min").tz_convert("UTC")
        last = history.iloc[-1]
        today = pd.DataFrame({column: [last[column]] * len(monday) for column in history.columns}, index=monday)
        frame = pd.concat([history, today])
        frame.index.name = "date"
        return frame

    # -- renewal evenings and mornings (SP2c Plan 5) ----------------------------------------------------------------
    def evening(self, day: dt.date) -> None:
        """17:00 New York on ``day``: inside that evening's research window (slot due from 16:30).

        It is one ``run_session`` jump, so it skips the sessions in between on purpose: they get no session-end
        ledger rows."""
        self.world.served.run_session(dt.datetime.combine(day, dt.time(17, 0), NEW_YORK).astimezone(dt.timezone.utc))

    def morning(self, day: dt.date, hour: int, minute: int) -> None:
        self.world.served.run_session(dt.datetime.combine(day, dt.time(hour, minute), NEW_YORK)
                                      .astimezone(dt.timezone.utc))

    def version(self, digest: str) -> dict:
        return self.trader_call("cli", "get_ai_deployment_version", {"version_digest": digest})["version"]

    def record_forward_rows(self, judgment_id: str, sessions, *, status: str = "COMPLETE") -> None:
        """Rows as the research replay sends them: signed as research through the real record_shadow_result."""
        judgment = self.trader_call("research", "get_backtest_judgment",
                                    {"judgment_id": judgment_id, "case_digest": None})["judgment"]
        for day in sessions:
            numbers = ({"reason": None, "pnl_usd": 6.0, "fees_usd": 1.0, "trades": 1, "end_equity_usd": 100_006.0}
                       if status == "COMPLETE" else
                       {"reason": "BARS_MISSING: fixture", "pnl_usd": None, "fees_usd": None, "trades": None,
                        "end_equity_usd": None})
            reply = self.trader_call("research", "record_shadow_result", {
                "judgment_id": judgment_id, "case_digest": judgment["case_digest"], "verdict": judgment["verdict"],
                "session_date": day.isoformat(), "status": status, "bar_size": "15 mins", **numbers})
            assert reply["status"] == "INSERTED", reply

    def opened_holdouts(self) -> int:
        return len(self.registry.opened_holdout_windows(STRATEGY_PATH, STRATEGY_CLASS))

    def forward_view(self, version: str) -> ForwardEvidenceView:
        reply = self.trader_call("research", "get_deployment_forward_evidence", {"deployment_version": version})
        assert reply["status"] == "FOUND", reply
        return ForwardEvidenceView.model_validate(reply["evidence"])

    @property
    def cases_dir(self) -> Path:
        return self.artifacts / "cases"                    # the directory the trader reads cases from

    @property
    def research_signer(self) -> AttestationSigner:
        return self.signer

    # -- reads and calls ------------------------------------------------------------------------------------------
    def node_rows(self, sql: str, params: Optional[list] = None) -> list:
        return self.node.node.store.db.execute(sql, params, fetch="all")

    def trader_rows(self, sql: str) -> list:
        return self.world.served.trader.journal_db.execute(sql, fetch="all")

    def trader_call(self, principal: str, method: str, body: dict) -> dict:
        return self.world.served.call(principal, method, body)

    def research_client(self, principal: str, role: str) -> Any:
        """A signed client of ``principal`` at the research server. A principal that is no research peer still
        holds the research public key (public), so it can read the server's own signed refusal."""
        key = (principal, role)
        if key not in self._research_clients:
            self._research_clients[key] = self.research_sockets.client(
                principal, server="research", role=role, timeout=30.0, identity=self._trusting_research(principal))
        return self._research_clients[key]

    def _trusting_research(self, principal: str) -> Any:
        identities = self.world.served.identities
        if "research" in peers_for(principal):
            return identities[principal]
        trusted = {peer: identities[peer].public_key for peer in peers_for(principal) | {"research"}}
        return _identity_class()(principal, identities[principal]._private_key_for_tests(),
                                 RpcKeyring.from_public_keys(trusted), nonce_cache=ReplayNonceCache())

    def reviews(self) -> list:
        return self.research_db.execute("SELECT reviewer, reviewer_kind, holdout_opened_once_confirmed "
                                        "FROM operator_reviews ORDER BY reviewed_at", fetch="all")

    def bundles(self) -> list[str]:
        return sorted(path.name for path in self.artifacts.glob("sha256_*"))

    def strategy_trials(self) -> int:
        return len(self.registry.strategy_trials(STRATEGY_PATH, STRATEGY_CLASS))

    def registration_with_evidence(self, evidence: str) -> dict:
        """A valid-looking binding of the fixture strategy whose bundle and evidence are ``evidence``."""
        (judgment_id,), = self.node_rows("SELECT judgment_id FROM ai_backtest_judgments WHERE state = 'RECORDED'")
        (body_json,), = self.node_rows("SELECT body_json FROM ai_research_candidates")
        file_hash = "sha256:" + hashlib.sha256((self.repo / STRATEGY_PATH).read_bytes()).hexdigest()
        binding = Binding(strategy_path=STRATEGY_PATH, class_name=STRATEGY_CLASS, file_hash=file_hash,
                          params=json.loads(body_json)["cohort"][0], conids=list(RESEARCH_CONIDS),
                          bar_size="15 mins", order_notional=1900.0)
        return registration_body(binding, judgment_id=judgment_id, bundle_digest=evidence)

    def close(self) -> None:
        """Close everything; the first failure is raised after the rest are closed."""
        closers, self._closers = self._closers, []
        failure: Optional[BaseException] = None
        for close in reversed(closers):
            try:
                close()
            except Exception as exc:
                failure = failure or exc
        if failure is not None:
            raise failure
