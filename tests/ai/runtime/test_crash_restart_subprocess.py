"""SP2 spec 12, crash test 2: the trader accepted a decision, the ai process died before saving the receipt.
This test terminates and restarts the ai process as a real OS process (SIGKILL, then a fresh process)."""
import dataclasses
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.ai.runtime.trader_world import TraderWorld, write_service_config
from tests.rpc_identity_fixtures import make_identities, write_keyset
from tests.sp1_fixtures import LoopThread
from trader.ai.ids import derive_decision_id
from trader.data.duckdb_store import DuckDBConnection

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.timeout(240)


@pytest.fixture
def loop_thread():
    thread = LoopThread()
    yield thread
    thread.stop()


def spawn(args, log: Path) -> subprocess.Popen:
    env = {**os.environ, "OPENROUTER_API_KEY": "test-only-not-a-key", "PYTHONPATH": str(ROOT)}
    return subprocess.Popen([sys.executable, "-m", "tests.ai.runtime.child_service", *args], cwd=ROOT, env=env,
                            stdout=log.open("w"), stderr=subprocess.STDOUT)


def wait_until(check, process: subprocess.Popen, log: Path, timeout: float = 90.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        if process.poll() is not None:
            raise AssertionError(f"the ai process exited with {process.returncode}:\n{log.read_text()[-4000:]}")
        time.sleep(0.1)
    raise AssertionError(f"timed out:\n{log.read_text()[-4000:]}")


def heartbeat(path: Path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def submission_row(db: Path, decision_id: str) -> dict:
    columns = ("state", "receipt_state", "created_epoch", "last_epoch", "attempts", "body_sha256")
    row = DuckDBConnection.get_instance(str(db)).execute(
        f"SELECT {', '.join(columns)} FROM ai_submissions WHERE decision_id = ?", [decision_id], fetch="one")
    return dict(zip(columns, row))


def test_trader_accepted_receipt_not_saved_survives_a_real_process_restart(tmp_path, loop_thread, monkeypatch):
    keys = tmp_path / "keys"
    world = TraderWorld(tmp_path, loop_thread, monkeypatch, identities=make_identities(keys=write_keyset(keys)))
    processes = []
    try:
        beat, marker = tmp_path / "hb.json", tmp_path / "sent.json"
        config = write_service_config(tmp_path, world, beat)
        decision = world.enter()
        script = tmp_path / "script.json"
        script.write_text(json.dumps({"entry_signal": [dataclasses.asdict(decision)]}))
        source_event_id = world.strategy_signal()
        decision_id = derive_decision_id(source_event_id, decision.action_key)
        args = ["--config", str(config), "--keys-dir", str(keys), "--clock-start", world.served.now().isoformat(),
                "--script", str(script)]

        first = spawn([*args, "--block-after-submit", str(marker)], tmp_path / "first.log")
        processes.append(first)
        wait_until(marker.exists, first, tmp_path / "first.log")
        accepted = json.loads(marker.read_text())
        assert accepted["command_id"] == f"aip-{decision_id}" and world.receipt(decision_id) is not None
        first.send_signal(signal.SIGKILL)                          # terminated: no cleanup, no receipt write
        assert first.wait(timeout=10) == -signal.SIGKILL

        world.advance(61)                                          # the dead process's lease ends on the trader
        log = tmp_path / "second.log"
        second = spawn(args, log)
        processes.append(second)
        wait_until(lambda: (status := heartbeat(beat)) and status["epoch"] == 2
                   and status["unsettled_submissions"] == 0, second, log)
        second.terminate()
        assert second.wait(timeout=30) == 0

        row = submission_row(world.ai_db, decision_id)
        assert row["state"] in ("ACCEPTED", "FINAL") and row["receipt_state"] is not None
        assert (row["created_epoch"], row["last_epoch"], row["attempts"]) == (1, 1, 1)   # reconciled, never resent
        assert world.decision_row(decision_id).controller_epoch == 1                   # the original admission
        opportunities = DuckDBConnection.get_instance(str(world.ai_db)).execute(
            "SELECT state FROM ai_opportunities", fetch="all")
        assert opportunities == [("DECIDED",)]                    # the signal was not judged a second time
        world.settle()
        assert len(world.entries()) == 1 and world.protected()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        world.close()
