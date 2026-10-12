"""Shared helpers for SP3 Plan 3 tests: an authored file, its tunables and journals with the tables."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from tests.automation.backtest_judge_fixtures import NOW, Clock
from trader.automation.authored_config import AuthoredStrategiesConfig
from trader.automation.authored_files import AuthoredFileStore
from trader.automation.authored_schema import apply_authored_migrations
from trader.automation.authored_strategies import (
    DEPLOYED, EVALUATING, SHADOW, AuthoredRecord, AuthoredState, AuthoredStrategyStore, move_in_tx, record_in_tx,
)
from trader.automation.authored_wire import (
    RecordIsolationProofRequest, ReportAuthoredEvaluationRequest, SubmitAuthoredStrategyRequest,
)
from trader.automation.calendar_policy import XNYSCalendarPolicy
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


def submit_request(text: Optional[str] = None, **changes: Any) -> SubmitAuthoredStrategyRequest:
    text = text or VALID_SOURCE
    found = re.search(r"RANGE_BARS = (\d+)", text)
    raw = {"source": text, "tunables": tunables_for(int(found.group(1)) if found else 15), "bar_size": "5 mins",
           "note": "breaks above the range of the last bars", "author_model": "openrouter/author-1"}
    raw.update(changes)
    return SubmitAuthoredStrategyRequest.model_validate(raw)


@dataclass
class AuthoredWorld:
    db: DuckDBConnection
    clock: Clock
    store: AuthoredStrategyStore
    tmp_path: Path


def _store(db, clock, tmp_path, *, lines=None, calendar=None, proofs=None, withdraw=None,
           **config) -> AuthoredStrategyStore:
    return AuthoredStrategyStore(db, config=replace(AuthoredStrategiesConfig(enabled=True), **config),
                                 files=AuthoredFileStore(tmp_path / "store"), now=clock, proofs=proofs,
                                 calendar=calendar or XNYSCalendarPolicy(), shadow_sessions=20, lines=lines,
                                 withdraw=withdraw)


def authored_world(tmp_path: Path, *, lines=None, calendar=None, proofs=None, withdraw=None,
                   **config) -> AuthoredWorld:
    db, clock = authored_journal(tmp_path), Clock(NOW)
    return AuthoredWorld(db, clock, _store(db, clock, tmp_path, lines=lines, calendar=calendar, proofs=proofs,
                                           withdraw=withdraw, **config), tmp_path)


def with_config(world: AuthoredWorld, **config) -> AuthoredStrategyStore:
    """Another store on the same journal and files, as after a trader restart with an edited trader.yaml."""
    return _store(world.db, world.clock, world.tmp_path, **config)


def stored(world: AuthoredWorld, range_bars: int = 15) -> str:
    reply = world.store.submit(submit_request(source(range_bars)))
    assert reply["status"] == "STORED", reply
    return reply["record"]["digest"]


def stored_files(world: AuthoredWorld) -> list[Path]:
    root = world.tmp_path / "store"
    return sorted(root.glob("*.py")) if root.exists() else []


VERSION = "sha256:" + "7" * 64


def evaluating(world: AuthoredWorld, digest: str, request_id_: str) -> AuthoredRecord:
    """What an accepted SP2c claim does (Task 7): STORED -> EVALUATING on ``request_id_``."""
    return world.db.transaction(lambda conn: move_in_tx(
        conn, record_in_tx(conn, digest), EVALUATING, event="CLAIMED", reason=None, now=world.clock(),
        request_id=request_id_, evaluation_request_id=request_id_))


def moved(world: AuthoredWorld, digest: str, target: AuthoredState, *, reason: Optional[str] = None,
          **changes: Any) -> AuthoredRecord:
    return world.db.transaction(lambda conn: move_in_tx(
        conn, record_in_tx(conn, digest), target, event="TEST", reason=reason, now=world.clock(), **changes))


def report(world: AuthoredWorld, digest: str, request_id_: str, event: str, image: Optional[str] = None) -> dict:
    return world.store.report(ReportAuthoredEvaluationRequest.model_validate({
        "digest": digest, "request_id": request_id_, "event": event, "sandbox_image_digest": image,
        "detail": ""}))


def deployed(world: AuthoredWorld, range_bars: int = 15, version: str = VERSION) -> str:
    """A record judged DEPLOY and registered as ``version`` (what Tasks 7 and 8 do through SP2c)."""
    digest = stored(world, range_bars)
    evaluating(world, digest, request_id(range_bars))
    moved(world, digest, SHADOW, reason="AWAITING_REGISTRATION", verdict="DEPLOY", judgment_id="jdg-00000001",
          evaluation_request_id=None)
    moved(world, digest, DEPLOYED, deployment_version=version)
    return digest


def proof_request(image: str = IMAGE, *, passed: bool = True, ran_at=NOW) -> RecordIsolationProofRequest:
    return RecordIsolationProofRequest.model_validate({
        "image_digest": image, "passed": passed, "ran_at": ran_at.isoformat(),
        "checks": [{"name": "hostile.os_kill_parent", "passed": passed, "detail": "EPERM"},
                   {"name": "host.network_none", "passed": True, "detail": "NetworkMode=none"}]})
