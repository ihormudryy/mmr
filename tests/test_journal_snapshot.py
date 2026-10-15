"""Issue #189: the trader, which holds the journal open, writes an hourly snapshot the backups can copy."""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from types import SimpleNamespace

import duckdb
import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.journal_snapshot import snapshot_path, write_journal_snapshot


@pytest.fixture
def journal(tmp_path):
    path = str(tmp_path / "mmr.duckdb.journal")
    db = DuckDBConnection.get_instance(path)
    db.execute("CREATE TABLE command_ledger (command_id VARCHAR PRIMARY KEY, state VARCHAR)")
    db.execute("INSERT INTO command_ledger VALUES ('cmd-1', 'RESOLVED'), ('cmd-2', 'REJECTED')")
    return path, db


def _rows(path):
    with duckdb.connect(path, read_only=True) as conn:
        return conn.execute("SELECT command_id, state FROM command_ledger ORDER BY 1").fetchall()


def test_the_snapshot_is_a_full_copy_of_the_journal(journal):
    path, _db = journal
    size = write_journal_snapshot(path)
    assert size > 0 and os.path.getsize(snapshot_path(path)) == size
    assert _rows(snapshot_path(path)) == [("cmd-1", "RESOLVED"), ("cmd-2", "REJECTED")]


def test_a_new_snapshot_replaces_the_old_one_and_leaves_no_partial_file(journal):
    path, db = journal
    write_journal_snapshot(path)
    db.execute("INSERT INTO command_ledger VALUES ('cmd-3', 'RESOLVED')")
    write_journal_snapshot(path)
    assert [row[0] for row in _rows(snapshot_path(path))] == ["cmd-1", "cmd-2", "cmd-3"]
    assert sorted(os.listdir(os.path.dirname(path))) == ["mmr.duckdb.journal", "mmr.duckdb.journal.snapshot.duckdb"]


def test_the_snapshot_name_is_picked_up_by_the_duckdb_glob_of_both_backups(journal):
    path, _db = journal
    assert snapshot_path(path).endswith(".duckdb") and snapshot_path(path) != path


def test_the_trader_starts_the_snapshot_loop(monkeypatch):
    from trader import trader_service
    started = []

    class Loop:
        def create_task(self, coro):
            started.append(coro)
            coro.close()

    trader_service._maybe_start_journal_snapshot(SimpleNamespace(journal_duckdb_path="/x/mmr.duckdb.journal"), Loop())
    assert len(started) == 1


def _note(tmp_path, *, age_minutes):
    from trader import mmr_cli
    journal_path = tmp_path / "mmr.duckdb.journal"
    journal_path.write_bytes(b"")
    snapshot = tmp_path / "mmr.duckdb.journal.snapshot.duckdb"
    now = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
    if age_minutes is not None:
        snapshot.write_bytes(b"x")
        stamp = (now - dt.timedelta(minutes=age_minutes)).timestamp()
        os.utime(snapshot, (stamp, stamp))
    summary = {"databases": [{"db": snapshot.name, "ok": True}] if age_minutes is not None else []}
    return mmr_cli._journal_backup_note(str(journal_path), summary, now=now)


def test_the_backup_names_the_snapshot_and_its_age(tmp_path):
    note = _note(tmp_path, age_minutes=20)
    assert note["included_as"] == "mmr.duckdb.journal.snapshot.duckdb" and note["snapshot_age_minutes"] == 20
    assert "warning" not in note


def test_a_stale_snapshot_is_a_warning(tmp_path):
    assert "older than" in _note(tmp_path, age_minutes=180)["warning"]


def test_a_missing_snapshot_says_the_journal_is_not_in_the_backup(tmp_path):
    note = _note(tmp_path, age_minutes=None)
    assert note["included_as"] is None and "./docker.sh -B" in note["warning"]
