"""Human + machine readable report for one `research evaluate` run."""
from __future__ import annotations

import dataclasses
import json
import math
import uuid
from pathlib import Path
from typing import Any

from trader.simulation.live_rules import NOT_MIRRORED


def _jsonable(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def write_evaluation_report(reports_dir: Path, *, spec, family_id: str, stage: str, state: str,
                            artifact_id, failed, missing, evidence, main, neighbours,
                            created_at, container_digest: str = 'local:none') -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    # The short id keeps two runs in the same second from overwriting each other.
    stem = f'evaluation_{spec.name}_{created_at:%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}'
    data = {
        'spec': spec.name, 'strategy': spec.strategy_path, 'class': spec.class_name,
        'family_id': family_id, 'artifact_id': artifact_id, 'stage': stage, 'state': state,
        'container_digest': container_digest,
        'failed_rules': list(failed), 'missing_rules': list(missing),
        'evidence': {k: _jsonable(v) for k, v in dataclasses.asdict(evidence).items()},
        'folds': [
            {'index': o.job.window_index, 'start': o.job.start.isoformat(),
             'end': o.job.end.isoformat(), 'net_pnl': o.net_pnl,
             'live_rule_blocks': dict(o.live_rule_blocks)}
            for o in main.outcomes.get(1.0, [])],
        'trials': [
            {'params': p.params, 'trial_id': p.trial_id,
             **{k: _jsonable(p.metrics.get(k))
                for k in ('oos_expectancy_bps', 'oos_net_pnl', 'daily_sharpe', 'n_round_trips')}}
            for p in [main, *neighbours]],
        'not_mirrored': list(NOT_MIRRORED),
    }
    (reports_dir / f'{stem}.json').write_text(json.dumps(data, indent=2, default=str))
    md_path = reports_dir / f'{stem}.md'
    md_path.write_text(_markdown(data))
    return md_path


def _markdown(data: dict) -> str:
    lines = [
        f"# Evaluation {data['spec']}", '',
        f"- Strategy: `{data['strategy']}` / `{data['class']}`",
        f"- Family: `{data['family_id']}`",
        f"- Stage: **{data['stage']}**, state: **{data['state']}**",
        f"- Artifact: `{data['artifact_id']}`" if data['artifact_id'] else '- Artifact: none (holdout not opened)',
        f"- Failed rules: {', '.join(data['failed_rules']) or 'none'}",
        f"- Missing evidence: {', '.join(data['missing_rules']) or 'none'}",
        '', '## Evidence', '', '| Field | Value |', '|---|---|',
    ]
    lines += [f'| {k} | {v} |' for k, v in data['evidence'].items()]
    lines += ['', '## Walk-forward folds (main point, 1x costs)', '',
              '| Fold | Window | Net P&L | Entries refused by live rules |', '|---|---|---|---|']
    lines += [f"| {f['index']} | {f['start'][:10]} – {f['end'][:10]} | {f['net_pnl']:.2f} | "
              f"{f['live_rule_blocks'] or '-'} |" for f in data['folds']]
    lines += ['', '## Trials', '', '| Params | Expectancy (bps) | Net P&L | Daily Sharpe | Round trips |',
              '|---|---|---|---|---|']
    lines += [f"| {t['params']} | {t['oos_expectancy_bps']} | {t['oos_net_pnl']} | "
              f"{t['daily_sharpe']} | {t['n_round_trips']} |" for t in data['trials']]
    lines += ['', '## Not mirrored from live paper trading', '']
    lines += [f'- {item}' for item in data['not_mirrored']]
    return '\n'.join(lines) + '\n'
