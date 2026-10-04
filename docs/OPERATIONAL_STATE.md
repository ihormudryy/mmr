# MMR Operational State

Living snapshot of the **deployed/running** state (config, strategies, data, infra)
and the reasoning behind it. Distinct from `AUDIT_ROADMAP.md` (code backlog).
Update the date + relevant sections when the running config changes.

How to arm unattended paper automation from scratch:
[`PAPER_AUTOMATION_SETUP.md`](PAPER_AUTOMATION_SETUP.md).

**Last updated: 2026-10-04 (Sun) — paper trading, command-center stack.**

---

## How this stack actually trades (2026)

The July 5 notes below described five strategies with `auto_execute=True`.
That path is **obsolete**: `auto_execute: true` is refused at load.

Current model:

| Mode | What happens |
|------|----------------|
| **Paper automation** | Exactly **one** strategy (`automation.strategy_name`) may place protected orders without human approve (`execute_automated_intent`). |
| **Propose** | Other strategies may set `auto_execute: propose` → PENDING cards on `/cc` for approve/reject. |
| **Live** | `automation.live_enabled` stays `false`. Human approve only. |

Config (host, not git): `~/.config/mmr/trader.yaml` + `strategy_runtime.yaml`.

- `command_authority.enabled: true`, `live_enabled: false`
- `automation.enabled: true`, `strategy_name: momentum`
- `momentum` must **not** also have `auto_execute: propose` (rule R1)

Dashboard: Scaling tab → `lifecycle=armed` for momentum. Trading tab Strategies
list comes from journaled `strategy.updated` (not from host `mmr strategies`).

**Host CLI caveat:** `mmr strategies` from the Mac cannot reach strategy typed
ports 42104/42105 (private Compose network). Use:

```bash
docker compose exec trader python -m trader.mmr_cli strategies
```

or the `/cc` Strategies panel. Trader ports 42101/42102 **are** published, so
`mmr status` / `portfolio-snapshot` / `propose` / `approve` work from the host.

---

## Armed paper automation (current)

| Strategy | Role | Notes |
|---|---|---|
| **momentum** | Single auto slot | Artifact bundle on disk; `auto_execute` off. Enable it before a soak (`INSTALLED` ≠ dispatchable). |
| orb_* / ensemble | Optional propose | Human review on `/cc` if `auto_execute: propose`. |
| global | Always present | Enable/Disable/Undeploy hidden by design. |

Kill switches: Scaling **Deactivate**; `pause_trading`; `automation.enabled: false` + restart strategy.

---

## Historical note — 2026-07-05 paper book (superseded)

The following was the **old** auto-execute book on account `DUM422056`. Kept as
backtest evidence only — do **not** re-arm with `auto_execute=True`.

| Strategy | Sym | conId | Class | 1yr return | PF | Notes |
|---|---|---|---|---|---|---|
| orb_googl | GOOGL | 208813719 | OpeningRangeBreakout | +13.9% | 2.45 | strongest |
| orb_pltr | PLTR | 444857009 | OpeningRangeBreakout | +7.5% | 1.44 | |
| orb_wds | WDS | 564155292 | OpeningRangeBreakout | +7.1% | 1.68 | ASX |
| vwap_reclaim_cat | CAT | 5437 | VwapReclaim | +4.7% | 1.44 | |
| orb_bhp | BHP | 4036812 | OpeningRangeBreakout | +4.7% | 1.26 | ASX |

Disabled after that session (do not re-arm without new evidence): orb_cba,
orb_rio, orb_fmg, orb_csl, orb_gld (losing), orb_xlk (too much drawdown).

---

## Backtest findings (2026-07-04/05, 1yr 1-min)

- **ORB on ASX is marginal universe-wide.** Top-20 leaderboard: 11/20 positive,
  median PF 0.89, negative median per-trade expectancy. Only BHP (PF 1.26) and
  WDS (PF 1.68) have a genuine per-name edge. **Param sweep** (RANGE_MINUTES ×
  VOLUME_MULT) confirmed the DEFAULTS (30 / 1.5) are already the best combo and
  still only marginal (medPF 1.075, mean return −0.58%) — **tuning does not
  rescue ORB on ASX**. Treat ORB-ASX as a per-name edge (BHP, WDS), not a
  universe strategy.
- **US ORB:** GOOGL strong (PF 2.45), PLTR/XLK positive, GLD losing.
- **VWAP:** works on CAT (PF 1.44, +18.9bps); loses on every other US name tried
  → correctly deployed only on CAT.
- **Metric interpretation:** `expectancy_bps` equally weights each SELL's net
  P&L / closed entry notional; PF uses cash P&L sums. Both include allocated
  entry and exit commissions. Unequal notionals (even with fixed share counts)
  or partial exits can legitimately produce PF > 1 with negative expectancy;
  total return also includes unrealized P&L. No calculation defect was
  reproduced in deterministic regression tests; the historical runs above were
  not rerun. See [metric semantics and examples](BACKTEST_METRICS.md).
- **Not done:** statistical-confidence tests (PSR/t-test/bootstrap) — the script
  hung on MC/bootstrap over large trade sets after ~3 of 6 survivors. Rerun with
  iteration caps + per-strategy timeouts if wanted. No results saved; nothing
  relies on partial numbers.

---

## Data coverage

- **ASX 1-min:** 53 universe symbols, ~1 year as of 2026-07-02.
- **US 1-min:** deep history for the historically deployed names.
- DuckDB lives in named volume `mmr_db_data`. Backup: `./docker.sh -B` /
  `mmr data backup`. Nightly pycron `db_backup`.

---

## History data source (Alpaca default)

Code default for `data download` and US `data refresh` jobs is now **Alpaca**
(free Basic plan, SIP, split-adjusted). Config templates are copied only on
first run, so the live host config does **not** change by itself. Operator steps:

1. Backup first: `./docker.sh -B before_alpaca`.
2. Check the adjustment basis on the stored data. Inside the trader container
   (`./docker.sh -e`) run
   `mmr --json data query NVDA --bar-size "1 day" --days 900` (or run it against
   the `-B` snapshot) and read the closes around 2024-06-07. About $120 means
   split-adjusted. About $1,200 means not adjusted: **stop**, the Alpaca switch
   would splice two price bases.
3. Put the keys in the repo `.env`: `ALPACA_API_KEY_ID` and
   `ALPACA_API_SECRET_KEY`. Docker compose passes them to the services.
   `alpaca_api_key_id` / `alpaca_api_secret_key` in `~/.config/mmr/trader.yaml`
   also work, because empty env values are ignored.
4. Switch: set `default_data_source: alpaca` in `~/.config/mmr/trader.yaml` and
   change US jobs to `source: alpaca` in `~/.config/mmr/data_refresh.yaml`.
   A US job without Alpaca keys fails loudly (the job and the batch are marked
   failed, no silent fallback).
5. Optional: a one-time forced US refetch (`force: true` on the job) so stored
   history is one source within each job's `days` window. Older stored bars stay
   TwelveData/Massive.

Until step 5, stored US history may be a TwelveData/Massive to Alpaca splice.
The adjustment basis is documented as the same (TwelveData default
`adjust=splits`, Massive `adjusted` = splits only). It has not yet been checked
against stored data in the Docker volume: that is step 2. The vendor still
differs, so small differences at the seam are possible.

Alpaca returns only completed NYSE sessions (after 20:16 ET). TwelveData and
Massive remain opt-in via `--source`. Known quirk, left as is: TwelveData
returns nothing for intraday bars when start == end, so a single-day gap can
be skipped with `--source twelvedata`.

### Movers and news (phase 3a)

From phase 3a, `mmr movers` and `mmr news` default to Alpaca **without any
config edit** (they ignore `default_data_source`). The CLI needs the Alpaca
keys: export `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`, or put them in the
live `~/.config/mmr/trader.yaml` (empty env values no longer blank YAML keys).
To keep Massive: set `data_providers: {movers: massive, news: polygon}` in the
live `trader.yaml`.

Live checks (2026-10-04): all gated Alpaca live tests passed (quotes, news,
movers filter, crypto movers, history). Weekday intraday movers check: not run
(outside US market hours, it was Sunday 08:16 ET).

- TODO (operator): Confirm Alpaca movers `last_updated` is intraday on a weekday (spec 3a check)

### Idea scanner (phase 3b)

From phase 3b, bare `mmr ideas` uses Alpaca **without any config edit** (it
ignores `default_data_source`). It needs the Alpaca keys, like `movers` and
`news`. Discovery is movers + most-actives (about 100-150 symbols, not the full
market) with 15-minute-delayed prices. To keep Massive, set
`data_providers: {ideas: massive}` in the live `trader.yaml`, or pass
`--source massive|twelvedata` per call. `--fundamentals` needs one of those two
until phase 4.

Live checks (2026-10-04, Sunday): the two gated Alpaca ideas tests passed
(momentum scan, explicit tickers with an unknown symbol). Weekday intraday
quality was not checked.

- TODO (operator): The weekday intraday movers check above also gates `ideas` discovery quality (same screener endpoints)

---

### Options (phase 5)

`mmr options expirations|chain|snapshot|implied` default to Alpaca's free
indicative feed **without any config edit** (options ignore
`default_data_source`). Indicative (Alpaca's free, delayed estimate — not
tradable quotes) is not the OPRA NBBO (the official consolidated best
bid/offer): quotes are derived, trades delayed, greeks/IV only on liquid
contracts (on 2026-10-04 AAPL
2026-11-06 calls: 26 of 61 strikes had no usable IV, 35 were used by
`implied`). Rows say `feed: indicative`. To keep Massive: set
`data_providers: {options: massive}` in the live `trader.yaml`. This key's
Massive plan returns NOT_AUTHORIZED for option snapshots (2026-10-04), so
Massive options need a paid options plan, and Massive options are only tested
with fakes. `options buy|sell` still go to IB.

Live checks (2026-10-04, a Sunday, markets closed): 11 gated Alpaca live tests
passed (history, quotes, news, movers, and the five new options tests); the
Massive options test was skipped (no Massive key exported). Real CLI run:
expirations, chain, snapshot (`O:` form), implied and the indicative-feed
table worked. Quotes were the Friday 2026-10-02 close (`quote_time`
19:59:59Z). The CLI `--source massive` call returned the NOT_AUTHORIZED
message with no traceback.

---

## Forex and index movers (phase 6)

`forex convert`, `forex snapshot-all` and `forex movers` default to free ECB daily reference rates (Frankfurter, no key) — one rate per business day about 16:00 CET, labelled with its date, not live. `movers --market indices` defaults to ETF proxies from Alpaca IEX prices (needs the Alpaca keys). `forex snapshot` / `forex quote` still default to IB. Users who relied on `default_data_source: twelvedata` for forex: set `data_providers: {forex: twelvedata}` in the live `~/.config/mmr/trader.yaml`.

- `data_providers.forex: ib` breaks `forex convert` and `forex snapshot-all` (they need a REST forex source).
- `data_providers: {movers: massive}` keeps Massive for index and forex movers too. To set them one by one: `movers_indices: massive` / `movers_forex: massive` (these win over `movers`).
- Massive may return no forex bid/ask; not checked live.
- Checked live on 2026-10-04 (Sunday): Frankfurter returns the Friday 2026-10-02 rate as the latest; Alpaca IEX ETF quotes are Friday's prices.
- Massive forex bid/ask is read from the documented forex snapshot keys (the Massive key is not entitled). To check with an entitled key: `MMR_LIVE_TESTS=1 MASSIVE_API_KEY=... pytest -m live tests/data_providers/test_live_massive_forex.py`.
- TODO (operator): when IB Gateway is up, verify mmr forex snapshot EURUSD and mmr forex quote EUR USD (IB IDEALPRO CASH path; not live-verified in phase 6).

---

## Infrastructure

- Split compose: ib-gateway, trader, strategy, data, dashboard, scheduler.
- Restart policy: `unless-stopped`.
- Paper mode; typed HMAC RPC. Dashboard on host loopback (default 7424).

---

## Next operator session (paper soak)

1. `./docker.sh -b -u` after this polish (baked dashboard image).
2. Confirm `mmr status` → IB upstream connected.
3. Enable **momentum** from `/cc` or `docker compose exec trader … strategies enable momentum`.
4. Scaling: `lifecycle=armed`, operating mode Auto.
5. Leave the stack up through US RTH. Watch intents / protectives / fills.
6. Host monitors: `mmr --json portfolio-snapshot`, `portfolio-diff`, `/cc` Risk bars.

Do not arm a second automatic strategy. Do not set `automation.live_enabled`.

---

## Open items / follow-ups (offline)

- Cluster G (AUDIT_ROADMAP): G1 mass-enable RPC timeout, G2 IB farm-status log noise.
- Historical `expectancy_bps`/PF disagreements: reconcile original trade traces,
  entry notionals and partial exits before alleging a calculation defect (see above).
- Statistical-confidence script needs timeouts/caps before rerun.
- ORB-ASX: consider a proper train/test split before trusting BHP/WDS edges live.
