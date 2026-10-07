# Ideas scanner

One `IdeaScanner(source)` pipeline (discover → filter → indicators → score → rank) with three US scan sources, plus `IBIdeaScanner` for `--location`:

```
ideas momentum                                  → IdeaScanner(AlpacaScanSource)     default, free
ideas --source massive                          → IdeaScanner(MassiveScanSource)    paid plan
ideas --source twelvedata --tickers …           → IdeaScanner(TwelveDataScanSource)
ideas momentum --location STK.AU.ASX --tickers  → IBIdeaScanner (IB, international, ~30-90s)
```

Sources live in `trader/data_providers/<provider>/scan.py` and are picked through the provider registry (`Capability.IDEAS`). Bare `ideas` uses Alpaca. Change it with `data_providers.ideas` or `--source`. It never inherits `default_data_source`. The Massive → TwelveData fallback on `NOT_AUTHORIZED` (Stocks Basic) runs only when the resolved source is Massive; the SDK then scans a small liquid US set (or your `--tickers`/`--universe`) and prints a yellow notice.

Every result carries `df.attrs['ideas_provider']`; Alpaca (and fallback) results also carry `df.attrs['ideas_notice']`. `mmr --json ideas` adds both as `provider` and `notice` next to `data` and `title`.

## Alpaca source (default)

```
movers + most-actives (top 50 each)  →  delayed SIP snapshots  →  filter  →  daily bars + local RSI/EMA/SMA  →  score  →  rank
```

- Free. Discovery is the union of movers and most-actives, **not the full market**. The notice states how many symbols came back and names symbols with no snapshot, no previous close (dropped) or an invalid format.
- Prices and volume are consolidated SIP snapshots, 15 minutes delayed.
- `--tickers` / `--universe` scan exactly those symbols (de-duplicated). Preset filters still apply to them; relax with `--min-change` etc.
- Indicators (RSI, EMA 9, SMA 20/50) are computed locally from Alpaca daily bars (120 calendar days, completed sessions only). Company names come from the Alpaca asset list.
- Warrants, rights and units are dropped from discovery by the asset list. If the asset list is unavailable, the warrant check falls back to the ticker-suffix rule and the notice says so.
- News is a headline only, no sentiment.
- No fundamentals until phase 4: `--fundamentals` raises an error that names `--source massive|twelvedata`. `--detail` shows names, news and indicators, with a notice that ratios are not available.
- Auth, entitlement and rate-limit errors stop the scan. If every ticker's indicators fail, the scan raises.

## Massive source (`--source massive`)

```
Massive movers / snapshot_all  →  filter  →  Massive indicator API (parallel)  →  score  →  rank
```

- Fast (~4s) when entitled: full-market snapshots, server-side indicators
- US only; fundamentals + news with sentiment when plan allows

## TwelveData source (`--source twelvedata`)

```
movers (Pro+) or batch /quote  →  filter  →  time_series + local RSI/EMA/SMA  →  score  →  rank
```

- Quotes work on Basic/Starter; `/market_movers` needs Pro+
- No news endpoint — `--news` is a no-op on this path

## IBIdeaScanner (IB path — `--location`)

```
IB scanner / resolve_contract  →  get_snapshot (sequential)  →  reqHistoricalData  →  local RSI/EMA/SMA  →  score  →  rank
```

- Slower (~30-90s) — sequential IB snapshot requests, IB pacing limits
- Works for any IB-supported market: ASX, TSE, SEHK, EU exchanges, etc.
- Discovery: IB scanner API (when available) or explicit `--tickers`/`--universe`
- The IB scanner API may not support all location codes (error 162). When this happens, use `--tickers` to specify symbols explicitly
- Indicators computed locally from history bars (pure pandas) — `compute_rsi()`, `compute_ema()`, `compute_sma()`
- Fundamentals from `reqFundamentalData` (ReportSnapshot XML), news from `reqHistoricalNews` (no sentiment)
- IB news headlines include metadata prefixes like `{A:800015:L:en}...` which are stripped before display

## Shared module-level functions (used by all scanners)

- `PRESETS` dict, `ScanFilter`/`ScanPreset` dataclasses
- Scoring functions: `_score_momentum`, `_score_gap_up`, `_score_gap_down`, `_score_mean_reversion`, `_score_breakout`, `_score_volatile`
- `apply_filters()`, `to_dataframe()`, `merge_filters()`
- `PRESET_SCAN_CODES` maps preset names to IB scan codes

## IB Scanner Location Codes

Common codes: `STK.US.MAJOR` (US), `STK.AU.ASX` (Australia), `STK.CA` (Canada), `STK.HK.SEHK` (Hong Kong), `STK.JP.TSE` (Japan), `STK.EU` (Europe).

The scanner API requires market data subscriptions for the target exchange. If the scanner returns error 162 ("Market Scanner is not configured for one of the chosen locations"), use `--tickers` to provide symbols explicitly — they'll be resolved via `resolve_contract` with the exchange extracted from the location code.

When explicit tickers are provided with `--location`, the `min_change_pct`/`max_change_pct` preset defaults are relaxed (the user chose these symbols specifically and shouldn't have them filtered out).

## Errors and entitlements

Raises `IdeaScannerError` (not an empty DataFrame) on IB discovery failure, when every ticker fails to resolve, or when every Alpaca indicator fetch fails. Massive movers/snapshots require Stocks Starter+; TwelveData `/market_movers` requires Pro+. When the source is Massive, entitlement errors fall back to TwelveData quote scans (liquid US set or `--tickers`/`--universe`) with a yellow CLI notice. Alpaca auth, entitlement and rate-limit errors stop the scan. Batch IB ops use `asyncio.gather` / `ThreadPoolExecutor` — keep DuckDB warm via `data refresh` so IB historical isn't on the hot path.
