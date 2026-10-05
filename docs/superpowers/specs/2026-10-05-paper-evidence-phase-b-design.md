# Real Paper Evidence (Phase B) Design

**Date:** 2026-10-05
**Status:** design approved in chat; spec awaiting review.
**Depends on:** Phase A, merged as PR #11 (`45520990`). Phase A spec: `2026-10-04-real-paper-evidence-design.md`.

## Problem

`mmr research evaluate` builds real walk-forward evidence, but five `paper-v1` values are still `None`: `order_within_envelope`, `benchmark_drawdown_ratio`, `eligible_regime_positive_fraction`, `worst_eligible_regime_loss` and `regime_transitions_stable`. A `None` fails its rule, so every run stops at stage `pre_holdout` and no strategy can become `PAPER_ELIGIBLE`.

A second gap: "holdout opened once" is keyed per family. Any change to the code, params, `uv.lock` or the cost config makes a new family, and a new family gets a fresh holdout over the same dates. An operator can peek at the same holdout again and again.

## Goal

A strategy that passes every `paper-v1` rule on real data reaches `PAPER_ELIGIBLE`. Each of the five values is computed from real market data with a fixed, written definition. A strategy gets one look at a given holdout period, whatever else changes.

## Decisions (locked with the operator)

1. **Holdout key: strategy + window.** A holdout cannot be opened if the same strategy file and class already opened a holdout whose dates overlap.
2. **Benchmark: volatility-matched SPY.** Exposure is reported next to the ratio, not used to scale it.
3. **Approach A:** everything is computed inside `research evaluate` from local daily bars. No separate market-context artifact.
4. The `paper-v1` ruleset file is not edited, so its digest does not change.

## Non-goals

- Declaring eligible regimes (strategy abstention outside some regimes). All six regimes count.
- Order-book depth checks (there is no depth history).
- Non-US venues (the evaluator already accepts XNYS only).
- Running `research evaluate` in split Docker.
- Changing `paper-v1` thresholds.

## Overview

```text
spec.yaml ──► load strategy bars + SPY daily bars ──► qualify both, seal manifest (SPY included)
          ──► refuse if this strategy+class already opened an overlapping holdout
          ──► walk-forward trials (unchanged)
          ──► pre-holdout evidence: Phase A values + liquidity + regime values
          ──► pre-holdout gate ──► else stop, holdout untouched
          ──► holdout (once; the overlap check runs again inside the open transaction)
          ──► holdout evidence: Phase A values + vol-matched SPY benchmark
          ──► decision, report (with a "Market context" section)
```

## Constants

All live in one new module, `trader/research/market_context.py`, and all enter the family identity (section 2).

| Name | Value | Meaning |
|---|---|---|
| `BENCHMARK_CONID` | 756733 | SPY, the XNYS benchmark and regime index |
| `SPY_LOOKBACK_SESSIONS` | 220 | SPY sessions required before `period.start` |
| `TREND_SMA_SESSIONS` | 200 | trend = previous close ÷ 200-session average − 1 |
| `VOLATILITY_SESSIONS` | 20 | volatility = std (ddof 1) of the last 20 daily returns |
| `REGIME_MIN_SAMPLES` | 30 | round trips for a regime bucket to be "adequate" |
| `REGIME_HOLD_SESSIONS` | 3 | a regime change counts when the new label holds this long |
| `TRANSITION_WINDOW_SESSIONS` | 5 | first sessions of a new regime that form the change window |
| `LIQUIDITY_ADV_SESSIONS` | 20 | rolling median window for daily dollar volume |
| `LIQUIDITY_MAX_ADV_SHARE` | 0.01 | order notional ≤ 1% of the lowest rolling median |
| `LIVE_NOTIONAL_TOLERANCE` | 0.05 | live entry may exceed the attested notional by at most 5% |

The regime thresholds (1% and 2% daily volatility) and the six labels stay as frozen in `trader/research/attribution.py`.

## 1. Data inputs

- `research evaluate` loads SPY daily bars from `SPY_LOOKBACK_SESSIONS` XNYS sessions before `period.start` through `period.end`, from the same history DB as the strategy bars.
- Missing or short SPY history stops the run before any trial, with `EvaluationDataError` naming the first date needed and the exact command, for example `mmr data download SPY --bar-size "1 day" --days 1100`.
- SPY bars pass the same `DatasetQualifier` checks (bar interval `1 day`). A failed required finding stops the run.
- The SPY file is added to the dataset manifest's `files` as `tick_data/756733/1 day`, so the manifest digest covers it. New SPY data makes a new family.
- The strategy's own 1-minute bars are unchanged.

## 2. Family identity

`validation_protocol` gains three entries, so changing any definition makes a new family:

- `benchmark_conid`: 756733;
- `regime_definition`: the regime constants above plus `regime_taxonomy_digest()`;
- `liquidity`: `LIQUIDITY_ADV_SESSIONS` and `LIQUIDITY_MAX_ADV_SHARE`.

No Phase A family has been stored yet, so nothing has to be migrated.

## 3. Holdout key: strategy + window

- **Before any trial**, the evaluator computes the planned holdout dates (first and last session) and asks the registry for every opened holdout of the same strategy and class. Strategy paths compare repo-relative (`normalize_strategy_path`), the same as the selection count. Holdout dates come from each family's `validation_folds` row of kind `holdout`.
- Two windows overlap when `start_a <= end_b` and `start_b <= end_a`. Any overlap stops the run with `EvaluationError`: "strategy `<path>` class `<cls>` already opened a holdout over `<start>`–`<end>` (artifact `<id>`); the next holdout must start after `<end>`".
- **Inside `open_holdout`**, in the same transaction that writes `holdout_access_log`, the registry repeats the check from the artifact's own family and raises `HoldoutAlreadyOpened` on overlap. Two concurrent evaluations cannot both open.
- The existing per-family refusal stays.
- Legacy imported families have no holdout folds and are ignored.
- No schema change.
- **Consequence:** after a holdout is spent, the next attempt needs a period whose last `holdout_sessions` sessions all fall after the old holdout. With the example spec that is 90 new sessions, about 4.5 months.
- **Limitation:** renaming the file or the class gets around the rule. The signed review records the strategy path.

## 4. Liquidity envelope (`order_within_envelope`, pre-holdout)

- For each conid, daily dollar volume = Σ close × volume over that session's 1-minute bars, from the bars already loaded. Only sessions before the holdout are used.
- Rolling median over `LIQUIDITY_ADV_SESSIONS` sessions, shifted by one session (known as of the previous close). Sessions without a full window are skipped.
- A conid passes when `order_notional <= LIQUIDITY_MAX_ADV_SHARE × min(rolling median)`. `order_within_envelope` is true when every conid passes.
- A conid with no full window gives `None`, and the report names it.
- `capacity_estimate` (reported, not gating) = `LIQUIDITY_MAX_ADV_SHARE × min over conids of min(rolling median)`, i.e. the largest order notional that would still pass.
- Depth is not checked (listed in the report).

## 5. Benchmark (`benchmark_drawdown_ratio`, holdout stage)

Computed from the first holdout run at 1x, on session closes:

1. Strategy session returns: `evidence.session_returns([holdout equity curve], starting_equity=account_equity)`.
2. SPY session returns: close-to-close over the holdout sessions. Inner-join both series on session date.
3. Scale `k = std(strategy) / std(SPY)` (ddof 1).
4. Benchmark equity: `account_equity × cumprod(1 + k × SPY return)`. Strategy equity: `account_equity × cumprod(1 + strategy return)`.
5. `validation.benchmark_metrics(strategy_equity, benchmark_equity, periods_per_year=252, strategy_time_in_market=..., benchmark_time_in_market=1.0)`. `benchmark_drawdown_ratio` = its `drawdown_ratio`.

The ratio is `None` (rule fails, the report says why) when:

- fewer than 2 joined sessions;
- the strategy's session returns have zero volatility;
- the scaled SPY curve has no drawdown in the holdout.

The ratio uses session-close drawdown. The canary rule (`scaled_holdout_drawdown`) keeps using the run's intraday drawdown. Both appear in the report.

Reported, not gating, in `EligibilityEvidence`:

- `benchmark_return`, `benchmark_downside_deviation`, `benchmark_recovery_time`, all of the scaled SPY curve;
- `strategy_time_in_market`: the share of holdout regular-session minutes with at least one round trip open (from round-trip entry and exit times, merged across conids).

The report also shows `k` and the raw (unscaled) SPY return and drawdown.

## 6. Regime labels

For each session `d` in the period, from SPY closes before `d` only:

- `trend_d = close[d-1] / mean(close[d-200 .. d-1]) − 1`;
- `volatility_d = std(daily returns over the 20 sessions ending at d-1)`, ddof 1;
- `label_d = attribution.classify_regime({'trend': trend_d, 'volatility': volatility_d})`.

Each walk-forward round trip of the main point at 1x gets the label of its entry session (the XNYS session date of the entry time).

## 7. Regime values (pre-holdout)

- `table = attribution.attribute(round_trips, by='regime', min_samples=REGIME_MIN_SAMPLES)`.
- `eligible_regime_positive_fraction` = `table.positive_fraction_of_adequate`. `None` when no regime has 30 round trips.
- `worst_eligible_regime_loss` = `min(0, min(b.share for adequate buckets))`, where `share` = bucket P&L ÷ total net P&L. `None` when there is no adequate bucket or total net P&L ≤ 0. Example: -0.08 means the worst regime gave back 8% of the total profit. The rule needs ≥ -0.10.
  - Why not "share of account equity": at $1,900 orders a regime would have to lose $10,000 to fail, so the rule would never bite.
- `regime_transitions_stable`:
  - A **regime change** happens at session `d` when `label_d` differs from the current regime and holds for `REGIME_HOLD_SESSIONS` sessions in a row (`d .. d+2`). The current regime starts as the first label that holds 3 sessions. This stops flapping around the 1% volatility line.
  - The **change window** is the first `TRANSITION_WINDOW_SESSIONS` sessions of each new regime (`d .. d+4`).
  - Using `d+1` and `d+2` to confirm a change is not lookahead: the windows only group past trades for the evaluation; the strategy never sees them.
  - Round trips entered in any change window form one group. Stable when `group P&L >= REGIME_LOSS_TOLERANCE × total net P&L` (gave back no more than 10% of the profit). `REGIME_LOSS_TOLERANCE` is imported from `paper_v1`.
  - No round trips in any window: true (the strategy was not active during changes).
  - No regime change in the walk-forward sessions: `None` (nothing to judge).
  - Total net P&L ≤ 0: `None`.
  - The report shows the number of changes, the share of sessions inside change windows, the group's trades and P&L.

## 8. Live check: order size ≤ attested notional

- In the session-risk entry block (`trader/automation/session_risk.py`, next to the `POSITION_PCT` check), an entry whose `quantity × entry_price` exceeds `attested order_notional × (1 + LIVE_NOTIONAL_TOLERANCE)` adds the reason `ORDER_EXCEEDS_ATTESTED_NOTIONAL`.
- The attested notional comes from the verified bundle's family `cost_model.order_notional` (as `strategy_binding` reads it today).
- A verified bundle without an attested notional also refuses the entry with the same reason and a detail saying the notional is missing.
- Exits are not checked.

## 9. Report and errors

- The evaluation report (markdown and JSON) gets a "Market context" section:
  - the regime table: label, trades, P&L, share, adequate;
  - the regime-change summary;
  - liquidity per conid: lowest rolling median dollar volume, its date, order notional ÷ median;
  - the benchmark numbers from section 5.
- Every `None` from sections 4–7 carries a cause string, shown next to the missing rule (CLAUDE.md "fail loudly").
- The evaluation summary JSON read by Activate is unchanged.

## 10. Testing

Pure functions (`tests/research/test_market_context.py`), with hand-made series:

- regime labels match hand-computed trend and volatility;
- no lookahead: changing SPY bars after session `d` does not change `label_d`;
- a flapping label does not count as a change; a label held 3 sessions does;
- transition stability: true with no trades in windows, `None` with no changes, false when the window group gives back more than 10%;
- `worst_eligible_regime_loss` and the positive fraction, including "no adequate bucket" and "total P&L ≤ 0";
- vol matching: `k` and the ratio on a known pair of series; zero SPY drawdown gives `None`; zero strategy volatility gives `None`;
- time in market with overlapping round trips on two conids;
- liquidity: notional just under and just over 1% of the lowest median; a conid without a full window gives `None`.

Pipeline (`tests/research/test_evaluation*.py`, synthetic DB):

- a strategy that passes every Phase A rule, with SPY data on which the Phase B values pass, opens the holdout and reaches `PAPER_ELIGIBLE`;
- the same strategy and class in a second family (different params) with an overlapping holdout is refused before any trial runs, and `holdout_access_log` gains no row;
- a non-overlapping later holdout is allowed;
- `open_holdout` refuses an overlapping window even when the pre-check was bypassed (concurrency guard);
- missing SPY bars stop the run with the download command in the message;
- SPY bars change the manifest digest and therefore the family id.

Trader (`tests/automation/`): an automated entry sized more than 5% above the attested notional is refused with `ORDER_EXCEEDS_ATTESTED_NOTIONAL`; one at the notional passes; a bundle without an attested notional refuses.

Full suite green before each commit.

## 11. Operational impact

- Before evaluating, download SPY daily bars once: `mmr data download SPY --bar-size "1 day" --days <n>` (the error message gives `n`).
- After this ships, a strategy that passes all rules can be reviewed, attested and armed.
- **Arming is still blocked in practice** by the open blocker in `docs/OPERATIONAL_STATE.md`: automated exits are not safe (an unsized SELL is refused with `QUANTITY_REQUIRED`; a sized SELL gets a reverse BUY stop while the entry stop stays working). That needs its own fix before any strategy is armed.
- Update `CLAUDE.md` (research evaluation paragraph), `docs/OPERATIONAL_STATE.md` and `docs/PAPER_AUTOMATION_SETUP.md` (the SPY download step; runs no longer always stop at `pre_holdout`).

## Known limitations

- Volume from a single-venue feed (for example Alpaca's free IEX feed) understates dollar volume. The liquidity check is then stricter than needed, not looser.
- One regime index (SPY) for every instrument.
- The frozen 1% / 2% volatility bands put most 2024–2026 sessions in "low", so few regimes may reach 30 trades.
- Renaming the strategy file or class gets around the holdout key.
- Depth is not checked.
