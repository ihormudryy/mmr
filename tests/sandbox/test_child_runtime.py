import os
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trader.authored.allowed_api import ALLOWED_BUILTINS
from trader.sandbox import child as child_module
from trader.sandbox.child import (ChildError, ChildRuntime, build_guarded_builtins, guarded_import,
                                  load_authored_class)
from trader.sandbox.protocol import MAX_FRAME_BYTES

T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
C1, C2 = 101, 202
BAR = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}

GOOD = '''
import numpy as np
from trader.authored.api import AuthoredStrategy, Decision


class Counter(AuthoredStrategy):
    LOOKBACK_BARS = 3

    def on_bar(self, history, params):
        if len(history) >= int(params["N"]):
            return Decision(action="BUY", probability=float(min(max(len(history) / 10.0, 0.0), 1.0)))
        return None
'''


def _runtime(source=GOOD, params=None, conids=(C1,)):
    return ChildRuntime(source=source, class_name="Counter",
                        params=params or {"N": 2.0}, lookback=3, conids=conids)


def _bars(runtime, count, conid=C1):
    return [runtime.on_bar(conid, T0 + timedelta(minutes=i), BAR) for i in range(count)]


def test_a_good_strategy_loads_and_signals():
    runtime = _runtime()
    runtime.load()
    signals = _bars(runtime, 3)
    assert signals[0] == {"action": None, "probability": None}
    assert signals[1]["action"] == "BUY" and 0.0 <= signals[1]["probability"] <= 1.0


def test_history_is_trimmed_to_lookback():
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Counter(AuthoredStrategy):
    LOOKBACK_BARS = 3

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=min(len(history) / 100.0, 1.0))
'''
    runtime = ChildRuntime(source=source, class_name="Counter", params={}, lookback=3, conids=(C1,))
    runtime.load()
    signals = _bars(runtime, 10)
    assert signals[-1]["probability"] == pytest.approx(3 / 100.0)  # never more than LOOKBACK_BARS


def test_each_conid_has_its_own_history_and_instance():
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Counter(AuthoredStrategy):
    LOOKBACK_BARS = 50

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=min(len(history) / 100.0, 1.0))
'''
    runtime = ChildRuntime(source=source, class_name="Counter", params={}, lookback=50, conids=(C1, C2))
    runtime.load()
    _bars(runtime, 5, conid=C1)
    first_c2 = runtime.on_bar(C2, T0, BAR)          # C2's own clock: an earlier time than C1's last is fine
    assert first_c2["probability"] == pytest.approx(1 / 100.0)   # C2 never sees C1's bars
    assert runtime.on_bar(C1, T0 + timedelta(minutes=5), BAR)["probability"] == pytest.approx(6 / 100.0)


def test_a_conid_outside_the_session_is_a_fault():
    runtime = _runtime()
    runtime.load()
    with pytest.raises(ChildError):
        runtime.on_bar(C2, T0, BAR)


def test_a_bar_time_that_does_not_increase_is_a_fault():
    runtime = _runtime()
    runtime.load()
    runtime.on_bar(C1, T0, BAR)
    with pytest.raises(ChildError):
        runtime.on_bar(C1, T0, BAR)


def test_an_exception_in_on_bar_is_a_child_error():
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Boom(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=1.0 / (len(history) - 1))
'''
    runtime = ChildRuntime(source=source, class_name="Boom", params={}, lookback=2, conids=(C1,))
    runtime.load()
    with pytest.raises(ChildError):
        runtime.on_bar(C1, T0, BAR)


def test_a_non_decision_return_is_a_child_error():
    source = '''
from trader.authored.api import AuthoredStrategy


class Bad(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        return "BUY"
'''
    runtime = ChildRuntime(source=source, class_name="Bad", params={}, lookback=2, conids=(C1,))
    runtime.load()
    with pytest.raises(ChildError):
        runtime.on_bar(C1, T0, BAR)


def test_a_probability_out_of_range_is_a_child_error():
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Over(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=2.0)
'''
    runtime = ChildRuntime(source=source, class_name="Over", params={}, lookback=2, conids=(C1,))
    runtime.load()
    with pytest.raises(ChildError):
        runtime.on_bar(C1, T0, BAR)


def test_the_guarded_builtins_are_exactly_plan_1s_allow_list():
    # Ruling 31: built from ALLOWED_BUILTINS, never a wider list of the child's own.
    guarded = build_guarded_builtins()
    assert set(guarded) == set(ALLOWED_BUILTINS) | {"__import__", "__build_class__"}
    for name in ("eval", "exec", "open", "compile", "input", "breakpoint", "getattr",
                 "iter", "next", "map", "filter", "issubclass", "type", "ValueError", "Exception"):
        assert name not in guarded
    assert guarded["__import__"] is guarded_import


@pytest.mark.parametrize("name, fromlist", [
    ("typing", ()), ("dataclasses", ()), ("typing", ("operator",)), ("typing", ("Mapping", "sys")),
    ("numpy", ("mean",)), ("trader.authored.api", ("pd",)), ("trader.authored.api", ()), ("os", ()),
    ("typing", ("*",)),
])
def test_the_import_guard_follows_plan_1s_import_lists(name, fromlist):
    with pytest.raises(ChildError, match="not allowed"):
        guarded_import(name, None, None, fromlist, 0)


@pytest.mark.parametrize("name, fromlist", [
    ("numpy", ()), ("pandas", None), ("math", ()), ("statistics", ()), ("typing", ("Mapping",)),
    ("dataclasses", ("dataclass", "field")), ("trader.authored.api", ("AuthoredStrategy", "Decision")),
])
def test_the_import_guard_admits_what_the_checker_admits(name, fromlist):
    assert guarded_import(name, None, None, fromlist, 0) is not None


def test_a_disallowed_import_at_module_load_is_blocked():
    source = '''
import os
from trader.authored.api import AuthoredStrategy, Decision


class Sneaky(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        return None
'''
    # The static checker refuses it first; even with that bypassed the import guard blocks it.
    with pytest.raises(ChildError):
        load_authored_class(source, "Sneaky", skip_static_check=True)


def test_a_runtime_import_of_a_blocked_module_is_blocked():
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Late(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        mod = __import__("socket")
        return None
'''
    runtime = ChildRuntime(source=source, class_name="Late", params={}, lookback=2, conids=(C1,),
                           skip_static_check=True)
    runtime.load()
    with pytest.raises(ChildError):
        runtime.on_bar(C1, T0, BAR)


def test_the_benign_api_class_loads_and_answers_one_bar():
    # Review r1 #1: a class statement needs __build_class__, and `from trader.authored.api import ...` must pass
    # the guard by its exact module name and ALLOWED_FROM_IMPORT_NAMES, although no root `trader` is importable.
    source = '''
import numpy as np
from trader.authored.api import AuthoredStrategy, Decision


class Benign(AuthoredStrategy):
    LOOKBACK_BARS = 5

    def on_bar(self, history, params):
        if np.mean(history["close"]) > 0:
            return Decision(action="BUY", probability=min(len(history) / 10.0, 1.0))
        return None
'''
    runtime = ChildRuntime(source=source, class_name="Benign", params={}, lookback=5, conids=(C1,))
    runtime.load()
    assert runtime.on_bar(C1, T0, BAR) == {"action": "BUY", "probability": 0.1}


@pytest.mark.parametrize("statement", ["from trader.sandbox import protocol", "import trader.authored.api",
                                       "from trader.authored import keys", "from trader import authored",
                                       "import trader"])
def test_every_other_trader_import_is_refused(statement):
    source = f"""
{statement}
from trader.authored.api import AuthoredStrategy, Decision


class Sneaky(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        return None
"""
    with pytest.raises(ChildError, match="not allowed"):
        load_authored_class(source, "Sneaky", skip_static_check=True)


# ── beyond the brief: the ORB fixture, hardening of the guard and the output, the fork plumbing ──

ORB_SOURCE = (Path(__file__).parents[1] / "authored" / "fixtures" / "orb_authored.py").read_text()
ORB_PARAMS = {"RANGE_MINUTES": 30.0, "VOLUME_MULT": 1.5, "RTH_OPEN_MIN": 570.0,
              "RTH_CLOSE_MIN": 960.0, "VOLUME_SMA_BARS": 20.0, "MIN_BARS": 40.0}


def _orb_session(n=81, breakout_at=80, base=100.0):
    """One session of 1-min UTC bars from 09:30 ET: a flat open range, then a volume-backed break up."""
    index = pd.date_range(pd.Timestamp("2024-01-02 14:30:00", tz="UTC"), periods=n, freq="1min")
    close = np.full(n, base)
    close[breakout_at:] = base + 2.0
    volume = np.full(n, 1000.0)
    volume[breakout_at] = 10_000.0
    return [(when.to_pydatetime(), {"open": c, "high": c + 0.25, "low": c - 0.25, "close": c, "volume": v})
            for when, c, v in zip(index, close, volume)]


def test_the_orb_fixture_loads_with_the_static_check_and_runs_in_the_child_runtime():
    runtime = ChildRuntime(source=ORB_SOURCE, class_name="OpeningRangeBreakout", params=ORB_PARAMS,
                           lookback=420, conids=(C1,))
    runtime.load()
    signals = [runtime.on_bar(C1, when, bar) for when, bar in _orb_session()]
    assert all(signal == {"action": None, "probability": None} for signal in signals[:-1])
    assert signals[-1] == {"action": "BUY", "probability": 0.6}


def test_the_history_index_is_utc_and_named_date():
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Probe(AuthoredStrategy):
    LOOKBACK_BARS = 3

    def on_bar(self, history, params):
        if history.index.name == "date" and str(history.index.tz) == "UTC":
            return Decision(action="BUY", probability=1.0)
        return None
'''
    runtime = ChildRuntime(source=source, class_name="Probe", params={}, lookback=3, conids=(C1,))
    runtime.load()
    eastern = timezone(timedelta(hours=-5))
    assert runtime.on_bar(C1, T0.astimezone(eastern), BAR) == {"action": "BUY", "probability": 1.0}


def test_a_bar_time_without_an_offset_is_a_fault():
    runtime = _runtime()
    runtime.load()
    with pytest.raises(ChildError, match="UTC offset"):
        runtime.on_bar(C1, T0.replace(tzinfo=None), BAR)


@pytest.mark.parametrize("ohlcv", [
    {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
    {**BAR, "close": float("nan")},
    {**BAR, "volume": "100"},
    {**BAR, "open": True},
])
def test_a_bad_bar_is_a_fault(ohlcv):
    runtime = _runtime()
    runtime.load()
    with pytest.raises(ChildError):
        runtime.on_bar(C1, T0, ohlcv)


def test_on_bar_before_load_is_a_fault():
    with pytest.raises(ChildError, match="not loaded"):
        _runtime().on_bar(C1, T0, BAR)


def test_params_cannot_be_changed_by_the_strategy():
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Mutator(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        params["N"] = 99.0
        return None
'''
    runtime = ChildRuntime(source=source, class_name="Mutator", params={"N": 1.0}, lookback=2, conids=(C1,),
                           skip_static_check=True)
    runtime.load()
    with pytest.raises(ChildError):
        runtime.on_bar(C1, T0, BAR)


def test_a_decision_subclass_is_a_fault():
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Other(Decision):
    pass


class Sub(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        return Other(action="BUY", probability=0.5)
'''
    runtime = ChildRuntime(source=source, class_name="Sub", params={}, lookback=2, conids=(C1,),
                           skip_static_check=True)
    runtime.load()
    with pytest.raises(ChildError, match="Decision"):
        runtime.on_bar(C1, T0, BAR)


def test_a_numpy_probability_is_returned_as_a_plain_float():
    source = '''
import numpy as np
from trader.authored.api import AuthoredStrategy, Decision


class Np(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        return Decision(action="SELL", probability=np.float64(0.25))
'''
    runtime = ChildRuntime(source=source, class_name="Np", params={}, lookback=2, conids=(C1,))
    runtime.load()
    signal = runtime.on_bar(C1, T0, BAR)
    assert signal == {"action": "SELL", "probability": 0.25} and type(signal["probability"]) is float


def test_the_class_name_must_match_the_files_class():
    with pytest.raises(ChildError, match="does not match"):
        load_authored_class(GOOD, "Other")


def test_a_static_rejection_names_its_rule():
    with pytest.raises(ChildError, match="AUTHORED_STATIC_REJECTED:"):
        load_authored_class(GOOD.replace("return None", "return open"), "Counter")


def test_authored_code_compiles_without_the_runtimes_future_flags():
    # child.py has `from __future__ import annotations`; the authored file must not inherit it.
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Ann(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params) -> Decision:
        return None
'''
    cls = load_authored_class(source, "Ann")
    assert cls.on_bar.__annotations__["return"] is not None
    assert not isinstance(cls.on_bar.__annotations__["return"], str)


class _LyingStr(str):
    """Hashes and compares like "numpy" while the real import reads its true text ("os" passes a set lookup)."""

    def __eq__(self, other):
        return other == "numpy" or str.__eq__(self, other)

    def __hash__(self):
        return hash("numpy")


@pytest.mark.parametrize("name, fromlist, level", [
    (_LyingStr("os"), (), 0),
    ("typing", (_LyingStr("Mapping"),), 0),
    ("typing", ["Mapping"], 1),
    ("typing", 7, 0),
    ("numpy", (), True),
    (b"numpy", (), 0),
])
def test_the_import_guard_takes_only_plain_str_names(name, fromlist, level):
    with pytest.raises(ChildError, match="not allowed"):
        guarded_import(name, None, None, fromlist, level)


def test_a_from_import_hands_back_only_the_named_names():
    bound = guarded_import("typing", None, None, ("Mapping",), 0)
    assert bound.Mapping is __import__("typing").Mapping
    assert not hasattr(bound, "sys")


def test_a_from_import_of_a_name_the_module_lacks_is_a_fault(monkeypatch):
    monkeypatch.setitem(child_module.ALLOWED_FROM_IMPORT_NAMES, "typing", frozenset({"NoSuchName"}))
    with pytest.raises(ChildError, match="NoSuchName"):
        guarded_import("typing", None, None, ("NoSuchName",), 0)


def test_the_benign_class_cannot_reach_the_real_builtins():
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Peek(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=1.0) if open is None else None
'''
    runtime = ChildRuntime(source=source, class_name="Peek", params={}, lookback=2, conids=(C1,),
                           skip_static_check=True)
    runtime.load()
    with pytest.raises(ChildError, match="NameError"):
        runtime.on_bar(C1, T0, BAR)


# ── the fork plumbing that needs no root ──

def test_a_frame_round_trips_over_a_socket():
    left, right = socket.socketpair()
    with left, right:
        child_module.send_frame(left, {"ok": True, "signal": {"action": None, "probability": None}})
        assert child_module.recv_frame(right) == {"ok": True, "signal": {"action": None, "probability": None}}


def test_an_oversize_frame_header_is_refused_before_reading_the_body():
    left, right = socket.socketpair()
    with left, right:
        left.sendall(struct.pack(">I", MAX_FRAME_BYTES + 1))
        with pytest.raises(ChildError, match="cap"):
            child_module.recv_frame(right)


@pytest.mark.parametrize("body", [b"[1]", b"{nope", b"\xff\xfe"])
def test_a_frame_that_is_not_a_json_object_is_refused(body):
    left, right = socket.socketpair()
    with left, right:
        left.sendall(struct.pack(">I", len(body)) + body)
        with pytest.raises(ChildError):
            child_module.recv_frame(right)


def test_a_closed_pipe_is_a_child_error():
    left, right = socket.socketpair()
    left.close()
    with right, pytest.raises(ChildError, match="closed"):
        child_module.recv_frame(right)


@pytest.mark.parametrize("reply", [
    {"ok": True, "signal": {"action": "HOLD", "probability": 0.5}},
    {"ok": True, "signal": {"action": "BUY", "probability": 1.5}},
    {"ok": True, "signal": "BUY"},
    {"ok": True},
    {"ok": "yes", "signal": {"action": None, "probability": None}},
    {"ok": False, "detail": 7},
])
def test_the_parent_refuses_an_out_of_contract_child_reply(reply):
    with pytest.raises(ChildError):
        child_module.signal_from_reply(reply)


def _trickle(sock, length=100, every_s=0.05):
    """Send a frame header, then one body byte at a time: each recv returns quickly, the frame never ends."""
    def run():
        try:
            sock.sendall(struct.pack(">I", length))
            for _ in range(length):
                time.sleep(every_s)
                sock.sendall(b" ")
        except OSError:
            pass
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_a_trickling_peer_cannot_stretch_the_reply_deadline():
    # Review fix round 1 (ruling 6): one deadline for the whole frame, not one per recv.
    left, right = socket.socketpair()
    with left, right:
        _trickle(left)
        started = time.monotonic()
        with pytest.raises(ChildError, match="timed out"):
            child_module.recv_frame(right, timeout_s=0.3)
        assert time.monotonic() - started < 0.3 + 0.2


def test_a_silent_peer_times_out():
    left, right = socket.socketpair()
    with left, right:
        started = time.monotonic()
        with pytest.raises(ChildError, match="timed out"):
            child_module.recv_frame(right, timeout_s=0.2)
        assert time.monotonic() - started < 0.2 + 0.2
        assert right.gettimeout() is None


def test_a_bar_reply_has_one_deadline_too():
    left, right = socket.socketpair()
    with left, right:
        handle = child_module._ForkedChild(pid=-1, sock=right, reply_timeout_s=0.3)
        _trickle(left)
        started = time.monotonic()
        with pytest.raises(ChildError, match="timed out"):
            handle.send_bar(C1, T0, BAR)
        assert time.monotonic() - started < 0.3 + 0.2


def test_a_long_child_detail_is_truncated():
    with pytest.raises(ChildError) as caught:
        child_module.signal_from_reply({"ok": False, "detail": "x" * 10_000})
    assert len(caught.value.detail) <= child_module.MAX_CHILD_DETAIL_CHARS


def test_a_huge_integer_bar_value_is_a_fault():
    runtime = _runtime()
    runtime.load()
    with pytest.raises(ChildError, match="close"):
        runtime.on_bar(C1, T0, {**BAR, "close": 10 ** 400})


def _serve_in_thread(spec):
    parent, child = socket.socketpair()
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", child_module._serve_session(spec, child)),
                              daemon=True)
    thread.start()
    return parent, child, thread, result


def _spec(**overrides):
    fields = dict(source=GOOD, class_name="Counter", params={"N": 1.0}, lookback=3, conids=(C1,),
                  slot_uid=20001, slot_dir="/nonexistent", child_mem_bytes=1)
    return child_module.SpawnSpec(**{**fields, **overrides})


@pytest.mark.parametrize("bar_request", [
    {"type": "BAR", "conid": C2, "t": T0.isoformat(), "ohlcv": BAR},
    {"type": "BAR", "conid": C1, "t": "not a time", "ohlcv": BAR},
])
def test_the_child_exits_non_zero_after_a_bar_fault(bar_request):
    parent, child, thread, result = _serve_in_thread(_spec())
    with parent, child:
        assert child_module.recv_frame(parent, timeout_s=5) == {"ok": True}
        child_module.send_frame(parent, bar_request)
        assert child_module.recv_frame(parent, timeout_s=5)["ok"] is False
        thread.join(5)
    assert result["code"] == child_module._EXIT_CHILD_CRASHED


def test_the_child_exits_non_zero_after_a_load_fault():
    parent, child, thread, result = _serve_in_thread(_spec(class_name="Other"))
    with parent, child:
        assert child_module.recv_frame(parent, timeout_s=5)["ok"] is False
        thread.join(5)
    assert result["code"] == child_module._EXIT_CHILD_CRASHED


def test_the_child_exits_zero_on_close():
    parent, child, thread, result = _serve_in_thread(_spec())
    with parent, child:
        assert child_module.recv_frame(parent, timeout_s=5) == {"ok": True}
        child_module.send_frame(parent, {"type": "CLOSE"})
        thread.join(5)
    assert result["code"] == 0


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_failed_privilege_drop_reports_its_code_and_exits_71():
    # A plain fork (no privileges): uid 0 is refused before any syscall, so no authored code runs.
    parent, child = socket.socketpair()
    pid = os.fork()
    if pid == 0:
        parent.close()
        child_module._run_forked_child(_spec(slot_uid=0), child)   # always os._exit
    child.close()
    with parent:
        reply = child_module.recv_frame(parent, timeout_s=5)
    _, status = os.waitpid(pid, 0)
    assert reply == {"ok": False, "detail": "PRIVILEGE_DROP_FAILED"}
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == child_module._EXIT_DROP_FAILED


def test_the_parent_reads_a_good_child_reply_and_a_child_fault():
    assert child_module.signal_from_reply(
        {"ok": True, "signal": {"action": "BUY", "probability": 1}}) == {"action": "BUY", "probability": 1.0}
    with pytest.raises(ChildError, match="on_bar raised"):
        child_module.signal_from_reply({"ok": False, "detail": "on_bar raised ZeroDivisionError"})


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_the_child_closes_every_inherited_fd_but_the_ones_it_keeps():
    # Forks a plain process (no privilege change); the child reports which test fds survived.
    extra = [os.open(os.devnull, os.O_RDONLY) for _ in range(3)]
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            child_module.close_fds_except({write_end})
            alive = []
            for fd in extra + [read_end]:
                try:
                    os.fstat(fd)
                    alive.append(fd)
                except OSError:
                    pass
            os.write(write_end, repr(alive).encode())
            code = 0
        finally:
            os._exit(code)
    os.close(write_end)
    try:
        report = os.read(read_end, 1024)
        _, status = os.waitpid(pid, 0)
    finally:
        os.close(read_end)
        for fd in extra:
            os.close(fd)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert report == b"[]"


def test_an_unbounded_fd_limit_is_refused_before_any_fd_is_closed(monkeypatch):
    monkeypatch.setattr(child_module.os, "sysconf", lambda name: -1)
    monkeypatch.setattr(child_module.os, "closerange", lambda low, high: pytest.fail("closerange was called"))
    with pytest.raises(RuntimeError, match="SC_OPEN_MAX"):
        child_module.close_fds_except({5})


@pytest.mark.parametrize("slot_uid", [0, 1000, 20000, 20008, True])
def test_the_drop_refuses_a_uid_that_is_not_a_slot_before_any_syscall(slot_uid, monkeypatch):
    monkeypatch.setattr(child_module.os, "setgroups", lambda groups: pytest.fail("setgroups was called"),
                        raising=False)
    spec = child_module.SpawnSpec(source=GOOD, class_name="Counter", params={}, lookback=3, conids=(C1,),
                                  slot_uid=slot_uid, slot_dir="/nonexistent", child_mem_bytes=1)
    with pytest.raises(child_module._PrivilegeDropFailed, match="not a slot uid"):
        child_module._drop_privileges(spec)


# ── the real fork with the privilege drop: Linux and root only (the container tests run it, Task 7) ──

_ROOT_ON_LINUX = sys.platform.startswith("linux") and os.geteuid() == 0


@pytest.mark.skipif(not _ROOT_ON_LINUX, reason="the privilege drop needs root on Linux")
def test_a_forked_child_runs_as_its_slot_uid_and_answers_a_bar(tmp_path):
    slot_uid = 20001
    slot_dir = tmp_path / "s20001"
    slot_dir.mkdir(mode=0o700)
    os.chown(slot_dir, slot_uid, child_module.SBXCHILD_GID)
    tmp_path.chmod(0o755)
    spec = child_module.SpawnSpec(source=GOOD, class_name="Counter", params={"N": 1.0}, lookback=3,
                                  conids=(C1,), slot_uid=slot_uid, slot_dir=str(slot_dir),
                                  child_mem_bytes=256 * 1024 * 1024)
    handle = child_module.ForkSpawner().spawn(spec)
    try:
        assert handle.send_bar(C1, T0, BAR) == {"action": "BUY", "probability": 0.1}
        with open(f"/proc/{handle.pid}/status") as status:
            fields = dict(line.split(":", 1) for line in status if ":" in line)
        assert fields["Uid"].split() == [str(slot_uid)] * 4
        assert int(fields["CapEff"].strip(), 16) == 0
    finally:
        handle.close()
