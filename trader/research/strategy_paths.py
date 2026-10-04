"""One strategy file, one identity: repo-relative strategy paths.

Sweeps store absolute paths (often from inside the container), research
families store what was typed. Comparing them raw would split one strategy's
trial history into several.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Optional


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def normalize_strategy_path(path: str, root: Optional[Path] = None) -> str:
    raw = os.path.normpath(os.path.expanduser(str(path)))
    candidate = Path(raw)
    if not candidate.is_absolute():
        return PurePosixPath(*candidate.parts).as_posix()
    base = (root or repo_root()).resolve()
    try:
        return candidate.resolve().relative_to(base).as_posix()
    except ValueError:
        parts = candidate.parts
        if 'strategies' in parts:
            start = len(parts) - 1 - parts[::-1].index('strategies')
            return '/'.join(parts[start:])
        return candidate.as_posix()


def resolve_strategy_file(module: str, strategies_dir: str) -> Path:
    requested = Path(os.path.expanduser(module))
    if requested.is_absolute():
        return requested
    in_strategies_dir = Path(os.path.expanduser(strategies_dir)).resolve() / requested
    if in_strategies_dir.exists():
        return in_strategies_dir
    return (repo_root() / requested).resolve()
