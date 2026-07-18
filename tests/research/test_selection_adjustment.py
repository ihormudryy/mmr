"""P2 Task 5 -- selection-bias-aware statistics (design §8.3).

Covers the quantities the ``PAPER_ELIGIBLE`` gate reads: bootstrap intervals
(reproducible, tightening with sample size), the deflated / selection-adjusted
Sharpe (the anti-selection-bias core property -- more trials must LOWER
confidence), profit factor, profit concentration, and the remove-top-outlier
diagnostic. Fully offline + deterministic (seeded RNG, no wall-clock).
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from trader.research.statistics import (
    BootstrapCI,
    ConcentrationResult,
    OutlierDiagnostic,
    annualized_sharpe,
    annualized_sharpe_ci,
    bootstrap_mean_ci,
    deflated_sharpe_ratio,
    expected_max_sharpe_null,
    profit_concentration,
    profit_factor,
    remove_top_outlier_diagnostic,
    selection_adjusted_confidence,
)


def _normal(n, mean, sd, seed):
    return np.random.default_rng(seed).normal(mean, sd, n)


# --------------------------------------------------------------------------- #
# Bootstrap CI: reproducibility + tightening with sample size
# --------------------------------------------------------------------------- #
class TestBootstrapMeanCI:
    def test_seeded_reproducible_byte_for_byte(self):
        pnls = _normal(200, 5.0, 20.0, seed=1)
        a = bootstrap_mean_ci(pnls, seed=7)
        b = bootstrap_mean_ci(pnls, seed=7)
        assert a == b
        assert isinstance(a, BootstrapCI)
        assert a.low <= a.point <= a.high

    def test_point_is_the_sample_mean(self):
        pnls = _normal(120, 3.0, 10.0, seed=2)
        ci = bootstrap_mean_ci(pnls, seed=3)
        assert ci.point == pytest.approx(float(np.mean(pnls)))

    def test_ci_tightens_as_sample_grows(self):
        small = _normal(20, 0.5, 1.0, seed=11)
        large = _normal(600, 0.5, 1.0, seed=12)
        w_small = bootstrap_mean_ci(small, seed=5)
        w_large = bootstrap_mean_ci(large, seed=5)
        assert (w_large.high - w_large.low) < (w_small.high - w_small.low)

    def test_below_minimum_sample_returns_none(self):
        assert bootstrap_mean_ci([1.0] * 9, seed=1) is None
        assert bootstrap_mean_ci([1.0] * 10, seed=1) is not None


class TestAnnualizedSharpe:
    def test_zero_variance_is_zero(self):
        assert annualized_sharpe([0.01] * 50, periods_per_year=252) == 0.0

    def test_single_point_is_zero(self):
        assert annualized_sharpe([0.01], periods_per_year=252) == 0.0

    def test_positive_drift_positive_sharpe(self):
        r = _normal(500, 0.001, 0.01, seed=9)
        assert annualized_sharpe(r, periods_per_year=252) > 0.0

    def test_ci_lower_bound_readable_and_reproducible(self):
        r = _normal(500, 0.002, 0.01, seed=21)
        a = annualized_sharpe_ci(r, periods_per_year=252, seed=4)
        b = annualized_sharpe_ci(r, periods_per_year=252, seed=4)
        assert a == b and a.low <= a.point <= a.high

    def test_ci_none_below_minimum(self):
        assert annualized_sharpe_ci([0.01] * 9, periods_per_year=252, seed=1) is None


# --------------------------------------------------------------------------- #
# Expected max Sharpe under the null + selection adjustment (anti-selection-bias)
# --------------------------------------------------------------------------- #
class TestExpectedMaxSharpeNull:
    def test_single_trial_is_zero(self):
        assert expected_max_sharpe_null(1, 0.5) == 0.0

    def test_grows_with_n_trials(self):
        vals = [expected_max_sharpe_null(n, 0.5) for n in (2, 5, 20, 100, 500)]
        assert all(b > a for a, b in zip(vals, vals[1:]))

    def test_scales_with_trial_std(self):
        assert expected_max_sharpe_null(50, 1.0) == pytest.approx(
            2.0 * expected_max_sharpe_null(50, 0.5))

    def test_zero_std_is_zero(self):
        assert expected_max_sharpe_null(100, 0.0) == 0.0

    def test_rejects_zero_trials(self):
        with pytest.raises(ValueError):
            expected_max_sharpe_null(0, 0.5)


class TestSelectionAdjustedConfidence:
    def test_more_trials_lowers_confidence(self):
        # The core anti-selection-bias property: as the selection family grows,
        # the same observed Sharpe earns strictly LESS confidence.
        confs = [
            selection_adjusted_confidence(
                observed_sharpe=0.15, n_trials=n, trial_sharpe_std=0.1,
                n_obs=250, skew=0.0, excess_kurtosis=0.0)
            for n in (1, 5, 25, 100, 500)
        ]
        assert all(c is not None for c in confs)
        assert all(b < a for a, b in zip(confs, confs[1:]))

    def test_single_trial_equals_undeflated_psr(self):
        # N == 1 -> SR0 == 0 -> plain PSR against zero.
        adj = selection_adjusted_confidence(
            observed_sharpe=0.2, n_trials=1, trial_sharpe_std=0.1, n_obs=300)
        psr = deflated_sharpe_ratio(0.2, sr0=0.0, n_obs=300)
        assert adj == pytest.approx(psr)

    def test_below_min_obs_is_none(self):
        assert selection_adjusted_confidence(
            observed_sharpe=0.2, n_trials=10, trial_sharpe_std=0.1, n_obs=2) is None


class TestDeflatedSharpe:
    def test_below_minimum_obs_returns_none(self):
        assert deflated_sharpe_ratio(0.3, sr0=0.0, n_obs=2) is None
        assert deflated_sharpe_ratio(0.3, sr0=0.0, n_obs=3) is not None

    def test_higher_benchmark_lowers_confidence(self):
        hi = deflated_sharpe_ratio(0.2, sr0=0.05, n_obs=300)
        lo = deflated_sharpe_ratio(0.2, sr0=0.15, n_obs=300)
        assert lo < hi

    def test_probability_in_unit_interval(self):
        p = deflated_sharpe_ratio(0.1, sr0=0.02, n_obs=100, skew=-0.5,
                                  excess_kurtosis=3.0)
        assert 0.0 <= p <= 1.0

    def test_negative_skew_fat_tails_reduce_confidence(self):
        clean = deflated_sharpe_ratio(0.15, sr0=0.0, n_obs=200, skew=0.0,
                                      excess_kurtosis=0.0)
        ugly = deflated_sharpe_ratio(0.15, sr0=0.0, n_obs=200, skew=-1.5,
                                     excess_kurtosis=6.0)
        assert ugly < clean


# --------------------------------------------------------------------------- #
# Profit factor, concentration, outlier diagnostics
# --------------------------------------------------------------------------- #
class TestProfitFactor:
    def test_mixed(self):
        assert profit_factor([10.0, -5.0, 20.0, -5.0]) == pytest.approx(30.0 / 10.0)

    def test_no_losses_is_inf(self):
        assert profit_factor([1.0, 2.0, 3.0]) == float("inf")

    def test_no_trades_or_all_flat_is_zero(self):
        assert profit_factor([]) == 0.0
        assert profit_factor([0.0, 0.0]) == 0.0

    def test_all_losses_is_zero(self):
        assert profit_factor([-1.0, -2.0]) == 0.0

    def test_ignores_non_finite(self):
        assert profit_factor([10.0, float("nan"), -5.0]) == pytest.approx(2.0)


class TestProfitConcentration:
    def test_month_shares(self):
        buckets = {"2024-01": 100.0, "2024-02": 50.0, "2024-03": 50.0}
        res = profit_concentration(buckets)
        assert isinstance(res, ConcentrationResult)
        assert res.dominant_bucket == "2024-01"
        assert res.max_share == pytest.approx(0.5)

    def test_instrument_shares_ignore_losers_in_denominator(self):
        # Loss buckets do not dilute the positive-profit share.
        buckets = {265598: 80.0, 272093: 20.0, 4815747: -1000.0}
        res = profit_concentration(buckets)
        assert res.dominant_bucket == 265598
        assert res.max_share == pytest.approx(0.8)

    def test_no_positive_profit_is_none(self):
        res = profit_concentration({"a": -1.0, "b": 0.0})
        assert res.max_share is None and res.dominant_bucket is None


class TestRemoveTopOutlier:
    def test_basic_share(self):
        d = remove_top_outlier_diagnostic([10.0, 20.0, 70.0])
        assert isinstance(d, OutlierDiagnostic)
        assert d.total == pytest.approx(100.0)
        assert d.total_without_top == pytest.approx(30.0)
        assert d.top_share == pytest.approx(0.7)

    def test_removing_top_can_flip_to_negative(self):
        d = remove_top_outlier_diagnostic([-10.0, -10.0, 40.0])
        assert d.total == pytest.approx(20.0)
        assert d.total_without_top == pytest.approx(-20.0)
        assert d.top_share == pytest.approx(2.0)

    def test_non_positive_total_share_is_none(self):
        d = remove_top_outlier_diagnostic([-5.0, -3.0, 1.0])
        assert d.top_share is None

    def test_empty(self):
        d = remove_top_outlier_diagnostic([])
        assert d.total == 0.0 and d.top_share is None
