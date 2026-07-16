"""Tests for SchemaMigrator ([M1-F1] Task 3).

FROZEN CONTRACT under test: `SchemaMigrator.apply(version: int, name: str,
statements: Sequence[str])` — [M1-F2] (version 10) and [M1-F3] (version 20)
call this exact 3-positional-argument signature; drift here breaks both.
"""
import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator


@pytest.fixture
def migrator(tmp_duckdb_path):
    db = DuckDBConnection.get_instance(tmp_duckdb_path)
    return SchemaMigrator(db)


def _tables(db):
    rows = db.execute("SELECT table_name FROM information_schema.tables", fetch="all")
    return {row[0] for row in rows}


def test_apply_runs_statements_and_records_version(migrator):
    applied = migrator.apply(1, "create_foo", ["CREATE TABLE foo (x INTEGER)"])

    assert applied is True
    assert "foo" in _tables(migrator.db)
    rows = migrator.db.execute(
        "SELECT version, name FROM schema_migrations", fetch="all"
    )
    assert rows == [(1, "create_foo")]


def test_apply_is_idempotent_and_does_not_rerun_statements(migrator):
    # No "IF NOT EXISTS" on purpose: if apply() re-ran the statement on the
    # second call, this would raise CatalogException (table already exists).
    first = migrator.apply(1, "create_foo", ["CREATE TABLE foo (x INTEGER)"])
    second = migrator.apply(1, "create_foo", ["CREATE TABLE foo (x INTEGER)"])

    assert first is True
    assert second is False
    rows = migrator.db.execute("SELECT version FROM schema_migrations", fetch="all")
    assert rows == [(1,)]


def test_apply_records_multiple_independent_versions(migrator):
    migrator.apply(1, "create_foo", ["CREATE TABLE foo (x INTEGER)"])
    migrator.apply(2, "create_bar", ["CREATE TABLE bar (y INTEGER)"])

    tables = _tables(migrator.db)
    assert {"foo", "bar"} <= tables
    versions = migrator.applied_versions()
    assert versions == {1, 2}


def test_apply_runs_multiple_statements_in_one_version(migrator):
    migrator.apply(
        1,
        "create_foo_and_index",
        [
            "CREATE TABLE foo (x INTEGER)",
            "CREATE INDEX idx_foo_x ON foo (x)",
        ],
    )

    assert "foo" in _tables(migrator.db)


def test_apply_rolls_back_partial_statements_on_failure(migrator):
    # The second statement is invalid SQL; the whole version's statements
    # run in one transaction, so the (valid) first statement's CREATE TABLE
    # must not survive either -- and the version must not be recorded as
    # applied (so a later, corrected retry can still apply cleanly).
    with pytest.raises(Exception):
        migrator.apply(
            1,
            "broken_migration",
            [
                "CREATE TABLE foo (x INTEGER)",
                "THIS IS NOT VALID SQL",
            ],
        )

    assert "foo" not in _tables(migrator.db)
    assert migrator.applied_versions() == set()


def test_applied_versions_empty_on_fresh_file(migrator):
    assert migrator.applied_versions() == set()


def test_apply_returns_bool_new_vs_skipped(migrator):
    assert migrator.apply(5, "v5", ["CREATE TABLE v5_table (a INTEGER)"]) is True
    assert migrator.apply(5, "v5", ["CREATE TABLE v5_table (a INTEGER)"]) is False
