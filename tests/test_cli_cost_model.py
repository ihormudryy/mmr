"""CLI backtests price fills with the realistic cost model unless told otherwise."""

import pytest

from tests.test_execution_costs import CONFIG, US_CONID, accessor  # noqa: F401  (fixture)
from trader.mmr_cli import _cost_record_fields, _resolve_cost_model, build_parser
from trader.simulation.execution_costs import (
    FlatCosts,
    RealisticCosts,
    parse_execution_costs_config,
)
from trader.simulation.slippage import SquareRootImpact

COSTS_CONFIG = parse_execution_costs_config(CONFIG)


def test_backtest_defaults_to_the_realistic_cost_model():
    args = build_parser().parse_args(['backtest', '-s', 'x.py', '--class', 'X'])
    assert args.cost_model == 'realistic'
    assert args.slippage_model is None and args.slippage_bps is None


def test_bt_sweep_defaults_to_the_realistic_cost_model():
    args = build_parser().parse_args(['bt-sweep', '-s', 'x.py', '--class', 'X',
                                      '--conids', '1', '--grid', '{"A": [1]}'])
    assert args.cost_model == 'realistic'


def test_realistic_resolves_each_conid_venue(accessor):  # noqa: F811
    costs = _resolve_cost_model('realistic', [US_CONID], accessor, costs_config=COSTS_CONFIG)
    assert isinstance(costs, RealisticCosts)
    assert costs.commission(US_CONID, 10, 189.0) == pytest.approx(1.0)


def test_realistic_rejects_legacy_slippage_flags(accessor):  # noqa: F811
    with pytest.raises(ValueError, match='legacy'):
        _resolve_cost_model('realistic', [US_CONID], accessor, slippage_bps=2.0,
                            costs_config=COSTS_CONFIG)


def test_legacy_defaults_to_one_bps_and_half_a_cent(accessor):  # noqa: F811
    costs = _resolve_cost_model('legacy', [US_CONID], accessor)
    assert costs == FlatCosts(slippage_bps=1.0, commission_per_share=0.005)


def test_legacy_accepts_a_named_slippage_model(accessor):  # noqa: F811
    costs = _resolve_cost_model('legacy', [US_CONID], accessor, slippage_model_name='sqrt')
    assert isinstance(costs.slippage_model, SquareRootImpact)


def test_record_fields_name_the_cost_model(accessor):  # noqa: F811
    realistic = _resolve_cost_model('realistic', [US_CONID], accessor, costs_config=COSTS_CONFIG)
    assert _cost_record_fields(realistic) == {
        'cost_model': 'realistic', 'slippage_bps': 0.0, 'commission_per_share': 0.0}
    assert _cost_record_fields(FlatCosts(slippage_bps=2.0, commission_per_share=0.01)) == {
        'cost_model': 'legacy', 'slippage_bps': 2.0, 'commission_per_share': 0.01}
