"""Shared helpers for SP3 Plan 3 tests: an authored file, its tunables and journals with the tables."""
from __future__ import annotations

import hashlib
from pathlib import Path

from trader.automation.authored_schema import apply_authored_migrations
from trader.automation.backtest_judge_schema import apply_backtest_judge_migrations
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

# Written against spec 7.2: allowed imports only, no name or attribute starting with "_", exactly one class.
VALID_SOURCE = '''\
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class RangeBreak(AuthoredStrategy):
    LOOKBACK_BARS = 40
    RANGE_BARS = 15
    MIN_PROBABILITY = 0.6

    def on_bar(self, history, params):
        bars = int(params["RANGE_BARS"])
        if len(history) <= bars:
            return None
        ceiling = history["high"].iloc[-bars - 1:-1].max()
        if history["close"].iloc[-1] > ceiling:
            return Decision(action="BUY", probability=float(params["MIN_PROBABILITY"]))
        return None
'''


def tunables_for(range_bars: int = 15) -> dict:
    """The submit tunables of ``source(range_bars)``: each default is the class value (ruling 5)."""
    return {"RANGE_BARS": {"low": 5, "high": 30, "default": range_bars},
            "MIN_PROBABILITY": {"low": 0.5, "high": 0.9, "default": 0.6}}


TUNABLES = tunables_for()
IMAGE = "a1b2c3d-20261011T220000Z"             # Plan 2 build ids (<git sha>-<UTC time>)
OTHER_IMAGE = "e4f5a6b-20261012T090000Z"


def source(range_bars: int = 15) -> str:
    """A distinct authored file per value; 5-30 stays inside TUNABLES."""
    return VALID_SOURCE.replace("RANGE_BARS = 15", f"RANGE_BARS = {range_bars}")


def digest_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_id(n: int) -> str:
    return "sha256:" + f"{n:064x}"


def authored_journal(tmp_path: Path) -> DuckDBConnection:
    """A journal with the SP2c claim and judgment tables and this plan's tables 130-131."""
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    apply_backtest_judge_migrations(migrator)
    apply_authored_migrations(migrator)
    return db
