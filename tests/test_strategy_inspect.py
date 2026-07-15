"""Tests for trader.strategy.inspect.scan_strategies — the AST-based strategy
scanner shared by `mmr strategies inspect` and the web dashboard's
"available strategies" section. Static analysis only: never executes files."""

from pathlib import Path

import pytest

from trader.strategy.inspect import scan_strategies


STRATEGY_SOURCE = '''
"""Module docstring."""
from trader.trading.strategy import Strategy, Signal
from trader.objects import Action


class MyBreakout(Strategy):
    """Breakout on N-day highs.

    Longer explanation of the thesis over
    several lines.
    """

    RANGE_MINUTES = 30
    VOLUME_MULT: float = 1.5
    _PRIVATE = 'skip me'
    computed = 1 + 1  # lower-case: not a tunable

    def precompute(self, prices):
        lookback = self.params.get('LOOKBACK', 20)
        return {}

    def on_bar(self, prices, state, index):
        return None


class NotAStrategy:
    IGNORED = 1
'''

LEGACY_SOURCE = '''
from trader.trading.strategy import Strategy

class Legacy(Strategy):
    def on_prices(self, prices):
        thresh = self.params.get('threshold', 0.5)
        return None
'''


@pytest.fixture
def strategies_dir(tmp_path) -> Path:
    (tmp_path / 'my_breakout.py').write_text(STRATEGY_SOURCE)
    (tmp_path / 'legacy.py').write_text(LEGACY_SOURCE)
    (tmp_path / '_helper.py').write_text('X = 1')  # underscore: skipped
    (tmp_path / 'broken.py').write_text('def nope(:\n')  # syntax error
    return tmp_path


class TestScanStrategies:
    def test_finds_strategy_classes_only(self, strategies_dir):
        rows = scan_strategies(strategies_dir)
        names = {r['class'] for r in rows if r['class']}
        assert 'MyBreakout' in names
        assert 'Legacy' in names
        assert 'NotAStrategy' not in names

    def test_tunables_from_class_attrs_and_params_get(self, strategies_dir):
        row = next(r for r in scan_strategies(strategies_dir) if r['class'] == 'MyBreakout')
        assert row['tunables']['RANGE_MINUTES'] == 30
        assert row['tunables']['VOLUME_MULT'] == 1.5
        assert row['tunables']['LOOKBACK'] == 20      # self.params.get(...)
        assert '_PRIVATE' not in row['tunables']
        assert 'computed' not in row['tunables']

    def test_mode_detection(self, strategies_dir):
        rows = scan_strategies(strategies_dir)
        assert next(r for r in rows if r['class'] == 'MyBreakout')['mode'] == 'precompute'
        assert next(r for r in rows if r['class'] == 'Legacy')['mode'] == 'on_prices'

    def test_docstring_first_line_and_full(self, strategies_dir):
        row = next(r for r in scan_strategies(strategies_dir) if r['class'] == 'MyBreakout')
        assert row['docstring'] == 'Breakout on N-day highs.'
        assert 'thesis' in row['docstring_full']

    def test_underscore_files_skipped(self, strategies_dir):
        rows = scan_strategies(strategies_dir)
        assert not any('_helper' in r['file'] for r in rows)

    def test_syntax_error_yields_parse_error_row(self, strategies_dir):
        rows = scan_strategies(strategies_dir)
        broken = [r for r in rows if r['mode'] == 'parse_error']
        assert len(broken) == 1
        assert 'broken.py' in broken[0]['file']

    def test_missing_directory_returns_empty(self, tmp_path):
        assert scan_strategies(tmp_path / 'nope') == []
