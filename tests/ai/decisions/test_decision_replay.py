"""SP2 Plan 6 Task 7: a recorded Jev decision replays offline, exactly, or says what is missing (spec 11)."""
import datetime as dt
import json

import pytest

from tests.ai.decisions.fakes import AAPL, MSFT, NOW, FakeReads, entry_quote_reply
from tests.ai.decisions.test_decision_engine import BLOCK, SIGNAL, Rig, ruling, started
from tests.ai.decisions.test_discovery_client import candidate, response
from tests.ai.fakes import load_test_config
from trader.ai.decision_replay import recorded_judgment, replay_decision
from trader.ai.ids import derive_decision_id
from trader.ai.replay import COMPLETE, INCOMPLETE, ExternalAdapterCounter
from trader.ai.roles import ENTRY_MARKER, JEV_MARKER

DECISION_ID = derive_decision_id(SIGNAL.opportunity_id, f"enter:{AAPL}")


@pytest.mark.asyncio
async def test_replay_reproduces_a_jev_decision_offline(tmp_path, no_network, monkeypatch):    # review focus 5
    rig = await started(tmp_path)
    rig.jev.script(JEV_MARKER, ruling("REDUCE", 4))
    await rig.engine.on_entry_signal(rig.signal())
    reads_before, provider_before = len(rig.reads.calls), len(rig.jev.requests)
    counter = ExternalAdapterCounter()
    counter.instrument("openrouter", rig.gateway._clients["jev"])          # replay must never touch it
    monkeypatch.setattr(type(rig.reads), "call", counter.tripwire("trader_read"))
    result = await replay_decision(rig.store, DECISION_ID, config=rig.config, counter=counter)
    assert result.status == COMPLETE and result.value == recorded_judgment(rig.store, DECISION_ID)
    assert result.value["outcome"] == "REDUCE" and result.value["quantity"] == 4
    assert counter.total == 0 and (len(rig.reads.calls), len(rig.jev.requests)) == (reads_before, provider_before)


@pytest.mark.asyncio
async def test_missing_evidence_is_incomplete(tmp_path, no_network):                           # review focus 5
    rig = await started(tmp_path)
    rig.jev.script(JEV_MARKER, ruling())
    await rig.engine.on_entry_signal(rig.signal())
    rig.store.db.execute("DELETE FROM ai_replay_evidence WHERE decision_key = ? AND name = 'account'", [DECISION_ID])
    result = await replay_decision(rig.store, DECISION_ID, config=rig.config)
    assert result.status == INCOMPLETE and result.missing == ("tool_result:account#1",)


@pytest.mark.asyncio
async def test_a_rejudged_unit_is_reported_incomplete(tmp_path):
    rig = await started(tmp_path)
    rig.jev.script(JEV_MARKER, ruling(), ruling("SKIP"))
    await rig.engine.on_entry_signal(rig.signal())
    await rig.engine.on_entry_signal(rig.signal())               # Plan 5 Ruling 9: the same key judged again
    result = await replay_decision(rig.store, DECISION_ID, config=rig.config)
    assert (result.status, result.missing) == (INCOMPLETE, ("rejudged_unit",))


@pytest.mark.asyncio
async def test_a_changed_config_or_prompt_is_never_complete(tmp_path, monkeypatch):
    rig = await started(tmp_path)
    thesis = "a long thesis that the shorter news limit of the replay config will cut " * 2
    rig.orchestrator.script(ENTRY_MARKER, json.dumps({"picks": [{"candidate": "C1", "thesis": thesis}]}))
    rig.jev.script(JEV_MARKER, ruling())
    cycle = rig.entry_cycle()
    await rig.engine.on_entry_cycle(cycle)
    decision_id = derive_decision_id(cycle.slot.cycle_id, f"enter:{MSFT}")
    same = await replay_decision(rig.store, decision_id, config=rig.config)
    assert same.status == COMPLETE and same.value == recorded_judgment(rig.store, decision_id)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    shorter = load_test_config(other_dir, extra_top_level=BLOCK + "  news_chars_per_item: 50\n")
    result = await replay_decision(rig.store, decision_id, config=shorter)
    assert (result.status, result.missing) == (INCOMPLETE, ("config_mismatch",))      # PR #86 4211394935
    monkeypatch.setattr("trader.ai.roles.JEV_SYSTEM", "[JEV_ENTRY_RULING] a changed prompt")   # code, same config
    monkeypatch.setenv("MMR_CODE_VERSION", recorded_code_version(rig.store, decision_id))
    result = await replay_decision(rig.store, decision_id, config=rig.config)
    assert result.status == INCOMPLETE and result.missing[0].startswith("request_changed:")


def recorded_code_version(store, decision_id):
    row = store.db.execute("SELECT payload_json FROM ai_replay_evidence WHERE decision_key = ? AND kind = 'manifest'",
                           [decision_id], fetch="one")
    return json.loads(row[0])["code_version"]


@pytest.mark.asyncio
async def test_a_changed_config_is_incomplete_even_when_the_verdict_would_match(tmp_path, no_network, monkeypatch):
    rig = await started(tmp_path)                                    # PR #86 thread 4211394935
    rig.jev.script(JEV_MARKER, ruling())
    await rig.engine.on_entry_signal(rig.signal())
    counter = ExternalAdapterCounter()
    monkeypatch.setattr(type(rig.reads), "call", counter.tripwire("trader_read"))
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    tighter = load_test_config(other_dir, extra_top_level=BLOCK + "  quote_max_age_seconds: 1\n")
    result = await replay_decision(rig.store, DECISION_ID, config=tighter, counter=counter)
    assert (result.status, result.missing, result.value) == (INCOMPLETE, ("config_mismatch",), None)
    assert counter.total == 0 and len(rig.jev.requests) == 1
    same = await replay_decision(rig.store, DECISION_ID, config=rig.config, counter=counter)
    assert same.status == COMPLETE and same.value["outcome"] == "TAKE"


@pytest.mark.asyncio
async def test_a_changed_code_version_is_incomplete(tmp_path, no_network, monkeypatch):
    rig = await started(tmp_path)
    rig.jev.script(JEV_MARKER, ruling())
    await rig.engine.on_entry_signal(rig.signal())
    monkeypatch.setenv("MMR_CODE_VERSION", recorded_code_version(rig.store, DECISION_ID) + "-changed")
    result = await replay_decision(rig.store, DECISION_ID, config=rig.config)
    assert (result.status, result.missing) == (INCOMPLETE, ("code_mismatch",))


@pytest.mark.asyncio
async def test_a_refusal_before_any_model_replays_as_the_same_refusal(tmp_path):
    stale = FakeReads(get_ai_entry_quote=lambda body: entry_quote_reply(conid=body["conid"],
                                                                         at=NOW - dt.timedelta(seconds=60)))
    rig = await started(tmp_path, stale)
    await rig.engine.on_entry_signal(rig.signal())
    result = await replay_decision(rig.store, DECISION_ID, config=rig.config)
    assert result.status == COMPLETE and result.value["code"] == "QUOTE_STALE"
    assert result.value == recorded_judgment(rig.store, DECISION_ID) and rig.jev.requests == []
    attempts = rig.store.db.execute("SELECT COUNT(*) FROM ai_model_attempts", fetch="one")[0]
    assert attempts == 0


@pytest.mark.asyncio
async def test_an_unknown_decision_is_incomplete(tmp_path):
    rig = Rig(tmp_path, FakeReads(discover_ai_candidates=response([candidate("AAPL", AAPL)])))
    result = await replay_decision(rig.store, "dec-" + "0" * 32, config=rig.config)
    assert result.status == INCOMPLETE
