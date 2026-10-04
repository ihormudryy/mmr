# Free Data Providers — Design

Date: 2026-10-04
Branch: `feat/free-data-providers` (from `feat/command-center-foundation`)
Status: approved in chat, pending written-spec review

## 1. Goal

Make free data providers the default for every feature that today uses
TwelveData or Massive (Polygon). Keep TwelveData and Massive as opt-in
`--source` choices for people who have a paid key.

Success means:

- A fresh install with only free keys (Alpaca, Finnhub, Alpha Vantage) and
  no paid keys can run every CLI command, dashboard research view, cron job,
  data refresh and backtest that works today, with two named exceptions:
  - forex streaming (`stream --feed forex`) — no free stream; use IB
    `listen` or opt in to Massive;
  - seconds bars (`1 secs` … `30 secs`) — Alpaca has none; IB is the free
    path but is not yet verified without a paid data bundle.
- Where a free source is weaker than the paid one, the output says so
  (label), it never pretends.
- No new inline `if source == ...` branches. One provider interface.

Out of scope:

- Paying for anything. Near-free IB bundles are documented, not built.
- Normalising financial statements across providers (each provider keeps its
  own columns).
- Locally estimated sentiment (FinBERT etc.). The project rule "no fake or
  estimated sentiment" stands.
- Yahoo Finance (project rule).

## 2. What we replace (from code mapping)

There is no shared provider abstraction today. TwelveData and Massive meet
by duck typing, and dispatch is spread over ~12 `source ==` branches in
`trader/sdk.py`, plus `trader/mmr_cli.py`, `trader/data_service.py`
(one `_download_*_one` + `pull_*` RPC per provider) and
`trader/tools/massive_research.py` (dashboard).

| Capability | Today | Main code |
|---|---|---|
| History OHLCV | TwelveData (default), Massive, IB | `listeners/twelvedata_history.py`, `listeners/massive_history.py`, `data_service.py`, `mmr_cli.py:_handle_data_download/_handle_data_refresh` |
| Snapshot / batch quotes | IB (default), TwelveData | `sdk.py:snapshot/snapshot_batch` |
| Movers (stocks/crypto/indices) | Massive, TwelveData (Pro+) | `sdk.py:movers/movers_detail`, `massive_research.py:movers` |
| Idea scanner | Massive `IdeaScanner`, `TwelveDataIdeaScanner` (~950 lines, duplicated) | `tools/idea_scanner.py` |
| Indicators | Massive server-side, local `compute_rsi/ema/sma` | `tools/idea_scanner.py` |
| News + sentiment | Massive (`list_ticker_news` insights, Benzinga) | `sdk.py:news/news_detail`, scanner, dashboard |
| Ratios | Massive, TwelveData | `sdk.py:ratios`, scanner |
| Statements | Massive, TwelveData | `sdk.py:balance_sheet/income_statement/cash_flow` |
| 10-K sections | Massive (private `_get`) | `sdk.py:filing_sections` |
| Options | Massive | `tools/chain.py`, `tools/options_data.py`, `sdk.py:options_*` |
| Forex snapshot/quote | IB (default), Massive, TwelveData | `sdk.py:forex_snapshot/forex_quote` |
| Forex snapshot-all / movers / convert | Massive (convert also TwelveData) | `sdk.py:forex_*` |
| Streaming | Massive (`MassiveReactive`), TwelveData (`TwelveDataReactive`, Pro+) | `listeners/*_reactive.py`, `mmr_cli.py:_handle_stream/_handle_watch` |
| Dashboard research | `MassiveResearch` (hosts TD fallbacks) | `web/command_center/research.py`, `routes_research.py` |

Dead code to delete: `trader/listeners/polygon_listener.py`,
`trader/listeners/polygon_reactive.py`, `trader/batch/polygon_batch.py`,
`trader/batch/polygon_queuer.py` (they import `polygon`, `arctic`,
`ib_insync` — none are dependencies; nothing imports them).

## 3. Verified facts about free providers

Checked on 2026-10-04 with real free keys (read-only calls). This section
is the ground truth the design rests on.

**Alpaca (Basic plan, paper account)** — `https://data.alpaca.markets`

- SIP (full-market) 1-min bars back to 2016-01-04: HTTP 200. Includes pre
  and post market. 10,000 bars per page with `next_page_token`.
- SIP daily bars from 2016 with `adjustment=` param: HTTP 200.
- SIP data newer than ~15 minutes: HTTP 403
  `"subscription does not permit querying recent SIP data"`.
- `timeframe=1Sec`: HTTP 400 `"invalid timeframe"`. No seconds bars.
- `/v2/stocks/snapshots` and `/quotes/latest` with `feed=iex`: 200.
  With `feed=sip`: 403 (same recent-data rule).
- `/v1beta1/screener/stocks/movers`, `/most-actives`,
  `/screener/crypto/movers`: 200. Raw stock movers contain warrants and
  sub-penny names (e.g. `HPAIW` +221%).
- `/v1beta1/news`: 200, Benzinga headlines, no sentiment field.
- `/v1beta1/options/snapshots/{underlying}?feed=indicative`: 200. Greeks and
  IV present on 24 of 50 contracts sampled (liquid ones). `feed=opra`: 403
  `"OPRA agreement is not signed"`.
- Crypto latest bars: 200.
- `/v1beta1/forex/latest/rates`: 403 `"insufficient grants"`. No forex.
- Documented limits: 200 REST calls/min, WebSocket 30 equity symbols (IEX).

**Finnhub (free)** — `https://finnhub.io/api/v1`

- 200: `/quote`, `/stock/metric?metric=all`, `/company-news`,
  `/stock/financials-reported`.
- 403: `/stock/candle`, `/news-sentiment`, `/forex/rates`.
- Free plan ~60 calls/min (third-party source; not verified).

**SEC EDGAR** — 200 on `data.sec.gov/api/xbrl/companyfacts/...` and
`/submissions/...`. Requires a descriptive `User-Agent`. Max 10 req/s.

**Frankfurter** — 200 on `api.frankfurter.dev/v1/latest`. ECB daily rates,
no key.

**Alpha Vantage** — not yet probed (needs a key). Docs: 25 calls/day free,
`NEWS_SENTIMENT` is on the free tier.

**IB** — not yet probed (gateway down). Open question: does
`reqHistoricalData` for US stocks work without a paid data bundle? This only
affects seconds bars.

## 4. Architecture

New package `trader/data_providers/`:

```
trader/data_providers/
├── __init__.py
├── capabilities.py      # Protocols + shared result shapes
├── errors.py            # ProviderNotConfigured, CapabilityNotSupported,
│                        # ProviderEntitlementError, ProviderRateLimited
├── registry.py          # ProviderRegistry: (capability, source) -> provider
├── rate_limit.py        # per-provider limiter + 429 retry helper
├── symbols.py           # per-provider symbol mapping (BRK.B vs "BRK B")
├── alpaca/              # history, quotes, movers, news, options, stream, assets
├── finnhub.py           # ratios, quote, company news
├── edgar.py             # statements, 10-K sections
├── frankfurter.py       # FX rates + convert
├── alphavantage.py      # news sentiment
├── computed_movers.py   # FX movers (pairs), index movers (ETF proxy)
├── massive/             # existing Massive code behind the interfaces
└── twelvedata/          # existing TwelveData code behind the interfaces
```

### 4.1 Capabilities

Each capability is a `typing.Protocol`. A provider implements only the ones
it has. Names below are the contract; argument lists follow today's SDK
methods so callers change as little as possible.

| Protocol | Method(s) | Returns |
|---|---|---|
| `HistoryProvider` | `get_history(ticker, bar_size, start_date, end_date, timezone='US/Eastern')` | DataFrame, **same schema as today**: tz-aware index `date`; columns `open, high, low, close, volume, average, bar_count, bar_size, what_to_show` |
| `QuoteProvider` | `quotes(symbols)` | `list[Quote]` (symbol, time, last, bid, ask, sizes, open, high, low, close, volume, previous_close, change, change_pct, feed) |
| `MoversProvider` | `movers(market, direction, top)` | DataFrame: `ticker, name, close, volume, change, change_pct, provider, note` sorted by `change_pct` |
| `NewsProvider` | `news(ticker, limit)` | `list[NewsItem]` (id, time, headline, summary, url, source, tickers) |
| `SentimentProvider` | `sentiment(ticker, limit)` | `list[NewsItem]` with `sentiment` + `sentiment_score` filled |
| `RatiosProvider` | `ratios(symbol)` | one-row DataFrame with the shared ratio columns the scanner already uses (from `_TD_FUNDAMENTAL_FIELDS`) plus provider extras |
| `StatementsProvider` | `statement(symbol, kind, limit, timeframe)` | DataFrame, provider-native columns (`kind` ∈ balance, income, cashflow) |
| `FilingsProvider` | `ten_k_sections(symbol, sections, limit)` | list of `{filed, period, section, text}` |
| `OptionsProvider` | `expirations(underlying)`, `chain(underlying, expiration, type, strike_min, strike_max)`, `contract(option_ticker)` | records with `feed` (`indicative` / `opra`) |
| `ForexProvider` | `rate(base, quote)`, `rates(base, symbols)`, `convert(base, quote, amount)` | dicts with `as_of` and `source` |
| `StreamProvider` | `agg_subject`, `trade_subject`, `quote_subject`, `start(symbols, data_type)`, `stop()` | same Rx surface as `MassiveReactive` today (emits `ib_async.Ticker`) |

### 4.2 Registry

```python
registry = ProviderRegistry.from_config(config)
provider = registry.get(Capability.HISTORY, source=None)  # None -> default
```

- A new provider is built per `get()`; adapters may keep their own HTTP session.
- `source=None` means the per-capability default (table 4.3), overridable in
  config `data_providers:`.
- Unknown source, or a source without that capability, raises
  `CapabilityNotSupported` listing the providers that do support it.
- Missing key raises `ProviderNotConfigured` naming the env var.
- The registry is the only place that knows provider names. `sdk.py`,
  `mmr_cli.py`, `data_service.py` and the dashboard call it instead of
  branching.

### 4.3 Defaults

| Capability | Free default | Opt-in sources | Notes |
|---|---|---|---|
| History (US) | `alpaca` | `massive`, `twelvedata`, `ib` | International: `ib` (existing auto-detect by exchange) |
| History, seconds bars | `ib` | `massive` | Alpaca has none; IB not yet verified |
| Snapshot / quotes | `ib` (as today) | `alpaca` (IEX), `finnhub`, `twelvedata` | |
| Movers stocks, crypto | `alpaca` | `massive`, `twelvedata` | filtered (4.4) |
| Movers indices | `etf_proxy` | `massive` | labelled "ETF proxy" |
| Movers forex | `computed_fx` | `massive` | from Frankfurter daily rates; IB when up |
| News | `alpaca` | `massive` (`polygon`, `benzinga`), `finnhub` | |
| Sentiment | `alphavantage` | `massive` | single ticker only (`news --detail`) |
| Ratios | `finnhub` | `massive`, `twelvedata` | |
| Statements | `edgar` | `massive`, `twelvedata` | |
| 10-K sections | `edgar` | `massive` | |
| Options | `alpaca` (indicative) | `massive` | |
| FX snapshot / quote | `ib` (as today) | `frankfurter` (daily), `massive`, `twelvedata` | |
| FX convert | `frankfurter` | `massive`, `twelvedata` | |
| FX snapshot-all | `frankfurter` | `massive` | daily rates for all pairs vs base |
| Streaming stocks / crypto | `alpaca` (IEX, ≤30 symbols) | `massive`, `twelvedata` | |
| Streaming forex | — (use IB `listen`) | `massive` | no free stream |

`default_data_source` changes from `twelvedata` to `alpaca`. `ideas` and
`movers` follow the registry defaults (no more hard-coded `massive`).

### 4.4 One idea scanner

`IdeaScanner` (Massive) and `TwelveDataIdeaScanner` merge into one
`IdeaScanner` built from capabilities:

- discovery: `MoversProvider` (movers presets) or `QuoteProvider` over
  explicit tickers / universe
- indicators: daily bars from `HistoryProvider` (60 days), then the existing
  local `compute_rsi/ema/sma`. No server-side indicators.
- enrichment: `RatiosProvider` (`--fundamentals`), `NewsProvider`
  (`--news`), names from the Alpaca asset list
- scoring, filters, presets, output: unchanged module-level functions

`IBIdeaScanner` stays as is (international path).

### 4.5 Callers after the change

- `sdk.py`: each research method asks the registry and maps the shared
  result to today's return type. The `_massive_client` /
  `_twelvedata_client` properties move into the adapters.
- `data_service.py`: one `pull_history(source, ...)` path through
  `HistoryProvider`. `pull_massive` / `pull_twelvedata` RPCs stay as thin
  aliases so existing clients and the skill helpers keep working.
- `mmr_cli.py`: `--source` choices come from the registry
  (`registry.sources_for(capability)`).
- Dashboard: `MassiveResearch` becomes `ResearchProvider` on top of the
  registry. `ResearchResult.provider` reports the real provider used.
  `MASSIVE_NOT_CONFIGURED` becomes `PROVIDER_NOT_CONFIGURED` naming the
  missing key.

## 5. Data correctness

- **Only completed sessions.** `TickData.missing()`
  (`trader/data/data_access.py:629`) treats a day as present as soon as it
  has any valid bar. A partial day written mid-session would therefore never
  be backfilled. (This gap exists today with TwelveData/Massive too; Alpaca's
  15-minute SIP block would make it worse.) Rule: the Alpaca history provider
  returns bars only for **completed sessions**. A session counts as completed
  once `now >= 20:16 ET` on that session date (post-market ends 20:00 ET,
  plus the 15-minute SIP block). The provider cuts `end_date` back to the end
  of the last completed session, for intraday and daily bars alike, and logs
  the cut. The nightly cron (20:30 ET) is therefore unaffected. The gap is
  never filled from IEX — IEX volume is a few percent of the market, and
  mixed feeds would corrupt stored bars.
- **Adjustment.** Request `adjustment=split`. Phase 2 must first verify what
  the stored data actually is: the TwelveData worker passes no `adjust`
  argument (provider default), and Massive `list_aggs` uses its default
  `adjusted=true`. If either turns out to be dividend-adjusted too, stop and
  decide before switching. Then a test compares Alpaca vs stored bars on one
  symbol that had a split (e.g. NVDA 2024-06-10) and checks there is no jump.
- **Provenance.** `tick_data` has no provider column. After the switch,
  stored US history would be TwelveData/Massive rows up to some date and
  Alpaca rows after it, with small consolidation differences at the seam.
  Decision: recommend a one-time full refetch of the US refresh jobs from
  Alpaca (`mmr data refresh --all` with `force: true` on the US jobs), after
  a DuckDB backup (`./docker.sh -B before_alpaca`). This is a documented,
  user-run step in phase 2 — not automatic. Until it runs, the splice is
  accepted and noted in `docs/OPERATIONAL_STATE.md`.
- **Timestamps.** Alpaca daily bars arrive at 04:00/05:00 UTC (midnight
  ET). They are normalised to the index today's workers produce. A shared
  contract test asserts identical index type, tz and columns across all
  history providers.
- **Symbols.** `symbols.py` maps class shares per provider (`BRK.B` for
  Alpaca/Massive, `BRK B` for IB). An unmapped or unknown symbol is an
  error — no fuzzy fallback.
- **Movers filter.** Drop warrants, rights and units (from the Alpaca asset
  list, cached daily) and anything below `min_price` (default $1, CLI
  `--min-price`). Applies to all movers sources.
- **Labels.** Every result carries `provider`. ETF-proxy index movers,
  indicative option quotes, IEX snapshots and daily (not live) FX rates are
  labelled in CLI tables, JSON and the dashboard.

## 6. Errors

- Fail loudly. Errors name the provider, the capability and what to do
  (which env var to set, which source supports the feature).
- **Removed:** the silent Massive → TwelveData entitlement fallback in
  `sdk.py:scan_ideas`, `massive_research.py` (ideas, movers, snapshot). A
  failing provider raises; the user picks another `--source`.
  `entitlement_fallback_notice` and `LIQUID_US_FALLBACK_TICKERS` go away
  with it.
- Provider errors map to the four error types in `errors.py`. The dashboard
  maps `ProviderRateLimited` to its existing 429 `RESEARCH_RATE_LIMITED` and
  `ProviderNotConfigured` to 503.

## 7. Rate limits

- `rate_limit.py`: one in-process limiter per provider, shared across
  threads. Alpaca 200/min, Finnhub 60/min, Frankfurter polite 5/s. EDGAR
  uses `edgartools`' own limiter (10/s) if that library is adopted; we do
  not layer a second one on top.
- Alpha Vantage has **no** local daily counter: every `mmr` command is a
  new process, so an in-memory 25/day counter would reset each time. The
  provider's own "quota used" response (below) is the only enforcement.
- HTTP 429 → retry with exponential backoff (simple loop, honours
  `Retry-After`), at most 3 tries, then `ProviderRateLimited`.
- Alpha Vantage returns HTTP 200 with a `"Note"`/`"Information"` body when
  the daily quota is used up. That maps to `ProviderRateLimited`, never to
  empty data.

## 8. Config

`config_defaults/trader.yaml`, `trader/config.py` (dataclasses + env
fallback) and `trader/container.py` (flat keys), wired like
`massive_api_key` today:

```yaml
alpaca_api_key_id: ''        # env ALPACA_API_KEY_ID
alpaca_api_secret_key: ''    # env ALPACA_API_SECRET_KEY
finnhub_api_key: ''          # env FINNHUB_API_KEY
alphavantage_api_key: ''     # env ALPHAVANTAGE_API_KEY
sec_edgar_user_agent: ''     # env SEC_EDGAR_USER_AGENT; "name email"; EDGAR refuses if empty
default_data_source: alpaca  # was twelvedata
data_providers: {}           # optional per-capability override, e.g. {history: alpaca, ratios: finnhub}
```

- `config_defaults/data_refresh.yaml`: jobs move from `source: twelvedata`
  to `source: alpaca`. Auto-detect: US exchanges → `alpaca`, else `ib`.
- `docker-compose.yml`, `docker.sh`, `start_mmr.sh`: pass through and show
  status for the new keys; setup wizard asks for them.
- New dependencies: `alpaca-py` (streaming only, phase 7; REST uses `requests`), `finnhub-python`, `edgartools` (only if the
  phase-4 test passes; otherwise plain HTTP). With `edgartools`,
  `sec_edgar_user_agent` is passed to its `set_identity()`. Alpha Vantage and Frankfurter
  use plain `requests` (no SDK needed). `massive` and `twelvedata` stay.

## 9. Testing

- **Contract tests per capability** in `tests/data_providers/`: every
  `HistoryProvider` returns the same index/columns/tz; every
  `MoversProvider` the shared columns, sorted and filtered; and so on. Each
  provider, including the TwelveData and Massive adapters, runs the same
  suite.
- **Recorded responses**: adapter tests use JSON captured from real calls
  (keys stripped), stored under `tests/data_providers/fixtures/`. No
  network in the normal suite.
- **Live smoke tests**: `@pytest.mark.live`, skipped unless `-m live` and
  keys are present.
- **Existing tests** (~330 touching TwelveData/Massive) keep passing through
  the adapters. Tests for the two old scanners move to the merged scanner.
  Tests for the removed fallback are rewritten to assert the loud error.
- **Done means**: full suite green
  (`pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py`) plus
  one real CLI run against the free providers for the phase's commands.

## 10. Delivery

Slicing rule (from the plan index): each phase moves one capability behind the
registry, wraps that capability's existing TwelveData/Massive code as adapters,
and adds the free provider. Nothing else moves.

One local commit per phase on `feat/free-data-providers`. No push and no PR
unless asked; squash before any PR. Each phase leaves the app working.

1. Interfaces, registry, errors, rate limiter. TwelveData and Massive
   behind adapters. Delete dead `polygon_*` modules. No behaviour change.
2. Alpaca history (+ completed-session rule, adjustment check). Switch
   history defaults and `data_refresh.yaml`. Document the optional one-time
   US refetch.
3a. Alpaca quotes, movers (+ filter), news adapters; SDK/CLI wired through
    the registry. Verify on a weekday that Basic-plan movers are intraday,
    not end-of-day only.
3b. Merge the two idea scanners into one capability-based scanner.
3c. Remove the silent Massive → TwelveData fallback (SDK and dashboard).
4. Finnhub ratios. EDGAR statements and 10-K sections (test `edgartools`
   first; if sections are unreliable, stop and ask).
5. Alpaca options (indicative).
6. Frankfurter FX rates/convert/snapshot-all, computed FX movers,
   ETF-proxy index movers.
7. Alpaca streaming for `stream` / `watch`.
8. Alpha Vantage sentiment (needs a third free key).
9. Dashboard research page, config templates, Docker/scripts, CLAUDE.md,
   `docs/`, skills references.

## 11. Open questions

- IB `reqHistoricalData` without a paid bundle (affects seconds bars only).
  Check when the gateway is up.
- Alpaca WebSocket concurrent-connection limit on Basic (docs unclear).
- Finnhub free per-minute limit (60/min is third-party; limiter is
  configurable).
- Finnhub `/company-news` returns articles syndicated from Yahoo
  (`source: "Yahoo"`). This is Finnhub's API, not Yahoo Finance as a data
  source, and Finnhub news is opt-in only (default is Alpaca), so it is
  treated as allowed. Revisit if the "no Yahoo" rule is meant to cover
  syndicated headlines.
- Licences: Alpaca, Finnhub and Alpha Vantage free tiers are personal /
  non-redistribution. Fine for a personal system; a shared dashboard would
  need review.
