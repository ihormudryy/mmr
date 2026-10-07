"""A gateway wired to real adapters and fake providers, for gateway, replay and flow tests."""
from tests.ai.fakes import FakeProvider, load_test_config
from trader.ai.config import usd_to_micros_floor
from trader.ai.gateway import ModelGateway
from trader.ai.model_client import ChatMessage, ModelRequest
from trader.ai.store import AiStore


def request(key="d1/orchestrator/1", text="find ideas", max_output_tokens=500) -> ModelRequest:
    return ModelRequest(request_key=key, messages=(ChatMessage("user", text),), max_output_tokens=max_output_tokens)


class World:
    """``start()`` stands in for Plan 5's controller: it starts the gateway, then sets the owner cap."""

    def __init__(self, tmp_path, clock, *, cap_usd: float = 2000.0, **config):
        self.clock = clock
        self.cap_usd = cap_usd
        self.config = load_test_config(tmp_path, **config)
        self.store = AiStore(tmp_path / "ai.duckdb", clock=clock)
        orchestrator_model = self.config.role("orchestrator").model
        self.orchestrator = FakeProvider(clock=clock, model=orchestrator_model)
        self.jev = FakeProvider(clock=clock, model="vendor/jev-1")
        self.gateway = ModelGateway(
            config=self.config, store=self.store, clock=clock,
            clients={"orchestrator": self.orchestrator.adapter(orchestrator_model),
                     "jev": self.jev.adapter("vendor/jev-1")})

    async def start(self) -> None:
        await self.gateway.start()
        await self.gateway.budget.set_cap(usd_to_micros_floor(self.cap_usd))

    def rows(self, sql):
        return self.store.db.execute(sql, fetch="all")
