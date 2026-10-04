"""Recovery for DuckDB unreplayable-WAL INTERNAL Error on open."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from trader.data.duckdb_store import (
    connect_duckdb,
    is_unreplayable_wal_error,
    quarantine_duckdb_wal,
)


class _FakeInternal(Exception):
    pass


def test_is_unreplayable_wal_error_matches_known_message():
    exc = _FakeInternal(
        'INTERNAL Error: Failure while replaying WAL file "/x.wal": '
        'Calling DatabaseManager::GetDefaultDatabase with no default database set'
    )
    assert is_unreplayable_wal_error(exc)
    assert not is_unreplayable_wal_error(RuntimeError('lock conflict'))


def test_quarantine_duckdb_wal_moves_sidecar(tmp_path: Path):
    db = tmp_path / 'mmr.duckdb.journal'
    db.write_bytes(b'x')
    wal = Path(str(db) + '.wal')
    wal.write_bytes(b'corrupt')
    dest = quarantine_duckdb_wal(str(db))
    assert not wal.exists()
    assert dest.exists()
    assert dest.read_bytes() == b'corrupt'
    assert 'corrupt_wal_quarantine_' in dest.parent.name


def test_connect_duckdb_quarantines_wal_and_retries(tmp_path: Path):
    db = tmp_path / 'journal.duckdb'
    db.write_bytes(b'')
    wal = Path(str(db) + '.wal')
    wal.write_bytes(b'bad')

    boom = _FakeInternal(
        'Failure while replaying WAL file "/tmp/journal.duckdb.wal": '
        'Calling DatabaseManager::GetDefaultDatabase with no default database set'
    )
    good = object()
    calls = {'n': 0}

    def _connect(path, read_only=False):
        calls['n'] += 1
        if calls['n'] == 1:
            raise boom
        return good

    with patch('trader.data.duckdb_store.duckdb.connect', side_effect=_connect):
        conn = connect_duckdb(str(db))
    assert conn is good
    assert calls['n'] == 2
    assert not wal.exists()
    quarantines = list(tmp_path.glob('corrupt_wal_quarantine_*/*.wal'))
    assert len(quarantines) == 1


def test_connect_duckdb_does_not_swallow_unrelated_errors(tmp_path: Path):
    db = tmp_path / 'x.duckdb'
    db.write_bytes(b'')
    with patch(
        'trader.data.duckdb_store.duckdb.connect',
        side_effect=RuntimeError('permission denied'),
    ):
        with pytest.raises(RuntimeError, match='permission denied'):
            connect_duckdb(str(db))
