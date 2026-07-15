# Dashboard: strategy controls, human names, param editing, tooltips

**Date:** 2026-07-15
**Status:** approved (user-directed; scope grew over three messages)

## Requirements (user's words, consolidated)

1. Enable/disable strategies from the dashboard UI.
2. Human-readable strategy names; strategy description on hover.
3. Unfold a strategy row to change its parameters.
4. Info tooltips (ⓘ icon, hover) on all dashboard elements.
5. Expose ALL implemented strategies (everything in `strategies/`), not just deployed ones.

## Components

### A. Reusable strategy scanner — `trader/strategy/inspect.py` (extraction)

The AST scan inside `mmr_cli._handle_strategies_inspect` (class, dispatch mode,
upper-case tunables + `self.params.get` knobs, docstring) moves to
`scan_strategies(directory) -> list[dict]`, adding the full docstring alongside
the first line. The CLI handler delegates to it; the web app imports it directly
(same container, local `strategies/` dir — no RPC needed for static file facts).

### B. `update_strategy_params` RPC (strategy_runtime)

New method on StrategyRuntime, exposed via `strategy_service_api` and proxied
through `trading_runtime` + `trader_service_api` (same chain as
enable/disable), surfaced as `sdk.update_strategy_params(name, params)`.

Behavior:
1. Coerce incoming values (form strings): int → float → bool (`true`/`false`) → str.
2. Rewrite the strategy's `params:` block in `strategy_runtime.yaml`
   (safe_load → mutate → atomic write via tmp+rename). Empty-string value
   deletes the key. Persistence survives restarts.
3. Hot-swap the live strategy: remove the instance from
   `strategy_implementations` / per-conid `strategies` lists /
   `_last_dispatched_bar`, evict its `sys.modules` entry, then `load_strategy`
   from the updated config and re-subscribe its conids. Enabled state is
   restored by the existing persisted-state mechanism (D2). Params therefore
   take effect immediately — no container restart.
4. Unknown strategy / YAML entry missing → `SuccessFail.fail` (fail loudly).

Caveat (documented in tooltip): a strategy sees new params live only where it
reads `self.params` (all currently-deployed strategies do, per the July fix);
class-attr-only strategies get them on the next backtest/deploy.

### C. SDK strategies listing enrichment

`sdk.strategies()` rows gain `class_name` and `description` (already on
StrategyConfig). No other shape changes.

### D. Web dashboard (`web/app.py` + `dashboard.html`)

Routes (all following the existing approve/reject pattern — access check,
CSRF form token, flash + 303 redirect, `retry=False`):
- `POST /strategies/{name}/enable`
- `POST /strategies/{name}/disable`
- `POST /strategies/{name}/params` — form fields become the params dict
  (`param_<KEY>` inputs + optional `new_key`/`new_value` pair).

Fetchers:
- `fetch_strategies` additionally derives `display_name` (CamelCase class name
  → spaced words) and passes `description`, `params`, `auto_execute` through.
- `fetch_available_strategies` calls `scan_strategies()` and marks which
  classes are already deployed (matched by module file + class name).

Template — Strategies section:
- Deployed table: Name (display name, config name dim beneath; hover shows the
  YAML description via CSS tooltip), State chip, Auto chip (`propose`),
  Bar, ConIds, Actions (Enable/Disable button + ▸ unfold toggle).
- Unfold row (hidden `<tr>`, small JS toggle like the reasoning modal): the
  strategy's current params as labelled inputs, one add-new-param row, Save
  button. Undeclared tunables for its class (from the scanner) are shown as
  placeholders so the user knows what knobs exist.
- "Available (not deployed)" sub-table: display name, class, file, dispatch
  mode, tunables-with-defaults, first docstring line (full docstring on
  hover). Read-only — deploying from the UI is out of scope (needs
  conids/bar-size/universe wizard; CLI `strategies deploy` covers it).

Tooltips: `info(text)` Jinja macro rendering `<span class="info">ⓘ<span
class="tip">…</span></span>`, pure CSS hover/focus, dark-themed. Applied to:
header tiles (Net Liq, Daily P&L, Positions), every section heading, all risk
metrics and risk-gate limits, strategy State/Auto/Actions columns, proposal
Source/Size/Conf/Status columns.

## Error handling

Route failures flash the error (service down ≠ blank page, matching existing
sections). `update_strategy_params` never leaves the YAML half-written (atomic
rename); hot-swap failure after YAML write logs + returns fail with the
instruction to restart (config is already persisted, so restart converges).

## Not in scope

Deploying new strategies from the UI; editing conids/bar_size/universe from
the UI; auth beyond the existing CSRF + optional MMR_WEB_TOKEN; live param
push without re-install.

## Tests

- `tests/test_strategy_inspect.py` — scanner: tunables (Assign + AnnAssign +
  params.get), mode detection, docstring, parse-error row.
- `tests/test_update_strategy_params.py` — coercion; YAML rewrite + atomicity;
  hot-swap replaces instance with new params and preserves enabled state;
  unknown name fails; empty value deletes key.
- `tests/test_web_dashboard.py` — TestClient + stub SDK: enable/disable/params
  routes call the right SDK method and redirect; bad CSRF → 403; page renders
  display names, buttons, unfold form, available strategies, tooltip markup.
