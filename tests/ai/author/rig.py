"""The author loop on a real gateway (scripted OpenRouter behind the real adapter) and scripted RPC clients."""
import datetime as dt
from types import SimpleNamespace

from tests.ai.author.helpers import author_text
from tests.ai.decisions.fakes import ScriptedProvider
from tests.ai.fakes import FakeClock, write_config
from tests.ai.research.rig import Leader, OpenCap, ScriptedClient
from trader.ai.author_loop import AuthorLoop
from trader.ai.budget_cap import CapGatedGateway
from trader.ai.config import load_ai_config, usd_to_micros_floor
from trader.ai.gateway import ModelGateway
from trader.ai.replay import ReplayRecorder
from trader.ai.rpc_clients import AUTHOR_COMMANDS, AUTHOR_QUERIES
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.schedule import SessionSlots
from trader.ai.store import AiStore

NIGHT = dt.datetime(2026, 10, 8, 21, 5, tzinfo=dt.timezone.utc)            # 17:05 New York, Thursday
EXPERIMENT = "exp-" + "c" * 20
SCOREBOARD = {"label": "PAPER", "account": {"sessions": 3}, "splits": {"strategy_version": {}},
              "authored_vs_catalogue": {"verdict": "not enough data"}}


class Watch:
    def __init__(self, experiment_id=EXPERIMENT, state="ARMED"):
        self.known = True
        self.view = None if experiment_id is None else SimpleNamespace(experiment_id=experiment_id, state=state)


class AuthorRig:
    def __init__(self, tmp_path, *, text=None):
        self.clock = FakeClock(NIGHT)
        self.config = load_ai_config(str(write_config(tmp_path, text or author_text())))
        self.store = AiStore(tmp_path / "ai.duckdb", clock=self.clock)
        self.store.migrate(ALL_MIGRATIONS)
        self.model = ScriptedProvider("vendor/author-1")
        self.gateway = ModelGateway(config=self.config, store=self.store, clock=self.clock, clients={
            "orchestrator": ScriptedProvider("vendor/orch-1").adapter("vendor/orch-1"),
            "jev": ScriptedProvider("vendor/jev-1").adapter("vendor/jev-1"),
            "author": self.model.adapter("vendor/author-1")})
        self.gated = CapGatedGateway(self.gateway, OpenCap())                      # as ai_service wires it
        self.author = ScriptedClient(AUTHOR_COMMANDS | AUTHOR_QUERIES)
        self.supervisor = ScriptedClient({"get_scoreboard"})
        self.supervisor.script("get_scoreboard", SCOREBOARD)
        self.watch = Watch()
        self.loop = self.new_loop()

    def new_loop(self):
        """A fresh AuthorLoop on the same store and clients: what a restarted controller builds."""
        return AuthorLoop(config=self.config, store=self.store, clock=self.clock,
                          slots=SessionSlots(author_after_close_minutes=60), leadership=Leader(1),
                          watch=self.watch, author=self.author, supervisor=self.supervisor, gateway=self.gated,
                          recorder=ReplayRecorder(self.store))

    async def start(self, cap_usd=2000.0):
        await self.gateway.start()
        await self.gateway.budget.set_cap(usd_to_micros_floor(cap_usd))

    def rows(self, sql, params=None):
        return self.store.db.execute(sql, params, fetch="all")
