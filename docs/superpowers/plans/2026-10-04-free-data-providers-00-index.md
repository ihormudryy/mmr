# Free Data Providers — Plan Index

**Spec:** `docs/superpowers/specs/2026-10-04-free-data-providers-design.md`
**Branch:** `feat/free-data-providers` (from `feat/command-center-foundation`)

The spec has 11 phases. Most depend on the interface created in phase 1, so
each phase gets its own detailed plan, written when the previous phase has
landed (against real code, not guesses). This file is the map.

## Slicing rule

Each phase moves **one capability** behind the provider registry. The same
phase wraps that capability's existing TwelveData/Massive code as adapters
*and* adds the free provider. Nothing else moves. One concern per phase,
one local commit per task, app working after every task.

## Phases

| # | Plan file | Capability | Free provider | Status |
|---|---|---|---|---|
| 1 | `2026-10-04-free-data-providers-01-02-history.md` | registry foundation + history routing (TD, Massive behind registry; dead polygon code removed) | — | planned |
| 2 | same file | history | Alpaca | planned |
| 3a | `…-03a-quotes-movers-news.md` | quotes, movers (+filter), news | Alpaca | to write after 2 |
| 3b | `…-03b-scanner-merge.md` | one capability-based idea scanner | — | after 3a |
| 3c | `…-03c-remove-fallback.md` | remove silent Massive→TD fallback | — | after 3b |
| 4 | `…-04-fundamentals.md` | ratios, statements, 10-K sections | Finnhub, EDGAR | after 3c |
| 5 | `…-05-options.md` | options | Alpaca (indicative) | after 4 |
| 6 | `…-06-forex-computed-movers.md` | FX rates/convert/snapshot-all, FX movers, index ETF movers | Frankfurter, computed | after 5 |
| 7 | `…-07-streaming.md` | streaming | Alpaca WebSocket | after 6 |
| 8 | `…-08-sentiment.md` | news sentiment | Alpha Vantage | after 7 (needs key) |
| 9 | `…-09-dashboard-docs.md` | dashboard research page, move provider modules into `trader/data_providers/{massive,twelvedata}/`, config templates, Docker/scripts, CLAUDE.md, skills docs | — | last |

Moving the existing `trader/listeners/massive_*.py` / `twelvedata_*.py`
files into `trader/data_providers/` is deferred to phase 9 so earlier diffs
stay small (they are registered in place until then).

## Operator steps (user-run, not automated)

- After phase 2: edit the **live** `~/.config/mmr/trader.yaml` and
  `~/.config/mmr/data_refresh.yaml` (templates are copied only on first
  run) — set `default_data_source: alpaca`, add the Alpaca keys, change US
  jobs to `source: alpaca`. Optional: `./docker.sh -B before_alpaca`, then a
  one-time forced US refetch (spec §5 "Provenance").
- Before phase 8: create a free Alpha Vantage key.
- When IB Gateway is up: run the IB history-without-bundle check (spec §11).
