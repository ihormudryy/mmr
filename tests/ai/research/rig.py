"""The research cycle on a real gateway (scripted OpenRouter behind the real adapter) and scripted RPC clients."""
import datetime as dt
import json
from types import SimpleNamespace

from tests.ai.decisions.fakes import ScriptedProvider
from tests.ai.fakes import FakeClock, config_text, write_config
from tests.research.evaluation_fixtures import TIME_OF_DAY_STRATEGY
from trader.ai.backtest_judge import BacktestJudgeRunner
from trader.ai.budget_cap import CapGatedGateway
from trader.ai.config import load_ai_config, usd_to_micros_floor
from trader.ai.gateway import ModelGateway
from trader.ai.replay import ReplayRecorder
from trader.ai.research_roles import NARRATIVE_FIELDS
from trader.ai.rpc_clients import (LAB_COMMANDS, LAB_QUERIES, RESEARCH_COMMANDS, RESEARCH_QUERIES,
                                   MethodNotAllowedLocally)
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.schedule import SessionSlots
from trader.ai.store import AiStore

NIGHT = dt.datetime(2026, 10, 8, 21, 0, tzinfo=dt.timezone.utc)            # 17:00 New York, Thursday
EXPERIMENT = "exp-" + "b" * 20
BLOCK = ("research:\n  enabled: true\n  strategy_keys: [\"strategies/time_of_day.py:TimeOfDay\"]\n"
         "  universes: {us_eight: [1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008]}\n")
NARRATIVE = {name: f"{name} from the case" for name in NARRATIVE_FIELDS}


def ruling(verdict="DEPLOY", narrative=True, **fields):
    body = {"verdict": verdict, "reason": "ok", **(NARRATIVE if verdict == "DEPLOY" and narrative else {})}
    return json.dumps({**body, **fields})


class ScriptedClient:
    """A PrincipalClient double. Replies queue per method (dict, callable(body) or exception); the last repeats."""

    def __init__(self, methods):
        self.methods, self.queues, self.calls = frozenset(methods), {}, []

    def script(self, method, *replies):
        self.queues.setdefault(method, []).extend(replies)

    async def call(self, method, body):
        if method not in self.methods:
            raise MethodNotAllowedLocally("METHOD_NOT_IN_CLIENT_SET", method)
        self.calls.append((method, json.loads(json.dumps(body))))
        queue = self.queues.get(method)
        if not queue:
            raise AssertionError(f"unscripted call {method}")
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply(body) if callable(reply) else reply

    def sent(self, method):
        return [body for name, body in self.calls if name == method]


class Leader:
    def __init__(self, epoch):
        self.epoch = epoch

    def current_epoch(self):
        return self.epoch


class Watch:
    def __init__(self, experiment_id):
        self.known = True
        self.view = None if experiment_id is None else SimpleNamespace(experiment_id=experiment_id, state="ARMED")


class OpenCap:
    """The owner cap of the current window has been read: the gate is open."""
    last_error = None

    def ready(self):
        return True


class Rig:
    def __init__(self, tmp_path, block=BLOCK):
        self.tmp_path, self.clock = tmp_path, FakeClock(NIGHT)
        self.config = load_ai_config(str(write_config(tmp_path, config_text(extra_top_level=block))))
        self.store = AiStore(tmp_path / "ai.duckdb", clock=self.clock)
        self.store.migrate(ALL_MIGRATIONS)
        self.orchestrator, self.jev = ScriptedProvider("vendor/orch-1"), ScriptedProvider("vendor/jev-1")
        self.gateway = ModelGateway(config=self.config, store=self.store, clock=self.clock,
                                    clients={"orchestrator": self.orchestrator.adapter("vendor/orch-1"),
                                             "jev": self.jev.adapter("vendor/jev-1")})
        self.gated = CapGatedGateway(self.gateway, OpenCap())               # as ai_service wires it
        self.judge = BacktestJudgeRunner(config=self.config, gateway=self.gated, store=self.store, clock=self.clock,
                                         recorder=ReplayRecorder(self.store))
        self.lab = ScriptedClient(LAB_COMMANDS | LAB_QUERIES)
        self.registry = ScriptedClient(RESEARCH_COMMANDS | RESEARCH_QUERIES)
        (tmp_path / "strategies").mkdir(exist_ok=True)
        (tmp_path / "strategies" / "time_of_day.py").write_text(TIME_OF_DAY_STRATEGY)

    async def start(self, cap_usd=2000.0):
        await self.gateway.start()
        await self.gateway.budget.set_cap(usd_to_micros_floor(cap_usd))

    def cycle(self, *, epoch=1, experiment=EXPERIMENT, gateway=None):
        from trader.ai.research_cycle import ResearchCycle                     # Task 6
        return ResearchCycle(config=self.config, store=self.store, clock=self.clock, slots=SessionSlots(),
                             leadership=Leader(epoch), watch=Watch(experiment), lab=self.lab, registry=self.registry,
                             gateway=gateway or self.gated, judge=self.judge, strategies_root=self.tmp_path)

    def rows(self, sql, params=None):
        return self.store.db.execute(sql, params, fetch="all")
