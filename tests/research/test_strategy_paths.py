from pathlib import Path

from trader.research.strategy_paths import normalize_strategy_path, repo_root


def test_relative_path_is_kept():
    assert normalize_strategy_path('./strategies/orb.py') == 'strategies/orb.py'


def test_absolute_path_under_the_repo_becomes_relative():
    absolute = repo_root() / 'strategies' / 'orb.py'
    assert normalize_strategy_path(str(absolute)) == 'strategies/orb.py'


def test_absolute_path_from_another_install_keeps_the_strategies_suffix():
    assert normalize_strategy_path('/home/trader/mmr/strategies/orb.py') == 'strategies/orb.py'


def test_root_override(tmp_path: Path):
    assert normalize_strategy_path(str(tmp_path / 'strategies' / 'x.py'), tmp_path) == 'strategies/x.py'
