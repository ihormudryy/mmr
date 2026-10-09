"""Small stand-ins for a cohort result and an evaluation case, for the research service tests."""
from types import SimpleNamespace

from trader.research.cohort_evaluation import CohortResult, PointVerdict
from trader.research.evaluation import HoldoutOutcome, PointResult
from trader.research.evidence import GateOutcome
from trader.research.rulesets.paper_v1 import PAPER_V1


def rule_results(passed=True, observed=12.5):
    return tuple(SimpleNamespace(code=rule.code, passed=passed, observed=observed) for rule in PAPER_V1.rules)


def verdict(index, params, trial_id, *, passed, statistic, observed=12.5):
    evidence = SimpleNamespace(expectancy_bps_baseline=12.5, expectancy_bps_1_5x=9.0, expectancy_bps_2x=5.5,
                               selection_adjusted_confidence=statistic)
    decision = SimpleNamespace(results=rule_results(passed, observed), digest="e" * 64, state="CANDIDATE",
                               ruleset_digest=PAPER_V1.digest)
    return PointVerdict(index, PointResult(dict(params), trial_id, {}), (PointResult({}, f"{trial_id}-n", {}),),
                        GateOutcome(passed, () if passed else ("expectancy_2x_positive",), ()), decision, evidence,
                        None)


def pre_holdout_result(params=({"ENTRY_MINUTE": 600},)):
    verdicts = tuple(verdict(i, p, f"t{i}", passed=False, statistic=0.4, observed=float("nan"))
                     for i, p in enumerate(params))
    return CohortResult("f" * 64, "pre_holdout", verdicts, 3, None, 0, None, [], 0)


def complete_result(params=({"ENTRY_MINUTE": 600},), *, holdout_passed=True, artifact_id="a" * 64, selected=0):
    """Every point passes its pre-holdout gate; the holdout opened for ``params[selected]``."""
    verdicts = tuple(verdict(i, p, f"t{i}", passed=True, statistic=0.9 if i == selected else 0.5)
                     for i, p in enumerate(params))
    decision = SimpleNamespace(results=rule_results(holdout_passed), digest="d" * 64,
                               state="PAPER_ELIGIBLE" if holdout_passed else "CANDIDATE",
                               ruleset_digest=PAPER_V1.digest)
    outcome = HoldoutOutcome(artifact_id, holdout_passed, verdicts[selected].evidence, decision,
                             "d" * 64 if holdout_passed else None,
                             {"start": "2024-03-22", "end": "2024-03-28", "passed": holdout_passed, "detail": "x"})
    stage = "complete" if holdout_passed else "holdout_failed"
    return CohortResult("f" * 64, stage, verdicts, 3, verdicts[selected], selected, outcome, ["2024-02-20"], 1)
