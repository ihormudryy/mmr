import asyncio
import os
import shutil
import tempfile
import threading

import pytest

from trader.authored.api import Decision
from trader.authored.sandbox_client import (SandboxClient, SandboxFault, SandboxProtocolError,
                                            SandboxUnavailable)
from trader.sandbox import isolation_checks as ic
from trader.sandbox.child import ChildError, ChildRuntime
from trader.sandbox.server import serve
from trader.sandbox.supervisor import ESCAPE_DIR, PROBE_DIR, Supervisor
from tests.sandbox.fakes import InProcessSpawner


def _bar_fault(name):
    """The detail the real child gives when this escape is contained at BAR (ic.EXPECTED_FAULT_MARKERS)."""
    return f"on_bar raised {ic.EXPECTED_FAULT_MARKERS[name][0]}: fake"


CONFIRMED = Decision(action="BUY", probability=0.5)       # what a property probe answers when the property holds
_CHECKING_PROBES = {*ic.PROPERTY_PROBES.values(), ic.PIPE_PROBE, ic.SLOT_PROBE}
PROBE_CHECKS = (*ic.PROPERTY_PROBES, "child_holds_only_its_pipe", "slot_wiped_between_sessions")


class _FakeSession:
    def __init__(self, fault=None, decision=None):
        self.fault = fault             # None: the bar answers; else the SandboxFault detail to raise
        self.decision = decision
        self.bars = 0

    def bar(self, conid, *a, **k):
        assert conid == 1      # a probe session holds PROBE_CONID only
        if self.fault is not None:
            raise SandboxFault(self.fault, code="SANDBOX_ERROR")
        self.bars += 1
        return self.decision

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


class _FakeInfo:
    image_digest = "build-x"
    boot_id = "b"
    pool = "research"
    free = 4
    size = 4
    live_open = 0
    posture = {"cap_eff": ic.EXPECTED_CAP_EFF, "seccomp": 2, "no_new_privs": 1, "uid": 0,
               "interfaces": ["lo"], "forbidden_env": [], "home_mounts": [],
               "root_read_only": True, "pids_max": "64"}


class _FakeClient:
    """Benign probes run; every hostile escape opens and faults at BAR for its own reason; posture is clean."""

    def __init__(self):
        self.pings = 0

    def ping(self):
        self.pings += 1
        return _FakeInfo()

    def open_probe(self, kind, name):
        if kind == "probe" and name == ic.SLOT_PROBE:
            return _FakeSession(decision=_slot_answer(self.next_slot()))
        if kind == "probe":
            return _FakeSession(decision=CONFIRMED if name in _CHECKING_PROBES else None)
        return _FakeSession(_bar_fault(name))

    def next_slot(self):
        """The research pool hands out its slots in turn (FIFO), as the supervisor does."""
        if not hasattr(self, "_slots"):
            self._slots = list(RESEARCH_UIDS[:_FakeInfo.size])
        uid = self._slots.pop(0)
        self._slots.append(uid)
        return uid


RESEARCH_UIDS = (20004, 20005, 20006, 20007)


def _slot_answer(uid, clean=True):
    """What writes_own_slot answers in slot ``uid``: BUY when the slot was empty, its uid in the probability."""
    return Decision(action="BUY" if clean else "SELL", probability=(uid - 20000) / 10)


def check_of(result, name):
    return next(c for c in result.checks if c.name == name)


def test_a_clean_service_passes_every_check():
    result = ic.run_isolation_checks(_FakeClient())
    assert result.passed is True and result.image_digest == "build-x"
    assert {c.name for c in result.checks} >= set(ic.HOSTILE_ESCAPES) | {
        "benign_session", "cap_eff", "seccomp", "network_none", "read_only_root", "pids_limit"}
    assert set(ic.EXPECTED_FAULT_MARKERS) == set(ic.HOSTILE_ESCAPES)


def test_every_property_probe_ships_in_the_image():
    from trader.sandbox.supervisor import PROBE_DIR, _only_class, _read_probe_source

    for probe in _CHECKING_PROBES:
        assert (PROBE_DIR / f"{probe}.py").is_file()
        assert _only_class(_read_probe_source("probe", probe))


@pytest.mark.parametrize("check", PROBE_CHECKS)
@pytest.mark.parametrize("answer", [None, Decision(action="SELL", probability=0.5),
                                    SandboxFault("on_bar raised OSError: x", code="SANDBOX_ERROR"),
                                    SandboxUnavailable("sandbox unavailable: fork failed"),
                                    SandboxProtocolError("probe 'x' not found")])
def test_a_probe_that_does_not_confirm_fails_its_check(check, answer):
    probe = {**ic.PROPERTY_PROBES, "child_holds_only_its_pipe": ic.PIPE_PROBE,
             "slot_wiped_between_sessions": ic.SLOT_PROBE}[check]

    class Unconfirmed(_FakeClient):
        def open_probe(self, kind, name):
            if name != probe:
                return super().open_probe(kind, name)
            if isinstance(answer, Exception):
                raise answer
            return _FakeSession(decision=answer)

    result = ic.run_isolation_checks(Unconfirmed())
    assert result.passed is False and check_of(result, check).passed is False
    assert all(check_of(result, other).passed for other in PROBE_CHECKS if other != check)


def test_the_probe_checks_run_once_and_after_the_benign_session():
    class Recording(_FakeClient):
        def __init__(self):
            super().__init__()
            self.opened = []

        def open_probe(self, kind, name):
            self.opened.append(name)
            return super().open_probe(kind, name)

    client = Recording()
    result = ic.run_isolation_checks(client)
    assert result.passed is True
    names = [c.name for c in result.checks]
    assert names.index("benign_session") < min(names.index(c) for c in PROBE_CHECKS)
    assert max(names.index(c) for c in PROBE_CHECKS) < min(names.index(e) for e in ic.HOSTILE_ESCAPES)
    assert client.opened.count("ptrace_denied") == 1 and client.opened.count("runs_as_slot_uid") == 1


def test_the_pipe_probe_runs_while_a_neighbour_session_is_open():
    class Tracking(_FakeClient):
        def __init__(self):
            super().__init__()
            self.open_now = 0
            self.open_at_pipe_probe = None

        def open_probe(self, kind, name):
            if name == ic.PIPE_PROBE:
                self.open_at_pipe_probe = self.open_now
            session = super().open_probe(kind, name)
            self.open_now += 1
            tracker = self

            class Counted(_FakeSession):
                def __exit__(self, *a):
                    tracker.open_now -= 1
            counted = Counted(session.fault, session.decision)
            return counted

    client = Tracking()
    assert check_of(ic.run_isolation_checks(client), "child_holds_only_its_pipe").passed
    assert client.open_at_pipe_probe == 1


def _slot_check(client):
    return check_of(ic.run_isolation_checks(client), "slot_wiped_between_sessions")


class _SlotScript(_FakeClient):
    """The slot probe answers from ``answer(visit, uid)``; ``order`` lists the slot uids the pool hands out."""

    def __init__(self, order, answer=lambda visit, uid: True):
        super().__init__()
        self.order, self.answer, self.visits, self.calls = list(order), answer, {}, 0

    def open_probe(self, kind, name):
        if name != ic.SLOT_PROBE:
            return super().open_probe(kind, name)
        uid = self.order[self.calls % len(self.order)]
        self.calls += 1
        self.visits[uid] = self.visits.get(uid, 0) + 1
        return _FakeSession(decision=_slot_answer(uid, clean=self.answer(self.visits[uid], uid)))


def test_every_research_slot_is_reused_and_found_clean():
    client = _SlotScript(RESEARCH_UIDS)
    check = _slot_check(client)
    assert check.passed, check.detail
    assert client.visits == {uid: 2 for uid in RESEARCH_UIDS}            # each slot dirtied, then found clean


def test_a_pool_that_always_reuses_the_same_slot_fails():                # PR #159 review: LIFO hid the others
    check = _slot_check(_SlotScript([20007]))
    assert check.passed is False and "slot" in check.detail


def test_one_dirty_slot_beside_clean_ones_fails():                        # PR #159 review regression
    class TwoSlots(_SlotScript):
        def ping(self):
            info = super().ping()
            info.size = 2
            return info

    dirty = TwoSlots([20004, 20005], answer=lambda visit, uid: not (uid == 20005 and visit > 1))
    check = _slot_check(dirty)
    assert check.passed is False and "20005" in check.detail
    clean = TwoSlots([20004, 20005])
    assert _slot_check(clean).passed is True


def test_a_quarantined_slot_that_never_comes_back_fails():
    # Slot 20006 is used once, its wipe fails and it is quarantined: the pool rotates over the other three.
    class Quarantine(_SlotScript):
        def open_probe(self, kind, name):
            if name == ic.SLOT_PROBE and self.calls >= 4:
                self.order = [20004, 20005, 20007]
            return super().open_probe(kind, name)

    check = _slot_check(Quarantine(RESEARCH_UIDS))
    assert check.passed is False and "20006" in check.detail


@pytest.mark.parametrize("probability", [0.0, 0.05, 0.9, 1.0])
def test_a_slot_answer_without_a_slot_uid_fails(probability):
    class NoUid(_FakeClient):
        def open_probe(self, kind, name):
            if name == ic.SLOT_PROBE:
                return _FakeSession(decision=Decision(action="BUY", probability=probability))
            return super().open_probe(kind, name)

    assert _slot_check(NoUid()).passed is False


def test_more_slot_uids_than_the_pool_has_fails():
    check = _slot_check(_SlotScript([20001, 20002, 20003, 20004, 20005]))
    assert check.passed is False


@pytest.mark.parametrize("size", [0, None, True, "4"])
def test_a_pool_size_that_is_not_a_slot_count_fails_the_slot_check(size):
    class OddSize(_FakeClient):
        def ping(self):
            info = _FakeInfo()
            info.size = size
            return info

    assert check_of(ic.run_isolation_checks(OddSize()), "slot_wiped_between_sessions").passed is False


def test_the_slot_probe_reports_its_uid_and_whether_the_slot_was_empty(monkeypatch, tmp_path):
    import os as real_os
    from datetime import datetime, timezone
    from trader.sandbox.supervisor import _read_probe_source

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(real_os, "getuid", lambda: 20006)
    bar = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}

    def one_session():
        runtime = ChildRuntime(source=_read_probe_source("probe", ic.SLOT_PROBE), class_name="WritesOwnSlot",
                               params={}, lookback=5, conids=(1,), skip_static_check=True)
        runtime.load()
        return runtime.on_bar(1, datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), bar)

    assert one_session() == {"action": "BUY", "probability": 0.6}
    assert one_session() == {"action": "SELL", "probability": 0.6}        # leftover.csv from the first session


def test_the_identity_probe_pins_the_slot_uids_and_group():
    from trader.sandbox.caps import SBXCHILD_GID, SLOT_UIDS
    from trader.sandbox.supervisor import PROBE_DIR

    source = (PROBE_DIR / "runs_as_slot_uid.py").read_text()
    assert f"SLOT_UIDS = range({SLOT_UIDS.start}, {SLOT_UIDS.stop})" in source
    assert f"SBXCHILD_GID = {SBXCHILD_GID}" in source
    assert f"SLOT_UIDS = range({SLOT_UIDS.start}, {SLOT_UIDS.stop})" in (PROBE_DIR / "writes_own_slot.py").read_text()


_SLOT_CHILD_STATUS = ("Name:\tpython\nUid:\t20004\t20004\t20004\t20004\nNoNewPrivs:\t1\nSeccomp:\t2\n"
                      "Seccomp_filters:\t1\nCapInh:\t0000000000000000\nCapPrm:\t0000000000000000\n"
                      "CapEff:\t0000000000000000\nCapBnd:\t00000000000000e0\nCapAmb:\t0000000000000000\n")


class _FakeOs:
    """What the identity probe reads, so its logic runs here under the guarded built-ins (no real child)."""
    O_RDONLY = 0

    def __init__(self, uids=(20004,) * 3, gids=(20000,) * 3, groups=(), status=_SLOT_CHILD_STATUS):
        self._uids, self._gids, self._groups, self._status = uids, gids, list(groups), status

    def getuid(self):
        return self._uids[0]

    def getresuid(self):
        return self._uids

    def getresgid(self):
        return self._gids

    def getgroups(self):
        return list(self._groups)

    def open(self, path, flags):
        assert path == "/proc/self/status"
        return 3

    def read(self, fd, count):
        return self._status.encode()

    def close(self, fd):
        return None


def _identity_answer(monkeypatch, fake_os):
    import pandas as pd
    from datetime import datetime, timezone
    from trader.sandbox.supervisor import _read_probe_source

    monkeypatch.setattr(pd.io.common, "os", fake_os)
    # Our own probe, run in process only with a fake os: it cannot reach the real one.
    runtime = ChildRuntime(source=_read_probe_source("probe", "runs_as_slot_uid"), class_name="RunsAsSlotUid",
                           params={}, lookback=5, conids=(1,), skip_static_check=True)
    runtime.load()
    return runtime.on_bar(1, datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
                          {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0})


def test_the_identity_probe_confirms_a_slot_child(monkeypatch):
    assert _identity_answer(monkeypatch, _FakeOs()) == {"action": "BUY", "probability": 0.4}


@pytest.mark.parametrize("fake_os", [
    _FakeOs(uids=(0, 0, 0)),
    _FakeOs(uids=(1000, 1000, 1000)),
    _FakeOs(uids=(20004, 20004, 0)),                                     # saved uid still root
    _FakeOs(gids=(20000, 20000, 1000)),
    _FakeOs(groups=(1000,)),
    _FakeOs(status=_SLOT_CHILD_STATUS.replace("CapEff:\t0000000000000000", "CapEff:\t00000000000000e0")),
    _FakeOs(status=_SLOT_CHILD_STATUS.replace("CapAmb:\t0000000000000000\n", "")),
    _FakeOs(status=_SLOT_CHILD_STATUS.replace("Seccomp:\t2", "Seccomp:\t0")),
    _FakeOs(status=_SLOT_CHILD_STATUS.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0")),
    _FakeOs(status=""),
])
def test_the_identity_probe_refuses_anything_else(monkeypatch, fake_os):
    assert _identity_answer(monkeypatch, fake_os)["action"] == "SELL"


def test_the_hostile_names_are_plan_1s_files():
    from tests.authored.test_escape_corpus import ESCAPE_DIR, SANDBOX_HOSTILE

    assert ic.HOSTILE_ESCAPES == SANDBOX_HOSTILE
    assert all((ESCAPE_DIR / f"{name}.py").is_file() for name in ic.HOSTILE_ESCAPES)


def test_an_open_time_fault_is_never_containment():                                     # review r1 #3
    class NeverOpens(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "escape":
                raise SandboxFault("load failed: NameError: __build_class__ not found", code="SANDBOX_ERROR")
            return _FakeSession()

    result = ic.run_isolation_checks(NeverOpens())
    assert result.passed is False
    assert all(check_of(result, name).detail.startswith("never OPENED") for name in ic.HOSTILE_ESCAPES)


@pytest.mark.parametrize("detail", ["load failed: SyntaxError", "constructor failed: TypeError: x",
                                    "on_bar raised KeyError: 'close'"])
def test_a_bar_fault_that_the_escape_does_not_explain_is_not_containment(detail):
    class Unexplained(_FakeClient):
        def open_probe(self, kind, name):
            return _FakeSession(None if kind == "probe" else detail)

    result = ic.run_isolation_checks(Unexplained())
    assert result.passed is False
    assert not any(check_of(result, name).passed for name in ic.HOSTILE_ESCAPES)


def test_an_escape_that_kills_the_neighbour_fails():
    class KillsNeighbour(_FakeClient):
        def __init__(self):
            super().__init__()
            self.benign = []

        def open_probe(self, kind, name):
            if kind == "probe":
                session = _FakeSession()
                self.benign.append(session)
                return session
            for held in self.benign:
                held.fault = "child pipe closed"           # the escape killed the neighbour
            return _FakeSession(_bar_fault(name))

    result = ic.run_isolation_checks(KillsNeighbour())
    check = check_of(result, "escape_kill_other_child")
    assert check.passed is False and "neighbour" in check.detail


def test_an_escape_after_which_ping_fails_fails():
    class DiesAfterEscape(_FakeClient):
        def ping(self):
            self.pings += 1
            if self.pings > 1:
                raise SandboxUnavailable("supervisor gone")
            return _FakeInfo()

    result = ic.run_isolation_checks(DiesAfterEscape())
    assert result.passed is False
    assert "PING" in check_of(result, ic.HOSTILE_ESCAPES[0]).detail


def test_a_hostile_escape_that_does_not_fault_fails_the_run():
    class Leaky(_FakeClient):
        def open_probe(self, kind, name):
            return _FakeSession()                # nothing faults: the sandbox did not contain it

    result = ic.run_isolation_checks(Leaky())
    assert result.passed is False
    assert all(check_of(result, name).detail == "ran to completion: NOT contained" for name in ic.HOSTILE_ESCAPES)


def test_bad_posture_fails_the_run():
    class BadCaps(_FakeClient):
        def ping(self):
            info = _FakeInfo()
            info.posture = dict(info.posture, cap_eff="0000000000000fff")   # too many caps
            return info

    result = ic.run_isolation_checks(BadCaps())
    assert result.passed is False
    assert any(c.name == "cap_eff" and not c.passed for c in result.checks)


@pytest.mark.parametrize("key,value,check", [("root_read_only", False, "read_only_root"),
                                             ("pids_max", "max", "pids_limit"),
                                             ("pids_max", "", "pids_limit")])
def test_a_writable_root_or_a_missing_pids_limit_fails_the_run(key, value, check):
    class Loose(_FakeClient):
        def ping(self):
            info = _FakeInfo()
            info.posture = dict(info.posture, **{key: value})
            return info

    result = ic.run_isolation_checks(Loose())
    assert result.passed is False
    assert any(c.name == check and not c.passed for c in result.checks)


# ── fail closed: a missing, unknown or odd posture value is a failed check ──

_DROP = object()   # remove the key from the posture


def _with_posture(**changes):
    class Posture(_FakeClient):
        def ping(self):
            self.pings += 1
            info = _FakeInfo()
            info.posture = {**_FakeInfo.posture, **changes}
            for key, value in changes.items():
                if value is _DROP:
                    del info.posture[key]
            return info
    return Posture()


@pytest.mark.parametrize("key,value,check", [
    ("cap_eff", _DROP, "cap_eff"),
    ("cap_eff", "00000000000000e1", "cap_eff"),          # CAP_CHOWN not dropped (ruling 2)
    ("cap_eff", "1000000000000000e0", "cap_eff"),        # a bit past 64 must not hide
    ("cap_eff", "0xe0", "cap_eff"),
    ("seccomp", _DROP, "seccomp"),
    ("seccomp", None, "seccomp"),
    ("seccomp", 0, "seccomp"),
    ("seccomp", 2.0, "seccomp"),
    ("no_new_privs", _DROP, "no_new_privs"),
    ("no_new_privs", True, "no_new_privs"),
    ("interfaces", _DROP, "network_none"),
    ("interfaces", [], "network_none"),                  # nothing read is not "only lo"
    ("interfaces", ["<unreadable>"], "network_none"),
    ("interfaces", ["eth0", "lo"], "network_none"),
    ("interfaces", "lo", "network_none"),
    ("forbidden_env", _DROP, "no_forbidden_env"),
    ("forbidden_env", None, "no_forbidden_env"),
    ("forbidden_env", ["ALPACA_API_KEY_ID"], "no_forbidden_env"),
    ("home_mounts", _DROP, "no_home_mounts"),
    ("home_mounts", ["/home/trader/.config"], "no_home_mounts"),
    ("home_mounts", ["<unreadable>"], "no_home_mounts"),
    ("root_read_only", _DROP, "read_only_root"),
    ("root_read_only", 1, "read_only_root"),
    ("pids_max", _DROP, "pids_limit"),
    ("pids_max", 64, "pids_limit"),
])
def test_a_missing_or_unknown_posture_value_fails_its_check(key, value, check):
    result = ic.run_isolation_checks(_with_posture(**{key: value}))
    assert result.passed is False
    assert check_of(result, check).passed is False


_DOWN = {"operstate": "down", "flags": 0x80, "ipv4": False, "ipv6": False}
_LO = {"operstate": "unknown", "flags": 0x9, "ipv4": True, "ipv6": True}


def test_only_lo_passes_the_network_check():
    result = ic.run_isolation_checks(_with_posture(interfaces=["lo"], interface_state={"lo": _LO}))
    assert check_of(result, "network_none").passed is True


def test_lo_and_down_fallback_tunnels_without_addresses_pass():                         # ruling 33
    result = ic.run_isolation_checks(_with_posture(
        interfaces=["ip6tnl0", "lo", "tunl0"],
        interface_state={"lo": _LO, "tunl0": _DOWN, "ip6tnl0": dict(_DOWN)}))
    assert check_of(result, "network_none").passed is True and result.passed is True


@pytest.mark.parametrize("interfaces,state", [
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "operstate": "up"}}),           # tunnel up
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "flags": 0x81}}),               # IFF_UP set
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "ipv4": True}}),                # has an IPv4 address
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "ipv6": True}}),                # has an IPv6 address
    (["eth0", "lo"], {"eth0": _DOWN}),                                    # not a fallback tunnel
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "operstate": None}}),           # unreadable state
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "flags": None}}),
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "ipv4": None}}),
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "ipv6": None}}),
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "flags": True}}),
    (["lo", "tunl0"], {}),                                                # no state for the device
    (["lo", "tunl0"], _DROP),                                             # no state at all
    (["lo", "tunl0"], "down"),
    (["tunl0"], {"tunl0": _DOWN}),                                        # no lo
    (["lo", ["tunl0"]], {"tunl0": _DOWN}),                                # not a name
])
def test_any_other_interface_or_an_up_addressed_or_unknown_tunnel_fails(interfaces, state):
    result = ic.run_isolation_checks(_with_posture(interfaces=interfaces, interface_state=state))
    assert result.passed is False and check_of(result, "network_none").passed is False


def test_the_store_mount_is_the_only_mount_allowed():
    result = ic.run_isolation_checks(_with_posture(home_mounts=["/store"]))
    assert result.passed is True and check_of(result, "no_home_mounts").passed


@pytest.mark.parametrize("build_id", ["", "unset sha", "-leading-dash"])
def test_a_malformed_build_id_fails_the_run(build_id):
    class OddBuild(_FakeClient):
        def ping(self):
            info = _FakeInfo()
            info.image_digest = build_id
            return info

    result = ic.run_isolation_checks(OddBuild())
    assert result.passed is False and check_of(result, "build_id").passed is False


@pytest.mark.parametrize("boot_id", ["", None, 7])
def test_a_baseline_without_a_boot_id_fails_the_run(boot_id):
    # Without a boot id, a supervisor restarted by an escape could not be told apart.
    class NoBootId(_FakeClient):
        def ping(self):
            info = _FakeInfo()
            info.boot_id = boot_id
            return info

    result = ic.run_isolation_checks(NoBootId())
    assert result.passed is False and check_of(result, "boot_id").passed is False


def test_open_socket_counts_only_as_network_unreachable():
    assert ic.EXPECTED_FAULT_MARKERS["escape_open_socket"] == ("Network is unreachable",)

    class OtherOSError(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "probe":
                return _FakeSession()
            if name == "escape_open_socket":
                return _FakeSession("on_bar raised OSError: [Errno 9] Bad file descriptor")
            return _FakeSession(_bar_fault(name))

    result = ic.run_isolation_checks(OtherOSError())
    assert result.passed is False and check_of(result, "escape_open_socket").passed is False


# ── live sessions: no probe ever runs beside a live child (rulings 21, 29) ──

@pytest.mark.parametrize("live_open", [None, 1])
def test_an_open_or_unknown_live_session_runs_no_probe(live_open):
    class LiveBusy(_FakeClient):
        def __init__(self):
            super().__init__()
            self.opened = []

        def ping(self):
            info = _FakeInfo()
            info.live_open = live_open
            return info

        def open_probe(self, kind, name):
            self.opened.append((kind, name))
            return super().open_probe(kind, name)

    client = LiveBusy()
    result = ic.run_isolation_checks(client)
    assert result.passed is False and client.opened == []
    assert check_of(result, "live_sessions_closed").passed is False
    assert not any(c.name in ic.HOSTILE_ESCAPES for c in result.checks)


def test_a_live_session_that_opens_during_the_run_fails_the_escape_it_overlapped():
    class LiveOpensLater(_FakeClient):
        def ping(self):
            self.pings += 1
            info = _FakeInfo()
            info.live_open = 0 if self.pings == 1 else 1
            return info

    result = ic.run_isolation_checks(LiveOpensLater())
    assert result.passed is False
    assert "live" in check_of(result, ic.HOSTILE_ESCAPES[0]).detail


def test_a_supervisor_that_restarted_during_an_escape_fails_it():
    class Restarts(_FakeClient):
        def ping(self):
            self.pings += 1
            info = _FakeInfo()
            info.boot_id = "b" if self.pings == 1 else "b-new"   # killed, and docker started a new one
            return info

    result = ic.run_isolation_checks(Restarts())
    assert result.passed is False
    assert all("restarted" in check_of(result, name).detail for name in ic.HOSTILE_ESCAPES)


# ── an unexpected error anywhere is a failed check, never a crash or a pass ──

def test_an_unexpected_error_in_a_hostile_check_is_a_failed_check():
    class Broken(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "escape":
                raise RuntimeError("bug")
            return _FakeSession()

    result = ic.run_isolation_checks(Broken())
    assert result.passed is False
    assert all(not check_of(result, name).passed for name in ic.HOSTILE_ESCAPES)


def test_an_unexpected_error_while_attributing_the_fault_is_a_failed_check():
    class OddFault(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "probe":
                return _FakeSession()
            session = _FakeSession()
            session.bar = lambda *a, **k: (_ for _ in ()).throw(ValueError("odd"))
            return session

    result = ic.run_isolation_checks(OddFault())
    assert result.passed is False
    assert all(check_of(result, name).detail.startswith("no fault at BAR") for name in ic.HOSTILE_ESCAPES)


def test_a_refused_benign_bar_fails_the_benign_check():
    class Refuses(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "probe":
                raise SandboxProtocolError("probes are research-only")
            return super().open_probe(kind, name)

    result = ic.run_isolation_checks(Refuses())
    assert result.passed is False and check_of(result, "benign_session").passed is False


def test_a_fault_with_another_code_is_not_containment():
    class Mismatch(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "probe":
                return _FakeSession()
            session = _FakeSession()

            def bar(*a, **k):
                raise SandboxFault(_bar_fault(name), code="SANDBOX_DIGEST_MISMATCH")
            session.bar = bar
            return session

    result = ic.run_isolation_checks(Mismatch())
    assert not any(check_of(result, name).passed for name in ic.HOSTILE_ESCAPES)


def test_a_supervisor_side_error_naming_the_marker_is_not_containment():
    # The supervisor answers "child failed: <Type>" for its own bugs; that is not the escape faulting.
    class SupervisorBug(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "probe":
                return _FakeSession()
            return _FakeSession(f"child failed: {ic.EXPECTED_FAULT_MARKERS[name][0]}")

    result = ic.run_isolation_checks(SupervisorBug())
    assert not any(check_of(result, name).passed for name in ic.HOSTILE_ESCAPES)


def test_a_sandbox_unavailable_at_bar_is_not_containment():
    # Ruling 34: a sandbox-side failure is availability, so it never proves an escape was stopped.
    class Unavailable(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "probe":
                return super().open_probe(kind, name)
            session = _FakeSession()

            def bar(*a, **k):
                raise SandboxUnavailable("sandbox unavailable: internal sandbox error: RuntimeError")
            session.bar = bar
            return session

    result = ic.run_isolation_checks(Unavailable())
    assert all(check_of(result, name).detail.startswith("no fault at BAR") for name in ic.HOSTILE_ESCAPES)


def test_an_unknown_escape_name_is_a_failed_check():
    check = ic.hostile_check(_FakeClient(), "escape_not_in_the_list")
    assert check.passed is False


# ── over the real wire: client -> asyncio server -> supervisor ─────────────

@pytest.fixture
def socket_dir():
    path = tempfile.mkdtemp(prefix="sbx", dir="/tmp")   # macOS caps a socket path at 104 bytes
    yield path
    shutil.rmtree(path, ignore_errors=True)


# What each escape's fault looks like when the real forked child stops it (rulings 3, 5; seccomp; network none).
_CONTAINED_DETAIL = {
    "escape_read_config_keys": "on_bar raised FileNotFoundError: [Errno 2] No such file or directory: "
                               "'/home/trader/.config/mmr/keys/rpc/trader.key'",
    "escape_write_outside_tmpfs": "on_bar raised OSError: [Errno 30] Read-only file system: '/store/escape.csv'",
    "escape_fork_bomb": "on_bar raised BlockingIOError: [Errno 11] Resource temporarily unavailable",
    "escape_allocate_past_limit": "on_bar raised _ArrayMemoryError: Unable to allocate 64.0 GiB for an array",
    "escape_loop_forever": None,   # hangs: the server's wall timeout ends it
    "escape_pd_io_common_oskill": "on_bar raised PermissionError: [Errno 1] Operation not permitted",
    "escape_kill_other_child": "on_bar raised PermissionError: [Errno 1] Operation not permitted",
    "escape_sys_modules_lookup": "on_bar raised PermissionError: [Errno 1] Operation not permitted",
    "escape_open_socket": "on_bar raised OSError: [Errno 101] Network is unreachable",
}


class _ScriptedEscape:
    """Stands in for a forked child running an escape. It never runs the escape's code: it only gives the
    fault the real child gives, or hangs until it is killed."""
    pid = -1
    lookback = 50

    def __init__(self, detail):
        self._detail = detail
        self._killed = threading.Event()

    def send_bar(self, conid, t, ohlcv):
        if self._detail is None:
            self._killed.wait(10)
            raise ChildError("child pipe closed")
        raise ChildError(self._detail)

    def close(self):
        self._killed.set()


class _BenignChild:
    pid = -1
    lookback = 50

    def __init__(self, spec):
        self._runtime = ChildRuntime(source=spec.source, class_name=spec.class_name, params=spec.params,
                                     lookback=spec.lookback, conids=spec.conids, skip_static_check=False)
        self._runtime.load()

    def send_bar(self, conid, t, ohlcv):
        return self._runtime.on_bar(conid, t, ohlcv)

    def close(self):
        return None


class _ScriptedProbe:
    """Stands in for a forked child running a property probe: it answers BUY, as the real one does when the
    property holds, with its slot uid in the probability (writes_own_slot). It never runs the probe's code."""
    pid = -1
    lookback = 50

    def __init__(self, slot_uid):
        self._slot_uid = slot_uid

    def send_bar(self, conid, t, ohlcv):
        return {"action": "BUY", "probability": (self._slot_uid - 20000) / 10}

    def close(self):
        return None


class _ScriptedSpawner:
    def __init__(self):
        self._escapes = {(ESCAPE_DIR / f"{name}.py").read_text(encoding="utf-8"): name
                         for name in ic.HOSTILE_ESCAPES}
        self._probes = {(PROBE_DIR / f"{name}.py").read_text(encoding="utf-8") for name in _CHECKING_PROBES}

    def spawn(self, spec):
        if spec.source in self._probes:
            return _ScriptedProbe(spec.slot_uid)
        name = self._escapes.get(spec.source)
        if name is None:
            return _BenignChild(spec)   # the static check still runs: only a clean probe loads here
        return _ScriptedEscape(_CONTAINED_DETAIL[name])


class _Stack:
    def __init__(self, socket_dir, research_spawner, posture):
        self.research = os.path.join(socket_dir, "research.sock")
        live = os.path.join(socket_dir, "live.sock")
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()
        sup = Supervisor(spawners={"live": InProcessSpawner(), "research": research_spawner},
                         pool_sizes={"live": 1, "research": 2}, store_dir=socket_dir,
                         image_digest="build-x", posture=posture, wiper=lambda slot: True)

        def run():
            asyncio.set_event_loop(self._loop)
            self._server = self._loop.run_until_complete(
                serve(sup, {"live": live, "research": self.research}, bar_timeout_s=1.0,
                      socket_gid=os.getgid()))
            self._loop.call_soon(ready.set)
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(5)

    def stop(self):
        asyncio.run_coroutine_threadsafe(self._server.close(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._loop.close()


def test_the_real_wire_passes_a_contained_service(socket_dir):
    stack = _Stack(socket_dir, _ScriptedSpawner(), dict(_FakeInfo.posture))
    try:
        result = ic.run_isolation_checks(SandboxClient(stack.research))
    finally:
        stack.stop()
    assert [c for c in result.checks if not c.passed] == []
    assert result.passed is True and result.image_digest == "build-x"
    assert "bar timeout" in check_of(result, "escape_loop_forever").detail
    assert all(check_of(result, name).passed for name in PROBE_CHECKS)


def test_the_real_wire_with_the_in_process_spawner_proves_nothing(socket_dir):
    # The in-process spawner never skips the static check, so every escape is refused at OPEN: not containment.
    stack = _Stack(socket_dir, InProcessSpawner(), dict(_FakeInfo.posture))
    try:
        result = ic.run_isolation_checks(SandboxClient(stack.research))
    finally:
        stack.stop()
    assert result.passed is False and check_of(result, "benign_session").passed is True
    assert all(check_of(result, name).detail.startswith("never OPENED") for name in ic.HOSTILE_ESCAPES)
    # The property probes skip the static check only in a real child; refused here, they prove nothing either.
    assert not any(check_of(result, name).passed for name in PROBE_CHECKS)
