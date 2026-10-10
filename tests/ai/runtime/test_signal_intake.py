"""SP2 Plan 5 Task 8: durable signal intake (spec 5.5, amendment 6.1, Ruling 9)."""
import pytest

from tests.ai.fakes import FakeClock
from tests.ai.runtime.fakes import FakeSignals, et
from trader.ai.runtime_schema import ALL_MIGRATIONS, read_cursor, set_cursor_in_tx
from trader.ai.signal_intake import SignalIntake, SignalIntakeError
from trader.ai.store import AiStore


class Rig:
    def __init__(self, tmp_path):
        self.clock = FakeClock(et(11, 0, 30))
        self.store = AiStore(tmp_path / "ai.duckdb", clock=self.clock)
        self.store.migrate(ALL_MIGRATIONS)
        self.signals = FakeSignals()
        self.intake = SignalIntake(store=self.store, supervisor=self.signals, clock=self.clock, page_limit=2)

    def opportunities(self):
        return self.store.db.execute("SELECT opportunity_id, state, reason FROM ai_opportunities "
                                     "ORDER BY signal_cursor", fetch="all")

    def gaps(self):
        return self.store.db.execute("SELECT kind, after_cursor, resumed_cursor FROM ai_coverage_gaps", fetch="all")


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.mark.asyncio
async def test_new_signals_become_opportunities_and_move_the_cursor(rig):
    added = [rig.signals.add() for _ in range(3)]
    assert await rig.intake.poll() == [s["source_event_id"] for s in added[:2]]     # one page of two
    assert await read_cursor(rig.store, "signals") == 2
    assert await rig.intake.poll() == [added[2]["source_event_id"]]
    assert [row[1] for row in rig.opportunities()] == ["NEW"] * 3


@pytest.mark.asyncio
async def test_crash_between_intake_and_cursor_skips_no_signal(rig, monkeypatch):    # Review Focus 4
    added = [rig.signals.add() for _ in range(2)]
    import trader.ai.signal_intake as intake_module

    def crash(*args, **kwargs):
        raise RuntimeError("process died before the cursor moved")
    monkeypatch.setattr(intake_module, "set_cursor_in_tx", crash)
    with pytest.raises(RuntimeError):
        await rig.intake.poll()
    assert rig.opportunities() == [] and await read_cursor(rig.store, "signals") == 0
    monkeypatch.setattr(intake_module, "set_cursor_in_tx", set_cursor_in_tx)
    assert await rig.intake.poll() == [s["source_event_id"] for s in added]


@pytest.mark.asyncio
async def test_a_redelivered_signal_is_not_a_new_opportunity(rig):                  # Review Focus 4
    rig.signals.add()
    await rig.intake.poll()
    await rig.store.atransaction(lambda conn: set_cursor_in_tx(conn, "signals", 0, rig.clock.now()))
    assert await rig.intake.poll() == []
    assert len(rig.opportunities()) == 1


@pytest.mark.asyncio
async def test_a_retention_gap_is_recorded_not_reconstructed(rig):
    for _ in range(5):
        rig.signals.add()
    rig.signals.watermark = 3                                                 # cursors 1-3 were pruned unread
    new = await rig.intake.poll()
    assert len(new) == 2 and rig.gaps() == [("RETENTION", 0, 4)]
    assert len(rig.opportunities()) == 2                                      # only what was actually seen


@pytest.mark.asyncio
async def test_cursor_ahead_records_a_gap_and_restarts_from_zero(rig):
    await rig.store.atransaction(lambda conn: set_cursor_in_tx(conn, "signals", 40, rig.clock.now()))
    rig.signals.add()
    assert await rig.intake.poll() == []
    assert rig.gaps() == [("CURSOR_AHEAD", 40, 0)] and await read_cursor(rig.store, "signals") == 0
    assert len(await rig.intake.poll()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("source_event_id", "sig-xyz"), ("action", "NEUTRAL"), ("conid", True),
                                         ("signal_time", "2026-07-17T15:00:00"), ("cursor", "2")])
async def test_a_malformed_signal_fails_loudly_and_commits_nothing(rig, field, value):
    rig.signals.add()
    rig.signals.add()[field] = value
    with pytest.raises(SignalIntakeError):
        await rig.intake.poll()
    assert rig.opportunities() == [] and await read_cursor(rig.store, "signals") == 0


@pytest.mark.asyncio
async def test_retained_stale_signals_become_missed(rig):
    old = rig.signals.add(at=et(10, 50))
    fresh = rig.signals.add(at=et(10, 59))
    await rig.intake.poll()
    assert await rig.intake.expire_stale() == [old["source_event_id"]]
    assert [(row[0], row[1], row[2]) for row in rig.opportunities()] == [
        (old["source_event_id"], "MISSED", "STALE"), (fresh["source_event_id"], "NEW", None)]


@pytest.mark.asyncio
async def test_the_read_carries_the_held_epoch_from_the_client(rig):
    rig.signals.add()
    await rig.intake.poll()
    assert rig.signals.calls == [(0, None)]          # the epoch is attached by PrincipalClient, not by the intake


@pytest.mark.asyncio
async def test_a_replaced_record_at_the_same_high_water_is_a_coverage_gap(rig):        # PR #84 thread 4210304622
    rig.intake = SignalIntake(store=rig.store, supervisor=rig.signals, clock=rig.clock, page_limit=10)
    old = [rig.signals.add() for _ in range(5)]
    assert await rig.intake.poll() == [s["source_event_id"] for s in old]
    rig.signals.replace_record("gen-" + "b" * 32)
    new = [rig.signals.add(strategy="momentum") for _ in range(5)]                # cursor 5 again
    assert await rig.intake.poll() == []                                          # nothing claimed from this read
    assert rig.gaps() == [("GENERATION_CHANGED", 5, 0)] and await read_cursor(rig.store, "signals") == 0
    assert await rig.intake.poll() == [s["source_event_id"] for s in new]         # from the new record's start
    assert await rig.intake.poll() == [] and len(rig.gaps()) == 1


@pytest.mark.asyncio
async def test_the_first_read_adopts_the_generation_and_a_reset_forgets_it(rig):
    rig.signals.add()
    await rig.intake.poll()
    assert rig.gaps() == []
    await rig.store.atransaction(lambda conn: set_cursor_in_tx(conn, "signals", 40, rig.clock.now()))
    rig.signals.generation = "gen-" + "c" * 32                                  # a reset record answers AHEAD first
    assert await rig.intake.poll() == []
    assert await rig.intake.poll() == []                                          # the same signal: deduplicated
    assert [g[0] for g in rig.gaps()] == ["CURSOR_AHEAD"]                         # one gap, not two
    assert await read_cursor(rig.store, "signals") == 1


@pytest.mark.asyncio
async def test_a_page_without_a_valid_generation_fails_loudly(rig):
    rig.signals.add()
    rig.signals.generation = "not-a-generation"
    with pytest.raises(SignalIntakeError):
        await rig.intake.poll()
    assert rig.opportunities() == [] and await read_cursor(rig.store, "signals") == 0
