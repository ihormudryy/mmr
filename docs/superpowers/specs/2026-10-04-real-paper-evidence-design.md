# Real Paper Evidence (Phase A) Design

**Date:** 2026-10-04
**Status:** design approved in chat; spec awaiting review.
**Depends on:** dollar-weighted `expectancy_bps` (`fix/expectancy-bps`), realistic execution costs (`fix/execution-costs`). This branch stacks both.
**Related:** the July research-bundle and dashboard paper-automation activation designs (`2026-07-18-p2-research-bundle-design.md`, `2026-07-20-dashboard-paper-automation-activation-design.md`), removed from master in `f0c6527`; read them in git history.

## Problem

Paper automation is armed with a signed `PAPER_ELIGIBLE` bundle that contains made-up numbers. Both activation paths in `trader/automation/paper_activation.py` call `export_fixture_paper_eligible_bundle` (`trader/automation/paper_materials.py`). Its `_evidence()` is hard-coded: 250 round trips, profit factor 1.5, every boolean true. The attested strategy is always `strategies/orb.py` with `source_digest="source-1"`, whatever strategy the operator arms. The July activation design listed "treating fixture attestations as promotion evidence" as a non-goal, but nothing replaced the fixture.

So the `paper-v1` gate (`trader/research/rulesets/paper_v1.py`) is on the operator path, but it has only ever judged fixture data. No code turns real backtests into `EligibilityEvidence`. No runtime check ties a bundle to the strategy that actually runs.

## Goal

"Activate" arms a strategy only when real backtests of that exact code, those params and those instruments pass `paper-v1`. If nothing passes, paper automation stays off and says why.

## Decisions (locked with the operator)

1. Evidence is produced by a **CLI command**, `mmr research evaluate <spec.yaml>`. The dashboard only uses existing bundles.
2. **Fixed-notional sizing.** The spec declares the order notional and account equity. Costs and the 3% canary drawdown are measured at that size.
3. **Two phases.** Phase A (this spec) builds the pipeline, binding and activation. Evidence that needs new helpers (liquidity envelope, SPY benchmark, three regime fields) is reported as missing, so the gate fails closed. Phase B adds those helpers.
4. **When evidence says "not eligible", Activate refuses.** No research-canary escape hatch.
5. **The operator review may be written by an LLM for paper bundles.** Live attestations need a human review.
6. **Approach A:** an in-process evaluator that calls the existing `run_window`, with every parameter point registered as a trial.

## Non-goals

- Phase B evidence helpers (separate spec).
- A dashboard "Evaluate" button.
- Live automation (`automation.live_enabled` stays refused).
- Mixing markets in one evaluation (one exchange calendar per spec).
- Changing the `paper-v1` thresholds.

## Overview

```text
spec.yaml ──► validate ──► qualify data + seal dataset manifest ──► create family
          ──► walk-forward trials (main + neighbours, 1x; main also 1.5x, 2x)
          ──► pre-holdout gate: every non-holdout rule must pass ──► else stop, holdout untouched
          ──► seal artifact ──► holdout (once, run twice) ──► open_holdout(passed)
          ──► full EligibilityEvidence ──► evaluate_eligibility ──► record decision
          ──► evaluation record + evidence report (markdown + JSON)
research review submit (human or llm on paper) ──► research attest ──► signed bundle
Activate(strategy) ──► find eligible bundle bound to that strategy ──► arm, or refuse with reason
```

## 1. Evaluation spec

One YAML file per evaluation, for example `research/orb_us.yaml`:

```yaml
name: orb_us_large
strategy: strategies/opening_range_breakout.py
class: OpeningRangeBreakout
params: {RANGE_MINUTES: 45, VOLUME_MULT: 1.3}
neighbourhood:            # adjacent values per tunable; neighbours vary one key at a time
  RANGE_MINUTES: [30, 60]
  VOLUME_MULT: [1.2, 1.4]
conids: [265598, 272093, 4815747, 208813719, 76792991, 15124833, 4391, 13824]
bar_size: 1 min
period: {start: 2024-01-02, end: 2026-09-30}
walk_forward: {folds: 6, embargo_sessions: 5, holdout_sessions: 90}
sizing: {order_notional: 1900, account_equity: 100000}
max_gross_allocation: 0.05
```

Validation fails loudly (non-zero exit, message names the field) when:

- fewer than 8 conids, or any conid is not in a local universe;
- the conids resolve to more than one execution-cost venue (one market per spec);
- `strategy` is outside `strategies/` or the class does not exist;
- a key in `params` or `neighbourhood` is not an upper-case tunable of the class;
- a neighbourhood value equals the main value, or `neighbourhood` is empty;
- `order_notional` or `account_equity` is not positive.

The cost model is always `realistic`. Each venue in `execution_costs.yaml` gains a `calendar` key (`us: XNYS`, `asx: XASX`). The walk-forward uses the venue's calendar.

## 2. Pipeline (`mmr research evaluate`)

New module `trader/research/evaluation.py`, with the evidence maths in small pure functions (`trader/research/evidence.py`) so each is unit-tested on hand-made trades.

1. **Validate** the spec (section 1).
2. **Qualify data.** Run `DatasetQualifier.qualify` on the bars of every conid for the period. Seal a `DatasetManifest`. If it is not `research_eligible`, stop and print the findings.
3. **Create the family** (`ExperimentRegistry.create_family`):
   - `source_tree_digest` = SHA-256 of the strategy file (`compute_strategy_hash`);
   - `dependency_lock_digest` = `_dependency_lock_digest()` (hash of `uv.lock`);
   - `dataset_manifest_digest` = the sealed manifest;
   - `repository_commit` = `_git_head()`; the command refuses to run with uncommitted changes to the strategy file;
   - `search_space` = `params` + `neighbourhood`;
   - `cost_model` = `{model: realistic, config_digest: <hash of the execution-cost config>, order_notional, account_equity}`;
   - `validation_protocol` = `{calendar, bar_size, period, folds, embargo_sessions, holdout_sessions}`;
   - `validation_folds` = the plan from `generate_walk_forward`, plus the holdout.

   The family id is content-addressed. Running the same spec again reuses the family. Any change makes a new family.
4. **Walk-forward trials.** For each parameter point (the main point and each neighbour) start a trial. Run `run_window` on every fold's test window with `RealisticCosts` and `order_notional`. The main point also runs at `scaled(1.5)` and `scaled(2.0)`. Jobs run in a process pool (default `cpu_count - 1`, cap 16). Finish each trial `SUCCEEDED` with metrics (section 5), or `FAILED` with the traceback digest.
5. **Pre-holdout gate.** Build `EligibilityEvidence` from the walk-forward results with the holdout-stage fields empty, and run `evaluate_eligibility(PAPER_V1, ...)`. The holdout-stage rules are `holdout_drawdown_within_canary`, `holdout_opened_once`, `benchmark_relative_drawdown` and the deterministic-replay rule (the plan pins the exact rule ids from `paper_v1.py`). If any other rule fails or is missing, stop here: record the evaluation as ineligible at the pre-holdout stage, with its failed and missing rules, and do not seal or open the holdout. You only touch the holdout when you would deploy the result. In Phase A this always stops here (liquidity and regime evidence are missing), so Phase A never opens a holdout.
6. **Seal** the artifact for the main point's trial.
7. **Holdout.** Run the main point on the holdout window at 1x, twice. Apply the holdout rule (section 4) and call `open_holdout(passed=...)`. A fail retires the artifact.
8. **Decide.** Build `ValidationResult`, then the full `EligibilityEvidence` (section 3), then `evaluate_eligibility(PAPER_V1, evidence)`. Record the decision with `EligibilityDecisionRepository`.
9. **Record and report.** Every run, whether it stopped at step 5 or finished, writes one row to a new `research_evaluations` table (research migration 11): spec name, family id, strategy path and class, stage reached, decision state, failed rules, missing rules, report path, created time. It writes `~/.local/share/mmr/reports/evaluation_<name>_<timestamp>.md` and `.json`: every evidence value, every failed rule, every missing field, the fold table and the trial list. It prints the decision and the report path.

The research DB is the existing research registry (`_research_registry()`).

## 3. Evidence definitions

All trade-based values use the main point's out-of-sample fold runs, unless the row says otherwise. Round trips come from `attribution.build_round_trips` (FIFO, both commissions netted).

| Field | Computation | Phase |
|---|---|---|
| `n_round_trips` | count of round trips at 1x | A |
| `n_instruments` | conids in the spec that passed data qualification | A |
| `expectancy_bps_baseline` / `_1_5x` / `_2x` | Σ net P&L ÷ Σ entry notional × 10,000 over round trips at 1x / 1.5x / 2x | A |
| `selection_adjusted_confidence` | `statistics.selection_adjusted_confidence(sr, n_trials, trial_sharpe_std, n_obs, skew, kurt)`: `sr` = main point's per-day Sharpe; `n_trials` and `trial_sharpe_std` per section 5; `n_obs`, skew, excess kurtosis of the main point's daily returns | A |
| `annualized_sharpe_ci_low` | `annualized_sharpe_ci(daily_returns, periods_per_year=252, seed=0).low`; daily returns = each fold's equity resampled to the last value per session, `pct_change` inside the fold, folds concatenated | A |
| `profit_factor` | `statistics.profit_factor(round-trip net P&L)` at 1x | A |
| `walk_forward_positive_fraction` | share of folds with net P&L > 0 at 1x | A |
| `max_month_profit_share` | `profit_concentration` of net P&L by close month | A |
| `max_instrument_profit_share` | `profit_concentration` of net P&L by conid | A |
| `scaled_holdout_drawdown` | max drawdown of the holdout run (fraction of `account_equity`, at `order_notional`) | A, holdout stage |
| `neighborhood_robust` | true if at least ⅔ of neighbour trials have `oos_expectancy_bps > 0` at 1x | A |
| `deterministic_replay_ok` | `trace_signature` of holdout run 1 == run 2 | A, holdout stage |
| `holdout_opened_once` | `get_artifact(...).holdout_opened` from the registry | A, holdout stage |
| `order_within_envelope` | missing (None) | B |
| `benchmark_drawdown_ratio` | missing (None) | B, holdout stage |
| `eligible_regime_positive_fraction`, `worst_eligible_regime_loss`, `regime_transitions_stable` | missing (None) | B |

`evaluate_eligibility` already fails closed on a missing value. After Phase A no strategy can be `PAPER_ELIGIBLE`. That is intended: paper automation stays off until Phase B.

## 4. Holdout rule

The holdout is opened only after the pre-holdout gate passes (section 2, step 5). It passes when both hold for the single holdout run at 1x:

- `expectancy_bps > 0` (dollar-weighted, after realistic costs);
- `|max_drawdown| <= 0.03` at the declared size.

A holdout with zero round trips fails.

## 5. Trials and the selection count

Each parameter point is one trial: key `point:<canonical params>`. Metrics: `oos_net_pnl`, `oos_expectancy_bps`, `daily_sharpe`, `n_round_trips`, `fold_net_pnls`, and `trace_signature`. The trace signature is a SHA-256 over the fold runs' `trace_signature` values in fold order (64 lower-case hex, as `ResearchBundle.export` requires).

**The selection count is per strategy, not per family.** Changing one param or one conid makes a new family, so a per-family count would reset with every tweak. `n_trials` therefore counts every terminal trial of every family with the same `strategy_path` and `class_name`, including legacy imported backtests (`research import-legacy`). A new registry query, `selection_trial_count_for_strategy(strategy_path, class_name)`, returns it. Paths are compared repo-relative (`strategies/x.py`): sweeps store absolute paths (`_resolve_strategy_path`) and `research family create` stores what was typed, so the query normalises both the stored and the queried path. Without that, legacy sweep trials would drop out of the count. `trial_sharpe_std` is the std of `daily_sharpe` over the trials that have it (evaluation trials; at least the main point and two neighbours). Every retry makes the gate stricter.

**Re-runs.** Running a spec whose family already opened its holdout stops with "holdout already opened for this family; change the spec to start a new family". Running a spec whose last run stopped before step 6 resumes the same family: finished trials are kept, missing ones run, and the gate is evaluated again. This is how a Phase A evaluation becomes eligible once Phase B evidence exists, without spending its holdout first.

## 6. Review, attestation and export

**Review.** `OperatorReview` gets a `reviewer_kind` field: `human` or `llm`. A new research migration (10) adds the column. Rows written before it get `unknown`. The field is part of the review digest. `mmr research review submit` gains `--reviewer-kind` (required). For a live attestation, `reviewer_kind` must be `human`.

**Attestation.** New command `mmr research attest <artifact_id>`. It refuses unless the decision is `PAPER_ELIGIBLE` and a review exists for that decision. It calls `build_attestation` with:

- `source_digest`, `config_digest` (= family dependency-lock digest, as the bundle check requires), `dataset_manifest_digest` from the family;
- `permitted_instruments` = the spec conids **as strings** (`session_risk` compares `str(conid)` against the list); `allowlist_digest` = SHA-256 of the sorted list;
- `max_gross_allocation` from the spec; `expires_at` = 90 days;
- `cost_assumptions` and `capacity_assumptions` from the family cost model;
- training / validation / holdout / evidence boundaries from the plan;
- `evidence_refs` = decision refs plus the `bundle_trials:` and `bundle_folds:` digests.

It signs with the key from `ensure_signing_keypair` and exports with `ResearchBundle.export` to `~/.local/share/mmr/artifacts/sha256_<manifest digest>/`. The manifest digest is only known after export, so it exports to a temporary directory under `artifacts/`, reads the digest from `manifest.json`, and renames. That is the layout `automated_intent_command` uses to find a bundle (`<bundle_root>/sha256_<digest>`). The fixture's `<artifact_id>/` layout does not match it; the plan starts with a test that pins the layout. The existing `research attest paper` subcommand is removed; it cannot produce an exportable bundle (it omits the `bundle_*` refs).

## 7. Binding checks

A bundle is bound to a strategy entry when all of these match:

| Bundle | Strategy entry / disk |
|---|---|
| `family.strategy_path`, `family.class_name` | YAML `module`, `class_name` |
| `family.source_tree_digest` | SHA-256 of the module file now |
| artifact `selected_parameters` | YAML `params`, upper-case keys only, both directions |
| attestation `permitted_instruments` | YAML `conids` (as a set) |
| `family.validation_protocol.bar_size` | YAML `bar_size` |

A new function `check_strategy_binding(verified_bundle, strategy_entry, module_path) -> None` raises `StrategyBindingError` naming every field that differs. It runs:

- **at load**: `strategy_runtime.load_strategy` passes the entry to `_verify_artifact_at_load`;
- **at arm**: `arm_paper_automation` checks the loaded strategy;
- **at dispatch**: the intent emitter adds the source digest computed at load to each intent; `automated_intent_command` compares it with the attestation's `source_digest`. `VerifiedArtifact` gains `source_digest` and `order_notional`. Instruments are already enforced at dispatch by the allowlist check in `session_risk`.

The fixture bundles on disk today fail this check (`strategies/orb.py`, `source-1`). The current fixture arm stops working when this ships. That is intended.

## 8. Sizing

**Backtester.** `BacktestConfig.order_notional: Optional[float]`. When set, a BUY signal without a quantity gets `floor(order_notional / fill_basis)` shares instead of 10% of cash. Evaluation runs always set it. Other backtests are unchanged.

**Live paper.** Today an automated intent needs the signal's quantity and is rejected with `QUANTITY_REQUIRED` otherwise (`session_risk.py`). The intent emitter gains one rule: when the signal has no quantity, quantity = `floor(order_notional / reference price)`, where `order_notional` comes from the verified bundle (`family.cost_model`) and the reference price is the close of the bar that produced the signal. So paper orders use the size the evidence was measured at. A signal that carries its own quantity keeps it, in the backtester and live alike. A SELL without a quantity closes the whole held position, in the backtester (as today) and in the emitter.

**Position caps.** Live `session_risk` rejects orders past its hard caps and the attestation's `max_gross_allocation`, while the backtester lets a position grow without limit (ORB stacks BUYs). Evaluation runs therefore set `BacktestConfig.max_gross_notional = max_gross_allocation × account_equity`: a BUY that would push the total open notional (all conids) past it is dropped, as the live path would reject it. The plan reads the hard caps in `session_risk.py` and mirrors every one that can be checked per order; any it cannot mirror is listed in the evaluation report.

## 9. Activate

`paper_activation` no longer creates bundles. For the selected strategy it:

1. lists bundles under `~/.local/share/mmr/artifacts/sha256_*`;
2. verifies each with `ArtifactVerifier` (paper mode) and keeps `PAPER_ELIGIBLE` ones that pass `check_strategy_binding` for the strategy's current YAML entry and file;
3. picks the newest by attestation `created_at`;
4. writes the existing `automation.*` keys (`artifact_bundle_path` = the artifacts root, `expected_artifact_id`, `public_key_ring_path`, `strategy_name`) and sets the strategy's `params.artifact_bundle_path` to the bundle directory;
5. refuses with code `NO_ELIGIBLE_BUNDLE` when none qualifies. The message reads the latest `research_evaluations` row for the strategy's file and class, and names its stage and failed or missing rules, or says no evaluation exists.

`export_fixture_paper_eligible_bundle` moves to a test helper (`tests/automation/fixture_bundle.py`). `scripts/bootstrap_paper_automation.py` keeps key generation and takes `--bundle PATH` instead of exporting a fixture.

## 10. CLI surface

```bash
mmr research evaluate research/orb_us.yaml        # run the pipeline, print decision + report path
mmr research evaluate research/orb_us.yaml --dry-run   # validate + show the job count
mmr research review submit --artifact-id ... --reviewer-kind llm ...
mmr research attest <artifact_id>                 # sign + export (PAPER_ELIGIBLE only)
mmr --json research evaluations                   # list evaluations: name, decision, failed rules, report
```

## 11. Errors

Every failure raises a named exception (`EvaluationSpecError`, `ExecutionCostError`, `StrategyBindingError`, `PaperAutomationActivationError`) with a message that names the cause. The CLI prints it and exits non-zero. A crashed backtest marks its trial `FAILED`; it still counts in the selection total. A partial run leaves no artifact unless step 6 completed. Nothing is attested unless the decision is `PAPER_ELIGIBLE`.

## 12. Testing

- Unit tests for each function in `trader/research/evidence.py`, on hand-made trades and equity curves.
- Spec validation: one test per refusal in section 1.
- Integration: the full pipeline on synthetic bars for 8 synthetic conids with a deterministic strategy:
  - a losing strategy ends ineligible, with its failed rules in the report;
  - a profitable strategy ends ineligible in Phase A at the pre-holdout stage, with the Phase B fields listed as missing, and the holdout is never opened (`holdout_access_log` stays empty);
  - export path, through public APIs only (no test seam in production code): seal and open the holdout with the registry, build a passing `EligibilityEvidence` directly, record it with `EligibilityDecisionRepository`, submit a review, then `research attest` exports a bundle that passes `ResearchBundle.verify` and `ArtifactVerifier.verify`, under a `sha256_<manifest digest>` directory;
  - re-running a spec that stopped before the holdout resumes the same family;
  - a second spec for the same strategy file (one param changed) is a new family, and its `n_trials` includes the first family's trials.
- Binding: refusal on a changed file, changed params, changed conids, changed bar size, and the old fixture bundle.
- Activate: refusal with `NO_ELIGIBLE_BUNDLE`; success with an eligible bound bundle; the written `artifact_bundle_path` is the root that `automated_intent_command` resolves.
- Sizing: backtester `order_notional`; `max_gross_notional` drops a BUY past the cap; emitter quantity from the bundle notional; emitter SELL without quantity closes the position.
- Selection count: a family created with an absolute strategy path and one with a relative path count as the same strategy.
- Full suite green before each commit.

## 13. Operational impact

- After this ships, paper automation cannot be armed until a strategy passes `paper-v1` on real evidence, which needs Phase B. Strategies can still run with `auto_execute: propose`.
- `docs/OPERATIONAL_STATE.md` and `docs/PAPER_AUTOMATION_SETUP.md` must say so, and drop the fixture instructions.
- Bundles expire after 90 days; re-run `research evaluate` and `research attest` to renew.

## 14. Phase B (outline, separate spec)

- `order_within_envelope`: order notional ≤ a share of 20-day dollar ADV, from daily bars; live order size ≤ attested notional.
- `benchmark_drawdown_ratio`: SPY (or the venue's index ETF) over the holdout, matched to the strategy's average exposure.
- Regime fields: trend and volatility buckets from the venue index's daily bars, fed to `attribution.attribute(by="regime")`.

## Known limitations

- Each fold starts without indicator warm-up, so the strategy trades a little less at the start of every fold. This biases results down, not up. A warm-up window can be added later.
- Spreads are estimated (one tick); history has no bid/ask. Cost stress at 1.5x and 2x is the guard.
- One market per evaluation.
