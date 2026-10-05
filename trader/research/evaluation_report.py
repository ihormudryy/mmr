"""Human + machine readable report for one `research evaluate` run."""
from __future__ import annotations

import dataclasses
import json
import math
import uuid
from pathlib import Path
from typing import Any, Optional

from trader.simulation.live_rules import NOT_MIRRORED


def _jsonable(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def write_evaluation_report(reports_dir: Path, *, spec, family_id: str, stage: str, state: str,
                            artifact_id, failed, missing, evidence, main, neighbours,
                            created_at, container_digest: str = 'local:none',
                            market_context: Optional[dict] = None,
                            missing_causes: Optional[dict] = None) -> Path:
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
        'market_context': _json_safe(market_context or {}),
        'missing_causes': dict(missing_causes or {}),
        'not_mirrored': list(NOT_MIRRORED),
    }
    (reports_dir / f'{stem}.json').write_text(json.dumps(data, indent=2, default=str))
    md_path = reports_dir / f'{stem}.md'
    md_path.write_text(_markdown(data))
    return md_path


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return _jsonable(value)


def _market_context_lines(context: dict) -> list[str]:
    lines = ['', '## Market context']
    regimes = context.get('regimes')
    if regimes:
        lines += ['', '### Regimes', '', '| Regime | Trades | Net P&L | Share | Adequate |',
                  '|---|---|---|---|---|']
        lines += [f"| {r['regime']} | {r['trades']} | {r['net_pnl']} | {r['share']} | "
                  f"{r['adequate']} |" for r in regimes['table']]
        lines += ['', f"Regime changes: {regimes['n_changes']}. Round trips entered in the first "
                      f"sessions of a new regime: {regimes['transition_group_trades']} "
                      f"(net P&L {regimes['transition_group_pnl']})."]
    liquidity = context.get('liquidity')
    if liquidity:
        lines += ['', '### Liquidity', '',
                  f"Capacity estimate: {liquidity['capacity_estimate']}", '',
                  '| Conid | Floor median $ volume | Date | Order share |', '|---|---|---|---|']
        lines += [f"| {r['conid']} | {r['floor_median']} | {r['floor_date']} | {r['share']} |"
                  for r in liquidity['rows']]
    benchmark = context.get('benchmark')
    if benchmark:
        lines += ['', '### Benchmark', '', '| Field | Value |', '|---|---|']
        lines += [f'| {k} | {v} |' for k, v in benchmark.items()]
    return lines


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
    if data['market_context']:
        lines += _market_context_lines(data['market_context'])
    if data['missing_causes']:
        lines += ['', '## Missing evidence causes', '', '| Rule | Cause |', '|---|---|']
        lines += [f'| {rule} | {cause} |' for rule, cause in data['missing_causes'].items()]
    lines += ['', '## Not mirrored from live paper trading', '']
    lines += [f'- {item}' for item in data['not_mirrored']]
    return '\n'.join(lines) + '\n'
