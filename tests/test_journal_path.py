"""Issue #174: one journal file for every process.

Without `journal_duckdb_path` the trader opened `<duckdb_path>.journal` while the CLI and MMRConfig defaulted to
`mmr_journal.duckdb`, and both backup paths skipped the journal.
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from trader.config import MMRConfig, default_journal_path
from trader.data.db_backup import run_backup

ROOT = Path(__file__).resolve().parents[1]


def _config(tmp_path, extra=""):
    path = tmp_path / "trader.yaml"
    path.write_text(f"duckdb_path: {tmp_path / 'data' / 'mmr.duckdb'}\n{extra}")
    return MMRConfig.from_yaml(str(path))


def test_without_the_key_the_journal_is_the_file_the_trader_has_always_opened(tmp_path):
    assert _config(tmp_path).storage.journal_duckdb_path == str(tmp_path / "data" / "mmr.duckdb.journal")


def test_a_configured_journal_path_wins(tmp_path):
    configured = tmp_path / "elsewhere.duckdb"
    assert _config(tmp_path, f"journal_duckdb_path: {configured}\n").storage.journal_duckdb_path == str(configured)


def test_two_journal_files_and_no_key_are_refused(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "mmr.duckdb.journal").write_bytes(b"")
    (data / "mmr_journal.duckdb").write_bytes(b"")
    with pytest.raises(ValueError, match="journal_duckdb_path"):
        default_journal_path(str(data / "mmr.duckdb"))


def test_the_cli_reads_the_resolved_journal_path(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from trader import mmr_cli
    journal = str(tmp_path / "data" / "mmr.duckdb.journal")
    monkeypatch.delenv("MMR_JOURNAL_DUCKDB", raising=False)
    monkeypatch.setattr("trader.container.Container.instance",
                        classmethod(lambda cls: SimpleNamespace(config=lambda: {"journal_duckdb_path": journal})))
    assert mmr_cli._journal_duckdb_path() == journal


def test_the_container_hands_the_resolved_path_to_every_constructor():
    text = (ROOT / "trader" / "container.py").read_text()
    assert "self.configuration['journal_duckdb_path'] = self.mmr_config.storage.journal_duckdb_path" in text


def test_the_in_db_backup_names_the_journal_it_cannot_copy(tmp_path):
    from trader import mmr_cli
    data, backups = tmp_path / "data", tmp_path / "backups"
    data.mkdir()
    for name in ("mmr.duckdb", "mmr.duckdb.journal"):
        with duckdb.connect(str(data / name)) as conn:
            conn.execute("CREATE TABLE t (x INTEGER)")
    summary = run_backup(data, backups, name="b1")
    note = mmr_cli._journal_backup_note(str(data / "mmr.duckdb.journal"), summary)
    assert (note["db"], note["included_as"]) == ("mmr.duckdb.journal", None)     # no trader snapshot yet (#189)
    assert "./docker.sh -B" in note["warning"]


def test_the_plain_copy_backup_takes_the_journal_and_wal_files():
    text = (ROOT / "docker.sh").read_text()
    assert "/src/*.journal" in text and "/src/*.wal" in text
