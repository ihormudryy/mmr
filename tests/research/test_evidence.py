import datetime as dt

import numpy as np
import pandas as pd
import pytest

from trader.research.attribution import RoundTrip
from trader.research.eligibility import EligibilityEvidence, evaluate_eligibility
from trader.research import evidence as ev
from trader.research.rulesets.paper_v1 import PAPER_V1

T = dt.datetime(2024, 2, 1, 15, 0, tzinfo=dt.timezone.utc)


def _trip(pnl, qty=10, entry=100.0, conid=1, month=2):
    close = T.replace(month=month)
    return RoundTrip(conid=conid, open_time=close, close_time=close, quantity=qty,
                     entry_price=entry, exit_price=entry + pnl / qty, pnl=pnl)


def test_expectancy_is_dollar_weighted():
    trips = [_trip(-1, qty=1), _trip(-1, qty=1), _trip(-1, qty=1), _trip(6, qty=3)]
    assert ev.dollar_weighted_expectancy_bps(trips) == pytest.approx(50.0)


def test_expectancy_without_trades_is_missing():
    assert ev.dollar_weighted_expectancy_bps([]) is None


def test_session_returns_start_from_the_starting_equity():
    index = pd.to_datetime(['2024-02-01 15:00', '2024-02-01 20:00', '2024-02-02 20:00'], utc=True)
    curve = pd.Series([100_500.0, 101_000.0, 99_990.0], index=index)
    returns = ev.session_returns([curve], starting_equity=100_000.0)
    assert returns == pytest.approx([0.01, -0.01])


def test_per_period_sharpe():
    assert ev.per_period_sharpe([0.01, 0.03]) == pytest.approx(0.02 / np.std([0.01, 0.03], ddof=1))
    assert ev.per_period_sharpe([0.01]) is None


def test_positive_fold_fraction():
    assert ev.positive_fold_fraction([10.0, -5.0, 3.0, 0.0]) == pytest.approx(0.5)
    assert ev.positive_fold_fraction([]) is None


def test_concentrations():
    trips = [_trip(30, conid=1, month=2), _trip(10, conid=2, month=3), _trip(-50, conid=3, month=3)]
    assert ev.instrument_concentration(trips) == pytest.approx(0.75)
    assert ev.month_concentration(trips) == pytest.approx(1.0)  # March is net negative


@pytest.mark.parametrize('expectancies, robust', [
    ([5.0, 3.0, -1.0], True),
    ([5.0, -3.0, -1.0], False),
    ([5.0, None, 1.0], True),
    ([5.0], None),
])
def test_neighbourhood_rule(expectancies, robust):
    assert ev.neighbourhood_robust(expectancies) is robust


def test_combined_trace_signature_is_64_hex_and_order_sensitive():
    a = ev.combined_trace_signature(['x', 'y'])
    assert len(a) == 64 and int(a, 16) >= 0
    assert a != ev.combined_trace_signature(['y', 'x'])


def test_selection_confidence_falls_with_more_trials():
    rng = np.random.default_rng(0)
    returns = rng.normal(0.002, 0.01, 250)
    few = ev.selection_confidence(returns, n_trials=3, trial_sharpes=[0.1, 0.15, 0.2])
    many = ev.selection_confidence(returns, n_trials=300, trial_sharpes=[0.1, 0.15, 0.2])
    assert few > many


def test_selection_confidence_needs_two_trial_sharpes():
    assert ev.selection_confidence([0.01] * 30, n_trials=1, trial_sharpes=[0.2]) is None


def test_selection_confidence_without_trial_dispersion_is_missing():
    returns = [0.01] * 30
    assert ev.selection_confidence(returns, n_trials=500, trial_sharpes=[0.12, 0.12, 0.12]) is None


@pytest.mark.parametrize('trips, drawdown, passed', [
    ([_trip(5)], -0.01, True),
    ([_trip(-5)], -0.01, False),
    ([_trip(5)], -0.031, False),
    ([], 0.0, False),
])
def test_holdout_rule(trips, drawdown, passed):
    assert ev.holdout_passes(trips, drawdown) is passed


def _passing_walk_forward_evidence(**overrides):
    values = dict(
        n_round_trips=250, n_instruments=10, expectancy_bps_baseline=5.0,
        expectancy_bps_1_5x=3.0, expectancy_bps_2x=1.0, selection_adjusted_confidence=0.97,
        annualized_sharpe_ci_low=0.5, profit_factor=1.5, walk_forward_positive_fraction=0.7,
        max_month_profit_share=0.25, max_instrument_profit_share=0.30,
        neighborhood_robust=True, order_within_envelope=True,
        eligible_regime_positive_fraction=0.8, worst_eligible_regime_loss=-0.05,
        regime_transitions_stable=True)
    values.update(overrides)
    return EligibilityEvidence(**values)


def test_pre_holdout_gate_ignores_holdout_stage_rules():
    outcome = ev.pre_holdout_outcome(evaluate_eligibility(PAPER_V1, _passing_walk_forward_evidence()))
    assert outcome.passed and outcome.failed == () and outcome.missing == ()


def test_pre_holdout_gate_separates_failed_from_missing():
    evidence = _passing_walk_forward_evidence(profit_factor=1.0, order_within_envelope=None)
    outcome = ev.pre_holdout_outcome(evaluate_eligibility(PAPER_V1, evidence))
    assert not outcome.passed
    assert outcome.failed == ('profit_factor_after_costs',)
    assert outcome.missing == ('liquidity_capacity_envelope',)


def test_infinite_profit_factor_fails_closed_and_still_digests():
    decision = evaluate_eligibility(PAPER_V1, _passing_walk_forward_evidence(profit_factor=float('inf')))
    assert 'profit_factor_after_costs' in ev.pre_holdout_outcome(decision).failed
    assert len(decision.digest) == 64
