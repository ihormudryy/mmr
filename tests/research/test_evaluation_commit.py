import shutil
import subprocess
from pathlib import Path

import pytest

from trader.research.evaluation import EvaluationError, _repository_commit

pytestmark = pytest.mark.skipif(shutil.which('git') is None, reason='git is not on PATH')


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(['git', '-C', str(repo), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', message)
    return _git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, 'init', '-q')
    _git(tmp_path, 'config', 'user.email', 'test@example.com')
    _git(tmp_path, 'config', 'user.name', 'Test')
    _git(tmp_path, 'config', 'commit.gpgsign', 'false')
    (tmp_path / 'strategies').mkdir()
    return tmp_path


@pytest.fixture
def strategy(repo):
    path = repo / 'strategies' / 'x.py'
    path.write_text('VALUE = 1\n')
    return path


def test_returns_the_commit_that_added_the_strategy(repo, strategy):
    commit = _commit_all(repo, 'add strategy')
    assert _repository_commit(repo, strategy) == commit


def test_an_unrelated_commit_does_not_change_it(repo, strategy):
    commit = _commit_all(repo, 'add strategy')
    (repo / 'notes.txt').write_text('unrelated\n')
    assert _commit_all(repo, 'unrelated change') != commit

    assert _repository_commit(repo, strategy) == commit


def test_a_commit_that_edits_the_strategy_changes_it(repo, strategy):
    first = _commit_all(repo, 'add strategy')
    strategy.write_text('VALUE = 2\n')
    second = _commit_all(repo, 'edit strategy')

    assert second != first
    assert _repository_commit(repo, strategy) == second


def test_uncommitted_edit_is_refused(repo, strategy):
    _commit_all(repo, 'add strategy')
    strategy.write_text('VALUE = 2\n')
    with pytest.raises(EvaluationError, match='uncommitted'):
        _repository_commit(repo, strategy)


def test_untracked_strategy_is_refused(repo, strategy):
    (repo / 'notes.txt').write_text('first commit needs a file\n')
    _git(repo, 'add', 'notes.txt')
    _git(repo, 'commit', '-q', '-m', 'notes only')
    with pytest.raises(EvaluationError, match='not committed'):
        _repository_commit(repo, strategy)


def test_a_directory_that_is_not_a_git_checkout_gives_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path.parent))
    strategy = tmp_path / 'x.py'
    strategy.write_text('VALUE = 1\n')
    assert _repository_commit(tmp_path, strategy) == 'unknown'
