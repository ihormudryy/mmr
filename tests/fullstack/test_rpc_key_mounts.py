"""Optional fullstack check: the tmpfs + per-file key binds on a running stack.

Runs only inside the `fullstack-tests` container, like
test_process_supervision.py (which stops the dashboard and signals trader
PID 1). It is NOT the cutover gate: the owner runs `./docker.sh -K`, an
isolated, mount-only check (tests/test_docker_helper.py, keys_cli check-mount).
"""
import os
import socket
from pathlib import Path

import pytest

from trader.messaging.principals import rpc_files_for

docker = pytest.importorskip('docker', reason='docker (docker-py) not installed in this environment')


def _skip_reason() -> str | None:
    if os.environ.get('MMR_FAKE_BROKER') != '1':
        return 'requires MMR_FAKE_BROKER=1 -- only set by the `fullstack-tests` runner'
    if not Path('/var/run/docker.sock').exists():
        return 'requires the host Docker socket bind-mounted at /var/run/docker.sock'
    return None


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or '')


@pytest.fixture(scope='module')
def docker_client():
    client = docker.from_env()
    try:
        yield client
    finally:
        client.close()


def _exec(client, service: str, command: list[str]) -> str:
    project = client.containers.get(socket.gethostname()).labels['com.docker.compose.project']
    matches = client.containers.list(filters={'label': [
        f'com.docker.compose.project={project}', f'com.docker.compose.service={service}']})
    assert len(matches) == 1, f'expected one running {service} container'
    result = matches[0].exec_run(command, user='trader')
    assert result.exit_code == 0, result.output
    return result.output.decode()


@pytest.mark.parametrize('service,principal', [('trader', 'trader'), ('strategy', 'strategy'),
                                               ('dashboard', 'dashboard'), ('scheduler', None),
                                               ('data', None)])
def test_container_lists_only_its_keys(docker_client, service, principal):
    out = _exec(docker_client, service, ['ls', '/home/trader/.config/mmr/keys/rpc'])
    assert set(out.split()) == rpc_files_for(principal)


@pytest.mark.parametrize('service', ['trader', 'strategy', 'dashboard', 'scheduler', 'data'])
def test_retired_hmac_key_reads_empty_in_the_container(docker_client, service):
    out = _exec(docker_client, service, ['wc', '-c', '/home/trader/.config/mmr/service_hmac.key'])
    assert out.split()[0] == '0'
