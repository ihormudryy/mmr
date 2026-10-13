# tests/strategy/test_authored_live.py
"""SP3 spec 7.6 as amended (live): one session per deployment per New York day for all its conids, a warm-up for
each conid's first bar in a session, an alarm on availability errors, retirement on a fault. Plan 4 rulings 14-18."""
import logging

import numpy as np
import pandas as pd
import pytest

from tests.authored.fake_sandbox import FakeSandbox, caller_source, fault
from trader.objects import Action, BarSize
from trader.sandbox.protocol import SANDBOX_DIGEST_MISMATCH, SANDBOX_ERROR
from trader.strategy.authored_runtime import (DIGEST_ALARM, ISOLATION_ALARM, SANDBOX_FAULT_ALARM,
                                              SANDBOX_UNAVAILABLE_ALARM, AuthoredLiveStrategy)
from trader.trading.strategy import StrategyContext, StrategyState

CONID, OTHER = 265598, 76792991


def bars(count, start="2024-03-04 14:30"):
    index = pd.date_range(start, periods=count, freq="1min", tz="UTC", name="date")
    closes = [100.0 + i for i in range(count)]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000.0},
                        index=index)


def live_sandbox(**kwargs):
    return FakeSandbox(research=False, **kwargs)            # the live pool: 3 slots, a deployment's conids each


def live(sandbox, faults, name="counts_bars", class_name="CountsBars", params=None, conids=(CONID,),
         proven=None, proof_gate=None, now=None):
    digest = sandbox.add(caller_source(name))
    proven = {sandbox.image_digest} if proven is None else proven     # the builds with a passing proof
    strategy = AuthoredLiveStrategy(client=sandbox, digest=digest, class_name=class_name, params=params or {},
                                    bar_size="1 min", conids=conids,
                                    on_fault=lambda d, code: faults.append((d, code)),
                                    proof_gate=proof_gate or (lambda build: build in proven),
                                    **({} if now is None else {"now": now}))
    strategy.install(StrategyContext(name="aidv-test", bar_size=BarSize.Mins1, conids=list(conids), universe=None,
                                     historical_days_prior=5, paper_only=True, storage=None, universe_accessor=None,
                                     logger=logging))
    strategy.ai_source_digest = digest
    strategy.enable()
    strategy._dispatch_conid = CONID
    return strategy


def test_the_first_bar_warms_the_session_and_answers_for_the_newest_only():
    sandbox, faults = live_sandbox(), []
    strategy, frame = live(sandbox, faults), bars(30)
    signal = strategy.on_prices(frame.iloc[:20])
    assert (signal.action, signal.probability) == (Action.BUY, 20 / 10000)
    assert len(sandbox.sessions) == 1 and len(sandbox.sessions[0].stamps) == 20
    assert strategy.on_prices(frame.iloc[:21]).probability == 21 / 10000
    assert len(sandbox.sessions) == 1 and len(sandbox.sessions[0].stamps) == 21


def test_the_conids_of_a_deployment_share_one_session_with_their_own_histories():
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults, conids=(CONID, OTHER))
    strategy._dispatch_conid = CONID
    assert strategy.on_prices(bars(20)).probability == 20 / 10000
    strategy._dispatch_conid = OTHER
    assert strategy.on_prices(bars(5)).probability == 5 / 10000          # OTHER warms with its own bars only
    strategy._dispatch_conid = CONID
    assert strategy.on_prices(bars(21)).probability == 21 / 10000
    assert len(sandbox.sessions) == 1 and sandbox.sessions[0].conids == (CONID, OTHER)
    assert (len(sandbox.sessions[0].stamps_of(CONID)), len(sandbox.sessions[0].stamps_of(OTHER))) == (21, 5)


def test_the_bound_conids_are_the_deployments_before_and_after_install():      # pre-flight F3
    sandbox = live_sandbox()
    strategy = AuthoredLiveStrategy(client=sandbox, digest="sha256:" + "1" * 64, class_name="CountsBars",
                                    params={}, bar_size="1 min", conids=[CONID, OTHER],
                                    on_fault=lambda d, code: None, proof_gate=lambda build: True)
    assert strategy.bound_conids == (CONID, OTHER)
    strategy.install(StrategyContext(name="aidv-test", bar_size=BarSize.Mins1, conids=[CONID], universe=None,
                                     historical_days_prior=5, paper_only=True, storage=None, universe_accessor=None,
                                     logger=logging))
    assert strategy.bound_conids == (CONID, OTHER)


def test_a_conid_the_deployment_does_not_bind_gets_no_signal_and_sends_nothing():
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)
    strategy._dispatch_conid = OTHER
    assert strategy.on_prices(bars(5)) is None
    assert sandbox.sessions == [] and faults == []


def test_no_signal_from_a_session_on_an_unproven_build_and_the_strategy_stays(caplog):     # review r1 #6
    sandbox, faults, proven = live_sandbox(), [], set()
    strategy = live(sandbox, faults, proven=proven)
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(5)) is None                         # the first OPEN: build not proven
    assert ISOLATION_ALARM in caplog.text and sandbox.sessions == [] and faults == []   # never OPENED
    proven.add(sandbox.image_digest)
    assert strategy.on_prices(bars(6)).probability == 6 / 10000           # proven now: a fresh warmed session
    assert strategy.state == StrategyState.RUNNING


def test_a_gate_that_raises_counts_as_unproven(caplog):
    sandbox, faults = live_sandbox(), []

    def broken_gate(build):
        raise RuntimeError("trader unreachable")
    strategy = live(sandbox, faults, proof_gate=broken_gate)
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(5)) is None
    assert ISOLATION_ALARM in caplog.text and sandbox.sessions == [] and faults == []
    assert strategy.state == StrategyState.RUNNING


def test_a_gate_that_raises_mid_session_ends_the_session():
    sandbox, faults = live_sandbox(), []
    answers = iter([True] * 4)                             # the PING build, then the OPENED build before each BAR

    def gate(build):
        try:
            return next(answers)
        except StopIteration:
            raise RuntimeError("trader unreachable") from None
    strategy = live(sandbox, faults, proof_gate=gate)
    assert strategy.on_prices(bars(3)) is not None
    assert strategy.on_prices(bars(4)) is None and sandbox.open_sessions == [] and faults == []


def test_a_reconnect_to_a_rebuilt_unproven_sandbox_gives_no_signal(caplog):              # review r1 #6
    sandbox, faults = live_sandbox(), []
    proven = {sandbox.image_digest}
    strategy = live(sandbox, faults, proven=proven)
    assert strategy.on_prices(bars(3)).probability == 3 / 10000
    sandbox.unavailable = True
    assert strategy.on_prices(bars(4)) is None
    sandbox.unavailable, sandbox.image_digest = False, "e4f5a6b-20261012T090000Z"   # restarted on build B
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(5)) is None
    assert ISOLATION_ALARM in caplog.text and len(sandbox.sessions) == 1 and sandbox.open_sessions == []
    proven.add(sandbox.image_digest)
    assert strategy.on_prices(bars(6)).probability == 6 / 10000 and len(sandbox.sessions) == 2


def test_a_restart_between_ping_and_open_is_caught_by_the_opened_build():
    sandbox, faults = live_sandbox(), []
    proven = {sandbox.image_digest}
    strategy = live(sandbox, faults, proven=proven)
    real_open = sandbox.open_session

    def restart_then_open(*args, **kwargs):
        sandbox.image_digest = "e4f5a6b-20261012T090000Z"                # the supervisor restarted on build B
        return real_open(*args, **kwargs)
    sandbox.open_session = restart_then_open
    assert strategy.on_prices(bars(5)) is None and sandbox.open_sessions == []


def test_a_proof_withdrawn_mid_session_stops_the_signals_at_the_next_bar():
    sandbox, faults = live_sandbox(), []
    proven = {sandbox.image_digest}
    strategy = live(sandbox, faults, proven=proven)
    assert strategy.on_prices(bars(3)) is not None
    proven.clear()                                                          # a later failing run of that build
    assert strategy.on_prices(bars(4)) is None and sandbox.open_sessions == []


def test_each_signal_saw_exactly_the_bars_sent_capped_at_lookback():
    sandbox, faults = live_sandbox(), []
    strategy, frame = live(sandbox, faults), bars(60)
    probabilities = [strategy.on_prices(frame.iloc[:i]).probability for i in range(1, 61)]
    assert probabilities == [min(i, 50) / 10000 for i in range(1, 61)]


def test_bars_skipped_between_calls_are_sent_and_only_the_newest_counts():
    sandbox, faults = live_sandbox(), []
    strategy, frame = live(sandbox, faults), bars(30)
    strategy.on_prices(frame.iloc[:20])
    assert strategy.on_prices(frame.iloc[:23]).probability == 23 / 10000
    assert len(sandbox.sessions[0].stamps) == 23


def test_the_warm_up_sends_the_lookback_opened_reports():                       # #164
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)
    signal = strategy.on_prices(bars(2100))
    assert len(sandbox.sessions[0].stamps) == sandbox.sessions[0].lookback == 50   # CountsBars.LOOKBACK_BARS
    assert signal.probability == 50 / 10000                  # the full lookback decided the last bar
    strategy.on_prices(bars(2101))
    assert len(sandbox.sessions[0].stamps) == 51             # later bars stream one at a time


def test_a_reconnect_gives_the_same_signal_as_no_drop(caplog):
    sandbox, faults = live_sandbox(), []
    strategy, frame = live(sandbox, faults), bars(30)
    strategy.on_prices(frame.iloc[:20])
    sandbox.unavailable = True
    with caplog.at_level(logging.INFO):
        assert strategy.on_prices(frame.iloc[:21]) is None
        assert strategy.on_prices(frame.iloc[:22]) is None
        sandbox.unavailable = False
        signal = strategy.on_prices(frame.iloc[:23])
    assert signal.probability == 23 / 10000 and len(sandbox.sessions) == 2
    assert caplog.text.count(SANDBOX_UNAVAILABLE_ALARM) == 1
    assert "answers again" in caplog.text
    assert strategy.state == StrategyState.RUNNING and faults == []


def test_a_new_new_york_day_opens_a_new_warmed_session():
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)
    frame = pd.concat([bars(10, start="2024-03-04 20:50"), bars(1, start="2024-03-05 14:30")])
    strategy.on_prices(frame.iloc[:10])
    signal = strategy.on_prices(frame)
    assert len(sandbox.sessions) == 2 and sandbox.sessions[0].closed
    assert signal.probability == 11 / 10000


def test_a_conid_still_on_yesterday_does_not_roll_the_session_back():
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults, conids=(CONID, OTHER))
    today = pd.concat([bars(10, start="2024-03-04 20:50"), bars(1, start="2024-03-05 14:30")])
    strategy._dispatch_conid = CONID
    strategy.on_prices(today)                                            # opens the 2024-03-05 session
    strategy._dispatch_conid = OTHER
    assert strategy.on_prices(bars(10, start="2024-03-04 20:50")).probability == 10 / 10000
    assert len(sandbox.sessions) == 1 and not sandbox.sessions[0].closed


def test_a_full_live_pool_is_an_availability_error_and_the_strategy_stays(caplog):
    sandbox, faults = live_sandbox(pool=0), []
    strategy = live(sandbox, faults)
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(5)) is None
    assert SANDBOX_UNAVAILABLE_ALARM in caplog.text
    assert strategy.state == StrategyState.RUNNING and faults == []


def test_a_fault_on_one_conid_stops_every_conid_of_the_deployment():
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults, "faults_above_limit", "FaultsAboveLimit", {"LIMIT": 104.5},
                    conids=(CONID, OTHER))
    strategy._dispatch_conid = OTHER
    assert strategy.on_prices(bars(3)) is None
    strategy._dispatch_conid = CONID
    assert strategy.on_prices(bars(10)) is None                          # close 105 > 104.5: a fault
    assert faults == [(strategy.digest, "SANDBOX_ERROR")] and sandbox.open_sessions == []
    strategy._dispatch_conid = OTHER
    assert strategy.on_prices(bars(4)) is None and len(sandbox.sessions) == 1


def test_a_fault_retires_through_the_callback_and_stops(caplog):
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults, "faults_above_limit", "FaultsAboveLimit", {"LIMIT": 104.5})
    frame = bars(10)
    assert strategy.on_prices(frame.iloc[:3]) is None
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(frame.iloc[:6]) is None              # close 105 > 104.5
    assert faults == [(strategy.digest, SANDBOX_ERROR)]
    assert strategy.state == StrategyState.DISABLED and not strategy.authored_binding_intact()
    assert SANDBOX_FAULT_ALARM in caplog.text and sandbox.open_sessions == []
    sent = len(sandbox.sessions[0].stamps)
    assert strategy.on_prices(frame) is None                            # nothing more is sent, no SELL either
    assert len(sandbox.sessions) == 1 and len(sandbox.sessions[0].stamps) == sent


def test_a_stored_file_that_changed_retires_as_a_digest_fault(caplog):
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)
    sandbox.tampered.add(strategy.digest)
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(3)) is None
    assert faults == [(strategy.digest, SANDBOX_DIGEST_MISMATCH)]
    assert DIGEST_ALARM in caplog.text and strategy.state == StrategyState.DISABLED


def test_the_disable_after_a_fault_may_close_the_sessions_without_a_deadlock():
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults, "faults_above_limit", "FaultsAboveLimit", {"LIMIT": 104.5})
    strategy.disable_after_fault = lambda: (strategy.close_sessions(), faults.append("disabled"))
    assert strategy.on_prices(bars(10)) is None
    assert faults == [(strategy.digest, SANDBOX_ERROR), "disabled"]     # the retirement is noted first


@pytest.mark.parametrize("code", ["SANDBOX_BAD_REQUEST", "SANDBOX_SOMETHING_NEW"])
def test_a_fault_with_another_code_is_an_alarm_not_a_retirement(caplog, code):
    from trader.authored.sandbox_client import SandboxFault
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)
    assert strategy.on_prices(bars(3)) is not None

    def odd_fault(*args, **kwargs):
        sandbox.sessions[0].close()
        raise SandboxFault("odd", code=code)
    sandbox.sessions[0].bar = odd_fault
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(4)) is None
    assert SANDBOX_UNAVAILABLE_ALARM in caplog.text and faults == []
    assert strategy.state == StrategyState.RUNNING and sandbox.open_sessions == []
    assert strategy.on_prices(bars(5)).probability == 5 / 10000 and len(sandbox.sessions) == 2


def test_a_transport_error_is_an_alarm_not_a_retirement(caplog):
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)
    assert strategy.on_prices(bars(3)) is not None

    def broken_pipe(*args, **kwargs):
        raise BrokenPipeError("socket gone")
    sandbox.sessions[0].bar = broken_pipe
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(4)) is None
    assert SANDBOX_UNAVAILABLE_ALARM in caplog.text and faults == []
    assert strategy.state == StrategyState.RUNNING and sandbox.open_sessions == []


def test_a_protocol_error_ends_the_session_with_an_alarm_and_no_retirement(caplog):     # pre-flight F1
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)
    assert strategy.on_prices(bars(3)) is not None
    sandbox.sessions[0].close()                       # the session is gone underneath: the next BAR is refused
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(4)) is None
    assert SANDBOX_UNAVAILABLE_ALARM in caplog.text and "SandboxProtocolError" in caplog.text
    assert strategy.state == StrategyState.RUNNING and faults == []
    assert strategy.on_prices(bars(5)).probability == 5 / 10000 and len(sandbox.sessions) == 2


def test_an_unexpected_error_frees_the_live_slot_and_reaches_the_runtime():
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)
    assert strategy.on_prices(bars(3)) is not None

    def bug(*args, **kwargs):
        raise KeyError("a bug in this process")
    sandbox.sessions[0].bar = bug
    with pytest.raises(KeyError):
        strategy.on_prices(bars(4))
    assert sandbox.open_sessions == [] and faults == []


def test_a_nan_newest_bar_gives_no_signal_and_keeps_the_session():
    sandbox, faults = live_sandbox(), []
    strategy, frame = live(sandbox, faults), bars(21)
    frame.iloc[-1, frame.columns.get_loc("volume")] = np.nan
    strategy.on_prices(frame.iloc[:20])
    assert strategy.on_prices(frame) is None
    session = sandbox.sessions[0]
    assert len(sandbox.sessions) == 1 and not session.closed and len(session.stamps) == 20


@pytest.mark.parametrize("column, value", [("low", 125.0), ("volume", -1.0), ("close", np.inf)])
def test_a_newest_bar_the_protocol_refuses_is_skipped_and_keeps_the_session(column, value):     # pre-flight F1
    sandbox, faults = live_sandbox(), []
    strategy, frame = live(sandbox, faults), bars(21)
    frame.iloc[-1, frame.columns.get_loc(column)] = value                 # low 125 > high 120; negative; inf
    strategy.on_prices(frame.iloc[:20])
    assert strategy.on_prices(frame) is None
    session = sandbox.sessions[0]
    assert len(sandbox.sessions) == 1 and not session.closed and len(session.stamps) == 20
    assert strategy.state == StrategyState.RUNNING and faults == []


def test_a_bad_bar_inside_the_warm_up_is_skipped():
    sandbox, faults = live_sandbox(), []
    strategy, frame = live(sandbox, faults), bars(20)
    frame.iloc[5, frame.columns.get_loc("high")] = 1.0                     # high < low
    assert strategy.on_prices(frame).probability == 19 / 10000
    assert len(sandbox.sessions[0].stamps) == 19


def test_a_newest_bar_after_2100_is_skipped_and_does_not_pin_the_day():        # pre-flight F1
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)
    today = bars(5)
    strategy.on_prices(today)
    assert strategy.on_prices(pd.concat([today, bars(1, start="2101-01-03 14:30")])) is None
    assert len(sandbox.sessions) == 1 and not sandbox.sessions[0].closed and len(sandbox.sessions[0].stamps) == 5
    tomorrow = pd.concat([today, bars(1, start="2024-03-05 14:30")])
    assert strategy.on_prices(tomorrow).probability == 6 / 10000          # the next New York day still rolls
    assert len(sandbox.sessions) == 2 and sandbox.sessions[0].closed


def test_a_warm_up_bar_before_1970_is_skipped():                                # pre-flight F1
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)
    frame = pd.concat([bars(1, start="1969-12-31 14:30"), bars(5)])
    assert strategy.on_prices(frame).probability == 5 / 10000
    assert len(sandbox.sessions[0].stamps) == 5


def test_closing_the_sessions_is_final():                                       # review fix round 1
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)
    assert strategy.on_prices(bars(3)) is not None
    strategy.close_sessions()                         # the reconcile thread unloads it
    assert strategy.on_prices(bars(4)) is None        # a tick thread that still held it
    assert sandbox.open_sessions == [] and len(sandbox.sessions) == 1
    assert not strategy.authored_binding_intact()


def test_a_bar_dated_after_today_is_skipped_and_does_not_pin_the_session(caplog):     # review fix round 1
    sandbox, faults = live_sandbox(), []
    today = pd.Timestamp("2024-03-04 15:00", tz="UTC").to_pydatetime()
    strategy = live(sandbox, faults, conids=(CONID, OTHER), now=lambda: today)
    strategy._dispatch_conid = CONID
    assert strategy.on_prices(bars(10)).probability == 10 / 10000
    strategy._dispatch_conid = OTHER
    with caplog.at_level(logging.WARNING):
        assert strategy.on_prices(bars(5, start="2024-03-05 14:30")) is None
        assert strategy.on_prices(bars(6, start="2024-03-05 14:30")) is None
    assert caplog.text.count("after today") == 1                       # once per conid per day
    assert len(sandbox.sessions) == 1 and not sandbox.sessions[0].closed
    assert sandbox.sessions[0].stamps_of(OTHER) == []
    strategy._dispatch_conid = CONID
    assert strategy.on_prices(bars(11)).probability == 11 / 10000      # same session, still today's
    assert len(sandbox.sessions) == 1


def test_an_outage_across_a_skipped_bar_alarms_once_and_never_recovers_falsely(caplog):   # review fix round 1
    sandbox, faults = live_sandbox(), []
    strategy, frame = live(sandbox, faults), bars(23)
    frame.iloc[21, frame.columns.get_loc("close")] = np.nan
    strategy.on_prices(frame.iloc[:20])
    sandbox.unavailable = True
    with caplog.at_level(logging.INFO):
        assert strategy.on_prices(frame.iloc[:21]) is None
        assert strategy.on_prices(frame.iloc[:22]) is None             # a NaN newest bar: the sandbox is not asked
        assert strategy.on_prices(frame.iloc[:23]) is None
    assert caplog.text.count(SANDBOX_UNAVAILABLE_ALARM) == 1
    assert "answers again" not in caplog.text


def test_a_fault_whose_close_raises_still_retires_and_disables():              # review fix round 1
    from trader.authored.sandbox_client import SandboxFault, SandboxProtocolError
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)
    assert strategy.on_prices(bars(3)) is not None
    session = sandbox.sessions[0]

    def faulting_bar(*args, **kwargs):
        raise SandboxFault("boom", code=SANDBOX_ERROR)

    def broken_close():
        raise SandboxProtocolError("close failed")
    session.bar, session.close = faulting_bar, broken_close
    assert strategy.on_prices(bars(4)) is None
    assert faults == [(strategy.digest, SANDBOX_ERROR)] and strategy.state == StrategyState.DISABLED
    assert strategy.on_prices(bars(5)) is None and len(sandbox.sessions) == 1


def test_closing_the_sessions_frees_the_slots():
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)
    strategy.on_prices(bars(3))
    strategy.close_sessions()
    assert sandbox.open_sessions == []


def test_the_binding_is_the_deployments_digest():
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)
    assert strategy.authored_binding_intact()
    strategy.ai_source_digest = "sha256:" + "0" * 64
    assert not strategy.authored_binding_intact()


# Final review I1: OPEN answers before any session reported a build, so the build is read again on a fault there.

REBUILT = "e4f5a6b-20261012T090000Z"


def test_an_open_fault_on_a_rebuilt_sandbox_is_an_outage_not_a_retirement(caplog):
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults, proven={sandbox.image_digest})

    def restart_then_refuse(*args, **kwargs):
        sandbox.image_digest = REBUILT                       # restarted on build B, never proven
        raise sandbox.open_fault(SANDBOX_ERROR)              # B's checker refuses the stored file at OPEN
    sandbox.open_session = restart_then_refuse
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(5)) is None
    assert faults == [] and SANDBOX_UNAVAILABLE_ALARM in caplog.text
    assert strategy.state == StrategyState.RUNNING and strategy.authored_binding_intact()


def test_an_open_fault_that_names_no_build_is_an_outage_not_a_retirement(caplog):
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)

    def refuse_without_a_build(*args, **kwargs):
        raise fault(SANDBOX_DIGEST_MISMATCH)                 # an older sandbox: its ERROR names no build
    sandbox.open_session = refuse_without_a_build
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(5)) is None
    assert faults == [] and SANDBOX_UNAVAILABLE_ALARM in caplog.text and strategy.state == StrategyState.RUNNING


def test_an_open_fault_on_the_same_build_still_retires(caplog):
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)

    def refuse(*args, **kwargs):
        raise sandbox.open_fault(SANDBOX_ERROR)
    sandbox.open_session = refuse
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(5)) is None
    assert faults == [(strategy.digest, SANDBOX_ERROR)] and SANDBOX_FAULT_ALARM in caplog.text
    assert strategy.state == StrategyState.DISABLED


def test_an_open_fault_from_build_b_between_two_pings_of_a_is_an_outage(caplog):              # PR #161 review
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults, proven={sandbox.image_digest})
    build_a = sandbox.image_digest

    def b_answers_then_a_is_back(*args, **kwargs):
        sandbox.image_digest = REBUILT
        refused = sandbox.open_fault(SANDBOX_ERROR)           # B refuses the OPEN
        sandbox.image_digest = build_a                        # A restored before any later PING
        raise refused
    sandbox.open_session = b_answers_then_a_is_back
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(5)) is None
    assert faults == [] and SANDBOX_UNAVAILABLE_ALARM in caplog.text and strategy.state == StrategyState.RUNNING


def test_a_fault_answered_to_ping_is_an_outage_not_a_retirement(caplog):
    sandbox, faults = live_sandbox(), []
    strategy = live(sandbox, faults)

    def faulting_ping():
        raise fault(SANDBOX_ERROR)
    sandbox.ping = faulting_ping
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(5)) is None
    assert faults == [] and SANDBOX_UNAVAILABLE_ALARM in caplog.text and strategy.state == StrategyState.RUNNING


def test_a_proof_withdrawn_after_the_first_bar_of_a_frame_stops_the_frame():                 # PR #161 review
    sandbox, faults = live_sandbox(), []
    answers = iter([True, True])                    # the PING build, then the OPENED build before BAR 1 only

    def gate(build):
        return next(answers, False)
    strategy = live(sandbox, faults, proof_gate=gate)
    assert strategy.on_prices(bars(3)) is None
    assert len(sandbox.sessions[0].stamps_of(CONID)) == 1                  # BAR 2 and BAR 3 were never sent
    assert sandbox.open_sessions == [] and faults == []
