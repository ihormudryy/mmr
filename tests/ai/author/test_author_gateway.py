# tests/ai/author/test_author_gateway.py
import datetime as dt

import pytest

from trader.scoreboard.ingest_models import RecordAiCostRequest

from tests.ai.author.helpers import author_text, load
from tests.ai.decisions.fakes import ScriptedProvider
from tests.ai.fakes import FakeClock, config_text
from tests.ai.research.rig import OpenCap
from trader.ai.budget_cap import CapGatedGateway
from trader.ai.config import AiConfigError, usd_to_micros_floor
from trader.ai.gateway import CallRefused, ModelGateway
from trader.ai.journal import COST_CONFIRMED
from trader.ai.model_client import ChatMessage, ModelRequest
from trader.ai.outbox import CallContext, cost_body
from trader.ai.replay import ReplayClock, ReplayEvidence, ReplayGateway
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.store import AiStore

NIGHT = dt.datetime(2026, 10, 8, 21, 5, tzinfo=dt.timezone.utc)
MODELS = {"orchestrator": "vendor/orch-1", "jev": "vendor/jev-1", "author": "vendor/author-1"}


async def started(tmp_path, config, names):
    clock = FakeClock(NIGHT)
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    store.migrate(ALL_MIGRATIONS)
    fakes = {name: ScriptedProvider(MODELS[name]) for name in names}
    gateway = ModelGateway(config=config, store=store, clock=clock,
                           clients={name: fake.adapter(MODELS[name]) for name, fake in fakes.items()})
    await gateway.start()
    await gateway.budget.set_cap(usd_to_micros_floor(2000.0))
    return gateway, fakes, store


def request(key="aut-20261008/author/1"):
    return ModelRequest(request_key=key, messages=(ChatMessage("user", "[A] write"),), max_output_tokens=8000)


@pytest.mark.asyncio
async def test_authoring_off_needs_no_author_client_and_refuses_author_calls(tmp_path):
    config = load(tmp_path, config_text())
    gateway, _fakes, _store = await started(tmp_path, config, ("orchestrator", "jev"))
    with pytest.raises(CallRefused) as exc:
        await gateway.call("author", request(), gateway.new_deadline("x"))
    assert exc.value.code == "ROLE_DISABLED"


def test_authoring_on_needs_the_author_client(tmp_path):
    config = load(tmp_path, author_text())
    clock = FakeClock(NIGHT)
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    with pytest.raises(AiConfigError) as exc:
        ModelGateway(config=config, store=store, clock=clock, clients={
            name: ScriptedProvider(MODELS[name]).adapter(MODELS[name]) for name in ("orchestrator", "jev")})
    assert exc.value.code == "CLIENT_ROLE_MISMATCH"


@pytest.mark.asyncio
async def test_an_author_call_runs_under_its_own_deadline_and_is_journaled_as_author(tmp_path):
    config = load(tmp_path, author_text())
    gateway, fakes, store = await started(tmp_path, config, ("orchestrator", "jev", "author"))
    fakes["author"].script("[A]", '{"ok": true}')
    deadline = gateway.new_deadline("aut-20261008", seconds=config.authoring.call_timeout_seconds)
    assert deadline.remaining() == 300.0 and gateway.new_deadline("x").remaining() == 60.0
    result = await gateway.call("author", request(), deadline)
    assert result.response.text == '{"ok": true}'
    assert store.db.execute("SELECT role, model FROM ai_model_attempts", fetch="all") == [("author", "vendor/author-1")]


class InnerGateway:
    budget = journal = None

    def new_deadline(self, label="", seconds=None):
        return (label, seconds)


def test_the_cap_gate_and_replay_pass_the_deadline_length():
    gated = CapGatedGateway(InnerGateway(), OpenCap())
    assert gated.new_deadline("u", 300.0) == ("u", 300.0) and gated.new_deadline("u") == ("u", None)
    replay = ReplayGateway(ReplayEvidence.__new__(ReplayEvidence), ReplayClock([NIGHT]))
    assert replay.new_deadline("u", seconds=300.0).remaining() > 300.0          # replay never runs out of time


def test_author_spend_is_reported_under_its_own_cost_role():                          # ruling 14 (owner)
    class Event:
        kind, role, backend, model = COST_CONFIRMED, "author", "openrouter", "vendor/author-1"
        attempt_key, event_id, input_tokens, output_tokens, cost_micros = "aut-20261008/author/1#1", "e1", 900, 60, 2280

    class Attempt:
        started_at = NIGHT
    body = cost_body(Event(), Attempt(), CallContext("aut-20261008", "exp-" + "c" * 20, "research", "aut-20261008"))
    assert (body["role"], body["model"], body["served_kind"], body["served_id"]) == (
        "author", "vendor/author-1", "research", "aut-20261008")
    RecordAiCostRequest.model_validate(body)                    # the trader's wire model accepts the role
