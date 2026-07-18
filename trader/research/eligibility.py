"""Deterministic quantitative eligibility gate (P2 Task 6, design §8.3 / §8.4).

This is the gate that decides whether a sealed strategy artifact may PAPER-trade.
It is the enforcement point for the structural invariant of §8.5 -- *"the
qualitative review cannot override a failed quantitative gate"*: ``evaluate_eligibility``
takes ONLY quantitative ``EligibilityEvidence``. There is no argument, field, or
code path by which a human note can flip a failed rule to pass.

Design principles, non-negotiable for a trading system:

* **Fail closed.** Every rule reads one observed value from the evidence. A
  MISSING (``None``) or NON-FINITE (``nan`` / ``inf``, or the wrong type where a
  number is required) value FAILS the rule -- absent evidence never passes. This
  is implemented once, in the helper evaluators, so no rule can forget it.
* **Deterministic + offline + pure.** ``evaluate_eligibility(ruleset, evidence)``
  is a pure function: no wall-clock, no RNG, no operational DB. Same evidence =>
  same decision => same decision digest.
* **Immutable versioned ruleset.** A ``Ruleset``'s ``digest`` is computed from
  the exact rule configuration AND the source identity of the ruleset module
  (``source_digest``), so editing a threshold OR editing the ruleset code changes
  the digest. ``paper_v1`` pins its own code identity via ``module_source_digest``.
* **Complete decision record.** Persistence stores EVERY rule result -- passing
  and failing -- never just the failures.

The evidence fields are DERIVED from the Task-5 statistics/validation/attribution
outputs (``annualized_sharpe_ci().low``, ``selection_adjusted_confidence``,
``profit_factor``, ``profit_concentration``, ``BenchmarkComparison.drawdown_ratio``,
``AttributionTable.positive_fraction_of_adequate``, and the holdout-opened-once
fact from the registry). This module does not compute them; it only judges them.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from trader.data.schema_migrations import SchemaMigrator
from trader.research.canonical import sha256_digest

RULESET_DIGEST_PREFIX = "eligibility_ruleset"
DECISION_DIGEST_PREFIX = "eligibility_decision"

# Eligibility states this gate may emit (design §4.3). A passing artifact is
# PAPER_ELIGIBLE; anything else is CANDIDATE. The downstream lifecycle states
# (CANARY_ELIGIBLE / SUSPENDED / RETIRED) belong to other services and are NOT
# settable here -- this gate only certifies "may it PAPER-trade?".
STATE_PAPER_ELIGIBLE = "PAPER_ELIGIBLE"
STATE_CANDIDATE = "CANDIDATE"

RESEARCH_MIGRATION_ELIGIBILITY = 7


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EligibilityEvidence:
    """The complete quantitative evidence a decision is a pure function of.

    Every field defaults to ``None`` so a test (or a partial pipeline) can
    construct evidence for a single rule; a ``None`` gating field fails its rule
    closed. The gating fields map 1:1 onto the §8.3/§8.4 conditions; the trailing
    fields are non-gating REPORTED context (§8.4 requires the benchmark's return,
    downside deviation, recovery time, and time-in-market accompany the ratio so
    trivially-low exposure cannot game the comparison).
    """

    # §8.3 quantitative gate
    n_round_trips: Optional[int] = None
    n_instruments: Optional[int] = None
    expectancy_bps_baseline: Optional[float] = None
    expectancy_bps_1_5x: Optional[float] = None
    expectancy_bps_2x: Optional[float] = None
    selection_adjusted_confidence: Optional[float] = None
    annualized_sharpe_ci_low: Optional[float] = None
    profit_factor: Optional[float] = None
    walk_forward_positive_fraction: Optional[float] = None
    max_month_profit_share: Optional[float] = None
    max_instrument_profit_share: Optional[float] = None
    scaled_holdout_drawdown: Optional[float] = None          # <= 0
    neighborhood_robust: Optional[bool] = None
    order_within_envelope: Optional[bool] = None
    deterministic_replay_ok: Optional[bool] = None
    holdout_opened_once: Optional[bool] = None               # from the registry
    # §8.4 benchmark + regime gate
    benchmark_drawdown_ratio: Optional[float] = None
    eligible_regime_positive_fraction: Optional[float] = None
    worst_eligible_regime_loss: Optional[float] = None       # <= 0
    regime_transitions_stable: Optional[bool] = None
    # Non-gating reported context (§8.4)
    benchmark_return: Optional[float] = None
    benchmark_downside_deviation: Optional[float] = None
    benchmark_recovery_time: Optional[int] = None
    strategy_time_in_market: Optional[float] = None
    capacity_estimate: Optional[float] = None


# --------------------------------------------------------------------------- #
# Rule + result
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RuleResult:
    """The outcome of evaluating one rule against evidence.

    ``observed`` is the raw evidence value the rule read (which may be ``None`` or
    non-finite when the rule failed closed); ``threshold`` is the rule's fixed
    bound; ``evidence_ref`` points back at the Task-5 output that produced the
    value; ``detail`` explains a failure.
    """

    code: str
    passed: bool
    observed: Any
    threshold: Any
    evidence_ref: str
    detail: str = ""


@dataclass(frozen=True)
class Rule:
    """One immutable eligibility rule.

    The DIGESTIBLE configuration is ``(code, description, threshold, evidence_ref,
    critical)`` -- these define the rule's identity for the ruleset digest. The
    ``evaluate`` callable is EXCLUDED from equality and the digest (``compare=False``);
    the code's identity is captured by the ruleset's ``source_digest``, not by
    hashing a closure (closures are not stably hashable across processes).
    """

    code: str
    description: str
    threshold: Any
    evidence_ref: str
    evaluate: Callable[[EligibilityEvidence], RuleResult] = field(
        compare=False, repr=False)
    critical: bool = True

    def _config_body(self) -> dict:
        return {
            "code": self.code,
            "description": self.description,
            "threshold": self.threshold,
            "evidence_ref": self.evidence_ref,
            "critical": self.critical,
        }


# --------------------------------------------------------------------------- #
# Fail-closed helper evaluators
# --------------------------------------------------------------------------- #
_MISSING_DETAIL = "missing/non-finite evidence"


def _is_bad_number(value: Any) -> bool:
    """True when ``value`` is not usable numeric evidence: ``None``, a bool where
    a number is expected, a non-finite float, or any non-numeric type. Fail
    closed -- an unusable value never passes a numeric rule."""
    if value is None:
        return True
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return not math.isfinite(float(value))
    return True


def _numeric_rule(code: str, description: str, evidence_ref: str, field_name: str,
                  threshold: float, op: Callable[[float, float], bool],
                  op_label: str, *, transform: Callable[[float], float] = lambda x: x,
                  critical: bool = True) -> Rule:
    def _eval(evidence: EligibilityEvidence) -> RuleResult:
        observed = getattr(evidence, field_name)
        if _is_bad_number(observed):
            return RuleResult(code=code, passed=False, observed=observed,
                              threshold=threshold, evidence_ref=evidence_ref,
                              detail=_MISSING_DETAIL)
        passed = bool(op(transform(float(observed)), threshold))
        detail = "" if passed else f"{op_label} failed: observed={observed}, threshold={threshold}"
        return RuleResult(code=code, passed=passed, observed=observed,
                          threshold=threshold, evidence_ref=evidence_ref, detail=detail)

    return Rule(code=code, description=description, threshold=threshold,
                evidence_ref=evidence_ref, evaluate=_eval, critical=critical)


def at_least(code: str, description: str, evidence_ref: str, field_name: str,
             threshold: float, *, critical: bool = True) -> Rule:
    """``observed >= threshold`` (fail closed on missing/non-finite)."""
    return _numeric_rule(code, description, evidence_ref, field_name, threshold,
                         lambda o, t: o >= t, ">=", critical=critical)


def at_most(code: str, description: str, evidence_ref: str, field_name: str,
            threshold: float, *, critical: bool = True) -> Rule:
    """``observed <= threshold`` (fail closed on missing/non-finite)."""
    return _numeric_rule(code, description, evidence_ref, field_name, threshold,
                         lambda o, t: o <= t, "<=", critical=critical)


def greater_than(code: str, description: str, evidence_ref: str, field_name: str,
                 threshold: float, *, critical: bool = True) -> Rule:
    """``observed > threshold`` (fail closed on missing/non-finite)."""
    return _numeric_rule(code, description, evidence_ref, field_name, threshold,
                         lambda o, t: o > t, ">", critical=critical)


def abs_at_most(code: str, description: str, evidence_ref: str, field_name: str,
                threshold: float, *, critical: bool = True) -> Rule:
    """``abs(observed) <= threshold`` (fail closed on missing/non-finite).

    Used for the scaled holdout drawdown, which is reported as a non-positive
    number; the gate cares about its magnitude against the canary stop.
    """
    return _numeric_rule(code, description, evidence_ref, field_name, threshold,
                         lambda o, t: o <= t, "|.| <=", transform=abs,
                         critical=critical)


def is_true(code: str, description: str, evidence_ref: str, field_name: str, *,
            critical: bool = True) -> Rule:
    """Passes only when the observed value is exactly ``True`` (fail closed on
    ``None``; any non-``True`` value fails)."""
    def _eval(evidence: EligibilityEvidence) -> RuleResult:
        observed = getattr(evidence, field_name)
        if observed is None:
            return RuleResult(code=code, passed=False, observed=observed,
                              threshold=True, evidence_ref=evidence_ref,
                              detail=_MISSING_DETAIL)
        passed = observed is True
        detail = "" if passed else f"expected True, observed={observed}"
        return RuleResult(code=code, passed=passed, observed=observed,
                          threshold=True, evidence_ref=evidence_ref, detail=detail)

    return Rule(code=code, description=description, threshold=True,
                evidence_ref=evidence_ref, evaluate=_eval, critical=critical)


# --------------------------------------------------------------------------- #
# Ruleset
# --------------------------------------------------------------------------- #
def module_source_digest(module: Any) -> str:
    """SHA-256 hex digest of a module's source file (via ``inspect.getsource``).

    A ruleset pins this as its ``source_digest`` so that editing the ruleset code
    -- even in a way that does not change a threshold literal -- changes the
    ruleset digest and therefore invalidates any attestation built on it.
    """
    source = inspect.getsource(module)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Ruleset:
    """An immutable, versioned collection of eligibility rules.

    ``digest`` is content-addressed over the ruleset identity (name + version +
    ``source_digest``) and every rule's digestible configuration. It is stable
    across calls and changes iff a threshold, a rule's config, or the source
    identity changes.
    """

    name: str
    version: str
    rules: tuple[Rule, ...]
    source_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "rules", tuple(self.rules))
        codes = [r.code for r in self.rules]
        dupes = sorted({c for c in codes if codes.count(c) > 1})
        if dupes:
            raise ValueError(f"duplicate rule codes in ruleset {self.name!r}: {dupes}")

    def _body(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "source_digest": self.source_digest,
            "rules": [r._config_body() for r in self.rules],
        }

    @property
    def digest(self) -> str:
        return sha256_digest(RULESET_DIGEST_PREFIX, self._body())


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #
def _encode_value(value: Any) -> str:
    """Deterministic JSON text for an observed/threshold value.

    Unlike ``canonical_json_bytes`` this tolerates non-finite floats (evidence
    may legitimately be ``nan``/``inf`` on a fail-closed rule) by emitting the
    ``NaN``/``Infinity`` tokens, which ``json.loads`` reverses -- so the value
    round-trips through persistence and the decision digest stays deterministic.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=True)


def _decode_value(text: str) -> Any:
    return json.loads(text)


@dataclass(frozen=True)
class EligibilityDecision:
    """The complete outcome of running a ruleset over evidence.

    Carries EVERY rule result (passing and failing). ``digest`` is a pure
    function of the ruleset digest, the state, and the sorted results, so equal
    evidence under an equal ruleset yields an equal decision digest.
    """

    state: str
    ruleset_name: str
    ruleset_version: str
    ruleset_digest: str
    passed: bool
    results: tuple[RuleResult, ...]

    @property
    def failures(self) -> tuple[RuleResult, ...]:
        return tuple(r for r in self.results if not r.passed)

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(r.evidence_ref for r in self.results)

    def _digest_body(self) -> dict:
        return {
            "ruleset_digest": self.ruleset_digest,
            "state": self.state,
            "results": [
                {
                    "code": r.code,
                    "passed": r.passed,
                    "observed": _encode_value(r.observed),
                    "threshold": _encode_value(r.threshold),
                    "evidence_ref": r.evidence_ref,
                    "detail": r.detail,
                }
                for r in sorted(self.results, key=lambda r: r.code)
            ],
        }

    @property
    def digest(self) -> str:
        return sha256_digest(DECISION_DIGEST_PREFIX, self._digest_body())


def evaluate_eligibility(ruleset: Ruleset,
                         evidence: EligibilityEvidence) -> EligibilityDecision:
    """Run EVERY rule and produce the decision.

    Pure function of ``(ruleset, evidence)`` -- no wall-clock, no RNG, no I/O, and
    NO qualitative input. ``passed`` is the conjunction of all rule results;
    ``state`` is ``PAPER_ELIGIBLE`` iff every rule passed, else ``CANDIDATE``.
    Both passing and failing results are retained.
    """
    results = tuple(rule.evaluate(evidence) for rule in ruleset.rules)
    passed = all(r.passed for r in results)
    state = STATE_PAPER_ELIGIBLE if passed else STATE_CANDIDATE
    return EligibilityDecision(
        state=state, ruleset_name=ruleset.name, ruleset_version=ruleset.version,
        ruleset_digest=ruleset.digest, passed=passed, results=results)


# --------------------------------------------------------------------------- #
# Persistence (research migration 7)
# --------------------------------------------------------------------------- #
_ELIGIBILITY_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS eligibility_decisions (
        decision_digest VARCHAR PRIMARY KEY,
        artifact_id VARCHAR NOT NULL,
        ruleset_name VARCHAR NOT NULL,
        ruleset_version VARCHAR NOT NULL,
        ruleset_digest VARCHAR NOT NULL,
        state VARCHAR NOT NULL,
        passed BOOLEAN NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS eligibility_rule_results (
        decision_digest VARCHAR NOT NULL,
        code VARCHAR NOT NULL,
        passed BOOLEAN NOT NULL,
        observed VARCHAR NOT NULL,
        threshold VARCHAR NOT NULL,
        evidence_ref VARCHAR NOT NULL,
        detail VARCHAR NOT NULL DEFAULT '',
        PRIMARY KEY (decision_digest, code)
    )
    """,
)


def apply_eligibility_migrations(migrator: SchemaMigrator) -> None:
    """Research DB migration 7 (idempotent): eligibility decision + rule-result
    tables in the SEPARATE offline research DuckDB."""
    migrator.apply(version=RESEARCH_MIGRATION_ELIGIBILITY,
                   name="research_eligibility_decisions",
                   statements=list(_ELIGIBILITY_STATEMENTS))


class EligibilityDecisionRepository:
    """Append-only, digest-keyed store for eligibility decisions.

    Records the COMPLETE decision -- every rule result, passing and failing.
    ``record`` is idempotent by ``decision_digest``. There is deliberately no
    update/delete API: a decision is a content-addressed fact.
    """

    def __init__(self, db: Any):
        self._db = db

    def record(self, decision: EligibilityDecision, *, artifact_id: str,
               recorded_at: Any) -> str:
        digest = decision.digest

        def _tx(conn):
            if conn.execute(
                    "SELECT 1 FROM eligibility_decisions WHERE decision_digest = ?",
                    [digest]).fetchone() is not None:
                return digest  # idempotent: content-addressed, already recorded
            conn.execute(
                "INSERT INTO eligibility_decisions (decision_digest, artifact_id, "
                "ruleset_name, ruleset_version, ruleset_digest, state, passed, "
                "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [digest, artifact_id, decision.ruleset_name, decision.ruleset_version,
                 decision.ruleset_digest, decision.state, decision.passed, recorded_at])
            for r in decision.results:
                conn.execute(
                    "INSERT INTO eligibility_rule_results (decision_digest, code, "
                    "passed, observed, threshold, evidence_ref, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [digest, r.code, r.passed, _encode_value(r.observed),
                     _encode_value(r.threshold), r.evidence_ref, r.detail])
            return digest

        return self._db.transaction(_tx)

    def get(self, decision_digest: str) -> Optional[EligibilityDecision]:
        def _tx(conn):
            row = conn.execute(
                "SELECT ruleset_name, ruleset_version, ruleset_digest, state, passed "
                "FROM eligibility_decisions WHERE decision_digest = ?",
                [decision_digest]).fetchone()
            if row is None:
                return None
            results = tuple(
                RuleResult(code=r[0], passed=bool(r[1]), observed=_decode_value(r[2]),
                           threshold=_decode_value(r[3]), evidence_ref=r[4], detail=r[5])
                for r in conn.execute(
                    "SELECT code, passed, observed, threshold, evidence_ref, detail "
                    "FROM eligibility_rule_results WHERE decision_digest = ? "
                    "ORDER BY code", [decision_digest]).fetchall())
            return EligibilityDecision(
                state=row[3], ruleset_name=row[0], ruleset_version=row[1],
                ruleset_digest=row[2], passed=bool(row[4]), results=results)

        return self._db.transaction(_tx)
