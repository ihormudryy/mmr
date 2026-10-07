"""SP2 Plan 1 Task 1: the trader-granted controller epoch and its lease (spec 5.1, 6.2)."""
from __future__ import annotations

import datetime as dt
import threading

import pytest

from trader.automation.controller_epoch import (
    EPOCH_HELD, EPOCH_MISSING, EPOCH_STALE, EPOCH_UNKNOWN, ControllerEpochs, EpochRefused,
    apply_controller_epoch_migration,
)
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

NOW = dt.datetime(2026, 10, 7, 14, 0, tzinfo=dt.timezone.utc)


class Clock:
    """A settable trader clock; Plan 1's other test files import it."""

    def __init__(self, start=NOW):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += dt.timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def epochs(tmp_path, clock):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    assert apply_controller_epoch_migration(migrator) is True
    assert apply_controller_epoch_migration(migrator) is False
    return ControllerEpochs(journal=journal, now=clock)


def refused(fn, **kwargs):
    with pytest.raises(EpochRefused) as exc:
        fn(**kwargs)
    return exc.value.code


def test_first_grant_is_epoch_one_with_a_lease(epochs):
    grant = epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    assert (grant.epoch, grant.renewed) == (1, False)
    assert grant.lease_expires_at == NOW + dt.timedelta(seconds=60)


def test_renew_keeps_the_epoch_and_moves_the_lease(epochs, clock):
    epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    clock.advance(20)
    grant = epochs.grant(holder_id="ctl-a", current_epoch=1, lease_seconds=60)
    assert (grant.epoch, grant.renewed) == (1, True)
    assert grant.lease_expires_at == NOW + dt.timedelta(seconds=80)


def test_another_holder_is_held_while_the_lease_lives(epochs, clock):
    epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    clock.advance(59)
    assert refused(epochs.grant, holder_id="ctl-b", current_epoch=None, lease_seconds=60) == EPOCH_HELD


def test_takeover_after_expiry_gets_the_next_epoch(epochs, clock):
    epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    clock.advance(60)
    assert epochs.grant(holder_id="ctl-b", current_epoch=None, lease_seconds=60).epoch == 2
    assert refused(epochs.grant, holder_id="ctl-a", current_epoch=1, lease_seconds=60) == EPOCH_HELD


def test_same_holder_without_its_epoch_is_held_until_expiry(epochs, clock):     # Review Focus 5, Ruling 3
    epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    assert refused(epochs.grant, holder_id="ctl-a", current_epoch=None, lease_seconds=60) == EPOCH_HELD
    clock.advance(60)
    assert epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60).epoch == 2


def test_an_expired_holder_renewing_gets_a_new_epoch(epochs, clock):
    epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    clock.advance(61)
    grant = epochs.grant(holder_id="ctl-a", current_epoch=1, lease_seconds=60)
    assert (grant.epoch, grant.renewed) == (2, False)


def test_an_epoch_never_granted_is_unknown(epochs):
    assert refused(epochs.grant, holder_id="ctl-a", current_epoch=1, lease_seconds=60) == EPOCH_UNKNOWN
    epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    assert refused(epochs.grant, holder_id="ctl-a", current_epoch=7, lease_seconds=60) == EPOCH_UNKNOWN


def test_require_current(epochs, clock):
    assert refused(epochs.require_current, epoch=None) == EPOCH_MISSING
    assert refused(epochs.require_current, epoch=1) == EPOCH_STALE         # nothing granted yet
    epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    epochs.require_current(1)
    clock.advance(600)
    epochs.require_current(1)                                              # Ruling 2: expiry alone is not stale
    epochs.grant(holder_id="ctl-b", current_epoch=None, lease_seconds=60)
    assert refused(epochs.require_current, epoch=1) == EPOCH_STALE
    epochs.require_current(2)


@pytest.mark.parametrize("holder,current,lease", [
    ("Ctl-A", None, 60), ("ctl:a", None, 60), ("ctl-a", None, 9), ("ctl-a", None, 601),
    ("ctl-a", None, True), ("ctl-a", True, 60), ("ctl-a", 0, 60)])
def test_bad_grant_input_is_a_value_error(epochs, holder, current, lease):
    with pytest.raises(ValueError):
        epochs.grant(holder_id=holder, current_epoch=current, lease_seconds=lease)


def test_concurrent_grants_give_one_epoch(epochs):                            # Review Focus 5
    barrier, results = threading.Barrier(2), {}

    def run(holder):
        barrier.wait()
        try:
            results[holder] = epochs.grant(holder_id=holder, current_epoch=None, lease_seconds=60).epoch
        except EpochRefused as ex:
            results[holder] = ex.code
    threads = [threading.Thread(target=run, args=(h,)) for h in ("ctl-a", "ctl-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(map(str, results.values())) == ["1", EPOCH_HELD]
