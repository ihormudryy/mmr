"""SP3 Plan 3 Task 10 (D3): the in-container step runs in the real research container against the real sandbox."""
import json
import os
import socket as _socket
from pathlib import Path

import pytest

docker = pytest.importorskip("docker", reason="docker-py not installed")

from trader.automation.authored_wire import RecordIsolationProofRequest  # noqa: E402
from trader.sandbox import protocol as proto  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("MMR_FAKE_BROKER") != "1" or not Path(proto.RESEARCH_SOCKET).exists(),
    reason="only the sandbox fullstack runner mounts the sandbox sockets")


def _research_container(client):
    me = client.containers.get(_socket.gethostname())
    project = me.labels["com.docker.compose.project"]
    found = client.containers.list(filters={"label": [
        f"com.docker.compose.project={project}", "com.docker.compose.service=research"]})
    assert len(found) == 1, "expected one research container"
    return found[0]


def test_the_in_container_step_prints_a_passing_proof_the_trader_accepts():
    client = docker.from_env()
    try:
        result = _research_container(client).exec_run(["mmr", "ai-author", "prove-isolation", "--in-container"])
    finally:
        client.close()
    assert result.exit_code == 0, result.output.decode()[-2000:]
    proof = json.loads(result.output.decode().strip().splitlines()[-1])
    RecordIsolationProofRequest.model_validate(proof)
    failed = [check for check in proof["checks"] if not check["passed"]]
    assert proof["passed"] is True, failed
