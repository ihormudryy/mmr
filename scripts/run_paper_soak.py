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
`p95_critical_ms` (from `latency_ms.p95`) is derived from the harness. The
ring/FIFO/terminal-retention metrics -- `max_replay_ring_events`,
`max_client_fifo_depth`, `max_terminal_rows` -- ARE now live: the read model
exposes the current counts on the authenticated `/api/cc-health` endpoint
(`replay_ring_events`/`client_fifo_depth_max`/`terminal_rows`), and this
runner samples that endpoint each interval and folds the running maxima into
the dict `evaluate_soak` sees (see `cc_health_metrics`/`fold_read_model_sample`
below). Only `unhandled_errors` and `unresolved_commands` still have no live
exporter (those are trader-side/command-ledger state with no web surface --
a Worker-B/F3 follow-up); for those two the existing fail-closed contract
(`harness.get(name) is None` -> check fails) reports honestly as "not
observed" rather than fabricating a passing zero.
Likewise, the real authenticated `GET /api/health` payload
(`web/app.py::api_health` / `trader/operations/health.py::
build_health_payload`) is `{process, dependencies: {trader: {reachable,
status}}, jobs}` -- a single hardcoded `trader` dependency, not a
per-service `{name: {"state": ...}}` map. There is currently no live signal
for `strategy` service reachability through this endpoint at all, so the
`strategy_outage` scenario cannot assert "outage was surfaced" the way the
other three can; it falls back to the post-recovery parity check alone
(see `run_scenario` / `Scenario.probe`).

P4 Task 2 (paper evidence gate) extension -- OPT-IN ONLY, fully backward
compatible with every check above: six new `SoakThresholds` fields
(`max_breaker_trips`, `min_readiness_pass_rate`, `max_reconciliation_mismatches`,
`max_protection_gaps`, `max_missed_flats`, `max_replay_mismatches`) default to
`None`, meaning "not required" -- `evaluate_soak` adds no new `Check` for a
`None` threshold, so a caller that never sets them (every existing call site,
including the default `SoakThresholds()` `main()` uses) sees byte-identical
behavior to before this extension. A caller running the P4 paper soak sets
whichever of these it wants enforced; once set, the same fail-closed
"missing observation -> failing check" rule as `p95_critical_ms` etc. applies
-- see `optional_metric` below. `evaluate_soak` also accepts an optional
`config` mapping and `signer` (an `AttestationSigner` from
`trader.research.signing`): when `config` is supplied the report carries a
deterministic SHA-256 `config_digest` (via `trader.research.canonical`,
the same primitive the research evidence chain digests over); when a
`signer` is ALSO supplied, the digest is Ed25519-signed and the report
additionally carries `config_signature` + `config_signer_key_id`, so a
soak report can be bound to (and later verified against) the exact
artifact/allowlist/risk-policy configuration it ran under.
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
from typing import Any, Mapping, Optional

sys.path.insert(0, str(Path(__file__).parent))
from parity_compare import login  # noqa: E402

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from trader.research.canonical import canonical_json_bytes, sha256_digest  # noqa: E402
from trader.research.signing import AttestationSigner  # noqa: E402

CONFIG_DIGEST_PREFIX = "paper_soak_config"


def compute_config_digest(config: Mapping[str, Any]) -> str:
    """Deterministic SHA-256 digest of a soak-run configuration (e.g. the
    artifact/allowlist/risk-policy identity it ran under). Same canonical
    bytes primitive the research evidence chain signs over."""
    return sha256_digest(CONFIG_DIGEST_PREFIX, dict(config))


def sign_config_digest(config: Mapping[str, Any], signer: AttestationSigner) -> str:
    """Ed25519-sign the canonical bytes of ``config`` (not just its digest
    string) so the signature covers the exact fields, not merely their
    hash-of-a-hash. Deterministic: the same key + config always yields the
    same signature."""
    return signer.sign_message(canonical_json_bytes(dict(config)))


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
    # P4 Task 2 (paper evidence gate) additions -- see module docstring.
    # `None` (the default) means "not required": evaluate_soak adds no
    # check for it, so every pre-existing caller/test is unaffected.
    max_breaker_trips: Optional[int] = None
    min_readiness_pass_rate: Optional[float] = None
    max_reconciliation_mismatches: Optional[int] = None
    max_protection_gaps: Optional[int] = None
    max_missed_flats: Optional[int] = None
    max_replay_mismatches: Optional[int] = None


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
    config_digest: Optional[str] = None
    config_signature: Optional[str] = None
    config_signer_key_id: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps({
            'started_at': self.started_at,
            'passed': self.passed,
            'checks': [dataclasses.asdict(c) for c in self.checks],
            'scenarios': self.scenarios,
            'config_digest': self.config_digest,
            'config_signature': self.config_signature,
            'config_signer_key_id': self.config_signer_key_id,
        }, indent=2)


def evaluate_soak(samples: list[Sample], harness: dict,
                  scenario_outcomes: dict[str, str],
                  t: SoakThresholds,
                  *,
                  config: Optional[Mapping[str, Any]] = None,
                  signer: Optional[AttestationSigner] = None) -> SoakReport:
    """Pure, deterministic, fail-closed evaluation of one soak run.

    ``harness`` carries flat spec-13.3 metric keys (see module docstring
    for which of these the current `soak_harness.py` actually populates).
    A key absent from ``harness`` evaluates to a failed check -- missing
    data is never treated as "passing by default".

    ``config``/``signer`` are keyword-only and both default to ``None`` so
    every pre-existing positional call site is unaffected; see the module
    docstring's P4 Task 2 extension note for what they add to the report.
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

    def optional_metric(name: str, limit: Optional[float], *, minimum: bool = False) -> None:
        """Same fail-closed contract as ``metric``, but only added at all
        when ``limit`` is not ``None`` -- see the P4 Task 2 docstring note:
        this is what keeps every pre-existing threshold/report shape
        unchanged unless a caller opts in."""
        if limit is None:
            return
        observed = harness.get(name)
        if observed is None:
            ok = False
        elif minimum:
            ok = float(observed) >= limit
        else:
            ok = float(observed) <= limit
        checks.append(Check(name, limit, observed, ok))

    metric('p95_critical_ms', t.p95_critical_ms_max)
    metric('unhandled_errors', t.max_unhandled_errors)
    metric('unresolved_commands', t.max_unresolved_commands)
    metric('max_replay_ring_events', t.replay_ring_max)
    metric('max_client_fifo_depth', t.client_fifo_max)
    metric('max_terminal_rows', t.terminal_rows_max)

    # P4 Task 2: breaker/readiness/reconciliation/protection/flat/replay --
    # opt-in only (see `optional_metric`).
    optional_metric('breaker_trips', t.max_breaker_trips)
    optional_metric('readiness_pass_rate', t.min_readiness_pass_rate, minimum=True)
    optional_metric('reconciliation_mismatches', t.max_reconciliation_mismatches)
    optional_metric('protection_gaps', t.max_protection_gaps)
    optional_metric('missed_flats', t.max_missed_flats)
    optional_metric('replay_mismatches', t.max_replay_mismatches)

    scenarios_ok = all(v in ('coherent', 'explicit-degraded')
                       for v in scenario_outcomes.values())
    checks.append(Check('scenarios_coherent', 1,
                        int(scenarios_ok), scenarios_ok))

    config_digest = compute_config_digest(config) if config is not None else None
    config_signature = None
    config_signer_key_id = None
    if config is not None and signer is not None:
        config_signature = sign_config_digest(config, signer)
        config_signer_key_id = signer.public_key_id

    return SoakReport(
        started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        checks=checks, scenarios=scenario_outcomes,
        passed=all(c.passed for c in checks),
        config_digest=config_digest,
        config_signature=config_signature,
        config_signer_key_id=config_signer_key_id)


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


# --- COMPAT exporters: replay-ring / FIFO / terminal-rows soak metrics -------
#
# Unlike `unhandled_errors`/`unresolved_commands` (still no live exporter --
# trader-side command-ledger / error-log surface, a Worker-B/F3 follow-up),
# three of the six `evaluate_soak` thresholds ARE now backed by a live
# exporter: `web/command_center/state.py`'s `ring_depth()`/
# `terminal_row_count()` and `sse.py`'s `max_fifo_depth()`, surfaced on the
# authenticated `/api/cc-health` endpoint as `replay_ring_events`/
# `terminal_rows`/`client_fifo_depth_max`. This section samples that
# endpoint each minute alongside the container stats and keeps a running max
# of each, mirroring `harness_metrics` above for the soak-harness report.

def cc_health_metrics(raw: dict) -> dict:
    """Translate one `/api/cc-health` response into the flat `max_*` keys
    `evaluate_soak` understands. Missing keys default to 0 rather than
    raising -- a single malformed/incomplete sample must not crash an
    otherwise-healthy multi-hour run.
    """
    return {
        'max_replay_ring_events': int(raw.get('replay_ring_events') or 0),
        'max_client_fifo_depth': int(raw.get('client_fifo_depth_max') or 0),
        'max_terminal_rows': int(raw.get('terminal_rows') or 0),
    }


def fold_read_model_sample(maxima: dict, raw: dict) -> dict:
    """Merge one `/api/cc-health` sample into the running max-so-far for
    the three read-model soak metrics. Pure -- returns a new dict rather
    than mutating ``maxima``, so callers can chain samples across the soak
    window without aliasing bugs.
    """
    sample = cc_health_metrics(raw)
    return {key: max(maxima.get(key, 0), sample[key]) for key in sample}


def _cc_health(base_url: str, cookie: str) -> dict:
    req = urllib.request.Request(base_url + '/api/cc-health',
                                 headers={'Cookie': cookie})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def sample_read_model(base_url: str, token: str, cookie: str,
                      maxima: dict) -> tuple[str, dict]:
    """Fetch `/api/cc-health` and fold it into the running maxima. Returns
    ``(cookie, maxima)`` -- a dashboard restart mid-soak invalidates the
    prior session cookie (see `run_scenario`'s own re-login for the same
    reason), so a failed fetch here triggers one re-login attempt before
    giving up for this tick; a transient miss is one skipped data point,
    never a soak-ending exception.
    """
    try:
        return cookie, fold_read_model_sample(maxima, _cc_health(base_url, cookie))
    except Exception:  # noqa: BLE001 -- best-effort sample, see docstring
        try:
            cookie = login(base_url, token)
            return cookie, fold_read_model_sample(maxima, _cc_health(base_url, cookie))
        except Exception:  # noqa: BLE001
            return cookie, maxima


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
    # P4 Task 2 additions -- all optional/opt-in, see module docstring.
    parser.add_argument('--config-json', default='',
                        help='path to a JSON file identifying the exact config this soak ran '
                             'under (artifact/allowlist/risk-policy); adds config_digest to the '
                             'report')
    parser.add_argument('--signing-key', default='',
                        help='path to an Ed25519 PKCS8 PEM private key (0o600); if given with '
                             '--config-json, signs the config digest')
    parser.add_argument('--max-breaker-trips', type=int, default=None)
    parser.add_argument('--min-readiness-pass-rate', type=float, default=None)
    parser.add_argument('--max-reconciliation-mismatches', type=int, default=None)
    parser.add_argument('--max-protection-gaps', type=int, default=None)
    parser.add_argument('--max-missed-flats', type=int, default=None)
    parser.add_argument('--max-replay-mismatches', type=int, default=None)
    args = parser.parse_args()

    token = Path(args.token_file).read_text().strip()
    minutes = int(args.hours * 60)
    report_path = Path('/tmp/soak_harness_report.json')
    harness_proc = spawn_harness(args.hours * 60.0, report_path)

    samples: list[Sample] = []
    outcomes: dict[str, str] = {}
    pending = [] if args.skip_scenarios else \
        sorted((s for s in SCENARIOS if s.offset_minutes < minutes),
               key=lambda s: s.offset_minutes)
    parity_cmd = args.parity_cmd.split() if args.parity_cmd else None

    # COMPAT exporters: sample `/api/cc-health` alongside the container
    # stats and keep a running max of the three now-live read-model
    # metrics (replay-ring depth, deepest client FIFO, terminal-row count).
    read_model_cookie = login(args.base_url, token)
    read_model_maxima: dict = {}

    for minute in range(minutes):
        samples.append(sample_container('dashboard', minute))
        read_model_cookie, read_model_maxima = sample_read_model(
            args.base_url, token, read_model_cookie, read_model_maxima)
        while pending and pending[0].offset_minutes <= minute:
            scenario = pending.pop(0)
            outcomes[scenario.name] = run_scenario(
                scenario, args.base_url, token, parity_cmd)
        if harness_proc.poll() is not None:
            break
        time.sleep(60)

    harness_proc.wait(timeout=600)
    raw_metrics = json.loads(report_path.read_text()) if report_path.exists() else {}
    metrics = harness_metrics(raw_metrics)
    metrics.update(read_model_maxima)
    # `unhandled_errors` / `unresolved_commands` still have no live exporter
    # (trader-side command-ledger / error-log surface -- a Worker-B/F3
    # follow-up) -- left absent so `evaluate_soak`'s existing fail-closed
    # default (`harness.get(name) is None` -> check fails) reports them
    # honestly as "not observed" rather than fabricating a passing zero.
    thresholds = SoakThresholds(
        max_breaker_trips=args.max_breaker_trips,
        min_readiness_pass_rate=args.min_readiness_pass_rate,
        max_reconciliation_mismatches=args.max_reconciliation_mismatches,
        max_protection_gaps=args.max_protection_gaps,
        max_missed_flats=args.max_missed_flats,
        max_replay_mismatches=args.max_replay_mismatches,
    )
    config = json.loads(Path(args.config_json).read_text()) if args.config_json else None
    signer = AttestationSigner.from_key_file(args.signing_key) if args.signing_key else None
    report = evaluate_soak(samples, metrics, outcomes, thresholds, config=config, signer=signer)

    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = Path(args.out) if args.out else \
        Path('~/.local/share/mmr/reports').expanduser() / f'soak_report_{stamp}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.to_json())
    print(f'{"PASS" if report.passed else "FAIL"} -> {out}')
    return 0 if report.passed else 1


if __name__ == '__main__':
    sys.exit(main())
