"""G0 Task 6: full-stack process-supervision gate.

Runs INSIDE the `docker-compose.yml` `fullstack-tests` service (the `test`
Compose profile only), on the same `mmr-internal` network as the five real
long-lived services, with a read-only bind mount of the host's Docker
socket. It proves G0 Task 5's "one supervisor per process" claim
empirically against REAL containers, not just by reading YAML:

1. Stopping `dashboard` must not disturb `trader`/`strategy` at all --
   same container IDs, same restart counts.
2. Killing `trader`'s PROCESS must cause Compose's `restart: unless-stopped`
   policy to bring `trader` back on its own, with `trader`'s OWN
   healthcheck genuinely degrading (not just "eventually healthy") and
   then recovering -- while `data`/`strategy`/`scheduler` are completely
   unaffected (same container IDs, same restart counts).

Requires `MMR_FAKE_BROKER=1` (only ever set by `docker-compose.yml`'s
`fullstack-tests` service) so `trader_service` can boot and stay up
without a real IB Gateway login -- see
`trader/listeners/ibreactive.py::IBAIORx.connect_fake` and
`trader/trading/trading_runtime.py::Trader.connect()`. Skips cleanly
(not an error) when collected outside that environment -- in particular,
the main CI `test` job's `pytest tests/ ...` run and any local `pytest
tests/` run must never even attempt this file for real.

## Why "kill the process", not "stop the container"

Verified empirically against this project's actual Docker/Compose version
(recorded in the G0 Task 6 report): `container.stop()` and `container.kill()`
(both Engine-API operations, i.e. what a `docker stop`/`docker kill` from
the host does) mark a container as *manually* stopped, and Docker's
`restart: unless-stopped` policy deliberately does NOT undo a manual stop
-- the container just stays exited. That's the right tool for the
dashboard scenario above (we WANT it to stay down while we check the
siblings, then bring it back ourselves), but it is the WRONG tool for
proving the restart *policy* -- there is nothing to observe if the
container never comes back on its own.

A real crash does not go through that Engine-API "manual stop" path. We
reproduce one by delivering `SIGTERM` to PID 1 *from inside* the
container's own PID namespace via `exec` (`kill -TERM 1`) -- MMR's
services (`trader_service.py`, `strategy_service.py`, `web.app`, `pycron`)
all install real `SIGTERM` handlers and exit cleanly on receipt, so this
signal IS delivered (Linux only immunizes a PID-namespace init process
against SIGNALS FOR WHICH IT HAS NO HANDLER when sent from within its own
namespace -- SIGTERM here has a handler, so delivery is normal). The
resulting process exit is NOT a "manual stop" from the daemon's point of
view, so `restart: unless-stopped` engages exactly as it would for a real
crash.
"""
from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from typing import Callable

import pytest

docker = pytest.importorskip('docker', reason='docker (docker-py) not installed in this environment')


def _skip_reason() -> str | None:
    if os.environ.get('MMR_FAKE_BROKER') != '1':
        return (
            'requires MMR_FAKE_BROKER=1 -- only set by docker-compose.yml\'s '
            '`fullstack-tests` test-profile runner'
        )
    if not Path('/var/run/docker.sock').exists():
        return 'requires the host Docker socket bind-mounted at /var/run/docker.sock'
    return None


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or '')


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def client():
    c = docker.from_env()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(scope='module')
def self_container(client):
    """The `fullstack-tests` container this test is itself running inside --
    used to auto-detect the Compose project name rather than hardcoding
    ``mmr`` (docker-compose.yml's `name:`), so this file stays correct even
    if the project is ever run under a different project name."""
    return client.containers.get(socket.gethostname())


@pytest.fixture(scope='module')
def project(self_container) -> str:
    return self_container.labels['com.docker.compose.project']


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _container(client, project: str, service: str):
    matches = client.containers.list(all=True, filters={
        'label': [
            f'com.docker.compose.project={project}',
            f'com.docker.compose.service={service}',
        ],
    })
    assert matches, f'no container found for compose service {service!r} in project {project!r}'
    assert len(matches) == 1, f'expected exactly one container for service {service!r}, found {len(matches)}'
    return matches[0]


def _restart_count(container) -> int:
    container.reload()
    return container.attrs['RestartCount']


def _health_status(container) -> str:
    container.reload()
    health = container.attrs.get('State', {}).get('Health')
    return health['Status'] if health else 'none'


def _status(container) -> str:
    container.reload()
    return container.status


def _wait_until(predicate: Callable[[], bool], *, timeout: float, desc: str, soft: bool = False,
                 interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    if soft:
        return False
    pytest.fail(f'timed out after {timeout}s waiting for: {desc}')


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_stopping_dashboard_does_not_disturb_trader_or_strategy(client, project):
    dashboard = _container(client, project, 'dashboard')
    trader = _container(client, project, 'trader')
    strategy = _container(client, project, 'strategy')

    trader_id_before, strategy_id_before = trader.id, strategy.id
    trader_restarts_before = _restart_count(trader)
    strategy_restarts_before = _restart_count(strategy)

    dashboard.stop(timeout=10)
    try:
        _wait_until(lambda: _status(dashboard) == 'exited',
                    timeout=20, desc='dashboard to actually stop')

        # Give a moment for anything that WOULD perturb the siblings a
        # chance to happen before asserting isolation.
        time.sleep(5)

        trader.reload()
        strategy.reload()
        assert trader.id == trader_id_before
        assert strategy.id == strategy_id_before
        assert _restart_count(trader) == trader_restarts_before
        assert _restart_count(strategy) == strategy_restarts_before
    finally:
        dashboard.start()
        _wait_until(lambda: _status(dashboard) == 'running',
                    timeout=30, desc='dashboard to come back up for cleanup')


def test_killing_trader_process_triggers_isolated_restart_and_health_recovers(client, project):
    trader = _container(client, project, 'trader')
    siblings = {
        name: _container(client, project, name)
        for name in ('data', 'strategy', 'scheduler')
    }

    sibling_ids_before = {name: c.id for name, c in siblings.items()}
    sibling_restarts_before = {name: _restart_count(c) for name, c in siblings.items()}
    trader_restarts_before = _restart_count(trader)

    # See the module docstring: this is deliberately NOT trader.kill()/stop().
    # `kill` is a shell builtin here (the slim Python base image ships no
    # standalone `/bin/kill` executable from `procps`) -- exec_run() runs a
    # program directly, not through a shell, so it must be invoked via `sh -c`.
    exit_code, output = trader.exec_run(['sh', '-c', 'kill -TERM 1'])
    assert exit_code == 0, f'sending SIGTERM to trader PID 1 failed: {output!r}'

    _wait_until(
        lambda: _restart_count(_container(client, project, 'trader')) > trader_restarts_before,
        timeout=60, desc='Compose to restart trader (RestartCount increment)',
    )

    # "Degraded until readiness returns" -- must OBSERVE a non-"healthy"
    # status while the fresh process warms back up, not just eventually
    # see "healthy" (which would also be true of a healthcheck that never
    # degraded at all).
    observed_degraded = _wait_until(
        lambda: _health_status(_container(client, project, 'trader')) != 'healthy',
        timeout=25, desc='trader health to report non-healthy after the restart', soft=True,
    )
    assert observed_degraded, (
        'expected trader\'s healthcheck to report something other than '
        '"healthy" while the restarted process was warming back up; never '
        'observed it -- either the restart was missed entirely or the '
        'healthcheck is not actually exercising the right endpoint'
    )

    _wait_until(
        lambda: _health_status(_container(client, project, 'trader')) == 'healthy',
        timeout=120, desc='trader health to recover to "healthy"',
    )

    for name, c in siblings.items():
        c.reload()
        assert c.id == sibling_ids_before[name], f'{name} container identity changed -- it was disturbed'
        assert _restart_count(c) == sibling_restarts_before[name], f'{name} was restarted -- isolation violated'
