#!/usr/bin/env python3
"""[COMPAT] Eight-hour representative paper soak with failure injection.

Drives two independent signals over the soak window (spec §13.3):

1. **Container health** -- samples the real `dashboard` Compose service's
   RSS/CPU once a minute via `docker stats` (`Sample`), used for the RSS
   growth (<=20% after the first warm-up hour) and average-CPU (<1 core)
   thresholds.
2. **Harness metrics** -- spawns `scripts/soak_harness.py` ([M1-R]) as a
   subprocess for the same duration. That harness drives its own
   self-contained fake feed/quote-plane/uvicorn instance (it does not talk
   to the real paper stack) purely to measure producer-commit ->
   client-reducer p95 latency without depending on live market data.

Additionally injects dependency failures against the real Compose stack
(`trader`, `strategy`, `ib-gateway`, `dashboard`) at fixed offsets and
checks `GET /api/health` (session-cookie authenticated via
`scripts/parity_compare.login`) to confirm every outage is either
recovered coherently or left in an explicitly-degraded (never silently
inconsistent) state.

Evaluation (`evaluate_soak`) is a pure, deterministic, fail-closed
function: exit 0 only when every threshold AND every scenario passes.
The 8-hour execution itself, and real Compose failure injection, are
operational concerns -- this module provides the scaffold plus a short
rehearsal path (`--hours 0.1 --skip-scenarios`); it does not attempt an
actual 8-hour run.

NOTE -- source-vs-brief drift (see the report accompanying this task):
`scripts/soak_harness.py`'s real CLI is `--minutes/--instruments/
--quote-hz/--domain-eps/--tabs/--report` (there is no `--duration-minutes`,
`--commands-per-hour`, or `--metrics-out`), and its `--report` JSON is a
nested `{config, domain_events_received, latency_ms, rss_bytes,
cpu_avg_cores, violations}` shape, not the flat spec-13.3 counter set. Only
`p95_critical_ms` (from `latency_ms.p95`) is derived from it here --
`unhandled_errors`, `unresolved_commands`, `max_replay_ring_events`,
`max_client_fifo_depth`, and `max_terminal_rows` have no live exporter
anywhere in this codebase revision (the ring/FIFO/terminal-retention
bookkeeping in `web/command_center/state.py` and `sse.py` is private
in-process state with no HTTP surface), so they are intentionally left
absent from the harness dict `evaluate_soak` sees. Its existing
fail-closed contract (`harness.get(name) is None` -> check fails) reports
that honestly as "not observed" rather than fabricating a passing zero.
Likewise, the real authenticated `GET /api/health` payload
(`web/app.py::api_health` / `trader/operations/health.py::
build_health_payload`) is `{process, dependencies: {trader: {reachable,
status}}, jobs}` -- a single hardcoded `trader` dependency, not a
per-service `{name: {"state": ...}}` map. There is currently no live signal
for `strategy` service reachability through this endpoint at all, so the
`strategy_outage` scenario cannot assert "outage was surfaced" the way the
other three can; it falls back to the post-recovery parity check alone
(see `run_scenario` / `Scenario.probe`).
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parity_compare import login  # noqa: E402


@dataclass(frozen=True)
class SoakThresholds:
    warmup_minutes: int = 60
    rss_growth_max: float = 0.20
    cpu_avg_cores_max: float = 1.0          # strictly below one core
    p95_critical_ms_max: float = 500.0
    max_unhandled_errors: int = 0
    max_unresolved_commands: int = 0
    replay_ring_max: int = 10_000
    client_fifo_max: int = 1_000
    terminal_rows_max: int = 500


@dataclass(frozen=True)
class Sample:
    minute: int
    rss_bytes: float
    cpu_cores: float


@dataclass(frozen=True)
class Check:
    name: str
    limit: float
    observed: float | None
    passed: bool


@dataclass
class SoakReport:
    started_at: str
    checks: list[Check]
    scenarios: dict[str, str]
    passed: bool

    def to_json(self) -> str:
        return json.dumps({
            'started_at': self.started_at,
            'passed': self.passed,
            'checks': [dataclasses.asdict(c) for c in self.checks],
            'scenarios': self.scenarios,
        }, indent=2)


def evaluate_soak(samples: list[Sample], harness: dict,
                  scenario_outcomes: dict[str, str],
                  t: SoakThresholds) -> SoakReport:
    """Pure, deterministic, fail-closed evaluation of one soak run.

    ``harness`` carries flat spec-13.3 metric keys (see module docstring
    for which of these the current `soak_harness.py` actually populates).
    A key absent from ``harness`` evaluates to a failed check -- missing
    data is never treated as "passing by default".
    """
    checks: list[Check] = []
    post = [s for s in samples if s.minute >= t.warmup_minutes]
    checks.append(Check('samples_present', 1, len(post), len(post) >= 2))
    if len(post) >= 2:
        baseline, final = post[0].rss_bytes, post[-1].rss_bytes
        growth = (final - baseline) / baseline if baseline > 0 else float('inf')
        checks.append(Check('rss_growth', t.rss_growth_max, round(growth, 4),
                            growth <= t.rss_growth_max))
        cpu = sum(s.cpu_cores for s in post) / len(post)
        checks.append(Check('cpu_avg_cores', t.cpu_avg_cores_max, round(cpu, 3),
                            cpu < t.cpu_avg_cores_max))
    else:
        checks.append(Check('rss_growth', t.rss_growth_max, None, False))
        checks.append(Check('cpu_avg_cores', t.cpu_avg_cores_max, None, False))

    def metric(name: str, limit: float) -> None:
        observed = harness.get(name)
        ok = observed is not None and float(observed) <= limit
        checks.append(Check(name.replace('max_', '') if name.startswith('max_')
                            else name, limit, observed, ok))

    metric('p95_critical_ms', t.p95_critical_ms_max)
    metric('unhandled_errors', t.max_unhandled_errors)
    metric('unresolved_commands', t.max_unresolved_commands)
    metric('max_replay_ring_events', t.replay_ring_max)
    metric('max_client_fifo_depth', t.client_fifo_max)
    metric('max_terminal_rows', t.terminal_rows_max)

    scenarios_ok = all(v in ('coherent', 'explicit-degraded')
                       for v in scenario_outcomes.values())
    checks.append(Check('scenarios_coherent', 1,
                        int(scenarios_ok), scenarios_ok))
    return SoakReport(
        started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        checks=checks, scenarios=scenario_outcomes,
        passed=all(c.passed for c in checks))


# --- sampling (real `dashboard` Compose container) --------------------------

_MEM_UNITS = {'B': 1, 'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3}


def parse_mem(mem_usage: str) -> float:
    """'512.3MiB / 24GiB' -> bytes of the first term."""
    m = re.match(r'([\d.]+)(B|KiB|MiB|GiB)', mem_usage.strip())
    if not m:
        raise ValueError(f'unparsable MemUsage: {mem_usage!r}')
    return float(m.group(1)) * _MEM_UNITS[m.group(2)]


def sample_container(service: str, minute: int) -> Sample:
    cid = subprocess.run(['docker', 'compose', 'ps', '-q', service],
                         capture_output=True, text=True, check=True).stdout.strip()
    out = subprocess.run(['docker', 'stats', '--no-stream', '--format',
                          '{{json .}}', cid],
                         capture_output=True, text=True, check=True)
    row = json.loads(out.stdout)
    return Sample(minute=minute, rss_bytes=parse_mem(row['MemUsage']),
                  cpu_cores=float(row['CPUPerc'].rstrip('%')) / 100.0)


# --- harness (M1-R scripts/soak_harness.py) subprocess -----------------------

def spawn_harness(minutes: float, report_path: Path) -> subprocess.Popen:
    """Launch the real `soak_harness.py` CLI (self-contained synthetic
    load; see module docstring for its actual flag names)."""
    return subprocess.Popen([
        sys.executable, str(Path(__file__).parent / 'soak_harness.py'),
        '--minutes', str(minutes), '--instruments', '100',
        '--quote-hz', '4', '--domain-eps', '20', '--tabs', '3',
        '--report', str(report_path)])


def harness_metrics(raw: dict) -> dict:
    """Translate `soak_harness.py`'s own `--report` JSON into the flat
    keys `evaluate_soak` understands. See the module docstring's
    source-vs-brief drift note: only p95 latency is currently derivable
    from the upstream harness -- the rest are intentionally left absent
    (not fabricated) until a live exporter exists for them.
    """
    latency = raw.get('latency_ms') or {}
    return {'p95_critical_ms': latency.get('p95')}


# --- failure injection -------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    name: str
    offset_minutes: int
    stop_cmd: tuple[str, ...]
    start_cmd: tuple[str, ...]
    outage_seconds: int
    # Which `/api/health` signal proves the outage was surfaced, given the
    # real payload only ever carries one dependency key, 'trader':
    #   'trader' -> dependencies.trader.reachable
    #   'ib'     -> dependencies.trader.status.ib_upstream_connected
    #   'dashboard' -> the dashboard process's own /readyz flag
    #   None     -> not observable via /api/health today (strategy_service
    #               has no live probe); coherence relies on parity_cmd only
    probe: str | None


SCENARIOS: tuple[Scenario, ...] = (
    Scenario('trader_outage', 120, ('docker', 'compose', 'stop', 'trader'),
             ('docker', 'compose', 'start', 'trader'), 180, 'trader'),
    Scenario('strategy_outage', 240, ('docker', 'compose', 'stop', 'strategy'),
             ('docker', 'compose', 'start', 'strategy'), 180, None),
    Scenario('broker_disconnect', 300, ('docker', 'compose', 'stop', 'ib-gateway'),
             ('docker', 'compose', 'start', 'ib-gateway'), 120, 'ib'),
    Scenario('dashboard_restart', 360,
             ('docker', 'compose', 'restart', 'dashboard'),
             ('true',), 0, 'dashboard'),
)


def _health(base_url: str, cookie: str) -> dict:
    req = urllib.request.Request(base_url + '/api/health',
                                 headers={'Cookie': cookie})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _ready(base_url: str) -> bool:
    req = urllib.request.Request(base_url + '/readyz')
    with urllib.request.urlopen(req, timeout=10) as resp:
        return bool(json.load(resp).get('ready'))


def _trader_reachable(health: dict) -> bool:
    return bool((health.get('dependencies') or {}).get('trader', {}).get('reachable'))


def _ib_upstream_connected(health: dict) -> bool | None:
    """None means the trader dependency didn't report status at all (e.g.
    trader_service itself unreachable) -- distinct from a definite False."""
    status = ((health.get('dependencies') or {}).get('trader', {}) or {}).get('status') or {}
    if 'ib_upstream_connected' not in status:
        return None
    return bool(status['ib_upstream_connected'])


def _wait_for(predicate, timeout_s: int, interval_s: int = 5) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 — health may be briefly unreachable
            pass
        time.sleep(interval_s)
    return False


def run_scenario(s: Scenario, base_url: str, token: str,
                 parity_cmd: list[str] | None) -> str:
    subprocess.run(s.stop_cmd, check=True)
    cookie = login(base_url, token)

    if s.probe == 'trader':
        degraded = _wait_for(lambda: not _trader_reachable(_health(base_url, cookie)),
                             timeout_s=30)
    elif s.probe == 'ib':
        degraded = _wait_for(
            lambda: _ib_upstream_connected(_health(base_url, cookie)) is False,
            timeout_s=30)
    elif s.probe == 'dashboard':
        degraded = _wait_for(lambda: not _ready(base_url), timeout_s=30)
    else:
        # No live signal for this dependency today (see Scenario.probe) --
        # accept without asserting, and lean on the post-recovery parity
        # check below as the real coherence gate.
        degraded = True

    if s.outage_seconds:
        time.sleep(s.outage_seconds)
    subprocess.run(s.start_cmd, check=True)
    cookie = login(base_url, token)   # dashboard_restart invalidates the session

    if s.probe == 'dashboard':
        recovered = _wait_for(lambda: _ready(base_url), timeout_s=300)
    elif s.probe == 'trader':
        recovered = _wait_for(lambda: _trader_reachable(_health(base_url, cookie)),
                              timeout_s=300)
    elif s.probe == 'ib':
        recovered = _wait_for(
            lambda: _ib_upstream_connected(_health(base_url, cookie)) is True,
            timeout_s=300)
    else:
        recovered = _wait_for(lambda: _trader_reachable(_health(base_url, cookie)),
                              timeout_s=300)

    if not degraded:
        return 'failed'          # outage was never surfaced — silently wrong
    if not recovered:
        # Explicit degraded is acceptable; silence is not.
        if s.probe == 'dashboard':
            return 'explicit-degraded' if not _ready(base_url) else 'failed'
        health = _health(base_url, cookie)
        if s.probe == 'trader':
            return 'explicit-degraded' if not _trader_reachable(health) else 'failed'
        if s.probe == 'ib':
            return 'explicit-degraded' if _ib_upstream_connected(health) is False else 'failed'
        return 'failed'
    if parity_cmd is not None:
        if subprocess.run(parity_cmd).returncode != 0:
            return 'failed'      # recovered but incoherent
    return 'coherent'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hours', type=float, default=8.0)
    parser.add_argument('--base-url', default='http://127.0.0.1:7424')
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--skip-scenarios', action='store_true')
    parser.add_argument('--parity-cmd', default='',
                        help='post-recovery coherence command, e.g. '
                             '"python3 scripts/parity_compare.py"')
    parser.add_argument('--out', default='')
    args = parser.parse_args()

    token = Path(args.token_file).read_text().strip()
    minutes = int(args.hours * 60)
    report_path = Path('/tmp/soak_harness_report.json')
    harness = spawn_harness(args.hours * 60.0, report_path)

    samples: list[Sample] = []
    outcomes: dict[str, str] = {}
    pending = [] if args.skip_scenarios else \
        sorted((s for s in SCENARIOS if s.offset_minutes < minutes),
               key=lambda s: s.offset_minutes)
    parity_cmd = args.parity_cmd.split() if args.parity_cmd else None

    for minute in range(minutes):
        samples.append(sample_container('dashboard', minute))
        while pending and pending[0].offset_minutes <= minute:
            scenario = pending.pop(0)
            outcomes[scenario.name] = run_scenario(
                scenario, args.base_url, token, parity_cmd)
        if harness.poll() is not None:
            break
        time.sleep(60)

    harness.wait(timeout=600)
    raw_metrics = json.loads(report_path.read_text()) if report_path.exists() else {}
    report = evaluate_soak(samples, harness_metrics(raw_metrics), outcomes,
                           SoakThresholds())

    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = Path(args.out) if args.out else \
        Path('~/.local/share/mmr/reports').expanduser() / f'soak_report_{stamp}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.to_json())
    print(f'{"PASS" if report.passed else "FAIL"} -> {out}')
    return 0 if report.passed else 1


if __name__ == '__main__':
    sys.exit(main())
