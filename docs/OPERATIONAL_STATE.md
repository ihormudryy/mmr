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

- **ORB on ASX is marginal universe-wide.** Per-name edge only (BHP, WDS).
- **US ORB:** GOOGL strong; GLD losing.
- **VWAP:** CAT only among names tried.
- Treat return + PF as the reliable metrics until `expectancy_bps` is reconciled.

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
