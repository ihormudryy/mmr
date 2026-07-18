"""The immutable ``paper-v1`` eligibility ruleset (P2 Task 6, design §8.3 / §8.4).

Every rule below is one condition from the ``PAPER_ELIGIBLE`` quantitative gate.
The thresholds are the design's exact numbers -- they are FIXED. Because
``PAPER_V1.source_digest`` is ``module_source_digest(this module)``, editing ANY
line of this file (a threshold, a rule, a comment) changes the ruleset digest and
therefore invalidates every attestation built on the old ruleset. That is the
point: the ruleset is versioned by its own content.

All rules are ``critical`` and fail closed: missing or non-finite evidence fails
the rule (see ``eligibility.py`` helpers). No qualitative judgment enters here.
"""
from __future__ import annotations

import sys

from trader.research.eligibility import (
    Ruleset,
    abs_at_most,
    at_least,
    at_most,
    greater_than,
    is_true,
    module_source_digest,
)

# Per-eligible-regime loss tolerance (§8.4: "no eligible regime breaches its
# predeclared regime-loss tolerance"). Both this bound and the observed worst
# regime loss are <= 0, so "worst loss >= tolerance" means "the worst regime's
# loss is no deeper than -10%". Fixed as part of the ruleset identity.
REGIME_LOSS_TOLERANCE = -0.10

# Scaled holdout drawdown must fit the 3% canary stop (§2 / §8.3).
CANARY_DRAWDOWN_LIMIT = 0.03

# Benchmark-relative drawdown ceiling (§8.4: strategy holdout drawdown no worse
# than 50% of the exposure/vol-matched SPY benchmark drawdown).
BENCHMARK_DRAWDOWN_RATIO_LIMIT = 0.50

_RULES = (
    # ---- §8.3 sample floors ------------------------------------------------ #
    at_least("min_round_trips",
             "At least 200 historical round trips.",
             "validation.n_round_trips", "n_round_trips", 200),
    at_least("min_instruments",
             "Research across at least 8 eligible instruments.",
             "validation.n_instruments", "n_instruments", 8),
    # ---- §8.3 expectancy under cost stress -------------------------------- #
    greater_than("expectancy_baseline_positive",
                 "Positive net expectancy at baseline costs.",
                 "validation.cost_stress[1.0]", "expectancy_bps_baseline", 0.0),
    greater_than("expectancy_stress_1_5x_positive",
                 "Positive net expectancy at 1.5x costs.",
                 "validation.cost_stress[1.5]", "expectancy_bps_1_5x", 0.0),
    at_least("expectancy_stress_2x_nonneg",
             "Non-negative net expectancy at 2x costs.",
             "validation.cost_stress[2.0]", "expectancy_bps_2x", 0.0),
    # ---- §8.3 statistical confidence -------------------------------------- #
    at_least("selection_adjusted_confidence",
             "Deflated / selection-adjusted Sharpe confidence >= 95%.",
             "statistics.selection_adjusted_confidence",
             "selection_adjusted_confidence", 0.95),
    greater_than("annualized_sharpe_lower_bound_positive",
                 "Annualized Sharpe bootstrap lower bound above zero.",
                 "statistics.annualized_sharpe_ci.low",
                 "annualized_sharpe_ci_low", 0.0),
    at_least("profit_factor_after_costs",
             "Profit factor of at least 1.20 after costs.",
             "statistics.profit_factor", "profit_factor", 1.20),
    # ---- §8.3 walk-forward + concentration -------------------------------- #
    at_least("walk_forward_positive_folds",
             "Positive results in at least 60% of walk-forward folds.",
             "validation.walk_forward_positive_fraction",
             "walk_forward_positive_fraction", 0.60),
    at_most("month_profit_concentration",
            "No single month supplies more than 35% of total profit.",
            "statistics.profit_concentration[month]",
            "max_month_profit_share", 0.35),
    at_most("instrument_profit_concentration",
            "No single instrument supplies more than 40% of total profit.",
            "statistics.profit_concentration[instrument]",
            "max_instrument_profit_share", 0.40),
    # ---- §8.3 drawdown + robustness --------------------------------------- #
    abs_at_most("holdout_drawdown_within_canary",
                "Holdout drawdown fits the 3% canary stop after scaling.",
                "validation.scaled_holdout_drawdown",
                "scaled_holdout_drawdown", CANARY_DRAWDOWN_LIMIT),
    is_true("parameter_neighborhood_robust",
            "Profitable behavior across a reasonable parameter neighborhood.",
            "validation.neighborhood_robust", "neighborhood_robust"),
    is_true("liquidity_capacity_envelope",
            "Expected order size within the approved liquidity/depth envelope.",
            "validation.order_within_envelope", "order_within_envelope"),
    is_true("deterministic_replay",
            "Deterministic replay produces identical signals and orders.",
            "validation.deterministic_replay_ok", "deterministic_replay_ok"),
    is_true("holdout_opened_once",
            "The final holdout was opened exactly once (registry fact).",
            "registry.holdout_opened_once", "holdout_opened_once"),
    # ---- §8.4 benchmark + regime gates ------------------------------------ #
    at_most("benchmark_relative_drawdown",
            "Holdout drawdown no worse than 50% of the vol-matched SPY benchmark.",
            "validation.benchmark.drawdown_ratio",
            "benchmark_drawdown_ratio", BENCHMARK_DRAWDOWN_RATIO_LIMIT),
    at_least("regime_positive_expectancy_fraction",
             "Positive net expectancy in at least 70% of eligible regime buckets.",
             "attribution.regime.positive_fraction_of_adequate",
             "eligible_regime_positive_fraction", 0.70),
    at_least("regime_loss_tolerance",
             "No eligible regime breaches its predeclared regime-loss tolerance.",
             "attribution.regime.worst_eligible_regime_loss",
             "worst_eligible_regime_loss", REGIME_LOSS_TOLERANCE),
    is_true("regime_transition_stability",
            "Regime transitions do not produce material instability.",
            "validation.regime_transitions_stable", "regime_transitions_stable"),
)

PAPER_V1: Ruleset = Ruleset(
    name="paper-v1",
    version="1",
    rules=_RULES,
    source_digest=module_source_digest(sys.modules[__name__]),
)
