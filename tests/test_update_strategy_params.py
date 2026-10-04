"""Tests for StrategyRuntime.update_strategy_params — the dashboard's
"edit params" backend: type coercion, atomic YAML persistence, and hot-swap
of the live strategy instance (so changes apply without a service restart).

Spec: docs/superpowers/specs/2026-07-15-dashboard-strategy-controls-design.md
"""

from pathlib import Path

import pytest
import yaml

from trader.strategy.strategy_runtime import StrategyRuntime
from trader.trading.strategy import StrategyState


_STRATEGY_BODY = """
from trader.trading.strategy import Strategy, Signal
from trader.objects import Action

class Tunable(Strategy):
    RANGE_MINUTES = 30
    def on_prices(self, prices):
        return None
"""


def _make_runtime(tmp_path, duckdb_path=None) -> StrategyRuntime:
    rt = StrategyRuntime.__new__(StrategyRuntime)  # skip __init__
    rt.strategies_directory = str(tmp_path)
    rt.strategy_config_file = str(tmp_path / 'strategy_runtime.yaml')
    rt.strategy_implementations = []
    rt.strategies = {}
    rt.streams = {}
    rt.storage = None  # type: ignore
    rt.universe_accessor = None  # type: ignore
    rt._config_mtime = 0.0
    rt._last_dispatched_bar = {}
    rt._trader_gateway = None  # type: ignore
    rt.paper_trading = True
    if duckdb_path:
        rt.duckdb_path = duckdb_path
    return rt


def _write_config(tmp_path: Path, params: dict | None) -> None:
    entry = {
        'name': 'tunable', 'module': 'tunable.py', 'class_name': 'Tunable',
        'bar_size': '1 min', 'historical_days_prior': 5, 'conids': [4391],
        'description': 'test strategy',
    }
    if params:
        entry['params'] = params
    (tmp_path / 'strategy_runtime.yaml').write_text(
        yaml.safe_dump({'strategies': [entry]}))


@pytest.fixture
def runtime(tmp_path):
    (tmp_path / 'tunable.py').write_text(_STRATEGY_BODY)
    _write_config(tmp_path, params={'RANGE_MINUTES': 30})
    rt = _make_runtime(tmp_path)
    rt.load_strategy(
        name='tunable', bar_size_str='1 min', conids=[4391], universe=None,
        historical_days_prior=5, module='tunable.py', class_name='Tunable',
        description='test strategy', params={'RANGE_MINUTES': 30},
    )
    assert len(rt.strategy_implementations) == 1
    return rt


def _saved_params(rt) -> dict:
    cfg = yaml.safe_load(open(rt.strategy_config_file))
    return cfg['strategies'][0].get('params', {})


class TestUpdateStrategyParams:
    def test_updates_yaml_and_coerces_types(self, runtime):
        # Upper-case keys must exist on the class (they're instance-attr
        # overrides applied at load); lower-case keys are free-form
        # (self.params.get idiom) and get blind type coercion.
        result = runtime.update_strategy_params(
            'tunable', {'RANGE_MINUTES': '45', 'vol_mult': '1.5', 'flag': 'true'})
        saved = _saved_params(runtime)
        assert saved['RANGE_MINUTES'] == 45          # int, not '45'
        assert saved['vol_mult'] == 1.5              # float
        assert saved['flag'] is True                 # bool
        assert result['params'] == saved

    def test_unknown_uppercase_param_rejected_before_persisting(self, runtime):
        """A typo'd upper-case key must be refused UP FRONT: were it
        persisted first, the fail-loud loader would refuse the reload — and
        every later restart — leaving a config on disk the strategy can't
        load."""
        with pytest.raises(ValueError) as exc:
            runtime.update_strategy_params('tunable', {'VOLUME_MULTT': '2.0'})
        assert 'VOLUME_MULTT' in str(exc.value)
        assert 'RANGE_MINUTES' in str(exc.value)     # known tunables listed
        assert _saved_params(runtime) == {'RANGE_MINUTES': 30}  # YAML untouched
        assert runtime.get_strategy('tunable') is not None      # instance intact

    def test_hot_swaps_live_instance(self, runtime):
        old = runtime.strategy_implementations[0]
        runtime.strategies[4391] = [old]
        runtime._last_dispatched_bar[(4391, 'tunable')] = 'marker'

        runtime.update_strategy_params('tunable', {'RANGE_MINUTES': '45'})

        assert len(runtime.strategy_implementations) == 1
        new = runtime.strategy_implementations[0]
        assert new is not old
        assert new.ctx.params['RANGE_MINUTES'] == 45
        assert old not in runtime.strategies[4391]
        # The replacement must slot into the existing dispatch list WITHOUT
        # any RPC — calling resolve_symbol here deadlocks (trader_service's
        # loop is blocked awaiting this very RPC's reply).
        assert new in runtime.strategies[4391]
        assert (4391, 'tunable') not in runtime._last_dispatched_bar

    def test_empty_value_deletes_key(self, runtime):
        runtime.update_strategy_params('tunable', {'RANGE_MINUTES': ''})
        assert 'RANGE_MINUTES' not in _saved_params(runtime)

    def test_unknown_strategy_raises(self, runtime):
        with pytest.raises(ValueError, match='nope'):
            runtime.update_strategy_params('nope', {'A': '1'})
        # YAML untouched
        assert _saved_params(runtime) == {'RANGE_MINUTES': 30}

    def test_preserves_disabled_state_across_swap(self, tmp_path, tmp_duckdb_path):
        (tmp_path / 'tunable.py').write_text(_STRATEGY_BODY)
        _write_config(tmp_path, params={'RANGE_MINUTES': 30})
        rt = _make_runtime(tmp_path, duckdb_path=tmp_duckdb_path)
        rt.load_strategy(
            name='tunable', bar_size_str='1 min', conids=[4391], universe=None,
            historical_days_prior=5, module='tunable.py', class_name='Tunable',
            description='', params={'RANGE_MINUTES': 30},
        )
        rt.disable_strategy('tunable')
        assert rt.strategy_implementations[0].state == StrategyState.DISABLED

        rt.update_strategy_params('tunable', {'RANGE_MINUTES': '45'})

        assert rt.strategy_implementations[0].state == StrategyState.DISABLED

    def test_non_string_values_pass_through(self, runtime):
        runtime.update_strategy_params('tunable', {'RANGE_MINUTES': 45})
        assert _saved_params(runtime)['RANGE_MINUTES'] == 45
