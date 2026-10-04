"""Is this bundle the evidence for the strategy that is about to run?

A bundle attests one strategy file (by content hash), one class, one set of
params, one instrument list and one bar size. Anything else is a
different strategy, and running it under this bundle would be a false claim.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, FrozenSet, Mapping, Optional, Sequence

from trader.data.backtest_store import compute_strategy_hash
from trader.research.canonical import canonical_json_bytes
from trader.research.strategy_paths import normalize_strategy_path


class StrategyBindingError(Exception):
    """The bundle does not attest this strategy; the message lists every difference."""


@dataclass(frozen=True)
class AttestedStrategy:
    strategy_path: str
    class_name: str
    source_digest: str
    parameters: Mapping[str, Any]
    instruments: FrozenSet[str]
    bar_size: Optional[str]
    order_notional: Optional[float]


def load_attested_strategy(bundle_path: Path, *, source_digest: str,
                           parameters: Mapping[str, Any],
                           instruments: Sequence[str]) -> AttestedStrategy:
    family = json.loads((Path(bundle_path) / 'family.json').read_text(encoding='utf-8'))
    protocol = family.get('validation_protocol') or {}
    notional = (family.get('cost_model') or {}).get('order_notional')
    return AttestedStrategy(
        strategy_path=family['strategy_path'], class_name=family['class_name'],
        source_digest=source_digest, parameters=dict(parameters),
        instruments=frozenset(str(i) for i in instruments),
        bar_size=protocol.get('bar_size'),
        order_notional=float(notional) if notional is not None else None)


# Where the bundle lives is transport, not strategy behaviour.
_TRANSPORT_PARAMS = frozenset({'artifact_bundle_path'})


def _behaviour_params(params: Optional[Mapping[str, Any]]) -> dict:
    """Every param that can change what the strategy does, lower-case ones included."""
    return {k: v for k, v in (params or {}).items() if k not in _TRANSPORT_PARAMS}


def _same_values(actual: dict, attested: dict) -> bool:
    """Exact comparison: 600 and 600.0 differ, unlike Python ``==``."""
    try:
        return canonical_json_bytes(actual) == canonical_json_bytes(attested)
    except (TypeError, ValueError):  # NaN or a type canonical JSON refuses
        return False


def _bar_size_key(value: Any) -> str:
    return ' '.join(str(value).lower().split())


def check_strategy_binding(attested: Optional[AttestedStrategy], *, module_file: Path,
                           class_name: str, params: Optional[Mapping[str, Any]],
                           conids: Optional[Sequence[Any]], bar_size: str,
                           loaded_source_digest: Optional[str] = None) -> None:
    """``loaded_source_digest`` is the digest of code already loaded; without it
    (Activate, before anything is loaded) the file on disk is hashed instead."""
    if attested is None:
        raise StrategyBindingError('the verified bundle carries no attested strategy')
    problems = []
    if normalize_strategy_path(str(module_file)) != normalize_strategy_path(attested.strategy_path):
        problems.append(f'strategy file {module_file} is not the attested {attested.strategy_path}')
    if class_name != attested.class_name:
        problems.append(f'class {class_name!r} is not the attested {attested.class_name!r}')
    if loaded_source_digest is not None:
        if loaded_source_digest != attested.source_digest:
            problems.append(f'loaded code of {Path(module_file).name} differs from the attested '
                            f'file; reload the strategy')
    else:
        actual_digest = compute_strategy_hash(str(module_file))
        if not actual_digest:
            problems.append(f'strategy file {module_file} cannot be read')
        elif actual_digest != attested.source_digest:
            problems.append(f'strategy file {Path(module_file).name} changed since attestation')
    actual_params = _behaviour_params(params)
    attested_params = _behaviour_params(attested.parameters)
    if not _same_values(actual_params, attested_params):
        problems.append(f'params {actual_params} differ from attested {attested_params}')
    actual_conids = {str(c) for c in (conids or ())}
    if actual_conids != set(attested.instruments):
        problems.append(f'conids {sorted(actual_conids)} differ from attested {sorted(attested.instruments)}')
    if attested.bar_size is None or _bar_size_key(bar_size) != _bar_size_key(attested.bar_size):
        problems.append(f'bar size {bar_size!r} differs from attested {attested.bar_size!r}')
    if problems:
        raise StrategyBindingError('; '.join(problems))
