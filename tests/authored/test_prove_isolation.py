"""SP3 Plan 3 Task 10: the isolation run inside research and the host record step (spec 7.9; D2, D3; rulings 15, 22)."""
from __future__ import annotations

import datetime as dt
import io
import json
from types import SimpleNamespace

import pytest

import trader.authored.prove_isolation as runner
from trader.authored.isolation_host import (
    ProveIsolationError, compose_command, in_container_command, parse_proof, prove_isolation,
)
from trader.authored.sandbox_client import SandboxUnavailable
from trader.automation.authored_wire import IMAGE_ID, RecordIsolationProofRequest
from trader.sandbox.protocol import BUILD_ID_RE

IMAGE = "a1b2c3d-20261011T220000Z"                      # a Plan 2 build id
RAN_AT = dt.datetime(2026, 10, 11, 22, 0, tzinfo=dt.timezone.utc)
PROOF = {"image_digest": IMAGE, "passed": True, "ran_at": RAN_AT.isoformat(),
         "checks": [{"name": "escape_pd_io_common_oskill", "passed": True, "detail": "contained: SANDBOX_ERROR"},
                    {"name": "read_only_root", "passed": True, "detail": "root_read_only=True"}]}


def test_the_trader_and_the_sandbox_spell_a_build_id_the_same_way():
    assert IMAGE_ID.pattern == BUILD_ID_RE.pattern                  # one spelling (D2)


def test_the_authored_cap_never_exceeds_the_live_sandbox_pool():
    from trader.automation.authored_config import MAX_ACTIVE_RANGE
    from trader.sandbox.supervisor import LIVE_POOL_SIZE

    assert MAX_ACTIVE_RANGE[1] == LIVE_POOL_SIZE                    # controller decision C2


class FakeSandbox:
    """PING answers with ``live_open`` from the list, one value per call (the last repeats)."""

    def __init__(self, *live_open):
        self.live_open = list(live_open or (0,))

    def ping(self):
        value = self.live_open.pop(0) if len(self.live_open) > 1 else self.live_open[0]
        return SimpleNamespace(image_digest=IMAGE, live_open=value)


def test_the_runner_prints_one_proof_body(monkeypatch):
    result = SimpleNamespace(passed=True, image_digest=IMAGE, checks=(
        SimpleNamespace(name="escape_fork_bomb", passed=True, detail="contained: SANDBOX_ERROR"),))
    seen = []
    monkeypatch.setattr(runner, "SandboxClient", lambda path: seen.append(path) or FakeSandbox(0))
    monkeypatch.setattr(runner, "run_isolation_checks", lambda client: result)
    out = io.StringIO()
    assert runner.run_in_container(out=out, now=lambda: RAN_AT) == 0
    assert seen == [runner.RESEARCH_SOCKET]                         # the research pool, never a live slot
    assert json.loads(out.getvalue()) == {
        "image_digest": IMAGE, "passed": True, "ran_at": RAN_AT.isoformat(),
        "checks": [{"name": "escape_fork_bomb", "passed": True, "detail": "contained: SANDBOX_ERROR"}]}


@pytest.mark.parametrize("live_open", [(1,), (None,), (0, 2)])          # before, unknown, opened during the run
def test_the_run_refuses_while_a_live_session_is_open(monkeypatch, capsys, live_open):   # owner ruling 2026-10-11
    ran = []
    monkeypatch.setattr(runner, "SandboxClient", lambda path: FakeSandbox(*live_open))
    monkeypatch.setattr(runner, "run_isolation_checks", lambda client: ran.append(1) or SimpleNamespace(
        passed=True, image_digest=IMAGE, checks=()))
    out = io.StringIO()
    assert runner.run_in_container(out=out) == runner.EXIT_LIVE_SESSIONS_OPEN
    assert out.getvalue() == "" and "LIVE_SESSIONS_OPEN" in capsys.readouterr().err
    assert ran == ([] if live_open[0] != 0 else [1])


def test_an_unreachable_sandbox_exits_2_and_prints_nothing(monkeypatch):
    def down(client):
        raise SandboxUnavailable("no socket")
    monkeypatch.setattr(runner, "SandboxClient", lambda path: FakeSandbox(0))
    monkeypatch.setattr(runner, "run_isolation_checks", down)
    out = io.StringIO()
    assert runner.run_in_container(out=out) == 2
    assert out.getvalue() == ""


def test_the_host_runs_the_in_container_step_once_and_returns_the_checked_proof():
    calls = []

    def run(argv):
        calls.append(argv)
        return "a warning line\n" + json.dumps(PROOF) + "\n"
    compose = ["docker", "compose", "-f", "/repo/docker-compose.yml"]
    proof = prove_isolation(compose, run=run)
    assert calls == [compose + ["exec", "-T", "research", "mmr", "ai-author", "prove-isolation", "--in-container"]]
    assert calls[0] == in_container_command(compose)
    assert proof == PROOF


def test_a_failing_check_gives_a_failing_proof_that_the_trader_accepts():
    failing = {**PROOF, "passed": False,
               "checks": [{**PROOF["checks"][0], "passed": False, "detail": "ran to completion: NOT contained"}]}
    assert parse_proof(json.dumps(failing))["passed"] is False
    RecordIsolationProofRequest.model_validate(failing)


@pytest.mark.parametrize("stdout, hint", [
    ("", "printed nothing"),
    ("not json\n", "did not print JSON"),
    (json.dumps({**PROOF, "passed": True,
                 "checks": [{**PROOF["checks"][0], "passed": False}]}), "not a valid proof"),
    (json.dumps({**PROOF, "image_digest": "unset"}), "not a valid proof"),
    (json.dumps({**PROOF, "docker_inspect": {}}), "not a valid proof"),
])
def test_output_that_is_not_a_valid_proof_is_an_error(stdout, hint):
    with pytest.raises(ProveIsolationError, match=hint):
        parse_proof(stdout)


def test_compose_command_names_the_repo_compose_file_and_its_runtime():
    command = compose_command("docker compose")
    assert command[:3] == ["docker", "compose", "-f"] and command[3].endswith("docker-compose.yml")
    assert compose_command("podman-compose")[0] == "podman-compose"
