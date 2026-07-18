"""P2 Task 6 -- the deterministic quantitative eligibility gate (design §8.3/§8.4).

This gate decides whether a sealed artifact may PAPER-trade. The tests pin the
things that make it trustworthy:

* one pass + one fail per approved gate, with exact boundary values;
* fail-closed behaviour: a missing (``None``) or non-finite (``nan``/``inf``)
  observation FAILS its rule, and the overall state falls back to ``CANDIDATE``;
* the decision is a PURE function of the quantitative evidence -- there is no
  qualitative parameter and no override path;
* the ruleset digest is immutable/content-addressed (threshold + source pinned);
* the persisted decision round-trips with EVERY rule result and ``record`` is
  idempotent with no update/delete API.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.eligibility import (
    STATE_CANDIDATE,
    STATE_PAPER_ELIGIBLE,
    EligibilityDecisionRepository,
    EligibilityEvidence,
    Ruleset,
    apply_eligibility_migrations,
    evaluate_eligibility,
    module_source_digest,
)
from trader.research.rulesets import paper_v1
from trader.research.rulesets.paper_v1 import PAPER_V1, REGIME_LOSS_TOLERANCE
from trader.research.schema import apply_research_migrations

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def passing_evidence(**over) -> EligibilityEvidence:
    """A fully-passing evidence object; flip one field per test to fail one gate."""
    kw = dict(
        n_round_trips=250,
        n_instruments=10,
        expectancy_bps_baseline=5.0,
        expectancy_bps_1_5x=3.0,
        expectancy_bps_2x=1.0,
        selection_adjusted_confidence=0.97,
        annualized_sharpe_ci_low=0.5,
        profit_factor=1.5,
        walk_forward_positive_fraction=0.7,
        max_month_profit_share=0.25,
        max_instrument_profit_share=0.30,
        scaled_holdout_drawdown=-0.02,
        neighborhood_robust=True,
        order_within_envelope=True,
        deterministic_replay_ok=True,
        holdout_opened_once=True,
        benchmark_drawdown_ratio=0.40,
        eligible_regime_positive_fraction=0.80,
        worst_eligible_regime_loss=-0.05,
        regime_transitions_stable=True,
        # reported (non-gating) context
        benchmark_return=0.10,
        benchmark_downside_deviation=0.08,
        benchmark_recovery_time=12,
        strategy_time_in_market=0.35,
        capacity_estimate=2_000_000.0,
    )
    kw.update(over)
    return EligibilityEvidence(**kw)


def _decision(**over):
    return evaluate_eligibility(PAPER_V1, passing_evidence(**over))


def _result(decision, code):
    for r in decision.results:
        if r.code == code:
            return r
    raise AssertionError(f"rule {code!r} not in results")


@pytest.fixture
def repo(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    migrator = SchemaMigrator(db)
    apply_research_migrations(migrator)  # bootstraps 1-2, 3-6, and 7
    return EligibilityDecisionRepository(db)


# --------------------------------------------------------------------------- #
# The fully-passing baseline
# --------------------------------------------------------------------------- #
class TestFullyPassing:
    def test_all_rules_pass_yields_paper_eligible(self):
        d = _decision()
        assert d.passed is True
        assert d.state == STATE_PAPER_ELIGIBLE
        assert d.failures == ()

    def test_every_rule_present_in_results(self):
        d = _decision()
        assert len(d.results) == len(PAPER_V1.rules)
        assert {r.code for r in d.results} == {r.code for r in PAPER_V1.rules}
        assert all(r.passed for r in d.results)

    def test_evidence_refs_cover_every_rule(self):
        d = _decision()
        assert len(d.evidence_refs) == len(PAPER_V1.rules)
        assert all(ref for ref in d.evidence_refs)


# --------------------------------------------------------------------------- #
# One pass + one fail per approved gate (with exact boundaries)
# --------------------------------------------------------------------------- #
# Each entry: (code, field, passing_boundary, failing_value)
_GATES = [
    ("min_round_trips", "n_round_trips", 200, 199),
    ("min_instruments", "n_instruments", 8, 7),
    ("expectancy_baseline_positive", "expectancy_bps_baseline", 0.01, 0.0),
    ("expectancy_stress_1_5x_positive", "expectancy_bps_1_5x", 0.01, 0.0),
    ("expectancy_stress_2x_nonneg", "expectancy_bps_2x", 0.0, -0.01),
    ("selection_adjusted_confidence", "selection_adjusted_confidence", 0.95, 0.9499),
    ("annualized_sharpe_lower_bound_positive", "annualized_sharpe_ci_low", 0.0001, 0.0),
    ("profit_factor_after_costs", "profit_factor", 1.20, 1.19),
    ("walk_forward_positive_folds", "walk_forward_positive_fraction", 0.60, 0.59),
    ("month_profit_concentration", "max_month_profit_share", 0.35, 0.3501),
    ("instrument_profit_concentration", "max_instrument_profit_share", 0.40, 0.4001),
    ("holdout_drawdown_within_canary", "scaled_holdout_drawdown", -0.03, -0.0301),
    ("benchmark_relative_drawdown", "benchmark_drawdown_ratio", 0.50, 0.51),
    ("regime_positive_expectancy_fraction", "eligible_regime_positive_fraction", 0.70, 0.69),
    ("regime_loss_tolerance", "worst_eligible_regime_loss", REGIME_LOSS_TOLERANCE, -0.1001),
]

_BOOL_GATES = [
    ("parameter_neighborhood_robust", "neighborhood_robust"),
    ("liquidity_capacity_envelope", "order_within_envelope"),
    ("deterministic_replay", "deterministic_replay_ok"),
    ("holdout_opened_once", "holdout_opened_once"),
    ("regime_transition_stability", "regime_transitions_stable"),
]


@pytest.mark.parametrize("code,field_name,boundary,fail", _GATES)
def test_gate_boundary_passes(code, field_name, boundary, fail):
    d = _decision(**{field_name: boundary})
    assert _result(d, code).passed is True, f"{code} should pass at boundary {boundary}"
    # the boundary value alone must not disturb any other gate
    assert d.passed is True
    assert d.state == STATE_PAPER_ELIGIBLE


@pytest.mark.parametrize("code,field_name,boundary,fail", _GATES)
def test_gate_just_over_boundary_fails(code, field_name, boundary, fail):
    d = _decision(**{field_name: fail})
    r = _result(d, code)
    assert r.passed is False, f"{code} should fail at {fail}"
    assert r.observed == fail
    assert d.passed is False
    assert d.state == STATE_CANDIDATE
    assert code in {f.code for f in d.failures}
    # exactly the flipped gate should fail
    assert {f.code for f in d.failures} == {code}


@pytest.mark.parametrize("code,field_name", _BOOL_GATES)
def test_bool_gate_true_passes(code, field_name):
    d = _decision(**{field_name: True})
    assert _result(d, code).passed is True
    assert d.passed is True


@pytest.mark.parametrize("code,field_name", _BOOL_GATES)
def test_bool_gate_false_fails(code, field_name):
    d = _decision(**{field_name: False})
    r = _result(d, code)
    assert r.passed is False
    assert r.observed is False
    assert d.state == STATE_CANDIDATE
    assert {f.code for f in d.failures} == {code}


# --------------------------------------------------------------------------- #
# Fail closed: missing + non-finite critical observations
# --------------------------------------------------------------------------- #
_ALL_GATE_FIELDS = [(c, f) for (c, f, _, _) in _GATES] + _BOOL_GATES


@pytest.mark.parametrize("code,field_name", _ALL_GATE_FIELDS)
def test_missing_evidence_fails_closed(code, field_name):
    d = _decision(**{field_name: None})
    r = _result(d, code)
    assert r.passed is False, f"{code} must FAIL when its evidence is None"
    assert r.detail == "missing/non-finite evidence"
    assert d.passed is False
    assert d.state == STATE_CANDIDATE


_NUMERIC_GATE_FIELDS = [(c, f) for (c, f, _, _) in _GATES]


@pytest.mark.parametrize("code,field_name", _NUMERIC_GATE_FIELDS)
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_evidence_fails_closed(code, field_name, bad):
    d = _decision(**{field_name: bad})
    r = _result(d, code)
    assert r.passed is False, f"{code} must FAIL when its evidence is {bad}"
    assert r.detail == "missing/non-finite evidence"
    assert d.state == STATE_CANDIDATE


def test_all_none_evidence_is_candidate_with_every_rule_failing():
    d = evaluate_eligibility(PAPER_V1, EligibilityEvidence())
    assert d.passed is False
    assert d.state == STATE_CANDIDATE
    assert len(d.failures) == len(PAPER_V1.rules)
    assert all(f.detail == "missing/non-finite evidence" for f in d.failures)


def test_missing_benchmark_and_regime_evidence_fail_closed():
    # §8.4 gates specifically must fail closed on absent benchmark/regime evidence
    d = _decision(benchmark_drawdown_ratio=None,
                  eligible_regime_positive_fraction=None,
                  worst_eligible_regime_loss=None,
                  regime_transitions_stable=None)
    failing = {f.code for f in d.failures}
    assert failing == {
        "benchmark_relative_drawdown",
        "regime_positive_expectancy_fraction",
        "regime_loss_tolerance",
        "regime_transition_stability",
    }
    assert d.state == STATE_CANDIDATE


# --------------------------------------------------------------------------- #
# No override: pure function of the evidence
# --------------------------------------------------------------------------- #
class TestPureFunction:
    def test_same_evidence_same_decision_digest(self):
        d1 = evaluate_eligibility(PAPER_V1, passing_evidence())
        d2 = evaluate_eligibility(PAPER_V1, passing_evidence())
        assert d1.digest == d2.digest
        assert d1.state == d2.state == STATE_PAPER_ELIGIBLE

    def test_different_evidence_different_digest(self):
        d1 = evaluate_eligibility(PAPER_V1, passing_evidence())
        d2 = evaluate_eligibility(PAPER_V1, passing_evidence(profit_factor=1.19))
        assert d1.digest != d2.digest

    def test_no_qualitative_parameter_on_evaluate(self):
        import inspect
        sig = inspect.signature(evaluate_eligibility)
        assert list(sig.parameters) == ["ruleset", "evidence"]

    def test_evidence_has_no_review_or_override_field(self):
        names = {f.name for f in dataclasses.fields(EligibilityEvidence)}
        for banned in ("review", "override", "approved", "operator", "waiver",
                       "reviewer", "rationale", "attestation"):
            assert not any(banned in n for n in names), f"{banned!r} leaked into evidence"

    def test_failed_gate_cannot_be_flipped_by_extra_context(self):
        # A failing quantitative gate stays failed no matter what non-gating
        # reported context is supplied.
        base = _decision(profit_factor=1.19)
        with_ctx = _decision(profit_factor=1.19, capacity_estimate=9e9,
                             benchmark_return=99.0, strategy_time_in_market=1.0)
        assert base.state == with_ctx.state == STATE_CANDIDATE
        assert base.digest == with_ctx.digest  # context is not part of the decision


# --------------------------------------------------------------------------- #
# Ruleset immutability + digest
# --------------------------------------------------------------------------- #
class TestRulesetDigest:
    def test_digest_stable_across_calls(self):
        assert PAPER_V1.digest == PAPER_V1.digest

    def test_source_digest_pins_module_identity(self):
        assert PAPER_V1.source_digest == module_source_digest(paper_v1)

    def test_changing_a_threshold_changes_digest(self):
        rules = list(PAPER_V1.rules)
        # rebuild the profit-factor rule at a different threshold
        from trader.research.eligibility import at_least
        for i, r in enumerate(rules):
            if r.code == "profit_factor_after_costs":
                rules[i] = at_least(r.code, r.description, r.evidence_ref,
                                    "profit_factor", 1.30)
        modified = Ruleset(name=PAPER_V1.name, version=PAPER_V1.version,
                           rules=tuple(rules), source_digest=PAPER_V1.source_digest)
        assert modified.digest != PAPER_V1.digest

    def test_digest_depends_on_source_digest(self):
        other = Ruleset(name=PAPER_V1.name, version=PAPER_V1.version,
                        rules=PAPER_V1.rules, source_digest="deadbeef")
        assert other.digest != PAPER_V1.digest

    def test_evaluate_callable_excluded_from_rule_equality(self):
        # two rules with identical config but different closures compare equal
        from trader.research.eligibility import at_least
        a = at_least("x", "d", "ref", "profit_factor", 1.0)
        b = at_least("x", "d", "ref", "profit_factor", 1.0)
        assert a == b

    def test_duplicate_rule_codes_rejected(self):
        from trader.research.eligibility import at_least
        dup = at_least("min_round_trips", "d", "ref", "n_round_trips", 1)
        with pytest.raises(ValueError, match="duplicate rule codes"):
            Ruleset(name="x", version="1", rules=(dup, dup), source_digest="d")


# --------------------------------------------------------------------------- #
# Exactly the §8.3/§8.4 codes + thresholds are encoded
# --------------------------------------------------------------------------- #
def test_encoded_rules_match_design():
    by_code = {r.code: r for r in PAPER_V1.rules}
    expected = {
        "min_round_trips": 200,
        "min_instruments": 8,
        "expectancy_baseline_positive": 0.0,
        "expectancy_stress_1_5x_positive": 0.0,
        "expectancy_stress_2x_nonneg": 0.0,
        "selection_adjusted_confidence": 0.95,
        "annualized_sharpe_lower_bound_positive": 0.0,
        "profit_factor_after_costs": 1.20,
        "walk_forward_positive_folds": 0.60,
        "month_profit_concentration": 0.35,
        "instrument_profit_concentration": 0.40,
        "holdout_drawdown_within_canary": 0.03,
        "parameter_neighborhood_robust": True,
        "liquidity_capacity_envelope": True,
        "deterministic_replay": True,
        "holdout_opened_once": True,
        "benchmark_relative_drawdown": 0.50,
        "regime_positive_expectancy_fraction": 0.70,
        "regime_loss_tolerance": REGIME_LOSS_TOLERANCE,
        "regime_transition_stability": True,
    }
    assert set(by_code) == set(expected), "ruleset rule set drifted from design"
    for code, threshold in expected.items():
        assert by_code[code].threshold == threshold, code
    assert all(r.critical for r in PAPER_V1.rules)
    assert PAPER_V1.name == "paper-v1"
    assert PAPER_V1.version == "1"
    assert REGIME_LOSS_TOLERANCE == -0.10


# --------------------------------------------------------------------------- #
# Persistence round-trip + idempotency
# --------------------------------------------------------------------------- #
class TestPersistence:
    def test_record_then_get_round_trips_every_rule(self, repo):
        # a mixed decision: one gate fails so both passing + failing rows persist
        decision = _decision(profit_factor=1.19)
        assert decision.state == STATE_CANDIDATE
        digest = repo.record(decision, artifact_id="artifact-abc", recorded_at=T0)
        assert digest == decision.digest

        got = repo.get(digest)
        assert got is not None
        assert got.digest == decision.digest
        assert got.state == decision.state == STATE_CANDIDATE
        assert got.passed is False
        # every rule result present (passing + failing)
        assert len(got.results) == len(PAPER_V1.rules)
        assert {r.code for r in got.results} == {r.code for r in PAPER_V1.rules}
        got_by_code = {r.code: r for r in got.results}
        orig_by_code = {r.code: r for r in decision.results}
        for code, orig in orig_by_code.items():
            assert got_by_code[code].passed == orig.passed
            assert got_by_code[code].observed == orig.observed
            assert got_by_code[code].threshold == orig.threshold
            assert got_by_code[code].evidence_ref == orig.evidence_ref
        assert {f.code for f in got.failures} == {"profit_factor_after_costs"}

    def test_record_is_idempotent(self, repo):
        decision = _decision()
        d1 = repo.record(decision, artifact_id="a1", recorded_at=T0)
        d2 = repo.record(decision, artifact_id="a1", recorded_at=T0)
        assert d1 == d2 == decision.digest
        # exactly one decision row + one row per rule
        got = repo.get(decision.digest)
        assert len(got.results) == len(PAPER_V1.rules)

    def test_passing_decision_round_trips(self, repo):
        decision = _decision()
        assert decision.state == STATE_PAPER_ELIGIBLE
        repo.record(decision, artifact_id="a2", recorded_at=T0)
        got = repo.get(decision.digest)
        assert got.state == STATE_PAPER_ELIGIBLE
        assert got.passed is True
        assert got.failures == ()

    def test_get_unknown_digest_returns_none(self, repo):
        assert repo.get("does-not-exist") is None

    def test_repository_exposes_no_update_or_delete(self):
        for banned in ("update", "delete", "remove", "unseal", "edit", "set_state"):
            assert not hasattr(EligibilityDecisionRepository, banned), banned


def test_apply_eligibility_migrations_is_idempotent(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "idem.duckdb"))
    migrator = SchemaMigrator(db)
    apply_eligibility_migrations(migrator)
    # second application is a no-op, not an error
    apply_eligibility_migrations(migrator)
    rows = db.execute("SELECT COUNT(*) FROM eligibility_decisions", fetch="one")
    assert rows[0] == 0


def test_non_finite_observed_still_digests_and_persists(repo):
    # a nan observation must not break the decision digest or persistence
    decision = _decision(profit_factor=float("nan"))
    digest = decision.digest  # must not raise despite nan observed
    assert isinstance(digest, str) and len(digest) == 64
    repo.record(decision, artifact_id="a-nan", recorded_at=T0)
    got = repo.get(digest)
    assert got is not None
    r = {x.code: x for x in got.results}["profit_factor_after_costs"]
    assert r.passed is False
    assert math.isnan(r.observed)
