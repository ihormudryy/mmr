"""REPL history must not crash on read-only container roots."""

from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.history import FileHistory, InMemoryHistory

from trader.mmr_cli import _repl_history


def test_repl_history_prefers_logs_dir(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    logs = home / '.local' / 'share' / 'mmr' / 'logs'
    logs.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('TMPDIR', raising=False)

    history = _repl_history()
    assert isinstance(history, FileHistory)
    assert Path(history.filename) == logs / '.mmr_repl_history'
    assert (logs / '.mmr_repl_history').is_file()


def test_repl_history_falls_back_to_tmpdir_when_logs_readonly(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    logs = home / '.local' / 'share' / 'mmr' / 'logs'
    logs.mkdir(parents=True)
    tmpdir = tmp_path / 'tmp'
    tmpdir.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('TMPDIR', str(tmpdir))

    real_open = open

    def _open(path, mode='r', *args, **kwargs):
        if Path(path) == logs / '.mmr_repl_history':
            raise OSError(30, 'Read-only file system')
        return real_open(path, mode, *args, **kwargs)

    with patch('builtins.open', _open):
        history = _repl_history()

    assert isinstance(history, FileHistory)
    assert Path(history.filename) == tmpdir / '.mmr_repl_history'


def test_repl_history_falls_back_to_memory_when_nothing_writable(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / '.local' / 'share' / 'mmr' / 'logs').mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('TMPDIR', str(tmp_path / 'tmp'))

    with patch('builtins.open', side_effect=OSError(30, 'Read-only file system')):
        history = _repl_history()

    assert isinstance(history, InMemoryHistory)
