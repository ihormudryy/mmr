# Real Paper Evidence (Phase A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixture `PAPER_ELIGIBLE` bundle with a CLI pipeline that turns real backtests into `paper-v1` evidence, bind every bundle to the exact strategy it attests, and make paper "Activate" refuse without a real eligible bundle.

**Architecture:** `mmr research evaluate <spec.yaml>` validates a YAML spec, qualifies the data, creates a content-addressed experiment family, runs walk-forward backtests (main point at 1x/1.5x/2x costs, neighbours at 1x) that mirror live paper rules, and evaluates every non-holdout rule first. Only if all of them pass does it seal the artifact and open the holdout once. `research review submit` (human or LLM on paper) and `research attest bundle` then sign and export the bundle to `artifacts/sha256_<manifest digest>/`. The strategy runtime, the order dispatcher and Activate all check the bundle against the strategy's file, params, conids and bar size.

**Tech Stack:** Python 3.12, DuckDB (research registry), pandas/numpy/scipy, exchange_calendars, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-04-real-paper-evidence-design.md`

## Global Constraints

- Branch `feat/real-paper-evidence` (already stacks `fix/expectancy-bps` and `fix/execution-costs`). Commit after every task. Never push.
- `$PY` below is the project's Python (e.g. `PY=.venv/bin/python` after `uv sync --extra test`); run `$PY -m pytest ...` from the repo root.
- Full suite: `$PY -m pytest tests/ --timeout=60 -q -p no:cacheprovider --ignore=tests/test_ibrx_async.py` (about 2 minutes). Run it at the end of every task that touches `trader/`.
- Commit subject: conventional style used in this repo (`feat(research): ...`, `fix(automation): ...`), lowercase, imperative. Last line of every commit message, after a blank line: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Fail loudly: every refusal raises a named exception whose message names the cause. Never swallow an error and return an empty result.
- Comments only where the code is not obvious. Match the surrounding code style.
- `paper-v1` thresholds and rule codes are not changed (`trader/research/rulesets/paper_v1.py`).
- Holdout-stage rule codes (exact): `holdout_drawdown_within_canary`, `deterministic_replay`, `holdout_opened_once`, `benchmark_relative_drawdown`.
- Phase B evidence stays missing (`None`): `order_within_envelope`, `benchmark_drawdown_ratio`, `eligible_regime_positive_fraction`, `worst_eligible_regime_loss`, `regime_transitions_stable`.
- Bundle directory name: `"sha256_" + manifest_digest` (manifest digest is plain hex).
- `permitted_instruments` are conid **strings**.
- Neighbourhood rule: robust when at least 2/3 of neighbour trials have `oos_expectancy_bps > 0`; fewer than 2 neighbours means `None` (fails closed).
- Holdout pass rule: at least one round trip, dollar-weighted expectancy > 0, and `|max_drawdown| <= 0.03`.
- Attestation lifetime: 90 days.

## Spec deviations (decided while planning; reasons given)

1. **Backtests run through `Backtester.run_from_module`, not `run_window`.** Both use `Backtester.run`; `run_from_module` also loads the strategy from its file and applies params, which a worker process needs. `run_window` stays for research tests.
2. **`automation.artifact_bundle_path` stays the bundle directory**, as Activate writes it today. `command_stack` learns that a `sha256_*` directory is a bundle, so its parent is the bundle root that order dispatch resolves. This is a smaller change than moving every caller to the artifacts root, with the same effect.
3. **CLI name `research attest bundle <artifact_id>`** instead of `research attest <artifact_id>`: `attest` already has subcommands (`verify`), and a positional id would clash with them.
4. **Live rules mirrored beyond position caps.** `session_risk.py` and `calendar_policy.py` also gate entries by time of day (no entries in the first 5 minutes or the last 30), halt on 0.5% daily loss or 3% drawdown, cap open positions at 3, and the session controller flattens every position at close-15 minutes. The spec asks to mirror every live check that can be checked per order; these are all mirrored (Task 2). Protective stop orders and the stop-distance trade-risk cap cannot be mirrored without stop simulation; they are listed in every evaluation report.
5. **Paper automation only runs on XNYS** (`calendar_policy.XNYSCalendarPolicy`). Evaluation refuses a venue whose calendar is not XNYS, with that reason.
6. **Execution-cost venues get an optional `calendar` key.** Users whose `~/.config/mmr/execution_costs.yaml` predates it get a loud error naming the key to add.
7. **Activate reads a summary file, not the research DB.** `trader/config.py` says the research DuckDB is never opened by trader_service or strategy_service (the research CLI owns it, so its locks never contend with the trader's). Activate runs in trader_service, so each evaluation also writes a one-file JSON summary to `~/.local/share/mmr/artifacts/evaluations/` (the mount Activate already reads bundles from), and Activate's refusal message reads that file. The `research_evaluations` table stays for the CLI.

## Review Focus

1. A strategy whose signals carry their own quantities bypasses `order_notional`; its entries must still hit the live caps (blocked, counted) — test in Task 2.
2. One conid has no bars in the period (new listing, missing download): evaluation must stop and name the conid, not silently drop it — test in Task 7.
3. An evaluation interrupted mid-run leaves a `RUNNING` trial: the next run must finish it as `FAILED` (it still counts) and start a fresh attempt — test in Task 8.
4. All out-of-sample round trips win (profit factor `inf`): the report and the decision must still serialize, and the rule fails closed — test in Task 8.
5. The operator edits the strategy YAML (params or conids) after attesting, or two bundles exist for the same strategy: Activate must pick only a bundle bound to the current YAML and file, and refuse otherwise — test in Task 13.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `trader/simulation/backtester.py` | `order_notional` sizing; calls `PaperAutomationRules` | 1, 2 |
| `trader/simulation/live_rules.py` (new) | live paper entry gates + end-of-day flatten, constants imported from live code | 2 |
| `trader/simulation/execution_costs.py` | venue `calendar` key | 3 |
| `trader/research/strategy_paths.py` (new) | repo root, repo-relative strategy paths, strategy file resolution | 3 |
| `trader/research/evaluation_spec.py` (new) | load + validate the evaluation YAML | 3 |
| `trader/research/evidence.py` (new) | pure evidence maths + pre-holdout gate | 4 |
| `trader/research/review.py` | `reviewer_kind` (research migration 10) | 5 |
| `trader/research/bundle.py` | carry `reviewer_kind` through export/verify | 5 |
| `trader/research/experiment_registry.py` | strategy-level trial query, family artifacts | 6 |
| `trader/research/evaluation_store.py` (new) | `research_evaluations` table (migration 11) | 6 |
| `trader/research/schema.py` | register migrations 10 and 11 | 5, 6 |
| `trader/research/evaluation_jobs.py` (new) | one backtest window per job, process pool | 7 |
| `trader/research/evaluation_data.py` (new) | load bars, qualify, build dataset manifest | 7 |
| `trader/research/evaluation.py` (new) | `evaluate()` orchestration | 8 |
| `trader/research/evaluation_report.py` (new) | markdown + JSON report | 8 |
| `trader/research/attest_export.py` (new) | sign attestation + export bundle | 9 |
| `trader/mmr_cli.py` | `research evaluate`, `research evaluations`, `research attest bundle`, `--reviewer-kind`; remove `attest paper` | 10 |
| `trader/automation/strategy_binding.py` (new) | `AttestedStrategy`, `check_strategy_binding` | 11 |
| `trader/automation/artifact_verifier.py` | `VerifiedArtifact.attested_strategy` | 11 |
| `trader/strategy/strategy_runtime.py` | binding check at load and arm; pass reference price | 11, 12 |
| `trader/strategy/intent_emitter.py` | notional sizing; source digest on the wire | 12 |
| `trader/messaging/production_api.py` | `strategy_source_digest` request field | 12 |
| `trader/automation/automated_intent_command.py` | reject source-digest mismatch | 12 |
| `trader/automation/session_risk.py` | SELL without quantity closes the held position | 12 |
| `trader/trading/command_stack.py` | `sha256_*` bundle dirs → parent is bundle root | 12 |
| `trader/automation/bundle_finder.py` (new) | find the newest eligible bound bundle | 13 |
| `trader/automation/paper_activation.py` | use the finder; no fixture | 13 |
| `trader/automation/paper_materials.py` | keep keygen + hints; fixture removed | 13 |
| `tests/automation/fixture_bundle.py` (new) | the old fixture exporter, test-only | 13 |
| `scripts/bootstrap_paper_automation.py` | keygen + `--bundle PATH` | 13 |
| docs (`OPERATIONAL_STATE.md`, `PAPER_AUTOMATION_SETUP.md`, `CLAUDE.md`, skill) | operator truth | 14 |

---

### Task 1: Fixed order notional in the backtester

**Files:**
- Modify: `trader/simulation/backtester.py` (`BacktestConfig`, `_execute_signal` sizing block)
- Test: `tests/test_backtester_sizing.py` (new)

**Interfaces:**
- Produces: `BacktestConfig.order_notional: Optional[float] = None`. When set, a BUY signal with `quantity == 0` buys `math.floor(order_notional / fill_basis)` shares. Explicit signal quantities are unchanged. SELL without quantity still closes the held position.

- [ ] **Step 1: Write the failing tests**

```python
"""Fixed order notional: a BUY without a quantity buys floor(notional / price)."""
import datetime as dt

from tests.test_backtest_metrics import _install, _write_bars
from tests.test_backtester_costs import SizedScript
from trader.data.data_access import TickStorage
from trader.objects import Action, BarSize
from trader.simulation.backtester import Backtester, BacktestConfig
from trader.simulation.slippage import ZeroSlippage


def _run(duckdb_path, steps, **config):
    cfg = BacktestConfig(
        start_date=dt.datetime(2024, 1, 2, 9, 30, tzinfo=dt.timezone.utc),
        end_date=dt.datetime(2024, 1, 2, 10, 30, tzinfo=dt.timezone.utc),
        bar_size=BarSize.Mins1, initial_capital=100_000.0,
        slippage_model=ZeroSlippage(), commission_per_share=0.0, **config)
    strategy = _install(SizedScript(steps), duckdb_path)
    return Backtester(storage=TickStorage(duckdb_path=duckdb_path), config=cfg).run(strategy, [4391])


def test_buy_without_quantity_uses_order_notional(tmp_duckdb_path):
    _write_bars(tmp_duckdb_path, [100, 100, 100, 101, 101, 101])
    result = _run(tmp_duckdb_path, [(Action.BUY, 0), None, (Action.SELL, 0), None, None, None],
                  order_notional=1_950.0)
    assert [t.quantity for t in result.trades] == [19, 19]


def test_explicit_quantity_wins_over_order_notional(tmp_duckdb_path):
    _write_bars(tmp_duckdb_path, [100] * 6)
    result = _run(tmp_duckdb_path, [(Action.BUY, 5), None, None, None, None, None],
                  order_notional=1_950.0)
    assert result.trades[0].quantity == 5


def test_without_order_notional_buys_ten_percent_of_cash(tmp_duckdb_path):
    _write_bars(tmp_duckdb_path, [100] * 6)
    result = _run(tmp_duckdb_path, [(Action.BUY, 0), None, None, None, None, None])
    assert result.trades[0].quantity == 100
```

- [ ] **Step 2: Run them and watch the first two fail**

Run: `$PY -m pytest tests/test_backtester_sizing.py -q -p no:cacheprovider`
Expected: `test_buy_without_quantity_uses_order_notional` and `test_explicit_quantity_wins_over_order_notional` fail with `TypeError: BacktestConfig.__init__() got an unexpected keyword argument 'order_notional'`.

- [ ] **Step 3: Implement**

In `BacktestConfig`, after `cost_model`:

```python
    # Size of a BUY that carries no quantity, instead of 10% of cash. Evidence
    # runs set it to the paper order size so commission minimums count.
    order_notional: Optional[float] = None
```

In `_execute_signal`, replace the `else:` of the default-quantity block:

```python
            if quantity == 0:
                if signal.action == Action.SELL:
                    # (existing comment kept)
                    held = positions.get(conid, 0)
                    quantity = held if held > 0 else 0
                elif self.config.order_notional is not None:
                    quantity = math.floor(self.config.order_notional / fill_basis)
                else:
                    quantity = math.floor((cash * 0.1) / fill_basis) if fill_basis > 0 else 0
```

- [ ] **Step 4: Run the tests**

Run: `$PY -m pytest tests/test_backtester_sizing.py tests/test_backtester_costs.py tests/test_backtest_metrics.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/simulation/backtester.py tests/test_backtester_sizing.py
git commit -m "feat(backtest): size notional-less buys with a fixed order notional" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Mirror live paper-automation rules in the backtester

**Files:**
- Create: `trader/simulation/live_rules.py`
- Modify: `trader/simulation/backtester.py` (`BacktestConfig.live_rules`, `BacktestResult.live_rule_blocks`, `run()` loop, `_execute_signal` BUY branch)
- Test: `tests/test_live_rules.py` (new)

**Interfaces:**
- Consumes: `trader.automation.session_risk` constants `MAX_POSITIONS`, `MAX_POSITION_FRACTION`, `MAX_GROSS_FRACTION`, `MAX_DAILY_LOSS_FRACTION`, `MAX_DRAWDOWN_FRACTION`; `trader.automation.calendar_policy.XNYSCalendarPolicy`, `SessionSchedule`, `ET`.
- Produces:
  - `class PaperAutomationRules(*, max_gross_allocation: float, calendar: XNYSCalendarPolicy | None = None)` with `reset(equity: float) -> None`, `mark(ts, equity: float) -> None`, `entry_block_reason(*, ts, conid: int, order_notional: float, position_values: Mapping[int, float], equity: float) -> Optional[str]`, `flatten_due(ts) -> bool`.
  - Block reason codes: `ENTRY_WINDOW`, `EQUITY_INVALID`, `DAILY_LOSS`, `DRAWDOWN`, `MAX_POSITIONS`, `POSITION_PCT`, `GROSS`.
  - `NOT_MIRRORED: tuple[str, ...]` — live checks the backtester cannot reproduce (for reports).
  - `BacktestConfig.live_rules: Optional[PaperAutomationRules] = None`; `BacktestResult.live_rule_blocks: Dict[str, int]` (count per reason).

- [ ] **Step 1: Write the failing tests**

```python
"""Backtests that mirror live paper automation: entry window, caps, halts,
and the end-of-day flatten."""
import datetime as dt

import pandas as pd
import pytest

from tests.test_backtest_metrics import _install
from tests.test_backtester_costs import SizedScript
from trader.data.data_access import TickStorage
from trader.data.duckdb_store import DuckDBDataStore
from trader.objects import Action, BarSize
from trader.simulation.backtester import Backtester, BacktestConfig
from trader.simulation.live_rules import PaperAutomationRules
from trader.simulation.slippage import ZeroSlippage

UTC = dt.timezone.utc
TUESDAY_10_ET = dt.datetime(2024, 1, 2, 15, 0, tzinfo=UTC)


def _rules(max_gross_allocation=0.05, equity=100_000.0):
    rules = PaperAutomationRules(max_gross_allocation=max_gross_allocation)
    rules.reset(equity)
    rules.mark(TUESDAY_10_ET, equity)
    return rules


class TestEntryGates:
    def test_open_market_small_order_is_allowed(self):
        assert _rules().entry_block_reason(ts=TUESDAY_10_ET, conid=1, order_notional=1_900,
                                           position_values={}, equity=100_000) is None

    def test_first_five_minutes_are_blocked(self):
        ts = dt.datetime(2024, 1, 2, 14, 33, tzinfo=UTC)  # 09:33 ET
        assert _rules().entry_block_reason(ts=ts, conid=1, order_notional=1_900,
                                           position_values={}, equity=100_000) == 'ENTRY_WINDOW'

    def test_last_thirty_minutes_are_blocked(self):
        ts = dt.datetime(2024, 1, 2, 20, 31, tzinfo=UTC)  # 15:31 ET
        assert _rules().entry_block_reason(ts=ts, conid=1, order_notional=1_900,
                                           position_values={}, equity=100_000) == 'ENTRY_WINDOW'

    def test_weekend_is_blocked(self):
        ts = dt.datetime(2024, 1, 6, 15, 0, tzinfo=UTC)  # Saturday
        assert _rules().entry_block_reason(ts=ts, conid=1, order_notional=1_900,
                                           position_values={}, equity=100_000) == 'ENTRY_WINDOW'

    def test_daily_loss_halts_entries(self):
        assert _rules().entry_block_reason(ts=TUESDAY_10_ET, conid=1, order_notional=1_900,
                                           position_values={}, equity=99_400) == 'DAILY_LOSS'

    def test_drawdown_from_the_high_water_mark_halts_entries(self):
        rules = _rules()
        rules.mark(TUESDAY_10_ET, 103_000)
        wednesday = TUESDAY_10_ET + dt.timedelta(days=1)
        rules.mark(wednesday, 99_900)
        assert rules.entry_block_reason(ts=wednesday, conid=1, order_notional=1_000,
                                        position_values={}, equity=99_900) == 'DRAWDOWN'

    def test_fourth_position_is_blocked(self):
        held = {1: 1_000.0, 2: 1_000.0, 3: 1_000.0}
        assert _rules().entry_block_reason(ts=TUESDAY_10_ET, conid=4, order_notional=1_000,
                                           position_values=held, equity=100_000) == 'MAX_POSITIONS'

    def test_position_above_five_percent_is_blocked(self):
        assert _rules().entry_block_reason(ts=TUESDAY_10_ET, conid=1, order_notional=6_000,
                                           position_values={}, equity=100_000) == 'POSITION_PCT'

    def test_gross_above_attested_allocation_is_blocked(self):
        assert _rules(max_gross_allocation=0.02).entry_block_reason(
            ts=TUESDAY_10_ET, conid=2, order_notional=1_000,
            position_values={1: 1_500.0}, equity=100_000) == 'GROSS'


class TestFlatten:
    def test_flatten_starts_fifteen_minutes_before_the_close(self):
        rules = _rules()
        assert not rules.flatten_due(dt.datetime(2024, 1, 2, 20, 44, tzinfo=UTC))
        assert rules.flatten_due(dt.datetime(2024, 1, 2, 20, 45, tzinfo=UTC))


def _write_session_bars(duckdb_path, price=100.0, conid=4391):
    index = pd.date_range('2024-01-02 09:30', periods=390, freq='1min',
                          tz='America/New_York').tz_convert('UTC')
    frame = pd.DataFrame({'open': price, 'high': price + 0.01, 'low': price - 0.01,
                          'close': price, 'volume': 10_000.0}, index=index)
    frame.index.name = 'date'
    DuckDBDataStore(duckdb_path).write(str(conid), frame)


def _session_run(duckdb_path, steps):
    config = BacktestConfig(
        start_date=dt.datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
        end_date=dt.datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
        bar_size=BarSize.Mins1, initial_capital=100_000.0,
        slippage_model=ZeroSlippage(), commission_per_share=0.0,
        live_rules=PaperAutomationRules(max_gross_allocation=0.05))
    strategy = _install(SizedScript(steps), duckdb_path)
    return Backtester(storage=TickStorage(duckdb_path=duckdb_path), config=config).run(strategy, [4391])


def test_backtest_blocks_early_entry_and_flattens_before_close(tmp_duckdb_path):
    _write_session_bars(tmp_duckdb_path)
    steps = [(Action.BUY, 10)] + [None] * 9 + [(Action.BUY, 10)] + [None] * 379

    result = _session_run(tmp_duckdb_path, steps)

    assert result.live_rule_blocks == {'ENTRY_WINDOW': 1}
    buy, flatten = result.trades
    assert buy.action == Action.BUY
    assert pd.Timestamp(buy.timestamp).tz_convert('America/New_York').strftime('%H:%M') == '09:41'
    assert flatten.action == Action.SELL and flatten.quantity == 10
    assert pd.Timestamp(flatten.timestamp).tz_convert('America/New_York').strftime('%H:%M') == '15:45'


def test_explicit_quantity_above_the_position_cap_is_blocked(tmp_duckdb_path):
    _write_session_bars(tmp_duckdb_path)
    steps = [None] * 10 + [(Action.BUY, 60)] + [None] * 379  # 6,000 = 6% of equity

    result = _session_run(tmp_duckdb_path, steps)

    assert result.trades == []
    assert result.live_rule_blocks == {'POSITION_PCT': 1}
```

- [ ] **Step 2: Run them and watch them fail**

Run: `$PY -m pytest tests/test_live_rules.py -q -p no:cacheprovider`
Expected: collection error `ModuleNotFoundError: No module named 'trader.simulation.live_rules'`.

- [ ] **Step 3: Create `trader/simulation/live_rules.py`**

```python
"""Live paper-automation rules the backtester mirrors, so evidence is measured
under the same constraints paper trading runs under.

The limits are imported from the live modules (``session_risk``,
``calendar_policy``) so the two cannot drift apart.
"""
from __future__ import annotations

import datetime as dt
from typing import Mapping, Optional

from trader.automation.calendar_policy import ET, SessionSchedule, XNYSCalendarPolicy
from trader.automation.session_risk import (
    MAX_DAILY_LOSS_FRACTION,
    MAX_DRAWDOWN_FRACTION,
    MAX_GROSS_FRACTION,
    MAX_POSITION_FRACTION,
    MAX_POSITIONS,
)

# Live checks a bar backtest cannot reproduce; evaluation reports list them.
NOT_MIRRORED = (
    'protective stop orders and the stop-distance trade-risk cap (MAX_TRADE_RISK_FRACTION)',
    'broker quotes: fills use the next bar open plus estimated costs',
    'the multi-strategy portfolio budget',
)


def _as_utc(ts) -> dt.datetime:
    value = ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


class PaperAutomationRules:
    """Entry gates and the end-of-day flatten of live paper automation.

    Stateful for one backtest run: ``reset`` at the start, then ``mark`` once
    per bar with the equity known before that bar's fills.
    """

    def __init__(self, *, max_gross_allocation: float,
                 calendar: Optional[XNYSCalendarPolicy] = None):
        self._gross_cap = min(MAX_GROSS_FRACTION, max_gross_allocation)
        self._calendar = calendar or XNYSCalendarPolicy()
        self._schedules: dict[dt.date, Optional[SessionSchedule]] = {}
        self.reset(0.0)

    def reset(self, equity: float) -> None:
        self._high_water_mark = equity
        self._session_date: Optional[dt.date] = None
        self._session_start_equity = equity

    def mark(self, ts, equity: float) -> None:
        schedule = self._schedule(ts)
        session_date = schedule.session_date if schedule is not None else None
        if session_date != self._session_date:
            self._session_date = session_date
            self._session_start_equity = equity
        self._high_water_mark = max(self._high_water_mark, equity)

    def entry_block_reason(self, *, ts, conid: int, order_notional: float,
                           position_values: Mapping[int, float],
                           equity: float) -> Optional[str]:
        now = _as_utc(ts)
        schedule = self._schedule(now)
        if schedule is None or not self._calendar.allows_new_entry(now, schedule):
            return 'ENTRY_WINDOW'
        if equity <= 0:
            return 'EQUITY_INVALID'
        if (self._session_start_equity - equity) / equity >= MAX_DAILY_LOSS_FRACTION:
            return 'DAILY_LOSS'
        if (self._high_water_mark > 0
                and (self._high_water_mark - equity) / self._high_water_mark >= MAX_DRAWDOWN_FRACTION):
            return 'DRAWDOWN'
        open_conids = {c for c, value in position_values.items() if value > 0}
        if conid not in open_conids and len(open_conids) >= MAX_POSITIONS:
            return 'MAX_POSITIONS'
        if (position_values.get(conid, 0.0) + order_notional) / equity > MAX_POSITION_FRACTION:
            return 'POSITION_PCT'
        if (sum(position_values.values()) + order_notional) / equity > self._gross_cap:
            return 'GROSS'
        return None

    def flatten_due(self, ts) -> bool:
        now = _as_utc(ts)
        schedule = self._schedule(now)
        return schedule is not None and now >= schedule.flatten_start_utc

    def _schedule(self, ts) -> Optional[SessionSchedule]:
        now = _as_utc(ts)
        key = now.astimezone(ET).date()
        if key not in self._schedules:
            self._schedules[key] = self._calendar.resolve(now)
        return self._schedules[key]
```

- [ ] **Step 4: Wire it into the backtester**

In `trader/simulation/backtester.py`:

1. Imports (top): add `from typing import TYPE_CHECKING` to the typing import line, and

```python
if TYPE_CHECKING:
    from trader.simulation.live_rules import PaperAutomationRules
```

2. `BacktestConfig`, after `order_notional`:

```python
    # Live paper-automation entry gates and end-of-day flatten. Evidence runs
    # set it so a backtest cannot take trades paper trading would refuse.
    live_rules: Optional['PaperAutomationRules'] = None
```

3. `BacktestResult`, after `applied_params`:

```python
    # Entries the live rules refused, counted by reason code.
    live_rule_blocks: Dict[str, int] = field(default_factory=dict)
```

4. In `run()`, right after `cash = self.config.initial_capital`:

```python
        live_rules = self.config.live_rules
        live_rule_blocks: Dict[str, int] = {}
        last_equity = self.config.initial_capital
        if live_rules is not None:
            live_rules.reset(last_equity)
```

5. In `_execute_signal`, at the start of the `if signal.action == Action.BUY:` branch, before `cost = ...`:

```python
                if live_rules is not None:
                    position_values = {c: q * last_prices.get(c, 0.0) for c, q in positions.items()}
                    block = live_rules.entry_block_reason(
                        ts=bar_ts, conid=conid, order_notional=quantity * fill_price,
                        position_values=position_values,
                        equity=cash + sum(position_values.values()))
                    if block is not None:
                        live_rule_blocks[block] = live_rule_blocks.get(block, 0) + 1
                        return
```

6. In the `for timestamp, group in combined.groupby(combined.index):` loop, right after `bars_this_ts` is built and before step "1. Execute any signals queued":

```python
            # 0. Live paper automation: track session equity, flatten before the close.
            if live_rules is not None:
                live_rules.mark(timestamp, last_equity)
                if positions and live_rules.flatten_due(timestamp):
                    for conid in list(positions):
                        bar = bars_this_ts.get(conid)
                        if bar is None or bar.empty or conid not in accumulated:
                            continue
                        flatten = Signal(source_name='__flatten__', action=Action.SELL,
                                         probability=1.0, risk=0.0, quantity=positions[conid])
                        _execute_signal(flatten, conid, timestamp, timestamp,
                                        accumulated[conid].iloc[-1], float(bar['open'].iloc[0]))
                    pending_signals = [p for p in pending_signals if p[0].action != Action.BUY]
```

7. In step "4. Track equity", right after `portfolio_value` is fully computed, add `last_equity = portfolio_value`.

8. In the `BacktestResult(...)` construction, add `live_rule_blocks=dict(live_rule_blocks),`.

- [ ] **Step 5: Run the tests**

Run: `$PY -m pytest tests/test_live_rules.py tests/test_backtester_sizing.py tests/test_backtester_costs.py tests/test_backtest_metrics.py tests/test_backtest_time_exits.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Full suite, then commit**

Run the full suite (Global Constraints). Expected: no failures.

```bash
git add trader/simulation/live_rules.py trader/simulation/backtester.py tests/test_live_rules.py
git commit -m "feat(backtest): mirror live paper-automation entry gates, caps and flatten" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Venue calendar, strategy paths and the evaluation spec

**Files:**
- Modify: `trader/simulation/execution_costs.py` (`Venue.calendar`, parser), `config_defaults/execution_costs.yaml`
- Create: `trader/research/strategy_paths.py`, `trader/research/evaluation_spec.py`
- Test: `tests/test_execution_costs.py` (append), `tests/research/test_strategy_paths.py`, `tests/research/test_evaluation_spec.py` (new)

**Interfaces:**
- Consumes: `build_realistic_costs`, `ExecutionCostsConfig`, `ExecutionCostError` (`trader/simulation/execution_costs.py`); `BarSize.parse_str` (`trader/objects.py`).
- Produces:
  - `Venue.calendar: Optional[str] = None`.
  - `strategy_paths.repo_root() -> Path`; `normalize_strategy_path(path: str, root: Path | None = None) -> str`; `resolve_strategy_file(module: str, strategies_dir: str) -> Path`.
  - `class EvaluationSpecError(ValueError)`.
  - `@dataclass(frozen=True) class EvaluationSpec` with fields `name: str, strategy_path: str, strategy_file: Path, class_name: str, params: Mapping[str, Any], neighbourhood: Mapping[str, tuple], conids: tuple[int, ...], bar_size: str, period_start: dt.date, period_end: dt.date, folds: int, embargo_sessions: int, holdout_sessions: int, order_notional: float, account_equity: float, max_gross_allocation: float, calendar: str`.
  - `load_evaluation_spec(path: str | Path, *, universe_accessor, costs_config: ExecutionCostsConfig, repo_root: Path) -> EvaluationSpec`.
  - `neighbour_points(spec: EvaluationSpec) -> list[dict]` (one key changed at a time, in declared order).
  - `MIN_INSTRUMENTS = 8`, `LIVE_CALENDARS = ('XNYS',)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_execution_costs.py`:

```python
def test_venue_calendar_is_parsed():
    with_calendar = {**CONFIG, 'venues': {'us': {**CONFIG['venues']['us'], 'calendar': 'XNYS'}}}
    assert parse_execution_costs_config(with_calendar).venue_for('NASDAQ').calendar == 'XNYS'


def test_bundled_template_declares_calendars():
    config_path = Path(__file__).resolve().parent.parent / 'config_defaults' / 'execution_costs.yaml'
    config = load_execution_costs_config(str(config_path))
    assert config.venue_for('NASDAQ').calendar == 'XNYS'
    assert config.venue_for('ASX').calendar == 'XASX'
```

Create `tests/research/test_strategy_paths.py`:

```python
from pathlib import Path

from trader.research.strategy_paths import normalize_strategy_path, repo_root


def test_relative_path_is_kept():
    assert normalize_strategy_path('./strategies/orb.py') == 'strategies/orb.py'


def test_absolute_path_under_the_repo_becomes_relative():
    absolute = repo_root() / 'strategies' / 'orb.py'
    assert normalize_strategy_path(str(absolute)) == 'strategies/orb.py'


def test_absolute_path_from_another_install_keeps_the_strategies_suffix():
    assert normalize_strategy_path('/home/trader/mmr/strategies/orb.py') == 'strategies/orb.py'


def test_root_override(tmp_path: Path):
    assert normalize_strategy_path(str(tmp_path / 'strategies' / 'x.py'), tmp_path) == 'strategies/x.py'
```

Create `tests/research/test_evaluation_spec.py`:

```python
"""Evaluation spec validation: every refusal names the field."""
from pathlib import Path

import pytest
import yaml

from tests.test_execution_costs import CONFIG, _definition
from trader.data.universe import UniverseAccessor
from trader.research.evaluation_spec import (
    EvaluationSpecError,
    load_evaluation_spec,
    neighbour_points,
)
from trader.simulation.execution_costs import parse_execution_costs_config

US_CONIDS = list(range(1001, 1009))
STRATEGY = '''
from trader.trading.strategy import Strategy

class Trend(Strategy):
    FAST = 10
    SLOW = 30

    def on_prices(self, prices):
        return None
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / 'strategies').mkdir()
    (tmp_path / 'strategies' / 'trend.py').write_text(STRATEGY)
    return tmp_path


@pytest.fixture
def accessor(tmp_duckdb_path):
    universes = UniverseAccessor(tmp_duckdb_path, 'Universes')
    for conid in US_CONIDS:
        universes.insert('us', _definition(conid, f'S{conid}', 'NASDAQ'))
    universes.insert('asx', _definition(2001, 'BHP', 'ASX'))
    return universes


def _costs():
    venues = {'us': {**CONFIG['venues']['us'], 'calendar': 'XNYS'},
              'asx': {**CONFIG['venues']['asx'], 'calendar': 'XASX'}}
    return parse_execution_costs_config({**CONFIG, 'venues': venues})


def _spec(**overrides):
    base = {
        'name': 'trend_us',
        'strategy': 'strategies/trend.py',
        'class': 'Trend',
        'params': {'FAST': 10, 'SLOW': 30},
        'neighbourhood': {'FAST': [5, 15], 'SLOW': [20, 40]},
        'conids': US_CONIDS,
        'bar_size': '5 mins',
        'period': {'start': '2024-02-01', 'end': '2024-03-28'},
        'walk_forward': {'folds': 2, 'embargo_sessions': 1, 'holdout_sessions': 5},
        'sizing': {'order_notional': 1900, 'account_equity': 100000},
        'max_gross_allocation': 0.05,
    }
    base.update(overrides)
    return base


def _load(repo, accessor, raw):
    path = repo / 'spec.yaml'
    path.write_text(yaml.safe_dump(raw))
    return load_evaluation_spec(path, universe_accessor=accessor, costs_config=_costs(), repo_root=repo)


def test_valid_spec_loads(repo, accessor):
    spec = _load(repo, accessor, _spec())
    assert spec.strategy_path == 'strategies/trend.py'
    assert spec.calendar == 'XNYS'
    assert spec.conids == tuple(US_CONIDS)


def test_neighbour_points_change_one_key_at_a_time(repo, accessor):
    spec = _load(repo, accessor, _spec())
    assert neighbour_points(spec) == [
        {'FAST': 5, 'SLOW': 30}, {'FAST': 15, 'SLOW': 30},
        {'FAST': 10, 'SLOW': 20}, {'FAST': 10, 'SLOW': 40},
    ]


@pytest.mark.parametrize('overrides, field', [
    ({'conids': US_CONIDS[:7]}, 'conids'),
    ({'conids': US_CONIDS + [999]}, '999'),
    ({'conids': US_CONIDS[:7] + [2001]}, 'one market'),
    ({'strategy': '../outside.py'}, 'strategies/'),
    ({'class': 'Missing'}, 'Missing'),
    ({'params': {'FAST': 10, 'slow': 30}}, 'slow'),
    ({'neighbourhood': {'FAST': [10]}}, 'FAST'),
    ({'neighbourhood': {}}, 'neighbourhood'),
    ({'neighbourhood': {'MEDIUM': [1]}}, 'MEDIUM'),
    ({'sizing': {'order_notional': 0, 'account_equity': 100000}}, 'order_notional'),
    ({'max_gross_allocation': 1.5}, 'max_gross_allocation'),
    ({'bar_size': '7 parsecs'}, 'bar_size'),
    ({'period': {'start': '2024-03-28', 'end': '2024-02-01'}}, 'period'),
])
def test_invalid_spec_is_refused(repo, accessor, overrides, field):
    with pytest.raises(EvaluationSpecError, match=field):
        _load(repo, accessor, _spec(**overrides))


def test_non_xnys_market_is_refused(repo, accessor):
    asx = [2001 + i for i in range(8)]
    for conid in asx[1:]:
        accessor.insert('asx', _definition(conid, f'A{conid}', 'ASX'))
    with pytest.raises(EvaluationSpecError, match='XNYS'):
        _load(repo, accessor, _spec(conids=asx))


def test_venue_without_calendar_names_the_key(repo, accessor):
    path = repo / 'spec.yaml'
    path.write_text(yaml.safe_dump(_spec()))
    with pytest.raises(EvaluationSpecError, match='calendar'):
        load_evaluation_spec(path, universe_accessor=accessor,
                             costs_config=parse_execution_costs_config(CONFIG), repo_root=repo)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `$PY -m pytest tests/test_execution_costs.py tests/research/test_strategy_paths.py tests/research/test_evaluation_spec.py -q -p no:cacheprovider`
Expected: the calendar tests fail (`AttributeError: 'Venue' object has no attribute 'calendar'`); the two new files fail at import.

- [ ] **Step 3: Add the venue calendar**

In `trader/simulation/execution_costs.py`, `Venue` gains a last field `calendar: Optional[str] = None`; in `parse_execution_costs_config`, pass `calendar=spec.get('calendar')` to `Venue(...)`. In `config_defaults/execution_costs.yaml`, add `calendar: XNYS` under `us:` and `calendar: XASX` under `asx:` (right after `primary_exchanges`), and add to the header comment: `# calendar: the exchange session calendar; research evaluation needs it.`

- [ ] **Step 4: Create `trader/research/strategy_paths.py`**

```python
"""One strategy file, one identity: repo-relative strategy paths.

Sweeps store absolute paths (often from inside the container), research
families store what was typed. Comparing them raw would split one strategy's
trial history into several.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Optional


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def normalize_strategy_path(path: str, root: Optional[Path] = None) -> str:
    raw = os.path.normpath(os.path.expanduser(str(path)))
    candidate = Path(raw)
    if not candidate.is_absolute():
        return PurePosixPath(*candidate.parts).as_posix()
    base = (root or repo_root()).resolve()
    try:
        return candidate.resolve().relative_to(base).as_posix()
    except ValueError:
        parts = candidate.parts
        if 'strategies' in parts:
            start = len(parts) - 1 - parts[::-1].index('strategies')
            return '/'.join(parts[start:])
        return candidate.as_posix()


def resolve_strategy_file(module: str, strategies_dir: str) -> Path:
    requested = Path(os.path.expanduser(module))
    if requested.is_absolute():
        return requested
    in_strategies_dir = Path(os.path.expanduser(strategies_dir)).resolve() / requested
    if in_strategies_dir.exists():
        return in_strategies_dir
    return (repo_root() / requested).resolve()
```

- [ ] **Step 5: Create `trader/research/evaluation_spec.py`**

```python
"""The YAML spec behind `mmr research evaluate`, loaded and validated.

Every refusal raises ``EvaluationSpecError`` naming the field, so a bad spec
fails before any backtest runs.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from trader.objects import BarSize
from trader.research.strategy_paths import normalize_strategy_path
from trader.simulation.execution_costs import (
    ExecutionCostError,
    ExecutionCostsConfig,
    build_realistic_costs,
)

MIN_INSTRUMENTS = 8
# Calendars with a live paper-automation calendar policy (calendar_policy.py).
LIVE_CALENDARS = ('XNYS',)


class EvaluationSpecError(ValueError):
    """The evaluation spec is invalid; the message names the field."""


@dataclass(frozen=True)
class EvaluationSpec:
    name: str
    strategy_path: str
    strategy_file: Path
    class_name: str
    params: Mapping[str, Any]
    neighbourhood: Mapping[str, tuple]
    conids: tuple[int, ...]
    bar_size: str
    period_start: dt.date
    period_end: dt.date
    folds: int
    embargo_sessions: int
    holdout_sessions: int
    order_notional: float
    account_equity: float
    max_gross_allocation: float
    calendar: str


def neighbour_points(spec: EvaluationSpec) -> list[dict]:
    points = []
    for key, values in spec.neighbourhood.items():
        for value in values:
            points.append({**spec.params, key: value})
    return points


def _require(raw: Mapping[str, Any], key: str) -> Any:
    if key not in raw or raw[key] in (None, '', [], {}):
        raise EvaluationSpecError(f'{key}: required')
    return raw[key]


def _date(value: Any, field: str) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise EvaluationSpecError(f'{field}: not an ISO date: {value!r}') from exc


def _positive_number(raw: Mapping[str, Any], key: str, section: str) -> float:
    value = raw.get(key)
    if not isinstance(value, (int, float)) or value <= 0:
        raise EvaluationSpecError(f'{section}.{key}: must be a positive number, got {value!r}')
    return float(value)


def _tunables(strategy_file: Path, class_name: str) -> set[str]:
    module_spec = importlib.util.spec_from_file_location(f'_mmr_eval_{strategy_file.stem}', strategy_file)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    cls = getattr(module, class_name, None)
    if cls is None:
        raise EvaluationSpecError(f'class: {class_name} not found in {strategy_file.name}')
    return {name for name in dir(cls) if name.isupper() and not name.startswith('_')}


def _strategy_file(raw_path: str, repo_root: Path) -> tuple[str, Path]:
    strategies_dir = (repo_root / 'strategies').resolve()
    file = (repo_root / raw_path).resolve()
    if strategies_dir not in file.parents:
        raise EvaluationSpecError(f'strategy: {raw_path} must be inside strategies/')
    if not file.is_file():
        raise EvaluationSpecError(f'strategy: {raw_path} does not exist')
    return normalize_strategy_path(str(file), repo_root), file


def _calendar(conids: tuple[int, ...], universe_accessor: Any,
              costs_config: ExecutionCostsConfig) -> str:
    try:
        costs = build_realistic_costs(conids, universe_accessor, costs_config)
    except ExecutionCostError as exc:
        raise EvaluationSpecError(f'conids: {exc}') from exc
    venues = {venue.name: venue for venue in costs.venue_by_conid.values()}
    if len(venues) != 1:
        raise EvaluationSpecError(
            f'conids: one market per evaluation; got venues {sorted(venues)}')
    venue = next(iter(venues.values()))
    if not venue.calendar:
        raise EvaluationSpecError(
            f"conids: venue {venue.name!r} in execution_costs.yaml has no calendar; "
            f"add `calendar: XNYS` (or the venue's exchange calendar)")
    if venue.calendar not in LIVE_CALENDARS:
        raise EvaluationSpecError(
            f'conids: venue calendar {venue.calendar} has no live paper-automation policy; '
            f'paper automation only runs on {", ".join(LIVE_CALENDARS)}')
    return venue.calendar


def load_evaluation_spec(path: str | Path, *, universe_accessor: Any,
                         costs_config: ExecutionCostsConfig, repo_root: Path) -> EvaluationSpec:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    name = str(_require(raw, 'name'))
    strategy_path, strategy_file = _strategy_file(str(_require(raw, 'strategy')), repo_root)
    class_name = str(_require(raw, 'class'))
    tunables = _tunables(strategy_file, class_name)

    params = dict(_require(raw, 'params'))
    unknown = sorted(k for k in params if k not in tunables)
    if unknown:
        raise EvaluationSpecError(f'params: not upper-case tunables of {class_name}: {unknown}')

    neighbourhood_raw = raw.get('neighbourhood') or {}
    if not neighbourhood_raw:
        raise EvaluationSpecError('neighbourhood: required (adjacent values for at least one param)')
    neighbourhood = {}
    for key, values in neighbourhood_raw.items():
        if key not in params:
            raise EvaluationSpecError(f'neighbourhood: {key} is not one of params')
        values = tuple(values or ())
        if not values or any(v == params[key] for v in values):
            raise EvaluationSpecError(
                f'neighbourhood: {key} needs values different from the main value {params[key]!r}')
        neighbourhood[key] = values

    conids = tuple(int(c) for c in _require(raw, 'conids'))
    if len(set(conids)) < MIN_INSTRUMENTS:
        raise EvaluationSpecError(f'conids: need at least {MIN_INSTRUMENTS} distinct conids, got {len(set(conids))}')
    calendar = _calendar(conids, universe_accessor, costs_config)

    bar_size = str(_require(raw, 'bar_size'))
    try:
        BarSize.parse_str(bar_size)
    except Exception as exc:
        raise EvaluationSpecError(f'bar_size: {bar_size!r} is not a valid bar size') from exc

    period = _require(raw, 'period')
    period_start = _date(period.get('start'), 'period.start')
    period_end = _date(period.get('end'), 'period.end')
    if period_start >= period_end:
        raise EvaluationSpecError('period: start must be before end')

    walk_forward = _require(raw, 'walk_forward')
    folds = int(walk_forward.get('folds', 0))
    embargo = int(walk_forward.get('embargo_sessions', -1))
    holdout = int(walk_forward.get('holdout_sessions', 0))
    if folds < 1 or embargo < 0 or holdout < 1:
        raise EvaluationSpecError(
            'walk_forward: folds >= 1, embargo_sessions >= 0 and holdout_sessions >= 1 required')

    sizing = _require(raw, 'sizing')
    order_notional = _positive_number(sizing, 'order_notional', 'sizing')
    account_equity = _positive_number(sizing, 'account_equity', 'sizing')
    max_gross_allocation = raw.get('max_gross_allocation')
    if not isinstance(max_gross_allocation, (int, float)) or not 0 < max_gross_allocation <= 1:
        raise EvaluationSpecError(f'max_gross_allocation: must be in (0, 1], got {max_gross_allocation!r}')

    return EvaluationSpec(
        name=name, strategy_path=strategy_path, strategy_file=strategy_file,
        class_name=class_name, params=params, neighbourhood=neighbourhood,
        conids=conids, bar_size=bar_size, period_start=period_start,
        period_end=period_end, folds=folds, embargo_sessions=embargo,
        holdout_sessions=holdout, order_notional=order_notional,
        account_equity=account_equity, max_gross_allocation=float(max_gross_allocation),
        calendar=calendar,
    )
```

Remove the unused `itertools` import if your linter flags it (it is not needed).

- [ ] **Step 6: Run the tests**

Run: `$PY -m pytest tests/test_execution_costs.py tests/research/test_strategy_paths.py tests/research/test_evaluation_spec.py -q -p no:cacheprovider`
Expected: all pass. If `BarSize.parse_str('7 parsecs')` does not raise, add an explicit check that `str(BarSize.parse_str(bar_size))` round-trips, and keep the test.

- [ ] **Step 7: Commit**

```bash
git add trader/simulation/execution_costs.py config_defaults/execution_costs.yaml trader/research/strategy_paths.py trader/research/evaluation_spec.py tests/test_execution_costs.py tests/research/test_strategy_paths.py tests/research/test_evaluation_spec.py
git commit -m "feat(research): load and validate research evaluation specs" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Evidence maths and the pre-holdout gate

**Files:**
- Create: `trader/research/evidence.py`
- Test: `tests/research/test_evidence.py` (new)

**Interfaces:**
- Consumes: `RoundTrip` (`trader/research/attribution.py`: `conid, open_time, close_time, quantity, entry_price, exit_price, pnl`); `statistics.profit_concentration`, `statistics.selection_adjusted_confidence`; `paper_v1.CANARY_DRAWDOWN_LIMIT`; `EligibilityDecision` (`.results` of `RuleResult(code, passed, observed, ...)`).
- Produces (all pure):
  - `HOLDOUT_STAGE_RULES: frozenset[str]`, `NEIGHBOURHOOD_MIN_POSITIVE_SHARE = 2/3`, `TRADING_DAYS_PER_YEAR = 252`.
  - `dollar_weighted_expectancy_bps(round_trips) -> Optional[float]`
  - `session_returns(equity_curves: Sequence[pd.Series], *, starting_equity: float) -> np.ndarray`
  - `per_period_sharpe(returns) -> Optional[float]`
  - `positive_fold_fraction(fold_net_pnls: Sequence[float]) -> Optional[float]`
  - `month_concentration(round_trips) -> Optional[float]`, `instrument_concentration(round_trips) -> Optional[float]`
  - `neighbourhood_robust(neighbour_expectancies: Sequence[Optional[float]]) -> Optional[bool]`
  - `combined_trace_signature(signatures: Sequence[str]) -> str` (64 lower-case hex)
  - `selection_confidence(returns, *, n_trials: int, trial_sharpes: Sequence[float]) -> Optional[float]`
  - `holdout_passes(round_trips, max_drawdown: float) -> bool`
  - `@dataclass(frozen=True) class GateOutcome: passed: bool; failed: tuple[str, ...]; missing: tuple[str, ...]`
  - `pre_holdout_outcome(decision) -> GateOutcome`; `final_outcome(decision) -> GateOutcome`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run them and watch them fail**

Run: `$PY -m pytest tests/research/test_evidence.py -q -p no:cacheprovider`
Expected: import error for `trader.research.evidence`.

- [ ] **Step 3: Create `trader/research/evidence.py`**

```python
"""Pure evidence maths for `research evaluate` (spec section 3).

Every function takes plain values and returns a number, a bool, or ``None``
when the evidence cannot be computed. ``None`` fails its paper-v1 rule closed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from trader.research.attribution import RoundTrip
from trader.research.rulesets.paper_v1 import CANARY_DRAWDOWN_LIMIT
from trader.research.statistics import profit_concentration, selection_adjusted_confidence

HOLDOUT_STAGE_RULES = frozenset({
    'holdout_drawdown_within_canary',
    'deterministic_replay',
    'holdout_opened_once',
    'benchmark_relative_drawdown',
})
NEIGHBOURHOOD_MIN_POSITIVE_SHARE = 2 / 3
TRADING_DAYS_PER_YEAR = 252


def dollar_weighted_expectancy_bps(round_trips: Sequence[RoundTrip]) -> Optional[float]:
    notional = sum(rt.quantity * rt.entry_price for rt in round_trips)
    if notional <= 0:
        return None
    return sum(rt.pnl for rt in round_trips) / notional * 10_000


def session_returns(equity_curves: Sequence[pd.Series], *, starting_equity: float) -> np.ndarray:
    """Close-to-close returns per session, each fold starting from ``starting_equity``."""
    parts = []
    for curve in equity_curves:
        if curve is None or len(curve) == 0:
            continue
        closes = curve.groupby(curve.index.date).last().to_numpy(dtype=float)
        levels = np.concatenate([[starting_equity], closes])
        parts.append(np.diff(levels) / levels[:-1])
    return np.concatenate(parts) if parts else np.array([])


def _finite(values) -> np.ndarray:
    array = np.asarray(list(values), dtype=float)
    return array[np.isfinite(array)]


def per_period_sharpe(returns) -> Optional[float]:
    r = _finite(returns)
    if len(r) < 2:
        return None
    sd = r.std(ddof=1)
    return 0.0 if sd == 0 else float(r.mean() / sd)


def positive_fold_fraction(fold_net_pnls: Sequence[float]) -> Optional[float]:
    if not fold_net_pnls:
        return None
    return sum(1 for pnl in fold_net_pnls if pnl > 0) / len(fold_net_pnls)


def _concentration(round_trips: Sequence[RoundTrip], bucket) -> Optional[float]:
    pnl_by_bucket: dict = {}
    for rt in round_trips:
        key = bucket(rt)
        pnl_by_bucket[key] = pnl_by_bucket.get(key, 0.0) + rt.pnl
    return profit_concentration(pnl_by_bucket).max_share


def month_concentration(round_trips: Sequence[RoundTrip]) -> Optional[float]:
    return _concentration(round_trips, lambda rt: f'{rt.close_time:%Y-%m}')


def instrument_concentration(round_trips: Sequence[RoundTrip]) -> Optional[float]:
    return _concentration(round_trips, lambda rt: int(rt.conid))


def neighbourhood_robust(neighbour_expectancies: Sequence[Optional[float]]) -> Optional[bool]:
    if len(neighbour_expectancies) < 2:
        return None
    positive = sum(1 for e in neighbour_expectancies if e is not None and e > 0)
    return positive / len(neighbour_expectancies) >= NEIGHBOURHOOD_MIN_POSITIVE_SHARE


def combined_trace_signature(signatures: Sequence[str]) -> str:
    payload = json.dumps(list(signatures), separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def selection_confidence(returns, *, n_trials: int,
                         trial_sharpes: Sequence[float]) -> Optional[float]:
    r = _finite(returns)
    sharpe = per_period_sharpe(r)
    spread = _finite(trial_sharpes)
    if sharpe is None or len(spread) < 2 or n_trials < 1:
        return None
    return selection_adjusted_confidence(
        sharpe, n_trials=n_trials, trial_sharpe_std=float(spread.std(ddof=1)),
        n_obs=len(r), skew=float(scipy_stats.skew(r)),
        excess_kurtosis=float(scipy_stats.kurtosis(r)))


def holdout_passes(round_trips: Sequence[RoundTrip], max_drawdown: float) -> bool:
    expectancy = dollar_weighted_expectancy_bps(round_trips)
    return (bool(round_trips) and expectancy is not None and expectancy > 0
            and abs(max_drawdown) <= CANARY_DRAWDOWN_LIMIT)


@dataclass(frozen=True)
class GateOutcome:
    passed: bool
    failed: tuple[str, ...]
    missing: tuple[str, ...]


def _outcome(decision, *, skip: frozenset) -> GateOutcome:
    failed, missing = [], []
    for result in decision.results:
        if result.code in skip or result.passed:
            continue
        (missing if result.observed is None else failed).append(result.code)
    return GateOutcome(passed=not failed and not missing, failed=tuple(failed), missing=tuple(missing))


def pre_holdout_outcome(decision) -> GateOutcome:
    """Every rule except the holdout-stage ones must pass before the holdout opens."""
    return _outcome(decision, skip=HOLDOUT_STAGE_RULES)


def final_outcome(decision) -> GateOutcome:
    return _outcome(decision, skip=frozenset())
```

- [ ] **Step 4: Run the tests**

Run: `$PY -m pytest tests/research/test_evidence.py -q -p no:cacheprovider`
Expected: all pass. If `test_infinite_profit_factor_fails_closed_and_still_digests` fails on the digest, read `_encode_value` in `trader/research/eligibility.py`; if it cannot encode `inf`, map `profit_factor` `inf` to `None` in Task 8's evidence builder instead and change this test to assert the evidence builder output (keep the "fails closed" assertion).

- [ ] **Step 5: Commit**

```bash
git add trader/research/evidence.py tests/research/test_evidence.py
git commit -m "feat(research): add evidence maths and the pre-holdout gate" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 5: `reviewer_kind` on operator reviews (research migration 10)

**Files:**
- Modify: `trader/research/review.py`, `trader/research/bundle.py` (`_review_public`, review rebuild in `_validate_payload_bindings`), `trader/research/schema.py`
- Test: `tests/research/test_reviewer_kind.py` (new)

**Interfaces:**
- Produces:
  - `REVIEWER_KINDS = ('human', 'llm', 'unknown')`; `OperatorReview.reviewer_kind: str = 'unknown'` (last field).
  - The review digest includes `reviewer_kind` only when it is not `'unknown'`, so digests of reviews recorded before this change do not move.
  - `review_allowed_for(review: OperatorReview, account_mode: str) -> None` raises `ValueError` when `account_mode == 'live'` and `reviewer_kind != 'human'`.
  - `apply_reviewer_kind_migration(migrator)` — research migration 10: `ALTER TABLE operator_reviews ADD COLUMN IF NOT EXISTS reviewer_kind VARCHAR DEFAULT 'unknown'`.

- [ ] **Step 1: Write the failing tests**

```python
import datetime as dt

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.review import OperatorReview, OperatorReviewRepository, review_allowed_for
from trader.research.schema import apply_research_migrations

T0 = dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc)


def _review(**overrides):
    fields = dict(
        artifact_id='a1', eligibility_decision_digest='d1', reviewer='claude', reviewed_at=T0,
        economic_rationale='x', edge_survives_costs='x', known_failure_regimes='x',
        data_and_survivorship_limits='x', parameter_sensitivity='x',
        operational_dependencies='x', capacity_and_decay='x', episode_dominance='x',
        holdout_opened_once_confirmed=True)
    fields.update(overrides)
    return OperatorReview(**fields)


def test_unknown_kind_keeps_the_old_digest():
    assert 'reviewer_kind' not in _review()._body()


def test_kind_is_part_of_the_digest():
    assert _review(reviewer_kind='llm').digest != _review(reviewer_kind='human').digest


def test_invalid_kind_is_refused():
    with pytest.raises(ValueError, match='reviewer_kind'):
        _review(reviewer_kind='robot')


def test_kind_round_trips_through_the_repository(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / 'research.duckdb'))
    apply_research_migrations(SchemaMigrator(db))
    repo = OperatorReviewRepository(db)
    digest = repo.record(_review(reviewer_kind='llm'))
    assert repo.get(digest).reviewer_kind == 'llm'


def test_live_needs_a_human_review():
    review_allowed_for(_review(reviewer_kind='llm'), 'paper')
    review_allowed_for(_review(reviewer_kind='human'), 'live')
    with pytest.raises(ValueError, match='human'):
        review_allowed_for(_review(reviewer_kind='llm'), 'live')
```

- [ ] **Step 2: Run them and watch them fail**

Run: `$PY -m pytest tests/research/test_reviewer_kind.py -q -p no:cacheprovider`
Expected: `ImportError: cannot import name 'review_allowed_for'`.

- [ ] **Step 3: Implement in `trader/research/review.py`**

1. Constants next to `RESEARCH_MIGRATION_REVIEWS`:

```python
RESEARCH_MIGRATION_REVIEWER_KIND = 10
REVIEWER_KINDS = ("human", "llm", "unknown")
```

2. `OperatorReview`: add the last field `reviewer_kind: str = "unknown"`. In `__post_init__`, after the holdout check:

```python
        if self.reviewer_kind not in REVIEWER_KINDS:
            raise ValueError(
                f"OperatorReview.reviewer_kind must be one of {REVIEWER_KINDS}, "
                f"got {self.reviewer_kind!r}")
```

3. `_body`: build the dict as today into `body`, then

```python
        # Reviews recorded before reviewer_kind existed keep their digest.
        if self.reviewer_kind != "unknown":
            body["reviewer_kind"] = self.reviewer_kind
        return body
```

4. Migration (below `apply_review_migrations`):

```python
def apply_reviewer_kind_migration(migrator: SchemaMigrator) -> None:
    """Research DB migration 10: who wrote the review (human or llm)."""
    migrator.apply(version=RESEARCH_MIGRATION_REVIEWER_KIND,
                   name="research_operator_review_kind",
                   statements=["ALTER TABLE operator_reviews ADD COLUMN IF NOT EXISTS "
                               "reviewer_kind VARCHAR DEFAULT 'unknown'"])
```

5. `OperatorReviewRepository.record`: add `reviewer_kind` to the column list and `review.reviewer_kind` to the values. `get`: select `reviewer_kind` last and pass `reviewer_kind=r[13] or "unknown"`.

6. Below the repository:

```python
def review_allowed_for(review: OperatorReview, account_mode: str) -> None:
    """Paper accepts a human or an LLM review; live needs a human."""
    if account_mode == "live" and review.reviewer_kind != "human":
        raise ValueError(
            f"a live attestation needs a human review; this one is {review.reviewer_kind!r}")
```

- [ ] **Step 4: Register the migration and carry the field through bundles**

- `trader/research/schema.py`, at the end of `apply_research_migrations`:

```python
    from trader.research.review import apply_reviewer_kind_migration
    apply_reviewer_kind_migration(migrator)
```

- `trader/research/bundle.py`: in `_review_public`, add `"reviewer_kind": review.reviewer_kind`. In the `OperatorReview(...)` rebuild inside verification, add `reviewer_kind=review.get("reviewer_kind", "unknown")`.

- [ ] **Step 5: Run the research tests**

Run: `$PY -m pytest tests/research tests/automation -q -p no:cacheprovider`
Expected: all pass (bundle tests prove old reviews still verify).

- [ ] **Step 6: Commit**

```bash
git add trader/research/review.py trader/research/bundle.py trader/research/schema.py tests/research/test_reviewer_kind.py
git commit -m "feat(research): record whether a human or an llm wrote the operator review" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Strategy-level trial count and the evaluation log (research migration 11)

**Files:**
- Modify: `trader/research/experiment_registry.py` (`strategy_trials`, `family_artifacts`), `trader/research/schema.py`
- Create: `trader/research/evaluation_store.py`
- Test: `tests/research/test_strategy_trials.py`, `tests/research/test_evaluation_store.py` (new)

**Interfaces:**
- Consumes: `normalize_strategy_path` (Task 3).
- Produces:
  - `ExperimentRegistry.strategy_trials(strategy_path: str, class_name: str) -> list[TrialRecord]` — terminal trials of every family (research and legacy) of that strategy file and class; paths compared repo-relative.
  - `ExperimentRegistry.family_artifacts(family_id: str) -> list[ArtifactRecord]`.
  - `evaluation_store`: `STAGE_PRE_HOLDOUT = 'pre_holdout'`, `STAGE_HOLDOUT_FAILED = 'holdout_failed'`, `STAGE_COMPLETE = 'complete'`; `@dataclass(frozen=True) EvaluationRecord(spec_name, family_id, strategy_path, class_name, stage, state, artifact_id: Optional[str], decision_digest: Optional[str], failed_rules: tuple[str, ...], missing_rules: tuple[str, ...], report_path: str, created_at: dt.datetime, evaluation_id: str = <uuid4 hex>)` with `summary() -> str`; `EvaluationRepository(db)` with `record(rec) -> str`, `list(limit=50) -> list[EvaluationRecord]`, `latest_for_strategy(strategy_path, class_name) -> Optional[EvaluationRecord]`; `write_evaluation_summary(summaries_dir: Path, rec: EvaluationRecord) -> Path` (one JSON per strategy file + class, overwritten by the next run); `describe_latest_evaluation(summaries_dir: Path, strategy_path: str, class_name: str) -> str` (reads that file, never raises, never opens a DuckDB); `apply_evaluation_migrations(migrator)`.

- [ ] **Step 1: Write the failing tests**

`tests/research/test_strategy_trials.py`:

```python
import datetime as dt

from tests.test_backtest_store import _make_record
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.artifact import TRIAL_SUCCEEDED, ExperimentFamily
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.schema import apply_research_migrations
from trader.research.strategy_paths import repo_root

T0 = dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc)


def _registry(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / 'research.duckdb'))
    apply_research_migrations(SchemaMigrator(db))
    return ExperimentRegistry(db)


def _family(path, minutes):
    return ExperimentFamily(
        strategy_path=path, class_name='RSIStrategy', repository_commit='c',
        source_tree_digest='s', dependency_lock_digest='d', container_digest='x',
        dataset_manifest_digest='m', search_space={'MINUTES': [minutes]},
        cost_model={}, validation_protocol={})


def _trial(registry, family_id, key, finish=True):
    tid = registry.start_trial(family_id, trial_key=key, parameters={}, started_at=T0)
    if finish:
        registry.finish_trial(tid, status=TRIAL_SUCCEEDED, finished_at=T0, metrics={'daily_sharpe': 0.1})
    return tid


def test_trials_of_all_families_of_one_strategy_are_counted(tmp_path):
    registry = _registry(tmp_path)
    relative = registry.create_family(_family('strategies/rsi.py', 15), created_at=T0)
    absolute = registry.create_family(
        _family(str(repo_root() / 'strategies' / 'rsi.py'), 30), created_at=T0)
    other = registry.create_family(_family('strategies/other.py', 15), created_at=T0)
    _trial(registry, relative, 'a')
    _trial(registry, absolute, 'b')
    _trial(registry, absolute, 'running', finish=False)
    _trial(registry, other, 'c')

    trials = registry.strategy_trials('./strategies/rsi.py', 'RSIStrategy')

    assert sorted(t.trial_key for t in trials) == ['a', 'b']


def test_legacy_imported_backtests_count(tmp_path):
    registry = _registry(tmp_path)
    record = _make_record()
    record.id = 7
    registry.import_legacy_backtests([record], imported_at=T0)
    assert len(registry.strategy_trials('/home/trader/mmr/strategies/rsi.py', 'RSIStrategy')) == 1


def test_family_artifacts(tmp_path):
    registry = _registry(tmp_path)
    fid = registry.create_family(_family('strategies/rsi.py', 15), created_at=T0)
    tid = _trial(registry, fid, 'a')
    aid = registry.seal_artifact(fid, selected_trial_id=tid, selected_parameters={}, sealed_at=T0)
    assert [a.artifact_id for a in registry.family_artifacts(fid)] == [aid]
```

`tests/research/test_evaluation_store.py`:

```python
import datetime as dt

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.evaluation_store import (
    STAGE_PRE_HOLDOUT, EvaluationRecord, EvaluationRepository, describe_latest_evaluation,
    write_evaluation_summary,
)
from trader.research.schema import apply_research_migrations


def _db(path):
    db = DuckDBConnection.get_instance(str(path))
    apply_research_migrations(SchemaMigrator(db))
    return db


def _record(day, **overrides):
    fields = dict(
        spec_name='orb_us', family_id='f', strategy_path='strategies/orb.py',
        class_name='OpeningRangeBreakout', stage=STAGE_PRE_HOLDOUT, state='CANDIDATE',
        artifact_id=None, decision_digest=None, failed_rules=('min_round_trips',),
        missing_rules=('liquidity_capacity_envelope',), report_path='/tmp/r.md',
        created_at=dt.datetime(2026, 10, day, tzinfo=dt.timezone.utc))
    fields.update(overrides)
    return EvaluationRecord(**fields)


def test_latest_for_strategy_matches_normalised_paths(tmp_path):
    repo = EvaluationRepository(_db(tmp_path / 'research.duckdb'))
    repo.record(_record(1))
    repo.record(_record(3, spec_name='orb_us_v2'))
    latest = repo.latest_for_strategy('/somewhere/strategies/orb.py', 'OpeningRangeBreakout')
    assert latest.spec_name == 'orb_us_v2'
    assert latest.failed_rules == ('min_round_trips',)


def test_summary_file_describes_the_latest_run(tmp_path):
    summaries = tmp_path / 'evaluations'
    assert 'no evaluation' in describe_latest_evaluation(summaries, 'strategies/orb.py', 'X')
    write_evaluation_summary(summaries, _record(2))
    text = describe_latest_evaluation(summaries, '/elsewhere/strategies/orb.py', 'OpeningRangeBreakout')
    assert 'pre_holdout' in text and 'liquidity_capacity_envelope' in text


def test_unreadable_summary_does_not_raise(tmp_path):
    summaries = tmp_path / 'evaluations'
    path = write_evaluation_summary(summaries, _record(2))
    path.write_text('{not json')
    assert 'unavailable' in describe_latest_evaluation(summaries, 'strategies/orb.py', 'OpeningRangeBreakout')
```

- [ ] **Step 2: Run them and watch them fail**

Run: `$PY -m pytest tests/research/test_strategy_trials.py tests/research/test_evaluation_store.py -q -p no:cacheprovider`
Expected: `AttributeError: 'ExperimentRegistry' object has no attribute 'strategy_trials'` and an import error for `evaluation_store`.

- [ ] **Step 3: Add the registry queries** (`trader/research/experiment_registry.py`, after `selection_trial_count`; add `from trader.research.strategy_paths import normalize_strategy_path` to the imports)

```python
    def strategy_trials(self, strategy_path: str, class_name: str) -> list[TrialRecord]:
        """Terminal trials of EVERY family of one strategy file + class: the
        multiple-testing denominator across families. A new family per param
        tweak must not reset it, so paths compare repo-relative."""
        target = normalize_strategy_path(strategy_path)

        def _tx(conn):
            families = conn.execute(
                "SELECT family_id, strategy_path FROM experiment_families WHERE class_name = ?",
                [class_name]).fetchall()
            family_ids = [fid for fid, path in families
                          if normalize_strategy_path(path) == target]
            trials: list[TrialRecord] = []
            for fid in family_ids:
                rows = conn.execute(
                    "SELECT trial_id, family_id, trial_key, parameters, status, started_at, "
                    "finished_at, traceback_digest, safe_summary, archived "
                    "FROM experiment_trials WHERE family_id = ? AND status != ? "
                    "ORDER BY started_at, trial_key", [fid, TRIAL_RUNNING]).fetchall()
                trials.extend(self._row_to_trial(conn, r[0], r[1:]) for r in rows)
            return trials

        return self._db.transaction(_tx)

    def family_artifacts(self, family_id: str) -> list[ArtifactRecord]:
        def _tx(conn):
            return [r[0] for r in conn.execute(
                "SELECT artifact_id FROM strategy_artifacts WHERE family_id = ? "
                "ORDER BY sealed_at", [family_id]).fetchall()]

        return [self.get_artifact(aid) for aid in self._db.transaction(_tx)]
```

- [ ] **Step 4: Create `trader/research/evaluation_store.py`**

```python
"""Every `research evaluate` run, recorded (research migration 11).

Activate reads the latest row for a strategy to explain a refusal.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from trader.data.schema_migrations import SchemaMigrator
from trader.research.strategy_paths import normalize_strategy_path

RESEARCH_MIGRATION_EVALUATIONS = 11
STAGE_PRE_HOLDOUT = "pre_holdout"
STAGE_HOLDOUT_FAILED = "holdout_failed"
STAGE_COMPLETE = "complete"

_COLUMNS = ("evaluation_id, spec_name, family_id, strategy_path, class_name, stage, state, "
            "artifact_id, decision_digest, failed_rules, missing_rules, report_path, created_at")
_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS research_evaluations (
        evaluation_id VARCHAR PRIMARY KEY,
        spec_name VARCHAR NOT NULL,
        family_id VARCHAR NOT NULL,
        strategy_path VARCHAR NOT NULL,
        class_name VARCHAR NOT NULL,
        stage VARCHAR NOT NULL,
        state VARCHAR NOT NULL,
        artifact_id VARCHAR,
        decision_digest VARCHAR,
        failed_rules VARCHAR NOT NULL,
        missing_rules VARCHAR NOT NULL,
        report_path VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
)
NO_EVALUATION = "no evaluation found; run `mmr research evaluate <spec.yaml>`"


def apply_evaluation_migrations(migrator: SchemaMigrator) -> None:
    migrator.apply(version=RESEARCH_MIGRATION_EVALUATIONS, name="research_evaluations",
                   statements=list(_STATEMENTS))


@dataclass(frozen=True)
class EvaluationRecord:
    spec_name: str
    family_id: str
    strategy_path: str
    class_name: str
    stage: str
    state: str
    artifact_id: Optional[str]
    decision_digest: Optional[str]
    failed_rules: tuple[str, ...]
    missing_rules: tuple[str, ...]
    report_path: str
    created_at: dt.datetime
    evaluation_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def summary(self) -> str:
        parts = [f"latest evaluation {self.spec_name!r}: {self.state} at stage {self.stage}"]
        if self.failed_rules:
            parts.append("failed " + ", ".join(self.failed_rules))
        if self.missing_rules:
            parts.append("missing " + ", ".join(self.missing_rules))
        parts.append(f"report {self.report_path}")
        return "; ".join(parts)


def _row_to_record(r) -> EvaluationRecord:
    return EvaluationRecord(
        evaluation_id=r[0], spec_name=r[1], family_id=r[2], strategy_path=r[3],
        class_name=r[4], stage=r[5], state=r[6], artifact_id=r[7], decision_digest=r[8],
        failed_rules=tuple(json.loads(r[9])), missing_rules=tuple(json.loads(r[10])),
        report_path=r[11], created_at=r[12])


class EvaluationRepository:
    def __init__(self, db: Any):
        self._db = db

    def record(self, rec: EvaluationRecord) -> str:
        def _tx(conn):
            conn.execute(
                f"INSERT INTO research_evaluations ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [rec.evaluation_id, rec.spec_name, rec.family_id,
                 normalize_strategy_path(rec.strategy_path), rec.class_name, rec.stage,
                 rec.state, rec.artifact_id, rec.decision_digest,
                 json.dumps(list(rec.failed_rules)), json.dumps(list(rec.missing_rules)),
                 rec.report_path, rec.created_at])
            return rec.evaluation_id

        return self._db.transaction(_tx)

    def list(self, limit: int = 50) -> list[EvaluationRecord]:
        def _tx(conn):
            return conn.execute(
                f"SELECT {_COLUMNS} FROM research_evaluations "
                "ORDER BY created_at DESC LIMIT ?", [limit]).fetchall()

        return [_row_to_record(r) for r in self._db.transaction(_tx)]

    def latest_for_strategy(self, strategy_path: str,
                            class_name: str) -> Optional[EvaluationRecord]:
        target = normalize_strategy_path(strategy_path)

        def _tx(conn):
            return conn.execute(
                f"SELECT {_COLUMNS} FROM research_evaluations WHERE class_name = ? "
                "ORDER BY created_at DESC", [class_name]).fetchall()

        for row in self._db.transaction(_tx):
            if normalize_strategy_path(row[3]) == target:
                return _row_to_record(row)
        return None


def _summary_path(summaries_dir: Path, strategy_path: str, class_name: str) -> Path:
    key = normalize_strategy_path(strategy_path).replace("/", "__")
    return Path(summaries_dir) / f"{key}__{class_name}.json"


def write_evaluation_summary(summaries_dir: Path, rec: EvaluationRecord) -> Path:
    """The latest evaluation for one strategy file + class, as a plain file.

    trader_service must never open the research DuckDB (trader/config.py), so
    Activate explains a refusal from this file instead.
    """
    path = _summary_path(summaries_dir, rec.strategy_path, rec.class_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"summary": rec.summary(), "family_id": rec.family_id,
                                "stage": rec.stage, "state": rec.state,
                                "created_at": rec.created_at.isoformat()}, indent=2))
    return path


def describe_latest_evaluation(summaries_dir: Path, strategy_path: str,
                               class_name: str) -> str:
    """One line for refusal messages. Best effort: it never raises."""
    path = _summary_path(summaries_dir, strategy_path, class_name)
    if not path.is_file():
        return NO_EVALUATION
    try:
        return str(json.loads(path.read_text())["summary"])
    except (OSError, ValueError, KeyError) as exc:
        return f"evaluation status unavailable ({type(exc).__name__}: {exc})"
```

Register it at the end of `apply_research_migrations` in `trader/research/schema.py`:

```python
    from trader.research.evaluation_store import apply_evaluation_migrations
    apply_evaluation_migrations(migrator)
```

- [ ] **Step 5: Run the tests**

Run: `$PY -m pytest tests/research -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add trader/research/experiment_registry.py trader/research/evaluation_store.py trader/research/schema.py tests/research/test_strategy_trials.py tests/research/test_evaluation_store.py
git commit -m "feat(research): count trials per strategy and log every evaluation" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Backtest jobs and dataset qualification

**Files:**
- Create: `trader/research/evaluation_jobs.py`, `trader/research/evaluation_data.py`, `tests/research/evaluation_fixtures.py` (test helper, no tests inside)
- Test: `tests/research/test_evaluation_jobs.py` (new)

**Interfaces:**
- Consumes: Task 1 `order_notional`, Task 2 `PaperAutomationRules` / `live_rule_blocks`, `build_realistic_costs`, `load_execution_costs_config`, `Backtester.run_from_module`, `trace_signature`, `DatasetQualifier`, `DatasetManifest`.
- Produces:
  - `evaluation_jobs`: `RunEnvironment(history_db, universe_db, universe_library, execution_costs_path, strategy_file, class_name, conids: tuple[int, ...], bar_size, order_notional, account_equity, max_gross_allocation)`; `WindowJob(point_key, params, window_kind, window_index, start, end, cost_multiplier, replay=0)`; `WindowOutcome(job, trades: tuple[dict, ...], equity: tuple[tuple[datetime, float], ...], net_pnl, max_drawdown, trace_signature, live_rule_blocks)` with `equity_series() -> pd.Series`; `run_window_job(env, job) -> WindowOutcome`; `run_jobs(env, jobs, max_workers=1) -> list[WindowOutcome]` (same order as `jobs`); `default_workers() -> int`.
  - `evaluation_data`: `class EvaluationDataError(Exception)`; `load_bars(history_db, conids, bar_size, start, end) -> dict[int, pd.DataFrame]` (UTC index; raises naming every conid without bars); `qualify_dataset(bars, spec) -> DatasetManifest` (raises on any failed required finding; deterministic for identical data).
  - `tests/research/evaluation_fixtures.py`: `CONIDS`, `TIME_OF_DAY_STRATEGY`, `write_costs_config(path) -> Path`, `write_universe(duckdb_path)`, `write_trend_bars(duckdb_path, *, start, end, drift)`, `build_spec_file(repo, **overrides) -> Path`.

- [ ] **Step 1: Write the test helper `tests/research/evaluation_fixtures.py`**

```python
"""Synthetic market for research-evaluation tests: 8 NASDAQ conids, 5-minute
bars on real XNYS sessions, a price that drifts by ``drift`` every bar, and a
strategy that buys at 10:00 ET and sells at 11:00 ET."""
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd
import yaml

from tests.test_execution_costs import CONFIG, _definition
from trader.data.duckdb_store import DuckDBDataStore
from trader.data.universe import UniverseAccessor

CONIDS = list(range(1001, 1009))
PERIOD = ('2024-02-01', '2024-03-28')
TIME_OF_DAY_STRATEGY = '''
import pandas as pd

from trader.objects import Action
from trader.trading.strategy import Signal, Strategy


class TimeOfDay(Strategy):
    ENTRY_MINUTE = 600
    EXIT_MINUTE = 660

    def on_prices(self, prices):
        ts = pd.Timestamp(prices.index[-1])
        ts = ts.tz_localize('UTC') if ts.tzinfo is None else ts
        local = ts.tz_convert('America/New_York')
        minute = local.hour * 60 + local.minute
        if minute == self.ENTRY_MINUTE:
            return Signal(source_name='time_of_day', action=Action.BUY, probability=0.5, risk=0.5)
        if minute == self.EXIT_MINUTE:
            return Signal(source_name='time_of_day', action=Action.SELL, probability=0.5, risk=0.5)
        return None
'''


def write_costs_config(path: Path) -> Path:
    venues = {'us': {**CONFIG['venues']['us'], 'calendar': 'XNYS'},
              'asx': {**CONFIG['venues']['asx'], 'calendar': 'XASX'}}
    path.write_text(yaml.safe_dump({**CONFIG, 'venues': venues}))
    return path


def write_universe(duckdb_path: str) -> UniverseAccessor:
    universes = UniverseAccessor(duckdb_path, 'Universes')
    for conid in CONIDS:
        universes.insert('evaluation', _definition(conid, f'S{conid}', 'NASDAQ'))
    return universes


def write_trend_bars(duckdb_path: str, *, drift: float, start=PERIOD[0], end=PERIOD[1],
                     conids=CONIDS) -> None:
    calendar = xcals.get_calendar('XNYS')
    stamps = []
    for session in calendar.sessions_in_range(start, end):
        session_open = calendar.session_open(session)
        session_close = calendar.session_close(session)
        stamps.extend(pd.date_range(session_open, session_close - pd.Timedelta(minutes=5), freq='5min'))
    index = pd.DatetimeIndex(stamps).tz_convert('UTC')
    store = DuckDBDataStore(duckdb_path)
    for offset, conid in enumerate(conids):
        price = (100.0 + 10 * offset) * (1 + drift) ** pd.RangeIndex(len(index)).to_numpy()
        frame = pd.DataFrame({'open': price, 'high': price * 1.0005, 'low': price * 0.9995,
                              'close': price, 'volume': 50_000.0, 'bar_size': '5 mins'},
                             index=index)
        frame.index.name = 'date'
        store.write(str(conid), frame)


def build_spec_file(repo: Path, **overrides) -> Path:
    (repo / 'strategies').mkdir(exist_ok=True)
    strategy = repo / 'strategies' / 'time_of_day.py'
    if not strategy.exists():
        strategy.write_text(TIME_OF_DAY_STRATEGY)
    spec = {
        'name': 'time_of_day_us',
        'strategy': 'strategies/time_of_day.py',
        'class': 'TimeOfDay',
        'params': {'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660},
        'neighbourhood': {'ENTRY_MINUTE': [615, 630], 'EXIT_MINUTE': [645, 675]},
        'conids': CONIDS,
        'bar_size': '5 mins',
        'period': {'start': PERIOD[0], 'end': PERIOD[1]},
        'walk_forward': {'folds': 2, 'embargo_sessions': 1, 'holdout_sessions': 5},
        'sizing': {'order_notional': 1900, 'account_equity': 100000},
        'max_gross_allocation': 0.05,
    }
    spec.update(overrides)
    path = repo / f"{spec['name']}.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path
```

- [ ] **Step 2: Write the failing tests `tests/research/test_evaluation_jobs.py`**

```python
import datetime as dt

import pytest

from tests.research.evaluation_fixtures import (
    CONIDS, build_spec_file, write_costs_config, write_trend_bars, write_universe,
)
from trader.research.evaluation_data import EvaluationDataError, load_bars, qualify_dataset
from trader.research.evaluation_jobs import RunEnvironment, WindowJob, run_jobs, run_window_job
from trader.research.evaluation_spec import load_evaluation_spec
from trader.simulation.execution_costs import load_execution_costs_config

UTC = dt.timezone.utc
FEB = (dt.datetime(2024, 2, 1, tzinfo=UTC), dt.datetime(2024, 2, 9, 23, 59, tzinfo=UTC))


@pytest.fixture
def market(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0002)
    costs = write_costs_config(tmp_path / 'execution_costs.yaml')
    spec_path = build_spec_file(tmp_path)
    return tmp_path, tmp_duckdb_path, costs, spec_path


def _env(market):
    repo, db, costs, _ = market
    return RunEnvironment(
        history_db=db, universe_db=db, universe_library='Universes',
        execution_costs_path=str(costs), strategy_file=str(repo / 'strategies' / 'time_of_day.py'),
        class_name='TimeOfDay', conids=tuple(CONIDS), bar_size='5 mins',
        order_notional=1900.0, account_equity=100_000.0, max_gross_allocation=0.05)


def _job(multiplier=1.0, index=0):
    return WindowJob(point_key='p', params={'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660},
                     window_kind='fold', window_index=index, start=FEB[0], end=FEB[1],
                     cost_multiplier=multiplier)


def test_window_job_trades_under_live_rules(market):
    outcome = run_window_job(_env(market), _job())
    assert outcome.net_pnl > 0
    assert len(outcome.trace_signature) == 64
    assert outcome.live_rule_blocks.get('GROSS', 0) > 0  # 8 entries a day, room for 2
    assert all(t['commission'] >= 1.0 for t in outcome.trades)


def test_higher_costs_never_help(market):
    base, stressed = run_jobs(_env(market), [_job(1.0), _job(2.0)])
    assert stressed.net_pnl < base.net_pnl


def test_process_pool_gives_the_same_trades(market):
    jobs = [_job(1.0, 0), _job(1.5, 1)]
    sequential = run_jobs(_env(market), jobs, max_workers=1)
    pooled = run_jobs(_env(market), jobs, max_workers=2)
    assert [o.trace_signature for o in pooled] == [o.trace_signature for o in sequential]


def test_missing_bars_name_the_conid(market):
    _, db, _, _ = market
    with pytest.raises(EvaluationDataError, match='9999'):
        load_bars(db, CONIDS + [9999], '5 mins', *FEB)


def test_qualified_dataset_is_eligible_and_deterministic(market, tmp_duckdb_path):
    repo, db, costs, spec_path = market
    spec = load_evaluation_spec(spec_path, universe_accessor=write_universe(db),
                                costs_config=load_execution_costs_config(str(costs)), repo_root=repo)
    bars = load_bars(db, spec.conids, spec.bar_size,
                     dt.datetime(2024, 2, 1, tzinfo=UTC), dt.datetime(2024, 3, 28, 23, 59, tzinfo=UTC))
    first = qualify_dataset(bars, spec)
    second = qualify_dataset(bars, spec)
    assert first.research_eligible
    assert first.digest == second.digest
```

Note: the fixture calls `write_universe` once; `test_qualified_dataset_is_eligible_and_deterministic` calls it again only to get an accessor — inserting the same definitions twice is harmless for `resolve_symbol` (it de-duplicates), but if it is not, build `UniverseAccessor(db, 'Universes')` directly instead.

- [ ] **Step 3: Run them and watch them fail**

Run: `$PY -m pytest tests/research/test_evaluation_jobs.py -q -p no:cacheprovider`
Expected: import errors for `evaluation_data` and `evaluation_jobs`.

- [ ] **Step 4: Create `trader/research/evaluation_jobs.py`**

```python
"""Backtest jobs for `research evaluate`: one strategy run over one window.

Jobs are plain picklable values so they can run in a process pool; every
worker rebuilds its storage, costs and rules from ``RunEnvironment``.
"""
from __future__ import annotations

import datetime as dt
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import pandas as pd

from trader.data.data_access import TickStorage
from trader.data.universe import UniverseAccessor
from trader.objects import BarSize
from trader.simulation.backtester import Backtester, BacktestConfig, trace_signature
from trader.simulation.execution_costs import build_realistic_costs, load_execution_costs_config
from trader.simulation.live_rules import PaperAutomationRules


@dataclass(frozen=True)
class RunEnvironment:
    history_db: str
    universe_db: str
    universe_library: str
    execution_costs_path: str
    strategy_file: str
    class_name: str
    conids: tuple[int, ...]
    bar_size: str
    order_notional: float
    account_equity: float
    max_gross_allocation: float


@dataclass(frozen=True)
class WindowJob:
    point_key: str
    params: Mapping[str, Any]
    window_kind: str
    window_index: int
    start: dt.datetime
    end: dt.datetime
    cost_multiplier: float
    replay: int = 0


@dataclass(frozen=True)
class WindowOutcome:
    job: WindowJob
    trades: tuple[Mapping[str, Any], ...]
    equity: tuple[tuple[dt.datetime, float], ...]
    net_pnl: float
    max_drawdown: float
    trace_signature: str
    live_rule_blocks: Mapping[str, int]

    def equity_series(self) -> pd.Series:
        if not self.equity:
            return pd.Series(dtype=float)
        stamps, values = zip(*self.equity)
        return pd.Series(values, index=pd.DatetimeIndex(stamps))


def default_workers() -> int:
    return max(1, min(16, (os.cpu_count() or 2) - 1))


def run_window_job(env: RunEnvironment, job: WindowJob) -> WindowOutcome:
    storage = TickStorage(env.history_db)
    accessor = UniverseAccessor(env.universe_db, env.universe_library)
    costs = build_realistic_costs(
        env.conids, accessor, load_execution_costs_config(env.execution_costs_path)
    ).scaled(job.cost_multiplier)
    config = BacktestConfig(
        start_date=job.start, end_date=job.end, initial_capital=env.account_equity,
        bar_size=BarSize.parse_str(env.bar_size), cost_model=costs,
        order_notional=env.order_notional,
        live_rules=PaperAutomationRules(max_gross_allocation=env.max_gross_allocation))
    result = Backtester(storage, config).run_from_module(
        env.strategy_file, env.class_name, list(env.conids),
        universe_accessor=accessor, params=dict(job.params))
    curve = result.equity_curve
    return WindowOutcome(
        job=job,
        trades=tuple({'timestamp': t.timestamp, 'conid': int(t.conid), 'action': str(t.action),
                      'quantity': float(t.quantity), 'price': float(t.price),
                      'commission': float(t.commission)} for t in result.trades),
        equity=tuple((pd.Timestamp(ts).to_pydatetime(), float(value)) for ts, value in curve.items()),
        net_pnl=float(curve.iloc[-1]) - env.account_equity if len(curve) else 0.0,
        max_drawdown=float(result.max_drawdown),
        trace_signature=trace_signature(result),
        live_rule_blocks=dict(result.live_rule_blocks))


def run_jobs(env: RunEnvironment, jobs: Sequence[WindowJob],
             max_workers: int = 1) -> list[WindowOutcome]:
    if max_workers <= 1 or len(jobs) <= 1:
        return [run_window_job(env, job) for job in jobs]
    with ProcessPoolExecutor(max_workers=min(max_workers, len(jobs))) as pool:
        return list(pool.map(run_window_job, [env] * len(jobs), jobs))
```

- [ ] **Step 5: Create `trader/research/evaluation_data.py`**

```python
"""Bars for an evaluation: load, qualify, and seal into a dataset manifest.

A conid without bars, or any failed required quality finding, stops the
evaluation; nothing is dropped silently.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Sequence

import exchange_calendars as xcals
import pandas as pd

from trader.data.data_access import TickStorage
from trader.data.market_data import normalize_historical
from trader.data.store import DateRange
from trader.objects import BarSize
from trader.research.data_quality import DatasetQualificationRequest, DatasetQualifier
from trader.research.dataset_manifest import DatasetFile, DatasetManifest

OHLCV = ['open', 'high', 'low', 'close', 'volume']


class EvaluationDataError(Exception):
    """The evaluation's bars are missing or failed qualification."""


def load_bars(history_db: str, conids: Sequence[int], bar_size: str,
              start: dt.datetime, end: dt.datetime) -> dict[int, pd.DataFrame]:
    tickdata = TickStorage(history_db).get_tickdata(BarSize.parse_str(bar_size))
    bars: dict[int, pd.DataFrame] = {}
    missing: list[int] = []
    for conid in conids:
        raw = tickdata.read(conid, date_range=DateRange(start=start, end=end))
        frame = None
        if raw is not None and len(raw) > 0:
            frame = normalize_historical(raw).dropna(subset=['close'])
        if frame is None or frame.empty:
            missing.append(int(conid))
            continue
        index = frame.index
        frame = frame[OHLCV].copy()
        frame.index = index.tz_localize('UTC') if index.tz is None else index.tz_convert('UTC')
        bars[int(conid)] = frame
    if missing:
        raise EvaluationDataError(
            f'no {bar_size} bars between {start:%Y-%m-%d} and {end:%Y-%m-%d} for conids {missing}; '
            f'download them before evaluating')
    return bars


def _utc_midnight(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)


def qualify_dataset(bars: dict[int, pd.DataFrame], spec) -> DatasetManifest:
    qualification = DatasetQualifier().qualify(DatasetQualificationRequest(
        bars=bars, bar_interval=spec.bar_size, calendar_name=spec.calendar,
        expected_start=spec.period_start, expected_end=spec.period_end))
    failed = [f for f in qualification.findings if f.required and not f.passed]
    if failed:
        raise EvaluationDataError('dataset failed qualification: ' + '; '.join(
            f'{f.name}: {f.detail}' for f in failed))
    files = tuple(
        DatasetFile(path=f'tick_data/{conid}/{spec.bar_size}',
                    sha256=hashlib.sha256(frame.to_csv().encode('utf-8')).hexdigest(),
                    rows=len(frame))
        for conid, frame in sorted(qualification.qualified_bars.items()))
    as_of = max(frame.index.max() for frame in bars.values()).to_pydatetime()
    return DatasetManifest(
        vendor='mmr_history', retrieval_timestamp=as_of, bar_interval=spec.bar_size,
        timestamp_convention='bar_start', session_calendar=spec.calendar,
        calendar_version=xcals.__version__, adjustment_policy='as_stored',
        start_boundary=_utc_midnight(spec.period_start),
        end_boundary=_utc_midnight(spec.period_end),
        spread_source='estimated:tick_table', instruments=tuple(sorted(spec.conids)),
        files=files, findings=qualification.findings)
```

`retrieval_timestamp` is the last bar time (not "now") so the same data always gives the same manifest digest, and so the same family.

- [ ] **Step 6: Run the tests**

Run: `$PY -m pytest tests/research/test_evaluation_jobs.py -q -p no:cacheprovider`
Expected: all pass. If the qualifier rejects the synthetic data, print `qualification.findings` and fix the fixture (not the qualifier): sessions must be complete and the period must match `expected_start/expected_end`.

- [ ] **Step 7: Commit**

```bash
git add trader/research/evaluation_jobs.py trader/research/evaluation_data.py tests/research/evaluation_fixtures.py tests/research/test_evaluation_jobs.py
git commit -m "feat(research): run evaluation backtests as jobs and qualify their data" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: `evaluate()` and the evidence report

**Files:**
- Create: `trader/research/evaluation.py`, `trader/research/evaluation_report.py`
- Test: `tests/research/test_evaluation.py` (new)

**Interfaces:**
- Consumes: Tasks 3, 4, 6, 7; `generate_walk_forward`, `ExperimentRegistry`, `DatasetManifestRepository`, `EligibilityDecisionRepository`, `evaluate_eligibility`, `PAPER_V1`, `annualized_sharpe_ci`, `profit_factor`, `compute_strategy_hash`, `NOT_MIRRORED`.
- Produces:
  - `class EvaluationError(Exception)`; `COST_MULTIPLIERS = (1.0, 1.5, 2.0)`.
  - `EvaluationPaths(history_db, universe_db, universe_library, execution_costs, repo_root: Path, reports_dir: Path, summaries_dir: Path)` — `summaries_dir` is `~/.local/share/mmr/artifacts/evaluations` in production.
  - `EvaluationResult(spec_name, family_id, stage, state, artifact_id, decision_digest, failed_rules, missing_rules, report_path: Path)`.
  - `PointResult(params: dict, trial_id: str, metrics: dict, outcomes: dict[float, list[WindowOutcome]])`.
  - `evaluate(spec, *, research_db, paths, now, max_workers=1, ruleset=PAPER_V1) -> EvaluationResult`.
  - `evaluation_report.write_evaluation_report(reports_dir, *, spec, family_id, stage, state, artifact_id, failed, missing, evidence, main, neighbours, created_at) -> Path` (returns the `.md` path; a `.json` sits next to it).
  - Family fields used by later tasks: `family.validation_protocol['conids']` (sorted ints), `['bar_size']`, `['calendar']`, `['period_start']`, `['period_end']`; `family.cost_model['order_notional']`, `['account_equity']`, `['max_gross_allocation']`.

- [ ] **Step 1: Write the failing tests `tests/research/test_evaluation.py`**

```python
import dataclasses
import datetime as dt
import json

import pytest

from tests.research.evaluation_fixtures import (
    build_spec_file, write_costs_config, write_trend_bars, write_universe,
)
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.artifact import TRIAL_FAILED
from trader.research.eligibility import EligibilityEvidence, Ruleset
from trader.research.evaluation import EvaluationError, EvaluationPaths, PointResult, evaluate
from trader.research.evaluation_report import write_evaluation_report
from trader.research.evaluation_spec import load_evaluation_spec
from trader.research.evaluation_store import (
    STAGE_COMPLETE, STAGE_PRE_HOLDOUT, EvaluationRepository, describe_latest_evaluation,
)
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.schema import apply_research_migrations
from trader.simulation.execution_costs import load_execution_costs_config

PHASE_B_PRE_HOLDOUT = {
    'liquidity_capacity_envelope', 'regime_positive_expectancy_fraction',
    'regime_loss_tolerance', 'regime_transition_stability',
}
CLOCK = [dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)]


def _now():
    CLOCK[0] += dt.timedelta(seconds=1)
    return CLOCK[0]


@pytest.fixture
def workspace(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    costs = write_costs_config(tmp_path / 'execution_costs.yaml')
    db = DuckDBConnection.get_instance(str(tmp_path / 'research.duckdb'))
    apply_research_migrations(SchemaMigrator(db))
    paths = EvaluationPaths(history_db=tmp_duckdb_path, universe_db=tmp_duckdb_path,
                            universe_library='Universes', execution_costs=str(costs),
                            repo_root=tmp_path, reports_dir=tmp_path / 'reports',
                            summaries_dir=tmp_path / 'artifacts' / 'evaluations')
    return tmp_path, tmp_duckdb_path, db, paths


def _spec(workspace, **overrides):
    repo, db_path, _, paths = workspace
    from trader.data.universe import UniverseAccessor
    return load_evaluation_spec(
        build_spec_file(repo, **overrides),
        universe_accessor=UniverseAccessor(db_path, 'Universes'),
        costs_config=load_execution_costs_config(paths.execution_costs), repo_root=repo)


def test_phase_a_stops_before_the_holdout(workspace):
    repo, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0002)

    result = evaluate(_spec(workspace), research_db=db, paths=paths, now=_now)

    assert result.stage == STAGE_PRE_HOLDOUT and result.state == 'CANDIDATE'
    assert set(result.missing_rules) == PHASE_B_PRE_HOLDOUT
    assert 'min_round_trips' in result.failed_rules
    assert ExperimentRegistry(db).family_artifacts(result.family_id) == []
    assert result.report_path.exists() and result.report_path.with_suffix('.json').exists()
    assert EvaluationRepository(db).list()[0].family_id == result.family_id
    assert 'pre_holdout' in describe_latest_evaluation(
        paths.summaries_dir, 'strategies/time_of_day.py', 'TimeOfDay')


def test_losing_strategy_fails_expectancy(workspace):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=-0.0002)
    result = evaluate(_spec(workspace), research_db=db, paths=paths, now=_now)
    assert 'expectancy_baseline_positive' in result.failed_rules


def test_interrupted_trial_is_failed_and_a_rerun_resumes(workspace):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0002)
    spec = _spec(workspace)
    first = evaluate(spec, research_db=db, paths=paths, now=_now)
    registry = ExperimentRegistry(db)
    stale = registry.start_trial(
        first.family_id, trial_key='point:{"ENTRY_MINUTE":615,"EXIT_MINUTE":660}#2',
        parameters={'ENTRY_MINUTE': 615, 'EXIT_MINUTE': 660}, started_at=_now())

    second = evaluate(spec, research_db=db, paths=paths, now=_now)

    assert second.family_id == first.family_id
    assert registry.get_trial(stale).status == TRIAL_FAILED


def test_new_param_is_a_new_family_and_the_trial_count_carries_over(workspace):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0002)
    first = evaluate(_spec(workspace), research_db=db, paths=paths, now=_now)
    registry = ExperimentRegistry(db)
    before = len(registry.strategy_trials('strategies/time_of_day.py', 'TimeOfDay'))

    second = evaluate(_spec(workspace, params={'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 690}),
                      research_db=db, paths=paths, now=_now)

    assert second.family_id != first.family_id
    assert len(registry.strategy_trials('strategies/time_of_day.py', 'TimeOfDay')) > before


def test_edited_strategy_file_is_a_new_family(workspace):
    repo, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0002)
    first = evaluate(_spec(workspace), research_db=db, paths=paths, now=_now)
    strategy = repo / 'strategies' / 'time_of_day.py'
    strategy.write_text(strategy.read_text() + '\n# edited\n')
    second = evaluate(_spec(workspace), research_db=db, paths=paths, now=_now)
    assert second.family_id != first.family_id


def _holdout_ruleset():
    keep = {'expectancy_baseline_positive', 'holdout_drawdown_within_canary',
            'deterministic_replay', 'holdout_opened_once'}
    return Ruleset(name='paper-v1-subset', version='test',
                   rules=tuple(r for r in PAPER_V1.rules if r.code in keep), source_digest='test')


def test_passing_gate_opens_the_holdout_once(workspace):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0002)
    spec = _spec(workspace)

    result = evaluate(spec, research_db=db, paths=paths, now=_now, ruleset=_holdout_ruleset())

    assert result.stage == STAGE_COMPLETE and result.state == 'PAPER_ELIGIBLE'
    assert ExperimentRegistry(db).get_artifact(result.artifact_id).holdout_opened
    assert result.decision_digest
    with pytest.raises(EvaluationError, match='holdout already opened'):
        evaluate(spec, research_db=db, paths=paths, now=_now, ruleset=_holdout_ruleset())


def test_report_serializes_an_infinite_profit_factor(workspace):
    _, _, _, paths = workspace
    point = PointResult(params={'A': 1}, trial_id='t', metrics={}, outcomes={})
    path = write_evaluation_report(
        paths.reports_dir, spec=_spec(workspace), family_id='f', stage=STAGE_PRE_HOLDOUT,
        state='CANDIDATE', artifact_id=None, failed=('profit_factor_after_costs',), missing=(),
        evidence=EligibilityEvidence(profit_factor=float('inf')), main=point, neighbours=[],
        created_at=_now())
    data = json.loads(path.with_suffix('.json').read_text())
    assert data['evidence']['profit_factor'] == 'inf'
```

- [ ] **Step 2: Run them and watch them fail**

Run: `$PY -m pytest tests/research/test_evaluation.py -q -p no:cacheprovider`
Expected: import error for `trader.research.evaluation`.

- [ ] **Step 3: Create `trader/research/evaluation_report.py`**

```python
"""Human + machine readable report for one `research evaluate` run."""
from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path
from typing import Any

from trader.simulation.live_rules import NOT_MIRRORED


def _jsonable(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def write_evaluation_report(reports_dir: Path, *, spec, family_id: str, stage: str, state: str,
                            artifact_id, failed, missing, evidence, main, neighbours,
                            created_at) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = f'evaluation_{spec.name}_{created_at:%Y%m%dT%H%M%S}'
    data = {
        'spec': spec.name, 'strategy': spec.strategy_path, 'class': spec.class_name,
        'family_id': family_id, 'artifact_id': artifact_id, 'stage': stage, 'state': state,
        'failed_rules': list(failed), 'missing_rules': list(missing),
        'evidence': {k: _jsonable(v) for k, v in dataclasses.asdict(evidence).items()},
        'folds': [
            {'index': o.job.window_index, 'start': o.job.start.isoformat(),
             'end': o.job.end.isoformat(), 'net_pnl': o.net_pnl,
             'live_rule_blocks': dict(o.live_rule_blocks)}
            for o in main.outcomes.get(1.0, [])],
        'trials': [
            {'params': p.params, 'trial_id': p.trial_id,
             **{k: _jsonable(p.metrics.get(k))
                for k in ('oos_expectancy_bps', 'oos_net_pnl', 'daily_sharpe', 'n_round_trips')}}
            for p in [main, *neighbours]],
        'not_mirrored': list(NOT_MIRRORED),
    }
    (reports_dir / f'{stem}.json').write_text(json.dumps(data, indent=2, default=str))
    md_path = reports_dir / f'{stem}.md'
    md_path.write_text(_markdown(data))
    return md_path


def _markdown(data: dict) -> str:
    lines = [
        f"# Evaluation {data['spec']}", '',
        f"- Strategy: `{data['strategy']}` / `{data['class']}`",
        f"- Family: `{data['family_id']}`",
        f"- Stage: **{data['stage']}**, state: **{data['state']}**",
        f"- Artifact: `{data['artifact_id']}`" if data['artifact_id'] else '- Artifact: none (holdout not opened)',
        f"- Failed rules: {', '.join(data['failed_rules']) or 'none'}",
        f"- Missing evidence: {', '.join(data['missing_rules']) or 'none'}",
        '', '## Evidence', '', '| Field | Value |', '|---|---|',
    ]
    lines += [f'| {k} | {v} |' for k, v in data['evidence'].items()]
    lines += ['', '## Walk-forward folds (main point, 1x costs)', '',
              '| Fold | Window | Net P&L | Entries refused by live rules |', '|---|---|---|---|']
    lines += [f"| {f['index']} | {f['start'][:10]} – {f['end'][:10]} | {f['net_pnl']:.2f} | "
              f"{f['live_rule_blocks'] or '-'} |" for f in data['folds']]
    lines += ['', '## Trials', '', '| Params | Expectancy (bps) | Net P&L | Daily Sharpe | Round trips |',
              '|---|---|---|---|---|']
    lines += [f"| {t['params']} | {t['oos_expectancy_bps']} | {t['oos_net_pnl']} | "
              f"{t['daily_sharpe']} | {t['n_round_trips']} |" for t in data['trials']]
    lines += ['', '## Not mirrored from live paper trading', '']
    lines += [f'- {item}' for item in data['not_mirrored']]
    return '\n'.join(lines) + '\n'
```

- [ ] **Step 4: Create `trader/research/evaluation.py`**

```python
"""`mmr research evaluate`: real backtests -> paper-v1 evidence -> decision.

The holdout opens only when every non-holdout rule already passes, so a run
that could not be deployed never spends it (spec section 2, step 5).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import pandas as pd

from trader.data.backtest_store import compute_strategy_hash
from trader.research import evidence as ev
from trader.research.artifact import (
    TRIAL_FAILED, TRIAL_RUNNING, TRIAL_SUCCEEDED, ExperimentFamily,
)
from trader.research.attribution import RoundTrip, build_round_trips
from trader.research.eligibility import (
    EligibilityDecisionRepository, EligibilityEvidence, Ruleset, evaluate_eligibility,
)
from trader.research.evaluation_data import load_bars, qualify_dataset
from trader.research.evaluation_jobs import RunEnvironment, WindowJob, WindowOutcome, run_jobs
from trader.research.evaluation_report import write_evaluation_report
from trader.research.evaluation_spec import EvaluationSpec, neighbour_points
from trader.research.evaluation_store import (
    STAGE_COMPLETE, STAGE_HOLDOUT_FAILED, STAGE_PRE_HOLDOUT,
    EvaluationRecord, EvaluationRepository, write_evaluation_summary,
)
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.schema import DatasetManifestRepository
from trader.research.statistics import annualized_sharpe_ci, profit_factor
from trader.research.validation import ValidationPlan, generate_walk_forward

COST_MULTIPLIERS = (1.0, 1.5, 2.0)


class EvaluationError(Exception):
    """The evaluation cannot continue; the message names the cause."""


@dataclass(frozen=True)
class EvaluationPaths:
    history_db: str
    universe_db: str
    universe_library: str
    execution_costs: str
    repo_root: Path
    reports_dir: Path
    summaries_dir: Path


@dataclass(frozen=True)
class EvaluationResult:
    spec_name: str
    family_id: str
    stage: str
    state: str
    artifact_id: Optional[str]
    decision_digest: Optional[str]
    failed_rules: tuple[str, ...]
    missing_rules: tuple[str, ...]
    report_path: Path


@dataclass
class PointResult:
    params: dict
    trial_id: str
    metrics: dict
    # cost multiplier -> fold outcomes in fold order; empty for a reused neighbour
    outcomes: dict = field(default_factory=dict)


def evaluate(spec: EvaluationSpec, *, research_db: Any, paths: EvaluationPaths,
             now: Callable[[], dt.datetime], max_workers: int = 1,
             ruleset: Ruleset = PAPER_V1) -> EvaluationResult:
    registry = ExperimentRegistry(research_db)
    plan = generate_walk_forward((spec.period_start, spec.period_end), n_folds=spec.folds,
                                 embargo=spec.embargo_sessions, holdout=spec.holdout_sessions,
                                 calendar_name=spec.calendar)
    bars = load_bars(paths.history_db, spec.conids, spec.bar_size,
                     _day_start(spec.period_start), _day_end(spec.period_end))
    manifest_digest = DatasetManifestRepository(research_db).seal(
        qualify_dataset(bars, spec), sealed_at=now())
    family_id = registry.create_family(_family(spec, manifest_digest, paths),
                                       created_at=now(), validation_folds=_fold_specs(plan))
    _refuse_if_holdout_opened(registry, family_id)

    env = RunEnvironment(
        history_db=paths.history_db, universe_db=paths.universe_db,
        universe_library=paths.universe_library, execution_costs_path=paths.execution_costs,
        strategy_file=str(spec.strategy_file), class_name=spec.class_name,
        conids=tuple(spec.conids), bar_size=spec.bar_size, order_notional=spec.order_notional,
        account_equity=spec.account_equity, max_gross_allocation=spec.max_gross_allocation)
    main = _run_point(registry, family_id, env, plan, dict(spec.params), COST_MULTIPLIERS,
                      now, max_workers, rerun_existing=True)
    neighbours = [_run_point(registry, family_id, env, plan, point, (1.0,), now, max_workers,
                             rerun_existing=False) for point in neighbour_points(spec)]
    evidence = _walk_forward_evidence(
        spec, main, neighbours, registry.strategy_trials(spec.strategy_path, spec.class_name))
    gate = ev.pre_holdout_outcome(evaluate_eligibility(ruleset, evidence))
    if not gate.passed:
        return _finish(research_db, spec, paths, now, family_id=family_id,
                       stage=STAGE_PRE_HOLDOUT, state='CANDIDATE', artifact_id=None,
                       decision_digest=None, gate=gate, evidence=evidence, main=main,
                       neighbours=neighbours)

    artifact_id = registry.seal_artifact(family_id, selected_trial_id=main.trial_id,
                                         selected_parameters=dict(spec.params), sealed_at=now())
    start, end = _day_start(plan.holdout.start), _day_end(plan.holdout.end)
    first, second = run_jobs(env, [
        WindowJob(_point_key(spec.params), dict(spec.params), 'holdout', 0, start, end, 1.0,
                  replay=replay) for replay in (0, 1)], max_workers)
    passed = ev.holdout_passes(build_round_trips(first.trades), first.max_drawdown)
    registry.open_holdout(artifact_id, opened_at=now(), passed=passed,
                          detail=f'net_pnl={first.net_pnl:.2f} max_drawdown={first.max_drawdown:.4f}')
    evidence = replace(
        evidence, scaled_holdout_drawdown=first.max_drawdown,
        deterministic_replay_ok=first.trace_signature == second.trace_signature,
        holdout_opened_once=registry.get_artifact(artifact_id).holdout_opened)
    decision = evaluate_eligibility(ruleset, evidence)
    decision_digest = EligibilityDecisionRepository(research_db).record(
        decision, artifact_id=artifact_id, recorded_at=now())
    return _finish(research_db, spec, paths, now, family_id=family_id,
                   stage=STAGE_COMPLETE if passed else STAGE_HOLDOUT_FAILED,
                   state=decision.state, artifact_id=artifact_id,
                   decision_digest=decision_digest, gate=ev.final_outcome(decision),
                   evidence=evidence, main=main, neighbours=neighbours)


def _day_start(day) -> dt.datetime:
    return dt.datetime.combine(pd.Timestamp(day).date(), dt.time.min, tzinfo=dt.timezone.utc)


def _day_end(day) -> dt.datetime:
    return dt.datetime.combine(pd.Timestamp(day).date(), dt.time.max, tzinfo=dt.timezone.utc)


def _point_key(params) -> str:
    return 'point:' + json.dumps(dict(params), sort_keys=True, separators=(',', ':'))


def _file_digest(path: Path) -> str:
    return 'sha256:' + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _dependency_lock_digest(repo_root: Path) -> str:
    for name in ('uv.lock', 'requirements.txt', 'pyproject.toml'):
        if (repo_root / name).is_file():
            return _file_digest(repo_root / name)
    return 'unknown'


def _repository_commit(repo_root: Path, strategy_file: Path) -> str:
    def git(*args):
        return subprocess.run(['git', '-C', str(repo_root), *args],
                              capture_output=True, text=True, timeout=10)

    head = git('rev-parse', 'HEAD')
    if head.returncode != 0:
        return 'unknown'  # not a git checkout; the source digest still pins the file
    if git('status', '--porcelain', '--', str(strategy_file)).stdout.strip():
        raise EvaluationError(
            f'{strategy_file.name} has uncommitted changes; commit it so the evidence names a real commit')
    return head.stdout.strip()


def _family(spec: EvaluationSpec, manifest_digest: str, paths: EvaluationPaths) -> ExperimentFamily:
    return ExperimentFamily(
        strategy_path=spec.strategy_path, class_name=spec.class_name,
        repository_commit=_repository_commit(paths.repo_root, spec.strategy_file),
        source_tree_digest=compute_strategy_hash(str(spec.strategy_file)),
        dependency_lock_digest=_dependency_lock_digest(paths.repo_root),
        container_digest=os.environ.get('MMR_CONTAINER_DIGEST', 'local:none'),
        dataset_manifest_digest=manifest_digest,
        search_space={'params': dict(spec.params),
                      'neighbourhood': {k: list(v) for k, v in spec.neighbourhood.items()}},
        cost_model={'model': 'realistic', 'config_digest': _file_digest(Path(paths.execution_costs)),
                    'order_notional': spec.order_notional, 'account_equity': spec.account_equity,
                    'max_gross_allocation': spec.max_gross_allocation},
        validation_protocol={'calendar': spec.calendar, 'bar_size': spec.bar_size,
                             'conids': sorted(spec.conids),
                             'period_start': spec.period_start.isoformat(),
                             'period_end': spec.period_end.isoformat(), 'folds': spec.folds,
                             'embargo_sessions': spec.embargo_sessions,
                             'holdout_sessions': spec.holdout_sessions})


def _fold_specs(plan: ValidationPlan) -> list[dict]:
    def day(value) -> str:
        return pd.Timestamp(value).date().isoformat()

    specs = [{'kind': 'walk_forward', 'index': f.index, 'train_start': day(f.train_start),
              'train_end': day(f.train_end), 'test_start': day(f.test_start),
              'test_end': day(f.test_end)} for f in plan.folds]
    specs.append({'kind': 'holdout', 'start': day(plan.holdout.start), 'end': day(plan.holdout.end)})
    return specs


def _refuse_if_holdout_opened(registry: ExperimentRegistry, family_id: str) -> None:
    for artifact in registry.family_artifacts(family_id):
        if artifact.holdout_opened:
            raise EvaluationError(
                f'holdout already opened for family {family_id[:12]}; '
                f'change the spec to start a new family')


def _round_trips(outcomes: Sequence[WindowOutcome]) -> list[RoundTrip]:
    return [rt for o in outcomes for rt in build_round_trips(o.trades)]


def _trial_metrics(outcomes: Sequence[WindowOutcome], starting_equity: float) -> dict:
    trips = _round_trips(outcomes)
    returns = ev.session_returns([o.equity_series() for o in outcomes],
                                 starting_equity=starting_equity)
    return {
        'oos_net_pnl': float(sum(o.net_pnl for o in outcomes)),
        'oos_expectancy_bps': ev.dollar_weighted_expectancy_bps(trips),
        'daily_sharpe': ev.per_period_sharpe(returns),
        'n_round_trips': len(trips),
        'fold_net_pnls': [float(o.net_pnl) for o in outcomes],
        'trace_signature': ev.combined_trace_signature([o.trace_signature for o in outcomes]),
    }


def _run_point(registry: ExperimentRegistry, family_id: str, env: RunEnvironment,
               plan: ValidationPlan, params: dict, multipliers: Sequence[float],
               now: Callable[[], dt.datetime], max_workers: int, *,
               rerun_existing: bool) -> PointResult:
    key = _point_key(params)
    attempts = [t for t in registry.list_trials(family_id, include_archived=True)
                if t.trial_key.split('#')[0] == key]
    for stale in (t for t in attempts if t.status == TRIAL_RUNNING):
        registry.finish_trial(stale.trial_id, status=TRIAL_FAILED, finished_at=now(),
                              safe_summary='interrupted before it finished')
    succeeded = next((t for t in attempts if t.status == TRIAL_SUCCEEDED), None)
    if succeeded is not None and not rerun_existing:
        return PointResult(params, succeeded.trial_id, dict(succeeded.metrics))

    trial_id = succeeded.trial_id if succeeded else registry.start_trial(
        family_id, trial_key=f'{key}#{len(attempts) + 1}', parameters=params, started_at=now())
    jobs = [WindowJob(key, params, 'fold', fold.index, _day_start(fold.test_start),
                      _day_end(fold.test_end), multiplier)
            for multiplier in multipliers for fold in plan.folds]
    try:
        outcomes = run_jobs(env, jobs, max_workers)
    except Exception as exc:
        if succeeded is None:
            registry.finish_trial(trial_id, status=TRIAL_FAILED, finished_at=now(),
                                  traceback=traceback.format_exc(),
                                  safe_summary=type(exc).__name__)
        raise EvaluationError(f'backtest failed for {key}: {exc}') from exc

    by_multiplier = {m: sorted((o for o in outcomes if o.job.cost_multiplier == m),
                               key=lambda o: o.job.window_index) for m in multipliers}
    metrics = _trial_metrics(by_multiplier[1.0], env.account_equity)
    if succeeded is None:
        registry.finish_trial(trial_id, status=TRIAL_SUCCEEDED, finished_at=now(), metrics=metrics)
    elif succeeded.metrics.get('trace_signature') != metrics['trace_signature']:
        raise EvaluationError(
            f'{key} produced different trades than its recorded trial; '
            f'the data or the code changed since the last run')
    return PointResult(params, trial_id, metrics, by_multiplier)


def _walk_forward_evidence(spec: EvaluationSpec, main: PointResult,
                           neighbours: Sequence[PointResult], strategy_trials) -> EligibilityEvidence:
    trips = {m: _round_trips(main.outcomes[m]) for m in COST_MULTIPLIERS}
    baseline = trips[1.0]
    returns = ev.session_returns([o.equity_series() for o in main.outcomes[1.0]],
                                 starting_equity=spec.account_equity)
    sharpe_ci = annualized_sharpe_ci(returns, periods_per_year=ev.TRADING_DAYS_PER_YEAR, seed=0)
    trial_sharpes = [t.metrics['daily_sharpe'] for t in strategy_trials
                     if isinstance(t.metrics.get('daily_sharpe'), (int, float))]
    return EligibilityEvidence(
        n_round_trips=len(baseline),
        n_instruments=len(set(spec.conids)),
        expectancy_bps_baseline=ev.dollar_weighted_expectancy_bps(baseline),
        expectancy_bps_1_5x=ev.dollar_weighted_expectancy_bps(trips[1.5]),
        expectancy_bps_2x=ev.dollar_weighted_expectancy_bps(trips[2.0]),
        selection_adjusted_confidence=ev.selection_confidence(
            returns, n_trials=len(strategy_trials), trial_sharpes=trial_sharpes),
        annualized_sharpe_ci_low=None if sharpe_ci is None else sharpe_ci.low,
        profit_factor=profit_factor([rt.pnl for rt in baseline]) if baseline else None,
        walk_forward_positive_fraction=ev.positive_fold_fraction(main.metrics['fold_net_pnls']),
        max_month_profit_share=ev.month_concentration(baseline),
        max_instrument_profit_share=ev.instrument_concentration(baseline),
        neighborhood_robust=ev.neighbourhood_robust(
            [n.metrics.get('oos_expectancy_bps') for n in neighbours]),
    )


def _finish(research_db, spec: EvaluationSpec, paths: EvaluationPaths, now, *, family_id: str,
            stage: str, state: str, artifact_id: Optional[str], decision_digest: Optional[str],
            gate: ev.GateOutcome, evidence: EligibilityEvidence, main: PointResult,
            neighbours: Sequence[PointResult]) -> EvaluationResult:
    created_at = now()
    report_path = write_evaluation_report(
        paths.reports_dir, spec=spec, family_id=family_id, stage=stage, state=state,
        artifact_id=artifact_id, failed=gate.failed, missing=gate.missing, evidence=evidence,
        main=main, neighbours=neighbours, created_at=created_at)
    record = EvaluationRecord(
        spec_name=spec.name, family_id=family_id, strategy_path=spec.strategy_path,
        class_name=spec.class_name, stage=stage, state=state, artifact_id=artifact_id,
        decision_digest=decision_digest, failed_rules=gate.failed, missing_rules=gate.missing,
        report_path=str(report_path), created_at=created_at)
    EvaluationRepository(research_db).record(record)
    write_evaluation_summary(paths.summaries_dir, record)
    return EvaluationResult(spec.name, family_id, stage, state, artifact_id, decision_digest,
                            gate.failed, gate.missing, report_path)
```

- [ ] **Step 5: Run the tests**

Run: `$PY -m pytest tests/research/test_evaluation.py -q -p no:cacheprovider --timeout=300`
Expected: all pass. Notes if one fails:
- `test_phase_a_stops_before_the_holdout`: if `selection_adjusted_confidence` or `annualized_sharpe_ci_low` shows up in `missing`, the fixture has too few sessions per fold — print `result.report_path.read_text()` and widen `PERIOD` in the fixture rather than loosening the assertion.
- `test_passing_gate_opens_the_holdout_once`: the holdout has 5 sessions with 2 profitable round trips each; if the holdout fails, print the report and check fees versus drift in the fixture.

- [ ] **Step 6: Full suite, then commit**

```bash
git add trader/research/evaluation.py trader/research/evaluation_report.py tests/research/test_evaluation.py
git commit -m "feat(research): evaluate strategies on real walk-forward evidence" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 9: Sign and export a bundle (`attest_and_export`)

**Files:**
- Create: `trader/research/attest_export.py`
- Modify: `tests/research/evaluation_fixtures.py` (add `export_eligible_bundle`)
- Test: `tests/research/test_attest_export.py` (new)

**Interfaces:**
- Consumes: Task 5 `review_allowed_for`, Task 8 `evaluate` (family fields), `build_attestation`, `AttestationRepository`, `ResearchBundle.export`, `EligibilityDecisionRepository.get`, `OperatorReviewRepository.get`, `ExperimentRegistry.get_validation_folds/list_trials/get_family/get_artifact`.
- Produces:
  - `class AttestExportError(Exception)`; `ATTESTATION_LIFETIME = timedelta(days=90)`.
  - `bundle_dir_name(manifest_digest: str) -> str` → `'sha256_' + hex`.
  - `attest_and_export(research_db, *, artifact_id: str, signer, artifacts_root: Path, now: datetime) -> Path` — refuses unless the artifact has exactly one `PAPER_ELIGIBLE` decision and exactly one review for it; reuses an existing attestation for that (decision, review); exports to a staging dir, then renames to `artifacts_root / bundle_dir_name(digest)`; returns that path (idempotent).
  - Test helper `export_eligible_bundle(repo: Path, duckdb_path: str) -> EligibleBundleFixture(bundle_path, signer, artifact_id, spec, research_db)` in `tests/research/evaluation_fixtures.py`.

- [ ] **Step 1: Add the shared fixture helper** (append to `tests/research/evaluation_fixtures.py`)

```python
import datetime as dt
from dataclasses import dataclass

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.eligibility import Ruleset
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.review import OperatorReview, OperatorReviewRepository
from trader.research.schema import apply_research_migrations
from trader.research.signing import AttestationSigner

HOLDOUT_RULES = {'expectancy_baseline_positive', 'holdout_drawdown_within_canary',
                 'deterministic_replay', 'holdout_opened_once'}
FIXED_NOW = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)


def holdout_ruleset() -> Ruleset:
    """A paper-v1 subset small enough for synthetic data to pass, so tests can
    exercise the holdout and export paths through public APIs."""
    return Ruleset(name='paper-v1-subset', version='test', source_digest='test',
                   rules=tuple(r for r in PAPER_V1.rules if r.code in HOLDOUT_RULES))


def submit_review(research_db, artifact_id: str, decision_digest: str, kind: str = 'llm') -> str:
    return OperatorReviewRepository(research_db).record(OperatorReview(
        artifact_id=artifact_id, eligibility_decision_digest=decision_digest,
        reviewer='claude-test', reviewed_at=FIXED_NOW, economic_rationale='test drift',
        edge_survives_costs='modeled', known_failure_regimes='flat days',
        data_and_survivorship_limits='synthetic', parameter_sensitivity='neighbours',
        operational_dependencies='none', capacity_and_decay='small',
        episode_dominance='none', holdout_opened_once_confirmed=True, reviewer_kind=kind))


@dataclass(frozen=True)
class EligibleBundleFixture:
    bundle_path: 'Path'
    signer: AttestationSigner
    artifact_id: str
    spec: object
    research_db: object


def export_eligible_bundle(repo: Path, duckdb_path: str) -> EligibleBundleFixture:
    from trader.data.universe import UniverseAccessor
    from trader.research.attest_export import attest_and_export
    from trader.research.evaluation import EvaluationPaths, evaluate
    from trader.research.evaluation_spec import load_evaluation_spec
    from trader.simulation.execution_costs import load_execution_costs_config

    write_universe(duckdb_path)
    write_trend_bars(duckdb_path, drift=0.0002)
    costs = write_costs_config(repo / 'execution_costs.yaml')
    research_db = DuckDBConnection.get_instance(str(repo / 'research.duckdb'))
    apply_research_migrations(SchemaMigrator(research_db))
    spec = load_evaluation_spec(build_spec_file(repo),
                                universe_accessor=UniverseAccessor(duckdb_path, 'Universes'),
                                costs_config=load_execution_costs_config(str(costs)), repo_root=repo)
    ticks = iter(range(10_000))
    result = evaluate(spec, research_db=research_db,
                      paths=EvaluationPaths(duckdb_path, duckdb_path, 'Universes', str(costs),
                                            repo, repo / 'reports', repo / 'artifacts' / 'evaluations'),
                      now=lambda: FIXED_NOW + dt.timedelta(seconds=next(ticks)),
                      ruleset=holdout_ruleset())
    submit_review(research_db, result.artifact_id, result.decision_digest)
    signer = AttestationSigner.generate()
    bundle_path = attest_and_export(research_db, artifact_id=result.artifact_id, signer=signer,
                                    artifacts_root=repo / 'artifacts', now=FIXED_NOW)
    return EligibleBundleFixture(bundle_path, signer, result.artifact_id, spec, research_db)
```

- [ ] **Step 2: Write the failing tests `tests/research/test_attest_export.py`**

```python
import json

import pytest

from tests.research.evaluation_fixtures import FIXED_NOW, export_eligible_bundle
from trader.automation.artifact_verifier import ArtifactVerifier
from trader.research.attest_export import AttestExportError, attest_and_export, bundle_dir_name
from trader.research.bundle import ResearchBundle
from trader.research.signing import AttestationSigner


@pytest.fixture
def exported(tmp_path, tmp_duckdb_path):
    return export_eligible_bundle(tmp_path, tmp_duckdb_path)


def test_bundle_lands_under_its_manifest_digest(exported):
    manifest = json.loads((exported.bundle_path / 'manifest.json').read_text())
    assert exported.bundle_path.name == bundle_dir_name(manifest['manifest_digest'])


def test_bundle_verifies_for_research_and_for_the_runtime(exported):
    keys = {exported.signer.public_key_id: exported.signer.public_key}
    ResearchBundle(exported.research_db).verify(exported.bundle_path, trusted_public_keys=keys)
    verified = ArtifactVerifier([exported.signer.public_key]).verify(
        exported.bundle_path, 'paper', exported.artifact_id, FIXED_NOW)
    assert set(verified.allowlist) == {str(c) for c in exported.spec.conids}


def test_attesting_twice_reuses_the_attestation_and_the_directory(exported):
    again = attest_and_export(exported.research_db, artifact_id=exported.artifact_id,
                              signer=exported.signer, artifacts_root=exported.bundle_path.parent,
                              now=FIXED_NOW)
    assert again == exported.bundle_path
    assert sorted(p.name for p in exported.bundle_path.parent.glob('sha256_*')) == [again.name]


def test_unknown_artifact_is_refused(exported, tmp_path):
    with pytest.raises(AttestExportError, match='unknown artifact'):
        attest_and_export(exported.research_db, artifact_id='nope',
                          signer=AttestationSigner.generate(), artifacts_root=tmp_path / 'x',
                          now=FIXED_NOW)
```

- [ ] **Step 3: Run them and watch them fail**

Run: `$PY -m pytest tests/research/test_attest_export.py -q -p no:cacheprovider --timeout=300`
Expected: import error for `trader.research.attest_export`.

- [ ] **Step 4: Create `trader/research/attest_export.py`**

```python
"""Sign the attestation for an eligible artifact and export its bundle.

The bundle lands in ``<artifacts_root>/sha256_<manifest digest>``: the path
order dispatch resolves from the digest the intent emitter sends.
"""
from __future__ import annotations

import datetime as dt
import shutil
import uuid
from pathlib import Path
from typing import Any

from trader.research.attestation import AttestationRepository, build_attestation
from trader.research.bundle import ResearchBundle
from trader.research.canonical import sha256_digest
from trader.research.eligibility import STATE_PAPER_ELIGIBLE, EligibilityDecisionRepository
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.review import OperatorReviewRepository, review_allowed_for

ATTESTATION_LIFETIME = dt.timedelta(days=90)


class AttestExportError(Exception):
    """The artifact cannot be attested; the message says what is missing."""


def bundle_dir_name(manifest_digest: str) -> str:
    return 'sha256_' + manifest_digest.removeprefix('sha256:')


def _rows(db: Any, sql: str, params: list) -> list:
    return db.transaction(lambda conn: conn.execute(sql, params).fetchall())


def _single(db: Any, sql: str, params: list, error: str) -> str:
    rows = _rows(db, sql, params)
    if len(rows) != 1:
        raise AttestExportError(error)
    return rows[0][0]


def attest_and_export(research_db: Any, *, artifact_id: str, signer: Any,
                      artifacts_root: Path, now: dt.datetime) -> Path:
    registry = ExperimentRegistry(research_db)
    artifact = registry.get_artifact(artifact_id)
    if artifact is None:
        raise AttestExportError(f'unknown artifact {artifact_id}')
    family = registry.get_family(artifact.family_id)
    decision_digest = _single(
        research_db, 'SELECT decision_digest FROM eligibility_decisions WHERE artifact_id = ?',
        [artifact_id], f'artifact {artifact_id[:12]} must have exactly one eligibility decision')
    decision = EligibilityDecisionRepository(research_db).get(decision_digest)
    if decision.state != STATE_PAPER_ELIGIBLE:
        raise AttestExportError(
            f'artifact {artifact_id[:12]} is {decision.state}, not {STATE_PAPER_ELIGIBLE}; nothing to attest')
    review_digest = _single(
        research_db,
        'SELECT review_digest FROM operator_reviews WHERE artifact_id = ? '
        'AND eligibility_decision_digest = ?', [artifact_id, decision_digest],
        'submit exactly one operator review for this decision first '
        '(`mmr research review submit ... --reviewer-kind human|llm`)')
    review = OperatorReviewRepository(research_db).get(review_digest)
    try:
        review_allowed_for(review, 'paper')
    except ValueError as exc:
        raise AttestExportError(str(exc)) from exc

    existing = _rows(research_db,
                     'SELECT payload_digest FROM eligibility_attestations '
                     'WHERE eligibility_decision_digest = ? AND review_digest = ?',
                     [decision_digest, review_digest])
    if not existing:
        unsigned = _attestation(registry, family, artifact_id, decision, review, signer, now)
        AttestationRepository(research_db).record(signer.sign(unsigned))
    return _export(research_db, artifact_id, artifacts_root)


def _attestation(registry: ExperimentRegistry, family, artifact_id: str, decision, review,
                 signer, now: dt.datetime):
    folds = registry.get_validation_folds(family.family_id)
    trials = registry.list_trials(family.family_id, include_archived=True)
    trial_payload = [
        {'trial_id': t.trial_id, 'family_id': t.family_id, 'trial_key': t.trial_key,
         'parameters': dict(t.parameters), 'status': t.status, 'started_at': t.started_at,
         'finished_at': t.finished_at, 'metrics': dict(t.metrics),
         'traceback_digest': t.traceback_digest, 'safe_summary': t.safe_summary,
         'archived': t.archived}
        for t in trials]
    walk_forward = [f for f in folds if f.get('kind') == 'walk_forward']
    holdout = next(f for f in folds if f.get('kind') == 'holdout')
    protocol = family.validation_protocol
    cost_model = family.cost_model
    instruments = tuple(str(c) for c in sorted(protocol['conids']))
    return build_attestation(
        decision=decision, review=review, public_key_id=signer.public_key_id,
        artifact_digest=artifact_id, source_digest=family.source_tree_digest,
        config_digest=family.dependency_lock_digest,
        dataset_manifest_digest=family.dataset_manifest_digest,
        allowlist_digest=sha256_digest('attestation_allowlist', list(instruments)),
        training_boundary=f"{walk_forward[0]['train_start']}/{walk_forward[-1]['train_end']}",
        validation_boundary=f"{walk_forward[0]['test_start']}/{walk_forward[-1]['test_end']}",
        holdout_boundary=f"{holdout['start']}/{holdout['end']}",
        evidence_boundary=f"{protocol['period_start']}/{protocol['period_end']}",
        cost_assumptions=dict(cost_model),
        capacity_assumptions={'order_notional': cost_model['order_notional'],
                              'account_equity': cost_model['account_equity']},
        max_gross_allocation=float(cost_model['max_gross_allocation']),
        permitted_instruments=instruments,
        created_at=now, expires_at=now + ATTESTATION_LIFETIME,
        operator_approved_at=review.reviewed_at,
        evidence_refs=tuple(decision.evidence_refs) + (
            f"bundle_trials:{sha256_digest('research_bundle_trials', trial_payload)}",
            f"bundle_folds:{sha256_digest('research_bundle_folds', folds)}"),
    )


def _remove_read_only_tree(path: Path) -> None:
    for child in path.rglob('*'):
        child.chmod(0o644)
    path.chmod(0o755)
    shutil.rmtree(path)


def _export(research_db: Any, artifact_id: str, artifacts_root: Path) -> Path:
    artifacts_root.mkdir(parents=True, exist_ok=True)
    staging = artifacts_root / f'.export-{uuid.uuid4().hex}'
    digest = ResearchBundle(research_db).export(artifact_id, staging).manifest_digest
    final = artifacts_root / bundle_dir_name(digest)
    if final.exists():
        _remove_read_only_tree(staging)
        return final
    staging.rename(final)
    return final
```

- [ ] **Step 5: Run the tests**

Run: `$PY -m pytest tests/research/test_attest_export.py -q -p no:cacheprovider --timeout=300`
Expected: all pass. If `test_attesting_twice_...` finds two directories, the manifest carries an export timestamp; then make `_export` return the existing directory whose `manifest.json` has the same `artifact_id` and attestation `payload_digest`, and keep the test.

- [ ] **Step 6: Commit**

```bash
git add trader/research/attest_export.py tests/research/evaluation_fixtures.py tests/research/test_attest_export.py
git commit -m "feat(research): sign and export eligible bundles under their manifest digest" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: CLI — `research evaluate`, `research evaluations`, `research attest bundle`, `--reviewer-kind`

**Files:**
- Modify: `trader/mmr_cli.py` (research parser near the `research` subparsers; `_handle_research`; `_handle_research_review`; `_handle_research_attest`; new handlers and path helpers)
- Test: `tests/test_research_evaluate_cli.py` (new)

**Interfaces:**
- Consumes: Tasks 3, 6, 8, 9; `ensure_signing_keypair`, `default_key_paths` (`trader/automation/paper_materials.py`); `repo_root()`.
- Produces (CLI):
  - `mmr research evaluate SPEC [--dry-run] [--workers N]`
  - `mmr research evaluations [--limit N]`
  - `mmr research attest bundle ARTIFACT_ID`
  - `mmr research review submit ... --reviewer-kind {human,llm}` (required)
  - `research attest paper` is removed.
  - Helpers patched by tests: `_evaluation_paths() -> EvaluationPaths`, `_signing_key_paths() -> tuple[Path, Path]` (private, public), `_artifacts_root() -> Path`.
  - Failures print the message and exit with code 1.

- [ ] **Step 1: Write the failing tests**

```python
import argparse
import json

import pytest

from tests.research.evaluation_fixtures import (
    build_spec_file, write_costs_config, write_trend_bars, write_universe,
)
import trader.mmr_cli as cli
from trader.research.evaluation import EvaluationPaths


@pytest.fixture
def cli_env(tmp_path, tmp_duckdb_path, monkeypatch):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0002)
    costs = write_costs_config(tmp_path / 'execution_costs.yaml')
    paths = EvaluationPaths(tmp_duckdb_path, tmp_duckdb_path, 'Universes', str(costs),
                            tmp_path, tmp_path / 'reports', tmp_path / 'artifacts' / 'evaluations')
    monkeypatch.setenv('MMR_RESEARCH_DUCKDB', str(tmp_path / 'research.duckdb'))
    monkeypatch.setattr(cli, '_evaluation_paths', lambda: paths)
    monkeypatch.setattr(cli, '_signing_key_paths',
                        lambda: (tmp_path / 'keys' / 'signing.pem', tmp_path / 'keys' / 'verify.pem'))
    monkeypatch.setattr(cli, '_artifacts_root', lambda: tmp_path / 'artifacts')
    monkeypatch.setattr(cli, '_json_mode', True)
    return tmp_path


def _json_out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_parser_has_the_new_commands():
    parser = cli.build_parser()
    args = parser.parse_args(['research', 'evaluate', 'spec.yaml', '--dry-run', '--workers', '2'])
    assert (args.spec, args.dry_run, args.workers) == ('spec.yaml', True, 2)
    assert parser.parse_args(['research', 'attest', 'bundle', 'abc']).artifact_id == 'abc'
    with pytest.raises(SystemExit):
        parser.parse_args(['research', 'attest', 'paper', '--decision-id', 'd'])


def test_review_submit_requires_reviewer_kind():
    base = ['research', 'review', 'submit', '--artifact-id', 'a', '--decision-id', 'd',
            '--reviewer', 'r', '--economic-rationale', 'x', '--edge-survives-costs', 'x',
            '--known-failure-regimes', 'x', '--data-limits', 'x', '--parameter-sensitivity', 'x',
            '--operational-dependencies', 'x', '--capacity-and-decay', 'x',
            '--episode-dominance', 'x', '--holdout-opened-once']
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(base)
    assert cli.build_parser().parse_args(base + ['--reviewer-kind', 'llm']).reviewer_kind == 'llm'


def test_dry_run_counts_jobs(cli_env, capsys):
    spec = build_spec_file(cli_env)
    cli._handle_research_evaluate(argparse.Namespace(spec=str(spec), dry_run=True, workers=1))
    data = _json_out(capsys)['data']
    assert data['parameter_points'] == 5 and data['walk_forward_jobs'] == 2 * (3 + 4)


def test_evaluate_then_list(cli_env, capsys):
    spec = build_spec_file(cli_env)
    cli._handle_research_evaluate(argparse.Namespace(spec=str(spec), dry_run=False, workers=1))
    result = _json_out(capsys)['data']
    assert result['stage'] == 'pre_holdout'
    cli._handle_research_evaluations(argparse.Namespace(limit=5))
    assert _json_out(capsys)['data'][0]['family_id'] == result['family_id']


def test_bad_spec_exits_non_zero(cli_env):
    spec = build_spec_file(cli_env, conids=[1001])
    with pytest.raises(SystemExit) as exc:
        cli._handle_research_evaluate(argparse.Namespace(spec=str(spec), dry_run=False, workers=1))
    assert exc.value.code == 1


def test_attest_unknown_artifact_exits_non_zero(cli_env):
    with pytest.raises(SystemExit) as exc:
        cli._handle_research_attest(argparse.Namespace(attest_action='bundle', artifact_id='nope'))
    assert exc.value.code == 1
```

- [ ] **Step 2: Run them and watch them fail**

Run: `$PY -m pytest tests/test_research_evaluate_cli.py -q -p no:cacheprovider --timeout=300`
Expected: `AttributeError: module 'trader.mmr_cli' has no attribute '_evaluation_paths'` (and parser failures).

- [ ] **Step 3: Parser changes** (in `build_parser`, in the research section)

1. After the `import-legacy` parser, add:

```python
    re_eval = research_sub.add_parser(
        'evaluate', help='Run real walk-forward evidence for a strategy spec (paper-v1)')
    re_eval.add_argument('spec', help='Evaluation spec YAML (see docs/PAPER_AUTOMATION_SETUP.md)')
    re_eval.add_argument('--dry-run', action='store_true',
                         help='Validate the spec and show the job count; run nothing')
    re_eval.add_argument('--workers', type=int, default=0,
                         help='Parallel backtest processes (default: cpu count - 1, max 16)')
    re_list = research_sub.add_parser('evaluations', help='List recent evaluations')
    re_list.add_argument('--limit', type=int, default=20)
```

2. On `rr_submit`, add:

```python
    rr_submit.add_argument('--reviewer-kind', required=True, choices=['human', 'llm'],
                           help='Who wrote this review; live attestations need human')
```

3. Delete the whole `rat_paper = rat_sub.add_parser('paper', ...)` block and its arguments. Add instead:

```python
    rat_bundle = rat_sub.add_parser(
        'bundle', help='Sign the attestation for a PAPER_ELIGIBLE artifact and export its bundle')
    rat_bundle.add_argument('artifact_id', help='Artifact id (from `research evaluations`)')
```

4. Update the `research` epilog examples: replace the `research family create` example lines with `research evaluate research/orb_us.yaml --dry-run`, `research evaluate research/orb_us.yaml`, `research evaluations`, `research attest bundle <artifact_id>`.

- [ ] **Step 4: Handlers**

1. `_handle_research`: add branches `elif action == 'evaluate': _handle_research_evaluate(args)` and `elif action == 'evaluations': _handle_research_evaluations(args)`, and add both names to the usage string.

2. `_handle_research_review`: pass `reviewer_kind=args.reviewer_kind` to `OperatorReview(...)`.

3. `_handle_research_attest`: delete the `paper` branch (and any helper used only by it); add:

```python
    if action == 'bundle':
        _handle_research_attest_bundle(args)
        return
```

4. New helpers and handlers (place them after `_handle_research_review`):

```python
def _evaluation_paths():
    from trader.container import Container, ensure_config_dir
    from trader.research.evaluation import EvaluationPaths
    from trader.research.strategy_paths import repo_root
    from trader.simulation.execution_costs import EXECUTION_COSTS_FILE

    cfg = Container.instance().config()
    duckdb_path = cfg.get('duckdb_path', '')
    return EvaluationPaths(
        history_db=cfg.get('history_duckdb_path', '') or duckdb_path,
        universe_db=duckdb_path,
        universe_library=cfg.get('universe_library', 'Universes'),
        execution_costs=str(ensure_config_dir() / EXECUTION_COSTS_FILE),
        repo_root=repo_root(),
        reports_dir=Path('~/.local/share/mmr/reports').expanduser(),
        summaries_dir=_artifacts_root() / 'evaluations')


def _signing_key_paths():
    from trader.automation.paper_materials import default_key_paths
    from trader.container import ensure_config_dir

    private_pem, _verify_dir, public_pem = default_key_paths(ensure_config_dir())
    return private_pem, public_pem


def _artifacts_root():
    return Path('~/.local/share/mmr/artifacts').expanduser()


def _handle_research_evaluate(args: argparse.Namespace):
    """Run real walk-forward evidence for a spec (no service needed)."""
    from trader.data.universe import UniverseAccessor
    from trader.research.evaluation import EvaluationError, evaluate
    from trader.research.evaluation_data import EvaluationDataError
    from trader.research.evaluation_jobs import default_workers
    from trader.research.evaluation_spec import (
        EvaluationSpecError, load_evaluation_spec, neighbour_points,
    )
    from trader.simulation.execution_costs import ExecutionCostError, load_execution_costs_config

    paths = _evaluation_paths()
    try:
        spec = load_evaluation_spec(
            args.spec, universe_accessor=UniverseAccessor(paths.universe_db, paths.universe_library),
            costs_config=load_execution_costs_config(paths.execution_costs),
            repo_root=paths.repo_root)
        neighbours = neighbour_points(spec)
        if args.dry_run:
            print_json_result({
                'spec': spec.name, 'strategy': spec.strategy_path, 'class': spec.class_name,
                'conids': list(spec.conids), 'calendar': spec.calendar,
                'parameter_points': 1 + len(neighbours),
                'walk_forward_jobs': spec.folds * (3 + len(neighbours)),
                'holdout_jobs_if_gate_passes': 2,
            }, title='Evaluation dry run')
            return
        result = evaluate(spec, research_db=_research_db(), paths=paths, now=_research_now,
                          max_workers=args.workers or default_workers())
    except (EvaluationSpecError, EvaluationDataError, EvaluationError, ExecutionCostError) as exc:
        print_status(str(exc), success=False)
        sys.exit(1)
    print_json_result({
        'spec': result.spec_name, 'family_id': result.family_id, 'stage': result.stage,
        'state': result.state, 'artifact_id': result.artifact_id,
        'decision_digest': result.decision_digest, 'failed_rules': list(result.failed_rules),
        'missing_rules': list(result.missing_rules), 'report': str(result.report_path),
    }, title='Evaluation')


def _handle_research_evaluations(args: argparse.Namespace):
    from trader.research.evaluation_store import EvaluationRepository

    rows = EvaluationRepository(_research_db()).list(limit=args.limit)
    print_json_result([{
        'created_at': r.created_at, 'spec': r.spec_name, 'strategy': r.strategy_path,
        'class': r.class_name, 'family_id': r.family_id, 'stage': r.stage, 'state': r.state,
        'artifact_id': r.artifact_id, 'failed_rules': list(r.failed_rules),
        'missing_rules': list(r.missing_rules), 'report': r.report_path,
    } for r in rows], title='Evaluations')


def _handle_research_attest_bundle(args: argparse.Namespace):
    from trader.automation.paper_materials import PaperMaterialsError, ensure_signing_keypair
    from trader.research.attest_export import AttestExportError, attest_and_export

    private_pem, public_pem = _signing_key_paths()
    try:
        signer, _reused = ensure_signing_keypair(private_key_path=private_pem,
                                                 public_key_path=public_pem)
        path = attest_and_export(_research_db(), artifact_id=args.artifact_id, signer=signer,
                                 artifacts_root=_artifacts_root(), now=_research_now())
    except (AttestExportError, PaperMaterialsError) as exc:
        print_status(str(exc), success=False)
        sys.exit(1)
    print_json_result({'artifact_id': args.artifact_id, 'bundle': str(path),
                       'public_key_id': signer.public_key_id}, title='Attested bundle')
```

`Path` and `sys` are already imported at the top of `mmr_cli.py`; if `Path` is not, import it inside the helpers.

- [ ] **Step 5: Run the tests**

Run: `$PY -m pytest tests/test_research_evaluate_cli.py tests/test_research_config.py -q -p no:cacheprovider --timeout=300`
Expected: all pass.

- [ ] **Step 6: Full suite, then commit**

```bash
git add trader/mmr_cli.py tests/test_research_evaluate_cli.py
git commit -m "feat(cli): add research evaluate, evaluations and attest bundle" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Bind bundles to the strategy that runs

**Files:**
- Create: `trader/automation/strategy_binding.py`
- Modify: `trader/automation/artifact_verifier.py` (`VerifiedArtifact.attested_strategy`, populated in `verify`), `trader/strategy/strategy_runtime.py` (`_strategy_file_path`, `_verify_artifact_at_load`, `load_strategy`, `arm_paper_automation`)
- Modify tests: `tests/test_strategy_paper_arm.py`, `tests/test_strategy_artifact_soft_load.py`
- Test: `tests/automation/test_strategy_binding.py` (new)

**Interfaces:**
- Consumes: `compute_strategy_hash`, `normalize_strategy_path`; Task 9 `export_eligible_bundle` (tests).
- Produces:
  - `class StrategyBindingError(Exception)`.
  - `@dataclass(frozen=True) AttestedStrategy(strategy_path: str, class_name: str, source_digest: str, parameters: Mapping[str, Any], instruments: frozenset[str], bar_size: Optional[str], order_notional: Optional[float])`.
  - `load_attested_strategy(bundle_path: Path, *, source_digest: str, parameters: Mapping, instruments: Sequence[str]) -> AttestedStrategy` (reads `family.json`).
  - `check_strategy_binding(attested: AttestedStrategy | None, *, module_file: Path, class_name: str, params: Mapping | None, conids: Sequence | None, bar_size: str) -> None` — raises `StrategyBindingError` listing every difference.
  - `VerifiedArtifact.attested_strategy: Optional[AttestedStrategy] = None` (last field).
  - `StrategyRuntime._strategy_file_path(module: str) -> str`; `_verify_artifact_at_load(strategy_name, bundle_path, *, module=None, class_name=None, conids=None, bar_size=None, params=None)`.

- [ ] **Step 1: Write the failing tests `tests/automation/test_strategy_binding.py`**

```python
import dataclasses

import pytest

from tests.research.evaluation_fixtures import FIXED_NOW, export_eligible_bundle
from trader.automation.artifact_verifier import ArtifactVerifier
from trader.automation.paper_materials import export_fixture_paper_eligible_bundle
from trader.automation.strategy_binding import StrategyBindingError, check_strategy_binding
from trader.research.signing import AttestationSigner


@pytest.fixture
def bound(tmp_path, tmp_duckdb_path):
    exported = export_eligible_bundle(tmp_path, tmp_duckdb_path)
    verified = ArtifactVerifier([exported.signer.public_key]).verify(
        exported.bundle_path, 'paper', exported.artifact_id, FIXED_NOW)
    entry = dict(module_file=tmp_path / 'strategies' / 'time_of_day.py', class_name='TimeOfDay',
                 params={'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660,
                         'artifact_bundle_path': str(exported.bundle_path)},
                 conids=list(exported.spec.conids), bar_size='5 mins')
    return verified, entry


def test_matching_strategy_passes(bound):
    verified, entry = bound
    assert verified.attested_strategy.order_notional == 1900.0
    check_strategy_binding(verified.attested_strategy, **entry)


@pytest.mark.parametrize('change, message', [
    (lambda e: {**e, 'params': {**e['params'], 'ENTRY_MINUTE': 615}}, 'params'),
    (lambda e: {**e, 'conids': e['conids'][:-1]}, 'conids'),
    (lambda e: {**e, 'bar_size': '1 min'}, 'bar size'),
    (lambda e: {**e, 'class_name': 'Other'}, 'class'),
])
def test_any_difference_is_refused(bound, change, message):
    verified, entry = bound
    with pytest.raises(StrategyBindingError, match=message):
        check_strategy_binding(verified.attested_strategy, **change(entry))


def test_edited_strategy_file_is_refused(bound):
    verified, entry = bound
    entry['module_file'].write_text(entry['module_file'].read_text() + '\n# edited\n')
    with pytest.raises(StrategyBindingError, match='changed since attestation'):
        check_strategy_binding(verified.attested_strategy, **entry)


def test_old_fixture_bundle_is_refused(bound, tmp_path):
    _, entry = bound
    signer = AttestationSigner.generate()
    artifact_id = export_fixture_paper_eligible_bundle(signer=signer, artifacts_root=tmp_path / 'fx')
    fixture = ArtifactVerifier([signer.public_key]).verify(
        tmp_path / 'fx' / artifact_id, 'paper', artifact_id, FIXED_NOW)
    with pytest.raises(StrategyBindingError):
        check_strategy_binding(fixture.attested_strategy, **entry)


def test_missing_attested_strategy_is_refused(bound):
    _, entry = bound
    with pytest.raises(StrategyBindingError, match='no attested strategy'):
        check_strategy_binding(None, **entry)
```

Note: `FIXED_NOW` is 2026-10-04 and the fixture exporter in `paper_materials` uses `T0 = 2026-07-18` with a 90-day TTL (expires 2026-10-16), so it still verifies at `FIXED_NOW`. Task 13 changes this test's import to `tests.automation.fixture_bundle`.

- [ ] **Step 2: Run them and watch them fail**

Run: `$PY -m pytest tests/automation/test_strategy_binding.py -q -p no:cacheprovider --timeout=300`
Expected: import error for `trader.automation.strategy_binding`.

- [ ] **Step 3: Create `trader/automation/strategy_binding.py`**

```python
"""Is this bundle the evidence for the strategy that is about to run?

A bundle attests one strategy file (by content hash), one class, one set of
tunable params, one instrument list and one bar size. Anything else is a
different strategy, and running it under this bundle would be a false claim.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from trader.data.backtest_store import compute_strategy_hash
from trader.research.strategy_paths import normalize_strategy_path


class StrategyBindingError(Exception):
    """The bundle does not attest this strategy; the message lists every difference."""


@dataclass(frozen=True)
class AttestedStrategy:
    strategy_path: str
    class_name: str
    source_digest: str
    parameters: Mapping[str, Any]
    instruments: frozenset
    bar_size: Optional[str]
    order_notional: Optional[float]


def load_attested_strategy(bundle_path: Path, *, source_digest: str,
                           parameters: Mapping[str, Any],
                           instruments: Sequence[str]) -> AttestedStrategy:
    family = json.loads((Path(bundle_path) / 'family.json').read_text(encoding='utf-8'))
    protocol = family.get('validation_protocol') or {}
    notional = (family.get('cost_model') or {}).get('order_notional')
    return AttestedStrategy(
        strategy_path=family['strategy_path'], class_name=family['class_name'],
        source_digest=source_digest, parameters=dict(parameters),
        instruments=frozenset(str(i) for i in instruments),
        bar_size=protocol.get('bar_size'),
        order_notional=float(notional) if notional is not None else None)


def _tunables(params: Optional[Mapping[str, Any]]) -> dict:
    return {k: v for k, v in (params or {}).items() if str(k).isupper()}


def _bar_size_key(value: Any) -> str:
    return ' '.join(str(value).lower().split())


def check_strategy_binding(attested: Optional[AttestedStrategy], *, module_file: Path,
                           class_name: str, params: Optional[Mapping[str, Any]],
                           conids: Optional[Sequence[Any]], bar_size: str) -> None:
    if attested is None:
        raise StrategyBindingError('bundle has no attested strategy (family.json missing)')
    problems = []
    if normalize_strategy_path(str(module_file)) != normalize_strategy_path(attested.strategy_path):
        problems.append(f'strategy file {module_file} is not the attested {attested.strategy_path}')
    if class_name != attested.class_name:
        problems.append(f'class {class_name!r} is not the attested {attested.class_name!r}')
    if compute_strategy_hash(str(module_file)) != attested.source_digest:
        problems.append(f'strategy file {Path(module_file).name} changed since attestation')
    if _tunables(params) != _tunables(attested.parameters):
        problems.append(f'params {_tunables(params)} differ from attested {_tunables(attested.parameters)}')
    actual_conids = {str(c) for c in (conids or ())}
    if actual_conids != set(attested.instruments):
        problems.append(f'conids {sorted(actual_conids)} differ from attested {sorted(attested.instruments)}')
    if attested.bar_size is None or _bar_size_key(bar_size) != _bar_size_key(attested.bar_size):
        problems.append(f'bar size {bar_size!r} differs from attested {attested.bar_size!r}')
    if problems:
        raise StrategyBindingError('; '.join(problems))
```

- [ ] **Step 4: Populate `VerifiedArtifact.attested_strategy`** (`trader/automation/artifact_verifier.py`)

1. `from trader.automation.strategy_binding import AttestedStrategy, load_attested_strategy` at the top.
2. Add the last field to `VerifiedArtifact`:

```python
    # Strategy this bundle attests (file digest, class, params, instruments,
    # bar size, order notional) for runtime binding checks.
    attested_strategy: Optional[AttestedStrategy] = None
```

3. In `verify`, after `parameters = self._load_parameters(bundle_path)`:

```python
        try:
            attested_strategy = load_attested_strategy(
                bundle_path, source_digest=attestation.source_digest,
                parameters=parameters, instruments=verified.permitted_instruments)
        except (OSError, ValueError, KeyError) as exc:
            raise ArtifactVerifierError(f"failed to read family.json: {exc}") from exc
```

and pass `attested_strategy=attested_strategy` to the returned `VerifiedArtifact(...)`.

- [ ] **Step 5: Check the binding in the strategy runtime** (`trader/strategy/strategy_runtime.py`)

1. New method next to `_verify_artifact_at_load`:

```python
    def _strategy_file_path(self, module: str) -> str:
        """The file ``load_strategy`` loads for ``module`` (same resolution rules)."""
        strategies_dir = os.path.abspath(os.path.expanduser(self.strategies_directory))
        requested = os.path.expanduser(module)
        if os.path.isabs(requested):
            return os.path.abspath(requested)
        filepath = os.path.abspath(os.path.join(strategies_dir, requested))
        if not os.path.exists(filepath):
            filepath = os.path.abspath(requested)
        return filepath
```

In `load_class_from_file`, replace the inline `if os.path.isabs(requested): ... else: ...` resolution with `filepath = self._strategy_file_path(filename)` and keep the "outside strategies directory" check and everything after it unchanged.

2. `_verify_artifact_at_load` signature becomes:

```python
    def _verify_artifact_at_load(self, strategy_name: str, artifact_bundle_path_str: str, *,
                                 module: Optional[str] = None, class_name: Optional[str] = None,
                                 conids: Optional[List[int]] = None, bar_size: Optional[str] = None,
                                 params: Optional[Dict] = None) -> None:
```

Right after `verified = verifier.verify(...)`:

```python
        if module is not None:
            from trader.automation.strategy_binding import check_strategy_binding
            check_strategy_binding(
                verified.attested_strategy, module_file=Path(self._strategy_file_path(module)),
                class_name=class_name or '', params=params, conids=conids, bar_size=bar_size or '')
```

(`from pathlib import Path as _Path` is already imported locally in this method; use `_Path`.)

3. `load_strategy`: call `self._verify_artifact_at_load(name, artifact_bundle_path_str, module=module, class_name=class_name, conids=conids, bar_size=bar_size_str, params=params)`.

4. `arm_paper_automation`: before `self._verify_artifact_at_load(name, bundle)`:

```python
        strategy = self.get_strategy(name)
        if strategy is None:
            self.disarm_paper_automation()
            raise PaperAutomationArmError(
                'STRATEGY_NOT_FOUND', f'strategy {name!r} is not loaded; load it before arming')
```

and call `self._verify_artifact_at_load(name, bundle, module=strategy.module, class_name=strategy.class_name, conids=strategy.conids, bar_size=str(strategy.bar_size), params=strategy.params)`.

- [ ] **Step 6: Update the runtime tests that fake verification**

- `tests/test_strategy_paper_arm.py`: `_fake_verify(strategy_name, bundle_path, **binding)`; before calling `arm_paper_automation`, add
  `monkeypatch.setattr(rt, 'get_strategy', lambda name: SimpleNamespace(module='strategies/orb.py', class_name='OpeningRangeBreakout', conids=[51529211], bar_size='1 min', params={}))` (import `SimpleNamespace` from `types`). Add one test: `arm_paper_automation` for a name `get_strategy` returns `None` for raises `PaperAutomationArmError` with code `STRATEGY_NOT_FOUND`.
- `tests/test_strategy_artifact_soft_load.py`: in `test_verify_runs_when_automation_enabled`, the `assert_called_once_with(...)` gains the binding keywords exactly as `load_strategy` passes them (`module=..., class_name=..., conids=..., bar_size=..., params=...`, values from that test's `load_strategy` call).

- [ ] **Step 7: Run the tests**

Run: `$PY -m pytest tests/automation tests/test_strategy_paper_arm.py tests/test_strategy_artifact_soft_load.py tests/test_strategy_load.py -q -p no:cacheprovider --timeout=300`
Expected: all pass.

- [ ] **Step 8: Full suite, then commit**

```bash
git add trader/automation/strategy_binding.py trader/automation/artifact_verifier.py trader/strategy/strategy_runtime.py tests/automation/test_strategy_binding.py tests/test_strategy_paper_arm.py tests/test_strategy_artifact_soft_load.py
git commit -m "feat(automation): refuse bundles that do not attest the strategy being run" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Live path — notional sizing, source digest on the wire, closing sells, bundle root

**Files:**
- Modify: `trader/strategy/intent_emitter.py`, `trader/strategy/strategy_runtime.py` (`_maybe_build_intent_emitter`, emitter call site), `trader/messaging/production_api.py` (`ExecuteAutomatedIntentRequest`), `trader/automation/automated_intent_command.py`, `trader/automation/session_risk.py`, `trader/trading/command_stack.py`
- Test: `tests/test_intent_emitter_sizing.py` (new), `tests/automation/test_automated_command_boundary.py` (append), `tests/automation/test_session_risk.py` (append), `tests/test_bundle_root.py` (new)

**Interfaces:**
- Consumes: Task 11 `VerifiedArtifact.attested_strategy`.
- Produces:
  - `IntentEmitterContext.strategy_source_digest: str = ''`, `IntentEmitterContext.order_notional: Optional[Decimal] = None` (last fields).
  - `IntentEmitter.on_signal(..., reference_price: Optional[float] = None)`; `build_intent(..., reference_price: Optional[float] = None)`.
  - Wire body key `strategy_source_digest`; `ExecuteAutomatedIntentRequest.strategy_source_digest: Optional[str] = None`.
  - Rejection code `STRATEGY_SOURCE_MISMATCH` in `automated_intent_command`.
  - `command_stack._bundle_root_for(bundle_path: str) -> Path`.

- [ ] **Step 1: Write the failing tests**

`tests/test_intent_emitter_sizing.py`:

```python
import datetime as dt
from decimal import Decimal

from trader.automation.artifact_verifier import VerifiedArtifact
from trader.objects import Action
from trader.strategy.intent_emitter import IntentEmitter, IntentEmitterContext
from trader.trading.strategy import Signal

BAR = dt.datetime(2026, 10, 5, 15, 0, tzinfo=dt.timezone.utc)


class RecordingClient:
    def __init__(self):
        self.bodies = []

    def call(self, method, body, response_type):
        self.bodies.append(body)
        return {'command_id': body['command_id'], 'state': 'SUBMITTED'}


def _emitter(order_notional=Decimal('1900')):
    artifact = VerifiedArtifact(
        artifact_id='a1', manifest_digest='m1', dataset_manifest_digest='d1',
        parameters={'stop_price': '90'}, allowlist=('1001',), max_gross_allocation=0.05,
        expires_at=BAR + dt.timedelta(days=30), public_key_id='k', verification_reason_codes=())
    client = RecordingClient()
    context = IntentEmitterContext(
        enabled=True, live_enabled=False, strategy_name='trend', artifact=artifact,
        artifact_digest='m1', eligibility_attestation_digest='m1',
        artifact_bundle_digest='sha256:m1', account_mode='paper',
        strategy_source_digest='src-1', order_notional=order_notional)
    return IntentEmitter(command_client=client, context=context, now=lambda: BAR), client


def _signal(quantity=0):
    return Signal(source_name='trend', action=Action.BUY, probability=0.5, risk=0.5,
                  conid=1001, quantity=quantity)


def test_buy_without_quantity_uses_the_attested_notional():
    emitter, client = _emitter()
    emitter.on_signal(strategy_name='trend', signal=_signal(), completed_bar_timestamp=BAR,
                      session_id='s', reference_price=100.0)
    assert client.bodies[0]['requested_quantity'] == '19'
    assert client.bodies[0]['strategy_source_digest'] == 'src-1'


def test_explicit_quantity_is_kept():
    emitter, client = _emitter()
    emitter.on_signal(strategy_name='trend', signal=_signal(quantity=5), completed_bar_timestamp=BAR,
                      session_id='s', reference_price=100.0)
    assert client.bodies[0]['requested_quantity'] == '5'


def test_no_notional_and_no_quantity_sends_none():
    emitter, client = _emitter(order_notional=None)
    emitter.on_signal(strategy_name='trend', signal=_signal(), completed_bar_timestamp=BAR,
                      session_id='s', reference_price=100.0)
    assert client.bodies[0]['requested_quantity'] is None
```

Append to `tests/automation/test_automated_command_boundary.py`:

```python
class AttestingVerifier(FakeArtifactVerifier):
    def verify(self, bundle_path, expected_mode, expected_artifact_id, now, *, revoked_digests=()):
        artifact = super().verify(bundle_path, expected_mode, expected_artifact_id, now)
        artifact.attested_strategy = SimpleNamespace(source_digest='src-attested')
        return artifact


@pytest.mark.parametrize('sent, expected_error', [
    ('src-attested', None),
    ('src-other', 'STRATEGY_SOURCE_MISMATCH'),
    (None, 'STRATEGY_SOURCE_MISMATCH'),
])
def test_intent_source_digest_must_match_the_attested_file(tmp_path, sent, expected_error):
    stack = _build_stack(tmp_path, verifier=AttestingVerifier())
    intent = make_intent()
    (tmp_path / 'bundles').mkdir()
    (tmp_path / 'bundles' / ARTIFACT_DIGEST.replace(':', '_')).mkdir()
    body = intent_to_request_body(intent)
    body['strategy_source_digest'] = sent
    receipt = stack.coordinator.execute(CommandRequest(
        command_id=intent.command_id, action='execute_automated_intent', account_id=ACCOUNT,
        target_type='intent', target_id=intent.intent_id, expected_version=None,
        body=body, source='strategy_service'))
    assert receipt.error_code == expected_error
```

Append to `tests/automation/test_session_risk.py` (reuses its helpers `make_controller`, `make_intent`, `make_artifact`, `make_approval`, `make_broker`, `_position`, `make_session`, `CONID`):

```python
def test_sell_without_quantity_closes_the_held_position():
    held = make_broker(positions=(_position(CONID, 25, 2_500.0),))
    decision = make_controller().evaluate(
        make_intent(side="SELL", requested_quantity=None,
                    stop_policy=StopPolicy(stop_price=Decimal("101"), order_type="STP")),
        make_artifact(), make_approval(broker=held), make_session(),
        AllocationCeiling(max_gross_fraction=0.06))
    assert "QUANTITY_REQUIRED" not in decision.reason_codes
    assert decision.approved_quantity == Decimal("25")


def test_sell_without_quantity_and_nothing_held_is_long_only():
    decision = make_controller().evaluate(
        make_intent(side="SELL", requested_quantity=None,
                    stop_policy=StopPolicy(stop_price=Decimal("101"), order_type="STP")),
        make_artifact(), make_approval(), make_session(),
        AllocationCeiling(max_gross_fraction=0.06))
    assert "LONG_ONLY" in decision.reason_codes
```

Also append to `tests/automation/test_automated_command_boundary.py` — the production RPC handler builds the command body with `model_dump`, so the field must be on the model and survive it:

```python
def test_wire_model_carries_the_strategy_source_digest():
    wire = intent_to_wire(make_intent())
    wire['strategy_source_digest'] = 'src-1'
    parsed = ExecuteAutomatedIntentRequest(**wire)
    assert parsed.model_dump(mode='json')['strategy_source_digest'] == 'src-1'
```

(`ExecuteAutomatedIntentRequest` is already imported there for `test_wire_model_forbids_account_id_and_extra_fields`; import it from `trader.messaging.production_api` if not.)

`tests/test_bundle_root.py`:

```python
from pathlib import Path

from trader.trading.command_stack import _bundle_root_for


def test_sha256_bundle_dir_resolves_to_its_parent(tmp_path):
    assert _bundle_root_for(str(tmp_path / 'artifacts' / 'sha256_abc')) == tmp_path / 'artifacts'


def test_legacy_artifact_dir_still_resolves_to_its_parent(tmp_path):
    assert _bundle_root_for(str(tmp_path / 'artifacts' / 'artifact-1')) == tmp_path / 'artifacts'


def test_root_dir_is_kept(tmp_path):
    assert _bundle_root_for(str(tmp_path / 'artifacts')) == tmp_path / 'artifacts'
```

- [ ] **Step 2: Run them and watch them fail**

Run: `$PY -m pytest tests/test_intent_emitter_sizing.py tests/automation/test_automated_command_boundary.py tests/automation/test_session_risk.py tests/test_bundle_root.py -q -p no:cacheprovider`
Expected: failures for the missing context fields, the missing wire field (`extra="forbid"` rejects `strategy_source_digest`), `QUANTITY_REQUIRED`, and the missing `_bundle_root_for`.

- [ ] **Step 3: Implement**

1. `trader/strategy/intent_emitter.py`
   - `IntentEmitterContext`: add `strategy_source_digest: str = ""` and `order_notional: Optional[Decimal] = None` as the last fields (add `Optional` to the typing import if missing).
   - `on_signal(..., session_id: str, reference_price: Optional[float] = None)`; pass `reference_price=reference_price` to `build_intent`.
   - `build_intent(..., session_id: str, reference_price: Optional[float] = None)`; replace the quantity block with:

```python
        qty = None
        if signal.quantity and signal.quantity > 0:
            qty = _dec(signal.quantity)
        elif meta.get("requested_quantity") is not None:
            qty = _dec(meta["requested_quantity"])
        elif side == "BUY" and self._ctx.order_notional and reference_price:
            # Same size the evidence was measured at (attested order notional).
            shares = int(self._ctx.order_notional // Decimal(str(reference_price)))
            qty = Decimal(shares) if shares > 0 else None
```

   - `_to_wire`: add `"strategy_source_digest": self._ctx.strategy_source_digest or None,` to the returned dict.

2. `trader/strategy/strategy_runtime.py`
   - `_maybe_build_intent_emitter`: before building the context,

```python
        attested = getattr(self._verified_artifact, 'attested_strategy', None)
        order_notional = (Decimal(str(attested.order_notional))
                          if attested is not None and attested.order_notional else None)
```

   and pass `strategy_source_digest=attested.source_digest if attested is not None else ''` and `order_notional=order_notional` to `IntentEmitterContext(...)` (`from decimal import Decimal` at the top if missing).
   - Emitter call site (`emitter.on_signal(...)`): add `reference_price=float(frame['close'].iloc[-1]),`.

3. `trader/messaging/production_api.py` — `ExecuteAutomatedIntentRequest`, after `artifact_bundle_digest: str`:

```python
    # Content hash of the strategy file the emitting runtime loaded; the trader
    # checks it against the attested source digest.
    strategy_source_digest: Optional[str] = None
```

4. `trader/automation/automated_intent_command.py` — right after the successful `artifact = self._verifier.verify(...)` block (before `self._transition(cmd, "RECEIVED", "VALIDATED")`):

```python
        attested = getattr(artifact, "attested_strategy", None)
        if attested is not None and cmd.body.get("strategy_source_digest") != attested.source_digest:
            self._transition(cmd, "RECEIVED", "REJECTED", error_code="STRATEGY_SOURCE_MISMATCH")
            return self._receipt(
                cmd.command_id, "REJECTED", "STRATEGY_SOURCE_MISMATCH", False,
                outcome={"detail": "intent strategy source digest does not match the attested file"},
            )
```

5. `trader/automation/session_risk.py` — replace

```python
        qty = intent.requested_quantity
        if qty is None:
            reasons.append("QUANTITY_REQUIRED")
            qty = Decimal("0")
```

with

```python
        qty = intent.requested_quantity
        if qty is None and intent.side == "SELL" and held > 0:
            qty = Decimal(str(held))  # an exit without a size closes the position
        if qty is None:
            reasons.append("QUANTITY_REQUIRED")
            qty = Decimal("0")
```

6. `trader/trading/command_stack.py` — add at module level:

```python
def _bundle_root_for(bundle_path: str) -> _Path:
    """A bundle directory's parent is the root order dispatch resolves digests under."""
    root = _Path(_os.path.abspath(_os.path.expanduser(bundle_path)))
    return root.parent if root.name.startswith(("artifact-", "sha256_")) else root
```

(use the module's existing `os`/`Path` import names; if they are imported locally as `_os`/`_Path` inside the function, import `os` and `Path` at module level for this helper), and replace the two lines at the `bundle_root = ...` site with `bundle_root = _bundle_root_for(bundle_path)`.

- [ ] **Step 4: Run the tests**

Run: `$PY -m pytest tests/test_intent_emitter_sizing.py tests/automation tests/test_bundle_root.py tests/test_strategy_paper_arm.py tests/test_command_stack.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Full suite, then commit**

```bash
git add trader/strategy/intent_emitter.py trader/strategy/strategy_runtime.py trader/messaging/production_api.py trader/automation/automated_intent_command.py trader/automation/session_risk.py trader/trading/command_stack.py tests/test_intent_emitter_sizing.py tests/automation/test_automated_command_boundary.py tests/automation/test_session_risk.py tests/test_bundle_root.py
git commit -m "feat(automation): size paper orders from the attested notional and pin the source digest" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 13: Activate uses only real, bound bundles

**Files:**
- Create: `trader/automation/bundle_finder.py`, `tests/automation/fixture_bundle.py`
- Modify: `trader/automation/paper_activation.py`, `trader/automation/paper_materials.py`, `scripts/bootstrap_paper_automation.py`
- Modify tests: `tests/automation/test_paper_activation.py`, `tests/automation/test_paper_hot_arm.py`, `tests/automation/test_paper_materials.py`, `tests/test_command_stack.py`, `tests/automation/test_strategy_binding.py`
- Test: `tests/automation/test_bundle_finder.py` (new)

**Interfaces:**
- Consumes: Task 6 `describe_latest_evaluation`; Task 9 `export_eligible_bundle` (tests); Task 11 `check_strategy_binding`, `VerifiedArtifact.attested_strategy`; `ArtifactVerifier`; `signing.load_verify_key`; `resolve_strategy_file`.
- Produces:
  - `@dataclass(frozen=True) EligibleBundle(path: Path, artifact_id: str, expires_at: datetime)`.
  - `class NoEligibleBundle(Exception)` with `.reasons: tuple[str, ...]`.
  - `find_eligible_bundle(*, artifacts_root: Path, verifier, strategy: Mapping[str, Any], strategy_file: Path, now: datetime) -> EligibleBundle` — newest (by `expires_at`) `sha256_*` bundle that verifies in paper mode and is bound to `strategy`.
  - `PaperAutomationActivationService._eligible_bundle(strategy_name, strategy, trader_data) -> EligibleBundle`; refusal code `NO_ELIGIBLE_BUNDLE`.
  - `tests/automation/fixture_bundle.py::export_fixture_paper_eligible_bundle(*, signer, artifacts_root) -> str` (moved verbatim, test-only).

- [ ] **Step 1: Move the fixture out of production code**

1. Create `tests/automation/fixture_bundle.py` containing, verbatim from `trader/automation/paper_materials.py`: the imports it needs, `T0`, `_evidence`, `_try_reuse_existing_fixture_bundle` and `export_fixture_paper_eligible_bundle`, plus this module docstring:

```python
"""Test-only: a structurally valid PAPER_ELIGIBLE bundle with made-up evidence.

It used to arm paper automation; production now only accepts bundles from
`mmr research attest bundle`. Plumbing tests still need a signed bundle.
"""
```

2. In `trader/automation/paper_materials.py`, delete `T0`, `_evidence`, `_try_reuse_existing_fixture_bundle`, `export_fixture_paper_eligible_bundle` and every import only they used. Keep `PaperMaterialsError`, `default_key_paths`, `read_allocation_binding_hints`, `_write_private_key`, `_write_public_key`, `ensure_signing_keypair`. Update the module docstring to `"""Paper automation signing keys and bundle hints (no bundle creation)."""`.

3. Change the imports in `tests/automation/test_paper_materials.py`, `tests/automation/test_paper_hot_arm.py`, `tests/test_command_stack.py` and `tests/automation/test_strategy_binding.py` from `trader.automation.paper_materials import export_fixture_paper_eligible_bundle` to `tests.automation.fixture_bundle import export_fixture_paper_eligible_bundle` (`grep -rn export_fixture_paper_eligible_bundle tests trader scripts` must show no `trader.` importer afterwards).

- [ ] **Step 2: Write the failing tests `tests/automation/test_bundle_finder.py`**

```python
import shutil

import pytest

from tests.automation.fixture_bundle import export_fixture_paper_eligible_bundle
from tests.research.evaluation_fixtures import FIXED_NOW, export_eligible_bundle
from trader.automation.artifact_verifier import ArtifactVerifier
from trader.automation.bundle_finder import NoEligibleBundle, find_eligible_bundle


@pytest.fixture
def exported(tmp_path, tmp_duckdb_path):
    return export_eligible_bundle(tmp_path, tmp_duckdb_path)


def _strategy(exported, **overrides):
    entry = {'name': 'time_of_day', 'module': 'strategies/time_of_day.py',
             'class_name': 'TimeOfDay', 'bar_size': '5 mins',
             'conids': list(exported.spec.conids),
             'params': {'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660}}
    entry.update(overrides)
    return entry


def _find(exported, tmp_path, strategy):
    return find_eligible_bundle(
        artifacts_root=exported.bundle_path.parent,
        verifier=ArtifactVerifier([exported.signer.public_key]), strategy=strategy,
        strategy_file=tmp_path / 'strategies' / 'time_of_day.py', now=FIXED_NOW)


def test_finds_the_bound_bundle(exported, tmp_path):
    found = _find(exported, tmp_path, _strategy(exported))
    assert found.path == exported.bundle_path and found.artifact_id == exported.artifact_id


def test_yaml_changed_after_attesting_is_refused(exported, tmp_path):
    with pytest.raises(NoEligibleBundle) as exc:
        _find(exported, tmp_path, _strategy(exported, params={'ENTRY_MINUTE': 615, 'EXIT_MINUTE': 660}))
    assert any('params' in reason for reason in exc.value.reasons)


def test_unbound_bundles_next_to_the_bound_one_are_skipped(exported, tmp_path):
    fixture_id = export_fixture_paper_eligible_bundle(
        signer=exported.signer, artifacts_root=tmp_path / 'fx')
    shutil.copytree(tmp_path / 'fx' / fixture_id, exported.bundle_path.parent / 'sha256_fixture')
    assert _find(exported, tmp_path, _strategy(exported)).path == exported.bundle_path


def test_empty_artifacts_root_is_refused(exported, tmp_path):
    with pytest.raises(NoEligibleBundle):
        find_eligible_bundle(artifacts_root=tmp_path / 'none',
                             verifier=ArtifactVerifier([exported.signer.public_key]),
                             strategy=_strategy(exported),
                             strategy_file=tmp_path / 'strategies' / 'time_of_day.py',
                             now=FIXED_NOW)
```

- [ ] **Step 3: Run them and watch them fail**

Run: `$PY -m pytest tests/automation/test_bundle_finder.py -q -p no:cacheprovider --timeout=300`
Expected: import error for `trader.automation.bundle_finder`.

- [ ] **Step 4: Create `trader/automation/bundle_finder.py`**

```python
"""Activate's only source of bundles: real, eligible, and bound to the strategy.

Every candidate under ``artifacts/sha256_*`` must verify in paper mode and pass
the strategy binding check; the newest by expiry wins. Each rejected candidate
keeps its reason so a refusal can explain itself.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from trader.automation.strategy_binding import check_strategy_binding


@dataclass(frozen=True)
class EligibleBundle:
    path: Path
    artifact_id: str
    expires_at: dt.datetime


class NoEligibleBundle(Exception):
    def __init__(self, message: str, reasons: tuple[str, ...] = ()):
        super().__init__(message)
        self.reasons = reasons


def _manifest_artifact_id(bundle_dir: Path) -> Optional[str]:
    try:
        return json.loads((bundle_dir / 'manifest.json').read_text(encoding='utf-8')).get('artifact_id')
    except (OSError, ValueError):
        return None


def find_eligible_bundle(*, artifacts_root: Path, verifier: Any, strategy: Mapping[str, Any],
                         strategy_file: Path, now: dt.datetime) -> EligibleBundle:
    candidates: list[EligibleBundle] = []
    reasons: list[str] = []
    bundle_dirs = sorted(artifacts_root.glob('sha256_*')) if artifacts_root.is_dir() else []
    for bundle_dir in bundle_dirs:
        artifact_id = _manifest_artifact_id(bundle_dir)
        if not artifact_id:
            reasons.append(f'{bundle_dir.name[:24]}: unreadable manifest')
            continue
        try:
            verified = verifier.verify(bundle_dir, 'paper', artifact_id, now)
            check_strategy_binding(
                verified.attested_strategy, module_file=strategy_file,
                class_name=str(strategy.get('class_name', '')), params=strategy.get('params'),
                conids=strategy.get('conids'), bar_size=str(strategy.get('bar_size', '')))
        except Exception as exc:  # each bundle's reason is kept; the caller raises once
            reasons.append(f'{bundle_dir.name[:24]}: {exc}')
            continue
        candidates.append(EligibleBundle(bundle_dir, artifact_id, verified.expires_at))
    if not candidates:
        raise NoEligibleBundle(
            f'no eligible bundle under {artifacts_root} is bound to this strategy', tuple(reasons))
    return max(candidates, key=lambda bundle: bundle.expires_at)
```

- [ ] **Step 5: Use it in `paper_activation.py`**

1. Imports: remove `export_fixture_paper_eligible_bundle` and `ensure_signing_keypair` from the `paper_materials` import (keep `PaperMaterialsError`, `default_key_paths`, `read_allocation_binding_hints`); add

```python
from trader.automation.artifact_verifier import ArtifactVerifier
from trader.automation.bundle_finder import EligibleBundle, NoEligibleBundle, find_eligible_bundle
from trader.research.evaluation_store import describe_latest_evaluation
from trader.research.signing import load_verify_key
from trader.research.strategy_paths import resolve_strategy_file
```

2. New method on `PaperAutomationActivationService`:

```python
    def _eligible_bundle(self, strategy_name: str, strategy: dict,
                         trader_data: dict) -> EligibleBundle:
        _, verify_dir, _ = default_key_paths(self._config_dir)
        keys = ([load_verify_key(str(p)) for p in sorted(verify_dir.glob("*.pem"))]
                if verify_dir.is_dir() else [])
        if not keys:
            raise PaperAutomationActivationError(
                "NO_ELIGIBLE_BUNDLE",
                f"no verify keys under {verify_dir}; run `mmr research attest bundle <artifact_id>` first",
            )
        module = str(strategy.get("module", ""))
        strategy_file = resolve_strategy_file(
            module, str(trader_data.get("strategies_directory", "strategies")))
        try:
            return find_eligible_bundle(
                artifacts_root=self._share_dir / "artifacts", verifier=ArtifactVerifier(keys),
                strategy=strategy, strategy_file=strategy_file, now=self._now())
        except NoEligibleBundle as exc:
            latest = describe_latest_evaluation(
                self._share_dir / "artifacts" / "evaluations", module,
                str(strategy.get("class_name", "")))
            checked = "; ".join(exc.reasons[:5])
            message = f"{strategy_name}: {exc}. {latest}"
            if checked:
                message += f". Bundles checked: {checked}"
            self._last_error = message
            raise PaperAutomationActivationError("NO_ELIGIBLE_BUNDLE", message) from exc
```

3. `_activate_restart_required`: replace the block from `private_key_path, verify_dir, public_key_path = default_key_paths(...)` through `bundle_path = self._share_dir / "artifacts" / artifact_id` with:

```python
        _, verify_dir, _ = default_key_paths(self._config_dir)
        eligible = self._eligible_bundle(strategy_name, strategy, trader_data)
        artifact_id = eligible.artifact_id
        bundle_path = eligible.path
```

and return `"reused_existing_keys": True` (keys are no longer generated here).

4. `_activate_hot_arm`: delete the `prepare_keys` and `export_artifact` phases (from `private_key_path, verify_dir, public_key_path = default_key_paths(...)` through `bundle_path = self._share_dir / "artifacts" / artifact_id`). Put the bundle lookup **before** the `try:` — nothing is committed yet, so a refusal needs no compensation, and the `except Exception` that turns errors into `HOT_ARM_FAILED` must not swallow `NO_ELIGIBLE_BUNDLE`:

```python
        _, verify_dir, _ = default_key_paths(self._config_dir)
        self._phase = "find_bundle"
        eligible = self._eligible_bundle(strategy_name, strategy, trader_data)
        artifact_id = eligible.artifact_id
        bundle_path = eligible.path
        try:
            self._phase = "trader_commit"
            # ... the existing trader_commit / strategy_commit / verify body, unchanged
```

and pass `reused=True` where the old code passed `reused=reused`.

- [ ] **Step 6: Update the activation tests**

- `tests/automation/test_paper_activation.py`: delete `_fake_export`. Replace every `patch("trader.automation.paper_activation.export_fixture_paper_eligible_bundle", ...)` with

```python
patch.object(PaperAutomationActivationService, "_eligible_bundle",
             lambda self, name, strategy, trader_data: EligibleBundle(
                 tmp_path / "share" / "artifacts" / f"sha256_{ARTIFACT_ID}", ARTIFACT_ID, NOW))
```

  (import `EligibleBundle` from `trader.automation.bundle_finder`). Expected `artifact_bundle_path` values become `.../artifacts/sha256_<ARTIFACT_ID>`. Replace `test_activate_reuses_existing_signing_keys` with:

```python
def test_activate_refuses_without_an_eligible_bundle(tmp_path: Path) -> None:
    service = _service(tmp_path)
    verify_dir = tmp_path / "config" / "keys" / "verify"
    verify_dir.mkdir(parents=True)
    from trader.research.signing import AttestationSigner
    (verify_dir / "k.pem").write_bytes(AttestationSigner.generate().public_key_pem())
    with pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name="orb_gld")
    assert exc.value.code == "NO_ELIGIBLE_BUNDLE"
    assert "no evaluation found" in str(exc.value)


def test_activate_refuses_without_verify_keys(tmp_path: Path) -> None:
    with pytest.raises(PaperAutomationActivationError) as exc:
        _service(tmp_path).activate(strategy_name="orb_gld")
    assert exc.value.code == "NO_ELIGIBLE_BUNDLE"
    assert "verify keys" in str(exc.value)
```

  and add an end-to-end test with a real bound bundle:

```python
def test_activate_arms_a_real_bound_bundle(tmp_path: Path, tmp_duckdb_path) -> None:
    from tests.research.evaluation_fixtures import export_eligible_bundle
    from trader.trading.command_stack import _bundle_root_for

    repo = tmp_path / "repo"
    repo.mkdir()
    exported = export_eligible_bundle(repo, tmp_duckdb_path)
    service = _service(tmp_path, now=lambda: FIXED_NOW, strategies=[{
        "name": "time_of_day", "module": str(repo / "strategies" / "time_of_day.py"),
        "class_name": "TimeOfDay", "bar_size": "5 mins", "conids": list(exported.spec.conids),
        "params": {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}}])
    verify_dir = tmp_path / "config" / "keys" / "verify"
    verify_dir.mkdir(parents=True)
    (verify_dir / "research.pem").write_bytes(exported.signer.public_key_pem())
    shutil.copytree(exported.bundle_path.parent, tmp_path / "share" / "artifacts")

    result = service.activate(strategy_name="time_of_day")

    assert result["artifact_id"] == exported.artifact_id
    assert _bundle_root_for(result["artifact_bundle_path"]) == tmp_path / "share" / "artifacts"
```

  `import shutil` at the top. Give `_service` a `now: Callable[[], dt.datetime] = lambda: NOW` parameter passed to the service, and build this test's service with `now=lambda: FIXED_NOW` (import `FIXED_NOW` from `tests.research.evaluation_fixtures`): the bundle is attested at 2026-10-04, after the module's `NOW`.

- `tests/automation/test_paper_hot_arm.py`: patch `_eligible_bundle` the same way instead of the fixture exporter; drop any `fail_after="export_artifact"` / `"prepare_keys"` cases (those phases no longer exist); `retry["reused_existing_keys"] is True` stays true. Add:

```python
def test_hot_arm_refusal_keeps_its_code(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name="orb_gld", reason="go")
    assert exc.value.code == "NO_ELIGIBLE_BUNDLE"
    assert service.status().lifecycle != "armed"
```

  (import `PaperAutomationActivationError` from `trader.automation.paper_activation` if the file does not already).

- [ ] **Step 7: Bootstrap script takes a real bundle** (`scripts/bootstrap_paper_automation.py`)

- Docstring: replace "one fixture PAPER_ELIGIBLE artifact" with "the verify key ring, for a bundle exported by `mmr research attest bundle`"; usage line `python3 scripts/bootstrap_paper_automation.py --bundle ~/.local/share/mmr/artifacts/sha256_<digest> --strategy-name <name>`.
- Import: drop `export_fixture_paper_eligible_bundle`; add `import json`.
- Parser: add `parser.add_argument("--bundle", type=Path, required=True, help="Bundle directory from `mmr research attest bundle`")`; change `--strategy-name` to `required=True` (no default).
- Body: after `ensure_signing_keypair(...)`, replace the fixture export and `bundle_path = artifacts_root / artifact_id` with:

```python
    manifest_path = args.bundle.expanduser() / "manifest.json"
    if not manifest_path.is_file():
        print(f"not a bundle (no manifest.json): {args.bundle}", file=sys.stderr)
        return 1
    artifact_id = json.loads(manifest_path.read_text(encoding="utf-8"))["artifact_id"]
    bundle_path = args.bundle.expanduser().resolve()
```

  and remove the now-unused `artifacts_root` variable.

- [ ] **Step 8: Run the tests**

Run: `$PY -m pytest tests/automation tests/test_command_stack.py tests/test_strategy_paper_arm.py -q -p no:cacheprovider --timeout=300`
Expected: all pass. `grep -rn "export_fixture_paper_eligible_bundle" trader scripts` prints nothing.

- [ ] **Step 9: Full suite, then commit**

```bash
git add trader/automation/bundle_finder.py trader/automation/paper_activation.py trader/automation/paper_materials.py scripts/bootstrap_paper_automation.py tests/automation/fixture_bundle.py tests/automation/test_bundle_finder.py tests/automation/test_paper_activation.py tests/automation/test_paper_hot_arm.py tests/automation/test_paper_materials.py tests/test_command_stack.py tests/automation/test_strategy_binding.py
git commit -m "feat(automation): activate only real eligible bundles bound to the strategy" -m "The fixture PAPER_ELIGIBLE bundle moves to tests. Activate now refuses with NO_ELIGIBLE_BUNDLE and names the latest evaluation." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: Operator docs

**Files:**
- Modify: `docs/OPERATIONAL_STATE.md`, `docs/PAPER_AUTOMATION_SETUP.md`, `CLAUDE.md`, `skills/mmr-skill/SKILL.md`
- Create: `research/example_spec.yaml` (documented example, not run by tests)

- [ ] **Step 1: `docs/OPERATIONAL_STATE.md`**

Under "## Armed paper automation (current)", add before the table:

```markdown
> **Since 2026-10 (real paper evidence, Phase A):** Activate only arms a bundle
> produced by `mmr research evaluate` → `research review submit` →
> `research attest bundle`, bound to the strategy's file, params, conids and
> bar size. The old fixture bundle no longer verifies, so the `momentum` arm
> stops after this ships. Phase A leaves the liquidity, benchmark and regime
> evidence missing, so no strategy can be eligible until Phase B. Run
> strategies with `auto_execute: propose` meanwhile.
```

- [ ] **Step 2: `docs/PAPER_AUTOMATION_SETUP.md`**

Replace every instruction that runs `scripts/bootstrap_paper_automation.py` to create a fixture bundle with this section (keep the rules R1–R5 and the release-gate sections):

```markdown
## Evidence before automation

1. Write a spec (see `research/example_spec.yaml`): strategy file, class, params,
   a neighbourhood, at least 8 XNYS conids, period, walk-forward settings,
   order notional and account equity.
2. `mmr research evaluate research/my_spec.yaml --dry-run` — validate, count jobs.
3. `mmr research evaluate research/my_spec.yaml` — runs walk-forward backtests at
   realistic costs (1x, 1.5x, 2x) under the live paper rules. The holdout is
   opened only if every other paper-v1 rule passes. Read the report it prints.
4. If the stage is `complete` and the state `PAPER_ELIGIBLE`:
   `mmr research review submit --artifact-id ... --decision-id ... --reviewer-kind human|llm ...`
   (an LLM may review paper bundles; live needs a human).
5. `mmr research attest bundle <artifact_id>` — signs and exports
   `~/.local/share/mmr/artifacts/sha256_<digest>/`.
6. Activate from `/cc`. It refuses (`NO_ELIGIBLE_BUNDLE`) unless that bundle is
   bound to the strategy's current YAML entry and file. Editing the strategy
   file, its params, conids or bar size needs a new evaluation.

Bundles expire after 90 days. `mmr research evaluations` lists past runs.
```

- [ ] **Step 3: `research/example_spec.yaml`**

```yaml
# Example spec for `mmr research evaluate`. Copy, edit, and keep it in git:
# the evaluation records the spec's strategy file by content hash.
name: orb_us_large
strategy: strategies/opening_range_breakout.py
class: OpeningRangeBreakout
params: {RANGE_MINUTES: 45, VOLUME_MULT: 1.3}
neighbourhood:            # adjacent values; neighbours change one key at a time
  RANGE_MINUTES: [30, 60]
  VOLUME_MULT: [1.2, 1.4]
conids: [265598, 272093, 4815747, 208813719, 76792991, 15124833, 4391, 13824]  # verify with `mmr resolve`
bar_size: 1 min
period: {start: 2024-01-02, end: 2026-09-30}
walk_forward: {folds: 6, embargo_sessions: 5, holdout_sessions: 90}
sizing: {order_notional: 1900, account_equity: 100000}
max_gross_allocation: 0.05
```

- [ ] **Step 4: `CLAUDE.md`**

Add a "**Research evaluation (paper evidence)**" paragraph to Key Patterns (after "Backtester execution costs"):

```markdown
**Research evaluation (paper evidence)**: `mmr research evaluate <spec.yaml>` (`trader/research/evaluation.py`) qualifies the data, creates a content-addressed experiment family (strategy file hash, uv.lock hash, dataset manifest, params, costs, walk-forward protocol), and runs walk-forward backtests through `evaluation_jobs` with `RealisticCosts`, a fixed order notional and `PaperAutomationRules` (live entry window, position/gross/count caps, daily-loss and drawdown halts, 15:45 ET flatten; constants imported from `session_risk`/`calendar_policy`). The main point runs at 1x/1.5x/2x costs, neighbours at 1x. Every paper-v1 rule except the holdout-stage ones (`holdout_drawdown_within_canary`, `deterministic_replay`, `holdout_opened_once`, `benchmark_relative_drawdown`) must pass before the artifact is sealed and the holdout opened (once). The selection count for the deflated Sharpe is per strategy file + class across all families, including imported legacy backtests. Each run writes a `research_evaluations` row, a report under `~/.local/share/mmr/reports/`, and a one-line summary JSON under `~/.local/share/mmr/artifacts/evaluations/` (Activate reads that file because trader_service must never open the research DB). `research attest bundle` signs and exports to `artifacts/sha256_<manifest digest>/`. Phase A leaves liquidity, benchmark and regime evidence missing, so nothing is eligible yet. Protective stops and the stop-distance trade-risk cap are not simulated (listed in every report).

**Bundle binding**: `trader/automation/strategy_binding.py::check_strategy_binding` compares a bundle's family/attestation with the strategy entry (file hash, class, upper-case params, conids, bar size). It runs at strategy load, at arm, and in Activate (`bundle_finder.find_eligible_bundle`); order dispatch rejects an intent whose `strategy_source_digest` differs from the attested one (`STRATEGY_SOURCE_MISMATCH`). The intent emitter sizes a BUY without a quantity from the attested order notional.
```

Add to the CLI Commands block:

```
research evaluate research/orb_us.yaml --dry-run   # validate spec + job count
research evaluate research/orb_us.yaml             # real walk-forward evidence (paper-v1)
research evaluations                               # past evaluations
research review submit ... --reviewer-kind llm     # paper: llm allowed; live: human
research attest bundle <artifact_id>               # sign + export the bundle
```

- [ ] **Step 5: `skills/mmr-skill/SKILL.md`**

Add a short section:

```markdown
## Reviewing a research evaluation (paper only)

When `mmr research evaluate` ends with stage `complete` and state `PAPER_ELIGIBLE`,
you may write the operator review for a **paper** bundle with
`--reviewer-kind llm`. Read the evaluation report first (path in the output). In
each field, say what the evidence shows, not what you hope: the economic reason
for the edge, why it survives the 2x cost stress, the regimes where it fails, the
data limits, the neighbourhood results, operational dependencies, capacity, and
whether one episode dominates. Never review a live bundle; live needs a human.
```

- [ ] **Step 6: Commit**

```bash
git add docs/OPERATIONAL_STATE.md docs/PAPER_AUTOMATION_SETUP.md CLAUDE.md skills/mmr-skill/SKILL.md research/example_spec.yaml
git commit -m "docs: document the real paper evidence flow" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: Final verification

**Files:** none new.

- [ ] **Step 1: Full suite**

Run: `$PY -m pytest tests/ --timeout=120 -q -p no:cacheprovider --ignore=tests/test_ibrx_async.py`
Expected: zero failures. Report the pass/skip counts.

- [ ] **Step 2: No fixture left in production code**

Run: `grep -rn "export_fixture_paper_eligible_bundle\|_evidence()" trader scripts`
Expected: no output.

- [ ] **Step 3: CLI smoke on synthetic data (scratchpad only, never `~/.config/mmr`)**

In a scratch directory, write a minimal `trader.yaml` (copy the `test_config_file` template from `tests/conftest.py`, set `duckdb_path`/`history_duckdb_path` to scratch files), build the synthetic universe and bars with the helpers in `tests/research/evaluation_fixtures.py` (call them from a small script with `sys.path` pointing at the worktree), put a copy of the fixture strategy under the worktree's `strategies/` **only in the scratch copy of the repo** — or set `MMR_STRATEGIES_EXTRA_ROOT` to the scratch dir — and run:

```bash
TRADER_CONFIG=<scratch>/trader.yaml MMR_RESEARCH_DUCKDB=<scratch>/research.duckdb \
  $PY -m trader.mmr_cli --json research evaluate <scratch>/time_of_day_us.yaml --workers 2
TRADER_CONFIG=<scratch>/trader.yaml MMR_RESEARCH_DUCKDB=<scratch>/research.duckdb \
  $PY -m trader.mmr_cli --json research evaluations
```

Expected: stage `pre_holdout`, missing exactly the four Phase B pre-holdout rules, a report path that exists. Note: `research evaluate` reads `execution_costs.yaml` from `ensure_config_dir()`; if the user's `~/.config/mmr/execution_costs.yaml` lacks `calendar`, the command must fail loudly naming the key — confirm that message, then point the smoke run at a scratch config by setting `HOME=<scratch>` for the command.

- [ ] **Step 4: Request the final review**

Use superpowers:requesting-code-review (or the native whole-branch review) on `b32c5b0..HEAD`.
