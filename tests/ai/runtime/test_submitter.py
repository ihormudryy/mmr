"""SP2 Plan 5 Task 6: persist the exact command before sending; reconcile unknown outcomes (spec 5.3, 9)."""
import datetime as dt
import json

import pytest

from tests.ai.fakes import FakeClock
from tests.ai.runtime.fakes import AAPL, FakeLeadership, ScriptedTrader, et
from trader.ai.engine import ProposedDecision
from trader.ai.ids import derive_decision_id
from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown, RpcRefused
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.schedule import SessionSlots
from trader.ai.store import AiStore
from trader.ai.submitter import SubmissionConflict, Submitter

SIG = "sig-" + "1" * 32


def enter(**changes):
    base = dict(action_key=f"enter:{AAPL}", action="ENTER", conid=AAPL, side="BUY", decider="jev",
                evidence_digest="sha256:" + "c" * 64, deployment_digest="sha256:" + "a" * 64, policy_revision=1,
                stop_price=225.4, target_price=234.6, quantity=3)
    base.update(changes)
    return ProposedDecision(**base)


def close():
    return ProposedDecision(action_key=f"close:{AAPL}", action="CLOSE", conid=AAPL, side="SELL", decider="orchestrator",
                            evidence_digest="sha256:" + "d" * 64)


class Rig:
    def __init__(self, tmp_path, at=et(11, 0), state="ARMED", epoch=1):
        self.clock = FakeClock(at)
        self.store = AiStore(tmp_path / "ai.duckdb", clock=self.clock)
        self.store.migrate(ALL_MIGRATIONS)
        self.trader, self.leadership, self.state = ScriptedTrader(), FakeLeadership(epoch), state
        self.submitter = self.build()

    def build(self, leadership=None):
        return Submitter(store=self.store, supervisor=self.trader, leadership=leadership or self.leadership,
                         clock=self.clock, slots=SessionSlots(), experiment_state=lambda: self.state)

    async def plan(self, decision=None, ttl=300, source=SIG, kind="entry_signal"):
        now = self.clock.now()
        return await self.store.atransaction(lambda conn: self.submitter.insert_in_tx(
            conn, source_kind=kind, source_id=source, decision=decision or enter(),
            expires_at=now + dt.timedelta(seconds=ttl), epoch=self.leadership.last_epoch, now=now))

    async def row(self, decision_id):
        return await self.submitter.get(decision_id)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.mark.asyncio
async def test_the_exact_body_and_id_are_persisted_before_the_send(rig):
    decision_id = await rig.plan()
    assert decision_id == derive_decision_id(SIG, f"enter:{AAPL}")
    seen = {}

    def check(body):
        row = rig.store.db.execute("SELECT state, body_json FROM ai_submissions WHERE decision_id = ?",
                                   [decision_id], fetch="one")
        seen.update(state=row[0], stored=json.loads(row[1]), sent=body)
    rig.trader.on_submit = check
    await rig.submitter.send_due()
    assert seen["state"] == "SENDING" and seen["stored"] == seen["sent"]
    assert seen["sent"]["decision_id"] == decision_id and seen["sent"]["expires_at"] == "2026-07-17T15:05:00+00:00"
    row = await rig.row(decision_id)
    assert (row.state, row.receipt_state, row.last_epoch, row.attempts) == ("ACCEPTED", "SUBMITTED", 1, 1)


@pytest.mark.asyncio
async def test_a_proven_presend_failure_stays_unsent_until_it_expires(rig):
    decision_id = await rig.plan(ttl=60)
    rig.trader.script = [RpcNotSent("TRADER_UNREACHABLE")] * 5
    await rig.submitter.send_due()
    row = await rig.row(decision_id)
    assert (row.state, row.error_code, rig.trader.ledger) == ("PENDING", "TRADER_UNREACHABLE", {})
    rig.clock.advance(56)                                       # 4 s left: below the 5 s send margin
    await rig.submitter.send_due()
    assert (await rig.row(decision_id)).state == "ABANDONED"
    assert (await rig.row(decision_id)).error_code == "EXPIRED_UNSENT"


@pytest.mark.asyncio
async def test_a_lost_reply_is_reconciled_by_the_same_id_without_a_resend(rig):         # Review Focus 2
    decision_id = await rig.plan()
    rig.trader.script = ["accept_lose_reply"]
    await rig.submitter.send_due()
    assert (await rig.row(decision_id)).state == "UNKNOWN"
    await rig.submitter.reconcile_once()
    row = await rig.row(decision_id)
    assert (row.state, row.receipt_state) == ("ACCEPTED", "SUBMITTED")
    assert len(rig.trader.sent) == 1 and rig.trader.reads == [(decision_id, 1)]


@pytest.mark.asyncio
async def test_not_found_after_the_settle_window_resends_the_identical_body(rig):
    decision_id = await rig.plan()
    rig.trader.script = [RpcOutcomeUnknown("REPLY_TIMEOUT")]   # the request never landed
    await rig.submitter.send_due()
    await rig.submitter.reconcile_once()
    assert len(rig.trader.sent) == 1                           # inside the settle window: wait
    rig.clock.advance(120)
    await rig.submitter.reconcile_once()
    assert len(rig.trader.sent) == 2 and rig.trader.sent[0][0] == rig.trader.sent[1][0]
    assert (await rig.row(decision_id)).state == "ACCEPTED"


@pytest.mark.asyncio
async def test_not_found_after_expiry_is_not_admitted(rig):
    decision_id = await rig.plan(ttl=60)
    rig.trader.script = [RpcOutcomeUnknown("REPLY_TIMEOUT")]
    await rig.submitter.send_due()
    rig.clock.advance(121)
    await rig.submitter.reconcile_once()
    row = await rig.row(decision_id)
    assert (row.state, row.error_code, len(rig.trader.sent)) == ("NOT_ADMITTED", "NOT_FOUND_AFTER_EXPIRY", 1)


@pytest.mark.asyncio
async def test_an_epoch_refusal_keeps_the_command_pending_and_drops_leadership(rig):    # Review Focus 3
    decision_id = await rig.plan()
    rig.trader.script = [RpcRefused("CONTROLLER_EPOCH_STALE", "stale")]
    await rig.submitter.send_due()
    row = await rig.row(decision_id)
    assert (row.state, rig.leadership.lost, rig.trader.ledger) == ("PENDING", ["CONTROLLER_EPOCH_STALE"], {})
    await rig.submitter.send_due()
    assert len(rig.trader.sent) == 1                            # no epoch, no send
    rig.leadership.epoch = 2                                    # this process takes the next epoch
    await rig.submitter.send_due()
    assert rig.trader.sent[1] == (rig.trader.sent[0][0], 2) and (await rig.row(decision_id)).last_epoch == 2


@pytest.mark.asyncio
async def test_a_rejected_stale_receipt_is_final_and_never_regenerated(rig):
    decision_id = await rig.plan()
    rig.trader.script = [("receipt", "REJECTED", "CONTROLLER_EPOCH_STALE")]
    await rig.submitter.send_due()
    row = await rig.row(decision_id)
    assert (row.state, row.error_code, rig.leadership.lost) == ("FINAL", "CONTROLLER_EPOCH_STALE",
                                                                ["CONTROLLER_EPOCH_STALE"])
    rig.leadership.epoch = 2
    await rig.submitter.send_due()
    await rig.submitter.reconcile_once()
    assert len(rig.trader.sent) == 1


@pytest.mark.asyncio
async def test_a_restart_turns_sending_into_unknown_and_the_successor_reconciles_under_its_epoch(rig):
    decision_id = await rig.plan()
    rig.store.db.execute("UPDATE ai_submissions SET state = 'SENDING', attempts = 1, last_epoch = 1, "
                         "last_sent_at = ? WHERE decision_id = ?", [rig.clock.now(), decision_id])
    rig.trader.ledger[decision_id] = {"command_id": f"aip-{decision_id}", "correlation_id": "c",
                                      "state": "SUBMITTED", "outcome": None, "error_code": None, "retryable": False}
    successor = rig.build(leadership=FakeLeadership(epoch=2))
    assert await successor.recover() == 1
    assert (await successor.get(decision_id)).error_code == "PROCESS_RESTARTED"
    await successor.reconcile_once()
    row = await successor.get(decision_id)
    assert (row.state, row.created_epoch, row.last_epoch) == ("ACCEPTED", 1, 1)
    assert rig.trader.reads == [(decision_id, 2)] and rig.trader.sent == []


@pytest.mark.asyncio
async def test_an_enter_after_the_cutoff_is_abandoned_unsent_but_a_close_is_sent(tmp_path):
    rig = Rig(tmp_path, at=et(15, 31))
    entry_id = await rig.plan()
    close_id = await rig.plan(close(), kind="position_cycle", source="cyc-position-20260717-1530")
    await rig.submitter.send_due()
    assert (await rig.row(entry_id)).error_code == "OUTSIDE_ENTRY_WINDOW"
    assert (await rig.row(close_id)).state == "ACCEPTED"
    assert [json.loads(s)["action"] for s, _ in rig.trader.sent] == ["CLOSE"]


@pytest.mark.asyncio
async def test_an_unarmed_experiment_holds_an_enter_and_a_stopped_one_abandons_a_close(tmp_path):
    rig = Rig(tmp_path, state="PAUSED")
    entry_id = await rig.plan()
    await rig.submitter.send_due()
    assert (await rig.row(entry_id)).state == "PENDING" and rig.trader.sent == []
    rig.state = "STOPPED"
    close_id = await rig.plan(close(), kind="exit_signal")
    await rig.submitter.send_due()
    assert (await rig.row(close_id)).error_code == "EXPERIMENT_STOPPED"


@pytest.mark.asyncio
async def test_nothing_is_sent_without_leadership(tmp_path):
    rig = Rig(tmp_path, epoch=None)
    rig.leadership.last_epoch = 1
    await rig.plan()
    await rig.submitter.send_due()
    await rig.submitter.reconcile_once()
    assert rig.trader.sent == [] and rig.trader.reads == []


@pytest.mark.asyncio
async def test_the_same_id_with_a_different_body_is_a_conflict(rig):
    await rig.plan()
    with pytest.raises(SubmissionConflict):
        await rig.plan(enter(quantity=4))
    assert await rig.plan() == derive_decision_id(SIG, f"enter:{AAPL}")      # the same body is a no-op


@pytest.mark.asyncio
async def test_a_broken_request_fails_loudly(rig):
    decision_id = await rig.plan()
    rig.trader.script = [RpcRefused("VALIDATION_ERROR", "bad")]
    await rig.submitter.send_due()
    assert ((await rig.row(decision_id)).state, (await rig.row(decision_id)).error_code) == ("FAILED",
                                                                                              "VALIDATION_ERROR")
