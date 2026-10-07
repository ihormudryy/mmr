"""A gateway wired to real adapters and fake providers, for gateway, replay and flow tests."""
from tests.ai.fakes import FakeProvider, load_test_config
from trader.ai.gateway import ModelGateway
from trader.ai.model_client import ChatMessage, ModelRequest
from trader.ai.store import AiStore


def request(key="d1/orchestrator/1", text="find ideas", max_output_tokens=500) -> ModelRequest:
    return ModelRequest(request_key=key, messages=(ChatMessage("user", text),), max_output_tokens=max_output_tokens)


class World:
    def __init__(self, tmp_path, clock, **config):
        self.clock = clock
        self.config = load_test_config(tmp_path, **config)
        self.store = AiStore(tmp_path / "ai.duckdb", clock=clock)
        orchestrator_model = self.config.role("orchestrator").model
        self.orchestrator = FakeProvider(clock=clock, model=orchestrator_model)
        self.jev = FakeProvider(clock=clock, model="vendor/jev-1")
        self.gateway = ModelGateway(
            config=self.config, store=self.store, clock=clock,
            clients={"orchestrator": self.orchestrator.adapter(orchestrator_model),
                     "jev": self.jev.adapter("vendor/jev-1")})

    def rows(self, sql):
        return self.store.db.execute(sql, fetch="all")
