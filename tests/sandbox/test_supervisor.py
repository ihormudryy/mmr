import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.sandbox import protocol as p
from trader.sandbox.supervisor import Supervisor
from tests.sandbox.fakes import InProcessSpawner

T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
C1, C2 = 101, 202
OHLCV = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}

GOOD = '''
from trader.authored.api import AuthoredStrategy, Decision


class Counter(AuthoredStrategy):
    LOOKBACK_BARS = 5

    def on_bar(self, history, params):
        if len(history) >= 2:
            return Decision(action="BUY", probability=0.6)
        return None
'''


def _store(tmp_path: Path, source: str) -> str:
    digest = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
    (tmp_path / f"{digest.split(':')[1]}.py").write_text(source)
    return digest


def _supervisor(tmp_path, live=3, research=4, spawners=None):
    # A no-op wiper: the real one would fork a helper against /tmp/s2000X on this machine.
    return Supervisor(spawners=spawners or {"live": InProcessSpawner(), "research": InProcessSpawner()},
                      pool_sizes={"live": live, "research": research},
                      store_dir=str(tmp_path), image_digest="mmr-build-id", posture={},
                      wiper=lambda slot: True)


def _open_frame(digest, class_name="Counter", conids=(C1,)):
    return {"v": 1, "type": "OPEN", "digest": digest, "class": class_name, "params": {},
            "bar_size": "1 min", "conids": list(conids)}


def _open(sup, tmp_path, conn=1, pool="live", conids=(C1,)):
    digest = _store(tmp_path, GOOD)
    return sup.handle(pool, conn, p.parse_request(_open_frame(digest, conids=conids)))


def _bar(sup, session, when, conn=1, pool="live", conid=C1):
    return sup.handle(pool, conn, p.parse_request(
        {"v": 1, "type": "BAR", "session": session, "conid": conid, "t": when.isoformat(), **OHLCV}))


def test_open_then_bars_then_close(tmp_path):
    sup = _supervisor(tmp_path)
    opened = _open(sup, tmp_path)
    assert opened["type"] == "OPENED"
    session = opened["session"]
    assert opened["image_digest"] == "mmr-build-id"                  # ruling 29: the session's own build
    reply = _bar(sup, session, T0)
    assert reply["type"] == "SIGNAL" and reply["action"] is None and reply["conid"] == C1
    reply = _bar(sup, session, T0 + timedelta(minutes=1))
    assert reply["action"] == "BUY"
    assert sup.handle("live", 1, p.parse_request({"v": 1, "type": "CLOSE", "session": session}))["type"] == "CLOSED"


def test_a_live_session_serves_each_of_its_conids_on_its_own_clock(tmp_path):
    sup = _supervisor(tmp_path)
    session = _open(sup, tmp_path, conids=(C1, C2))["session"]
    assert _bar(sup, session, T0, conid=C1)["action"] is None
    assert _bar(sup, session, T0, conid=C2)["action"] is None      # C2's first bar: its own history
    assert _bar(sup, session, T0 + timedelta(minutes=1), conid=C1)["action"] == "BUY"
    assert sup.pong("live")["free"] == 2                             # one slot for both conids


def test_a_bar_for_a_conid_outside_the_session_is_a_bad_request(tmp_path):
    sup = _supervisor(tmp_path)
    session = _open(sup, tmp_path)["session"]
    assert _bar(sup, session, T0, conid=C2)["code"] == p.SANDBOX_BAD_REQUEST
    assert _bar(sup, session, T0)["type"] == "SIGNAL"                # the session still serves C1


def test_a_research_session_takes_exactly_one_conid(tmp_path):
    sup = _supervisor(tmp_path)
    reply = _open(sup, tmp_path, pool="research", conids=(C1, C2))
    assert reply["type"] == "ERROR" and reply["code"] == p.SANDBOX_BAD_REQUEST
    assert sup.pong("research")["free"] == 4                         # refused before a slot was taken


def test_a_fault_on_one_conid_ends_the_whole_session(tmp_path):
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Picky(AuthoredStrategy):
    LOOKBACK_BARS = 5

    def on_bar(self, history, params):
        if history["close"].iloc[-1] > 100.0:
            return Decision(action="BUY", probability=2.0)
        return None
'''
    sup = _supervisor(tmp_path)
    digest = _store(tmp_path, source)
    session = sup.handle("live", 1, p.parse_request(_open_frame(digest, "Picky", (C1, C2))))["session"]
    bad = sup.handle("live", 1, p.parse_request(
        {"v": 1, "type": "BAR", "session": session, "conid": C2, "t": T0.isoformat(),
         "open": 1.0, "high": 200.0, "low": 0.5, "close": 150.0, "volume": 1.0}))
    assert bad["code"] == p.SANDBOX_ERROR
    assert _bar(sup, session, T0, conid=C1)["code"] == p.SANDBOX_BAD_REQUEST   # session gone for C1 too
    assert sup.pong("live")["free"] == 3


def test_a_missing_store_file_is_a_digest_mismatch(tmp_path):
    sup = _supervisor(tmp_path)
    reply = sup.handle("live", 1, p.parse_request(_open_frame("sha256:" + "a" * 64)))
    assert reply["type"] == "ERROR" and reply["code"] == p.SANDBOX_DIGEST_MISMATCH


def test_a_tampered_store_file_is_a_digest_mismatch(tmp_path):
    sup = _supervisor(tmp_path)
    digest = _store(tmp_path, GOOD)
    (tmp_path / f"{digest.split(':')[1]}.py").write_text(GOOD + "\n# changed\n")
    reply = sup.handle("live", 1, p.parse_request(_open_frame(digest)))
    assert reply["code"] == p.SANDBOX_DIGEST_MISMATCH


def test_a_class_name_not_in_the_file_is_a_digest_mismatch(tmp_path):
    sup = _supervisor(tmp_path)
    digest = _store(tmp_path, GOOD)
    reply = sup.handle("live", 1, p.parse_request(_open_frame(digest, "Other")))
    assert reply["code"] == p.SANDBOX_DIGEST_MISMATCH


def test_a_static_rejection_at_open_is_a_sandbox_error(tmp_path):
    sup = _supervisor(tmp_path)
    source = "import os\nfrom trader.authored.api import AuthoredStrategy, Decision\n" \
             "class S(AuthoredStrategy):\n    LOOKBACK_BARS = 2\n" \
             "    def on_bar(self, history, params):\n        return None\n"
    digest = _store(tmp_path, source)
    reply = sup.handle("live", 1, p.parse_request(_open_frame(digest, "S")))
    assert reply["type"] == "ERROR" and reply["code"] == p.SANDBOX_ERROR
    assert "AUTHORED_STATIC_REJECTED" in reply["detail"]


def test_full_pool_answers_busy_at_once(tmp_path):
    sup = _supervisor(tmp_path, live=2)
    _open(sup, tmp_path, conn=1)
    _open(sup, tmp_path, conn=2)
    reply = _open(sup, tmp_path, conn=3)
    assert reply["type"] == "ERROR" and reply["code"] == p.SANDBOX_BUSY


def test_dropping_a_connection_frees_the_slot(tmp_path):
    sup = _supervisor(tmp_path, live=1)
    _open(sup, tmp_path, conn=1)
    assert _open(sup, tmp_path, conn=2)["code"] == p.SANDBOX_BUSY
    sup.drop_connection(1)
    assert _open(sup, tmp_path, conn=2)["type"] == "OPENED"


def test_a_bar_for_an_unknown_session_is_a_bad_request(tmp_path):
    sup = _supervisor(tmp_path)
    assert _bar(sup, "sdead", T0)["code"] == p.SANDBOX_BAD_REQUEST


def test_a_session_belongs_to_its_connection(tmp_path):
    sup = _supervisor(tmp_path)
    session = _open(sup, tmp_path, conn=1)["session"]
    assert _bar(sup, session, T0, conn=2)["code"] == p.SANDBOX_BAD_REQUEST


def test_research_and_live_pools_are_separate(tmp_path):
    sup = _supervisor(tmp_path, live=1, research=1)
    _open(sup, tmp_path, conn=1, pool="live")
    assert _open(sup, tmp_path, conn=2, pool="research")["type"] == "OPENED"


def test_pong_reports_the_image_digest_and_free_counts(tmp_path):
    sup = _supervisor(tmp_path, live=3, research=4)
    pong = sup.pong("live")
    assert pong["type"] == "PONG" and pong["image_digest"] == "mmr-build-id"
    assert pong["free"] == 3 and pong["pool"] == "live" and pong["live_open"] == 0
    _open(sup, tmp_path)
    assert sup.pong("research")["live_open"] == 1                     # every socket reports the live count


def test_a_closed_session_wipes_its_slot_outside_the_lock_and_frees_it(tmp_path):    # review r1 #2
    sup = _supervisor(tmp_path, live=1)
    wiped = []

    def wiper(slot):
        assert not sup._lock.locked()              # ruling 25: the wipe helper is never forked under the lock
        wiped.append(slot.uid)
        return True
    sup._wiper = wiper
    session = _open(sup, tmp_path)["session"]
    assert sup.handle("live", 1, p.parse_request({"v": 1, "type": "CLOSE", "session": session}))["type"] == "CLOSED"
    assert wiped == [20001] and sup.pong("live")["free"] == 1
    assert _open(sup, tmp_path, conn=2)["type"] == "OPENED"


def test_a_slot_that_cannot_be_wiped_is_never_reused(tmp_path):
    sup = _supervisor(tmp_path, live=1)
    sup._wiper = lambda slot: False
    session = _open(sup, tmp_path)["session"]
    sup.handle("live", 1, p.parse_request({"v": 1, "type": "CLOSE", "session": session}))
    reply = _open(sup, tmp_path, conn=2)
    assert sup.pong("live")["free"] == 0 and reply["code"] == p.SANDBOX_BUSY      # availability (ruling 34)
    assert "1 quarantined" in reply["detail"]


def test_the_wipe_helper_empties_the_directory_as_the_slot_uid(tmp_path, monkeypatch):
    from trader.sandbox.supervisor import Slot, wipe_slot_as_owner

    directory = tmp_path / "s20001"
    (directory / "nested").mkdir(parents=True)
    (directory / "leftover.csv").write_text("x")
    (directory / "nested" / "deep.txt").write_text("y")
    seen = tmp_path / "uid-seen"
    for name in ("setgroups", "setresgid"):
        monkeypatch.setattr(os, name, lambda *args: None, raising=False)       # a unit test is not root
    # raising=False: macOS has no setresuid/setresgid; production (Linux) has both.
    monkeypatch.setattr(os, "setresuid", lambda *ids: seen.write_text(str(ids)), raising=False)
    assert wipe_slot_as_owner(Slot(uid=20001, directory=str(directory))) is True
    assert list(directory.iterdir()) == [] and seen.read_text() == "(20001, 20001, 20001)"


def test_the_wipe_helper_undoes_a_hostile_chmod_on_its_slot(tmp_path, monkeypatch):
    # A child owns its slot dir, so it may chmod it (or a subdir) to 000 or add the sticky bit. The helper is that
    # same owner: it restores 0700 instead of failing, so one session cannot quarantine its slot for good.
    from trader.sandbox.supervisor import Slot, wipe_slot_as_owner

    directory = tmp_path / "s20003"
    (directory / "locked" / "inner").mkdir(parents=True)
    (directory / "locked" / "inner" / "deep.txt").write_text("y")
    (directory / "locked" / "kept.txt").write_text("x")
    (directory / "locked" / "inner").chmod(0)
    (directory / "locked").chmod(0o500)
    directory.chmod(0o1000)
    for name in ("setgroups", "setresgid", "setresuid"):
        monkeypatch.setattr(os, name, lambda *args: None, raising=False)       # a unit test is not root
    try:
        assert wipe_slot_as_owner(Slot(uid=20003, directory=str(directory))) is True
        assert list(directory.iterdir()) == [] and (directory.stat().st_mode & 0o7777) == 0o700
    finally:
        for path in (directory, directory / "locked", directory / "locked" / "inner"):
            if path.exists():
                path.chmod(0o700)   # let pytest clean up after a failure


def test_the_wipe_helper_resets_its_signals_before_anything_else(tmp_path, monkeypatch):
    # The fork seam: os.fork returns 0, so the helper's path runs in process; os._exit becomes an exception.
    from trader.sandbox import supervisor as supervisor_module
    from trader.sandbox.supervisor import Slot, wipe_slot_as_owner

    class Exited(BaseException):
        pass

    directory = tmp_path / "s20004"
    directory.mkdir()
    calls = []
    monkeypatch.setattr(os, "fork", lambda: 0)
    monkeypatch.setattr(os, "_exit", lambda code: (_ for _ in ()).throw(Exited(code)))
    monkeypatch.setattr(supervisor_module, "reset_signals_after_fork", lambda: calls.append("reset"))
    monkeypatch.setattr(supervisor_module.resource, "setrlimit", lambda *args: calls.append("rlimit"))
    monkeypatch.setattr(supervisor_module, "close_fds_except", lambda keep: calls.append("close_fds"))
    for name in ("setgroups", "setresgid", "setresuid"):
        monkeypatch.setattr(os, name, lambda *args, name=name: calls.append(name), raising=False)
    with pytest.raises(Exited) as exited:
        wipe_slot_as_owner(Slot(uid=20004, directory=str(directory)))
    assert calls == ["reset", "rlimit", "close_fds", "setgroups", "setresgid", "setresuid"]
    assert exited.value.args == (0,)


def test_a_helper_that_cannot_become_the_slot_uid_leaves_a_dirty_slot(tmp_path, monkeypatch):
    from trader.sandbox.supervisor import Slot, wipe_slot_as_owner

    directory = tmp_path / "s20002"
    directory.mkdir()
    (directory / "leftover.csv").write_text("x")
    monkeypatch.setattr(os, "setgroups", lambda *args: (_ for _ in ()).throw(PermissionError("not root")))
    assert wipe_slot_as_owner(Slot(uid=20002, directory=str(directory))) is False
    assert (directory / "leftover.csv").exists()


def test_the_production_pools_are_three_live_and_four_research():
    from trader.sandbox.supervisor import LIVE_POOL_SIZE, MAX_CONIDS_PER_SESSION, RESEARCH_POOL_SIZE

    assert (LIVE_POOL_SIZE, RESEARCH_POOL_SIZE) == (3, 4)     # live: one session per authored deployment
    assert MAX_CONIDS_PER_SESSION == {"live": p.MAX_SESSION_CONIDS, "research": 1}


def test_a_probe_loads_with_the_static_check_skipped(tmp_path):
    sup = _supervisor(tmp_path)
    opened = sup.handle("research", 1, p.parse_request(
        {"v": 1, "type": "OPEN_PROBE", "kind": "probe", "name": "benign_orb", "conids": [p.PROBE_CONID]}))
    assert opened["type"] == "OPENED"


# ── carry-forwards and slot-leak paths ──────────────────────────────────────

class _ScriptedChild:
    """A child whose send_bar runs a test-supplied function."""
    pid = -1

    def __init__(self, on_bar):
        self._on_bar = on_bar
        self.closes = 0

    def send_bar(self, conid, t, ohlcv):
        return self._on_bar(conid, t, ohlcv)

    def close(self):
        self.closes += 1


class _ScriptedSpawner:
    def __init__(self, make):
        self._make = make
        self.children = []

    def spawn(self, spec):
        child = self._make(spec)
        self.children.append(child)
        return child


def _scripted(tmp_path, make, live=1):
    spawner = _ScriptedSpawner(make)
    return _supervisor(tmp_path, live=live, spawners={"live": spawner, "research": InProcessSpawner()}), spawner


def test_a_bar_time_that_does_not_increase_is_a_bad_request_and_keeps_the_session(tmp_path):
    sup = _supervisor(tmp_path)
    session = _open(sup, tmp_path)["session"]
    assert _bar(sup, session, T0)["type"] == "SIGNAL"
    for when in (T0, T0 - timedelta(minutes=1)):
        reply = _bar(sup, session, when)
        assert reply["code"] == p.SANDBOX_BAD_REQUEST and "increase" in reply["detail"]
    assert _bar(sup, session, T0 + timedelta(minutes=1))["action"] == "BUY"   # the session still serves
    assert sup.pong("live")["free"] == 2


def test_bar_times_increase_per_conid_not_across_conids(tmp_path):
    sup = _supervisor(tmp_path)
    session = _open(sup, tmp_path, conids=(C1, C2))["session"]
    assert _bar(sup, session, T0 + timedelta(minutes=5), conid=C1)["type"] == "SIGNAL"
    assert _bar(sup, session, T0, conid=C2)["type"] == "SIGNAL"


def test_a_conid_outside_the_session_never_reaches_the_child(tmp_path):
    sup, spawner = _scripted(tmp_path, lambda spec: _ScriptedChild(
        lambda *args: pytest.fail("the child must not see this bar")))
    session = _open(sup, tmp_path)["session"]
    assert _bar(sup, session, T0, conid=C2)["code"] == p.SANDBOX_BAD_REQUEST
    assert spawner.children[0].closes == 0


def test_a_second_open_on_one_connection_is_a_bad_request(tmp_path):    # ruling 7
    sup = _supervisor(tmp_path)
    _open(sup, tmp_path, conn=1)
    reply = _open(sup, tmp_path, conn=1)
    assert reply["code"] == p.SANDBOX_BAD_REQUEST and "already holds a session" in reply["detail"]
    assert sup.pong("live")["free"] == 2


def test_a_connection_may_open_again_after_its_session_closed(tmp_path):
    sup = _supervisor(tmp_path)
    session = _open(sup, tmp_path, conn=1)["session"]
    sup.handle("live", 1, p.parse_request({"v": 1, "type": "CLOSE", "session": session}))
    assert _open(sup, tmp_path, conn=1)["type"] == "OPENED"


def test_a_fault_closes_the_child_and_frees_the_slot(tmp_path):
    spawner = InProcessSpawner()
    sup = _supervisor(tmp_path, live=1, spawners={"live": spawner, "research": InProcessSpawner()})
    source = GOOD.replace('probability=0.6', 'probability=7.0')
    digest = _store(tmp_path, source)
    session = sup.handle("live", 1, p.parse_request(_open_frame(digest)))["session"]
    _bar(sup, session, T0)
    assert _bar(sup, session, T0 + timedelta(minutes=1))["code"] == p.SANDBOX_ERROR
    assert spawner.closed == 1 and sup.pong("live")["free"] == 1


def test_any_exception_from_the_child_ends_the_session(tmp_path):
    def crash(*args):
        raise RuntimeError("pipe bug")
    sup, spawner = _scripted(tmp_path, lambda spec: _ScriptedChild(crash))
    session = _open(sup, tmp_path)["session"]
    reply = _bar(sup, session, T0)
    # A bug on the sandbox side, not a fault of the strategy (ruling 34).
    assert reply["code"] == p.SANDBOX_UNAVAILABLE and "internal sandbox error" in reply["detail"]
    assert "RuntimeError" in reply["detail"]
    assert spawner.children[0].closes == 1 and sup.pong("live")["free"] == 1
    assert _bar(sup, session, T0 + timedelta(minutes=1))["code"] == p.SANDBOX_BAD_REQUEST


def test_an_out_of_contract_signal_ends_the_session(tmp_path):
    sup, spawner = _scripted(tmp_path, lambda spec: _ScriptedChild(
        lambda *args: {"action": "HOLD", "probability": 0.5}))
    session = _open(sup, tmp_path)["session"]
    assert _bar(sup, session, T0)["code"] == p.SANDBOX_ERROR
    assert spawner.children[0].closes == 1 and sup.pong("live")["free"] == 1


def test_dropping_a_connection_closes_its_child(tmp_path):
    spawner = InProcessSpawner()
    sup = _supervisor(tmp_path, spawners={"live": spawner, "research": InProcessSpawner()})
    _open(sup, tmp_path, conn=1)
    _open(sup, tmp_path, conn=2)
    sup.drop_connection(1)
    sup.drop_connection(1)                                            # idempotent
    assert spawner.closed == 1 and sup.pong("live")["free"] == 2


def _raises(exc):
    def make(spec):
        raise exc
    return make


def test_a_load_fault_of_the_strategy_is_a_sandbox_error_and_frees_the_slot(tmp_path):
    from trader.sandbox.child import ChildError

    sup, _ = _scripted(tmp_path, _raises(ChildError("load failed: boom")))
    reply = _open(sup, tmp_path)
    assert reply["code"] == p.SANDBOX_ERROR and "boom" in reply["detail"]
    assert sup.pong("live")["free"] == 1


@pytest.mark.parametrize("failure, marker", [
    ("PRIVILEGE_DROP_FAILED", "PRIVILEGE_DROP_FAILED"),
    ("CHILD_LIMITS_FAILED", "CHILD_LIMITS_FAILED"),
    ("CHILD_SETUP_FAILED", "CHILD_SETUP_FAILED"),
    (OSError(11, "Resource temporarily unavailable"), "OSError"),     # fork or socketpair: EAGAIN
    (OSError(24, "Too many open files"), "OSError"),
    (RuntimeError("spawner bug"), "RuntimeError"),
])
def test_a_spawn_failure_of_the_sandbox_is_availability_and_frees_the_slot(tmp_path, failure, marker):
    # Ruling 34: the strategy did not cause it, so it must not be a SANDBOX_ERROR that retires the strategy.
    from trader.sandbox.child import ChildSetupError

    exc = ChildSetupError(failure) if isinstance(failure, str) else failure
    sup, _ = _scripted(tmp_path, _raises(exc))
    reply = _open(sup, tmp_path)
    assert reply["code"] == p.SANDBOX_UNAVAILABLE and marker in reply["detail"]
    assert sup.pong("live")["free"] == 1


def test_a_connection_dropped_during_open_frees_the_slot(tmp_path):     # ruling 7: no leaked slot
    holder = {}

    def make(spec):
        holder["sup"].drop_connection(1)          # the caller went away while the child loaded
        return _ScriptedChild(lambda *args: {"action": None, "probability": None})
    sup, spawner = _scripted(tmp_path, make)
    holder["sup"] = sup
    reply = _open(sup, tmp_path, conn=1)
    assert reply["type"] == "ERROR" and reply["code"] == p.SANDBOX_UNAVAILABLE   # the caller left: no fault
    assert spawner.children[0].closes == 1 and sup.pong("live")["free"] == 1
    assert sup.pong("live")["live_open"] == 0


def test_a_second_bar_while_one_is_in_flight_is_refused_and_the_lock_is_free(tmp_path):    # ruling 25
    import threading

    entered, release = threading.Event(), threading.Event()
    holder = {}

    def slow(*args):
        assert not holder["sup"]._lock.locked()       # never across a bar round-trip
        entered.set()
        release.wait(5)
        return {"action": None, "probability": None}
    sup, _ = _scripted(tmp_path, lambda spec: _ScriptedChild(slow))
    holder["sup"] = sup
    session = _open(sup, tmp_path)["session"]
    replies = []
    worker = threading.Thread(target=lambda: replies.append(_bar(sup, session, T0)))
    worker.start()
    assert entered.wait(5)
    reply = _bar(sup, session, T0 + timedelta(minutes=1))
    assert reply["code"] == p.SANDBOX_BAD_REQUEST and "in flight" in reply["detail"]
    assert sup.pong("research")["free"] == 4                          # the other pool is not blocked
    release.set()
    worker.join(5)
    assert replies[0]["type"] == "SIGNAL"
    assert _bar(sup, session, T0 + timedelta(minutes=2))["type"] == "SIGNAL"


def test_a_drop_during_a_bar_ends_the_session_once(tmp_path):    # the server's 2 s timeout path
    import threading

    entered, release = threading.Event(), threading.Event()
    wiped = []

    def slow(*args):
        entered.set()
        release.wait(5)
        return {"action": "BUY", "probability": 0.5}
    sup, spawner = _scripted(tmp_path, lambda spec: _ScriptedChild(slow))
    sup._wiper = lambda slot: wiped.append(slot.uid) or True
    session = _open(sup, tmp_path)["session"]
    replies = []
    worker = threading.Thread(target=lambda: replies.append(_bar(sup, session, T0)))
    worker.start()
    assert entered.wait(5)
    sup.drop_connection(1)
    release.set()
    worker.join(5)
    assert replies[0]["code"] == p.SANDBOX_ERROR and "ended" in replies[0]["detail"]
    assert spawner.children[0].closes == 1 and wiped == [20001] and sup.pong("live")["free"] == 1


def test_open_passes_the_lookback_ceiling_and_a_slot_of_its_pool(tmp_path):
    spawners = {"live": InProcessSpawner(), "research": InProcessSpawner()}
    sup = _supervisor(tmp_path, spawners=spawners)
    _open(sup, tmp_path, pool="live")
    _open(sup, tmp_path, conn=2, pool="research")
    live_spec, research_spec = spawners["live"].specs[0], spawners["research"].specs[0]
    assert live_spec.lookback == research_spec.lookback == 2000
    assert live_spec.slot_uid in range(20001, 20004) and research_spec.slot_uid in range(20004, 20008)
    assert live_spec.slot_dir == f"/tmp/s{live_spec.slot_uid}"
    assert live_spec.skip_static_check is False                       # the store path never skips (ruling 11)


def test_an_unknown_pool_is_a_bad_request(tmp_path):
    sup = _supervisor(tmp_path)
    reply = _open(sup, tmp_path, pool="other")
    assert reply["code"] == p.SANDBOX_BAD_REQUEST and "unknown pool" in reply["detail"]


def test_the_supervisor_refuses_a_pool_it_cannot_serve(tmp_path):
    def build(**overrides):
        kwargs = dict(spawners={"live": InProcessSpawner(), "research": InProcessSpawner()},
                      pool_sizes={"live": 3, "research": 4}, store_dir=str(tmp_path),
                      image_digest="mmr-build-id", posture={})
        kwargs.update(overrides)
        return Supervisor(**kwargs)
    with pytest.raises(ValueError, match="slot uids"):
        build(pool_sizes={"live": 4, "research": 4})
    with pytest.raises(ValueError, match="unknown pool"):
        build(pool_sizes={"other": 1})
    with pytest.raises(ValueError, match="no spawner"):
        build(spawners={"live": InProcessSpawner()})


def test_pong_carries_the_supervisor_pid(tmp_path):     # ruling 10
    pong = _supervisor(tmp_path).pong("live")
    assert pong["pid"] == os.getpid() and pong["size"] == 3 and pong["boot_id"]


# ── the store file ──────────────────────────────────────────────────────────

def test_a_store_symlink_is_a_digest_mismatch(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.py").write_text(GOOD)
    digest = "sha256:" + hashlib.sha256(GOOD.encode()).hexdigest()
    store = tmp_path / "store"
    store.mkdir()
    (store / f"{digest.split(':')[1]}.py").symlink_to(outside / "real.py")
    sup = Supervisor(spawners={"live": InProcessSpawner()}, pool_sizes={"live": 1}, store_dir=str(store),
                     image_digest="mmr-build-id", posture={}, wiper=lambda slot: True)
    assert sup.handle("live", 1, p.parse_request(_open_frame(digest)))["code"] == p.SANDBOX_DIGEST_MISMATCH


def test_a_store_fifo_is_a_digest_mismatch_and_never_blocks(tmp_path):
    digest = "sha256:" + "b" * 64
    os.mkfifo(tmp_path / f"{'b' * 64}.py")
    sup = _supervisor(tmp_path)
    assert sup.handle("live", 1, p.parse_request(_open_frame(digest)))["code"] == p.SANDBOX_DIGEST_MISMATCH


def test_an_oversized_store_file_is_a_static_rejection(tmp_path):
    from trader.authored.static_check import MAX_SOURCE_BYTES

    source = GOOD + "#" * MAX_SOURCE_BYTES + "\n"
    sup = _supervisor(tmp_path)
    reply = sup.handle("live", 1, p.parse_request(_open_frame(_store(tmp_path, source))))
    assert reply["code"] == p.SANDBOX_ERROR and reply["detail"] == "AUTHORED_STATIC_REJECTED:oversize"
    assert sup.pong("live")["free"] == 3


def test_a_store_file_that_is_not_utf8_is_a_static_rejection(tmp_path):
    raw = GOOD.encode() + b"# \xff\n"
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    (tmp_path / f"{digest.split(':')[1]}.py").write_bytes(raw)
    reply = _supervisor(tmp_path).handle("live", 1, p.parse_request(_open_frame(digest)))
    assert reply["code"] == p.SANDBOX_ERROR and reply["detail"] == "AUTHORED_STATIC_REJECTED:unparseable"


# ── probes (ruling 11) ──────────────────────────────────────────────────────

def _probe(sup, kind, name, conn=1):
    return sup.handle("research", conn, p.parse_request(
        {"v": 1, "type": "OPEN_PROBE", "kind": kind, "name": name, "conids": [p.PROBE_CONID]}))


def test_a_probe_session_is_spawned_with_the_static_check_skipped(tmp_path):
    spawners = {"live": InProcessSpawner(), "research": InProcessSpawner()}
    sup = _supervisor(tmp_path, spawners=spawners)
    session = _probe(sup, "probe", "benign_orb")["session"]
    spec = spawners["research"].specs[0]
    assert spec.skip_static_check is True and spec.class_name == "BenignOrb"
    assert spec.conids == (p.PROBE_CONID,)
    reply = sup.handle("research", 1, p.parse_request(
        {"v": 1, "type": "BAR", "session": session, "conid": p.PROBE_CONID, "t": T0.isoformat(), **OHLCV}))
    assert reply["type"] == "SIGNAL"


def test_an_unknown_probe_is_a_bad_request(tmp_path):
    sup = _supervisor(tmp_path)
    for kind, name in (("probe", "no_such_probe"), ("probe", "__init__"), ("escape", "benign_orb")):
        reply = _probe(sup, kind, name)
        assert reply["code"] == p.SANDBOX_BAD_REQUEST, (kind, name, reply)
    assert sup.pong("research")["free"] == 4


def test_an_escape_name_resolves_in_the_escape_directory(tmp_path):
    # The in-process fake never runs hostile code: it refuses the escape at load, which proves the
    # name was found in tests/authored/escapes (an unknown name would be a BAD_REQUEST).
    sup = _supervisor(tmp_path)
    reply = _probe(sup, "escape", "escape_import_os")
    assert reply["code"] == p.SANDBOX_ERROR and "AUTHORED_STATIC_REJECTED" in reply["detail"]
    assert sup.pong("research")["free"] == 4


def test_the_probe_directories_are_the_shipped_ones():
    from trader.sandbox.supervisor import ESCAPE_DIR, PROBE_DIR

    assert (PROBE_DIR / "benign_orb.py").is_file() and (PROBE_DIR / "writes_own_slot.py").is_file()
    assert (ESCAPE_DIR / "escape_import_os.py").is_file()


@pytest.mark.parametrize("name, class_name", [("holds_only_its_pipe", "HoldsOnlyItsPipe"),
                                               ("ptrace_denied", "PtraceDenied"),
                                               ("runs_as_slot_uid", "RunsAsSlotUid")])
def test_the_container_check_probes_ship_with_one_class_each(name, class_name):
    # Task 11: these run only in a real forked child (tests/fullstack/test_sandbox_service.py); here only their shape.
    from trader.sandbox.supervisor import _only_class, _read_probe_source

    assert _only_class(_read_probe_source("probe", name)) == class_name


# ── the wipe helper (ruling 28) ─────────────────────────────────────────────

def test_the_wipe_helper_never_follows_a_symlink_out_of_the_slot(tmp_path, monkeypatch):
    from trader.sandbox.supervisor import Slot, wipe_slot_as_owner

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("k")
    directory = tmp_path / "s20003"
    directory.mkdir()
    (directory / "link").symlink_to(outside, target_is_directory=True)
    for name in ("setgroups", "setresgid", "setresuid"):
        monkeypatch.setattr(os, name, lambda *args: None, raising=False)
    assert wipe_slot_as_owner(Slot(uid=20003, directory=str(directory))) is True
    assert list(directory.iterdir()) == [] and (outside / "keep.txt").exists()


# ── review fixes: defence in depth ──────────────────────────────────────────

def test_a_probe_on_the_live_pool_is_a_bad_request(tmp_path):     # ruling 32
    sup = _supervisor(tmp_path)
    reply = sup.handle("live", 1, p.parse_request(
        {"v": 1, "type": "OPEN_PROBE", "kind": "probe", "name": "benign_orb", "conids": [p.PROBE_CONID]}))
    assert reply["code"] == p.SANDBOX_BAD_REQUEST and reply["detail"] == "probes are research-only"
    assert sup.pong("live")["free"] == 3 and sup.pong("live")["live_open"] == 0


def test_the_wipe_helper_closes_inherited_fds_before_it_drops_to_the_slot_uid(tmp_path, monkeypatch):
    from trader.sandbox import supervisor as sv

    directory = tmp_path / "s20001"
    directory.mkdir()
    order = tmp_path / "order"

    def record(name, real=None):
        def step(*args):
            with open(order, "a") as out:
                out.write(f"{name}:{args!r}\n")
            if real is not None:
                real(*args)
        return step
    monkeypatch.setattr(sv, "close_fds_except", record("close_fds", sv.close_fds_except))
    for name in ("setgroups", "setresgid", "setresuid"):
        monkeypatch.setattr(os, name, record(name), raising=False)
    assert sv.wipe_slot_as_owner(sv.Slot(uid=20001, directory=str(directory))) is True
    steps = [line.split(":")[0] for line in order.read_text().splitlines()]
    assert steps == ["close_fds", "setgroups", "setresgid", "setresuid"]
    assert "close_fds:(set(),)" in order.read_text()          # keeps only stdout and stderr


def test_a_wipe_helper_that_never_exits_is_killed_and_the_slot_is_dirty(tmp_path, monkeypatch):
    import time

    from trader.sandbox import supervisor as sv

    directory = tmp_path / "s20002"
    directory.mkdir()
    for name in ("setgroups", "setresgid", "setresuid"):
        monkeypatch.setattr(os, name, lambda *args: None, raising=False)
    monkeypatch.setattr(sv, "_empty", lambda path: time.sleep(60))     # a helper stuck forever
    started = time.monotonic()
    assert sv.wipe_slot_as_owner(sv.Slot(uid=20002, directory=str(directory)), timeout_s=0.3) is False
    assert time.monotonic() - started < 5



# ── no probe beside a live child (ruling 29, Task 6 review) ────────────────

def test_a_probe_is_busy_while_a_live_session_is_open(tmp_path):
    sup = _supervisor(tmp_path)
    live = _open(sup, tmp_path, conn=1)["session"]
    reply = _probe(sup, "probe", "benign_orb", conn=2)
    assert reply["code"] == p.SANDBOX_BUSY and reply["detail"] == "live sessions open"
    assert sup.pong("research")["free"] == 4
    assert sup.handle("live", 1, p.parse_request({"v": 1, "type": "CLOSE", "session": live}))["type"] == "CLOSED"
    assert _probe(sup, "probe", "benign_orb", conn=2)["type"] == "OPENED"


def test_a_probe_is_busy_while_a_live_session_is_still_opening(tmp_path):
    import threading

    entered, release = threading.Event(), threading.Event()

    class SlowLive(InProcessSpawner):
        def spawn(self, spec):
            entered.set()
            assert release.wait(5)
            return super().spawn(spec)

    sup = _supervisor(tmp_path, spawners={"live": SlowLive(), "research": InProcessSpawner()})
    replies = []
    worker = threading.Thread(target=lambda: replies.append(_open(sup, tmp_path, conn=1)))
    worker.start()
    try:
        assert entered.wait(5)
        reply = _probe(sup, "probe", "benign_orb", conn=2)
        assert reply["code"] == p.SANDBOX_BUSY and reply["detail"] == "live sessions open"
    finally:
        release.set()
        worker.join(5)
    assert replies[0]["type"] == "OPENED"
