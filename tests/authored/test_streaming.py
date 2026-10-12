import datetime as dt
import math

import pandas as pd
import pytest

from tests.authored.fake_sandbox import FakeSandbox, caller_source, fault, orb_fixture_source
from trader.authored.api import Decision
from trader.authored.sandbox_client import SandboxFault, SandboxProtocolError, SandboxUnavailable
from trader.authored.static_check import require_clean
from trader.authored.streaming import AUTHORED_SIGNAL_RISK, BarStream, bar_fields, decision_signal
from trader.objects import Action
from trader.sandbox.protocol import SANDBOX_DIGEST_MISMATCH, SANDBOX_ERROR

UTC = dt.timezone.utc
CONID, OTHER = 265598, 76792991
NOON = pd.Timestamp("2024-03-04 14:30", tz="UTC")


def bars(count, start="2024-03-04 14:30", close=100.0):
    index = pd.date_range(start, periods=count, freq="1min", tz="UTC", name="date")
    closes = [close + i for i in range(count)]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000.0},
                        index=index)


def session_of(sandbox, name, class_name, params=None, conids=(CONID,)):
    return sandbox.open_session(sandbox.add(caller_source(name)), class_name, params or {}, "1 min", conids)


def test_bar_fields_are_floats_at_a_utc_time():
    fields = bar_fields(pd.Timestamp("2024-03-04 14:30"), (1, 2, 0.5, 1.5, 100))
    assert fields == {"t": dt.datetime(2024, 3, 4, 14, 30, tzinfo=UTC), "open": 1.0, "high": 2.0, "low": 0.5,
                      "close": 1.5, "volume": 100.0}


@pytest.mark.parametrize("values", [(1, 2, 0.5, math.nan, 100), (1, 2, 0.5, 1.5, None), (1, 2, 0.5, 1.5, math.inf),
                                    (1, 2, 0.5, 1.5)])
def test_a_bar_with_a_missing_value_is_not_sent(values):
    assert bar_fields(NOON, values) is None


def test_a_bar_with_high_below_low_is_not_sent():
    assert bar_fields(NOON, (1, 0.5, 2, 1.5, 100)) is None


def test_a_bar_with_negative_volume_is_not_sent():
    assert bar_fields(NOON, (1, 2, 0.5, 1.5, -1)) is None


@pytest.mark.parametrize("stamp", ["1969-12-31 23:59", "2100-01-01 00:00", pd.NaT])
def test_a_bar_outside_1970_to_2100_is_not_sent(stamp):
    assert bar_fields(pd.Timestamp(stamp, tz="UTC") if stamp is not pd.NaT else stamp, (1, 2, 0.5, 1.5, 100)) is None


def test_a_bar_at_the_edges_of_the_time_range_is_sent():
    assert bar_fields(pd.Timestamp("1970-01-01", tz="UTC"), (1, 2, 0.5, 1.5, 0)) is not None
    assert bar_fields(pd.Timestamp("2099-12-31 23:59", tz="UTC"), (1, 1, 1, 1, 0)) is not None


def test_a_bar_not_after_the_last_sent_is_not_sent():
    sandbox = FakeSandbox()
    stream = BarStream(session_of(sandbox, "counts_bars", "CountsBars"), CONID)
    frame = bars(2)
    assert stream.send_frame(frame)[-1].probability == 2 / 10000
    assert stream.send(frame.index[0], (1, 1, 1, 1, 1)) is None
    assert len(sandbox.sessions[0].stamps) == 2


def test_a_bar_the_protocol_refuses_never_reaches_the_session():
    sandbox = FakeSandbox()
    stream = BarStream(session_of(sandbox, "counts_bars", "CountsBars"), CONID)
    assert stream.send(NOON, (1, 0.5, 2, 1.5, 100)) is None
    assert sandbox.sessions[0].stamps == []


def test_the_history_is_the_bars_sent_capped_at_lookback():
    sandbox = FakeSandbox()
    decisions = BarStream(session_of(sandbox, "counts_bars", "CountsBars"), CONID).send_frame(bars(60))
    assert [d.probability for d in decisions] == [min(i + 1, 50) / 10000 for i in range(60)]


def test_two_conids_of_one_session_keep_their_own_history_and_clock():
    sandbox = FakeSandbox()
    session = session_of(sandbox, "counts_bars", "CountsBars", conids=(CONID, OTHER))
    first, second = BarStream(session, CONID), BarStream(session, OTHER)
    assert first.send_frame(bars(30))[-1].probability == 30 / 10000
    assert second.send_frame(bars(5))[-1].probability == 5 / 10000      # same times as CONID's first bars
    assert len(sandbox.sessions) == 1 and session.conids == (CONID, OTHER)
    assert len(session.stamps_of(CONID)) == 30 and len(session.stamps_of(OTHER)) == 5


def test_a_conid_outside_the_session_is_a_protocol_error():
    sandbox = FakeSandbox()
    stream = BarStream(session_of(sandbox, "counts_bars", "CountsBars"), OTHER)
    with pytest.raises(SandboxProtocolError):
        stream.send_frame(bars(1))


def test_a_fault_ends_the_session_and_frees_its_slot():
    sandbox = FakeSandbox(pool=1)
    stream = BarStream(session_of(sandbox, "faults_above_limit", "FaultsAboveLimit", {"LIMIT": 101.5}), CONID)
    with pytest.raises(SandboxFault) as raised:
        stream.send_frame(bars(5))
    assert raised.value.code == SANDBOX_ERROR
    assert sandbox.open_sessions == []
    stream.close()                                   # closing a dead session is not an error


def test_a_full_pool_is_refused_at_once():
    sandbox = FakeSandbox(pool=1)
    session_of(sandbox, "counts_bars", "CountsBars")
    with pytest.raises(SandboxUnavailable):
        session_of(sandbox, "counts_bars", "CountsBars")


@pytest.mark.parametrize("action, expected", [("BUY", Action.BUY), ("SELL", Action.SELL)])
def test_a_decision_becomes_a_signal_without_risk(action, expected):
    signal = decision_signal(Decision(action, 0.25), "aidv-1")
    assert (signal.source_name, signal.action, signal.probability, signal.risk) == (
        "aidv-1", expected, 0.25, AUTHORED_SIGNAL_RISK)
    assert signal.close_by_time is None
    assert decision_signal(None, "aidv-1") is None


# The fake is as strict as Plan 2's client (pre-flight F2).

def test_a_call_on_a_closed_session_is_a_protocol_error():
    sandbox = FakeSandbox()
    session = session_of(sandbox, "counts_bars", "CountsBars")
    session.close()
    with pytest.raises(SandboxProtocolError):
        session.bar(CONID, NOON.to_pydatetime(), 1, 1, 1, 1, 1)


def test_the_fake_runs_the_protocol_parser_on_every_bar():
    session = session_of(FakeSandbox(), "counts_bars", "CountsBars")
    with pytest.raises(SandboxProtocolError):
        session.bar(CONID, NOON.to_pydatetime(), 1, 0.5, 2, 1, 1)            # high below low
    with pytest.raises(SandboxProtocolError):
        session.bar(CONID, NOON.to_pydatetime(), 1, 2, 1, 1, -1)             # negative volume
    with pytest.raises(SandboxProtocolError):
        session.bar(CONID, dt.datetime(2024, 3, 4, 14, 30), 1, 1, 1, 1, 1)   # naive time
    assert session.stamps == []
    assert not session.closed                                                 # a refused bar leaves it usable


def test_a_bar_not_after_the_last_is_a_protocol_error_that_keeps_the_session():
    session = session_of(FakeSandbox(), "counts_bars", "CountsBars")
    session.bar(CONID, NOON.to_pydatetime(), 1, 1, 1, 1, 1)
    with pytest.raises(SandboxProtocolError):
        session.bar(CONID, NOON.to_pydatetime(), 1, 1, 1, 1, 1)
    assert not session.closed


@pytest.mark.parametrize("bar_size, conids", [("7 min", (CONID,)), ("1 min", ()), ("1 min", (CONID, CONID)),
                                              ("1 min", (0,))])
def test_the_fake_runs_the_protocol_parser_on_every_open(bar_size, conids):
    sandbox = FakeSandbox()
    digest = sandbox.add(caller_source("counts_bars"))
    with pytest.raises(SandboxProtocolError):
        sandbox.open_session(digest, "CountsBars", {}, bar_size, conids)
    assert sandbox.sessions == []


def test_an_unknown_digest_is_a_digest_mismatch():
    sandbox = FakeSandbox()
    with pytest.raises(SandboxFault) as raised:
        sandbox.open_session("sha256:" + "0" * 64, "CountsBars", {}, "1 min", (CONID,))
    assert raised.value.code == SANDBOX_DIGEST_MISMATCH


def test_a_tampered_file_is_a_digest_mismatch():
    sandbox = FakeSandbox()
    digest = sandbox.add(caller_source("counts_bars"))
    sandbox.tampered.add(digest)
    with pytest.raises(SandboxFault) as raised:
        sandbox.open_session(digest, "CountsBars", {}, "1 min", (CONID,))
    assert raised.value.code == SANDBOX_DIGEST_MISMATCH


def test_a_dead_socket_ends_the_session_and_every_call_after_it():
    sandbox = FakeSandbox()
    session = session_of(sandbox, "counts_bars", "CountsBars")
    sandbox.unavailable = True
    with pytest.raises(SandboxUnavailable):
        session.bar(CONID, NOON.to_pydatetime(), 1, 1, 1, 1, 1)
    sandbox.unavailable = False
    with pytest.raises(SandboxProtocolError):
        session.bar(CONID, NOON.to_pydatetime(), 1, 1, 1, 1, 1)
    assert sandbox.open_sessions == []


def test_opens_are_refused_after_the_configured_count():
    sandbox = FakeSandbox()
    sandbox.refuse_opens_after = 1
    session_of(sandbox, "counts_bars", "CountsBars")
    with pytest.raises(SandboxUnavailable):
        session_of(sandbox, "counts_bars", "CountsBars")


def test_the_fake_reports_the_peak_of_open_sessions_and_the_build():
    sandbox = FakeSandbox(pool=2)
    first, second = (session_of(sandbox, "counts_bars", "CountsBars") for _ in range(2))
    first.close()
    session_of(sandbox, "counts_bars", "CountsBars")
    assert sandbox.most_open == 2
    assert first.image_digest == sandbox.ping().image_digest
    assert sandbox.ping().free == 0 and second in sandbox.open_sessions


def test_every_fixture_source_passes_the_static_check():
    for name, class_name in (("counts_bars", "CountsBars"), ("faults_above_limit", "FaultsAboveLimit"),
                             ("time_of_day_port", "TimeOfDayPort")):
        assert require_clean(caller_source(name)) == class_name
    assert require_clean(orb_fixture_source()) == "OpeningRangeBreakout"


def test_a_strategy_that_returns_a_bad_decision_faults_the_session():
    sandbox = FakeSandbox()
    stream = BarStream(session_of(sandbox, "faults_above_limit", "FaultsAboveLimit", {"LIMIT": 100.5}), CONID)
    with pytest.raises(SandboxFault):
        stream.send_frame(bars(3))                       # probability 2.0 is out of range
    assert sandbox.open_sessions == [] and fault(SANDBOX_ERROR).code == SANDBOX_ERROR
