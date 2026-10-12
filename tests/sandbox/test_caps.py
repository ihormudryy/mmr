import os
import stat
import sys

import pytest

from trader.sandbox import caps
from trader.sandbox.child import SBXCHILD_GID, SLOT_UIDS

BEFORE_DROP = 0xE1   # CHOWN|KILL|SETGID|SETUID: the supervisor at startup
SERVING = 0xE0       # KILL|SETGID|SETUID


def test_slot_dirs_are_named_by_uid():
    assert caps.slot_dirs([20001, 20002]) == ["/tmp/s20001", "/tmp/s20002"]


def test_the_supervisor_uses_the_slot_dirs_that_startup_prepares():
    from trader.sandbox.supervisor import LIVE_POOL_SIZE, RESEARCH_POOL_SIZE, _build_pool

    spawners = {"live": object(), "research": object()}
    pools = [_build_pool("live", LIVE_POOL_SIZE, spawners), _build_pool("research", RESEARCH_POOL_SIZE, spawners)]
    assert sorted(slot.directory for pool in pools for slot in pool.free_slots) == caps.slot_dirs(SLOT_UIDS)


def _status(tmp_path, monkeypatch, cap_eff=SERVING, cap_prm=None, threads=1):
    path = tmp_path / "status"
    _write_status(path, cap_eff, cap_eff if cap_prm is None else cap_prm, threads)
    monkeypatch.setattr(caps, "_STATUS_PATH", str(path))
    return path


def _write_status(path, cap_eff, cap_prm, threads=1):
    path.write_text(f"Name:\tpython\nThreads:\t{threads}\nCapInh:\t0000000000000000\n"
                    f"CapPrm:\t{cap_prm:016x}\nCapEff:\t{cap_eff:016x}\n")


def test_has_effective_cap_reads_proc_status(monkeypatch, tmp_path):
    status = tmp_path / "status"
    status.write_text("CapEff:\t00000000000000e0\n")
    monkeypatch.setattr(caps, "_STATUS_PATH", str(status))
    assert caps.has_effective_cap(caps.CAP_KILL) is True     # bit 5
    assert caps.has_effective_cap(caps.CAP_CHOWN) is False    # bit 0


def test_cap_chown_is_bit_zero():
    assert caps.CAP_CHOWN == 0 and caps.CAP_KILL == 5 and caps.CAP_SETGID == 6 and caps.CAP_SETUID == 7
    assert caps.SERVING_CAPS == SERVING


@pytest.mark.parametrize("content", [None, "Name:\tpython\n", "CapEff:\tnot-hex\n", b"CapEff:\t\xff\n"])
def test_an_unreadable_cap_set_is_an_error_never_a_clean_answer(monkeypatch, tmp_path, content):
    status = tmp_path / "status"
    if isinstance(content, bytes):
        status.write_bytes(content)
    elif content is not None:
        status.write_text(content)
    monkeypatch.setattr(caps, "_STATUS_PATH", str(status))
    with pytest.raises(caps.CapabilityError):
        caps.has_effective_cap(caps.CAP_CHOWN)
    with pytest.raises(caps.CapabilityError):
        caps.require_serving_caps()


def test_the_serving_set_must_be_exactly_setuid_setgid_kill(monkeypatch, tmp_path):
    _status(tmp_path, monkeypatch, cap_eff=SERVING)
    caps.require_serving_caps()
    for wrong in (BEFORE_DROP, SERVING | (1 << 1), 1 << caps.CAP_KILL, 0):   # CHOWN kept, DAC_OVERRIDE added, ...
        _status(tmp_path, monkeypatch, cap_eff=wrong)
        with pytest.raises(caps.CapabilityError, match="serving"):
            caps.require_serving_caps()


class FakeCapSyscalls:
    """Stands in for capget/capset: a test must never change this process's real capabilities."""

    def __init__(self, status_path, effective=BEFORE_DROP, permitted=BEFORE_DROP, inheritable=1 << caps.CAP_CHOWN,
                 set_error=None, kernel_ignores_set=False):
        self.status_path = status_path
        self.words = [caps.CapWord(effective, permitted, inheritable), caps.CapWord(0, 0, 0)]
        self.set_calls = []
        self.set_error = set_error
        self.kernel_ignores_set = kernel_ignores_set

    def get(self):
        return list(self.words)

    def set(self, words):
        self.set_calls.append(list(words))
        if self.set_error is not None:
            raise self.set_error
        if not self.kernel_ignores_set:
            self.words = list(words)
            _write_status(self.status_path, words[0].effective, words[0].permitted)


def test_dropping_chown_clears_it_from_effective_permitted_and_inheritable(monkeypatch, tmp_path):
    path = _status(tmp_path, monkeypatch, cap_eff=BEFORE_DROP)
    fake = FakeCapSyscalls(path)
    caps.drop_capability(caps.CAP_CHOWN, syscalls=fake)
    assert fake.set_calls == [[caps.CapWord(SERVING, SERVING, 0), caps.CapWord(0, 0, 0)]]
    assert caps.has_effective_cap(caps.CAP_CHOWN) is False
    caps.require_serving_caps()


def test_a_capability_above_bit_31_is_cleared_in_the_second_word(monkeypatch, tmp_path):
    path = _status(tmp_path, monkeypatch)
    fake = FakeCapSyscalls(path)
    fake.words[1] = caps.CapWord(0b11, 0b11, 0)
    caps.drop_capability(33, syscalls=fake)
    assert fake.set_calls[0][1] == caps.CapWord(0b01, 0b01, 0)
    assert fake.set_calls[0][0] == fake.words[0]


@pytest.mark.parametrize("bit", [-1, 64, True, "0", 0.0])
def test_a_bad_capability_bit_is_refused(monkeypatch, tmp_path, bit):
    fake = FakeCapSyscalls(_status(tmp_path, monkeypatch))
    with pytest.raises(ValueError):
        caps.drop_capability(bit, syscalls=fake)
    assert fake.set_calls == []


def test_a_failed_capset_is_raised(monkeypatch, tmp_path):
    fake = FakeCapSyscalls(_status(tmp_path, monkeypatch, cap_eff=BEFORE_DROP),
                           set_error=OSError(1, "capset failed"))
    with pytest.raises(OSError, match="capset failed"):
        caps.drop_capability(caps.CAP_CHOWN, syscalls=fake)


def test_a_drop_the_kernel_did_not_apply_is_an_error(monkeypatch, tmp_path):
    fake = FakeCapSyscalls(_status(tmp_path, monkeypatch, cap_eff=BEFORE_DROP), kernel_ignores_set=True)
    with pytest.raises(caps.CapabilityError, match="still"):
        caps.drop_capability(caps.CAP_CHOWN, syscalls=fake)


def test_a_drop_that_leaves_the_bit_permitted_is_an_error(monkeypatch, tmp_path):
    path = _status(tmp_path, monkeypatch, cap_eff=BEFORE_DROP)
    fake = FakeCapSyscalls(path)
    fake.set = lambda words: _write_status(path, SERVING, BEFORE_DROP)   # effective cleared, permitted kept
    with pytest.raises(caps.CapabilityError, match="still"):
        caps.drop_capability(caps.CAP_CHOWN, syscalls=fake)


def test_the_drop_refuses_while_other_threads_run(monkeypatch, tmp_path):
    # capset changes only the calling thread: another thread would keep CAP_CHOWN.
    fake = FakeCapSyscalls(_status(tmp_path, monkeypatch, cap_eff=BEFORE_DROP, threads=3))
    with pytest.raises(caps.CapabilityError, match="3 threads"):
        caps.drop_capability(caps.CAP_CHOWN, syscalls=fake)
    assert fake.set_calls == []


def test_the_real_drop_refuses_off_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(caps.CapabilityError, match="Linux"):
        caps.drop_capability(caps.CAP_CHOWN)


@pytest.mark.parametrize("failing_step", ["prepare_slot_dirs", "drop_capability", "require_serving_caps"])
def test_a_failed_slot_setup_or_drop_refuses_to_start(monkeypatch, failing_step):
    calls = []
    for name in ("prepare_slot_dirs", "drop_capability", "require_serving_caps"):
        monkeypatch.setattr(caps, name, lambda *args, name=name: calls.append(name))

    def fail(*args):
        calls.append(failing_step)
        raise (OSError(1, "Operation not permitted") if failing_step == "prepare_slot_dirs"
               else caps.CapabilityError("capability 0 is still effective"))

    monkeypatch.setattr(caps, failing_step, fail)
    with pytest.raises(SystemExit, match="sandbox refuses to start"):
        caps.prepare_slots_then_drop_chown()
    order = ["prepare_slot_dirs", "drop_capability", "require_serving_caps"]
    assert calls == order[:order.index(failing_step) + 1]


def test_startup_prepares_all_seven_slots_then_drops_chown_then_checks(monkeypatch):
    calls = []
    monkeypatch.setattr(caps, "prepare_slot_dirs", lambda uids: calls.append(("prepare", tuple(uids))))
    monkeypatch.setattr(caps, "drop_capability", lambda bit: calls.append(("drop", bit)))
    monkeypatch.setattr(caps, "require_serving_caps", lambda: calls.append("require"))
    caps.prepare_slots_then_drop_chown()
    assert calls == [("prepare", tuple(range(20001, 20008))), ("drop", caps.CAP_CHOWN), "require"]


# ── slot directories ────────────────────────────────────────────────────────
def test_prepare_makes_each_slot_dir_owned_by_its_uid_and_group_mode_0700(tmp_path):
    # Not root here: "chown" to this process's own uid and gid, which needs no capability.
    uid, gid = os.getuid(), os.getgid()
    caps.prepare_slot_dirs([uid], gid=gid, root=str(tmp_path))
    info = os.lstat(tmp_path / f"s{uid}")
    assert stat.S_ISDIR(info.st_mode) and (info.st_uid, info.st_gid) == (uid, gid)
    assert stat.S_IMODE(info.st_mode) == 0o700


def test_prepare_sets_the_mode_before_giving_the_dir_away(tmp_path, monkeypatch):
    # Root without CAP_FOWNER cannot chmod a dir it no longer owns: chmod must come before chown.
    calls = []
    real_fchmod, real_fchown = os.fchmod, os.fchown
    monkeypatch.setattr(os, "fchmod", lambda fd, mode: calls.append("chmod") or real_fchmod(fd, mode))
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: calls.append("chown") or real_fchown(fd, uid, gid))
    caps.prepare_slot_dirs([os.getuid()], gid=os.getgid(), root=str(tmp_path))
    assert calls == ["chmod", "chown"]


def test_prepare_defaults_to_group_sbxchild(tmp_path, monkeypatch):
    owners = []
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: owners.append((uid, gid)))
    with pytest.raises(caps.SlotDirError, match="owner"):   # the fake chown changed nothing: caught
        caps.prepare_slot_dirs([20001], root=str(tmp_path))
    assert owners == [(20001, SBXCHILD_GID)]


def test_prepare_refuses_a_slot_dir_that_already_exists(tmp_path):
    # A fresh tmpfs has none; one that exists may hold an earlier session's files (ruling 13).
    (tmp_path / f"s{os.getuid()}").mkdir()
    with pytest.raises(caps.SlotDirError, match="already exists"):
        caps.prepare_slot_dirs([os.getuid()], gid=os.getgid(), root=str(tmp_path))


def test_prepare_refuses_a_symlink_in_place_of_a_slot_dir(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / f"s{os.getuid()}").symlink_to(target)
    with pytest.raises(caps.SlotDirError, match="already exists"):
        caps.prepare_slot_dirs([os.getuid()], gid=os.getgid(), root=str(tmp_path))
