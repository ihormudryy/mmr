"""Evaluation spec validation: every refusal names the field."""
import datetime as dt
from pathlib import Path

import pytest
import yaml

from tests.test_execution_costs import CONFIG, _definition
from trader.data.universe import UniverseAccessor
from trader.research.evaluation_spec import (
    EvaluationSpecError,
    load_evaluation_spec,
    neighbour_points,
)
from trader.simulation.execution_costs import parse_execution_costs_config

US_CONIDS = list(range(1001, 1009))
STRATEGY = '''
from trader.trading.strategy import Strategy

class Trend(Strategy):
    FAST = 10
    SLOW = 30

    def on_prices(self, prices):
        return None
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / 'strategies').mkdir()
    (tmp_path / 'strategies' / 'trend.py').write_text(STRATEGY)
    return tmp_path


@pytest.fixture
def accessor(tmp_duckdb_path):
    universes = UniverseAccessor(tmp_duckdb_path, 'Universes')
    for conid in US_CONIDS:
        universes.insert('us', _definition(conid, f'S{conid}', 'NASDAQ'))
    universes.insert('asx', _definition(2001, 'BHP', 'ASX'))
    return universes


def _costs():
    venues = {'us': {**CONFIG['venues']['us'], 'calendar': 'XNYS'},
              'asx': {**CONFIG['venues']['asx'], 'calendar': 'XASX'}}
    return parse_execution_costs_config({**CONFIG, 'venues': venues})


def _spec(**overrides):
    base = {
        'name': 'trend_us',
        'strategy': 'strategies/trend.py',
        'class': 'Trend',
        'params': {'FAST': 10, 'SLOW': 30},
        'neighbourhood': {'FAST': [5, 15], 'SLOW': [20, 40]},
        'conids': US_CONIDS,
        'bar_size': '5 mins',
        'period': {'start': '2024-02-01', 'end': '2024-03-28'},
        'walk_forward': {'folds': 2, 'embargo_sessions': 1, 'holdout_sessions': 5},
        'sizing': {'order_notional': 1900, 'account_equity': 100000},
        'max_gross_allocation': 0.05,
    }
    base.update(overrides)
    return base


def _load(repo, accessor, raw):
    path = repo / 'spec.yaml'
    path.write_text(yaml.safe_dump(raw))
    return load_evaluation_spec(path, universe_accessor=accessor, costs_config=_costs(), repo_root=repo)


def test_valid_spec_loads(repo, accessor):
    spec = _load(repo, accessor, _spec())
    assert spec.strategy_path == 'strategies/trend.py'
    assert spec.calendar == 'XNYS'
    assert spec.conids == tuple(US_CONIDS)


def test_neighbour_points_change_one_key_at_a_time(repo, accessor):
    spec = _load(repo, accessor, _spec())
    assert neighbour_points(spec) == [
        {'FAST': 5, 'SLOW': 30}, {'FAST': 15, 'SLOW': 30},
        {'FAST': 10, 'SLOW': 20}, {'FAST': 10, 'SLOW': 40},
    ]


@pytest.mark.parametrize('overrides, field', [
    ({'conids': US_CONIDS[:7]}, 'conids'),
    ({'conids': US_CONIDS + [999]}, '999'),
    ({'conids': US_CONIDS[:7] + [2001]}, 'one market'),
    ({'strategy': '../outside.py'}, 'strategies/'),
    ({'class': 'Missing'}, 'Missing'),
    ({'params': {'FAST': 10, 'slow': 30}}, 'slow'),
    ({'neighbourhood': {'FAST': [10]}}, 'FAST'),
    ({'neighbourhood': {}}, 'neighbourhood'),
    ({'neighbourhood': {'MEDIUM': [1]}}, 'MEDIUM'),
    ({'sizing': {'order_notional': 0, 'account_equity': 100000}}, 'order_notional'),
    ({'sizing': {'order_notional': float('nan'), 'account_equity': 100000}}, 'order_notional'),
    ({'sizing': {'order_notional': 1900, 'account_equity': float('inf')}}, 'account_equity'),
    ({'max_gross_allocation': 1.5}, 'max_gross_allocation'),
    ({'bar_size': '7 parsecs'}, 'bar_size'),
    ({'bar_size': '1 hour'}, 'bar_size'),
    ({'period': {'start': '2024-03-28', 'end': '2024-02-01'}}, 'period'),
])
def test_invalid_spec_is_refused(repo, accessor, overrides, field):
    with pytest.raises(EvaluationSpecError, match=field):
        _load(repo, accessor, _spec(**overrides))


def _walk_forward(**overrides):
    return {**_spec()['walk_forward'], **overrides}


@pytest.mark.parametrize('overrides, field', [
    ({'walk_forward': _walk_forward(folds='two')}, 'walk_forward.folds'),
    ({'walk_forward': _walk_forward(folds=2.5)}, 'walk_forward.folds'),
    ({'walk_forward': _walk_forward(folds=True)}, 'walk_forward.folds'),
    ({'walk_forward': _walk_forward(embargo_sessions='x')}, 'walk_forward.embargo_sessions'),
    ({'walk_forward': _walk_forward(holdout_sessions=None)}, 'walk_forward.holdout_sessions'),
    ({'walk_forward': [1, 2]}, 'walk_forward'),
    ({'period': ['2024-02-01', '2024-03-28']}, 'period'),
    ({'period': '2024-02-01'}, 'period'),
    ({'period': {'start': 20240201, 'end': '2024-03-28'}}, 'period.start'),
    ({'period': {'start': 'soon', 'end': '2024-03-28'}}, 'period.start'),
    ({'sizing': ['x']}, 'sizing'),
    ({'sizing': 'big'}, 'sizing'),
    ({'sizing': {'order_notional': True, 'account_equity': 100000}}, 'order_notional'),
    ({'max_gross_allocation': True}, 'max_gross_allocation'),
    ({'neighbourhood': {'FAST': 5}}, 'neighbourhood.FAST'),
    ({'neighbourhood': {'FAST': None}}, 'neighbourhood.FAST'),
    ({'neighbourhood': ['FAST']}, 'neighbourhood'),
    ({'params': ['FAST']}, 'params'),
    ({'params': 'FAST'}, 'params'),
    ({'params': {'fast': 1, 3: 4}}, 'params'),
    ({'conids': 'abc'}, 'conids'),
    ({'conids': US_CONIDS[:7] + ['abc']}, r'conids\[7\]'),
    ({'conids': US_CONIDS[:7] + ['1008']}, r'conids\[7\]'),
    ({'conids': US_CONIDS[:7] + [1008.9]}, r'conids\[7\]'),
    ({'conids': US_CONIDS[:7] + [True]}, r'conids\[7\]'),
])
def test_malformed_spec_is_refused_naming_the_field(repo, accessor, overrides, field):
    with pytest.raises(EvaluationSpecError, match=field):
        _load(repo, accessor, _spec(**overrides))


def test_duplicate_conids_are_refused_and_named(repo, accessor):
    with pytest.raises(EvaluationSpecError, match=r'conids: duplicate conids \[1001, 1002\]'):
        _load(repo, accessor, _spec(conids=US_CONIDS + [1001, 1002]))


@pytest.mark.parametrize('text, field', [
    ('- name: a\n- name: b\n', 'spec'),
    ('name: [unclosed\n', 'spec'),
])
def test_unreadable_spec_file_is_refused(repo, accessor, text, field):
    path = repo / 'spec.yaml'
    path.write_text(text)
    with pytest.raises(EvaluationSpecError, match=field):
        load_evaluation_spec(path, universe_accessor=accessor, costs_config=_costs(), repo_root=repo)


def test_unquoted_yaml_datetime_period_is_read_as_a_date(repo, accessor):
    period = {'start': dt.datetime(2024, 2, 1, 9, 30), 'end': dt.date(2024, 3, 28)}
    spec = _load(repo, accessor, _spec(period=period))
    assert spec.period_start == dt.date(2024, 2, 1)
    assert type(spec.period_start) is dt.date


def test_non_xnys_market_is_refused(repo, accessor):
    asx = [2001 + i for i in range(8)]
    for conid in asx[1:]:
        accessor.insert('asx', _definition(conid, f'A{conid}', 'ASX'))
    with pytest.raises(EvaluationSpecError, match='XNYS'):
        _load(repo, accessor, _spec(conids=asx))


def test_venue_without_calendar_names_the_key(repo, accessor):
    path = repo / 'spec.yaml'
    path.write_text(yaml.safe_dump(_spec()))
    with pytest.raises(EvaluationSpecError, match='calendar'):
        load_evaluation_spec(path, universe_accessor=accessor,
                             costs_config=parse_execution_costs_config(CONFIG), repo_root=repo)


@pytest.mark.parametrize('source, cause', [
    ('import no_such_module_for_mmr_tests\n', 'ModuleNotFoundError'),
    ('class Trend(:\n', 'SyntaxError'),
])
def test_a_strategy_file_that_cannot_be_imported_is_refused_naming_it(repo, accessor, source, cause):
    (repo / 'strategies' / 'trend.py').write_text(source)
    with pytest.raises(EvaluationSpecError, match=rf'strategy: trend\.py cannot be imported: {cause}'):
        _load(repo, accessor, _spec())
